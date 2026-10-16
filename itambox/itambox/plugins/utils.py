import importlib
import re
from collections.abc import Sequence

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from packaging.version import InvalidVersion
from packaging.version import parse as parse_version

from itambox.dictutils import deep_merge
from itambox.plugins import PluginConfig
from itambox.plugins.runtime import (
    PluginStartupError,
    get_plugin_diagnostics,
    is_plugin_active,
    record_plugin_skip,
)


class PluginCompatibilityError(Exception):
    """The running ITAMbox version is outside the range a plugin declares (the single skip case)."""


_RANGE = re.compile(r"^\s*>=\s*(\d+)\.(\d+)\s*,\s*<\s*(\d+)\.(\d+)\s*$")


def parse_itambox_range(plugin_name, value):
    """Parse ``>=X.Y,<X.Y`` into ((lo_major, lo_minor), (hi_major, hi_minor)); malformed is a configuration error."""
    match = _RANGE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ImproperlyConfigured(
            f"Plugin {plugin_name!r} must declare itambox_range as '>=<major.minor>,<<next-major.minor>>' "
            f"(for example '>=1.1,<1.2'), got {value!r}"
        )
    lower = (int(match.group(1)), int(match.group(2)))
    upper = (int(match.group(3)), int(match.group(4)))
    if lower >= upper:
        raise ImproperlyConfigured(
            f"Plugin {plugin_name!r} itambox_range {value!r} is empty (lower bound >= upper bound)"
        )
    return lower, upper


def _release_pair(version_str):
    try:
        release = parse_version(version_str).release
    except InvalidVersion as exc:
        raise ImproperlyConfigured(f"Running ITAMbox version {version_str!r} is not a valid version") from exc
    return (release[0], release[1] if len(release) > 1 else 0)


def _load_plugin_config(plugin_name):
    try:
        plugin_module = importlib.import_module(plugin_name)
    except ImportError as exc:
        raise ImproperlyConfigured(f"Failed to import plugin {plugin_name!r}: {exc}") from exc

    config_cls = getattr(plugin_module, "config", None)
    if config_cls is None:
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} does not declare a 'config' attribute in its __init__.py.")
    try:
        is_plugin_config = issubclass(config_cls, PluginConfig)
    except TypeError:
        is_plugin_config = False
    if not is_plugin_config:
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} config class is not a subclass of PluginConfig.")
    return config_cls


def _check_version_range(settings_module, plugin_name, config_cls):
    """Return the (message) of a version incompatibility, else None. A malformed range raises."""
    declared = getattr(config_cls, "itambox_range", None)
    lower, upper = parse_itambox_range(plugin_name, declared)
    current_str = getattr(settings_module, "VERSION", "0.0.0")
    current = _release_pair(current_str)
    if lower <= current < upper:
        return None
    return (
        f'Plugin "{plugin_name}" supports ITAMbox {declared.strip()} but this is ITAMbox {current_str}; '
        f"the plugin was not loaded. Install a plugin release that supports {current[0]}.{current[1]} "
        f"or remove it from ITAMBOX_PLUGINS."
    )


def _validate_config_metadata(plugin_name, config_cls):
    config_name = getattr(config_cls, "name", None)
    if not isinstance(config_name, str) or not config_name.strip():
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} must declare a non-empty PluginConfig.name")
    if config_name != plugin_name:
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} PluginConfig.name must match the configured package name")
    description = getattr(config_cls, "description", None)
    if not isinstance(description, str) or not description.strip():
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} must declare a non-empty PluginConfig.description")
    base_url = getattr(config_cls, "base_url", None)
    if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
        raise TypeError(f"Plugin {plugin_name!r} base_url must be a non-empty string or None")


def _resolve_plugin_settings(plugin_name, config_cls, plugins_config):
    merged_config = deep_merge(
        getattr(config_cls, "default_settings", {}),
        plugins_config.get(plugin_name, {}),
    )
    for key in getattr(config_cls, "required_settings", []):
        if key not in merged_config or merged_config[key] is None:
            raise ImproperlyConfigured(
                f"Plugin {plugin_name!r} requires setting {key!r} to be defined in PLUGINS_CONFIG."
            )
    return merged_config


