from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.tests.mixins import grant
from organization.models import (
    AssetHolder,
    Membership,
    Role,
    RoleGrantScope,
    Tenant,
    TenantGroup,
)

User = get_user_model()


class TenantViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="password123", is_superuser=True, is_staff=True
        )
        self.client.force_login(self.user)
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")

    def test_list_view(self):
        url = reverse("organization:tenant_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_list_view_counts(self):
        from organization.models import Location, Site

        # Create 2 sites for this tenant
        Site.objects.create(name="Site 1", slug="site-1", tenant=self.tenant)
        Site.objects.create(name="Site 2", slug="site-2", tenant=self.tenant)

        # Create 3 locations for this tenant
        dummy_site = Site.objects.create(name="Dummy Site", slug="dummy-site")
        Location.objects.create(name="Location 1", slug="loc-1", site=dummy_site, tenant=self.tenant)
        Location.objects.create(name="Location 2", slug="loc-2", site=dummy_site, tenant=self.tenant)
        Location.objects.create(name="Location 3", slug="loc-3", site=dummy_site, tenant=self.tenant)

        url = reverse("organization:tenant_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        table = response.context["table"]
        tenant_obj = None
        for row in table.data:
            if row.pk == self.tenant.pk:
                tenant_obj = row
                break

        self.assertIsNotNone(tenant_obj)
        self.assertEqual(tenant_obj.site_count, 2)
        self.assertEqual(tenant_obj.location_count, 3)

    def test_detail_view(self):
        url = reverse("organization:tenant_detail", kwargs={"pk": self.tenant.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_view_german_locale(self):
        url = reverse("organization:tenant_detail", kwargs={"pk": self.tenant.pk})
        response = self.client.get(url, HTTP_ACCEPT_LANGUAGE="de")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "organization/tenants/tenant_detail.html")

    def test_create_view_post(self):
        url = reverse("organization:tenant_create")
        response = self.client.post(url, {"name": "Globex Inc", "slug": "globex-inc", "currency": "EUR"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Tenant.objects.filter(name="Globex Inc").exists())

    def test_tenant_membership_create_view(self):
        from organization.models import Membership, Role

        role = Role.objects.create(tenant=self.tenant, name="Test Role", permissions=[])
        url = reverse("organization:membership_create") + f"?user={self.user.pk}"
        response = self.client.post(
            url,
            {
                "user": self.user.pk,
                "tenant": self.tenant.pk,
                "own_roles": [role.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        membership = Membership.objects.get(user=self.user, tenant=self.tenant)
        self.assertEqual(
            response.url,
            reverse("organization:membership_detail", kwargs={"pk": membership.pk}),
        )

    def test_edit_view_post(self):
        url = reverse("organization:tenant_update", kwargs={"pk": self.tenant.pk})
        response = self.client.post(url, {"name": "Acme Corp Renamed", "slug": "acme-corp-renamed", "currency": "EUR"})
        if response.status_code != 302:
            form = response.context.get("form")
            if form:
                self.fail(f"Form invalid. Errors: {form.errors}")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.name, "Acme Corp Renamed")

    def test_delete_view_post(self):
        url = reverse("organization:tenant_delete", kwargs={"pk": self.tenant.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Tenant.objects.filter(pk=self.tenant.pk).exists())

    @patch("organization.views.tenant_views.async_task")
    def test_tenant_ldap_sync_view(self, mock_async):
        with override_settings(ITAMBOX_TENANT_LDAP_CONFIGS={"acme-corp": {"SERVER_URI": "ldap://localhost"}}):
            url = reverse("organization:tenant_ldap_sync", kwargs={"pk": self.tenant.pk})
            response = self.client.post(url)
            self.assertEqual(response.status_code, 302)

            # Verify Job was created
            from core.models import Job

            job = Job.objects.filter(name=f"LDAP Sync: {self.tenant.name}").first()
            self.assertIsNotNone(job)
            self.assertEqual(job.status, Job.STATUS_PENDING)
            self.assertEqual(job.tenant, self.tenant)

            # Verify async_task was called
            mock_async.assert_called_once()
            args = mock_async.call_args[0]
            self.assertEqual(args[0], "core.tasks.sync_tenant_ldap_task")
            self.assertEqual(args[1], job.pk)
            self.assertEqual(args[2], self.tenant.slug)
            self.assertEqual(args[4], self.tenant.pk)

    @patch("core.tasks.ldap.call_command")
    def test_sync_tenant_ldap_task(self, mock_call_command):
        from django.contrib.contenttypes.models import ContentType

        from core.models import Job
        from core.tasks.ldap import sync_tenant_ldap_task

        ct = ContentType.objects.get_for_model(Tenant)
        job = Job.objects.create(name="Test LDAP Sync Job", model=ct, status=Job.STATUS_PENDING)

        sync_tenant_ldap_task(job.pk, self.tenant.slug, self.user.pk)

        job.refresh_from_db()
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        mock_call_command.assert_called_once()
        self.assertEqual(mock_call_command.call_args[0][0], "sync_tenant_ldap")
        self.assertEqual(mock_call_command.call_args[1]["tenant"], self.tenant.slug)


@override_settings(
    ITAMBOX_TENANT_LDAP_CONFIGS={
        "ldap-sibling-a": {"SERVER_URI": "ldap://a.example.test"},
        "ldap-sibling-b": {"SERVER_URI": "ldap://b.example.test"},
    }
)
class TenantLDAPSyncGroupScopeTests(TestCase):
    def setUp(self):
        self.group = TenantGroup.objects.create(
            name="LDAP sibling group",
            slug="ldap-sibling-group",
        )
        self.tenant_a = Tenant.objects.create(
            name="LDAP Sibling A",
            slug="ldap-sibling-a",
            group=self.group,
        )
        self.tenant_b = Tenant.objects.create(
            name="LDAP Sibling B",
            slug="ldap-sibling-b",
            group=self.group,
        )
        self.actor = User.objects.create_user(
            username="ldap-sibling-admin",
            password="pw",
        )
        change_role = Role.objects.create(
            tenant=self.tenant_a,
            name="LDAP settings editor",
            permissions=["organization.change_tenant"],
        )
        view_role = Role.objects.create(
            tenant=self.tenant_b,
            name="LDAP sibling viewer",
            permissions=["organization.view_tenant"],
        )
        grant(self.actor, self.tenant_a, change_role)
        grant(self.actor, self.tenant_b, view_role)

        self.client.force_login(self.actor)
        session = self.client.session
        session["active_tenant_group_id"] = self.group.pk
        session.pop("active_tenant_id", None)
        session.save()

    @patch("organization.views.tenant_views.async_task")
    def test_group_scope_cannot_borrow_change_permission_from_sibling(self, mock_async):
        from core.models import Job

        response = self.client.post(
            reverse("organization:tenant_ldap_sync", args=[self.tenant_b.pk]),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.tenant_b.get_absolute_url())
        self.assertFalse(Job.objects.filter(name=f"LDAP Sync: {self.tenant_b.name}").exists())
        mock_async.assert_not_called()

    @patch("organization.views.tenant_views.async_task")
    def test_group_scope_job_and_task_are_bound_to_requested_tenant(self, mock_async):
        from core.models import Job

        response = self.client.post(
            reverse("organization:tenant_ldap_sync", args=[self.tenant_a.pk]),
        )

        self.assertEqual(response.status_code, 302)
        job = Job.objects.get(name=f"LDAP Sync: {self.tenant_a.name}")
        self.assertEqual(job.tenant, self.tenant_a)
        mock_async.assert_called_once()
        args = mock_async.call_args.args
        self.assertEqual(args[0], "core.tasks.sync_tenant_ldap_task")
        self.assertEqual(args[1], job.pk)
        self.assertEqual(args[2], self.tenant_a.slug)
        self.assertEqual(args[3], self.actor.pk)
        self.assertEqual(args[4], self.tenant_a.pk)


class TenantGroupViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="password123", is_superuser=True, is_staff=True
        )
        self.client.force_login(self.user)
        self.group = TenantGroup.objects.create(name="Customers", slug="customers")

    def test_list_view(self):
        url = reverse("organization:tenantgroup_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_view(self):
        url = reverse("organization:tenantgroup_detail", kwargs={"pk": self.group.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_create_view_post(self):
        url = reverse("organization:tenantgroup_create")
        response = self.client.post(url, {"name": "Vendors", "slug": "vendors"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TenantGroup.objects.filter(name="Vendors").exists())

    def test_delete_view_post(self):
        url = reverse("organization:tenantgroup_delete", kwargs={"pk": self.group.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TenantGroup.objects.filter(pk=self.group.pk).exists())


class TenantGroupViewExpansionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="tenantadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.user)
        self.group = TenantGroup.objects.create(name="Premium Clients", slug="premium-clients")

    def test_update_view_post(self):
        url = reverse("organization:tenantgroup_update", kwargs={"pk": self.group.pk})
        response = self.client.post(url, {"name": "Premium Clients Updated", "slug": "premium-clients-updated"})
        self.assertEqual(response.status_code, 302)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Premium Clients Updated")


class MultiTenantMembershipTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.user = User.objects.create_user(username="staffuser", email="staff@example.com", password="password123")
        # Create standard tenant roles
        self.role_admin = Role.objects.create(
            tenant=self.tenant_a,
            name="Administrator",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "assets.delete_asset",
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
                "inventory.view_component",
                "inventory.add_component",
                "inventory.change_component",
                "inventory.delete_component",
                "organization.view_location",
                "organization.add_location",
                "organization.change_location",
                "organization.delete_location",
                "organization.view_site",
                "organization.add_site",
                "organization.change_site",
                "organization.delete_site",
                "organization.view_assetholder",
                "organization.add_assetholder",
                "organization.change_assetholder",
                "organization.delete_assetholder",
                "extras.view_dashboard",
                "extras.add_dashboard",
                "extras.change_dashboard",
                "extras.delete_dashboard",
            ],
        )
        self.role_member = Role.objects.create(
            tenant=self.tenant_a,
            name="Standard Member",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "inventory.view_accessory",
                "inventory.add_accessory",
                "inventory.change_accessory",
                "inventory.view_consumable",
                "inventory.add_consumable",
                "inventory.change_consumable",
                "inventory.view_kit",
                "inventory.add_kit",
                "inventory.change_kit",
                "inventory.view_component",
                "inventory.add_component",
                "inventory.change_component",
                "organization.view_location",
                "organization.add_location",
                "organization.change_location",
                "organization.view_site",
                "organization.add_site",
                "organization.change_site",
                "organization.view_assetholder",
                "organization.add_assetholder",
                "organization.change_assetholder",
                "extras.view_dashboard",
                "extras.add_dashboard",
                "extras.change_dashboard",
            ],
        )
        self.role_reader = Role.objects.create(
            tenant=self.tenant_a,
            name="Read-Only Viewer",
            permissions=[
                "assets.view_asset",
                "inventory.view_accessory",
                "inventory.view_consumable",
                "inventory.view_kit",
                "inventory.view_component",
                "organization.view_location",
                "organization.view_site",
                "organization.view_assetholder",
                "extras.view_dashboard",
            ],
        )

    def test_tenant_membership_creation_and_string_representation(self):
        membership = grant(self.user, self.tenant_a, self.role_member).membership
        self.assertTrue(
            membership.role_grants.filter(
                role=self.role_member,
                scopes__scope_type=RoleGrantScope.SCOPE_OWN,
            ).exists()
        )
        self.assertEqual(str(membership), f"{self.user} @ {self.tenant_a}")

    def test_membership_creation_binds_matching_assetholder(self):
        """The invitation flow is gone — holder binding now fires on membership creation
        (organization/signals.py bind_asset_holder_on_membership): an unclaimed AssetHolder
        in the joined tenant with a matching email (case-insensitive) is claimed."""
        holder = AssetHolder.objects.create(
            first_name="Beate",
            last_name="Office",
            upn="beate.office",
            email="Beate@example.com",  # different casing than the account email
            tenant=self.tenant_a,
        )
        self.assertIsNone(holder.user)

        joining_user = User.objects.create_user(
            username="beate_user", email="beate@example.com", password="password123"
        )
        grant(joining_user, self.tenant_a, self.role_member)

        holder.refresh_from_db()
        self.assertEqual(holder.user, joining_user)

    def test_membership_creation_binding_respects_tenant_claim_and_email(self):
        """No binding across tenants, onto already-claimed holders, or on email mismatch."""
        from organization.models import Membership

        other_owner = User.objects.create_user(username="owner", email="owner@example.com", password="password123")
        claimed = AssetHolder.objects.create(
            first_name="Claimed",
            last_name="Holder",
            upn="claimed.holder",
            email="beate@example.com",
            tenant=self.tenant_a,
            user=other_owner,
        )
        wrong_tenant = AssetHolder.objects.create(
            first_name="Other",
            last_name="Tenant",
            upn="other.tenant",
            email="beate@example.com",
            tenant=self.tenant_b,
        )
        wrong_email = AssetHolder.objects.create(
            first_name="Wrong",
            last_name="Email",
            upn="wrong.email",
            email="someone.else@example.com",
            tenant=self.tenant_a,
        )

        joining_user = User.objects.create_user(
            username="beate_user2", email="beate@example.com", password="password123"
        )
        Membership.objects.create(user=joining_user, tenant=self.tenant_a)

        claimed.refresh_from_db()
        wrong_tenant.refresh_from_db()
        wrong_email.refresh_from_db()
        self.assertEqual(claimed.user, other_owner)  # not stolen
        self.assertIsNone(wrong_tenant.user)  # tenant boundary respected
        self.assertIsNone(wrong_email.user)  # email must match

    def test_tenant_membership_backend_permissions(self):
        from core.managers import set_current_membership

        def _flush():
            # The backend caches the effective permission set per (user, tenant) for the
            # request; clear it after changing roles so the next has_perm re-resolves.
            for attr in list(self.user.__dict__):
                if attr.startswith("_perms_tenant_") or attr.startswith("_tenant_membership_"):
                    delattr(self.user, attr)

        def _set_role(membership, role):
            membership.role_grants.filter(
                scopes__scope_type=RoleGrantScope.SCOPE_OWN,
            ).delete()
            grant(self.user, membership.tenant, role)

        reader_mem = Membership.objects.create(
            user=self.user,
            tenant=self.tenant_a,
        )
        _set_role(reader_mem, self.role_reader)

        set_current_membership(reader_mem)
        _flush()
        self.assertTrue(self.user.has_perm("assets.view_asset"))
        self.assertFalse(self.user.has_perm("assets.add_asset"))

        _set_role(reader_mem, self.role_member)
        set_current_membership(reader_mem)
        _flush()
        self.assertTrue(self.user.has_perm("assets.view_asset"))
        self.assertTrue(self.user.has_perm("assets.add_asset"))
        self.assertFalse(self.user.has_perm("assets.delete_asset"))

        _set_role(reader_mem, self.role_admin)
        set_current_membership(reader_mem)
        _flush()
        self.assertTrue(self.user.has_perm("assets.view_asset"))
        self.assertTrue(self.user.has_perm("assets.add_asset"))
        self.assertTrue(self.user.has_perm("assets.delete_asset"))

        set_current_membership(None)

    def test_tenant_switching_middleware(self):
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from itambox.middleware import TenantMiddleware

        grant(self.user, self.tenant_a, self.role_member)

        factory = RequestFactory()

        request = factory.get("/")
        request.user = self.user

        middleware = SessionMiddleware(lambda r: None)
        middleware.process_request(request)

        tenant_middleware = TenantMiddleware(lambda r: None)
        tenant_middleware.process_request(request)

        self.assertEqual(request.active_tenant, self.tenant_a)
        self.assertEqual(request.session["active_tenant_id"], self.tenant_a.id)

        request = factory.get("/?switch_tenant={}".format(self.tenant_b.id))
        request.user = self.user
        middleware.process_request(request)
        tenant_middleware.process_request(request)

        self.assertEqual(request.active_tenant, self.tenant_a)
