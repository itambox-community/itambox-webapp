from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from django.db.models import Q
from graphql import GraphQLError

from assets.services.specifications._command_support import load_prospective_definition
from core.graphql_choice_enums import choice_enum
from core.graphql_scalars import JSONString
from core.graphql_utils import paginate_queryset
from organization.models import Location, Tenant

from .graphql_specifications.inputs import RequestedScopeSelectorInput
from .graphql_specifications.integration import (
    asset_queryset_for_scope,
    asset_type_connection,
    authenticated_user,
    bind_scope,
    category_fieldsets_for,
    choice_set_for_identity,
    decode_cursor,
    fieldsets_for_type,
    has_global_permission,
    owner_resource_revision,
    owner_user_errors,
    page_size,
    prepare_asset_graph,
    prepare_type_graph,
    require_global_permission,
    resolve_read_scope,
    specification_field_connection,
)
from .graphql_specifications.loaders import request_loader_for_info
from .graphql_specifications.scalars import CursorScalar
from .graphql_specifications.types import (
    ChoiceSetType,
    PageInfoType,
    SpecificationDefinitionType,
    SpecificationEntryType,
    SpecificationFieldConnectionType,
    SpecificationFieldsetType,
    SpecificationTargetEnum,
    UserErrorType,
)
from .models import Asset, AssetRole, AssetType, Category, Depreciation, Manufacturer, StatusLabel, Supplier

if TYPE_CHECKING:  # the runtime reference stays lazy to avoid an import cycle
    from software.schema import SoftwareNode

_SCHEMA_MISSING = object()

StatusLabelTypeChoices = choice_enum(StatusLabel, "type")


@strawberry_django.type(Tenant, name="TenantNode", fields=["id", "name", "slug"])
class TenantNode:
    pass


@strawberry_django.type(Location, name="LocationNode", fields=["id", "name", "slug"])
class LocationNode:
    tenant: TenantNode | None


@strawberry_django.type(
    StatusLabel,
    name="StatusLabelNode",
    fields=["id", "name", "slug", "description", "color", "created_at", "updated_at"],
)
class StatusLabelNode:
    type: StatusLabelTypeChoices


@strawberry_django.type(
    AssetRole,
    name="AssetRoleNode",
    fields=["id", "name", "slug", "description", "color", "created_at", "updated_at"],
)
class AssetRoleNode:
    pass


@strawberry_django.type(
    Manufacturer,
    name="ManufacturerNode",
    fields=["id", "name", "slug", "description", "created_at", "updated_at"],
)
class ManufacturerNode:
    software_products: list[Annotated["SoftwareNode", strawberry.lazy("software.schema")]]


@strawberry_django.type(
    Depreciation,
    name="DepreciationNode",
    fields=["id", "name", "months", "created_at", "updated_at"],
)
class DepreciationNode:
    pass


@strawberry_django.type(
    Supplier,
    name="SupplierNode",
    fields=[
        "id",
        "name",
        "slug",
        "website",
        "portal_url",
        "account_id",
        "address",
        "notes",
        "is_active",
        "created_at",
        "updated_at",
    ],
)
class SupplierNode:
    tenant: TenantNode | None


@strawberry_django.type(
    Category,
    name="Category",
    fields=["id", "name", "slug", "color", "description", "created_at", "updated_at"],
)
class CategoryNode:
    applies_to: JSONString

    @strawberry.field
    def key(self) -> str:
        return self.slug

    @strawberry.field
    def resource_revision(self) -> str:
        return owner_resource_revision(self)

    @strawberry.field
    def default_fieldsets(self) -> list[SpecificationFieldsetType]:
        return category_fieldsets_for(self)


