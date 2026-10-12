"""REST transport adapters for the final ordered-specification contract.

This module deliberately stops at the existing Assets specification commands and
Extras DTOs.  It contains only transport parsing, DTO projection, and the stable
HTTP error envelope; it is not a second value codec or persistence layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from assets.models import Asset, AssetType, Category, CategoryDefaultFieldset
from assets.services.specifications._command_support import (
    issue,
    load_prospective_definition,
    normalize_patch,
    resource_revision_for_owner,
    stored_values_for,
)
from assets.services.specifications.contracts import (
    AssetTypePreviewDTO,
    CommandRejectedDTO,
    DefinitionRevision,
    DomainIssueDTO,
    ExplicitFieldsetSelectionDTO,
    FieldKey,
    FieldsetSelectionDTO,
    HistoryCleanupPreviewDTO,
    OwnerRefDTO,
    SpecificationGraphLoadRequest,
    SpecificationPatchDTO,
    SpecificationProjectionRequest,
    SpecificationResolutionRequest,
    StoredSpecificationEntryDTO,
)
from assets.services.specifications.loader import load_specification_graph
from assets.services.specifications.messages import specification_message
from assets.specification_adapters import patch_from_mapping
from core.managers import Scope
from extras.services.specifications.composition import resolve_specification_definition
from extras.services.specifications.contracts import (
    ChoiceSetDTO,
    FieldDefinitionDTO,
    LoadedSpecificationGraphDTO,
    ResolvedFieldDTO,
    SpecificationDefinitionDTO,
    SpecificationProjectionDTO,
)
from extras.services.specifications.projection import project_specification_values
from itambox.api.base import reject_unknown_or_writableless
from organization.services.access_scope import (
    AccessScopeDeniedDTO,
    AccessScopeResolutionRequestDTO,
    AccessScopeResolvedDTO,
    ActorContextDTO,
    RequestedScopeSelectorDTO,
    ResolvedAccessAuthorizationDTO,
    TenantId,
    authentication_revision_for_actor,
    resolve_access_scope,
)

_MISSING_PRECONDITION_STATUS = status.HTTP_428_PRECONDITION_REQUIRED
_STALE_STATUS = status.HTTP_412_PRECONDITION_FAILED


class StrictInputSerializer(serializers.Serializer):
    """Reject unknown/read-only request members instead of silently dropping them."""

    def to_internal_value(self, data: object) -> object:
        if isinstance(data, Mapping):
            reject_transport_writes(data, self.fields)
        return super().to_internal_value(data)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        initial = getattr(self, "initial_data", None)
        if isinstance(initial, Mapping):
            reject_transport_writes(initial, self.fields)
        return attrs


def reject_transport_writes(
    submitted: Mapping[str, object],
    fields: Mapping[str, serializers.Field[Any]],
) -> None:
    reject_unknown_or_writableless(submitted, fields)
    read_only = sorted(name for name in submitted if name in fields and fields[name].read_only)
    if read_only:
        raise serializers.ValidationError({name: "This field is read-only." for name in read_only})


class SpecificationPatchInputSerializer(StrictInputSerializer):
    """Structural parsing only; command codecs own all specification semantics."""

    set = serializers.JSONField(required=False, default=dict)
    clear = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        allow_empty=True,
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs = super().validate(attrs)
        submitted = attrs.get("set", {})
        clear_keys = attrs.get("clear", [])
        errors: dict[str, object] = {}
        if not isinstance(submitted, Mapping):
            errors["set"] = "Expected an object."
        if not isinstance(clear_keys, list):
            errors["clear"] = "Expected a list of field keys."
        if errors:
            raise serializers.ValidationError(errors)
        return {"set": dict(submitted), "clear": list(clear_keys)}


class CompositionInputSerializer(StrictInputSerializer):
    fieldsets = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=True,
        allow_empty=True,
    )
    expected_definition_revision = serializers.CharField(required=False, allow_blank=False)
    specification_patch = SpecificationPatchInputSerializer(required=False)


class HistoryCleanupInputSerializer(StrictInputSerializer):
    keys = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=True,
        allow_empty=True,
    )
    expected_definition_revision = serializers.CharField(required=False, allow_blank=False)


class HistoryCleanupWriteInputSerializer(HistoryCleanupInputSerializer):
    preview_token = serializers.CharField(required=False, allow_blank=False)


class CategoryDefaultFieldsetsInputSerializer(StrictInputSerializer):
    fieldsets = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=True,
        allow_empty=True,
    )


class ApplyCategoryDefaultsInputSerializer(StrictInputSerializer):
    preview_token = serializers.CharField(required=False, allow_blank=False)
    expected_definition_revision = serializers.CharField(required=False, allow_blank=False)
    expected_category_default_snapshot_revision = serializers.CharField(required=False, allow_blank=False)
    specification_patch = SpecificationPatchInputSerializer(required=False)


class ApplyCategoryDefaultsPreviewInputSerializer(StrictInputSerializer):
    specification_patch = SpecificationPatchInputSerializer(required=False)


class AssetTypePreviewResponseSerializer(serializers.Serializer):
    preview_token = serializers.CharField(allow_null=True)
    definition = serializers.JSONField()
    expected_definition_revision = serializers.CharField()
    expected_resource_revision = serializers.CharField(allow_null=True)
    expected_category_default_snapshot_revision = serializers.CharField(allow_null=True)
    consumes_category_defaults = serializers.BooleanField()
    issues = serializers.ListField(child=serializers.DictField())
    can_apply = serializers.BooleanField()


def _message(message_key: str) -> str:
    return specification_message(message_key)


def _transport_path(issue_dto: DomainIssueDTO) -> tuple[str, ...]:
    path = tuple(str(item) for item in issue_dto.path)
    if not path:
        if issue_dto.code == "STALE_RESOURCE":
            return ("expected_resource_revision",)
        if issue_dto.code == "STALE_DEFINITION":
            return ("expected_definition_revision",)
        if issue_dto.code == "STALE_PLAN":
            return ("preview_token",)
    if path[:1] in (("set",), ("clear",)):
        return ("specification_patch", *path)
    return path


def issue_payload(issue_dto: DomainIssueDTO) -> dict[str, object]:
    return {
        "code": str(issue_dto.code),
        "path": list(_transport_path(issue_dto)),
        "field_key": None if issue_dto.field_key is None else str(issue_dto.field_key),
        "message": _message(str(issue_dto.message_key)),
    }


def _status_for_issues(issues: Sequence[DomainIssueDTO]) -> int:
    codes = {str(item.code) for item in issues}
    if "OBJECT_UNAVAILABLE" in codes:
        return status.HTTP_404_NOT_FOUND
    if codes.intersection({"STALE_RESOURCE", "STALE_DEFINITION", "STALE_PLAN"}):
        return _STALE_STATUS
    if "MISSING_PRECONDITION" in codes:
        return _MISSING_PRECONDITION_STATUS
    if codes.intersection(
        {"REFERENCE_CONFLICT", "OWNERSHIP_CONFLICT", "DEPENDENCY_RETIREMENT", "UNSUPPORTED_STRUCTURE"}
    ):
        return status.HTTP_409_CONFLICT
    return status.HTTP_400_BAD_REQUEST


def _outer_code(issues: Sequence[DomainIssueDTO]) -> str:
    codes = {str(item.code) for item in issues}
    if len(codes) == 1 and codes.intersection(
        {"OBJECT_UNAVAILABLE", "STALE_RESOURCE", "STALE_DEFINITION", "STALE_PLAN", "MISSING_PRECONDITION"}
    ):
        return next(iter(codes))
    return "SPECIFICATION_VALIDATION_FAILED"


def error_response(
    issues: Sequence[DomainIssueDTO],
    *,
    status_code: int | None = None,
) -> Response:
    normalized = tuple(issues)
    if not normalized:
        normalized = (issue("UNSUPPORTED_STRUCTURE", message_key="specifications.unsupported_structure"),)
    return Response(
        {
            "error": {
                "code": _outer_code(normalized),
                "message": (
                    "Some specification values are invalid."
                    if _outer_code(normalized) == "SPECIFICATION_VALIDATION_FAILED"
                    else _message(str(normalized[0].message_key))
                ),
                "issues": [issue_payload(item) for item in normalized],
            }
        },
        status=status_code if status_code is not None else _status_for_issues(normalized),
    )


def command_result_response(result: object, *, success_status: int = status.HTTP_200_OK) -> Response:
    """Convert one canonical command union to the final REST envelope."""
    if isinstance(result, CommandRejectedDTO):
        return error_response(result.issues)
    owner = getattr(result, "owner", None)
    payload: dict[str, object] = {"outcome": getattr(result, "outcome", None)}
    if isinstance(owner, OwnerRefDTO):
        payload["owner"] = {"kind": owner.owner_kind, "id": owner.owner_id}
    if hasattr(result, "resource_revision"):
        payload["resource_revision"] = str(result.resource_revision)
    if hasattr(result, "definition_revision"):
        payload["definition_revision"] = str(result.definition_revision)
    return Response(payload, status=success_status)


class SpecificationCommandAPIException(APIException):
    """Turn a rejected command union into the final REST response body."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, result: CommandRejectedDTO):
        self.status_code = _status_for_issues(result.issues)
        super().__init__(detail=error_response(result.issues).data)


