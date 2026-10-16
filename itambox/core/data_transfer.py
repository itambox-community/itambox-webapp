"""Per-model data-transfer policy: one explicit declaration drives import and export.

Every installed model is declared here, in both directions, and nothing is
inferred. A model that is missing from ``DECLARATIONS`` is denied for import and
export alike (fail closed); ``core/tests/test_data_transfer_policy.py`` fails the
build for any such model, so adding a model forces an explicit decision.

``import_`` means "may be written through the generic CSV/YAML importer". It is
only honoured together with a curated, registered ``BulkImportForm`` (see
``core.importers.bulk_forms``); the contract test keeps the two in lockstep.
Lifecycle rows that a domain service owns (assignments, custody receipts and
signing sessions, disposals, seat assignments, reservations, inventory
assignments) are never importable: a raw-field write could put them into states
their service never produces.

``export`` is the single gate for the generic export route
(``/export/<app>/<model>/<template>/``), the list-header export menu and the
Export Template content type picker. An exportable model carries a contract:

* ``scope`` names how rows are restricted, and the route applies exactly that
  and never a bare ``Model.objects``:

  - ``manager``: the default manager is tenant-scoped (``filter_by_tenant``);
  - ``global``: tenantless reference data shared by every tenant;
  - ``container``: rows are narrowed with ``visible_to_containers`` to the
    tenants whose permission the requester holds;
  - ``owner``: rows are narrowed to the requesting user (``owner_field``),
    superusers included.

* The permission is ``view_<model>``; when the model also declares a dedicated
  ``export_<model>`` permission, that one is required too, so the generic gate
  is never weaker than the dedicated export (``required_export_permissions``).

``export_scope=all`` means every row the requester may see under the declared
scope, never every row in the database. Superusers pass the permission and
tenant checks by platform convention; owner scoping still applies to them.

A denied model carries a ``reason`` and answers 404 on every generic path. The
field-level policy stays the existing substring redaction; a per-field export
policy is a recorded follow-up decision.
"""

from dataclasses import dataclass

SCOPE_MANAGER = "manager"
SCOPE_GLOBAL = "global"
SCOPE_CONTAINER = "container"
SCOPE_OWNER = "owner"
EXPORT_SCOPES = frozenset({SCOPE_MANAGER, SCOPE_GLOBAL, SCOPE_CONTAINER, SCOPE_OWNER})

R_DEDICATED = "dedicated export surface is authoritative"
R_AUTHZ = "authorization or identity metadata"
R_PERSONAL = "personal or user-owned data"
R_SYSTEM = "system, framework or generated log record"
R_CONFIG = "configuration without a reviewed export contract"
R_SECRET = "bearer or credential material"
R_NO_SCOPE = "no safe generic scoping strategy"


@dataclass(frozen=True)
class DataTransferPolicy:
    import_: bool = False
    export: bool = False
    scope: str | None = None
    owner_field: str | None = None
    reason: str = "undeclared models are denied"


DENIED = DataTransferPolicy()


def _allow(scope, *, import_=False, owner_field=None):
    return DataTransferPolicy(import_=import_, export=True, scope=scope, owner_field=owner_field, reason="")


def _deny(reason, detail, *, import_=False):
    return DataTransferPolicy(import_=import_, export=False, reason=f"{reason}: {detail}")


