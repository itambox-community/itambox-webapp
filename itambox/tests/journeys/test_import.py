"""Import journeys: import never mutates beyond the role's grants and never writes lifecycle rows."""

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, Manufacturer
from core.importers.bulk_forms import is_model_importable
from core.models import Job
from organization.models import AssetHolder, Tenant

from .support import JourneyMixin


class ImportJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.make_tenant("journey-import")

    def _import(self, user, app_label, model_name, csv_text):
        self.client_login_to_tenant(user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": app_label, "model_name": model_name})
        payload = {"import_text": csv_text, "active_tab": "editor", "import_format": "csv", "delimiter": ","}
        preview = self.client.post(url, {**payload, "_preview": "1"})
        confirm = self.client.post(url, {"_confirm": "1"})
        return preview, confirm

    def test_add_only_role_cannot_update_an_existing_row_by_id(self):
        existing = Manufacturer.objects.create(name="Journey Original", slug="journey-original")
        adder = self.make_member("import-adder", {"assets.add_manufacturer"})

        self._import(adder, "assets", "manufacturer", f"id,name,slug\n{existing.pk},Journey Renamed,journey-original\n")

        existing.refresh_from_db()
        self.assertEqual(existing.name, "Journey Original", "an add-only role rewrote an existing row by id")

    def test_add_only_role_cannot_update_an_existing_row_by_natural_key(self):
        existing = Manufacturer.objects.create(name="Journey Keyed", slug="journey-keyed", description="before")
        adder = self.make_member("import-key-adder", {"assets.add_manufacturer"})

        self._import(adder, "assets", "manufacturer", "name,description\nJourney Keyed,after\n")

        existing.refresh_from_db()
        self.assertEqual(existing.description, "before", "an add-only role updated a row through the natural key")
        job = Job.objects.latest("created")
        self.assertIn("permission to update", job.logs)

    def test_change_role_can_update_by_natural_key(self):
        existing = Manufacturer.objects.create(name="Journey Editable", slug="journey-editable", description="before")
        editor = self.make_member("import-editor", {"assets.add_manufacturer", "assets.change_manufacturer"})

        self._import(editor, "assets", "manufacturer", "name,description\nJourney Editable,after\n")

        existing.refresh_from_db()
        self.assertEqual(existing.description, "after")

    def test_asset_update_ignores_same_tag_in_another_tenant(self):
        tenant_b = Tenant.objects.create(name="Journey Import B", slug="journey-import-b")
        asset_a = Asset.objects.create(name="Asset A before", asset_tag="SHARED-TAG", tenant=self.tenant)
        asset_b = Asset.objects.create(name="Asset B before", asset_tag="SHARED-TAG", tenant=tenant_b)
        editor = self.make_member("import-asset-editor", {"assets.add_asset", "assets.change_asset"})

        self._import(editor, "assets", "asset", "name,asset_tag\nAsset A after,SHARED-TAG\n")

        asset_a.refresh_from_db()
        asset_b.refresh_from_db()
        self.assertEqual(asset_a.name, "Asset A after")
        self.assertEqual(asset_b.name, "Asset B before")
        self.assertEqual(Job.objects.latest("created").status, Job.STATUS_COMPLETED)

    def test_holder_update_ignores_same_upn_in_another_tenant(self):
        tenant_b = Tenant.objects.create(name="Journey Holder B", slug="journey-holder-b")
        holder_a = self.make_holder(first_name="A before", upn="same@example.test")
        holder_b = AssetHolder.objects.create(
            first_name="B before", last_name="Holder", upn="same@example.test", tenant=tenant_b
        )
        editor = self.make_member(
            "import-holder-editor", {"organization.add_assetholder", "organization.change_assetholder"}
        )

        self._import(
            editor,
            "organization",
            "assetholder",
            "first_name,last_name,upn\nA after,Holder,same@example.test\n",
        )

        holder_a.refresh_from_db()
        holder_b.refresh_from_db()
        self.assertEqual(holder_a.first_name, "A after")
        self.assertEqual(holder_b.first_name, "B before")
        self.assertEqual(Job.objects.latest("created").status, Job.STATUS_COMPLETED)

    def test_holder_key_only_in_another_tenant_allows_local_creation(self):
        tenant_b = Tenant.objects.create(name="Journey Holder B", slug="journey-holder-b")
        holder_b = AssetHolder.objects.create(
            first_name="B before", last_name="Holder", upn="b-only@example.test", tenant=tenant_b
        )
        editor = self.make_member("import-holder-creator", {"organization.add_assetholder"})

        self._import(
            editor,
            "organization",
            "assetholder",
            "first_name,last_name,upn\nA created,Holder,b-only@example.test\n",
        )

        holder_a = AssetHolder.objects.get(tenant=self.tenant, upn="b-only@example.test")
        holder_b.refresh_from_db()
        self.assertEqual(holder_a.first_name, "A created")
        self.assertEqual(holder_b.first_name, "B before")
        self.assertEqual(Job.objects.latest("created").status, Job.STATUS_COMPLETED)

    def test_asset_tag_only_in_another_tenant_allows_local_creation(self):
        tenant_b = Tenant.objects.create(name="Journey Asset B", slug="journey-asset-b")
        asset_b = Asset.objects.create(name="Asset B before", asset_tag="B-ONLY-TAG", tenant=tenant_b)
        editor = self.make_member("import-asset-creator", {"assets.add_asset"})

        self._import(editor, "assets", "asset", "name,asset_tag\nAsset A created,B-ONLY-TAG\n")

        asset_a = Asset.objects.get(tenant=self.tenant, asset_tag="B-ONLY-TAG")
        asset_b.refresh_from_db()
        self.assertEqual(asset_a.name, "Asset A created")
        self.assertEqual(asset_b.name, "Asset B before")
        self.assertEqual(Job.objects.latest("created").status, Job.STATUS_COMPLETED)

    def test_add_only_role_cannot_update_existing_holder_in_job_tenant(self):
        holder = self.make_holder(first_name="Before", upn="add-only@example.test")
        adder = self.make_member("import-holder-add-only", {"organization.add_assetholder"})

        self._import(
            adder,
            "organization",
            "assetholder",
            "first_name,last_name,upn\nAfter,Holder,add-only@example.test\n",
        )

        holder.refresh_from_db()
        self.assertEqual(holder.first_name, "Before")
        self.assertIn("permission to update", Job.objects.latest("created").logs)

    def test_add_only_role_cannot_rewrite_an_asset_holder_by_id(self):
        holder = self.make_holder(email="holder@example.test", upn="holder@example.test")
        adder = self.make_member(
            "import-holder-adder", {"organization.add_assetholder", "organization.view_assetholder"}
        )

        self._import(
            adder,
            "organization",
            "assetholder",
            f"id,first_name,last_name,upn,email\n{holder.pk},Journey,Holder,holder@example.test,attacker@evil.test\n",
        )

        holder.refresh_from_db()
        self.assertEqual(holder.email, "holder@example.test")
        self.assertEqual(AssetHolder.objects.filter(upn="holder@example.test").count(), 1)

    def test_custody_receipts_and_asset_assignments_are_not_importable(self):
        for label in ("compliance.custodyreceipt", "assets.assetassignment"):
            model = apps.get_model(label)
            self.assertFalse(is_model_importable(model), f"{label} must be written only by its service")

    def test_service_owned_models_answer_404_on_the_import_route(self):
        user = self.make_member("import-lifecycle", {"compliance.add_custodyreceipt", "assets.add_assetassignment"})
        self.client_login_to_tenant(user, self.tenant)
        for app_label, model_name in (("compliance", "custodyreceipt"), ("assets", "assetassignment")):
            url = reverse("generic_import", kwargs={"app_label": app_label, "model_name": model_name})
            with self.subTest(model=f"{app_label}.{model_name}"):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {"_preview": "1", "import_text": "x"}).status_code, 404)
