"""PostgreSQL contract tests for the current canonical vocabulary consumer."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from assets.models import AssetType, Category, CategoryDefaultFieldset, Manufacturer
from assets.services.specifications.core_vocabulary import get_core_vocabulary
from core.management.commands.seed_data import Command as SeedDataCommand
from extras.customfields import validate_custom_field_data_owner
from extras.models import CustomField, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField

pytestmark = pytest.mark.django_db(transaction=True)

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "scripts" / "tests" / "fixtures" / "specification_vocabulary"


def _load_fixture(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _identity_slug(identity):
    return identity.rsplit("/", 1)[1]


def _expected_object_type_models(targets):
    return {"assettype" if target == "asset_type" else target for target in targets}


def _seed_catalog():
    command = SeedDataCommand(stdout=io.StringIO(), stderr=io.StringIO())
    command._seed_catalog()
    return command


def test_seeded_asset_types_use_only_current_canonical_keys():
    command = _seed_catalog()
    vocabulary_keys = {row["key"] for row in get_core_vocabulary()["active_fields"]}
    retired_or_unsupported_demo_keys = {
        "cpu",
        "ram_gb",
        "storage_gb",
        "storage_type",
        "screen_size",
        "port_count",
        "poe_budget_w",
        "input_voltage",
        "gpu",
        "cpu_architecture",
    }

    asset_types = list(command._asset_types.values())
    assert len(asset_types) == 23
    for asset_type in asset_types:
        values = asset_type.custom_field_data
        assert not (set(values) & retired_or_unsupported_demo_keys), asset_type.slug
        assert set(values) <= vocabulary_keys, asset_type.slug
        assert "nvme_ssd" not in values.values(), asset_type.slug
        if values.get("storage_interface") == "nvme":
            assert values.get("storage_medium") == "ssd", asset_type.slug

    categories = {asset_type.slug: asset_type.category.slug for asset_type in asset_types}
    assert categories["cisco-catalyst-9300"] == "switches"
    assert categories["unifi-switch-pro-48"] == "switches"
    assert categories["meraki-mr46"] == "access-points"
    assert categories["unifi-dream-machine-pro"] == "routers"
    assert categories["synology-ds1823xs"] == "storage-devices"
    assert categories["logitech-rally-bar"] == "conference-systems"


def test_seeded_asset_type_values_validate_against_deterministic_category_defaults():
    command = _seed_catalog()
    active_keys = {row["key"] for row in get_core_vocabulary()["active_fields"]}

    for asset_type in command._asset_types.values():
        category_defaults = list(
            asset_type.category.default_fieldset_memberships.values_list("fieldset__slug", "position")
        )
        type_fieldsets = list(asset_type.fieldset_memberships.values_list("fieldset__slug", "position"))

        assert type_fieldsets == category_defaults, asset_type.slug
        assert set(asset_type.custom_field_data) <= active_keys, asset_type.slug
        assert "input_voltage" not in asset_type.custom_field_data, asset_type.slug
        assert asset_type.custom_field_data.get("storage_medium") != "nvme_ssd", asset_type.slug
        validate_custom_field_data_owner(asset_type)


def test_runtime_seed_refuses_local_fieldset_membership_collision():
    _seed_catalog()
    fieldset = CustomFieldset.objects.get(namespace="itambox", slug="compute-memory")
    local_field = CustomField.objects.create(
        name="runtime_local_field",
        namespace="local",
        label="Runtime local field",
        field_type="text",
        activation="composed",
        management_kind="local",
        lifecycle="active",
    )
    membership = CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=local_field, position=999)

    with pytest.raises(ValueError, match="Core fieldset membership ownership collision"):
        _seed_catalog()

    assert CustomFieldsetField.objects.filter(pk=membership.pk).exists()


def test_runtime_seed_refuses_local_category_default_collision():
    _seed_catalog()
    local_fieldset = CustomFieldset.objects.create(
        namespace="local",
        slug="runtime-local-default",
        label="Runtime local default",
        description="Local default must not be deleted.",
        management_kind="local",
        lifecycle="active",
    )
    category = Category.objects.get(slug="laptops")
    membership = CategoryDefaultFieldset.objects.create(category=category, fieldset=local_fieldset, position=999)

    with pytest.raises(ValueError, match="Core category default ownership collision"):
        _seed_catalog()

    assert CategoryDefaultFieldset.objects.filter(pk=membership.pk).exists()


def test_runtime_seed_refuses_to_overwrite_existing_local_demo_composition():
    _seed_catalog()
    category = Category.objects.get(slug="storage-devices")
    category.default_fieldset_memberships.all().delete()
    unexpected_fieldset = CustomFieldset.objects.get(namespace="itambox", slug="compute-memory")
    membership = CategoryDefaultFieldset.objects.create(
        category=category,
        fieldset=unexpected_fieldset,
        position=10,
    )

    with pytest.raises(ValueError, match="Local demo category default composition collision: storage-devices"):
        _seed_catalog()

    assert CategoryDefaultFieldset.objects.filter(pk=membership.pk).exists()


def _assert_field_matches(field, expected):
    validation = expected["validation"]
    assert field.namespace == expected["namespace"]
    assert field.label == expected["label"]
    assert field.help_text == expected["help_text"]
    assert field.activation == expected["activation"]
    assert field.field_type == expected["field_type"]
    assert field.quantity_kind == expected["quantity_kind"]
    assert field.canonical_unit == expected["canonical_unit"]
    assert field.required is expected["required"]
    assert field.nullable is expected["nullable"]
    assert field.lifecycle == expected["lifecycle"]
    assert set(field.object_types.values_list("model", flat=True)) == _expected_object_type_models(expected["targets"])
    assert field.choice_set_id == (
        CustomFieldChoiceSet.objects.get(namespace="itambox", slug=_identity_slug(expected["choice_set"])).pk
        if expected["choice_set"]
        else None
    )

    assert field.minimum_value == (Decimal(validation["minimum"]) if "minimum" in validation else None)
    assert field.maximum_value == (Decimal(validation["maximum"]) if "maximum" in validation else None)
    assert field.regex == validation.get("regex")
    assert field.decimal_scale == validation.get("scale")
    assert field.max_values == validation.get("max_values")
    assert field.text_max_length == validation.get("max_length")
    assert field.validation_rule == validation.get("rule")


def test_runtime_seed_matches_complete_normalized_vocabulary():
    canonical = _load_fixture("canonical-target.json")
    runtime_vocabulary = get_core_vocabulary()
    _seed_catalog()

    assert runtime_vocabulary["label"] == canonical["label"]
    assert runtime_vocabulary["expected_counts"] == canonical["expected_counts"]
    expected_active = {row["key"]: row for row in canonical["active_fields"]}
    expected_retired = {row["key"]: row for row in canonical["reserved_retired_fields"]}
    expected_fields = {**expected_active, **expected_retired}
    runtime_fields = {
        field.name: field
        for field in CustomField.objects.filter(namespace="itambox", management_kind=CustomField.MANAGEMENT_CORE)
    }

    assert set(runtime_fields) == set(expected_fields)
    assert set(field.name for field in runtime_fields.values() if field.lifecycle == "active") == set(expected_active)
    assert set(field.name for field in runtime_fields.values() if field.lifecycle == "deprecated") == set(
        expected_retired
    )
    for key, expected in expected_fields.items():
        _assert_field_matches(runtime_fields[key], expected)

    expected_sections = {row["identity"]: row for row in canonical["sections"]}
    runtime_sections = {
        f"{fieldset.namespace}/{fieldset.slug}": fieldset
        for fieldset in CustomFieldset.objects.filter(
            namespace="itambox", management_kind=CustomFieldset.MANAGEMENT_CORE
        )
    }
    assert set(runtime_sections) == set(expected_sections)
    for identity, expected in expected_sections.items():
        fieldset = runtime_sections[identity]
        assert fieldset.label == expected["label"]
        assert fieldset.description == expected["description"]
        assert fieldset.lifecycle == expected["lifecycle"]
        assert list(fieldset.field_memberships.values_list("custom_field__name", "position")) == [
            (_identity_slug(member["field"]), member["position"]) for member in expected["memberships"]
        ]

    expected_choice_sets = {row["identity"]: row for row in canonical["choice_sets"]}
    runtime_choice_sets = {
        f"{choice_set.namespace}/{choice_set.slug}": choice_set
        for choice_set in CustomFieldChoiceSet.objects.filter(namespace="itambox")
    }
    assert set(runtime_choice_sets) == set(expected_choice_sets)
    for identity, expected in expected_choice_sets.items():
        choice_set = runtime_choice_sets[identity]
        assert choice_set.label == expected["label"]
        assert choice_set.lifecycle == expected["lifecycle"]
        assert list(choice_set.choices.values_list("key", "label", "position", "lifecycle")) == [
            (choice["key"], choice["label"], choice["position"], choice["lifecycle"]) for choice in expected["choices"]
        ]


def test_runtime_seed_matches_category_defaults_and_preserves_local_categories():
    canonical = _load_fixture("canonical-target.json")
    foundation = _load_fixture("foundation-baseline.json")
    _seed_catalog()

    expected_categories = {row["identity"]: row for row in canonical["categories"]}
    canonical_slugs = {_identity_slug(identity) for identity in expected_categories}
    for _identity, expected in expected_categories.items():
        category = Category.objects.get(slug=expected["slug"])
        assert category.name == expected["label"]
        assert category.description == expected["description"]
        assert category.applies_to == {target: True for target in expected["applies_to"]}
        assert list(
            category.default_fieldset_memberships.values_list("fieldset__namespace", "fieldset__slug", "position")
        ) == [("itambox", _identity_slug(item["fieldset"]), item["position"]) for item in expected["default_fieldsets"]]

    foundation_categories = {row["identity"]: row for row in foundation["categories"]}
    local_categories = foundation_categories.keys() - expected_categories.keys()
    assert local_categories
    for identity in local_categories:
        expected = foundation_categories[identity]
        category = Category.objects.get(slug=expected["slug"])
        assert category.name == expected["label"]
        assert category.color == expected["color"]
        assert category.applies_to == {target: True for target in expected["applies_to"]}
        assert category.slug not in canonical_slugs

    expected_local_defaults = {
        "storage-devices": [
            "product-physical",
            "compute-memory",
            "storage",
            "connectivity-io",
            "power-battery",
            "management-security",
            "compliance-sustainability",
        ],
        "conference-systems": [
            "product-physical",
            "connectivity-io",
            "power-battery",
            "display-av-imaging",
            "management-security",
            "compliance-sustainability",
        ],
    }
    for slug, expected_fieldsets in expected_local_defaults.items():
        category = Category.objects.get(slug=slug)
        category_defaults = list(category.default_fieldset_memberships.values_list("fieldset__slug", "position"))
        assert category_defaults == [
            (fieldset_slug, (index + 1) * 10) for index, fieldset_slug in enumerate(expected_fieldsets)
        ]
    assert not AssetType.objects.filter(category__slug="network-devices").exists()


def test_runtime_seed_is_idempotent_without_catalogue_duplication_or_type_propagation():
    _seed_catalog()
    first_counts = {
        "asset_types": AssetType.objects.count(),
        "manufacturers": Manufacturer.objects.count(),
        "categories": Category.objects.count(),
    }
    first_type_memberships = {
        asset_type.slug: list(asset_type.fieldset_memberships.values_list("fieldset__slug", "position"))
        for asset_type in AssetType.objects.all()
    }

    _seed_catalog()

    assert AssetType.objects.count() == first_counts["asset_types"]
    assert Manufacturer.objects.count() == first_counts["manufacturers"]
    assert Category.objects.count() == first_counts["categories"]
    assert {
        asset_type.slug: list(asset_type.fieldset_memberships.values_list("fieldset__slug", "position"))
        for asset_type in AssetType.objects.all()
    } == first_type_memberships
