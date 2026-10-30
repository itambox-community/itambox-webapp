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
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.management.commands import plugins as plugins_command
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

    def test_combined_startup_is_authoritative_over_isolated_probes(self):
        one = {"status": "ok", "active": ["a"], "skipped": [], "pending_migrations": ["a.0001"]}
        isolated_failure = {"status": "abort", "plugin": "b", "phase": "compose", "cause": "missing sibling"}
        combined = {
            "status": "ok",
            "active": ["a", "b"],
            "skipped": [],
            "pending_migrations": ["a.0001", "b.0001"],
        }
        report = self._report({("a",): one, ("b",): isolated_failure, ("a", "b"): combined}, ["a", "b"])

        self.assertFalse(report["abort"])
        self.assertEqual([row["outcome"] for row in report["plugins"]], ["ok", "ok"])
        self.assertEqual(report["pending_migrations"], ["a.0001", "b.0001"])

    def test_set_level_failure_aborts_even_when_each_plugin_is_ok(self):
        one = {"status": "ok", "active": ["a"], "skipped": []}
        two = {"status": "ok", "active": ["b"], "skipped": []}
        both = {"status": "abort", "plugin": "b", "phase": "compose", "cause": "ValueError: duplicate prefix"}
        report = self._report({("a",): one, ("b",): two, ("a", "b"): both}, ["a", "b"])
        self.assertTrue(report["abort"])
        self.assertEqual(report["set"]["phase"], "compose")
        self.assertIn("duplicate prefix", preflight.format_report(report))

    def test_empty_configuration_is_ok(self):
        result = {"status": "ok", "active": [], "skipped": [], "pending_migrations": [], "version": "1.2.3"}
        report = self._report({(): result}, [])
        self.assertFalse(report["abort"])
        self.assertIn("none configured", preflight.format_report(report))
        self.assertEqual(report["version"], "1.2.3")
        self.assertFalse(json.loads(preflight.format_report(report, "json"))["abort"])

    def test_main_exit_codes(self):
        stdout = io.StringIO()
        with mock.patch.object(preflight, "run_preflight", return_value=ABORTS):
            self.assertEqual(preflight.main(["--check"], stdout), 1)
        with mock.patch.object(preflight, "run_preflight", return_value=NO_ABORT):
            json_out = io.StringIO()
            self.assertEqual(preflight.main(["--check", "--format=json"], json_out), 0)
            self.assertFalse(json.loads(json_out.getvalue())["abort"])

    def test_default_runner_receives_explicit_environment(self):
        environ = {"ITAMBOX_PLUGINS": "a"}
        result = {"status": "ok", "active": ["a"], "skipped": [], "pending_migrations": []}
        with mock.patch.object(preflight, "run_startup", return_value=result) as run_startup:
            report = preflight.run_preflight(environ=environ)

        self.assertFalse(report["abort"])
        self.assertEqual(run_startup.call_count, 2)
        self.assertTrue(all(call.kwargs["environ"] is environ for call in run_startup.call_args_list))

    def test_unknown_pending_migrations_are_not_reported_as_none(self):
        unknown = {"status": "ok", "active": ["a"], "skipped": [], "pending_migrations": None}
        report = self._report({("a",): unknown}, ["a"])

        self.assertIsNone(report["pending_migrations"])
        self.assertIn("Pending plugin migrations: unknown", preflight.format_report(report))

    def test_configured_plugins_reads_environment_then_dotenv(self):
        self.assertEqual(preflight.configured_plugins({"ITAMBOX_PLUGINS": " a, b ,"}), ["a", "b"])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "app"
            base.mkdir()
            (Path(tmp) / ".env").write_text("# c\nITAMBOX_PLUGINS='x,y'\n", encoding="utf-8")
            self.assertEqual(preflight.configured_plugins({}, base_dir=base), ["x", "y"])
            empty = Path(tmp) / "a" / "b"
            empty.mkdir(parents=True)
            self.assertEqual(preflight.configured_plugins({}, base_dir=empty), [])

    def test_dotenv_skips_noise_and_reads_unquoted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "app"
            base.mkdir()
            (base / ".env").write_text("\nnot a pair\nOTHER=1\nITAMBOX_PLUGINS = z \n", encoding="utf-8")
            self.assertEqual(preflight.configured_plugins({}, base_dir=base), ["z"])
            (base / ".env").write_text("OTHER=1\n", encoding="utf-8")
            self.assertEqual(preflight.configured_plugins({}, base_dir=base), [])

    def test_run_startup_reads_the_child_result_or_reports_the_last_error_line(self):
        marker_line = preflight.RESULT_MARKER + json.dumps({"status": "ok", "active": ["a"]})
        done = subprocess.CompletedProcess([], 0, stdout="noise\n" + marker_line + "\n", stderr="")
        with mock.patch.object(preflight.subprocess, "run", return_value=done) as run:
            self.assertEqual(preflight.run_startup(["a", "b"], environ={}), {"status": "ok", "active": ["a"]})
        self.assertEqual(run.call_args.kwargs["env"]["ITAMBOX_PLUGINS"], "a,b")

        crashed = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback\nSystemError: boom\n")
        with mock.patch.object(preflight.subprocess, "run", return_value=crashed):
            result = preflight.run_startup(["a"], environ={})
        self.assertEqual(result["status"], "abort")
        self.assertEqual(result["phase"], "startup")
        self.assertEqual(result["cause"], "SystemError: boom")

    def test_table_shows_phase_and_cause(self):
        report = {
            "abort": True,
            "plugins": [{"plugin": "a", "outcome": preflight.ABORT, "phase": "compose", "cause": "ValueError: x"}],
            "set": None,
            "pending_migrations": [],
        }
        self.assertIn("a  would abort startup  compose: ValueError: x", preflight.format_report(report))

    def test_main_honours_settings_and_pythonpath(self):
        with (
            mock.patch.dict(os.environ, {"PYTHONPATH": "existing"}),
            mock.patch.object(preflight, "run_preflight", return_value=NO_ABORT),
        ):
            code = preflight.main(["--check", "--settings=core.settings", "--pythonpath=extra"], io.StringIO())
            self.assertEqual(code, 0)
            self.assertEqual(os.environ["DJANGO_SETTINGS_MODULE"], "core.settings")
            self.assertEqual(os.environ["PYTHONPATH"], os.pathsep.join(("extra", "existing")))


