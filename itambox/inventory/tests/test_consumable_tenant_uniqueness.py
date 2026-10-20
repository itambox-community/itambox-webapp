"""Catalogue consumable uniqueness is scoped per tenant (#767).

A second tenant of the same installation must be able to stock the same
product, so the active-row uniqueness moves from ``(manufacturer, name)`` to
``(tenant, manufacturer, name)``. Tenantless (global) rows keep their own
single-name rule, mirroring the tenant/global split used by ``Supplier``.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from assets.models import Manufacturer
from core.tests.mixins import TenantTestMixin
from inventory.models import Consumable
from organization.models import Tenant

User = get_user_model()


class ConsumableUniquenessTests(TenantTestMixin, TestCase):
    """Constraint behaviour of the catalogue row itself."""

    def setUp(self):
        self.manufacturer = Manufacturer.objects.create(name="HP", slug="hp")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="t767-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="t767-b")

    def _consumable(self, tenant, name="26A Toner"):
        return Consumable.objects.create(name=name, manufacturer=self.manufacturer, tenant=tenant)

    def test_two_tenants_can_stock_the_same_product(self):
        first = self._consumable(self.tenant_a)
        second = self._consumable(self.tenant_b)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(
            Consumable.all_objects.filter(manufacturer=self.manufacturer, name="26A Toner").count(),
            2,
        )

    def test_same_tenant_duplicate_is_rejected(self):
        self._consumable(self.tenant_a)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._consumable(self.tenant_a)

    def test_tenant_row_may_share_the_product_with_a_global_row(self):
        self._consumable(None)
        self._consumable(self.tenant_a)

        self.assertEqual(Consumable.all_objects.filter(name="26A Toner").count(), 2)

    def test_global_rows_keep_a_single_name(self):
        self._consumable(None)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._consumable(None)

    def test_soft_deleted_row_does_not_block_the_same_name(self):
        removed = self._consumable(self.tenant_a)
        removed.delete()

        recreated = self._consumable(self.tenant_a)

        self.assertNotEqual(removed.pk, recreated.pk)
        self.assertEqual(Consumable.objects.filter(manufacturer=self.manufacturer, name="26A Toner").count(), 1)


class ConsumableUniquenessFormTests(TestCase):
    """The catalogue create form reports the conflict instead of failing."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="uniquenessadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="uniquenessadmin", password="testpassword")
        self.manufacturer = Manufacturer.objects.create(name="HP", slug="hp")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="t767-form-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="t767-form-b")
        self.existing = Consumable.objects.create(
            name="26A Toner", manufacturer=self.manufacturer, tenant=self.tenant_a
        )

    def _post(self, tenant, slug):
        return self.client.post(
            reverse("inventory:consumable_create"),
            {
                "manufacturer": self.manufacturer.pk,
                "name": "26A Toner",
                "slug": slug,
                "tenant": tenant.pk,
            },
        )

    def _active_rows(self):
        return Consumable.all_objects.filter(manufacturer=self.manufacturer, name="26A Toner").count()

    def test_duplicate_in_the_same_tenant_is_a_form_error(self):
        response = self._post(self.tenant_a, "hp-26a-toner-duplicate")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(self._active_rows(), 1)

    def test_the_same_product_is_created_for_another_tenant(self):
        response = self._post(self.tenant_b, "hp-26a-toner-second-tenant")

        if response.status_code != 302:
            self.fail(f"Form invalid. Errors: {response.context['form'].errors}")
        self.assertEqual(self._active_rows(), 2)
