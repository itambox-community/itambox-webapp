import re
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.models import Asset
from core.models import ImportPreview, Job

User = get_user_model()


def _preview_id(response):
    match = re.search(rb'name="preview_id" value="([0-9a-f]{32})"', response.content)
    assert match is not None, "preview response has no preview_id"
    return match.group(1).decode()


def _latest_preview_id():
    """The newest unconfirmed preview id (hex), mirroring the view's own lookup."""
    preview = ImportPreview.objects.filter(consumed_at__isnull=True).order_by("-created_at").first()
    assert preview is not None, "no unconfirmed preview was stored"
    return preview.pk.hex


class ImportExportPermissionTestCase(TestCase):
    def setUp(self):
        # Create users
        self.admin = User.objects.create_user(
            username="adminuser", password="password123", is_staff=True, is_superuser=True
        )
        self.staff = User.objects.create_user(
            username="staffuser", password="password123", is_staff=True, is_superuser=False
        )
        self.guest = User.objects.create_user(
            username="guestuser", password="password123", is_staff=False, is_superuser=False
        )

        # Grant staff standard view permission on assets via multi-tenant RBAC
        from core.tests.mixins import grant
        from organization.models import Role, Tenant

        self.tenant = Tenant.objects.create(name="Test Tenant", slug="test-tenant")
        self.role = Role.objects.create(tenant=self.tenant, name="Staff Role", permissions=["assets.view_asset"])
        self.assignment = grant(self.staff, self.tenant, self.role)
        self.membership = self.assignment.membership

    def test_list_view_gating_without_add_permission(self):
        # Log in as staff (with only view permission, no add permission)
        self.client.login(username="staffuser", password="password123")

        response = self.client.get(reverse("assets:asset_list"))
        self.assertEqual(response.status_code, 200)

        # Verify can_add is False in context
        self.assertFalse(response.context["can_add"])

        # Verify that "Create Asset" or "Import" links are NOT present in the HTML output
        self.assertNotContains(response, "Create Asset")
        self.assertNotContains(response, "Import")
        # Export should be present since it only requires view permission
        # Export needs only view permission. Since the kebab consolidation, the
        # affordance is the export links in the ⋮ menu (no literal "Export" label).
        self.assertContains(response, "/export/assets/asset/")

    def test_list_view_gating_with_add_permission(self):
        # Grant add permission to staff by updating the role permissions
        self.role.permissions = ["assets.view_asset", "assets.add_asset"]
        self.role.save()

        # Log in as staff
        self.client.login(username="staffuser", password="password123")

        response = self.client.get(reverse("assets:asset_list"))
        self.assertEqual(response.status_code, 200)

        # Verify can_add is True in context
        self.assertTrue(response.context["can_add"])

        # Verify that Create and Import action buttons are present in HTML output
        self.assertContains(response, "Create Asset")
        self.assertContains(response, "Import")
        # Export needs only view permission. Since the kebab consolidation, the
        # affordance is the export links in the ⋮ menu (no literal "Export" label).
        self.assertContains(response, "/export/assets/asset/")

    def test_list_view_gating_admin(self):
        # Log in as superuser
        self.client.login(username="adminuser", password="password123")

        response = self.client.get(reverse("assets:asset_list"))
        self.assertEqual(response.status_code, 200)

        # Superuser should always have add permission
        self.assertTrue(response.context["can_add"])
        self.assertContains(response, "Create Asset")
        self.assertContains(response, "Import")
        # Export needs only view permission. Since the kebab consolidation, the
        # affordance is the export links in the ⋮ menu (no literal "Export" label).
        self.assertContains(response, "/export/assets/asset/")


