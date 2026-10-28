"""Per-aggregate archive service for :class:`Tenant` (#619, step 5).

The approved behaviour table for this aggregate (design section 2.8):

* **REFUSE** while the tenant still owns live business records (assets,
  requests, suppliers, inventory, kits, licenses, locations, sites, holders,
  contacts, cost centers, software, subscriptions, procurement, custody
  templates, audit sessions) or manages live child tenants. The refusal is a
  typed :class:`ArchiveBlocked` and nothing has been written when it is raised.
  A full decommission of a tenant stays a separate, non-archive operation.
* **ARCHIVE** the soft configuration rows (roles, user groups, event rules,
  webhook endpoints, report templates, notification channels, alert rules,
  saved filters, asset tag sequences) through each row's own ``save()``. A
  restore brings back exactly the rows archived at the tenant's own timestamp.
* **DETACH** live resource grants in both directions (owner and grantee): they
  are revoked and a restore never re-grants access.
* **HARD DELETE** the regenerable rows (jobs, dashboards, alert logs and report
  generation archives) as an explicit list.
* **KEEP** memberships, API tokens, scheduled reports, role grant scopes,
  journal entries, attachments, webhook deliveries and expiry evidence. Token
  authentication and membership lookups already ignore an archived tenant, so
  these rows are inert while the tenant is archived and active again when it is
  restored; no data is lost by the archive.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from core.archive_handlers import (
    ArchiveBehaviour,
    ArchiveBlocked,
    ArchiveOperation,
    ArchiveRelation,
    ArchiveResult,
    lock_aggregate_root,
)
from core.choices import ObjectChangeActionChoices

from ..models import Tenant, TenantResourceGrant

#: Live business children: archiving the tenant is refused while any exists.
#: ``(model label, plural noun for the refusal message)``.
_BUSINESS_MODELS: tuple[tuple[str, str], ...] = (
    ("assets.asset", "assets"),
    ("assets.assetrequest", "asset requests"),
    ("assets.supplier", "suppliers"),
    ("compliance.auditsession", "audit sessions"),
    ("compliance.custodytemplate", "custody templates"),
    ("inventory.accessory", "accessories"),
    ("inventory.accessorystock", "accessory stock rows"),
    ("inventory.component", "components"),
    ("inventory.componentstock", "component stock rows"),
    ("inventory.consumable", "consumables"),
    ("inventory.consumablestock", "consumable stock rows"),
    ("inventory.kit", "kits"),
    ("licenses.license", "licenses"),
    ("organization.assetholder", "asset holders"),
    ("organization.contact", "contacts"),
    ("organization.costcenter", "cost centers"),
    ("organization.location", "locations"),
    ("organization.site", "sites"),
    ("procurement.contract", "contracts"),
    ("procurement.fulfillmentlink", "fulfillment links"),
    ("procurement.purchaseorder", "purchase orders"),
    ("procurement.purchaseorderline", "purchase order lines"),
    ("software.software", "software"),
    ("subscriptions.subscription", "subscriptions"),
)

#: Soft configuration rows archived with the tenant (restored with it).
_CONFIG_MODELS: tuple[str, ...] = (
    "assets.assettagsequence",
    "compliance.signatureproviderconfig",
    "extras.alertrule",
    "extras.eventrule",
    "extras.notificationchannel",
    "extras.reporttemplate",
    "extras.savedfilter",
    "extras.webhookendpoint",
    "organization.role",
    "users.usergroup",
)

#: Regenerable rows removed for good, as an explicit list.
_REGENERABLE_MODELS: tuple[str, ...] = (
    "core.job",
    "extras.alertlog",
    "extras.dashboard",
    "extras.reportgenerationarchive",
)


_KEEP_RELATIONS: tuple[tuple[str, str], ...] = (
    ("extras.journalentry.tenant", "journal entries stay as evidence"),
    ("extras.reporttemplate.filter_tenants", "report scope links stay; the tenant row is kept"),
    ("extras.scheduledreport.filter_tenants", "schedule scope links stay; the tenant row is kept"),
    ("extras.scheduledreport.tenant", "schedules stay and do not fire while the tenant is archived"),
    ("extras.webhookdelivery.tenant", "delivery history stays as evidence"),
    ("file_attachments", "attachments stay with the tenant"),
    ("image_attachments", "attachments stay with the tenant"),
    ("journal_entries", "journal entries stay as evidence"),
    ("organization.membership.tenant", "memberships are inert while archived and active again on restore"),
    ("organization.rolegrantscope.tenant", "scopes are inert while archived and active again on restore"),
    ("organization.tenantresourcegrantexpiryrun.tenant", "expiry evidence stays"),
    ("users.token.tenant", "token authentication rejects an archived tenant; tokens return on restore"),
)


def archive_relations() -> tuple[ArchiveRelation, ...]:
    """The reviewed behaviour table, derived from the lists the service acts on."""
    rows = [
        ArchiveRelation(f"{label}.tenant", ArchiveBehaviour.REFUSE, "while live rows exist")
        for label, _noun in _BUSINESS_MODELS
    ]
    rows.append(
        ArchiveRelation("organization.tenant.managed_by", ArchiveBehaviour.REFUSE, "while live child tenants exist")
    )
    rows += [
        ArchiveRelation(f"{label}.tenant", ArchiveBehaviour.ARCHIVE, "configuration archived with the tenant")
        for label in _CONFIG_MODELS
    ]
    rows += [
        ArchiveRelation(
            f"{label}.tenant", ArchiveBehaviour.DETACH, "regenerable rows are deleted; restore does not recreate them"
        )
        for label in _REGENERABLE_MODELS
    ]
    rows += [
        ArchiveRelation(
            f"organization.tenantresourcegrant.{field}", ArchiveBehaviour.DETACH, "revoked; restore does not re-grant"
        )
        for field in ("tenant", "grantee_tenant")
    ]
    rows += [ArchiveRelation(key, ArchiveBehaviour.KEEP, note) for key, note in _KEEP_RELATIONS]
    return tuple(rows)


def _lock_tenant(tenant: Tenant) -> Tenant:
    return lock_aggregate_root(tenant, noun="tenant")


def _live_rows(model, **lookup):
    """Live rows of ``model`` matching ``lookup``, across every scope.

    The refusal must see the tenant's rows whatever scope the acting principal
    holds, otherwise a narrower scope would archive a tenant that still owns
    records.
    """
    # unscoped: the tenant archive guard must count live children regardless of the acting scope
    return model._base_manager.filter(deleted_at__isnull=True, **lookup)


def _blocker_counts(tenant: Tenant) -> list[tuple[str, int]]:
    """``(label, count)`` for every REFUSE condition that currently holds."""
    checks = [
        (_(label), _live_rows(apps.get_model(model_label), tenant=tenant).count())
        for model_label, label in _BUSINESS_MODELS
    ]
    checks.append((_("managed tenants"), _live_rows(Tenant, managed_by=tenant).count()))
    return [(label, count) for label, count in checks if count]


def _refusal(tenant: Tenant, blockers: Sequence[tuple[str, int]]) -> ArchiveBlocked:
    total = sum(count for _label, count in blockers)
    detail = ", ".join(f"{count} {label}" for label, count in blockers)
    headline = ngettext(
        "Cannot delete %(object)s: %(count)s dependent record must be resolved first (%(detail)s).",
        "Cannot delete %(object)s: %(count)s dependent records must be resolved first (%(detail)s).",
        total,
    ) % {"object": str(tenant), "count": total, "detail": detail}
    return ArchiveBlocked(headline, blockers=blockers)


def _archive_config_rows(tenant: Tenant, when, operation: ArchiveOperation) -> int:
    message = operation.archive_message()
    archived = 0
    for model_label in _CONFIG_MODELS:
        rows = _live_rows(apps.get_model(model_label), tenant=tenant).select_for_update().order_by("pk")
        for row in rows:
            row.deleted_at = when
            row.archive_operation_id = operation.id
            row._changelog_message = message
            row._changelog_action = ObjectChangeActionChoices.ACTION_DELETE
            row.save(update_fields=["deleted_at", "archive_operation_id"])
            archived += 1
    return archived


def _revoke_grants(tenant: Tenant, operation: ArchiveOperation) -> int:
    """Revoke every live resource grant the tenant owns or receives."""

    # unscoped: grants are revoked in both directions whatever scope the acting principal holds
    grants = (
        TenantResourceGrant._base_manager.select_for_update()
        .filter(deleted_at__isnull=True)
        .filter(Q(tenant=tenant) | Q(grantee_tenant=tenant))
        .order_by("pk")
    )
    revoked = 0
    for grant in grants:
        grant._changelog_message = operation.detach_message()
        grant.delete()
        revoked += 1
    return revoked


def _purge_regenerable_rows(tenant: Tenant) -> None:
    for model_label in _REGENERABLE_MODELS:
        # unscoped: regenerable rows are removed with the tenant whatever scope the acting principal holds
        for row in apps.get_model(model_label)._base_manager.filter(tenant=tenant):
            row.delete()


def archive_tenant(tenant: Tenant, *, actor=None, request=None) -> ArchiveResult:
    """Archive one empty tenant with its configuration rows, or refuse.

    An already-archived tenant is a no-op (``archived=0``).

    :raises ArchiveBlocked: the tenant still owns live business records or
        manages live child tenants.
    """
    with transaction.atomic():
        locked = _lock_tenant(tenant)
        if locked.deleted_at is not None:
            return ArchiveResult()
        blockers = _blocker_counts(locked)
        if blockers:
            raise _refusal(locked, blockers)

        operation = ArchiveOperation.begin(locked)
        when = timezone.now()
        archived_config = _archive_config_rows(locked, when, operation)
        detached = _revoke_grants(locked, operation)
        _purge_regenerable_rows(locked)

        locked.deleted_at = when
        locked._changelog_action = ObjectChangeActionChoices.ACTION_DELETE
        locked.save(update_fields=["deleted_at"])
    return ArchiveResult(archived=1 + archived_config, detached=detached, operation_id=operation.id)


def _archive_operation_id(tenant: Tenant):
    """Operation id recorded on the configuration rows this tenant's archive moved."""
    for model_label in _CONFIG_MODELS:
        # unscoped: the tombstoned rows are invisible to the scoped manager
        marker = (
            apps.get_model(model_label)
            ._base_manager.filter(tenant=tenant, deleted_at=tenant.deleted_at, archive_operation_id__isnull=False)
            .values_list("archive_operation_id", flat=True)
            .first()
        )
        if marker is not None:
            return marker
    return None


