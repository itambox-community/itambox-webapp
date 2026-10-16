"""Issue #832: vocabulary labels resolve through translation rows on display surfaces only."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation
from rest_framework.test import APIClient

from assets.forms import AssetTypeForm
from assets.models import Asset, AssetType, Manufacturer, StatusLabel
from assets.models.catalog import AssetTypeFieldset
from extras.models import (
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldChoiceTranslation,
    CustomFieldset,
    CustomFieldsetField,
    CustomFieldsetTranslation,
    CustomFieldTranslation,
)
from extras.services.vocabulary_translations import BASE_LABELS, VocabularyTranslator, language_candidates

# ``>Switch<`` matches the rendered choice text only: the German label contains "Switch".
ENGLISH_VOCABULARY = ("Device Role", ">Switch<", "Connectivity and I/O", "Print speed", "Network OS")
GERMAN_VOCABULARY = (
    "Geräterolle",
    "Netzwerk-Switch",
    "Konnektivität und E/A",
    "Druckgeschwindigkeit",
    "Netzwerk-Betriebssystem",
)


class _VocabularyWorld:
    """Shipped-style definitions with German rows plus one tenant-authored field."""

    @classmethod
    def build_world(cls):
        cls.admin = get_user_model().objects.create_superuser("voc832", "voc832@example.com", "password")
        type_ct = ContentType.objects.get_for_model(AssetType)
        asset_ct = ContentType.objects.get_for_model(Asset)
        cls.choice_set = CustomFieldChoiceSet.objects.create(namespace="itambox", slug="voc832-role", label="Role")
        cls.switch = CustomFieldChoice.objects.create(
            choice_set=cls.choice_set, key="switch", label="Switch", position=1
        )
        cls.router = CustomFieldChoice.objects.create(
            choice_set=cls.choice_set, key="router", label="Router", position=2
        )
        cls.role = CustomField.objects.create(
            namespace="itambox",
            name="voc832_device_role",
            label="Device Role",
            help_text="What the device does",
            field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
            activation=CustomField.ACTIVATION_COMPOSED,
            choice_set=cls.choice_set,
            max_values=1,
        )
        cls.speed = CustomField.objects.create(
            namespace="itambox",
            name="voc832_print_speed",
            label="Print speed",
            field_type=CustomField.FIELD_TYPE_INTEGER,
            activation=CustomField.ACTIVATION_COMPOSED,
            quantity_kind="rate",
            canonical_unit="pages_per_minute",
        )
        cls.os = CustomField.objects.create(
            namespace="itambox",
            name="voc832_network_os",
            label="Network OS",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        cls.tenant_field = CustomField.objects.create(
            namespace="local",
            name="voc832_rack_code",
            label="Rack code (local)",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        cls.fieldset = CustomFieldset.objects.create(
            namespace="itambox", slug="voc832-connectivity", label="Connectivity and I/O"
        )
        for position, field in enumerate((cls.role, cls.speed, cls.os, cls.tenant_field), start=1):
            field.object_types.add(type_ct, asset_ct)
            CustomFieldsetField.objects.create(fieldset=cls.fieldset, custom_field=field, position=position)
        CustomFieldTranslation.objects.create(
            custom_field=cls.role, locale="de", label="Geräterolle", help_text="Aufgabe des Geräts"
        )
        CustomFieldTranslation.objects.create(
            custom_field=cls.speed, locale="de", label="Druckgeschwindigkeit", unit_label="S/min"
        )
        CustomFieldTranslation.objects.create(custom_field=cls.os, locale="de", label="Netzwerk-Betriebssystem")
        CustomFieldChoiceTranslation.objects.create(choice=cls.switch, locale="de", label="Netzwerk-Switch")
        CustomFieldsetTranslation.objects.create(fieldset=cls.fieldset, locale="de", label="Konnektivität und E/A")
        manufacturer = Manufacturer.objects.create(name="VOC832 Mfr", slug="voc832-mfr")
        cls.asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="VOC832 Type",
            slug="voc832-type",
            custom_field_data={"voc832_device_role": "switch", "voc832_print_speed": 30},
        )
        AssetTypeFieldset.objects.create(asset_type=cls.asset_type, fieldset=cls.fieldset, position=1)
        status = StatusLabel.objects.create(name="VOC832 Ready", slug="voc832-ready", type=StatusLabel.TYPE_DEPLOYABLE)
        cls.asset = Asset.objects.create(
            name="VOC832 Asset",
            asset_tag="VOC832-1",
            asset_type=cls.asset_type,
            status=status,
            custom_field_data={"voc832_network_os": "IOS-XE 17", "voc832_rack_code": "R7"},
        )


class TranslatorResolutionTests(_VocabularyWorld, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def test_requested_locale_wins_over_active_locale(self):
        with translation.override("en"):
            translator = VocabularyTranslator(requested_locale="de", active_locale="en").load(fields=[self.role])
        self.assertEqual(translator.field_label(self.role), "Geräterolle")
        self.assertEqual(translator.field_help_text(self.role), "Aufgabe des Geräts")
        self.assertEqual(translator.choice_label(self.switch), "Netzwerk-Switch")

    def test_active_locale_is_used_without_a_requested_locale(self):
        with translation.override("de"):
            translator = VocabularyTranslator().load(fields=[self.speed], fieldsets=[self.fieldset])
        self.assertEqual(translator.field_label(self.speed), "Druckgeschwindigkeit")
        self.assertEqual(translator.unit(self.speed), "S/min")
        self.assertEqual(translator.fieldset_label(self.fieldset), "Konnektivität und E/A")

    def test_regional_locale_falls_back_to_its_base_language(self):
        self.assertEqual(language_candidates("de_AT"), ("de-at", "de"))
        translator = VocabularyTranslator(requested_locale="de-AT").load(fields=[self.role])
        self.assertEqual(translator.field_label(self.role), "Geräterolle")

    def test_missing_translation_falls_back_to_base_label_never_empty(self):
        CustomFieldTranslation.objects.create(custom_field=self.os, locale="fr", label="   ")
        translator = VocabularyTranslator(requested_locale="fr").load(
            fields=[self.role, self.speed, self.os], fieldsets=[self.fieldset]
        )
        self.assertEqual(translator.field_label(self.role), "Device Role")
        self.assertEqual(translator.field_label(self.os), "Network OS")
        self.assertEqual(translator.field_help_text(self.role), "What the device does")
        self.assertEqual(translator.choice_label(self.router), "Router")
        self.assertEqual(translator.unit(self.speed), "ppm")
        self.assertEqual(translator.fieldset_label(self.fieldset), "Connectivity and I/O")
        # A German row without a help text keeps the base help text.
        german = VocabularyTranslator(requested_locale="de").load(fields=[self.os, self.role])
        self.assertEqual(german.field_help_text(self.os), "")
        self.assertEqual(german.choice_label(self.router), "Router")

    def test_unloaded_and_unsaved_entities_fall_back_without_queries(self):
        translator = VocabularyTranslator(requested_locale="de")
        ghost = SimpleNamespace(pk=None, label="", name="ghost_key", help_text=None, canonical_unit="VA")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(translator.field_label(self.role), "Device Role")
            self.assertEqual(translator.field_label(ghost), "ghost_key")
            self.assertEqual(translator.field_help_text(ghost), "")
            self.assertEqual(translator.unit(ghost), "VA")
            self.assertEqual(translator.choice_label(SimpleNamespace(pk=None, label="", key="k")), "k")
            self.assertEqual(translator.fieldset_label(SimpleNamespace(pk=None, label="", slug="s")), "s")
            self.assertEqual(translator.fieldset_description(SimpleNamespace(pk=None, description=None)), "")
        self.assertEqual(len(queries), 0)

    def test_source_language_translator_is_inert(self):
        self.assertTrue(BASE_LABELS.inert)
        with CaptureQueriesContext(connection) as queries:
            translator = VocabularyTranslator(requested_locale="en").load(fields=[self.role], fieldsets=[self.fieldset])
        self.assertEqual(len(queries), 0)
        self.assertEqual(translator.field_label(self.role), "Device Role")

    def test_load_batches_per_kind_and_skips_loaded_ids(self):
        translator = VocabularyTranslator(requested_locale="de")
        with CaptureQueriesContext(connection) as queries:
            translator.load(fields=[self.role, self.speed, self.os], fieldsets=[self.fieldset])
        self.assertEqual(len(queries), 3)
        with CaptureQueriesContext(connection) as queries:
            translator.load(fields=[self.role, self.speed], fieldsets=[self.fieldset])
        self.assertEqual(len(queries), 0)

    def test_exact_regional_row_beats_base_language_row(self):
        CustomFieldTranslation.objects.create(custom_field=self.role, locale="de-ch", label="Geräterolle CH")
        translator = VocabularyTranslator(requested_locale="de-CH").load(fields=[self.role])
        self.assertEqual(translator.field_label(self.role), "Geräterolle CH")


class GermanDetailRenderingTests(_VocabularyWorld, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def setUp(self):
        self.client.force_login(self.admin)

    def _get(self, url, language):
        # LocaleMiddleware leaves the negotiated language active on the thread;
        # the override restores it so later tests on the same worker stay in English.
        with translation.override(language):
            response = self.client.get(url, HTTP_ACCEPT_LANGUAGE=language)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_asset_detail_shows_german_vocabulary_and_no_english_vocabulary(self):
        content = self._get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}), "de")
        for label in GERMAN_VOCABULARY:
            self.assertIn(label, content)
        self.assertIn("S/min", content)
        for label in ENGLISH_VOCABULARY:
            self.assertNotIn(label, content)
        self.assertNotIn(">ppm<", content)

    def test_tenant_authored_field_renders_verbatim_under_german(self):
        content = self._get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}), "de")
        self.assertIn("Rack code (local)", content)
        self.assertIn("R7", content)

    def test_asset_type_detail_spec_card_and_section_links_are_german(self):
        content = self._get(reverse("assets:assettype_detail", kwargs={"pk": self.asset_type.pk}), "de")
        self.assertIn("Konnektivität und E/A", content)
        self.assertIn("Geräterolle", content)
        self.assertIn("Netzwerk-Switch", content)
        for label in ("Connectivity and I/O", "Device Role", ">Switch<"):
            self.assertNotIn(label, content)

    def test_english_ui_keeps_base_labels(self):
        content = self._get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}), "en")
        for label in ENGLISH_VOCABULARY:
            self.assertIn(label, content)
        for label in GERMAN_VOCABULARY:
            self.assertNotIn(label, content)

    def test_missing_german_row_falls_back_to_base_label_on_detail(self):
        CustomFieldTranslation.objects.filter(custom_field=self.os).delete()
        content = self._get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}), "de")
        self.assertIn("Network OS", content)
        self.assertIn("IOS-XE 17", content)

    def test_historical_value_label_is_translated_in_further_values(self):
        CustomFieldsetField.objects.filter(fieldset=self.fieldset, custom_field=self.os).delete()
        content = self._get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}), "de")
        self.assertIn("Netzwerk-Betriebssystem", content)
        self.assertNotIn("Network OS", content)


class GermanSpecificationFormTests(_VocabularyWorld, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def test_asset_type_form_sections_fields_and_choices_are_german(self):
        with translation.override("de"):
            form = AssetTypeForm(instance=AssetType.objects.get(pk=self.asset_type.pk))
            section = next(s for s in form.specification_sections if s["identity"] == "itambox/voc832-connectivity")
            self.assertEqual(section["label"], "Konnektivität und E/A")
            role = form.fields["cf_voc832_device_role"]
            self.assertEqual(role.label, "Geräterolle")
            self.assertEqual(role.help_text, "Aufgabe des Geräts")
            self.assertIn(("switch", "Netzwerk-Switch"), role.choices)
            self.assertIn(("router", "Router"), role.choices)
            self.assertEqual(form.fields["cf_voc832_rack_code"].label, "Rack code (local)")
            option = next(o for o in form.specification_fieldset_options if o["id"] == self.fieldset.pk)
            self.assertEqual(option["label"], "Konnektivität und E/A")

    def test_asset_form_sections_model_values_and_history_are_german(self):
        from assets.forms import AssetForm

        self.asset.custom_field_data = {**self.asset.custom_field_data, "voc832_device_role": "switch"}
        with translation.override("de"):
            form = AssetForm(instance=Asset.objects.get(pk=self.asset.pk), initial={"asset_type": self.asset_type.pk})
            labels = {section["label"] for section in form.specification_sections}
            self.assertIn("Konnektivität und E/A", labels)
            self.assertEqual(form.fields["cf_voc832_network_os"].label, "Netzwerk-Betriebssystem")
            model_values = {item["key"]: item for item in form.model_specification_fields}
            self.assertEqual(model_values["voc832_device_role"]["label"], "Geräterolle")
            self.assertEqual(model_values["voc832_device_role"]["display_value"], "Netzwerk-Switch")
            history = {entry["key"]: entry for entry in form.specification_history}
            self.assertEqual(history["voc832_network_os"]["label"], "Netzwerk-Betriebssystem")
            self.assertEqual(history["voc832_rack_code"]["label"], "Rack code (local)")


class PublishedContractUnchangedTests(_VocabularyWorld, TestCase):
    """The REST and GraphQL definition payloads keep the canonical base label."""

    @classmethod
    def setUpTestData(cls):
        cls.build_world()

    def test_rest_definition_payload_returns_base_label_under_german(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(f"/api/extras/custom-fields/{self.role.pk}/", HTTP_ACCEPT_LANGUAGE="de")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["label"], "Device Role")
        self.assertEqual(response.data["help_text"], "What the device does")
        listing = client.get("/api/extras/custom-fields/?limit=500", HTTP_ACCEPT_LANGUAGE="de")
        self.assertEqual(listing.status_code, 200)
        labels = {row["name"]: row["label"] for row in listing.data["results"]}
        self.assertEqual(labels["voc832_network_os"], "Network OS")
        self.assertNotIn("Netzwerk-Betriebssystem", listing.content.decode())

    def test_graphql_definition_payload_returns_base_labels_under_german(self):
        from core.schema import build_schema

        query = """
        query($id: ID!) {
          assetType(id: $id) {
            fieldsets { label fields { key label choiceSet { choices { key label } } } }
          }
        }
        """
        with translation.override("de"):
            result = build_schema(extensions=[]).execute_sync(
                query, variable_values={"id": str(self.asset_type.pk)}, context_value=SimpleNamespace(user=self.admin)
            )
        self.assertIsNone(result.errors, result.errors)
        fieldset = result.data["assetType"]["fieldsets"][0]
        self.assertEqual(fieldset["label"], "Connectivity and I/O")
        fields = {field["key"]: field for field in fieldset["fields"]}
        role = next(field for key, field in fields.items() if key.endswith("voc832_device_role"))
        self.assertEqual(role["label"], "Device Role")
        self.assertIn({"key": "switch", "label": "Switch"}, role["choiceSet"]["choices"])
        self.assertNotIn("Geräterolle", str(result.data))
