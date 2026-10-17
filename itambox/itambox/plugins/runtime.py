import logging
import os
from collections.abc import Mapping, Sequence

from django.conf import settings
from django.utils.html import escape

from itambox.plugins.settings_schema import override_variable_name

logger = logging.getLogger(__name__)


class PluginStartupError(Exception):
    """A plugin problem that aborts startup; names the plugin, the phase and the cause.

    The message carries ``str(cause)`` with every ``secret``-flagged configuration
    value (and ``SECRET_KEY``) replaced by ``[redacted]``. ``settings_module`` is the
    settings module being loaded during the resolve phase; later phases fall back to
    ``django.conf.settings``. ``cause`` stays the original exception and the exception
    chain is kept: a traceback that Django or Python prints for the chained cause shows
    the cause's own text, which is outside this redaction.
    """

    def __init__(self, plugin_name, phase, cause, *, settings_module=None):
        self.plugin_name = plugin_name
        self.phase = phase
        self.cause = cause
        detail = _redact_text(str(cause), _settings_object(settings_module))
        super().__init__('Plugin "%s" failed during %s: %s: %s' % (plugin_name, phase, type(cause).__name__, detail))


def _setting_value_strings(value):
    """Strings of a ``SECRET_KEY``-style value: only strings of three or more characters."""
    if isinstance(value, str):
        return {value} if len(value) >= 3 else set()
    if isinstance(value, Mapping):
        result = set()
        for item in value.values():
            result.update(_setting_value_strings(item))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result = set()
        for item in value:
            result.update(_setting_value_strings(item))
        return result
    return set()


def _secret_value_strings(value):
    """Strings of a ``secret``-flagged value: any non-empty string and any int/float; bool is not secret."""
    if isinstance(value, bool):
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (int, float)):
        return {str(value)}
    if isinstance(value, Mapping):
        result = set()
        for item in value.values():
            result.update(_secret_value_strings(item))
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        result = set()
        for item in value:
            result.update(_secret_value_strings(item))
        return result
    return set()


def _secret_config_strings(settings_module):
    """Strings of every value whose schema key is flagged ``secret`` (no value guessing).

    Covers the resolved value and, for an environment override, the raw override text,
    which can differ from the converted value (``" 1e3 "`` resolves to ``1000.0``).
    """
    resolved = getattr(settings_module, "PLUGINS_RESOLVED_CONFIG", {})
    result = set()
    for plugin_name, keys in getattr(settings_module, "PLUGINS_SECRET_KEYS", {}).items():
        values = resolved.get(plugin_name, {})
        for key in keys:
            result.update(_secret_value_strings(values.get(key)))
            raw = os.environ.get(override_variable_name(plugin_name, key))
            result.update(_secret_value_strings(raw))
            result.update(_secret_value_strings(raw.strip() if raw else None))
    return result


def _redact_text(message, settings_module):
    """Plain-text redaction of secret configuration values and ``SECRET_KEY``."""
    secret_values = set()
    if settings_module is not None:
        secret_values.update(_secret_config_strings(settings_module))
        secret_values.update(_setting_value_strings(getattr(settings_module, "SECRET_KEY", "")))
    for secret in sorted(secret_values, key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    return message


def _redacted_error(exc, settings_module):
    return escape(_redact_text(str(exc), settings_module) or "[redacted]")


def _settings_object(settings_module):
    if settings_module is not None:
        return settings_module
    return settings if settings.configured else None


def record_plugin_skip(plugin_name, exc, *, settings_module=None):
    """Record the single degrade case (version incompatibility): skip with a warning and a diagnostic."""
    settings_module = _settings_object(settings_module)
    diagnostic = {
        "plugin": plugin_name,
        "plugin_name": plugin_name,
        "failure_class": type(exc).__name__,
        "stage": "compatibility",
        "compatibility": "incompatible-product",
        "activation_mode": "opt-in",
        "activation_state": "skipped",
        "active": False,
        "activation_source": "settings.PLUGINS",
        "source": "settings.PLUGINS",
        "value_present": True,
        "error": _redacted_error(exc, settings_module),
    }
    if settings_module is not None:
        disabled = list(getattr(settings_module, "PLUGINS_DISABLED", []))
        if plugin_name not in disabled:
            disabled.append(plugin_name)
        settings_module.PLUGINS_DISABLED = disabled
        rows = [row for row in getattr(settings_module, "PLUGINS_DIAGNOSTICS", []) if row["plugin"] != plugin_name]
        rows.append(diagnostic)
        settings_module.PLUGINS_DIAGNOSTICS = rows
    logger.warning("%s", _redact_text(str(exc), settings_module))
    return diagnostic


def get_plugin_diagnostics(settings_module=None):
    settings_module = _settings_object(settings_module)
    if settings_module is None:
        return ()
    return tuple(dict(row) for row in getattr(settings_module, "PLUGINS_DIAGNOSTICS", ()))


def is_plugin_active(plugin_name, settings_module=None):
    settings_module = _settings_object(settings_module)
    return settings_module is not None and plugin_name in getattr(settings_module, "PLUGINS_ACTIVE", ())
