from django.core.exceptions import ValidationError
from django.db import models, router, transaction
from django.db.models import CheckConstraint, Q
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from assets.models import Asset
from core.crypto import decrypt_string, encrypt_string
from core.currency import CurrencyField
from core.managers import AllObjectsManager, ExplicitScopeSoftDeleteManager, TenantScopingSoftDeleteQuerySet
from core.mixins import BookmarkableMixin, CustomFieldDataMixin, SoftDeleteMixin
from core.models import BaseModel, ChangeLoggingMixin, DeletableVaultModel
from extras.models import Tag
from organization.models import AssetHolder
from software.models import Software


class LicenseTypeChoices(models.TextChoices):
    PERPETUAL_SEAT = "perpetual_seat", _("Perpetual Seat")
    SUBSCRIPTION_SEAT = "subscription_seat", _("Subscription Seat")
    # Add others like 'Device', 'User CAL', 'Processor', 'Core' if needed later


class LicenseQuerySet(TenantScopingSoftDeleteQuerySet):
    def with_counts(self):
        from django.db.models import Count

        # Only count *active* (non-soft-deleted) assignments. A bare
        # Count('assignments') joins the raw table and counts checked-in seats as
        # occupied, disagreeing with the related-manager path in available_seats.
        return self.annotate(
            assigned_count=Count(
                "assignments",
                filter=Q(assignments__deleted_at__isnull=True),
            )
        )


class SoftDeleteLicenseManager(ExplicitScopeSoftDeleteManager.from_queryset(LicenseQuerySet)):
    pass


class AllObjectsLicenseManager(AllObjectsManager.from_queryset(LicenseQuerySet)):
    pass


