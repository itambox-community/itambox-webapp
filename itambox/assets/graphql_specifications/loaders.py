"""Request-local batching for typed specification GraphQL readers.

The existing Assets loader owns all ORM graph work.  This adapter only decides
which immutable DTO graph a request needs and memoizes it for the lifetime of
one GraphQL context.  No owner/value cache is global, and every owner cache key
contains the authorized scope fingerprint.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from graphql import GraphQLError

from assets.services.specifications.contracts import (
    FieldKey,
    SpecificationGraphLoadRequest,
    SpecificationProjectionRequest,
    SpecificationResolutionRequest,
)
from assets.services.specifications.loader import load_specification_graph
from extras.services.specifications.composition import resolve_specification_definition
from extras.services.specifications.contracts import (
    ChoiceSetDTO,
    LoadedSpecificationGraphDTO,
    SpecificationDefinitionDTO,
    SpecificationProjectionDTO,
    StoredSpecificationEntryDTO,
    TargetKind,
)
from extras.services.specifications.projection import project_specification_values

if TYPE_CHECKING:
    from .types import FieldsetView

GraphLoader = Callable[[SpecificationGraphLoadRequest], LoadedSpecificationGraphDTO]
DefinitionResolver = Callable[[SpecificationResolutionRequest], SpecificationDefinitionDTO]
ProjectionResolver = Callable[[SpecificationProjectionRequest], SpecificationProjectionDTO]

_EMPTY_GRAPH = LoadedSpecificationGraphDTO(
    type_memberships=MappingProxyType({}),
    fieldsets_by_identity=MappingProxyType({}),
    fields_by_key=MappingProxyType({}),
    global_field_keys_by_target=MappingProxyType({}),
    historical_definitions_by_key=MappingProxyType({}),
)


@dataclass(frozen=True)
class OwnerSpecificationRead:
    """The fully resolved, read-only specification view for one owner."""

    definition: SpecificationDefinitionDTO
    projection: SpecificationProjectionDTO


class RequestScopedSpecificationLoader:
    """Batch immutable definition graphs and owner projections per request."""

    def __init__(
        self,
        *,
        graph_loader: GraphLoader | None = None,
        definition_resolver: DefinitionResolver = resolve_specification_definition,
        projection_resolver: ProjectionResolver = project_specification_values,
        scope_fingerprint: str = "request",
    ) -> None:
        self._graph_loader = graph_loader or _default_graph_loader
        self._definition_resolver = definition_resolver
        self._projection_resolver = projection_resolver
        self._scope_fingerprint = scope_fingerprint
        self._graph_batches: dict[
            tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]], LoadedSpecificationGraphDTO
        ] = {}
        self._definitions: dict[tuple[int, str, tuple[str, ...]], SpecificationDefinitionDTO] = {}
        self._owner_reads: dict[tuple[str, str, int], OwnerSpecificationRead] = {}
        self._global_graphs: dict[tuple[str, ...], LoadedSpecificationGraphDTO] = {}
        self._choice_sets: dict[str, ChoiceSetDTO | None] = {}
        self._category_fieldsets: dict[int, tuple[FieldsetView, ...]] = {}

    @property
    def scope_fingerprint(self) -> str:
        return self._scope_fingerprint

    def bind_scope(self, scope_fingerprint: str) -> None:
        if type(scope_fingerprint) is not str or not scope_fingerprint:
            raise ValueError("scope_fingerprint must be a non-empty string")
        if scope_fingerprint == self._scope_fingerprint:
            return
        self._scope_fingerprint = scope_fingerprint
        # Definitions are global and remain reusable.  Owner projections are
        # explicitly scope-bound and must never be reused under a new scope.
        self._owner_reads.clear()

    def prime_category_fieldsets(self, fieldsets_by_category: Mapping[int, tuple[FieldsetView, ...]]) -> None:
        """Cache an already-batched set of global Category defaults."""
        self._category_fieldsets.update(fieldsets_by_category)

    def get_or_load_category_fieldsets(
        self,
        category_id: int,
        resolver: Callable[[], tuple[FieldsetView, ...]],
    ) -> tuple[FieldsetView, ...]:
        """Memoize one Category's defaults in this request context."""
        cached = self._category_fieldsets.get(category_id)
        if cached is None:
            cached = resolver()
            self._category_fieldsets[category_id] = cached
        return cached

    def get_or_load_choice_set(
        self,
        identity: str,
        resolver: Callable[[], ChoiceSetDTO | None],
    ) -> ChoiceSetDTO | None:
        """Memoize one global Choice Set lookup for this request only."""
        if identity not in self._choice_sets:
            self._choice_sets[identity] = resolver()
        return self._choice_sets[identity]

    def prepare_type_ids(
        self,
        type_ids: Iterable[int],
        *,
        target_kind: TargetKind | Iterable[TargetKind],
        requested_field_keys: Iterable[FieldKey | str] = (),
    ) -> LoadedSpecificationGraphDTO:
        canonical_ids = _canonical_positive_ids(type_ids)
        target_kinds = _canonical_target_kinds(target_kind)
        field_keys = _canonical_field_keys(requested_field_keys)
        if not canonical_ids:
            return self.global_graph(target_kinds)

        covering = self._find_covering_graph(canonical_ids, target_kinds, field_keys)
        if covering is not None:
            return covering

        request = SpecificationGraphLoadRequest(
            asset_type_ids=canonical_ids,
            requested_target_kinds=frozenset(target_kinds),
            requested_field_keys=frozenset(FieldKey(key) for key in field_keys),
        )
        graph = self._graph_loader(request)
        self._graph_batches[(canonical_ids, target_kinds, field_keys)] = graph
        return graph

    def graph_for_type(
        self,
        type_id: int,
        *,
        target_kind: TargetKind,
        requested_field_keys: Iterable[FieldKey | str] = (),
    ) -> LoadedSpecificationGraphDTO:
        _validate_positive_id(type_id, "asset_type_id")
        return self.prepare_type_ids(
            (type_id,),
            target_kind=target_kind,
            requested_field_keys=requested_field_keys,
        )

    def global_graph(
        self,
        target_kinds: Iterable[TargetKind] = ("asset_type", "asset"),
    ) -> LoadedSpecificationGraphDTO:
        canonical_targets = _canonical_target_kinds(target_kinds)
        cached = self._global_graphs.get(canonical_targets)
        if cached is not None:
            return cached
        request = SpecificationGraphLoadRequest(
            asset_type_ids=(),
            requested_target_kinds=frozenset(canonical_targets),
            requested_field_keys=frozenset(),
        )
        graph = self._graph_loader(request)
        self._global_graphs[canonical_targets] = graph
        return graph

    def definition_for_type(
        self,
        type_id: int,
        *,
        target_kind: TargetKind,
        requested_field_keys: Iterable[FieldKey | str] = (),
    ) -> SpecificationDefinitionDTO:
        field_keys = _canonical_field_keys(requested_field_keys)
        key = (type_id, target_kind, field_keys)
        cached = self._definitions.get(key)
        if cached is not None:
            return cached
        graph = self.graph_for_type(type_id, target_kind=target_kind, requested_field_keys=field_keys)
        definition = self._definition_resolver(
            SpecificationResolutionRequest(
                ordered_memberships=graph.type_memberships.get(type_id, ()),
                loaded_graph=graph,
                target_kind=target_kind,
            )
        )
        self._definitions[key] = definition
        return definition

    def read_owner(self, owner: object, *, target_kind: TargetKind) -> OwnerSpecificationRead:
        owner_id, type_id, stored_values = _owner_parts(owner, target_kind)
        cache_key = (self._scope_fingerprint, target_kind, owner_id)
        cached = self._owner_reads.get(cache_key)
        if cached is not None:
            return cached

        field_keys = tuple(FieldKey(str(key)) for key in stored_values)
        if type_id is None:
            graph = _EMPTY_GRAPH
            definition = self._definition_resolver(
                SpecificationResolutionRequest(
                    ordered_memberships=(),
                    loaded_graph=graph,
                    target_kind=target_kind,
                )
            )
        else:
            graph = self.graph_for_type(
                type_id,
                target_kind=target_kind,
                requested_field_keys=field_keys,
            )
            definition = self._definition_resolver(
                SpecificationResolutionRequest(
                    ordered_memberships=graph.type_memberships.get(type_id, ()),
                    loaded_graph=graph,
                    target_kind=target_kind,
                )
            )

        projection = self._projection_resolver(
            SpecificationProjectionRequest(
                definition=definition,
                stored_entries=tuple(
                    StoredSpecificationEntryDTO(key=FieldKey(str(key)), value=value)
                    for key, value in stored_values.items()
                ),
                historical_definitions_by_key=graph.historical_definitions_by_key,
            )
        )
        result = OwnerSpecificationRead(definition=definition, projection=projection)
        self._owner_reads[cache_key] = result
        return result

    def prepare_owners(
        self,
        owners: Iterable[object],
        *,
        target_kind: TargetKind,
        graph_target_kinds: Iterable[TargetKind] | None = None,
    ) -> None:
        """Prime one graph and stored-key set for all owners in a root collection."""
        type_ids: set[int] = set()
        field_keys: set[FieldKey] = set()
        graph_kinds = _canonical_target_kinds((target_kind,) if graph_target_kinds is None else graph_target_kinds)
        for owner in owners:
            _owner_id, type_id, stored_values = _owner_parts(owner, target_kind)
            if type_id is not None:
                type_ids.add(type_id)
            field_keys.update(FieldKey(str(key)) for key in stored_values)
        if type_ids:
            self.prepare_type_ids(
                type_ids,
                target_kind=graph_kinds,
                requested_field_keys=field_keys,
            )

    def _find_covering_graph(
        self,
        type_ids: tuple[int, ...],
        target_kinds: tuple[str, ...],
        field_keys: tuple[str, ...],
    ) -> LoadedSpecificationGraphDTO | None:
        requested_ids = set(type_ids)
        requested_targets = set(target_kinds)
        requested_fields = set(field_keys)
        for (loaded_ids, loaded_targets, loaded_fields), graph in self._graph_batches.items():
            if requested_ids.issubset(loaded_ids):
                if requested_targets.issubset(loaded_targets) and requested_fields.issubset(loaded_fields):
                    return graph
        return None


