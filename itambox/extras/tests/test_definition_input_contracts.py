"""DB-free constructor contracts for definition input DTOs."""

from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from extras.definition_contract import (
    custom_field_definition_contract_errors,
    validate_custom_field_definition_contract,
    validate_custom_field_regex,
)
from extras.services.definition_command_contracts import (
    CustomFieldChoiceCreateInputDTO,
    CustomFieldChoiceSetCreateInputDTO,
    CustomFieldChoiceSetUpdateInputDTO,
    CustomFieldChoiceUpdateInputDTO,
    CustomFieldCreateInputDTO,
    CustomFieldsetCreateInputDTO,
    CustomFieldsetUpdateInputDTO,
    CustomFieldUpdateInputDTO,
)


def _field_create_input(**overrides):
    values = {
        "namespace": "boundary",
        "local_key": "observed",
        "label": "Observed",
        "object_types": ("assets.assettype", "inventory.item"),
        "field_type": "decimal",
        "activation": "composed",
        "help_text": "Measured value",
        "quantity_kind": "capacity",
        "canonical_unit": "GiB",
        "minimum_value": Decimal("0.5"),
        "maximum_value": Decimal("10.5"),
        "regex": r"^\d+(\.\d+)?$",
        "decimal_scale": 2,
        "max_values": 3,
        "text_max_length": 64,
        "validation_rule": "positive",
        "required": True,
        "nullable": True,
        "mappings": (),
        "choice_set_id": 7,
        "replaced_by": "boundary.replacement",
    }
    values.update(overrides)
    return CustomFieldCreateInputDTO(**values)


def _field_update_input(**overrides):
    values = {
        "label": "Updated",
        "help_text": "Updated help",
        "activation": "global",
        "required": False,
        "mappings": (),
        "object_types": ("assets.assettype",),
        "replaced_by": "boundary.replacement",
    }
    values.update(overrides)
    return CustomFieldUpdateInputDTO(**values)


def _fieldset_create_input(**overrides):
    values = {
        "namespace": "boundary",
        "slug": "details",
        "label": "Details",
        "description": "Definition details",
        "field_identities": ("boundary.first", "boundary.second"),
        "replaced_by": "boundary.replacement",
    }
    values.update(overrides)
    return CustomFieldsetCreateInputDTO(**values)


def _fieldset_update_input(**overrides):
    values = {"label": "Updated details", "description": "Updated description", "replaced_by": "boundary.replacement"}
    values.update(overrides)
    return CustomFieldsetUpdateInputDTO(**values)


def _choice_set_create_input(**overrides):
    values = {
        "namespace": "boundary",
        "slug": "states",
        "label": "States",
        "replaced_by": "boundary.replacement",
    }
    values.update(overrides)
    return CustomFieldChoiceSetCreateInputDTO(**values)


def _choice_set_update_input(**overrides):
    values = {"label": "Updated states", "replaced_by": "boundary.replacement"}
    values.update(overrides)
    return CustomFieldChoiceSetUpdateInputDTO(**values)


def _choice_create_input(**overrides):
    values = {
        "choice_set_id": 7,
        "key": "active",
        "label": "Active",
        "position": 2,
        "replaced_by": "boundary.replacement",
    }
    values.update(overrides)
    return CustomFieldChoiceCreateInputDTO(**values)


def _choice_update_input(**overrides):
    values = {"label": "Updated active", "position": 3, "replaced_by": "boundary.replacement"}
    values.update(overrides)
    return CustomFieldChoiceUpdateInputDTO(**values)


