from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from assets.models import Asset, Manufacturer, StatusLabel, Supplier
from core.schema import schema
from core.tests.mixins import grant
from itambox.middleware import set_current_tenant
from licenses.models import License
from organization.models import AssetHolder, Role, Tenant
from software.models import Software

ASSET_QUERY = "query { assets { assetTag supplier { name accountId notes } } }"
LICENSE_QUERY = "query { licenses { name supplier { name accountId notes } } }"


class NestedSupplierGateTests(TestCase):
    """A role that may see an asset or license does not thereby see the supplier record (#1049)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Gate Tenant", slug="gate-tenant")
        set_current_tenant(self.tenant)
        self.addCleanup(set_current_tenant, None)
        self.supplier = Supplier.objects.create(
            name="Acme Supply", account_id="ACC-SECRET", notes="internal note", tenant=self.tenant
        )
        status = StatusLabel.objects.create(name="Ready", slug="ready", type="deployable")
        Asset.objects.create(name="A1", asset_tag="GATE-1", status=status, tenant=self.tenant, supplier=self.supplier)
        manufacturer = Manufacturer.objects.create(name="Gate Mfr", slug="gate-mfr")
        software = Software.objects.create(name="Gate Suite", manufacturer=manufacturer, tenant=self.tenant)
        License.objects.create(name="Gate Lic", software=software, seats=1, tenant=self.tenant, supplier=self.supplier)
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