def command_success_or_raise(result: object) -> object:
    if isinstance(result, CommandRejectedDTO):
        raise SpecificationCommandAPIException(result)
    return result


def missing_precondition_response(*paths: Sequence[str]) -> Response:
    return error_response(
        tuple(issue("MISSING_PRECONDITION", path=tuple(path)) for path in paths),
        status_code=_MISSING_PRECONDITION_STATUS,
    )


def create_missing_precondition_paths(data: object) -> tuple[tuple[str, ...], ...]:
    """Return only syntactically determinable create precondition omissions."""
    if not isinstance(data, Mapping):
        return (("expected_definition_revision",),)
    paths: list[tuple[str, ...]] = []
    category = data.get("category_id", data.get("category"))
    consumes_defaults = category not in (None, "", 0) and "fieldsets" not in data
    if consumes_defaults:
        if not data.get("preview_token"):
            paths.append(("preview_token",))
        if not data.get("expected_category_default_snapshot_revision"):
            paths.append(("expected_category_default_snapshot_revision",))
    if not data.get("expected_definition_revision"):
        paths.append(("expected_definition_revision",))
    return tuple(paths)


def if_match_revision(request: object) -> str | None:
    """Parse exactly one opaque strong/weak HTTP ETag into its command value."""
    meta = getattr(request, "META", {}) or {}
    raw = meta.get("HTTP_IF_MATCH")
    if not isinstance(raw, str) or not raw.strip():
        return None
    values: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if value.startswith("W/"):
            value = value[2:].strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        if value and value != "*":
            values.append(value)
    if len(values) != 1:
        return None
    return values[0]


