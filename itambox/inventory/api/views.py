from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import ValidationError

from inventory.filters import (
    AccessoryAssignmentFilterSet,
    AccessoryFilterSet,
    AccessoryStockFilterSet,
    ComponentAllocationFilterSet,
    ComponentFilterSet,
    ComponentStockFilterSet,
    ConsumableAssignmentFilterSet,
    ConsumableFilterSet,
    ConsumableStockFilterSet,
    KitFilterSet,
    KitItemFilterSet,
)
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
from inventory.services import (
    checkin_accessory,
    checkin_component,
    checkout_inventory_item,
    recipient_assignment_union,
    shared_stock_union,
)
from itambox.api.permissions import StrictTenantPermission, TokenPermissions
from itambox.api.viewsets import ITAMBoxModelViewSet

from .serializers import (
    AccessoryAssignmentSerializer,
    AccessorySerializer,
    AccessoryStockSerializer,
    ComponentAllocationSerializer,
    ComponentSerializer,
    ComponentStockSerializer,
    ConsumableAssignmentSerializer,
    ConsumableSerializer,
    ConsumableStockSerializer,
    KitItemSerializer,
    KitSerializer,
)


class SharedStockVisibilityMixin:
    """Read visibility for pools shared TO the active tenant (ADR-0001 4b).

    Widens list/retrieve resolution only; StrictTenantPermission keeps every
    non-SAFE method on a foreign pool a 404.
    """

    stock_model = None

    def get_queryset(self):
        return shared_stock_union(super().get_queryset(), self.stock_model)


class RecipientAssignmentVisibilityMixin:
    """Read visibility for assignments TARGETING the active tenant (ADR-0001
    4b: the recipient side of a granted checkout). Mutation stays owner-side
    via StrictTenantPermission."""

    assignment_model = None

    def get_queryset(self):
        return recipient_assignment_union(super().get_queryset(), self.assignment_model)


class AccessoryViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Accessory.objects.select_related("manufacturer", "tenant").prefetch_related("tags")
    serializer_class = AccessorySerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AccessoryFilterSet

    def get_queryset(self):
        return super().get_queryset().with_counts()


class AccessoryStockViewSet(SharedStockVisibilityMixin, ITAMBoxModelViewSet):
    stock_model = AccessoryStock
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = AccessoryStock.objects.select_related("accessory", "location").all()
    serializer_class = AccessoryStockSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AccessoryStockFilterSet


class AssignmentServiceCreateMixin:
    item_field = None

    def perform_create(self, serializer):
        is_many = getattr(serializer, "many", False)
        rows = serializer.validated_data if is_many else [serializer.validated_data]

        with transaction.atomic():
            instances = []
            for validated_row in rows:
                data = dict(validated_row)
                item = data.pop(self.item_field)
                instances.append(
                    checkout_inventory_item(
                        item,
                        data.pop("qty", 1),
                        holder=data.pop("assigned_holder", None),
                        location=data.pop("assigned_location", None),
                        asset=data.pop("assigned_asset", None),
                        source_location=data.pop("from_location", None),
                        user=self.request.user,
                        notes=data.pop("notes", ""),
                        assigned_date=data.pop("assigned_date", None),
                        **data,
                    )
                )

        serializer.instance = instances if is_many else instances[0]

    def perform_update(self, serializer):
        raise ValidationError("Assignments are immutable through the REST API; check in and create a replacement.")

    def perform_destroy(self, instance):
        if isinstance(instance, ConsumableAssignment):
            raise ValidationError("Consumable assignments cannot be deleted through the REST API.")
        checkin_service = {
            AccessoryAssignment: checkin_accessory,
            ComponentAllocation: checkin_component,
        }[type(instance)]
        checkin_service(instance.pk, user=self.request.user)


class AccessoryAssignmentViewSet(
    AssignmentServiceCreateMixin,
    RecipientAssignmentVisibilityMixin,
    ITAMBoxModelViewSet,
):
    item_field = "accessory"
    assignment_model = AccessoryAssignment
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = AccessoryAssignment.objects.select_related(
        "accessory__manufacturer", "assigned_holder", "assigned_location", "from_location"
    ).all()
    serializer_class = AccessoryAssignmentSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AccessoryAssignmentFilterSet


class ConsumableViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Consumable.objects.select_related("manufacturer", "tenant").prefetch_related("tags")
    serializer_class = ConsumableSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ConsumableFilterSet

    def get_queryset(self):
        return super().get_queryset().with_counts()


class ConsumableStockViewSet(SharedStockVisibilityMixin, ITAMBoxModelViewSet):
    stock_model = ConsumableStock
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = ConsumableStock.objects.select_related("consumable", "location").all()
    serializer_class = ConsumableStockSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ConsumableStockFilterSet


class ConsumableAssignmentViewSet(
    AssignmentServiceCreateMixin,
    RecipientAssignmentVisibilityMixin,
    ITAMBoxModelViewSet,
):
    item_field = "consumable"
    assignment_model = ConsumableAssignment
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = ConsumableAssignment.objects.select_related(
        "consumable__manufacturer", "assigned_holder", "assigned_location", "from_location"
    ).all()
    serializer_class = ConsumableAssignmentSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ConsumableAssignmentFilterSet


class KitViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Kit.objects.select_related("tenant").prefetch_related("items", "tags").all()
    serializer_class = KitSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = KitFilterSet


class KitItemViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = KitItem.objects.select_related("kit", "asset_type__manufacturer", "accessory__manufacturer").all()
    serializer_class = KitItemSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = KitItemFilterSet


class ComponentViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Component.objects.select_related("manufacturer", "tenant", "category").prefetch_related("tags")
    serializer_class = ComponentSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ComponentFilterSet

    def get_queryset(self):
        return super().get_queryset().with_counts()


class ComponentStockViewSet(SharedStockVisibilityMixin, ITAMBoxModelViewSet):
    stock_model = ComponentStock
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = ComponentStock.objects.select_related("component", "location").all()
    serializer_class = ComponentStockSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ComponentStockFilterSet


class ComponentAllocationViewSet(
    AssignmentServiceCreateMixin,
    RecipientAssignmentVisibilityMixin,
    ITAMBoxModelViewSet,
):
    item_field = "component"
    assignment_model = ComponentAllocation
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = ComponentAllocation.objects.select_related(
        "component__manufacturer", "assigned_holder", "assigned_location", "assigned_asset", "from_location"
    ).all()
    serializer_class = ComponentAllocationSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ComponentAllocationFilterSet
