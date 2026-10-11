"""Asset state machine and the core Asset model."""

import datetime
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from assets.choices import StatusTypeChoices
from assets.model_book_value import compute_book_value
from assets.models.audit_cadence import add_calendar_months, is_audit_overdue
from assets.models.tagsequence import AssetTagSequence
from core.currency import CurrencyField
from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager
from core.mixins import BookmarkableMixin, CustomFieldDataMixin, SubscribableMixin
from core.models import DeletableVaultModel


class AssetStateMachine:
    ALLOWED_TRANSITIONS = {
        "pending": ["deployable", "undeployable", "archived", "on_order", "in_repair"],
        "deployable": ["pending", "undeployable", "archived", "deployed", "in_repair"],
        "deployed": ["deployable", "undeployable", "archived", "pending", "in_repair"],
        "undeployable": ["pending", "deployable", "archived", "in_repair"],
        "archived": ["pending"],
        "in_repair": ["deployable", "undeployable", "archived"],
        # 'archived' is reachable from every non-terminal state: disposal is a
        # terminal action that may occur from any lifecycle point, including an
        # on-order asset cancelled/written off before it ever arrives.
        "on_order": ["pending", "deployable", "archived"],
    }

    @staticmethod
    def validate_transition(current_status_type, new_status_type, is_checked_out, *, managed_deployed_transition=False):
        if current_status_type == new_status_type:
            return
        if not managed_deployed_transition and (
            current_status_type == StatusTypeChoices.DEPLOYED or new_status_type == StatusTypeChoices.DEPLOYED
        ):
            raise ValidationError(_("The deployed status is managed by checkout and check-in."))
        if new_status_type not in AssetStateMachine.ALLOWED_TRANSITIONS.get(current_status_type, []):
            raise ValidationError(
                _("Illegal state transition from %(current)s to %(new)s")
                % {"current": current_status_type, "new": new_status_type}
            )
        if is_checked_out and new_status_type in ["undeployable", "archived"]:
            raise ValidationError(
                _("Cannot mark an actively checked-out asset as undeployable or archived. Check it in first.")
            )


