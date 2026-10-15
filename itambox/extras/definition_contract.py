"""Canonical validation contract for custom-field definitions."""

import re
from collections.abc import Iterable

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

FIELD_TYPES = frozenset(
    {
        "text",
        "integer",
        "decimal",
        "date",
        "boolean",
        "single-select",
        "multi-select",
    }
)
SELECT_FIELD_TYPES = frozenset({"single-select", "multi-select"})
NUMERIC_FIELD_TYPES = frozenset({"integer", "decimal"})
LIFECYCLES = frozenset({"active", "deprecated"})
ACTIVATIONS = frozenset({"composed", "global"})
MANAGEMENT_KINDS = frozenset({"core", "library", "local"})
VALIDATION_RULE_TYPES = {
    "rfc1123_hostname": "text",
    "runtime_requires_load": "decimal",
    "temperature_max_gte_min": "decimal",
    "voltage_max_gte_min": "decimal",
}
CORE_VALIDATION_RULE_IDENTITIES = {
    "runtime_requires_load": ("itambox", "battery_runtime_load"),
    "temperature_max_gte_min": ("itambox", "operating_temperature_max"),
    "voltage_max_gte_min": ("itambox", "input_voltage_max"),
}
QUANTITY_UNITS = {
    "length": frozenset({"U", "m", "cm", "mm", "in", "ft"}),
    "mass": frozenset({"kg", "g", "lb", "oz"}),
    "count": frozenset({None}),
    "digital_information": frozenset({"B", "KiB", "MiB", "GiB", "TiB"}),
    "data_rate": frozenset({"bit/s", "Kbit/s", "Mbit/s", "Gbit/s", "Tbit/s"}),
    "power": frozenset({"W", "kW"}),
    "apparent_power": frozenset({"VA"}),
    "voltage": frozenset({"V", "mV", "kV"}),
    "energy": frozenset({"Wh", "kWh"}),
    "duration": frozenset({"s", "min", "h", "d"}),
    "resolution": frozenset({"MP"}),
    "rate": frozenset({"pages_per_minute"}),
    "temperature": frozenset({"°C", "°F", "K"}),
    "sound_pressure": frozenset({"dBA"}),
}


def _regex_tokens(pattern):
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            yield "atom", None
            index += 2
        elif char == "[":
            index = _skip_character_class(pattern, index + 1)
            yield "atom", None
        elif char == "(":
            yield "open", None
            index += 1
        elif char == "|":
            yield "alternation", None
            index += 1
        elif char == ")":
            yield "close", None
            index += 1
        elif char in "*+?":
            yield "quantifier", char
            index += 1
        elif char == "{":
            token = _brace_quantifier(pattern, index)
            if token is None:
                yield "atom", None
                index += 1
            else:
                quantifier, index = token
                yield "quantifier", quantifier
        else:
            yield "boundary" if char in "^$" else "atom", None
            index += 1


def _skip_character_class(pattern, index):
    while index < len(pattern):
        if pattern[index] == "\\":
            index += 2
        elif pattern[index] == "]":
            return index + 1
        else:
            index += 1
    return index


def _brace_quantifier(pattern, index):
    end = pattern.find("}", index + 1)
    if end == -1 or re.fullmatch(r"(?:\d+|\d+,\d*|,\d+)", pattern[index + 1 : end]) is None:
        return None
    return ("{unbounded" if pattern[end - 1] == "," else "{", end + 1)


def _new_regex_group():
    return {"alternation": False, "repeat": False, "unbounded": False, "risk": False}


def _close_regex_group(groups):
    if not groups:
        return None
    closed = groups.pop()
    if groups:
        parent = groups[-1]
        parent["alternation"] |= closed["alternation"]
        parent["repeat"] |= closed["repeat"]
        parent["unbounded"] |= closed["unbounded"]
        parent["risk"] |= closed["risk"] or closed["alternation"]
    return closed


def _quantified_group_is_risky(group, quantifier):
    return group["alternation"] or group["risk"] or group["unbounded"] or (group["repeat"] and quantifier != "?")


def _mark_quantified_atom(groups, group, quantifier):
    unbounded = quantifier in {"*", "+", "{unbounded"}
    if group is not None:
        if _quantified_group_is_risky(group, quantifier):
            return True
        if groups:
            groups[-1]["repeat"] = True
            groups[-1]["unbounded"] |= unbounded
            groups[-1]["risk"] |= group["alternation"] or group["risk"]
    elif groups:
        groups[-1]["repeat"] = True
        groups[-1]["unbounded"] |= unbounded
    return False