@strawberry_django.type(
    AssetType,
    name="AssetType",
    fields=[
        "id",
        "slug",
        "model",
        "part_number",
        "eol_months",
        "description",
        "requestable",
        "created_at",
        "updated_at",
    ],
)
class AssetTypeNode:
    manufacturer: ManufacturerNode
    depreciation: DepreciationNode | None
    category: CategoryNode | None
    asset_role: AssetRoleNode | None

    @strawberry.field
    def resource_revision(self) -> str:
        return owner_resource_revision(self)

    @strawberry.field
    def fieldsets(self, info: strawberry.Info) -> list[SpecificationFieldsetType]:
        return fieldsets_for_type(request_loader_for_info(info), int(self.pk))

    @strawberry.field
    def specification_definition(
        self, info: strawberry.Info, target: SpecificationTargetEnum
    ) -> SpecificationDefinitionType:
        loader = request_loader_for_info(info)
        return loader.definition_for_type(int(self.pk), target_kind=target.value)

    @strawberry.field
    def specification_entries(self, info: strawberry.Info) -> list[SpecificationEntryType]:
        loader = request_loader_for_info(info)
        return list(loader.read_owner(self, target_kind="asset_type").projection.entries)

    @strawberry.field
    def specification_issues(self, info: strawberry.Info) -> list[UserErrorType]:
        loader = request_loader_for_info(info)
        return list(owner_user_errors(loader, self, "asset_type"))


@strawberry_django.type(
    Asset,
    name="Asset",
    fields=[
        "id",
        "name",
        "asset_tag",
        "serial_number",
        "purchase_date",
        "order_number",
        "requestable",
        "created_at",
        "updated_at",
    ],
)
class AssetNode:
    asset_role: AssetRoleNode | None
    status: StatusLabelNode | None
    location: LocationNode | None
    tenant: TenantNode | None
    supplier: SupplierNode | None

    @strawberry.field
    def asset_type(self, info: strawberry.Info) -> AssetTypeNode | None:
        if not has_global_permission(info, "assets.view_assettype"):
            return None
        return self.asset_type

    @strawberry.field
    def resource_revision(self) -> str:
        return owner_resource_revision(self)

    @strawberry.field
    def specification_definition(self, info: strawberry.Info) -> SpecificationDefinitionType:
        loader = request_loader_for_info(info)
        return loader.read_owner(self, target_kind="asset").definition

    @strawberry.field
    def specification_entries(self, info: strawberry.Info) -> list[SpecificationEntryType]:
        loader = request_loader_for_info(info)
        return list(loader.read_owner(self, target_kind="asset").projection.entries)

    @strawberry.field
    def specification_issues(self, info: strawberry.Info) -> list[UserErrorType]:
        loader = request_loader_for_info(info)
        return list(owner_user_errors(loader, self, "asset"))


ASSET_SORTABLE_FIELDS = {
    "name",
    "-name",
    "asset_tag",
    "-asset_tag",
    "serial_number",
    "-serial_number",
    "purchase_date",
    "-purchase_date",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}


@strawberry.type(name="AssetTypeEdge")
class AssetTypeEdgeType:
    cursor: CursorScalar
    node: AssetTypeNode


@strawberry.type(name="AssetTypeConnection")
class AssetTypeConnectionType:
    edges: list[AssetTypeEdgeType]
    page_info: PageInfoType


