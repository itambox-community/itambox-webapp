from django.db import IntegrityError, transaction
from django.test import TestCase

from assets.models import Manufacturer
from inventory.models import Accessory, Component
from organization.models import Tenant


class TenantCatalogueUniquenessTests:
    model_class = None

    def setUp(self):
        super().setUp()
        self.manufacturer = Manufacturer.objects.create(name="Logitech", slug="logitech-catalogue")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="catalogue-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="catalogue-tenant-b")

    def _create_item(self, tenant, slug):
        return self.model_class.objects.create(
            name="USB Receiver",
            slug=slug,
            manufacturer=self.manufacturer,
            tenant=tenant,
        )

    def test_same_product_can_exist_in_two_tenants(self):
        first = self._create_item(self.tenant_a, "usb-receiver-tenant-a")
        second = self._create_item(self.tenant_b, "usb-receiver-tenant-b")

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(
            self.model_class.all_objects.filter(manufacturer=self.manufacturer, name="USB Receiver").count(),
            2,
        )

    def test_active_duplicate_in_one_tenant_is_rejected(self):
        self._create_item(self.tenant_a, "usb-receiver-tenant-a")

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create_item(self.tenant_a, "usb-receiver-tenant-a-duplicate")

    def test_global_duplicates_are_rejected_but_tenant_owned_copy_is_allowed(self):
        self._create_item(None, "usb-receiver-global")
        tenant_item = self._create_item(self.tenant_a, "usb-receiver-tenant-a")

        self.assertIsNotNone(tenant_item.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create_item(None, "usb-receiver-global-duplicate")

    def test_soft_deleted_item_does_not_block_recreation(self):
        removed = self._create_item(self.tenant_a, "usb-receiver-tenant-a")
        removed.delete()

        recreated = self._create_item(self.tenant_a, "usb-receiver-tenant-a-recreated")

        self.assertNotEqual(removed.pk, recreated.pk)
        self.assertEqual(
            self.model_class.objects.filter(manufacturer=self.manufacturer, name="USB Receiver").count(),
            1,
        )


class AccessoryTenantUniquenessTests(TenantCatalogueUniquenessTests, TestCase):
    model_class = Accessory


class ComponentTenantUniquenessTests(TenantCatalogueUniquenessTests, TestCase):
    model_class = Component
