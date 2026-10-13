"""Catalogue journeys: role grants reach the catalogue views under the approved scope.

Catalogue rows -- asset types and their specifications -- are global, so the
approved provider-scoped semantics (#611, implemented by #646) authorize them
against a concrete, active provider tenant only. A role grant
held in a plain customer tenant (or in a tenant group, or under the
"All accessible tenants" scope) never opens a global catalogue surface, while a
role grant held in a managing (``is_provider``) tenant does.
"""

from django.test import TestCase
from django.urls import reverse

from assets.models import AssetType, Manufacturer

from .support import JourneyMixin


class CataloguePermissionJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.customer = self.make_tenant("journey-catalogue")
        self.provider = self.make_provider_tenant("journey-catalogue-provider")

    def test_removed_type_library_urls_answer_404(self):
        """The in-app Type Library is gone (#768): its UI and REST URLs no longer resolve."""
        reader = self.make_member("catalogue-reader", {"assets.view_assettype"}, tenant=self.provider)
        self.client_login_to_tenant(reader, self.provider)

        for url in (
            "/assets/type-libraries/",
            "/assets/type-libraries/import/",
            "/assets/type-libraries/1/",
            "/assets/type-libraries/1/export/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for url in (
            "/api/assets/type-libraries/preview/",
            "/api/assets/type-libraries/apply/",
            "/api/assets/type-libraries/export/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {}, content_type="application/json").status_code, 404)

    def test_provider_role_with_add_assettype_can_create_an_asset_type(self):
        manufacturer = Manufacturer.objects.create(name="Journey Catalogue Maker", slug="journey-catalogue-maker")
        creator = self.make_member(
            "type-creator", {"assets.add_assettype", "assets.view_assettype"}, tenant=self.provider
        )
        self.client_login_to_tenant(creator, self.provider)

        response = self.client.post(
            reverse("assets:assettype_create"),
            {"manufacturer": manufacturer.pk, "model": "Journey Type", "slug": "journey-type"},
        )

        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(AssetType.objects.filter(model="Journey Type").exists())

    def test_customer_role_with_add_assettype_cannot_create_an_asset_type(self):
        manufacturer = Manufacturer.objects.create(name="Journey Customer Maker", slug="journey-customer-maker")
        creator = self.make_member(
            "customer-type-creator", {"assets.add_assettype", "assets.view_assettype"}, tenant=self.customer
        )
        self.client_login_to_tenant(creator, self.customer)

        response = self.client.post(
            reverse("assets:assettype_create"),
            {"manufacturer": manufacturer.pk, "model": "Journey Customer Type", "slug": "journey-customer-type"},
        )

        self.assertNotEqual(response.status_code, 302)
        self.assertFalse(AssetType.objects.filter(model="Journey Customer Type").exists())
