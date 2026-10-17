import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from itambox.plugins import diagnostics, preflight

MANAGE = Path(__file__).resolve().parents[2] / "manage.py"

GOOD = """
from itambox.plugins import PluginConfig


class Cfg(PluginConfig):
    name = "{name}"
    description = "fixture"
    version = "2.3.4"
    itambox_range = "{rng}"


config = Cfg
"""

NO_ABORT = {"abort": False, "plugins": [], "set": None, "pending_migrations": []}
ABORTS = {"abort": True, "plugins": [], "set": None, "pending_migrations": []}


def _runner(results):
    def run(names):
        return results[tuple(names)]

    return run


class PreflightReportTests(SimpleTestCase):
    def _report(self, results, names):
        with mock.patch.object(preflight, "configured_plugins", return_value=names):
            return preflight.run_preflight(runner=_runner(results))

    def test_ok_skipped_and_would_abort_outcomes(self):
        ok = {"status": "ok", "active": ["a"], "skipped": [], "pending_migrations": ["a.0001_initial"]}
        skipped = {"status": "ok", "active": [], "skipped": [{"plugin": "b", "reason": "needs 9.9"}]}
        abort = {"status": "abort", "plugin": "c", "phase": "resolve", "cause": "ImportError: nope"}
        report = self._report({("a",): ok, ("b",): skipped, ("c",): abort, ("a", "b", "c"): abort}, ["a", "b", "c"])
        outcomes = {row["plugin"]: row for row in report["plugins"]}
        self.assertEqual(outcomes["a"]["outcome"], "ok")
        self.assertEqual(outcomes["b"]["outcome"], "skipped")
        self.assertEqual(outcomes["c"]["outcome"], "would abort startup")
        self.assertEqual(outcomes["c"]["phase"], "resolve")
        self.assertTrue(report["abort"])
        self.assertIsNone(report["set"])
        self.assertEqual(report["pending_migrations"], ["a.0001_initial"])

    def test_skip_only_does_not_abort(self):
        skipped = {"status": "ok", "active": [], "skipped": [{"plugin": "b", "reason": "x"}]}
        report = self._report({("b",): skipped}, ["b"])
        self.assertFalse(report["abort"])

    def test_set_level_failure_aborts_even_when_each_plugin_is_ok(self):
        one = {"status": "ok", "active": ["a"], "skipped": []}
        two = {"status": "ok", "active": ["b"], "skipped": []}
        both = {"status": "abort", "plugin": "b", "phase": "compose", "cause": "ValueError: duplicate prefix"}
        report = self._report({("a",): one, ("b",): two, ("a", "b"): both}, ["a", "b"])
        self.assertTrue(report["abort"])
        self.assertEqual(report["set"]["phase"], "compose")
        self.assertIn("duplicate prefix", preflight.format_report(report))

    def test_empty_configuration_is_ok(self):
        report = self._report({}, [])
        self.assertFalse(report["abort"])
        self.assertIn("none configured", preflight.format_report(report))
        self.assertFalse(json.loads(preflight.format_report(report, "json"))["abort"])

    def test_main_exit_codes(self):
        stdout = io.StringIO()
        with mock.patch.object(preflight, "run_preflight", return_value=ABORTS):
            self.assertEqual(preflight.main([], stdout), 1)
        with mock.patch.object(preflight, "run_preflight", return_value=NO_ABORT):
            self.assertEqual(preflight.main(["--format", "json"], stdout), 0)

    def test_configured_plugins_reads_environment_then_dotenv(self):
        self.assertEqual(preflight.configured_plugins({"ITAMBOX_PLUGINS": " a, b ,"}), ["a", "b"])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "app"
            base.mkdir()
            (Path(tmp) / ".env").write_text("# c\nITAMBOX_PLUGINS='x,y'\n", encoding="utf-8")
            self.assertEqual(preflight.configured_plugins({}, base_dir=base), ["x", "y"])
            self.assertEqual(preflight.configured_plugins({}, base_dir=Path(tmp) / "none"), [])


