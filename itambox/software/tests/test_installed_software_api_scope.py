from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APITestCase

from assets.models import Asset, Manufacturer, StatusLabel
from core.tests.mixins import grant
from organization.models import Role, Tenant, TenantGroup
from software.models import InstalledSoftware, Software

User = get_user_model()


@override_settings(ROOT_URLCONF="core.urls")
class InstalledSoftwareAPIScopeTests(APITestCase):
    def setUp(self):
        self.group = TenantGroup.objects.create(name="Customer Group", slug="installed-api-group")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="installed-api-a", group=self.group)
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="installed-api-b", group=self.group)
        self.tenant_c = Tenant.objects.create(name="Tenant C", slug="installed-api-c")
        self.tenant_outside = Tenant.objects.create(name="Outside Tenant", slug="installed-api-outside")

        self.user = User.objects.create_user(username="installed-api-member", password="pw")
        for tenant in (self.tenant_a, self.tenant_b, self.tenant_c):
            role = Role.objects.create(
                tenant=tenant,
                name=f"Installed software reader {tenant.slug}",
                permissions=["software.view_installedsoftware"],
            )
            grant(self.user, tenant, role)
        self.superuser = User.objects.create_superuser(
            username="installed-api-root", email="installed-api-root@example.com", password="pw"
        )

        manufacturer = baker.make(Manufacturer)
        status_label = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.installations = {}
        for tenant in (self.tenant_a, self.tenant_b, self.tenant_c, self.tenant_outside):
            software = Software.objects.create(
                name=f"Product {tenant.slug}",
                manufacturer=manufacturer,
                tenant=tenant,
            )
            asset = baker.make(
                Asset,
                tenant=tenant,
                status=status_label,
                asset_tag=f"INST-{tenant.slug}",
            )
            self.installations[tenant.slug] = InstalledSoftware.objects.create(
                asset=asset,
                software=software,
                version_detected="1.0",
            )

        self.soft_deleted = InstalledSoftware.objects.create(
            asset=self.installations[self.tenant_a.slug].asset,
            software=self.installations[self.tenant_a.slug].software,
            version_detected="retired",
        )
        InstalledSoftware.all_objects.filter(pk=self.soft_deleted.pk).update(deleted_at=timezone.now())

    def _select_scope(self, *, tenant=None, group=None, all_accessible=False, user=None):
        self.client.force_login(user or self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session.pop("active_all_accessible", None)
        if tenant is not None:
            session["active_tenant_id"] = tenant.pk
        elif group is not None:
            session["active_tenant_group_id"] = group.pk
        elif all_accessible:
            session["active_all_accessible"] = True
        session.save()

    def _list(self):
        return reverse("api:software_api:installedsoftware-list")

    def _detail(self, installation):
        return reverse("api:software_api:installedsoftware-detail", kwargs={"pk": installation.pk})

    @staticmethod
    def _ids(response):
        return {row["id"] for row in response.data["results"]}

    def test_single_tenant_scope_keeps_list_isolation(self):
        self._select_scope(tenant=self.tenant_a)

        response = self.client.get(self._list())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._ids(response), {self.installations[self.tenant_a.slug].pk})
        self.assertNotIn(self.soft_deleted.pk, self._ids(response))

    def test_single_tenant_scope_keeps_detail_isolation(self):
        self._select_scope(tenant=self.tenant_a)

        own_detail = self.client.get(self._detail(self.installations[self.tenant_a.slug]))
        foreign_detail = self.client.get(self._detail(self.installations[self.tenant_b.slug]))
        deleted_detail = self.client.get(self._detail(self.soft_deleted))
        self.assertEqual(own_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(foreign_detail.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(deleted_detail.status_code, status.HTTP_404_NOT_FOUND)

    def test_tenant_group_scope_lists_only_authorized_group_installations(self):
        self._select_scope(group=self.group)

        response = self.client.get(self._list())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        expected = {
            self.installations[self.tenant_a.slug].pk,
            self.installations[self.tenant_b.slug].pk,
        }
        self.assertEqual(self._ids(response), expected)
        self.assertNotIn(self.soft_deleted.pk, self._ids(response))

    def test_tenant_group_scope_retrieves_authorized_details_only(self):
        self._select_scope(group=self.group)

        for tenant in (self.tenant_a, self.tenant_b):
            detail = self.client.get(self._detail(self.installations[tenant.slug]))
            self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        outside_group = self.client.get(self._detail(self.installations[self.tenant_c.slug]))
        inaccessible = self.client.get(self._detail(self.installations[self.tenant_outside.slug]))
        deleted = self.client.get(self._detail(self.soft_deleted))
        self.assertEqual(outside_group.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(inaccessible.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(deleted.status_code, status.HTTP_404_NOT_FOUND)

    def test_all_accessible_scope_lists_authorized_installations_only(self):
        self._select_scope(all_accessible=True)

        response = self.client.get(self._list())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        expected = {
            self.installations[self.tenant_a.slug].pk,
            self.installations[self.tenant_b.slug].pk,
            self.installations[self.tenant_c.slug].pk,
        }
        self.assertEqual(self._ids(response), expected)
        self.assertNotIn(self.soft_deleted.pk, self._ids(response))

    def test_all_accessible_scope_retrieves_authorized_details_only(self):
        self._select_scope(all_accessible=True)

        for tenant in (self.tenant_a, self.tenant_b, self.tenant_c):
            detail = self.client.get(self._detail(self.installations[tenant.slug]))
            self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        inaccessible = self.client.get(self._detail(self.installations[self.tenant_outside.slug]))
        deleted = self.client.get(self._detail(self.soft_deleted))
        self.assertEqual(inaccessible.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(deleted.status_code, status.HTTP_404_NOT_FOUND)

    def test_superuser_without_active_tenant_lists_system_installations(self):
        self._select_scope(user=self.superuser)

        response = self.client.get(self._list())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        expected = {installation.pk for installation in self.installations.values()}
        self.assertEqual(self._ids(response), expected)
        self.assertNotIn(self.soft_deleted.pk, self._ids(response))

    def test_superuser_without_active_tenant_retrieves_system_detail(self):
        self._select_scope(user=self.superuser)

        outside_detail = self.client.get(self._detail(self.installations[self.tenant_outside.slug]))
        deleted_detail = self.client.get(self._detail(self.soft_deleted))
        self.assertEqual(outside_detail.status_code, status.HTTP_200_OK, outside_detail.data)
        self.assertEqual(deleted_detail.status_code, status.HTTP_404_NOT_FOUND)

    def test_unbound_member_is_denied_without_returning_installations(self):
        unbound = User.objects.create_user(username="installed-api-unbound", password="pw")
        self._select_scope(user=unbound)

        list_response = self.client.get(self._list())
        detail_response = self.client.get(self._detail(self.installations[self.tenant_a.slug]))
        self.assertEqual(list_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(detail_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn(str(self.installations[self.tenant_a.slug].pk), str(list_response.data))
        self.assertNotIn(str(self.installations[self.tenant_a.slug].pk), str(detail_response.data))
