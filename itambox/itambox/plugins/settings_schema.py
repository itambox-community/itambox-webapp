"""Operator configuration of plugins: the ``settings_schema`` contract and its resolution.

Safe to import at settings-load time (no Django models, no app registry).

A plugin declares ``settings_schema = {key: spec}``. A spec is a mapping with:

``type``      one of ``str``, ``int``, ``float``, ``bool``, ``list``, ``dict`` (default ``str``)
``default``   value used when the operator supplies none (not allowed with ``required``/``secret``)
``required``  the operator must supply a value (default ``False``)
``secret``    the value is never logged, shown in diagnostics or put in an error message
``choices``   optional list of allowed values (scalar types only)

Operator values come from two channels, the later one winning:

1. ``ITAMBOX_PLUGINS_CONFIG``: one JSON object keyed by package name, the same shape
   convention as the ``ITAMBOX_TENANT_*_CONFIGS`` variables.
2. ``ITAMBOX_PLUGIN_<NAME>_<KEY>``: a per-key environment override so a secret store can
   inject a single value. ``<NAME>`` and ``<KEY>`` are the package name and the schema key,
   upper-cased, every character outside ``A-Z0-9`` replaced by ``_``. Values are strings and
   are converted to the declared type (``bool``: true/false/1/0/yes/no/on/off; ``int`` and
   ``float``: numeric text; ``list`` and ``dict``: JSON text).

Every error names the plugin and the key and never contains a configured value.
"""

import json
import re

from django.core.exceptions import ImproperlyConfigured

ENV_PREFIX = "ITAMBOX_PLUGIN_"

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SPEC_FIELDS = frozenset({"type", "default", "required", "secret", "choices"})
_TYPES = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}
_SCALARS = frozenset({"str", "int", "float", "bool"})
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def override_variable_name(plugin_name, key):
    """Environment variable that overrides ``key`` of ``plugin_name``."""
    normalise = re.compile(r"[^A-Z0-9]")
    return f"{ENV_PREFIX}{normalise.sub('_', plugin_name.upper())}_{normalise.sub('_', key.upper())}"


def _matches_type(type_name, value):
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[type_name])


def _check_flags_and_default(where, spec, type_name):
    for flag in ("required", "secret"):
        if not isinstance(spec.get(flag, False), bool):
            raise ImproperlyConfigured(f"{where} field {flag!r} must be a boolean")
    if "default" in spec and (spec.get("required") or spec.get("secret")):
        raise ImproperlyConfigured(f"{where} must not combine a default with required or secret")
    if "default" in spec and not _matches_type(type_name, spec["default"]):
        raise ImproperlyConfigured(f"{where} default is not of type {type_name}")


def _check_choices(where, spec, type_name):
    choices = spec["choices"]
    if type_name not in _SCALARS or not isinstance(choices, (list, tuple)) or not choices:
        raise ImproperlyConfigured(f"{where} choices must be a non-empty list on a scalar type")
    if not all(_matches_type(type_name, choice) for choice in choices):
        raise ImproperlyConfigured(f"{where} has a choice that is not of type {type_name}")
    if "default" in spec and spec["default"] not in choices:
        raise ImproperlyConfigured(f"{where} default is not one of its choices")


def _check_spec(plugin_name, key, spec):
    where = f"Plugin {plugin_name!r} settings_schema key {key!r}"
    if not isinstance(key, str) or not _KEY.match(key):
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} settings_schema key {key!r} is not a valid identifier")
    if not isinstance(spec, dict):
        raise ImproperlyConfigured(f"{where} must be a mapping")
    unknown = sorted(set(spec) - _SPEC_FIELDS)
    if unknown:
        raise ImproperlyConfigured(f"{where} has unknown field(s) {unknown}")
    type_name = spec.get("type", "str")
    if type_name not in _TYPES:
        raise ImproperlyConfigured(f"{where} has unknown type {type_name!r}; use one of {sorted(_TYPES)}")
    _check_flags_and_default(where, spec, type_name)
    if "choices" in spec:
        _check_choices(where, spec, type_name)


def validate_schema(plugin_name, schema):
    """Validate the declared schema itself; a malformed schema is a configuration error."""
    if not isinstance(schema, dict):
        raise ImproperlyConfigured(f"Plugin {plugin_name!r} settings_schema must be a mapping")
    for key, spec in schema.items():
        _check_spec(plugin_name, key, spec)
    return schema


def secret_keys(schema):
    return frozenset(key for key, spec in schema.items() if spec.get("secret"))


def _from_text(plugin_name, key, type_name, text):
    """Convert an environment string; the message never repeats the text."""
    where = f"Plugin {plugin_name!r} setting {key!r}"
    if type_name == "str":
        return text
    if type_name == "bool":
        lowered = text.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ImproperlyConfigured(f"{where} (environment override) must be a boolean such as true or false")
    try:
        if type_name == "int":
            return int(text.strip())
        if type_name == "float":
            return float(text.strip())
        value = json.loads(text)
    except ValueError:
        # no exception chaining: int()/float() messages repeat the offending text
        raise ImproperlyConfigured(f"{where} (environment override) is not a valid {type_name}") from None
    return value


def _check_value(plugin_name, key, spec, value):
    where = f"Plugin {plugin_name!r} setting {key!r}"
    type_name = spec.get("type", "str")
    if not _matches_type(type_name, value):
        raise ImproperlyConfigured(f"{where} must be of type {type_name}")
    if "choices" in spec and value not in spec["choices"]:
        raise ImproperlyConfigured(f"{where} is not one of the allowed choices")


def _json_entry(plugin_name, plugins_config):
    entry = plugins_config.get(plugin_name, {})
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(f"ITAMBOX_PLUGINS_CONFIG entry for plugin {plugin_name!r} must be a JSON object")
    return entry


def _supplied_value(plugin_name, key, spec, entry, environ):
    """Return (found, value): the environment override beats the JSON entry."""
    variable = override_variable_name(plugin_name, key)
    if variable in environ:
        return True, _from_text(plugin_name, key, spec.get("type", "str"), environ[variable])
    if key in entry:
        return True, entry[key]
    return False, None


def resolve_plugin_settings(plugin_name, schema, plugins_config, environ):
    """Resolve and validate the operator values of one plugin against its schema.

    Returns the final ``{key: value}`` mapping. Unknown keys, missing required keys,
    wrong types and values outside ``choices`` raise ``ImproperlyConfigured``.
    """
    validate_schema(plugin_name, schema)
    entry = _json_entry(plugin_name, plugins_config)
    unknown = sorted(set(entry) - set(schema))
    if unknown:
        raise ImproperlyConfigured(
            f"Plugin {plugin_name!r} has unknown setting(s) {unknown} in ITAMBOX_PLUGINS_CONFIG; "
            f"declared keys are {sorted(schema)}"
        )
    resolved = {}
    for key, spec in schema.items():
        found, value = _supplied_value(plugin_name, key, spec, entry, environ)
        if not found or (value is None and "default" in spec):
            if spec.get("required"):
                raise ImproperlyConfigured(
                    f"Plugin {plugin_name!r} requires setting {key!r}; set it in ITAMBOX_PLUGINS_CONFIG "
                    f"or through the environment variable {override_variable_name(plugin_name, key)}"
                )
            if "default" in spec:
                resolved[key] = json.loads(json.dumps(spec["default"]))
            continue
        _check_value(plugin_name, key, spec, value)
        resolved[key] = value
    return resolved
