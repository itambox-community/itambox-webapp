"""Children of an Asset are never wider than the asset itself (F10).

A tenant-less asset is hidden from every tenant scope. Its lifecycle children
(maintenance, assignment, disposal, warranty, reservation, installed software)
must be hidden as well instead of widening through ``asset.tenant IS NULL``.
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import (
    Asset,
    AssetAssignment,
    AssetDisposal,
    AssetMaintenance,
    AssetReservation,
    StatusLabel,
    Warranty,
)
from core.managers import Scope
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Tenant
from software.models import InstalledSoftware, Software

User = get_user_model()

TODAY = datetime.date(2026, 6, 1)


def _make_child(model, asset, holder):
    if model is AssetMaintenance:
        return model.objects.create(asset=asset, start_date=TODAY)
    if model is AssetAssignment:
        return model.objects.create(asset=asset, assigned_user=holder, is_active=False)
    if model is AssetDisposal:
        return model.objects.create(asset=asset, disposal_date=TODAY)
    if model is Warranty:
        return model.objects.create(asset=asset, start_date=TODAY, end_date=TODAY)
    if model is AssetReservation:
        return model.objects.create(asset=asset, reserved_for=holder, start_date=TODAY, end_date=TODAY)
    return model.objects.create(asset=asset, software=baker.make(Software, tenant=asset.tenant))


class LifecycleChildScopeTests(TenantTestMixin, TestCase):
    models = (AssetMaintenance, AssetAssignment, AssetDisposal, Warranty, AssetReservation, InstalledSoftware)

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Life A", slug="life-a")
        cls.tenant_b = Tenant.objects.create(name="Life B", slug="life-b")
        status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        cls.global_asset = baker.make(Asset, tenant=None, asset_tag="LIFE-G", status=status)
        cls.asset_a = baker.make(Asset, tenant=cls.tenant_a, asset_tag="LIFE-A", status=status)
        holder = AssetHolder.objects.create(
            first_name="Life", last_name="Holder", upn="life@a.test", tenant=cls.tenant_a
        )
        cls.global_children = {m: _make_child(m, cls.global_asset, holder) for m in cls.models}
        cls.a_children = {m: _make_child(m, cls.asset_a, holder) for m in cls.models}

    def _pks(self, model, tenant):
        return set(model.objects.for_scope(Scope(Scope.TENANT, tenant=tenant)).values_list("pk", flat=True))

    def test_children_of_tenantless_asset_are_hidden_from_every_tenant(self):
        for model in self.models:
            for tenant in (self.tenant_a, self.tenant_b):
                with self.subTest(model=model.__name__, tenant=tenant.slug):
                    self.assertNotIn(self.global_children[model].pk, self._pks(model, tenant))

    def test_children_of_tenant_asset_are_visible_to_owner_only(self):
        for model in self.models:
            with self.subTest(model=model.__name__):
                pk = self.a_children[model].pk
                self.assertIn(pk, self._pks(model, self.tenant_a))
                self.assertNotIn(pk, self._pks(model, self.tenant_b))

    def test_scoped_sql_no_longer_widens_on_null_parent_tenant(self):
        for model in self.models:
            with self.subTest(model=model.__name__):
                sql = str(model.objects.for_scope(Scope(Scope.TENANT, tenant=self.tenant_a)).query)
                self.assertNotIn('"tenant_id" IS NULL', sql)
                self.assertIn('"assets_asset"."tenant_id" IN (', sql)

    def test_maintenance_list_hides_child_of_tenantless_asset(self):
        user = User.objects.create_user(username="life-actor", password="x")
        self.client_login_to_tenant(user, self.tenant_a, role_permissions=["assets.view_assetmaintenance"])
        response = self.client.get(reverse("assets:assetmaintenance_list"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("LIFE-A", content)
        self.assertNotIn("LIFE-G", content)
