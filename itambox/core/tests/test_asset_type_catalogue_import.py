"""#770: asset type import runs through the specification commands.

Rows are the community catalogue shape (YAML mapping ``specs``), validated by the
generated catalogue schema, created via ``create_asset_type`` and skipped when the
Type already exists. The actor holds only role grants (no superuser).
"""

import io
import json

import pytest
from django.core.exceptions import ValidationError

from assets.forms.import_forms import AssetTypeBulkImportForm, _spec_patch_values
from assets.models import AssetType, Category, Manufacturer
from core.management.commands.seed_data import Command as SeedDataCommand
from core.models import Job
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin

PERMISSIONS = ("assets.add_assettype", "assets.add_manufacturer")

pytestmark = pytest.mark.django_db(transaction=True)


class TestAssetTypeCatalogueImport(TenantTestMixin):
    def _context(self, permissions=PERMISSIONS):
        self.setup_tenant_context(name="Cat Tenant", slug="cat-tenant", permissions=list(permissions))
        self.tenant.is_provider = True
        self.tenant.save(update_fields=["is_provider"])
        self.set_active_tenant(self.tenant, self.tenant_membership)
        SeedDataCommand(stdout=io.StringIO(), stderr=io.StringIO())._seed_catalog()

    def _run(self, rows):
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)
        import_csv_task(
            job_id=job.pk,
            rows_data=rows,
            app_label="assets",
            model_name="assettype",
            user_id=self.tenant_user.pk,
            tenant_id=self.tenant.pk,
        )
        job.refresh_from_db()
        return job

    def _preview(self, rows):
        form = AssetTypeBulkImportForm(
            data={"import_format": "yaml", "active_tab": "editor", "import_text": json.dumps(rows)}
        )
        form.actor = self.tenant_user
        return form

    @staticmethod
    def _errors(form):
        assert not form.is_valid()
        return " | ".join(form.errors["__all__"])

    def test_specs_land_on_the_type_and_row_imports(self):
        self._context()
        Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        category = Category.objects.filter(slug="laptops").first()
        job = self._run(
            [
                {
                    "manufacturer": "Lenovo",
                    "model": "ThinkPad T14",
                    "category": category.slug if category else "",
                    "specs": {"form_factor": "notebook", "core_count": 8, "battery_capacity": "57 Wh"},
                }
            ]
        )
        assert job.result["imported"] == 1, job.logs
        asset_type = AssetType.objects.get(model="ThinkPad T14")
        assert asset_type.custom_field_data["form_factor"] == "notebook"
        assert asset_type.custom_field_data["core_count"] == 8
        assert str(asset_type.custom_field_data["battery_capacity"]) in {"57", "57.0", "57.000"}

    def test_existing_type_is_skipped_and_reported(self):
        self._context()
        manufacturer = Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        rows = [{"manufacturer": "Lenovo", "model": "ThinkPad T14"}]
        first = self._run(rows)
        assert first.result["imported"] == 1, first.logs
        second = self._run(rows)
        assert second.result["skipped"] == 1
        assert second.result["imported"] == 0
        assert "already exists" in second.logs
        assert AssetType.objects.filter(manufacturer=manufacturer, model="ThinkPad T14").count() == 1

    def test_unknown_category_is_an_error_and_creates_nothing(self):
        self._context()
        Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        job = self._run([{"manufacturer": "Lenovo", "model": "X1", "category": "no-such-category"}])
        assert job.status == Job.STATUS_FAILED
        assert "category" in job.logs.lower()
        assert not AssetType.objects.filter(model="X1").exists()
        assert not Category.objects.filter(slug="no-such-category").exists()

    def test_missing_manufacturer_is_created_for_an_authorised_actor(self):
        self._context()
        job = self._run([{"manufacturer": "Newco", "model": "N1"}])
        assert job.result["imported"] == 1, job.logs
        assert AssetType.objects.get(model="N1").manufacturer.name == "Newco"

    def test_missing_manufacturer_requires_permission(self):
        self._context(permissions=("assets.add_assettype",))
        job = self._run([{"manufacturer": "Newco", "model": "N1"}])
        assert job.status == Job.STATUS_FAILED
        assert not Manufacturer.objects.filter(name="Newco").exists()
        assert not AssetType.objects.filter(model="N1").exists()

    def test_preview_rejects_invalid_rows_before_any_job(self):
        self._context()
        Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        for specs in ({"flux_capacitor": "1"}, {"battery_capacity": "57 kWh"}, {"battery_capacity": "57"}):
            form = self._preview([{"manufacturer": "Lenovo", "model": "Bad", "specs": specs}])
            assert "Row 2" in self._errors(form)
        form = self._preview([{"manufacturer": "Lenovo", "model": "Bad", "category": "nope"}])
        assert "Row 2" in self._errors(form)
        assert not AssetType.objects.filter(model="Bad").exists()

    def test_preview_reports_skips_and_manufacturer_creation(self):
        self._context()
        Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        self._run([{"manufacturer": "Lenovo", "model": "ThinkPad T14"}])
        form = self._preview(
            [
                {"manufacturer": "Lenovo", "model": "ThinkPad T14"},
                {"manufacturer": "Newco", "model": "N1"},
            ]
        )
        assert form.is_valid(), form.errors
        assert any("already exists" in note for note in form.row_notes)
        assert any("will be created" in note for note in form.row_notes)
        assert not Manufacturer.objects.filter(name="Newco").exists()

    def test_preview_requires_manufacturer_permission(self):
        self._context(permissions=("assets.add_assettype",))
        form = self._preview([{"manufacturer": "Newco", "model": "N1"}])
        assert "permission" in self._errors(form)


class TestSpecsValidation:
    def test_unknown_spec_key_is_rejected(self):
        with pytest.raises(ValidationError):
            _spec_patch_values({"flux_capacitor": "1"})

    def test_quantity_suffix_is_stripped_to_the_codec_value(self):
        assert _spec_patch_values({"battery_capacity": "50.5 Wh"})["battery_capacity"] == "50.5"

    def test_wrong_unit_is_rejected(self):
        with pytest.raises(ValidationError):
            _spec_patch_values({"battery_capacity": "50 kWh"})

    def test_specs_must_be_a_mapping(self):
        with pytest.raises(ValidationError):
            _spec_patch_values(["form_factor"])

    def test_form_declares_specs_as_structured(self):
        assert "specs" in AssetTypeBulkImportForm.structured_fields
