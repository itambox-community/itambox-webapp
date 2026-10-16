import importlib
import runpy
import sys
import types
from pathlib import Path
from unittest.mock import patch

import strawberry
from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.html import escape

from core.templatetags.plugins import plugin_template_content
from itambox.context_processors import plugin_diagnostics_processor
from itambox.plugins import PluginConfig
from itambox.plugins.utils import deep_merge, is_plugin_active, load_plugins
from itambox.plugins.views import PluginTemplateContent
from itambox.registry import registry


class DummySettings:
    def __init__(self):
        self.INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes"]
        self.MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
        self.PLUGINS = []
        self.PLUGINS_CONFIG = {}
        self.PLUGINS_RESOLVED_CONFIG = {}
        self.PLUGINS_ACTIVE = []
        self.PLUGINS_DIAGNOSTICS = []


def _plugin_module(name, config_cls):
    module = types.ModuleType(name)
    module.config = config_cls
    sys.modules[name] = module
    return module


def _api_compatible_config(plugin_name, **attrs):
    return type(
        "MockPluginConfig",
        (PluginConfig,),
        {
            "__module__": plugin_name,
            "name": plugin_name,
            "verbose_name": plugin_name,
            "path": str(Path(__file__).resolve().parent),
            "min_plugin_api_version": "1.0",
            "max_plugin_api_version": "1.0",
            **attrs,
        },
    )


def _raise_for_plugin_schema_import(name, *args, **kwargs):
    """Fail only the plugin schema module import under test.

    The core schema resolves cross-module GraphQL types lazily at build time, so
    a blanket ``importlib.import_module`` failure would also break the core
    schema instead of isolating the plugin failure.
    """
    if name == "test_mock_plugin.graphql":
        raise RuntimeError("schema secret")
    return _ORIGINAL_IMPORT_MODULE(name, *args, **kwargs)


_ORIGINAL_IMPORT_MODULE = importlib.import_module


