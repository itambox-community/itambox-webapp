from django.core.exceptions import FieldError, ValidationError
from django.db import models, transaction
from django.db.models import CheckConstraint, Q, Sum
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.managers import (
    ExplicitScopeAllObjectsManager,
    ExplicitScopeSoftDeleteManager,
    Scope,
    SoftDeleteQuerySet,
    TenantScopingQuerySet,
    TenantScopingSoftDeleteQuerySet,
)
from core.mixins import (
    CloneableMixin,
    ExportableMixin,
    JournalingMixin,
    SoftDeleteMixin,
    TaggableMixin,
)
from core.models import BaseModel, ChangeLoggingMixin

from .abstract_models import AbstractAssignment, AbstractInventoryItem, AbstractStock
from .models_assignment_write import assignment_hard_purge_is_permitted
from .models_stock import adjust_inventory_stock


class AssignmentWriteProtectionMixin:
    """Forbid assignment queryset writes that bypass authorized services."""

    @staticmethod
    def _deny_bulk_write():
        raise ValidationError(
            _("This assignment cannot be changed here. Use the authorized checkout or check-in action.")
        )

    def bulk_create(self, *args, **kwargs):
        self._deny_bulk_write()

    def bulk_update(self, *args, **kwargs):
        self._deny_bulk_write()

    def update(self, **kwargs):
        self._deny_bulk_write()

    def update_or_create(self, *args, **kwargs):
        self._deny_bulk_write()

    def get_or_create(self, *args, **kwargs):
        self._deny_bulk_write()

    def delete(self):
        self._deny_bulk_write()

    def _raw_delete(self, using):
        self._deny_bulk_write()


class AssignmentBaseQuerySet(AssignmentWriteProtectionMixin, models.QuerySet):
    """Unscoped assignment reads with the fail-closed write boundary."""


class AssignmentTenantSoftDeleteQuerySet(AssignmentWriteProtectionMixin, TenantScopingSoftDeleteQuerySet):
    """Live tenant-scoped assignment reads with protected writes."""


class AssignmentTenantAllObjectsQuerySet(AssignmentWriteProtectionMixin, TenantScopingQuerySet):
    """All-row tenant-scoped assignment reads with protected writes."""


class AssignmentBaseManager(models.Manager.from_queryset(AssignmentBaseQuerySet)):
    """Unscoped base reads while retaining the fail-closed write boundary."""


class AssignmentSoftDeleteManager(ExplicitScopeSoftDeleteManager.from_queryset(AssignmentTenantSoftDeleteQuerySet)):
    pass


class AssignmentAllObjectsManager(ExplicitScopeAllObjectsManager.from_queryset(AssignmentTenantAllObjectsQuerySet)):
    pass


class AccessoryQuerySet(TenantScopingSoftDeleteQuerySet):
    def with_counts(self):
        from django.db.models import IntegerField, OuterRef, Subquery, Sum
        from django.db.models.functions import Coalesce

        # One independent correlated Subquery per multi-valued reverse relation.
        # Annotating two Sum() over different relations in a single .annotate()
        # produces a |stocks|x|assignments| cartesian JOIN that inflates BOTH
        # sums; Subqueries keep each aggregate independent and un-inflated.
        total_stock_subquery = (
            AccessoryStock.objects.for_scope(Scope.current())
            .filter(accessory=OuterRef("pk"))
            .order_by()
            .values("accessory")
            .annotate(total=Sum("qty"))
            .values("total")
        )

        checked_out_subquery = (
            AccessoryAssignment.objects.for_scope(Scope.current())
            .filter(accessory=OuterRef("pk"), deleted_at__isnull=True)
            .order_by()
            .values("accessory")
            .annotate(total=Sum("qty"))
            .values("total")
        )

        return self.annotate(
            _total_stock=Coalesce(Subquery(total_stock_subquery, output_field=IntegerField()), 0),
            _checked_out=Coalesce(Subquery(checked_out_subquery, output_field=IntegerField()), 0),
        )


class ExplicitAccessoryManager(models.Manager.from_queryset(AccessoryQuerySet)):
    """Soft-delete filtering only; readers scope explicitly through ``for_scope``."""

    def get_queryset(self):
        qs = super().get_queryset()
        try:
            return qs.filter(deleted_at__isnull=True)
        except FieldError:
            return qs