def test_custom_field_create_round_trips_accepted_scalars_and_tuples():
    dto = _field_create_input()

    assert dto.namespace == "boundary"
    assert dto.local_key == "observed"
    assert dto.label == "Observed"
    assert dto.object_types == ("assets.assettype", "inventory.item")
    assert dto.field_type == "decimal"
    assert dto.activation == "composed"
    assert dto.help_text == "Measured value"
    assert dto.quantity_kind == "capacity"
    assert dto.canonical_unit == "GiB"
    assert dto.minimum_value == Decimal("0.5")
    assert dto.maximum_value == Decimal("10.5")
    assert dto.regex == r"^\d+(\.\d+)?$"
    assert dto.decimal_scale == 2
    assert dto.max_values == 3
    assert dto.text_max_length == 64
    assert dto.validation_rule == "positive"
    assert dto.required is True
    assert dto.nullable is True
    assert type(dto.required) is bool
    assert type(dto.nullable) is bool
    assert dto.mappings == ()
    assert dto.choice_set_id == 7
    assert type(dto.choice_set_id) is int
    assert dto.replaced_by == "boundary.replacement"


def test_custom_field_update_round_trips_accepted_scalars_and_tuples():
    dto = _field_update_input()

    assert dto.label == "Updated"
    assert dto.help_text == "Updated help"
    assert dto.activation == "global"
    assert dto.required is False
    assert type(dto.required) is bool
    assert dto.mappings == ()
    assert dto.object_types == ("assets.assettype",)
    assert dto.replaced_by == "boundary.replacement"


def test_custom_fieldset_create_round_trips_strings_and_identity_tuple():
    dto = _fieldset_create_input()

    assert dto.namespace == "boundary"
    assert dto.slug == "details"
    assert dto.label == "Details"
    assert dto.description == "Definition details"
    assert dto.field_identities == ("boundary.first", "boundary.second")
    assert dto.replaced_by == "boundary.replacement"


def test_custom_fieldset_update_round_trips_optional_strings():
    dto = _fieldset_update_input()

    assert dto.label == "Updated details"
    assert dto.description == "Updated description"
    assert dto.replaced_by == "boundary.replacement"


def test_choice_set_create_round_trips_strings():
    dto = _choice_set_create_input()

    assert dto.namespace == "boundary"
    assert dto.slug == "states"
    assert dto.label == "States"
    assert dto.replaced_by == "boundary.replacement"


def test_choice_set_update_round_trips_optional_string():
    dto = _choice_set_update_input()

    assert dto.label == "Updated states"
    assert dto.replaced_by == "boundary.replacement"


def test_choice_create_round_trips_positive_ids_and_strings():
    dto = _choice_create_input()

    assert dto.choice_set_id == 7
    assert type(dto.choice_set_id) is int
    assert dto.key == "active"
    assert dto.label == "Active"
    assert dto.position == 2
    assert type(dto.position) is int
    assert dto.replaced_by == "boundary.replacement"


def test_choice_update_round_trips_optional_string_and_positive_position():
    dto = _choice_update_input()

    assert dto.label == "Updated active"
    assert dto.position == 3
    assert type(dto.position) is int
    assert dto.replaced_by == "boundary.replacement"


@pytest.mark.parametrize(
    ("overrides", "expected_exception"),
    [
        pytest.param({"namespace": None}, TypeError, id="namespace-type"),
        pytest.param({"namespace": ""}, ValueError, id="namespace-empty"),
        pytest.param({"local_key": 1}, TypeError, id="local-key-type"),
        pytest.param({"local_key": ""}, ValueError, id="local-key-empty"),
        pytest.param({"label": None}, TypeError, id="label-type"),
        pytest.param({"help_text": None}, TypeError, id="help-text-type"),
        pytest.param({"object_types": ["assets.assettype"]}, TypeError, id="object-types-container"),
        pytest.param({"object_types": (1,)}, ValueError, id="object-types-item-type"),
        pytest.param({"object_types": ("",)}, ValueError, id="object-types-empty-item"),
        pytest.param({"required": 1}, TypeError, id="required-integer"),
        pytest.param({"nullable": 0}, TypeError, id="nullable-integer"),
        pytest.param({"mappings": []}, TypeError, id="mappings-container"),
        pytest.param({"choice_set_id": 0}, ValueError, id="choice-set-id-zero"),
        pytest.param({"choice_set_id": True}, ValueError, id="choice-set-id-bool"),
    ],
)
def test_custom_field_create_rejects_invalid_constructor_values(overrides, expected_exception):
    with pytest.raises(expected_exception):
        _field_create_input(**overrides)


