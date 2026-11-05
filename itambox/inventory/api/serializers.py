from django.db import models, transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from assets.api.nested_serializers import (
    NestedAssetSerializer,
    NestedAssetTypeSerializer,
    NestedManufacturerSerializer,
)
from assets.models import Asset, Category
from extras.api.serializers import TagSerializer
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
    Kit,
    KitItem,
)
from itambox.api.base import BaseModelSerializer
from organization.api.serializers import AssetHolderSerializer, NestedLocationSerializer, NestedTenantSerializer


class _AssignmentAvailabilityMixin:
    """Re-applies the availability + row-lock invariant on the REST create/update path.

    The over-allocation guard lives only in checkout_inventory_item(); the CRUD viewsets
    create/update assignments straight through the serializer, and adjust_inventory_stock
    only checks/deducts stock when ``from_location`` is set — so a POST/PATCH with
    ``from_location`` omitted (or qty > available) bypassed every check. Lock the parent
    item and re-check ``available`` here (held through perform_create's/perform_update's
    transaction), mirroring the service.
    """

    item_source_field = None  # 'accessory' | 'consumable' | 'component'

    def create(self, validated_data: dict[str, object]) -> object:
        item = validated_data.get(self.item_source_field)
        # Use the effective qty: what was supplied, or the model-field default (1).
        # The previous `or 0` falsy-coercion caused the availability guard to be
        # skipped when qty was omitted, while the model default of 1 still
        # materialised and reduced stock — a silent over-allocation bypass.
        qty = validated_data.get("qty")
        if qty is None:
            qty = 1  # mirrors AbstractAssignment.qty default=1
        if item is not None:
            with transaction.atomic():
                locked = type(item).objects.select_for_update().with_counts().get(pk=item.pk)
                if not locked.allow_overallocate and locked.available < qty:
                    raise serializers.ValidationError(
                        {
                            "qty": _("Not enough stock for %(item)s: %(available)s available, %(qty)s requested.")
                            % {"item": locked.name, "available": locked.available, "qty": qty}
                        }
                    )
                validated_data[self.item_source_field] = locked
                return super().create(validated_data)
        return super().create(validated_data)

    def update(self, instance: object, validated_data: dict[str, object]) -> object:
        # D5-1: create() re-checks availability under a row lock, but the default
        # ModelSerializer.update() bypassed it entirely — a PATCH/PUT (or bulk-update,
        # which calls perform_update per row) could raise qty (or repoint the item FK)
        # with no capacity check at all, driving Accessory/Consumable/Component
        # `available` negative. Mirror create()'s lock-then-check here.
        old_item = getattr(instance, self.item_source_field)
        new_item = validated_data.get(self.item_source_field)
        item_changing = new_item is not None and new_item.pk != old_item.pk
        target_item = new_item if item_changing else old_item

        qty = validated_data.get("qty", instance.qty)
        if qty is None:
            qty = 1  # mirrors AbstractAssignment.qty default=1

        if target_item is not None:
            with transaction.atomic():
                locked = type(target_item).objects.select_for_update().with_counts().get(pk=target_item.pk)
                # Credit back what this row already holds against `locked.available`,
                # but only when the item is unchanged AND the instance's current demand
                # is actually counted in `available` — i.e. it has no `from_location`.
                # A from_location-bearing row's demand is deducted separately via the
                # per-location Accessory/Consumable/ComponentStock row (governed by
                # adjust_inventory_stock's own check); crediting it back here would
                # double-count and over-permit.
                credit = 0
                if not item_changing and instance.from_location_id is None:
                    credit = instance.qty
                effective_available = locked.available + credit
                if not locked.allow_overallocate and effective_available < qty:
                    raise serializers.ValidationError(
                        {
                            "qty": _("Not enough stock for %(item)s: %(available)s available, %(qty)s requested.")
                            % {"item": locked.name, "available": effective_available, "qty": qty}
                        }
                    )
                if item_changing:
                    validated_data[self.item_source_field] = locked
                return super().update(instance, validated_data)
        return super().update(instance, validated_data)