def etag_for_revision(revision: object) -> str:
    return '"{}"'.format(revision)


def etag_for_owner(owner: object) -> str:
    return etag_for_revision(resource_revision_for_owner(owner))


def _validation_payload(validation: object) -> dict[str, object]:
    return {
        "minimum": None if getattr(validation, "minimum", None) is None else str(validation.minimum),
        "maximum": None if getattr(validation, "maximum", None) is None else str(validation.maximum),
        "scale": getattr(validation, "scale", None),
        "max_length": getattr(validation, "max_length", None),
        "max_values": getattr(validation, "max_values", None),
        "regex": getattr(validation, "regex", None),
        "rule": getattr(validation, "rule", None),
    }


def _choice_set_payload(choice_set: ChoiceSetDTO | None) -> dict[str, object] | None:
    if choice_set is None:
        return None
    return {
        "identity": str(choice_set.identity),
        "label": choice_set.label,
        "resource_revision": str(choice_set.resource_revision),
        "lifecycle": choice_set.lifecycle,
        "choices": [
            {
                "key": choice.key,
                "label": choice.label,
                "lifecycle": choice.lifecycle,
                "position": choice.position,
            }
            for choice in choice_set.choices
        ],
    }


def _field_payload(field: ResolvedFieldDTO | FieldDefinitionDTO) -> dict[str, object]:
    payload: dict[str, object] = {
        "key": str(field.key),
        "identity": str(field.identity),
        "label": field.label,
        "help_text": field.help_text,
        "targets": sorted(str(target) for target in field.targets),
        "activation": field.activation,
        "field_type": field.field_type,
        "quantity_kind": field.quantity_kind,
        "canonical_unit": field.canonical_unit,
        "validation": _validation_payload(field.validation),
        "required": field.required,
        "nullable": field.nullable,
        "lifecycle": field.lifecycle,
        "choice_set": _choice_set_payload(field.choice_set),
    }
    if isinstance(field, ResolvedFieldDTO):
        payload.update(
            {
                "first_placement_section": (
                    None
                    if field.first_placement_section_identity is None
                    else str(field.first_placement_section_identity)
                ),
                "contributing_sections": [str(value) for value in field.contributing_section_identities],
            }
        )
    return payload


