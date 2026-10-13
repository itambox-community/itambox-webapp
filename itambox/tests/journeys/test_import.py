"""Import journeys: import never mutates beyond the role's grants and never writes lifecycle rows."""

from unittest.mock import patch

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.urls import reverse

from assets.models import Asset, AssetType, Manufacturer, Supplier
from assets.models.catalog import AssetTypeFieldset
from assets.services.specifications.commands import create_asset_type
from core.context import set_current_all_accessible, set_current_tenant_group
from core.importers.bulk_forms import is_model_importable
from core.models import Job, ObjectChange
from inventory.models import Accessory
from itambox.views.generic import GenericObjectImportView
from organization.models import AssetHolder, Location, Site, Tenant, TenantGroup
from subscriptions.forms import SubscriptionBulkImportForm
from subscriptions.models import Subscription

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

    def test_subscription_import_cannot_set_a_foreign_tenant(self):
        foreign_tenant = Tenant.objects.create(name="Import Foreign", slug="import-foreign")
        supplier = Supplier.objects.create(name="Global Import Supplier", slug="global-import-supplier")
        adder = self.make_member("import-subscription-adder", {"subscriptions.add_subscription"})
        names = ["Numeric Tenant Import", "Slug Tenant Import", "Name Tenant Import"]
        tenant_values = [str(foreign_tenant.pk), foreign_tenant.slug, foreign_tenant.name]
        rows = [
            f"{name},{supplier.pk},EUR,{tenant_value}" for name, tenant_value in zip(names, tenant_values, strict=True)
        ]
        csv_text = "name,supplier,currency,tenant\n" + "\n".join(rows) + "\n"

        self._import(adder, "subscriptions", "subscription", csv_text)

        imported = list(Subscription._base_manager.filter(name__in=names))
        self.assertEqual(len(imported), len(names), "forged tenant cells should be ignored, not block valid rows")
        self.assertEqual({subscription.tenant_id for subscription in imported}, {self.tenant.pk})
        subscription_type = ContentType.objects.get_for_model(Subscription)
        self.assertFalse(
            ObjectChange._base_manager.filter(
                tenant=foreign_tenant,
                changed_object_type=subscription_type,
                changed_object_id__in=[subscription.pk for subscription in imported],
            ).exists(),
            "the import must not create subscription audit records in the forged tenant",
        )

    def test_subscription_import_field_docs_match_mapper_allowlist(self):
        adder = self.make_member("import-subscription-field-reader", {"subscriptions.add_subscription"})
        self.client_login_to_tenant(adder, self.tenant)
        import_url = reverse("generic_import", kwargs={"app_label": "subscriptions", "model_name": "subscription"})

        response = self.client.get(import_url)
        documented_fields = {field["name"] for field in response.context["fields"]}
        import_fields = set(SubscriptionBulkImportForm().field_names)
        self.assertNotIn("tenant", documented_fields)
        self.assertNotIn("tenant", import_fields)
        self.assertEqual(documented_fields, import_fields)

    def test_subscription_mapper_skips_excluded_fields_even_if_a_form_lists_them(self):
        class LegacyTenantSubscriptionImportForm(SubscriptionBulkImportForm):
            @property
            def field_names(self):
                return ["tenant"]

        form = LegacyTenantSubscriptionImportForm()
        self.assertEqual(form.map_row({"tenant": "forged-tenant"}), {})

    def _audit_count(self, *models):
        content_types = ContentType.objects.get_for_models(*models).values()
        return ObjectChange._base_manager.filter(changed_object_type__in=content_types).count()

    def _assert_reference_rejected(self, user, app_label, model_name, csv_text, target_model, *reference_models):
        rows_before = target_model._base_manager.count()
        audit_before = self._audit_count(target_model, *reference_models)

        _preview, response = self._import(user, app_label, model_name, csv_text)

        self.assertEqual(response.status_code, 302)
        job = Job.objects.latest("created")
        self.assertIn("Row 2:", job.logs)
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertEqual(target_model._base_manager.count(), rows_before)
        self.assertEqual(self._audit_count(target_model, *reference_models), audit_before)

    def test_import_rejects_foreign_supplier_ids_slugs_and_names(self):
        foreign_tenant = Tenant.objects.create(name="Private import tenant", slug="private-import-tenant")
        supplier = Supplier.objects.create(
            name="Private import supplier", slug="private-import-supplier", tenant=foreign_tenant
        )
        manufacturer = Manufacturer.objects.create(name="Import supplier manufacturer", slug="import-supplier-mfr")
        user = self.make_member("import-supplier-ref", {"inventory.add_accessory"})

        for index, value in enumerate((str(supplier.pk), supplier.slug, supplier.name), start=1):
            with self.subTest(reference=value):
                self._assert_reference_rejected(
                    user,
                    "inventory",
                    "accessory",
                    f"name,manufacturer,supplier\nPrivate reference {index},{manufacturer.name},{value}\n",
                    Accessory,
                    Supplier,
                )

    def test_import_rejects_foreign_site_ids_slugs_and_names(self):
        foreign_tenant = Tenant.objects.create(name="Private site tenant", slug="private-site-tenant")
        site = Site.objects.create(name="Private import site", slug="private-import-site", tenant=foreign_tenant)
        user = self.make_member("import-site-ref", {"organization.add_location"})

        for index, value in enumerate((str(site.pk), site.slug, site.name), start=1):
            with self.subTest(reference=value):
                self._assert_reference_rejected(
                    user,
                    "organization",
                    "location",
                    f"name,site\nPrivate location {index},{value}\n",
                    Location,
                    Site,
                )

    def test_import_rejects_foreign_location_parent_ids_slugs_and_names(self):
        foreign_tenant = Tenant.objects.create(name="Private parent tenant", slug="private-parent-tenant")
        foreign_site = Site.objects.create(
            name="Private parent site", slug="private-parent-site", tenant=foreign_tenant
        )
        parent = Location.objects.create(
            name="Private import parent", slug="private-import-parent", site=foreign_site, tenant=foreign_tenant
        )
        site = Site.objects.create(name="Import destination site", slug="import-destination-site", tenant=self.tenant)
        user = self.make_member("import-parent-ref", {"organization.add_location"})

        for index, value in enumerate((str(parent.pk), parent.slug, parent.name), start=1):
            with self.subTest(reference=value):
                self._assert_reference_rejected(
                    user,
                    "organization",
                    "location",
                    f"name,site,parent\nChild location {index},{site.pk},{value}\n",
                    Location,
                    Location,
                )

    def test_import_rejects_numeric_ids_of_soft_deleted_supplier_site_and_location(self):
        manufacturer = Manufacturer.objects.create(name="Deleted reference manufacturer", slug="deleted-ref-mfr")
        supplier = Supplier.objects.create(
            name="Deleted import supplier", slug="deleted-import-supplier", tenant=self.tenant
        )
        site = Site.objects.create(name="Deleted import site", slug="deleted-import-site", tenant=self.tenant)
        parent_site = Site.objects.create(name="Parent import site", slug="parent-import-site", tenant=self.tenant)
        parent = Location.objects.create(
            name="Deleted import parent", slug="deleted-import-parent", site=parent_site, tenant=self.tenant
        )
        supplier.delete()
        site.delete()
        parent.delete()
        user = self.make_member("import-deleted-ref", {"inventory.add_accessory", "organization.add_location"})

        self._assert_reference_rejected(
            user,
            "inventory",
            "accessory",
            f"name,manufacturer,supplier\nDeleted supplier ref,{manufacturer.name},{supplier.pk}\n",
            Accessory,
            Supplier,
        )
        self._assert_reference_rejected(
            user,
            "organization",
            "location",
            f"name,site\nDeleted site ref,{site.pk}\n",
            Location,
            Site,
        )
        self._assert_reference_rejected(
            user,
            "organization",
            "location",
            f"name,site,parent\nDeleted parent ref,{parent_site.pk},{parent.pk}\n",
            Location,
            Location,
        )

    def test_import_keeps_same_tenant_global_and_group_visible_suppliers(self):
        group = TenantGroup.objects.create(name="Import sharing group", slug="import-sharing-group")
        self.tenant.group = group
        self.tenant.save()
        suppliers = [
            Supplier.objects.create(name="Tenant import supplier", slug="tenant-import-supplier", tenant=self.tenant),
            Supplier.objects.create(name="Global import supplier", slug="global-import-supplier"),
            Supplier.objects.create(name="Group import supplier", slug="group-import-supplier", tenant_group=group),
        ]
        manufacturer = Manufacturer.objects.create(name="Shared import manufacturer", slug="shared-import-mfr")
        user = self.make_member("import-shared-ref", {"inventory.add_accessory"})
        names = ("Tenant supplier accessory", "Global supplier accessory", "Group supplier accessory")
        csv_text = "name,manufacturer,supplier\n" + "".join(
            f"{name},{manufacturer.name},{supplier_value}\n"
            for name, supplier_value in zip(
                names, (str(suppliers[0].pk), suppliers[1].slug, suppliers[2].name), strict=True
            )
        )

        _preview, response = self._import(user, "inventory", "accessory", csv_text)

        self.assertEqual(response.status_code, 302)
        job = Job.objects.latest("created")
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        imported = dict(Accessory._base_manager.filter(name__in=names).values_list("name", "supplier_id"))
        self.assertEqual(imported, dict(zip(names, (supplier.pk for supplier in suppliers), strict=True)))

    def test_import_keeps_same_tenant_site_and_parent_location_references(self):
        site = Site.objects.create(name="Visible import site", slug="visible-import-site", tenant=self.tenant)
        parent = Location.objects.create(
            name="Visible import parent", slug="visible-import-parent", site=site, tenant=self.tenant
        )
        user = self.make_member("import-visible-location-ref", {"organization.add_location"})

        _preview, response = self._import(
            user,
            "organization",
            "location",
            f"name,site,parent\nVisible import child,{site.pk},{parent.slug}\n",
        )

        self.assertEqual(response.status_code, 302)
        job = Job.objects.latest("created")
        self.assertEqual(job.status, Job.STATUS_COMPLETED, job.logs)
        child = Location._base_manager.get(name="Visible import child")
        self.assertEqual(child.site_id, site.pk)
        self.assertEqual(child.parent_id, parent.pk)
