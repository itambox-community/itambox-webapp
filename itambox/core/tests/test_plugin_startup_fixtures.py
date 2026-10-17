"""Real-startup proof of the plugin failure model (fail fast, single version skip).

Every case boots Django in a subprocess with ``ITAMBOX_PLUGINS`` pointing at a
fixture package from ``plugin_fixtures/``, so the loader runs through the real
settings module and ``django.setup()``, not through mocks. Startup-level only:
no database connection is opened.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from django.test import SimpleTestCase

FIXTURES = Path(__file__).resolve().parent / "plugin_fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

HEALTHY_PROBE = textwrap.dedent(
    """
    import json
    import django
    django.setup()
    from django.conf import settings
    from django.urls import resolve
    from core.schema import build_schema
    from core.management.commands._seed.access import (
        CORE_OPERATIONAL_APPS,
        _technician_plugin_models,
        _technician_permissions,
    )
    from itambox.registry import registry
    from organization.forms.role_matrix import build_matrix_models
    from organization.services.role_permission_policy import active_role_permission_targets

    schema = build_schema(extensions=[])
    plugin_targets = [target for target in active_role_permission_targets() if target.plugin_name == "fixture_plugin"]
    fake_permission = type("Permission", (), {
        "content_type": type("ContentType", (), {"app_label": "fixture_plugin", "model": "fixturewidget"})(),
        "codename": "view_fixturewidget",
    })()
    fake_unrelated_permission = type("Permission", (), {
        "content_type": type("ContentType", (), {"app_label": "fixture_plugin", "model": "unrelatedwidget"})(),
        "codename": "view_unrelatedwidget",
    })()
    matrix = build_matrix_models()
    technician_permissions = _technician_permissions(
        [fake_permission, fake_unrelated_permission], CORE_OPERATIONAL_APPS, _technician_plugin_models()
    )
    result = {
        "active": list(settings.PLUGINS_ACTIVE),
        "disabled": list(settings.PLUGINS_DISABLED),
        "template_models": [m for m in ("assets.manufacturer",) if registry.get_plugin_template_contents(m)],
        "menus": [m.label for m in registry.get_plugin_menus()],
        "menu_items": [i.link_text for i in registry.get_plugin_menu_items()],
        "viewsets": {k: [p for p, _, _ in v] for k, v in registry.get_plugin_viewsets().items()},
        "permission_targets": [target.key for target in plugin_targets],
        "matrix_target": matrix.get("plugin_14_fixture_plugin_fixturewidget"),
        "seeded_target_permissions": technician_permissions,
        "graphql_fields": "fixturePluginPing" in schema.as_str(),
        "ui_route": resolve("/plugins/fixture/").view_name,
        "api_route": resolve("/api/plugins/fixture_plugin/manufacturers/").view_name,
        "api_actions": resolve("/api/plugins/fixture_plugin/manufacturers/").func.actions,
    }
    print("PROBE" + json.dumps(result))
    """
)

SKIP_PROBE = textwrap.dedent(
    """
    import json
    import sys
    import django
    django.setup()
    from django.apps import apps
    from django.conf import settings
    from django.db.models.signals import post_migrate
    from django.db.migrations.loader import MigrationLoader

    print("PROBE" + json.dumps({
        "active": list(settings.PLUGINS_ACTIVE),
        "disabled": list(settings.PLUGINS_DISABLED),
        "installed": [a for a in settings.INSTALLED_APPS if a.startswith("fxskip")],
        "app_labels": [c.label for c in apps.get_app_configs() if c.label.startswith("fxskip")],
        "models_imported": "fxskip_range.models" in sys.modules,
        "signals_imported": "fxskip_range.signals" in sys.modules,
        "package_has_migrations": "fxskip_range.migrations" in sys.modules,
        "diagnostics": [d["plugin"] for d in settings.PLUGINS_DIAGNOSTICS],
    }))
    """
)

BARE_SETUP = "import django\ndjango.setup()\n"


def boot(plugins, probe=BARE_SETUP):
    """Run ``django.setup()`` with the given plugins in a fresh interpreter."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ITAMBOX_PLUGINS")}
    env.update(
        ITAMBOX_ENV="dev",
        ITAMBOX_PLUGINS=plugins,
        DJANGO_SETTINGS_MODULE="core.settings",
        PYTHONPATH=os.pathsep.join([str(FIXTURES), str(PROJECT_ROOT)]),
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(  # noqa: S603 - fixed argv, test-only interpreter
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def probe_payload(completed):
    for line in completed.stdout.splitlines():
        if line.startswith("PROBE"):
            return json.loads(line[len("PROBE") :])
    raise AssertionError("no probe output\nstdout:\n%s\nstderr:\n%s" % (completed.stdout, completed.stderr))


class PluginStartupFixtureTests(SimpleTestCase):
    def test_fixture_plugin_loads_through_django_setup(self):
        completed = boot("fixture_plugin", HEALTHY_PROBE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = probe_payload(completed)
        self.assertEqual(payload["active"], ["fixture_plugin"])
        self.assertEqual(payload["disabled"], [])
        self.assertEqual(payload["template_models"], ["assets.manufacturer"])
        self.assertEqual(payload["menus"], ["Fixture menu"])
        self.assertEqual(payload["menu_items"], ["Fixture standalone entry"])
        self.assertEqual(payload["viewsets"], {"fixture_plugin": ["manufacturers"]})
        self.assertIs(payload["graphql_fields"], True)
        self.assertEqual(payload["ui_route"], "plugins:fixture_plugin:index")
        self.assertTrue(payload["api_route"].startswith("plugins-api:"), payload["api_route"])
        self.assertEqual(payload["permission_targets"], ["plugin_14_fixture_plugin_fixturewidget"])
        self.assertEqual(payload["matrix_target"]["app"], "fixture_plugin")
        self.assertEqual(payload["matrix_target"]["model_name"], "fixturewidget")
        self.assertEqual(payload["matrix_target"]["group"], "Fixture plugin")
        self.assertIn("fixture_plugin.view_fixturewidget", payload["seeded_target_permissions"])
        self.assertNotIn("fixture_plugin.view_unrelatedwidget", payload["seeded_target_permissions"])
        self.assertEqual(payload["api_actions"].get("post"), "create")

    def assert_aborts(self, plugin, phase, detail=None):
        completed = boot(plugin)
        self.assertNotEqual(completed.returncode, 0, "startup should have aborted:\n" + completed.stdout)
        self.assertIn("PluginStartupError", completed.stderr)
        self.assertIn('Plugin "%s" failed during %s' % (plugin, phase), completed.stderr)
        if detail:
            self.assertIn(detail, completed.stderr)

    def test_missing_package_aborts_in_resolve(self):
        self.assert_aborts("fxbad_not_installed", "resolve", "Failed to import plugin")

    def test_import_failure_aborts_in_resolve(self):
        self.assert_aborts("fxbad_import", "resolve", "itambox_fixture_dependency_that_does_not_exist")

    def test_malformed_version_range_aborts_in_resolve(self):
        self.assert_aborts("fxbad_range_malformed", "resolve", "itambox_range")

    def test_missing_version_range_aborts_in_resolve(self):
        self.assert_aborts("fxbad_range_missing", "resolve", "itambox_range")

    def test_invalid_configuration_aborts_in_resolve(self):
        self.assert_aborts("fxbad_config", "resolve", "api_token")

    def test_compose_failure_aborts_in_compose(self):
        self.assert_aborts("fxbad_compose", "compose", "template_extensions")

    def test_failing_ready_aborts_in_ready(self):
        self.assert_aborts("fxbad_ready", "ready", "fixture ready() failure")

    def test_invalid_graphql_type_aborts_in_assemble(self):
        self.assert_aborts("fxbad_graphql", "assemble", "UnresolvedFieldTypeError")

    def test_unmountable_viewset_aborts_in_assemble(self):
        self.assert_aborts("fxbad_assemble", "assemble")

    def test_urlconf_failure_aborts_at_start_not_on_first_request(self):
        self.assert_aborts("fxbad_urlconf", "assemble", "views_that_do_not_exist")

    def test_out_of_range_plugin_is_skipped_and_nothing_is_loaded(self):
        completed = boot("fxskip_range", SKIP_PROBE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = probe_payload(completed)
        self.assertEqual(payload["active"], [])
        self.assertEqual(payload["disabled"], ["fxskip_range"])
        self.assertEqual(payload["diagnostics"], ["fxskip_range"])
        self.assertEqual(payload["installed"], [])
        self.assertEqual(payload["app_labels"], [])
        self.assertFalse(payload["models_imported"])
        self.assertFalse(payload["signals_imported"])
        self.assertFalse(payload["package_has_migrations"])
        self.assertIn("supports ITAMbox", completed.stderr)
