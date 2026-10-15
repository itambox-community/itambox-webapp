from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import translation

from assets.spec_display import (
    KIND_BOOLEAN,
    KIND_CHIPS,
    KIND_CHOICE,
    KIND_DATE,
    KIND_NUMBER,
    KIND_TEXT,
    KIND_UNKNOWN,
    STATE_DEPRECATED,
    STATE_HISTORICAL,
    STATE_INVALID,
    STATE_OK,
    STATE_UNKNOWN_FIELD,
    STATE_UNKNOWN_VALUE,
    build_entry,
    build_further_values,
    build_spec_groups,
    build_spec_panel,
)


def _choice(key, label, position, lifecycle="active"):
    return SimpleNamespace(key=key, label=label, position=position, lifecycle=lifecycle)


def _choice_set(*choices):
    return SimpleNamespace(choices=SimpleNamespace(all=lambda: list(choices)))


def _definition(name, field_type, label=None, unit=None, choice_set=None):
    return SimpleNamespace(
        name=name,
        label=label or name.title(),
        field_type=field_type,
        canonical_unit=unit,
        choice_set=choice_set,
    )


def _resolved(definition, *provenance):
    return SimpleNamespace(definition=definition, provenance=tuple(provenance))


class ScalarRendererTests(SimpleTestCase):
    def test_boolean_true_false_and_mismatch(self):
        definition = _definition("poe", "boolean")
        self.assertIs(build_entry(definition, True).flag, True)
        self.assertIs(build_entry(definition, False).flag, False)
        self.assertEqual(build_entry(definition, False).kind, KIND_BOOLEAN)
        mismatch = build_entry(definition, "True")
        self.assertEqual((mismatch.kind, mismatch.text, mismatch.state), (KIND_TEXT, "True", STATE_INVALID))

    def test_integer_grouping_per_locale_and_unit(self):
        definition = _definition("ports", "integer", unit="U")
        with translation.override("de"):
            self.assertEqual(build_entry(definition, 12000).text, "12.000")
        with translation.override("en"):
            self.assertEqual(build_entry(definition, 12000).text, "12,000")
        self.assertEqual(build_entry(definition, 48).text, "48")
        self.assertEqual(build_entry(definition, 48).unit, "U")

    def test_integer_mismatch_list_and_bool(self):
        definition = _definition("ports", "integer")
        for value in (["1"], True, "abc", 1.5):
            entry = build_entry(definition, value)
            self.assertEqual(entry.state, STATE_INVALID, value)
        self.assertEqual(build_entry(definition, ["1"]).text, '["1"]')

    def test_decimal_strips_trailing_zeros(self):
        definition = _definition("poe_budget", "decimal", unit="W")
        with translation.override("en"):
            cases = {
                "740.000": "740",
                "1.500": "1.5",
                "0.000": "0",
                "-2.50": "-2.5",
                "7": "7",
                "1200.000": "1,200",
                "1E+3": "1,000",
            }
            for stored, shown in cases.items():
                entry = build_entry(definition, stored)
                self.assertEqual(entry.text, shown, stored)
                self.assertNotIn("E", entry.text)
        entry = build_entry(definition, "740.000")
        self.assertEqual((entry.unit, entry.raw, entry.kind), ("W", "740.000", KIND_NUMBER))

    def test_decimal_german_separator_and_invalid(self):
        definition = _definition("weight", "decimal")
        with translation.override("de"):
            self.assertEqual(build_entry(definition, "1.500").text, "1,5")
        for value in ("abc", "NaN", "Infinity", None, ["1"], True):
            self.assertEqual(build_entry(definition, value).state, STATE_INVALID, value)

    def test_date_local_format_and_invalid(self):
        definition = _definition("eol", "date")
        with translation.override("de"):
            entry = build_entry(definition, "2026-03-04")
        self.assertEqual((entry.kind, entry.raw), (KIND_DATE, "2026-03-04"))
        self.assertIn("2026", entry.text)
        self.assertNotEqual(entry.text, "2026-03-04")
        self.assertEqual(build_entry(definition, "2026-02-31").state, STATE_INVALID)
        self.assertEqual(build_entry(definition, "soon").text, "soon")

    def test_text_truncates_and_keeps_full_value(self):
        definition = _definition("notes", "text")
        long_text = "x" * 300
        entry = build_entry(definition, long_text)
        self.assertLess(len(entry.text), 130)
        self.assertEqual(entry.raw, long_text)
        short = build_entry(definition, "short")
        self.assertEqual((short.text, short.raw), ("short", ""))

    def test_units_symbol_ppm_unknown_token_and_none(self):
        self.assertEqual(build_entry(_definition("a", "integer", unit="Gbit/s"), 10).unit, "Gbit/s")
        self.assertEqual(build_entry(_definition("a", "integer"), 10).unit, "")
        self.assertEqual(build_entry(_definition("a", "integer", unit="VA"), 10).unit, "VA")
        self.assertEqual(build_entry(_definition("a", "integer", unit="frobnicate"), 10).unit, "frobnicate")
        self.assertEqual(build_entry(_definition("a", "integer", unit="pages_per_minute"), 10).unit, "ppm")
        # The unit token is vocabulary: a locale changes it only through a translation row.
        translated = SimpleNamespace(unit=lambda definition: "S/min", field_label=lambda definition: definition.label)
        entry = build_entry(_definition("a", "integer", unit="pages_per_minute"), 10, translator=translated)
        self.assertEqual(entry.unit, "S/min")