class License(CustomFieldDataMixin, BookmarkableMixin, DeletableVaultModel):
    """Represents the specific entitlement/purchase record for software."""

    # product_key is stored encrypted (enc$…); keep even the ciphertext out of
    # the changelog JSON so it does not accumulate in every ObjectChange row.
    _change_logging_excluded_fields = ["updated_at", "product_key"]

    name = models.CharField(
        max_length=255,
        verbose_name=_("Name"),
        help_text=_("Descriptive name for the license (e.g., Visio Pro 2021 - EA Renewal FY24)"),
    )
    software = models.ForeignKey(
        to=Software, on_delete=models.PROTECT, related_name="licenses", verbose_name=_("Software")
    )
    license_type = models.CharField(
        max_length=50,
        choices=LicenseTypeChoices.choices,
        default=LicenseTypeChoices.PERPETUAL_SEAT,
        db_index=True,
        verbose_name=_("License Type"),
    )
    product_key = models.TextField(
        blank=True,
        verbose_name=_("Product Key"),
        help_text=_("Product key or activation code. Consider security implications."),
    )
    seats = models.PositiveIntegerField(
        default=1, verbose_name=_("Seats"), help_text=_("Total number of seats purchased or entitled")
    )
    purchase_date = models.DateField(blank=True, null=True, db_index=True, verbose_name=_("Purchase Date"))
    purchase_cost = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True, verbose_name=_("Purchase Cost")
    )
    currency = CurrencyField(verbose_name=_("Currency"))
    order_number = models.CharField(max_length=100, blank=True, verbose_name=_("Order Number"))
    version = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Version"),
        help_text=_(
            "Optional version constraint for this license entitlement (e.g. '2021', '16.x'). "
            "Informational only — reconciliation is performed at the Software level (version-agnostic)."
        ),
    )
    expiration_date = models.DateField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Expiration Date"),
        help_text=_("For term licenses or maintenance"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    tags = models.ManyToManyField(Tag, blank=True, related_name="licenses", verbose_name=_("Tags"))
    supplier = models.ForeignKey(
        "assets.Supplier",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="licenses",
        db_index=True,
        verbose_name=_("Supplier"),
    )
    cost_center = models.ForeignKey(
        "organization.CostCenter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="licenses",
        db_index=True,
        verbose_name=_("Cost Center"),
    )
    subscription = models.ForeignKey(
        "subscriptions.Subscription",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="licenses",
        db_index=True,
        verbose_name=_("Subscription"),
        help_text=_("Optional subscription (billing agreement) that funds this license; seats roll up to it."),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="licenses",
        db_index=True,
        verbose_name=_("Tenant"),
    )

    objects = SoftDeleteLicenseManager()
    all_objects = AllObjectsLicenseManager()

    class Meta:
        ordering = ("software__manufacturer", "software__name", "name")
        verbose_name = _("License")
        verbose_name_plural = _("Licenses")

    def __str__(self):
        return f"{self.software.name} - {self.name} ({self.seats} seats)"

    def clean(self):
        super().clean()
        # A license may only reference software in its own tenant (or a global,
        # null-tenant catalogue entry). Prevents a tenant from entitling against
        # another tenant's software product.
        if self.software_id and self.software.tenant_id is not None and self.software.tenant_id != self.tenant_id:
            raise ValidationError(
                {
                    "software": _("Selected software belongs to a different tenant."),
                }
            )
        # The funding subscription must belong to the same tenant.
        if (
            self.subscription_id
            and self.subscription.tenant_id is not None
            and self.subscription.tenant_id != self.tenant_id
        ):
            raise ValidationError(
                {
                    "subscription": _("Selected subscription belongs to a different tenant."),
                }
            )
        # Seats cannot be reduced below the number of currently-assigned seats (which would
        # silently leave the pool over-allocated with no audit signal).
        if self.pk and self.seats is not None:
            self.assert_seat_capacity(seats=self.seats)

    def assert_seat_capacity(self, seats=None):
        """Raise ValidationError when the proposed seat count is below the number of active assignments.

        Called from clean() (form/admin path) and from LicenseSerializer.validate()
        (REST PATCH/PUT path) so the invariant is enforced on every write path.
        ``seats`` defaults to self.seats when not provided.
        """
        # LicenseSeatAssignment is defined later in this module; it resolves from the
        # module globals at call time, so no import is needed here.
        proposed = seats if seats is not None else self.seats
        if not self.pk or proposed is None:
            return
        assigned = LicenseSeatAssignment.all_objects.filter(
            license=self,
            deleted_at__isnull=True,
        ).count()
        if proposed < assigned:
            raise ValidationError(
                {
                    "seats": _("Cannot set the seat count to %(seats)s because %(n)s seats are already assigned.")
                    % {
                        "seats": proposed,
                        "n": assigned,
                    },
                }
            )

    def get_absolute_url(self):
        try:
            return reverse("licenses:license_detail", kwargs={"pk": self.pk})
        except NoReverseMatch:
            # Fallback to Django admin change view if front-end view is not yet defined
            return reverse("admin:licenses_license_change", args=[self.pk])

    @property
    def decrypted_product_key(self):
        """Returns the decrypted product key value, handling plaintext fallback transparently."""
        return decrypt_string(self.product_key)

    def save(self, *args, **kwargs):
        """Symmetrically encrypt product key before saving to the database."""
        if self.product_key and not self.product_key.startswith("enc$"):
            self.product_key = encrypt_string(self.product_key)
        update_fields = kwargs.get("update_fields")
        if self.pk and not self._state.adding and (update_fields is None or "seats" in update_fields):
            # Seat reductions must be validated against fresh state under the same
            # parent-row lock checkout_license() takes, or a concurrent checkout can
            # slip in between validation and write and leave assignments > seats.
            with transaction.atomic(using=kwargs.get("using") or router.db_for_write(type(self), instance=self)):
                stored = type(self).all_objects.select_for_update().filter(pk=self.pk).values_list("seats", flat=True)
                stored_seats = stored.first()
                if stored_seats is not None and self.seats is not None and self.seats < stored_seats:
                    self.assert_seat_capacity(seats=self.seats)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def available_seats(self):
        """Calculate the number of unassigned seats, using annotation if available to avoid N+1 queries."""
        assigned_count = getattr(self, "assigned_count", None)
        if assigned_count is None:
            assigned_count = self.assignments.count()
        return max(0, self.seats - assigned_count)


class LicenseSeatAssignment(SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    # No direct `tenant` field: a seat derives its tenant from its license.
    # `tenant_lookup` lets TenantScopingSoftDeleteManager scope queries through
    # the FK, and the `tenant` property lets DRF StrictTenantPermission enforce
    # the object-level boundary on detail/mutation endpoints.
    tenant_lookup = "license__tenant"
    # A seat is never shared catalogue: a global (tenant=None) license is an
    # anomaly a tenant admin can mint, so do NOT expose its seats cross-tenant.
    # Opt out of the default "global-parent children stay visible" behaviour.
    deny_global_tenant = True

    objects = ExplicitScopeSoftDeleteManager()
    all_objects = AllObjectsManager()

    @property
    def tenant(self):
        return self.license.tenant if self.license_id else None

    """Tracks the explicit assignment of one license seat to an asset or holder."""
    license = models.ForeignKey(
        to=License, on_delete=models.PROTECT, related_name="assignments", db_index=True, verbose_name=_("License")
    )
    # CASCADE (not SET_NULL): a seat targets exactly one of asset/holder
    # (chk_assignment_to_one_target). Hard-deleting the target releases the seat
    # back to the pool; SET_NULL would leave an asset+holder-null row that
    # violates the constraint.
    asset = models.ForeignKey(
        to=Asset,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="license_assignments",
        db_index=True,
        verbose_name=_("Asset"),
    )
    assigned_holder = models.ForeignKey(
        to=AssetHolder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="license_assignments",
        db_index=True,
        verbose_name=_("Assigned Holder"),
    )
    # Optional precise link: an asset-assigned seat may point at the exact
    # InstalledSoftware row it covers (seat-level SAM).  Only valid when the
    # seat is asset-assigned (holder seats have no associated install), and the
    # install must be on the same asset — enforced in clean().
    installed_software = models.ForeignKey(
        "software.InstalledSoftware",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="covering_seats",
        db_index=True,
        verbose_name=_("Installed Software"),
    )
    assigned_date = models.DateTimeField(auto_now_add=True, editable=False)
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        ordering = ("license", "asset", "assigned_holder")
        verbose_name = _("License Seat Assignment")
        verbose_name_plural = _("License Seat Assignments")
        constraints = [
            CheckConstraint(
                check=Q(asset__isnull=False, assigned_holder__isnull=True)
                | Q(asset__isnull=True, assigned_holder__isnull=False),
                name="chk_assignment_to_one_target",
            ),
            # A target (asset or holder) may hold at most ONE active seat on a given
            # license — a hard DB backstop against the same target consuming multiple
            # seats (the API create path also checks this for a friendly error).
            models.UniqueConstraint(
                fields=["license", "asset"],
                condition=Q(asset__isnull=False, deleted_at__isnull=True),
                name="unique_active_license_seat_per_asset",
            ),
            models.UniqueConstraint(
                fields=["license", "assigned_holder"],
                condition=Q(assigned_holder__isnull=False, deleted_at__isnull=True),
                name="unique_active_license_seat_per_holder",
            ),
        ]

    def __str__(self):
        target = self.asset or self.assigned_holder
        return f"Seat for {self.license} assigned to {target or 'Unspecified'}"

    def get_absolute_url(self):
        # Usually link back to the license itself
        return self.license.get_absolute_url()

    def clean(self):
        """Ensure assignment is to either asset or holder, not both or neither.

        Also validates the optional ``installed_software`` link:
        - It may only be set on asset-assigned seats (not holder-assigned seats).
        - The install's asset must match the seat's asset (same physical machine).
        """
        super().clean()
        if self.asset and self.assigned_holder:
            raise ValidationError(
                _("A license seat can only be assigned to an Asset OR an Asset Holder, not both."),
                code="invalid_assignment",
            )
        if not self.asset and not self.assigned_holder:
            raise ValidationError(
                _("A license seat must be assigned to either an Asset or an Asset Holder."), code="missing_assignment"
            )
        if self.installed_software_id is not None:
            if self.assigned_holder_id is not None:
                raise ValidationError(
                    {
                        "installed_software": _(
                            "An install link can only be set on asset-assigned seats, not on holder-assigned seats."
                        )
                    },
                    code="install_link_holder_seat",
                )
            if self.asset_id is not None and self.installed_software.asset_id != self.asset_id:
                raise ValidationError(
                    {"installed_software": _("The linked install must be on the same asset as this seat assignment.")},
                    code="install_asset_mismatch",
                )
