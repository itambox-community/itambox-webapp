from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _

from itambox.capabilities import (
    ALWAYS_ON,
    CAPABILITY_REGISTRY_DOC_URL,
    CONTRACT_VERSION,
    RESOURCE_GRANT_SECURITY_DOC_URL,
    SOURCE_ALWAYS,
    STABLE,
    Capability,
    registry,
)
from itambox.labels import LabelField, LabelSpec, label_registry


class OrganizationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "organization"

    def ready(self):
        # Import search indexes to register them
        import organization.search
        import organization.signals  # noqa: F401 -- side-effect import registers organization signal receivers

        # inline imports: app-registry: domain modules load only after the app registry is ready.
        from core.archive_handlers import ArchiveBehaviour, ArchiveRelation, register_archive_handler
        from core.identity_provisioning import configure_identity_provisioner
        from core.restore_authority import configure_restore_authority_validator
        from core.tenant_access import configure_tenant_access_policy
        from core.tenant_scope import register_tenant_scope_provider
        from organization.access import (
            accessible_tenant_ids_with_expiry,
            managed_accessible_tenant_ids,
        )
        from organization.models import AssetHolder, Location, Tenant
        from organization.rbac import (
            applicable_grants,
            build_accessible_tenant_permissions_map,
            effective_permissions_with_expiry,
        )
        from organization.services.archive import archive_holder, restore_holder
        from organization.services.identity_provisioning import organization_identity_provisioner
        from organization.services.location_archive import archive_location, restore_location
        from organization.services.restore_authority import organization_restore_authority
        from organization.services.tenant_access import organization_tenant_access_policy
        from organization.services.tenant_archive import archive_relations, archive_tenant, restore_tenant

        configure_tenant_access_policy(organization_tenant_access_policy)
        configure_identity_provisioner(organization_identity_provisioner)
        configure_restore_authority_validator(organization_restore_authority)
        register_archive_handler(
            AssetHolder._meta.label_lower,
            archive=archive_holder,
            restore=restore_holder,
            relations=(
                ArchiveRelation(
                    "assets.assetassignment.assigned_user", ArchiveBehaviour.REFUSE, "while active; closed rows KEEP"
                ),
                ArchiveRelation("assets.assetrequest.assigned_user", ArchiveBehaviour.REFUSE, "while open"),
                ArchiveRelation("assets.assetreservation.reserved_for", ArchiveBehaviour.REFUSE, "while live"),
                ArchiveRelation("compliance.custodyreceipt.holder", ArchiveBehaviour.KEEP, "receipts stay as evidence"),
                ArchiveRelation(
                    "compliance.custodysigningsession.intended_holder", ArchiveBehaviour.REFUSE, "while pending"
                ),
                ArchiveRelation("inventory.accessoryassignment.assigned_holder", ArchiveBehaviour.REFUSE, "while open"),
                ArchiveRelation("inventory.componentallocation.assigned_holder", ArchiveBehaviour.REFUSE, "while open"),
                ArchiveRelation(
                    "inventory.consumableassignment.assigned_holder", ArchiveBehaviour.REFUSE, "while open"
                ),
                ArchiveRelation("journal_entries", ArchiveBehaviour.KEEP, "journal entries stay as evidence"),
                ArchiveRelation(
                    "licenses.licenseseatassignment.assigned_holder", ArchiveBehaviour.REFUSE, "while the seat is held"
                ),
                ArchiveRelation(
                    "subscriptions",
                    ArchiveBehaviour.DETACH,
                    "subscription assignments are ended; restore does not re-attach",
                ),
            ),
        )
        register_archive_handler(
            Tenant._meta.label_lower,
            archive=archive_tenant,
            restore=restore_tenant,
            relations=archive_relations(),
        )
        register_archive_handler(
            Location._meta.label_lower,
            archive=archive_location,
            restore=restore_location,
            relations=(
                ArchiveRelation("assets.asset.location", ArchiveBehaviour.REFUSE, "while live assets reference it"),
                ArchiveRelation(
                    "assets.assetassignment.assigned_location",
                    ArchiveBehaviour.REFUSE,
                    "while active; closed rows KEEP",
                ),
                ArchiveRelation(
                    "assets.assetrequest.source_location", ArchiveBehaviour.DETACH, "open requests; closed rows KEEP"
                ),
                ArchiveRelation(
                    "assets.assetrequest.assigned_location", ArchiveBehaviour.DETACH, "open requests; closed rows KEEP"
                ),
                ArchiveRelation(
                    "inventory.componentstock.location",
                    ArchiveBehaviour.ARCHIVE,
                    "refused while qty > 0; empty rows archived with the location",
                ),
                ArchiveRelation(
                    "inventory.accessorystock.location",
                    ArchiveBehaviour.ARCHIVE,
                    "refused while qty > 0; empty rows archived with the location",
                ),
                ArchiveRelation(
                    "inventory.consumablestock.location",
                    ArchiveBehaviour.ARCHIVE,
                    "refused while qty > 0; empty rows archived with the location",
                ),
                ArchiveRelation(
                    "inventory.componentallocation.assigned_location",
                    ArchiveBehaviour.REFUSE,
                    "while open; closed rows KEEP",
                ),
                ArchiveRelation(
                    "inventory.componentallocation.from_location",
                    ArchiveBehaviour.REFUSE,
                    "while open; closed rows KEEP",
                ),
                ArchiveRelation(
                    "inventory.accessoryassignment.assigned_location",
                    ArchiveBehaviour.REFUSE,
                    "while open; closed rows KEEP",
                ),
                ArchiveRelation(
                    "inventory.accessoryassignment.from_location",
                    ArchiveBehaviour.REFUSE,
                    "while open; closed rows KEEP",
                ),
                ArchiveRelation(
                    "inventory.consumableassignment.assigned_location",
                    ArchiveBehaviour.REFUSE,
                    "while open; closed rows KEEP",
                ),
                ArchiveRelation(
                    "inventory.consumableassignment.from_location",
                    ArchiveBehaviour.REFUSE,
                    "while open; closed rows KEEP",
                ),
                ArchiveRelation(
                    "compliance.auditsession.location", ArchiveBehaviour.DETACH, "open sessions; finished rows KEEP"
                ),
                ArchiveRelation(
                    "compliance.assetaudit.location", ArchiveBehaviour.KEEP, "audit results stay as evidence"
                ),
                ArchiveRelation(
                    "organization.location.parent", ArchiveBehaviour.REFUSE, "while live child locations exist"
                ),
                ArchiveRelation(
                    "procurement.purchaseorder.destination_location",
                    ArchiveBehaviour.REFUSE,
                    "while an open order targets it",
                ),
                ArchiveRelation("subscriptions", ArchiveBehaviour.DETACH, "subscription assignments are ended"),
                ArchiveRelation("journal_entries", ArchiveBehaviour.KEEP, "journal entries stay as evidence"),
            ),
        )
        register_tenant_scope_provider(
            accessible_tenant_ids_with_expiry=accessible_tenant_ids_with_expiry,
            managed_accessible_tenant_ids=managed_accessible_tenant_ids,
            applicable_grants=applicable_grants,
            build_accessible_tenant_permissions_map=build_accessible_tenant_permissions_map,
            resolve_effective_permissions_with_expiry=effective_permissions_with_expiry,
        )

        post_migrate.connect(self._register_expiry_schedule, sender=self)
        self._register_capabilities()

        # Location labels go on rooms, racks and shelves; audit sessions bound to a
        # location start from them.
        label_registry.register(
            Location,
            LabelSpec(
                fields=(
                    LabelField("site", _("Site"), lambda location: location.site),
                    LabelField("status", _("Status"), lambda location: location.get_status_display()),
                ),
                title=lambda location: location.name,
            ),
        )

    def _register_expiry_schedule(self, sender, **kwargs):
        # inline imports: app-registry: schedule models and helpers load after migrations/apps are ready.
        from django_q.models import Schedule

        # inline import: app-registry: schedule helpers load after migrations/apps are ready.
        from core.schedules import register_schedule

        register_schedule(
            "core.tasks.resource_grants.coordinate_resource_grant_expiry",
            defaults={
                "name": "Hourly Resource Grant Expiry Sweep",
                "schedule_type": Schedule.HOURLY,
                "repeats": -1,
            },
        )

    def _register_capabilities(self):
        registry.register_all(self._capabilities())

    def _capabilities(self):
        return (
            Capability(
                key="organization.role_grants",
                title="Role Grants",
                owning_area="area:auth-rbac",
                maturity=STABLE,
                security_critical=True,
                # Security-critical by declaration, and therefore always-on by
                # construction: the registry refuses a probe here, so no
                # deployment state can ever report the authorization path off.
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("organization.RoleGrant",),
                docs_url=CAPABILITY_REGISTRY_DOC_URL,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
            Capability(
                key="organization.resource_grants",
                title="Tenant Resource Grants",
                owning_area="area:auth-rbac",
                maturity=STABLE,
                security_critical=True,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=(
                    "organization.TenantResourceGrant",
                    "organization.TenantResourceGrantExpiryRun",
                    "organization.TenantResourceGrantExpiryRevocation",
                ),
                docs_url=RESOURCE_GRANT_SECURITY_DOC_URL,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
        )
