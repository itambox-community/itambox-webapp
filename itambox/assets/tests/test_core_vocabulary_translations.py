import importlib
import io
import unittest
from copy import deepcopy
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from assets.migrations._core_vocabulary import CORE_VOCABULARY as MIGRATION_CORE_VOCABULARY
from assets.services.specifications.core_vocabulary import get_core_vocabulary
from assets.services.specifications.core_vocabulary_translations import (
    CoreVocabularyTranslationDrift,
    get_core_vocabulary_translations,
    seed_core_vocabulary_translations,
)
from core.management.commands.seed_data import Command as SeedDataCommand
from extras.models import (
    CustomField,
    CustomFieldChoiceSet,
    CustomFieldChoiceTranslation,
    CustomFieldset,
    CustomFieldsetTranslation,
    CustomFieldTranslation,
)


class CoreVocabularyTranslationLockstepTests(unittest.TestCase):
    def test_runtime_and_migration_vocabulary_mirrors_match(self):
        runtime_vocabulary = get_core_vocabulary()
        for key in (
            "active_fields",
            "categories",
            "choice_sets",
            "expected_counts",
            "reserved_retired_fields",
            "schema_version",
            "sections",
            "value_write_policy",
        ):
            with self.subTest(part=key):
                self.assertEqual(runtime_vocabulary[key], MIGRATION_CORE_VOCABULARY[key])

    def test_translation_tables_cover_every_shipped_entry(self):
        vocabulary = get_core_vocabulary()
        translations = get_core_vocabulary_translations("de")
        fields = vocabulary["active_fields"] + vocabulary["reserved_retired_fields"]

        self.assertEqual(set(translations["fields"]), {row["identity"] for row in fields})
        self.assertEqual(set(translations["sections"]), {row["identity"] for row in vocabulary["sections"]})
        self.assertEqual(set(translations["choice_sets"]), {row["identity"] for row in vocabulary["choice_sets"]})
        for choice_set in vocabulary["choice_sets"]:
            self.assertEqual(
                set(translations["choice_sets"][choice_set["identity"]]["choices"]),
                {choice["key"] for choice in choice_set["choices"]},
            )
        for values in translations["fields"].values():
            self.assertTrue(values["label"].strip())
            self.assertTrue(values["help_text"].strip())
        for values in translations["sections"].values():
            self.assertTrue(values["label"].strip())
            self.assertTrue(values["description"].strip())
        for values in translations["choice_sets"].values():
            self.assertTrue(all(label.strip() for label in values["choices"].values()))

    def test_untranslated_vocabulary_addition_fails_loudly(self):
        vocabulary = get_core_vocabulary()
        added_field = deepcopy(vocabulary["active_fields"][0])
        added_field["key"] = "new_untranslated_field"
        added_field["identity"] = "itambox/new_untranslated_field"
        vocabulary["active_fields"].append(added_field)

        with self.assertRaisesRegex(CoreVocabularyTranslationDrift, "fields.*new_untranslated_field"):
            get_core_vocabulary_translations("de", vocabulary=vocabulary)

    def test_new_choice_requires_an_explicit_translation_or_keep_entry(self):
        vocabulary = get_core_vocabulary()
        vocabulary["choice_sets"][0]["choices"].append({"key": "new_untranslated_choice", "label": "New choice"})

        with self.assertRaisesRegex(CoreVocabularyTranslationDrift, "new_untranslated_choice"):
            get_core_vocabulary_translations("de", vocabulary=vocabulary)

    def test_new_word_like_unit_requires_a_translation(self):
        vocabulary = get_core_vocabulary()
        vocabulary["active_fields"][0]["canonical_unit"] = "items_per_second"

        with self.assertRaisesRegex(CoreVocabularyTranslationDrift, "word-like unit tokens"):
            get_core_vocabulary_translations("de", vocabulary=vocabulary)


class CoreVocabularyGermanMigrationRowsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        SeedDataCommand(stdout=io.StringIO(), stderr=io.StringIO())._seed_catalog()

    def _assert_german_rows_for_all_shipped_vocabulary(self):
        vocabulary = get_core_vocabulary()
        expected = get_core_vocabulary_translations("de")

        for row in vocabulary["active_fields"] + vocabulary["reserved_retired_fields"]:
            with self.subTest(kind="field", identity=row["identity"]):
                field = CustomField.objects.get(namespace="itambox", name=row["key"])
                translated = field.translations.get(locale="de")
                values = expected["fields"][row["identity"]]
                self.assertEqual(
                    (translated.label, translated.help_text, translated.unit_label),
                    (
                        values["label"],
                        values["help_text"],
                        values["unit_label"],
                    ),
                )

        for row in vocabulary["sections"]:
            with self.subTest(kind="section", identity=row["identity"]):
                fieldset = CustomFieldset.objects.get(namespace="itambox", slug=row["slug"])
                translated = fieldset.translations.get(locale="de")
                values = expected["sections"][row["identity"]]
                self.assertEqual((translated.label, translated.description), (values["label"], values["description"]))

        for row in vocabulary["choice_sets"]:
            choice_set = CustomFieldChoiceSet.objects.get(namespace="itambox", slug=row["slug"])
            for choice_data in row["choices"]:
                with self.subTest(kind="choice", identity=f"{row['identity']}#{choice_data['key']}"):
                    choice = choice_set.choices.get(key=choice_data["key"])
                    translated = choice.translations.get(locale="de")
                    self.assertEqual(
                        translated.label,
                        expected["choice_sets"][row["identity"]]["choices"][choice_data["key"]],
                    )

        self.assertEqual(
            CustomFieldTranslation.objects.filter(locale="de", custom_field__namespace="itambox").count(),
            len(vocabulary["active_fields"]) + len(vocabulary["reserved_retired_fields"]),
        )
        self.assertEqual(
            CustomFieldChoiceTranslation.objects.filter(locale="de", choice__choice_set__namespace="itambox").count(),
            sum(len(row["choices"]) for row in vocabulary["choice_sets"]),
        )

    def test_core_catalog_seed_has_german_rows_for_all_shipped_vocabulary(self):
        self._assert_german_rows_for_all_shipped_vocabulary()

    def test_migration_seeds_german_rows_for_all_shipped_vocabulary(self):
        CustomFieldTranslation.objects.filter(locale="de", custom_field__namespace="itambox").delete()
        CustomFieldsetTranslation.objects.filter(locale="de", fieldset__namespace="itambox").delete()
        CustomFieldChoiceTranslation.objects.filter(locale="de", choice__choice_set__namespace="itambox").delete()

        migration = importlib.import_module("assets.migrations.0006_core_vocabulary_german_translations")
        migration.seed_translations(apps, SimpleNamespace(connection=connection))
        self._assert_german_rows_for_all_shipped_vocabulary()

    def test_runtime_seed_is_idempotent_and_never_changes_authored_labels(self):
        field = CustomField.objects.get(namespace="itambox", name="form_factor")
        canonical_label = field.label
        first = seed_core_vocabulary_translations(apps.get_model, vocabulary=get_core_vocabulary())
        second = seed_core_vocabulary_translations(apps.get_model, vocabulary=get_core_vocabulary())
        self.assertEqual(first, second)
        field.refresh_from_db()
        self.assertEqual(field.label, canonical_label)

        authored = CustomField.objects.create(
            name="authored_german_translation_test",
            namespace="local",
            label="Exactly as authored",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        seed_core_vocabulary_translations(apps.get_model, vocabulary=get_core_vocabulary())
        authored.refresh_from_db()
        self.assertEqual(authored.label, "Exactly as authored")
        self.assertFalse(authored.translations.filter(locale="de").exists())
