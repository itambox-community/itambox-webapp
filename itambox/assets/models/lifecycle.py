"""Lifecycle models: AssetDisposal, Warranty, AssetReservation, and DateRange helper."""

import datetime

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Func
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models.choices import (
    DataSanitizationMethodChoices,
    DisposalMethodChoices,
    ReservationStatusChoices,
    WarrantyTypeChoices,
)
from core.currency import CurrencyField
from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager, Scope
from core.mixins import FileAttachmentMixin, JournalingMixin, SoftDeleteMixin
from core.models import BaseModel, ChangeLoggingMixin


class DateRange(Func):
    """SQL ``daterange(start, end, bounds)`` expression for ExclusionConstraint.

    Used to build a half-open ``[)`` daterange over the two DateField columns so
    the overlap (``&&``) operator matches the half-open semantics in
    ``AssetReservation.clean()`` (touching boundaries — one ends the day the next
    starts — do NOT overlap).
    """

    function = "DATERANGE"
    output_field = models.fields.Field()  # daterange; only used inside the constraint


class AssetDisposal(FileAttachmentMixin, JournalingMixin, SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    """End-of-Life / Disposal record with data-sanitization evidence.

    A disposal record IS the evidence. It is never replaced and never deleted by
    a lifecycle operation: correcting a mistaken disposal is an explicit
    cancellation (``cancel_asset_disposal``) that keeps this row visible and
    auditable, and a later disposal creates a NEW row — hence
    ``related_name="disposals"`` (a history), not a one-to-one.

    Tenant-scoped through the parent asset so multi-tenant boundary checks flow
    through the same ``tenant_lookup`` pattern as AssetMaintenance and
    AssetAssignment.

    on_delete=PROTECT is used on the asset FK. Rationale: a disposal record is
    evidence for GDPR Art. 17 / WEEE / SOC 2 audit purposes — deleting the
    linked asset (which itself requires a vault-grade soft-delete) should not
    silently cascade and destroy disposal proof. The operator must explicitly
    cancel the disposal, making the change a deliberate, auditable action.

    Lifecycle ownership: at most ONE row per asset may be active
    (``cancelled_at IS NULL``), enforced by a conditional unique constraint that
    deliberately INCLUDES soft-deleted rows — a tombstone must not silently
    release the asset's disposal state.
    """

    tenant_lookup = "asset__tenant"
    # Children of a tenant-less asset must not be wider than the asset (hidden when global).
    deny_global_tenant = True
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    @property
    def tenant(self):
        return self.asset.tenant if self.asset_id else None

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        related_name="disposals",
        verbose_name=_("Asset"),
    )
    disposal_method = models.CharField(
        max_length=30,
        choices=DisposalMethodChoices.choices,
        default=DisposalMethodChoices.DESTRUCTION,
        verbose_name=_("Disposal Method"),
        db_index=True,
    )
    disposal_date = models.DateField(
        verbose_name=_("Disposal Date"),
        db_index=True,
    )
    data_sanitization_method = models.CharField(
        max_length=30,
        choices=DataSanitizationMethodChoices.choices,
        default=DataSanitizationMethodChoices.NONE,
        verbose_name=_("Data Sanitization Method"),
        help_text=_("NIST SP 800-88 Rev.1 aligned method used to sanitize storage media."),
        db_index=True,
    )
    sanitization_certificate = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Sanitization Certificate / Reference"),
        help_text=_("Certificate serial number or reference ID from the sanitization vendor."),
    )
    sanitized_by = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Sanitized By"),
        help_text=_("Person or vendor who performed the data sanitization."),
    )
    recipient = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Recipient"),
        help_text=_("Buyer, recycler, charity, or other recipient of the disposed asset."),
    )
    proceeds = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        verbose_name=_("Proceeds"),
        help_text=_("Amount received for the asset (resale / salvage). Leave blank if none."),
    )
    currency = CurrencyField()
    weee_compliant = models.BooleanField(
        default=False,
        verbose_name=_("WEEE Compliant"),
        help_text=_("Disposal was carried out by an authorised WEEE recycler."),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    maintenance = models.ForeignKey(
        "assets.AssetMaintenance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disposals",
        verbose_name=_("Repair Maintenance"),
        help_text=_("Optional: the repair maintenance this disposal closes out."),
    )

    # Cancellation: an erroneous disposal is CORRECTED, never erased.
    # The record keeps its identity and stays in the disposal history; these
    # fields are owned exclusively by ``cancel_asset_disposal`` (non-editable, so
    # no ordinary form, serializer or admin field can set them).
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("Cancelled at"))
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="cancelled_asset_disposals",
        verbose_name=_("Cancelled by"),
    )
    cancellation_reason = models.TextField(blank=True, editable=False, verbose_name=_("Cancellation reason"))

    class Meta:
        ordering = ["-disposal_date"]
        verbose_name = _("Asset Disposal")
        verbose_name_plural = _("Asset Disposals")
        permissions = [
            ("dispose_asset", _("Can record asset disposal / end-of-life")),
        ]
        constraints = [
            # One lifecycle-owning record per asset. The condition is deliberately
            # only ``cancelled_at IS NULL``: soft-deleted rows keep occupying the
            # slot so a tombstone cannot silently re-open the asset (their
            # evidence must be resolved visibly, by cancellation).
            models.UniqueConstraint(
                fields=["asset"],
                condition=models.Q(cancelled_at__isnull=True),
                name="uniq_active_disposal_per_asset",
            ),
            # Cancellation state is coherent: a cancelled row names actor and
            # reason, an active row has neither.
            models.CheckConstraint(
                check=(
                    (
                        models.Q(cancelled_at__isnull=False, cancelled_by__isnull=False)
                        & ~models.Q(cancellation_reason="")
                    )
                    | models.Q(cancelled_at__isnull=True, cancelled_by__isnull=True, cancellation_reason="")
                ),
                name="disposal_cancellation_coherent",
            ),
        ]

    def __str__(self):
        return f"Disposal of {self.asset} ({self.get_disposal_method_display()}, {self.disposal_date})"

    def get_absolute_url(self):
        return reverse("assets:assetdisposal_detail", kwargs={"pk": self.pk})

    @property
    def is_active(self) -> bool:
        """True while this record owns the asset's disposal lifecycle.

        A soft-deleted but uncancelled record still owns it: hiding evidence in
        the recycle bin must not release the disposed state.
        """
        return self.cancelled_at is None

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None

    def clean(self):
        super().clean()
        if self.maintenance_id and self.asset_id and self.maintenance.asset.tenant_id != self.asset.tenant_id:
            raise ValidationError({"maintenance": _("The maintenance must belong to the same tenant as the asset.")})
        reason = (self.cancellation_reason or "").strip()
        if self.cancelled_at is not None:
            errors = {}
            if self.cancelled_by is None:
                errors["cancelled_by"] = _("A cancelled disposal must record who cancelled it.")
            if not reason:
                errors["cancellation_reason"] = _("A cancellation reason is required.")
            if errors:
                raise ValidationError(errors)
        elif self.cancelled_by is not None or reason:
            raise ValidationError(
                {"cancellation_reason": _("Cancellation fields can only be set by cancelling the disposal.")}
            )

    def delete(self, *args, force_hard_delete=False, **kwargs):
        """Disposal records are evidence: no ordinary delete, only cancellation.

        ``force_hard_delete=True`` stays available for the explicitly authorized,
        separately permissioned purge path; a plain soft/hard delete would be a
        hidden cancellation or a hidden loss of evidence (#496).
        """
        forced = bool(force_hard_delete) or getattr(self, "_force_hard_delete", False)
        if not forced:
            raise ValidationError(
                _("Disposal records are lifecycle evidence and cannot be deleted. Cancel the disposal instead.")
            )
        return super().delete(*args, force_hard_delete=True, **kwargs)

    def restore(self):
        """Cancelled rows are preserved history; restoring must not create a second active row."""
        if self.cancelled_at is not None:
            raise ValidationError(_("Cancelled disposal records are preserved history and cannot be restored."))
        # ``_base_manager`` on purpose: the clash check must not depend on the
        # ambient tenant/soft-delete lens of whoever clicks restore.
        cls = type(self)
        if (
            self.pk
            and cls._base_manager.filter(asset_id=self.asset_id, cancelled_at__isnull=True).exclude(pk=self.pk).exists()
        ):
            raise ValidationError(_("Restoring this record would leave two active disposals for the same asset."))
        return super().restore()


