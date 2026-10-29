from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.managers import (
    ExplicitScopeAllObjectsManager,
    ExplicitScopeSoftDeleteManager,
    Scope,
)
from core.mixins import CustomFieldDataMixin, SoftDeleteMixin
from core.models import BaseModel, ChangeLoggingMixin, DeletableVaultModel
from extras.models import Tag


class SoftwareCategoryChoices(models.TextChoices):
    OPERATING_SYSTEM = "operating_system", "Operating System"
    PRODUCTIVITY = "productivity", "Productivity"
    DEVELOPMENT = "development", "Development"
    SECURITY = "security", "Security"
    DESIGN = "design", "Design"
    OTHER = "other", "Other"


class SoftwareLicenseTypeChoices(models.TextChoices):
    PROPRIETARY = "proprietary", "Proprietary"
    OPEN_SOURCE = "open_source", "Open Source"
    FREEWARE = "freeware", "Freeware"
    SHAREWARE = "shareware", "Shareware"
    SUBSCRIPTION = "subscription", "Subscription"


class Software(CustomFieldDataMixin, DeletableVaultModel):
    # Tenant-scoped catalogue. A null tenant denotes a shared/global entry that
    # is visible to every tenant (allow_global_tenant); a tenant-set entry is
    # private to that tenant. This closes the cross-tenant software exposure
    # (the software report compiler previously returned all tenants' rows).
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    """
    Represents a catalog entry for a software product.
    """
    tenant = models.ForeignKey(
        to="organization.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="software",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("Owning tenant. Null denotes a shared/global catalogue entry visible to all tenants."),
    )
    name = models.CharField(
        max_length=255,
        verbose_name=_("Name"),
        help_text=_("Name of the software product (e.g., Microsoft Visio Professional 2021). Unique per tenant."),
    )
    manufacturer = models.ForeignKey(
        to="assets.Manufacturer",
        on_delete=models.PROTECT,
        related_name="software_products",
        verbose_name=_("Manufacturer"),
    )
    version = models.CharField(
        max_length=50, blank=True, verbose_name=_("Version"), help_text=_("Current version (e.g., 2021, 16.0)")
    )
    category = models.CharField(
        max_length=50,
        choices=SoftwareCategoryChoices.choices,
        blank=True,
        db_index=True,
        verbose_name=_("Category"),
        help_text=_("Functional category"),
    )
    license_type = models.CharField(
        max_length=50,
        choices=SoftwareLicenseTypeChoices.choices,
        blank=True,
        verbose_name=_("License Type"),
        help_text=_("Default license type"),
    )
    website = models.URLField(blank=True, verbose_name=_("Website"), help_text=_("Product homepage or vendor URL"))
    description = models.TextField(
        blank=True, verbose_name=_("Description"), help_text=_("Optional description of the software product.")
    )
    tags = models.ManyToManyField(to=Tag, blank=True, related_name="software", verbose_name=_("Tags"))

    class Meta:
        ordering = ("manufacturer", "name")
        verbose_name = _("Software")
        verbose_name_plural = _("Software")
        constraints = [
            # Active (non-soft-deleted) names are unique within a tenant, and
            # separately unique among global (null-tenant) entries.
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(deleted_at__isnull=True, tenant__isnull=False),
                name="unique_tenant_software_name",
            ),
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(deleted_at__isnull=True, tenant__isnull=True),
                name="unique_global_software_name",
            ),
        ]

    def __str__(self):
        return f"{self.manufacturer.name} - {self.name}"

    def get_absolute_url(self):
        return reverse("software:software_detail", kwargs={"pk": self.pk})

    @property
    def installed_count(self):
        if hasattr(self, "_installed_count"):
            return self._installed_count
        return InstalledSoftware.objects.for_scope(Scope.current()).filter(software=self).count()

    @property
    def license_count(self):
        if hasattr(self, "_license_count"):
            return self._license_count
        # Resolved through the app registry: ``licenses.models`` imports this
        # module for the ``License.software`` FK, so a static import here would
        # close a cycle. The registry returns the same class and therefore the
        # same tenant-scoped manager.
        License = apps.get_model("licenses", "License")
        return License.objects.for_scope(Scope.current()).filter(software=self, deleted_at__isnull=True).count()


class InstalledSoftware(SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    """
    Represents an instance of software discovered or inventoried on a specific asset.
    Distinct from license assignment/tracking.
    """

    # Tenant scoping via asset FK — asset is non-null (no blank=True/null=True),
    # so every row is always associated with a tenant through its asset.
    tenant_lookup = "asset__tenant"
    # ChangeLoggingMixin reads changelog_tenant_lookup (NOT tenant_lookup, which
    # only scopes the manager) — set both so audit rows are attributed to the
    # asset's tenant instead of the ambient request tenant.
    changelog_tenant_lookup = "asset__tenant"
    # An install is a leaf row of the Asset aggregate: archiving an asset archives
    # its installs with it (and restores them with it), so the model carries its
    # own `deleted_at` and the soft-delete-aware default manager.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    #: Correlates the row with the aggregate archive operation that moved it
    #: (#619). A restore only brings back rows an operation archived; a row
    #: deleted on its own has no marker and stays deleted. NULL for every
    #: pre-existing row.
    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    asset = models.ForeignKey(
        to="assets.Asset",
        on_delete=models.CASCADE,
        related_name="installed_software",
        db_index=True,
        verbose_name=_("Asset"),
    )
    software = models.ForeignKey(
        to=Software, on_delete=models.PROTECT, related_name="installed_instances", verbose_name=_("Software")
    )
    version_detected = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Version Detected"),
        help_text=_("Specific version discovered on the asset (e.g., 16.78.1)"),
    )
    install_date = models.DateField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Install Date"),
        help_text=_("Estimated or known installation date"),
    )
    discovered_by_agent = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Discovered By"),
        help_text=_("Identifier for the discovery source or agent (e.g., SCCM, Intune, Lansweeper)"),
    )
    last_seen_date = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Last Seen Date"),
        help_text=_("Timestamp when this software was last detected on the asset"),
    )
    notes = models.TextField(
        blank=True, verbose_name=_("Notes"), help_text=_("Optional notes specific to this installation")
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "software", "version_detected"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_asset_software_version",
            )
        ]
        ordering = ["asset", "software", "-last_seen_date"]
        verbose_name = _("Installed Software Instance")
        verbose_name_plural = _("Installed Software Instances")

    def __str__(self):
        version_part = f" (v{self.version_detected})" if self.version_detected else ""
        return f"{self.software.name}{version_part} on {self.asset.name}"

    def get_absolute_url(self):
        return self.asset.get_absolute_url()

    def clean(self):
        super().clean()
        # A tenant-owned software product can only be installed on assets of the
        # same tenant. Global (null-tenant) software is usable everywhere.
        if (
            self.software_id
            and self.asset_id
            and self.software.tenant_id is not None
            and self.software.tenant_id != self.asset.tenant_id
        ):
            raise ValidationError(
                {
                    "software": _("Selected software belongs to a different tenant than the asset."),
                }
            )
