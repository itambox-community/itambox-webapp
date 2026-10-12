import copy
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

import jsonschema
from django.core.management import call_command
from django.test import SimpleTestCase

from assets.services.specifications.catalogue_schema import (
    SCHEMA_PATH,
    build_catalogue_schema,
    render_catalogue_schema,
)
from assets.services.specifications.core_vocabulary import get_core_vocabulary

VALID = {
    "manufacturer": "Lenovo",
    "model": "ThinkPad T14",
    "part_number": "20XK",
    "ean": "1234567890123",
    "category": "laptops",
    "description": "Business laptop",
    "specs": {
        "form_factor": "notebook",
        "battery_capacity": "50.5 Wh",
        "core_count": 8,
        "operating_temperature_min": "-10 °C",
    },
}


class CatalogueSchemaDriftTests(SimpleTestCase):
    def test_committed_schema_matches_vocabulary(self):
        self.assertEqual(
            SCHEMA_PATH.read_bytes().decode("utf-8"),
            render_catalogue_schema(),
            "generated_schema.json is stale: run manage.py export_catalogue_schema",
        )

    def test_output_is_deterministic(self):
        self.assertEqual(render_catalogue_schema(), render_catalogue_schema())

    def test_command_writes_identical_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            call_command("export_catalogue_schema", output=str(target), stdout=io.StringIO())
            first = target.read_bytes()
            call_command("export_catalogue_schema", output=str(target), stdout=io.StringIO())
            self.assertEqual(first, target.read_bytes())
        self.assertEqual(first, SCHEMA_PATH.read_bytes())

    def test_vocabulary_changes_alter_the_schema(self):
        base = render_catalogue_schema()
        mutations = {
            "unit": lambda v: next(f for f in v["active_fields"] if f["key"] == "battery_capacity").update(
                canonical_unit="kWh"
            ),
            "choice": lambda v: v["choice_sets"][0]["choices"].append({"key": "zzz"}),
            "category": lambda v: v["categories"].append({"slug": "zzz"}),
            "field": lambda v: v["active_fields"].append({**v["active_fields"][0], "key": "zzz_new"}),
        }
        for name, mutate in mutations.items():
            vocabulary = get_core_vocabulary()
            mutate(vocabulary)
            with (
                self.subTest(name),
                mock.patch(
                    "assets.services.specifications.catalogue_schema.get_core_vocabulary", return_value=vocabulary
                ),
            ):
                self.assertNotEqual(base, render_catalogue_schema())


class CatalogueSchemaValidationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(cls.schema)

    def errors(self, document):
        return [e.message + " @" + "/".join(map(str, e.absolute_path)) for e in self.validator.iter_errors(document)]

    def _broken(self, mutate):
        document = copy.deepcopy(VALID)
        mutate(document)
        return self.errors(document)

    def test_schema_is_valid_2020_12(self):
        jsonschema.Draft202012Validator.check_schema(self.schema)
        self.assertEqual(self.schema, build_catalogue_schema())

    def test_valid_sample_passes(self):
        self.assertEqual(self.errors(VALID), [])

    def test_wrong_unit_fails_naming_key(self):
        errors = self._broken(lambda d: d["specs"].update(battery_capacity="50 kWh"))
        self.assertTrue(any("battery_capacity" in e for e in errors), errors)

    def test_unknown_spec_key_fails_naming_key(self):
        errors = self._broken(lambda d: d["specs"].update(flux_capacitor="1"))
        self.assertTrue(any("flux_capacitor" in e for e in errors), errors)

    def test_unknown_category_fails_naming_key(self):
        errors = self._broken(lambda d: d.update(category="toasters"))
        self.assertTrue(any("toasters" in e and "category" in e for e in errors), errors)

    def test_extra_top_level_key_fails_naming_key(self):
        errors = self._broken(lambda d: d.update(colour="red"))
        self.assertTrue(any("colour" in e for e in errors), errors)

    def test_bad_choice_fails(self):
        errors = self._broken(lambda d: d["specs"].update(form_factor="spaceship"))
        self.assertTrue(any("form_factor" in e for e in errors), errors)
