import importlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.html import escape

from core.templatetags.plugins import plugin_template_content
from itambox.context_processors import plugin_diagnostics_processor
from itambox.plugins import PluginConfig, assembly, runtime
from itambox.plugins.navigation import PluginNavigationItem, PluginNavigationMenu
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

            def __call__(self, request):
                return self.get_response(request)

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


class PluginAssemblyTestCase(SimpleTestCase):
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

    def test_resolve_declared_values(self):
        marker = object()
        self.assertIs(assembly._resolve(types.SimpleNamespace(name="p", menu=marker), "menu"), marker)
        self.assertIs(
            assembly._resolve(types.SimpleNamespace(name="p", menu="os.path"), "menu"),
            importlib.import_module("os.path"),
        )
        self.assertEqual(
            assembly._resolve(types.SimpleNamespace(name="p", graphql_schema="json"), "graphql_schema"),
            importlib.import_module("json"),
        )

    def test_resolve_defaults(self):
        config = types.SimpleNamespace(name="no_such_plugin_pkg")
        self.assertIsNone(assembly._resolve(config, "template_extensions"))
        module = types.SimpleNamespace(template_extensions=["x"])
        with (
            patch("itambox.plugins.assembly.importlib.util.find_spec", return_value=object()),
            patch("itambox.plugins.assembly.importlib.import_module", return_value=module),
        ):
            self.assertEqual(assembly._resolve(config, "template_extensions"), ["x"])
            self.assertIs(assembly._resolve(config, "graphql_schema"), module)
        with patch("itambox.plugins.assembly.importlib.util.find_spec", return_value=None):
            self.assertIsNone(assembly._resolve(config, "menu"))

    def test_as_list_rejects_scalars(self):
        for bad in ("abc", b"abc", 5, {"a": 1}):
            with self.assertRaises(TypeError):
                assembly._as_list(bad, "x")
        self.assertEqual(assembly._as_list((1, 2), "x"), [1, 2])

    def test_compose_navigation(self):
        class Menu(PluginNavigationMenu):
            label = "M"

        class Item(PluginNavigationItem):
            link = "x"

        assembly._compose_navigation(types.SimpleNamespace(name="p", menu=Menu, menu_items=[Item]))
        self.assertIn(Menu, registry.get_plugin_menus())
        self.assertIn(Item, registry.get_plugin_menu_items())
        with self.assertRaises(TypeError):
            assembly._compose_navigation(types.SimpleNamespace(name="p", menu=object, menu_items=None))
        with self.assertRaises(TypeError):
            assembly._compose_navigation(types.SimpleNamespace(name="p", menu=None, menu_items=[object]))

    def test_compose_viewsets(self):
        from itambox.api.viewsets import BaseViewSet

        class ViewSet(BaseViewSet):
            pass

        assembly._compose_viewsets(
            "p", [("things", ViewSet, "p-things"), ("other", "itambox.api.viewsets.BaseViewSet")]
        )
        prefixes = [row[0] for row in registry.get_plugin_viewsets()["p"]]
        self.assertEqual(prefixes, ["things", "other"])
        with self.assertRaises(ValueError):
            assembly._compose_viewsets("p", [("Bad Prefix", ViewSet)])

    def test_compose_plugin_registers_only_active_plugins(self):
        from itambox.api.viewsets import BaseViewSet

        class Query:
            pass

        class Content(PluginTemplateContent):
            models = ["assets.asset"]

        config = types.SimpleNamespace(
            name="p",
            template_extensions=[Content],
            menu=None,
            menu_items=None,
            viewsets=[("things", BaseViewSet)],
            graphql_schema=types.SimpleNamespace(Query=Query),
        )
        with override_settings(PLUGINS_ACTIVE=[]):
            assembly.compose_plugin(config)
        self.assertEqual(registry.get_plugin_graphql(), tuple(self._snapshot["_plugin_graphql"]))
        with override_settings(PLUGINS_ACTIVE=["p"]):
            assembly.compose_plugin(config)
        self.assertIn(("p", Query), registry.get_plugin_graphql())
        self.assertIn(Content, registry.get_plugin_template_contents("assets.asset"))

    def test_assemble_waits_for_last_plugin_then_freezes(self):
        with override_settings(PLUGINS_ACTIVE=["a", "b"]), patch("itambox.plugins.assembly.importlib.import_module"):
            assembly.assemble_after_last_plugin(types.SimpleNamespace(name="a"))
            self.assertFalse(registry._plugins_frozen)
            assembly.assemble_after_last_plugin(types.SimpleNamespace(name="b"))
            self.assertTrue(registry._plugins_frozen)
        with override_settings(PLUGINS_ACTIVE=[]):
            assembly.assemble_after_last_plugin(types.SimpleNamespace(name="a"))

    def test_assemble_failures_name_the_plugin(self):
        registry._register_plugin_graphql("a", object)
        with (
            override_settings(PLUGINS_ACTIVE=["a"]),
            patch("itambox.plugins.assembly._trial_build", side_effect=RuntimeError("bad schema")),
            self.assertRaises(PluginStartupError) as ctx,
        ):
            assembly.assemble_after_last_plugin(types.SimpleNamespace(name="a"))
        self.assertEqual((ctx.exception.plugin_name, ctx.exception.phase), ("a", "assemble"))
        registry._plugin_graphql.clear()
        with (
            override_settings(PLUGINS_ACTIVE=["a"]),
            patch("itambox.plugins.assembly.importlib.import_module", side_effect=ImportError("urls")),
            self.assertRaises(PluginStartupError) as ctx,
        ):
            assembly.assemble_after_last_plugin(types.SimpleNamespace(name="a"))
        self.assertEqual(ctx.exception.phase, "assemble")
        passthrough = PluginStartupError("z", "assemble", RuntimeError("x"))
        with (
            override_settings(PLUGINS_ACTIVE=["a"]),
            patch("itambox.plugins.assembly.importlib.import_module", side_effect=passthrough),
            self.assertRaises(PluginStartupError) as ctx,
        ):
            assembly.assemble_after_last_plugin(types.SimpleNamespace(name="a"))
        self.assertIs(ctx.exception, passthrough)

    def test_trial_build_accepts_a_query_type(self):
        import strawberry

        @strawberry.type
        class Query:
            @strawberry.field
            def ping(self) -> int:
                return 1

        assembly._trial_build(Query)


