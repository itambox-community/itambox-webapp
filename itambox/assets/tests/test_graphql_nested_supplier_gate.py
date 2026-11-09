from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase

from assets.models import Asset, Manufacturer, StatusLabel, Supplier
from core.schema import schema
from core.tests.mixins import grant
from inventory.models import Accessory
from itambox.middleware import set_current_tenant
from licenses.models import License
from organization.models import AssetHolder, Role, Tenant
from software.models import Software
from subscriptions.models import Subscription, SubscriptionAssignment

ASSET_QUERY = "query { assets { assetTag supplier { name accountId notes } } }"
LICENSE_QUERY = "query { licenses { name supplier { name accountId notes } } }"
ACCESSORY_QUERY = "query { accessories { name supplier { name accountId notes } } }"
SUBSCRIPTION_QUERY = "query { subscriptions { name supplier { name accountId notes } } }"
ASSIGNMENT_QUERY = "query { subscriptionAssignments { subscription { name notes } } }"


class NestedSupplierGateTests(TestCase):
    """Nested GraphQL relations do not grant access to the related object (#1049, #1051)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Gate Tenant", slug="gate-tenant")
        set_current_tenant(self.tenant)
        self.addCleanup(set_current_tenant, None)
        self.supplier = Supplier.objects.create(
            name="Acme Supply", account_id="ACC-SECRET", notes="internal note", tenant=self.tenant
        )
        status = StatusLabel.objects.create(name="Ready", slug="ready", type="deployable")
        self.asset = Asset.objects.create(
            name="A1", asset_tag="GATE-1", status=status, tenant=self.tenant, supplier=self.supplier
        )
        manufacturer = Manufacturer.objects.create(name="Gate Mfr", slug="gate-mfr")
        software = Software.objects.create(name="Gate Suite", manufacturer=manufacturer, tenant=self.tenant)
        License.objects.create(name="Gate Lic", software=software, seats=1, tenant=self.tenant, supplier=self.supplier)
        self.accessory = Accessory.objects.create(
            name="Gate Accessory", manufacturer=manufacturer, supplier=self.supplier, tenant=self.tenant
        )
        self.subscription = Subscription.objects.create(
            name="Gate Subscription", supplier=self.supplier, tenant=self.tenant, notes="subscription secret"
        )
        SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ContentType.objects.get_for_model(Asset),
            object_id=self.asset.pk,
        )
        self.restricted = self._member("restricted", ["assets.view_asset", "licenses.view_license"])
        self.allowed = self._member("allowed", ["assets.view_asset", "licenses.view_license", "assets.view_supplier"])

    def _member(self, username, permissions):
        user = get_user_model().objects.create_user(username=username, email=f"{username}@example.com", password="x")
        AssetHolder.objects.create(
            user=user, first_name=username, last_name="User", upn=username, email=user.email, tenant=self.tenant
        )
        role = Role.objects.create(tenant=self.tenant, name=f"{username} role", permissions=permissions)
        grant(user, self.tenant, role)
        return user

    def _run(self, query, user):
        request = RequestFactory().post("/graphql")
        request.user = get_user_model().objects.get(pk=user.pk)
        request.active_tenant = self.tenant
        result = schema.execute_sync(query, context_value=request)
        self.assertIsNone(result.errors)
        return result.data

    def test_asset_supplier_hidden_without_supplier_permission(self):
        rows = self._run(ASSET_QUERY, self.restricted)["assets"]
        self.assertEqual([r["assetTag"] for r in rows], ["GATE-1"])
        self.assertIsNone(rows[0]["supplier"])

    def test_asset_supplier_visible_with_supplier_permission(self):
        rows = self._run(ASSET_QUERY, self.allowed)["assets"]
        self.assertEqual(rows[0]["supplier"]["name"], "Acme Supply")

    def test_license_supplier_hidden_without_supplier_permission(self):
        rows = self._run(LICENSE_QUERY, self.restricted)["licenses"]
        self.assertEqual([r["name"] for r in rows], ["Gate Lic"])
        self.assertIsNone(rows[0]["supplier"])

    def test_license_supplier_visible_with_supplier_permission(self):
        rows = self._run(LICENSE_QUERY, self.allowed)["licenses"]
        self.assertEqual(rows[0]["supplier"]["name"], "Acme Supply")

    def test_accessory_supplier_requires_supplier_permission(self):
        restricted = self._member("accessory-reader", ["inventory.view_accessory"])
        allowed = self._member("accessory-supplier-reader", ["inventory.view_accessory", "assets.view_supplier"])

        rows = self._run(ACCESSORY_QUERY, restricted)["accessories"]
        self.assertEqual([row["name"] for row in rows], ["Gate Accessory"])
        self.assertIsNone(rows[0]["supplier"])

        rows = self._run(ACCESSORY_QUERY, allowed)["accessories"]
        self.assertEqual(
            rows[0]["supplier"],
            {"name": "Acme Supply", "accountId": "ACC-SECRET", "notes": "internal note"},
        )

    def test_subscription_supplier_requires_supplier_permission(self):
        restricted = self._member("subscription-reader", ["subscriptions.view_subscription"])
        allowed = self._member(
            "subscription-supplier-reader", ["subscriptions.view_subscription", "assets.view_supplier"]
        )

        rows = self._run(SUBSCRIPTION_QUERY, restricted)["subscriptions"]
        self.assertEqual([row["name"] for row in rows], ["Gate Subscription"])
        self.assertIsNone(rows[0]["supplier"])

        rows = self._run(SUBSCRIPTION_QUERY, allowed)["subscriptions"]
        self.assertEqual(
            rows[0]["supplier"],
            {"name": "Acme Supply", "accountId": "ACC-SECRET", "notes": "internal note"},
        )

    def test_assignment_subscription_requires_subscription_permission(self):
        restricted = self._member("assignment-reader", ["subscriptions.view_subscriptionassignment"])
        allowed = self._member(
            "assignment-subscription-reader",
            ["subscriptions.view_subscriptionassignment", "subscriptions.view_subscription"],
        )

        rows = self._run(ASSIGNMENT_QUERY, restricted)["subscriptionAssignments"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["subscription"])

        rows = self._run(ASSIGNMENT_QUERY, allowed)["subscriptionAssignments"]
        self.assertEqual(
            rows[0]["subscription"],
            {"name": "Gate Subscription", "notes": "subscription secret"},
        )