class PluginInventoryTests(SimpleTestCase):
    @override_settings(
        PLUGINS_ACTIVE=["plugin_active"],
        PLUGINS_DIAGNOSTICS=[{"plugin": "plugin_skipped"}],
        PLUGINS_CONFIG={"plugin_active": {"token": "inventory-private-sentinel"}},
        PLUGINS_RESOLVED_CONFIG={"plugin_active": {"token": "inventory-private-sentinel"}},
        PLUGINS_SECRET_KEYS={"plugin_active": ["token"]},
        VERSION="3.1.0",
    )
    def test_manifest_state_resources_and_applied_pending_migrations(self):
        manifest = {"version": "2.3.4", "itambox_range": ">=2.3,<2.4"}
        resources = {
            "template_extensions": [{"model": "assets.asset", "class_name": "plugin_active.Content"}],
            "menu": [],
            "menu_items": [],
            "viewsets": [],
            "permission_targets": [],
            "graphql_schema": [],
        }
        migration_rows = {
            "plugin_active": {
                "applied_migrations": ["plugin_active.0001_initial"],
                "pending_migrations": ["plugin_active.0002_add_field"],
            },
            "plugin_skipped": {
                "applied_migrations": ["plugin_skipped.0001_initial"],
                "pending_migrations": [],
            },
        }
        with (
            mock.patch.object(diagnostics, "_manifest", return_value=manifest),
            mock.patch.object(diagnostics, "plugin_app_labels", return_value=["plugin_active"]),
            mock.patch.object(diagnostics, "_skipped_plugin_app_labels", return_value=["plugin_skipped"]),
            mock.patch.object(diagnostics, "migration_state", return_value=migration_rows) as migration_state,
            mock.patch.object(
                diagnostics, "plugin_resources", side_effect=[resources, diagnostics.empty_plugin_resources()]
            ),
        ):
            inventory = diagnostics.plugin_inventory()

        self.assertEqual(
            inventory,
            [
                {
                    "plugin": "plugin_active",
                    "state": "active",
                    **manifest,
                    "running_version": "3.1.0",
                    "resources": resources,
                    **migration_rows["plugin_active"],
                },
                {
                    "plugin": "plugin_skipped",
                    "state": "skipped",
                    **manifest,
                    "running_version": "3.1.0",
                    "resources": diagnostics.empty_plugin_resources(),
                    **migration_rows["plugin_skipped"],
                },
            ],
        )
        self.assertNotIn("inventory-private-sentinel", repr(inventory))
        migration_state.assert_called_once_with({"plugin_active", "plugin_skipped"}, loaded_labels={"plugin_active"})

    def test_inventory_with_no_plugins_does_not_query_migration_state(self):
        with (
            mock.patch.object(diagnostics, "migration_state") as migration_state,
            override_settings(PLUGINS_ACTIVE=[], PLUGINS_DIAGNOSTICS=[]),
        ):
            self.assertEqual(diagnostics.plugin_inventory(), [])

        migration_state.assert_not_called()

    def test_migration_state_reads_all_labels_with_one_executor(self):
        migration = mock.Mock()
        migration.app_label = "plugin_a"
        migration.name = "0002_second"
        executor = mock.Mock()
        executor.loader.applied_migrations = {("plugin_a", "0001_initial"): object()}
        executor.loader.graph.leaf_nodes.return_value = [("plugin_a", "0002_second")]
        executor.migration_plan.return_value = [(migration, False)]

        with mock.patch.object(diagnostics, "MigrationExecutor", return_value=executor) as create_executor:
            state = diagnostics.migration_state({"plugin_a", "plugin_b"}, loaded_labels={"plugin_a", "plugin_b"})

        self.assertEqual(
            state,
            {
                "plugin_a": {
                    "applied_migrations": ["plugin_a.0001_initial"],
                    "pending_migrations": ["plugin_a.0002_second"],
                },
                "plugin_b": {"applied_migrations": [], "pending_migrations": []},
            },
        )
        create_executor.assert_called_once_with(diagnostics.connection)
        executor.migration_plan.assert_called_once_with([("plugin_a", "0002_second")])

    def test_plugin_resources_group_all_extension_points_by_owner(self):
        class Content:
            pass

        class OtherContent:
            pass

        class Menu:
            pass

        class MenuItem:
            pass

        class ViewSet:
            pass

        class Query:
            pass

        registry = mock.Mock()
        registry._plugin_template_contents = {"assets.asset": [Content, OtherContent]}
        registry._plugin_template_content_sources = {"assets.asset": ["inventory_plugin", "other_plugin"]}
        registry._plugin_menus = [Menu]
        registry._plugin_menu_sources = ["inventory_plugin"]
        registry.get_plugin_menus.return_value = [Menu]
        registry._plugin_menu_items = [MenuItem]
        registry._plugin_menu_item_sources = ["inventory_plugin"]
        registry.get_plugin_menu_items.return_value = [MenuItem]
        registry.get_plugin_viewsets.return_value = {
            "inventory_plugin": [("assets", ViewSet, "asset")],
            "other_plugin": [("roles", OtherContent, "role")],
        }
        registry.get_plugin_permission_targets.return_value = {
            "inventory_plugin": ("inventory_plugin.asset",),
            "other_plugin": ("other_plugin.role",),
        }
        registry.get_plugin_graphql.return_value = (("inventory_plugin", Query), ("other_plugin", OtherContent))

        with mock.patch.object(diagnostics, "registry", registry):
            resources = diagnostics.plugin_resources("inventory_plugin")

        self.assertEqual(
            resources,
            {
                "template_extensions": [
                    {"model": "assets.asset", "class_name": f"{Content.__module__}.{Content.__qualname__}"}
                ],
                "menu": [f"{Menu.__module__}.{Menu.__qualname__}"],
                "menu_items": [f"{MenuItem.__module__}.{MenuItem.__qualname__}"],
                "viewsets": [
                    {"prefix": "assets", "viewset": f"{ViewSet.__module__}.{ViewSet.__qualname__}", "basename": "asset"}
                ],
                "permission_targets": ["inventory_plugin.asset"],
                "graphql_schema": [f"{Query.__module__}.{Query.__qualname__}"],
            },
        )

    def test_manifest_reads_the_plugin_config(self):
        module = mock.Mock(config=mock.Mock(version="2.3.4", itambox_range=">=2.3"))
        with mock.patch.object(diagnostics.importlib, "import_module", return_value=module):
            self.assertEqual(diagnostics._manifest("pf_x"), {"version": "2.3.4", "itambox_range": ">=2.3"})

    def test_database_errors_are_reported_not_raised(self):
        self.assertEqual(diagnostics.pending_migrations([]), [])
        with mock.patch.object(diagnostics, "MigrationExecutor", side_effect=OperationalError("down")):
            self.assertIsNone(diagnostics.pending_migrations(["pf_x"]))
        with mock.patch.object(diagnostics, "connection") as database:
            database.introspection.table_names.side_effect = OperationalError("down")
            self.assertEqual(diagnostics.find_orphans(), {"error": diagnostics.DATABASE_UNAVAILABLE})


