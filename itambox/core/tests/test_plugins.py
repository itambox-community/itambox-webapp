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
from core.tests.mixins import TenantTestMixin
from itambox.context_processors import plugin_diagnostics_processor
from itambox.dictutils import deep_merge
from itambox.plugins import PluginConfig, assembly, runtime
from itambox.plugins.navigation import PluginNavigationItem, PluginNavigationMenu
from itambox.plugins.runtime import PluginStartupError
from itambox.plugins.settings_schema import override_variable_name
from itambox.plugins.utils import load_plugins, parse_itambox_range
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
            settings_schema={
                "a": {"default": "da"},
                "b": {"default": "db"},
                "req": {"required": True, "secret": True},
            },
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
            self._load("test_mock_missing", settings_schema={"crucial": {"required": True}})
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
        self._install("test_bad", _config("test_bad", settings_schema={"x": {"required": True}}))
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
        from itambox.api.viewsets import BaseViewSet

        with self.assertRaises(TypeError):
            assembly._compose_viewsets("p", [("things", BaseViewSet)])
        with self.assertRaises(TypeError):
            assembly._compose_viewsets("p", [("things", object)])

    def test_viewset_cannot_override_host_security_and_scope_hooks(self):
        from itambox.api.viewsets import ITAMBoxModelViewSet

        def unsafe_override(self, *args, **kwargs):
            return None

        for method_name in (
            "as_view",
            "dispatch",
            "initialize_request",
            "perform_authentication",
            "initial",
            "check_permissions",
            "check_object_permissions",
            "permission_denied",
            "get_object",
            "list",
            "retrieve",
        ):
            with self.subTest(method=method_name):
                viewset = type(
                    f"Unsafe{method_name.title()}ViewSet",
                    (ITAMBoxModelViewSet,),
                    {method_name: unsafe_override},
                )
                with self.assertRaisesRegex(TypeError, method_name):
                    assembly._compose_viewsets("p", [(f"things_{method_name}", viewset)])

        class PermissionDeniedMixin:
            permission_denied = unsafe_override

        class InheritedPermissionDeniedViewSet(PermissionDeniedMixin, ITAMBoxModelViewSet):
            pass

        with self.assertRaisesRegex(TypeError, "permission_denied"):
            assembly._compose_viewsets("p", [("things_inherited", InheritedPermissionDeniedViewSet)])

    def test_permission_targets_validate_and_register_owned_models(self):
        config = types.SimpleNamespace(name="widget_plugin")
        model = types.SimpleNamespace(
            _meta=types.SimpleNamespace(app_config=types.SimpleNamespace(name="widget_plugin.models"))
        )
        with patch("itambox.plugins.assembly.apps.get_model", return_value=model) as get_model:
            assembly._compose_permission_targets(config, ["widget_plugin.widget"])

        get_model.assert_called_once_with("widget_plugin.widget")
        self.assertEqual(
            registry.get_plugin_permission_targets()["widget_plugin"],
            ("widget_plugin.widget",),
        )
        with self.assertRaises(ValueError):
            registry._register_plugin_permission_target("another_plugin", "widget_plugin.widget")

    def test_permission_targets_reject_invalid_labels_duplicates_and_foreign_models(self):
        config = types.SimpleNamespace(name="widget_plugin")
        with self.assertRaises(TypeError):
            assembly._compose_permission_targets(config, "widget_plugin.widget")
        for invalid in (None, "invalid-label"):
            with self.assertRaises(ValueError):
                assembly._compose_permission_targets(config, [invalid])

        model = types.SimpleNamespace(
            _meta=types.SimpleNamespace(app_config=types.SimpleNamespace(name="widget_plugin"))
        )
        with patch("itambox.plugins.assembly.apps.get_model", return_value=model) as get_model:
            with self.assertRaises(ValueError):
                assembly._compose_permission_targets(config, ["widget_plugin.widget", "widget_plugin.widget"])
            get_model.assert_called_once_with("widget_plugin.widget")

        foreign_model = types.SimpleNamespace(
            _meta=types.SimpleNamespace(app_config=types.SimpleNamespace(name="another_plugin"))
        )
        with patch("itambox.plugins.assembly.apps.get_model", return_value=foreign_model):
            with self.assertRaises(ValueError):
                assembly._compose_permission_targets(config, ["widget_plugin.widget"])

    def test_last_plugin_refreshes_early_role_matrix_import_in_place(self):
        import organization.forms.role_form as role_form
        from organization.forms import role_matrix

        matrix_models = role_matrix.MATRIX_MODELS
        original = matrix_models.copy()
        target_key = "plugin_11_late_plugin_latermodel"
        target_row = {
            "label": "Late target",
            "app": "late_plugin",
            "model_name": "latermodel",
            "group": "Late plugin",
        }
        try:
            matrix_models.clear()
            matrix_models.update(role_matrix.ROLE_PERMISSION_PRESENTATION)
            with (
                override_settings(PLUGINS_ACTIVE=["early_plugin", "late_plugin"]),
                patch.object(role_matrix, "_plugin_permission_presentations", return_value={target_key: target_row}),
                patch.object(registry, "get_plugin_graphql", return_value=()),
                patch.object(registry, "_freeze_plugins"),
                patch("itambox.plugins.assembly.importlib.import_module"),
            ):
                assembly.assemble_after_last_plugin(types.SimpleNamespace(name="late_plugin"))

            self.assertIs(role_form.MATRIX_MODELS, matrix_models)
            self.assertEqual(matrix_models[target_key], target_row)
        finally:
            matrix_models.clear()
            matrix_models.update(original)

    def test_technician_seed_scope_uses_declared_plugin_models(self):
        from core.management.commands._seed.access import _technician_plugin_models
        from organization.services.role_permission_policy import RolePermissionTarget

        targets = (
            RolePermissionTarget("asset", "assets", "asset"),
            RolePermissionTarget("plugin_widget", "widget_plugin", "widget", plugin_name="widget_plugin"),
        )
        with patch("core.management.commands._seed.access.active_role_permission_targets", return_value=targets):
            self.assertEqual(_technician_plugin_models(), {("widget_plugin", "widget")})

    def test_host_owned_viewset_enforces_scope_when_static_queryset_method_is_noop(self):
        from typing import cast

        from django.db.models import QuerySet

        from core.managers import Scope, TenantScopingQuerySet
        from itambox.api.viewsets import ITAMBoxModelViewSet
        from itambox.plugins.urls import _host_owned_viewset
        from organization.models import Role

        class NoOpTenantScopeQuerySet(QuerySet):
            def filter_by_tenant(self) -> QuerySet:
                return self

        class UnscopedRoleViewSet(ITAMBoxModelViewSet):
            queryset = NoOpTenantScopeQuerySet(model=Role, using="default")
            serializer_class = object

        host_viewset = _host_owned_viewset("test_plugin", UnscopedRoleViewSet)
        scope = Scope(Scope.TENANT, tenant=types.SimpleNamespace(pk=42, group=None))
        with (
            patch.object(Scope, "current", return_value=scope),
            patch.object(TenantScopingQuerySet, "_resolve_allowed_tenant_ids", return_value=[42]),
        ):
            queryset = cast(QuerySet, host_viewset().get_queryset())

        sql, params = queryset.query.sql_with_params()
        self.assertIn("tenant_id", sql)
        self.assertIn(42, params)

    def test_host_owned_dynamic_queryset_enforces_group_scope_when_method_is_noop(self):
        from typing import cast

        from django.db.models import QuerySet

        from core.managers import Scope, TenantScopingQuerySet
        from itambox.plugins.urls import _HostOwnedViewSetDefaults
        from organization.models import TenantGroup

        class NoOpTenantScopeQuerySet(QuerySet):
            def filter_by_tenant(self) -> QuerySet:
                return self

        class Base:
            def get_queryset(self) -> QuerySet:
                return NoOpTenantScopeQuerySet(model=TenantGroup, using="default")

        class DynamicViewSet(_HostOwnedViewSetDefaults, Base):
            pass

        def _scope_group_rows(queryset, scope, get_descendant_group_ids):
            return queryset.filter(pk=42)

        scope = Scope(Scope.GROUP, group=types.SimpleNamespace(pk=7))
        with (
            patch.object(Scope, "current", return_value=scope),
            patch.object(TenantScopingQuerySet, "_resolve_allowed_tenant_ids", return_value=[]),
            patch.object(TenantScopingQuerySet, "_scope_tenant_group_rows", _scope_group_rows),
        ):
            queryset = cast(QuerySet, DynamicViewSet().get_queryset())

        _sql, params = queryset.query.sql_with_params()
        self.assertIn(42, params)

    def test_host_filter_queryset_reapplies_tenant_scope_after_plugin_override(self):
        from typing import cast

        from django.db.models import QuerySet

        from core.managers import Scope, TenantScopingQuerySet
        from itambox.plugins.urls import _HostOwnedViewSetDefaults
        from organization.models import Role

        class NoOpTenantScopeQuerySet(QuerySet):
            def filter_by_tenant(self) -> QuerySet:
                return self

        class PluginFilter:
            def filter_queryset(self, queryset: QuerySet) -> QuerySet:
                return NoOpTenantScopeQuerySet(model=Role, using=queryset.db)

        class HostViewSet(_HostOwnedViewSetDefaults, PluginFilter):
            pass

        queryset = NoOpTenantScopeQuerySet(model=Role, using="default")
        scope = Scope(Scope.TENANT, tenant=types.SimpleNamespace(pk=42, group=None))
        with (
            patch.object(Scope, "current", return_value=scope),
            patch.object(TenantScopingQuerySet, "_resolve_allowed_tenant_ids", return_value=[42]),
        ):
            result = cast(QuerySet, HostViewSet().filter_queryset(queryset))

        sql, params = result.query.sql_with_params()
        self.assertIn("tenant_id", sql)
        self.assertIn(42, params)

    def test_host_filter_queryset_rejects_plugin_model_swap(self):
        from django.db.models import QuerySet

        from itambox.plugins.urls import _HostOwnedViewSetDefaults
        from organization.models import Role, Tenant

        class PluginFilter:
            def filter_queryset(self, queryset: QuerySet) -> QuerySet:
                return QuerySet(model=Tenant, using=queryset.db)

        class HostViewSet(_HostOwnedViewSetDefaults, PluginFilter):
            pass

        with self.assertRaises(ImproperlyConfigured):
            HostViewSet().filter_queryset(QuerySet(model=Role, using="default"))

    def test_host_owned_schema_generates_openapi_for_plugin_viewset(self):
        from django.urls import path
        from drf_spectacular.generators import SchemaGenerator
        from rest_framework import serializers
        from rest_framework.settings import api_settings

        from itambox.api.viewsets import ITAMBoxModelViewSet
        from itambox.plugins.urls import _host_owned_viewset
        from organization.models import Role

        class RoleSerializer(serializers.ModelSerializer):
            class Meta:
                model = Role
                fields = ("id", "tenant", "name")

        class PluginRoleViewSet(ITAMBoxModelViewSet):
            queryset = Role._base_manager.none()
            serializer_class = RoleSerializer
            schema = None

        viewset = _host_owned_viewset("schema_probe", PluginRoleViewSet)
        view = viewset()
        view.schema = None
        self.assertIsInstance(view.schema, api_settings.DEFAULT_SCHEMA_CLASS)
        self.assertIs(view.schema.view, view)

        patterns = [path("api/plugins/schema_probe/roles/", viewset.as_view({"get": "list"}), name="role-list")]
        schema = SchemaGenerator(title="Plugin API", patterns=patterns).get_schema(public=True)

        self.assertIn("/api/plugins/schema_probe/roles/", schema["paths"])

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
        from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet

        class ViewSet(ITAMBoxModelViewSet):
            pass

        class ReadOnlyViewSet(ITAMBoxReadOnlyModelViewSet):
            pass

        assembly._compose_viewsets(
            "p",
            [("things", ViewSet, "p-things"), ("other", ReadOnlyViewSet, "p-other")],
        )
        prefixes = [row[0] for row in registry.get_plugin_viewsets()["p"]]
        self.assertEqual(prefixes, ["things", "other"])
        with self.assertRaises(TypeError):
            assembly._compose_viewsets("p", [("unsafe", object)])
        with self.assertRaises(ValueError):
            assembly._compose_viewsets("p", [("Bad Prefix", ViewSet)])

    def test_compose_plugin_registers_only_active_plugins(self):
        from itambox.api.viewsets import ITAMBoxModelViewSet

        class Query:
            pass

        class Content(PluginTemplateContent):
            models = ["assets.asset"]

        config = types.SimpleNamespace(
            name="p",
            template_extensions=[Content],
            menu=None,
            menu_items=None,
            viewsets=[("things", ITAMBoxModelViewSet)],
            permission_targets=[],
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
        from itambox.api.viewsets import ITAMBoxModelViewSet

        snapshot = {k: v.copy() for k, v in vars(registry).items() if k.startswith("_plugin") and hasattr(v, "copy")}
        try:
            registry._plugin_viewsets["p"].append(("things", ITAMBoxModelViewSet, "p-things"))
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


class PluginRESTSurfaceTestCase(TenantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self._snapshot = {
            key: (value.copy() if hasattr(value, "copy") else value)
            for key, value in vars(registry).items()
            if key.startswith("_plugin")
        }
        registry._plugins_frozen = False
        from django.db.models import QuerySet
        from django.urls import clear_url_caches
        from rest_framework import serializers
        from rest_framework.filters import BaseFilterBackend

        from itambox.api.viewsets import ITAMBoxModelViewSet
        from organization.models import Role, Tenant

        class RoleSerializer(serializers.ModelSerializer):
            class Meta:
                model = Role
                fields = ("id", "tenant", "name")
                read_only_fields = ("id",)

        class NoOpRoleQuerySet(QuerySet):
            def filter_by_tenant(self) -> QuerySet:
                return self

        class PluginRoleViewSet(ITAMBoxModelViewSet):
            queryset = NoOpRoleQuerySet(model=Role, using="default")
            serializer_class = RoleSerializer
            authentication_classes = ()
            permission_classes = ()
            schema = None

        class PluginRoleFilterOverrideViewSet(PluginRoleViewSet):
            def filter_queryset(self, queryset):
                return NoOpRoleQuerySet(model=Role, using=queryset.db)

        class UnscopedRoleFilterBackend(BaseFilterBackend):
            def filter_queryset(self, request, queryset, view):
                return NoOpRoleQuerySet(model=Role, using=queryset.db)

        class PluginRoleUnscopedBackendViewSet(PluginRoleViewSet):
            filter_backends = (UnscopedRoleFilterBackend,)

        registry._register_plugin_viewset("rest_probe", "roles", PluginRoleViewSet, "rest-probe-roles")
        registry._register_plugin_viewset(
            "rest_probe",
            "roles_filter_override",
            PluginRoleFilterOverrideViewSet,
            "rest-probe-roles-filter-override",
        )
        registry._register_plugin_viewset(
            "rest_probe",
            "roles_unscoped_backend",
            PluginRoleUnscopedBackendViewSet,
            "rest-probe-roles-unscoped-backend",
        )
        importlib.reload(importlib.import_module("itambox.plugins.urls"))
        importlib.reload(importlib.import_module("core.urls"))
        clear_url_caches()
        self.setup_tenant_context(name="Tenant A", slug="tenant-a", permissions=["organization.view_role"])
        self.tenant_role.name = "Tenant A private role"
        self.tenant_role.save(update_fields=["name"])
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.foreign_role = Role.objects.create(
            tenant=self.other_tenant, name="Tenant B private role", permissions=["organization.view_role"]
        )

    def tearDown(self):
        from django.urls import clear_url_caches

        self.clear_tenant_context()
        for key, value in self._snapshot.items():
            setattr(registry, key, value)
        importlib.reload(importlib.import_module("itambox.plugins.urls"))
        importlib.reload(importlib.import_module("core.urls"))
        clear_url_caches()
        super().tearDown()

    def test_plugin_endpoint_is_scoped_and_uses_host_defaults(self):
        from django.urls import resolve
        from rest_framework.settings import api_settings

        from itambox.plugins.urls import _HostOwnedViewSetDefaults
        from organization.models import Role

        self.client_login_to_tenant(self.tenant_user, self.tenant)
        response = self.client.get("/api/plugins/rest_probe/roles/")
        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        names = {row["name"] for row in rows}
        self.assertIn("Tenant A private role", names)
        self.assertNotIn("Tenant B private role", names)
        detail = self.client.get(f"/api/plugins/rest_probe/roles/{self.foreign_role.pk}/")
        self.assertEqual(detail.status_code, 404)

        view_class = resolve("/api/plugins/rest_probe/roles/").func.cls
        self.assertTrue(issubclass(view_class, _HostOwnedViewSetDefaults))
        self.assertEqual(view_class.permission_classes, api_settings.DEFAULT_PERMISSION_CLASSES)
        self.assertIsInstance(view_class().schema, api_settings.DEFAULT_SCHEMA_CLASS)
        self.assertEqual(
            [permission.__class__ for permission in view_class().get_permissions()],
            api_settings.DEFAULT_PERMISSION_CLASSES,
        )
        view = view_class()
        view.schema = None
        self.assertIsInstance(view.schema, api_settings.DEFAULT_SCHEMA_CLASS)
        self.assertEqual(Role._base_manager.filter(tenant=self.other_tenant).count(), 1)

    def test_host_tenant_scope_survives_plugin_filter_overrides(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        for prefix in ("roles_filter_override", "roles_unscoped_backend"):
            with self.subTest(prefix=prefix):
                response = self.client.get(f"/api/plugins/rest_probe/{prefix}/")
                self.assertEqual(response.status_code, 200, response.content.decode())
                payload = response.json()
                rows = payload["results"] if isinstance(payload, dict) else payload
                names = {row["name"] for row in rows}
                self.assertIn("Tenant A private role", names)
                self.assertNotIn("Tenant B private role", names)

                detail = self.client.get(f"/api/plugins/rest_probe/{prefix}/{self.foreign_role.pk}/")
                self.assertEqual(detail.status_code, 404)

    def test_host_token_permission_cannot_be_disabled_by_plugin(self):
        from django.contrib.auth import get_user_model
        from django.urls import resolve
        from rest_framework.settings import api_settings

        from core.tests.mixins import grant
        from organization.models import Role
        from users.models import Token

        user = get_user_model().objects.create_user(username="no_role_reader", password="test")
        denied_role = Role.objects.create(tenant=self.tenant, name="No view role", permissions=[])
        grant(user, self.tenant, denied_role)
        self.client_login_to_tenant(user, self.tenant)
        response = self.client.get("/api/plugins/rest_probe/roles/")
        self.assertEqual(response.status_code, 403)

        token = Token.objects.create(user=user, tenant=self.tenant)
        self.client.logout()
        token_response = self.client.get(
            "/api/plugins/rest_probe/roles/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        self.assertEqual(token_response.status_code, 403)

        allowed_user = get_user_model().objects.create_user(username="token_role_reader", password="test")
        allowed_role = Role.objects.create(
            tenant=self.tenant,
            name="Token view role",
            permissions=["organization.view_role"],
        )
        grant(allowed_user, self.tenant, allowed_role)
        allowed_token = Token.objects.create(user=allowed_user, tenant=self.tenant)
        allowed_response = self.client.get(
            "/api/plugins/rest_probe/roles/",
            HTTP_AUTHORIZATION=f"Token {allowed_token.key}",
        )
        self.assertEqual(allowed_response.status_code, 200, allowed_response.content.decode())

        view_class = resolve("/api/plugins/rest_probe/roles/").func.cls
        self.assertEqual(
            [auth.__class__ for auth in view_class().get_authenticators()],
            api_settings.DEFAULT_AUTHENTICATION_CLASSES,
        )

    def test_plugin_rest_endpoint_is_the_supported_write_surface(self):
        from django.contrib.auth import get_user_model

        from core.tests.mixins import grant
        from organization.models import Role

        user = get_user_model().objects.create_user(username="role_writer", password="test")
        role = Role.objects.create(
            tenant=self.tenant,
            name="Role writer",
            permissions=["organization.add_role"],
        )
        grant(user, self.tenant, role)
        self.client_login_to_tenant(user, self.tenant)

        response = self.client.post(
            "/api/plugins/rest_probe/roles/",
            {"name": "Created through plugin REST", "tenant": self.tenant.pk},
        )

        self.assertEqual(response.status_code, 201, response.content.decode())
        self.assertEqual(
            Role._base_manager.filter(tenant=self.tenant, name="Created through plugin REST").count(),
            1,
        )


class PluginSettingsSchemaTestCase(SimpleTestCase):
    """Operator configuration channel: schema validation, override precedence, secret redaction."""

    NAME = "test_cfg_plugin"
    SCHEMA = {
        "endpoint": {"type": "str", "default": "https://default.example"},
        "retries": {"type": "int", "default": 3},
        "sandbox": {"type": "bool", "default": True},
        "mode": {"type": "str", "choices": ["fast", "safe"], "default": "safe"},
        "api_key": {"type": "str", "required": True, "secret": True},
    }

    def setUp(self):
        self._installed = []

    def tearDown(self):
        for name in self._installed:
            sys.modules.pop(name, None)

    def _resolve(self, plugin_config=None, environ=None, schema=None):
        module = types.ModuleType(self.NAME)
        module.config = _config(self.NAME, settings_schema=self.SCHEMA if schema is None else schema)
        sys.modules[self.NAME] = module
        self._installed.append(self.NAME)
        dummy = DummySettings()
        dummy.PLUGINS = [self.NAME]
        dummy.PLUGINS_CONFIG = plugin_config if plugin_config is not None else {}
        with patch.dict("os.environ", environ or {}, clear=False):
            load_plugins(dummy)
        return dummy

    def test_valid_values_and_defaults(self):
        dummy = self._resolve({self.NAME: {"api_key": "k-123456", "retries": 5}})
        self.assertEqual(
            dummy.PLUGINS_RESOLVED_CONFIG[self.NAME],
            {
                "endpoint": "https://default.example",
                "retries": 5,
                "sandbox": True,
                "mode": "safe",
                "api_key": "k-123456",
            },
        )

    def test_missing_required_key_names_plugin_and_key(self):
        with self.assertRaises(PluginStartupError) as ctx:
            self._resolve({})
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.phase, "resolve")
        self.assertIn(self.NAME, message)
        self.assertIn("'api_key'", message)
        self.assertIn(override_variable_name(self.NAME, "api_key"), message)

    def test_invalid_values_abort_startup_naming_plugin_and_key(self):
        for bad, key in (
            ({"retries": "five"}, "retries"),
            ({"retries": True}, "retries"),
            ({"sandbox": "yes"}, "sandbox"),
            ({"mode": "turbo"}, "mode"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(PluginStartupError) as ctx:
                    self._resolve({self.NAME: {"api_key": "k-123456", **bad}})
                self.assertIn(self.NAME, str(ctx.exception))
                self.assertIn(repr(key), str(ctx.exception))

    def test_unknown_key_is_a_configuration_error(self):
        with self.assertRaises(PluginStartupError) as ctx:
            self._resolve({self.NAME: {"api_key": "k-123456", "retires": 1}})
        self.assertIn("retires", str(ctx.exception))
        self.assertIn(self.NAME, str(ctx.exception))

    def test_entry_must_be_an_object(self):
        with self.assertRaises(PluginStartupError):
            self._resolve({self.NAME: ["api_key"]})

    def test_removed_attributes_are_rejected(self):
        for attribute, value in (("default_settings", {"a": 1}), ("required_settings", ["a"])):
            with self.subTest(attribute=attribute):
                module = types.ModuleType(self.NAME)
                module.config = _config(self.NAME, **{attribute: value})
                sys.modules[self.NAME] = module
                self._installed.append(self.NAME)
                dummy = DummySettings()
                dummy.PLUGINS = [self.NAME]
                with self.assertRaises(PluginStartupError) as ctx:
                    load_plugins(dummy)
                self.assertIn(attribute, str(ctx.exception))

    def test_malformed_schema_aborts_startup(self):
        bad_schemas = (
            {"k": {"type": "decimal"}},
            {"k": {"surprise": 1}},
            {"k": {"default": 1}},
            {"k": {"required": True, "default": "x"}},
            {"k": {"secret": True, "default": "x"}},
            {"k": {"type": "int", "default": "x"}},
            {"k": {"choices": []}},
            {"k": {"type": "list", "choices": [[1]]}},
            {"k": {"choices": ["a"], "default": "b"}},
            {"k": {"choices": [1]}},
            {"k": {"required": "yes"}},
            {"not valid": {}},
            {"k": "str"},
            ["k"],
        )
        for schema in bad_schemas:
            with self.subTest(schema=schema):
                with self.assertRaises(PluginStartupError):
                    self._resolve({}, schema=schema)

    def test_environment_override_beats_json(self):
        variable = override_variable_name(self.NAME, "api_key")
        self.assertEqual(variable, "ITAMBOX_PLUGIN_TEST_CFG_PLUGIN_API_KEY")
        dummy = self._resolve({self.NAME: {"api_key": "from-json-1", "mode": "fast"}}, {variable: "from-env-1"})
        resolved = dummy.PLUGINS_RESOLVED_CONFIG[self.NAME]
        self.assertEqual(resolved["api_key"], "from-env-1")
        self.assertEqual(resolved["mode"], "fast")

    def test_environment_override_can_supply_a_required_key_alone(self):
        variable = override_variable_name(self.NAME, "api_key")
        dummy = self._resolve({}, {variable: "only-env-1"})
        self.assertEqual(dummy.PLUGINS_RESOLVED_CONFIG[self.NAME]["api_key"], "only-env-1")

    def test_environment_values_are_converted_to_the_declared_type(self):
        env = {
            override_variable_name(self.NAME, "api_key"): "k-123456",
            override_variable_name(self.NAME, "retries"): " 7 ",
            override_variable_name(self.NAME, "sandbox"): "Off",
        }
        resolved = self._resolve({}, env).PLUGINS_RESOLVED_CONFIG[self.NAME]
        self.assertEqual((resolved["retries"], resolved["sandbox"]), (7, False))
        schema = {"items": {"type": "list"}, "opts": {"type": "dict"}, "ratio": {"type": "float"}}
        env = {
            override_variable_name(self.NAME, "items"): "[1, 2]",
            override_variable_name(self.NAME, "opts"): '{"a": 1}',
            override_variable_name(self.NAME, "ratio"): "0.5",
        }
        resolved = self._resolve({}, env, schema=schema).PLUGINS_RESOLVED_CONFIG[self.NAME]
        self.assertEqual(resolved, {"items": [1, 2], "opts": {"a": 1}, "ratio": 0.5})

    def test_bad_environment_values_do_not_echo_the_value(self):
        for key, bad in (("retries", "hunter2-secret"), ("sandbox", "hunter2-secret"), ("items", "hunter2-secret")):
            with self.subTest(key=key):
                env = {
                    override_variable_name(self.NAME, "api_key"): "k-123456",
                    override_variable_name(self.NAME, key): bad,
                }
                schema = {**self.SCHEMA, "items": {"type": "list"}}
                with self.assertRaises(PluginStartupError) as ctx:
                    self._resolve({}, env, schema=schema)
                self.assertIn(repr(key), str(ctx.exception))
                self.assertNotIn("hunter2", str(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__.__cause__)

    def test_invalid_value_error_never_contains_the_secret(self):
        schema = {"token": {"type": "int", "secret": True}}
        with self.assertRaises(PluginStartupError) as ctx:
            self._resolve({self.NAME: {"token": "s3cr3t-token"}}, schema=schema)
        self.assertNotIn("s3cr3t-token", str(ctx.exception))

    def test_secret_values_are_redacted_by_flag_not_by_guessing(self):
        dummy = self._resolve({self.NAME: {"api_key": "s3cr3t-token", "endpoint": "https://visible.example"}})
        self.assertEqual(dummy.PLUGINS_SECRET_KEYS[self.NAME], frozenset({"api_key"}))
        error = runtime._redacted_error(RuntimeError("failed calling https://visible.example with s3cr3t-token"), dummy)
        self.assertNotIn("s3cr3t-token", error)
        self.assertIn("https://visible.example", error)
        self.assertIn("[redacted]", error)

    def test_override_name_collision_is_a_resolve_error_by_name_only(self):
        # "a-b" / "c" and "a" / "b_c" both normalise to ITAMBOX_PLUGIN_A_B_C; no variable is set.
        first, second = "cfg_collide_a_b", "cfg_collide_a"
        for name, schema in ((first, {"c": {"type": "str"}}), (second, {"b_c": {"type": "str"}})):
            module = types.ModuleType(name)
            module.config = _config(name, settings_schema=schema)
            sys.modules[name] = module
            self._installed.append(name)
        # same normalised variable: ITAMBOX_PLUGIN_CFG_COLLIDE_A_B_C
        self.assertEqual(override_variable_name(first, "c"), override_variable_name(second, "b_c"))
        dummy = DummySettings()
        dummy.PLUGINS = [first, second]
        with patch.dict("os.environ", {}, clear=False):
            with self.assertRaises(PluginStartupError) as ctx:
                load_plugins(dummy)
        message = str(ctx.exception)
        self.assertEqual((ctx.exception.plugin_name, ctx.exception.phase), (second, "resolve"))
        for expected in (first, second, "'c'", "'b_c'", "ITAMBOX_PLUGIN_CFG_COLLIDE_A_B_C"):
            self.assertIn(expected, message)

    def test_override_name_collision_message_never_contains_a_value(self):
        first, second = "cfg_collide_a_b", "cfg_collide_a"
        for name, schema in ((first, {"c": {"type": "str"}}), (second, {"b_c": {"type": "str"}})):
            module = types.ModuleType(name)
            module.config = _config(name, settings_schema=schema)
            sys.modules[name] = module
            self._installed.append(name)
        dummy = DummySettings()
        dummy.PLUGINS = [first, second]
        env = {"ITAMBOX_PLUGIN_CFG_COLLIDE_A_B_C": "hunter2-secret"}
        with patch.dict("os.environ", env, clear=False):
            with self.assertRaises(PluginStartupError) as ctx:
                load_plugins(dummy)
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_optional_key_without_default_is_absent_from_resolved_config(self):
        schema = {"note": {"type": "str"}, "limit": {"type": "int", "default": 2}}
        resolved = self._resolve({}, schema=schema).PLUGINS_RESOLVED_CONFIG[self.NAME]
        self.assertEqual(resolved, {"limit": 2})
        self.assertNotIn("note", resolved)

    def test_environment_variable_for_an_undeclared_key_is_ignored(self):
        env = {
            override_variable_name(self.NAME, "api_key"): "k-123456",
            override_variable_name(self.NAME, "undeclared"): "whatever",
        }
        resolved = self._resolve({}, env).PLUGINS_RESOLVED_CONFIG[self.NAME]
        self.assertNotIn("undeclared", resolved)
        self.assertEqual(resolved["api_key"], "k-123456")

    def test_config_entry_for_a_plugin_not_in_plugins_is_ignored(self):
        dummy = self._resolve({self.NAME: {"api_key": "k-123456"}, "not_configured_plugin": {"anything": 1}})
        self.assertEqual(set(dummy.PLUGINS_RESOLVED_CONFIG), {self.NAME})

    def test_get_plugin_config_reads_resolved_values(self):
        from itambox.plugins import get_plugin_config

        dummy = self._resolve({self.NAME: {"api_key": "k-123456"}})
        with override_settings(PLUGINS_RESOLVED_CONFIG=dummy.PLUGINS_RESOLVED_CONFIG):
            self.assertEqual(get_plugin_config(self.NAME, "retries"), 3)
            self.assertEqual(get_plugin_config(self.NAME, "absent", "fallback"), "fallback")
            whole = get_plugin_config(self.NAME)
            whole["retries"] = 99
            self.assertEqual(get_plugin_config(self.NAME, "retries"), 3)
            self.assertEqual(get_plugin_config("unknown_plugin"), {})


class PluginsConfigSettingsLoadTestCase(SimpleTestCase):
    """ITAMBOX_PLUGINS_CONFIG is parsed at settings load like the ITAMBOX_TENANT_*_CONFIGS variables."""

    def _load(self, raw):
        from core.settings import base

        with patch.dict("os.environ", {"ITAMBOX_PLUGINS_CONFIG": raw}, clear=False):
            return base._load_tenant_json_config("ITAMBOX_PLUGINS_CONFIG")

    def test_valid_json_object_is_returned(self):
        self.assertEqual(
            self._load('{"example_plugin": {"endpoint": "https://x"}}'), {"example_plugin": {"endpoint": "https://x"}}
        )

    def test_malformed_json_reports_position_only(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._load('{"example_plugin": {"api_key": "s3cr3t-token", }}')
        message = str(ctx.exception)
        self.assertIn("ITAMBOX_PLUGINS_CONFIG", message)
        self.assertIn("line 1, column", message)
        self.assertNotIn("s3cr3t-token", message)

    def test_non_object_top_level_is_rejected(self):
        for raw in ('["s3cr3t-token"]', '"s3cr3t-token"', "42"):
            with self.subTest(raw=raw):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self._load(raw)
                self.assertIn("must contain a JSON object", str(ctx.exception))
                self.assertNotIn("s3cr3t-token", str(ctx.exception))


class PluginSecretRedactionTestCase(SimpleTestCase):
    """Secret-flagged values never reach the startup error, the log or the diagnostics rows."""

    NAME = "test_secret_plugin"
    SCHEMA = {
        "endpoint": {"type": "str", "default": "https://visible.example"},
        "api_key": {"type": "str", "required": True, "secret": True},
        "pin": {"type": "int", "secret": True},
        "ratio": {"type": "float", "secret": True},
        "debug": {"type": "bool", "secret": True},
    }

    def setUp(self):
        self._snapshot = {
            key: (value.copy() if hasattr(value, "copy") else value)
            for key, value in vars(registry).items()
            if key.startswith("_plugin")
        }

    def tearDown(self):
        for key, value in self._snapshot.items():
            setattr(registry, key, value)
        sys.modules.pop(self.NAME, None)

    def test_secret_value_strings_cover_containers_and_bool(self):
        collect = runtime._secret_value_strings
        self.assertEqual(collect({"a": "x", "b": [7, "yz"], "c": {"d": 1.5}}), {"x", "7", "yz", "1.5"})
        self.assertEqual(collect(("t1", ["t2"])), {"t1", "t2"})
        self.assertEqual(collect(True), set())
        self.assertEqual(collect(b"raw"), set())
        self.assertEqual(collect(None), set())

    def _resolve(self, plugin_config, environ=None, **attrs):
        module = types.ModuleType(self.NAME)
        module.config = _config(self.NAME, settings_schema=self.SCHEMA, **attrs)
        sys.modules[self.NAME] = module
        dummy = DummySettings()
        dummy.SECRET_KEY = "django-secret-key-value"
        dummy.PLUGINS = [self.NAME]
        dummy.PLUGINS_CONFIG = plugin_config
        with patch.dict("os.environ", environ or {}, clear=False):
            load_plugins(dummy)
        return dummy

    def _fail_in_ready(self, dummy, message, environ=None):
        """Run the plugin's ready() with a cause that leaks ``message``; return (error, log output)."""
        config_cls = sys.modules[self.NAME].config

        def ready(self):
            raise RuntimeError(message)

        failing = type("FailingConfig", (config_cls,), {"__module__": self.NAME, "ready": ready})
        with (
            override_settings(
                PLUGINS_RESOLVED_CONFIG=dummy.PLUGINS_RESOLVED_CONFIG,
                PLUGINS_SECRET_KEYS=dummy.PLUGINS_SECRET_KEYS,
                SECRET_KEY=dummy.SECRET_KEY,
            ),
            patch.dict("os.environ", environ or {}, clear=False),
            patch("itambox.plugins.assembly.compose_plugin"),
            self.assertLogs("itambox.plugins", level="ERROR") as logs,
            self.assertRaises(PluginStartupError) as ctx,
        ):
            try:
                failing.__new__(failing).ready()
            except PluginStartupError as exc:
                # The startup log of the host records the error message, never the cause.
                runtime.logger.error("%s", exc)
                raise
        return ctx.exception, "\n".join(logs.output)

    def test_json_secret_is_absent_from_error_and_log(self):
        dummy = self._resolve({self.NAME: {"api_key": "json-secret-123"}})
        error, log = self._fail_in_ready(dummy, "call to https://visible.example with json-secret-123 failed")
        for text in (str(error), log):
            self.assertNotIn("json-secret-123", text)
            self.assertIn("https://visible.example", text)
            self.assertIn("[redacted]", text)
        self.assertEqual((error.plugin_name, error.phase), (self.NAME, "ready"))
        self.assertIsInstance(error.cause, RuntimeError)
        self.assertIn("json-secret-123", str(error.cause))
        self.assertIs(error.__cause__, error.cause)

    def test_env_override_secret_is_absent_from_error_and_log(self):
        variable = override_variable_name(self.NAME, "api_key")
        env = {variable: "env-secret-456"}
        dummy = self._resolve({self.NAME: {"api_key": "json-loses-789"}}, env)
        self.assertEqual(dummy.PLUGINS_RESOLVED_CONFIG[self.NAME]["api_key"], "env-secret-456")
        error, log = self._fail_in_ready(dummy, "token env-secret-456 rejected by https://visible.example", env)
        for text in (str(error), log):
            self.assertNotIn("env-secret-456", text)
            self.assertIn("https://visible.example", text)

    def test_raw_env_override_text_of_a_numeric_secret_is_redacted(self):
        env = {override_variable_name(self.NAME, "pin"): " 0042 "}
        dummy = self._resolve({self.NAME: {"api_key": "json-secret-123"}}, env)
        self.assertEqual(dummy.PLUGINS_RESOLVED_CONFIG[self.NAME]["pin"], 42)
        error, _ = self._fail_in_ready(dummy, "bad pin 0042 for https://visible.example", env)
        self.assertNotIn("0042", str(error))
        self.assertIn("https://visible.example", str(error))

    def test_short_and_numeric_secrets_are_redacted(self):
        dummy = self._resolve({self.NAME: {"api_key": "zq", "pin": 9917, "ratio": 0.375, "debug": True}})
        error, log = self._fail_in_ready(dummy, "key=zq pin=9917 ratio=0.375 debug=True at https://visible.example")
        for text in (str(error), log):
            for secret in ("zq", "9917", "0.375"):
                self.assertNotIn(secret, text)
            self.assertIn("debug=True", text)
            self.assertIn("https://visible.example", text)
        diagnostic = runtime._redacted_error(RuntimeError("pin 9917 key zq"), dummy)
        self.assertEqual(diagnostic, "pin [redacted] key [redacted]")

    def test_secret_key_keeps_the_three_character_rule(self):
        dummy = self._resolve({self.NAME: {"api_key": "json-secret-123"}})
        dummy.SECRET_KEY = "ab"
        self.assertEqual(runtime._redact_text("ab cd", dummy), "ab cd")
        dummy.SECRET_KEY = "abc"
        self.assertEqual(runtime._redact_text("abc cd", dummy), "[redacted] cd")

    def test_resolve_phase_error_uses_the_settings_module_being_loaded(self):
        dummy = self._resolve({self.NAME: {"api_key": "json-secret-123"}})
        error = PluginStartupError(self.NAME, "resolve", ValueError("saw json-secret-123"), settings_module=dummy)
        self.assertEqual(str(error), 'Plugin "%s" failed during resolve: ValueError: saw [redacted]' % self.NAME)

    def test_secret_from_json_and_env_is_absent_from_diagnostics_rows(self):
        variable = override_variable_name(self.NAME, "api_key")
        for plugin_config, env, secret in (
            ({self.NAME: {"api_key": "json-secret-123"}}, {}, "json-secret-123"),
            ({}, {variable: "env-secret-456"}, "env-secret-456"),
        ):
            with self.subTest(secret=secret):
                dummy = self._resolve(plugin_config, env)
                with (
                    patch.dict("os.environ", env, clear=False),
                    self.assertLogs("itambox.plugins.runtime", level="WARNING") as logs,
                ):
                    runtime.record_plugin_skip(
                        self.NAME,
                        RuntimeError(f"skipped: {secret} at https://visible.example"),
                        settings_module=dummy,
                    )
                row = dummy.PLUGINS_DIAGNOSTICS[-1]
                self.assertNotIn(secret, repr(row))
                self.assertIn("https://visible.example", row["error"])
                self.assertNotIn(secret, "\n".join(logs.output))
                self.assertIn("https://visible.example", "\n".join(logs.output))
