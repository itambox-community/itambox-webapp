"""Bounded ORM loading for Assets specification definition graphs.

The loader is the only database-facing operation in the specification seam. It
normalizes the current ORM representation into immutable DTOs, keeps all request
state local, and leaves definition resolution and value projection to the pure
Extras services.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch, Q

from assets.models.catalog import AssetTypeFieldset
from extras.models import CustomField, CustomFieldChoice, CustomFieldset, CustomFieldsetField
from extras.services.specifications.contracts import (
    ChoiceDTO,
    ChoiceSetDTO,
    FieldDefinitionDTO,
    FieldKey,
    LoadedSpecificationGraphDTO,
    OrderedFieldMembershipDTO,
    OrderedFieldsetMembershipDTO,
    PersistedFieldsetDTO,
    QualifiedIdentity,
    ResourceRevision,
    SpecificationValidationDTO,
    TargetKind,
)

from .contracts import SpecificationGraphLoadRequest

# This is deliberately a private implementation bound. Query work grows with
# the number of chunks, not with the number of Types, Fields, or Choice Sets.
_BATCH_SIZE = 100
_VALID_TARGET_KINDS = frozenset({"asset_type", "asset"})
_VALID_LIFECYCLES = frozenset({"active", "deprecated"})
_VALID_ACTIVATIONS = frozenset({"composed", "global"})
_FIELD_TYPE_MAP = {
    "text": "text",
    "integer": "integer",
    "decimal": "decimal",
    "boolean": "boolean",
    "date": "date",
    "single-select": "single_select",
    "single_select": "single_select",
    "multi-select": "multi_select",
    "multi_select": "multi_select",
}
_TARGET_MODEL_BY_KIND = {"asset_type": "assettype", "asset": "asset"}
_TARGET_KIND_BY_CONTENT_TYPE = {
    ("assets", "assettype"): "asset_type",
    ("assets", "asset"): "asset",
}

_FIELDSET_SELECT_RELATED = (
    "fieldset",
    "custom_field",
    "custom_field__choice_set",
)
_FIELD_SELECT_RELATED = ("choice_set",)


def _validate_type_ids(raw_type_ids: Any) -> tuple[int, ...]:
    if type(raw_type_ids) is not tuple:
        raise TypeError("asset_type_ids must be a tuple")
    type_ids: set[int] = set()
    for value in raw_type_ids:
        if type(value) is not int or value < 1:
            raise ValueError("asset_type_ids must contain positive integers")
        type_ids.add(value)
    return tuple(sorted(type_ids))


def _validate_target_kinds(raw_target_kinds: Any) -> tuple[str, ...]:
    if type(raw_target_kinds) is not frozenset:
        raise TypeError("requested_target_kinds must be a frozenset")
    values = tuple(raw_target_kinds)
    if any(type(value) is not str for value in values):
        raise ValueError("requested_target_kinds must contain strings")
    target_kinds = tuple(sorted(values))
    if not set(target_kinds).issubset(_VALID_TARGET_KINDS):
        raise ValueError("requested_target_kinds contains an unsupported target")
    return target_kinds


def _validate_field_keys(raw_field_keys: Any) -> tuple[str, ...]:
    if type(raw_field_keys) is not frozenset:
        raise TypeError("requested_field_keys must be a frozenset")
    field_keys: set[str] = set()
    for value in raw_field_keys:
        if type(value) is not str or not value:
            raise ValueError("requested_field_keys must contain non-empty strings")
        field_keys.add(value)
    return tuple(sorted(field_keys))


def _validate_fieldset_identities(raw_identities: Any) -> tuple[str, ...]:
    if type(raw_identities) is not tuple:
        raise TypeError("fieldset_identities must be a tuple")
    identities: list[str] = []
    seen: set[str] = set()
    for value in raw_identities:
        if type(value) is not str or value.count("/") != 1:
            raise ValueError("fieldset_identities must contain qualified identities")
        namespace, slug = value.split("/")
        if not namespace or not slug:
            raise ValueError("fieldset_identities must contain non-empty qualified identities")
        if value in seen:
            raise ValueError(f"duplicate Fieldset identity: {value}")
        identities.append(value)
        seen.add(value)
    return tuple(identities)


def _validate_request(
    request: SpecificationGraphLoadRequest,
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]]:
    """Validate transport-independent request shape before touching a manager."""
    if not isinstance(request, SpecificationGraphLoadRequest):
        raise TypeError("request must be a SpecificationGraphLoadRequest")
    return (
        _validate_type_ids(request.asset_type_ids),
        _validate_target_kinds(request.requested_target_kinds),
        _validate_field_keys(request.requested_field_keys),
    )


def _chunks(values: Sequence[Any], size: int | None = None) -> Iterable[tuple[Any, ...]]:
    size = _BATCH_SIZE if size is None else size
    if size < 1:
        raise ValueError("loader batch size must be positive")
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


def _safe_attr(instance: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(instance, name)
    except (AttributeError, ObjectDoesNotExist):
        return default


def _relation_rows(instance: Any, name: str) -> tuple[Any, ...]:
    relation = _safe_attr(instance, name)
    if relation is None:
        return ()
    all_method = getattr(relation, "all", None)
    if callable(all_method):
        return tuple(all_method())
    return tuple(relation)


def _positive_ordinal(value: Any, description: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{description} must be a positive integer")
    return value


def _lifecycle(value: Any, description: str) -> str:
    if value not in _VALID_LIFECYCLES:
        raise ValueError(f"unsupported {description} lifecycle: {value!r}")
    return str(value)


def _activation(value: Any) -> str:
    if value not in _VALID_ACTIVATIONS:
        raise ValueError(f"unsupported Field activation: {value!r}")
    return str(value)


def _field_type(value: Any) -> str:
    try:
        return _FIELD_TYPE_MAP[value]
    except KeyError as exc:
        raise ValueError(f"unsupported Field type: {value!r}") from exc


def _qualified_identity(namespace: Any, local_name: Any, description: str) -> QualifiedIdentity:
    if type(namespace) is not str or not namespace:
        raise ValueError(f"{description} namespace must be a non-empty string")
    if type(local_name) is not str or not local_name:
        raise ValueError(f"{description} identity component must be a non-empty string")
    return QualifiedIdentity(f"{namespace}/{local_name}")


def _revision(payload: Mapping[str, Any]) -> ResourceRevision:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ResourceRevision("sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest())


def _target_kinds(field: Any) -> frozenset[TargetKind]:
    targets: set[TargetKind] = set()
    for content_type in _relation_rows(field, "object_types"):
        app_label = _safe_attr(content_type, "app_label")
        model = _safe_attr(content_type, "model")
        target = _TARGET_KIND_BY_CONTENT_TYPE.get((app_label, model))
        if target is not None:
            targets.add(target)
    return frozenset(targets)


def _choice_set_dto(choice_set: Any, cache: dict[str, ChoiceSetDTO]) -> ChoiceSetDTO:
    identity = _qualified_identity(
        _safe_attr(choice_set, "namespace"),
        _safe_attr(choice_set, "slug"),
        "Choice Set",
    )
    identity_key = str(identity)
    cached = cache.get(identity_key)
    if cached is not None:
        return cached

    lifecycle = _lifecycle(_safe_attr(choice_set, "lifecycle"), "Choice Set")
    choices = tuple(
        sorted(
            _relation_rows(choice_set, "choices"),
            key=lambda item: (
                _positive_ordinal(_safe_attr(item, "position"), "Choice position"),
                str(_safe_attr(item, "key", "")),
            ),
        )
    )
    choice_dtos: list[ChoiceDTO] = []
    choice_revision_rows: list[dict[str, Any]] = []
    for choice in choices:
        key = _safe_attr(choice, "key")
        if type(key) is not str or not key:
            raise ValueError("Choice key must be a non-empty string")
        choice_lifecycle = _lifecycle(_safe_attr(choice, "lifecycle"), "Choice")
        position = _positive_ordinal(_safe_attr(choice, "position"), "Choice position")
        choice_dtos.append(
            ChoiceDTO(
                key=key,
                label=str(_safe_attr(choice, "label", "")),
                lifecycle=choice_lifecycle,  # type: ignore[arg-type]
                position=position,
            )
        )
        choice_revision_rows.append(
            {
                "key": key,
                "label": str(_safe_attr(choice, "label", "")),
                "lifecycle": choice_lifecycle,
                "position": position,
                "version": _safe_attr(choice, "version"),
                "replaced_by": _safe_attr(choice, "replaced_by"),
            }
        )

    revision = _revision(
        {
            "kind": "choice_set",
            "identity": identity_key,
            "label": str(_safe_attr(choice_set, "label", "")),
            "lifecycle": lifecycle,
            "version": _safe_attr(choice_set, "version"),
            "management_kind": _safe_attr(choice_set, "management_kind"),
            "connector_identity": _safe_attr(choice_set, "connector_identity"),
            "choices": choice_revision_rows,
        }
    )
    result = ChoiceSetDTO(
        identity=identity,
        label=str(_safe_attr(choice_set, "label", "")),
        resource_revision=revision,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        choices=tuple(choice_dtos),
    )
    cache[identity_key] = result
    return result


def _field_dto(field: Any, choice_set_cache: dict[str, ChoiceSetDTO]) -> FieldDefinitionDTO:
    name = _safe_attr(field, "name")
    if type(name) is not str or not name:
        raise ValueError("Field key must be a non-empty string")
    namespace = _safe_attr(field, "namespace")
    identity = _qualified_identity(namespace, name, "Field")
    targets = _target_kinds(field)
    activation = _activation(_safe_attr(field, "activation"))
    lifecycle = _lifecycle(_safe_attr(field, "lifecycle"), "Field")
    normalized_type = _field_type(_safe_attr(field, "field_type"))
    choice_set = _safe_attr(field, "choice_set")
    choice_set_dto = None if choice_set is None else _choice_set_dto(choice_set, choice_set_cache)
    validation = SpecificationValidationDTO(
        minimum=None if _safe_attr(field, "minimum_value") is None else str(_safe_attr(field, "minimum_value")),
        maximum=None if _safe_attr(field, "maximum_value") is None else str(_safe_attr(field, "maximum_value")),
        scale=_safe_attr(field, "decimal_scale"),
        max_length=_safe_attr(field, "text_max_length"),
        max_values=_safe_attr(field, "max_values"),
        regex=_safe_attr(field, "regex"),
        rule=_safe_attr(field, "validation_rule"),
    )
    resource_revision = _revision(
        {
            "kind": "field",
            "identity": str(identity),
            "key": name,
            "label": str(_safe_attr(field, "label", "")),
            "help_text": str(_safe_attr(field, "help_text", "")),
            "targets": sorted(targets),
            "activation": activation,
            "field_type": normalized_type,
            "quantity_kind": _safe_attr(field, "quantity_kind"),
            "canonical_unit": _safe_attr(field, "canonical_unit"),
            "validation": {
                "minimum": validation.minimum,
                "maximum": validation.maximum,
                "scale": validation.scale,
                "max_length": validation.max_length,
                "max_values": validation.max_values,
                "regex": validation.regex,
                "rule": validation.rule,
            },
            "required": bool(_safe_attr(field, "required", False)),
            "nullable": bool(_safe_attr(field, "nullable", False)),
            "lifecycle": lifecycle,
            "version": _safe_attr(field, "version"),
            "management_kind": _safe_attr(field, "management_kind"),
            "connector_identity": _safe_attr(field, "connector_identity"),
            "replaced_by": _safe_attr(field, "replaced_by"),
            "choice_set": (
                None
                if choice_set_dto is None
                else {
                    "identity": str(choice_set_dto.identity),
                    "resource_revision": str(choice_set_dto.resource_revision),
                }
            ),
        }
    )
    return FieldDefinitionDTO(
        resource_revision=resource_revision,
        key=FieldKey(name),
        identity=identity,
        label=str(_safe_attr(field, "label", "")),
        help_text=str(_safe_attr(field, "help_text", "")),
        targets=targets,
        activation=activation,  # type: ignore[arg-type]
        field_type=normalized_type,  # type: ignore[arg-type]
        quantity_kind=_safe_attr(field, "quantity_kind"),
        canonical_unit=_safe_attr(field, "canonical_unit"),
        validation=validation,
        required=bool(_safe_attr(field, "required", False)),
        nullable=bool(_safe_attr(field, "nullable", False)),
        lifecycle=lifecycle,  # type: ignore[arg-type]
        choice_set=choice_set_dto,
    )


def _merge_field(
    fields_by_key: dict[FieldKey, FieldDefinitionDTO],
    fields_by_identity: dict[str, FieldDefinitionDTO],
    field: FieldDefinitionDTO,
) -> None:
    identity_key = str(field.identity)
    existing_identity = fields_by_identity.get(identity_key)
    if existing_identity is not None and existing_identity != field:
        raise ValueError(f"conflicting Field identity: {identity_key}")
    fields_by_identity[identity_key] = field

    existing_key = fields_by_key.get(field.key)
    if existing_key is not None and existing_key != field:
        raise ValueError(f"conflicting Field key: {field.key}")
    fields_by_key[field.key] = field


def _fieldset_id(row: Any) -> Any:
    fieldset_id = _safe_attr(row, "fieldset_id")
    if fieldset_id is not None:
        return fieldset_id
    fieldset = _safe_attr(row, "fieldset")
    identity = _safe_attr(fieldset, "pk")
    if identity is not None:
        return identity
    return str(
        _qualified_identity(
            _safe_attr(fieldset, "namespace"),
            _safe_attr(fieldset, "slug"),
            "Fieldset",
        )
    )


def _fieldset_dto(
    fieldset: Any,
    membership_rows: Sequence[Any],
    fields_by_identity: Mapping[str, FieldDefinitionDTO],
) -> PersistedFieldsetDTO:
    identity = _qualified_identity(
        _safe_attr(fieldset, "namespace"),
        _safe_attr(fieldset, "slug"),
        "Fieldset",
    )
    lifecycle = _lifecycle(_safe_attr(fieldset, "lifecycle"), "Fieldset")
    normalized_memberships: list[OrderedFieldMembershipDTO] = []
    revision_memberships: list[dict[str, Any]] = []
    for row in sorted(
        membership_rows,
        key=lambda item: (
            _positive_ordinal(_safe_attr(item, "position"), "Fieldset membership position"),
            str(_safe_attr(_safe_attr(item, "custom_field"), "name", "")),
        ),
    ):
        field = _safe_attr(row, "custom_field")
        field_identity = str(
            _qualified_identity(
                _safe_attr(field, "namespace"),
                _safe_attr(field, "name"),
                "Field",
            )
        )
        field_definition = fields_by_identity.get(field_identity)
        if field_definition is None:
            raise ValueError(f"unresolved Fieldset member: {field_identity}")
        ordinal = _positive_ordinal(_safe_attr(row, "position"), "Fieldset membership position")
        normalized_memberships.append(
            OrderedFieldMembershipDTO(
                field_identity=QualifiedIdentity(field_identity),
                ordinal=ordinal,
            )
        )
        revision_memberships.append(
            {
                "field_identity": field_identity,
                "ordinal": ordinal,
                "field_resource_revision": str(field_definition.resource_revision),
            }
        )

    revision = _revision(
        {
            "kind": "fieldset",
            "identity": str(identity),
            "label": str(_safe_attr(fieldset, "label", "")),
            "description": str(_safe_attr(fieldset, "description", "")),
            "lifecycle": lifecycle,
            "version": _safe_attr(fieldset, "version"),
            "management_kind": _safe_attr(fieldset, "management_kind"),
            "connector_identity": _safe_attr(fieldset, "connector_identity"),
            "replaced_by": _safe_attr(fieldset, "replaced_by"),
            "field_memberships": revision_memberships,
        }
    )
    return PersistedFieldsetDTO(
        identity=identity,
        label=str(_safe_attr(fieldset, "label", "")),
        description=str(_safe_attr(fieldset, "description", "")),
        resource_revision=revision,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        field_memberships=tuple(normalized_memberships),
    )


def _definition_queryset(queryset: Any, *, with_fieldset: bool = False) -> Any:
    select_related = _FIELDSET_SELECT_RELATED if with_fieldset else _FIELD_SELECT_RELATED
    queryset = queryset.select_related(*select_related)
    if with_fieldset:
        queryset = queryset.prefetch_related(
            "custom_field__object_types",
            Prefetch(
                "custom_field__choice_set__choices",
                queryset=CustomFieldChoice.objects.order_by("position", "key"),
            ),
        )
    else:
        queryset = queryset.prefetch_related(
            "object_types",
            Prefetch("choice_set__choices", queryset=CustomFieldChoice.objects.order_by("position", "key")),
        )
    return queryset


def _load_type_memberships(
    type_ids: Sequence[int],
) -> tuple[dict[int, list[Any]], dict[Any, Any]]:
    rows_by_type: dict[int, list[Any]] = defaultdict(list)
    fieldsets_by_id: dict[Any, Any] = {}
    for chunk in _chunks(type_ids):
        rows = (
            AssetTypeFieldset.objects.filter(asset_type_id__in=chunk)
            .select_related("fieldset")
            .order_by("asset_type_id", "position", "fieldset__namespace", "fieldset__slug", "fieldset_id")
        )
        for row in rows:
            type_id = _safe_attr(row, "asset_type_id")
            if type(type_id) is not int or type_id < 1:
                raise ValueError("AssetTypeFieldset returned an invalid Asset Type ID")
            fieldset = _safe_attr(row, "fieldset")
            if fieldset is None:
                raise ValueError("AssetTypeFieldset returned an unresolved Fieldset")
            rows_by_type[type_id].append(row)
            fieldset_id = _fieldset_id(row)
            previous = fieldsets_by_id.get(fieldset_id)
            if previous is not None and previous != fieldset:
                raise ValueError(f"conflicting Fieldset row: {fieldset_id}")
            fieldsets_by_id[fieldset_id] = fieldset
    return rows_by_type, fieldsets_by_id


def _load_fieldset_memberships(fieldset_ids: Sequence[Any]) -> dict[Any, list[Any]]:
    rows_by_fieldset: dict[Any, list[Any]] = defaultdict(list)
    for chunk in _chunks(tuple(fieldset_ids)):
        rows = _definition_queryset(
            CustomFieldsetField.objects.filter(fieldset_id__in=chunk),
            with_fieldset=True,
        ).order_by("fieldset_id", "position", "custom_field__namespace", "custom_field__name", "custom_field_id")
        for row in rows:
            rows_by_fieldset[_fieldset_id(row)].append(row)
    return rows_by_fieldset


def _load_global_fields(target_kinds: Sequence[str]) -> tuple[Any, ...]:
    if not target_kinds:
        return ()
    target_models = tuple(_TARGET_MODEL_BY_KIND[kind] for kind in target_kinds)
    queryset = (
        _definition_queryset(
            CustomField.objects.filter(
                activation=CustomField.ACTIVATION_GLOBAL,
                object_types__app_label="assets",
                object_types__model__in=target_models,
            )
        )
        .distinct()
        .order_by("name", "namespace", "pk")
    )
    return tuple(
        sorted(
            queryset,
            key=lambda item: (
                str(_safe_attr(item, "name", "")),
                str(_safe_attr(item, "namespace", "")),
                str(_safe_attr(item, "pk", "")),
            ),
        )
    )


def _load_historical_fields(field_keys: Sequence[str]) -> tuple[Any, ...]:
    rows: list[Any] = []
    for chunk in _chunks(field_keys):
        queryset = _definition_queryset(CustomField.objects.filter(name__in=chunk)).order_by("name", "namespace", "pk")
        rows.extend(queryset)
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                str(_safe_attr(item, "name", "")),
                str(_safe_attr(item, "namespace", "")),
                str(_safe_attr(item, "pk", "")),
            ),
        )
    )


def _assemble_current_fields(
    fieldset_rows: Mapping[Any, Sequence[Any]],
    choice_set_cache: dict[str, ChoiceSetDTO],
) -> tuple[dict[FieldKey, FieldDefinitionDTO], dict[str, FieldDefinitionDTO]]:
    fields_by_key: dict[FieldKey, FieldDefinitionDTO] = {}
    fields_by_identity: dict[str, FieldDefinitionDTO] = {}
    for rows in fieldset_rows.values():
        for row in rows:
            field = _field_dto(_safe_attr(row, "custom_field"), choice_set_cache)
            _merge_field(fields_by_key, fields_by_identity, field)
    return fields_by_key, fields_by_identity


def _assemble_fieldsets(
    fieldset_ids: Sequence[Any],
    fieldsets_by_id: Mapping[Any, Any],
    fieldset_rows: Mapping[Any, Sequence[Any]],
    fields_by_identity: Mapping[str, FieldDefinitionDTO],
) -> dict[QualifiedIdentity, PersistedFieldsetDTO]:
    fieldsets_by_identity: dict[QualifiedIdentity, PersistedFieldsetDTO] = {}
    for fieldset_id in fieldset_ids:
        dto = _fieldset_dto(fieldsets_by_id[fieldset_id], fieldset_rows.get(fieldset_id, ()), fields_by_identity)
        existing = fieldsets_by_identity.get(dto.identity)
        if existing is not None and existing != dto:
            raise ValueError(f"conflicting Fieldset identity: {dto.identity}")
        fieldsets_by_identity[dto.identity] = dto
    return fieldsets_by_identity


def _assemble_global_fields(
    target_kinds: Sequence[str],
    fields_by_key: dict[FieldKey, FieldDefinitionDTO],
    fields_by_identity: dict[str, FieldDefinitionDTO],
    choice_set_cache: dict[str, ChoiceSetDTO],
) -> dict[str, set[FieldKey]]:
    global_keys_by_target: dict[str, set[FieldKey]] = defaultdict(set)
    for field_row in _load_global_fields(target_kinds):
        field = _field_dto(field_row, choice_set_cache)
        _merge_field(fields_by_key, fields_by_identity, field)
        if field.activation != "global":
            raise ValueError(f"global query returned a non-global Field: {field.key}")
        for target_kind in target_kinds:
            if target_kind in field.targets:
                global_keys_by_target[target_kind].add(field.key)
    return global_keys_by_target


def _assemble_historical_fields(
    requested_field_keys: Sequence[str],
    fields_by_key: Mapping[FieldKey, FieldDefinitionDTO],
    choice_set_cache: dict[str, ChoiceSetDTO],
) -> dict[FieldKey, FieldDefinitionDTO]:
    requested_key_set = frozenset(requested_field_keys)
    historical_by_key = {
        field_key: fields_by_key[field_key] for field_key in fields_by_key if str(field_key) in requested_key_set
    }
    missing_history_keys = tuple(key for key in requested_field_keys if FieldKey(key) not in historical_by_key)
    for field_row in _load_historical_fields(missing_history_keys):
        field = _field_dto(field_row, choice_set_cache)
        if str(field.key) not in requested_key_set:
            continue
        existing = historical_by_key.get(field.key)
        if existing is not None and existing != field:
            raise ValueError(f"conflicting historical Field key: {field.key}")
        historical_by_key[field.key] = field
    return historical_by_key


def _type_membership_dto(row: Any) -> OrderedFieldsetMembershipDTO:
    fieldset = _safe_attr(row, "fieldset")
    ordinal = _positive_ordinal(_safe_attr(row, "position"), "AssetType Fieldset position")
    identity = _qualified_identity(
        _safe_attr(fieldset, "namespace"),
        _safe_attr(fieldset, "slug"),
        "Fieldset",
    )
    return OrderedFieldsetMembershipDTO(fieldset_identity=identity, ordinal=ordinal)


def _assemble_type_memberships(
    type_ids: Sequence[int],
    rows_by_type: Mapping[int, Sequence[Any]],
) -> dict[int, tuple[OrderedFieldsetMembershipDTO, ...]]:
    return {
        type_id: tuple(
            sorted(
                (_type_membership_dto(row) for row in rows_by_type.get(type_id, ())),
                key=lambda item: (item.ordinal, str(item.fieldset_identity)),
            )
        )
        for type_id in type_ids
    }


def _assemble_graph_from_fieldsets(
    type_ids: Sequence[int],
    target_kinds: Sequence[str],
    requested_field_keys: Sequence[str],
    rows_by_type: Mapping[int, Sequence[Any]],
    fieldsets_by_id: Mapping[Any, Any],
) -> LoadedSpecificationGraphDTO:
    fieldset_ids = tuple(sorted(fieldsets_by_id, key=lambda value: str(value)))
    fieldset_rows = _load_fieldset_memberships(fieldset_ids)
    choice_set_cache: dict[str, ChoiceSetDTO] = {}
    fields_by_key, fields_by_identity = _assemble_current_fields(fieldset_rows, choice_set_cache)
    fieldsets_by_identity = _assemble_fieldsets(fieldset_ids, fieldsets_by_id, fieldset_rows, fields_by_identity)
    global_keys_by_target = _assemble_global_fields(
        target_kinds,
        fields_by_key,
        fields_by_identity,
        choice_set_cache,
    )
    historical_by_key = _assemble_historical_fields(requested_field_keys, fields_by_key, choice_set_cache)
    type_memberships = _assemble_type_memberships(type_ids, rows_by_type)

    ordered_fieldsets = {
        identity: fieldsets_by_identity[identity] for identity in sorted(fieldsets_by_identity, key=str)
    }
    ordered_fields = {key: fields_by_key[key] for key in sorted(fields_by_key, key=str)}
    ordered_globals = {
        target_kind: tuple(sorted(global_keys_by_target.get(target_kind, ()), key=str))
        for target_kind in sorted(target_kinds)
    }
    ordered_history = {key: historical_by_key[key] for key in sorted(historical_by_key, key=str)}
    ordered_types = {type_id: type_memberships[type_id] for type_id in type_ids}

    return LoadedSpecificationGraphDTO(
        type_memberships=MappingProxyType(ordered_types),
        fieldsets_by_identity=MappingProxyType(ordered_fieldsets),
        fields_by_key=MappingProxyType(ordered_fields),
        global_field_keys_by_target=MappingProxyType(ordered_globals),
        historical_definitions_by_key=MappingProxyType(ordered_history),
    )


def _assemble_graph(
    type_ids: Sequence[int],
    target_kinds: Sequence[str],
    requested_field_keys: Sequence[str],
) -> LoadedSpecificationGraphDTO:
    rows_by_type, fieldsets_by_id = _load_type_memberships(type_ids)
    return _assemble_graph_from_fieldsets(
        type_ids,
        target_kinds,
        requested_field_keys,
        rows_by_type,
        fieldsets_by_id,
    )


def _load_prospective_fieldsets(identities: Sequence[str]) -> dict[Any, Any]:
    fieldsets_by_id: dict[Any, Any] = {}
    for chunk in _chunks(tuple(identities)):
        predicate = Q()
        for identity in chunk:
            namespace, slug = identity.split("/", 1)
            predicate |= Q(namespace=namespace, slug=slug)
        rows = CustomFieldset.objects.filter(predicate).order_by("namespace", "slug", "pk")
        for fieldset in rows:
            identity = f"{_safe_attr(fieldset, 'namespace')}/{_safe_attr(fieldset, 'slug')}"
            if identity in chunk:
                fieldsets_by_id[_safe_attr(fieldset, "pk")] = fieldset
    expected = set(identities)
    found = {
        f"{_safe_attr(fieldset, 'namespace')}/{_safe_attr(fieldset, 'slug')}" for fieldset in fieldsets_by_id.values()
    }
    missing = sorted(expected - found)
    if missing:
        raise ValueError(f"unresolved prospective Fieldset identity: {missing[0]}")
    return fieldsets_by_id


def fieldset_ids_for_identities(identities: Sequence[str], *, using: str) -> tuple[int | None, ...]:
    if not identities:
        return ()
    predicate = Q()
    for identity in identities:
        namespace, slug = identity.split("/", 1)
        predicate |= Q(namespace=namespace, slug=slug)
    rows = CustomFieldset.objects.using(using).filter(predicate).values("pk", "namespace", "slug")
    by_identity = {f"{row['namespace']}/{row['slug']}": row["pk"] for row in rows}
    return tuple(by_identity.get(identity) for identity in identities)


def load_prospective_specification_graph(
    *,
    fieldset_identities: tuple[QualifiedIdentity, ...],
    requested_target_kinds: frozenset[TargetKind],
    requested_field_keys: frozenset[FieldKey],
) -> LoadedSpecificationGraphDTO:
    """Load a graph for an explicit Fieldset list without reading an owner row."""
    identities = _validate_fieldset_identities(fieldset_identities)
    target_kinds = _validate_target_kinds(requested_target_kinds)
    field_keys = _validate_field_keys(requested_field_keys)
    fieldsets_by_id = _load_prospective_fieldsets(identities)
    return _assemble_graph_from_fieldsets((), target_kinds, field_keys, {}, fieldsets_by_id)


def load_specification_graph(request: SpecificationGraphLoadRequest) -> LoadedSpecificationGraphDTO:
    """Load one immutable, batched graph for the requested Asset Types.

    Repeated IDs and keys are deduplicated only inside this call. No cache or
    owner/value lookup crosses request boundaries; the loader never reads
    ``Asset.custom_field_data`` or ``AssetType.custom_field_data``.
    """
    type_ids, target_kinds, requested_field_keys = _validate_request(request)
    return _assemble_graph(type_ids, target_kinds, requested_field_keys)


__all__ = ["load_prospective_specification_graph", "load_specification_graph"]
