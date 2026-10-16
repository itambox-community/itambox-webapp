import sys
import types
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.utils.html import escape

from core.templatetags.plugins import plugin_template_content
from itambox.context_processors import plugin_diagnostics_processor
from itambox.plugins import PluginConfig, assembly
from itambox.plugins.runtime import PluginStartupError
from itambox.plugins.utils import deep_merge, load_plugins, parse_itambox_range
from itambox.plugins.views import PluginTemplateContent
from itambox.registry import registry


class DummySettings:
    VERSION = "1.1.3"

    def __init__(self):
        self.INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes"]
        self.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
        self.PLUGINS = []
        self.PLUGINS_CONFIG = {}
        self.PLUGINS_RESOLVED_CONFIG = {}
        self.PLUGINS_ACTIVE = []
        self.PLUGINS_DISABLED = []
        self.PLUGINS_DIAGNOSTICS = []


def _config(plugin_name, **attrs):
    return type(
        "MockPluginConfig",
        (PluginConfig,),
        {
            "__module__": plugin_name,
            "name": plugin_name,
            "verbose_name": plugin_name,
            "description": "A test plugin",
            "itambox_range": ">=1.1,<1.2",
            "path": str(Path(__file__).resolve().parent),
            **attrs,
        },
    )


class PluginLoaderTestCase(SimpleTestCase):
    def setUp(self):
        self._names = []

    def tearDown(self):
        for name in self._names:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]

    def _install(self, name, config_cls):
        module = types.ModuleType(name)
        module.config = config_cls
        sys.modules[name] = module
        self._names.append(name)

    def _load(self, pkg, plugin_config=None, **attrs):
        name = pkg
        self._install(name, _config(name, **attrs))
        dummy = DummySettings()
        dummy.PLUGINS = [name]
        dummy.PLUGINS_CONFIG = plugin_config or {}
        load_plugins(dummy)
        return dummy

    def test_deep_merge(self):
        self.assertEqual(
            deep_merge({"a": 1, "b": {"c": 3}}, {"b": {"d": 4}, "e": 5}), {"a": 1, "b": {"c": 3, "d": 4}, "e": 5}
        )

    def test_parse_range(self):
        self.assertEqual(parse_itambox_range("p", ">=1.1,<1.2"), ((1, 1), (1, 2)))
        for bad in (None, "1.1", ">=1.1", ">=1.1.0,<1.2", "<1.2,>=1.1", ">=1.2,<1.1", ">=1.1,<1.1"):
            with self.assertRaises(ImproperlyConfigured, msg=bad):
                parse_itambox_range("p", bad)

    def test_successful_load_merges_settings_and_is_idempotent(self):
        name = "test_mock_plugin"
        mw = types.ModuleType(f"{name}.middleware")

        class MockMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

        mw.MockMiddleware = MockMiddleware
        sys.modules[f"{name}.middleware"] = mw
        dummy = self._load(
            name,
            {name: {"req": "x", "b": "over"}},
            default_settings={"a": "da", "b": "db"},
            required_settings=["req"],
            middleware=[f"{name}.middleware.MockMiddleware"],
            django_apps=[f"{name}.auxiliary"],
        )
        self.assertEqual(dummy.PLUGINS_ACTIVE, [name])
        self.assertIn(f"{name}.auxiliary", dummy.INSTALLED_APPS)
        self.assertEqual(dummy.PLUGINS_RESOLVED_CONFIG[name], {"a": "da", "b": "over", "req": "x"})
        load_plugins(dummy)
        self.assertEqual(dummy.INSTALLED_APPS.count(f"{name}.MockPluginConfig"), 1)
        self.assertEqual(dummy.MIDDLEWARE.count(f"{name}.middleware.MockMiddleware"), 1)

    def test_missing_required_setting_aborts_startup(self):
        with self.assertRaises(PluginStartupError) as ctx:
            self._load("test_mock_missing", required_settings=["crucial"])
        self.assertEqual(ctx.exception.plugin_name, "test_mock_missing")
        self.assertEqual(ctx.exception.phase, "resolve")
        self.assertIn("test_mock_missing", str(ctx.exception))

    def test_missing_package_aborts_startup(self):
        dummy = DummySettings()
        dummy.PLUGINS = ["test_no_such_plugin_pkg"]
        with self.assertRaises(PluginStartupError) as ctx:
            load_plugins(dummy)
        self.assertEqual(ctx.exception.plugin_name, "test_no_such_plugin_pkg")

    def test_missing_config_object_aborts_startup(self):
        name = "test_mock_noconfig"
        sys.modules[name] = types.ModuleType(name)
        self._names.append(name)
        dummy = DummySettings()
        dummy.PLUGINS = [name]
        with self.assertRaises(PluginStartupError):
            load_plugins(dummy)

    def test_config_name_must_match_package(self):
        with self.assertRaises(PluginStartupError):
            self._load("test_mock_name", **{"name": "other_name"})

    def test_description_and_range_are_required(self):
        with self.assertRaises(PluginStartupError):
            self._load("test_mock_nodesc", description="")
        with self.assertRaises(PluginStartupError):
            self._load("test_mock_norange", itambox_range=None)

    def test_malformed_range_aborts_startup(self):
        with self.assertRaises(PluginStartupError):
            self._load("test_mock_badrange", itambox_range="1.1")

    def test_malformed_middleware_aborts_startup(self):
        with self.assertRaises(PluginStartupError):
            self._load("test_mock_badmw", middleware=["test_mock_badmw.missing.Mw"])

    def test_incompatible_version_is_skipped_with_warning(self):
        name = "test_mock_old"
        with self.assertLogs("itambox.plugins.runtime", level="WARNING") as logs:
            dummy = self._load(name, itambox_range=">=1.0,<1.1")
        self.assertEqual(dummy.PLUGINS_ACTIVE, [])
        self.assertEqual(dummy.PLUGINS_DISABLED, [name])
        self.assertNotIn(f"{name}.MockPluginConfig", dummy.INSTALLED_APPS)
        self.assertIn(name, "\n".join(logs.output))
        self.assertIn("1.1.3", "\n".join(logs.output))
        self.assertEqual(dummy.PLUGINS_DIAGNOSTICS[0]["plugin"], name)

    def test_range_accepts_patch_releases_and_rejects_next_minor(self):
        for version, expected in (("1.1.0", ["p_ok"]), ("1.1.99", ["p_ok"]), ("1.2.0", [])):
            with self.subTest(version=version):
                name = "p_ok"
                self._install(name, _config(name))
                dummy = DummySettings()
                dummy.VERSION = version
                dummy.PLUGINS = [name]
                load_plugins(dummy)
                self.assertEqual(dummy.PLUGINS_ACTIVE, expected)

    def test_one_bad_plugin_aborts_even_with_a_good_one(self):
        self._install("test_good", _config("test_good"))
        self._install("test_bad", _config("test_bad", required_settings=["x"]))
        dummy = DummySettings()
        dummy.PLUGINS = ["test_good", "test_bad"]
        with self.assertRaises(PluginStartupError):
            load_plugins(dummy)


