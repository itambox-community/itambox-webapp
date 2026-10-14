"""
Comprehensive test suite for the generic CBV layer (itambox/itambox/views/generic/).

Coverage targets:
- ObjectListView         — list, pagination stub, filterset narrowing, HTMX partial
- ObjectDetailView       — GET 200, related_objects_list present
- ObjectEditView         — GET create form, POST create, POST update,
                           HTMX POST returns 204+HX-Trigger, non-HTMX POST redirects
- ObjectDeleteView       — POST soft-deletes (deleted_at set, hidden from objects)
- ObjectRestoreView      — soft-deleted object becomes visible again
- ObjectPurgeView        — hard-deletes (gone from all_objects)
- ObjectBulkEditView     — select multiple pks, POST _apply updates them
- ObjectBulkDeleteView   — select multiple pks, POST _confirm deletes them
- Permission enforcement — non-superuser without perm gets 403/redirect
- Tenant scoping         — tenant-A user gets 404 on tenant-B Asset detail

Models used:
  - Manufacturer (assets)  — not tenant-scoped; used for list/detail/edit/delete/
                             restore/purge tests.  Fixtures use the '-gcbv' suffix
                             so slugs never collide with other suites.
  - Asset (assets)         — tenant-scoped; used for bulk edit/delete and
                             cross-tenant 404 test.

All URL names verified against itambox/assets/urls.py:
  assets:manufacturer_list    -> /manufacturers/
  assets:manufacturer_detail  -> /manufacturers/<pk>/
  assets:manufacturer_create  -> /manufacturers/add/
  assets:manufacturer_update  -> /manufacturers/<pk>/edit/
  assets:manufacturer_delete  -> /manufacturers/<pk>/delete/
  assets:asset_bulk_edit      -> /assets/edit/
  assets:asset_bulk_delete    -> /assets/delete/
  assets:asset_detail         -> /assets/<pk>/
Restore/purge use core URL names:
  object_restore  -> /object/<content_type_id>/<object_id>/restore/
  object_purge    -> /object/<content_type_id>/<object_id>/purge/
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.forms.mixins import ConfirmationForm, SlugModelForm
from core.tests.mixins import TenantTestMixin
from organization.models import Tenant

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_superuser(username):
    return User.objects.create_superuser(
        username=username,
        email=f"{username}@example.com",
        password="testpassword",
    )


def _make_regular_user(username):
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="testpassword",
    )


class _ManufacturerSlugForm(SlugModelForm):
    class Meta:
        model = Manufacturer
        fields = ("name", "slug")


class ConfirmationFormTests(TestCase):
    def test_explicit_return_url_initial_wins(self):
        form = ConfirmationForm(
            instance=Manufacturer(name="Explicit Return Target"), initial={"return_url": "/custom/"}
        )

        self.assertEqual(form["return_url"].value(), "/custom/")

    def test_resolvable_list_route_wins_over_object_absolute_url(self):
        form = ConfirmationForm(instance=Manufacturer(name="List Return Target"))

        self.assertEqual(form["return_url"].value(), reverse("assets:manufacturer_list"))

    def test_no_reverse_match_falls_back_to_absolute_url(self):
        instance = SimpleNamespace(get_absolute_url=lambda: "/fallback-target/")
        with (
            patch("core.forms.mixins.get_model_viewname", return_value="missing:list"),
            patch("core.forms.mixins.reverse", side_effect=NoReverseMatch) as reverse_mock,
        ):
            form = ConfirmationForm(instance=instance)

        reverse_mock.assert_called_once_with("missing:list")
        self.assertEqual(form["return_url"].value(), "/fallback-target/")

    def test_instance_without_any_return_route_keeps_return_url_empty(self):
        instance = SimpleNamespace()
        with (
            patch("core.forms.mixins.get_model_viewname", return_value="missing:list"),
            patch("core.forms.mixins.reverse", side_effect=NoReverseMatch),
        ):
            form = ConfirmationForm(instance=instance)

        self.assertIsNone(form.fields["return_url"].initial)

    def test_slug_model_form_does_not_reference_removed_standalone_script(self):
        self.assertTrue(all("slugify.js" not in path for path in _ManufacturerSlugForm().media._js))


# ---------------------------------------------------------------------------
# 1. ObjectListView
# ---------------------------------------------------------------------------


class ObjectListViewTests(TestCase):
    """Tests for ObjectListView via ManufacturerListView."""

    def setUp(self):
        self.user = _make_superuser("listview-gcbv")
        self.client.force_login(self.user)

        # Create two manufacturers; '-gcbv' suffix prevents slug collisions
        self.mfr1 = Manufacturer.objects.create(name="Alpha Corp GCBV", slug="alpha-corp-gcbv")
        self.mfr2 = Manufacturer.objects.create(name="Beta Corp GCBV", slug="beta-corp-gcbv")

    def test_list_get_200(self):
        """GET /manufacturers/ returns 200."""
        response = self.client.get(reverse("assets:manufacturer_list"))
        self.assertEqual(response.status_code, 200)

    def test_list_shows_created_rows(self):
        """Both manufacturers appear in the response body."""
        response = self.client.get(reverse("assets:manufacturer_list"))
        self.assertContains(response, "Alpha Corp GCBV")
        self.assertContains(response, "Beta Corp GCBV")

    def test_filterset_narrows_results(self):
        """Passing ?q=Alpha returns only Alpha; Beta is absent."""
        response = self.client.get(reverse("assets:manufacturer_list"), {"q": "Alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alpha Corp GCBV")
        self.assertNotContains(response, "Beta Corp GCBV")

    def test_htmx_partial_returns_partial_template(self):
        """An HTMX non-boosted request receives the content partial, not the
        full page template."""
        # Send HX-Request without HX-Boosted so BaseHTMXView.is_htmx_partial()
        # returns True and renders content_partial_name instead of the full page.
        response = self.client.get(
            reverse("assets:manufacturer_list"),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        # The partial wraps table content without the full shell; the full page
        # template is 'generic/object_list.html'.  When the partial is served,
        # the templates_used list will contain the partial name but NOT the
        # full-page shell name.
        template_names = [t.name for t in response.templates]
        self.assertIn("htmx/list_page_wrapper.html", template_names)
        self.assertNotIn("base.html", template_names)

    def test_normal_request_uses_full_template(self):
        """A non-HTMX request uses the full page template (has 'base.html' or
        the object_list template)."""
        response = self.client.get(reverse("assets:manufacturer_list"))
        template_names = [t.name for t in response.templates]
        # Full-page render: the list template is used (not just the partial)
        any_list_template = any("list" in name or "base" in name for name in template_names)
        self.assertTrue(any_list_template)
        self.assertNotIn("htmx/list_page_wrapper.html", template_names)

    def test_full_list_defines_page_header_once(self):
        """The list template delegates the page-header wrapper to its base."""
        response = self.client.get(reverse("assets:manufacturer_list"))
        self.assertEqual(response.content.decode().count('id="page-header-block"'), 1)

    def test_table_in_context(self):
        """The context always contains a 'table' key."""
        response = self.client.get(reverse("assets:manufacturer_list"))
        self.assertIn("table", response.context)


# ---------------------------------------------------------------------------
# 2. ObjectDetailView
# ---------------------------------------------------------------------------


class ObjectDetailViewTests(TestCase):
    """Tests for ObjectDetailView via ManufacturerDetailView."""

    def setUp(self):
        self.user = _make_superuser("detailview-gcbv")
        self.client.force_login(self.user)

        self.mfr = Manufacturer.objects.create(name="Detail Mfr GCBV", slug="detail-mfr-gcbv")

    def test_detail_get_200(self):
        """GET /manufacturers/<pk>/ returns 200."""
        response = self.client.get(reverse("assets:manufacturer_detail", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 200)

    def test_detail_renders_object_name(self):
        """The manufacturer name appears in the rendered output."""
        response = self.client.get(reverse("assets:manufacturer_detail", kwargs={"pk": self.mfr.pk}))
        self.assertContains(response, "Detail Mfr GCBV")

    def test_related_objects_list_present_in_context(self):
        """The 'related_objects_list' key is always present in context after the
        view builds it (may be an empty list when there are no reverse-related
        objects with counts > 0)."""
        response = self.client.get(reverse("assets:manufacturer_detail", kwargs={"pk": self.mfr.pk}))
        self.assertIn("related_objects_list", response.context)

    def test_detail_unknown_pk_returns_404(self):
        """An unknown pk yields 404."""
        response = self.client.get(reverse("assets:manufacturer_detail", kwargs={"pk": 999999}))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# 3. ObjectEditView
# ---------------------------------------------------------------------------


class ObjectEditViewTests(TestCase):
    """Tests for ObjectEditView via ManufacturerEditView."""

    def setUp(self):
        self.user = _make_superuser("editview-gcbv")
        self.client.force_login(self.user)

        self.mfr = Manufacturer.objects.create(name="Edit Mfr GCBV", slug="edit-mfr-gcbv")

    # --- GET ---

    def test_create_form_get_200(self):
        """GET /manufacturers/add/ returns 200 with a form."""
        response = self.client.get(reverse("assets:manufacturer_create"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)

    def test_edit_form_get_200(self):
        """GET /manufacturers/<pk>/edit/ returns 200."""
        response = self.client.get(reverse("assets:manufacturer_update", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 200)

    # --- POST create ---

    def test_post_create_creates_object(self):
        """POSTing the create form with valid data creates a new Manufacturer."""
        count_before = Manufacturer.objects.count()
        response = self.client.post(
            reverse("assets:manufacturer_create"),
            data={
                "name": "New Mfr GCBV",
                "slug": "new-mfr-gcbv",
                "description": "",
            },
        )
        # non-HTMX success → redirect (302)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Manufacturer.objects.count(), count_before + 1)
        self.assertTrue(Manufacturer.objects.filter(slug="new-mfr-gcbv").exists())

    # --- POST update ---

    def test_post_update_modifies_object(self):
        """POSTing the edit form updates an existing Manufacturer."""
        response = self.client.post(
            reverse("assets:manufacturer_update", kwargs={"pk": self.mfr.pk}),
            data={
                "name": "Edit Mfr GCBV Updated",
                "slug": "edit-mfr-gcbv",
                "description": "Updated description",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.mfr.refresh_from_db()
        self.assertEqual(self.mfr.name, "Edit Mfr GCBV Updated")

    # --- HTMX POST → 204 with HX-Trigger ---

    def test_htmx_edit_post_returns_redirect_not_204(self):
        """ObjectEditView form_valid always returns an HttpResponseRedirect (302
        regardless of HTMX) — it is NOT an HtmxActionMixin service view."""
        response = self.client.post(
            reverse("assets:manufacturer_update", kwargs={"pk": self.mfr.pk}),
            data={
                "name": "HTMX Edit GCBV",
                "slug": "edit-mfr-gcbv",
                "description": "",
            },
            HTTP_HX_REQUEST="true",
        )
        # ObjectEditView is a standard UpdateView; HTMX callers still get a
        # redirect (the client follows it or HTMX intercepts).
        self.assertEqual(response.status_code, 302)

    # --- Non-HTMX POST → redirect ---

    def test_non_htmx_post_redirects(self):
        """A non-HTMX create POST redirects (302) on success."""
        response = self.client.post(
            reverse("assets:manufacturer_create"),
            data={
                "name": "Redirect Test GCBV",
                "slug": "redirect-test-gcbv",
                "description": "",
            },
        )
        self.assertEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# 4. ObjectDeleteView
# ---------------------------------------------------------------------------


class ObjectDeleteViewTests(TestCase):
    """Tests for ObjectDeleteView via ManufacturerDeleteView.

    Manufacturer.delete() is a soft-delete (SoftDeleteMixin) — it sets
    deleted_at and hides the object from the default manager.
    """

    def setUp(self):
        self.user = _make_superuser("deleteview-gcbv")
        self.client.force_login(self.user)

        self.mfr = Manufacturer.objects.create(name="Delete Mfr GCBV", slug="delete-mfr-gcbv")

    def test_delete_get_200(self):
        """GET the delete confirmation page returns 200."""
        response = self.client.get(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        form_start = body.index('<form method="post"')
        form_end = body.index(">", form_start)
        self.assertIn('hx-boost="false"', body[form_start:form_end])

    def test_post_delete_soft_deletes(self):
        """POSTing the delete view sets deleted_at (soft delete) so the object
        disappears from the default manager but exists in all_objects."""
        self.client.post(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        # Hidden from default manager
        self.assertFalse(Manufacturer.objects.filter(pk=self.mfr.pk).exists())
        # Still in all_objects
        deleted = Manufacturer.all_objects.get(pk=self.mfr.pk)
        self.assertIsNotNone(deleted.deleted_at)

    def test_post_delete_redirects(self):
        """A non-HTMX delete POST redirects on success."""
        response = self.client.post(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("assets:manufacturer_list"))

    def test_confirmation_page_lists_blockers_and_disables_confirm(self):
        """Before submission the page names the referencing rows and disables the confirm control (#817)."""
        AssetType.objects.create(manufacturer=self.mfr, model="Blocker Model GCBV", slug="blocker-model-gcbv")
        response = self.client.get(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Blocker Model GCBV", body)
        self.assertIn("still referenced by", body)
        button = body[body.index('<button type="submit" class="btn btn-danger"') :]
        self.assertIn("disabled", button[: button.index(">")])

    def test_refused_delete_renders_message_naming_blocker(self):
        """A refused delete re-renders the page with an alert naming the blocker; the record survives (#817)."""
        AssetType.objects.create(manufacturer=self.mfr, model="Blocker Model GCBV", slug="blocker-model-gcbv")
        response = self.client.post(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 409)
        body = response.content.decode()
        self.assertIn('class="alert alert-danger"', body)
        self.assertIn("Blocker Model GCBV", body)
        self.assertTrue(Manufacturer.objects.filter(pk=self.mfr.pk).exists())

    def test_archived_dependent_blocks_and_is_reported_by_count(self):
        """An archived asset type still blocks the delete; it is counted, not named (#817)."""
        asset_type = AssetType.objects.create(manufacturer=self.mfr, model="Archived GCBV", slug="archived-gcbv")
        asset_type.delete()
        self.assertFalse(AssetType.objects.filter(pk=asset_type.pk).exists())
        response = self.client.post(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        self.assertEqual(response.status_code, 409)
        body = response.content.decode()
        self.assertIn("1 archived or outside your scope", body)
        self.assertNotIn("Archived GCBV", body)
        self.assertTrue(Manufacturer.objects.filter(pk=self.mfr.pk).exists())


# ---------------------------------------------------------------------------
# 5. ObjectRestoreView and ObjectPurgeView
# ---------------------------------------------------------------------------


class RestoreAndPurgeTests(TestCase):
    """Tests for ObjectRestoreView / ObjectPurgeView (core URL names)."""

    def setUp(self):
        self.user = _make_superuser("restorepurge-gcbv")
        self.client.force_login(self.user)

        # Create and soft-delete a manufacturer directly
        self.mfr = Manufacturer.objects.create(name="Restore Mfr GCBV", slug="restore-mfr-gcbv")
        self.mfr.delete()  # soft-delete
        self.mfr.refresh_from_db()
        self.assertIsNotNone(self.mfr.deleted_at)

        self.ct = ContentType.objects.get_for_model(Manufacturer)

    def test_restore_makes_object_visible_again(self):
        """POST to object_restore brings the soft-deleted object back."""
        url = reverse(
            "object_restore",
            kwargs={"content_type_id": self.ct.pk, "object_id": self.mfr.pk},
        )
        response = self.client.post(url)
        # Non-HTMX → redirect to list?deleted=true
        self.assertIn(response.status_code, [302, 204])

        self.mfr.refresh_from_db()
        self.assertIsNone(self.mfr.deleted_at)
        # Visible via default manager again
        self.assertTrue(Manufacturer.objects.filter(pk=self.mfr.pk).exists())

    def test_purge_hard_deletes_object(self):
        """POST to object_purge permanently removes the object from all managers."""
        # Create a fresh soft-deleted manufacturer to avoid interference
        mfr2 = Manufacturer.objects.create(name="Purge Mfr GCBV", slug="purge-mfr-gcbv")
        mfr2.delete()
        pk = mfr2.pk

        url = reverse(
            "object_purge",
            kwargs={"content_type_id": self.ct.pk, "object_id": pk},
        )
        response = self.client.post(url)
        self.assertIn(response.status_code, [302, 204])

        self.assertFalse(Manufacturer.objects.filter(pk=pk).exists())
        self.assertFalse(Manufacturer.all_objects.filter(pk=pk).exists())


# ---------------------------------------------------------------------------
# 6. ObjectBulkEditView / ObjectBulkDeleteView
# ---------------------------------------------------------------------------


class BulkViewTests(TenantTestMixin, TestCase):
    """Tests for ObjectBulkEditView and ObjectBulkDeleteView via the Asset bulk
    views.  Asset is tenant-scoped so we use TenantTestMixin to wire up the
    session and context-var."""

    def setUp(self):
        # Set up a full tenant + superuser context
        self.setup_tenant_context(
            name="BulkTest Tenant GCBV",
            slug="bulktest-tenant-gcbv",
        )

        # Log in as tenant_admin (superuser) and set active tenant in session
        self.client_login_to_tenant(self.tenant_admin, self.tenant)

        # Status label needed for Asset creation
        self.status, _ = StatusLabel.objects.get_or_create(
            slug="available",
            defaults={
                "name": "Available",
                "type": "deployable",
                "color": "28a745",
            },
        )

        # Create two assets in the tenant
        self.asset1 = Asset.objects.create(
            name="Bulk Asset 1 GCBV",
            asset_tag="BULK-001-GCBV",
            tenant=self.tenant,
            status=self.status,
        )
        self.asset2 = Asset.objects.create(
            name="Bulk Asset 2 GCBV",
            asset_tag="BULK-002-GCBV",
            tenant=self.tenant,
            status=self.status,
        )

    def test_bulk_edit_get_shows_confirmation_form(self):
        """GET to bulk edit with pk params renders the edit form (200)."""
        url = reverse("assets:asset_bulk_edit")
        response = self.client.post(
            url,
            data={
                "pk": [str(self.asset1.pk), str(self.asset2.pk)],
                "model_name": "assets.asset",
            },
        )
        # First POST without _apply shows the form
        self.assertEqual(response.status_code, 200)

    def test_bulk_edit_apply_updates_objects(self):
        """POSTing with _apply and a selected field updates all selected pks."""
        # Create a new role to bulk-assign
        role, _ = AssetRole.objects.get_or_create(
            slug="bulk-role-gcbv",
            defaults={"name": "Bulk Role GCBV"},
        )

        url = reverse("assets:asset_bulk_edit")
        response = self.client.post(
            url,
            data={
                "pk": [str(self.asset1.pk), str(self.asset2.pk)],
                "_apply": "1",
                "_selected_fields": ["asset_role"],
                "asset_role": str(role.pk),
                "model_name": "assets.asset",
            },
        )
        # Should redirect after successful bulk edit
        self.assertEqual(response.status_code, 302)

        self.asset1.refresh_from_db()
        self.asset2.refresh_from_db()
        self.assertEqual(self.asset1.asset_role, role)
        self.assertEqual(self.asset2.asset_role, role)

    def test_bulk_delete_shows_confirmation(self):
        """POSTing without _confirm renders the confirmation page (200)."""
        url = reverse("assets:asset_bulk_delete")
        response = self.client.post(
            url,
            data={
                "pk": [str(self.asset1.pk)],
                "model_name": "assets.asset",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_bulk_delete_confirm_soft_deletes(self):
        """POSTing with _confirm soft-deletes all selected assets."""
        url = reverse("assets:asset_bulk_delete")
        response = self.client.post(
            url,
            data={
                "pk": [str(self.asset1.pk), str(self.asset2.pk)],
                "_confirm": "1",
                "model_name": "assets.asset",
            },
        )
        self.assertEqual(response.status_code, 302)

        # Soft-deleted: gone from default manager
        self.assertFalse(Asset.objects.filter(pk=self.asset1.pk).exists())
        self.assertFalse(Asset.objects.filter(pk=self.asset2.pk).exists())

        # Still in all_objects with deleted_at set
        self.assertTrue(Asset.all_objects.filter(pk=self.asset1.pk, deleted_at__isnull=False).exists())
        self.assertTrue(Asset.all_objects.filter(pk=self.asset2.pk, deleted_at__isnull=False).exists())


# ---------------------------------------------------------------------------
# 7. Permission enforcement
# ---------------------------------------------------------------------------


class PermissionEnforcementTests(TenantTestMixin, TestCase):
    """A logged-in user WITHOUT the required permission gets 403 (or a redirect
    to login when the framework redirects instead of 403)."""

    def setUp(self):
        # Regular tenant with no permissions
        self.setup_tenant_context(
            name="Perm Test Tenant GCBV",
            slug="perm-test-tenant-gcbv",
            permissions=[],  # empty: no perms granted
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        self.mfr = Manufacturer.objects.create(name="Perm Mfr GCBV", slug="perm-mfr-gcbv")

    def test_no_view_perm_blocks_list(self):
        """A user without assets.view_manufacturer cannot list manufacturers."""
        response = self.client.get(reverse("assets:manufacturer_list"))
        # PermissionRequiredMixin redirects anonymous users and 403-s authenticated
        # users who lack the perm (Django raises PermissionDenied -> 403).
        self.assertIn(response.status_code, [302, 403])

    def test_no_view_perm_blocks_detail(self):
        """Without view perm, the detail page is also blocked."""
        response = self.client.get(reverse("assets:manufacturer_detail", kwargs={"pk": self.mfr.pk}))
        self.assertIn(response.status_code, [302, 403])

    def test_no_add_perm_blocks_create_post(self):
        """Without assets.add_manufacturer, POSTing the create form is blocked."""
        response = self.client.post(
            reverse("assets:manufacturer_create"),
            data={
                "name": "Unauthorized GCBV",
                "slug": "unauthorized-gcbv",
                "description": "",
            },
        )
        self.assertIn(response.status_code, [302, 403])
        self.assertFalse(Manufacturer.objects.filter(slug="unauthorized-gcbv").exists())

    def test_no_delete_perm_blocks_delete(self):
        """Without assets.delete_manufacturer, delete is blocked."""
        response = self.client.post(reverse("assets:manufacturer_delete", kwargs={"pk": self.mfr.pk}))
        self.assertIn(response.status_code, [302, 403])
        # Object must still exist (not deleted)
        self.assertTrue(Manufacturer.objects.filter(pk=self.mfr.pk).exists())


# ---------------------------------------------------------------------------
# 8. Tenant scoping — cross-tenant 404 on Asset detail
# ---------------------------------------------------------------------------


class TenantScopingTests(TenantTestMixin, TestCase):
    """A tenant-A user gets 404 on an Asset that belongs to tenant-B because
    TenantScopingViewMixin + TenantScopingSoftDeleteManager filter it out of
    the queryset, and Django's get_object_or_404 raises Http404.
    """

    def setUp(self):
        # Tenant A — user we log in as
        self.setup_tenant_context(
            name="TenantA GCBV",
            slug="tenanta-gcbv",
            permissions=["assets.view_asset", "assets.add_asset"],
        )

        # Tenant B — a different tenant whose assets must not be visible to A
        self.tenant_b = Tenant.objects.create(name="TenantB GCBV", slug="tenantb-gcbv")

        # Status label for Asset
        self.status, _ = StatusLabel.objects.get_or_create(
            slug="available",
            defaults={
                "name": "Available",
                "type": "deployable",
                "color": "28a745",
            },
        )

        # Asset belonging to tenant B
        self.asset_b = Asset.objects.create(
            name="Tenant B Asset GCBV",
            asset_tag="TB-ASSET-001-GCBV",
            tenant=self.tenant_b,
            status=self.status,
        )

        # Log tenant_user in as a member of tenant A
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def test_cross_tenant_asset_detail_returns_404(self):
        """Tenant-A user cannot access tenant-B's Asset detail — gets 404."""
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset_b.pk}))
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_asset_clone_returns_404(self):
        """The clone source lookup must use the tenant-scoped queryset."""
        response = self.client.get(reverse("assets:asset_clone", kwargs={"pk": self.asset_b.pk}))
        self.assertEqual(response.status_code, 404)

    def test_own_tenant_asset_detail_accessible(self):
        """A tenant-A user CAN access an asset that belongs to tenant A."""
        asset_a = Asset.objects.create(
            name="Tenant A Asset GCBV",
            asset_tag="TA-ASSET-001-GCBV",
            tenant=self.tenant,
            status=self.status,
        )
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": asset_a.pk}))
        self.assertEqual(response.status_code, 200)