class AllObjectsAccessoryManager(models.Manager.from_queryset(AccessoryQuerySet)):
    pass


class ConsumableQuerySet(TenantScopingSoftDeleteQuerySet):
    def with_counts(self):
        from django.db.models import IntegerField, OuterRef, Subquery, Sum
        from django.db.models.functions import Coalesce

        # See AccessoryQuerySet.with_counts: independent Subqueries avoid the
        # stocks x consumptions cartesian-product double-count.
        total_stock_subquery = (
            ConsumableStock.objects.for_scope(Scope.current())
            .filter(consumable=OuterRef("pk"))
            .order_by()
            .values("consumable")
            .annotate(total=Sum("qty"))
            .values("total")
        )

        consumed_subquery = (
            ConsumableAssignment.objects.for_scope(Scope.current())
            .filter(consumable=OuterRef("pk"), deleted_at__isnull=True)
            .order_by()
            .values("consumable")
            .annotate(total=Sum("qty"))
            .values("total")
        )

        return self.annotate(
            _total_stock=Coalesce(Subquery(total_stock_subquery, output_field=IntegerField()), 0),
            _consumed=Coalesce(Subquery(consumed_subquery, output_field=IntegerField()), 0),
        )


class ExplicitConsumableManager(models.Manager.from_queryset(ConsumableQuerySet)):
    """Soft-delete filtering only; readers scope explicitly through ``for_scope``."""

    def get_queryset(self):
        qs = super().get_queryset()
        try:
            return qs.filter(deleted_at__isnull=True)
        except FieldError:
            return qs


class AllObjectsConsumableManager(models.Manager.from_queryset(ConsumableQuerySet)):
    pass


class ComponentQuerySet(SoftDeleteQuerySet, TenantScopingQuerySet):
    def with_counts(self):
        from django.db.models import IntegerField, OuterRef, Subquery, Sum
        from django.db.models.functions import Coalesce

        # Subquery to sum the quantities in ComponentStock for this component
        total_stock_subquery = (
            ComponentStock.objects.for_scope(Scope.current())
            .filter(component=OuterRef("pk"))
            .order_by()
            .values("component")
            .annotate(total=Sum("qty"))
            .values("total")
        )

        # Subquery to sum active qty in ComponentAllocation for this component
        allocated_stock_subquery = (
            ComponentAllocation.objects.for_scope(Scope.current())
            .filter(component=OuterRef("pk"), deleted_at__isnull=True)
            .order_by()
            .values("component")
            .annotate(total=Sum("qty"))
            .values("total")
        )
        target_only_allocated_subquery = (
            ComponentAllocation.objects.for_scope(Scope.current())
            .filter(
                component=OuterRef("pk"),
                deleted_at__isnull=True,
                from_location__isnull=True,
            )
            .order_by()
            .values("component")
            .annotate(total=Sum("qty"))
            .values("total")
        )

        return self.annotate(
            _total_stock=Coalesce(Subquery(total_stock_subquery, output_field=IntegerField()), 0),
            _allocated_stock=Coalesce(Subquery(allocated_stock_subquery, output_field=IntegerField()), 0),
            _target_only_allocated=Coalesce(
                Subquery(target_only_allocated_subquery, output_field=IntegerField()),
                0,
            ),
        )


class ExplicitComponentManager(models.Manager.from_queryset(ComponentQuerySet)):
    """Soft-delete filtering only; readers scope explicitly through ``for_scope``."""

    def get_queryset(self):
        qs = super().get_queryset()
        try:
            return qs.filter(deleted_at__isnull=True)
        except FieldError:
            return qs


class AllObjectsComponentManager(models.Manager.from_queryset(ComponentQuerySet)):
    pass