class PluginLoaderTestCase(SimpleTestCase):
    def test_deep_merge(self):
        dict1 = {"a": 1, "b": {"c": 3}}
        dict2 = {"b": {"d": 4}, "e": 5}
        expected = {"a": 1, "b": {"c": 3, "d": 4}, "e": 5}
        self.assertEqual(deep_merge(dict1, dict2), expected)

    def test_successful_loader_registration(self):
        dummy_name = "test_mock_plugin"
        MockPluginConfig = _api_compatible_config(
            dummy_name,
            verbose_name="Mock Plugin",
            default_settings={"setting_a": "default_a", "setting_b": "default_b"},
            required_settings=["setting_req"],
            middleware=["test_mock_plugin.middleware.MockMiddleware"],
            django_apps=["test_mock_plugin.auxiliary"],
        )
        _plugin_module(dummy_name, MockPluginConfig)
        middleware_module = types.ModuleType("test_mock_plugin.middleware")

        class MockMiddleware:
            __slots__ = ("get_response",)

            def __init__(self, get_response):
                self.get_response = get_response

        middleware_module.MockMiddleware = MockMiddleware
        sys.modules["test_mock_plugin.middleware"] = middleware_module

        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]
        dummy_settings.PLUGINS_CONFIG = {dummy_name: {"setting_req": "val_req", "setting_b": "val_b_overridden"}}

        load_plugins(dummy_settings)

        self.assertIn(f"{dummy_name}.{MockPluginConfig.__name__}", dummy_settings.INSTALLED_APPS)
        self.assertIn("test_mock_plugin.auxiliary", dummy_settings.INSTALLED_APPS)
        self.assertIn("test_mock_plugin.middleware.MockMiddleware", dummy_settings.MIDDLEWARE)
        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [dummy_name])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS, [])

        load_plugins(dummy_settings)
        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [dummy_name])
        self.assertEqual(dummy_settings.INSTALLED_APPS.count(f"{dummy_name}.{MockPluginConfig.__name__}"), 1)
        self.assertEqual(dummy_settings.MIDDLEWARE.count("test_mock_plugin.middleware.MockMiddleware"), 1)

        resolved = dummy_settings.PLUGINS_RESOLVED_CONFIG[dummy_name]
        self.assertEqual(resolved["setting_a"], "default_a")
        self.assertEqual(resolved["setting_b"], "val_b_overridden")
        self.assertEqual(resolved["setting_req"], "val_req")

        if dummy_name in sys.modules:
            del sys.modules[dummy_name]
        sys.modules.pop("test_mock_plugin.middleware", None)

    def test_improperly_configured_missing_required(self):
        dummy_name = "test_mock_plugin_missing"
        MockPluginConfig = _api_compatible_config(
            dummy_name,
            verbose_name="Mock Plugin Missing",
            required_settings=["crucial_setting"],
        )
        _plugin_module(dummy_name, MockPluginConfig)

        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]
        dummy_settings.PLUGINS_CONFIG = {}

        load_plugins(dummy_settings)

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        diagnostic = dummy_settings.PLUGINS_DIAGNOSTICS[0]
        self.assertEqual(diagnostic["plugin"], dummy_name)
        self.assertEqual(diagnostic["activation_state"], "disabled")
        self.assertEqual(diagnostic["source"], "settings.PLUGINS")
        self.assertEqual(diagnostic["failure_class"], "ImproperlyConfigured")

        if dummy_name in sys.modules:
            del sys.modules[dummy_name]

    def test_missing_plugin_config_object_is_disabled(self):
        dummy_name = "test_mock_plugin_missing_config"
        sys.modules[dummy_name] = types.ModuleType(dummy_name)
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        diagnostic = dummy_settings.PLUGINS_DIAGNOSTICS[0]
        self.assertEqual(diagnostic["failure_class"], "ImproperlyConfigured")
        self.assertEqual(diagnostic["stage"], "configuration")
        del sys.modules[dummy_name]

    def test_import_failure_is_disabled_without_aborting_startup(self):
        dummy_name = "test_mock_plugin_not_importable"
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)
        load_plugins(dummy_settings)

        diagnostic = dummy_settings.PLUGINS_DIAGNOSTICS[0]
        self.assertEqual(diagnostic["failure_class"], "ModuleNotFoundError")
        self.assertEqual(diagnostic["stage"], "import")
        self.assertEqual(len(dummy_settings.PLUGINS_DIAGNOSTICS), 1)

    def test_active_settings_fallback_does_not_activate_an_unconfigured_plugin(self):
        dummy_name = "test_mock_plugin_registry_transition"
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS_ACTIVE = [dummy_name]

        with patch("itambox.plugins.runtime.apps.get_app_config", side_effect=LookupError):
            self.assertTrue(is_plugin_active(dummy_name, dummy_settings))
            self.assertFalse(is_plugin_active("test_mock_plugin_not_active", dummy_settings))

    def test_rest_router_failure_isolated_from_plugin_router_startup(self):
        dummy_name = "test_mock_plugin_router_failure"
        module_path = Path(__file__).resolve().parents[2] / "itambox" / "plugins" / "urls.py"

        with (
            patch("itambox.plugins.utils.is_plugin_active", return_value=True),
            patch("itambox.plugins.utils.record_plugin_failure") as record_failure,
            patch.object(
                registry,
                "get_plugin_viewsets",
                return_value={dummy_name: [("broken", object, "broken")]},
            ),
            patch("rest_framework.routers.DefaultRouter.register", side_effect=RuntimeError("router secret")),
        ):
            runpy.run_path(str(module_path), run_name="issue99_plugin_urls")

        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.kwargs["stage"], "api")

    def test_graphql_schema_failure_isolated_from_core_schema_startup(self):
        dummy_name = "test_mock_plugin_graphql_failure"
        module_path = Path(__file__).resolve().parents[1] / "schema.py"

        with (
            patch("itambox.plugins.utils.is_plugin_active", return_value=True),
            patch("itambox.plugins.utils.record_plugin_failure") as record_failure,
            patch(
                "django.apps.apps.get_app_config",
                return_value=types.SimpleNamespace(graphql_schema="test_mock_plugin.graphql"),
            ),
            patch("importlib.import_module", side_effect=_raise_for_plugin_schema_import),
            self.settings(PLUGINS=[dummy_name]),
        ):
            namespace = runpy.run_path(str(module_path), run_name="issue99_core_schema")

        self.assertIn("schema", namespace)
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[0], dummy_name)
        self.assertEqual(record_failure.call_args.kwargs["stage"], "graphql")

    def test_graphql_schema_adds_plugin_query_and_mutation_types(self):
        dummy_name = "test_mock_plugin_graphql_success"
        module_path = Path(__file__).resolve().parents[1] / "schema.py"

        @strawberry.type
        class PluginQuery:
            plugin_value: str = "available"

        @strawberry.type
        class PluginMutation:
            @strawberry.mutation
            def update_plugin_value(self) -> bool:
                return True

        plugin_schema = types.SimpleNamespace(Query=PluginQuery, Mutation=PluginMutation)

        def import_plugin_schema(name, *args, **kwargs):
            if name == "test_mock_plugin.graphql":
                return plugin_schema
            return _ORIGINAL_IMPORT_MODULE(name, *args, **kwargs)

        with (
            self.settings(PLUGINS=[dummy_name]),
            patch("itambox.plugins.utils.is_plugin_active", return_value=True),
            patch("itambox.plugins.utils.record_plugin_failure") as record_failure,
            patch(
                "django.apps.apps.get_app_config",
                return_value=types.SimpleNamespace(graphql_schema="test_mock_plugin.graphql"),
            ),
            patch("importlib.import_module", side_effect=import_plugin_schema),
        ):
            namespace = runpy.run_path(str(module_path), run_name="issue99_core_schema_with_plugin")

        self.assertIn(PluginQuery, namespace["query_bases"])
        self.assertIn(PluginMutation, namespace["mutation_bases"])
        self.assertIsNotNone(namespace["Mutation"])
        record_failure.assert_not_called()

    def test_plugin_urlconf_failure_isolated_from_core_url_startup(self):
        dummy_name = "test_mock_plugin_urlconf_failure"
        module_path = Path(__file__).resolve().parents[1] / "urls.py"

        with (
            self.settings(PLUGINS=[dummy_name], DEBUG=False),
            patch("itambox.plugins.utils.is_plugin_active", return_value=True),
            patch("itambox.plugins.utils.record_plugin_failure") as record_failure,
            patch(
                "django.apps.apps.get_app_config",
                return_value=types.SimpleNamespace(base_url=dummy_name),
            ),
            patch("importlib.import_module", side_effect=RuntimeError("urlconf secret")),
        ):
            runpy.run_path(str(module_path), run_name="issue99_core_urls")

        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[0], dummy_name)
        self.assertEqual(record_failure.call_args.kwargs["stage"], "urls")

    def test_missing_optional_plugin_urlconf_is_not_reported_as_a_failure(self):
        dummy_name = "test_mock_plugin_without_urlconf"
        module_path = Path(__file__).resolve().parents[1] / "urls.py"

        with (
            self.settings(PLUGINS=[dummy_name], DEBUG=False),
            patch("itambox.plugins.utils.is_plugin_active", return_value=True),
            patch("itambox.plugins.utils.record_plugin_failure") as record_failure,
            patch(
                "django.apps.apps.get_app_config",
                return_value=types.SimpleNamespace(base_url=dummy_name),
            ),
            patch("importlib.import_module", side_effect=ModuleNotFoundError(name=f"{dummy_name}.urls")),
        ):
            runpy.run_path(str(module_path), run_name="issue99_core_urls_without_optional_conf")

        record_failure.assert_not_called()

    def test_nested_module_not_found_is_not_treated_as_optional_absence(self):
        # A ModuleNotFoundError for anything other than the exact optional
        # "<plugin>.urls" module (e.g. a missing dependency inside an existing
        # urls.py) must be recorded as a plugin failure, never swallowed as
        # "optional URLconf absent".
        dummy_name = "test_mock_plugin_nested_import_error"
        module_path = Path(__file__).resolve().parents[1] / "urls.py"

        with (
            self.settings(PLUGINS=[dummy_name], DEBUG=False),
            patch("itambox.plugins.utils.is_plugin_active", return_value=True),
            patch("itambox.plugins.utils.record_plugin_failure") as record_failure,
            patch(
                "django.apps.apps.get_app_config",
                return_value=types.SimpleNamespace(base_url=dummy_name),
            ),
            patch("importlib.import_module", side_effect=ModuleNotFoundError(name="missing.dependency")),
        ):
            runpy.run_path(str(module_path), run_name="issue99_core_urls_nested_import_error")

        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[0], dummy_name)
        self.assertEqual(record_failure.call_args.kwargs["stage"], "urls")

    def test_record_plugin_failure_redacts_configured_secrets_from_diagnostic(self):
        # The failure diagnostic for the new api/graphql/urls stages must not
        # carry configured secret values even when the exception text contains
        # them verbatim.
        from itambox.plugins.runtime import record_plugin_failure

        dummy_name = "test_mock_plugin_secret_redaction"
        dummy = DummySettings()
        dummy.PLUGINS_CONFIG = {dummy_name: {"api_token": "router secret value"}}
        with patch("itambox.plugins.runtime._settings_object", return_value=dummy):
            record_plugin_failure(dummy_name, RuntimeError("router secret value"), stage="api")

        diagnostic = dummy.PLUGINS_DIAGNOSTICS[0]
        self.assertEqual(diagnostic["plugin"], dummy_name)
        self.assertEqual(diagnostic["stage"], "api")
        self.assertNotIn("router secret value", str(diagnostic))

    def test_loader_min_version_compatible(self):
        dummy_name = "test_mock_plugin_min_ok"
        MockPluginConfig = _api_compatible_config(
            dummy_name,
            verbose_name="Mock Plugin Min OK",
            min_version="1.0.0-alpha",
        )
        _plugin_module(dummy_name, MockPluginConfig)

        dummy_settings = DummySettings()
        dummy_settings.VERSION = "1.0.0-alpha"
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)
        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [dummy_name])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS, [])

        if dummy_name in sys.modules:
            del sys.modules[dummy_name]

    def test_loader_min_version_incompatible(self):
        dummy_name = "test_mock_plugin_min_fail"
        MockPluginConfig = _api_compatible_config(
            dummy_name,
            verbose_name="Mock Plugin Min Fail",
            min_version="1.1.0",
        )
        _plugin_module(dummy_name, MockPluginConfig)

        dummy_settings = DummySettings()
        dummy_settings.VERSION = "1.0.0-alpha"
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)
        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS[0]["compatibility"], "incompatible-product")

        if dummy_name in sys.modules:
            del sys.modules[dummy_name]

    def test_loader_max_version_incompatible(self):
        dummy_name = "test_mock_plugin_max_fail"
        config_cls = _api_compatible_config(dummy_name, max_version="0.9.0")
        _plugin_module(dummy_name, config_cls)
        dummy_settings = DummySettings()
        dummy_settings.VERSION = "1.0.0-alpha"
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS[0]["compatibility"], "incompatible-product")
        del sys.modules[dummy_name]

    def test_missing_plugin_api_metadata_is_disabled_without_guessing(self):
        dummy_name = "test_mock_plugin_no_api_metadata"
        config_cls = type("MockPluginConfig", (PluginConfig,), {"name": dummy_name})
        _plugin_module(dummy_name, config_cls)
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        diagnostic = dummy_settings.PLUGINS_DIAGNOSTICS[0]
        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        self.assertEqual(diagnostic["compatibility"], "missing-plugin-api")
        self.assertTrue(diagnostic["value_present"])
        del sys.modules[dummy_name]

    def test_plugin_config_name_must_match_package(self):
        dummy_name = "test_mock_plugin_wrong_name"
        config_cls = _api_compatible_config(dummy_name, name="other_plugin")
        _plugin_module(dummy_name, config_cls)
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS[0]["failure_class"], "ImproperlyConfigured")
        del sys.modules[dummy_name]

    def test_plugin_api_version_is_independent_from_product_version(self):
        dummy_name = "test_mock_plugin_api_version"
        config_cls = _api_compatible_config(
            dummy_name,
            min_version="9.0.0",
            max_version="9.0.0",
        )
        _plugin_module(dummy_name, config_cls)
        dummy_settings = DummySettings()
        dummy_settings.VERSION = "9.0.0"
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [dummy_name])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS, [])
        del sys.modules[dummy_name]

    def test_incompatible_plugin_api_version_is_disabled(self):
        dummy_name = "test_mock_plugin_api_version_fail"
        config_cls = _api_compatible_config(
            dummy_name,
            min_plugin_api_version="2.0",
            max_plugin_api_version="2.0",
        )
        _plugin_module(dummy_name, config_cls)
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS[0]["compatibility"], "incompatible-plugin-api")
        del sys.modules[dummy_name]

    def test_one_failed_plugin_does_not_disable_another(self):
        good_name = "test_mock_plugin_good"
        bad_name = "test_mock_plugin_bad"
        good_config = _api_compatible_config(good_name)

        class BadMenu:
            pass

        def broken_ready(self):
            registry.register_plugin_menu(BadMenu)
            registry.register_plugin_viewset(self.name, "broken", object)
            raise ValueError("bad <script>alert('secret-value')</script>")

        bad_middleware_module = types.ModuleType("test_mock_plugin_bad.middleware")

        class BadMiddleware:
            __slots__ = ("get_response",)

            def __init__(self, get_response):
                self.get_response = get_response

        bad_middleware_module.BadMiddleware = BadMiddleware
        sys.modules["test_mock_plugin_bad.middleware"] = bad_middleware_module
        bad_config = _api_compatible_config(
            bad_name,
            ready=broken_ready,
            middleware=["test_mock_plugin_bad.middleware.BadMiddleware"],
        )
        _plugin_module(good_name, good_config)
        _plugin_module(bad_name, bad_config)
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [good_name, bad_name]
        dummy_settings.PLUGINS_CONFIG = {bad_name: {"secret": "secret-value"}}

        load_plugins(dummy_settings)
        bad_instance = bad_config(bad_name, sys.modules[bad_name])
        bad_instance.ready()

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [good_name])
        diagnostic = dummy_settings.PLUGINS_DIAGNOSTICS[0]
        self.assertEqual(diagnostic["plugin"], bad_name)
        self.assertEqual(diagnostic["failure_class"], "ValueError")
        self.assertNotIn("secret-value", diagnostic["error"])
        self.assertNotIn("test_mock_plugin_bad.middleware.BadMiddleware", dummy_settings.MIDDLEWARE)
        self.assertNotIn(BadMenu, registry.get_plugin_menus())
        self.assertNotIn(bad_name, registry.get_plugin_viewsets())
        del sys.modules[good_name]
        del sys.modules[bad_name]
        del sys.modules["test_mock_plugin_bad.middleware"]

    def test_malformed_middleware_is_disabled(self):
        dummy_name = "test_mock_plugin_bad_middleware"
        config_cls = _api_compatible_config(dummy_name, middleware=[object()])
        _plugin_module(dummy_name, config_cls)
        dummy_settings = DummySettings()
        dummy_settings.PLUGINS = [dummy_name]

        load_plugins(dummy_settings)

        self.assertEqual(dummy_settings.PLUGINS_ACTIVE, [])
        self.assertEqual(dummy_settings.PLUGINS_DIAGNOSTICS[0]["stage"], "middleware")
        del sys.modules[dummy_name]


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

        registry.register_plugin_template_content("assets.asset", GoodContent)
        registry.register_plugin_template_content("assets.asset", BrokenContent)

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
