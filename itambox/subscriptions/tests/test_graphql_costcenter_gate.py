from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from assets.models import Supplier
from core.schema import schema
from core.tests.mixins import grant
from itambox.middleware import set_current_tenant
from organization.models import AssetHolder, CostCenter, Role, Tenant
from subscriptions.models import Subscription

QUERY = "query { subscriptions { name costCenterId costCenterName } }"


class SubscriptionCostCenterGateTests(TestCase):
    """The subscriptions entry point does not authorize the cost center relation (#1066)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Cc Gate Tenant", slug="cc-gate-tenant")
        set_current_tenant(self.tenant)
        self.addCleanup(set_current_tenant, None)
        supplier = Supplier.objects.create(name="Cc Gate Supplier", slug="cc-gate-supplier")
        self.cost_center = CostCenter.objects.create(
            name="Finance Gate", slug="finance-gate", code="CC-GATE", tenant=self.tenant
        )
        Subscription.objects.create(
            name="Gate Sub", supplier=supplier, tenant=self.tenant, cost_center=self.cost_center
        )
        self.restricted = self._member("ccrestricted", ["subscriptions.view_subscription"])
        self.allowed = self._member("ccallowed", ["subscriptions.view_subscription", "organization.view_costcenter"])

    def _member(self, username, permissions):
        user = get_user_model().objects.create_user(username=username, email=f"{username}@example.com", password="x")
        AssetHolder.objects.create(
            user=user, first_name=username, last_name="User", upn=username, email=user.email, tenant=self.tenant
        )
        role = Role.objects.create(tenant=self.tenant, name=f"{username} role", permissions=permissions)
        grant(user, self.tenant, role)
        return user

    def _row(self, user):
        request = RequestFactory().post("/graphql")
        request.user = get_user_model().objects.get(pk=user.pk)
        request.active_tenant = self.tenant
        result = schema.execute_sync(QUERY, context_value=request)
        self.assertIsNone(result.errors)
        rows = result.data["subscriptions"]
        self.assertEqual([r["name"] for r in rows], ["Gate Sub"])
        return rows[0]

    def test_cost_center_hidden_without_cost_center_permission(self):
        row = self._row(self.restricted)
        self.assertIsNone(row["costCenterId"])
        self.assertIsNone(row["costCenterName"])

    def test_cost_center_visible_with_cost_center_permission(self):
        row = self._row(self.allowed)
        self.assertEqual(row["costCenterId"], str(self.cost_center.pk))
        self.assertEqual(row["costCenterName"], str(self.cost_center))
