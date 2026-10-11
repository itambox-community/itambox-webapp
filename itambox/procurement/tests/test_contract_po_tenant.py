"""A contract may only reference a purchase order of its own tenant (#713)."""

import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from assets.models import Supplier
from core.tests.mixins import grant
from organization.models import Location, Role, Site, Tenant, TenantGroup
from procurement.models import Contract, PurchaseOrder

User = get_user_model()


class ContractPurchaseOrderFixtureMixin:
    def setUp(self):
        super().setUp()
        self.group = TenantGroup.objects.create(name="PO Group", slug="po-group")
        self.tenant_a = Tenant.objects.create(name="PO Tenant A", slug="po-tenant-a", group=self.group)
        self.tenant_b = Tenant.objects.create(name="PO Tenant B", slug="po-tenant-b", group=self.group)
        self.user = User.objects.create_user(username="po-link-user", password="password")
        for tenant in (self.tenant_a, self.tenant_b):
            role = Role.objects.create(
                tenant=tenant,
                name=f"Procurement {tenant.slug}",
                permissions=[
                    "procurement.view_contract",
                    "procurement.add_contract",
                    "procurement.change_contract",
                    "procurement.view_purchaseorder",
                ],
            )
            grant(self.user, tenant, role)
        self.supplier = Supplier.objects.create(name="PO Supplier", slug="po-supplier")
        self.contract_a = self._contract(self.tenant_a, "CTR-PO-A")
        self.po_a = self._po(self.tenant_a, "PO-A")
        self.po_b = self._po(self.tenant_b, "PO-B")

    def _contract(self, tenant, number):
        return Contract.objects.create(
            tenant=tenant,
            name=f"Contract {number}",
            contract_number=number,
            contract_type="support",
            status="active",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )

    def _po(self, tenant, number):
        site = Site.objects.get_or_create(name="PO Site", slug="po-site")[0]
        location = Location.objects.create(name=f"Loc {number}", slug=f"loc-{number.lower()}", site=site, tenant=tenant)
        return PurchaseOrder.objects.create(
            tenant=tenant, order_number=number, supplier=self.supplier, destination_location=location
        )

    def _scope(self, *, tenant=None, group=None, all_accessible=False):
        self.client.force_login(self.user)
        session = self.client.session
        for key in ("active_tenant_id", "active_tenant_group_id", "active_all_accessible"):
            session.pop(key, None)
        if tenant is not None:
            session["active_tenant_id"] = tenant.pk
        elif group is not None:
            session["active_tenant_group_id"] = group.pk
        elif all_accessible:
            session["active_all_accessible"] = True
        session.save()

    def _etag(self, contract):
        contract.refresh_from_db()
        return 'W/"' + contract.updated_at.isoformat() + '"'


class ContractPurchaseOrderModelTests(ContractPurchaseOrderFixtureMixin, TestCase):
    def test_clean_rejects_foreign_tenant_purchase_order(self):
        self.contract_a.purchase_order = self.po_b
        with self.assertRaises(ValidationError) as ctx:
            self.contract_a.full_clean(validate_unique=False)
        self.assertIn("purchase_order", ctx.exception.message_dict)

    def test_clean_accepts_same_tenant_and_cleared_purchase_order(self):
        self.contract_a.purchase_order = self.po_a
        self.contract_a.full_clean(validate_unique=False)
        self.contract_a.purchase_order = None
        self.contract_a.full_clean(validate_unique=False)


class ContractPurchaseOrderFormTests(ContractPurchaseOrderFixtureMixin, TestCase):
    def _payload(self, purchase_order):
        c = self.contract_a
        return {
            "name": c.name,
            "contract_number": c.contract_number,
            "contract_type": c.contract_type,
            "status": c.status,
            "currency": c.currency,
            "billing_cycle": c.billing_cycle,
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat(),
            "tenant": self.tenant_a.pk,
            "purchase_order": purchase_order.pk if purchase_order else "",
        }

    def _post(self, purchase_order):
        return self.client.post(
            reverse("procurement:contract_edit", kwargs={"pk": self.contract_a.pk}), self._payload(purchase_order)
        )

    def test_foreign_po_rejected_in_every_scope(self):
        scopes = {
            "tenant": {"tenant": self.tenant_a},
            "group": {"group": self.group},
            "all_accessible": {"all_accessible": True},
        }
        for label, scope in scopes.items():
            with self.subTest(scope=label):
                self._scope(**scope)
                response = self._post(self.po_b)
                self.assertEqual(response.status_code, 200, label)
                self.assertIn("purchase_order", response.context["form"].errors)
                self.contract_a.refresh_from_db()
                self.assertIsNone(self.contract_a.purchase_order_id)

    def test_same_tenant_link_and_clearing_remain_supported(self):
        for label, scope in {"group": {"group": self.group}, "all_accessible": {"all_accessible": True}}.items():
            with self.subTest(scope=label):
                self._scope(**scope)
                self.assertEqual(self._post(self.po_a).status_code, 302)
                self.contract_a.refresh_from_db()
                self.assertEqual(self.contract_a.purchase_order_id, self.po_a.pk)
                self.assertEqual(self._post(None).status_code, 302)
                self.contract_a.refresh_from_db()
                self.assertIsNone(self.contract_a.purchase_order_id)


class ContractPurchaseOrderAPITests(ContractPurchaseOrderFixtureMixin, APITestCase):
    def _url(self):
        return reverse("api:procurement_api:contract-detail", kwargs={"pk": self.contract_a.pk})

    def _patch(self, body):
        return self.client.patch(self._url(), body, format="json", HTTP_IF_MATCH=self._etag(self.contract_a))

    def test_group_scope_partial_update_rejects_foreign_po(self):
        self._scope(group=self.group)
        response = self._patch({"purchase_order": self.po_b.pk})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.assertIn("purchase_order", response.data)
        self.contract_a.refresh_from_db()
        self.assertIsNone(self.contract_a.purchase_order_id)

    def test_same_tenant_link_and_clearing_remain_supported(self):
        self._scope(group=self.group)
        self.assertEqual(self._patch({"purchase_order": self.po_a.pk}).status_code, status.HTTP_200_OK)
        self.contract_a.refresh_from_db()
        self.assertEqual(self.contract_a.purchase_order_id, self.po_a.pk)
        self.assertEqual(self._patch({"purchase_order": None}).status_code, status.HTTP_200_OK)
        self.contract_a.refresh_from_db()
        self.assertIsNone(self.contract_a.purchase_order_id)

    def test_inaccessible_po_is_still_rejected_by_scope(self):
        other = Tenant.objects.create(name="PO Tenant X", slug="po-tenant-x")
        po_x = self._po(other, "PO-X")
        self._scope(group=self.group)
        response = self._patch({"purchase_order": po_x.pk})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.contract_a.refresh_from_db()
        self.assertIsNone(self.contract_a.purchase_order_id)