@pytest.mark.parametrize(
    ("overrides", "expected_exception"),
    [
        pytest.param({"label": 1}, TypeError, id="label-type"),
        pytest.param({"help_text": []}, TypeError, id="help-text-type"),
        pytest.param({"required": 0}, TypeError, id="required-integer"),
        pytest.param({"mappings": [True]}, TypeError, id="mappings-container"),
        pytest.param({"object_types": ["assets.assettype"]}, TypeError, id="object-types-container"),
        pytest.param({"object_types": (None,)}, ValueError, id="object-types-item-type"),
        pytest.param({"object_types": ("",)}, ValueError, id="object-types-empty-item"),
    ],
)
def test_custom_field_update_rejects_invalid_constructor_values(overrides, expected_exception):
    with pytest.raises(expected_exception):
        _field_update_input(**overrides)


@pytest.mark.parametrize(
    ("overrides", "expected_exception"),
    [
        pytest.param({"namespace": ""}, ValueError, id="namespace-empty"),
        pytest.param({"slug": ""}, ValueError, id="slug-empty"),
        pytest.param({"label": None}, TypeError, id="label-type"),
        pytest.param({"description": 1}, TypeError, id="description-type"),
        pytest.param({"field_identities": ["boundary.first"]}, TypeError, id="identities-container"),
        pytest.param({"field_identities": ("",)}, ValueError, id="identity-empty"),
        pytest.param({"field_identities": ("boundary.first", "boundary.first")}, ValueError, id="identity-duplicate"),
    ],
)
def test_custom_fieldset_create_rejects_invalid_constructor_values(overrides, expected_exception):
    with pytest.raises(expected_exception):
        _fieldset_create_input(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"label": 1}, id="label-type"),
        pytest.param({"description": []}, id="description-type"),
    ],
)
def test_custom_fieldset_update_rejects_non_string_values(overrides):
    with pytest.raises(TypeError):
        _fieldset_update_input(**overrides)


@pytest.mark.parametrize(
    ("overrides", "expected_exception"),
    [
        pytest.param({"namespace": ""}, ValueError, id="namespace-empty"),
        pytest.param({"slug": ""}, ValueError, id="slug-empty"),
        pytest.param({"label": None}, TypeError, id="label-type"),
    ],
)
def test_choice_set_create_rejects_invalid_constructor_values(overrides, expected_exception):
    with pytest.raises(expected_exception):
        _choice_set_create_input(**overrides)


def test_choice_set_update_rejects_non_string_label():
    with pytest.raises(TypeError):
        _choice_set_update_input(label=1)


@pytest.mark.parametrize(
    ("overrides", "expected_exception"),
    [
        pytest.param({"choice_set_id": 0}, ValueError, id="choice-set-id-zero"),
        pytest.param({"choice_set_id": -1}, ValueError, id="choice-set-id-negative"),
        pytest.param({"choice_set_id": True}, ValueError, id="choice-set-id-bool"),
        pytest.param({"key": ""}, ValueError, id="key-empty"),
        pytest.param({"key": None}, TypeError, id="key-type"),
        pytest.param({"label": 1}, TypeError, id="label-type"),
        pytest.param({"position": 0}, ValueError, id="position-zero"),
        pytest.param({"position": False}, ValueError, id="position-bool"),
    ],
)
def test_choice_create_rejects_invalid_constructor_values(overrides, expected_exception):
    with pytest.raises(expected_exception):
        _choice_create_input(**overrides)


@pytest.mark.parametrize(
    ("overrides", "expected_exception"),
    [
        pytest.param({"label": 1}, TypeError, id="label-type"),
        pytest.param({"position": 0}, ValueError, id="position-zero"),
        pytest.param({"position": True}, ValueError, id="position-bool"),
    ],
)
def test_choice_update_rejects_invalid_constructor_values(overrides, expected_exception):
    with pytest.raises(expected_exception):
        _choice_update_input(**overrides)