@strawberry.type
class Query:
    @strawberry.field
    def assets(
        self,
        info: strawberry.Info,
        requested_scope: RequestedScopeSelectorInput,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
        asset_tag: str | None = None,
        serial_number: str | None = None,
        status_id: strawberry.ID | None = None,
        location_id: strawberry.ID | None = None,
    ) -> list[AssetNode | None] | None:
        authenticated_user(info)
        scope = resolve_read_scope(info, requested_scope)
        if scope is None:
            return []
        loader = request_loader_for_info(info)
        bind_scope(loader, scope)
        qs = asset_queryset_for_scope(scope)
        filters = {
            "name": name,
            "asset_tag": asset_tag,
            "serial_number": serial_number,
            "status_id": status_id,
            "location_id": location_id,
        }
        for key, val in filters.items():
            if val is not None:
                qs = qs.filter(**{key: val})
        if sort_by and sort_by in ASSET_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        items = list(paginate_queryset(qs, limit, offset))
        prepare_asset_graph(loader, items)
        return items

    @strawberry.field
    def asset(
        self, info: strawberry.Info, id: strawberry.ID, requested_scope: RequestedScopeSelectorInput
    ) -> AssetNode | None:
        authenticated_user(info)
        scope = resolve_read_scope(info, requested_scope)
        if scope is None:
            return None
        loader = request_loader_for_info(info)
        bind_scope(loader, scope)
        try:
            asset = asset_queryset_for_scope(scope).get(pk=id)
            prepare_asset_graph(loader, (asset,))
            return asset
        except Asset.DoesNotExist:
            return None

    @strawberry.field
    def asset_type(self, info: strawberry.Info, id: strawberry.ID) -> AssetTypeNode | None:
        require_global_permission(info, "assets.view_assettype")
        try:
            asset_type = AssetType.objects.get(pk=id)
        except AssetType.DoesNotExist:
            return None
        prepare_type_graph(request_loader_for_info(info), (asset_type,))
        return asset_type

    @strawberry.field
    def asset_types(
        self, info: strawberry.Info, first: int = 50, after: CursorScalar | None = None
    ) -> AssetTypeConnectionType:
        require_global_permission(info, "assets.view_assettype")
        size = page_size(first)
        asset_types_qs = AssetType.objects.order_by("slug", "pk")
        if after:
            after_slug, after_id = decode_cursor(after, prefix="asset-type")
            asset_types_qs = asset_types_qs.filter(Q(slug__gt=after_slug) | Q(slug=after_slug, pk__gt=after_id))
        asset_types = tuple(asset_types_qs[: size + 1])
        loader = request_loader_for_info(info)
        prepare_type_graph(loader, asset_types)
        return asset_type_connection(asset_types, first=size, after=None)

    @strawberry.field
    def specification_fields(
        self, info: strawberry.Info, first: int = 50, after: CursorScalar | None = None
    ) -> SpecificationFieldConnectionType:
        require_global_permission(info, "extras.view_customfield")
        loader = request_loader_for_info(info)
        graph = loader.global_graph(("asset_type", "asset"))
        fields = tuple(graph.fields_by_key.values())
        return specification_field_connection(fields, first=first, after=after)

    @strawberry.field
    def choice_set(self, info: strawberry.Info, identity: str) -> ChoiceSetType | None:
        require_global_permission(info, "extras.view_customfieldchoiceset")
        return choice_set_for_identity(identity, loader=request_loader_for_info(info))

    @strawberry.field
    def category(self, info: strawberry.Info, id: strawberry.ID) -> CategoryNode | None:
        require_global_permission(info, "assets.view_category")
        return Category.objects.filter(pk=id).first()

    @strawberry.field
    def preview_asset_type_definition(
        self,
        info: strawberry.Info,
        target: SpecificationTargetEnum,
        category_id: strawberry.ID | None = None,
        fieldsets: list[str] | None = strawberry.UNSET,
    ) -> SpecificationDefinitionType:
        require_global_permission(info, "assets.view_assettype")
        target_kind = target.value
        category = None
        if category_id is not None:
            category = Category.objects.filter(pk=category_id).first()
            if category is None:
                raise GraphQLError(
                    "The requested object is unavailable.",
                    extensions={"code": "OBJECT_UNAVAILABLE", "path": ["categoryId"]},
                )
        if fieldsets is strawberry.UNSET:
            if category is None:
                selected_fieldsets = ()
            else:
                selected_fieldsets = tuple(str(item.definition.identity) for item in category_fieldsets_for(category))
        elif fieldsets is None:
            raise GraphQLError(
                "The submitted value has an invalid type.",
                extensions={"code": "INVALID_TYPE", "path": ["fieldsets"]},
            )
        else:
            selected_fieldsets = tuple(str(identity) for identity in fieldsets)
        try:
            definition, _, _ = load_prospective_definition(selected_fieldsets, target_kind, ())
        except (KeyError, TypeError, ValueError):
            raise GraphQLError(
                "The requested object is unavailable.",
                extensions={"code": "OBJECT_UNAVAILABLE", "path": ["fieldsets"]},
            ) from None
        return definition
