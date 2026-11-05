"""Stock pools on item detail pages are scoped by the pool's location."""

from django.test import TestCase
from django.urls import reverse

from assets.models import Manufacturer
from core.tests.mixins import TenantTestMixin
from inventory.models import (
    Accessory,
    AccessoryStock,
    Component,
    ComponentStock,
    Consumable,
    ConsumableStock,
)
from organization.models import Location, Site, Tenant


class ItemStockPoolScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Stock Scope A",
            slug="stock-scope-a",
            permissions=["inventory.view_accessory", "inventory.view_consumable", "inventory.view_component"],
        )
        self.tenant_b = Tenant.objects.create(name="Stock Scope B", slug="stock-scope-b")

        site_a = Site.objects.create(name="Stock A Site", slug="stock-a-site", tenant=self.tenant)
        site_b = Site.objects.create(name="Stock B Site", slug="stock-b-site", tenant=self.tenant_b)
        self.location_a = Location.objects.create(
            name="Stock A Only Depot", slug="stock-a-only-depot", site=site_a, tenant=self.tenant
        )
        self.location_b = Location.objects.create(
            name="Stock B Only Depot", slug="stock-b-only-depot", site=site_b, tenant=self.tenant_b
        )
        manufacturer = Manufacturer.objects.create(name="Stock Mfr", slug="stock-mfr")

        self.accessory = Accessory.objects.create(
            name="Stock Global Accessory", slug="stock-global-accessory", manufacturer=manufacturer
        )
        self.consumable = Consumable.objects.create(
            name="Stock Global Consumable", slug="stock-global-consumable", manufacturer=manufacturer
        )
        self.component = Component.objects.create(
            name="Stock Global Component", slug="stock-global-component", manufacturer=manufacturer
        )

        for item, stock_model in (
            (self.accessory, AccessoryStock),
            (self.consumable, ConsumableStock),
            (self.component, ComponentStock),
        ):
            stock_model.objects.create(**{item._meta.model_name: item}, location=self.location_a, qty=10)
            stock_model.objects.create(**{item._meta.model_name: item}, location=self.location_b, qty=20)

        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def test_accessory_detail_hides_foreign_stock_pools(self):
        response = self.client.get(reverse("inventory:accessory_detail", kwargs={"pk": self.accessory.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.location_a.name)
        self.assertNotContains(response, self.location_b.name)

    def test_consumable_detail_hides_foreign_stock_pools(self):
        response = self.client.get(reverse("inventory:consumable_detail", kwargs={"pk": self.consumable.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.location_a.name)
        self.assertNotContains(response, self.location_b.name)

    def test_component_detail_hides_foreign_stock_pools(self):
        response = self.client.get(reverse("inventory:component_detail", kwargs={"pk": self.component.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.location_a.name)
        self.assertNotContains(response, self.location_b.name)
