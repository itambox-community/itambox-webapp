"""Type-aware display model for stored specification values.

The builders are pure: they read already loaded definitions and stored values and
never invent a value. A value that does not fit its definition is shown raw with an
explicit state instead of being coerced.

Every vocabulary label (definition, choice, section, unit token) goes through a
``VocabularyTranslator`` (``extras.services.vocabulary_translations``) passed in by
the view; without one the base labels are shown.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.utils.formats import date_format, number_format
from django.utils.text import Truncator
from django.utils.translation import gettext as _

from extras.services.vocabulary_translations import BASE_LABELS

TEXT_DISPLAY_LIMIT = 120
PLACEHOLDER = "-"

STATE_OK = "ok"
STATE_INVALID = "invalid"
STATE_UNKNOWN_VALUE = "unknown_value"
STATE_DEPRECATED = "deprecated"
STATE_HISTORICAL = "historical"
STATE_UNKNOWN_FIELD = "unknown_field"

KIND_TEXT = "text"
KIND_NUMBER = "number"
KIND_BOOLEAN = "boolean"
KIND_DATE = "date"
KIND_CHOICE = "choice"
KIND_CHIPS = "chips"
KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SpecItem:
    """One member of a multi-select value."""

    key: str
    text: str
    state: str = STATE_OK


@dataclass(frozen=True)
class SpecDisplayEntry:
    key: str
    label: str
    kind: str
    text: str = ""
    unit: str = ""
    items: tuple[SpecItem, ...] = ()
    raw: str = ""
    state: str = STATE_OK
    group: str | None = None
    flag: bool | None = None


@dataclass(frozen=True)
class SpecGroup:
    label: str | None
    entries: tuple[SpecDisplayEntry, ...]


@dataclass(frozen=True)
class SpecPanel:
    groups: tuple[SpecGroup, ...]
    further: tuple[SpecDisplayEntry, ...]

    def __bool__(self) -> bool:
        return bool(self.groups or self.further)


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _raw_text(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _format_decimal(number: Decimal) -> str:
    plain = format(number.normalize(), "f") if number != 0 else "0"
    places = len(plain.split(".")[1]) if "." in plain else 0
    return number_format(Decimal(plain), decimal_pos=places, use_l10n=True, force_grouping=True)


def _parse_decimal(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        number = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _invalid(value) -> dict:
    return {"kind": KIND_TEXT, "text": _raw_text(value), "state": STATE_INVALID}


def _build_boolean(definition, value, translator) -> dict:
    if isinstance(value, bool):
        return {"kind": KIND_BOOLEAN, "flag": value, "raw": _raw_text(value)}
    return _invalid(value)


def _build_integer(definition, value, translator) -> dict:
    if isinstance(value, bool) or not isinstance(value, int):
        return _invalid(value)
    return {
        "kind": KIND_NUMBER,
        "text": number_format(value, use_l10n=True, force_grouping=True),
        "unit": translator.unit(definition),
        "raw": str(value),
    }


def _build_decimal(definition, value, translator) -> dict:
    number = _parse_decimal(value)
    if number is None:
        return _invalid(value)
    return {
        "kind": KIND_NUMBER,
        "text": _format_decimal(number),
        "unit": translator.unit(definition),
        "raw": _raw_text(value),
    }


def _build_date(definition, value, translator) -> dict:
    if not isinstance(value, str):
        return _invalid(value)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return _invalid(value)
    return {"kind": KIND_DATE, "text": date_format(parsed, "DATE_FORMAT"), "raw": value}


def _build_text(definition, value, translator) -> dict:
    if not isinstance(value, str):
        return _invalid(value)
    shown = Truncator(value).chars(TEXT_DISPLAY_LIMIT)
    return {"kind": KIND_TEXT, "text": shown, "raw": value if shown != value else ""}


def _choice_index(definition) -> dict | None:
    choice_set = getattr(definition, "choice_set", None)
    if choice_set is None:
        return None
    return {choice.key: choice for choice in choice_set.choices.all()}


def _resolve_choice(index, key, translator) -> SpecItem:
    if not isinstance(key, str):
        return SpecItem(key=_raw_text(key), text=_raw_text(key), state=STATE_INVALID)
    if index is None:
        return SpecItem(key=key, text=key)
    choice = index.get(key)
    if choice is None:
        return SpecItem(key=key, text=key, state=STATE_UNKNOWN_VALUE)
    deprecated = getattr(choice, "lifecycle", "active") == "deprecated"
    return SpecItem(key=key, text=translator.choice_label(choice), state=STATE_DEPRECATED if deprecated else STATE_OK)


def _build_single_select(definition, value, translator) -> dict:
    item = _resolve_choice(_choice_index(definition), value, translator)
    return {"kind": KIND_CHOICE, "text": item.text, "raw": item.key, "state": item.state}


def _build_multi_select(definition, value, translator) -> dict:
    if not isinstance(value, list):
        return _invalid(value)
    index = _choice_index(definition)
    positions = {key: choice.position for key, choice in (index or {}).items()}
    items = [_resolve_choice(index, key, translator) for key in value]
    if index is not None:
        items.sort(key=lambda item: (positions.get(item.key, float("inf")), item.key))
    return {"kind": KIND_CHIPS, "items": tuple(items), "raw": _raw_text(value)}


_BUILDERS = {
    "boolean": _build_boolean,
    "integer": _build_integer,
    "decimal": _build_decimal,
    "date": _build_date,
    "text": _build_text,
    "single-select": _build_single_select,
    "multi-select": _build_multi_select,
}


def build_entry(definition, value, *, group=None, state=None, translator=BASE_LABELS) -> SpecDisplayEntry:
    """Render one stored value of a known definition."""
    builder = _BUILDERS.get(definition.field_type, _build_text)
    parts = builder(definition, value, translator)
    if state is not None and parts.get("state", STATE_OK) == STATE_OK:
        parts["state"] = state
    return SpecDisplayEntry(key=definition.name, label=translator.field_label(definition), group=group, **parts)


def build_spec_groups(resolved_fields, stored_values, group_labels=None, translator=BASE_LABELS) -> list[SpecGroup]:
    """Group the non-empty resolved values by their first fieldset, in composition order."""
    stored_values = stored_values or {}
    group_labels = group_labels or {}
    named: dict[str, list[SpecDisplayEntry]] = {}
    ungrouped: list[SpecDisplayEntry] = []
    for resolved in resolved_fields:
        value = stored_values.get(resolved.definition.name)
        if _is_empty(value):
            continue
        identity = resolved.provenance[0] if resolved.provenance else None
        label = None
        if identity is not None:
            label = group_labels.get(identity) or identity.split("/", 1)[-1]
        entry = build_entry(resolved.definition, value, group=label, translator=translator)
        (named.setdefault(identity, []) if identity is not None else ungrouped).append(entry)
    groups = [SpecGroup(label=entries[0].group, entries=tuple(entries)) for entries in named.values()]
    if ungrouped:
        groups.append(SpecGroup(label=_("Other") if groups else None, entries=tuple(ungrouped)))
    return groups


def _unknown_entry(key, value) -> SpecDisplayEntry:
    text = PLACEHOLDER if _is_empty(value) else _raw_text(value)
    return SpecDisplayEntry(key=key, label=key, kind=KIND_UNKNOWN, text=text, state=STATE_UNKNOWN_FIELD)


def build_further_values(
    resolved_fields, stored_values, definitions=None, translator=BASE_LABELS
) -> list[SpecDisplayEntry]:
    """Stored keys that the current specification does not resolve."""
    stored_values = stored_values or {}
    definitions = definitions or {}
    resolved_names = {resolved.definition.name for resolved in resolved_fields}
    entries = []
    for key in sorted(stored_values):
        if key in resolved_names:
            continue
        value = stored_values[key]
        definition = definitions.get(key)
        if definition is None:
            entries.append(_unknown_entry(key, value))
        elif _is_empty(value):
            entries.append(
                SpecDisplayEntry(
                    key=key,
                    label=translator.field_label(definition),
                    kind=KIND_TEXT,
                    text=PLACEHOLDER,
                    state=STATE_HISTORICAL,
                )
            )
        else:
            entries.append(build_entry(definition, value, state=STATE_HISTORICAL, translator=translator))
    return entries


def fieldset_group_labels(fieldsets, translator=BASE_LABELS) -> dict[str, str]:
    return {f"{fs.namespace}/{fs.slug}": translator.fieldset_label(fs) for fs in fieldsets}


def build_spec_panel(
    resolved_fields, stored_values, fieldsets, definitions_loader=None, include_further=True, translator=BASE_LABELS
) -> SpecPanel:
    """Build the grouped values plus the completeness block for one object.

    ``definitions_loader`` receives the unresolved stored keys and returns a
    name -> definition mapping; it is only called when such keys exist.
    ``translator`` resolves every vocabulary label; it is loaded here for the
    resolved definitions, the fieldsets and any historical definitions.
    """
    stored_values = dict(stored_values or {})
    translator.load_resolved(resolved_fields, fieldsets)
    groups = build_spec_groups(
        resolved_fields, stored_values, fieldset_group_labels(fieldsets, translator), translator=translator
    )
    resolved_names = {resolved.definition.name for resolved in resolved_fields}
    rest = [key for key in stored_values if key not in resolved_names] if include_further else []
    definitions = definitions_loader(rest) if rest and definitions_loader else {}
    translator.load(fields=list(definitions.values()))
    further = (
        build_further_values(resolved_fields, stored_values, definitions, translator=translator)
        if include_further
        else []
    )
    return SpecPanel(groups=tuple(groups), further=tuple(further))