def _accessory_category_queryset() -> models.QuerySet[Category]:
    return Category.objects.filter(applies_to__accessory=True)


def _consumable_category_queryset() -> models.QuerySet[Category]:
    return Category.objects.filter(applies_to__consumable=True)


class NestedAccessorySerializer(BaseModelSerializer):
    class Meta:
        model = Accessory
        fields = ["id", "name", "manufacturer"]
        brief_fields = ["id", "name"]


class AccessorySerializer(BaseModelSerializer):
    manufacturer = NestedManufacturerSerializer(read_only=True)
    manufacturer_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedManufacturerSerializer.Meta.model.objects.all(), source="manufacturer", write_only=True
    )
    category = serializers.SerializerMethodField(read_only=True)
    category_id: serializers.PrimaryKeyRelatedField[Category] = serializers.PrimaryKeyRelatedField(
        queryset=_accessory_category_queryset(), source="category", write_only=True, required=False, allow_null=True
    )
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[models.Model] = serializers.PrimaryKeyRelatedField(
        queryset=NestedTenantSerializer.Meta.model.objects,
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
    )
    tags = TagSerializer(many=True, read_only=True)
    total_stock = serializers.IntegerField(read_only=True)
    checked_out_qty = serializers.IntegerField(read_only=True)
    available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Accessory
        fields = [
            "id",
            "name",
            "slug",
            "manufacturer",
            "manufacturer_id",
            "category",
            "category_id",
            "part_number",
            "total_stock",
            "checked_out_qty",
            "available",
            "min_qty",
            "allow_overallocate",
            "notes",
            "tags",
            "tenant",
            "tenant_id",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "manufacturer", "category", "available"]

    def get_category(self, obj: Accessory) -> dict[str, object] | None:
        if obj.category:
            return {"id": obj.category.pk, "name": obj.category.name, "slug": obj.category.slug}
        return None


class AccessoryStockSerializer(BaseModelSerializer):
    accessory = NestedAccessorySerializer(read_only=True)
    accessory_id = serializers.PrimaryKeyRelatedField(queryset=Accessory.objects, source="accessory", write_only=True)
    location = NestedLocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects, source="location", write_only=True
    )

    class Meta:
        model = AccessoryStock
        fields = ["id", "accessory", "accessory_id", "location", "location_id", "qty", "created_at", "updated_at"]
        brief_fields = ["id", "accessory", "location", "qty"]


class AccessoryAssignmentSerializer(_AssignmentAvailabilityMixin, BaseModelSerializer):
    item_source_field = "accessory"
    accessory = NestedAccessorySerializer(read_only=True)
    accessory_id = serializers.PrimaryKeyRelatedField(queryset=Accessory.objects, source="accessory", write_only=True)
    assigned_holder = AssetHolderSerializer(read_only=True)
    assigned_holder_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetHolderSerializer.Meta.model.objects,
        source="assigned_holder",
        write_only=True,
        required=False,
        allow_null=True,
    )
    assigned_location = NestedLocationSerializer(read_only=True)
    assigned_location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects,
        source="assigned_location",
        write_only=True,
        required=False,
        allow_null=True,
    )
    from_location = NestedLocationSerializer(read_only=True)
    from_location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects,
        source="from_location",
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = AccessoryAssignment
        fields = [
            "id",
            "accessory",
            "accessory_id",
            "assigned_holder",
            "assigned_holder_id",
            "assigned_location",
            "assigned_location_id",
            "from_location",
            "from_location_id",
            "qty",
            "assigned_date",
            "notes",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "accessory", "assigned_holder", "qty"]


class NestedConsumableSerializer(BaseModelSerializer):
    class Meta:
        model = Consumable
        fields = ["id", "name", "manufacturer"]
        brief_fields = ["id", "name"]