class PluginHealthTests(SimpleTestCase):
    def test_health_exposes_plugin_state_without_failing(self):
        database = mock.MagicMock()
        with mock.patch("itambox.views.utility.connection", database):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"]["plugins"], "ok")

        with mock.patch("itambox.views.utility.connection", database), override_settings(PLUGINS_DISABLED=["old"]):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"]["plugins"], "skipped")


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

    def test_preflight_redacts_secret_setting_from_startup_cause(self):
        self._package(
            "pf_secret",
            """
            from itambox.plugins import PluginConfig, get_plugin_config

            class Cfg(PluginConfig):
                name = "pf_secret"
                description = "fixture"
                version = "2.3.4"
                itambox_range = ">=0.1,<99.0"
                settings_schema = {"token": {"required": True, "secret": True}}

                def ready(self):
                    raise RuntimeError(get_plugin_config(self.name, "token"))

            config = Cfg
            """,
        )
        environ = self._environ(["pf_secret"])
        environ["ITAMBOX_PLUGINS_CONFIG"] = json.dumps({"pf_secret": {"token": "pf-super-secret-token"}})
        result = preflight.run_startup(["pf_secret"], environ=environ)

        self.assertEqual(result["status"], "abort")
        self.assertIn("[redacted]", result["cause"])
        self.assertNotIn("pf-super-secret-token", result["cause"])

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