def request_loader_for_info(info: object) -> RequestScopedSpecificationLoader:
    """Return a loader attached to this GraphQL request context only."""
    context = getattr(info, "context", None)
    if context is None:
        raise GraphQLError(
            "A request context is required for specification reads.",
            extensions={"code": "OBJECT_UNAVAILABLE"},
        )
    if isinstance(context, Mapping):
        return _loader_for_mapping_context(context)
    return _loader_for_object_context(context)


def _validated_loader(loader: object) -> RequestScopedSpecificationLoader:
    if not isinstance(loader, RequestScopedSpecificationLoader):
        raise GraphQLError(
            "The request context contains an invalid specification loader.",
            extensions={"code": "OBJECT_UNAVAILABLE"},
        )
    return loader


def _loader_for_mapping_context(context: Mapping[str, object]) -> RequestScopedSpecificationLoader:
    key = "_itambox_specification_loader"
    loader = context.get(key)
    if loader is None:
        loader = RequestScopedSpecificationLoader()
        try:
            context[key] = loader  # type: ignore[index]
        except (AttributeError, TypeError) as exc:
            raise GraphQLError(
                "The request context cannot hold request-local state.",
                extensions={"code": "OBJECT_UNAVAILABLE"},
            ) from exc
    return _validated_loader(loader)


