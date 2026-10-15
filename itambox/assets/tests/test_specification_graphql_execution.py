"""DB-backed execution proof for the typed specification GraphQL readers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from graphql import build_schema, parse, validate
from graphql.validation import specified_rules

from assets.models import (
    Asset,
    AssetRole,
    AssetType,
    Category,
    CategoryDefaultFieldset,
    Depreciation,
    Manufacturer,
)
from assets.models.catalog import AssetTypeFieldset
from core.tests.mixins import grant
from extras.models import CustomField, CustomFieldChoice, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField
from organization.models import Role, Tenant
from software.models import Software

pytestmark = pytest.mark.django_db

User = get_user_model()


ENTRY_SELECTION = """
fragment EntrySelection on SpecificationEntry {
  key
  state
  reasonCodes
  definition { key fieldType }
  value {
    __typename
    ... on IntegerSpecificationValue { integer }
    ... on DecimalSpecificationValue { decimal }
    ... on ChoiceSpecificationValue { choice }
    ... on UninterpretedSpecificationValue { json }
  }
}
"""


@pytest.fixture
def graphql_world():
    tenant_a = Tenant.objects.create(name="T13 Tenant A", slug="t13-tenant-a")
    tenant_b = Tenant.objects.create(name="T13 Tenant B", slug="t13-tenant-b")
    user = User.objects.create_user(username="t13-reader", email="t13-reader@example.com")
    role = Role.objects.create(
        tenant=tenant_a,
        name="T13 GraphQL reader",
        permissions=[
            "assets.view_asset",
            "assets.view_assettype",
            "extras.view_customfield",
            "extras.view_customfieldchoiceset",
        ],
    )
    grant(user, tenant_a, role)

    manufacturer = Manufacturer.objects.create(name="T13 Manufacturer", slug="t13-manufacturer")
    asset_type = AssetType.objects.create(
        manufacturer=manufacturer,
        model="T13 Device",
        slug="t13-device",
        custom_field_data={
            "memory_gib": 9_007_199_254_740_991,
            "voltage": "24.000",
            "state": "ready",
        },
    )
    fieldset = CustomFieldset.objects.create(
        namespace="local",
        slug="t13-device-specs",
        label="T13 Device specifications",
    )
    content_type_asset_type = ContentType.objects.get_for_model(AssetType)
    content_type_asset = ContentType.objects.get_for_model(Asset)

    memory = CustomField.objects.create(
        namespace="local",
        name="memory_gib",
        label="Memory",
        field_type=CustomField.FIELD_TYPE_INTEGER,
        activation=CustomField.ACTIVATION_COMPOSED,
        nullable=True,
    )
    voltage = CustomField.objects.create(
        namespace="local",
        name="voltage",
        label="Voltage",
        field_type=CustomField.FIELD_TYPE_DECIMAL,
        activation=CustomField.ACTIVATION_COMPOSED,
        decimal_scale=3,
        quantity_kind="voltage",
        canonical_unit="V",
        nullable=True,
    )
    choices = CustomFieldChoiceSet.objects.create(
        namespace="local",
        slug="t13-device-state",
        label="Device state",
    )
    CustomFieldChoice.objects.create(choice_set=choices, key="ready", label="Ready", position=1)
    state = CustomField.objects.create(
        namespace="local",
        name="state",
        label="State",
        field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
        activation=CustomField.ACTIVATION_COMPOSED,
        choice_set=choices,
        max_values=1,
        nullable=True,
    )
    retired = CustomField.objects.create(
        namespace="local",
        name="retired_voltage",
        label="Retired voltage",
        field_type=CustomField.FIELD_TYPE_DECIMAL,
        activation=CustomField.ACTIVATION_COMPOSED,
        lifecycle=CustomField.LIFECYCLE_DEPRECATED,
        decimal_scale=2,
        nullable=True,
    )
    for field in (memory, voltage, state, retired):
        field.object_types.add(content_type_asset_type, content_type_asset)
    for position, field in enumerate((memory, voltage, state), start=1):
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=field, position=position)
    AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=1)

    assets_a = [
        Asset.objects.create(
            tenant=tenant_a,
            name=f"T13 Asset A {index}",
            asset_type=asset_type,
            custom_field_data={
                "memory_gib": 9_007_199_254_740_991,
                "voltage": "24.000",
                "state": "ready",
                "retired_voltage": "12.34",
                "unknown_history": {"raw": [index]},
            },
        )
        for index in range(1, 4)
    ]
    asset_b = Asset.objects.create(
        tenant=tenant_b,
        name="T13 Asset B",
        asset_type=asset_type,
        custom_field_data={"memory_gib": 128, "voltage": "12.500", "state": "ready"},
    )

    return SimpleNamespace(
        user=user,
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        asset_type=asset_type,
        assets_a=tuple(assets_a),
        asset_b=asset_b,
        schema=_public_schema(),
    )


def _public_schema():
    """The composed root schema without the HTTP operation budgets.

    These tests execute the schema directly to prove resolver semantics; the
    depth/alias/document/complexity budgets are the endpoint contract and are
    covered by the adversarial and security suites.
    """
    from core.schema import build_schema

    return build_schema(extensions=[])


def _context(world):
    return SimpleNamespace(user=world.user)


def _scope(tenant):
    return {"mode": "TENANT", "tenantId": str(tenant.pk)}


def _execute(world, query, *, variables=None, context=None):
    result = world.schema.execute_sync(
        query,
        variable_values=variables,
        context_value=_context(world) if context is None else context,
    )
    if result.errors:
        rendered = "; ".join(str(error) for error in result.errors)
        pytest.fail(f"GraphQL execution failed: {rendered}")
    return result


def test_public_root_executes_typed_union_history_and_scope_denial(graphql_world):
    query = f"""
    query($scope: RequestedScopeSelector!) {{
      assets(requestedScope: $scope, limit: 10) {{
        id
        name
        assetType {{
          model
          specificationEntries {{ ...EntrySelection }}
        }}
        specificationDefinition {{
          revision
          target
          sections {{
            identity
            fields {{
              key
              fieldType
              canonicalUnit
              validation {{ scale }}
              choiceSet {{ identity choices {{ key }} }}
            }}
          }}
        }}
        specificationEntries {{ ...EntrySelection }}
      }}
    }}
    {ENTRY_SELECTION}
    """
    result = _execute(graphql_world, query, variables={"scope": _scope(graphql_world.tenant_a)})
    rows = result.data["assets"]
    assert {row["id"] for row in rows} == {str(asset.pk) for asset in graphql_world.assets_a}
    assert all(row["assetType"]["model"] == "T13 Device" for row in rows)

    first_entries = {entry["key"]: entry for entry in rows[0]["specificationEntries"]}
    assert first_entries["memory_gib"]["state"] == "CURRENT"
    assert first_entries["memory_gib"]["value"] == {
        "__typename": "IntegerSpecificationValue",
        "integer": 9_007_199_254_740_991,
    }
    assert first_entries["voltage"]["value"] == {
        "__typename": "DecimalSpecificationValue",
        "decimal": "24.000",
    }
    assert first_entries["retired_voltage"]["state"] == "HISTORICAL"
    assert first_entries["retired_voltage"]["value"] == {
        "__typename": "DecimalSpecificationValue",
        "decimal": "12.34",
    }
    assert first_entries["unknown_history"]["state"] == "UNKNOWN"
    assert first_entries["unknown_history"]["value"] == {
        "__typename": "UninterpretedSpecificationValue",
        "json": {"raw": [1]},
    }
    assert rows[0]["specificationDefinition"]["target"] == "ASSET"
    assert {field["key"] for field in rows[0]["specificationDefinition"]["sections"][0]["fields"]} >= {
        "memory_gib",
        "voltage",
        "state",
    }

    denied = graphql_world.schema.execute_sync(
        """
        query($scope: RequestedScopeSelector!, $assetId: ID!) {
          assets(requestedScope: $scope) { id }
          asset(id: $assetId, requestedScope: $scope) { id name }
        }
        """,
        variable_values={"scope": _scope(graphql_world.tenant_b), "assetId": str(graphql_world.asset_b.pk)},
        context_value=_context(graphql_world),
    )
    assert denied.errors is None, denied.errors
    assert denied.data == {"assets": [], "asset": None}


def test_repeated_type_choice_and_entry_expansion_stays_batched(graphql_world):
    query_template = f"""
    query($scope: RequestedScopeSelector!) {{
      assets(requestedScope: $scope, limit: LIMIT) {{
        id
        assetType {{
          fieldsets {{
            fields {{
              key
              choiceSet {{ identity choices {{ key label }} }}
            }}
          }}
          specificationDefinition(target: ASSET_TYPE) {{
            sections {{ fields {{ key fieldType choiceSet {{ identity choices {{ key }} }} }} }}
          }}
        }}
        typeAgain: assetType {{ model fieldsets {{ fields {{ key }} }} }}
        specificationDefinition {{ sections {{ fields {{ key fieldType }} }} }}
        specificationEntries {{ ...EntrySelection }}
        entriesAgain: specificationEntries {{ ...EntrySelection }}
      }}
    }}
    {ENTRY_SELECTION}
    """

    def run(limit):
        result = graphql_world.schema.execute_sync(
            query_template.replace("LIMIT", str(limit)),
            variable_values={"scope": _scope(graphql_world.tenant_a)},
            context_value=_context(graphql_world),
        )
        assert result.errors is None, result.errors
        return result

    with CaptureQueriesContext(connection) as one_queries:
        one = run(1)
    with CaptureQueriesContext(connection) as many_queries:
        many = run(3)

    assert len(one.data["assets"]) == 1
    assert len(many.data["assets"]) == 3
    assert len(many_queries) <= len(one_queries) + 3
    print("T13_REPEATED_EXPANSION_QUERIES", len(one_queries), len(many_queries))


def test_asset_type_connection_paginates_actual_rows(graphql_world):
    manufacturer = Manufacturer.objects.get(pk=graphql_world.asset_type.manufacturer_id)
    extra_types = [
        AssetType.objects.create(
            manufacturer=manufacturer,
            model=f"T13 Pagination {index}",
            slug=f"t13-pagination-{index:02d}",
        )
        for index in range(1, 4)
    ]
    expected_ids = [str(pk) for pk in AssetType.objects.order_by("slug", "pk").values_list("pk", flat=True)]

    first = _execute(
        graphql_world,
        """
        { assetTypes(first: 2) { edges { cursor node { id model } } pageInfo { endCursor hasNextPage } } }
        """,
    ).data["assetTypes"]
    assert len(first["edges"]) == 2
    assert first["pageInfo"]["hasNextPage"] is True
    assert first["pageInfo"]["endCursor"]

    second = _execute(
        graphql_world,
        """
        query($after: Cursor) {
          assetTypes(first: 2, after: $after) {
            edges { cursor node { id model } }
            pageInfo { endCursor hasNextPage }
          }
        }
        """,
        variables={"after": first["pageInfo"]["endCursor"]},
    ).data["assetTypes"]
    combined = [edge["node"]["id"] for edge in (*first["edges"], *second["edges"])]
    assert combined == expected_ids
    assert second["pageInfo"]["hasNextPage"] is False
    assert len(extra_types) == 3


def test_public_complexity_rule_rejects_nested_list_fanout():
    from core.views.graphql import query_complexity_validator

    schema = _public_schema()
    query = """
    {
      assetTypes(first: 50) {
        edges {
          node {
            fieldsets {
              fields {
                choiceSet { choices { key } }
              }
            }
          }
        }
      }
    }
    """
    errors = validate(
        build_schema(schema.as_str()),
        parse(query),
        rules=(*specified_rules, query_complexity_validator(max_complexity=20, fan_out=10)),
    )
    assert any("maximum complexity" in error.message for error in errors)


def test_public_root_denies_missing_context():
    result = _public_schema().execute_sync("{ assetTypes(first: 1) { edges { node { id } } } }")
    assert result.data is None
    assert result.errors
    assert result.errors[0].extensions["code"] == "UNAUTHENTICATED"


@pytest.fixture
def asset_type_query_budget_world(graphql_world):
    manufacturer = Manufacturer.objects.get(pk=graphql_world.asset_type.manufacturer_id)
    fieldset = CustomFieldset.objects.get(namespace="local", slug="t13-device-specs")
    category = Category.objects.create(name="T13 Query Budget", slug="t13-query-budget")
    CategoryDefaultFieldset.objects.create(category=category, fieldset=fieldset, position=1)
    depreciation = Depreciation.objects.create(name="T13 Query Budget", months=36)
    role = AssetRole.objects.create(name="T13 Query Budget", slug="t13-query-budget")
    software = Software.objects.create(
        name="T13 Query Budget Software",
        manufacturer=manufacturer,
    )
    asset_types = tuple(
        AssetType.objects.create(
            manufacturer=manufacturer,
            model=f"T13 Query Budget {index:03d}",
            slug=f"000-t13-query-budget-{index:03d}",
            category=category,
            depreciation=depreciation,
            asset_role=role,
            custom_field_data={"memory_gib": index},
        )
        for index in range(101)
    )
    AssetTypeFieldset.objects.bulk_create(
        [AssetTypeFieldset(asset_type=asset_type, fieldset=fieldset, position=1) for asset_type in asset_types]
    )
    return SimpleNamespace(
        world=graphql_world,
        asset_types=asset_types,
        fieldset=fieldset,
        category=category,
        software=software,
    )


def test_asset_type_nested_query_counts_do_not_grow_with_page_size(asset_type_query_budget_world, record_property):
    world = asset_type_query_budget_world.world
    # Warm authorization before measuring so Django's per-user permission cache
    # does not make the first page look more expensive than later pages.
    assert world.user.has_perm("assets.view_assettype")
    selections = {
        "bare": "id",
        "fieldsets": "fieldsets { identity }",
        "manufacturer": "manufacturer { name }",
        "manufacturer_software_products": "manufacturer { softwareProducts { id } }",
        "specification_entries": "specificationEntries { key state }",
        "category_default_fieldsets": "category { defaultFieldsets { identity } }",
        "foreign_key_relations": "manufacturer { name } depreciation { name } category { name } assetRole { name }",
    }
    counts = {selection: {} for selection in selections}

    for name, selection in selections.items():
        for first in (10, 26, 100):
            query = f"""
            {{
              assetTypes(first: {first}) {{
                edges {{ node {{ {selection} }} }}
                pageInfo {{ hasNextPage }}
              }}
            }}
            """
            with CaptureQueriesContext(connection) as queries:
                result = world.schema.execute_sync(query, context_value=_context(world))
            assert result.errors is None, result.errors
            connection_data = result.data["assetTypes"]
            assert len(connection_data["edges"]) == first
            assert connection_data["pageInfo"]["hasNextPage"] is (first < 101)
            counts[name][first] = len(queries)

    assert all(values[10] == values[26] for values in counts.values()), counts
    assert all(values[100] <= values[26] + 4 for values in counts.values()), counts
    print("ASSET_TYPE_QUERY_BUDGET", counts)
    record_property("asset_type_query_budget_counts", repr(counts))


def test_asset_type_single_root_keeps_nested_reads_batched(asset_type_query_budget_world):
    world = asset_type_query_budget_world.world
    asset_type = asset_type_query_budget_world.asset_types[0]
    query = """
    query($assetTypeId: ID!) {
      assetType(id: $assetTypeId) {
        id
        manufacturer { softwareProducts { id } }
        depreciation { name }
        category { defaultFieldsets { identity } }
        assetRole { name }
        specificationEntries { key state }
      }
    }
    """
    result = world.schema.execute_sync(
        query,
        variable_values={"assetTypeId": str(asset_type.pk)},
        context_value=_context(world),
    )

    assert result.errors is None, result.errors
    node = result.data["assetType"]
    assert node["id"] == str(asset_type.pk)
    assert node["manufacturer"]["softwareProducts"] == [{"id": str(asset_type_query_budget_world.software.pk)}]
    assert node["depreciation"]["name"] == "T13 Query Budget"
    assert node["category"]["defaultFieldsets"] == [{"identity": "local/t13-device-specs"}]
    assert node["assetRole"]["name"] == "T13 Query Budget"
    assert {entry["key"] for entry in node["specificationEntries"]} == {"memory_gib"}