class PluginsPreflightCommandTests(SimpleTestCase):
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

    def test_table_lists_state_versions_migrations_and_orphans(self):
        inventory = [
            {"plugin": "a", "state": "active", "version": "1.0", "itambox_range": ">=1.0", "pending_migrations": None},
            {"plugin": "b", "state": "skipped", "version": "", "itambox_range": None, "pending_migrations": []},
        ]
        skipped = [
            {"plugin": "b", "failure_class": "PluginCompatibilityError", "stage": "compatibility", "compatibility": "x"}
        ]
        orphans = {"tables": ["gone_widget"], "content_types": [], "migrations": ["gone.0001_initial"]}
        out = io.StringIO()
        with (
            mock.patch.object(plugins_command, "get_plugin_diagnostics", return_value=skipped),
            mock.patch.object(diagnostics, "plugin_inventory", return_value=inventory),
            mock.patch.object(diagnostics, "find_orphans", return_value=orphans),
        ):
            call_command("plugins", stdout=out)
        text = out.getvalue()
        self.assertIn("a  active  1.0  >=1.0  unknown", text)
        self.assertIn("b  skipped  -  -  0  PluginCompatibilityError at compatibility (x)", text)
        self.assertIn("1 tables, 0 content types, 1 migration records", text)
        self.assertIn("  table gone_widget", text)

        out = io.StringIO()
        with (
            mock.patch.object(plugins_command, "get_plugin_diagnostics", return_value=[]),
            mock.patch.object(diagnostics, "plugin_inventory", return_value=[]),
            mock.patch.object(diagnostics, "find_orphans", return_value={"error": "database unavailable"}),
        ):
            call_command("plugins", stdout=out)
        self.assertIn("Orphaned data: database unavailable", out.getvalue())


class PluginsDiagnosticsCommandTests(TestCase):
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