class Component(AbstractInventoryItem):
    objects = ExplicitComponentManager()
    all_objects = AllObjectsComponentManager()

    specs = models.JSONField(default=dict, blank=True, verbose_name=_("Specs"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="components",
        verbose_name=_("Tenant"),
        db_index=True,
    )
    tags = models.ManyToManyField("extras.Tag", related_name="components", verbose_name=_("Tags"), blank=True)

    class Meta(AbstractInventoryItem.Meta):
        verbose_name = _("Component (Catalog)")
        verbose_name_plural = _("Components (Catalog)")
        constraints = [
            models.UniqueConstraint(
                fields=["manufacturer", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_component_manufacturer_name",
            )
        ]

    def get_absolute_url(self):
        return reverse("inventory:component_detail", kwargs={"pk": self.pk})

    @property
    def total_stock(self):
        if hasattr(self, "_total_stock"):
            return self._total_stock
        return self.stocks.aggregate(total=models.Sum("qty"))["total"] or 0

    @property
    def total_allocated(self):
        if hasattr(self, "_allocated_stock"):
            return self._allocated_stock
        return self.allocations.filter(deleted_at__isnull=True).aggregate(total=models.Sum("qty"))["total"] or 0

    @property
    def target_only_allocated(self):
        if hasattr(self, "_target_only_allocated"):
            return self._target_only_allocated
        return (
            self.allocations.filter(
                deleted_at__isnull=True,
                from_location__isnull=True,
            ).aggregate(total=models.Sum("qty"))["total"]
            or 0
        )

    @property
    def available_stock(self):
        return self.total_stock - self.target_only_allocated

    @property
    def available(self):
        return self.available_stock


class Accessory(AbstractInventoryItem):
    objects = ExplicitAccessoryManager()
    all_objects = AllObjectsAccessoryManager()

    manufacturer = models.ForeignKey(
        "assets.Manufacturer", on_delete=models.PROTECT, related_name="accessories", verbose_name=_("Manufacturer")
    )
    category = models.ForeignKey(
        "assets.Category",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accessories",
        verbose_name=_("Category"),
        db_index=True,
    )
    supplier = models.ForeignKey(
        "assets.Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supplier_accessories",
        verbose_name=_("Supplier"),
        db_index=True,
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="accessories",
        verbose_name=_("Tenant"),
        db_index=True,
    )

    class Meta(AbstractInventoryItem.Meta):
        verbose_name = _("Accessory")
        verbose_name_plural = _("Accessories")
        constraints = [
            models.UniqueConstraint(
                fields=["manufacturer", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="inventory_accessory_unique_manufacturer_name",
            )
        ]

    def get_absolute_url(self):
        return reverse("inventory:accessory_detail", kwargs={"pk": self.pk})

    @property
    def total_stock(self):
        ts = getattr(self, "_total_stock", None)
        if ts is not None:
            return ts
        return self.stocks.aggregate(total=Sum("qty"))["total"] or 0

    @property
    def checked_out_qty(self):
        co = getattr(self, "_checked_out", None)
        if co is not None:
            return co
        return sum(assignment.qty for assignment in self.assignments.all())

    @property
    def available(self):
        undeducted_qty = sum(a.qty for a in self.assignments.filter(from_location__isnull=True))
        return max(0, self.total_stock - undeducted_qty)


class Consumable(AbstractInventoryItem):
    objects = ExplicitConsumableManager()
    all_objects = AllObjectsConsumableManager()

    class Meta(AbstractInventoryItem.Meta):
        verbose_name = _("Consumable")
        verbose_name_plural = _("Consumables")
        constraints = [
            models.UniqueConstraint(
                fields=["manufacturer", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="inventory_consumable_unique_manufacturer_name",
            )
        ]

    def get_absolute_url(self):
        return reverse("inventory:consumable_detail", kwargs={"pk": self.pk})

    @property
    def total_stock(self):
        ts = getattr(self, "_total_stock", None)
        if ts is not None:
            return ts
        return self.stocks.aggregate(total=Sum("qty"))["total"] or 0

    @property
    def consumed_qty(self):
        cq = getattr(self, "_consumed", None)
        if cq is not None:
            return cq
        return sum(consumption.qty for consumption in self.consumptions.all())

    @property
    def available(self):
        undeducted_qty = sum(a.qty for a in self.consumptions.filter(from_location__isnull=True))
        return max(0, self.total_stock - undeducted_qty)


class ComponentStock(AbstractStock):
    # tenant is a REAL field derived from location.tenant (AbstractStock);
    # scoping runs on it directly — the catalogue item's tenant is irrelevant
    # to pool ownership.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    component = models.ForeignKey(
        Component, on_delete=models.PROTECT, related_name="stocks", verbose_name=_("Component"), db_index=True
    )
    location = models.ForeignKey(
        "organization.Location",
        on_delete=models.PROTECT,
        related_name="component_stocks",
        verbose_name=_("Location"),
        db_index=True,
    )

    class Meta(AbstractStock.Meta):
        verbose_name = _("Component Stock")
        verbose_name_plural = _("Component Stocks")
        constraints = [
            models.UniqueConstraint(
                fields=["component", "location"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_component_location",
            )
        ]

    def __str__(self):
        return f"{self.component.name} @ {self.location.name}: {self.qty}"

    def get_absolute_url(self):
        return self.component.get_absolute_url()


class AccessoryStock(AbstractStock):
    # See ComponentStock — tenant derives from location.tenant.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    accessory = models.ForeignKey(
        Accessory, on_delete=models.PROTECT, related_name="stocks", verbose_name=_("Accessory"), db_index=True
    )

    class Meta(AbstractStock.Meta):
        verbose_name = _("Accessory Stock")
        verbose_name_plural = _("Accessory Stocks")
        constraints = [
            models.UniqueConstraint(
                fields=["accessory", "location"],
                condition=models.Q(deleted_at__isnull=True),
                name="inventory_accessorystock_unique_accessory_location",
            )
        ]

    def __str__(self):
        return f"{self.accessory.name} @ {self.location.name}: {self.qty}"

    def get_absolute_url(self):
        return self.accessory.get_absolute_url()


class ConsumableStock(AbstractStock):
    # See ComponentStock — tenant derives from location.tenant.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    consumable = models.ForeignKey(
        Consumable, on_delete=models.PROTECT, related_name="stocks", verbose_name=_("Consumable"), db_index=True
    )

    class Meta(AbstractStock.Meta):
        verbose_name = _("Consumable Stock")
        verbose_name_plural = _("Consumable Stocks")
        constraints = [
            models.UniqueConstraint(
                fields=["consumable", "location"],
                condition=models.Q(deleted_at__isnull=True),
                name="inventory_consumablestock_unique_consumable_location",
            )
        ]

    def __str__(self):
        return f"{self.consumable.name} @ {self.location.name}: {self.qty}"

    def get_absolute_url(self):
        return self.consumable.get_absolute_url()


class ComponentAllocation(AbstractAssignment):
    tenant_lookup = "component__tenant"
    _item_attr = "component"
    _stock_model_label = "inventory.ComponentStock"
    objects = AssignmentSoftDeleteManager()
    all_objects = AssignmentAllObjectsManager()
    base_objects = AssignmentBaseManager()

    @property
    def tenant(self):
        return self.component.tenant if self.component_id else None

    component = models.ForeignKey(
        Component, on_delete=models.PROTECT, related_name="allocations", verbose_name=_("Component"), db_index=True
    )
    assigned_asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="component_allocations",
        verbose_name=_("Assigned Asset"),
        db_index=True,
    )
    from_location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="component_allocations",
        verbose_name=_("From Location"),
        db_index=True,
    )
    tags = models.ManyToManyField(
        "extras.Tag", related_name="component_allocations", verbose_name=_("Tags"), blank=True
    )

    class Meta(AbstractAssignment.Meta):
        verbose_name = _("Component Allocation")
        verbose_name_plural = _("Component Allocations")
        constraints = [
            CheckConstraint(
                check=(
                    Q(assigned_holder__isnull=False, assigned_location__isnull=True, assigned_asset__isnull=True)
                    | Q(assigned_holder__isnull=True, assigned_location__isnull=False, assigned_asset__isnull=True)
                    | Q(assigned_holder__isnull=True, assigned_location__isnull=True, assigned_asset__isnull=False)
                ),
                name="chk_componentallocation_single_target",
            )
        ]

    def __str__(self):
        recipient = self.assigned_holder or self.assigned_location or self.assigned_asset or "Unknown"
        return f"{self.qty}x {self.component} assigned to {recipient}"

    def get_absolute_url(self):
        if self.assigned_asset:
            return self.assigned_asset.get_absolute_url()
        return self.component.get_absolute_url()

    def save(self, *args, **kwargs):
        with transaction.atomic():
            adjust_inventory_stock(self)
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        force_hard = bool(kwargs.get("force_hard_delete", False))
        if force_hard and not assignment_hard_purge_is_permitted(self):
            raise ValidationError(_("Assignment hard purge requires the authorized purge service."))
        with transaction.atomic():
            if not force_hard:
                adjust_inventory_stock(self, is_delete=True)
            super().delete(*args, **kwargs)


