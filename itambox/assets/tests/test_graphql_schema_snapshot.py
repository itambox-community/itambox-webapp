"""Snapshot of the published read-only GraphQL schema (issue #613).

``data/graphql_schema_text.graphql`` is the canonical printing of the schema the
endpoint publishes; it lists every root field and every type field. The port from
Graphene to Strawberry kept the contract, so a difference here is either a
deliberate schema change (update the snapshot and the CHANGELOG together) or an
accidental one (fix the schema).

The transport scalars' wire contract is asserted against the registered scalar
definitions at the end of this module: the published ``BigInt``/``JSONString``
and the specification read scalars, including the payloads they reject.
"""

from __future__ import annotations

import difflib
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase
from graphql import GraphQLError, build_schema, print_schema

from assets.graphql_specifications.scalars import (
    CategoryDefaultSnapshotRevision,
    CursorScalar,
    DateScalar,
    DecimalScalar,
)
from core.graphql_scalars import BigInt, JSONString
from core.schema import schema

SNAPSHOT_PATH = Path(__file__).with_name("data") / "graphql_schema_text.graphql"

# Root fields of `core.schema.Query`, in the order the schema composes them.
EXPECTED_ROOT_FIELDS = (
    "assets",
    "asset",
    "assetType",
    "assetTypes",
    "specificationFields",
    "choiceSet",
    "category",
    "previewAssetTypeDefinition",
    "softwareList",
    "software",
    "licenses",
    "license",
    "accessories",
    "accessory",
    "consumables",
    "consumable",
    "kits",
    "kit",
    "components",
    "component",
    "subscriptions",
    "subscription",
    "subscriptionAssignments",
    "subscriptionAssignment",
)

# Scalars whose description changed with the port (CHANGELOG: "GraphQL read API
# moved to Strawberry"); the wire behavior of `Date`/`Decimal` is unchanged.
EXPECTED_SCALAR_DESCRIPTIONS = {
    "Date": "An ISO-8601 calendar date.",
    "DateTime": "Date with time (isoformat)",
    "Decimal": "A decimal transported as a string to preserve fixed scale.",
}


class GraphQLSchemaSnapshotTests(SimpleTestCase):
    def _published_sdl(self) -> str:
        return print_schema(build_schema(schema.as_str()))

    def test_schema_matches_checked_in_snapshot(self):
        # The fixture keeps the conventional trailing newline; the printer does not emit one.
        expected = SNAPSHOT_PATH.read_text(encoding="utf-8").rstrip("\n")
        actual = self._published_sdl().rstrip("\n")
        if actual != expected:
            diff = "\n".join(
                difflib.unified_diff(
                    expected.splitlines(),
                    actual.splitlines(),
                    "checked_in_snapshot",
                    "published_schema",
                    lineterm="",
                )
            )
            self.fail(
                "The published GraphQL schema differs from the checked-in snapshot. "
                "Update data/graphql_schema_text.graphql and the CHANGELOG together "
                "when the change is intended.\n" + diff
            )

    def test_root_fields_are_published_with_their_scope_and_pagination_arguments(self):
        query_type = build_schema(self._published_sdl()).query_type

        self.assertEqual(tuple(query_type.fields), EXPECTED_ROOT_FIELDS)
        self.assertEqual(
            {name: str(arg.type) for name, arg in query_type.fields["assets"].args.items()},
            {
                "requestedScope": "RequestedScopeSelector!",
                "limit": "Int",
                "offset": "Int",
                "sortBy": "String",
                "name": "String",
                "assetTag": "String",
                "serialNumber": "String",
                "statusId": "ID",
                "locationId": "ID",
            },
        )
        self.assertEqual(
            {name: str(arg.type) for name, arg in query_type.fields["assetTypes"].args.items()},
            {"first": "Int!", "after": "Cursor"},
        )
        self.assertEqual(
            str(query_type.fields["assets"].type),
            "[Asset]",
        )
        self.assertEqual(
            str(query_type.fields["assetTypes"].type),
            "AssetTypeConnection!",
        )

    def test_scalar_descriptions_match_the_port_differences(self):
        type_map = build_schema(self._published_sdl()).type_map

        for scalar_name, description in EXPECTED_SCALAR_DESCRIPTIONS.items():
            with self.subTest(scalar=scalar_name):
                self.assertEqual(type_map[scalar_name].description, description)

    def test_schema_publishes_no_mutation_root(self):
        # Writes are REST-only (#612); a plugin may still contribute mutations.
        self.assertNotIn("\ntype Mutation {", self._published_sdl())


def _coercers(scalar):
    """The registered coercion functions behind a ``strawberry.scalar`` wrapper.

    ``strawberry.scalar`` returns a ``ScalarWrapper``; the wire contract these
    tests pin is the ``ScalarDefinition`` it wraps, which is also what the
    endpoint invokes when a value crosses the schema boundary -- a query field
    reaches only some of them (``JSONString`` is published but has no field of
    its own), so the rejection branches are exercised directly.
    """
    return scalar._scalar_definition


class GraphQLTransportScalarTests(SimpleTestCase):
    """The shared transport scalars keep BigInt and JSONString's wire contract."""

    def test_bigint_accepts_whole_numbers_and_numeric_text(self):
        coercers = _coercers(BigInt)

        self.assertEqual(coercers.serialize(42), 42)
        self.assertEqual(coercers.serialize(3.9), 3)
        self.assertEqual(coercers.parse_value("42"), 42)

    def test_bigint_rejects_non_numeric_payloads(self):
        coercers = _coercers(BigInt)

        for value in (object(), "not-a-number"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GraphQLError):
                    coercers.serialize(value)

    def test_jsonstring_serializes_and_parses_json_text(self):
        coercers = _coercers(JSONString)

        self.assertEqual(coercers.serialize({"a": 1}), '{"a": 1}')
        self.assertEqual(coercers.parse_value('{"a": 1}'), {"a": 1})

    def test_jsonstring_rejects_non_text_and_malformed_payloads(self):
        coercers = _coercers(JSONString)

        for value in (3, "{"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GraphQLError):
                    coercers.parse_value(value)


class SpecificationTransportScalarTests(SimpleTestCase):
    """The specification read scalars keep their transport-shape contract."""

    def test_decimal_scalar_serializes_decimal_and_text(self):
        coercers = _coercers(DecimalScalar)

        self.assertEqual(coercers.serialize(Decimal("1.50")), "1.50")
        self.assertEqual(coercers.serialize("1.50"), "1.50")
        with self.assertRaises(GraphQLError):
            coercers.parse_value(1.5)

    def test_date_scalar_accepts_iso_text_and_dates_only(self):
        coercers = _coercers(DateScalar)

        self.assertEqual(coercers.serialize(date(2024, 1, 2)), "2024-01-02")
        self.assertEqual(coercers.serialize("2024-01-02"), "2024-01-02")
        for value in ("2024-02-31", 5):
            with self.subTest(value=repr(value)):
                with self.assertRaises(GraphQLError):
                    coercers.parse_value(value)

    def test_opaque_scalars_require_non_empty_text(self):
        for scalar in (CategoryDefaultSnapshotRevision, CursorScalar):
            with self.subTest(scalar=scalar._scalar_definition.name):
                coercers = _coercers(scalar)
                self.assertEqual(coercers.serialize("value"), "value")
                for value in ("", None):
                    with self.subTest(value=repr(value)):
                        with self.assertRaises(GraphQLError):
                            coercers.serialize(value)