class Warranty(JournalingMixin, SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    """First-class warranty entity attached to an asset.

    Tenant-scoped through the parent asset (same ``tenant_lookup`` pattern as
    AssetMaintenance and AssetAssignment).
    """

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

    @property
    def tenant(self):
        return self.asset.tenant if self.asset_id else None

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.CASCADE,
        related_name="warranties",
        db_index=True,
        verbose_name=_("Asset"),
    )
    warranty_type = models.CharField(
        max_length=30,
        choices=WarrantyTypeChoices.choices,
        default=WarrantyTypeChoices.HARDWARE,
        verbose_name=_("Warranty Type"),
        db_index=True,
    )
    supplier = models.ForeignKey(
        "assets.Supplier",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="warranties",
        verbose_name=_("Supplier"),
    )
    start_date = models.DateField(verbose_name=_("Start Date"), db_index=True)
    end_date = models.DateField(verbose_name=_("End Date"), db_index=True)
    cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Cost"),
    )
    currency = CurrencyField()
    reference = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Reference"),
        help_text=_("Claim number, policy reference, or contract ID."),
    )
    terms = models.TextField(blank=True, verbose_name=_("Terms"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        ordering = ["end_date"]
        verbose_name = _("Warranty")
        verbose_name_plural = _("Warranties")
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_date__gte=models.F("start_date")),
                name="warranty_end_date_gte_start_date",
            ),
        ]

    def __str__(self):
        return f"{self.get_warranty_type_display()} warranty on {self.asset} ({self.start_date} – {self.end_date})"

    def get_absolute_url(self):
        return reverse("assets:warranty_detail", kwargs={"pk": self.pk})

    @property
    def is_active(self) -> bool:
        """True when today falls within [start_date, end_date]."""
        today = datetime.date.today()
        return self.start_date <= today <= self.end_date