class AccessoryAssignment(AbstractAssignment):
    tenant_lookup = "accessory__tenant"
    _item_attr = "accessory"
    _stock_model_label = "inventory.AccessoryStock"
    objects = AssignmentSoftDeleteManager()
    all_objects = AssignmentAllObjectsManager()
    base_objects = AssignmentBaseManager()

    @property
    def tenant(self):
        return self.accessory.tenant if self.accessory_id else None

    accessory = models.ForeignKey(
        Accessory, on_delete=models.PROTECT, related_name="assignments", verbose_name=_("Accessory"), db_index=True
    )

    class Meta(AbstractAssignment.Meta):
        verbose_name = _("Accessory Assignment")
        verbose_name_plural = _("Accessory Assignments")
        constraints = [
            CheckConstraint(
                check=(
                    Q(assigned_holder__isnull=False, assigned_location__isnull=True, assigned_asset__isnull=True)
                    | Q(assigned_holder__isnull=True, assigned_location__isnull=False, assigned_asset__isnull=True)
                    | Q(assigned_holder__isnull=True, assigned_location__isnull=True, assigned_asset__isnull=False)
                ),
                name="chk_accessory_assignment_single_target",
            )
        ]

    def __str__(self):
        recipient = self.assigned_holder or self.assigned_location or self.assigned_asset or "Unknown"
        return f"{self.qty}x {self.accessory} assigned to {recipient}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            adjust_inventory_stock(self)
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        force_hard = bool(kwargs.get("force_hard_delete", False))
        if force_hard and not assignment_hard_purge_is_permitted(self):
            raise ValidationError(_("Assignment hard purge requires the authorized purge service."))
        with transaction.atomic():
            if not force_hard:
                adjust_inventory_stock(self, is_delete=True)
            super().delete(*args, **kwargs)