def test_create_tuple_defaults_and_explicit_empty_tuples_are_supported():
    field_required = {
        "namespace": "boundary",
        "local_key": "observed",
        "label": "Observed",
        "object_types": ("assets.assettype",),
    }
    omitted_field = CustomFieldCreateInputDTO(**field_required)
    explicit_empty_field = CustomFieldCreateInputDTO(**field_required, mappings=())
    assert omitted_field.mappings == ()
    assert explicit_empty_field.mappings == ()

    omitted_fieldset = CustomFieldsetCreateInputDTO(namespace="boundary", slug="details")
    explicit_empty_fieldset = CustomFieldsetCreateInputDTO(namespace="boundary", slug="details", field_identities=())
    assert omitted_fieldset.field_identities == ()
    assert explicit_empty_fieldset.field_identities == ()


def test_update_optional_tuples_preserve_omission_as_none_vs_empty_tuple():
    omitted = CustomFieldUpdateInputDTO()
    empty_mappings = CustomFieldUpdateInputDTO(mappings=())
    empty_object_types = CustomFieldUpdateInputDTO(object_types=())

    assert omitted.mappings is None
    assert omitted.object_types is None
    assert empty_mappings.mappings == ()
    assert empty_mappings.object_types is None
    assert empty_object_types.mappings is None
    assert empty_object_types.object_types == ()


def test_update_optional_scalars_preserve_omission_as_none():
    assert CustomFieldUpdateInputDTO().label is None
    assert CustomFieldUpdateInputDTO().help_text is None
    assert CustomFieldUpdateInputDTO().required is None
    assert CustomFieldsetUpdateInputDTO().label is None
    assert CustomFieldsetUpdateInputDTO().description is None
    assert CustomFieldChoiceSetUpdateInputDTO().label is None
    assert CustomFieldChoiceUpdateInputDTO().label is None
    assert CustomFieldChoiceUpdateInputDTO().position is None


@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_mapping_values_are_detached_and_deeply_immutable(updating):
    source = {"values": [True, {"nested": ["original"]}], "metadata": {"count": 1}}
    if updating:
        dto = CustomFieldUpdateInputDTO(mappings=(source,))
    else:
        dto = _field_create_input(mappings=(source,))

    source["values"].append(False)
    source["values"][1]["nested"].append("changed")
    source["metadata"]["count"] = 99

    mapping = dto.mappings[0]
    assert type(mapping) is MappingProxyType
    assert type(mapping["values"]) is tuple
    assert mapping["values"][0] is True
    assert type(mapping["values"][0]) is bool
    assert type(mapping["values"][1]) is MappingProxyType
    assert type(mapping["values"][1]["nested"]) is tuple
    assert mapping["values"][1]["nested"] == ("original",)
    assert mapping["metadata"]["count"] == 1
    assert type(mapping["metadata"]["count"]) is int

    with pytest.raises(TypeError):
        mapping["replacement"] = "changed"
    with pytest.raises(TypeError):
        mapping["metadata"]["count"] = 2
    with pytest.raises(TypeError):
        mapping["values"][0] = False
    with pytest.raises(TypeError):
        mapping["values"][1]["nested"][0] = "changed"


@pytest.mark.parametrize(
    "bad_value", [pytest.param({"not-json"}, id="set"), pytest.param(bytearray(b"not-json"), id="bytearray")]
)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_mapping_rejects_non_json_mutable_values(bad_value, updating):
    source = {"payload": bad_value}
    with pytest.raises(TypeError):
        if updating:
            CustomFieldUpdateInputDTO(mappings=(source,))
        else:
            _field_create_input(mappings=(source,))


@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_mapping_rejects_non_string_keys_recursively(updating):
    source = {"nested": {1: "invalid key"}}
    with pytest.raises(TypeError):
        if updating:
            CustomFieldUpdateInputDTO(mappings=(source,))
        else:
            _field_create_input(mappings=(source,))


# ---------------------------------------------------------------------------
# Batch E: the canonical definition contract - basic metadata, numeric and
# text limits, regex safety, validation rules, quantity metadata, choice
# constraints, applicability and mapping shape.
# ---------------------------------------------------------------------------


