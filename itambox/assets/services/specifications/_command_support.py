"""Private support for the locked specification value commands.

This module owns only command plumbing: stable owner revisions, conversion of pure codec issues, and the short
actor context needed by the existing ChangeLoggingMixin.  It does not define a
second specification resolver or value codec.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, transaction

from assets.models.asset import Asset
from assets.models.catalog import AssetType, AssetTypeFieldset, Category, CategoryDefaultFieldset
from assets.services.specifications.contracts import (
    CommandRejectedDTO,
    DefinitionRevision,
    DomainIssueDTO,
    OrderedFieldsetMembershipDTO,
    OwnerRefDTO,
    QualifiedIdentity,
    ResourceRevision,
    SpecificationGraphLoadRequest,
    SpecificationPatchDTO,
    SpecificationResolutionRequest,
)
from assets.services.specifications.loader import (
    load_prospective_specification_graph,
    load_specification_graph,
)
from extras.services.custom_field_data import actor_change_context, write_custom_field_data
from extras.services.specifications.codecs import (
    NormalizedSpecificationPatch,
    SpecificationCodecError,
    normalize_specification_patch,
)
from extras.services.specifications.composition import resolve_specification_definition
from extras.services.specifications.contracts import (
    FieldDefinitionDTO,
    FieldKey,
    LoadedSpecificationGraphDTO,
    ResolvedFieldDTO,
    SpecificationDefinitionDTO,
    TargetKind,
)
from organization.services.access_scope import (
    ActorContextDTO,
    authentication_revision_for_actor,
)

_OBJECT_UNAVAILABLE_MESSAGE = "specifications.object_unavailable"
_STALE_RESOURCE_MESSAGE = "specifications.stale_resource"
_STALE_DEFINITION_MESSAGE = "specifications.stale_definition"
_STALE_PLAN_MESSAGE = "specifications.stale_plan"
_REFERENCE_CONFLICT_MESSAGE = "specifications.reference_conflict"
_UNSUPPORTED_STRUCTURE_MESSAGE = "specifications.unsupported_structure"


def positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def revision_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def issue(
    code: str,
    *,
    path: Sequence[str] = (),
    field_key: FieldKey | None = None,
    message_key: str | None = None,
) -> DomainIssueDTO:
    return DomainIssueDTO(
        code=code,  # type: ignore[arg-type]
        path=tuple(path),
        field_key=field_key,
        message_key=message_key or f"specifications.{code.lower()}",
    )


def unavailable() -> CommandRejectedDTO:
    return CommandRejectedDTO(
        outcome="rejected",
        safe_owner=None,
        issues=(issue("OBJECT_UNAVAILABLE", message_key=_OBJECT_UNAVAILABLE_MESSAGE),),
    )


def rejected(owner: OwnerRefDTO | None, *issues: DomainIssueDTO) -> CommandRejectedDTO:
    return CommandRejectedDTO(outcome="rejected", safe_owner=owner, issues=tuple(issues))


def codec_issues(error: SpecificationCodecError) -> tuple[DomainIssueDTO, ...]:
    return tuple(
        DomainIssueDTO(
            code=codec_issue.code,  # type: ignore[arg-type]
            path=codec_issue.path,
            field_key=codec_issue.field_key,
            message_key=codec_issue.message_key,
        )
        for codec_issue in error.issues
    )


def _canonical_value(value: object) -> object:
    """Return a deterministic JSON-compatible representation for revisions."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(nested) for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(nested) for nested in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    return str(value)


def _owner_resource_payload(owner: Asset | AssetType | Category) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "model": owner._meta.label_lower,
        "owner_id": owner.pk,
    }
    for field in owner._meta.concrete_fields:
        # These timestamps are bookkeeping, not resource identity. In
        # particular, a save that only advances updated_at must not stale a
        # specification plan.
        if field.name in {"created_at", "updated_at"}:
            continue
        payload[field.name] = _canonical_value(getattr(owner, field.attname))

    if isinstance(owner, AssetType):
        payload["composition"] = [
            {
                "fieldset_id": row["fieldset_id"],
                "position": row["position"],
            }
            for row in AssetTypeFieldset.objects.using(owner._state.db or DEFAULT_DB_ALIAS)
            .filter(asset_type_id=owner.pk)
            .order_by("position", "fieldset_id")
            .values("fieldset_id", "position")
        ]
    elif isinstance(owner, Category):
        payload["composition"] = [
            {
                "fieldset_id": row["fieldset_id"],
                "position": row["position"],
            }
            for row in CategoryDefaultFieldset.objects.using(owner._state.db or DEFAULT_DB_ALIAS)
            .filter(category_id=owner.pk)
            .order_by("position", "fieldset_id")
            .values("fieldset_id", "position")
        ]
    return payload