def _loader_for_object_context(context: object) -> RequestScopedSpecificationLoader:
    key = "_itambox_specification_loader"
    loader = getattr(context, key, None)
    if loader is None:
        loader = RequestScopedSpecificationLoader()
        try:
            setattr(context, key, loader)
        except (AttributeError, TypeError) as exc:
            raise GraphQLError(
                "The request context cannot hold request-local state.",
                extensions={"code": "OBJECT_UNAVAILABLE"},
            ) from exc
    return _validated_loader(loader)


def _default_graph_loader(request: SpecificationGraphLoadRequest) -> LoadedSpecificationGraphDTO:
    return load_specification_graph(request)


def _canonical_positive_ids(values: Iterable[int]) -> tuple[int, ...]:
    result: set[int] = set()
    for value in values:
        _validate_positive_id(value, "asset_type_id")
        result.add(value)
    return tuple(sorted(result))


def _canonical_target_kinds(value: TargetKind | Iterable[TargetKind]) -> tuple[TargetKind, ...]:
    if isinstance(value, str):
        values = (value,)
    else:
        values = tuple(value)
    if not values or any(item not in {"asset_type", "asset"} for item in values):
        raise ValueError("target_kind must contain asset_type and/or asset")
    return tuple(sorted(set(values)))


def _canonical_field_keys(values: Iterable[FieldKey | str]) -> tuple[str, ...]:
    result: set[str] = set()
    for value in values:
        if type(value) is not str or not value:
            raise ValueError("requested field keys must be non-empty strings")
        result.add(value)
    return tuple(sorted(result))


def _validate_positive_id(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _owner_parts(owner: object, target_kind: TargetKind) -> tuple[int, int | None, Mapping[str, object]]:
    if target_kind == "asset_type":
        owner_id = getattr(owner, "pk", None)
        type_id = owner_id
    else:
        owner_id = getattr(owner, "pk", None)
        type_id = getattr(owner, "asset_type_id", None)
        if type_id is None:
            asset_type = getattr(owner, "asset_type", None)
            type_id = getattr(asset_type, "pk", None)
    _validate_positive_id(owner_id, "owner_id")
    if type_id is not None:
        _validate_positive_id(type_id, "asset_type_id")
    values = getattr(owner, "custom_field_data", {})
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise ValueError("custom_field_data must be a JSON object")
    return owner_id, type_id, values


__all__ = [
    "OwnerSpecificationRead",
    "RequestScopedSpecificationLoader",
    "request_loader_for_info",
]
