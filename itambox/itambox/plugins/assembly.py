"""Compose and assemble phases: declared resources become validated, host-owned registrations."""

import importlib
import importlib.util
import re

import strawberry
from django.apps import apps
from django.conf import settings
from django.utils.module_loading import import_string

from itambox.plugins.navigation import PluginNavigationItem, PluginNavigationMenu
from itambox.plugins.runtime import PluginStartupError
from itambox.plugins.views import PluginTemplateContent
from itambox.registry import registry

_LABEL = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_DEFAULTS = {
    "template_extensions": ("template_content", "template_extensions"),
    "menu": ("navigation", "menu"),
    "menu_items": ("navigation", "menu_items"),
    "viewsets": ("api.viewsets", "viewsets"),
    "graphql_schema": ("graphql.schema", None),
}


def _resolve(config, attr):
    """Return the declared resource, else the conventional default if it exists, else None."""
    declared = getattr(config, attr, None)
    if declared is not None:
        if not isinstance(declared, str):
            return declared
        # graphql_schema names a module; every other resource names an attribute.
        return importlib.import_module(declared) if attr == "graphql_schema" else import_string(declared)
    submodule, name = _DEFAULTS[attr]
    module_path = f"{config.name}.{submodule}"
    try:
        found = importlib.util.find_spec(module_path)
    except ModuleNotFoundError:
        found = None
    if found is None:
        return None
    module = importlib.import_module(module_path)
    return module if name is None else getattr(module, name, None)


def _as_list(value, what):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{what} must be a list or tuple")
    return list(value)


def _compose_template_extensions(plugin_name, value):
    for content_class in _as_list(value, "template_extensions"):
        if not (isinstance(content_class, type) and issubclass(content_class, PluginTemplateContent)):
            raise TypeError(f"template extension {content_class!r} must subclass PluginTemplateContent")
        models = getattr(content_class, "models", None)
        if not models or isinstance(models, str):
            raise TypeError(f"{content_class.__name__}.models must be a non-empty list of 'app_label.model' labels")
        for label in models:
            if not isinstance(label, str) or not _LABEL.match(label):
                raise ValueError(f"{content_class.__name__}.models entry {label!r} is not an 'app_label.model' label")
            apps.get_model(label)
            registry._register_plugin_template_content(plugin_name, label, content_class)


def _compose_navigation(config):
    menu = _resolve(config, "menu")
    if menu is not None:
        target = menu if isinstance(menu, type) else type(menu)
        if not issubclass(target, PluginNavigationMenu):
            raise TypeError("menu must be a PluginNavigationMenu")
        registry._register_plugin_menu(config.name, menu)
    items = _resolve(config, "menu_items")
    for item in _as_list(items, "menu_items") if items is not None else ():
        target = item if isinstance(item, type) else type(item)
        if not issubclass(target, PluginNavigationItem):
            raise TypeError(f"menu item {item!r} must be a PluginNavigationItem")
        registry._register_plugin_menu_item(config.name, item)


def _compose_viewsets(plugin_name, value):
    # inline import: app-registry: the API viewset base loads models that need the app registry
    from itambox.api.viewsets import BaseViewSet

    for entry in _as_list(value, "viewsets"):
        prefix, viewset, *rest = entry
        if isinstance(viewset, str):
            viewset = import_string(viewset)
        if not (isinstance(viewset, type) and issubclass(viewset, BaseViewSet)):
            raise TypeError(f"viewset {viewset!r} must derive from the core API viewset base")
        if not isinstance(prefix, str) or not re.match(r"^[a-z0-9][a-z0-9_-]*$", prefix):
            raise ValueError(f"viewset prefix {prefix!r} must be a simple lowercase path segment")
        registry._register_plugin_viewset(plugin_name, prefix, viewset, rest[0] if rest else None)


def _validate_graphql(module):
    if hasattr(module, "Mutation"):
        raise TypeError("plugin GraphQL is queries only; a Mutation contribution is not supported")
    if not hasattr(module, "Query"):
        raise TypeError("graphql schema module must define a Query type")
    return module.Query


def compose_plugin(config):
    """Compose phase: load, validate and register every declared resource of one plugin."""
    if config.name not in getattr(settings, "PLUGINS_ACTIVE", ()):
        return
    template_extensions = _resolve(config, "template_extensions")
    if template_extensions is not None:
        _compose_template_extensions(config.name, template_extensions)
    _compose_navigation(config)
    viewsets = _resolve(config, "viewsets")
    if viewsets is not None:
        _compose_viewsets(config.name, viewsets)
    schema_module = _resolve(config, "graphql_schema")
    if schema_module is not None:
        registry._register_plugin_graphql(config.name, _validate_graphql(schema_module))


def _trial_build(query):
    strawberry.Schema(query=query)


def assemble_after_last_plugin(config):
    """Assemble and freeze once the last active plugin has composed."""
    active = list(getattr(settings, "PLUGINS_ACTIVE", ()))
    if not active or config.name != active[-1]:
        return
    for name, query in registry.get_plugin_graphql():
        try:
            _trial_build(query)
        # broad except: boundary-isolation: plugin code is wrapped with its name and re-raised
        except Exception as exc:
            raise PluginStartupError(name, "assemble", exc) from exc
    try:
        for module_name in ("core.schema", settings.ROOT_URLCONF):
            importlib.import_module(module_name)
    except PluginStartupError:
        raise
    # broad except: boundary-isolation: plugin code is wrapped with its name and re-raised
    except Exception as exc:
        raise PluginStartupError("(plugin set)", "assemble", exc) from exc
    registry._freeze_plugins()
