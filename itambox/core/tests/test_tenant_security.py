import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.managers import Scope, set_current_membership, set_current_tenant
from core.tests.mixins import grant
from organization.models import (
    Location,
    Membership,
    Role,
    RoleGrant,
    RoleGrantScope,
    Site,
    Tenant,
    TenantGroup,
)
from users.models import GroupMembership, UserGroup

User = get_user_model()


def grant_group_role(group, membership, role):
    GroupMembership.objects.create(user_group=group, membership=membership)
    role_grant = RoleGrant.objects.create(user_group=group, role=role)
    RoleGrantScope.objects.create(
        role_grant=role_grant,
        scope_type=RoleGrantScope.SCOPE_OWN,
    )
    return role_grant


class CoreTenantSecurityTestCase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="testuser", password="password")
        self.superuser = User.objects.create_superuser(username="admin", password="password")
        self.tenant_group = TenantGroup.objects.create(name="Global Group", slug="global-group")
        self.tenant = Tenant.objects.create(name="Test Tenant", slug="test-tenant", group=self.tenant_group)
        self.site = Site.objects.create(name="Test Site", slug="test-site")
        self.location = Location.objects.create(
            name="Test Location", slug="test-location", tenant=self.tenant, site=self.site
        )
        self.role = Role.objects.create(
            tenant=self.tenant,
            name="Admin",
            permissions=["assets.view_asset", "assets.add_asset", "assets.change_asset", "assets.delete_asset"],
        )
        self.membership = grant(self.user, self.tenant, self.role).membership

    def test_tenant_group_scoping(self):
        from core.managers import set_current_tenant_group
        from itambox.middleware import _current_user

        # Superuser scoping
        _current_user.set(self.superuser)
        set_current_tenant_group(self.tenant_group)
        self.assertEqual(Tenant.objects.count(), 1)
        self.assertEqual(Location.objects.count(), 1)

        # Standard user scoping
        _current_user.set(self.user)
        self.assertEqual(Tenant.objects.count(), 1)
        self.assertEqual(Location.objects.count(), 1)

        # Anonymous scoping
        _current_user.set(None)
        self.assertEqual(Tenant.objects.count(), 1)
        self.assertEqual(Location.objects.count(), 1)

        # Cleanup
        set_current_tenant_group(None)

    def test_tenant_group_membership_isolation(self):
        """Test that a user cannot edit an asset of a tenant where they are reader, even if switched to an admin tenant."""
        # 1. Create TenantGroup and two tenants in the same group
        group = TenantGroup.objects.create(name="Test Group 2", slug="test-group-2")
        tenant_admin = Tenant.objects.create(name="Admin Tenant", slug="admin-tenant", group=group)
        tenant_readonly = Tenant.objects.create(name="Readonly Tenant", slug="readonly-tenant", group=group)

        # 2. Create status & role
        status = StatusLabel.objects.create(name="Test Active", slug="test-active", type="deployable")
        role = AssetRole.objects.create(name="Test Role", slug="test-role")

        mfr = Manufacturer.objects.create(name="Dell", slug="dell")
        asset_type = AssetType.objects.create(manufacturer=mfr, model="Latitude 5550")

        # 3. Create asset belonging to the readonly tenant
        asset_readonly = Asset.objects.create(
            name="Protected Desktop", asset_tag="TAG-PROT", status=status, asset_role=role, tenant=tenant_readonly
        )

        # Create a non-superuser user
        test_user = User.objects.create_user(username="tenant_test_user", password="password123", is_superuser=False)

        # 4. Bind memberships (M2M: create then add roles)
        admin_role = Role.objects.create(
            tenant=tenant_admin,
            name="Admin",
            permissions=["assets.view_asset", "assets.add_asset", "assets.change_asset", "assets.delete_asset"],
        )
        reader_role = Role.objects.create(tenant=tenant_readonly, name="Reader", permissions=["assets.view_asset"])
        grant(test_user, tenant_admin, admin_role)
        grant(test_user, tenant_readonly, reader_role)

        # Set active context in test client session
        self.client.force_login(test_user)
        session = self.client.session
        session["active_tenant_id"] = tenant_admin.pk
        session.save()

        # 5. Set active context to the ADMIN tenant
        from core.managers import set_current_membership, set_current_tenant

        membership_admin = Membership.objects.get(user=test_user, tenant=tenant_admin)
        set_current_tenant(tenant_admin)
        set_current_membership(membership_admin)

        # 6. Verify that the user has general 'change_asset' permission (under active context)
        self.assertTrue(test_user.has_perm("assets.change_asset"))

        # 7. BUT verify that the user CANNOT edit the specific asset of the READONLY tenant!
        self.assertFalse(test_user.has_perm("assets.change_asset", obj=asset_readonly))

        # 8. Test that GET/POST requests are blocked (scoped out, resulting in 404 Not Found) for the readonly tenant asset

        # Update GET
        url_update = reverse("assets:asset_update", kwargs={"pk": asset_readonly.pk})
        response = self.client.get(url_update)
        self.assertEqual(response.status_code, 404)

        # Delete GET
        url_delete = reverse("assets:asset_delete", kwargs={"pk": asset_readonly.pk})
        response = self.client.get(url_delete)
        self.assertEqual(response.status_code, 404)

        # Clone GET
        url_clone = reverse("assets:asset_clone", kwargs={"pk": asset_readonly.pk})
        response = self.client.get(url_clone)
        self.assertEqual(response.status_code, 404)

        # Checkout GET (modal)
        url_checkout = reverse("assets:asset_checkout_modal", kwargs={"pk": asset_readonly.pk})
        response = self.client.get(url_checkout)
        self.assertEqual(response.status_code, 404)

        # Checkin POST
        url_checkin = reverse("assets:asset_checkin", kwargs={"pk": asset_readonly.pk})
        response = self.client.post(url_checkin)
        self.assertEqual(response.status_code, 404)

        # 9. Test that creating an asset and assigning it to the readonly tenant is blocked by form validation
        url_create = reverse("assets:asset_create")
        post_data = {
            "name": "Illegally Assigned Laptop",
            "asset_tag": "TAG-ILLEGAL",
            "status": status.pk,
            "asset_type": asset_type.pk,
            "asset_role": role.pk,
            "tenant": tenant_readonly.pk,
        }
        response = self.client.post(url_create, data=post_data)
        # The owning-tenant picker is scoped to the user's accessible tenants, so a
        # crafted tenant_readonly value is never accepted. For a member whose active
        # context resolves to a single tenant the field is hidden/auto-set (the POST
        # value is ignored and the asset lands in the active tenant); a member with
        # several accessible tenants instead gets a "not a valid choice" form error.
        # Either way the asset is NEVER created in the readonly tenant.
        self.assertIn(response.status_code, (200, 302))
        # _base_manager (unscoped): all_objects is itself tenant-scoped and would
        # hide a cross-tenant leak under the active context.
        self.assertFalse(Asset._base_manager.filter(asset_tag="TAG-ILLEGAL", tenant=tenant_readonly).exists())
        if response.status_code == 302:
            created = Asset._base_manager.filter(asset_tag="TAG-ILLEGAL").first()
            self.assertIsNotNone(created)
            self.assertEqual(created.tenant_id, tenant_admin.pk)

        # Cleanup context
        set_current_tenant(None)
        set_current_membership(None)


