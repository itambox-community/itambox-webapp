"""GraphQL list roots return the same object set as REST in tenant, group and all-accessible sessions."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Manufacturer, Supplier
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, Component, Consumable, Kit
from licenses.models import License
from organization.models import Role, Tenant, TenantGroup
from software.models import Software
from subscriptions.models import Subscription

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


class GraphQLRootScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.group = TenantGroup.objects.create(name="GQ Group", slug="gq-group")
        self.tenant_a = Tenant.objects.create(name="GQ A", slug="gq-a", group=self.group, is_provider=True)
        self.tenant_b = Tenant.objects.create(name="GQ B", slug="gq-b", group=self.group, managed_by=self.tenant_a)
        self.tenant_c = Tenant.objects.create(name="GQ C", slug="gq-c", managed_by=self.tenant_a)
        self.user = User.objects.create_user("gq_reader", "gq@example.com", "pw")
        role = Role.objects.create(tenant=self.tenant_a, name="GQ Reader", permissions=[r[2] for r in ROOTS])
        self.grant(self.user, self.tenant_a, role, managed_scope="all")
        mfr = Manufacturer.objects.create(name="GQ Mfr", slug="gq-mfr")
        supplier = Supplier.objects.create(name="GQ Sup", slug="gq-sup")
        for key, tenant in (("a", self.tenant_a), ("b", self.tenant_b), ("c", self.tenant_c)):
            Accessory.objects.create(name=f"acc-{key}", slug=f"acc-{key}", manufacturer=mfr, tenant=tenant)
            Consumable.objects.create(name=f"con-{key}", manufacturer=mfr, tenant=tenant)
            Kit.objects.create(name=f"kit-{key}", tenant=tenant)
            Component.objects.create(name=f"cmp-{key}", manufacturer=mfr, tenant=tenant)
            software = Software.objects.create(name=f"sw-{key}", manufacturer=mfr, tenant=tenant)
            License.objects.create(name=f"lic-{key}", software=software, seats=1, tenant=tenant)
            Subscription.objects.create(name=f"sub-{key}", supplier=supplier, tenant=tenant)
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
