"""The checkout modal pre-fills its target from an Asset Request, when one exists."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset, AssetRequest, AssetType, StatusLabel

User = get_user_model()


class CheckoutPrefillFromRequestTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.client.force_login(self.user)
        status = baker.make(StatusLabel, type="deployable")
        baker.make(StatusLabel, type="deployed", name="Deployed")
        self.monitor = baker.make(Asset, name="Monitor", status=status, tenant=None)
        self.laptop = baker.make(Asset, name="Laptop", status=status, tenant=None)
        self.url = reverse("assets:asset_checkout_modal", kwargs={"pk": self.monitor.pk})

    def _initial(self, **query):
        response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, 200)
        return response.context["form"].initial

    def test_request_target_prefills_the_form(self):
        asset_request = baker.make(
            AssetRequest,
            tenant=None,
            status="approved",
            assigned_asset=self.laptop,
            asset_type=baker.make(AssetType, requestable=True),
        )
        initial = self._initial(request_id=asset_request.pk)
        self.assertEqual(initial["target_type"], "asset")
        self.assertEqual(initial["asset_target"], self.laptop)

    def test_unknown_request_id_leaves_the_form_unfilled(self):
        initial = self._initial(request_id=987654321)
        self.assertNotIn("target_type", initial)
