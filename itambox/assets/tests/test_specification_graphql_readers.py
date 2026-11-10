from __future__ import annotations

from dataclasses import dataclass

import pytest
import strawberry
from django.utils.translation import override
from graphql import GraphQLError

from assets.graphql_specifications.loaders import RequestScopedSpecificationLoader
from assets.graphql_specifications.readers import issues_for_entries, issues_for_missing_required
from assets.graphql_specifications.scalars import DecimalScalar, SafeInteger
from assets.graphql_specifications.types import SpecificationDefinitionType, SpecificationEntryType
from assets.services.specifications.messages import specification_message
from extras.services.specifications.contracts import (
    ChoiceDTO,
    ChoiceSetDTO,
    DefinitionRevision,
    FieldDefinitionDTO,
    FieldKey,
    LoadedSpecificationGraphDTO,
    ProjectionIssueDTO,
    ResolvedFieldDTO,
    ResolvedSectionDTO,
    SpecificationDefinitionDTO,
    SpecificationProjectionDTO,
    SpecificationProjectionEntryDTO,
    SpecificationValidationDTO,
)


def _permitted_context() -> dict[str, object]:
    """Context whose user holds the custom-field permission.

    The permission guards read the request user, so a schema executed without a
    context is rejected as unauthenticated. These tests exercise the readers and
    serialisation, not the gate, so they run as a permitted user.
    """

    class _PermittedUser:
        is_authenticated = True

        def has_perm(self, permission: str) -> bool:
            return True

    return {"user": _PermittedUser()}


def _field(*, field_type: str = "integer", key: str = "memory_capacity") -> FieldDefinitionDTO:
    choice_set = None
    if field_type == "single_select":
        choice_set = ChoiceSetDTO(
            identity="core/storage-medium",
            label="Storage medium",
            resource_revision="sha256:choice-set",
            lifecycle="active",
            choices=(
                ChoiceDTO(key="ssd", label="SSD", lifecycle="active", position=1),
                ChoiceDTO(key="retired", label="Retired", lifecycle="deprecated", position=2),
            ),
        )
    return FieldDefinitionDTO(
        resource_revision="sha256:field",
        key=FieldKey(key),
        identity=f"core/{key}",
        label="Memory capacity",
        help_text="",
        targets=frozenset({"asset_type", "asset"}),
        activation="composed",
        field_type=field_type,
        quantity_kind="capacity" if field_type == "decimal" else None,
        canonical_unit="GiB" if field_type == "decimal" else None,
        validation=SpecificationValidationDTO(
            minimum=None,
            maximum=None,
            scale=3 if field_type == "decimal" else None,
            max_length=None,
            max_values=None,
            regex=None,
            rule=None,
        ),
        required=False,
        nullable=True,
        lifecycle="active",
        choice_set=choice_set,
    )


def _definition() -> SpecificationDefinitionDTO:
    field = _field(field_type="decimal")
    resolved = ResolvedFieldDTO(
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
        first_placement_section_identity="core/compute",
        contributing_section_identities=("core/compute",),
    )
    section = ResolvedSectionDTO(
        section_kind="persisted_fieldset",
        identity="core/compute",
        label="Compute",
        description="",
        persisted_ordinal=1,
        fields=(resolved,),
    )
    return SpecificationDefinitionDTO(
        revision=DefinitionRevision("definition-revision"),
        target_kind="asset_type",
        persisted_memberships=(),
        rendered_sections=(section,),
    )


def test_user_errors_never_expose_internal_specification_message_keys() -> None:
    entry = SpecificationProjectionEntryDTO(
        key=FieldKey("legacy_value"),
        value="invalid",
        state="invalid",
        reason_codes=("INVALID_TYPE",),
        definition=None,
    )
    entry_issues = issues_for_entries((entry,))
    required_issues = issues_for_missing_required((ProjectionIssueDTO("MISSING_REQUIRED", FieldKey("required")),))

    assert len(entry_issues) == len(required_issues) == 1
    assert not entry_issues[0].message.startswith("specifications.")
    assert not required_issues[0].message.startswith("specifications.")
    with override("de"):
        denial_message = specification_message("specifications.object_unavailable")
    assert "Provider-Mandanten" in denial_message
    assert not denial_message.startswith("specifications.")


def _scalar_echo_schema() -> strawberry.Schema:
    """Schema that routes values through the SafeInteger/Decimal scalars."""

    @strawberry.type
    class ScalarEchoQuery:
        @strawberry.field
        def big(self) -> SafeInteger:
            # Beyond GraphQL Int, inside the JavaScript safe-integer range.
            return 2_147_483_648

        @strawberry.field
        def echo(self, value: SafeInteger) -> SafeInteger:
            return value

        @strawberry.field
        def decimal_echo(self, value: DecimalScalar) -> DecimalScalar:
            return value

    return strawberry.Schema(query=ScalarEchoQuery)


def test_safe_integer_uses_javascript_safe_range_and_accepts_beyond_graphql_int() -> None:
    schema = _scalar_echo_schema()

    result = schema.execute_sync("{ big echo(value: 9007199254740991) }")
    assert result.errors is None, result.errors
    assert result.data == {"big": 2_147_483_648, "echo": 9_007_199_254_740_991}

    over_range = schema.execute_sync("{ echo(value: 9007199254740992) }")
    assert over_range.errors, "out-of-range SafeInteger literal must be rejected"

    boolean = schema.execute_sync("{ echo(value: true) }")
    assert boolean.errors, "boolean is not a safe integer"


