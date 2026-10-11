from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.currency import CurrencyField
from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager
from core.models import BaseModel, ChangeLoggingMixin, SoftDeleteMixin, TaggableMixin

User = get_user_model()


class PurchaseOrder(BaseModel, ChangeLoggingMixin, SoftDeleteMixin, TaggableMixin):
    STATUS_DRAFT = "draft"
    STATUS_APPROVED = "approved"
    STATUS_ORDERED = "ordered"
    STATUS_PARTIAL = "partial"
    STATUS_RECEIVED = "received"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_DRAFT, _("Draft")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_ORDERED, _("Ordered")),
        (STATUS_PARTIAL, _("Partially Received")),
        (STATUS_RECEIVED, _("Received")),
        (STATUS_CANCELLED, _("Cancelled")),
    ]

    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="purchase_orders",
        verbose_name=_("Tenant"),
    )
    order_number = models.CharField(max_length=100, db_index=True, verbose_name=_("Order Number"))
    currency = CurrencyField()
    supplier = models.ForeignKey(
        "assets.Supplier", on_delete=models.PROTECT, related_name="purchase_orders", verbose_name=_("Supplier")
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name=_("Status"))
    order_date = models.DateField(null=True, blank=True, verbose_name=_("Order Date"))
    expected_delivery_date = models.DateField(null=True, blank=True, verbose_name=_("Expected Delivery Date"))
    destination_location = models.ForeignKey(
        "organization.Location",
        on_delete=models.PROTECT,
        related_name="incoming_purchase_orders",
        verbose_name=_("Destination Location"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="created_purchase_orders", verbose_name=_("Created By")
    )

    class Meta:
        verbose_name = _("Purchase Order")
        verbose_name_plural = _("Purchase Orders")
        permissions = [
            ("receive_purchaseorder", _("Can receive stock from a purchase order")),
            ("approve_purchaseorder", _("Can approve/submit a purchase order")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["order_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_purchaseorder_number_active",
            ),
        ]

    def __str__(self):
        return f"PO {self.order_number}"

    def get_absolute_url(self):
        return reverse("procurement:purchaseorder_detail", kwargs={"pk": self.pk})


class PurchaseOrderLine(BaseModel, ChangeLoggingMixin, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="po_lines",
        verbose_name=_("Tenant"),
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines", verbose_name=_("Purchase Order")
    )
    asset_type = models.ForeignKey(
        "assets.AssetType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="po_lines",
        verbose_name=_("Asset Type"),
    )
    component = models.ForeignKey(
        "inventory.Component",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="po_lines",
        verbose_name=_("Component"),
    )
    accessory = models.ForeignKey(
        "inventory.Accessory",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="po_lines",
        verbose_name=_("Accessory"),
    )
    consumable = models.ForeignKey(
        "inventory.Consumable",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="po_lines",
        verbose_name=_("Consumable"),
    )
    license = models.ForeignKey(
        "licenses.License",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="po_lines",
        verbose_name=_("License"),
    )

    qty_ordered = models.PositiveIntegerField(default=1, verbose_name=_("Qty Ordered"))
    qty_received = models.PositiveIntegerField(default=0, verbose_name=_("Qty Received"))
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, verbose_name=_("Unit Price")
    )

    def __str__(self):
        item = self.asset_type or self.component or self.accessory or self.consumable or self.license or "Unknown Item"
        return f"{self.qty_ordered}x {item} for PO {self.purchase_order.order_number}"

    @property
    def currency(self):
        """Delegate to the parent PO's currency so ``{{ line.unit_price|money:line }}`` resolves correctly."""
        return self.purchase_order.currency

    @property
    def qty_outstanding(self):
        return max(0, self.qty_ordered - self.qty_received)

    @property
    def total_cost(self):
        if self.unit_price and self.qty_ordered:
            return self.unit_price * self.qty_ordered
        return None

    def clean(self):
        super().clean()
        filled = sum(
            [
                1 if self.asset_type else 0,
                1 if self.component else 0,
                1 if self.accessory else 0,
                1 if self.consumable else 0,
                1 if self.license else 0,
            ]
        )
        if filled == 0:
            raise ValidationError(_("You must specify what item you are ordering."))
        if filled > 1:
            raise ValidationError(_("A line item can only refer to one type of item."))


class ContractTypeChoices(models.TextChoices):
    SUPPORT = "support", _("Support")
    MAINTENANCE = "maintenance", _("Maintenance")
    LEASE = "lease", _("Lease")
    WARRANTY = "warranty", _("Warranty")
    SERVICE = "service", _("Service")
    OTHER = "other", _("Other")


class ContractStatusChoices(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ACTIVE = "active", _("Active")
    EXPIRED = "expired", _("Expired")
    CANCELLED = "cancelled", _("Cancelled")


class ContractBillingCycleChoices(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    QUARTERLY = "quarterly", _("Quarterly")
    ANNUAL = "annual", _("Annual")
    BIANNUAL = "biannual", _("Biannual")
    MULTI_YEAR = "multi_year", _("Multi-Year")
    ONETIME = "onetime", _("One-Time")


class Contract(BaseModel, ChangeLoggingMixin, SoftDeleteMixin, TaggableMixin):
    """A hardware/software support agreement, SLA, lease, or service contract."""

    objects = ExplicitScopeSoftDeleteManager()

    # --- Identity ---
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="contracts",
        verbose_name=_("Tenant"),
    )
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    contract_number = models.CharField(max_length=100, db_index=True, verbose_name=_("Contract Number"))
    contract_type = models.CharField(
        max_length=20,
        choices=ContractTypeChoices.choices,
        default=ContractTypeChoices.SUPPORT,
        verbose_name=_("Contract Type"),
    )
    status = models.CharField(
        max_length=20,
        choices=ContractStatusChoices.choices,
        default=ContractStatusChoices.DRAFT,
        verbose_name=_("Status"),
    )

    # --- Vendor / commercial ---
    supplier = models.ForeignKey(
        "assets.Supplier",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Supplier"),
    )
    cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Contract Cost"),
        help_text=_("Total or per-cycle cost"),
    )
    currency = CurrencyField()
    billing_cycle = models.CharField(
        max_length=20,
        choices=ContractBillingCycleChoices.choices,
        default=ContractBillingCycleChoices.ANNUAL,
        blank=True,
        verbose_name=_("Billing Cycle"),
    )

    # --- Dates ---
    start_date = models.DateField(verbose_name=_("Start Date"))
    end_date = models.DateField(verbose_name=_("End Date"))
    renewal_date = models.DateField(null=True, blank=True, verbose_name=_("Renewal Date"))
    auto_renew = models.BooleanField(
        default=False,
        verbose_name=_("Auto-Renew"),
        help_text=_("Whether this contract renews automatically"),
    )

    # --- SLA ---
    sla_response_time = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("SLA Response Time"),
        help_text=_('e.g. "4 business hours", "Next business day"'),
    )
    sla_resolution_time = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("SLA Resolution Time"),
        help_text=_('e.g. "8 business hours", "3 business days"'),
    )
    coverage_hours = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Coverage Hours"),
        help_text=_('e.g. "24x7", "9-5 Mon-Fri"'),
    )
    sla_terms = models.TextField(
        blank=True,
        verbose_name=_("SLA Terms"),
        help_text=_("Full SLA terms or summary text"),
    )

    # --- Coverage ---
    assets = models.ManyToManyField(
        "assets.Asset",
        blank=True,
        related_name="contracts",
        verbose_name=_("Covered Assets"),
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Purchase Order"),
        help_text=_("The PO this contract originated from, if any"),
    )

    # --- Cost center ---
    cost_center = models.ForeignKey(
        "organization.CostCenter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contracts",
        verbose_name=_("Cost Center"),
    )

    # --- Notes ---
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_contract_number_active",
            ),
            models.CheckConstraint(
                check=models.Q(end_date__gte=models.F("start_date")),
                name="contract_end_date_gte_start_date",
            ),
        ]

    def __str__(self):
        return f"{self.contract_number} – {self.name}"

    def get_absolute_url(self):
        return reverse("procurement:contract_detail", kwargs={"pk": self.pk})

    @property
    def days_until_expiry(self):
        """Return the number of calendar days until end_date (negative if expired)."""
        delta = self.end_date - timezone.now().date()
        return delta.days

    @property
    def is_expiring_soon(self) -> bool:
        """True if the contract expires within 30 days."""
        days = self.days_until_expiry
        return 0 <= days <= 30

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": _("End date must be on or after the start date.")})

        if self.purchase_order_id and self.purchase_order.tenant_id != self.tenant_id:
            raise ValidationError(
                {"purchase_order": _("The purchase order must belong to the same tenant as the contract.")}
            )