def _has_nested_repetition_risk(pattern):
    groups = []
    last_group = None
    last_atom = False
    for token, value in _regex_tokens(pattern):
        if token == "open":
            groups.append(_new_regex_group())
            last_group = None
            last_atom = False
        elif token == "alternation":
            if groups:
                groups[-1]["alternation"] = True
            last_group = None
            last_atom = False
        elif token == "close":
            last_group = _close_regex_group(groups)
            last_atom = True
        elif token == "quantifier" and last_atom:
            if _mark_quantified_atom(groups, last_group, value):
                return True
            last_group = None
            last_atom = False
        elif token == "atom":
            last_group = None
            last_atom = True
        else:
            last_group = None
            last_atom = False
    return False


def _count_unbounded_repeats(pattern):
    count = 0
    index = 0
    in_character_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_character_class = True
        elif char == "]" and in_character_class:
            in_character_class = False
        elif not in_character_class and char in "*+":
            if index == 0 or pattern[index - 1] not in "*+":
                count += 1
        elif not in_character_class and char == "{":
            end = pattern.find("}", index + 1)
            if end != -1 and "," in pattern[index + 1 : end] and pattern[end - 1] == ",":
                count += 1
                index = end
        index += 1
    return count


INLINE_FLAGS_RE = re.compile(r"\(\?[aiLmsux-]+(?::|\))")


def _has_inline_flags(pattern):
    return INLINE_FLAGS_RE.search(pattern) is not None


def _has_backreference(pattern):
    return re.search(r"\\(?:[1-9]|g<|k<)|\(\?P=", pattern) is not None


def validate_custom_field_regex(pattern):
    if not isinstance(pattern, str) or len(pattern) > 256:
        raise ValidationError(_("The regular expression is invalid."), code="INVALID_REGEX")
    if _has_inline_flags(pattern):
        raise ValidationError(_("Inline regular-expression flags are not allowed."), code="INVALID_REGEX")
    try:
        re.compile(pattern, re.ASCII)
    except (re.error, OverflowError, ValueError) as exc:
        raise ValidationError(_("The regular expression is invalid."), code="INVALID_REGEX") from exc
    if _has_backreference(pattern) or _has_nested_repetition_risk(pattern):
        raise ValidationError(_("The regular expression is too complex."), code="INVALID_REGEX")
    if _count_unbounded_repeats(pattern) > 1:
        raise ValidationError(_("The regular expression is too complex."), code="INVALID_REGEX")
    return pattern


def _add_error(errors, field, message):
    errors.setdefault(field, message)


def _basic_definition_errors(field_type, activation, management_kind, lifecycle, minimum_value, maximum_value):
    errors = {}
    if field_type not in FIELD_TYPES:
        _add_error(errors, "field_type", "Unsupported custom field type.")
    if activation not in ACTIVATIONS:
        _add_error(errors, "activation", "Unsupported custom field activation.")
    if management_kind not in MANAGEMENT_KINDS:
        _add_error(errors, "management_kind", "Unsupported management kind.")
    if lifecycle not in LIFECYCLES:
        _add_error(errors, "lifecycle", "Unsupported lifecycle.")
    if minimum_value is not None and maximum_value is not None and minimum_value > maximum_value:
        _add_error(errors, "maximum_value", "The maximum must not be below the minimum.")
    return errors


def _numeric_metadata_errors(field_type, decimal_scale, text_max_length, minimum_value, maximum_value):
    errors = {}
    if field_type == "decimal":
        if decimal_scale is None or isinstance(decimal_scale, bool) or not 0 <= decimal_scale <= 6:
            _add_error(errors, "decimal_scale", _("Decimal fields require a scale from 0 to 6."))
    elif decimal_scale is not None:
        _add_error(errors, "decimal_scale", _("Only decimal fields may define a scale."))

    if field_type == "text":
        if text_max_length is not None and not 1 <= text_max_length <= 4096:
            _add_error(errors, "text_max_length", "Text length must be between 1 and 4096.")
    elif text_max_length is not None:
        _add_error(errors, "text_max_length", "Only text fields may define a text length.")

    if field_type not in NUMERIC_FIELD_TYPES:
        if minimum_value is not None:
            _add_error(errors, "minimum_value", "Only numeric fields may define a minimum.")
        if maximum_value is not None:
            _add_error(errors, "maximum_value", "Only numeric fields may define a maximum.")
    return errors