def _contract_errors(**overrides):
    values = {"field_type": "text"}
    values.update(overrides)
    return {key: str(value) for key, value in custom_field_definition_contract_errors(**values).items()}


@pytest.mark.parametrize(
    "pattern",
    [
        r"^\d{2}$",
        r"^a{,x}$",
        r"^[\\d]+$",
        r"^((ab)+c)$",
        r"^(ab)+c$",
        r"^a(b+)c$",
        r"^(a|b)c$",
        r"^a|b$",
        r"^\d+$",
        r"^a{2,}$",
    ],
)
def test_safe_regex_patterns_pass_the_contract(pattern):
    assert validate_custom_field_regex(pattern) == pattern


@pytest.mark.parametrize(
    "pattern",
    [r"^(a|b)*c$", r"(?i)^abc$", r"^(\w+)\1$", r"\b(\w+)\s+\1\b", r"(a+)+$", r"^\d+.*x*$"],
    ids=[
        "nested_alternation",
        "inline_flags",
        "numeric_backreference",
        "word_backreference",
        "nested_repetition",
        "two_unbounded",
    ],
)
def test_unsafe_regex_patterns_are_rejected(pattern):
    with pytest.raises(ValidationError) as excinfo:
        validate_custom_field_regex(pattern)

    assert excinfo.value.code == "INVALID_REGEX"


@pytest.mark.parametrize("pattern", [123, b"^a$", None, "a" * 257], ids=["int", "bytes", "none", "too_long"])
def test_regex_contract_rejects_non_text_or_oversized_input(pattern):
    with pytest.raises(ValidationError) as excinfo:
        validate_custom_field_regex(pattern)

    assert excinfo.value.code == "INVALID_REGEX"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"field_type": "unsupported"}, {"field_type": "Unsupported custom field type."}),
        ({"activation": "sometimes"}, {"activation": "Unsupported custom field activation."}),
        ({"management_kind": "vendor"}, {"management_kind": "Unsupported management kind."}),
        ({"lifecycle": "retired"}, {"lifecycle": "Unsupported lifecycle."}),
        (
            {"field_type": "integer", "minimum_value": 10, "maximum_value": 5},
            {"maximum_value": "The maximum must not be below the minimum."},
        ),
    ],
)
def test_basic_definition_metadata_errors(overrides, expected):
    assert _contract_errors(**overrides) == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"field_type": "decimal", "decimal_scale": 7},
            {"decimal_scale": "Decimal fields require a scale from 0 to 6."},
        ),
        (
            {"field_type": "decimal", "decimal_scale": True},
            {"decimal_scale": "Decimal fields require a scale from 0 to 6."},
        ),
        ({"field_type": "text", "decimal_scale": 2}, {"decimal_scale": "Only decimal fields may define a scale."}),
        ({"field_type": "text", "text_max_length": 0}, {"text_max_length": "Text length must be between 1 and 4096."}),
        (
            {"field_type": "integer", "text_max_length": 8},
            {"text_max_length": "Only text fields may define a text length."},
        ),
        ({"field_type": "text", "minimum_value": 1}, {"minimum_value": "Only numeric fields may define a minimum."}),
        ({"field_type": "text", "maximum_value": 9}, {"maximum_value": "Only numeric fields may define a maximum."}),
    ],
)
def test_numeric_and_text_metadata_errors(overrides, expected):
    assert _contract_errors(**overrides) == expected


@pytest.mark.parametrize("field_type", ["integer", "boolean", "date", "multi-select"])
def test_regex_is_only_allowed_on_text_fields(field_type):
    errors = _contract_errors(
        field_type=field_type, regex=r"^\d+$", max_values=1 if field_type == "multi-select" else None
    )

    assert errors["regex"] == "Only text fields may define a regular expression."


def test_regex_validation_failures_surface_on_the_definition():
    errors = _contract_errors(field_type="text", regex="(?i)^abc$")

    assert set(errors) == {"regex"}
    assert "Inline regular-expression flags" in errors["regex"]


