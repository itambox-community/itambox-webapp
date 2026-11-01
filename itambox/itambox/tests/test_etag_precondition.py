"""Regression tests for audit finding I1.

An own-tenant authenticated user (with change/delete perms + active tenant)
issuing a mutating request (PATCH/DELETE) on a detail endpoint WITHOUT an
If-Match header must receive HTTP 428 Precondition Required — not a Django 500
from the exception constructor — and the write must NOT occur.

A stale If-Match token (concurrency conflict) must return HTTP 412 Precondition
Failed and likewise not mutate the resource.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APITestCase

from assets.models import Manufacturer
from core.tests.mixins import grant
from software.models import Software

User = get_user_model()


class ETagPreconditionTests(APITestCase):
    def setUp(self):
        self.staff = baker.make(
            User,
            username="staff",
            email="staff@example.com",
            is_staff=True,
            is_superuser=False,
        )
        self.staff.set_password("password123")
        self.staff.save()

        from organization.models import Role, Tenant

        self.tenant = baker.make(Tenant, name="Test Tenant", slug="test-tenant")
        self.role = baker.make(
            Role,
            tenant=self.tenant,
            name="Staff Role",
            permissions=[
                "software.view_software",
                "software.add_software",
                "software.change_software",
                "software.delete_software",
            ],
        )
        self.membership = grant(self.staff, self.tenant, self.role).membership

        self.manufacturer = baker.make(Manufacturer, name="Microsoft", slug="microsoft")
        self.software = baker.make(
            Software,
            name="Office 2021 Professional",
            manufacturer=self.manufacturer,
            version="16.0",
            category="productivity",
            license_type="proprietary",
            tenant=self.tenant,
        )

        self.detail_url = reverse("api:software_api:software-detail", kwargs={"pk": self.software.pk})

    def _login(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    def _current_etag(self):
        self.software.refresh_from_db()
        return 'W/"%s"' % self.software.updated_at.isoformat()

    # --- missing If-Match -> 428, no write -------------------------------

    def test_patch_without_if_match_returns_428_and_does_not_write(self):
        self._login()
        response = self.client.patch(self.detail_url, data={"name": "Renamed"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_428_PRECONDITION_REQUIRED)
        self.software.refresh_from_db()
        self.assertEqual(self.software.name, "Office 2021 Professional")

    def test_delete_without_if_match_returns_428_and_does_not_delete(self):
        self._login()
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_428_PRECONDITION_REQUIRED)
        self.assertTrue(Software.objects.filter(pk=self.software.pk).exists())

    # --- stale If-Match -> 412, no write ---------------------------------

    def test_patch_with_stale_if_match_returns_412_and_does_not_write(self):
        self._login()
        response = self.client.patch(
            self.detail_url,
            data={"name": "Renamed"},
            format="json",
            HTTP_IF_MATCH='W/"1999-01-01T00:00:00+00:00"',
        )
        self.assertEqual(response.status_code, status.HTTP_412_PRECONDITION_FAILED)
        self.software.refresh_from_db()
        self.assertEqual(self.software.name, "Office 2021 Professional")

    def test_delete_with_stale_if_match_returns_412_and_does_not_delete(self):
        self._login()
        response = self.client.delete(self.detail_url, HTTP_IF_MATCH='W/"1999-01-01T00:00:00+00:00"')
        self.assertEqual(response.status_code, status.HTTP_412_PRECONDITION_FAILED)
        self.assertTrue(Software.objects.filter(pk=self.software.pk).exists())

    # --- sanity: correct If-Match still works ----------------------------

    def test_patch_with_current_if_match_succeeds(self):
        self._login()
        response = self.client.patch(
            self.detail_url,
            data={"name": "Renamed"},
            format="json",
            HTTP_IF_MATCH=self._current_etag(),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.software.refresh_from_db()
        self.assertEqual(self.software.name, "Renamed")
