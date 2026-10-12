"""Import journeys: import never mutates beyond the role's grants and never writes lifecycle rows."""

from unittest.mock import patch

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.urls import reverse

from assets.models import AssetType, Manufacturer
from assets.models.catalog import AssetTypeFieldset
from assets.services.specifications.commands import create_asset_type
from core.context import set_current_all_accessible, set_current_tenant_group
from core.importers.bulk_forms import is_model_importable
from core.models import Job, ObjectChange
from itambox.views.generic import GenericObjectImportView
from organization.models import AssetHolder, Tenant, TenantGroup

from .support import JourneyMixin


class ImportJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.make_tenant("journey-import")

    def _import(self, user, app_label, model_name, import_text, *, import_format="csv", tenant=None):
        self.client_login_to_tenant(user, tenant or self.tenant)
        url = reverse("generic_import", kwargs={"app_label": app_label, "model_name": model_name})
        payload = {
            "import_text": import_text,
            "active_tab": "editor",
            "import_format": import_format,
            "delimiter": ",",
        }
        preview = self.client.post(url, {**payload, "_preview": "1"})
        confirm = self.client.post(url, {"_confirm": "1"})
        return preview, confirm

    def test_customer_add_assettype_grant_cannot_preview_or_confirm_csv_or_yaml(self):
        Manufacturer.objects.create(name="Journey Import Maker", slug="journey-import-maker")
        creator = self.make_member("customer-import-type", {"assets.add_assettype"})
        content_type = ContentType.objects.get_for_model(AssetType)
        before_types = AssetType.objects.count()
        before_memberships = AssetTypeFieldset.objects.count()
        before_changes = ObjectChange.objects.filter(changed_object_type=content_type).count()

        for import_format, import_text in (
            ("csv", "manufacturer,model\nJourney Import Maker,Customer CSV Type\n"),
            ("yaml", "- manufacturer: Journey Import Maker\n  model: Customer YAML Type\n"),
        ):
            with self.subTest(import_format=import_format):
                preview, confirm = self._import(
                    creator,
                    "assets",
                    "assettype",
                    import_text,
                    import_format=import_format,
                )
                self.assertEqual(preview.status_code, 403)
                self.assertEqual(confirm.status_code, 403)

        self.assertEqual(AssetType.objects.count(), before_types)
        self.assertEqual(AssetTypeFieldset.objects.count(), before_memberships)
        self.assertEqual(ObjectChange.objects.filter(changed_object_type=content_type).count(), before_changes)

    def test_provider_add_assettype_role_can_import_csv_and_yaml(self):
        Manufacturer.objects.create(name="Journey Provider Maker", slug="journey-provider-maker")
        provider = self.make_provider_tenant("journey-import-provider")
        creator = self.make_member("provider-import-type", {"assets.add_assettype"}, tenant=provider)

        for import_format, import_text, model in (
            ("csv", "manufacturer,model\nJourney Provider Maker,Provider CSV Type\n", "Provider CSV Type"),
            (
                "yaml",
                "- manufacturer: Journey Provider Maker\n  model: Provider YAML Type\n",
                "Provider YAML Type",
            ),
        ):
            with self.subTest(import_format=import_format):
                with patch(
                    "assets.forms.import_forms.create_asset_type",
                    create=True,
                    wraps=create_asset_type,
                ) as create_command:
                    preview, confirm = self._import(
                        creator,
                        "assets",
                        "assettype",
                        import_text,
                        import_format=import_format,
                        tenant=provider,
                    )
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(confirm.status_code, 302)
                self.assertEqual(create_command.call_count, 1)
                self.assertTrue(AssetType.objects.filter(model=model).exists())

    def test_provider_add_assettype_role_is_denied_in_group_and_all_accessible_scopes(self):
        group = TenantGroup.objects.create(name="Import Provider Group", slug="import-provider-group")
        provider = Tenant.objects.create(
            name="Grouped Import Provider",
            slug="grouped-import-provider",
            is_provider=True,
            group=group,
        )
        creator = self.make_member("aggregate-import-type", {"assets.add_assettype"}, tenant=provider)
        self.client_login_to_tenant(creator, provider)
        request = RequestFactory().get("/import/assets/assettype/")
        request.user = creator
        request.active_tenant = provider
        view = GenericObjectImportView()
        view.request = request
        view.kwargs = {"app_label": "assets", "model_name": "assettype"}

        try:
            for scope in ("tenant_group", "all_accessible"):
                with self.subTest(scope=scope):
                    self.clear_tenant_context()
                    if scope == "tenant_group":
                        set_current_tenant_group(group)
                        set_current_all_accessible(False)
                    else:
                        set_current_tenant_group(None)
                        set_current_all_accessible(True)
                    self.assertFalse(view.has_permission())
        finally:
            set_current_tenant_group(None)
            set_current_all_accessible(False)
            self.clear_tenant_context()

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
