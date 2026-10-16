from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from extras.models import (
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldChoiceTranslation,
    CustomFieldset,
    CustomFieldsetTranslation,
    CustomFieldTranslation,
)

User = get_user_model()


class DefinitionTranslationAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_user(username="translation-operator", is_staff=True, is_superuser=True)
        cls.outsider = User.objects.create_user(username="translation-outsider")
        cls.staff_without_permission = User.objects.create_user(username="translation-staff", is_staff=True)
        cls.choice_set = CustomFieldChoiceSet.objects.create(
            namespace="itambox", slug="tra-set", label="Set", management_kind="core"
        )
        cls.choice = CustomFieldChoice.objects.create(choice_set=cls.choice_set, key="https", label="HTTPS", position=1)
        cls.field = CustomField.objects.create(
            name="tra_network_os",
            namespace="itambox",
            label="Network OS",
            help_text="Operating system",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
            management_kind="core",
        )
        cls.fieldset = CustomFieldset.objects.create(
            namespace="itambox", slug="tra-power", label="Power and Battery", management_kind="core"
        )

    def _url(self, kind, entity, locale="de"):
        return reverse("extras:definition_translation_edit", kwargs={"kind": kind, "pk": entity.pk, "locale": locale})

    def test_operator_adds_then_edits_german_field_translation(self):
        self.client.force_login(self.operator)
        url = self._url("field", self.field)

        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Network OS")
        self.assertContains(page, "readonly")

        response = self.client.post(url, {"label": "Netzwerk-Betriebssystem", "help_text": "Betriebssystem"})
        self.assertEqual(response.status_code, 302)
        row = CustomFieldTranslation.objects.get(custom_field=self.field, locale="de")
        self.assertEqual(row.label, "Netzwerk-Betriebssystem")

        response = self.client.post(url, {"label": "Netzwerk-OS", "help_text": "Betriebssystem"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CustomFieldTranslation.objects.filter(custom_field=self.field, locale="de").count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.label, "Netzwerk-OS")

        self.field.refresh_from_db()
        self.assertEqual(self.field.label, "Network OS")
        self.assertEqual(self.field.help_text, "Operating system")

    def test_operator_translates_choice_and_section_labels(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(self._url("choice", self.choice), {"label": "HTTPS (de)"}).status_code, 302)
        self.assertEqual(
            self.client.post(self._url("fieldset", self.fieldset), {"label": "Strom und Akku"}).status_code, 302
        )
        self.assertEqual(CustomFieldChoiceTranslation.objects.get(choice=self.choice).label, "HTTPS (de)")
        self.assertEqual(CustomFieldsetTranslation.objects.get(fieldset=self.fieldset).label, "Strom und Akku")
        self.choice.refresh_from_db()
        self.fieldset.refresh_from_db()
        self.assertEqual((self.choice.label, self.fieldset.label), ("HTTPS", "Power and Battery"))

    def test_unit_token_is_editable_on_the_field_translation(self):
        self.client.force_login(self.operator)
        self.client.post(self._url("field", self.field), {"label": "Druck", "unit_label": "S/min"})
        self.assertEqual(self.field.translations.get(locale="de").unit_label, "S/min")

    def test_unknown_locale_and_kind_are_rejected(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(self._url("field", self.field, locale="xx")).status_code, 404)
        self.assertEqual(self.client.post(self._url("field", self.field, "xx"), {"label": "x"}).status_code, 404)
        self.assertFalse(CustomFieldTranslation.objects.exists())
        url = reverse("extras:definition_translation_edit", kwargs={"kind": "bogus", "pk": 1, "locale": "de"})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_label_is_required(self):
        self.client.force_login(self.operator)
        response = self.client.post(self._url("field", self.field), {"label": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomFieldTranslation.objects.exists())

    def test_non_operators_cannot_read_or_write(self):
        for user in (self.outsider, self.staff_without_permission):
            self.client.force_login(user)
            url = self._url("field", self.field)
            with self.subTest(user=user.username):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {"label": "x"}).status_code, 403)
        self.assertFalse(CustomFieldTranslation.objects.exists())

    def test_anonymous_user_is_denied(self):
        response = self.client.post(self._url("field", self.field), {"label": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CustomFieldTranslation.objects.exists())

    def test_choice_set_detail_links_to_the_translation_form(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("extras:definition_choice_set_detail", kwargs={"pk": self.choice_set.pk}))
        self.assertContains(response, self._url("choice", self.choice))