class PreflightRealStartupTests(SimpleTestCase):
    """Boot the real startup path in a child interpreter against fixture packages on disk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _package(self, name, body):
        package = self.root / name
        package.mkdir()
        (package / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")

    def _environ(self, names):
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        env.update(
            ITAMBOX_ENV="dev",
            ITAMBOX_PLUGINS=",".join(names),
            PYTHONPATH=os.pathsep.join([str(self.root), env.get("PYTHONPATH", "")]),
        )
        return env

    def _preflight(self, names):
        environ = self._environ(names)
        return preflight.run_preflight(environ=environ, runner=lambda subset: preflight.run_startup(subset, environ))

    def test_each_outcome_and_a_failing_set(self):
        self._package("pf_good", GOOD.format(name="pf_good", rng=">=0.1,<99.0"))
        self._package("pf_old", GOOD.format(name="pf_old", rng=">=0.1,<0.2"))
        self._package("pf_bad", "raise RuntimeError('boom at import')\n")
        report = self._preflight(["pf_good", "pf_old", "pf_bad"])
        outcomes = {row["plugin"]: row["outcome"] for row in report["plugins"]}
        self.assertEqual(outcomes, {"pf_good": "ok", "pf_old": "skipped", "pf_bad": "would abort startup"})
        self.assertTrue(report["abort"])
        bad = next(row for row in report["plugins"] if row["plugin"] == "pf_bad")
        self.assertEqual(bad["phase"], "resolve")
        self.assertIn("boom at import", bad["cause"])

    def test_manage_check_exit_codes_without_a_serving_process(self):
        self._package("pf_good", GOOD.format(name="pf_good", rng=">=0.1,<99.0"))
        self._package("pf_bad", "raise RuntimeError('boom at import')\n")
        for names, expected in ((["pf_good"], 0), (["pf_good", "pf_bad"], 1)):
            with self.subTest(names=names):
                completed = subprocess.run(
                    [sys.executable, str(MANAGE), "plugins", "--check", "--format", "json"],
                    cwd=MANAGE.parent,
                    env=self._environ(names),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["abort"], bool(expected))

    def test_phase_is_reported_for_a_manifest_failure(self):
        self._package("pf_nomanifest", "x = 1\n")
        report = self._preflight(["pf_nomanifest"])
        self.assertTrue(report["abort"])
        self.assertEqual(report["plugins"][0]["phase"], "resolve")


class PluginsCommandTests(TestCase):
    def test_check_command_exits_nonzero_when_startup_would_abort(self):
        with mock.patch.object(preflight, "run_preflight", return_value=ABORTS):
            with self.assertRaises(CommandError) as raised:
                call_command("plugins", "--check", stdout=io.StringIO())
        self.assertEqual(raised.exception.returncode, 1)

    def test_check_command_succeeds_when_ok(self):
        out = io.StringIO()
        with mock.patch.object(preflight, "run_preflight", return_value=NO_ABORT):
            call_command("plugins", "--check", stdout=out)
        self.assertIn("Startup would succeed.", out.getvalue())

    def test_json_diagnostics_have_manifest_state_migrations_and_orphans(self):
        out = io.StringIO()
        call_command("plugins", "--format", "json", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(set(payload), {"active", "diagnostics", "orphans", "plugins"})
        self.assertEqual(set(payload["orphans"]), {"tables", "content_types", "migrations"})

    @override_settings(PLUGINS_ACTIVE=[], PLUGINS_DIAGNOSTICS=[])
    def test_table_diagnostics(self):
        out = io.StringIO()
        call_command("plugins", stdout=out)
        self.assertIn("none configured", out.getvalue())
        self.assertIn("Orphaned data (read-only)", out.getvalue())

    def test_orphans_list_unowned_tables(self):
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE gone_plugin_widget (id integer)")
        self.assertIn("gone_plugin_widget", diagnostics.find_orphans()["tables"])
        self.assertNotIn("django_migrations", diagnostics.find_orphans()["tables"])

    def test_health_exposes_plugin_state_without_failing(self):
        self.assertEqual(self.client.get(reverse("health")).json()["checks"]["plugins"], "ok")
        with override_settings(PLUGINS_DISABLED=["old"]):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"]["plugins"], "skipped")
