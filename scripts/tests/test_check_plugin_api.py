"""Plugin API inventory gate: derivation from source, the manifest, and drift detection.

The Stable plugin surface is an external promise. These tests pin three things:

* the live source still derives the checked-in manifest (``Derivation*``);
* the gate fails when the surface drifts without a manifest edit (``Drift*``);
* the gate can only pass while the public reference lists exactly the manifest's tiers.

The gate imports no Django, so it runs on the bare interpreter with the other
repository gate suites.
"""

import copy
import json
import unittest
from pathlib import Path

from scripts import check_plugin_api as gate

REPO_ROOT = Path(__file__).resolve().parents[2]


def _live_inputs():
    manifest = gate.load_manifest()
    derived = gate.derive_surface()
    doc_tiers = gate.parse_doc_tiers(gate.API_DOC.read_text(encoding="utf-8"))
    fixture_names = gate.fixture_exercised_names()
    return manifest, derived, doc_tiers, fixture_names


class DerivationAgainstSourceTests(unittest.TestCase):
    """Characterization: the manifest is what the source says today."""

    def test_live_source_matches_the_checked_in_manifest(self):
        manifest, derived, doc_tiers, fixture_names = _live_inputs()
        self.assertEqual(gate.check(manifest, derived, doc_tiers, fixture_names), [])

    def test_config_attributes_are_read_from_the_plugin_config_class(self):
        attributes = gate.derive_config_attributes()
        self.assertIn("settings_schema", attributes)
        self.assertIn("itambox_range", attributes)
        self.assertNotIn("register_plugin_menu", attributes)

    def test_resource_defaults_match_the_assembly_default_table(self):
        self.assertEqual(
            gate.derive_resource_defaults(),
            {
                "graphql_schema": "graphql.schema",
                "menu": "navigation.menu",
                "menu_items": "navigation.menu_items",
                "permission_targets": "permissions.targets",
                "template_extensions": "template_content.template_extensions",
                "viewsets": "api.viewsets.viewsets",
            },
        )

    def test_public_symbols_exclude_private_names(self):
        symbols = gate.derive_public_symbols()
        for names in symbols.values():
            for name in names:
                self.assertFalse(name.startswith("_"), name)
        self.assertIn("PluginTemplateContent", symbols["itambox.plugins.views"])
        self.assertEqual(
            symbols["itambox.api.viewsets"],
            ["ITAMBoxModelViewSet", "ITAMBoxReadOnlyModelViewSet"],
        )
        self.assertNotIn("BaseViewSet", symbols["itambox.api.viewsets"])

    def test_fixture_plugin_exercises_each_stable_resource(self):
        names = gate.fixture_exercised_names()
        for key in ("template_extensions", "menu", "menu_items", "viewsets"):
            self.assertIn(key, names)
        self.assertTrue((gate.FIXTURE_PLUGIN / "graphql" / "schema.py").is_file())
        self.assertTrue((gate.FIXTURE_PLUGIN / "urls.py").is_file())


class DriftDetectionTests(unittest.TestCase):
    """Behavioural: the gate fails when the surface changes without a manifest edit."""

    def setUp(self):
        self.manifest, self.derived, self.doc_tiers, self.fixture_names = _live_inputs()

    def _problems(self, manifest=None, derived=None, doc_tiers=None, fixture_names=None):
        return gate.check(
            manifest or self.manifest,
            derived or self.derived,
            doc_tiers if doc_tiers is not None else self.doc_tiers,
            fixture_names if fixture_names is not None else self.fixture_names,
        )

    def test_new_plugin_config_attribute_is_reported(self):
        derived = copy.deepcopy(self.derived)
        derived["config_attributes"].append("new_resource")
        problems = self._problems(derived=derived)
        self.assertTrue(any("'new_resource' is not in the manifest" in p for p in problems), problems)

    def test_removed_plugin_config_attribute_is_reported(self):
        derived = copy.deepcopy(self.derived)
        derived["config_attributes"].remove("viewsets")
        problems = self._problems(derived=derived)
        self.assertTrue(any("'viewsets' that no longer exists" in p for p in problems), problems)

    def test_changed_resource_location_is_reported(self):
        derived = copy.deepcopy(self.derived)
        derived["resource_defaults"]["menu"] = "navigation.main_menu"
        problems = self._problems(derived=derived)
        self.assertTrue(any("resource defaults drifted" in p for p in problems), problems)

    def test_added_public_symbol_is_reported(self):
        derived = copy.deepcopy(self.derived)
        derived["public_symbols"]["itambox.plugins.views"].append("PluginNewThing")
        problems = self._problems(derived=derived)
        self.assertTrue(any("public symbols drifted" in p for p in problems), problems)

    def test_attribute_pinned_to_two_tiers_is_reported(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["experimental"]["config_attributes"].append("viewsets")
        problems = self._problems(manifest=manifest)
        self.assertTrue(any("pinned to two tiers" in p for p in problems), problems)

    def test_fixture_losing_a_stable_resource_is_reported(self):
        fixture_names = set(self.fixture_names) - {"viewsets"}
        problems = self._problems(fixture_names=fixture_names)
        self.assertTrue(any("does not exercise the Stable resource 'viewsets'" in p for p in problems), problems)

    def test_documentation_listing_an_unpinned_identifier_is_reported(self):
        doc_tiers = copy.deepcopy(self.doc_tiers)
        doc_tiers["Stable"].add("PluginNewThing")
        problems = self._problems(doc_tiers=doc_tiers)
        self.assertTrue(any("'PluginNewThing' under tier Stable but the manifest does not" in p for p in problems))

    def test_stable_member_missing_from_documentation_is_reported(self):
        doc_tiers = copy.deepcopy(self.doc_tiers)
        doc_tiers["Stable"].discard("PluginTemplateContent")
        problems = self._problems(doc_tiers=doc_tiers)
        self.assertTrue(any("lacks 'PluginTemplateContent' under tier Stable" in p for p in problems), problems)

    def test_main_reports_drift_with_exit_one(self):
        derived = copy.deepcopy(self.derived)
        derived["public_symbols"]["itambox.plugins"].append("register_plugin_menu")
        original = gate.derive_surface
        gate.derive_surface = lambda: derived
        try:
            with _captured_stdout() as out:
                exit_code = gate.main([])
        finally:
            gate.derive_surface = original
        self.assertEqual(exit_code, 1)
        self.assertIn("public symbols drifted", out.getvalue())


class ParseDocumentationTests(unittest.TestCase):
    def test_tier_rows_are_parsed_by_identifier_and_tier(self):
        text = "| `Foo` | Stable |\n| `Bar.baz` | Experimental |\n| not a row | Stable |\n"
        tiers = gate.parse_doc_tiers(text)
        self.assertEqual(tiers["Stable"], {"Foo"})
        self.assertEqual(tiers["Experimental"], {"Bar.baz"})
        self.assertEqual(tiers["Private"], set())


class ManifestShapeTests(unittest.TestCase):
    def test_manifest_declares_every_tier_and_is_valid_json(self):
        manifest = json.loads((REPO_ROOT / "scripts" / "plugin_api_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["documented"]), set(gate.TIERS))
        self.assertTrue(manifest["stable"]["config_attributes"])


class _captured_stdout:
    def __enter__(self):
        import io
        import sys

        self._previous = sys.stdout
        sys.stdout = self._buffer = io.StringIO()
        return self._buffer

    def __exit__(self, *exc):
        import sys

        sys.stdout = self._previous
        return False


if __name__ == "__main__":
    unittest.main()