def _fieldset_fields(
    identity: str,
    definition: SpecificationDefinitionDTO,
    graph: LoadedSpecificationGraphDTO | None,
) -> tuple[str, ...]:
    if graph is not None:
        fieldset = graph.fieldsets_by_identity.get(identity)
        if fieldset is not None:
            return tuple(str(item.field_identity) for item in fieldset.field_memberships)
    fields: list[str] = []
    for section in definition.rendered_sections:
        if section.identity == identity:
            fields.extend(str(field.identity) for field in section.fields)
    return tuple(fields)


def definition_payload(
    definition: SpecificationDefinitionDTO,
    graph: LoadedSpecificationGraphDTO | None = None,
) -> dict[str, object]:
    """Serialize the value-independent DTO, retaining ordered memberships."""
    fieldsets: list[dict[str, object]] = []
    for membership in definition.persisted_memberships:
        identity = str(membership.fieldset_identity)
        fieldset = graph.fieldsets_by_identity.get(identity) if graph is not None else None
        fieldsets.append(
            {
                "identity": identity,
                "label": "" if fieldset is None else fieldset.label,
                "description": "" if fieldset is None else fieldset.description,
                "resource_revision": None if fieldset is None else str(fieldset.resource_revision),
                "lifecycle": None if fieldset is None else fieldset.lifecycle,
                "ordinal": membership.ordinal,
                "fields": list(_fieldset_fields(identity, definition, graph)),
            }
        )
    sections = []
    for section in definition.rendered_sections:
        sections.append(
            {
                "section_kind": section.section_kind,
                "identity": None if section.identity is None else str(section.identity),
                "label": section.label,
                "description": section.description,
                "persisted_ordinal": section.persisted_ordinal,
                "fields": [_field_payload(field) for field in section.fields],
            }
        )
    return {
        "revision": str(definition.revision),
        "target": definition.target_kind,
        "fieldsets": fieldsets,
        "sections": sections,
    }


def projection_payload(projection: SpecificationProjectionDTO) -> dict[str, object]:
    values: dict[str, object] = {}
    historical_keys: list[str] = []
    state_issues: list[dict[str, object]] = []
    for entry in projection.entries:
        key = str(entry.key)
        values[key] = entry.value
        if entry.state != "current":
            historical_keys.append(key)
        for reason in entry.reason_codes:
            if reason != "ACTIVE_VALUE":
                state_issues.append(
                    {
                        "code": reason,
                        "path": ["specifications", key],
                        "field_key": key,
                        "message": _message(f"specifications.{reason.lower()}"),
                    }
                )
    for missing in projection.missing_required_issues:
        key = str(missing.field_key)
        state_issues.append(
            {
                "code": "MISSING_REQUIRED",
                "path": ["specifications", key],
                "field_key": key,
                "message": _message("specifications.required_field"),
            }
        )
    complete = not any(item["code"] in {"MISSING_REQUIRED", "INVALID_STORED_VALUE"} for item in state_issues)
    return {
        "specifications": values,
        "specification_state": {
            "complete": complete,
            "issues": state_issues,
            "historical_keys": historical_keys,
        },
    }


def _owner_graph(
    owner: Asset | AssetType,
) -> tuple[SpecificationDefinitionDTO, SpecificationProjectionDTO, LoadedSpecificationGraphDTO]:
    stored = stored_values_for(owner)
    target_kind = "asset_type" if isinstance(owner, AssetType) else "asset"
    asset_type_id = owner.pk if isinstance(owner, AssetType) else owner.asset_type_id
    type_ids = () if asset_type_id is None else (int(asset_type_id),)
    graph = load_specification_graph(
        SpecificationGraphLoadRequest(
            asset_type_ids=type_ids,
            requested_target_kinds=frozenset({target_kind}),
            requested_field_keys=frozenset(FieldKey(key) for key in stored),
        )
    )
    memberships = graph.type_memberships.get(int(asset_type_id), ()) if asset_type_id is not None else ()
    definition = resolve_specification_definition(
        SpecificationResolutionRequest(
            ordered_memberships=memberships,
            loaded_graph=graph,
            target_kind=target_kind,
        )
    )
    projection = project_specification_values(
        SpecificationProjectionRequest(
            definition=definition,
            stored_entries=tuple(StoredSpecificationEntryDTO(FieldKey(key), value) for key, value in stored.items()),
            historical_definitions_by_key=graph.historical_definitions_by_key,
        )
    )
    return definition, projection, graph


