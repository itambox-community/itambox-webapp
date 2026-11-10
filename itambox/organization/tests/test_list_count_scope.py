"""List count annotations follow the active tenant scope."""

from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.tests.mixins import TenantTestMixin
from organization.models import Location, Region, Site, SiteGroup, Tenant


class OrganizationListCountScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="List Count A",
            slug="list-count-a",
            permissions=[
                "organization.view_region",
                "organization.view_sitegroup",
                "organization.view_site",
                "organization.view_location",
            ],
        )
        self.tenant_b = Tenant.objects.create(name="List Count B", slug="list-count-b")

        self.region = Region.objects.create(name="List Count Region", slug="list-count-region")
        self.sitegroup = SiteGroup.objects.create(name="List Count Group", slug="list-count-group")
        self.site_a = Site.objects.create(
            name="List Count A Site",
            slug="list-count-a-site",
            region=self.region,
            group=self.sitegroup,
            tenant=self.tenant,
        )
        self.site_b = Site.objects.create(
            name="List Count B Site",
            slug="list-count-b-site",
            region=self.region,
            group=self.sitegroup,
            tenant=self.tenant_b,
        )
        self.location_a = Location.objects.create(
            name="List Count A Depot", slug="list-count-a-depot", site=self.site_a, tenant=self.tenant
        )
        self.location_b = Location.objects.create(
            name="List Count B Depot", slug="list-count-b-depot", site=self.site_b, tenant=self.tenant_b
        )

        manufacturer = Manufacturer.objects.create(name="List Count Mfr", slug="list-count-mfr")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer, model="List Count Model", slug="list-count-model"
        )
        asset_role = AssetRole.objects.create(name="List Count Role", slug="list-count-role")
        status = StatusLabel.objects.create(name="List Count Status", slug="list-count-status", type="deployable")
        self.asset_a = Asset.objects.create(
            name="List Count A Asset",
            asset_tag="LIST-A",
            asset_type=asset_type,
            asset_role=asset_role,
            status=status,
            tenant=self.tenant,
            location=self.location_a,
        )
        self.asset_b = Asset.objects.create(
            name="List Count B Asset",
            asset_tag="LIST-B",
            asset_type=asset_type,
            asset_role=asset_role,
            status=status,
            tenant=self.tenant_b,
            location=self.location_b,
        )
        # Move the foreign location under site A and the foreign asset under
        # location A, bypassing the write-path clean hooks to build the leak
        # fixture the read path must not trust.
        Location.objects.filter(pk=self.location_b.pk).update(site=self.site_a)
        Asset.objects.filter(pk=self.asset_b.pk).update(location=self.location_a)

        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _rows(self, response):
        return [row.record for row in response.context["table"].rows]

    def test_region_list_counts_only_visible_sites(self):
        response = self.client.get(reverse("organization:region_list"))
        self.assertEqual(response.status_code, 200)
        region = next(row for row in self._rows(response) if row.pk == self.region.pk)
        self.assertEqual(region.site_count, 1)

    def test_sitegroup_list_counts_only_visible_sites(self):
        response = self.client.get(reverse("organization:sitegroup_list"))
        self.assertEqual(response.status_code, 200)
        sitegroup = next(row for row in self._rows(response) if row.pk == self.sitegroup.pk)
        self.assertEqual(sitegroup.site_count, 1)

    def test_site_list_counts_only_visible_locations_and_assets(self):
        response = self.client.get(reverse("organization:site_list"))
        self.assertEqual(response.status_code, 200)
        site = next(row for row in self._rows(response) if row.pk == self.site_a.pk)
        self.assertEqual(site.location_count, 1)
        self.assertEqual(site.asset_count, 1)

    def test_location_list_counts_only_visible_assets(self):
        response = self.client.get(reverse("organization:location_list"))
        self.assertEqual(response.status_code, 200)
        location = next(row for row in self._rows(response) if row.pk == self.location_a.pk)
        self.assertEqual(location.asset_count, 1)
