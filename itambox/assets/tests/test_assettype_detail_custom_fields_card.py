"""Issue #827: the generic Custom Fields card is dropped on asset-type detail only."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetType, Manufacturer, StatusLabel, Supplier
from extras.models import CustomField

CARD_HEADING = "mdi-form-textbox"


class AssetTypeDetailCustomFieldsCardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("cf827", "cf827@example.com", "password")
        manufacturer = Manufacturer.objects.create(name="CF827 Mfr", slug="cf827-mfr")
        spec = CustomField.objects.create(
            name="port_count", label="Port Count", activation=CustomField.ACTIVATION_GLOBAL
        )
        spec.object_types.add(ContentType.objects.get_for_model(AssetType))
        cls.asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="CF827 Type",
            slug="cf827-type",
            custom_field_data={"port_count": "48", "legacy_note": "kept visible"},
        )
        status = StatusLabel.objects.create(name="CF827 Ready", slug="cf827-ready", type=StatusLabel.TYPE_DEPLOYABLE)
        cls.asset = Asset.objects.create(
            name="CF827 Asset",
            asset_tag="CF827-1",
            asset_type=cls.asset_type,
            status=status,
            custom_field_data={"rack_code": "R1"},
        )
        rack = CustomField.objects.create(name="rack_code", label="Rack Code", activation=CustomField.ACTIVATION_GLOBAL)
        rack.object_types.add(ContentType.objects.get_for_model(Asset))

    def setUp(self):
        self.client.force_login(self.admin)

    def test_asset_type_detail_has_no_generic_card_but_keeps_every_value(self):
        response = self.client.get(reverse("assets:assettype_detail", kwargs={"pk": self.asset_type.pk}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(CARD_HEADING, content)
        self.assertEqual(content.count("Port Count"), 1)
        self.assertIn("48", content)
        # A stored value outside the resolved specification set stays visible.
        self.assertIn("legacy_note", content)
        self.assertIn("kept visible", content)

    def test_other_custom_field_model_keeps_generic_card(self):
        field = CustomField.objects.create(
            name="vendor_code", label="Vendor Code", activation=CustomField.ACTIVATION_GLOBAL
        )
        field.object_types.add(ContentType.objects.get_for_model(Supplier))
        supplier = Supplier.objects.create(name="CF827 Supplier", custom_field_data={"vendor_code": "V9"})
        response = self.client.get(reverse("assets:supplier_detail", kwargs={"pk": supplier.pk}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(CARD_HEADING, content)
        self.assertIn("Vendor Code", content)


class AssetTypeFurtherStoredValuesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("cf827b", "cf827b@example.com", "password")
        cls.manufacturer = Manufacturer.objects.create(name="CF827B Mfr", slug="cf827b-mfr")
        cls.asset_type_ct = ContentType.objects.get_for_model(AssetType)
        cls.historical = CustomField.objects.create(
            name="old_spec", label="Old Spec", activation=CustomField.ACTIVATION_COMPOSED
        )
        cls.historical.object_types.add(cls.asset_type_ct)

    def setUp(self):
        self.client.force_login(self.admin)

    def _detail(self, **data):
        asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="Type %s" % AssetType.objects.count(),
            slug="cf827b-%s" % AssetType.objects.count(),
            custom_field_data=data,
        )
        response = self.client.get(reverse("assets:assettype_detail", kwargs={"pk": asset_type.pk}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_historical_value_with_definition_shows_label_and_badge(self):
        content = self._detail(old_spec="kept")
        self.assertIn("Further stored values", content)
        self.assertIn("Old Spec", content)
        self.assertIn("kept", content)
        self.assertIn("Not in current specification", content)
        self.assertNotIn("Unknown field", content)

    def test_unknown_key_shows_code_key_and_badge(self):
        content = self._detail(mystery_key="raw")
        self.assertIn("<code>mystery_key</code>", content)
        self.assertIn("Unknown field", content)
        self.assertIn("raw", content)

    def test_empty_historical_value_renders_dash(self):
        content = self._detail(old_spec="")
        self.assertIn("Further stored values", content)
        self.assertRegex(content, r"<div>\s*(?:<span[^>]*>)?\s*-\s*(?:</span>)?\s*</div>")

    def test_type_without_composition_but_legacy_values_renders_panel(self):
        content = self._detail(mystery_key="only-legacy")
        self.assertIn("specs-container", content)
        self.assertIn("only-legacy", content)

    def test_nothing_stored_renders_no_panel(self):
        content = self._detail()
        self.assertNotIn("specs-container", content)
        self.assertNotIn("Further stored values", content)
        self.assertNotIn(CARD_HEADING, content)