class ChoiceRendererTests(SimpleTestCase):
    def setUp(self):
        self.choices = _choice_set(
            _choice("1g", "1 Gbit/s", 1),
            _choice("10g", "10 Gbit/s", 2),
            _choice("old", "Old speed", 3, lifecycle="deprecated"),
        )

    def test_single_select_label_and_tooltip_key(self):
        entry = build_entry(_definition("os", "single-select", choice_set=self.choices), "10g")
        self.assertEqual((entry.kind, entry.text, entry.raw, entry.state), (KIND_CHOICE, "10 Gbit/s", "10g", STATE_OK))

    def test_single_select_deprecated_unknown_and_no_set(self):
        definition = _definition("os", "single-select", choice_set=self.choices)
        self.assertEqual(build_entry(definition, "old").state, STATE_DEPRECATED)
        unknown = build_entry(definition, "network_os")
        self.assertEqual((unknown.text, unknown.state), ("network_os", STATE_UNKNOWN_VALUE))
        bare = build_entry(_definition("os", "single-select"), "network_os")
        self.assertEqual((bare.text, bare.state), ("network_os", STATE_OK))

    def test_multi_select_position_order_not_storage_order(self):
        entry = build_entry(_definition("speeds", "multi-select", choice_set=self.choices), ["10g", "1g"])
        self.assertEqual(entry.kind, KIND_CHIPS)
        self.assertEqual([item.text for item in entry.items], ["1 Gbit/s", "10 Gbit/s"])

    def test_multi_select_item_states_and_mismatch(self):
        definition = _definition("speeds", "multi-select", choice_set=self.choices)
        entry = build_entry(definition, ["old", "gone"])
        self.assertEqual([item.state for item in entry.items], [STATE_DEPRECATED, STATE_UNKNOWN_VALUE])
        self.assertEqual(build_entry(definition, "10g").state, STATE_INVALID)


class GroupingAndCompletenessTests(SimpleTestCase):
    def setUp(self):
        self.a = _definition("alpha", "text", label="Alpha")
        self.b = _definition("beta", "integer", label="Beta")
        self.c = _definition("gamma", "text", label="Gamma")

    def test_first_fieldset_wins_and_global_goes_last(self):
        resolved = [
            _resolved(self.a, "local/net", "local/misc"),
            _resolved(self.b, "local/misc"),
            _resolved(self.c),
        ]
        stored = {"alpha": "a", "beta": 1, "gamma": "g"}
        groups = build_spec_groups(resolved, stored, {"local/net": "Networking", "local/misc": "Misc"})
        self.assertEqual([g.label for g in groups], ["Networking", "Misc", "Other"])
        self.assertEqual([e.key for e in groups[0].entries], ["alpha"])

    def test_single_ungrouped_group_has_no_heading_and_empty_hidden(self):
        groups = build_spec_groups([_resolved(self.a), _resolved(self.b)], {"alpha": "a", "beta": ""})
        self.assertEqual(len(groups), 1)
        self.assertIsNone(groups[0].label)
        self.assertEqual([e.key for e in groups[0].entries], ["alpha"])

    def test_further_values_cases(self):
        resolved = [_resolved(self.a, "local/net")]
        stored = {"alpha": "a", "beta": 5, "ghost": {"x": 1}, "gamma": ""}
        entries = {e.key: e for e in build_further_values(resolved, stored, {"beta": self.b, "gamma": self.c})}
        self.assertEqual(set(entries), {"beta", "ghost", "gamma"})
        self.assertEqual((entries["beta"].text, entries["beta"].state), ("5", STATE_HISTORICAL))
        self.assertEqual((entries["ghost"].kind, entries["ghost"].state), (KIND_UNKNOWN, STATE_UNKNOWN_FIELD))
        self.assertEqual(entries["ghost"].text, '{"x": 1}')
        self.assertEqual((entries["gamma"].text, entries["gamma"].state), ("-", STATE_HISTORICAL))

    def test_panel_truthiness(self):
        self.assertFalse(build_spec_panel([], {}, []))
        legacy = build_spec_panel([], {"ghost": 1}, [])
        self.assertTrue(legacy)
        self.assertFalse(legacy.groups)
        resolved = [_resolved(self.a, "local/net")]
        self.assertFalse(build_spec_panel(resolved, {}, []))

    def test_loader_called_only_for_unresolved_keys(self):
        calls = []
        resolved = [_resolved(self.a, "local/net")]
        build_spec_panel(resolved, {"alpha": "a"}, [], definitions_loader=lambda keys: calls.append(keys) or {})
        self.assertEqual(calls, [])
        build_spec_panel(
            resolved, {"alpha": "a", "zzz": 1}, [], definitions_loader=lambda keys: calls.append(keys) or {}
        )
        self.assertEqual(calls, [["zzz"]])


class SpecValueTemplateTests(SimpleTestCase):
    def _render(self, entry):
        return render_to_string("assets/includes/detail/_spec_value.html", {"entry": entry})

    def test_number_unit_and_raw_tooltip(self):
        html = self._render(build_entry(_definition("p", "decimal", unit="W"), "740.000"))
        self.assertIn("740 W", html)
        self.assertIn('title="740.000"', html)

    def test_chips_and_markers_escape(self):
        choices = _choice_set(_choice("a", "<b>A</b>", 1))
        html = self._render(build_entry(_definition("m", "multi-select", choice_set=choices), ["a", "zz"]))
        self.assertIn("badge bg-secondary-lt", html)
        self.assertIn("&lt;b&gt;A&lt;/b&gt;", html)
        self.assertIn("unknown value", html)

    def test_unknown_and_invalid_markers(self):
        self.assertIn("<code>", self._render(build_further_values([], {"ghost": 1})[0]))
        self.assertIn("invalid", self._render(build_entry(_definition("n", "integer"), "abc")))
