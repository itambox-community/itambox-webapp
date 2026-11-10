"""Location and site detail tables resolve their reverse rows through scope."""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.tests.mixins import TenantTestMixin
from organization.models import Location, Site, Tenant


class LocationSiteReverseScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Reverse Scope A",
            slug="reverse-scope-a",
            permissions=[
                "organization.view_location",
                "organization.view_site",
                "organization.delete_site",
                "assets.view_asset",
            ],
        )
        self.tenant_b = Tenant.objects.create(name="Reverse Scope B", slug="reverse-scope-b")

        self.site_a = Site.objects.create(name="Reverse A Site", slug="reverse-a-site", tenant=self.tenant)
        self.site_b = Site.objects.create(name="Reverse B Site", slug="reverse-b-site", tenant=self.tenant_b)
        self.location_a = Location.objects.create(
            name="Reverse A Only Depot", slug="reverse-a-only-depot", site=self.site_a, tenant=self.tenant
        )
        self.location_b_on_a = Location.objects.create(
            name="Reverse B On A Depot", slug="reverse-b-on-a-depot", site=self.site_b, tenant=self.tenant_b
        )
        self.child_b_on_a = Location.objects.create(
            name="Reverse B Child On A",
            slug="reverse-b-child-on-a",
            site=self.site_b,
            tenant=self.tenant_b,
        )

        manufacturer = Manufacturer.objects.create(name="Reverse Mfr", slug="reverse-mfr")
        asset_type = AssetType.objects.create(manufacturer=manufacturer, model="Reverse Model", slug="reverse-model")
        asset_role = AssetRole.objects.create(name="Reverse Role", slug="reverse-role")
        status = StatusLabel.objects.create(name="Reverse Status", slug="reverse-status", type="deployable")
        self.asset_a = Asset.objects.create(
            name="Reverse A Only Asset",
            asset_tag="REV-A",
            asset_type=asset_type,
            asset_role=asset_role,
            status=status,
            tenant=self.tenant,
            location=self.location_a,
        )
        self.asset_b_on_a = Asset.objects.create(
            name="Reverse B On A Asset",
            asset_tag="REV-B",
            asset_type=asset_type,
            asset_role=asset_role,
            status=status,
            tenant=self.tenant_b,
            location=self.location_b_on_a,
        )
        # The cross-tenant rows are built directly, bypassing the model clean
        # hooks that would reject them through the normal write paths.
        Location.objects.filter(pk=self.location_b_on_a.pk).update(site=self.site_a)
        Location.objects.filter(pk=self.child_b_on_a.pk).update(site=self.site_a, parent=self.location_a)
        Asset.objects.filter(pk=self.asset_b_on_a.pk).update(location=self.location_a)

        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def test_location_detail_hides_foreign_assets_and_children(self):
        response = self.client.get(reverse("organization:location_detail", kwargs={"pk": self.location_a.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.asset_a.name)
        self.assertNotContains(response, self.asset_b_on_a.name)
        labels = {str(row["label"]) for row in response.context["related_objects_list"]}
        self.assertNotIn("Child Locations", labels)

    def test_site_detail_hides_foreign_locations(self):
        response = self.client.get(reverse("organization:site_detail", kwargs={"pk": self.site_a.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.location_a.name)
        self.assertNotContains(response, self.location_b_on_a.name)
        self.assertNotContains(response, self.child_b_on_a.name)

    def test_site_delete_message_does_not_disclose_hidden_location_count(self):
        response = self.client.post(
            reverse("organization:site_delete", kwargs={"pk": self.site_a.pk}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("outside your scope" in message for message in messages))
        self.assertFalse(any("2 locations" in message for message in messages))
