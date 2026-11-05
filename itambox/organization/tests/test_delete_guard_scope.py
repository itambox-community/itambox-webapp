"""Delete guards separate the block decision from the visible reference count."""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, AccessoryAssignment, Consumable, ConsumableAssignment
from inventory.services import item_assignments_for_scope
from inventory.tests.factories import create_assignment_fixture
from organization.models import Region, Site, SiteGroup, Tenant


class DeleteGuardScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Delete Scope A",
            slug="delete-scope-a",
            permissions=[
                "assets.delete_assettype",
                "assets.delete_assetrole",
                "assets.delete_statuslabel",
                "organization.delete_region",
                "organization.delete_sitegroup",
                "organization.delete_location",
                "inventory.delete_accessory",
                "inventory.delete_consumable",
            ],
        )
        self.tenant_b = Tenant.objects.create(name="Delete Scope B", slug="delete-scope-b")

        self.site_a = Site.objects.create(name="Delete A Site", slug="delete-a-site", tenant=self.tenant)
        self.site_b = Site.objects.create(name="Delete B Site", slug="delete-b-site", tenant=self.tenant_b)
        self.location_a = self.site_a.locations.create(name="Delete A Depot", slug="delete-a-depot", tenant=self.tenant)
        self.location_b = self.site_b.locations.create(
            name="Delete B Depot", slug="delete-b-depot", tenant=self.tenant_b
        )
        self.location_hidden = self.site_a.locations.create(
            name="Delete Hidden Depot", slug="delete-hidden-depot", tenant=self.tenant
        )

        self.manufacturer = Manufacturer.objects.create(name="Delete Mfr", slug="delete-mfr")
        self.assetrole_a = AssetRole.objects.create(name="Delete A Role", slug="delete-a-role")
        self.assetrole_b = AssetRole.objects.create(name="Delete B Role", slug="delete-b-role")
        self.status_a = StatusLabel.objects.create(name="Delete A Status", slug="delete-a-status", type="deployable")
        self.status_b = StatusLabel.objects.create(name="Delete B Status", slug="delete-b-status", type="deployable")

        self.assettype_a = AssetType.objects.create(
            manufacturer=self.manufacturer, model="Delete A Model", slug="delete-a-model"
        )
        self.assettype_b = AssetType.objects.create(
            manufacturer=self.manufacturer, model="Delete B Model", slug="delete-b-model"
        )
        self.asset_a = self._asset(
            "Delete A Asset", "DEL-A", self.tenant, self.location_a, self.assettype_a, self.assetrole_a, self.status_a
        )
        self.asset_b = self._asset(
            "Delete B Asset", "DEL-B", self.tenant_b, self.location_b, self.assettype_b, self.assetrole_b, self.status_b
        )
        Asset.all_objects.bulk_create(
            [
                Asset(
                    name="Delete Hidden Asset",
                    asset_tag="DEL-H",
                    asset_type=self.assettype_b,
                    asset_role=self.assetrole_b,
                    status=self.status_b,
                    tenant=self.tenant_b,
                    location=self.location_hidden,
                )
            ]
        )

        self.region_a = Region.objects.create(name="Delete A Region", slug="delete-a-region")
        self.region_b = Region.objects.create(name="Delete B Region", slug="delete-b-region")
        self.sitegroup_a = SiteGroup.objects.create(name="Delete A Group", slug="delete-a-group")
        self.sitegroup_b = SiteGroup.objects.create(name="Delete B Group", slug="delete-b-group")
        self.site_a.group = self.sitegroup_a
        self.site_a.save(update_fields=["group"])
        self.site_b.group = self.sitegroup_b
        self.site_b.save(update_fields=["group"])
        self.site_a.region = self.region_a
        self.site_a.save(update_fields=["region"])
        self.site_b.region = self.region_b
        self.site_b.save(update_fields=["region"])

        self.accessory_a = Accessory.objects.create(
            name="Delete A Accessory", slug="delete-a-accessory", manufacturer=self.manufacturer
        )
        self.accessory_b = Accessory.objects.create(
            name="Delete B Accessory", slug="delete-b-accessory", manufacturer=self.manufacturer
        )
        create_assignment_fixture(
            AccessoryAssignment,
            accessory=self.accessory_a,
            assigned_location=self.location_a,
            qty=1,
            source_tenant=self.tenant,
            target_tenant=self.tenant,
        )
        create_assignment_fixture(
            AccessoryAssignment,
            accessory=self.accessory_b,
            assigned_location=self.location_b,
            qty=1,
            source_tenant=self.tenant_b,
            target_tenant=self.tenant_b,
        )

        self.consumable_a = Consumable.objects.create(
            name="Delete A Consumable", slug="delete-a-consumable", manufacturer=self.manufacturer
        )
        self.consumable_b = Consumable.objects.create(
            name="Delete B Consumable", slug="delete-b-consumable", manufacturer=self.manufacturer
        )
        create_assignment_fixture(
            ConsumableAssignment,
            consumable=self.consumable_a,
            assigned_location=self.location_a,
            qty=1,
            source_tenant=self.tenant,
            target_tenant=self.tenant,
        )
        create_assignment_fixture(
            ConsumableAssignment,
            consumable=self.consumable_b,
            assigned_location=self.location_b,
            qty=1,
            source_tenant=self.tenant_b,
            target_tenant=self.tenant_b,
        )

        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _asset(self, name, tag, tenant, location, asset_type, asset_role, status):
        return Asset.objects.create(
            name=name,
            asset_tag=tag,
            asset_type=asset_type,
            asset_role=asset_role,
            status=status,
            tenant=tenant,
            location=location,
        )

    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_assettype_guard_uses_visible_count_and_neutral_hidden_message(self):
        response = self.client.post(reverse("assets:assettype_delete", kwargs={"pk": self.assettype_b.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages))
        self.assertFalse(any("1 asset" in message for message in messages))

        response = self.client.post(reverse("assets:assettype_delete", kwargs={"pk": self.assettype_a.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("1 asset" in message for message in messages))

    def test_region_guard_uses_visible_count_and_neutral_hidden_message(self):
        response = self.client.post(reverse("organization:region_delete", kwargs={"pk": self.region_b.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages))
        self.assertFalse(any("1 site" in message for message in messages))

        response = self.client.post(reverse("organization:region_delete", kwargs={"pk": self.region_a.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("1 site" in message for message in messages))

    def test_accessory_guard_uses_visible_count_and_neutral_hidden_message(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        self.assertEqual(AccessoryAssignment.all_objects.filter(accessory=self.accessory_b).count(), 1)
        self.assertEqual(item_assignments_for_scope(AccessoryAssignment, "accessory", self.accessory_b).count(), 0)
        response = self.client.post(
            reverse("inventory:accessory_delete", kwargs={"pk": self.accessory_b.pk}), follow=True
        )
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages), messages)
        self.assertFalse(any("1 active assignment" in message for message in messages))

        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        response = self.client.post(
            reverse("inventory:accessory_delete", kwargs={"pk": self.accessory_a.pk}), follow=True
        )
        messages = self._messages(response)
        self.assertTrue(any("1 active assignment" in message for message in messages), messages)

    def test_consumable_guard_uses_visible_count_and_neutral_hidden_message(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        response = self.client.post(
            reverse("inventory:consumable_delete", kwargs={"pk": self.consumable_b.pk}), follow=True
        )
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages), messages)
        self.assertFalse(any("1 historical consumption" in message for message in messages))

        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        response = self.client.post(
            reverse("inventory:consumable_delete", kwargs={"pk": self.consumable_a.pk}), follow=True
        )
        messages = self._messages(response)
        self.assertTrue(any("1 historical consumption" in message for message in messages), messages)

    def test_assetrole_guard_uses_visible_count_and_neutral_hidden_message(self):
        response = self.client.post(reverse("assets:assetrole_delete", kwargs={"pk": self.assetrole_b.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages), messages)
        self.assertFalse(any("1 asset" in message for message in messages))

        response = self.client.post(reverse("assets:assetrole_delete", kwargs={"pk": self.assetrole_a.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("1 asset" in message for message in messages), messages)

    def test_statuslabel_guard_uses_visible_count_and_neutral_hidden_message(self):
        response = self.client.post(reverse("assets:statuslabel_delete", kwargs={"pk": self.status_b.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages), messages)
        self.assertFalse(any("1 asset" in message for message in messages))

        response = self.client.post(reverse("assets:statuslabel_delete", kwargs={"pk": self.status_a.pk}), follow=True)
        messages = self._messages(response)
        self.assertTrue(any("1 asset" in message for message in messages), messages)

    def test_sitegroup_guard_uses_visible_count_and_neutral_hidden_message(self):
        response = self.client.post(
            reverse("organization:sitegroup_delete", kwargs={"pk": self.sitegroup_b.pk}), follow=True
        )
        messages = self._messages(response)
        self.assertTrue(any("outside your scope" in message for message in messages), messages)
        self.assertFalse(any("1 site" in message for message in messages))

        response = self.client.post(
            reverse("organization:sitegroup_delete", kwargs={"pk": self.sitegroup_a.pk}), follow=True
        )
        messages = self._messages(response)
        self.assertTrue(any("1 site" in message for message in messages), messages)

    def test_assetrole_confirm_page_does_not_render_foreign_count(self):
        response = self.client.get(reverse("assets:assetrole_delete", kwargs={"pk": self.assetrole_b.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This asset role is referenced outside your scope.")
        self.assertNotContains(response, "1 asset")

    def test_sitegroup_confirm_page_does_not_render_foreign_count(self):
        response = self.client.get(reverse("organization:sitegroup_delete", kwargs={"pk": self.sitegroup_b.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Some sites are referenced outside your scope.")
        self.assertNotContains(response, "1 assigned site")

    def test_assettype_confirm_page_does_not_render_foreign_count(self):
        response = self.client.get(reverse("assets:assettype_delete", kwargs={"pk": self.assettype_b.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "referenced outside your scope")
        self.assertNotContains(response, "1 asset")

    def test_location_confirm_page_does_not_render_foreign_asset_count(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        response = self.client.get(reverse("organization:location_delete", kwargs={"pk": self.location_hidden.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Some assets are referenced outside your scope.")
        self.assertNotContains(response, "1 assigned asset")

    def test_region_confirm_page_does_not_render_foreign_count(self):
        response = self.client.get(reverse("organization:region_delete", kwargs={"pk": self.region_b.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "referenced outside your scope")
        self.assertNotContains(response, "1 assigned site")