class ConsumableSerializer(BaseModelSerializer):
    manufacturer = NestedManufacturerSerializer(read_only=True)
    manufacturer_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedManufacturerSerializer.Meta.model.objects.all(), source="manufacturer", write_only=True
    )
    category = serializers.SerializerMethodField(read_only=True)
    category_id: serializers.PrimaryKeyRelatedField[Category] = serializers.PrimaryKeyRelatedField(
        queryset=_consumable_category_queryset(), source="category", write_only=True, required=False, allow_null=True
    )
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[models.Model] = serializers.PrimaryKeyRelatedField(
        queryset=NestedTenantSerializer.Meta.model.objects,
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
    )
    tags = TagSerializer(many=True, read_only=True)
    total_stock = serializers.IntegerField(read_only=True)
    consumed_qty = serializers.IntegerField(read_only=True)
    available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Consumable
        fields = [
            "id",
            "name",
            "slug",
            "manufacturer",
            "manufacturer_id",
            "category",
            "category_id",
            "part_number",
            "total_stock",
            "consumed_qty",
            "available",
            "min_qty",
            "allow_overallocate",
            "notes",
            "tags",
            "tenant",
            "tenant_id",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "manufacturer", "category", "available"]

    def get_category(self, obj: Consumable) -> dict[str, object] | None:
        if obj.category:
            return {"id": obj.category.pk, "name": obj.category.name, "slug": obj.category.slug}
        return None


class ConsumableStockSerializer(BaseModelSerializer):
    consumable = NestedConsumableSerializer(read_only=True)
    consumable_id = serializers.PrimaryKeyRelatedField(
        queryset=Consumable.objects, source="consumable", write_only=True
    )
    location = NestedLocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects, source="location", write_only=True
    )

    class Meta:
        model = ConsumableStock
        fields = ["id", "consumable", "consumable_id", "location", "location_id", "qty", "created_at", "updated_at"]
        brief_fields = ["id", "consumable", "location", "qty"]


class ConsumableAssignmentSerializer(_AssignmentAvailabilityMixin, BaseModelSerializer):
    item_source_field = "consumable"
    consumable = NestedConsumableSerializer(read_only=True)
    consumable_id = serializers.PrimaryKeyRelatedField(
        queryset=Consumable.objects, source="consumable", write_only=True
    )
    assigned_holder = AssetHolderSerializer(read_only=True)
    assigned_holder_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetHolderSerializer.Meta.model.objects,
        source="assigned_holder",
        write_only=True,
        required=False,
        allow_null=True,
    )
    assigned_location = NestedLocationSerializer(read_only=True)
    assigned_location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects,
        source="assigned_location",
        write_only=True,
        required=False,
        allow_null=True,
    )
    from_location = NestedLocationSerializer(read_only=True)
    from_location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects,
        source="from_location",
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = ConsumableAssignment
        fields = [
            "id",
            "consumable",
            "consumable_id",
            "assigned_holder",
            "assigned_holder_id",
            "assigned_location",
            "assigned_location_id",
            "from_location",
            "from_location_id",
            "qty",
            "assigned_date",
            "notes",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "consumable", "assigned_holder", "qty"]


class KitItemSerializer(BaseModelSerializer):
    asset_type = NestedAssetTypeSerializer(read_only=True)
    asset_type_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedAssetTypeSerializer.Meta.model.objects.all(),
        source="asset_type",
        write_only=True,
        required=False,
        allow_null=True,
    )
    accessory = NestedAccessorySerializer(read_only=True)
    accessory_id = serializers.PrimaryKeyRelatedField(
        queryset=Accessory.objects, source="accessory", write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = KitItem
        fields = [
            "id",
            "kit",
            "asset_type",
            "asset_type_id",
            "accessory",
            "accessory_id",
            "license",
            "qty",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "asset_type", "accessory", "qty"]


class KitSerializer(BaseModelSerializer):
    items = KitItemSerializer(many=True, read_only=True)
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedTenantSerializer.Meta.model.objects,
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
    )
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = Kit
        fields = ["id", "name", "description", "items", "tenant", "tenant_id", "tags", "created_at", "updated_at"]
        brief_fields = ["id", "name", "description"]


def _component_category_queryset() -> models.QuerySet[Category]:
    return Category.objects.filter(applies_to__component=True)


class NestedComponentSerializer(BaseModelSerializer):
    class Meta:
        model = Component
        fields = ["id", "name", "manufacturer"]
        brief_fields = ["id", "name"]


