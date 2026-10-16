from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils.translation import override

from assets.archive_services import archive_asset
from assets.forms.import_forms import AssetMaintenanceBulkImportForm
from assets.models import Asset, AssetAssignment, AssetDisposal, AssetMaintenance, StatusLabel, Supplier
from core.context import set_current_user
from core.data_transfer import policy_for
from core.importers.bulk_forms import get_import_form_class, is_model_importable
from core.models import Job
from core.tests.mixins import TenantTestMixin
from itambox.views.generic import ObjectImportView
from organization.models import Tenant

HEADER = (
    "asset_tag,maintenance_type,status,start_date,completion_date,supplier,performed_by,cost,currency,description,notes"
)
IMPORT_PERMISSION = "assets.add_assetmaintenance"


class AssetMaintenanceImportTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(permissions=[IMPORT_PERMISSION])
        self.status = StatusLabel.objects.create(name="Ready", type=StatusLabel.TYPE_DEPLOYABLE)
        self.asset = Asset.objects.create(name="Laptop", asset_tag="TAG-A", status=self.status, tenant=self.tenant)
        self.foreign_tenant = Tenant.objects.create(name="Other", slug="other-tenant")
        self.foreign_asset = Asset.objects.create(
            name="Foreign", asset_tag="TAG-F", status=self.status, tenant=self.foreign_tenant
        )
        self.set_active_tenant(self.tenant)
        set_current_user(self.tenant_user)

    def tearDown(self):
        set_current_user(None)
        self.clear_tenant_context()

    @staticmethod
    def csv(*rows):
        return HEADER + "\n" + "\n".join(rows)

    def build_form(self, text):
        form = AssetMaintenanceBulkImportForm(
            {"import_text": text, "import_format": "csv", "delimiter": ",", "active_tab": "editor"}
        )
        form.actor = self.tenant_user
        return form

    def run_import(self, text):
        form = self.build_form(text)
        self.assertTrue(form.is_valid(), form.errors)
        result = form.import_data()
        return form, result

    def preview_errors(self, text):
        form = self.build_form(text)
        self.assertFalse(form.is_valid())
        return form, " ".join(form.errors.get("__all__", []))

    def test_asset_maintenance_has_a_curated_create_only_importer(self):
        self.assertTrue(policy_for(AssetMaintenance).import_)
        self.assertTrue(is_model_importable(AssetMaintenance))
        self.assertIs(get_import_form_class(AssetMaintenance), AssetMaintenanceBulkImportForm)
        self.assertEqual(AssetMaintenanceBulkImportForm.update_key, ())

    def test_import_reference_help_has_compiled_german_translations(self):
        with override("de"):
            self.assertEqual(
                str(AssetMaintenanceBulkImportForm.field_descriptions["asset_tag"]),
                "Exakte Inventarnummer eines nicht archivierten Assets im aktuellen Mandanten.",
            )
            self.assertEqual(
                str(AssetMaintenanceBulkImportForm.field_descriptions["supplier"]),
                "Geben Sie den Slug oder Namen eines für diesen Import verfügbaren Lieferanten ein; "
                "numerische IDs sind nicht zulässig.",
            )
            self.assertEqual(
                str(AssetMaintenanceBulkImportForm.field_descriptions["completion_date"]),
                "Bei abgebrochenen Wartungen optional; bei abgeschlossenen Wartungen erforderlich. Datumsformat: JJJJ-MM-TT.",
            )
            self.assertEqual(
                str(AssetMaintenanceBulkImportForm.field_descriptions["status"]),
                "Nur abgeschlossene oder abgebrochene Wartungen können importiert werden.",
            )

    def test_field_options_only_show_importable_maintenance_statuses(self):
        view = ObjectImportView()
        view.model_form = AssetMaintenanceBulkImportForm
        fields = {field["name"]: field for field in view._get_fields_info()}

        self.assertEqual([value for value, _label in fields["status"]["choices"]], ["completed", "cancelled"])
        self.assertIn(
            "Only completed or cancelled maintenance records can be imported.", fields["status"]["description"]
        )
        self.assertIn(
            "Optional for cancelled maintenance; required when status is completed. Format: YYYY-MM-DD.",
            fields["completion_date"]["description"],
        )

    def test_asset_reference_error_names_the_tag_and_uses_the_german_catalog(self):
        with override("de"):
            _form, errors = self.preview_errors(self.csv("TAG-F,repair,completed,2026-01-01,2026-01-02,,,,,,"))
        self.assertIn(
            "asset_tag: Im aktuellen Mandanten wurde kein nicht archiviertes Asset mit der Inventarnummer 'TAG-F' gefunden.",
            errors,
        )

    def test_archived_asset_reference_error_explains_why_it_is_ineligible(self):
        archive_asset(self.asset)
        _form, errors = self.preview_errors(self.csv("TAG-A,repair,completed,2026-01-01,2026-01-02,,,,,,"))
        self.assertIn(
            "Row 2: asset_tag: no non-archived asset with tag 'TAG-A' exists in this tenant.",
            errors,
        )

    def test_german_preview_errors_translate_row_prefix_and_date_order(self):
        with override("de"):
            _form, errors = self.preview_errors(self.csv("TAG-A,repair,completed,2026-02-01,2026-01-31,,,,,,"))
        self.assertIn("Zeile 2: completion_date darf nicht vor start_date liegen.", errors)

    def test_completed_and_cancelled_history_import_without_links_and_remains_archivable(self):
        _form, result = self.run_import(
            self.csv(
                "TAG-A,repair,completed,2026-01-01,2026-01-03,,,,,Repair completed,Imported history",
                "TAG-A,calibration,cancelled,2026-02-01,,,,,,,Cancelled history",
            )
        )
        self.assertEqual(result.imported_count, 2)
        maintenance_rows = list(AssetMaintenance.all_objects.filter(asset=self.asset).order_by("start_date"))
        self.assertEqual([row.status for row in maintenance_rows], ["completed", "cancelled"])
        self.assertEqual(maintenance_rows[0].completion_date, date(2026, 1, 3))
        self.assertIsNone(maintenance_rows[1].completion_date)
        self.assertFalse(AssetAssignment._base_manager.filter(maintenance__in=maintenance_rows).exists())
        self.assertFalse(AssetDisposal._base_manager.filter(maintenance__in=maintenance_rows).exists())

        archive_asset(self.asset)
        archived_asset = Asset.all_objects.get(pk=self.asset.pk)
        self.assertIsNotNone(archived_asset.deleted_at)
        archived_history = AssetMaintenance.all_objects.filter(asset=archived_asset).order_by("start_date")
        self.assertEqual(archived_history.count(), 2)
        self.assertTrue(all(row.deleted_at == archived_asset.deleted_at for row in archived_history))

    def test_scheduled_and_in_progress_rows_fail_preview_and_import(self):
        for status in ("scheduled", "in_progress"):
            with self.subTest(status=status):
                form, errors = self.preview_errors(self.csv(f"TAG-A,repair,{status},2026-01-01,,,,,,,"))
                self.assertIn("status:", errors)
                self.assertIn("only completed or cancelled maintenance can be imported", errors)
                result = form.import_data()
                self.assertEqual(result.imported_count, 0)
                self.assertIn("Row 2: status:", form.errors_list[0])
        self.assertEqual(AssetMaintenance.all_objects.count(), 0)

    def test_completed_without_completion_date_fails_with_column_name(self):
        form, errors = self.preview_errors(self.csv("TAG-A,repair,completed,2026-01-01,,,,,,,"))
        self.assertIn("completion_date:", errors)
        self.assertIn("requires a completion date", errors)
        self.assertEqual(form.import_data().imported_count, 0)
        self.assertIn("Row 2: completion_date:", form.errors_list[0])
        self.assertEqual(AssetMaintenance.all_objects.count(), 0)

    def test_completion_before_start_fails_with_column_name(self):
        form, errors = self.preview_errors(self.csv("TAG-A,repair,completed,2026-02-01,2026-01-31,,,,,,"))
        self.assertIn("completion_date", errors)
        self.assertIn("on or after start_date", errors)
        self.assertEqual(form.import_data().imported_count, 0)
        self.assertIn("Row 2: completion_date", form.errors_list[0])
        self.assertEqual(AssetMaintenance.all_objects.count(), 0)

    def test_preview_reports_multiple_invalid_rows_with_row_and_column(self):
        _form, errors = self.preview_errors(
            self.csv(
                "TAG-F,repair,completed,2026-01-01,2026-01-02,,,,,,",
                "TAG-A,repair,scheduled,2026-01-01,,,,,,,",
            )
        )
        self.assertIn("Row 2:", errors)
        self.assertIn("asset_tag:", errors)
        self.assertIn("TAG-F", errors)
        self.assertIn("Row 3:", errors)
        self.assertIn("status:", errors)
        self.assertNotIn("Foreign", errors)

    def test_id_is_ignored_and_duplicate_reimport_never_updates(self):
        existing = AssetMaintenance.all_objects.create(
            asset=self.asset,
            maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            status="completed",
            start_date=date(2025, 1, 1),
            completion_date=date(2025, 1, 2),
            performed_by="Original record",
            cost="10.00",
            currency="EUR",
        )
        text = (
            "id," + HEADER + "\n"
            f"{existing.pk},TAG-A,repair,completed,2026-01-01,2026-01-03,,,99.00,EUR,New history,First import"
        )
        form, result = self.run_import(text)
        self.assertEqual(result.imported_count, 1)
        created = AssetMaintenance.all_objects.get(asset=self.asset, start_date=date(2026, 1, 1))
        self.assertNotEqual(created.pk, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.performed_by, "Original record")
        self.assertEqual(str(existing.cost), "10.00")

        reimport, result = self.run_import(
            "id," + HEADER + "\n"
            f"{existing.pk},TAG-A,repair,cancelled,2026-01-01,,,,500.00,USD,Changed history,Changed notes"
        )
        self.assertEqual(result.imported_count, 0)
        self.assertEqual(len(reimport.skipped_list), 1)
        created.refresh_from_db()
        self.assertEqual(created.status, "completed")
        self.assertEqual(created.completion_date, date(2026, 1, 3))
        self.assertEqual(str(created.cost), "99.00")
        self.assertEqual(created.description, "New history")

    def test_duplicate_key_in_one_file_is_created_once(self):
        row = "TAG-A,repair,completed,2026-03-01,2026-03-02,,,10.00,EUR,Original,"
        form, result = self.run_import(self.csv(row, row.replace("Original", "Changed")))
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(AssetMaintenance.all_objects.filter(asset=self.asset).count(), 1)
        self.assertEqual(len(form.skipped_list), 1)
        self.assertEqual(AssetMaintenance.all_objects.get(asset=self.asset).description, "Original")

    def test_asset_tag_resolves_inside_active_tenant_and_rejects_foreign_only_tag(self):
        foreign_form, errors = self.preview_errors(self.csv("TAG-F,repair,completed,2026-01-01,2026-01-02,,,,,,"))
        self.assertIn("asset_tag:", errors)
        self.assertNotIn("Foreign", errors)
        self.assertEqual(foreign_form.import_data().imported_count, 0)
        self.assertFalse(AssetMaintenance.all_objects.exists())

    def test_foreign_tenant_same_tag_and_natural_key_are_not_matched_or_changed(self):
        foreign_asset = Asset.objects.create(
            name="Other Laptop", asset_tag="TAG-A", status=self.status, tenant=self.foreign_tenant
        )
        foreign_row = AssetMaintenance.all_objects.create(
            asset=foreign_asset,
            maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            status="completed",
            start_date=date(2026, 1, 1),
            completion_date=date(2026, 1, 2),
            performed_by="Foreign original",
            cost="1.00",
            currency="EUR",
        )
        _form, result = self.run_import(
            self.csv("TAG-A,repair,completed,2026-01-01,2026-01-03,,,99.00,EUR,Local history,")
        )
        self.assertEqual(result.imported_count, 1)
        local_row = AssetMaintenance.all_objects.get(asset=self.asset)
        self.assertEqual(local_row.completion_date, date(2026, 1, 3))
        foreign_row.refresh_from_db()
        self.assertEqual(foreign_row.asset_id, foreign_asset.pk)
        self.assertEqual(foreign_row.performed_by, "Foreign original")
        self.assertEqual(str(foreign_row.cost), "1.00")

    def test_tenant_column_rejects_the_whole_file_without_foreign_write(self):
        text = "tenant," + HEADER + f"\n{self.foreign_tenant.pk},TAG-F,repair,completed,2026-01-01,2026-01-02,,,,,,"
        _form, errors = self.preview_errors(text)
        self.assertIn("tenant", errors.lower())
        self.assertFalse(AssetMaintenance.all_objects.filter(asset__tenant=self.foreign_tenant).exists())

    def test_supplier_resolves_by_scoped_name_or_slug_but_never_numeric_id(self):
        supplier = Supplier.objects.create(name="Northstar Service", slug="northstar-service", tenant=self.tenant)
        _form, result = self.run_import(self.csv("TAG-A,repair,completed,2026-04-01,2026-04-02,northstar-service,,,,,"))
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(AssetMaintenance.all_objects.get(asset=self.asset).supplier_id, supplier.pk)

        _form, result = self.run_import(self.csv("TAG-A,repair,completed,2026-05-01,2026-05-02,NORTHSTAR-SERVICE,,,,,"))
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(
            AssetMaintenance.all_objects.get(asset=self.asset, start_date=date(2026, 5, 1)).supplier_id, supplier.pk
        )

        _form, result = self.run_import(self.csv("TAG-A,repair,completed,2026-05-03,2026-05-04,Northstar Service,,,,,"))
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(
            AssetMaintenance.all_objects.get(asset=self.asset, start_date=date(2026, 5, 3)).supplier_id, supplier.pk
        )

        _form, errors = self.preview_errors(
            self.csv(f"TAG-A,repair,completed,2026-06-01,2026-06-02,{supplier.pk},,,,,")
        )
        self.assertIn("supplier:", errors)
        self.assertNotIn("select a valid choice", errors)

    def test_ambiguous_or_foreign_supplier_reference_fails_without_disclosure(self):
        Supplier.objects.create(name="Shared Vendor", slug="tenant-shared-vendor", tenant=self.tenant)
        Supplier.objects.create(name="Shared Vendor", slug="global-shared-vendor")
        ambiguous_form, ambiguous_errors = self.preview_errors(
            self.csv("TAG-A,repair,completed,2026-07-01,2026-07-02,Shared Vendor,,,,,")
        )
        self.assertIn("matches more than one supplier", ambiguous_errors)
        self.assertEqual(ambiguous_form.import_data().imported_count, 0)

        Supplier.objects.create(name="Foreign Vendor", slug="foreign-vendor", tenant=self.foreign_tenant)
        _form, foreign_errors = self.preview_errors(
            self.csv("TAG-A,repair,completed,2026-08-01,2026-08-02,foreign-vendor,,,,,")
        )
        self.assertIn("supplier:", foreign_errors)
        self.assertIn("foreign-vendor", foreign_errors)
        self.assertNotIn("Foreign Vendor", foreign_errors)
        self.assertEqual(AssetMaintenance.all_objects.count(), 0)
        with override("de"):
            _form, german_errors = self.preview_errors(
                self.csv("TAG-A,repair,completed,2026-08-01,2026-08-02,foreign-vendor,,,,,")
            )
        self.assertIn(
            "supplier: Für diesen Import ist kein Lieferant mit dem Slug oder Namen 'foreign-vendor' verfügbar.",
            german_errors,
        )

    def test_authenticated_member_can_preview_and_import_in_concrete_tenant(self):
        self.tenant_role.permissions = [IMPORT_PERMISSION, "assets.change_assetmaintenance"]
        self.tenant_role.save(update_fields=["permissions"])
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "assetmaintenance"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exact asset tag of a non-archived asset in the current tenant.")
        self.assertContains(
            response, "Optional for cancelled maintenance; required when status is completed. Format: YYYY-MM-DD."
        )
        self.assertContains(response, "Only completed or cancelled maintenance records can be imported.")
        self.assertContains(response, "slug or name from those available for this import")
        self.assertContains(response, '<samp class="text-secondary">completed</samp>', html=True)
        self.assertContains(response, '<samp class="text-secondary">cancelled</samp>', html=True)
        self.assertNotContains(response, '<samp class="text-secondary">scheduled</samp>', html=True)
        self.assertNotContains(response, '<samp class="text-secondary">in_progress</samp>', html=True)
        self.assertNotContains(response, "Related objects (ForeignKeys) can be matched by ID")

        tenant_preview = self.client.post(
            url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": (
                    "tenant," + HEADER + "\n"
                    f"{self.foreign_tenant.pk},TAG-A,repair,completed,2026-09-01,2026-09-02,,,,,,"
                ),
                "_preview": "1",
            },
        )
        self.assertEqual(tenant_preview.status_code, 200)
        self.assertContains(tenant_preview, "tenant")
        self.assertNotContains(tenant_preview, "Confirm Import")
        rejected_confirm = self.client.post(url, {"_confirm": "1"})
        self.assertEqual(rejected_confirm.status_code, 200)
        self.assertFalse(Job.objects.exists())
        self.assertFalse(AssetMaintenance.all_objects.exists())

        invalid_preview = self.client.post(
            url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": self.csv(
                    "TAG-F,repair,completed,2026-08-01,2026-08-02,,,,,,",
                    "TAG-A,repair,scheduled,2026-08-01,,,,,,,",
                ),
                "_preview": "1",
            },
        )
        self.assertEqual(invalid_preview.status_code, 200)
        self.assertContains(invalid_preview, "Row 2:")
        self.assertContains(invalid_preview, "asset_tag:")
        self.assertContains(invalid_preview, "Row 3:")
        self.assertContains(invalid_preview, "status:")
        self.assertNotContains(invalid_preview, "Foreign")
        self.assertFalse(AssetMaintenance.all_objects.exists())

        import_text = self.csv("TAG-A,repair,completed,2026-09-01,2026-09-02,,,,,,")
        preview = self.client.post(
            url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": import_text,
                "_preview": "1",
            },
        )
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "TAG-A")
        confirmed = self.client.post(url, {"_confirm": "1"})
        self.assertEqual(confirmed.status_code, 302)
        created = AssetMaintenance.all_objects.get(asset=self.asset)
        self.assertEqual(created.status, "completed")
        job = Job.objects.get()
        self.assertEqual(job.tenant_id, self.tenant.pk)
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertEqual(job.result, {"imported": 1, "failed": 0, "total": 1})

        changed_duplicate = self.csv("TAG-A,repair,cancelled,2026-09-01,,,,,,Changed history,")
        duplicate_preview = self.client.post(
            url,
            {
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": changed_duplicate,
                "_preview": "1",
            },
        )
        self.assertEqual(duplicate_preview.status_code, 200)
        self.assertContains(duplicate_preview, "Row 2: record already exists; skipped.")
        self.client.post(url, {"_confirm": "1"})
        jobs = list(Job.objects.order_by("pk"))
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[1].result, {"imported": 0, "failed": 0, "total": 1, "skipped": 1})
        self.assertIn("Row 2: record already exists; skipped.", jobs[1].logs)
        self.assertEqual(AssetMaintenance.all_objects.filter(asset=self.asset).count(), 1)
        created.refresh_from_db()
        self.assertEqual(created.status, "completed")
        self.assertEqual(created.completion_date, date(2026, 9, 2))

    def test_all_accessible_import_view_is_forbidden(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()
        url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "assetmaintenance"})
        response = self.client.get(url)
        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(AssetMaintenance.all_objects.exists())
