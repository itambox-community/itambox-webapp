"""GraphQL list roots return the same object set as REST in tenant, group and all-accessible sessions."""

import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.models import Manufacturer, Supplier
from core.graphql_utils import requested_selector_from_scope
from core.managers import Scope
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, Component, Consumable, Kit
from licenses.models import License
from organization.models import Role, Tenant, TenantGroup
from software.models import Software
from subscriptions.models import Subscription, SubscriptionAssignment

User = get_user_model()

ROOTS = (
    ("accessories", "api:inventory_api:accessory-list", "inventory.view_accessory"),
    ("consumables", "api:inventory_api:consumable-list", "inventory.view_consumable"),
    ("kits", "api:inventory_api:kit-list", "inventory.view_kit"),
    ("components", "api:inventory_api:component-list", "inventory.view_component"),
    ("licenses", "api:licenses_api:license-list", "licenses.view_license"),
    ("subscriptions", "api:subscriptions_api:subscription-list", "subscriptions.view_subscription"),
    ("softwareList", "api:software_api:software-list", "software.view_software"),
)

PERMISSIONS = [r[2] for r in ROOTS] + ["subscriptions.view_subscriptionassignment"]


class GraphQLRootScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.group = TenantGroup.objects.create(name="GQ Group", slug="gq-group")
        self.tenant_a = Tenant.objects.create(name="GQ A", slug="gq-a", group=self.group, is_provider=True)
        self.tenant_b = Tenant.objects.create(name="GQ B", slug="gq-b", group=self.group, managed_by=self.tenant_a)
        self.tenant_c = Tenant.objects.create(name="GQ C", slug="gq-c", managed_by=self.tenant_a)
        self.user = User.objects.create_user("gq_reader", "gq@example.com", "pw")
        role = Role.objects.create(
            tenant=self.tenant_a,
            name="GQ Reader",
            permissions=PERMISSIONS,
        )
        self.grant(self.user, self.tenant_a, role, managed_scope="all")
        mfr = Manufacturer.objects.create(name="GQ Mfr", slug="gq-mfr")
        supplier = Supplier.objects.create(name="GQ Sup", slug="gq-sup")
        for key, tenant in (("a", self.tenant_a), ("b", self.tenant_b), ("c", self.tenant_c)):
            target = Accessory.objects.create(name=f"acc-{key}", slug=f"acc-{key}", manufacturer=mfr, tenant=tenant)
            Consumable.objects.create(name=f"con-{key}", manufacturer=mfr, tenant=tenant)
            Kit.objects.create(name=f"kit-{key}", tenant=tenant)
            Component.objects.create(name=f"cmp-{key}", manufacturer=mfr, tenant=tenant)
            software = Software.objects.create(name=f"sw-{key}", manufacturer=mfr, tenant=tenant)
            License.objects.create(name=f"lic-{key}", software=software, seats=1, tenant=tenant)
            sub = Subscription.objects.create(name=f"sub-{key}", supplier=supplier, tenant=tenant)
            SubscriptionAssignment.objects.create(
                subscription=sub, content_type=ContentType.objects.get_for_model(Accessory), object_id=target.pk
            )
        self.client.force_login(self.user)

    def _session(self, **values):
        session = self.client.session
        for key in ("active_tenant_id", "active_tenant_group_id", "active_all_accessible"):
            session.pop(key, None)
        session.update(values)
        session.save()

    def _graphql_names(self, root):
        query = "query { %s { name } }" % root
        response = self.client.post(
            reverse("graphql"), data=json.dumps({"query": query}), content_type="application/json"
        )
        payload = response.json()
        self.assertNotIn("errors", payload, payload)
        return {row["name"] for row in payload["data"][root]}

    def _rest_names(self, url_name):
        response = self.client.get(reverse(url_name) + "?limit=100")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        return {row["name"] for row in body.get("results", body)}

    def _assert_equivalent(self, must_include):
        """GraphQL equals REST for the same principal; a tenantless-only result (the defect) fails."""
        for root, url_name, _permission in ROOTS:
            graphql = self._graphql_names(root)
            self.assertEqual(graphql, self._rest_names(url_name), root)
            self.assertTrue({n.split("-")[-1] for n in graphql} >= must_include, root)

    def test_tenant_scope(self):
        self._session(active_tenant_id=self.tenant_a.pk)
        self._assert_equivalent({"a"})

    def test_group_scope_includes_every_group_tenant(self):
        self._session(active_tenant_group_id=self.group.pk)
        self._assert_equivalent({"a"})

    def test_all_accessible_scope_includes_every_tenant(self):
        self._session(active_all_accessible=True)
        self._assert_equivalent({"a"})

    def test_single_roots_resolve_in_group_scope(self):
        self._session(active_tenant_group_id=self.group.pk)
        for root, model, name in (
            ("accessory", Accessory, "acc-a"),
            ("consumable", Consumable, "con-a"),
            ("kit", Kit, "kit-a"),
            ("component", Component, "cmp-a"),
        ):
            pk = model.objects.get(name=name).pk
            query = 'query { %s(id: "%s") { name } }' % (root, pk)
            response = self.client.post(
                reverse("graphql"), data=json.dumps({"query": query}), content_type="application/json"
            )
            payload = response.json()
            self.assertNotIn("errors", payload, payload)
            self.assertEqual(payload["data"][root], {"name": name}, root)
        assignment = SubscriptionAssignment.objects.get(subscription__name="sub-a")
        query = 'query { subscriptionAssignment(id: "%s") { id } }' % assignment.pk
        response = self.client.post(
            reverse("graphql"), data=json.dumps({"query": query}), content_type="application/json"
        )
        payload = response.json()
        self.assertNotIn("errors", payload, payload)
        self.assertIsNotNone(payload["data"]["subscriptionAssignment"])

    def test_requested_selector_mirrors_each_scope_kind(self):
        self.assertEqual(
            requested_selector_from_scope(Scope(Scope.GROUP, group=self.group)),
            {"mode": "tenant_group", "tenant_id": None, "tenant_group_id": self.group.pk},
        )
        for kind in (Scope.ALL_ACCESSIBLE, Scope.SYSTEM):
            self.assertEqual(
                requested_selector_from_scope(Scope(kind)),
                {"mode": "all_accessible", "tenant_id": None, "tenant_group_id": None},
            )
        self.assertIsNone(requested_selector_from_scope(Scope(Scope.DENIED)))

    def test_assets_read_scope_denied_session_reads_nothing(self):
        from unittest import mock

        from assets import schema as assets_schema

        with mock.patch.object(assets_schema, "read_scope_from_info", return_value=Scope(Scope.DENIED)):
            self.assertIsNone(assets_schema._read_scope(None, None))