def restore_tenant(tenant: Tenant, *, actor=None, request=None) -> None:
    """Bring an archived tenant back with the configuration rows its archive moved.

    Revoked resource grants are not re-granted: access is re-established
    explicitly by the owning tenant.
    """
    with transaction.atomic():
        locked = _lock_tenant(tenant)
        if locked.deleted_at is None:
            return
        if locked.managed_by_id and not Tenant.objects.filter(pk=locked.managed_by_id).exists():
            raise ArchiveBlocked(
                _("Cannot restore %(object)s: its managing tenant is archived or unavailable.")
                % {"object": str(locked)}
            )
        if Tenant.objects.filter(name=locked.name).exists() or Tenant.objects.filter(slug=locked.slug).exists():
            raise ArchiveBlocked(
                _("Cannot restore %(object)s: another tenant already uses its name or slug.") % {"object": str(locked)}
            )
        operation_id = _archive_operation_id(locked)
        for model_label in _CONFIG_MODELS:
            model = apps.get_model(model_label)
            # unscoped: restore must see the tombstoned rows this archive moved, whatever the acting scope
            rows = (
                model._base_manager.select_for_update()
                .filter(tenant=locked, deleted_at__isnull=False, archive_operation_id=operation_id)
                .order_by("pk")
            )
            for row in rows:
                row.deleted_at = None
                row.archive_operation_id = None
                row.save(update_fields=["deleted_at", "archive_operation_id"])
        locked.restore()
