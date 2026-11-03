"""AssetMaintenance — scheduled/completed maintenance records for assets."""

from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models.choices import MaintenanceStatusChoices
from core.currency import CurrencyField
from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager
from core.mixins import (
    CloneableMixin,
    ExportableMixin,
    FileAttachmentMixin,
    ImageAttachmentMixin,
    JournalingMixin,
    SoftDeleteMixin,
    TaggableMixin,
)
from core.models import BaseModel, ChangeLoggingMixin


class AssetMaintenance(
    TaggableMixin,
    CloneableMixin,
    ExportableMixin,
    JournalingMixin,
    ImageAttachmentMixin,
    FileAttachmentMixin,
    SoftDeleteMixin,
    ChangeLoggingMixin,
    BaseModel,
):
    tenant_lookup = "asset__tenant"
    # Children of a tenant-less asset must not be wider than the asset (hidden when global).
    deny_global_tenant = True
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    #: Correlates the row with the aggregate archive operation that moved it
    #: (#619). A restore only brings back rows an operation archived; a row
    #: deleted on its own has no marker and stays deleted. NULL for every
    #: pre-existing row.
    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    MAINTENANCE_TYPE_UPGRADE = "upgrade"
    MAINTENANCE_TYPE_REPAIR = "repair"
    MAINTENANCE_TYPE_CALIBRATION = "calibration"
    MAINTENANCE_TYPE_SOFTWARE_SUPPORT = "software_support"
    MAINTENANCE_TYPE_HARDWARE_SUPPORT = "hardware_support"
    MAINTENANCE_TYPE_CHOICES = [
        (MAINTENANCE_TYPE_UPGRADE, _("Upgrade")),
        (MAINTENANCE_TYPE_REPAIR, _("Repair")),
        (MAINTENANCE_TYPE_CALIBRATION, _("Calibration")),
        (MAINTENANCE_TYPE_SOFTWARE_SUPPORT, _("Software Support")),
        (MAINTENANCE_TYPE_HARDWARE_SUPPORT, _("Hardware Support")),
    ]

    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="maintenances", db_index=True, verbose_name=_("Asset")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    supplier = models.ForeignKey(
        "assets.Supplier", on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("Supplier/Vendor")
    )
    performed_by = models.CharField(max_length=200, blank=True, verbose_name=_("Performed By"))
    maintenance_type = models.CharField(
        max_length=50,
        choices=MAINTENANCE_TYPE_CHOICES,
        default=MAINTENANCE_TYPE_REPAIR,
        verbose_name=_("Maintenance Type"),
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=MaintenanceStatusChoices.choices,
        default=MaintenanceStatusChoices.SCHEDULED,
        db_index=True,
        verbose_name=_("Status"),
    )
    cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name=_("Maintenance Cost")
    )
    currency = CurrencyField()
    start_date = models.DateField(verbose_name=_("Start Date"), db_index=True)
    completion_date = models.DateField(null=True, blank=True, verbose_name=_("Completion Date"), db_index=True)
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    tags = models.ManyToManyField("extras.Tag", related_name="asset_maintenances", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["-start_date"]
        verbose_name = _("Asset Maintenance")
        verbose_name_plural = _("Asset Maintenances")

    def __str__(self):
        return f"{self.get_maintenance_type_display()} on {self.asset.name}"

    def get_absolute_url(self):
        return reverse("assets:assetmaintenance_detail", kwargs={"pk": self.pk})

    @property
    def downtime_days(self):
        if self.start_date and self.completion_date:
            return (self.completion_date - self.start_date).days
        return None