class ConsumableAssignment(AbstractAssignment):
    tenant_lookup = "consumable__tenant"
    _item_attr = "consumable"
    _stock_model_label = "inventory.ConsumableStock"
    objects = AssignmentSoftDeleteManager()
    all_objects = AssignmentAllObjectsManager()
    base_objects = AssignmentBaseManager()

    @property
    def tenant(self):
        return self.consumable.tenant if self.consumable_id else None

    consumable = models.ForeignKey(
        Consumable, on_delete=models.PROTECT, related_name="consumptions", verbose_name=_("Consumable"), db_index=True
    )

    class Meta(AbstractAssignment.Meta):
        verbose_name = _("Consumable Consumption")
        verbose_name_plural = _("Consumable Consumptions")
        constraints = [
            CheckConstraint(
                check=(
                    Q(assigned_holder__isnull=False, assigned_location__isnull=True, assigned_asset__isnull=True)
                    | Q(assigned_holder__isnull=True, assigned_location__isnull=False, assigned_asset__isnull=True)
                    | Q(assigned_holder__isnull=True, assigned_location__isnull=True, assigned_asset__isnull=False)
                ),
                name="chk_consumable_assignment_single_target",
            )
        ]

    def __str__(self):
        recipient = self.assigned_holder or self.assigned_location or self.assigned_asset or "Unknown"
        return f"{self.qty}x {self.consumable} consumed by {recipient}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            adjust_inventory_stock(self)
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        force_hard = bool(kwargs.get("force_hard_delete", False))
        if force_hard and not assignment_hard_purge_is_permitted(self):
            raise ValidationError(_("Assignment hard purge requires the authorized purge service."))
        with transaction.atomic():
            if not force_hard:
                adjust_inventory_stock(self, is_delete=True)
            super().delete(*args, **kwargs)


