"""AssetAssignment — checkout/checkin records linking assets to holders/locations."""

import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.choices import StatusTypeChoices
from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager
from core.mixins import JournalingMixin, SoftDeleteMixin, TaggableMixin
from core.models import BaseModel, ChangeLoggingMixin


class AssetAssignment(SoftDeleteMixin, JournalingMixin, TaggableMixin, ChangeLoggingMixin, BaseModel):
    # Tenant is derived from the parent asset; scope through it so assignments
    # cannot be listed or mutated across tenant boundaries.
    tenant_lookup = "asset__tenant"
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    #: Correlates the row with the aggregate archive operation that moved it
    #: (#619). A restore only brings back rows an operation archived; a row
    #: deleted on its own has no marker and stays deleted. NULL for every
    #: pre-existing row.
    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    @property
    def tenant(self):
        return self.asset.tenant if self.asset_id else None

    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.CASCADE, related_name="assignments", db_index=True, verbose_name=_("Asset")
    )
    assigned_user = models.ForeignKey(
        "organization.AssetHolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_assignments",
        verbose_name=_("Assigned User"),
    )
    assigned_location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_assignments",
        verbose_name=_("Assigned Location"),
    )
    assigned_asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_assignments",
        verbose_name=_("Assigned Asset"),
    )
    pre_checkout_status = models.ForeignKey(
        "assets.StatusLabel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignment_pre_checkouts",
        verbose_name=_("Pre-checkout Status"),
        help_text=_("Preserved status label to revert to upon checkin."),
    )

    checked_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checkouts",
        verbose_name=_("Checked Out By"),
    )
    checked_out_at = models.DateTimeField(default=timezone.now, verbose_name=_("Checked Out At"))
    expected_checkin_date = models.DateField(null=True, blank=True, verbose_name=_("Expected Checkin Date"))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Is Active"))
    checked_in_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Checked In At"))
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checkins",
        verbose_name=_("Checked In By"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    tags = models.ManyToManyField("extras.Tag", related_name="asset_assignments", blank=True, verbose_name=_("Tags"))

    # Loaner-specific fields
    is_loan = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Is Loan"),
        help_text=_("Mark this assignment as a temporary loan with a mandatory return date."),
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Due Date"),
        help_text=_("Mandatory return date for loaner assets."),
    )
    returned_at = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Returned At"),
        help_text=_("Date the loaned asset was physically returned."),
    )
    maintenance = models.ForeignKey(
        "assets.AssetMaintenance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name=_("Repair Maintenance"),
        help_text=_("Optional: the repair maintenance this loan or handover belongs to."),
    )

    class Meta:
        ordering = ["-checked_out_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset"],
                # Also exclude soft-deleted rows: a soft-deleted assignment keeps is_active=True,
                # so without this a deleted-but-active row occupies the asset's unique slot and
                # a fresh checkout fails with a raw IntegrityError (500).
                condition=models.Q(is_active=True) & models.Q(deleted_at__isnull=True),
                name="unique_active_assignment_per_asset",
            ),
            models.UniqueConstraint(
                fields=["maintenance"],
                condition=models.Q(is_loan=True, is_active=True, maintenance__isnull=False, deleted_at__isnull=True),
                name="unique_open_loan_per_maintenance",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(assigned_user__isnull=False, assigned_location__isnull=True, assigned_asset__isnull=True)
                    | models.Q(assigned_user__isnull=True, assigned_location__isnull=False, assigned_asset__isnull=True)
                    | models.Q(assigned_user__isnull=True, assigned_location__isnull=True, assigned_asset__isnull=False)
                    | models.Q(assigned_user__isnull=True, assigned_location__isnull=True, assigned_asset__isnull=True)
                ),
                name="exactly_one_assignment_target",
            ),
        ]
        verbose_name = _("Asset Assignment")
        verbose_name_plural = _("Asset Assignments")

    def clean(self):
        super().clean()
        if self.maintenance_id and self.asset_id and self.maintenance.asset.tenant_id != self.asset.tenant_id:
            raise ValidationError({"maintenance": _("The maintenance must belong to the same tenant as the asset.")})
        targets = [self.assigned_user, self.assigned_location, self.assigned_asset]
        filled = [t for t in targets if t is not None]
        if self.is_active:
            if not filled:
                raise ValidationError(
                    _(
                        "Either assigned_user, assigned_location, or assigned_asset must be provided for an active assignment."
                    )
                )
            if len(filled) > 1:
                raise ValidationError(_("Select only one assignment target."))

            if self.asset_id and (not self.asset.status_id or self.asset.status.type != StatusTypeChoices.DEPLOYED):
                raise ValidationError(_("An active assignment requires the asset to have deployed status."))

            # Tenant boundary validation
            target = filled[0]
            if target and hasattr(target, "tenant") and target.tenant != self.asset.tenant:
                raise ValidationError(_("Assignment target must belong to the same tenant as the asset."))

    @property
    def assigned_target(self):
        return self.assigned_user or self.assigned_location or self.assigned_asset

    @property
    def assigned_to(self):
        return self.assigned_target

    @property
    def assigned_to_type(self):
        if self.assigned_user:
            return "assetholder"
        if self.assigned_location:
            return "location"
        if self.assigned_asset:
            return "asset"
        return None

    @property
    def is_overdue(self) -> bool:
        """True when this is an unreturned loan whose due date has passed."""
        if not self.is_loan:
            return False
        if self.returned_at:
            return False
        if not self.due_date:
            return False

        return datetime.date.today() > self.due_date

    def __str__(self):
        return f"{self.asset} → {self.assigned_target} ({'active' if self.is_active else 'inactive'})"

    def get_absolute_url(self):
        return self.asset.get_absolute_url()
