from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from assets.models import Category, Manufacturer
from core.schema import schema
from core.tests.mixins import grant
from inventory.models import Accessory
from itambox.middleware import set_current_tenant
from licenses.models import License
from organization.models import AssetHolder, Role, Tenant
from software.models import Software

LICENSE_QUERY = "query { licenses { name software { name } } }"
MANUFACTURER_QUERY = "query { accessories { manufacturer { softwareProducts { name } } } }"


class SoftwareRelationGateTests(TestCase):
    """Entry-point permission does not authorize the software relations (#1055)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Sw Gate Tenant", slug="sw-gate-tenant")
        set_current_tenant(self.tenant)
        self.addCleanup(set_current_tenant, None)
        self.manufacturer = Manufacturer.objects.create(name="Sw Gate Mfr", slug="sw-gate-mfr")
        software = Software.objects.create(name="Gate Suite", manufacturer=self.manufacturer, tenant=self.tenant)
        category = Category.objects.create(
            name="Gate Cat", slug="gate-cat", applies_to={"asset": True, "accessory": True}
        )
        Accessory.objects.create(name="Gate Acc", manufacturer=self.manufacturer, category=category, tenant=self.tenant)
        License.objects.create(name="Gate Lic", software=software, seats=1, tenant=self.tenant)
        self.restricted = self._member("swrestricted", ["licenses.view_license", "inventory.view_accessory"])
        self.allowed = self._member(
            "swallowed", ["licenses.view_license", "inventory.view_accessory", "software.view_software"]
        )

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

    def test_license_software_hidden_without_software_permission(self):
        rows = self._run(LICENSE_QUERY, self.restricted)["licenses"]
        self.assertEqual([r["name"] for r in rows], ["Gate Lic"])
        self.assertIsNone(rows[0]["software"])

    def test_license_software_visible_with_software_permission(self):
        rows = self._run(LICENSE_QUERY, self.allowed)["licenses"]
        self.assertEqual(rows[0]["software"]["name"], "Gate Suite")

    def _manufacturer_products(self, user):
        rows = self._run(MANUFACTURER_QUERY, user)["accessories"]
        self.assertEqual(len(rows), 1)
        return rows[0]["manufacturer"]["softwareProducts"]

    def test_manufacturer_software_products_empty_without_software_permission(self):
        self.assertEqual(self._manufacturer_products(self.restricted), [])

    def test_manufacturer_software_products_listed_with_software_permission(self):
        self.assertEqual([p["name"] for p in self._manufacturer_products(self.allowed)], ["Gate Suite"])