def resource_revision_for_owner(owner: Asset | AssetType | Category) -> ResourceRevision:
    serialized = json.dumps(
        _owner_resource_payload(owner),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ResourceRevision("sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest())


def _typed_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return (
            "object",
            tuple(
                sorted(
                    ((str(key), _typed_json_value(nested)) for key, nested in value.items()),
                    key=lambda item: item[0],
                )
            ),
        )
    if isinstance(value, (list, tuple)):
        return ("array", tuple(_typed_json_value(nested) for nested in value))
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) is int:
        return ("integer", value)
    if type(value) is float:
        return ("number", value)
    if type(value) is str:
        return ("string", value)
    return (type(value).__name__, _canonical_value(value))


def json_values_equal(left: object, right: object) -> bool:
    return _typed_json_value(left) == _typed_json_value(right)


def stored_values_for(owner: Asset | AssetType) -> dict[str, object]:
    values = owner.custom_field_data
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("custom_field_data must be a JSON object")
    return dict(values)


def _field_definition_for_edit(field: ResolvedFieldDTO) -> FieldDefinitionDTO:
    return FieldDefinitionDTO(
        resource_revision=field.resource_revision,
        key=field.key,
        identity=field.identity,
        label=field.label,
        help_text=field.help_text,
        targets=field.targets,
        activation=field.activation,
        field_type=field.field_type,
        quantity_kind=field.quantity_kind,
        canonical_unit=field.canonical_unit,
        validation=field.validation,
        required=field.required,
        nullable=field.nullable,
        lifecycle=field.lifecycle,
        choice_set=field.choice_set,
    )


def _editable_definitions(
    definition: SpecificationDefinitionDTO,
) -> Mapping[str, FieldDefinitionDTO]:
    definitions: dict[str, FieldDefinitionDTO] = {}
    for section in definition.rendered_sections:
        for field in section.fields:
            definitions.setdefault(str(field.key), _field_definition_for_edit(field))
    return MappingProxyType(definitions)


def load_effective_definition(
    asset_type_id: int | None,
    target_kind: TargetKind,
    stored_keys: Sequence[str],
) -> tuple[SpecificationDefinitionDTO, Mapping[str, FieldDefinitionDTO]]:
    type_ids = () if asset_type_id is None else (positive_id(asset_type_id, "Asset Type ID"),)
    graph = load_specification_graph(
        # The loader is deliberately the only ORM graph reader; this call is
        # made while the command's transaction and catalogue lock are held.
        SpecificationGraphLoadRequest(
            asset_type_ids=tuple(type_ids),
            requested_target_kinds=frozenset({target_kind}),
            requested_field_keys=frozenset(FieldKey(key) for key in stored_keys),
        )
    )
    ordered_memberships = graph.type_memberships.get(type_ids[0], ()) if type_ids else ()
    definition = resolve_specification_definition(
        SpecificationResolutionRequest(
            ordered_memberships=ordered_memberships,
            loaded_graph=graph,
            target_kind=target_kind,
        )
    )
    return definition, _editable_definitions(definition)


def load_prospective_definition(
    fieldset_identities: Sequence[str],
    target_kind: TargetKind,
    stored_keys: Sequence[str],
) -> tuple[SpecificationDefinitionDTO, Mapping[str, FieldDefinitionDTO], LoadedSpecificationGraphDTO]:
    identities = tuple(QualifiedIdentity(identity) for identity in fieldset_identities)
    graph = load_prospective_specification_graph(
        fieldset_identities=identities,
        requested_target_kinds=frozenset({target_kind}),
        requested_field_keys=frozenset(FieldKey(key) for key in stored_keys),
    )
    ordered_memberships = tuple(
        OrderedFieldsetMembershipDTO(fieldset_identity=identity, ordinal=ordinal)
        for ordinal, identity in enumerate(identities, start=1)
    )
    definition = resolve_specification_definition(
        SpecificationResolutionRequest(
            ordered_memberships=ordered_memberships,
            loaded_graph=graph,
            target_kind=target_kind,
        )
    )
    return definition, _editable_definitions(definition), graph


def normalize_patch(
    patch: SpecificationPatchDTO,
    definitions: Mapping[str, FieldDefinitionDTO],
    stored_values: Mapping[str, object],
    *,
    operation: str,
) -> NormalizedSpecificationPatch | tuple[DomainIssueDTO, ...]:
    try:
        return normalize_specification_patch(
            definitions,
            stored_values,
            setters=patch.set_values,
            clear_keys=patch.clear_keys,
            operation=operation,  # type: ignore[arg-type]
        )
    except SpecificationCodecError as error:
        return codec_issues(error)


def save_owner_in_savepoint(
    owner: Asset | AssetType | Category,
    actor: object,
    *,
    update_fields: Sequence[str],
    custom_field_data: Mapping[str, object] | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> None:
    """Rollback model-hook side effects when the existing save rejects the command."""
    fields = tuple(update_fields)
    if "custom_field_data" in fields:
        values = custom_field_data if custom_field_data is not None else owner.custom_field_data or {}
        write_custom_field_data(owner, values, actor=actor, update_fields=fields, using=using)
        return
    with transaction.atomic(using=using):
        with actor_change_context(actor):
            owner.save(using=using, update_fields=list(fields))


def reload_actor(actor: ActorContextDTO):
    user_model = get_user_model()
    # unscoped: actor reload for re-authorization must not depend on the ambient tenant
    candidate = user_model._base_manager.filter(pk=actor.actor_id, is_active=True).first()
    if candidate is None:
        return None
    if authentication_revision_for_actor(candidate) != actor.authentication_revision:
        return None
    return candidate


def map_structure_error(owner: OwnerRefDTO) -> CommandRejectedDTO:
    return rejected(
        owner,
        issue("UNSUPPORTED_STRUCTURE", message_key=_UNSUPPORTED_STRUCTURE_MESSAGE),
    )


def map_reference_error(owner: OwnerRefDTO, error: ValidationError) -> CommandRejectedDTO:
    del error
    return rejected(
        owner,
        issue("REFERENCE_CONFLICT", message_key=_REFERENCE_CONFLICT_MESSAGE),
    )


def stale_revision_issues(
    *,
    expected_resource_revision: str,
    actual_resource_revision: ResourceRevision,
    expected_definition_revision: str,
    actual_definition_revision: DefinitionRevision,
) -> tuple[DomainIssueDTO, ...]:
    # A stale owner and a stale definition are reported together so callers can
    # reject the whole request in one response; the fieldset/reference checks
    # are skipped by callers whenever any issue is present here.
    issues: list[DomainIssueDTO] = []
    if expected_resource_revision != actual_resource_revision:
        issues.append(issue("STALE_RESOURCE", message_key=_STALE_RESOURCE_MESSAGE))
    if expected_definition_revision != actual_definition_revision:
        issues.append(issue("STALE_DEFINITION", message_key=_STALE_DEFINITION_MESSAGE))
    return tuple(issues)


def stale_plan_issue() -> DomainIssueDTO:
    return issue("STALE_PLAN", message_key=_STALE_PLAN_MESSAGE)


__all__ = [
    "actor_change_context",
    "json_values_equal",
    "load_effective_definition",
    "load_prospective_definition",
    "map_reference_error",
    "map_structure_error",
    "normalize_patch",
    "positive_id",
    "rejected",
    "reload_actor",
    "resource_revision_for_owner",
    "revision_string",
    "save_owner_in_savepoint",
    "stale_plan_issue",
    "stale_revision_issues",
    "stored_values_for",
    "unavailable",
]
