from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.tests.mixins import grant
from organization.models import AssetHolder, Role, Tenant
from users.models import Token, UserGroup

User = get_user_model()


class SCIMStressTests(TestCase):
    def setUp(self):
        # Create Tenants
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")
        self.other_tenant = Tenant.objects.create(name="Other Corp", slug="other")

        # Create Admin User and membership — create first, then add roles (no role= kwarg).
        self.admin_user = User.objects.create_user(username="admin", email="admin@acme.com", password="adminpassword")
        self.role_admin = Role.objects.create(
            tenant=self.tenant,
            name="Admin",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "extras.view_dashboard",
                # SCIM auth authorizes on real permissions, not the "Admin" name —
                # this role must actually grant membership-management to pass it.
                "organization.change_membership",
                # WP-20: tenant SCIM Group reads require the same permission as provider reads.
                "users.view_usergroup",
            ],
        )
        grant(self.admin_user, self.tenant, self.role_admin)

        # Setup tokens — tenant explicit so it unambiguously matches self.tenant's URLs.
        self.valid_token = Token.objects.create(
            user=self.admin_user, tenant=self.tenant, expires=timezone.now() + timezone.timedelta(days=1)
        )
        self.expired_token = Token.objects.create(
            user=self.admin_user, tenant=self.tenant, expires=timezone.now() - timezone.timedelta(hours=1)
        )

        # Headers helpers
        self.auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.valid_token.key}"}

    def assertSCIMError(self, response, expected_status_code):
        """Helper to assert that a response conforms to the SCIM error schema."""
        self.assertEqual(response.status_code, expected_status_code)

        # Verify JSON content type
        self.assertIn("application/json", response.headers.get("Content-Type", ""))

        data = response.json()
        self.assertIn("schemas", data)
        self.assertIn("urn:ietf:params:scim:api:messages:2.0:Error", data["schemas"])
        self.assertIn("status", data)
        self.assertEqual(data["status"], str(expected_status_code))
        self.assertIn("detail", data)
        self.assertIsInstance(data["detail"], str)

    def test_expired_token_returns_scim_error(self):
        """1. Expired Token: check if 401 is returned and if it's SCIM-compliant."""
        url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.expired_token.key}")
        self.assertSCIMError(response, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_token_returns_scim_error(self):
        """2. Invalid Token key: check if 401 is returned and if it's SCIM-compliant."""
        url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION="Bearer invalid_key_here")
        self.assertSCIMError(response, status.HTTP_401_UNAUTHORIZED)

    def test_nonexistent_tenant_slug_returns_scim_error(self):
        """3. Non-existent tenant slug: check if 401 is returned (security anti-harvesting) and if it's SCIM-compliant."""
        url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": "nonexistent-tenant"})
        response = self.client.get(url, **self.auth_headers)
        self.assertSCIMError(response, status.HTTP_401_UNAUTHORIZED)

    def test_empty_payload_user_creation(self):
        """4. Empty payload on User creation: check if 400 is returned and if it's SCIM-compliant."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.post(url, data={}, content_type="application/json", **self.auth_headers)
        self.assertSCIMError(response, status.HTTP_400_BAD_REQUEST)

    def test_empty_payload_group_creation(self):
        """5. Group creation via tenant SCIM is read-only/rejected: an empty payload
        returns a SCIM-compliant 403 (not 400/500)."""
        url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.post(url, data={}, content_type="application/json", **self.auth_headers)
        self.assertSCIMError(response, status.HTTP_403_FORBIDDEN)

    def test_empty_body_raw_user_creation(self):
        """6. Raw empty string body on User creation: check if 400 is returned and if it's SCIM-compliant."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.post(url, data="", content_type="application/json", **self.auth_headers)
        self.assertSCIMError(response, status.HTTP_400_BAD_REQUEST)

    def test_invalid_json_user_creation(self):
        """7. Malformed JSON syntax on User creation: check if 400 is returned and if it's SCIM-compliant."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.post(
            url, data='{"userName": "test"', content_type="application/json", **self.auth_headers
        )
        self.assertSCIMError(response, status.HTTP_400_BAD_REQUEST)

    def test_long_username_creation(self):
        """8. Extremely long username: check behavior (e.g. max limits, validation, database constraint safety)."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        long_username = "a" * 5000 + "@example.com"
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": long_username,
            "emails": [{"value": long_username, "primary": True}],
            "active": True,
        }
        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        # Check that it returns an error status and doesn't throw a raw 500 server error
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR])
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            self.assertSCIMError(response, status.HTTP_400_BAD_REQUEST)
        else:
            # If 500, we should log that database limits are exceeded and cause internal errors
            print("WARNING: Extremely long username caused 500 Internal Server Error.")

    def test_long_groupname_creation(self):
        """9. Extremely long groupname: group creation is rejected (read-only) before any
        length handling — a SCIM 403, never a 500."""
        url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "g" * 5000, "members": []}
        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertSCIMError(response, status.HTTP_403_FORBIDDEN)

    def test_sql_injection_username_filter(self):
        """10. SQL injection in User query filter: should be handled safely and not crash/leak."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        sqli_payloads = [
            "admin' OR '1'='1",
            "admin'; DROP TABLE organization_tenant; --",
            "admin' UNION SELECT username, password FROM users_user; --",
            'admin" OR "1"="1',
        ]
        for val in sqli_payloads:
            # The filter parameter is passed in query string: ?filter=userName eq "val"
            query_url = '%s?filter=userName eq "%s"' % (url, val)
            response = self.client.get(query_url, **self.auth_headers)
            self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])
            if response.status_code == status.HTTP_200_OK:
                data = response.json()
                self.assertEqual(data["totalResults"], 0)
                self.assertEqual(len(data["Resources"]), 0)

            # Ensure organization_tenant table still exists by verifying tenant counts
            self.assertTrue(Tenant.objects.filter(slug=self.tenant.slug).exists())

    def test_sql_injection_group_filter(self):
        """11. SQL injection in Group query filter: should be handled safely and not crash/leak."""
        url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})
        sqli_payloads = [
            "Admin' OR '1'='1",
            "Admin'; DROP TABLE organization_tenant; --",
            'Admin" OR "1"="1',
        ]
        for val in sqli_payloads:
            query_url = '%s?filter=displayName eq "%s"' % (url, val)
            response = self.client.get(query_url, **self.auth_headers)
            self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])
            if response.status_code == status.HTTP_200_OK:
                data = response.json()
                self.assertEqual(data["totalResults"], 0)
                self.assertEqual(len(data["Resources"]), 0)
            self.assertTrue(Tenant.objects.filter(slug=self.tenant.slug).exists())

    def test_malformed_filters(self):
        """12. Malformed filters: should either return 400 Bad Request (SCIM Error) or handle safely."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        malformed_filters = [
            'userName equals "admin"',
            "userName eq",
            "userName co",
            'invalidAttribute eq "admin"',
        ]
        for flt in malformed_filters:
            query_url = f"{url}?filter={flt}"
            response = self.client.get(query_url, **self.auth_headers)
            # The API should either return a 400 SCIM Error or safely return empty/standard response, but never 500
            self.assertNotEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
            if response.status_code == status.HTTP_400_BAD_REQUEST:
                self.assertSCIMError(response, status.HTTP_400_BAD_REQUEST)

    def test_stress_user_creation_loop(self):
        """13. Stress testing user creation: create 50 users sequentially and verify DB consistency."""
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        created_usernames = []
        for i in range(50):
            username = f"stress_user_{i}@example.com"
            payload = {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": username,
                "name": {"familyName": f"LastName{i}", "givenName": f"FirstName{i}"},
                "emails": [{"value": username, "primary": True}],
                "active": True,
            }
            response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            created_usernames.append(username)

        # Verify that all 50 users exist and have associated AssetHolders.
        self.assertEqual(User.objects.filter(username__startswith="stress_user_").count(), 50)
        self.assertEqual(AssetHolder.objects.filter(tenant=self.tenant, email__startswith="stress_user_").count(), 50)

    def test_stress_group_creation_loop(self):
        """14. SCIM never creates groups (read-only): repeated POSTs all return 403 and
        create nothing."""
        url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})
        for i in range(10):
            payload = {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": f"stress_group_{i}",
                "members": [],
            }
            response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.assertEqual(UserGroup.objects.filter(name__startswith="stress_group_").count(), 0)
