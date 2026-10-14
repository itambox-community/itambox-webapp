"""Issue #827: the generic Custom Fields card is dropped on asset-type detail only."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetType, Manufacturer, StatusLabel
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
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(CARD_HEADING, content)
        self.assertIn("Rack Code", content)
