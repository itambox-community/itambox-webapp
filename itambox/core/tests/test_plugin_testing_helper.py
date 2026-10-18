"""The plugin test helper: success report and failure messages naming plugin and phase."""

from pathlib import Path

from django.test import SimpleTestCase

from itambox.plugins.testing import PluginReport, PluginVerificationError, verify_plugins

FIXTURES = Path(__file__).resolve().parent / "plugin_fixtures"


class VerifyPluginsTests(SimpleTestCase):
    def verify(self, *plugins):
        return verify_plugins(*plugins, pythonpath=[FIXTURES])

    def test_healthy_plugin_is_reported_with_its_registrations(self):
        report = self.verify("fixture_plugin")
        self.assertIsInstance(report, PluginReport)
        self.assertEqual(report.active, ("fixture_plugin",))
        record = report.plugins["fixture_plugin"]
        self.assertTrue(record["ui"])
        self.assertEqual(record["api_prefixes"], ["manufacturers"])
        for kind in ("template_extensions", "menu", "menu_items", "viewsets", "permission_targets", "graphql_schema"):
            self.assertIn(kind, record["registered"])

    def test_requires_a_plugin(self):
        with self.assertRaises(ValueError):
            verify_plugins()

    def test_failures_name_plugin_and_phase(self):
        cases = (
            ("fxbad_import", "resolve"),
            ("fxbad_range_malformed", "resolve"),
            ("fxbad_compose", "compose"),
            ("fxbad_ready", "ready"),
            ("fxbad_graphql", "assemble"),
            ("fxbad_urlconf", "assemble"),
        )
        for plugin, phase in cases:
            with self.subTest(plugin=plugin):
                with self.assertRaises(PluginVerificationError) as raised:
                    self.verify(plugin)
                self.assertEqual(raised.exception.plugin, plugin)
                self.assertEqual(raised.exception.phase, phase)
                self.assertIn(plugin, str(raised.exception))

    def test_out_of_range_plugin_is_a_failure_not_a_silent_skip(self):
        with self.assertRaises(PluginVerificationError) as raised:
            self.verify("fxskip_range")
        self.assertEqual(raised.exception.plugin, "fxskip_range")
        self.assertIn("skipped", str(raised.exception))