def test_safe_regex_is_accepted_by_the_full_definition_contract():
    assert validate_custom_field_definition_contract(field_type="text", regex=r"^\d{2}$") is None


def test_unparseable_regex_surfaces_the_compiler_error():
    errors = _contract_errors(field_type="text", regex="^[unterminated$")

    assert set(errors) == {"regex"}
    assert errors["regex"] == "The regular expression is invalid."


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"validation_rule": "positive"}, {"validation_rule": "Unsupported validation rule."}),
        (
            {"validation_rule": "temperature_max_gte_min"},
            {"validation_rule": "The validation rule does not match the field type."},
        ),
        (
            {
                "field_type": "text",
                "validation_rule": "rfc1123_hostname",
                "name": "host",
                "namespace": "itambox",
                "management_kind": "core",
            },
            {},
        ),
    ],
)
def test_validation_rule_errors(overrides, expected):
    assert _contract_errors(**overrides) == expected


def test_cross_field_rule_is_reserved_for_its_registered_core_definition():
    errors = _contract_errors(
        field_type="decimal",
        decimal_scale=2,
        validation_rule="temperature_max_gte_min",
        name="operating_temperature_min",
        namespace="itambox",
        management_kind="core",
    )

    assert errors == {"validation_rule": "This cross-field rule is reserved for its registered Core definition."}


def test_registered_core_definition_may_use_the_cross_field_rule():
    assert (
        validate_custom_field_definition_contract(
            field_type="decimal",
            decimal_scale=2,
            validation_rule="temperature_max_gte_min",
            name="operating_temperature_max",
            namespace="itambox",
            management_kind="core",
        )
        is None
    )


@pytest.mark.parametrize(
    ("rule", "name"),
    [
        ("voltage_max_gte_min", "input_voltage_max"),
        ("runtime_requires_load", "battery_runtime_load"),
    ],
)
def test_registered_core_voltage_and_runtime_rules_are_reserved(rule, name):
    assert (
        validate_custom_field_definition_contract(
            field_type="decimal",
            decimal_scale=3,
            validation_rule=rule,
            name=name,
            namespace="itambox",
            management_kind="core",
        )
        is None
    )
    assert custom_field_definition_contract_errors(
        field_type="decimal",
        decimal_scale=3,
        validation_rule=rule,
        name="user_defined",
        namespace="local",
        management_kind="local",
    ) == {"validation_rule": "This cross-field rule is reserved for its registered Core definition."}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"field_type": "decimal", "decimal_scale": 1, "quantity_kind": "capacity"},
            {"quantity_kind": "Unsupported quantity kind."},
        ),
        (
            {
                "field_type": "decimal",
                "decimal_scale": 1,
                "quantity_kind": "digital_information",
                "canonical_unit": "MB",
            },
            {"canonical_unit": "The unit is not valid for this quantity kind."},
        ),
        (
            {"field_type": "decimal", "decimal_scale": 1, "canonical_unit": "GiB"},
            {"quantity_kind": "A canonical unit requires a quantity kind."},
        ),
        (
            {"field_type": "text", "quantity_kind": "digital_information", "canonical_unit": "GiB"},
            {"quantity_kind": "Only numeric fields may define quantity metadata."},
        ),
        (
            {
                "field_type": "decimal",
                "decimal_scale": 1,
                "quantity_kind": "digital_information",
                "canonical_unit": "GiB",
            },
            {},
        ),
    ],
)
def test_quantity_metadata_errors(overrides, expected):
    assert _contract_errors(**overrides) == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"field_type": "single-select"},
            {
                "choice_set": "Select fields require a Choice Set.",
                "max_values": "Single-select fields must limit values to exactly one.",
            },
        ),
        ({"field_type": "single-select", "choice_set": "active-set", "max_values": 1}, {}),
        (
            {"field_type": "multi-select", "choice_set": "active-set"},
            {"max_values": "Multi-select fields require a maximum value count."},
        ),
        (
            {"field_type": "text", "choice_set": "active-set", "max_values": 2},
            {
                "choice_set": "Only select fields may reference a Choice Set.",
                "max_values": "Only select fields may define a maximum value count.",
            },
        ),
        (
            {"field_type": "multi-select", "choice_set": "active-set", "max_values": 65},
            {"max_values": "The maximum value count must be between 1 and 64."},
        ),
        (
            {"field_type": "single-select", "choice_set": SimpleNamespace(lifecycle="deprecated"), "max_values": 1},
            {"choice_set": "Choice Sets must be active."},
        ),
    ],
)
def test_choice_metadata_errors(overrides, expected):
    assert _contract_errors(**overrides) == expected