class FulfillmentLink(BaseModel, ChangeLoggingMixin, SoftDeleteMixin):
    """Links an AssetRequest to the PurchaseOrderLine that will supply it."""

    objects = ExplicitScopeSoftDeleteManager()

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="fulfillment_links",
        verbose_name=_("Tenant"),
    )
    asset_request = models.ForeignKey(
        "assets.AssetRequest",
        on_delete=models.CASCADE,
        related_name="fulfillment_links",
        verbose_name=_("Asset Request"),
    )
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.CASCADE,
        related_name="fulfillment_links",
        verbose_name=_("Purchase Order Line"),
    )
    qty_allocated = models.PositiveIntegerField(default=1, verbose_name=_("Qty Allocated"))
    qty_received = models.PositiveIntegerField(
        null=True,
        verbose_name=_("Qty Received"),
        help_text=_(
            "Quantity of this line's receipts attributed to this Asset Request. "
            "Null on links that predate receipt attribution."
        ),
    )

    @property
    def received_attribution_tracked(self):
        return self.qty_received is not None

    @property
    def qty_outstanding(self):
        """Still-undelivered quantity for this link; untracked links count their full allocation."""
        return max(0, self.qty_allocated - (self.qty_received or 0))

    @property
    def fully_delivered(self):
        return self.received_attribution_tracked and self.qty_received >= self.qty_allocated

    def clean(self):
        super().clean()
        request_tenant_id = self.asset_request.tenant_id if self.asset_request_id else None
        line_tenant_id = self.purchase_order_line.tenant_id if self.purchase_order_line_id else None
        po_tenant_id = self.purchase_order_line.purchase_order.tenant_id if self.purchase_order_line_id else None
        tenant_ids = {self.tenant_id, request_tenant_id, line_tenant_id, po_tenant_id}
        if None in tenant_ids or len(tenant_ids) != 1:
            raise ValidationError(
                _("The fulfillment link, Asset Request, line, and purchase order must use the same tenant.")
            )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset_request", "purchase_order_line"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_request_po_line_link",
            )
        ]
