import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Category, Manufacturer, StatusLabel
from core.tests.mixins import grant
from inventory.models import Accessory, Component, Consumable, Kit
from licenses.models import License
from organization.models import Location, Role, Site, Tenant, TenantGroup
from software.models import Software
from users.models import Token

User = get_user_model()


class GraphQLTestCase(TestCase):
    def setUp(self):
        # Create users
        self.admin_user = User.objects.create_superuser(
            username="admin_user", email="admin@example.com", password="password123"
        )
        self.staff_a = User.objects.create_user(username="staff_a", email="staff_a@example.com", password="password123")
        self.staff_b = User.objects.create_user(username="staff_b", email="staff_b@example.com", password="password123")

        # Tenants
        self.tenant_group = TenantGroup.objects.create(name="HQ Group", slug="hq-group")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a", group=self.tenant_group)
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b", group=self.tenant_group)

        # Site
        self.site = Site.objects.create(name="HQ Site", slug="hq-site")

        # Associate staff with Tenant membership/roles
        self.role_admin_a = Role.objects.create(
            tenant=self.tenant_a,
            name="Admin Role A",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "assets.delete_asset",
                "software.view_software",
                "software.add_software",
                "software.change_software",
                "software.delete_software",
                "licenses.view_license",
                "licenses.add_license",
                "licenses.change_license",
                "licenses.delete_license",
                "inventory.view_component",
                "inventory.add_component",
                "inventory.change_component",
                "inventory.delete_component",
                "inventory.view_accessory",
                "inventory.add_accessory",
                "inventory.change_accessory",
                "inventory.delete_accessory",
                "inventory.view_consumable",
                "inventory.add_consumable",
                "inventory.change_consumable",
                "inventory.delete_consumable",
                "inventory.view_kit",
                "inventory.add_kit",
                "inventory.change_kit",
                "inventory.delete_kit",
            ],
        )
        self.role_admin_b = Role.objects.create(
            tenant=self.tenant_b,
            name="Admin Role B",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "assets.delete_asset",
                "software.view_software",
                "software.add_software",
                "software.change_software",
                "software.delete_software",
                "licenses.view_license",
                "licenses.add_license",
                "licenses.change_license",
                "licenses.delete_license",
                "inventory.view_component",
                "inventory.add_component",
                "inventory.change_component",
                "inventory.delete_component",
                "inventory.view_accessory",
                "inventory.add_accessory",
                "inventory.change_accessory",
                "inventory.delete_accessory",
                "inventory.view_consumable",
                "inventory.add_consumable",
                "inventory.change_consumable",
                "inventory.delete_consumable",
                "inventory.view_kit",
                "inventory.add_kit",
                "inventory.change_kit",
                "inventory.delete_kit",
            ],
        )
        self.assignment_a = grant(self.staff_a, self.tenant_a, self.role_admin_a)
        self.membership_a = self.assignment_a.membership
        self.assignment_b = grant(self.staff_b, self.tenant_b, self.role_admin_b)
        self.membership_b = self.assignment_b.membership

        # Grant general Django permissions to staff users
        for user in [self.staff_a, self.staff_b]:
            for app, model in [
                ("assets", "asset"),
                ("software", "software"),
                ("licenses", "license"),
                ("inventory", "component"),
                ("inventory", "accessory"),
                ("inventory", "consumable"),
                ("inventory", "kit"),
            ]:
                ct = ContentType.objects.get(app_label=app, model=model)
                for action in ["view", "add", "change", "delete"]:
                    codename = f"{action}_{model}"
                    try:
                        perm = Permission.objects.get(codename=codename, content_type=ct)
                        user.user_permissions.add(perm)
                    except Permission.DoesNotExist:
                        pass

        # Create Tokens
        self.token_a = Token.objects.create(user=self.staff_a, tenant=self.tenant_a)
        self.token_b = Token.objects.create(user=self.staff_b, tenant=self.tenant_b)
        # Admin token: used in tests that require a superuser context so that
        # get_object_or_denied() runs without an active_tenant filter (superusers
        # have no forced tenant context when no session tenant is selected).
        self.token_admin = Token.objects.create(user=self.admin_user, tenant=self.tenant_a)

        # Setup base objects
        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.asset_role = AssetRole.objects.create(name="Laptop", slug="laptop")
        self.status = StatusLabel.objects.create(name="Ready", slug="ready", type=StatusLabel.TYPE_DEPLOYABLE)
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="Latitude 5540", slug="latitude-5540"
        )
        self.category = Category.objects.create(
            name="Laptop Cat",
            slug="laptop-cat",
            applies_to={"asset": True, "accessory": True, "component": True, "consumable": True},
        )

        # Tenant A objects
        self.location_a = Location.objects.create(
            name="Office A", slug="office-a", tenant=self.tenant_a, site=self.site
        )
        self.asset_a = Asset.objects.create(
            name="Laptop A",
            asset_tag="TAG-A",
            asset_type=self.asset_type,
            status=self.status,
            tenant=self.tenant_a,
            location=self.location_a,
        )
        # Tenant-owned so that get_object_or_denied(Software, ..., tenant=tenant_a)
        # can find it (the function adds an extra .filter(tenant=tenant) on top of
        # the TenantScopingSoftDeleteManager queryset, which would exclude a
        # global/null-tenant entry when active_tenant=tenant_a).
        self.software = Software.objects.create(name="Slack", manufacturer=self.manufacturer, tenant=self.tenant_a)
        self.license_a = License.objects.create(
            name="Slack Entitlement A", software=self.software, tenant=self.tenant_a, seats=5
        )
        self.component_a = Component.objects.create(
            name="RAM A", manufacturer=self.manufacturer, category=self.category, tenant=self.tenant_a
        )
        self.accessory_a = Accessory.objects.create(
            name="Mouse A", manufacturer=self.manufacturer, tenant=self.tenant_a
        )
        self.consumable_a = Consumable.objects.create(
            name="MX-4 Paste A", manufacturer=self.manufacturer, tenant=self.tenant_a
        )
        self.kit_a = Kit.objects.create(name="New Hire Kit A", tenant=self.tenant_a)

        # Tenant B objects
        self.location_b = Location.objects.create(
            name="Office B", slug="office-b", tenant=self.tenant_b, site=self.site
        )
        self.asset_b = Asset.objects.create(
            name="Laptop B",
            asset_tag="TAG-B",
            asset_type=self.asset_type,
            status=self.status,
            tenant=self.tenant_b,
            location=self.location_b,
        )

        self.graphql_url = reverse("graphql")

    @staticmethod
    def _tenant_scope(tenant_id):
        return f"{{ mode: TENANT, tenantId: {json.dumps(str(tenant_id))} }}"

    def _assets_query(self, tenant_id, selection):
        return f"{{ assets(requestedScope: {self._tenant_scope(tenant_id)}) {{ {selection} }} }}"

    def _asset_query(self, tenant_id, asset_id, selection):
        return f"{{ asset(id: {json.dumps(str(asset_id))}, requestedScope: {self._tenant_scope(tenant_id)}) {{ {selection} }} }}"

    @override_settings(
        DEBUG=True,
        MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != "debug_toolbar.middleware.DebugToolbarMiddleware"],
    )
    def test_graphiql_get_gated_by_session(self):
        # Unauthenticated GET request to GraphQL playground should redirect to login page
        response = self.client.get(self.graphql_url, HTTP_ACCEPT="text/html")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

        # Authenticated GET request should load successfully (status 200)
        self.client.force_login(self.admin_user)
        response = self.client.get(self.graphql_url, HTTP_ACCEPT="text/html")
        self.assertEqual(response.status_code, 200)

    @override_settings(
        DEBUG=False,
        MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != "debug_toolbar.middleware.DebugToolbarMiddleware"],
    )
    def test_graphiql_is_available_to_authenticated_users_in_production(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(self.graphql_url, HTTP_ACCEPT="text/html")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GraphiQL")
        self.assertContains(response, 'nonce="')
        self.assertContains(response, "dist/vendor/graphiql/graphiql.css")
        self.assertContains(response, "dist/vendor/graphiql/graphiql-ui.js")
        self.assertNotIn("https://cdn.jsdelivr.net", response.content.decode())
        self.assertNotIn("https://cdn.jsdelivr.net", response["Content-Security-Policy"])

    def test_graphql_post_gated_by_auth(self):
        query = self._assets_query(self.tenant_a.pk, "name")
        # Unauthenticated POST request should return 401
        response = self.client.post(self.graphql_url, data={"query": query})
        self.assertEqual(response.status_code, 401)

        # Authenticated POST request with token should succeed
        response = self.client.post(
            self.graphql_url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token_a.key}",
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertNotIn("errors", res_data)

    def test_asset_list_defaults_to_session_scope(self):
        response = self.client.post(
            self.graphql_url,
            data=json.dumps({"query": "{ assets { name } }"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token_a.key}",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("errors", payload)
        names = {row["name"] for row in payload["data"]["assets"]}
        self.assertIn(self.asset_a.name, names)
        self.assertNotIn(self.asset_b.name, names)

    def test_asset_lookup_defaults_to_session_scope(self):
        query = f"{{ asset(id: {json.dumps(str(self.asset_a.pk))}) {{ name }} }}"
        response = self.client.post(
            self.graphql_url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token_a.key}",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("errors", payload)
        self.assertEqual(payload["data"]["asset"]["name"], self.asset_a.name)

    def test_tenant_isolation_boundary_query(self):
        # When querying under Tenant A, only Asset A should be returned
        query = self._assets_query(self.tenant_a.pk, "name assetTag")

        response = self.client.post(
            self.graphql_url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token_a.key}",
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        assets = res_data["data"]["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["name"], "Laptop A")

        # When querying under Tenant B, only Asset B should be returned
        query = self._assets_query(self.tenant_b.pk, "name assetTag")
        response = self.client.post(
            self.graphql_url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token_b.key}",
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        assets = res_data["data"]["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["name"], "Laptop B")

    def test_unauthorized_individual_lookup_returns_none(self):
        # Query asset of Tenant B as Staff A
        query = self._asset_query(self.tenant_a.pk, self.asset_b.id, "name")
        response = self.client.post(
            self.graphql_url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token_a.key}",
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertIsNone(res_data["data"]["asset"])

    def test_post_request_using_session_auth(self):
        query = self._assets_query(self.tenant_a.pk, "name")
        self.client.force_login(self.staff_a)
        response = self.client.post(
            self.graphql_url, data=json.dumps({"query": query}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertNotIn("errors", res_data)
        self.assertEqual(len(res_data["data"]["assets"]), 1)
