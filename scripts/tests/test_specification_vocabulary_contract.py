"""Self-contained vocabulary contract and foundation-delta checks.

The fixtures in ``fixtures/specification_vocabulary`` are intentionally static:
this test does not import Django or runtime seed code.  It therefore remains a
lightweight review gate for the canonical source inventory and the read-only
foundation snapshot captured by T03.
"""

import json
import unittest
from pathlib import Path

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "specification_vocabulary"

EXPECTED_ADDED_FIELDS = {
    "apparent_power",
    "battery_runtime_load",
    "input_voltage_max",
    "input_voltage_min",
    "power_output",
    "storage_interface",
    "vcpu_count",
}
EXPECTED_ASSET_TARGET_FIELDS = {
    "battery_capacity",
    "battery_runtime",
    "battery_runtime_load",
    "core_count",
    "firmware_version",
    "hostname",
    "memory_capacity",
    "memory_type",
    "operating_system_family",
    "processor_model",
    "storage_capacity",
    "storage_interface",
    "storage_medium",
    "vcpu_count",
}
# The foundation snapshot has the scalar voltage identity; the final contract keeps only min/max.
EXPECTED_REMOVED_FOUNDATION_FIELDS = {"input_voltage"}
EXPECTED_ADDED_SECTION = "itambox/virtual-compute"
EXPECTED_ADDED_CHOICE_SET = "itambox/storage-interface"
EXPECTED_ADDED_CATEGORIES = {
    "catalog/access-points",
    "catalog/firewalls",
    "catalog/printers",
    "catalog/routers",
    "catalog/switches",
    "catalog/ups",
    "catalog/virtual-machines",
}
EXPECTED_EXISTING_STARTER_CATEGORIES = {
    "catalog/desktops",
    "catalog/laptops",
    "catalog/mobile-phones",
    "catalog/monitors",
    "catalog/servers",
    "catalog/tablets",
}
EXPECTED_LOCAL_ONLY_CATEGORIES = {
    "catalog/adaptor",
    "catalog/batteries",
    "catalog/cable",
    "catalog/charger",
    "catalog/conference-systems",
    "catalog/cpu",
    "catalog/display",
    "catalog/dock",
    "catalog/gpu",
    "catalog/hdd",
    "catalog/headset",
    "catalog/ink",
    "catalog/keyboard",
    "catalog/mouse",
    "catalog/network-devices",
    "catalog/nic",
    "catalog/other",
    "catalog/ram-memory",
    "catalog/ssd-nvme",
    "catalog/storage-devices",
    "catalog/thermal-paste",
    "catalog/toner",
    "catalog/webcam",
}


class SpecificationVocabularyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canonical = json.loads((FIXTURE_DIR / "canonical-target.json").read_text(encoding="utf-8"))
        cls.foundation = json.loads((FIXTURE_DIR / "foundation-baseline.json").read_text(encoding="utf-8"))
        cls.active_fields = {field["key"]: field for field in cls.canonical["active_fields"]}
        cls.retired_fields = {field["key"]: field for field in cls.canonical["reserved_retired_fields"]}
        cls.sections = {section["identity"]: section for section in cls.canonical["sections"]}
        cls.choice_sets = {choice_set["identity"]: choice_set for choice_set in cls.canonical["choice_sets"]}
        cls.categories = {category["identity"]: category for category in cls.canonical["categories"]}

    def test_complete_inventory_counts_and_identity_sets(self):
        self.assertEqual(
            self.canonical["expected_counts"],
            {
                "active_fields": 54,
                "reserved_retired_fields": 0,
                "sections": 13,
                "choice_sets": 14,
                "categories": 13,
            },
        )
        for source, index in [
            ("active_fields", self.active_fields),
            ("reserved_retired_fields", self.retired_fields),
            ("sections", self.sections),
            ("choice_sets", self.choice_sets),
            ("categories", self.categories),
        ]:
            self.assertEqual(len(self.canonical[source]), len(index), source)
        self.assertEqual(len(self.active_fields), 54)
        self.assertEqual(len(self.retired_fields), 0)
        self.assertEqual(len(self.sections), 13)
        self.assertEqual(len(self.choice_sets), 14)
        self.assertEqual(len(self.categories), 13)
        self.assertEqual(set(self.retired_fields), set())
        self.assertEqual(set(self.active_fields) & set(self.retired_fields), set())
        self.assertEqual(
            set(self.active_fields) - set(self.foundation_field_keys()),
            EXPECTED_ADDED_FIELDS,
        )

    def foundation_field_keys(self):
        return {field["key"] for field in self.foundation["fields"]}

    def test_field_metadata_targets_activation_and_requiredness(self):
        self.assertEqual(
            {field["key"] for field in self.canonical["active_fields"] if "asset" in field["targets"]},
            EXPECTED_ASSET_TARGET_FIELDS,
        )
        for field in self.canonical["active_fields"]:
            with self.subTest(field=field["key"]):
                self.assertEqual(field["identity"], f"itambox/{field['key']}")
                self.assertEqual(field["namespace"], "itambox")
                self.assertEqual(field["activation"], "composed")
                self.assertEqual(field["lifecycle"], "active")
                self.assertFalse(field["required"])
                self.assertFalse(field["nullable"])
                self.assertTrue(set(field["targets"]) <= {"asset_type", "asset"})
                self.assertGreaterEqual(len(field["memberships"]), 1)
        self.assertNotIn("input_voltage", self.active_fields)
        self.assertEqual(self.canonical["label"], "ITAMbox Core Vocabulary v1")

    def test_section_membership_is_ordered_complete_and_cross_referenced(self):
        member_keys = []
        for section in self.canonical["sections"]:
            self.assertEqual(section["identity"], f"itambox/{section['slug']}")
            self.assertEqual(section["lifecycle"], "active")
            positions = [member["position"] for member in section["memberships"]]
            self.assertEqual(positions, list(range(10, (len(positions) + 1) * 10, 10)))
            for member in section["memberships"]:
                with self.subTest(section=section["identity"], field=member["field"]):
                    self.assertIn(member["field"].rsplit("/", 1)[1], self.active_fields)
                    self.assertEqual(member["field"].split("/", 1)[0], "itambox")
                    member_keys.append(member["field"].rsplit("/", 1)[1])
        self.assertEqual(len(member_keys), 55)
        self.assertEqual(member_keys.count("memory_capacity"), 2)
        self.assertEqual(set(member_keys), set(self.active_fields))
        for field in self.canonical["active_fields"]:
            expected = {(membership["section"], membership["position"]) for membership in field["memberships"]}
            actual = {
                (section["identity"], member["position"])
                for section in self.canonical["sections"]
                for member in section["memberships"]
                if member["field"] == field["identity"]
            }
            self.assertEqual(actual, expected)
        self.assertEqual(
            self.active_fields["memory_capacity"]["memberships"],
            [
                {
                    "section": "itambox/compute-memory",
                    "order": 3,
                    "position": 30,
                },
                {
                    "section": "itambox/virtual-compute",
                    "order": 2,
                    "position": 20,
                },
            ],
        )

    def test_choice_sets_and_choice_lifecycle_are_closed(self):
        deprecated = []
        for choice_set in self.canonical["choice_sets"]:
            self.assertEqual(choice_set["identity"], f"itambox/{choice_set['slug']}")
            self.assertEqual(choice_set["lifecycle"], "active")
            keys = [choice["key"] for choice in choice_set["choices"]]
            self.assertEqual(len(keys), len(set(keys)))
            positions = [choice["position"] for choice in choice_set["choices"]]
            self.assertEqual(positions, list(range(10, (len(positions) + 1) * 10, 10)))
            for choice in choice_set["choices"]:
                if choice["lifecycle"] == "deprecated":
                    deprecated.append(f"{choice_set['identity']}#{choice['key']}")
                else:
                    self.assertEqual(choice["lifecycle"], "active")
        self.assertEqual(deprecated, [])
        storage_medium_choices = {choice["key"] for choice in self.choice_sets["itambox/storage-medium"]["choices"]}
        storage_interface_choices = {
            choice["key"] for choice in self.choice_sets["itambox/storage-interface"]["choices"]
        }
        self.assertIn("ssd", storage_medium_choices)
        self.assertNotIn("nvme_ssd", storage_medium_choices)
        self.assertIn("nvme", storage_interface_choices)
        for field in self.canonical["active_fields"]:
            if field["field_type"] in {"single-select", "multi-select"}:
                self.assertIn(field["choice_set"], self.choice_sets)
                self.assertEqual(self.choice_sets[field["choice_set"]]["lifecycle"], "active")
        self.assertEqual(self.active_fields["storage_interface"]["choice_set"], EXPECTED_ADDED_CHOICE_SET)
        multi_fields = [field for field in self.canonical["active_fields"] if field["field_type"] == "multi-select"]
        for field in multi_fields:
            self.assertLessEqual(field["validation"]["max_values"], 64)
        self.assertEqual(
            self.canonical["value_write_policy"]["multi_select_storage_order"],
            "lexicographic_key",
        )

    def test_units_and_validation_metadata_are_coherent(self):
        for field in self.canonical["active_fields"]:
            validation = field["validation"]
            with self.subTest(field=field["key"]):
                if field["field_type"] == "decimal":
                    self.assertIsNotNone(field["quantity_kind"])
                    self.assertIsNotNone(field["canonical_unit"])
                    self.assertIn("minimum", validation)
                    self.assertIn("maximum", validation)
                    self.assertIn("scale", validation)
                elif field["field_type"] == "integer":
                    self.assertIn(field["quantity_kind"], {"count", "duration"})
                    if field["key"] == "battery_runtime":
                        self.assertEqual(field["quantity_kind"], "duration")
                        self.assertEqual(field["canonical_unit"], "min")
                    else:
                        self.assertEqual(field["quantity_kind"], "count")
                        self.assertIsNone(field["canonical_unit"])
                    self.assertIn("minimum", validation)
                    self.assertIn("maximum", validation)
                elif field["field_type"] in {"single-select", "multi-select"}:
                    self.assertIsNone(field["quantity_kind"])
                    self.assertIsNone(field["canonical_unit"])
                    self.assertIn("max_values", validation)
                elif field["field_type"] in {"text", "boolean"}:
                    self.assertIsNone(field["quantity_kind"])
                    self.assertIsNone(field["canonical_unit"])
        self.assertEqual(
            self.active_fields["input_voltage_max"]["validation"]["rule"],
            "voltage_max_gte_min",
        )
        self.assertEqual(
            self.active_fields["battery_runtime_load"]["validation"]["rule"],
            "runtime_requires_load",
        )
        self.assertEqual(
            self.active_fields["operating_temperature_max"]["validation"]["rule"],
            "temperature_max_gte_min",
        )

    def test_category_defaults_are_ordered_and_reference_existing_sections(self):
        self.assertEqual(set(self.categories), EXPECTED_EXISTING_STARTER_CATEGORIES | EXPECTED_ADDED_CATEGORIES)
        for category in self.canonical["categories"]:
            self.assertEqual(category["identity"], f"catalog/{category['slug']}")
            self.assertEqual(category["namespace"], "catalog")
            self.assertEqual(category["lifecycle"], "active")
            self.assertEqual(category["applies_to"], ["asset"])
            positions = [item["position"] for item in category["default_fieldsets"]]
            self.assertEqual(positions, list(range(10, (len(positions) + 1) * 10, 10)))
            for item in category["default_fieldsets"]:
                self.assertIn(item["fieldset"], self.sections)
        self.assertNotIn(
            "itambox/virtual-compute",
            [
                item["fieldset"]
                for category in self.canonical["categories"]
                if category["identity"] != "catalog/virtual-machines"
                for item in category["default_fieldsets"]
            ],
        )

    def test_final_delta_is_explicit_against_foundation_snapshot(self):
        foundation_fields = self.foundation_field_keys()
        canonical_active = set(self.active_fields)
        self.assertEqual(len(foundation_fields), 48)
        self.assertEqual(len(canonical_active & foundation_fields), 47)
        self.assertEqual(canonical_active - foundation_fields, EXPECTED_ADDED_FIELDS)
        self.assertEqual(foundation_fields - canonical_active, EXPECTED_REMOVED_FOUNDATION_FIELDS)
        foundation_choice_sets = {item["identity"] for item in self.foundation["choice_sets"]}
        canonical_choice_sets = set(self.choice_sets)
        self.assertEqual(len(foundation_choice_sets), 13)
        self.assertEqual(canonical_choice_sets - foundation_choice_sets, {EXPECTED_ADDED_CHOICE_SET})
        foundation_sections = {item["identity"] for item in self.foundation["sections"]}
        self.assertEqual(len(foundation_sections), 12)
        self.assertEqual(set(self.sections), foundation_sections | {EXPECTED_ADDED_SECTION})
        foundation_categories = {item["identity"] for item in self.foundation["categories"]}
        self.assertEqual(len(foundation_categories), 29)
        self.assertEqual(
            set(self.categories) & foundation_categories,
            EXPECTED_EXISTING_STARTER_CATEGORIES,
        )
        self.assertEqual(set(self.categories) - foundation_categories, EXPECTED_ADDED_CATEGORIES)
        self.assertEqual(foundation_categories - set(self.categories), EXPECTED_LOCAL_ONLY_CATEGORIES)

    def test_existing_values_and_non_asset_collision_surface_are_explicit(self):
        inventory = self.foundation["demo_value_inventory"]
        self.assertEqual(len(inventory["records"]), 23)
        self.assertEqual(inventory["raw_key_counts"]["ram_gb"], 16)
        self.assertEqual(inventory["raw_key_counts"]["storage_gb"], 16)
        self.assertEqual(inventory["raw_key_counts"]["storage_type"], 12)
        self.assertEqual(inventory["written_key_counts"]["storage_medium"], 12)
        self.assertEqual(
            inventory["special_observations"][0]["current_written_value"],
            "nvme_ssd",
        )
        self.assertEqual(
            inventory["special_observations"][0]["records"],
            8,
        )
        voltage = next(item for item in inventory["special_observations"] if item["source_key"] == "input_voltage")
        self.assertEqual(voltage["records_in_current_demo_seed"], 0)
        self.assertEqual(voltage["value_action"], "do_not_split_or_infer_input_voltage_min_max")
        self.assertEqual(len(self.foundation["custom_field_data_owners"]), 13)
        non_asset = [owner for owner in self.foundation["custom_field_data_owners"] if not owner["target_names"]]
        self.assertEqual(len(non_asset), 11)
        collision_resources = {probe["resource"] for probe in self.foundation["collision_probes"]}
        self.assertEqual(collision_resources, {"field", "fieldset", "choice_set", "category"})
        self.assertEqual(
            set(self.foundation["collision_probes"][0]),
            {"resource", "identity", "namespace", "current_behavior", "evidence", "target_rule"},
        )


if __name__ == "__main__":
    unittest.main()