def owner_specification_payload(owner: Asset | AssetType) -> dict[str, object]:
    definition, projection, graph = _owner_graph(owner)
    payload = {
        "fieldsets": [str(item.fieldset_identity) for item in definition.persisted_memberships],
        **projection_payload(projection),
        "resource_revision": str(resource_revision_for_owner(owner)),
        "definition_revision": str(definition.revision),
    }
    payload["_definition"] = definition_payload(definition, graph)
    return payload


def owner_detail_specification_payload(owner: Asset | AssetType) -> dict[str, object]:
    payload = owner_specification_payload(owner)
    payload.pop("_definition", None)
    return payload


def definition_for_owner(owner: Asset | AssetType, target_kind: str) -> dict[str, object]:
    stored = stored_values_for(owner)
    type_id = owner.pk if isinstance(owner, AssetType) else owner.asset_type_id
    type_ids = () if type_id is None else (int(type_id),)
    graph = load_specification_graph(
        SpecificationGraphLoadRequest(
            asset_type_ids=type_ids,
            requested_target_kinds=frozenset({target_kind}),  # type: ignore[arg-type]
            requested_field_keys=frozenset(FieldKey(key) for key in stored),
        )
    )
    memberships = graph.type_memberships.get(int(type_id), ()) if type_id is not None else ()
    definition = resolve_specification_definition(
        SpecificationResolutionRequest(
            ordered_memberships=memberships,
            loaded_graph=graph,
            target_kind=target_kind,  # type: ignore[arg-type]
        )
    )
    return definition_payload(definition, graph)


def composition_preview_payload(
    owner: AssetType,
    fieldset_identities: Sequence[str],
    patch: object,
) -> dict[str, object]:
    """Resolve a proposed Type composition using the existing pure seams."""
    stored = stored_values_for(owner)
    definition, definitions, graph = load_prospective_definition(
        tuple(fieldset_identities),
        "asset_type",
        tuple(stored),
    )
    normalized = normalize_patch(
        patch,
        definitions,
        stored,
        operation="composition_edit",
    )
    issues = () if not isinstance(normalized, tuple) else normalized
    return {
        "fieldsets": [str(identity) for identity in fieldset_identities],
        "definition": definition_payload(definition, graph),
        "expected_resource_revision": str(resource_revision_for_owner(owner)),
        "expected_definition_revision": str(definition.revision),
        "issues": _issues_payload(issues),
        "can_apply": not issues,
        "impact": {
            "asset_count": Asset.objects.for_scope(Scope.current()).filter(asset_type_id=owner.pk).count(),
        },
    }


def category_default_payload(category: Category) -> dict[str, object]:
    rows = (
        CategoryDefaultFieldset.objects.select_related("fieldset")
        .filter(category_id=category.pk)
        .order_by("position", "fieldset_id", "pk")
    )
    return {
        "category_id": category.pk,
        "fieldsets": [f"{row.fieldset.namespace}/{row.fieldset.slug}" for row in rows],
        "resource_revision": str(resource_revision_for_owner(category)),
    }


def _issues_payload(issues: Sequence[DomainIssueDTO]) -> list[dict[str, object]]:
    return [issue_payload(item) for item in issues]


def preview_payload(result: object) -> dict[str, object] | None:
    if isinstance(result, AssetTypePreviewDTO):
        return {
            "preview_token": None if result.preview_token is None else str(result.preview_token),
            "definition": definition_payload(result.definition),
            "expected_definition_revision": str(result.expected_definition_revision),
            "expected_resource_revision": (
                None if result.expected_resource_revision is None else str(result.expected_resource_revision)
            ),
            "expected_category_default_snapshot_revision": (
                None
                if result.expected_category_default_snapshot_revision is None
                else str(result.expected_category_default_snapshot_revision)
            ),
            "consumes_category_defaults": result.consumes_category_defaults,
            "issues": _issues_payload(result.issues),
            "can_apply": not result.issues,
        }
    if isinstance(result, HistoryCleanupPreviewDTO):
        return {
            "preview_token": str(result.preview_token),
            "owner": {"kind": result.owner.owner_kind, "id": result.owner.owner_id},
            "keys": [str(key) for key in result.keys],
            "expected_resource_revision": str(result.expected_resource_revision),
            "expected_definition_revision": str(result.expected_definition_revision),
            "historical_state_digest": result.historical_state_digest,
            "issues": _issues_payload(result.issues),
            "can_apply": not result.issues,
        }
    return None