class PluginComposeTestCase(SimpleTestCase):
    def setUp(self):
        self._snapshot = {
            key: (value.copy() if hasattr(value, "copy") else value)
            for key, value in vars(registry).items()
            if key.startswith("_plugin")
        }
        registry._plugins_frozen = False

    def tearDown(self):
        for key, value in self._snapshot.items():
            setattr(registry, key, value)

    def _cfg(self, **attrs):
        cfg = _config("compose_plugin", **attrs)
        return cfg.__new__(cfg)

    def test_template_extension_must_subclass_and_name_models(self):
        class NotContent:
            models = ["assets.asset"]

        with self.assertRaises(TypeError):
            assembly._compose_template_extensions("p", [NotContent])

        class NoModels(PluginTemplateContent):
            pass

        with self.assertRaises(TypeError):
            assembly._compose_template_extensions("p", [NoModels])

        class Good(PluginTemplateContent):
            models = ["assets.asset"]

        assembly._compose_template_extensions("p", [Good])
        self.assertIn(Good, registry.get_plugin_template_contents("assets.asset"))

    def test_unknown_model_label_is_rejected(self):
        class Bad(PluginTemplateContent):
            models = ["assets.nosuchmodel"]

        with self.assertRaises(LookupError):
            assembly._compose_template_extensions("p", [Bad])

    def test_graphql_mutation_is_rejected_and_query_required(self):
        with self.assertRaises(TypeError):
            assembly._validate_graphql(types.SimpleNamespace(Query=object, Mutation=object))
        with self.assertRaises(TypeError):
            assembly._validate_graphql(types.SimpleNamespace())
        sentinel = object()
        self.assertIs(assembly._validate_graphql(types.SimpleNamespace(Query=sentinel)), sentinel)

    def test_viewset_prefix_and_base_are_validated(self):
        with self.assertRaises(TypeError):
            assembly._compose_viewsets("p", [("things", object)])

    def test_duplicate_viewset_prefix_is_rejected(self):
        registry._register_plugin_viewset("p", "things", object)
        with self.assertRaises(ValueError):
            registry._register_plugin_viewset("p", "things", object)

    def test_registry_is_frozen_after_assembly(self):
        registry._freeze_plugins()
        with self.assertRaises(RuntimeError):
            registry._register_plugin_menu("p", object)
        with self.assertRaises(RuntimeError):
            registry._register_plugin_graphql("p", object)

    def test_plugins_have_no_public_register_api(self):
        for attr in (
            "register_plugin_template_content",
            "register_plugin_menu",
            "register_plugin_menu_item",
            "register_plugin_viewset",
            "plugin_registration",
        ):
            self.assertFalse(hasattr(registry, attr), attr)

    def test_failing_ready_is_wrapped_with_plugin_name(self):
        class Boom(PluginConfig):
            name = "boom_plugin"
            description = "x"
            itambox_range = ">=1.1,<1.2"
            path = str(Path(__file__).resolve().parent)

            def ready(self):
                raise RuntimeError("kaboom")

        with patch("itambox.plugins.assembly.compose_plugin"), self.assertRaises(PluginStartupError) as ctx:
            Boom.__new__(Boom).ready()
        self.assertEqual(ctx.exception.plugin_name, "boom_plugin")
        self.assertEqual(ctx.exception.phase, "ready")


class TemplateTagTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self._orig_contents = registry._plugin_template_contents.copy()
        self._orig_sources = registry._plugin_template_content_sources.copy()
        registry._plugin_template_contents.clear()
        registry._plugin_template_content_sources.clear()

    def tearDown(self):
        registry._plugin_template_contents = self._orig_contents
        registry._plugin_template_content_sources = self._orig_sources
        super().tearDown()

    def test_template_tag_rendering_and_error_handling(self):
        # The failure is logged with full detail; the page gets nothing from it.
        class GoodContent(PluginTemplateContent):
            def left_panel(self):
                return "<div>Good Injection</div>"

        class BrokenContent(PluginTemplateContent):
            def left_panel(self):
                raise ValueError("Simulated template error")

        registry._register_plugin_template_content("p", "assets.asset", GoodContent)
        registry._register_plugin_template_content("p", "assets.asset", BrokenContent)

        context = {"request": None}
        with self.assertLogs("core.templatetags.plugins", level="ERROR") as logs:
            rendered = plugin_template_content(context, "assets.asset", "left_panel", None)
        self.assertIn("Simulated template error", "\n".join(logs.output))

        self.assertIn("<div>Good Injection</div>", rendered)
        self.assertNotIn("Simulated template error", rendered)
        self.assertNotIn("ValueError", rendered)
        self.assertNotIn("BrokenContent", rendered)
        self.assertNotIn("<!--", rendered)


def _request_for(*, is_superuser=None, anonymous=False):
    user = types.SimpleNamespace(is_authenticated=not anonymous, is_superuser=bool(is_superuser))
    return types.SimpleNamespace(user=user)


class PluginDiagnosticsSurfaceTest(SimpleTestCase):
    def test_diagnostics_hidden_from_non_operators(self):
        diagnostic = {"plugin": "hidden_plugin", "failure_class": "ValueError", "error": "boom"}
        with self.settings(PLUGINS_DIAGNOSTICS=[diagnostic]):
            for request in (None, _request_for(is_superuser=False), _request_for(anonymous=True)):
                context = plugin_diagnostics_processor(request)
                self.assertEqual(context["plugin_diagnostics"], [])
                rendered = render_to_string("global_includes/_plugin_diagnostics.html", context)
                self.assertNotIn("hidden_plugin", rendered)
                self.assertNotIn("plugin-startup-diagnostics", rendered)

    def test_diagnostic_is_visible_and_html_escaped(self):
        diagnostic = {
            "plugin": "unsafe_plugin",
            "failure_class": "ValueError",
            "stage": "ready",
            "compatibility": "compatible",
            "activation_mode": "opt-in",
            "activation_state": "disabled",
            "activation_source": "settings.PLUGINS",
            "value_present": True,
            "error": escape("bad <script>alert('secret')</script>"),
        }
        with self.settings(PLUGINS_DIAGNOSTICS=[diagnostic]):
            context = plugin_diagnostics_processor(_request_for(is_superuser=True))
            rendered = render_to_string("global_includes/_plugin_diagnostics.html", context)

        self.assertIn("unsafe_plugin", rendered)
        self.assertNotIn("plugin-api-experimental-warning", rendered)
        self.assertIn("ValueError", rendered)
        self.assertIn("settings.PLUGINS", rendered)
        self.assertIn("disabled", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