def _validate_middleware(plugin_name, config_cls):
    middleware = getattr(config_cls, "middleware", ())
    if isinstance(middleware, (str, bytes)) or not isinstance(middleware, Sequence):
        raise TypeError(f"Plugin {plugin_name!r} middleware must be a sequence of dotted paths")
    for middleware_path in middleware:
        if not isinstance(middleware_path, str) or middleware_path.count(".") < 1:
            raise TypeError(f"Plugin {plugin_name!r} middleware entry must be a dotted import path")
        try:
            middleware_class = import_string(middleware_path)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"Plugin {plugin_name!r} middleware {middleware_path!r} could not be imported"
            ) from exc
        if not callable(middleware_class):
            raise TypeError(f"Plugin {plugin_name!r} middleware {middleware_path!r} is not callable")


def _append_unique(target, values):
    for value in values:
        if value not in target:
            target.append(value)


def _resolve_plugin(settings_module, plugin_name, plugins_config):
    """Resolve phase for one plugin; returns None for a skip or the resolved data."""
    config_cls = _load_plugin_config(plugin_name)
    _validate_config_metadata(plugin_name, config_cls)
    incompatibility = _check_version_range(settings_module, plugin_name, config_cls)
    if incompatibility is not None:
        record_plugin_skip(plugin_name, PluginCompatibilityError(incompatibility), settings_module=settings_module)
        return None
    resolved_config = _resolve_plugin_settings(plugin_name, config_cls, plugins_config)
    _validate_middleware(plugin_name, config_cls)
    plugin_apps = [*getattr(config_cls, "django_apps", ()), f"{plugin_name}.{config_cls.__name__}"]
    if any(not isinstance(app_name, str) or not app_name.strip() for app_name in plugin_apps):
        raise TypeError(f"Plugin {plugin_name!r} django_apps must contain strings")
    return config_cls, resolved_config, plugin_apps, list(getattr(config_cls, "middleware", ()))


def load_plugins(settings_module):
    """Resolve phase: validate every configured plugin before Django populates apps.

    A missing package, import failure, invalid manifest or invalid configuration
    raises :class:`PluginStartupError` and aborts startup. A version
    incompatibility is the single degrade case: the plugin is skipped with a
    warning before it enters ``INSTALLED_APPS``.
    """
    plugins = list(getattr(settings_module, "PLUGINS", ()))
    plugins_config = getattr(settings_module, "PLUGINS_CONFIG", {}) or {}

    settings_module.PLUGINS_RESOLVED_CONFIG = {}
    settings_module.PLUGINS_ACTIVE = []
    settings_module.PLUGINS_DISABLED = []
    settings_module.PLUGINS_DIAGNOSTICS = []

    for plugin_name in plugins:
        try:
            resolved = _resolve_plugin(settings_module, plugin_name, plugins_config)
        except Exception as exc:
            raise PluginStartupError(plugin_name, "resolve", exc) from exc
        if resolved is None:
            continue
        config_cls, resolved_config, plugin_apps, plugin_middleware = resolved
        config_cls._plugin_settings_module = settings_module
        settings_module.PLUGINS_RESOLVED_CONFIG[plugin_name] = resolved_config
        _append_unique(settings_module.INSTALLED_APPS, plugin_apps)
        _append_unique(settings_module.MIDDLEWARE, plugin_middleware)
        settings_module.PLUGINS_ACTIVE.append(plugin_name)

    return tuple(settings_module.PLUGINS_ACTIVE)


__all__ = [
    "PluginCompatibilityError",
    "deep_merge",
    "get_plugin_diagnostics",
    "is_plugin_active",
    "load_plugins",
    "parse_itambox_range",
    "record_plugin_skip",
]