class Asset(CustomFieldDataMixin, BookmarkableMixin, SubscribableMixin, DeletableVaultModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    # Changing the Asset Type changes the effective fieldsets, field
    # definitions, required fields, and Choice Set validity, so a field-limited
    # save that touches only ``asset_type`` must still run the dynamic
    # custom-field validator against the target composition.
    custom_field_data_validation_dependencies = frozenset({"asset_type"})

    # NOTE: asset status is a FK to StatusLabel; the lifecycle vocabulary is
    # StatusLabel.type (assets.choices.StatusTypeChoices), not a local choice set.

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    asset_tag = models.CharField(max_length=50, blank=True, verbose_name=_("Asset Tag"))
    serial_number = models.CharField(max_length=100, blank=True, db_index=True, verbose_name=_("Serial Number"))
    asset_type = models.ForeignKey(
        "assets.AssetType",
        on_delete=models.PROTECT,
        related_name="assets",
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Asset Type"),
    )
    asset_role = models.ForeignKey(
        "assets.AssetRole",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="assets",
        db_index=True,
        verbose_name=_("Asset Role"),
    )
    purchase_date = models.DateField(blank=True, null=True, db_index=True, verbose_name=_("Purchase Date"))

    # Procurement metadata.
    purchase_cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name=_("Purchase Cost")
    )
    current_book_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Current Book Value"),
        help_text=_("Materialized depreciation value"),
    )
    depreciation_updated_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Depreciation Updated At"))
    order_number = models.CharField(max_length=100, blank=True, verbose_name=_("Order Number"))
    supplier = models.ForeignKey(
        "assets.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("Supplier"),
        db_index=True,
    )
    salvage_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name=_("Salvage Value")
    )
    currency = CurrencyField()
    status = models.ForeignKey(
        "assets.StatusLabel",
        on_delete=models.PROTECT,
        related_name="assets",
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Status"),
    )
    location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="assets",
        db_index=True,
        verbose_name=_("Location"),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="assets",
        db_index=True,
        verbose_name=_("Tenant"),
    )
    purchase_order_line = models.ForeignKey(
        "procurement.PurchaseOrderLine",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="assets",
        db_index=True,
        verbose_name=_("Purchase Order Line"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    tags = models.ManyToManyField("extras.Tag", related_name="assets", blank=True, verbose_name=_("Tags"))
    last_audited = models.DateTimeField(null=True, blank=True, verbose_name=_("Last Audited"), db_index=True)
    last_audited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audited_assets",
        verbose_name=_("Last Audited By"),
    )
    # custom_field_data JSONField comes from CustomFieldDataMixin
    requestable = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        db_index=True,
        verbose_name=_("Requestable"),
        help_text=_("Allow users to request this asset"),
    )
    depreciation_override = models.ForeignKey(
        "assets.Depreciation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_overrides",
        verbose_name=_("Depreciation override"),
        help_text=_("Override depreciation policy — leave empty to use the tenant default or asset-type schedule."),
    )
    in_service_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("In-service date"),
        help_text=_("Depreciation starts here; falls back to purchase date."),
    )
    cost_center = models.ForeignKey(
        "organization.CostCenter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("Cost Center"),
        db_index=True,
    )
    disposed_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("Disposed at"))
    disposal_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Sign-off value"),
    )

    @property
    def current_warranty_end(self):
        """Max end_date among all currently active warranties, or None."""
        today = datetime.date.today()
        result = (
            self.warranties.filter(start_date__lte=today, end_date__gte=today, deleted_at__isnull=True)
            .order_by("-end_date")
            .values_list("end_date", flat=True)
            .first()
        )
        return result

    @property
    def is_requestable(self):
        if self.requestable is not None:
            return self.requestable
        return self.asset_type.requestable if self.asset_type else False

    @property
    def manufacturer(self):
        return self.asset_type.manufacturer if self.asset_type else None

    @property
    def model(self):
        return self.asset_type.model if self.asset_type else None

    @property
    def audit_due_date(self):
        """Date by which the next physical audit is due, or None if no cadence is set.

        Never-audited assets with a cadence are overdue immediately (returns created_at).
        Calendar-month additions clamp month-end dates to the destination month's last day.
        """
        category = self.category
        if not category or not category.audit_interval_months:
            return None

        base = self.last_audited or self.created_at
        return add_calendar_months(base, category.audit_interval_months)

    @property
    def audit_overdue(self) -> bool:
        """True when the current local date is later than the audit's due date."""
        due = self.audit_due_date
        return due is not None and is_audit_overdue(due, timezone.localdate())

    def get_status_display(self):
        return self.status.name if self.status else _("Not set")

    @property
    def eol_date(self):
        if self.purchase_date and self.asset_type and self.asset_type.eol_months:
            from dateutil.relativedelta import relativedelta

            # relativedelta clamps month-end overflow (Jan 31 + 1 month = Feb 28/29).
            return self.purchase_date + relativedelta(months=self.asset_type.eol_months)
        return None

    @property
    def time_to_eol(self):
        eol = self.eol_date
        if eol:
            from dateutil.relativedelta import relativedelta

            today = datetime.date.today()
            if today >= eol:
                return str(_("Expired"))

            delta = relativedelta(eol, today)
            parts = []
            if delta.years > 0:
                parts.append(ngettext("%(count)s year", "%(count)s years", delta.years) % {"count": delta.years})
            if delta.months > 0:
                parts.append(ngettext("%(count)s month", "%(count)s months", delta.months) % {"count": delta.months})
            return ", ".join(parts) or str(_("Less than a month"))
        return str(_("Not set"))

    @property
    def total_cost_of_ownership(self):
        cost = self.purchase_cost or Decimal("0.00")
        maintenance_cost = sum(m.cost or Decimal("0.00") for m in self.maintenances.all())
        return cost + maintenance_cost

    @property
    def current_value(self):
        """Estimated book value — delegates to the pure compute_book_value function."""
        return compute_book_value(self)

    @property
    def is_modular(self):
        if self.component_allocations.filter(deleted_at__isnull=True).exists():
            return True
        return bool(self.asset_role and self.asset_role.allows_components)

    @property
    def active_assignment(self):
        prefetched = getattr(self, "prefetched_active_assignments", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.assignments.filter(is_active=True).first()

    @classmethod
    def issuable(cls):
        """Return live assets eligible for a new checkout or issuance."""
        assignment_model = apps.get_model("assets", "AssetAssignment")
        active_assignments = assignment_model.all_objects.filter(asset_id=OuterRef("pk"), is_active=True)
        queryset = cls.objects.filter(status__type=StatusTypeChoices.DEPLOYABLE).filter(~Exists(active_assignments))
        return cls.exclude_disposed(queryset)

    @property
    def has_active_assignment(self):
        """Whether any non-cancelled assignment still owns this asset."""
        if not self.pk:
            return False
        assignment_model = apps.get_model("assets", "AssetAssignment")
        return assignment_model.all_objects.filter(asset_id=self.pk, is_active=True).exists()

    @property
    def is_issuable(self):
        """Match the centralized ``issuable()`` queryset for one asset."""
        return bool(
            self.status_id
            and self.status.type == StatusTypeChoices.DEPLOYABLE
            and not self.has_active_assignment
            and not self.is_disposed
        )

    @property
    def assigned_to(self):
        active = self.active_assignment
        return active.assigned_target if active else None

    @property
    def active_disposal(self):
        """The disposal record that owns this asset's lifecycle, or None (#496).

        Mirrors ``active_assignment``: an explicitly prefetched list wins, so
        list views can avoid a per-row query.
        """
        prefetched = getattr(self, "prefetched_active_disposals", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        # Canonical definition (matches the DB constraint and active_disposal_filter): an
        # UNCANCELLED record owns the lifecycle even when it sits in the recycle bin, so the
        # default soft-delete-filtering reverse manager must not be used here. The model is
        # resolved through the app registry: no import edge, no claimed cycle.
        disposal_model = apps.get_model("assets", "AssetDisposal")
        return disposal_model.all_objects.filter(asset_id=self.pk, cancelled_at__isnull=True).first()

    @property
    def is_disposed(self) -> bool:
        """True while an active disposal record owns this asset."""
        return self.active_disposal is not None

    @staticmethod
    def active_disposal_filter(prefix: str = "") -> models.Q:
        """Q matching assets whose disposal is owned by a record (#496).

        Cancelled records are preserved history and never satisfy it. A
        soft-deleted but uncancelled record still does: hiding evidence in the
        recycle bin must not release the disposed state.

        ``disposals__isnull=False`` is required: an ``exclude()`` over a
        nullable relation is rendered as a LEFT OUTER JOIN inside a NOT EXISTS
        subquery, and without the existence guard the joined NULL row makes the
        ``cancelled_at IS NULL`` predicate true for assets that have no disposal
        record at all (which would drop every asset from the candidate set).
        """
        return models.Q(**{f"{prefix}disposals__isnull": False, f"{prefix}disposals__cancelled_at__isnull": True})

    @classmethod
    def exclude_disposed(cls, queryset, prefix: str = ""):
        """Drop assets with an active disposal record from a candidate queryset."""
        return queryset.exclude(cls.active_disposal_filter(prefix))

    @property
    def category(self):
        return self.asset_type.category if self.asset_type else None

    class Meta:
        verbose_name = _("Asset")
        verbose_name_plural = _("Assets")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "asset_tag"], condition=models.Q(tenant__isnull=False), name="unique_tenant_asset_tag"
            ),
            models.UniqueConstraint(
                fields=["asset_tag"], condition=models.Q(tenant__isnull=True), name="unique_global_asset_tag"
            ),
            models.UniqueConstraint(
                fields=["tenant", "serial_number"],
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(serial_number=""),
                name="uniq_active_tenant_serial",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.asset_tag})"

    def get_absolute_url(self):
        """Return the canonical URL for the asset."""
        return reverse("assets:asset_detail", kwargs={"pk": self.pk})

    def _validate_assignment_status(self):
        if self.status_id and self.status.type == StatusTypeChoices.DEPLOYED:
            operation = getattr(self, "_asset_status_transition", None)
            if not self.pk or (not self.has_active_assignment and operation != "checkout"):
                raise ValidationError(_("The deployed status is managed by checkout and check-in."))
        if (
            self.pk
            and self.has_active_assignment
            and (not self.status_id or self.status.type != StatusTypeChoices.DEPLOYED)
        ):
            raise ValidationError(_("An active assignment requires the asset to have deployed status."))

    @staticmethod
    def _is_managed_deployed_transition(operation, old_type, new_type):
        return (
            operation == "checkout"
            and new_type == StatusTypeChoices.DEPLOYED
            and old_type != StatusTypeChoices.DEPLOYED
        ) or (
            operation == "checkin" and old_type == StatusTypeChoices.DEPLOYED and new_type != StatusTypeChoices.DEPLOYED
        )

    def _validate_disposal_status_transition(self, old_type, new_type):
        if old_type != StatusTypeChoices.ARCHIVED or new_type == StatusTypeChoices.ARCHIVED:
            return
        if self.disposals(manager="all_objects").filter(cancelled_at__isnull=True).exists():
            raise ValidationError(
                _(
                    "This asset has an active disposal record. Cancel the disposal "
                    "before moving it out of the archived state."
                )
            )

    def _validate_status_transition(self):
        if not self.pk or not self.status_id:
            return
        old_asset = Asset._base_manager.filter(pk=self.pk).first()
        if not old_asset or not old_asset.status_id or old_asset.status_id == self.status_id:
            return
        old_type = old_asset.status.type
        new_type = self.status.type
        operation = getattr(self, "_asset_status_transition", None)
        managed_transition = self._is_managed_deployed_transition(operation, old_type, new_type)
        uses_deployed_status = old_type == StatusTypeChoices.DEPLOYED or new_type == StatusTypeChoices.DEPLOYED
        if uses_deployed_status and not managed_transition:
            raise ValidationError(_("The deployed status is managed by checkout and check-in."))
        AssetStateMachine.validate_transition(
            old_type,
            new_type,
            self.has_active_assignment,
            managed_deployed_transition=managed_transition,
        )
        self._validate_disposal_status_transition(old_type, new_type)

    def clean(self):
        super().clean()

        # B2: tenant-boundary integrity for tenant-owned FKs. A tenant-scoped
        # asset may only reference Locations / CostCenters / PurchaseOrderLines in
        # its own tenant; global (null-tenant) rows are shared and always allowed.
        # Closes the cross-tenant FK assignment hole the import-frozen form
        # querysets left open. This runs on every save via the global
        # validate_custom_validators_on_save pre_save signal, so it also guards
        # the API/import/GraphQL paths, not just the AssetForm.
        if self.tenant_id is not None:
            fk_errors = {}
            for fk_field_name in ("location", "cost_center", "purchase_order_line"):
                related = getattr(self, fk_field_name, None)
                if related is not None and related.tenant_id is not None and related.tenant_id != self.tenant_id:
                    fk_errors[fk_field_name] = _("Selected %(field)s belongs to a different tenant than the asset.") % {
                        "field": self._meta.get_field(fk_field_name).verbose_name
                    }
            if fk_errors:
                raise ValidationError(fk_errors)

        self._validate_assignment_status()
        self._validate_status_transition()

    def _prepare_asset_tag(self, update_fields):
        # A specification-only save must not reserve or change unrelated tags.
        if update_fields is not None and "asset_tag" not in update_fields:
            return
        if not self.asset_tag:
            self.asset_tag = AssetTagSequence.get_next_tag_for_asset(self)
        else:
            seq = AssetTagSequence.resolve_sequence_for_asset(self)
            if seq and self.asset_tag == seq.next_tag_preview:
                seq.next_tag()

    def save(self, *args, **kwargs):
        self._prepare_asset_tag(kwargs.get("update_fields"))

        # Freeze/unfreeze sign-off value on archive transition.
        # State-machine transition validation is NOT done here: it lives in
        # clean(), which the global `validate_custom_validators_on_save` pre_save
        # signal (core/signals.py) runs on every ChangeLoggingMixin save — so any
        # status change via save() is validated exactly once, before mutation.
        if self.pk:
            old = Asset._base_manager.filter(pk=self.pk).select_related("status").first()
            if old:
                old_type = old.status.type if old.status else None
                new_type = self.status.type if self.status else None
                if old_type != "archived" and new_type == "archived":
                    self.disposal_value = compute_book_value(self) or Decimal("0.00")
                    self.disposed_at = timezone.now()
                elif old_type == "archived" and new_type != "archived":
                    self.disposed_at = None
                    self.disposal_value = None

        super().save(*args, **kwargs)
