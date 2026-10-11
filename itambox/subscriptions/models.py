from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.currency import CurrencyField
from core.managers import (
    ExplicitScopeAllObjectsManager,
    ExplicitScopeSoftDeleteManager,
)
from core.mixins import (
    AutoSlugMixin,
    BookmarkableMixin,
    CustomFieldDataMixin,
    SoftDeleteMixin,
)
from core.models import BaseModel, ChangeLoggingMixin, DeletableVaultModel
from extras.models import Tag


class SubscriptionTypeChoices(models.TextChoices):
    SAAS = "saas", _("SaaS")
    SUPPORT = "support", _("Support")
    MAINTENANCE = "maintenance", _("Maintenance")
    LEASE = "lease", _("Lease")
    OTHER = "other", _("Other")


class SubscriptionStatusChoices(models.TextChoices):
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")
    CANCELLED = "cancelled", _("Cancelled")
    EXPIRED = "expired", _("Expired")


class BillingCycleChoices(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    QUARTERLY = "quarterly", _("Quarterly")
    ANNUAL = "annual", _("Annual")
    BIANNUAL = "biannual", _("Biannual")
    MULTI_YEAR = "multi_year", _("Multi-Year")
    ONETIME = "onetime", _("One-Time")


class Subscription(CustomFieldDataMixin, AutoSlugMixin, BookmarkableMixin, DeletableVaultModel):
    export_aliases = {"auto_renewal": "vendor_contract_auto_renews"}
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    """Represents a recurring service agreement (SaaS, Support, etc.)."""
    name = models.CharField(
        max_length=255,
        verbose_name=_("Name"),
        help_text=_("Descriptive name (e.g., Adobe Creative Cloud - All Apps (Team))"),
    )
    slug = models.SlugField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name=_("Slug"),
        help_text=_("URL-friendly identifier (auto-generated from name if left blank)"),
    )
    supplier = models.ForeignKey(
        "assets.Supplier",
        on_delete=models.PROTECT,
        related_name="subscriptions",
        verbose_name=_("Supplier"),
        help_text=_("The commercial vendor of this subscription."),
    )
    type = models.CharField(
        max_length=50,
        choices=SubscriptionTypeChoices.choices,
        default=SubscriptionTypeChoices.SAAS,
        verbose_name=_("Subscription Type"),
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatusChoices.choices,
        default=SubscriptionStatusChoices.ACTIVE,
        verbose_name=_("Status"),
        db_index=True,
    )
    start_date = models.DateField(
        blank=True,
        null=True,
        verbose_name=_("Start Date"),
        db_index=True,
    )
    renewal_date = models.DateField(
        blank=True,
        null=True,
        verbose_name=_("Next Renewal Date"),
        db_index=True,
    )
    renewal_reminder_cycle_date = models.DateField(blank=True, null=True, editable=False)
    renewal_reminder_days = models.PositiveSmallIntegerField(blank=True, null=True, editable=False)
    renewal_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("Renewal Cost"),
        help_text=_("Cost per renewal period"),
    )
    currency = CurrencyField()
    billing_cycle = models.CharField(
        max_length=20,
        choices=BillingCycleChoices.choices,
        default=BillingCycleChoices.ANNUAL,
        blank=True,
        verbose_name=_("Billing Cycle"),
    )
    term_months = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name=_("Term (Months)"),
        help_text=_("Duration of the subscription term in months"),
    )
    vendor_contract_auto_renews = models.BooleanField(
        default=True,
        verbose_name=_("Vendor Contract Auto-Renews"),
        help_text=_(
            "Records whether the vendor's contract renews automatically. ITAMbox does not renew subscriptions: "
            "a subscription past its renewal date is marked expired."
        ),
    )
    licensed_quantity = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name=_("Agreement Entitled Quantity"),
        help_text=_(
            "Number of seats, users, or devices entitled by the vendor agreement. "
            "This value is independent of linked License seats."
        ),
    )
    contract_reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Contract Reference"),
        help_text=_("Contract number, PO reference, or quote ID"),
    )
    linked_contract = models.ForeignKey(
        "procurement.Contract",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_subscriptions",
        verbose_name=_("Linked Contract"),
        help_text=_("Optional procurement contract this subscription relates to."),
    )
    cost_center = models.ForeignKey(
        "organization.CostCenter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscriptions",
        verbose_name=_("Cost Center"),
        help_text=_("Financial cost center responsible for this subscription"),
        db_index=True,
    )
    cancellation_date = models.DateField(
        blank=True,
        null=True,
        verbose_name=_("Cancellation Date"),
        db_index=True,
    )
    owner = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_subscriptions",
        verbose_name=_("Owner"),
        help_text=_("Person responsible for this subscription"),
    )
    description = models.TextField(
        blank=True, verbose_name=_("Description"), help_text=_("Optional text detailing coverage or terms")
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"), help_text=_("Optional internal notes"))
    tags = models.ManyToManyField(to=Tag, blank=True, related_name="subscriptions", verbose_name=_("Tags"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="subscriptions_org",
        db_index=True,
        verbose_name=_("Tenant"),
    )

    class Meta:
        ordering = ("-renewal_date", "supplier", "name")
        verbose_name = _("Subscription")
        verbose_name_plural = _("Subscriptions")
        constraints = [
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_subscription_slug_active"
            ),
        ]

    ALLOWED_STATUS_TRANSITIONS = {
        SubscriptionStatusChoices.ACTIVE: {
            SubscriptionStatusChoices.SUSPENDED,
            SubscriptionStatusChoices.CANCELLED,
            SubscriptionStatusChoices.EXPIRED,
        },
        SubscriptionStatusChoices.SUSPENDED: {
            SubscriptionStatusChoices.ACTIVE,
            SubscriptionStatusChoices.CANCELLED,
            SubscriptionStatusChoices.EXPIRED,
        },
        SubscriptionStatusChoices.EXPIRED: {
            SubscriptionStatusChoices.ACTIVE,
            SubscriptionStatusChoices.CANCELLED,
        },
        SubscriptionStatusChoices.CANCELLED: set(),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._loaded_status = self.__dict__.get("status")

    def __str__(self):
        return f"{self.supplier} - {self.name}"

    @property
    def total_seats(self):
        """Total entitled seats across all licenses funded by this subscription."""
        from django.db.models import Sum

        return (
            self.licenses.model._base_manager.filter(subscription=self, deleted_at__isnull=True).aggregate(
                total=Sum("seats")
            )["total"]
            or 0
        )

    def get_absolute_url(self):
        try:
            return reverse("subscriptions:subscription_detail", kwargs={"pk": self.pk})
        except NoReverseMatch:
            return reverse("admin:subscriptions_subscription_change", args=[self.pk])

    @property
    def is_expired(self):
        """Check if the subscription is past its renewal date."""
        if self.renewal_date:
            from django.utils import timezone

            return self.renewal_date < timezone.now().date()
        return False

    @property
    def days_until_renewal(self):
        """Number of days until the next renewal. Negative if overdue."""
        if self.renewal_date:
            from django.utils import timezone

            return (self.renewal_date - timezone.now().date()).days
        return None

    @property
    def annual_cost(self):
        """Estimated annual cost for the billing cycle (issue #501).

        Monthly, quarterly, biannual, and annual cycles scale the per-period
        cost to a year. A multi-year term is annualized as
        ``renewal_cost * 12 / term_months``; without ``term_months`` no yearly
        figure can be derived and ``None`` is returned instead of a misleading
        one. A one-time cost never recurs, so it is not placed in the annual
        slot either (``None``); the detail page labels the amount as the
        one-time cost. A recorded zero stays a real 0.00 (free), distinct from
        ``None`` (no cost recorded).
        """
        if self.renewal_cost is None:
            return None
        if self.billing_cycle == BillingCycleChoices.MONTHLY:
            return self.renewal_cost * 12
        elif self.billing_cycle == BillingCycleChoices.QUARTERLY:
            return self.renewal_cost * 4
        elif self.billing_cycle == BillingCycleChoices.BIANNUAL:
            return self.renewal_cost * 2
        elif self.billing_cycle == BillingCycleChoices.MULTI_YEAR:
            if not self.term_months:
                return None
            # An unrefreshed instance can still carry a raw assigned int/float;
            # normalize via str() so the money math sees the intended decimal
            # value rather than the binary float expansion.
            cost = self.renewal_cost if isinstance(self.renewal_cost, Decimal) else Decimal(str(self.renewal_cost))
            return (cost * 12 / self.term_months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        elif self.billing_cycle == BillingCycleChoices.ONETIME:
            return None
        return self.renewal_cost

    @property
    def auto_renewal(self):
        """Deprecated 1.x compatibility alias for vendor contract renewal terms."""
        return self.vendor_contract_auto_renews

    @auto_renewal.setter
    def auto_renewal(self, value):
        self.vendor_contract_auto_renews = value

    def validate_transition(self, target_status, source_status=None):
        source_status = source_status or self.status
        if target_status == source_status:
            return
        allowed = self.ALLOWED_STATUS_TRANSITIONS.get(source_status, set())
        if target_status not in allowed:
            raise ValidationError(
                _("Invalid subscription status transition from %(source)s to %(target)s.")
                % {"source": source_status, "target": target_status}
            )

    def clean(self):
        super().clean()
        if self.supplier_id and self.supplier.tenant_id is not None and self.supplier.tenant_id != self.tenant_id:
            raise ValidationError({"supplier": _("Selected supplier belongs to a different tenant.")})
        if (
            self.linked_contract_id
            and self.linked_contract.tenant_id is not None
            and self.linked_contract.tenant_id != self.tenant_id
        ):
            raise ValidationError({"linked_contract": _("Selected contract belongs to a different tenant.")})
        if not self.pk:
            return
        previous_status = type(self)._base_manager.filter(pk=self.pk).values_list("status", flat=True).first()
        if previous_status is not None:
            self.validate_transition(self.status, source_status=previous_status)

    def save(self, *args, **kwargs):
        if self._state.adding or self.pk is None:
            result = super().save(*args, **kwargs)
            self._loaded_status = self.status
            return result
        with transaction.atomic():
            current = type(self)._base_manager.select_for_update().get(pk=self.pk)
            if self._loaded_status != current.status and self.status == current.status:
                self.cancellation_date = current.cancellation_date
                self.renewal_date = current.renewal_date
                self.renewal_cost = current.renewal_cost
                self.notes = current.notes
            self._prechange_snapshot = None
            result = super().save(*args, **kwargs)
            self._loaded_status = self.status
            return result

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        if fields is None or "status" in fields:
            self._loaded_status = self.status

    def _refresh_from_locked_row(self):
        if self.pk is not None:
            type(self)._base_manager.select_for_update().only("pk").get(pk=self.pk)
            self.refresh_from_db()
            self._prechange_snapshot = None

    def renew(self, new_renewal_date, cost=None):
        with transaction.atomic():
            self._refresh_from_locked_row()
            if (
                self.status == SubscriptionStatusChoices.ACTIVE
                and self.renewal_date == new_renewal_date
                and (cost is None or self.renewal_cost == cost)
            ):
                return False
            self.validate_transition(SubscriptionStatusChoices.ACTIVE)
            self.renewal_date = new_renewal_date
            if cost is not None:
                self.renewal_cost = cost
            self.status = SubscriptionStatusChoices.ACTIVE
            self.save(update_fields=["renewal_date", "renewal_cost", "status", "updated_at"])
            return True

    def cancel(self, cancellation_date=None, reason=""):
        with transaction.atomic():
            self._refresh_from_locked_row()
            if self.status == SubscriptionStatusChoices.CANCELLED:
                return False
            self.validate_transition(SubscriptionStatusChoices.CANCELLED)
            self.cancellation_date = cancellation_date or timezone.now().date()
            self.status = SubscriptionStatusChoices.CANCELLED
            if reason:
                existing = self.notes or ""
                self.notes = f"{existing}\n[{timezone.now().date()}] Cancelled: {reason}".strip()
            self.save(update_fields=["cancellation_date", "status", "notes", "updated_at"])
            return True

    def suspend(self):
        with transaction.atomic():
            self._refresh_from_locked_row()
            if self.status == SubscriptionStatusChoices.SUSPENDED:
                return False
            self.validate_transition(SubscriptionStatusChoices.SUSPENDED)
            self.status = SubscriptionStatusChoices.SUSPENDED
            self.save(update_fields=["status", "updated_at"])
            return True

    def resume(self):
        with transaction.atomic():
            self._refresh_from_locked_row()
            if self.status == SubscriptionStatusChoices.ACTIVE:
                return False
            self.validate_transition(SubscriptionStatusChoices.ACTIVE)
            self.status = SubscriptionStatusChoices.ACTIVE
            self.save(update_fields=["status", "updated_at"])
            return True

    def expire(self):
        with transaction.atomic():
            self._refresh_from_locked_row()
            if self.status == SubscriptionStatusChoices.EXPIRED:
                return False
            if self.renewal_date and self.renewal_date >= timezone.localdate():
                return False
            self.validate_transition(SubscriptionStatusChoices.EXPIRED)
            self.status = SubscriptionStatusChoices.EXPIRED
            self.save(update_fields=["status", "updated_at"])
            return True


class SubscriptionAssignment(SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    tenant_lookup = "subscription__tenant"
    # Subscriptions are always tenant-owned; a global (tenant=None) parent would
    # be an anomaly, so never expose its assignments cross-tenant.
    deny_global_tenant = True
    # An assignment is a leaf row of the Subscription aggregate: archiving a
    # subscription archives its assignments with it (and restores them with it),
    # so the model carries its own `deleted_at` and the soft-delete-aware default
    # manager. `all_objects` is the including-deleted manager the aggregate
    # service reads under its lock.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    "Flexibly links a Subscription to the entity (or entities) it covers."

    #: Correlates the leaf row with the aggregate archive operation that moved it
    #: (#619). A restore only brings back rows an operation archived; a row ended
    #: on its own (a holder detachment or a leaf delete) has no marker and stays
    #: deleted. NULL for every pre-existing row.
    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    subscription = models.ForeignKey(
        to=Subscription, on_delete=models.CASCADE, related_name="assignments", verbose_name=_("Subscription")
    )

    @property
    def tenant(self):
        return self.subscription.tenant if self.subscription_id else None

    content_type = models.ForeignKey(
        to=ContentType,
        on_delete=models.CASCADE,
        limit_choices_to={
            "model__in": ("asset", "assetholder", "location", "consumable", "accessory"),
        },
    )
    object_id = models.PositiveBigIntegerField()
    assigned_object = GenericForeignKey(ct_field="content_type", fk_field="object_id")
    assigned_date = models.DateTimeField(auto_now_add=True, editable=False)
    assigned_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subscription_assignments_created",
        verbose_name=_("Assigned By"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        ordering = ("-assigned_date",)
        verbose_name = _("Subscription Assignment")
        verbose_name_plural = _("Subscription Assignments")
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "content_type", "object_id"],
                condition=Q(deleted_at__isnull=True),
                name="subscriptions_assignment_unique",
            )
        ]

    def __str__(self):
        target = self.tenant_safe_assigned_object
        if target:
            return f"Subscription {self.subscription} -> {target}"
        return f"Subscription {self.subscription} assignment (unlinked)"

    @property
    def tenant_safe_assigned_object(self):
        target = self._resolve_assigned_object_unscoped()
        if target is None:
            return None
        if getattr(target, "tenant_id", None) != self.subscription.tenant_id:
            return None
        return target

    def _resolve_assigned_object_unscoped(self):
        if not self.content_type_id or not self.object_id:
            return None
        model = self.content_type.model_class()
        if model is None:
            return None
        filters = {"pk": self.object_id}
        if any(field.name == "deleted_at" for field in model._meta.concrete_fields):
            filters["deleted_at__isnull"] = True
        return model._base_manager.filter(**filters).first()

    def clean(self):
        super().clean()
        if not self.content_type_id or not self.object_id:
            return
        target = self._resolve_assigned_object_unscoped()
        if target is None:
            raise ValidationError(_("The assignment target does not exist."))
        target_tenant_id = getattr(target, "tenant_id", None)
        subscription_tenant_id = self.subscription.tenant_id if self.subscription_id else None
        if target_tenant_id != subscription_tenant_id:
            raise ValidationError(_("The assignment target must belong to the subscription tenant."))

    def get_absolute_url(self):
        if self.subscription:
            return self.subscription.get_absolute_url()
        try:
            return reverse("admin:subscriptions_subscriptionassignment_changelist")
        except NoReverseMatch:
            return "#"
