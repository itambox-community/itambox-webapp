"""GraphQL adapter helpers for specification readers.

All ORM access here is at owner/graph boundaries.  Nested GraphQL types consume
DTOs prepared by :class:`RequestScopedSpecificationLoader`; they do not perform
one ORM lookup per Field, Choice, or value.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import router
from django.db.models.query import QuerySet
from graphql import GraphQLError

from assets.models import Asset, AssetType
from assets.services.specifications._command_support import resource_revision_for_owner
from assets.services.specifications.loader import (
    _assemble_current_fields,
    _assemble_fieldsets,
    _load_fieldset_memberships,
)
from extras.models import CustomFieldChoiceSet
from extras.services._definition_command_support import resource_revision_for_definition
from extras.services.specifications.contracts import (
    ChoiceDTO,
    ChoiceSetDTO,
    FieldDefinitionDTO,
    ResolvedFieldDTO,
    ResourceRevision,
    TargetKind,
)
from organization.services.access_scope import (
    AccessScopeDeniedDTO,
    AccessScopeResolutionRequestDTO,
    AccessScopeResolvedDTO,
    ActorContextDTO,
    RequestedScopeSelectorDTO,
    TenantGroupId,
    TenantId,
    authentication_revision_for_actor,
    resolve_access_scope,
)

from .loaders import RequestScopedSpecificationLoader
from .readers import fields_for_fieldset, issues_for_entries, issues_for_missing_required
from .types import FieldsetView, UserErrorType


@dataclass(frozen=True)
class PageInfoView:
    end_cursor: str | None
    has_next_page: bool


@dataclass(frozen=True)
class AssetTypeEdgeView:
    cursor: str
    node: AssetType


@dataclass(frozen=True)
class AssetTypeConnectionView:
    edges: tuple[AssetTypeEdgeView, ...]
    page_info: PageInfoView


@dataclass(frozen=True)
class SpecificationFieldEdgeView:
    cursor: str
    node: object


@dataclass(frozen=True)
class SpecificationFieldConnectionView:
    edges: tuple[SpecificationFieldEdgeView, ...]
    page_info: PageInfoView


@dataclass(frozen=True)
class ScopeReadView:
    tenant_ids: frozenset[int]
    fingerprint: str


_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 50


def authenticated_user(info: object) -> object:
    context = getattr(info, "context", None)
    if context is None:
        raise GraphQLError(
            "Authentication credentials were not provided.",
            extensions={"code": "UNAUTHENTICATED"},
        )
    if isinstance(context, Mapping):
        user = context.get("user")
    else:
        user = getattr(context, "user", None)
        if user is None:
            user = getattr(getattr(context, "request", None), "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        raise GraphQLError(
            "Authentication credentials were not provided.",
            extensions={"code": "UNAUTHENTICATED"},
        )
    return user


def has_global_permission(info: object, permission: str) -> bool:
    user = authenticated_user(info)
    return bool(user.has_perm(permission))


def require_global_permission(info: object, permission: str) -> object:
    user = authenticated_user(info)
    if not user.has_perm(permission):
        raise PermissionDenied("Permission denied.")
    return user


def resolve_read_scope(info: object, requested_scope: Mapping[str, object] | object) -> ScopeReadView | None:
    user = authenticated_user(info)
    mode = _enum_value(_input_value(requested_scope, "mode"))
    tenant_id = _positive_id_or_none(_input_value(requested_scope, "tenant_id", "tenantId"), "tenant_id")
    tenant_group_id = _positive_id_or_none(
        _input_value(requested_scope, "tenant_group_id", "tenantGroupId"),
        "tenant_group_id",
    )
    selector = RequestedScopeSelectorDTO(
        mode=mode,  # type: ignore[arg-type]
        tenant_id=None if tenant_id is None else TenantId(tenant_id),
        tenant_group_id=None if tenant_group_id is None else TenantGroupId(tenant_group_id),
    )
    actor = ActorContextDTO(
        actor_id=int(user.pk),
        authentication_revision=authentication_revision_for_actor(user),
    )
    resolved = resolve_access_scope(
        AccessScopeResolutionRequestDTO(
            actor=actor,
            selector=selector,
            operation="read_asset",
            required_permission="assets.view_asset",
        )
    )
    if isinstance(resolved, AccessScopeDeniedDTO):
        return None
    if not isinstance(resolved, AccessScopeResolvedDTO):
        return None
    return ScopeReadView(
        tenant_ids=frozenset(int(value) for value in resolved.access_scope.authorized_tenant_ids),
        fingerprint=str(resolved.access_scope.access_scope_fingerprint),
    )


def bind_scope(loader: RequestScopedSpecificationLoader, scope: ScopeReadView | None) -> None:
    if scope is not None:
        loader.bind_scope(scope.fingerprint)


def asset_queryset_for_scope(scope: ScopeReadView) -> QuerySet:
    """Create an explicitly scoped read queryset without ambient tenant state."""
    queryset = QuerySet(model=Asset, using=router.db_for_read(Asset))
    return (
        queryset.filter(deleted_at__isnull=True, tenant_id__in=scope.tenant_ids)
        .select_related(
            "asset_type",
            "asset_type__manufacturer",
            "asset_type__category",
            "asset_type__depreciation",
            "asset_type__asset_role",
            "asset_role",
            "status",
            "location",
            "location__site",
            "tenant",
            "supplier",
        )
        .prefetch_related("asset_type__manufacturer__software_products")
        .order_by("pk")
    )


def fieldsets_for_type(loader: RequestScopedSpecificationLoader, asset_type_id: int) -> tuple[FieldsetView, ...]:
    graph = loader.graph_for_type(asset_type_id, target_kind="asset_type")
    memberships = graph.type_memberships.get(asset_type_id, ())
    return tuple(
        FieldsetView(
            definition=graph.fieldsets_by_identity[membership.fieldset_identity],
            fields=fields_for_fieldset(graph.fieldsets_by_identity[membership.fieldset_identity], graph),
        )
        for membership in memberships
    )


def prepare_type_graph(loader: RequestScopedSpecificationLoader, asset_types: Sequence[AssetType]) -> None:
    type_ids = tuple(int(asset_type.pk) for asset_type in asset_types)
    if type_ids:
        loader.prepare_type_ids(type_ids, target_kind=("asset_type", "asset"))


def prepare_asset_graph(loader: RequestScopedSpecificationLoader, assets: Sequence[Asset]) -> None:
    loader.prepare_owners(assets, target_kind="asset")


def category_fieldsets_for(category: object) -> tuple[FieldsetView, ...]:
    """Read ordered Category defaults through the existing DTO assembler."""
    memberships = tuple(
        category.default_fieldset_memberships.select_related("fieldset").order_by(
            "position", "fieldset__namespace", "fieldset__slug"
        )
    )
    fieldset_ids = tuple(row.fieldset_id for row in memberships)
    if not fieldset_ids:
        return ()
    rows_by_fieldset = _load_fieldset_memberships(fieldset_ids)
    fieldsets_by_id = {row.fieldset_id: row.fieldset for row in memberships}
    _fields_by_key, fields_by_identity = _assemble_current_fields(rows_by_fieldset, {})
    fieldsets_by_identity = _assemble_fieldsets(
        fieldset_ids,
        fieldsets_by_id,
        rows_by_fieldset,
        fields_by_identity,
    )
    result: list[FieldsetView] = []
    for membership in memberships:
        identity = f"{membership.fieldset.namespace}/{membership.fieldset.slug}"
        fieldset = fieldsets_by_identity[identity]
        result.append(
            FieldsetView(
                definition=fieldset,
                fields=tuple(
                    fields_by_identity[str(field_membership.field_identity)]
                    for field_membership in fieldset.field_memberships
                ),
            )
        )
    return tuple(result)


def owner_resource_revision(owner: object) -> ResourceRevision:
    return resource_revision_for_owner(owner)


def owner_user_errors(loader: RequestScopedSpecificationLoader, owner: object, target_kind: TargetKind):
    read = loader.read_owner(owner, target_kind=target_kind)
    return tuple(
        UserErrorType(
            code=issue.code,
            path=issue.path,
            field_key=issue.field_key,
            message=issue.message,
        )
        for issue in (
            *issues_for_entries(read.projection.entries),
            *issues_for_missing_required(read.projection.missing_required_issues),
        )
    )


def choice_set_for_identity(
    identity: str,
    *,
    loader: RequestScopedSpecificationLoader | None = None,
) -> ChoiceSetDTO | None:
    def load() -> ChoiceSetDTO | None:
        return _choice_set_for_identity(identity)

    if loader is not None:
        return loader.get_or_load_choice_set(identity, load)
    return load()


def _choice_set_for_identity(identity: str) -> ChoiceSetDTO | None:
    if type(identity) is not str or identity.count("/") != 1:
        return None
    namespace, slug = identity.split("/", 1)
    choice_set = CustomFieldChoiceSet.objects.prefetch_related("choices").filter(namespace=namespace, slug=slug).first()
    if choice_set is None:
        return None
    choices = tuple(
        ChoiceDTO(
            key=choice.key,
            label=choice.label,
            lifecycle=choice.lifecycle,
            position=choice.position,
        )
        for choice in choice_set.choices.all().order_by("position", "key")
    )
    return ChoiceSetDTO(
        identity=f"{choice_set.namespace}/{choice_set.slug}",
        label=choice_set.label,
        resource_revision=resource_revision_for_definition(choice_set),
        lifecycle=choice_set.lifecycle,
        choices=choices,
    )


def asset_type_connection(
    asset_types: Sequence[AssetType],
    *,
    first: int | None,
    after: str | None,
) -> AssetTypeConnectionView:
    page_size_value = page_size(first)
    ordered = sorted(asset_types, key=lambda item: (str(item.slug), int(item.pk)))
    if after:
        after_slug, after_id = decode_cursor(after, prefix="asset-type")
        ordered = [item for item in ordered if (str(item.slug), int(item.pk)) > (after_slug, after_id)]
    page = ordered[: page_size_value + 1]
    has_next = len(page) > page_size_value
    page = page[:page_size_value]
    edges = tuple(
        AssetTypeEdgeView(
            cursor=encode_cursor("asset-type", str(item.slug), int(item.pk)),
            node=item,
        )
        for item in page
    )
    return AssetTypeConnectionView(
        edges=edges,
        page_info=PageInfoView(
            end_cursor=edges[-1].cursor if edges else None,
            has_next_page=has_next,
        ),
    )


def specification_field_connection(
    fields: Sequence[FieldDefinitionDTO | ResolvedFieldDTO],
    *,
    first: int | None,
    after: str | None,
) -> SpecificationFieldConnectionView:
    page_size_value = page_size(first)
    ordered = sorted(fields, key=lambda item: str(item.identity))
    if after:
        after_identity, _unused = decode_cursor(after, prefix="field")
        ordered = [item for item in ordered if str(item.identity) > after_identity]
    page = ordered[: page_size_value + 1]
    has_next = len(page) > page_size_value
    page = page[:page_size_value]
    edges = tuple(
        SpecificationFieldEdgeView(
            cursor=encode_cursor("field", str(item.identity), 0),
            node=item,
        )
        for item in page
    )
    return SpecificationFieldConnectionView(
        edges=edges,
        page_info=PageInfoView(
            end_cursor=edges[-1].cursor if edges else None,
            has_next_page=has_next,
        ),
    )


def encode_cursor(prefix: str, identity: str, numeric_id: int) -> str:
    payload = json.dumps([prefix, identity, numeric_id], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, prefix: str) -> tuple[str, int]:
    if type(cursor) is not str or not cursor:
        raise GraphQLError("Invalid cursor.", extensions={"code": "INVALID_CURSOR"})
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except ValueError as exc:
        raise GraphQLError("Invalid cursor.", extensions={"code": "INVALID_CURSOR"}) from exc
    if (
        type(payload) is not list
        or len(payload) != 3
        or payload[0] != prefix
        or type(payload[1]) is not str
        or type(payload[2]) is not int
        or payload[2] < 0
    ):
        raise GraphQLError("Invalid cursor.", extensions={"code": "INVALID_CURSOR"})
    return payload[1], payload[2]


def page_size(first: int | None) -> int:
    if first is None:
        return _DEFAULT_PAGE_SIZE
    if type(first) is not int or first < 1 or first > _MAX_PAGE_SIZE:
        raise GraphQLError(
            f"first must be between 1 and {_MAX_PAGE_SIZE}.",
            extensions={"code": "INVALID_RANGE"},
        )
    return first


def _input_value(value: object, *names: str) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _positive_id_or_none(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    raise GraphQLError(f"{name} must be a positive ID.", extensions={"code": "INVALID_TYPE"})


__all__ = [
    "AssetTypeConnectionView",
    "AssetTypeEdgeView",
    "PageInfoView",
    "ScopeReadView",
    "SpecificationFieldConnectionView",
    "SpecificationFieldEdgeView",
    "asset_queryset_for_scope",
    "asset_type_connection",
    "authenticated_user",
    "bind_scope",
    "choice_set_for_identity",
    "decode_cursor",
    "encode_cursor",
    "fieldsets_for_type",
    "has_global_permission",
    "owner_resource_revision",
    "owner_user_errors",
    "page_size",
    "prepare_asset_graph",
    "prepare_type_graph",
    "require_global_permission",
    "resolve_read_scope",
    "specification_field_connection",
]
