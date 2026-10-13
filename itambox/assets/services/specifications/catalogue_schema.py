"""Generate the catalogue JSON Schema from the core vocabulary.

The webapp owns the vocabulary; the community catalogue repository validates its
flat YAML files against a copy of the schema generated here.
"""

import json
from pathlib import Path

from assets.services.specifications.core_vocabulary import get_core_vocabulary

SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_PATH = Path(__file__).with_name("generated_schema.json")
_REGEX_SPECIALS = frozenset(".^$*+?{}[]\\|()")


def _escape(text: str) -> str:
    return "".join("\\" + ch if ch in _REGEX_SPECIALS else ch for ch in text)


def _number(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _quantity_schema(field: dict) -> dict:
    validation = field["validation"]
    negative = float(validation.get("minimum", "0")) < 0
    sign = "-?" if negative else ""
    fraction = "" if field["field_type"] == "integer" else r"(\.[0-9]+)?"
    unit = field["canonical_unit"]
    return {
        "type": "string",
        "pattern": f"^{sign}[0-9]+{fraction} {_escape(unit)}$",
        "description": f"Value in {unit}",
    }


def _text_schema(field: dict) -> dict:
    validation = field["validation"]
    schema: dict = {"type": "string"}
    if "max_length" in validation:
        schema["maxLength"] = validation["max_length"]
    if "regex" in validation:
        schema["pattern"] = validation["regex"]
    return schema


def _integer_schema(field: dict) -> dict:
    validation = field["validation"]
    schema: dict = {"type": "integer"}
    if "minimum" in validation:
        schema["minimum"] = _number(validation["minimum"])
    if "maximum" in validation:
        schema["maximum"] = _number(validation["maximum"])
    return schema


def _spec_schema(field: dict, choice_keys: dict[str, list[str]]) -> dict:
    field_type = field["field_type"]
    if field["canonical_unit"]:
        return _quantity_schema(field)
    if field_type == "single-select":
        return {"enum": sorted(choice_keys[field["choice_set"]])}
    if field_type == "multi-select":
        return {
            "type": "array",
            "items": {"enum": sorted(choice_keys[field["choice_set"]])},
            "uniqueItems": True,
            "maxItems": field["validation"]["max_values"],
        }
    if field_type == "boolean":
        return {"type": "boolean"}
    if field_type == "integer":
        return _integer_schema(field)
    return _text_schema(field)


def build_catalogue_schema() -> dict:
    vocabulary = get_core_vocabulary()
    choice_keys = {cs["identity"]: [c["key"] for c in cs["choices"]] for cs in vocabulary["choice_sets"]}
    specs = {f["key"]: _spec_schema(f, choice_keys) for f in vocabulary["active_fields"]}
    return {
        "$schema": SCHEMA_DRAFT,
        "title": "ITAMbox catalogue asset type",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "manufacturer": {"type": "string"},
            "model": {"type": "string"},
            "part_number": {"type": "string", "maxLength": 100},
            "ean": {"type": "string", "maxLength": 14},
            "category": {"enum": sorted(c["slug"] for c in vocabulary["categories"])},
            "description": {"type": "string"},
            "specs": {"type": "object", "additionalProperties": False, "properties": specs},
        },
    }


def render_catalogue_schema() -> str:
    return json.dumps(build_catalogue_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
