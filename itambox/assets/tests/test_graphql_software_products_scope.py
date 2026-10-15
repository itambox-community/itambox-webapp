import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from assets.models import Asset, AssetType, Category, Manufacturer, StatusLabel
from core.tests.mixins import grant
from inventory.models import Accessory, Component, Consumable
from licenses.models import License
from organization.models import Location, Role, Site, Tenant
from software.models import Software
from users.models import Token

User = get_user_model()

PERMS = [
    "assets.view_asset",
    "assets.view_assettype",
    "assets.view_manufacturer",
    "inventory.view_accessory",
    "inventory.view_consumable",
    "inventory.view_component",
    "licenses.view_license",
    "software.view_software",
]
PATHS = ("assets", "accessories", "consumables", "components", "licenses")


class ManufacturerSoftwareProductsScopeTests(TestCase):
    """Nested ``Manufacturer.softwareProducts`` must honour the request scope."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="SwScope A", slug="swscope-a")
        self.tenant_b = Tenant.objects.create(name="SwScope B", slug="swscope-b")
        self.site = Site.objects.create(name="SwScope Site", slug="swscope-site")
        self.tenants = {"a": self.tenant_a, "b": self.tenant_b}
        self.tokens = {}
        for key, tenant in self.tenants.items():
            user = User.objects.create_user(username=f"swscope_{key}", email=f"swscope_{key}@example.com", password="x")
            role = Role.objects.create(tenant=tenant, name=f"SwScope Role {key}", permissions=PERMS)
            grant(user, tenant, role)
            self.tokens[key] = Token.objects.create(user=user, tenant=tenant)

        self.manufacturer = Manufacturer.objects.create(name="SwScope Mfr", slug="swscope-mfr")
        self.global_sw = Software.objects.create(name="Shared Catalogue Tool", manufacturer=self.manufacturer)
        Software.objects.create(name="Private A Tool", manufacturer=self.manufacturer, tenant=self.tenant_a)
        Software.objects.create(name="Private B Tool", manufacturer=self.manufacturer, tenant=self.tenant_b)

        status = StatusLabel.objects.create(
            name="SwScope Ready", slug="swscope-ready", type=StatusLabel.TYPE_DEPLOYABLE
        )
        asset_type = AssetType.objects.create(manufacturer=self.manufacturer, model="SwScope Model", slug="swscope-m")
        category = Category.objects.create(
            name="SwScope Cat",
            slug="swscope-cat",
            applies_to={"asset": True, "accessory": True, "component": True, "consumable": True},
        )
        for key, tenant in self.tenants.items():
            location = Location.objects.create(
                name=f"SwScope Loc {key}", slug=f"swscope-loc-{key}", tenant=tenant, site=self.site
            )
            Asset.objects.create(
                name=f"SwScope Asset {key}",
                asset_tag=f"SW-{key}",
                asset_type=asset_type,
                status=status,
                tenant=tenant,
                location=location,
            )
            for model, label in ((Accessory, "Acc"), (Consumable, "Con"), (Component, "Comp")):
                model.objects.create(
                    name=f"SwScope {label} {key}", manufacturer=self.manufacturer, category=category, tenant=tenant
                )
            License.objects.create(name=f"SwScope Lic {key}", software=self.global_sw, tenant=tenant, seats=1)
        self.url = reverse("graphql")

    def _post(self, key, query):
        return self.client.post(
            self.url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.tokens[key].key}",
        )

    def _query(self, key, path):
        sel = "softwareProducts { name }"
        scope = '{ mode: TENANT, tenantId: "%s" }' % self.tenants[key].pk
        return {
            "assets": "{ assets(requestedScope: %s) { assetType { manufacturer { %s } } } }" % (scope, sel),
            "accessories": "{ accessories { manufacturer { %s } } }" % sel,
            "consumables": "{ consumables { manufacturer { %s } } }" % sel,
            "components": "{ components { manufacturer { %s } } }" % sel,
            "licenses": "{ licenses { software { manufacturer { %s } } } }" % sel,
        }[path]

    def _names(self, key, path):
        response = self._post(key, self._query(key, path))
        self.assertEqual(response.status_code, 200, response.content[:400])
        body = response.json()
        self.assertNotIn("errors", body)
        rows = body["data"][path]
        self.assertTrue(rows, f"{path}: no rows returned for tenant {key}")
        found = set()
        for row in rows:
            if path == "assets":
                manufacturer = row["assetType"]["manufacturer"]
            elif path == "licenses":
                manufacturer = row["software"]["manufacturer"]
            else:
                manufacturer = row["manufacturer"]
            found.update(item["name"] for item in manufacturer["softwareProducts"])
        return found

    def test_tenant_b_never_sees_tenant_a_private_software(self):
        for path in PATHS:
            with self.subTest(path=path):
                names = self._names("b", path)
                self.assertNotIn("Private A Tool", names)
                self.assertEqual(names, {"Shared Catalogue Tool", "Private B Tool"})

    def test_tenant_a_sees_own_and_shared_software(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.assertEqual(self._names("a", path), {"Shared Catalogue Tool", "Private A Tool"})

    def _software_sql(self, key, path):
        with CaptureQueriesContext(connection) as ctx:
            self._names(key, path)
        return [q["sql"] for q in ctx.captured_queries if 'FROM "software_software"' in q["sql"]]

    def test_every_software_query_carries_a_tenant_predicate(self):
        for path in PATHS:
            with self.subTest(path=path):
                queries = [sql for sql in self._software_sql("b", path) if '"manufacturer_id"' in sql]
                self.assertTrue(queries, "no manufacturer software query captured")
                for sql in queries:
                    self.assertIn('"software_software"."tenant_id"', sql.split("WHERE", 1)[1])
