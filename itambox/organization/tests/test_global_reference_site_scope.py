"""Global region and site-group details only show sites in the active scope."""

from django.test import TestCase
from django.urls import reverse

from core.tests.mixins import TenantTestMixin
from organization.models import Region, Site, SiteGroup, Tenant


class GlobalReferenceSiteScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Reference Scope A",
            slug="reference-scope-a",
            permissions=["organization.view_region", "organization.view_sitegroup"],
        )
        self.tenant_b = Tenant.objects.create(name="Reference Scope B", slug="reference-scope-b")

        self.region = Region.objects.create(name="Reference Region", slug="reference-region")
        self.sitegroup = SiteGroup.objects.create(name="Reference Group", slug="reference-group")
        self.site_a = Site.objects.create(
            name="Reference A Site",
            slug="reference-a-site",
            region=self.region,
            group=self.sitegroup,
            tenant=self.tenant,
        )
        self.site_b = Site.objects.create(
            name="Reference B Site",
            slug="reference-b-site",
            region=self.region,
            group=self.sitegroup,
            tenant=self.tenant_b,
        )

        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def test_region_detail_only_shows_sites_in_scope(self):
        response = self.client.get(reverse("organization:region_detail", kwargs={"pk": self.region.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.site_a.name)
        self.assertNotContains(response, self.site_b.name)
        self.assertEqual(response.context["related_objects_list"][0]["count"], 1)

    def test_sitegroup_detail_only_shows_sites_in_scope(self):
        response = self.client.get(reverse("organization:sitegroup_detail", kwargs={"pk": self.sitegroup.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.site_a.name)
        self.assertNotContains(response, self.site_b.name)
        self.assertEqual(response.context["related_objects_list"][0]["count"], 1)