def _regex_definition_errors(field_type, regex):
    if not regex:
        return {}
    if field_type != "text":
        return {"regex": "Only text fields may define a regular expression."}
    try:
        validate_custom_field_regex(regex)
    except ValidationError as exc:
        return {"regex": exc.messages[0]}
    return {}


def _rule_definition_errors(field_type, validation_rule, name, namespace, management_kind):
    if validation_rule is None:
        return {}
    expected_type = VALIDATION_RULE_TYPES.get(validation_rule)
    if expected_type is None:
        return {"validation_rule": "Unsupported validation rule."}
    if field_type != expected_type:
        return {"validation_rule": "The validation rule does not match the field type."}
    registered_identity = CORE_VALIDATION_RULE_IDENTITIES.get(validation_rule)
    if registered_identity and ((namespace, name) != registered_identity or management_kind != "core"):
        return {"validation_rule": "This cross-field rule is reserved for its registered Core definition."}
    return {}


def _quantity_definition_errors(field_type, quantity_kind, canonical_unit):
    errors = {}
    if quantity_kind is not None:
        units = QUANTITY_UNITS.get(quantity_kind)
        if units is None:
            _add_error(errors, "quantity_kind", "Unsupported quantity kind.")
        elif canonical_unit not in units:
            _add_error(errors, "canonical_unit", "The unit is not valid for this quantity kind.")
    elif canonical_unit is not None:
        _add_error(errors, "quantity_kind", "A canonical unit requires a quantity kind.")

    if field_type not in NUMERIC_FIELD_TYPES and (quantity_kind is not None or canonical_unit is not None):
        _add_error(errors, "quantity_kind", "Only numeric fields may define quantity metadata.")
    return errors


def _choice_definition_errors(field_type, choice_set, max_values):
    errors = {}
    if field_type in SELECT_FIELD_TYPES:
        if choice_set is None:
            _add_error(errors, "choice_set", _("Select fields require a Choice Set."))
        elif getattr(choice_set, "lifecycle", "active") != "active":
            _add_error(errors, "choice_set", _("Choice Sets must be active."))
        if field_type == "single-select" and max_values != 1:
            _add_error(errors, "max_values", _("Single-select fields must limit values to exactly one."))
        if field_type == "multi-select" and max_values is None:
            _add_error(errors, "max_values", _("Multi-select fields require a maximum value count."))
    else:
        if choice_set is not None:
            _add_error(errors, "choice_set", _("Only select fields may reference a Choice Set."))
        if max_values is not None:
            _add_error(errors, "max_values", "Only select fields may define a maximum value count.")
    if max_values is not None and not 1 <= max_values <= 64:
        _add_error(errors, "max_values", "The maximum value count must be between 1 and 64.")
    return errors


def _applicability_errors(object_types):
    if object_types is None:
        return {}
    if not list(object_types):
        return {"object_types": _("Select at least one applicable model.")}
    return {}


def _object_type_identity(item):
    return (getattr(item, "app_label", None), getattr(item, "model", None))


def custom_field_definition_contract_errors(
    *,
    field_type,
    activation="global",
    quantity_kind=None,
    canonical_unit=None,
    minimum_value=None,
    maximum_value=None,
    regex=None,
    decimal_scale=None,
    max_values=None,
    text_max_length=None,
    validation_rule=None,
    mappings=None,
    choice_set=None,
    object_types: Iterable | None = None,
    management_kind="local",
    lifecycle="active",
    required=None,
    nullable=None,
    name=None,
    namespace=None,
):
    """Return field-level errors for one complete custom-field definition."""
    errors = {}
    for group in (
        _basic_definition_errors(field_type, activation, management_kind, lifecycle, minimum_value, maximum_value),
        _numeric_metadata_errors(field_type, decimal_scale, text_max_length, minimum_value, maximum_value),
        _regex_definition_errors(field_type, regex),
        _rule_definition_errors(field_type, validation_rule, name, namespace, management_kind),
        _quantity_definition_errors(field_type, quantity_kind, canonical_unit),
        _choice_definition_errors(field_type, choice_set, max_values),
        _applicability_errors(object_types),
    ):
        for field, message in group.items():
            _add_error(errors, field, message)
    if mappings is not None and not isinstance(mappings, list):
        _add_error(errors, "mappings", "Mappings must be a list.")
    return errors


def validate_custom_field_definition_contract(**kwargs):
    """Raise a Django ValidationError when a definition violates the contract."""
    errors = custom_field_definition_contract_errors(**kwargs)
    if errors:
        raise ValidationError(errors)