def test_decimal_scalar_is_string_valued_and_preserves_fixed_scale_text() -> None:
    schema = _scalar_echo_schema()

    result = schema.execute_sync('{ decimalEcho(value: "24.000") }')
    assert result.errors is None, result.errors
    assert result.data == {"decimalEcho": "24.000"}

    float_literal = schema.execute_sync("{ decimalEcho(value: 24.0) }")
    assert float_literal.errors, "a JSON number must not be accepted as a Decimal string"


def test_typed_definition_and_union_execute_with_decimal_and_history_escape_hatch() -> None:
    definition = _definition()
    projection = SpecificationProjectionDTO(
        entries=(
            SpecificationProjectionEntryDTO(
                key=FieldKey("memory_capacity"),
                value="24.000",
                state="current",
                reason_codes=("ACTIVE_VALUE",),
                definition=_field(field_type="decimal"),
            ),
            SpecificationProjectionEntryDTO(
                key=FieldKey("removed_field"),
                value={"unexpected": ["history"]},
                state="unknown",
                reason_codes=("UNKNOWN_DEFINITION",),
                definition=None,
            ),
        ),
        missing_required_issues=(ProjectionIssueDTO("MISSING_REQUIRED", FieldKey("required_field")),),
    )

    @strawberry.type
    class Query:
        @strawberry.field
        def definition(self) -> SpecificationDefinitionType:
            return definition

        @strawberry.field
        def entries(self) -> list[SpecificationEntryType]:
            return list(projection.entries)

    result = strawberry.Schema(query=Query).execute_sync(
        """
        {
          definition {
            revision
            target
            sections { identity fields { key fieldType canonicalUnit sources } }
          }
          entries {
            key state reasonCodes
            value {
              __typename
              ... on DecimalSpecificationValue { decimal }
              ... on UninterpretedSpecificationValue { json }
            }
          }
        }
        """,
        context_value=_permitted_context(),
    )
    assert result.errors is None, result.errors
    assert result.data == {
        "definition": {
            "revision": "definition-revision",
            "target": "ASSET_TYPE",
            "sections": [
                {
                    "identity": "core/compute",
                    "fields": [
                        {
                            "key": "memory_capacity",
                            "fieldType": "DECIMAL",
                            "canonicalUnit": "GiB",
                            "sources": ["core/compute"],
                        }
                    ],
                }
            ],
        },
        "entries": [
            {
                "key": "memory_capacity",
                "state": "CURRENT",
                "reasonCodes": ["ACTIVE_VALUE"],
                "value": {"__typename": "DecimalSpecificationValue", "decimal": "24.000"},
            },
            {
                "key": "removed_field",
                "state": "UNKNOWN",
                "reasonCodes": ["UNKNOWN_DEFINITION"],
                "value": {"__typename": "UninterpretedSpecificationValue", "json": {"unexpected": ["history"]}},
            },
        ],
    }


def test_request_loader_batches_distinct_type_ids_and_does_not_cross_request_cache() -> None:
    calls: list[tuple[int, ...]] = []
    graph = LoadedSpecificationGraphDTO(
        type_memberships={},
        fieldsets_by_identity={},
        fields_by_key={},
        global_field_keys_by_target={},
        historical_definitions_by_key={},
    )

    def graph_loader(request):
        calls.append(tuple(request.asset_type_ids))
        return graph

    loader = RequestScopedSpecificationLoader(graph_loader=graph_loader)
    loader.prepare_type_ids((7, 7, 8), target_kind="asset_type")
    assert calls == [(7, 8)]
    assert loader.graph_for_type(7, target_kind="asset_type") is graph
    assert loader.graph_for_type(8, target_kind="asset_type") is graph
    assert calls == [(7, 8)]

    second_loader = RequestScopedSpecificationLoader(graph_loader=graph_loader)
    second_loader.prepare_type_ids((7,), target_kind="asset_type")
    assert calls == [(7, 8), (7,)]


def test_request_loader_rejects_missing_context_instead_of_using_ambient_visibility() -> None:
    from assets.graphql_specifications.loaders import request_loader_for_info

    @dataclass
    class Info:
        context: object

    with pytest.raises(GraphQLError):
        request_loader_for_info(Info(context=None))


def test_asset_schema_executes_typed_reader_contract_without_static_only_shortcut() -> None:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")
    os.environ.setdefault("ITAMBOX_ENV", "dev")
    import django

    django.setup()
    from graphql import build_schema

    from core.schema import schema

    graphql_schema = build_schema(schema.as_str())
    query = graphql_schema.query_type

    assert str(query.fields["assetTypes"].type) == "AssetTypeConnection!"
    assert {name: str(arg.type) for name, arg in query.fields["assetTypes"].args.items()} == {
        "first": "Int!",
        "after": "Cursor",
    }
    assert str(query.fields["specificationFields"].type) == "SpecificationFieldConnection!"
    assert str(graphql_schema.type_map["IntegerSpecificationValue"].fields["integer"].type) == "SafeInteger!"
    assert str(graphql_schema.type_map["DecimalSpecificationValue"].fields["decimal"].type) == "Decimal!"

    result = schema.execute_sync("{ __typename }")
    assert result.errors is None
    assert result.data == {"__typename": "Query"}