def test_applicability_and_mapping_shape_errors():
    assert _contract_errors(object_types=[]) == {"object_types": "Select at least one applicable model."}
    assert _contract_errors(mappings={"source": "target"}) == {"mappings": "Mappings must be a list."}
    assert _contract_errors(object_types=["assets.asset", "assets.assettype"]) == {}
    assert _contract_errors() == {}
    assert custom_field_definition_contract_errors(field_type="text") == {}


def test_definition_contract_validation_raises_with_per_field_errors():
    with pytest.raises(ValidationError) as excinfo:
        validate_custom_field_definition_contract(field_type="multi-select")

    assert set(excinfo.value.message_dict) == {"choice_set", "max_values"}


def _core_vocabulary_contract_kwargs(row):
    validation = row.get("validation", {})
    return {
        "field_type": row["field_type"],
        "activation": row["activation"],
        "quantity_kind": row["quantity_kind"],
        "canonical_unit": row["canonical_unit"],
        "minimum_value": Decimal(validation["minimum"]) if validation.get("minimum") is not None else None,
        "maximum_value": Decimal(validation["maximum"]) if validation.get("maximum") is not None else None,
        "regex": validation.get("regex"),
        "decimal_scale": validation.get("scale"),
        "max_values": validation.get("max_values"),
        "text_max_length": validation.get("max_length"),
        "validation_rule": validation.get("rule"),
        "mappings": [],
        "choice_set": row["choice_set"],
        "object_types": row["targets"],
        "management_kind": "core",
        "lifecycle": row["lifecycle"],
        "required": row["required"],
        "nullable": row["nullable"],
        "name": row["key"],
        "namespace": row["namespace"],
    }


def test_apparent_power_contract_accepts_only_the_vocabulary_unit():
    from extras.definition_contract import QUANTITY_UNITS

    assert QUANTITY_UNITS["apparent_power"] == frozenset({"VA"})
    assert (
        validate_custom_field_definition_contract(
            field_type="decimal",
            quantity_kind="apparent_power",
            canonical_unit="VA",
            decimal_scale=3,
        )
        is None
    )


def test_active_core_vocabulary_definitions_pass_definition_contract():
    from assets.services.specifications.core_vocabulary import get_core_vocabulary

    active_fields = get_core_vocabulary()["active_fields"]
    assert isinstance(active_fields, list)
    for row in active_fields:
        try:
            validate_custom_field_definition_contract(**_core_vocabulary_contract_kwargs(row))
        except ValidationError as exc:
            pytest.fail(f"Core vocabulary field {row['identity']} violates its definition contract: {exc.message_dict}")


def test_core_field_seed_rejects_invalid_definition_before_bulk_insert():
    from assets.services.specifications.core_vocabulary import get_core_vocabulary
    from core.management.commands._seed.catalog import _reconcile_core_fields
    from extras.models import CustomField

    active_fields = get_core_vocabulary()["active_fields"]
    assert isinstance(active_fields, list)
    apparent_power = next(row for row in active_fields if row["key"] == "apparent_power")
    invalid_row = {**apparent_power, "quantity_kind": "unsupported_kind"}

    with (
        patch.object(CustomField.objects, "filter", return_value=[]),
        patch.object(CustomField.objects, "bulk_create", side_effect=RuntimeError("bulk_create reached")) as create,
    ):
        with pytest.raises(ValidationError, match="Unsupported quantity kind"):
            _reconcile_core_fields([invalid_row], {}, SimpleNamespace(), SimpleNamespace(), 1)

    create.assert_not_called()
