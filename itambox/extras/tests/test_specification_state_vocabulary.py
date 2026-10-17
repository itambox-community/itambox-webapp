"""Drift guard for the specification state vocabulary (issue #833, option B).

Two specification resolution paths intentionally stay separate:

* the read projection (``extras.services.specifications.projection``) classifies stored
  values as ``current`` / ``historical`` / ``invalid`` / ``unknown`` with reason codes,
  and serves the API, GraphQL and filter consumers;
* the detail-view renderer (``assets.spec_display``) builds display entries from the
  field-type-aware resolution in ``assets.customfields`` and uses its own display state
  names (``ok``, ``unknown_value``, ``deprecated``, ...).

Decision B keeps both paths: no user-visible defect was found, the display contract and
the query-ceiling assertions stay untouched, and converging the two onto one loader is a
post-1.0 option. This module does not change either path. It declares the canonical
vocabulary as data, pins it against each side, and documents the explicit mapping
between them, so a rename or a new state on either side fails CI instead of surfacing
in the UI.

The user-facing description of these states lives in ``docs/usage/custom-fields.md``.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import get_args

import assets.spec_display as spec_display
from assets.spec_display import (
    STATE_DEPRECATED,
    STATE_HISTORICAL,
    STATE_INVALID,
    STATE_OK,
    STATE_UNKNOWN_FIELD,
    STATE_UNKNOWN_VALUE,
    build_entry,
    build_further_values,
)
from extras.services.specifications import projection as projection_module
from extras.services.specifications.contracts import ProjectionReasonCode, ProjectionState
from extras.services.specifications.projection import project_specification_values

from .test_specification_history_projection import choice_set, field, request

# Canonical vocabulary, declared as data. Changing a name or adding a state is a
# reviewed edit here; the assertions below fail until both sides agree with it.
PROJECTION_STATES = {
    "current": "The stored value is valid for the currently composed definition.",
    "historical": "The value is retained but its definition or choice is no longer active.",
    "invalid": "The stored value does not satisfy its definition; it is kept raw.",
    "unknown": "The stored key resolves to no definition, current or historical.",
}

PROJECTION_REASON_CODES = {
    "ACTIVE_VALUE": "Valid value of a current, active definition.",
    "INACTIVE_COMPOSITION": "Definition is no longer part of the active composition.",
    "DEPRECATED_FIELD": "The field definition itself is deprecated.",
    "DEPRECATED_CHOICE": "The stored value selects a deprecated choice.",
    "INVALID_STORED_VALUE": "The stored value fails validation for its definition.",
    "UNKNOWN_DEFINITION": "No current or historical definition exists for the stored key.",
    "MISSING_REQUIRED": "A required field has no stored value (reported as an issue, not an entry).",
}

DISPLAY_STATES = {
    STATE_OK: "Value renders normally.",
    STATE_INVALID: "Stored value does not fit its field type; shown raw.",
    STATE_UNKNOWN_VALUE: "Single or multi choice key is not in the current choice set.",
    STATE_DEPRECATED: "Stored choice is a deprecated tombstone.",
    STATE_HISTORICAL: "Value of a field outside the active composition.",
    STATE_UNKNOWN_FIELD: "Stored key with no definition at all.",
}

# Explicit mapping from the projection vocabulary to the display vocabulary. The two are
# not one-to-one: a deprecated choice is ``historical`` in the projection but a
# ``deprecated`` item in the detail view, and an unknown choice key is an ``invalid``
# projection that the detail view shows as ``unknown_value``.
PROJECTION_TO_DISPLAY = {
    "current": frozenset({STATE_OK}),
    "historical": frozenset({STATE_HISTORICAL, STATE_DEPRECATED}),
    "invalid": frozenset({STATE_INVALID, STATE_UNKNOWN_VALUE}),
    "unknown": frozenset({STATE_UNKNOWN_FIELD}),
}


def _display_state_constants() -> dict[str, str]:
    """Every ``STATE_*`` constant of the display module, keyed by its name."""
    return {name: value for name, value in vars(spec_display).items() if name.startswith("STATE_")}


def _choice_definition(name: str, *, field_type: str, choices: tuple[str, ...], deprecated: tuple[str, ...] = ()):
    """Lightweight stand-in for a Django definition as ``spec_display`` reads it."""
    rows = [
        SimpleNamespace(key=key, label=key.title(), lifecycle="active", position=index)
        for index, key in enumerate(choices)
    ]
    rows += [
        SimpleNamespace(key=key, label=key.title(), lifecycle="deprecated", position=len(choices) + index)
        for index, key in enumerate(deprecated)
    ]
    return SimpleNamespace(
        name=name,
        label=name,
        field_type=field_type,
        canonical_unit=None,
        choice_set=SimpleNamespace(choices=SimpleNamespace(all=lambda: rows)),
    )


class ProjectionVocabularyTests(unittest.TestCase):
    def test_declared_projection_states_match_the_contract_literal(self):
        self.assertEqual(set(get_args(ProjectionState)), set(PROJECTION_STATES))

    def test_declared_reason_codes_match_the_contract_literal(self):
        self.assertEqual(set(get_args(ProjectionReasonCode)), set(PROJECTION_REASON_CODES))

    def test_declared_reason_order_covers_every_reason_code_exactly_once(self):
        order = projection_module._REASON_ORDER  # pinned here so reordering or dropping a code fails
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(set(order), set(PROJECTION_REASON_CODES))

    def test_projection_exposes_exactly_the_declared_states_and_reason_codes(self):
        definitions = {
            "status": field(
                "status",
                "single_select",
                choice_definition=choice_set(active=("in_service",), deprecated=("old_status",)),
            ),
            "cost": field("cost", "decimal", scale=2),
            "owner_note": field("owner_note", "text"),
            "legacy_note": field("legacy_note", "text", lifecycle="deprecated"),
            "f4_cost": field("f4_cost", "decimal", scale=2),
            "required_status": field(
                "required_status",
                "single_select",
                required=True,
                choice_definition=choice_set(active=("active_z",)),
            ),
        }
        current = ("status", "cost", "owner_note", "legacy_note", "required_status")
        stored = {
            "status": "old_status",  # DEPRECATED_CHOICE -> historical
            "cost": "broken",  # INVALID_STORED_VALUE -> invalid
            "owner_note": "x",  # ACTIVE_VALUE -> current
            "legacy_note": "kept",  # DEPRECATED_FIELD -> historical
            "ghost_key": "v",  # UNKNOWN_DEFINITION -> unknown
        }
        historical = dict(definitions)
        historical["f4_cost"] = definitions["f4_cost"]  # INACTIVE_COMPOSITION -> historical
        stored["f4_cost"] = "12.30"
        result = project_specification_values(request(historical, current, stored))

        observed_states = {entry.state for entry in result.entries}
        observed_reasons = {reason for entry in result.entries for reason in entry.reason_codes}
        observed_reasons.update(issue.code for issue in result.missing_required_issues)

        self.assertEqual(observed_states, set(PROJECTION_STATES))
        self.assertEqual(observed_reasons, set(PROJECTION_REASON_CODES))


class DisplayVocabularyTests(unittest.TestCase):
    def test_display_state_constants_match_the_declared_vocabulary(self):
        self.assertEqual(set(_display_state_constants().values()), set(DISPLAY_STATES))

    def test_every_display_state_is_produced_by_the_detail_renderer(self):
        text = _choice_definition("note", field_type="text", choices=())
        number = _choice_definition("count", field_type="integer", choices=())
        status = _choice_definition(
            "status", field_type="single-select", choices=("in_service",), deprecated=("old_status",)
        )

        produced = {
            build_entry(text, "hello").state,
            build_entry(number, "not-a-number").state,
            build_entry(status, "zz").state,
            build_entry(status, "old_status").state,
        }
        produced.add(build_further_values((), {"note": "x"}, {"note": text})[0].state)
        produced.add(build_further_values((), {"ghost": "x"}, {})[0].state)

        self.assertEqual(produced, set(DISPLAY_STATES))


class VocabularyMappingTests(unittest.TestCase):
    def test_mapping_covers_every_projection_state(self):
        self.assertEqual(set(PROJECTION_TO_DISPLAY), set(PROJECTION_STATES))

    def test_mapping_targets_exactly_the_declared_display_states(self):
        mapped = frozenset().union(*PROJECTION_TO_DISPLAY.values())
        self.assertEqual(mapped, frozenset(DISPLAY_STATES))

    def test_mapping_is_not_one_to_one_for_the_documented_cases(self):
        # Deprecated choices are historical in the projection but deprecated in the detail view.
        self.assertIn(STATE_DEPRECATED, PROJECTION_TO_DISPLAY["historical"])
        # An unknown choice key is an invalid projection but an unknown_value detail item.
        self.assertIn(STATE_UNKNOWN_VALUE, PROJECTION_TO_DISPLAY["invalid"])
