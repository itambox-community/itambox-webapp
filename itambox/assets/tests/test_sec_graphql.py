import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.models import Asset, AssetType, Manufacturer, StatusLabel
from core.tests.mixins import grant
from organization.models import Location, Role, Site, Tenant, TenantGroup
from users.models import Token

User = get_user_model()

# Run every test in this module with debug_toolbar's middleware stripped: under
# DEBUG=False the toolbar can interfere with the GraphQL response, and these
# tests flip DEBUG explicitly per-method via @override_settings.
_MIDDLEWARE_NO_TOOLBAR = [m for m in settings.MIDDLEWARE if m != "debug_toolbar.middleware.DebugToolbarMiddleware"]


class GraphQLSecurityTestCase(TestCase):
    """Security guarantees of the GraphQL endpoint: introspection availability
    (F11) and the query-complexity / cost budget that bounds nested fan-out (F10)."""

    def setUp(self):
        # Unique fixture names/slugs ('-secgql') so this file can run in the
        # full, order-dependent suite without colliding with sibling tests that
        # share uniquely-constrained tables (Tenant slug, username, StatusLabel,
        # Manufacturer, etc.).
        self.staff = User.objects.create_user(
            username="staff_secgql", email="staff_secgql@example.com", password="password123"
        )

        self.tenant_group = TenantGroup.objects.create(name="SecGQL Group", slug="secgql-group")
        self.tenant = Tenant.objects.create(name="SecGQL Tenant", slug="secgql-tenant", group=self.tenant_group)
        self.site = Site.objects.create(name="SecGQL Site", slug="secgql-site")

        self.role = Role.objects.create(
            tenant=self.tenant,
            name="SecGQL Role",
            permissions=["assets.view_asset"],
        )
        self.assignment = grant(self.staff, self.tenant, self.role)
        self.membership = self.assignment.membership

        self.token = Token.objects.create(user=self.staff)

        self.manufacturer = Manufacturer.objects.create(name="SecGQL Mfr", slug="secgql-mfr")
        self.status = StatusLabel.objects.create(
            name="SecGQL Ready", slug="secgql-ready", type=StatusLabel.TYPE_DEPLOYABLE
        )
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="SecGQL Model",
            slug="secgql-model",
        )
        self.location = Location.objects.create(
            name="SecGQL Office", slug="secgql-office", tenant=self.tenant, site=self.site
        )
        self.asset = Asset.objects.create(
            name="SecGQL Laptop",
            asset_tag="TAG-SECGQL",
            asset_type=self.asset_type,
            status=self.status,
            tenant=self.tenant,
            location=self.location,
        )

        self.graphql_url = reverse("graphql")

    def _post(self, query):
        return self.client.post(
            self.graphql_url,
            data=json.dumps({"query": query}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token.key}",
        )

    # --- F11: introspection is available in every environment -------------

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_introspection_allowed_when_debug_false(self):
        # The schema is not a secret: introspection stays available with DEBUG
        # off so GraphiQL's docs explorer and autocompletion work in production
        # (NetBox model). Only authentication gates the endpoint.
        query = "{ __schema { types { name } } }"
        response = self._post(query)
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertNotIn("errors", res_data)
        self.assertTrue(res_data["data"]["__schema"]["types"])

    @override_settings(DEBUG=True, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_introspection_allowed_when_debug_true(self):
        # DEBUG on behaves identically: introspection succeeds.
        query = "{ __schema { types { name } } }"
        response = self._post(query)
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertNotIn("errors", res_data)
        self.assertTrue(res_data["data"]["__schema"]["types"])

    # --- F10: query-complexity budget bounds nested fan-out --------------

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_high_fan_out_query_rejected_by_complexity_rule(self):
        # Repeat an expensive list-over-list path (assets -> ... ->
        # softwareProducts, both list-returning) under several aliases. Each
        # aliased block is cheap on its own and stays within the depth and
        # field/alias caps, but together they blow the complexity budget.
        block = """
            assets(requestedScope: SCOPE_PLACEHOLDER) {
                assetType { manufacturer { softwareProducts { name slug } } }
            }
        """
        block = block.replace("SCOPE_PLACEHOLDER", f"{{ mode: TENANT, tenantId: {json.dumps(str(self.tenant.pk))} }}")
        aliased = "\n".join(f"a{i}: {block}" for i in range(12))
        query = "{" + aliased + "}"

        response = self._post(query)
        # A validation-rule rejection (complexity budget exceeded) returns HTTP
        # 400 with the error in the GraphQL response body.
        self.assertEqual(response.status_code, 400)
        res_data = response.json()
        self.assertIn("errors", res_data)
        combined = " ".join(e.get("message", "") for e in res_data["errors"]).lower()
        self.assertIn("complexity", combined)

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_normal_query_passes_complexity_rule(self):
        # A small, realistic query is well under the budget and must succeed.
        query = f"{{ assets(requestedScope: {{ mode: TENANT, tenantId: {json.dumps(str(self.tenant.pk))} }}) {{ name assetTag }} }}"
        response = self._post(query)
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertNotIn("errors", res_data)
        names = [a["name"] for a in res_data["data"]["assets"]]
        self.assertIn("SecGQL Laptop", names)

    # --- #813: GraphiQL's own introspection query must load ---------------

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_graphiql_introspection_query_succeeds(self):
        # GraphiQL's startup IntrospectionQuery (fragments, four ofType levels)
        # costs far more than a data query of the same size and used to be
        # rejected with "Query exceeds the maximum complexity of 2000".
        from graphql import get_introspection_query

        query = get_introspection_query(
            descriptions=True, specified_by_url=True, directive_is_repeatable=True, schema_description=True
        )
        response = self._post(query)
        self.assertEqual(response.status_code, 200, response.content[:300])
        res_data = response.json()
        self.assertNotIn("errors", res_data)
        type_names = {t["name"] for t in res_data["data"]["__schema"]["types"]}
        self.assertIn("Query", type_names)
        self.assertIsNone(res_data["data"]["__schema"]["mutationType"])

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_repeated_introspection_still_bounded_by_complexity_rule(self):
        # The introspection exemption is a flat per-root cost, not free: many
        # aliased copies of a full introspection still exhaust the budget.
        aliased = " ".join(f"a{i}: __schema {{ types {{ name }} }}" for i in range(45))
        response = self._post("{" + aliased + "}")
        self.assertEqual(response.status_code, 400)
        combined = " ".join(e.get("message", "") for e in response.json()["errors"]).lower()
        self.assertIn("complexity", combined)

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_introspection_alias_does_not_hide_data_fan_out(self):
        # Mixing an introspection root with an expensive data query must not
        # discount the data part.
        block = (
            "assets(requestedScope: {mode: TENANT, tenantId: %s}) { assetType { manufacturer { softwareProducts { name slug } } } }"
            % (json.dumps(str(self.tenant.pk)))
        )
        aliased = "\n".join(f"a{i}: {block}" for i in range(12))
        response = self._post("{ __schema { queryType { name } } " + aliased + "}")
        self.assertEqual(response.status_code, 400)

    @override_settings(DEBUG=False, MIDDLEWARE=_MIDDLEWARE_NO_TOOLBAR)
    def test_mutation_still_rejected(self):
        response = self._post("mutation { __typename }")
        self.assertEqual(response.status_code, 400)
        self.assertIn("mutation", response.content.decode().lower())
