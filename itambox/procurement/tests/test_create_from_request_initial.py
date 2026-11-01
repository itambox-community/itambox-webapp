"""``?from_request=`` pre-selects the destination, and a stale id just yields an empty form."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.choices import RequestStatusChoices
from assets.models import AssetRequest, AssetType, Manufacturer
from organization.models import Location, Site, Tenant

User = get_user_model()


class PurchaseOrderCreateFromRequestInitialTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Initial Tenant", slug="initial-tenant")
        site = Site.objects.create(name="Initial Site", slug="initial-site")
        self.location = Location.objects.create(tenant=self.tenant, site=site, name="Dock", slug="dock")
        manufacturer = Manufacturer.objects.create(name="Initial Maker", slug="initial-maker")
        self.asset_type = AssetType.objects.create(
            manufacturer=manufacturer, model="Initial Model", slug="initial-model", requestable=True
        )
        self.admin = User.objects.create_superuser(username="initial-admin", password="password")
        self.client.force_login(self.admin)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()
        self.url = reverse("procurement:purchaseorder_create")

    def test_request_location_becomes_the_initial_destination(self):
        asset_request = AssetRequest.objects.create(
            tenant=self.tenant,
            requester=self.admin,
            asset_type=self.asset_type,
            status=RequestStatusChoices.APPROVED,
            assigned_location=self.location,
        )
        response = self.client.get(self.url, {"from_request": asset_request.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["destination_location"], self.location.pk)

    def test_unknown_request_id_leaves_the_form_unfilled(self):
        response = self.client.get(self.url, {"from_request": 987654321})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("destination_location", response.context["form"].initial)

    def test_malformed_request_id_is_a_404(self):
        self.assertEqual(self.client.get(self.url, {"from_request": "abc"}).status_code, 404)