class PluginRuntimeEdgeTestCase(SimpleTestCase):
    def test_skip_is_recorded_once_and_listed(self):
        settings_module = DummySettings()
        for _ in range(2):
            runtime.record_plugin_skip("p", ValueError("no"), settings_module=settings_module)
        self.assertEqual(settings_module.PLUGINS_DISABLED, ["p"])
        self.assertEqual(len(runtime.get_plugin_diagnostics(settings_module)), 1)
        self.assertFalse(runtime.is_plugin_active("p", settings_module))

    def test_without_settings_nothing_is_reported(self):
        with patch("itambox.plugins.runtime._settings_object", return_value=None):
            self.assertEqual(runtime.get_plugin_diagnostics(), ())
            self.assertFalse(runtime.is_plugin_active("p"))
            runtime.record_plugin_skip("p", ValueError("no"))

    def test_invalid_running_version_is_a_configuration_error(self):
        from itambox.plugins.utils import _release_pair

        with self.assertRaises(ImproperlyConfigured):
            _release_pair("not-a-version")
        self.assertEqual(_release_pair("1"), (1, 0))

    def test_django_apps_must_be_strings(self):
        case = PluginLoaderTestCase()
        case.setUp()
        try:
            with self.assertRaises(PluginStartupError) as ctx:
                case._load("bad_django_apps_plugin", django_apps=[5])
            self.assertEqual(ctx.exception.phase, "resolve")
        finally:
            case.tearDown()


class PluginUrlMountingTestCase(SimpleTestCase):
    def tearDown(self):
        importlib.reload(importlib.import_module("itambox.plugins.urls"))
        importlib.reload(importlib.import_module("core.urls"))

    def test_api_router_mounts_registered_viewsets_and_names_failures(self):
        from itambox.api.viewsets import BaseViewSet

        snapshot = {k: v.copy() for k, v in vars(registry).items() if k.startswith("_plugin") and hasattr(v, "copy")}
        try:
            registry._plugin_viewsets["p"].append(("things", BaseViewSet, "p-things"))
            importlib.reload(importlib.import_module("itambox.plugins.urls"))
            registry._plugin_viewsets["p"].append(("broken", object, None))
            with self.assertRaises(PluginStartupError) as ctx:
                importlib.reload(importlib.import_module("itambox.plugins.urls"))
            self.assertEqual((ctx.exception.plugin_name, ctx.exception.phase), ("p", "assemble"))
        finally:
            for key, value in snapshot.items():
                setattr(registry, key, value)

    def test_ui_urls_are_mounted_skipped_or_fail_with_plugin_name(self):
        package = types.ModuleType("ui_plugin_pkg")
        package.__path__ = []
        urls_module = types.ModuleType("ui_plugin_pkg.urls")
        urls_module.urlpatterns = []
        bare = types.ModuleType("bare_plugin_pkg")
        bare.__path__ = []
        sys.modules.update({"ui_plugin_pkg": package, "ui_plugin_pkg.urls": urls_module, "bare_plugin_pkg": bare})
        config = types.SimpleNamespace(base_url=None)
        try:
            with (
                override_settings(PLUGINS_ACTIVE=["ui_plugin_pkg", "bare_plugin_pkg"]),
                patch("django.apps.apps.get_app_config", return_value=config),
            ):
                module = importlib.reload(importlib.import_module("core.urls"))
            self.assertTrue(any(str(p.pattern) == "plugins/" for p in module.urlpatterns))
            with (
                override_settings(PLUGINS_ACTIVE=["missing_plugin_pkg"]),
                patch("django.apps.apps.get_app_config", return_value=config),
                self.assertRaises(PluginStartupError) as ctx,
            ):
                importlib.reload(importlib.import_module("core.urls"))
            self.assertEqual(ctx.exception.plugin_name, "missing_plugin_pkg")
            real_import = importlib.import_module

            def flaky(name, *args):
                if name == "ui_plugin_pkg.urls":
                    raise RuntimeError("boom")
                return real_import(name, *args)

            with (
                override_settings(PLUGINS_ACTIVE=["ui_plugin_pkg"]),
                patch("django.apps.apps.get_app_config", return_value=config),
                patch("importlib.import_module", side_effect=flaky),
                self.assertRaises(PluginStartupError),
            ):
                importlib.reload(real_import("core.urls"))
        finally:
            for name in ("ui_plugin_pkg", "ui_plugin_pkg.urls", "bare_plugin_pkg"):
                sys.modules.pop(name, None)