class ComponentSerializer(BaseModelSerializer):
    manufacturer = NestedManufacturerSerializer(read_only=True)
    manufacturer_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedManufacturerSerializer.Meta.model.objects.all(), source="manufacturer", write_only=True
    )
    category = serializers.SerializerMethodField(read_only=True)
    category_id: serializers.PrimaryKeyRelatedField[Category] = serializers.PrimaryKeyRelatedField(
        queryset=_component_category_queryset(), source="category", write_only=True, required=False, allow_null=True
    )
    tenant = NestedTenantSerializer(read_only=True)
    tenant_id: serializers.PrimaryKeyRelatedField[models.Model] = serializers.PrimaryKeyRelatedField(
        queryset=NestedTenantSerializer.Meta.model.objects,
        source="tenant",
        write_only=True,
        required=False,
        allow_null=True,
    )
    tags = TagSerializer(many=True, read_only=True)
    total_stock = serializers.IntegerField(read_only=True)
    total_allocated = serializers.IntegerField(read_only=True)
    available_stock = serializers.IntegerField(read_only=True)

    class Meta:
        model = Component
        fields = [
            "id",
            "name",
            "slug",
            "manufacturer",
            "manufacturer_id",
            "category",
            "category_id",
            "part_number",
            "specs",
            "min_qty",
            "notes",
            "allow_overallocate",
            "tags",
            "tenant",
            "tenant_id",
            "total_stock",
            "total_allocated",
            "available_stock",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "manufacturer", "part_number"]

    def get_category(self, obj: Component) -> dict[str, object] | None:
        if obj.category:
            return {"id": obj.category.pk, "name": obj.category.name, "slug": obj.category.slug}
        return None


class ComponentStockSerializer(BaseModelSerializer):
    component = NestedComponentSerializer(read_only=True)
    component_id = serializers.PrimaryKeyRelatedField(queryset=Component.objects, source="component", write_only=True)
    component_name = serializers.CharField(source="component.name", read_only=True)
    location = NestedLocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects, source="location", write_only=True
    )

    class Meta:
        model = ComponentStock
        fields = [
            "id",
            "component",
            "component_id",
            "component_name",
            "location",
            "location_id",
            "qty",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "component_name", "location", "qty"]


class ComponentAllocationSerializer(_AssignmentAvailabilityMixin, BaseModelSerializer):
    item_source_field = "component"
    component = NestedComponentSerializer(read_only=True)
    component_id = serializers.PrimaryKeyRelatedField(queryset=Component.objects, source="component", write_only=True)
    component_name = serializers.CharField(source="component.name", read_only=True)

    assigned_holder = AssetHolderSerializer(read_only=True)
    assigned_holder_id = serializers.PrimaryKeyRelatedField(
        queryset=AssetHolderSerializer.Meta.model.objects,
        source="assigned_holder",
        write_only=True,
        required=False,
        allow_null=True,
    )
    assigned_location = NestedLocationSerializer(read_only=True)
    assigned_location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects,
        source="assigned_location",
        write_only=True,
        required=False,
        allow_null=True,
    )
    assigned_asset = NestedAssetSerializer(read_only=True)
    assigned_asset_id = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, source="assigned_asset", write_only=True, required=False, allow_null=True
    )
    from_location = NestedLocationSerializer(read_only=True)
    from_location_id = serializers.PrimaryKeyRelatedField(
        queryset=NestedLocationSerializer.Meta.model.objects,
        source="from_location",
        write_only=True,
        required=False,
        allow_null=True,
    )
    tags = TagSerializer(many=True, read_only=True)

    class Meta:
        model = ComponentAllocation
        fields = [
            "id",
            "component",
            "component_id",
            "component_name",
            "assigned_holder",
            "assigned_holder_id",
            "assigned_location",
            "assigned_location_id",
            "assigned_asset",
            "assigned_asset_id",
            "from_location",
            "from_location_id",
            "qty",
            "assigned_date",
            "notes",
            "tags",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "component_name", "qty"]
