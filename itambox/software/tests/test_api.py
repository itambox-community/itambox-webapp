from django.contrib.auth import get_user_model
from django.urls import reverse
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APITestCase

from assets.models import Manufacturer

from ..models import Software

User = get_user_model()


class SoftwareAPITests(APITestCase):
    def setUp(self):
        # Create users
        self.superuser = baker.make(
            User, username="superuser", email="super@example.com", is_staff=True, is_superuser=True
        )
        self.superuser.set_password("password123")
        self.superuser.save()

        self.staff = baker.make(User, username="staff", email="staff@example.com", is_staff=True, is_superuser=False)
        self.staff.set_password("password123")
        self.staff.save()

        # Base metadata
        self.manufacturer = baker.make(Manufacturer, name="Microsoft", slug="microsoft")
        self.software = baker.make(
            Software,
            name="Office 2021 Professional",
            manufacturer=self.manufacturer,
            version="16.0",
            category="productivity",
            license_type="proprietary",
        )

        # Grant specific software permission codenames to staff user via multi-tenant RBAC system
        from core.tests.mixins import grant
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
        self.assignment = grant(self.staff, self.tenant, self.role)
        self.membership = self.assignment.membership

        # Make the catalogue row tenant-owned so this tenant's staff can mutate
        # it. A global (tenant=None) Software is mutable only by superusers
        # (StrictTenantPermission); a non-superuser PATCH/DELETE of one now 404s
        # by design — see software/tests/test_user_tenant_isolation.py.
        self.software.tenant = self.tenant
        self.software.save()

    def _activate_tenant(self):
        # SoftwareViewSet now enforces StrictTenantPermission, which reads
        # request.active_tenant (resolved by TenantMiddleware from the session).
        # force_authenticate alone does not set it, so activate the tenant the
        # same way the security-boundary tests do.
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    def test_software_api_list_and_detail(self):
        self.client.force_login(self.staff)
        self._activate_tenant()

        # Test list endpoint
        list_url = reverse("api:software_api:software-list")
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

        # Test detail endpoint
        detail_url = reverse("api:software_api:software-detail", kwargs={"pk": self.software.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Office 2021 Professional")
        self.assertEqual(response.data["version"], "16.0")

    def test_software_api_create_and_update_with_permissions(self):
        self.client.force_login(self.staff)
        self._activate_tenant()

        # 1. Create a Software record
        list_url = reverse("api:software_api:software-list")
        post_data = {
            "name": "Visual Studio 2022",
            "manufacturer_id": self.manufacturer.id,
            "version": "17.0",
            "category": "development",
            "license_type": "proprietary",
        }
        response = self.client.post(list_url, data=post_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        new_pk = response.data["id"]
        etag = response["ETag"]

        # 2. Modify in-place using concurrency token ETag
        detail_url = reverse("api:software_api:software-detail", kwargs={"pk": new_pk})
        put_data = {
            "name": "Visual Studio 2022 Enterprise",
            "manufacturer_id": self.manufacturer.id,
            "version": "17.2",
            "category": "development",
            "license_type": "proprietary",
        }
        response = self.client.put(detail_url, data=put_data, format="json", HTTP_IF_MATCH=etag)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Visual Studio 2022 Enterprise")
        self.assertEqual(response.data["version"], "17.2")

    def test_software_api_create_without_permissions_denied(self):
        # A standard unauthenticated client should be rejected with 401 Unauthorized
        list_url = reverse("api:software_api:software-list")
        post_data = {
            "name": "Unauthorized Soft",
            "manufacturer_id": self.manufacturer.id,
        }
        response = self.client.post(list_url, data=post_data, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_software_api_delete_verifies_cascade_protection(self):
        self.client.force_login(self.staff)
        self._activate_tenant()

        # Verify initial record
        self.assertTrue(Software.objects.filter(pk=self.software.pk).exists())
        etag = 'W/"%s"' % self.software.updated_at.isoformat()

        # Perform deletion
        detail_url = reverse("api:software_api:software-detail", kwargs={"pk": self.software.pk})
        response = self.client.delete(detail_url, HTTP_IF_MATCH=etag)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Assert no longer in DB
        self.assertFalse(Software.objects.filter(pk=self.software.pk).exists())
