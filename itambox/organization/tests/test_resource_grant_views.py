"""Operator UI for TenantResourceGrant (ADR-0001 phase 4b).

The list shows grants involving the active tenant (given + received); a
grant is created FROM a concrete pool (URL-bound, owner derived from the
pool's location) by someone holding add_tenantresourcegrant in the OWNER
tenant; revocation is owner-side only and soft-deletes.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from assets.models import Manufacturer
from core.context import set_current_all_accessible, set_current_tenant, set_current_tenant_group
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, AccessoryStock
from organization.models import (
    Location,
    Site,
    Tenant,
    TenantGroup,
    TenantResourceGrant,
    TenantResourceGrantExpiryRun,
)
from organization.views.resource_grant_views import _run_owner_ids

User = get_user_model()


class ResourceGrantViewWorld(TenantTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = Tenant.objects.create(
            name="RGV Owner",
            slug="rgv-owner",
            is_provider=True,
        )
        cls.grantee = Tenant.objects.create(
            name="RGV Grantee",
            slug="rgv-grantee",
            managed_by=cls.owner,
        )
        cls.third = Tenant.objects.create(name="RGV Third", slug="rgv-third")
        site = Site.objects.create(name="RGV Site", slug="rgv-site", tenant=cls.owner)
        cls.location = Location.objects.create(
            name="RGV Depot",
            slug="rgv-depot",
            site=site,
            tenant=cls.owner,
        )
        mfr = Manufacturer.objects.create(name="RGV Mfg", slug="rgv-mfg")
        cls.accessory = Accessory.objects.create(
            name="RGV Dock",
            slug="rgv-dock",
            manufacturer=mfr,
            tenant=cls.owner,
        )
        cls.stock = AccessoryStock.objects.create(
            accessory=cls.accessory,
            location=cls.location,
            qty=5,
        )
        cls.ct = ContentType.objects.get_for_model(AccessoryStock)

    def _add_url(self, ct=None, resource_id=None):
        return reverse(
            "organization:tenantresourcegrant_add",
            kwargs={
                "content_type_id": (ct or self.ct).pk,
                "resource_id": resource_id or self.stock.pk,
            },
        )

    def _make_grant(self):
        return TenantResourceGrant.objects.create(
            tenant=self.owner,
            grantee_tenant=self.grantee,
            resource_type=self.ct,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_USE,
        )


class ResourceGrantListViewTests(ResourceGrantViewWorld):
    PERMS = ["organization.view_tenantresourcegrant"]

    def test_owner_sees_given_grant(self):
        self._make_grant()
        user = User.objects.create_user(username="rgv-list1", password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        response = self.client.get(reverse("organization:tenantresourcegrant_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RGV Grantee")

    def test_grantee_sees_received_grant(self):
        self._make_grant()
        user = User.objects.create_user(username="rgv-list2", password="x")
        self.client_login_to_tenant(user, self.grantee, role_permissions=self.PERMS)
        response = self.client.get(reverse("organization:tenantresourcegrant_list"))
        self.assertContains(response, "RGV Owner")

    def test_unrelated_tenant_sees_nothing(self):
        self._make_grant()
        user = User.objects.create_user(username="rgv-list3", password="x")
        self.client_login_to_tenant(user, self.third, role_permissions=self.PERMS)
        response = self.client.get(reverse("organization:tenantresourcegrant_list"))
        self.assertNotContains(response, "RGV Grantee")
        self.assertNotContains(response, "RGV Depot")


class ResourceGrantCreateViewTests(ResourceGrantViewWorld):
    PERMS = ["organization.add_tenantresourcegrant"]

    def _owner_admin(self, username="rgv-admin"):
        user = User.objects.create_user(username=username, password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        return user

    def test_get_share_form(self):
        self._owner_admin()
        response = self.client.get(self._add_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RGV Dock")

    def test_post_creates_grant_bound_to_pool(self):
        user = self._owner_admin("rgv-admin2")
        response = self.client.post(
            self._add_url(),
            {
                "grantee_tenant": self.grantee.pk,
                "access_level": TenantResourceGrant.ACCESS_USE,
                "reason": "MSP spare pool for this customer",
            },
        )
        self.assertEqual(response.status_code, 302)
        grant = TenantResourceGrant.objects.get()
        self.assertEqual(grant.tenant_id, self.owner.pk)
        self.assertEqual(grant.grantee_tenant_id, self.grantee.pk)
        self.assertEqual(grant.resource_id, self.stock.pk)
        self.assertEqual(grant.resource_type_id, self.ct.pk)
        self.assertEqual(grant.granted_by_id, user.pk)

    def test_post_saves_future_deadline(self):
        self._owner_admin("rgv-admin-deadline")
        deadline = timezone.now() + timezone.timedelta(days=2)
        response = self.client.post(
            self._add_url(),
            {
                "grantee_tenant": self.grantee.pk,
                "access_level": TenantResourceGrant.ACCESS_USE,
                "valid_until": deadline.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(TenantResourceGrant.objects.get().valid_until)

    def test_post_rejects_due_deadline(self):
        self._owner_admin("rgv-admin-expired")
        response = self.client.post(
            self._add_url(),
            {
                "grantee_tenant": self.grantee.pk,
                "access_level": TenantResourceGrant.ACCESS_USE,
                "valid_until": (timezone.now() - timezone.timedelta(seconds=1)).isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TenantResourceGrant.objects.exists())

    def test_both_grantees_is_a_form_error(self):
        from organization.models import TenantGroup

        group = TenantGroup.objects.create(name="RGV Group", slug="rgv-group")
        self._owner_admin("rgv-admin3")
        response = self.client.post(
            self._add_url(),
            {
                "grantee_tenant": self.grantee.pk,
                "grantee_tenant_group": group.pk,
                "access_level": TenantResourceGrant.ACCESS_VIEW,
            },
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with errors
        self.assertFalse(TenantResourceGrant.objects.exists())

    def test_grantee_side_user_cannot_share_foreign_pool(self):
        user = User.objects.create_user(username="rgv-intruder", password="x")
        self.client_login_to_tenant(user, self.grantee, role_permissions=self.PERMS)
        response = self.client.post(
            self._add_url(),
            {
                "grantee_tenant": self.third.pk,
                "access_level": TenantResourceGrant.ACCESS_USE,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TenantResourceGrant.objects.exists())

    def test_unapproved_content_type_is_404(self):
        self._owner_admin("rgv-admin4")
        tenant_ct = ContentType.objects.get_for_model(Tenant)
        response = self.client.get(self._add_url(ct=tenant_ct, resource_id=self.owner.pk))
        self.assertEqual(response.status_code, 404)

    def test_missing_pool_is_404(self):
        self._owner_admin("rgv-admin5")
        response = self.client.get(self._add_url(resource_id=self.stock.pk + 99999))
        self.assertEqual(response.status_code, 404)


class ResourceGrantRevokeViewTests(ResourceGrantViewWorld):
    PERMS = ["organization.delete_tenantresourcegrant"]

    def test_owner_can_revoke(self):
        grant = self._make_grant()
        user = User.objects.create_user(username="rgv-rev1", password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        url = reverse("organization:tenantresourcegrant_delete", kwargs={"pk": grant.pk})
        response = self.client.post(url, {"confirm": True})
        self.assertEqual(response.status_code, 302)
        grant.refresh_from_db()
        self.assertIsNotNone(grant.deleted_at)

    def test_grantee_cannot_revoke(self):
        grant = self._make_grant()
        user = User.objects.create_user(username="rgv-rev2", password="x")
        self.client_login_to_tenant(user, self.grantee, role_permissions=self.PERMS)
        url = reverse("organization:tenantresourcegrant_delete", kwargs={"pk": grant.pk})
        response = self.client.post(url, {"confirm": True})
        self.assertEqual(response.status_code, 404)
        grant.refresh_from_db()
        self.assertIsNone(grant.deleted_at)

    def test_revoking_an_already_revoked_grant_is_404(self):
        grant = self._make_grant()
        user = User.objects.create_user(username="rgv-rev3", password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        url = reverse("organization:tenantresourcegrant_delete", kwargs={"pk": grant.pk})
        self.assertEqual(self.client.post(url, {"confirm": True}).status_code, 302)
        self.assertEqual(self.client.post(url, {"confirm": True}).status_code, 404)


class ResourceGrantDocumentationLinkTests(ResourceGrantViewWorld):
    """Every grant surface links the usage page that explains the feature."""

    PERMS = [
        "organization.view_tenantresourcegrant",
        "organization.delete_tenantresourcegrant",
    ]
    DOCS_HREF = 'href="/static/docs/usage/resource-grants.html"'

    def _login(self, username="rgv-docs"):
        user = User.objects.create_user(username=username, password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        return user

    def test_list_page_links_usage_documentation(self):
        self._login("rgv-docs-list")
        response = self.client.get(reverse("organization:tenantresourcegrant_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.DOCS_HREF)

    def test_revoke_page_links_usage_documentation(self):
        grant = self._make_grant()
        self._login("rgv-docs-revoke")
        response = self.client.get(reverse("organization:tenantresourcegrant_delete", kwargs={"pk": grant.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.DOCS_HREF)

    def test_expiry_run_pages_link_usage_documentation(self):
        cutoff = timezone.now()
        run = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.owner,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + timezone.timedelta(minutes=1),
        )
        self._login("rgv-docs-runs")
        list_response = self.client.get(reverse("organization:tenantresourcegrantexpiryrun_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, self.DOCS_HREF)
        detail_response = self.client.get(
            reverse("organization:tenantresourcegrantexpiryrun_detail", kwargs={"pk": run.pk})
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, self.DOCS_HREF)


class ResourceGrantExpiryRunViewTests(ResourceGrantViewWorld):
    PERMS = ["organization.view_tenantresourcegrant"]

    def _run(self, tenant):
        cutoff = timezone.now()
        return TenantResourceGrantExpiryRun._base_manager.create(
            tenant=tenant,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + timezone.timedelta(minutes=1),
        )

    def test_owner_sees_own_run_in_list_and_detail(self):
        run = self._run(self.owner)
        user = User.objects.create_user(username="rgv-run1", password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        list_response = self.client.get(reverse("organization:tenantresourcegrantexpiryrun_list"))
        self.assertEqual(list_response.status_code, 200)
        detail_response = self.client.get(
            reverse("organization:tenantresourcegrantexpiryrun_detail", kwargs={"pk": run.pk})
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Resource grant expiry run")

    def test_foreign_run_is_hidden_in_list_and_404_in_detail(self):
        run = self._run(self.third)
        user = User.objects.create_user(username="rgv-run2", password="x")
        self.client_login_to_tenant(user, self.owner, role_permissions=self.PERMS)
        list_response = self.client.get(reverse("organization:tenantresourcegrantexpiryrun_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertNotContains(list_response, "rgv-third")
        detail_response = self.client.get(
            reverse("organization:tenantresourcegrantexpiryrun_detail", kwargs={"pk": run.pk})
        )
        self.assertEqual(detail_response.status_code, 404)


class ResourceGrantExpiryRunScopeTests(ResourceGrantViewWorld):
    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        request.auth = None
        return request

    def test_unbound_superuser_scope_is_global(self):
        superuser = User.objects.create_superuser(username="rgv-scope-su", password="x")
        self.assertIsNone(_run_owner_ids(self._request(superuser)))

    def test_unbound_regular_user_scope_is_empty(self):
        user = User.objects.create_user(username="rgv-scope-none", password="x")
        self.assertEqual(_run_owner_ids(self._request(user)), set())

    def test_group_scope_superuser_sees_group_tenants(self):
        group = TenantGroup.objects.create(name="RGV Scope Group", slug="rgv-scope-group")
        group_tenant = Tenant.objects.create(name="RGV Scope Group Tenant", slug="rgv-scope-group-tenant", group=group)
        superuser = User.objects.create_superuser(username="rgv-scope-group-su", password="x")
        set_current_tenant_group(group)
        self.assertEqual(_run_owner_ids(self._request(superuser)), {group_tenant.pk})

    def test_all_accessible_scope_uses_accessible_tenants(self):
        set_current_all_accessible(True)
        superuser = User.objects.create_superuser(username="rgv-scope-all-su", password="x")
        self.assertEqual(_run_owner_ids(self._request(superuser)), set())
