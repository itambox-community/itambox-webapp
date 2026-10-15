"""#829 regression: curated bulk import for the Software and Component catalogues.

Both catalogues hold shared (tenantless) rows next to tenant-owned rows. The
import writes into the active tenant only, never creates, matches or changes a
shared entry, resolves references by natural key, and never writes stock.
"""

import json

import pytest
from django.test import RequestFactory

from assets.models import Category, Manufacturer
from core.context import set_current_all_accessible
from core.importers.bulk_forms import get_registered_import_form, is_model_importable
from core.models import Job
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin
from inventory.models import Component, ComponentAllocation, ComponentStock
from itambox.views.generic.import_ import GenericObjectImportView
from organization.models import Tenant
from software.models import Software

PERMISSIONS = [
    "software.add_software",
    "software.change_software",
    "inventory.add_component",
    "inventory.change_component",
]


@pytest.mark.django_db
class TestHybridCatalogueImport(TenantTestMixin):
    def _setup(self, permissions=PERMISSIONS):
        self.setup_tenant_context(name="Hybrid Tenant", slug="hybrid-tenant", permissions=list(permissions))
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.maker = Manufacturer.objects.create(name="Acme", slug="acme")

    def _run(self, app_label, model_name, rows):
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)
        import_csv_task(
            job_id=job.pk,
            rows_data=rows,
            app_label=app_label,
            model_name=model_name,
            user_id=self.tenant_user.pk,
            tenant_id=self.tenant.pk,
        )
        job.refresh_from_db()
        return job

    def test_models_are_importable_with_registered_forms(self):
        for model in (Software, Component):
            assert is_model_importable(model)
            assert get_registered_import_form(model) is not None

    def test_software_row_lands_in_the_active_tenant(self):
        self._setup()
        job = self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "version": "1"}])
        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        software = Software.objects.get(name="Editor")
        assert software.tenant_id == self.tenant.pk
        assert software.manufacturer_id == self.maker.pk

    def test_software_reimport_updates_the_tenant_row(self):
        self._setup()
        self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "version": "1"}])
        self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "version": "2"}])
        assert Software.objects.filter(name="Editor").count() == 1
        assert Software.objects.get(name="Editor").version == "2"

    def test_software_key_equal_to_shared_entry_fails_and_leaves_it_untouched(self):
        self._setup()
        shared = Software.objects.create(name="Shared Suite", manufacturer=self.maker, version="9", tenant=None)
        job = self._run("software", "software", [{"name": "Shared Suite", "manufacturer": "Acme", "version": "1"}])
        assert job.status == Job.STATUS_FAILED
        assert Software.objects.filter(name="Shared Suite").count() == 1
        shared.refresh_from_db()
        assert shared.version == "9"
        assert shared.tenant_id is None

    def test_unknown_manufacturer_fails_and_is_not_created(self):
        self._setup()
        job = self._run("software", "software", [{"name": "Editor", "manufacturer": "Nobody"}])
        assert job.status == Job.STATUS_FAILED
        assert not Manufacturer.objects.filter(name="Nobody").exists()
        assert not Software.objects.filter(name="Editor").exists()

    def test_numeric_id_is_not_a_reference(self):
        self._setup()
        job = self._run("software", "software", [{"name": "Editor", "manufacturer": str(self.maker.pk)}])
        assert job.status == Job.STATUS_FAILED
        assert not Software.objects.filter(name="Editor").exists()

    def test_tenant_column_is_rejected_for_the_whole_file(self):
        self._setup()
        job = self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "tenant": "other"}])
        assert job.status == Job.STATUS_FAILED
        assert not Software.objects.filter(name="Editor").exists()

    def test_without_add_permission_nothing_is_written(self):
        self._setup(permissions=["software.view_software"])
        job = self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme"}])
        assert job.status == Job.STATUS_FAILED
        assert not Software.objects.filter(name="Editor").exists()

    def test_component_row_lands_in_the_active_tenant_with_a_component_category(self):
        self._setup()
        Category.objects.create(name="Memory", slug="memory", applies_to={"component": True})
        job = self._run(
            "inventory",
            "component",
            [
                {
                    "name": "RAM 16",
                    "manufacturer": "Acme",
                    "category": "Memory",
                    "min_qty": "2",
                    "allow_overallocate": "yes",
                }
            ],
        )
        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        component = Component.objects.get(name="RAM 16")
        assert component.tenant_id == self.tenant.pk
        assert component.min_qty == 2
        assert component.allow_overallocate is True

    def test_component_category_of_another_kind_is_rejected(self):
        self._setup()
        Category.objects.create(name="Mice", slug="mice", applies_to={"accessory": True})
        job = self._run("inventory", "component", [{"name": "RAM 16", "manufacturer": "Acme", "category": "Mice"}])
        assert job.status == Job.STATUS_FAILED
        assert not Component.objects.filter(name="RAM 16").exists()

    def test_component_stock_columns_are_rejected(self):
        self._setup()
        for column in ("qty", "quantity", "location", "stock"):
            job = self._run("inventory", "component", [{"name": "RAM 16", "manufacturer": "Acme", column: "5"}])
            assert job.status == Job.STATUS_FAILED, column
        assert not Component.objects.filter(name="RAM 16").exists()

    def test_component_key_equal_to_shared_entry_fails(self):
        self._setup()
        Component.all_objects.create(name="Shared RAM", manufacturer=self.maker, tenant=None)
        job = self._run("inventory", "component", [{"name": "Shared RAM", "manufacturer": "Acme"}])
        assert job.status == Job.STATUS_FAILED
        assert Component.all_objects.filter(name="Shared RAM").count() == 1

    def test_invalid_boolean_fails_the_row(self):
        self._setup()
        job = self._run(
            "inventory", "component", [{"name": "RAM 16", "manufacturer": "Acme", "allow_overallocate": "maybe"}]
        )
        assert job.status == Job.STATUS_FAILED

    def test_component_slug_column_is_ignored_and_stock_rows_are_never_written(self):
        self._setup()
        job = self._run(
            "inventory", "component", [{"name": "RAM 16", "manufacturer": "Acme", "slug": "custom-from-file"}]
        )
        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert Component.objects.get(name="RAM 16").slug != "custom-from-file"
        assert ComponentStock.objects.count() == 0
        assert ComponentAllocation.objects.count() == 0

    def test_rejected_stock_file_writes_no_stock_or_allocation_rows(self):
        self._setup()
        self._run("inventory", "component", [{"name": "RAM 16", "manufacturer": "Acme", "qty": "5"}])
        assert ComponentStock.objects.count() == 0
        assert ComponentAllocation.objects.count() == 0

    def test_reimport_of_an_unchanged_row_is_reported_skipped(self):
        self._setup()
        row = {"name": "Editor", "manufacturer": "Acme", "version": "1"}
        self._run("software", "software", [row])
        second = self._run("software", "software", [row])
        assert second.result == {"imported": 0, "failed": 0, "total": 1, "skipped": 1}
        assert Software.objects.filter(name="Editor").count() == 1

    def test_matched_row_without_change_permission_is_skipped_and_unchanged(self):
        self._setup(permissions=["software.add_software", "inventory.add_component"])
        self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "version": "1"}])
        job = self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "version": "2"}])
        assert job.result["skipped"] == 1
        assert job.result["imported"] == 0
        assert Software.objects.get(name="Editor").version == "1"

    def test_another_tenants_same_key_row_is_neither_matched_nor_updated(self):
        self._setup()
        other = Tenant.objects.create(name="Other Tenant", slug="other-tenant")
        foreign = Software.objects.create(name="Editor", manufacturer=self.maker, version="7", tenant=other)
        foreign_component = Component.all_objects.create(name="RAM 16", manufacturer=self.maker, tenant=other)
        self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "version": "1"}])
        self._run("inventory", "component", [{"name": "RAM 16", "manufacturer": "Acme", "notes": "mine"}])
        foreign.refresh_from_db()
        foreign_component.refresh_from_db()
        assert foreign.version == "7"
        assert foreign_component.notes == ""
        assert Software.objects.filter(name="Editor", tenant=self.tenant).count() == 1
        assert Component.objects.filter(name="RAM 16", tenant=self.tenant).count() == 1

    def test_tenant_column_negative_control_lands_no_row_in_a_foreign_tenant(self):
        self._setup()
        other = Tenant.objects.create(name="Foreign", slug="foreign")
        self._run("software", "software", [{"name": "Editor", "manufacturer": "Acme", "tenant": other.slug}])
        assert not Software.all_objects.filter(name="Editor").exists()

    def _preview(self, model_name, rows, permissions=None):
        model = Software if model_name == "software" else Component
        form = get_registered_import_form(model)(
            data={"import_format": "yaml", "active_tab": "editor", "import_text": json.dumps(rows)}
        )
        form.actor = self.tenant_user
        return form

    def test_preview_accepts_valid_rows_without_writing_anything(self):
        self._setup()
        form = self._preview("software", [{"name": "Editor", "manufacturer": "Acme"}])
        assert form.is_valid(), form.errors
        assert not Software.all_objects.filter(name="Editor").exists()

    def test_preview_errors_are_per_row_and_name_the_column(self):
        self._setup()
        form = self._preview(
            "software",
            [{"name": "Good", "manufacturer": "Acme"}, {"name": "Bad", "manufacturer": "Nobody"}],
        )
        assert not form.is_valid()
        message = " | ".join(form.errors["__all__"])
        assert "Row 3" in message and "manufacturer" in message
        assert "Row 2" not in message

    def test_preview_rejects_the_tenant_column_for_the_file(self):
        self._setup()
        form = self._preview("software", [{"name": "Editor", "manufacturer": "Acme", "tenant": "x"}])
        assert not form.is_valid()
        assert "tenant column" in " ".join(form.errors["__all__"])

    def test_preview_refuses_a_shared_entry(self):
        self._setup()
        Software.objects.create(name="Shared Suite", manufacturer=self.maker, tenant=None)
        form = self._preview("software", [{"name": "Shared Suite", "manufacturer": "Acme"}])
        assert not form.is_valid()
        assert "shared entries are not changed by an import" in " ".join(form.errors["__all__"])

    def test_preview_reports_a_row_without_change_permission_as_a_note(self):
        self._setup(permissions=["software.add_software"])
        Software.objects.create(name="Editor", manufacturer=self.maker, version="1", tenant=self.tenant)
        form = self._preview("software", [{"name": "Editor", "manufacturer": "Acme", "version": "2"}])
        assert form.is_valid(), form.errors
        assert any("do not have permission" in note for note in form.row_notes)
        assert Software.objects.get(name="Editor").version == "1"

    def test_ambiguous_reference_fails_the_row(self):
        self._setup()
        Manufacturer.objects.create(name="Twin Corp", slug="twin-a")
        Manufacturer.objects.create(name="TWIN CORP", slug="twin-b")
        form = self._preview("software", [{"name": "Editor", "manufacturer": "twin corp"}])
        assert not form.is_valid()
        assert "more than one" in " ".join(form.errors["__all__"])

    def test_preview_requires_an_active_tenant(self):
        self._setup()
        form = self._preview("software", [{"name": "Editor", "manufacturer": "Acme"}])
        self.set_active_tenant(None, None)
        assert not form.is_valid()
        assert "active tenant" in " ".join(form.errors["__all__"])

    def test_import_data_rejects_a_file_with_a_tenant_column(self):
        self._setup()
        form = self._preview("software", [{"name": "Editor", "manufacturer": "Acme", "tenant": "x"}])
        form._rows_data = [{"name": "Editor", "manufacturer": "Acme", "tenant": "x"}]
        result = form.import_data()
        assert result.imported_count == 0 and result.errors

    def test_import_view_is_denied_in_the_all_accessible_scope(self):
        self._setup()
        request = RequestFactory().get("/import/software/software/")
        request.user = self.tenant_user
        request.active_tenant = self.tenant
        view = GenericObjectImportView()
        view.request = request
        view.kwargs = {"app_label": "software", "model_name": "software"}
        try:
            assert view.has_permission()
            set_current_all_accessible(True)
            assert not view.has_permission()
        finally:
            set_current_all_accessible(False)