class RecycleBinTenantScopingTestCase(TestCase):
    """Regression tests: ``all_objects`` on tenant-scoped models must itself be
    tenant-scoped, otherwise the Recycle Bin (which queries all_objects) leaks
    soft-deleted objects across tenants."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="rb-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="rb-tenant-b")
        self.status = StatusLabel.objects.create(name="RB Active", slug="rb-active", type="deployable")
        self.asset_a = Asset.objects.create(
            name="Asset A", asset_tag="RB-A-001", status=self.status, tenant=self.tenant_a
        )
        self.asset_b = Asset.objects.create(
            name="Asset B", asset_tag="RB-B-001", status=self.status, tenant=self.tenant_b
        )
        self.asset_a.delete()  # soft delete
        self.asset_b.delete()  # soft delete

    def test_all_objects_is_tenant_scoped(self):
        from core.managers import set_current_tenant

        set_current_tenant(self.tenant_a)
        try:
            visible = set(Asset.all_objects.for_scope(Scope.current()).values_list("pk", flat=True))
            self.assertIn(self.asset_a.pk, visible)
            self.assertNotIn(
                self.asset_b.pk,
                visible,
                "Asset.all_objects leaked another tenant's (soft-deleted) asset",
            )
        finally:
            set_current_tenant(None)

    def test_all_objects_includes_soft_deleted_rows(self):
        from core.managers import set_current_tenant

        set_current_tenant(self.tenant_a)
        try:
            self.assertFalse(Asset.objects.filter(pk=self.asset_a.pk).exists())
            self.assertTrue(Asset.all_objects.filter(pk=self.asset_a.pk).exists())
        finally:
            set_current_tenant(None)

    def test_tenant_scoped_models_have_tenant_scoped_all_objects(self):
        """Every model with a tenant FK and an all_objects manager must expose
        filter_by_tenant on its queryset (the contract the Recycle Bin relies on)."""
        from django.apps import apps

        offenders = []
        for model in apps.get_models():
            if not any(f.name == "tenant" for f in model._meta.fields):
                continue
            manager = getattr(model, "all_objects", None)
            if manager is None:
                continue
            if not hasattr(manager.get_queryset(), "filter_by_tenant"):
                offenders.append(model._meta.label)
        self.assertEqual(
            offenders,
            [],
            f"Models with unscoped all_objects managers: {offenders}",
        )


class CrossTenantAttackTestCase(TestCase):
    """Authenticated user in Tenant B must not be able to read or mutate Tenant A objects
    via pk/id manipulation across all endpoint families (plan families 1-10)."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="CT-Tenant-A", slug="ct-tenant-a")
        self.tenant_b = Tenant.objects.create(name="CT-Tenant-B", slug="ct-tenant-b")

        self.status = StatusLabel.objects.create(name="CT-Active", slug="ct-active", type="deployable")
        self.mfr = Manufacturer.objects.create(name="CT-Mfr", slug="ct-mfr")
        self.asset_type = AssetType.objects.create(manufacturer=self.mfr, model="CT-Model")

        # Tenant A's asset — the object we try to attack from Tenant B
        self.asset_a = Asset.objects.create(
            name="CT-Asset-A",
            asset_tag="CT-A-001",
            status=self.status,
            asset_type=self.asset_type,
            tenant=self.tenant_a,
        )

        # User with membership ONLY in Tenant B
        self.user_b = User.objects.create_user(username="ct_user_b", password="pass123")
        self.role_b = Role.objects.create(
            tenant=self.tenant_b,
            name="CT-Admin-B",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "assets.delete_asset",
                "core.change_recyclebin",
                "core.delete_recyclebin",
            ],
        )
        self.membership_b = grant(self.user_b, self.tenant_b, self.role_b).membership

        # Login as user_b with active tenant = Tenant B
        self.client.force_login(self.user_b)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_b.pk
        session.save()

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    # -------------------------------------------------------------------------
    # Family 1: Generic UI views (detail / edit / delete / clone)
    # -------------------------------------------------------------------------

    def test_ui_detail_cross_tenant_404(self):
        url = reverse("assets:asset_detail", kwargs={"pk": self.asset_a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_ui_edit_cross_tenant_404(self):
        url = reverse("assets:asset_update", kwargs={"pk": self.asset_a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_ui_delete_cross_tenant_404(self):
        url = reverse("assets:asset_delete", kwargs={"pk": self.asset_a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_ui_clone_cross_tenant_404(self):
        url = reverse("assets:asset_clone", kwargs={"pk": self.asset_a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    # -------------------------------------------------------------------------
    # Family 2: Bulk edit / delete
    # -------------------------------------------------------------------------

    def test_bulk_delete_cross_tenant_pks_are_dropped(self):
        """POSTing Tenant A pks to the generic bulk-delete endpoint must not delete them."""
        url = reverse("bulk_delete")
        self.client.post(
            url,
            {
                "pk": [str(self.asset_a.pk)],
                "model_name": "assets.asset",
                "_confirm": "1",
            },
        )
        # Asset A must still exist (not deleted)
        self.assertTrue(Asset.all_objects.filter(pk=self.asset_a.pk).exists())

    def test_bulk_edit_cross_tenant_pks_are_not_modified(self):
        """POSTing Tenant A pks to bulk-edit must not change them."""
        original_name = self.asset_a.name
        url = reverse("bulk_edit")
        self.client.post(
            url,
            {
                "pk": [str(self.asset_a.pk)],
                "model_name": "assets.asset",
                "_selected_fields": ["name"],
                "name": "HACKED",
                "_apply": "1",
            },
        )
        self.asset_a.refresh_from_db()
        self.assertEqual(self.asset_a.name, original_name)

    # -------------------------------------------------------------------------
    # Family 3: Export endpoints
    # -------------------------------------------------------------------------

    def test_export_cross_tenant_pks_returns_empty(self):
        url = reverse("object_export", kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0})
        response = self.client.get(url + f"?pk={self.asset_a.pk}")
        self.assertEqual(response.status_code, 200)
        # The CSV must contain only the header row; the Tenant A asset must not appear
        content = response.content.decode()
        self.assertNotIn("CT-A-001", content)

    def _export_url(self):
        return reverse("object_export", kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0})

    def test_export_all_scope_excludes_other_tenant_rows(self):
        response = self.client.get(self._export_url() + "?format=csv&export_scope=all")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("CT-A-001", response.content.decode())

    def test_export_filtered_scope_excludes_other_tenant_rows(self):
        response = self.client.get(self._export_url() + "?format=csv&export_scope=filtered&q=CT-Asset-A")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("CT-A-001", response.content.decode())

    def test_export_template_render_excludes_other_tenant_rows(self):
        """The template path shares the scoped queryset — the sharpest regression
        risk, because a rendered template is free-form and carries no header row."""
        from extras.models import ExportTemplate

        template = ExportTemplate.objects.create(
            name="CT isolation template",
            content_type=ContentType.objects.get_for_model(Asset),
            template_code="{% for obj in queryset %}{{ obj.asset_tag }}\n{% endfor %}",
            file_extension="csv",
            mime_type="text/csv",
        )
        url = reverse(
            "object_export",
            kwargs={"app_label": "assets", "model_name": "asset", "template_id": template.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("CT-A-001", response.content.decode())

    # -------------------------------------------------------------------------
    # Family 4: Attachments and Journal entries (GenericFK endpoints)
    # -------------------------------------------------------------------------

    def test_journal_entry_cross_tenant_object_is_404(self):
        url = reverse(
            "journal_entry_add", kwargs={"app_label": "assets", "model_name": "asset", "object_id": self.asset_a.pk}
        )
        response = self.client.post(url, {"comment": "injected"})
        self.assertEqual(response.status_code, 404)

    def test_image_attachment_upload_cross_tenant_is_404(self):

        from django.core.files.uploadedfile import SimpleUploadedFile

        url = reverse(
            "image_attachment_upload",
            kwargs={"app_label": "assets", "model_name": "asset", "object_id": self.asset_a.pk},
        )
        fake_image = SimpleUploadedFile("test.png", b"\x89PNG\r\n", content_type="image/png")
        response = self.client.post(url, {"image": fake_image})
        self.assertEqual(response.status_code, 404)

    def test_image_attachment_delete_cross_tenant_is_404(self):
        from extras.models import ImageAttachment

        obj_ct = ContentType.objects.get_for_model(Asset)
        attachment = ImageAttachment.objects.create(
            model=obj_ct, object_id=self.asset_a.pk, name="x.png", image="test/x.png"
        )
        url = reverse("image_attachment_delete", kwargs={"pk": attachment.pk})
        response = self.client.post(url, {"return_url": "/"})
        self.assertEqual(response.status_code, 404)
        # Attachment must still exist
        self.assertTrue(ImageAttachment.objects.filter(pk=attachment.pk).exists())
        attachment.delete()

    def test_file_attachment_delete_cross_tenant_is_404(self):
        from extras.models import FileAttachment

        obj_ct = ContentType.objects.get_for_model(Asset)
        attachment = FileAttachment.objects.create(
            model=obj_ct,
            object_id=self.asset_a.pk,
            name="doc.pdf",
            file="test/doc.pdf",
        )
        url = reverse("file_attachment_delete", kwargs={"pk": attachment.pk})
        response = self.client.post(url, {"return_url": "/"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(FileAttachment.objects.filter(pk=attachment.pk).exists())
        attachment.delete()

    # -------------------------------------------------------------------------
    # Family 5: Service/action endpoints (checkout / checkin)
    # -------------------------------------------------------------------------

    def test_checkout_modal_cross_tenant_404(self):
        url = reverse("assets:asset_checkout_modal", kwargs={"pk": self.asset_a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_checkin_cross_tenant_404(self):
        url = reverse("assets:asset_checkin", kwargs={"pk": self.asset_a.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    # -------------------------------------------------------------------------
    # Family 6: REST API (session auth; TenantMiddleware resolves tenant from session)
    # -------------------------------------------------------------------------

    def test_api_list_queryset_does_not_include_tenant_a_asset(self):
        # The API list view calls Asset.objects.all() which uses TenantScopingSoftDeleteManager.
        # Prove the manager filters by active tenant so Tenant B context never returns Tenant A assets.
        set_current_tenant(self.tenant_b)
        set_current_membership(self.membership_b)
        try:
            pks = list(Asset.objects.for_scope(Scope.current()).values_list("pk", flat=True))
            self.assertNotIn(
                self.asset_a.pk, pks, "TenantScopingSoftDeleteManager leaked Tenant A's asset into Tenant B context"
            )
        finally:
            set_current_tenant(None)
            set_current_membership(None)

    def test_api_detail_cross_tenant_404(self):
        url = reverse("api:assets_api:asset-detail", kwargs={"pk": self.asset_a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_api_patch_cross_tenant_404(self):
        url = reverse("api:assets_api:asset-detail", kwargs={"pk": self.asset_a.pk})
        response = self.client.patch(
            url,
            data=json.dumps({"name": "HACKED"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        self.asset_a.refresh_from_db()
        self.assertEqual(self.asset_a.name, "CT-Asset-A")

    def test_api_delete_cross_tenant_404(self):
        url = reverse("api:assets_api:asset-detail", kwargs={"pk": self.asset_a.pk})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Asset.all_objects.filter(pk=self.asset_a.pk).exists())

    # -------------------------------------------------------------------------
    # Family 9: Recycle bin restore / purge
    # -------------------------------------------------------------------------

    def test_recycle_bin_restore_cross_tenant_blocked(self):
        # Soft-delete Tenant A asset and try to restore it as Tenant B user
        self.asset_a.delete()
        ct = ContentType.objects.get_for_model(Asset)
        url = reverse("object_restore", kwargs={"content_type_id": ct.pk, "object_id": self.asset_a.pk})
        response = self.client.post(url)
        # Expect 403 (PermissionDenied from has_permission) or 404
        self.assertIn(response.status_code, (403, 404))
        # Object must still be soft-deleted (not restored)
        self.assertFalse(Asset.objects.filter(pk=self.asset_a.pk).exists())

    def test_recycle_bin_purge_cross_tenant_blocked(self):
        self.asset_a.delete()
        ct = ContentType.objects.get_for_model(Asset)
        url = reverse("object_purge", kwargs={"content_type_id": ct.pk, "object_id": self.asset_a.pk})
        response = self.client.post(url)
        self.assertIn(response.status_code, (403, 404))
        # Object must still exist (not purged)
        self.assertTrue(Asset.all_objects.filter(pk=self.asset_a.pk).exists())


class EmailSettingsEncryptionTestCase(TestCase):
    """smtp_password must be encrypted at rest; mail send receives the plaintext."""

    def test_save_encrypts_plaintext_password(self):
        from core.models import EmailSettings

        obj = EmailSettings(smtp_host="localhost", smtp_port=587, smtp_password="s3cr3t")
        obj.save()
        db_obj = EmailSettings.objects.get(pk=1)
        self.assertTrue(
            db_obj.smtp_password.startswith("enc$"),
            "smtp_password was not encrypted on save",
        )
        self.assertNotEqual(db_obj.smtp_password, "s3cr3t")

    def test_decrypted_property_returns_plaintext(self):
        from core.models import EmailSettings

        obj = EmailSettings(smtp_host="localhost", smtp_port=587, smtp_password="s3cr3t")
        obj.save()
        db_obj = EmailSettings.objects.get(pk=1)
        self.assertEqual(db_obj.smtp_password_decrypted, "s3cr3t")

    def test_save_is_idempotent_for_already_encrypted(self):
        from core.models import EmailSettings

        obj = EmailSettings(smtp_host="localhost", smtp_port=587, smtp_password="s3cr3t")
        obj.save()
        first_cipher = EmailSettings.objects.get(pk=1).smtp_password
        # Save again without changing the password
        obj2 = EmailSettings.objects.get(pk=1)
        obj2.save()
        second_cipher = EmailSettings.objects.get(pk=1).smtp_password
        self.assertEqual(first_cipher, second_cipher, "Re-save should not double-encrypt")

    def test_empty_password_stays_empty(self):
        from core.models import EmailSettings

        obj = EmailSettings(smtp_host="localhost", smtp_port=587, smtp_password="")
        obj.save()
        db_obj = EmailSettings.objects.get(pk=1)
        self.assertFalse(db_obj.smtp_password)
        self.assertEqual(db_obj.smtp_password_decrypted, "")


# ---------------------------------------------------------------------------
# Canonical additive RBAC tests (direct and group RoleGrants)
# ---------------------------------------------------------------------------


class MultiRoleUnionTestCase(TestCase):
    """Verify the additive union of permission sources:
    direct membership RoleGrants + UserGroup RoleGrants."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Union Tenant", slug="union-tenant")
        self.user = User.objects.create_user(username="union_user", password="pass")

        # Role A grants 'assets.view_asset'
        self.role_a = Role.objects.create(
            tenant=self.tenant,
            name="Role A",
            permissions=["assets.view_asset"],
        )
        # Role B grants 'assets.add_asset'
        self.role_b = Role.objects.create(
            tenant=self.tenant,
            name="Role B",
            permissions=["assets.add_asset"],
        )
        # Role C (for UserGroup) grants 'assets.change_asset'
        self.role_c = Role.objects.create(
            tenant=self.tenant,
            name="Role C",
            permissions=["assets.change_asset"],
        )

        # Membership: direct role A plus a second additive direct grant.
        self.membership = grant(self.user, self.tenant, self.role_a).membership
        self.direct_role = Role.objects.create(
            tenant=self.tenant,
            name="Direct grants",
            permissions=["assets.delete_asset"],
        )
        grant(self.user, self.tenant, self.direct_role)

        # Tenant-owned UserGroup with a membership-backed role grant.
        self.group = UserGroup.objects.create(
            tenant=self.tenant,
            name="Test Group",
            is_active=True,
        )
        grant_group_role(self.group, self.membership, self.role_c)

        # Set context
        set_current_tenant(self.tenant)
        set_current_membership(self.membership)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def test_direct_role_perm_granted(self):
        """Permission from a direct membership role resolves True."""
        self.assertTrue(self.user.has_perm("assets.view_asset"))

    def test_additional_direct_grant_resolves_true(self):
        """Permission from a second direct RoleGrant resolves True."""
        self.assertTrue(self.user.has_perm("assets.delete_asset"))

    def test_usergroup_role_perm_granted(self):
        """Permission from a role attached to the user's UserGroup resolves True."""
        self.assertTrue(self.user.has_perm("assets.change_asset"))

    def test_unrelated_perm_resolves_false(self):
        """A permission not granted by any source resolves False."""
        self.assertFalse(self.user.has_perm("assets.add_asset"))

    def test_multi_role_membership_is_union(self):
        """Two direct roles on a membership yield the union of both roles' permissions."""
        grant(self.user, self.tenant, self.role_b)
        # Invalidate the per-request cache so the next has_perm re-queries
        cache_key = f"_effective_perms_{self.tenant.pk}"
        if hasattr(self.user, cache_key):
            delattr(self.user, cache_key)
        self.assertTrue(self.user.has_perm("assets.view_asset"))
        self.assertTrue(self.user.has_perm("assets.add_asset"))


class TenantBoundaryWithGroupsTestCase(TestCase):
    """Strict tenant boundary: grants in tenant A must not apply to objects in tenant B."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Boundary-A", slug="boundary-a")
        self.tenant_b = Tenant.objects.create(name="Boundary-B", slug="boundary-b")
        self.user = User.objects.create_user(username="boundary_user", password="pass")

        self.role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="Full Admin A",
            permissions=["assets.view_asset", "assets.change_asset", "assets.delete_asset"],
        )

        # Membership in tenant A with a second additive direct grant.
        self.membership_a = grant(self.user, self.tenant_a, self.role_a).membership
        self.direct_role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="Direct grants",
            permissions=["assets.delete_asset"],
        )
        grant(self.user, self.tenant_a, self.direct_role_a)

        # Group in tenant A with full permissions.
        self.group_a = UserGroup.objects.create(
            tenant=self.tenant_a,
            name="Group A",
            is_active=True,
        )
        grant_group_role(self.group_a, self.membership_a, self.role_a)

        # Status/asset for tenant B
        self.status = StatusLabel.objects.create(name="BA-Active", slug="ba-active", type="deployable")
        self.asset_b = Asset.objects.create(
            name="Boundary Asset B",
            asset_tag="BA-B-001",
            status=self.status,
            tenant=self.tenant_b,
        )

        set_current_tenant(self.tenant_a)
        set_current_membership(self.membership_a)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def test_group_grant_in_tenant_a_does_not_apply_to_tenant_b_object(self):
        """UserGroup grant in tenant A must not allow access to a tenant B object."""
        self.assertFalse(self.user.has_perm("assets.change_asset", obj=self.asset_b))

    def test_additional_direct_grant_does_not_apply_to_tenant_b_object(self):
        """An additional direct grant in tenant A cannot reach a tenant B object."""
        self.assertFalse(self.user.has_perm("assets.delete_asset", obj=self.asset_b))

    def test_role_in_tenant_a_does_not_apply_to_tenant_b_object(self):
        """Membership roles in tenant A must not allow access to a tenant B object."""
        self.assertFalse(self.user.has_perm("assets.view_asset", obj=self.asset_b))


class IsActiveGatingTestCase(TestCase):
    """Suspended memberships, inactive groups, and AssetHolder-only users all get zero perms."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Active Gating Tenant", slug="active-gating-tenant")
        self.user = User.objects.create_user(username="gating_user", password="pass")

        self.role = Role.objects.create(
            tenant=self.tenant,
            name="Full Role",
            permissions=["assets.view_asset", "assets.change_asset"],
        )

        self.status = StatusLabel.objects.create(name="AG-Active", slug="ag-active", type="deployable")
        self.asset = Asset.objects.create(
            name="Gating Asset",
            asset_tag="AG-001",
            status=self.status,
            tenant=self.tenant,
        )

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def _clear_perm_cache(self, user):
        """Remove per-request effective-perm caches so the next has_perm call re-queries."""
        for attr in list(vars(user)):
            if attr.startswith("_perms_tenant_") or attr.startswith("_tenant_membership_"):
                delattr(user, attr)

    def test_suspended_membership_own_roles_grant_nothing(self):
        """An inactive Membership contributes neither its direct grants nor any
        group grants that use it as their membership-backed principal."""
        membership = Membership.objects.create(user=self.user, tenant=self.tenant, is_active=False)
        grant(self.user, self.tenant, self.role)
        direct_role = Role.objects.create(
            tenant=self.tenant,
            name="Direct grants",
            permissions=["assets.delete_asset"],
        )
        grant(self.user, self.tenant, direct_role)

        set_current_tenant(self.tenant)
        set_current_membership(membership)
        self._clear_perm_cache(self.user)

        self.assertFalse(self.user.has_perm("assets.view_asset"))  # membership role
        self.assertFalse(self.user.has_perm("assets.delete_asset"))  # direct grant
        self.assertFalse(self.user.has_perm("assets.view_asset", obj=self.asset))

    def test_inactive_usergroup_contributes_nothing(self):
        """An inactive UserGroup must not contribute its roles to the effective perm set."""
        membership = Membership.objects.create(
            user=self.user,
            tenant=self.tenant,
            is_active=True,
        )
        # No direct roles on membership; only an inactive group
        group = UserGroup.objects.create(
            tenant=self.tenant,
            name="Inactive Group",
            is_active=False,
        )
        grant_group_role(group, membership, self.role)

        set_current_tenant(self.tenant)
        set_current_membership(membership)
        self._clear_perm_cache(self.user)

        self.assertFalse(self.user.has_perm("assets.view_asset"))

    def test_membership_backed_user_in_active_group_gets_group_perms(self):
        """An active group grants permissions through an owner-tenant Membership,
        even when that membership has no direct role grants."""
        other_user = User.objects.create_user(username="group_member_user", password="pass")
        other_membership = Membership.objects.create(
            user=other_user,
            tenant=self.tenant,
            is_active=True,
        )

        group = UserGroup.objects.create(
            tenant=self.tenant,
            name="Group Member",
            is_active=True,
        )
        grant_group_role(group, other_membership, self.role)

        set_current_tenant(self.tenant)
        set_current_membership(None)
        self._clear_perm_cache(other_user)

        self.assertTrue(other_user.has_perm("assets.view_asset"))
        self.assertTrue(other_user.has_perm("assets.view_asset", obj=self.asset))


class SoftDeletedRoleGrantsNothingTestCase(TestCase):
    """A soft-deleted Role must not contribute permissions via any path."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Soft-Del Tenant", slug="soft-del-tenant")
        self.user = User.objects.create_user(username="softdel_user", password="pass")

        self.role = Role.objects.create(
            tenant=self.tenant,
            name="Soon Deleted Role",
            permissions=["assets.view_asset", "assets.change_asset"],
        )
        self.membership = grant(self.user, self.tenant, self.role).membership

        self.group = UserGroup.objects.create(
            tenant=self.tenant,
            name="SD Group",
            is_active=True,
        )
        grant_group_role(self.group, self.membership, self.role)

        set_current_tenant(self.tenant)
        set_current_membership(self.membership)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def _clear_perm_cache(self):
        for attr in list(vars(self.user)):
            if attr.startswith("_perms_tenant_") or attr.startswith("_tenant_membership_"):
                delattr(self.user, attr)

    def test_active_role_grants_perms_before_delete(self):
        """Sanity: role grants access while active."""
        self._clear_perm_cache()
        self.assertTrue(self.user.has_perm("assets.view_asset"))

    def test_soft_deleted_role_on_membership_grants_nothing(self):
        """After soft-deleting the role, membership path must yield no perms."""
        self.role.delete()  # SoftDeleteMixin soft-delete
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("assets.view_asset"))

    def test_soft_deleted_role_on_group_grants_nothing(self):
        """After soft-deleting the role, group path must also yield no perms."""
        # Ensure only the group path is active
        self.membership.role_grants.all().delete()
        self.role.delete()  # SoftDeleteMixin soft-delete
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("assets.view_asset"))


class PermCacheTestCase(TestCase):
    """Two has_perm calls within the same request must hit the cached perm set,
    not re-query the database each time."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Cache Tenant", slug="cache-tenant")
        self.user = User.objects.create_user(username="cache_user", password="pass")

        self.role = Role.objects.create(
            tenant=self.tenant,
            name="Cache Role",
            permissions=["assets.view_asset"],
        )
        self.membership = grant(self.user, self.tenant, self.role).membership

        set_current_tenant(self.tenant)
        set_current_membership(self.membership)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def _clear_perm_cache(self):
        for attr in list(vars(self.user)):
            if attr.startswith("_perms_tenant_") or attr.startswith("_tenant_membership_"):
                delattr(self.user, attr)

    def test_second_has_perm_call_uses_cache(self):
        """After the first has_perm resolves perms, a second call must not issue new queries."""
        self._clear_perm_cache()
        # First call: populates the cache
        self.assertTrue(self.user.has_perm("assets.view_asset"))
        # Second call: must use the cached frozenset without hitting the DB
        with self.assertNumQueries(0):
            self.assertTrue(self.user.has_perm("assets.view_asset"))

    def test_second_has_perm_for_different_perm_uses_cache(self):
        """Cache is per-tenant perm-set, not per-perm — a second call for a different perm
        that is absent should also be served from cache without a DB query."""
        self._clear_perm_cache()
        self.user.has_perm("assets.view_asset")  # warms the cache
        with self.assertNumQueries(0):
            result = self.user.has_perm("assets.add_asset")
        self.assertFalse(result)


class SuperuserBypassTestCase(TestCase):
    """Superusers always get True, regardless of memberships, groups, or direct grants."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="SU Tenant", slug="su-tenant")
        self.superuser = User.objects.create_superuser(username="su_bypass_user", password="pass")
        # Superuser deliberately has NO Membership in this tenant

        set_current_tenant(self.tenant)
        set_current_membership(None)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def test_superuser_has_perm_without_membership(self):
        self.assertTrue(self.superuser.has_perm("assets.view_asset"))

    def test_superuser_has_perm_on_any_object(self):
        status = StatusLabel.objects.create(name="SU-Active", slug="su-active", type="deployable")
        other_tenant = Tenant.objects.create(name="Other Tenant", slug="other-su-tenant")
        asset = Asset.objects.create(
            name="SU Asset",
            asset_tag="SU-001",
            status=status,
            tenant=other_tenant,
        )
        self.assertTrue(self.superuser.has_perm("assets.change_asset", obj=asset))


class AmbientScopeLifecycleTestCase(TestCase):
    """Permission reads never bind scope; the descendant cache has one owner."""

    def setUp(self):
        self.user = User.objects.create_user(username="ambient_user", password="pass")
        self.tenant = Tenant.objects.create(name="Ambient Tenant", slug="ambient-tenant")
        self.role = Role.objects.create(tenant=self.tenant, name="Viewer", permissions=["assets.view_asset"])
        self.membership = grant(self.user, self.tenant, self.role).membership

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)

    def _scope(self):
        from core.managers import (
            get_current_all_accessible,
            get_current_membership,
            get_current_tenant,
            get_current_tenant_group,
        )

        return (
            get_current_tenant(),
            get_current_tenant_group(),
            get_current_membership(),
            get_current_all_accessible(),
        )

    def test_has_perm_without_bound_scope_does_not_write_scope(self):
        before = self._scope()
        self.assertEqual(before, (None, None, None, False))
        self.assertTrue(self.user.has_perm("assets.view_asset"))
        self.assertFalse(self.user.has_perm("assets.delete_asset"))
        self.assertEqual(self._scope(), before)

    def test_has_module_perms_without_bound_scope_does_not_write_scope(self):
        before = self._scope()
        self.assertTrue(self.user.has_module_perms("assets"))
        self.assertEqual(self._scope(), before)

    def test_ambient_fallback_membership_is_deterministic(self):
        from core.auth import MembershipBackend

        other = Tenant.objects.create(name="Aaa First", slug="aaa-first")
        Membership.objects.create(user=self.user, tenant=other, is_active=True)
        backend = MembershipBackend()
        for _ in range(3):
            self.assertEqual(backend._ambient_tenant(self.user), other)

    def test_descendant_cache_is_single_variable(self):
        from core import context, tenant_scope

        self.assertIs(tenant_scope._descendant_group_ids_cache, context._descendant_group_ids_cache)

    def test_scope_reset_clears_provider_descendant_cache(self):
        from core.tenant_scope import get_descendant_tenant_group_ids

        parent = TenantGroup.objects.create(name="C7 Parent", slug="c7-parent")
        child = TenantGroup.objects.create(name="C7 Child", slug="c7-child", parent=parent)
        self.assertEqual(get_descendant_tenant_group_ids(parent.pk, live_only=True), {parent.pk, child.pk})
        child.delete()  # soft delete
        # Still warm until a scope reset.
        set_current_tenant(None)
        self.assertEqual(get_descendant_tenant_group_ids(parent.pk, live_only=True), {parent.pk})