class AssetReservation(JournalingMixin, SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    """Reservation of an asset for a specific holder within a date window."""

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

    @property
    def tenant(self):
        return self.asset.tenant if self.asset_id else None

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.CASCADE,
        related_name="reservations",
        db_index=True,
        verbose_name=_("Asset"),
    )
    reserved_for = models.ForeignKey(
        "organization.AssetHolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_reservations",
        verbose_name=_("Reserved For"),
    )
    start_date = models.DateField(verbose_name=_("Start Date"), db_index=True)
    end_date = models.DateField(verbose_name=_("End Date"), db_index=True)
    status = models.CharField(
        max_length=20,
        choices=ReservationStatusChoices.choices,
        default=ReservationStatusChoices.PENDING,
        db_index=True,
        verbose_name=_("Status"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reservations",
        verbose_name=_("Created By"),
    )
    purpose = models.CharField(max_length=255, blank=True, verbose_name=_("Purpose"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        ordering = ["start_date"]
        verbose_name = _("Asset Reservation")
        verbose_name_plural = _("Asset Reservations")
        constraints = [
            # DB-level guard against double-booking. clean() (Python) is kept as
            # defense-in-depth, but concurrent/programmatic .save() calls bypass
            # it; this exclusion constraint makes overlap impossible at the row
            # level. Requires the btree_gist extension (added in the migration)
            # because it mixes equality (asset) with the overlap operator.
            #
            # Inclusive '[]' daterange: end_date is the LAST day the asset is
            # held, so two reservations that share a boundary day conflict (one
            # holder per calendar day — dates carry no time, so there is no
            # same-day handoff; a gap is required). A one-day reservation has
            # start_date == end_date. Matches clean()'s start_date <=
            # other.end_date AND end_date >= other.start_date test and the
            # end_date >= today "still active" check. Only ACTIVE/PENDING +
            # non-soft-deleted rows participate.
            ExclusionConstraint(
                name="assetreservation_no_overlap",
                expressions=[
                    ("asset", RangeOperators.EQUAL),
                    (
                        DateRange(
                            "start_date",
                            "end_date",
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=models.Q(
                    status__in=[
                        ReservationStatusChoices.ACTIVE,
                        ReservationStatusChoices.PENDING,
                    ],
                )
                & models.Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self):
        holder = self.reserved_for or _("(no holder)")
        return f"{self.asset} reserved for {holder} ({self.start_date} – {self.end_date})"

    def get_absolute_url(self):
        return reverse("assets:assetreservation_detail", kwargs={"pk": self.pk})

    def _overlapping_reservations(self):
        """Return QS of active/pending reservations for the same asset that overlap our window.

        Uses the default manager (``deleted_at__isnull=True``) so a soft-deleted
        reservation never raises a false overlap — matching the DB
        ExclusionConstraint, whose condition already excludes deleted rows.
        """
        qs = AssetReservation.objects.for_scope(Scope.current()).filter(
            asset=self.asset,
            status__in=[ReservationStatusChoices.ACTIVE, ReservationStatusChoices.PENDING],
            start_date__lte=self.end_date,
            end_date__gte=self.start_date,
        )
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError(_("End date must be on or after start date."))

        if (
            self.asset_id
            and self.start_date
            and self.end_date
            and self.status in (ReservationStatusChoices.ACTIVE, ReservationStatusChoices.PENDING)
        ):
            if self._overlapping_reservations().exists():
                raise ValidationError(
                    _(
                        "An active or pending reservation already exists for this asset "
                        "overlapping the requested date window."
                    )
                )
