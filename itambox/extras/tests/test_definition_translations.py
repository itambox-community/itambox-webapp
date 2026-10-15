from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from extras.models import (
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldChoiceTranslation,
    CustomFieldset,
    CustomFieldsetTranslation,
    CustomFieldTranslation,
)


class DefinitionTranslationRowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.choice_set = CustomFieldChoiceSet.objects.create(namespace="itambox", slug="tr-set", label="Set")
        cls.choice = CustomFieldChoice.objects.create(choice_set=cls.choice_set, key="https", label="HTTPS", position=1)
        cls.field = CustomField.objects.create(
            name="tr_print_speed",
            namespace="local",
            label="Print speed",
            help_text="Pages per minute",
            field_type=CustomField.FIELD_TYPE_INTEGER,
            activation=CustomField.ACTIVATION_COMPOSED,
            canonical_unit="pages_per_minute",
        )
        cls.fieldset = CustomFieldset.objects.create(namespace="local", slug="tr-power", label="Power and Battery")

    def test_definition_translation_roundtrip_keeps_base_label_canonical(self):
        CustomFieldTranslation.objects.create(
            custom_field=self.field,
            locale="de",
            label="Druckgeschwindigkeit",
            help_text="Seiten pro Minute",
            unit_label="S/min",
        )
        row = self.field.translations.get(locale="de")
        self.assertEqual(
            (row.label, row.help_text, row.unit_label), ("Druckgeschwindigkeit", "Seiten pro Minute", "S/min")
        )
        self.field.refresh_from_db()
        self.assertEqual(self.field.label, "Print speed")
        self.assertEqual(self.field.canonical_unit, "pages_per_minute")

    def test_choice_translation_roundtrip(self):
        CustomFieldChoiceTranslation.objects.create(choice=self.choice, locale="de", label="HTTPS (de)")
        self.assertEqual(self.choice.translations.get(locale="de").label, "HTTPS (de)")
        self.choice.refresh_from_db()
        self.assertEqual(self.choice.label, "HTTPS")

    def test_section_translation_roundtrip(self):
        CustomFieldsetTranslation.objects.create(fieldset=self.fieldset, locale="de", label="Strom und Akku")
        self.assertEqual(self.fieldset.translations.get(locale="de").label, "Strom und Akku")
        self.fieldset.refresh_from_db()
        self.assertEqual(self.fieldset.label, "Power and Battery")

    def test_unit_token_translation_is_a_definition_row(self):
        CustomFieldTranslation.objects.create(custom_field=self.field, locale="de", label="x", unit_label="S/min")
        self.assertEqual(self.field.translations.get(locale="de").unit_label, "S/min")

    def test_missing_locale_row_means_fallback_to_base(self):
        self.assertFalse(self.field.translations.filter(locale="de").exists())

    def test_unique_entity_locale_key(self):
        cases = [
            (CustomFieldTranslation, {"custom_field": self.field}),
            (CustomFieldChoiceTranslation, {"choice": self.choice}),
            (CustomFieldsetTranslation, {"fieldset": self.fieldset}),
        ]
        for model, ref in cases:
            with self.subTest(model=model.__name__):
                model.objects.create(locale="de", label="a", **ref)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.create(locale="de", label="b", **ref)
                model.objects.create(locale="fr", label="c", **ref)

    def test_locale_is_validated(self):
        row = CustomFieldTranslation(custom_field=self.field, locale="DE de", label="x")
        with self.assertRaises(ValidationError):
            row.full_clean()