DECLARATIONS = {
    # admin
    "admin.logentry": _deny(R_SYSTEM, "framework or third-party system table"),
    # assets
    "assets.asset": _allow(SCOPE_MANAGER, import_=True),
    "assets.assetassignment": _allow(SCOPE_MANAGER),
    "assets.assetdisposal": _allow(SCOPE_MANAGER),
    "assets.assetmaintenance": _allow(SCOPE_MANAGER),
    "assets.assetrequest": _allow(SCOPE_MANAGER),
    "assets.assetreservation": _allow(SCOPE_MANAGER),
    "assets.assetrole": _allow(SCOPE_GLOBAL),
    "assets.assettagsequence": _deny(R_SYSTEM, "internal per-tenant tag-sequence counter"),
    "assets.assettype": _allow(SCOPE_GLOBAL, import_=True),
    "assets.assettypefieldset": _deny(R_CONFIG, "asset-type composition binding, edited through the asset-type form"),
    "assets.assettypeimagestage": _deny(R_NO_SCOPE, "in-flight staging row for an asset-type image upload"),
    "assets.category": _allow(SCOPE_GLOBAL),
    "assets.categorydefaultfieldset": _deny(R_CONFIG, "category default binding, edited through the asset-type form"),
    "assets.depreciation": _allow(SCOPE_GLOBAL),
    "assets.manufacturer": _allow(SCOPE_GLOBAL, import_=True),
    "assets.statuslabel": _allow(SCOPE_GLOBAL),
    "assets.supplier": _allow(SCOPE_MANAGER),
    "assets.warranty": _allow(SCOPE_MANAGER, import_=True),
    # auth
    "auth.group": _deny(R_SYSTEM, "framework or third-party system table"),
    "auth.permission": _deny(R_SYSTEM, "framework or third-party system table"),
    # compliance
    "compliance.assetaudit": _deny(
        R_NO_SCOPE, "default manager is intentionally unscoped; the dedicated API scopes it"
    ),
    "compliance.auditsession": _allow(SCOPE_MANAGER),
    "compliance.custodyhandoffdelivery": _deny(R_SECRET, "handoff delivery rows carry signing-link material"),
    "compliance.custodyreceipt": _deny(
        R_DEDICATED, "signature evidence and a bearer token; only the dedicated custody exports serve it"
    ),
    "compliance.custodysigningsession": _deny(R_SECRET, "signing sessions carry bearer token material"),
    "compliance.custodytemplate": _allow(SCOPE_MANAGER),
    # contenttypes
    "contenttypes.contenttype": _deny(R_SYSTEM, "framework or third-party system table"),
    # core
    "core.emailsettings": _deny(R_SECRET, "mail credentials"),
    "core.job": _deny(R_NO_SCOPE, "visibility is governed by visible_jobs_for_user"),
    "core.notification": _deny(R_PERSONAL, "personal notifications"),
    "core.objectchange": _deny(R_SYSTEM, "audit log"),
    "core.recyclebin": _deny(R_SYSTEM, "system recycle-bin projection"),
    # django_q
    "django_q.failure": _deny(R_SYSTEM, "framework or third-party system table"),
    "django_q.ormq": _deny(R_SYSTEM, "framework or third-party system table"),
    "django_q.schedule": _deny(R_SYSTEM, "framework or third-party system table"),
    "django_q.success": _deny(R_SYSTEM, "framework or third-party system table"),
    "django_q.task": _deny(R_SYSTEM, "framework or third-party system table"),
    # extras
    "extras.alertlog": _deny(R_SYSTEM, "generated log"),
    "extras.alertrule": _deny(R_CONFIG, "UI-only configuration"),
    "extras.bookmark": _allow(SCOPE_OWNER, owner_field="user"),
    "extras.customfield": _deny(R_CONFIG, "custom-field definition, edited through the custom-field form"),
    "extras.customfieldchoice": _deny(R_CONFIG, "custom-field definition, edited through the custom-field form"),
    "extras.customfieldchoicetranslation": _deny(
        R_CONFIG, "shipped vocabulary translation, seeded with the definition"
    ),
    "extras.customfieldtranslation": _deny(R_CONFIG, "shipped vocabulary translation, seeded with the definition"),
    "extras.customfieldsettranslation": _deny(R_CONFIG, "shipped vocabulary translation, seeded with the definition"),
    "extras.customfieldchoiceset": _deny(R_CONFIG, "custom-field definition, edited through the custom-field form"),
    "extras.customfieldset": _deny(R_CONFIG, "custom-field definition, edited through the custom-field form"),
    "extras.customfieldsetfield": _deny(R_CONFIG, "custom-field definition, edited through the custom-field form"),
    "extras.dashboard": _deny(R_PERSONAL, "personal UI layout; plain manager"),
    "extras.event": _deny(R_SYSTEM, "generated event record"),
    "extras.eventrule": _deny(R_CONFIG, "UI-only configuration"),
    "extras.exporttemplate": _deny(R_CONFIG, "superuser-authored template code"),
    "extras.fileattachment": _deny(R_NO_SCOPE, "GFK-backed; served only through tenant-scoped proxies"),
    "extras.imageattachment": _deny(R_NO_SCOPE, "GFK-backed; served only through tenant-scoped proxies"),
    "extras.journalentry": _deny(R_CONFIG, "UI-only; personal authorship"),
    "extras.labeltemplate": _deny(R_CONFIG, "superuser-authored template code"),
    "extras.notificationchannel": _deny(R_SECRET, "channel configuration may hold credentials"),
    "extras.objectwatch": _allow(SCOPE_OWNER, owner_field="user"),
    "extras.reportgenerationarchive": _deny(R_NO_SCOPE, "generated report payloads follow the report permission model"),
    "extras.reporttemplate": _deny(R_CONFIG, "UI-only configuration"),
    "extras.savedfilter": _deny(R_PERSONAL, "private filters are visible only to their creator"),
    "extras.scheduledreport": _deny(R_CONFIG, "UI-only configuration"),
    "extras.scheduledreportfire": _deny(R_SYSTEM, "generated schedule record"),
    "extras.scheduledreportscopeauthorization": _deny(R_AUTHZ, "approval metadata"),
    "extras.tag": _allow(SCOPE_GLOBAL),
    "extras.webhookdelivery": _deny(R_SYSTEM, "generated delivery log"),
    "extras.webhookendpoint": _deny(R_SECRET, "endpoint configuration holds signing secrets"),
    # inventory
    "inventory.accessory": _allow(SCOPE_MANAGER, import_=True),
    "inventory.accessoryassignment": _allow(SCOPE_MANAGER),
    "inventory.accessorystock": _allow(SCOPE_MANAGER),
    "inventory.component": _allow(SCOPE_MANAGER, import_=True),
    "inventory.componentallocation": _allow(SCOPE_MANAGER),
    "inventory.componentstock": _allow(SCOPE_MANAGER),
    "inventory.consumable": _allow(SCOPE_MANAGER, import_=True),
    "inventory.consumableassignment": _allow(SCOPE_MANAGER),
    "inventory.consumablestock": _allow(SCOPE_MANAGER),
    "inventory.kit": _allow(SCOPE_MANAGER),
    "inventory.kititem": _allow(SCOPE_MANAGER),
    # licenses
    "licenses.license": _allow(SCOPE_MANAGER, import_=True),
    "licenses.licenseseatassignment": _allow(SCOPE_MANAGER),
    # organization
    "organization.assetholder": _allow(SCOPE_MANAGER, import_=True),
    "organization.contact": _allow(SCOPE_MANAGER),
    "organization.contactassignment": _deny(R_NO_SCOPE, "GFK-backed assignment on a plain manager"),
    "organization.contactrole": _allow(SCOPE_GLOBAL),
    "organization.costcenter": _allow(SCOPE_MANAGER),
    "organization.location": _allow(SCOPE_MANAGER, import_=True),
    "organization.membership": _allow(SCOPE_CONTAINER),
    "organization.region": _allow(SCOPE_GLOBAL),
    "organization.role": _deny(R_AUTHZ, "role permission sets are authorization metadata"),
    "organization.rolegrant": _deny(R_AUTHZ, "grants are authorization metadata"),
    "organization.rolegrantscope": _deny(R_AUTHZ, "grants are authorization metadata"),
    "organization.site": _allow(SCOPE_MANAGER),
    "organization.sitegroup": _allow(SCOPE_GLOBAL),
    "organization.tenant": _allow(SCOPE_MANAGER),
    "organization.tenantgroup": _allow(SCOPE_MANAGER),
    "organization.tenantresourcegrant": _deny(
        R_AUTHZ, "grants are authorization metadata, not a data-transfer surface"
    ),
    "organization.tenantresourcegrantexpiryrevocation": _deny(R_AUTHZ, "grant lifecycle audit metadata"),
    "organization.tenantresourcegrantexpiryrun": _deny(R_AUTHZ, "grant lifecycle audit metadata"),
    # otp_static
    "otp_static.staticdevice": _deny(R_SYSTEM, "framework or third-party system table"),
    "otp_static.statictoken": _deny(R_SYSTEM, "framework or third-party system table"),
    # otp_totp
    "otp_totp.totpdevice": _deny(R_SYSTEM, "framework or third-party system table"),
    # procurement
    "procurement.contract": _allow(SCOPE_MANAGER),
    "procurement.fulfillmentlink": _allow(SCOPE_MANAGER),
    "procurement.purchaseorder": _allow(SCOPE_MANAGER),
    "procurement.purchaseorderline": _allow(SCOPE_MANAGER),
    # sessions
    "sessions.session": _deny(R_SYSTEM, "framework or third-party system table"),
    # software
    "software.installedsoftware": _allow(SCOPE_MANAGER),
    "software.software": _allow(SCOPE_MANAGER, import_=True),
    # subscriptions
    "subscriptions.subscription": _allow(SCOPE_MANAGER, import_=True),
    "subscriptions.subscriptionassignment": _allow(SCOPE_MANAGER),
    # users
    "users.groupmembership": _deny(R_AUTHZ, "authorization metadata"),
    "users.oidcidentity": _deny(R_AUTHZ, "external identity bindings"),
    "users.token": _deny(R_SECRET, "bearer token digests must never be serialized"),
    "users.user": _deny(R_AUTHZ, "identity records"),
    "users.usergroup": _deny(R_NO_SCOPE, "default manager is intentionally unscoped; access control metadata"),
    "users.userpreference": _deny(R_PERSONAL, "personal preferences"),
}


def policy_for_label(label):
    """Declared policy for ``app_label.model_name``; undeclared models are denied."""
    return DECLARATIONS.get(label, DENIED)


def policy_for(model):
    if model is None:
        return DENIED
    return policy_for_label(model._meta.label_lower)


def required_export_permissions(model):
    """Permissions the generic export gate demands for ``model``.

    Always ``view_<model>``; plus the dedicated ``export_<model>`` permission
    when the model declares one, so the generic surface is never weaker than a
    stricter dedicated export.
    """
    meta = model._meta
    perms = [f"{meta.app_label}.view_{meta.model_name}"]
    dedicated = f"export_{meta.model_name}"
    if any(codename == dedicated for codename, _name in meta.permissions):
        perms.append(f"{meta.app_label}.{dedicated}")
    return perms
