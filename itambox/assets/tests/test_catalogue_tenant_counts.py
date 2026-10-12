"""Tenant isolation for catalogue counters and asset-type request badges."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRequest, AssetRole, AssetType, Category, Manufacturer, StatusLabel
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, Component, Consumable
from organization.models import Role, Tenant, TenantGroup

User = get_user_model()


class CatalogueTenantCounterTests(TenantTestMixin, TestCase):
    """Shared catalogue objects must count only live children in the request scope."""

    permissions = (
        "assets.view_asset",
        "assets.view_assetrequest",
        "assets.view_assetrole",
        "assets.view_assettype",
        "assets.view_category",
        "assets.view_manufacturer",
        "assets.view_statuslabel",
        "inventory.view_accessory",
        "inventory.view_component",
        "inventory.view_consumable",
    )

    def setUp(self):
        self.setup_tenant_context(
            name="Counter Tenant A",
            slug="counter-tenant-a",
            permissions=list(self.permissions),
        )
        self.group = TenantGroup.objects.create(name="Counter Group", slug="counter-group")
        self.tenant.group = self.group
        self.tenant.save(update_fields=["group"])
        self.tenant_b = Tenant.objects.create(name="Counter Tenant B", slug="counter-tenant-b", group=self.group)
        self.tenant_c = Tenant.objects.create(name="Counter Tenant C", slug="counter-tenant-c")

        role_b = Role.objects.create(tenant=self.tenant_b, name="Counter Role B", permissions=list(self.permissions))
        self.grant(self.tenant_user, self.tenant_b, role_b)
        requester_b = User.objects.create_user(username="counter-requester-b", password="password")
        requester_c = User.objects.create_user(username="counter-requester-c", password="password")
        self.requesters = {
            self.tenant: self.tenant_user,
            self.tenant_b: requester_b,
            self.tenant_c: requester_c,
        }

        self.manufacturer = Manufacturer.objects.create(name="Counter Manufacturer", slug="counter-manufacturer")
        self.asset_role = AssetRole.objects.create(name="Counter Role", slug="counter-asset-role")
        self.status = StatusLabel.objects.create(
            name="Counter Status",
            slug="counter-status",
            type=StatusLabel.TYPE_DEPLOYABLE,
        )
        self.category = Category.objects.create(name="Counter Category", slug="counter-category")
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="Counter Model",
            slug="counter-model",
            category=self.category,
            asset_role=self.asset_role,
            requestable=True,
        )
        deleted_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="Deleted Counter Model",
            slug="deleted-counter-model",
            category=self.category,
        )
        deleted_type.delete()

        self.assets = {
            "a": self._make_asset(self.tenant, "A"),
            "b": self._make_asset(self.tenant_b, "B"),
            "c": self._make_asset(self.tenant_c, "C"),
        }
        deleted_asset = self._make_asset(self.tenant, "A-DELETED")
        deleted_asset.delete()

        self.inventory_items = {Accessory: {}, Consumable: {}, Component: {}}
        for item_model, label in (
            (Accessory, "Accessory"),
            (Consumable, "Consumable"),
            (Component, "Component"),
        ):
            for suffix, tenant in (("A", self.tenant), ("B", self.tenant_b), ("C", self.tenant_c)):
                self.inventory_items[item_model][suffix] = item_model.objects.create(
                    name="Counter " + label + " " + suffix,
                    manufacturer=self.manufacturer,
                    category=self.category,
                    tenant=tenant,
                )
            deleted_item = item_model.objects.create(
                name="Counter " + label + " A Deleted",
                manufacturer=self.manufacturer,
                category=self.category,
                tenant=self.tenant,
            )
            deleted_item.delete()

        self.requests = {}
        for suffix, tenant in (("A", self.tenant), ("B", self.tenant_b), ("C", self.tenant_c)):
            self.requests[suffix] = AssetRequest.objects.create(
                requester=self.requesters[tenant],
                asset_type=self.asset_type,
                tenant=tenant,
            )
        deleted_request = AssetRequest.objects.create(
            requester=self.tenant_user,
            asset_type=self.asset_type,
            tenant=self.tenant,
        )
        deleted_request.delete()

    def _make_asset(self, tenant, suffix):
        return Asset.objects.create(
            name="Counter Asset " + suffix,
            asset_tag="COUNTER-781-" + suffix,
            serial_number="COUNTER-781-SN-" + suffix,
            asset_type=self.asset_type,
            asset_role=self.asset_role,
            status=self.status,
            tenant=tenant,
        )

    def _login_for_scope(self, scope):
        self.client.force_login(self.tenant_user)
        session = self.client.session
        for key in ("active_tenant_id", "active_tenant_group_id", "active_all_accessible"):
            session.pop(key, None)
        if scope == "tenant":
            session["active_tenant_id"] = self.tenant.pk
        elif scope == "group":
            session["active_tenant_group_id"] = self.group.pk
        else:
            session["active_all_accessible"] = True
        session.save()

    def _list_record(self, route, object_id):
        response = self.client.get(reverse(route))
        self.assertEqual(response.status_code, 200, response.content.decode()[:500])
        return next(row.record for row in response.context["table"].rows if row.record.pk == object_id)

    def _api_count(self, route, object_id):
        response = self.client.get(reverse(route, kwargs={"pk": object_id}))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["asset_count"]

    def _request_badge_count(self):
        response = self.client.get(reverse("assets:assettype_detail", kwargs={"pk": self.asset_type.pk}))
        self.assertEqual(response.status_code, 200, response.content.decode()[:500])
        match = re.search(
            r'href="\?tab=requests".*?bg-yellow-lt ms-2">(\d+)</span>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertIsNotNone(match, "Asset Type detail page did not render the Requests badge")
        return int(match.group(1))

    def _counts(self):
        role = self._list_record("assets:assetrole_list", self.asset_role.pk)
        status = self._list_record("assets:statuslabel_list", self.status.pk)
        manufacturer = self._list_record("assets:manufacturer_list", self.manufacturer.pk)
        category = self._list_record("assets:category_list", self.category.pk)
        return {
            "asset_role_list": role.asset_count,
            "status_label_list": status.asset_count,
            "manufacturer_asset_list": manufacturer.asset_count,
            "manufacturer_asset_types": manufacturer.asset_type_count,
            "category_asset_types": category.assettype_count,
            "category_accessories": category.accessory_count,
            "category_consumables": category.consumable_count,
            "category_components": category.component_count,
            "asset_role_api": self._api_count("api:assets_api:assetrole-detail", self.asset_role.pk),
            "manufacturer_api": self._api_count("api:assets_api:manufacturer-detail", self.manufacturer.pk),
            "asset_type_request_badge": self._request_badge_count(),
        }

    def test_tenant_scope_counts_hide_foreign_and_deleted_rows(self):
        self._login_for_scope("tenant")
        counts = self._counts()
        for surface in (
            "asset_role_list",
            "status_label_list",
            "manufacturer_asset_list",
            "category_accessories",
            "category_consumables",
            "category_components",
            "asset_role_api",
            "manufacturer_api",
            "asset_type_request_badge",
        ):
            with self.subTest(surface=surface):
                self.assertEqual(counts[surface], 1)
        self.assertEqual(counts["manufacturer_asset_types"], 1)
        self.assertEqual(counts["category_asset_types"], 1)

    def test_counts_follow_tenant_group_and_all_accessible_scopes(self):
        expected_by_scope = {"tenant": 1, "group": 2, "all_accessible": 2}
        for scope, expected in expected_by_scope.items():
            with self.subTest(scope=scope):
                self._login_for_scope(scope)
                counts = self._counts()
                for surface in (
                    "asset_role_list",
                    "status_label_list",
                    "manufacturer_asset_list",
                    "category_accessories",
                    "category_consumables",
                    "category_components",
                    "asset_role_api",
                    "manufacturer_api",
                    "asset_type_request_badge",
                ):
                    with self.subTest(surface=surface):
                        self.assertEqual(counts[surface], expected)
                self.assertEqual(counts["manufacturer_asset_types"], 1)
                self.assertEqual(counts["category_asset_types"], 1)

    def _assert_single_tenant_counts(self, stage):
        counts = self._counts()
        for surface in (
            "asset_role_list",
            "status_label_list",
            "manufacturer_asset_list",
            "category_accessories",
            "category_consumables",
            "category_components",
            "asset_role_api",
            "manufacturer_api",
            "asset_type_request_badge",
        ):
            with self.subTest(stage=stage, surface=surface):
                self.assertEqual(counts[surface], 1)

    def test_tenant_scope_totals_do_not_change_when_other_tenant_records_change(self):
        self._login_for_scope("tenant")
        self._assert_single_tenant_counts("initial")

        added_asset = self._make_asset(self.tenant_b, "B-ADDED")
        added_accessory = Accessory.objects.create(
            name="Counter Accessory B Added",
            manufacturer=self.manufacturer,
            category=self.category,
            tenant=self.tenant_b,
        )
        added_requester = User.objects.create_user(username="counter-requester-b-added", password="password")
        added_request = AssetRequest.objects.create(
            requester=added_requester,
            asset_type=self.asset_type,
            tenant=self.tenant_b,
        )
        self._assert_single_tenant_counts("after foreign inserts")

        added_asset.delete()
        added_accessory.delete()
        added_request.delete()
        self._assert_single_tenant_counts("after foreign soft deletes")

        self.assets["b"].delete()
        self.inventory_items[Accessory]["B"].delete()
        self.requests["B"].delete()
        self._assert_single_tenant_counts("after deleting original foreign records")
