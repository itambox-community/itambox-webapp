"""#829 regression: curated bulk import for the Software and Component catalogues.

Both catalogues hold shared (tenantless) rows next to tenant-owned rows. The
import writes into the active tenant only, never creates, matches or changes a
shared entry, resolves references by natural key, and never writes stock.
"""

import pytest

from assets.models import Category, Manufacturer
from core.importers.bulk_forms import get_registered_import_form, is_model_importable
from core.models import Job
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin
from inventory.models import Component
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