def preview_result_response(result: object) -> Response:
    if isinstance(result, CommandRejectedDTO):
        return error_response(result.issues)
    payload = preview_payload(result)
    if payload is None:
        return error_response((issue("UNSUPPORTED_STRUCTURE", message_key="specifications.unsupported_structure"),))
    return Response(payload, status=status.HTTP_200_OK)


def explicit_fieldset_selection(values: Sequence[str]) -> ExplicitFieldsetSelectionDTO:
    try:
        return ExplicitFieldsetSelectionDTO(tuple(str(value) for value in values))
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError({"fieldsets": "Invalid fieldset selection."}) from exc


def create_fieldset_selection_from_values(
    values: Sequence[str] | None,
    *,
    omitted: bool,
) -> FieldsetSelectionDTO:
    """Construct the presence-sensitive create DTO from parsed transport data."""

    if omitted:
        return FieldsetSelectionDTO(presence="omitted", identities=())
    try:
        explicit = ExplicitFieldsetSelectionDTO(tuple(str(value) for value in (values or ())))
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError({"fieldsets": "Invalid fieldset selection."}) from exc
    return FieldsetSelectionDTO(presence="explicit", identities=explicit.identities)


def patch_from_validated(value: Mapping[str, object] | None) -> SpecificationPatchDTO:
    return patch_from_mapping(None if value is None else {"set": value.get("set", {}), "clear": value.get("clear", [])})


def history_keys(values: Sequence[str]) -> tuple[FieldKey, ...]:
    return tuple(FieldKey(value) for value in values)


def asset_history_authorization_for_user(
    *,
    user: object,
    tenant_id: int | None,
) -> ResolvedAccessAuthorizationDTO | None:
    """Resolve the cleanup-specific tenant scope before entering its command."""
    if tenant_id is None or not getattr(user, "is_authenticated", False):
        return None
    actor = ActorContextDTO(
        actor_id=int(user.pk),
        authentication_revision=authentication_revision_for_actor(user),
    )
    request = AccessScopeResolutionRequestDTO(
        actor=actor,
        selector=RequestedScopeSelectorDTO(
            mode="tenant",
            tenant_id=TenantId(int(tenant_id)),
            tenant_group_id=None,
        ),
        operation="cleanup_asset_specification_history",
        required_permission="assets.change_asset",
    )
    resolved = resolve_access_scope(request)
    if isinstance(resolved, AccessScopeDeniedDTO):
        return None
    if not isinstance(resolved, AccessScopeResolvedDTO):
        return None
    return ResolvedAccessAuthorizationDTO(actor=actor, request=request, initial_scope=resolved.access_scope)


def expected_revision_or_missing(
    data: Mapping[str, object],
    field_name: str = "expected_definition_revision",
) -> Response | DefinitionRevision:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        return missing_precondition_response((field_name,))
    return DefinitionRevision(value)


__all__ = [
    "ApplyCategoryDefaultsInputSerializer",
    "ApplyCategoryDefaultsPreviewInputSerializer",
    "AssetTypePreviewResponseSerializer",
    "CategoryDefaultFieldsetsInputSerializer",
    "CompositionInputSerializer",
    "HistoryCleanupInputSerializer",
    "HistoryCleanupWriteInputSerializer",
    "SpecificationPatchInputSerializer",
    "asset_history_authorization_for_user",
    "category_default_payload",
    "composition_preview_payload",
    "command_result_response",
    "command_success_or_raise",
    "create_fieldset_selection_from_values",
    "create_missing_precondition_paths",
    "definition_for_owner",
    "definition_payload",
    "error_response",
    "etag_for_owner",
    "expected_revision_or_missing",
    "explicit_fieldset_selection",
    "history_keys",
    "if_match_revision",
    "issue_payload",
    "missing_precondition_response",
    "owner_detail_specification_payload",
    "owner_specification_payload",
    "patch_from_validated",
    "preview_result_response",
    "projection_payload",
]