class AdvancedImportExportTestCase(TestCase):
    def _confirm(self, url):
        return self.client.post(url, {"_confirm": "1", "preview_id": _latest_preview_id()})

    def setUp(self):
        self.admin = User.objects.create_user(
            username="adminuser", password="password123", is_staff=True, is_superuser=True
        )
        self.client.login(username="adminuser", password="password123")

        from assets.models import AssetType, Manufacturer, StatusLabel

        self.status = StatusLabel.objects.create(name="Active", slug="active")
        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.asset_type = AssetType.objects.create(manufacturer=self.manufacturer, model="Laptop", slug="laptop")

        self.asset1 = Asset.objects.create(
            name="Asset Alpha", asset_tag="TAG-001", status=self.status, asset_type=self.asset_type
        )
        self.asset2 = Asset.objects.create(
            name="Asset Beta", asset_tag="TAG-002", status=self.status, asset_type=self.asset_type
        )

    def test_yaml_export_all(self):
        import yaml

        url = reverse("object_export", kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0})
        response = self.client.get(f"{url}?format=yaml&export_scope=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/yaml")

        data = yaml.safe_load(response.content)
        self.assertEqual(len(data), 2)
        tags = [item["asset_tag"] for item in data]
        self.assertIn("TAG-001", tags)
        self.assertIn("TAG-002", tags)

    def test_csv_is_the_default_export_format(self):
        """``format`` omitted resolves to CSV (the documented default), and the
        declared manager scope still bounds the rows."""
        url = reverse("object_export", kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("TAG-001", content)
        self.assertIn("TAG-002", content)

    def test_csv_export_filtered(self):
        url = reverse("object_export", kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0})
        response = self.client.get(f"{url}?format=csv&export_scope=filtered&q=Alpha")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

        content = response.content.decode("utf-8")
        self.assertIn("TAG-001", content)
        self.assertNotIn("TAG-002", content)

    def test_yaml_export_filtered(self):
        import yaml

        url = reverse("object_export", kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0})
        response = self.client.get(f"{url}?format=yaml&export_scope=filtered&q=Beta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/yaml")

        data = yaml.safe_load(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["asset_tag"], "TAG-002")

    def test_yaml_import_text_preview_and_confirm(self):
        yaml_data = """
- name: "Asset Gamma"
  asset_tag: "TAG-003"
  status: "{}"
  asset_type: "{}"
""".format(self.status.pk, self.asset_type.pk)
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})

        post_data = {"active_tab": "editor", "import_format": "yaml", "import_text": yaml_data, "_preview": "1"}
        response = self.client.post(import_url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asset Gamma")
        self.assertContains(response, "TAG-003")

        response = self._confirm(import_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/jobs/", response.url)

        self.assertTrue(Asset.objects.filter(asset_tag="TAG-003").exists())

    def test_csv_import_text_preview_and_confirm(self):
        csv_data = f"""name,asset_tag,status,asset_type
Asset Delta,TAG-004,{self.status.pk},{self.asset_type.pk}"""
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})

        post_data = {
            "active_tab": "editor",
            "import_format": "csv",
            "delimiter": ",",
            "import_text": csv_data,
            "_preview": "1",
        }
        response = self.client.post(import_url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asset Delta")
        self.assertContains(response, "TAG-004")

        response = self._confirm(import_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/jobs/", response.url)

        self.assertTrue(Asset.objects.filter(asset_tag="TAG-004").exists())

    def test_yaml_import_file_upload_preview_and_confirm(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        yaml_content = """
- name: "Asset Epsilon"
  asset_tag: "TAG-005"
  status: "{}"
  asset_type: "{}"
""".format(self.status.pk, self.asset_type.pk).encode("utf-8")
        uploaded_file = SimpleUploadedFile("data.yaml", yaml_content, content_type="text/yaml")

        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})

        post_data = {"active_tab": "upload", "import_format": "yaml", "csv_file": uploaded_file, "_preview": "1"}
        response = self.client.post(import_url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asset Epsilon")
        self.assertContains(response, "TAG-005")

        response = self._confirm(import_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/jobs/", response.url)
        self.assertTrue(Asset.objects.filter(asset_tag="TAG-005").exists())

    @override_settings(Q_CLUSTER={"sync": False})
    def test_async_csv_import_calls_task_after_commit(self):
        csv_data = f"""name,asset_tag,status,asset_type
Asset Zeta,TAG-006,{self.status.pk},{self.asset_type.pk}"""
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        response = self.client.post(
            import_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": csv_data,
                "_preview": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        preview = ImportPreview.objects.filter(consumed_at__isnull=True).latest("created_at")
        rows = preview.payload

        with patch("itambox.views.generic.import_.async_task") as before_commit:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                response = self._confirm(import_url)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(len(callbacks), 1)
            before_commit.assert_not_called()

        job = Job.objects.get()
        with patch("itambox.views.generic.import_.async_task") as at_commit:
            callbacks[0]()

        at_commit.assert_called_once_with(
            "core.tasks.import_csv_task",
            job.pk,
            rows,
            "assets",
            "asset",
            self.admin.pk,
            tenant_id=None,
        )

    def test_confirm_imports_the_preview_it_names_not_the_latest(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})

        def preview(tag):
            response = self.client.post(
                import_url,
                {
                    "active_tab": "editor",
                    "import_format": "csv",
                    "delimiter": ",",
                    "import_text": f"name,asset_tag\nAsset {tag},{tag}",
                    "_preview": "1",
                },
            )
            self.assertEqual(response.status_code, 200)
            return _preview_id(response)

        first_id = preview("TAG-FIRST")
        second_id = preview("TAG-SECOND")

        response = self.client.post(import_url, {"_confirm": "1", "preview_id": first_id})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(asset_tag="TAG-FIRST").exists())
        self.assertFalse(Asset.objects.filter(asset_tag="TAG-SECOND").exists())

        response = self.client.post(import_url, {"_confirm": "1", "preview_id": second_id})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(asset_tag="TAG-SECOND").exists())

    def test_a_preview_can_be_confirmed_only_once(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        response = self.client.post(
            import_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": "name,asset_tag\nAsset Once,TAG-ONCE",
                "_preview": "1",
            },
        )
        preview_id = _preview_id(response)

        self.assertEqual(self.client.post(import_url, {"_confirm": "1", "preview_id": preview_id}).status_code, 302)
        jobs = Job.objects.count()

        response = self.client.post(import_url, {"_confirm": "1", "preview_id": preview_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.count(), jobs)

    def test_a_preview_bound_to_another_workspace_is_refused(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        response = self.client.post(
            import_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": "name,asset_tag\nAsset Tenant,TAG-TENANT",
                "_preview": "1",
            },
        )
        preview_id = _preview_id(response)

        # The preview belongs to the workspace that is active while confirming;
        # switching workspaces in another tab must not import into the new one.
        with patch(
            "itambox.views.generic.import_.get_current_tenant",
            return_value=SimpleNamespace(pk=999_999),
        ):
            response = self.client.post(import_url, {"_confirm": "1", "preview_id": preview_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.count(), 0)
        self.assertFalse(Asset.objects.filter(asset_tag="TAG-TENANT").exists())

    def test_a_preview_bound_to_another_user_is_refused(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        other = User.objects.create_user(username="owner", password="password123")
        stolen = ImportPreview.objects.create(
            user=other,
            tenant=None,
            app_label="assets",
            model_name="asset",
            payload=[{"name": "Stolen"}],
        )
        response = self.client.post(import_url, {"_confirm": "1", "preview_id": stolen.pk.hex})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Job.objects.count(), 0)
        # The foreign preview stays unconsumed: a rejected claim writes nothing.
        stolen.refresh_from_db()
        self.assertIsNone(stolen.consumed_at)

    def test_a_preview_bound_to_another_model_is_refused(self):
        asset_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        preview = self.client.post(
            asset_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": "name,asset_tag\nAsset Model,TAG-MODEL",
                "_preview": "1",
            },
        )
        preview_id = _preview_id(preview)
        other_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "manufacturer"})

        response = self.client.post(other_url, {"_confirm": "1", "preview_id": preview_id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Job.objects.count(), 0)

    def test_only_the_newest_previews_are_kept(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})

        def preview(tag):
            response = self.client.post(
                import_url,
                {
                    "active_tab": "editor",
                    "import_format": "csv",
                    "delimiter": ",",
                    "import_text": f"name,asset_tag\nAsset {tag},{tag}",
                    "_preview": "1",
                },
            )
            return _preview_id(response)

        ids = [preview(f"TAG-{index}") for index in range(4)]

        # The store keeps three previews; the oldest one has been evicted.
        response = self.client.post(import_url, {"_confirm": "1", "preview_id": ids[0]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.count(), 0)

        response = self.client.post(import_url, {"_confirm": "1", "preview_id": ids[-1]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Job.objects.count(), 1)

    def test_a_malformed_preview_id_is_rejected_without_a_job(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        response = self.client.post(import_url, {"_confirm": "1", "preview_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.count(), 0)

    def test_expired_and_consumed_previews_are_cleaned_up(self):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        consumed = ImportPreview.objects.create(
            user=self.admin, app_label="assets", model_name="asset", payload=[{"n": 1}], consumed_at=timezone.now()
        )
        expired = ImportPreview.objects.create(
            user=self.admin, app_label="assets", model_name="asset", payload=[{"n": 2}]
        )
        ImportPreview.objects.filter(pk=expired.pk).update(created_at=timezone.now() - timedelta(hours=25))
        fresh = ImportPreview.objects.create(
            user=self.admin, app_label="assets", model_name="asset", payload=[{"n": 3}]
        )

        # Storing a preview runs the opportunistic cleanup.
        response = self.client.post(
            import_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": "name,asset_tag\nAsset Clean,TAG-CLEAN",
                "_preview": "1",
            },
        )
        self.assertEqual(response.status_code, 200)

        self.assertFalse(ImportPreview.objects.filter(pk=consumed.pk).exists())
        self.assertFalse(ImportPreview.objects.filter(pk=expired.pk).exists())
        self.assertTrue(ImportPreview.objects.filter(pk=fresh.pk).exists())

    def test_a_rejected_confirmation_creates_no_job_object_or_event(self):
        from extras.models import Event

        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        response = self.client.post(
            import_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": f"name,asset_tag,status,asset_type\nAsset Reject,TAG-REJECT,{self.status.pk},{self.asset_type.pk}",
                "_preview": "1",
            },
        )
        preview_id = _preview_id(response)
        self.assertEqual(self.client.post(import_url, {"_confirm": "1", "preview_id": preview_id}).status_code, 302)

        jobs = Job.objects.count()
        assets = Asset.objects.count()
        events = Event.objects.count()

        # The second confirmation is rejected: no new job, object or event.
        response = self.client.post(import_url, {"_confirm": "1", "preview_id": preview_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Job.objects.count(), jobs)
        self.assertEqual(Asset.objects.count(), assets)
        self.assertEqual(Event.objects.count(), events)

    def _run_import(self, text, import_format="csv"):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        payload = {"active_tab": "editor", "import_format": import_format, "import_text": text, "_preview": "1"}
        if import_format == "csv":
            payload["delimiter"] = ","
        response = self.client.post(import_url, payload)
        self.assertEqual(response.status_code, 200)
        response = self._confirm(import_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/jobs/", response.url)

    def test_csv_import_updates_existing_by_asset_tag(self):
        self._run_import("name,asset_tag\nAsset Alpha Updated,TAG-001")

        self.asset1.refresh_from_db()
        self.assertEqual(self.asset1.name, "Asset Alpha Updated")
        self.assertEqual(self.asset1.asset_tag, "TAG-001")
        self.assertEqual(Asset.objects.count(), 2)

    def test_yaml_import_updates_existing_by_asset_tag(self):
        self._run_import('- name: "Asset Beta Updated"\n  asset_tag: "TAG-002"\n', import_format="yaml")

        self.asset2.refresh_from_db()
        self.assertEqual(self.asset2.name, "Asset Beta Updated")
        self.assertEqual(Asset.objects.count(), 2)

    def test_import_id_column_is_ignored_and_never_selects_a_row(self):
        # An exported file carries ``id``; it must not address an existing row.
        self._run_import(
            f"id,name,asset_tag,status,asset_type\n{self.asset1.pk},Asset Hijack,TAG-NEW,{self.status.pk},{self.asset_type.pk}"
        )

        self.asset1.refresh_from_db()
        self.assertEqual(self.asset1.name, "Asset Alpha")
        self.assertEqual(self.asset1.asset_tag, "TAG-001")
        created = Asset.objects.get(asset_tag="TAG-NEW")
        self.assertNotEqual(created.pk, self.asset1.pk)
        self.assertEqual(created.name, "Asset Hijack")


@pytest.mark.serial_only
class ImportPreviewConcurrencyTestCase(TransactionTestCase):
    """Two real connections confirming the same preview must claim it once.

    Uses ``TransactionTestCase`` (so writes commit and the threads see each
    other) and a barrier so both confirmations reach the atomic claim together.
    """

    def setUp(self):
        from assets.models import AssetType, Manufacturer, StatusLabel

        self.admin = User.objects.create_user(
            username="raceadmin", password="password123", is_staff=True, is_superuser=True
        )
        self.status = StatusLabel.objects.create(name="Active", slug="active")
        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.asset_type = AssetType.objects.create(manufacturer=self.manufacturer, model="Laptop", slug="laptop")

    def test_two_concurrent_confirmations_create_exactly_one_job(self):
        if connection.vendor == "sqlite":
            self.skipTest("Row-level claim contention needs PostgreSQL; SQLite serialises writes.")

        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})

        builder = Client()
        builder.login(username="raceadmin", password="password123")
        response = builder.post(
            import_url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": f"name,asset_tag,status,asset_type\nAsset Race,TAG-RACE,{self.status.pk},{self.asset_type.pk}",
                "_preview": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        preview_id = _preview_id(response)

        import itambox.views.generic.import_ as import_views

        barrier = threading.Barrier(2)
        statuses = []
        lock = threading.Lock()

        def confirm():
            client = Client()
            client.login(username="raceadmin", password="password123")
            try:
                result = client.post(import_url, {"_confirm": "1", "preview_id": preview_id})
                with lock:
                    statuses.append(result.status_code)
            finally:
                connections.close_all()

        def before_claim(preview_pk):
            # Both requests have read and validated the same preview here; only
            # now do they race on the conditional claim update.
            barrier.wait(timeout=10)

        import_views._BEFORE_PREVIEW_CLAIM = before_claim
        threads = [threading.Thread(target=confirm) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            import_views._BEFORE_PREVIEW_CLAIM = None

        # Exactly one confirmation redirects to its job; the other is rejected.
        self.assertEqual(sorted(statuses), [200, 302])
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(Asset.objects.filter(asset_tag="TAG-RACE").count(), 1)
        self.assertEqual(ImportPreview.objects.filter(consumed_at__isnull=False).count(), 1)
