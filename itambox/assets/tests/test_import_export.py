from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.models import Asset
from core.models import Job

User = get_user_model()


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

        response = self.client.post(import_url, {"_confirm": "1"})
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

        response = self.client.post(import_url, {"_confirm": "1"})
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

        response = self.client.post(import_url, {"_confirm": "1"})
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
        rows = self.client.session["import_rows"]

        with patch("itambox.views.generic.import_.async_task") as before_commit:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                response = self.client.post(import_url, {"_confirm": "1"})
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

    def _run_import(self, text, import_format="csv"):
        import_url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
        payload = {"active_tab": "editor", "import_format": import_format, "import_text": text, "_preview": "1"}
        if import_format == "csv":
            payload["delimiter"] = ","
        response = self.client.post(import_url, payload)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(import_url, {"_confirm": "1"})
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
