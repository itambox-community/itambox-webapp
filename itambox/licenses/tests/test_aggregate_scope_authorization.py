"""Regression coverage for #1088 (aggregate-scope read leak) and #1089
(aggregate-scope create without the target tenant's add permission).

Fixture: a user with membership (reach) in both Tenant A and Tenant B, but a
role granting ``licenses.view_license`` / ``licenses.add_license`` /
``licenses.view_licenseseatassignment`` / ``licenses.add_licenseseatassignment``
in Tenant A only. Tenant B is reachable but never authorized. All tests run
under the "all accessible tenants" aggregate session scope unless noted.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from assets.models import Manufacturer
from core.managers import (
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.models import ObjectChange
from core.schema import schema
from core.tests.mixins import grant
from extras.models import Event
from itambox.middleware import _current_user
from licenses.models import License, LicenseSeatAssignment, LicenseTypeChoices
from organization.models import AssetHolder, Membership, Role, Tenant
from software.models import Software
from users.models import Token

User = get_user_model()

A_PERMS = [
    "licenses.view_license",
    "licenses.add_license",
    "licenses.view_licenseseatassignment",
    "licenses.add_licenseseatassignment",
]


class AggregateScopeAuthorizationTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="agg-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="agg-tenant-b")

        self.user = User.objects.create_user(username="agg_user", password="password123")

        self.role_a = Role.objects.create(tenant=self.tenant_a, name="Admin", permissions=A_PERMS)
        self.membership_a = grant(self.user, self.tenant_a, self.role_a).membership
        # Reach into B with no role/permission there at all.
        self.membership_b = Membership.objects.create(user=self.user, tenant=self.tenant_b)

        self.mfr = Manufacturer.objects.create(name="Microsoft", slug="agg-microsoft")
        self.software_a = Software.objects.create(name="Office A", manufacturer=self.mfr, tenant=self.tenant_a)
        self.software_b = Software.objects.create(name="Office B", manufacturer=self.mfr, tenant=self.tenant_b)

        self.license_a = License.objects.create(
            name="License A",
            software=self.software_a,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant_a,
        )
        self.license_b = License.objects.create(
            name="License B",
            software=self.software_b,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant_b,
        )

        self.holder_a = AssetHolder.objects.create(
            first_name="Alice", last_name="A", upn="alice@agg-a.example.com", tenant=self.tenant_a
        )
        self.holder_b = AssetHolder.objects.create(
            first_name="Bob", last_name="B", upn="bob@agg-b.example.com", tenant=self.tenant_b
        )

        self.factory = RequestFactory()

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)
        set_current_membership(None)
        _current_user.set(None)

    # ---------------------------------------------------------- activation

    def _activate_all_accessible(self):
        self.client.force_login(self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session["active_all_accessible"] = True
        session.save()

    def _activate_single_tenant(self, tenant):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.pop("active_tenant_group_id", None)
        session.pop("active_all_accessible", None)
        session.save()

    def _graphql_context(self):
        _current_user.set(self.user)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(True)
        set_current_membership(None)
        request = self.factory.post("/graphql")
        request.user = self.user
        request.active_tenant = None
        return request

    @staticmethod
    def _rows(response):
        data = response.json()
        return data["results"] if isinstance(data, dict) and "results" in data else data

    # ---------------------------------------------------------- #1088 reads

    def test_rest_list_excludes_unauthorized_tenant_under_all_accessible(self):
        self._activate_all_accessible()
        response = self.client.get(reverse("api:licenses_api:license-list"))
        self.assertEqual(response.status_code, 200, response.content)
        ids = {row["id"] for row in self._rows(response)}
        self.assertIn(self.license_a.pk, ids)
        self.assertNotIn(self.license_b.pk, ids)

    def test_graphql_list_excludes_unauthorized_tenant_under_all_accessible(self):
        ctx = self._graphql_context()
        result = schema.execute_sync("{ licenses { id } }", context_value=ctx)
        self.assertIsNone(result.errors)
        ids = {int(row["id"]) for row in result.data["licenses"]}
        self.assertIn(self.license_a.pk, ids)
        self.assertNotIn(self.license_b.pk, ids)

    def test_graphql_single_object_returns_null_for_unauthorized_tenant(self):
        ctx = self._graphql_context()
        result = schema.execute_sync(f"{{ license(id: {self.license_b.pk}) {{ id }} }}", context_value=ctx)
        self.assertIsNone(result.errors)
        self.assertIsNone(result.data["license"])

    def test_graphql_single_object_returns_authorized_tenant(self):
        ctx = self._graphql_context()
        result = schema.execute_sync(f"{{ license(id: {self.license_a.pk}) {{ id }} }}", context_value=ctx)
        self.assertIsNone(result.errors)
        self.assertEqual(int(result.data["license"]["id"]), self.license_a.pk)

    # ---------------------------------------------------------- #1089 creates

    def test_create_with_explicit_unauthorized_tenant_is_rejected(self):
        self._activate_all_accessible()
        license_ct = ContentType.objects.get_for_model(License)
        before_count = License.all_objects.count()
        before_changes = ObjectChange.objects.filter(changed_object_type=license_ct).count()
        before_events = Event.objects.filter(model=license_ct).count()

        response = self.client.post(
            reverse("api:licenses_api:license-list"),
            data={
                "name": "Hostile License",
                "software_id": self.software_b.pk,
                "license_type": LicenseTypeChoices.PERPETUAL_SEAT,
                "seats": 5,
                "tenant_id": self.tenant_b.pk,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(License.all_objects.count(), before_count)
        self.assertEqual(ObjectChange.objects.filter(changed_object_type=license_ct).count(), before_changes)
        self.assertEqual(Event.objects.filter(model=license_ct).count(), before_events)

    def test_create_with_explicit_authorized_tenant_succeeds(self):
        self._activate_all_accessible()
        response = self.client.post(
            reverse("api:licenses_api:license-list"),
            data={
                "name": "Friendly License",
                "software_id": self.software_a.pk,
                "license_type": LicenseTypeChoices.PERPETUAL_SEAT,
                "seats": 5,
                "tenant_id": self.tenant_a.pk,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        created = License.all_objects.get(pk=response.json()["id"])
        self.assertEqual(created.tenant_id, self.tenant_a.pk)

    def test_bulk_create_mixed_authorized_and_unauthorized_rows_is_rejected_entirely(self):
        self._activate_all_accessible()
        before_count = License.all_objects.count()

        response = self.client.post(
            reverse("api:licenses_api:license-list"),
            data=[
                {
                    "name": "Bulk OK",
                    "software_id": self.software_a.pk,
                    "license_type": LicenseTypeChoices.PERPETUAL_SEAT,
                    "seats": 1,
                    "tenant_id": self.tenant_a.pk,
                },
                {
                    "name": "Bulk Hostile",
                    "software_id": self.software_b.pk,
                    "license_type": LicenseTypeChoices.PERPETUAL_SEAT,
                    "seats": 1,
                    "tenant_id": self.tenant_b.pk,
                },
            ],
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(License.all_objects.count(), before_count)
        self.assertFalse(License.all_objects.filter(name="Bulk OK").exists())
        self.assertFalse(License.all_objects.filter(name="Bulk Hostile").exists())

    def test_parent_derived_tenant_create_into_unauthorized_tenant_is_rejected(self):
        """LicenseSeatAssignment has no direct tenant field; its effective
        target is resolved through tenant_lookup = "license__tenant"."""
        self._activate_all_accessible()
        before_count = LicenseSeatAssignment.all_objects.count()

        response = self.client.post(
            reverse("api:licenses_api:licenseseatassignment-list"),
            data={
                "license_id": self.license_b.pk,
                "assigned_holder_id": self.holder_b.pk,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(LicenseSeatAssignment.all_objects.count(), before_count)

    def test_parent_derived_tenant_create_into_authorized_tenant_succeeds(self):
        self._activate_all_accessible()

        response = self.client.post(
            reverse("api:licenses_api:licenseseatassignment-list"),
            data={
                "license_id": self.license_a.pk,
                "assigned_holder_id": self.holder_a.pk,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(
            LicenseSeatAssignment.all_objects.filter(license=self.license_a, assigned_holder=self.holder_a).exists()
        )

    # ---------------------------------------------------------- unaffected paths

    def test_single_tenant_session_unchanged(self):
        self._activate_single_tenant(self.tenant_a)
        response = self.client.get(reverse("api:licenses_api:license-list"))
        self.assertEqual(response.status_code, 200, response.content)
        ids = {row["id"] for row in self._rows(response)}
        self.assertEqual(ids, {self.license_a.pk})

        create_response = self.client.post(
            reverse("api:licenses_api:license-list"),
            data={
                "name": "Single Tenant License",
                "software_id": self.software_a.pk,
                "license_type": LicenseTypeChoices.PERPETUAL_SEAT,
                "seats": 2,
            },
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201, create_response.content)
        created = License.all_objects.get(pk=create_response.json()["id"])
        self.assertEqual(created.tenant_id, self.tenant_a.pk)


class TenantBoundTokenUnchangedTests(APITestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Token Tenant A", slug="agg-token-a")
        self.tenant_b = Tenant.objects.create(name="Token Tenant B", slug="agg-token-b")
        self.user = User.objects.create_user(username="agg_token_user", password="password123")
        self.role_a = Role.objects.create(tenant=self.tenant_a, name="Admin", permissions=A_PERMS)
        grant(self.user, self.tenant_a, self.role_a)
        Membership.objects.create(user=self.user, tenant=self.tenant_b)

        self.mfr = Manufacturer.objects.create(name="Microsoft", slug="agg-token-microsoft")
        self.software_a = Software.objects.create(name="Office A", manufacturer=self.mfr, tenant=self.tenant_a)
        self.license_a = License.objects.create(
            name="License A",
            software=self.software_a,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant_a,
        )
        self.software_b = Software.objects.create(name="Office B", manufacturer=self.mfr, tenant=self.tenant_b)
        self.license_b = License.objects.create(
            name="License B",
            software=self.software_b,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant_b,
        )
        self.token = Token.objects.create(user=self.user, tenant=self.tenant_a)

    def test_tenant_bound_token_list_unchanged(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        response = self.client.get(reverse("api:licenses_api:license-list"))
        self.assertEqual(response.status_code, 200, response.data)
        rows = (
            response.data["results"]
            if isinstance(response.data, dict) and "results" in response.data
            else (response.data)
        )
        ids = {row["id"] for row in rows}
        self.assertIn(self.license_a.pk, ids)
        self.assertNotIn(self.license_b.pk, ids)
