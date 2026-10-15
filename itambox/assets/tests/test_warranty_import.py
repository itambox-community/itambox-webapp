from datetime import date

from django.test import TestCase

from assets.forms.import_forms import WarrantyBulkImportForm
from assets.models import Asset, StatusLabel, Supplier, Warranty
from core.context import set_current_tenant, set_current_user
from core.importers.bulk_forms import get_import_form_class, is_model_importable
from core.tests.mixins import TenantTestMixin
from organization.models import Tenant

WARRANTY_PERMS = ["assets.view_warranty", "assets.add_warranty", "assets.change_warranty"]
ASSET_PERMS = ["assets.view_asset", "assets.add_asset", "assets.change_asset"]
HEADER = "asset_tag,start_date,end_date,cost,currency,reference\n"


class WarrantyImportTestBase(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(permissions=ASSET_PERMS + WARRANTY_PERMS)
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
        set_current_tenant(None)

    def build_form(self, text):
        form = WarrantyBulkImportForm(
            {"import_text": text, "import_format": "csv", "delimiter": ",", "active_tab": "editor"}
        )
        form.actor = self.tenant_user
        return form

    def run_import(self, text):
        """Positive path: preview must validate, then the import runs."""
        form = self.build_form(text)
        self.assertTrue(form.is_valid(), form.errors)
        result = form.import_data()
        return form, result

    def preview_errors(self, text):
        """Negative path: preview rejects the file; returns the joined error text."""
        form = self.build_form(text)
        self.assertFalse(form.is_valid())
        return " ".join(form.errors.get("__all__", []))

    def warranty_count(self):
        return Warranty.all_objects.count()


class WarrantyImportFunctionalTests(WarrantyImportTestBase):
    def test_declaration_is_importable_and_form_registered(self):
        self.assertTrue(is_model_importable(Warranty))
        self.assertIs(get_import_form_class(Warranty), WarrantyBulkImportForm)

    def test_asset_resolves_by_tag_in_active_tenant_and_row_lands(self):
        form, result = self.run_import(HEADER + "TAG-A,2026-01-01,2027-01-01,99.00,EUR,CLM-1\n")
        self.assertEqual(result.imported_count, 1)
        warranty = Warranty.all_objects.get()
        self.assertEqual(warranty.asset_id, self.asset.pk)
        self.assertEqual(warranty.asset.tenant_id, self.tenant.pk)

    def test_foreign_tenant_tag_fails_the_row_and_creates_nothing(self):
        errors = self.preview_errors(HEADER + "TAG-F,2026-01-01,2027-01-01,,,\n")
        self.assertIn("asset_tag", errors)
        self.assertEqual(self.warranty_count(), 0)

    def test_end_before_start_fails_row_with_readable_message(self):
        errors = self.preview_errors(HEADER + "TAG-A,2027-01-01,2026-01-01,,,\n")
        self.assertIn("end_date must be on or after start_date", errors)
        self.assertEqual(self.warranty_count(), 0)

    def test_duplicate_row_in_same_file_is_skipped_once(self):
        text = HEADER + "TAG-A,2026-01-01,2027-01-01,,,\nTAG-A,2026-01-01,2027-01-01,,,\n"
        form, result = self.run_import(text)
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(self.warranty_count(), 1)
        self.assertEqual(len(form.skipped_list), 1)

    def test_reimport_reports_existing_row_as_present_and_does_not_update(self):
        self.run_import(HEADER + "TAG-A,2026-01-01,2027-01-01,99.00,EUR,CLM-1\n")
        form, result = self.run_import(HEADER + "TAG-A,2026-01-01,2027-01-01,500.00,USD,CLM-CHANGED\n")
        self.assertEqual(result.imported_count, 0)
        self.assertEqual(self.warranty_count(), 1)
        first = Warranty.all_objects.get()
        self.assertEqual(str(first.cost), "99.00")
        self.assertEqual(first.reference, "CLM-1")
        self.assertEqual(first.currency, "EUR")
        self.assertEqual(len(form.skipped_list), 1)

    def test_supplier_resolves_by_slug_and_numeric_id_is_rejected(self):
        supplier = Supplier.objects.create(name="CDW Deutschland", slug="cdw-deutschland", tenant=self.tenant)
        _, result = self.run_import(
            "asset_tag,start_date,end_date,supplier\nTAG-A,2026-01-01,2027-01-01,cdw-deutschland\n"
        )
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(Warranty.all_objects.get().supplier_id, supplier.pk)

        errors = self.preview_errors(
            f"asset_tag,start_date,end_date,supplier\nTAG-A,2026-02-01,2027-02-01,{supplier.pk}\n"
        )
        self.assertIn("supplier", errors)
        self.assertEqual(self.warranty_count(), 1)


class WarrantyImportTenantTests(WarrantyImportTestBase):
    def test_tenant_column_is_rejected_for_whole_file(self):
        text = "tenant,asset_tag,start_date,end_date\n" + f"{self.foreign_tenant.pk},TAG-A,2026-01-01,2027-01-01\n"
        errors = self.preview_errors(text)
        self.assertIn("tenant", errors.lower())
        self.assertEqual(self.warranty_count(), 0)
        self.assertFalse(Warranty.all_objects.filter(asset__tenant=self.foreign_tenant).exists())

    def test_same_tag_in_foreign_tenant_is_neither_matched_nor_updated(self):
        foreign_warranty = Warranty.all_objects.create(
            asset=self.foreign_asset,
            start_date=date(2026, 1, 1),
            end_date=date(2027, 1, 1),
            cost="1.00",
            currency="EUR",
        )
        self.run_import(HEADER + "TAG-A,2026-01-01,2027-01-01,99.00,EUR,CLM-1\n")
        foreign_warranty.refresh_from_db()
        self.assertEqual(str(foreign_warranty.cost), "1.00")
        self.assertEqual(foreign_warranty.asset_id, self.foreign_asset.pk)
        self.assertEqual(Warranty.all_objects.filter(asset=self.asset).count(), 1)

    def test_preview_errors_name_the_column(self):
        errors = self.preview_errors(HEADER + "TAG-F,2026-01-01,2027-01-01,,,\n")
        self.assertIn("asset_tag", errors)
