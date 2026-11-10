"""Inventory assignment visibility is owned by the assignment, not the item.

An assignment belongs to its ``source_tenant`` (owner side) and is readable by
its ``target_tenant`` (recipient side, ADR-0001 phase 4b). The catalogue
item's tenant decides nothing: a GLOBAL catalogue item (``tenant=None``) used
by several tenants must not expose one tenant's holders to another on the
assignment lists, the item detail tables or the REST API.

Each case runs for accessories, consumables and components.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.models import Manufacturer
from core.managers import Scope
from core.tests.mixins import TenantTestMixin, grant
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
)
from inventory.services import checkout_inventory_item
from organization.models import AssetHolder, Location, Role, Site, Tenant, TenantResourceGrant

User = get_user_model()


class _AssignmentVisibilityCases:
    """Shared world and cases; concrete classes bind one inventory kind."""

    item_model = None
    stock_model = None
    assignment_model = None
    item_field = ""
    assignment_name = ""
    list_route = ""
    detail_route = ""
    api_list_route = ""
    api_detail_route = ""
    item_list_route = ""
    api_item_list_route = ""
    api_item_detail_route = ""
    quantity_field = ""
    available_field = ""
    checkin_route = None

    @classmethod
    def _perms(cls):
        item = cls.item_field
        assignment = cls.assignment_name
        return [
            f"inventory.view_{item}",
            f"inventory.change_{item}",
            f"inventory.view_{assignment}",
            f"inventory.add_{assignment}",
        ]

    @classmethod
    def _tenant_world(cls, key):
        tenant = Tenant.objects.create(name=f"{cls.prefix} {key}", slug=f"{cls.prefix}-{key}")
        site = Site.objects.create(name=f"{cls.prefix} {key} Site", slug=f"{cls.prefix}-{key}-site", tenant=tenant)
        location = Location.objects.create(
            name=f"{cls.prefix} {key} Depot",
            slug=f"{cls.prefix}-{key}-depot",
            site=site,
            tenant=tenant,
        )
        holder = AssetHolder.objects.create(
            first_name=f"Holder{key.title()}",
            last_name=cls.prefix.title(),
            upn=f"holder.{key}@{cls.prefix}-{key}.test",
            tenant=tenant,
        )
        actor = User.objects.create_user(username=f"{cls.prefix}-{key}-actor", password="x")
        role = Role.objects.create(tenant=tenant, name=f"{cls.prefix} {key} role", permissions=cls._perms())
        grant(actor, tenant, role)
        return tenant, location, holder, actor

    @classmethod
    def _checkout(cls, tenant, actor, item, holder, source_location=None):
        with cls._tenant_scope(tenant):
            return checkout_inventory_item(item, 1, holder=holder, source_location=source_location, user=actor)

    @staticmethod
    def _tenant_scope(tenant):
        helper = TenantTestMixin()
        return helper.tenant_context(tenant)

    @classmethod
    def setUpTestData(cls):
        cls.prefix = cls.item_field[:4] + "vis"
        cls.tenant_a, cls.location_a, cls.holder_a, cls.actor_a = cls._tenant_world("alpha")
        cls.tenant_b, cls.location_b, cls.holder_b, cls.actor_b = cls._tenant_world("beta")
        cls.tenant_c, cls.location_c, cls.holder_c, cls.actor_c = cls._tenant_world("gamma")
        manufacturer = Manufacturer.objects.create(name=f"{cls.prefix} Mfg", slug=f"{cls.prefix}-mfg")

        # A GLOBAL catalogue item, stocked in tenant A's depot.
        cls.global_item = cls.item_model.objects.create(
            name=f"{cls.prefix} Global Item", slug=f"{cls.prefix}-global", manufacturer=manufacturer, tenant=None
        )
        cls.stock_model.objects.create(**{cls.item_field: cls.global_item}, location=cls.location_a, qty=10)
        cls.stock_model.objects.create(**{cls.item_field: cls.global_item}, location=cls.location_b, qty=20)
        # Tenant A draws from its own pool (source A); tenant B checks out
        # target-only (no source pool: source_tenant stays NULL, target B).
        cls.global_row_a = cls._checkout(
            cls.tenant_a, cls.actor_a, cls.global_item, cls.holder_a, source_location=cls.location_a
        )
        cls.global_row_b = cls._checkout(cls.tenant_b, cls.actor_b, cls.global_item, cls.holder_b)

        # An A-owned item checked out from A's pool INTO tenant B (inbound,
        # authorized by a use grant): source A, target B.
        cls.owned_item = cls.item_model.objects.create(
            name=f"{cls.prefix} Owned Item", slug=f"{cls.prefix}-owned", manufacturer=manufacturer, tenant=cls.tenant_a
        )
        owned_stock = cls.stock_model.objects.create(
            **{cls.item_field: cls.owned_item}, location=cls.location_a, qty=10
        )
        TenantResourceGrant.objects.create(
            tenant=cls.tenant_a,
            grantee_tenant=cls.tenant_b,
            resource_type=ContentType.objects.get_for_model(cls.stock_model),
            resource_id=owned_stock.pk,
            access_level=TenantResourceGrant.ACCESS_USE,
        )
        cls.inbound_row = cls._checkout(
            cls.tenant_b, cls.actor_b, cls.owned_item, cls.holder_b, source_location=cls.location_a
        )

    # ------------------------------------------------------------ helpers
    def _login(self, tenant, key):
        viewer = User.objects.create_user(username=f"{self.prefix}-{key}-viewer", password="x")
        self.client_login_to_tenant(viewer, tenant, role_permissions=self._perms())
        return viewer

    @staticmethod
    def _holder_url(holder):
        return reverse("organization:assetholder_detail", kwargs={"pk": holder.pk})

    def _assert_holders(self, response, visible, hidden):
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for holder in visible:
            self.assertIn(holder.upn, content)
            self.assertIn(self._holder_url(holder), content)
        for holder in hidden:
            self.assertNotIn(holder.upn, content)
            self.assertNotIn(self._holder_url(holder), content)

    def _api_ids(self):
        response = self.client.get(reverse(self.api_list_route), {"limit": 100})
        self.assertEqual(response.status_code, 200)
        return {row["id"] for row in response.json()["results"]}

    # ------------------------------------------------- provenance premise
    def test_fixture_provenance(self):
        self.assertEqual(self.global_row_a.source_tenant_id, self.tenant_a.pk)
        self.assertEqual(self.global_row_a.target_tenant_id, self.tenant_a.pk)
        self.assertIsNone(self.global_row_b.source_tenant_id)
        self.assertEqual(self.global_row_b.target_tenant_id, self.tenant_b.pk)
        self.assertEqual(self.inbound_row.source_tenant_id, self.tenant_a.pk)
        self.assertEqual(self.inbound_row.target_tenant_id, self.tenant_b.pk)

    # --------------------------------------------- global catalogue item
    def test_model_scope_hides_foreign_rows_of_a_global_item(self):
        manager = self.assignment_model.objects
        rows = {
            "alpha": (self.tenant_a, {self.global_row_a.pk, self.inbound_row.pk}),
            "beta": (self.tenant_b, {self.global_row_b.pk}),
            "gamma": (self.tenant_c, set()),
        }
        for key, (tenant, expected) in rows.items():
            with self.subTest(tenant=key):
                scoped = manager.for_scope(Scope(Scope.TENANT, tenant=tenant))
                self.assertEqual(set(scoped.values_list("pk", flat=True)), expected)

    def test_list_view_shows_only_own_holders_of_a_global_item(self):
        cases = (
            ("alpha", self.tenant_a, [self.holder_a], [self.holder_c]),
            ("beta", self.tenant_b, [self.holder_b], [self.holder_a, self.holder_c]),
            ("gamma", self.tenant_c, [], [self.holder_a, self.holder_b]),
        )
        for key, tenant, visible, hidden in cases:
            with self.subTest(tenant=key):
                self._login(tenant, f"list-{key}")
                self._assert_holders(self.client.get(reverse(self.list_route)), visible, hidden)

    def test_global_item_detail_table_shows_only_own_holders(self):
        url = reverse(self.detail_route, kwargs={"pk": self.global_item.pk})
        cases = (
            ("alpha", self.tenant_a, [self.holder_a], [self.holder_b, self.holder_c]),
            ("beta", self.tenant_b, [self.holder_b], [self.holder_a, self.holder_c]),
            ("gamma", self.tenant_c, [], [self.holder_a, self.holder_b]),
        )
        for key, tenant, visible, hidden in cases:
            with self.subTest(tenant=key):
                self._login(tenant, f"detail-{key}")
                self._assert_holders(self.client.get(url), visible, hidden)

    def test_api_list_shows_only_own_rows_of_a_global_item(self):
        cases = (
            ("alpha", self.tenant_a, {self.global_row_a.pk, self.inbound_row.pk}),
            ("beta", self.tenant_b, {self.global_row_b.pk, self.inbound_row.pk}),
            ("gamma", self.tenant_c, set()),
        )
        for key, tenant, expected in cases:
            with self.subTest(tenant=key):
                self._login(tenant, f"api-{key}")
                self.assertEqual(self._api_ids(), expected)

    def test_active_assignments_are_scoped_on_a_global_item(self):
        with self._tenant_scope(self.tenant_a):
            scoped_item = self.item_model.objects.get(pk=self.global_item.pk)
            self.assertEqual(
                set(scoped_item.active_assignments.values_list("pk", flat=True)),
                {self.global_row_a.pk},
            )

    def test_global_item_aggregates_are_scoped_in_list_detail_and_api(self):
        cases = (
            ("agg-alpha", self.tenant_a, 9, 1, 9),
            ("agg-beta", self.tenant_b, 20, 1, 19),
            ("agg-gamma", self.tenant_c, 0, 0, 0),
        )
        detail_url = reverse(self.detail_route, kwargs={"pk": self.global_item.pk})
        api_detail_url = reverse(self.api_item_detail_route, kwargs={"pk": self.global_item.pk})
        for key, tenant, total, quantity, available in cases:
            with self.subTest(tenant=key):
                self._login(tenant, key)
                detail = self.client.get(detail_url)
                self.assertEqual(detail.status_code, 200)
                obj = detail.context["object"]
                self.assertEqual(obj.total_stock, total)
                self.assertEqual(getattr(obj, self.quantity_field), quantity)
                self.assertEqual(getattr(obj, self.available_field), available)

                list_response = self.client.get(reverse(self.item_list_route))
                self.assertEqual(list_response.status_code, 200)
                row = next(item for item in list_response.context["table"].data if item.pk == self.global_item.pk)
                self.assertEqual(row.total_stock, total)
                self.assertEqual(getattr(row, self.quantity_field), quantity)
                self.assertEqual(getattr(row, self.available_field), available)

                api_detail = self.client.get(api_detail_url)
                self.assertEqual(api_detail.status_code, 200)
                payload = api_detail.json()
                self.assertEqual(payload["total_stock"], total)
                self.assertEqual(payload[self.quantity_field], quantity)
                self.assertEqual(payload[self.available_field], available)

                api_list = self.client.get(reverse(self.api_item_list_route), {"limit": 100})
                self.assertEqual(api_list.status_code, 200)
                api_row = next(item for item in api_list.json()["results"] if item["id"] == self.global_item.pk)
                self.assertEqual(api_row["total_stock"], total)
                self.assertEqual(api_row[self.quantity_field], quantity)
                self.assertEqual(api_row[self.available_field], available)

    # ------------------------------------------ inbound cross-tenant row
    def test_inbound_row_visible_to_owner_and_recipient_only(self):
        detail = reverse(self.api_detail_route, kwargs={"pk": self.inbound_row.pk})
        for key, tenant, status in (
            ("alpha", self.tenant_a, 200),
            ("beta", self.tenant_b, 200),
            ("gamma", self.tenant_c, 404),
        ):
            with self.subTest(tenant=key):
                self._login(tenant, f"inbound-{key}")
                self.assertEqual(self.client.get(detail).status_code, status)

    def test_owned_item_detail_table_shows_inbound_recipient_to_owner(self):
        self._login(self.tenant_a, "owned-detail")
        response = self.client.get(reverse(self.detail_route, kwargs={"pk": self.owned_item.pk}))
        self._assert_holders(response, [self.holder_b], [self.holder_a, self.holder_c])

    def test_recipient_check_in_and_foreign_denial(self):
        if self.checkin_route is None:
            return
        url = reverse(self.checkin_route, kwargs={"pk": self.inbound_row.pk})
        self._login(self.tenant_c, "checkin-gamma")
        self.assertEqual(self.client.post(url).status_code, 404)
        self.inbound_row.refresh_from_db()
        self.assertIsNone(self.inbound_row.deleted_at)

        self.client.logout()
        self._login(self.tenant_b, "checkin-beta")
        self.assertIn(self.client.post(url).status_code, (200, 302))
        self.inbound_row.refresh_from_db()
        self.assertIsNotNone(self.inbound_row.deleted_at)


class AccessoryAssignmentVisibilityTests(_AssignmentVisibilityCases, TenantTestMixin, TestCase):
    item_model = Accessory
    stock_model = AccessoryStock
    assignment_model = AccessoryAssignment
    item_field = "accessory"
    assignment_name = "accessoryassignment"
    list_route = "inventory:accessoryassignment_list"
    detail_route = "inventory:accessory_detail"
    api_list_route = "api:inventory_api:accessoryassignment-list"
    api_detail_route = "api:inventory_api:accessoryassignment-detail"
    item_list_route = "inventory:accessory_list"
    api_item_list_route = "api:inventory_api:accessory-list"
    api_item_detail_route = "api:inventory_api:accessory-detail"
    quantity_field = "checked_out_qty"
    available_field = "available"
    checkin_route = "inventory:accessory_checkin"


class ConsumableAssignmentVisibilityTests(_AssignmentVisibilityCases, TenantTestMixin, TestCase):
    item_model = Consumable
    stock_model = ConsumableStock
    assignment_model = ConsumableAssignment
    item_field = "consumable"
    assignment_name = "consumableassignment"
    list_route = "inventory:consumableassignment_list"
    detail_route = "inventory:consumable_detail"
    api_list_route = "api:inventory_api:consumableassignment-list"
    api_detail_route = "api:inventory_api:consumableassignment-detail"
    item_list_route = "inventory:consumable_list"
    api_item_list_route = "api:inventory_api:consumable-list"
    api_item_detail_route = "api:inventory_api:consumable-detail"
    quantity_field = "consumed_qty"
    available_field = "available"


class ComponentAllocationVisibilityTests(_AssignmentVisibilityCases, TenantTestMixin, TestCase):
    item_model = Component
    stock_model = ComponentStock
    assignment_model = ComponentAllocation
    item_field = "component"
    assignment_name = "componentallocation"
    list_route = "inventory:componentallocation_list"
    detail_route = "inventory:component_detail"
    api_list_route = "api:inventory_api:componentallocation-list"
    api_detail_route = "api:inventory_api:componentallocation-detail"
    item_list_route = "inventory:component_list"
    api_item_list_route = "api:inventory_api:component-list"
    api_item_detail_route = "api:inventory_api:component-detail"
    quantity_field = "total_allocated"
    available_field = "available_stock"
    checkin_route = "inventory:component_checkin"
