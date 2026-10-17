from __future__ import annotations

import strawberry
import strawberry_django

from assets.schema import CategoryNode, ManufacturerNode, SupplierNode, TenantNode
from core.graphql_utils import check_permission, paginate_queryset, scoped_queryset

from .models import Accessory, Component, Consumable, Kit

ACCESSORY_SORTABLE_FIELDS = {
    "name",
    "-name",
    "slug",
    "-slug",
    "part_number",
    "-part_number",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}
_ACCESSORY_RELATED = ("manufacturer", "category", "supplier", "tenant")


CONSUMABLE_SORTABLE_FIELDS = {
    "name",
    "-name",
    "slug",
    "-slug",
    "part_number",
    "-part_number",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}
_CONSUMABLE_RELATED = ("manufacturer", "category", "tenant")


KIT_SORTABLE_FIELDS = {"name", "-name", "created_at", "-created_at", "updated_at", "-updated_at"}
_KIT_RELATED = ("tenant",)


COMPONENT_SORTABLE_FIELDS = {
    "name",
    "-name",
    "slug",
    "-slug",
    "part_number",
    "-part_number",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}
_COMPONENT_RELATED = ("manufacturer", "category", "tenant")


@strawberry_django.type(
    Accessory,
    name="AccessoryNode",
    fields=["id", "name", "slug", "part_number", "min_qty", "allow_overallocate", "created_at", "updated_at"],
)
class AccessoryNode:
    manufacturer: ManufacturerNode
    category: CategoryNode | None
    supplier: SupplierNode | None
    tenant: TenantNode | None


@strawberry_django.type(
    Consumable,
    name="ConsumableNode",
    fields=["id", "name", "slug", "part_number", "min_qty", "allow_overallocate", "created_at", "updated_at"],
)
class ConsumableNode:
    manufacturer: ManufacturerNode
    category: CategoryNode | None
    tenant: TenantNode | None


@strawberry_django.type(Kit, name="KitNode", fields=["id", "name", "description", "created_at", "updated_at"])
class KitNode:
    tenant: TenantNode | None


@strawberry_django.type(
    Component,
    name="ComponentNode",
    fields=["id", "name", "slug", "part_number", "allow_overallocate", "created_at", "updated_at"],
)
class ComponentNode:
    manufacturer: ManufacturerNode
    category: CategoryNode | None
    tenant: TenantNode | None

    @strawberry.field
    def min_stock_level(self) -> int | None:
        return self.min_qty

    @strawberry.field
    def description(self) -> str | None:
        return self.notes


@strawberry.type
class Query:
    @strawberry.field
    def accessories(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
    ) -> list[AccessoryNode | None] | None:
        check_permission(info, "inventory.view_accessory")
        qs = scoped_queryset(info, Accessory.objects).select_related(*_ACCESSORY_RELATED)
        if name is not None:
            qs = qs.filter(name=name)
        if sort_by and sort_by in ACCESSORY_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def accessory(self, info: strawberry.Info, id: strawberry.ID) -> AccessoryNode | None:
        check_permission(info, "inventory.view_accessory")
        try:
            return scoped_queryset(info, Accessory.objects).select_related(*_ACCESSORY_RELATED).get(pk=id)
        except Accessory.DoesNotExist:
            return None

    @strawberry.field
    def consumables(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
    ) -> list[ConsumableNode | None] | None:
        check_permission(info, "inventory.view_consumable")
        qs = scoped_queryset(info, Consumable.objects).select_related(*_CONSUMABLE_RELATED)
        if name is not None:
            qs = qs.filter(name=name)
        if sort_by and sort_by in CONSUMABLE_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def consumable(self, info: strawberry.Info, id: strawberry.ID) -> ConsumableNode | None:
        check_permission(info, "inventory.view_consumable")
        try:
            return scoped_queryset(info, Consumable.objects).select_related(*_CONSUMABLE_RELATED).get(pk=id)
        except Consumable.DoesNotExist:
            return None

    @strawberry.field
    def kits(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
    ) -> list[KitNode | None] | None:
        check_permission(info, "inventory.view_kit")
        qs = scoped_queryset(info, Kit.objects).select_related(*_KIT_RELATED)
        if name is not None:
            qs = qs.filter(name=name)
        if sort_by and sort_by in KIT_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def kit(self, info: strawberry.Info, id: strawberry.ID) -> KitNode | None:
        check_permission(info, "inventory.view_kit")
        try:
            return scoped_queryset(info, Kit.objects).select_related(*_KIT_RELATED).get(pk=id)
        except Kit.DoesNotExist:
            return None

    @strawberry.field
    def components(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
    ) -> list[ComponentNode | None] | None:
        check_permission(info, "inventory.view_component")
        qs = scoped_queryset(info, Component.objects).select_related(*_COMPONENT_RELATED)
        if name is not None:
            qs = qs.filter(name=name)
        if sort_by and sort_by in COMPONENT_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def component(self, info: strawberry.Info, id: strawberry.ID) -> ComponentNode | None:
        check_permission(info, "inventory.view_component")
        try:
            return scoped_queryset(info, Component.objects).select_related(*_COMPONENT_RELATED).get(pk=id)
        except Component.DoesNotExist:
            return None