class Kit(
    JournalingMixin, TaggableMixin, CloneableMixin, ExportableMixin, SoftDeleteMixin, ChangeLoggingMixin, BaseModel
):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    name = models.CharField(max_length=100, verbose_name=_("Kit Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="kits",
        verbose_name=_("Tenant"),
        db_index=True,
    )
    tags = models.ManyToManyField("extras.Tag", related_name="kits", verbose_name=_("Tags"), blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Kit")
        verbose_name_plural = _("Kits")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=Q(deleted_at__isnull=True), name="unique_kit_name_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("inventory:kit_detail", kwargs={"pk": self.pk})


class KitItem(SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    tenant_lookup = "kit__tenant"
    # Global Kits (tenant=None) are cross-tenant templates, so their items stay
    # READABLE by default (the manager's default behaviour for tenant_lookup
    # models). Cross-tenant WRITE is blocked by StrictTenantPermission /
    # perform_create tenant-defaulting.
    #
    # Kit items are leaf rows of the Kit aggregate: archiving a kit archives its
    # items with it (and restores them with it), so the model carries its own
    # `deleted_at` and the soft-delete-aware default manager. `all_objects` is the
    # including-deleted manager the aggregate service reads under its lock.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    @property
    def tenant(self):
        return self.kit.tenant if self.kit_id else None

    #: Correlates the leaf row with the aggregate archive operation that moved it
    #: (#619). A restore only brings back rows an operation archived; a row
    #: deleted on its own (leaf delete) has no marker and stays deleted. NULL for
    #: every pre-existing row.
    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    kit = models.ForeignKey(Kit, on_delete=models.CASCADE, related_name="items", verbose_name=_("Kit"), db_index=True)
    asset_type = models.ForeignKey(
        "assets.AssetType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kit_items",
        verbose_name=_("Asset Type / Model"),
    )
    accessory = models.ForeignKey(
        Accessory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kit_items",
        verbose_name=_("Accessory Catalog Item"),
    )
    license = models.ForeignKey(
        "licenses.License",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kit_items",
        verbose_name=_("Software License"),
    )
    consumable = models.ForeignKey(
        Consumable,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kit_items",
        verbose_name=_("Consumable Catalog Item"),
    )
    component = models.ForeignKey(
        Component,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="kit_items",
        verbose_name=_("Component Catalog Item"),
    )
    qty = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Quantity"),
        help_text=_("Quantity to checkout (applies to Accessories, Consumables, and Components)"),
    )

    class Meta:
        verbose_name = _("Kit Item")
        verbose_name_plural = _("Kit Items")
        constraints = [
            CheckConstraint(
                check=(
                    Q(
                        asset_type__isnull=False,
                        accessory__isnull=True,
                        license__isnull=True,
                        consumable__isnull=True,
                        component__isnull=True,
                    )
                    | Q(
                        asset_type__isnull=True,
                        accessory__isnull=False,
                        license__isnull=True,
                        consumable__isnull=True,
                        component__isnull=True,
                    )
                    | Q(
                        asset_type__isnull=True,
                        accessory__isnull=True,
                        license__isnull=False,
                        consumable__isnull=True,
                        component__isnull=True,
                    )
                    | Q(
                        asset_type__isnull=True,
                        accessory__isnull=True,
                        license__isnull=True,
                        consumable__isnull=False,
                        component__isnull=True,
                    )
                    | Q(
                        asset_type__isnull=True,
                        accessory__isnull=True,
                        license__isnull=True,
                        consumable__isnull=True,
                        component__isnull=False,
                    )
                ),
                name="chk_kit_item_single_target",
            )
        ]

    def __str__(self):
        if self.asset_type:
            return f"Asset Type: {self.asset_type}"
        elif self.accessory:
            return f"{self.qty}x Accessory: {self.accessory}"
        elif self.license:
            return f"License: {self.license.software.name} ({self.license.name})"
        elif self.consumable:
            return f"{self.qty}x Consumable: {self.consumable}"
        elif self.component:
            return f"{self.qty}x Component: {self.component}"
        return "Empty Kit Item"

    def clean(self):
        super().clean()
        targets = [self.asset_type, self.accessory, self.license, self.consumable, self.component]
        filled = [t for t in targets if t is not None]
        if len(filled) == 0:
            raise ValidationError(
                _("A kit item must select either an Asset Type, Accessory, License, Consumable, or Component.")
            )
        if len(filled) > 1:
            raise ValidationError(_("A kit item cannot select more than one target."))
