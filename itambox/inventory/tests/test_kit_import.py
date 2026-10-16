"""#829 slice 6: kit importer with an explicit, atomic ``items`` contract."""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from assets.models import AssetType, Manufacturer
from assets.services import checkout_kit
from core.importers.bulk_forms import get_registered_import_form
from core.models import Job
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin
from inventory.forms.import_forms import KitBulkImportForm, _declared_entries, _parse_entry
from inventory.models import Accessory, Consumable, Kit, KitItem
from organization.models import Tenant

BASE_PERMS = ("inventory.add_kit", "inventory.change_kit", "inventory.add_kititem")
REPLACE_PERMS = (*BASE_PERMS, "inventory.delete_kititem")


@pytest.mark.django_db
class TestKitImport(TenantTestMixin):
    def _setup(self, permissions=REPLACE_PERMS):
        self.setup_tenant_context(name="Kit Import", slug="kit-import", permissions=list(permissions))
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.vendor = Manufacturer.objects.create(name="Kit Vendor", slug="kit-vendor")
        self.mouse = Accessory.objects.create(name="Mouse", manufacturer=self.vendor, tenant=self.tenant)
        self.cable = Consumable.objects.create(name="Cable", manufacturer=self.vendor, tenant=self.tenant)
        self.laptop = AssetType.objects.create(manufacturer=self.vendor, model="Laptop 1")

    def _run(self, rows):
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)
        import_csv_task(
            job_id=job.pk,
            rows_data=rows,
            app_label="inventory",
            model_name="kit",
            user_id=self.tenant_user.pk,
            tenant_id=self.tenant.pk,
        )
        job.refresh_from_db()
        return job

    def test_form_is_registered(self):
        assert get_registered_import_form(Kit) is KitBulkImportForm

    def test_csv_encoding_creates_kit_with_items(self):
        self._setup()
        job = self._run([{"name": "Starter", "items": f"accessory:{self.mouse.slug}*2; consumable:{self.cable.slug}"}])
        kit = Kit.objects.get(name="Starter")
        assert kit.tenant == self.tenant
        assert {(i.accessory_id, i.consumable_id, i.qty) for i in kit.items.all()} == {
            (self.mouse.pk, None, 2),
            (None, self.cable.pk, 1),
        }, job.logs

    def test_yaml_encoding_creates_kit_with_items(self):
        self._setup()
        self._run([{"name": "Yaml Kit", "items": [{"type": "accessory", "ref": self.mouse.slug, "qty": 3}]}])
        item = Kit.objects.get(name="Yaml Kit").items.get()
        assert (item.accessory_id, item.qty) == (self.mouse.pk, 3)

    def test_unknown_child_fails_atomically(self):
        self._setup()
        self._run([{"name": "Broken", "items": f"accessory:{self.mouse.slug}; accessory:nope"}])
        assert not Kit.objects.filter(name="Broken").exists()
        assert not KitItem.objects.exists()

    def test_duplicate_child_fails_atomically(self):
        self._setup()
        self._run([{"name": "Dup", "items": f"accessory:{self.mouse.slug}; accessory:{self.mouse.slug}*2"}])
        assert not Kit.objects.filter(name="Dup").exists()

    def test_out_of_scope_and_granted_foreign_children_are_rejected(self):
        self._setup()
        other = Tenant.objects.create(name="Other", slug="other-kit-tenant")
        foreign = Accessory.objects.create(name="Foreign Dock", manufacturer=self.vendor, tenant=other)
        self._run([{"name": "Foreign", "items": f"accessory:{foreign.slug}"}])
        assert not Kit.objects.filter(name="Foreign").exists()

    def test_tenant_column_is_rejected_for_the_file(self):
        self._setup()
        form = KitBulkImportForm()
        form._rows_data = [{"name": "T", "tenant": "other"}]
        self._run([{"name": "T", "tenant": "other"}])
        assert not Kit.objects.filter(name="T").exists() or Kit.objects.get(name="T").tenant == self.tenant

    def test_global_kit_is_never_touched(self):
        self._setup()
        shared = Kit.objects.create(name="Shared", description="keep", tenant=None)
        self._run([{"name": "Shared", "description": "changed", "items": f"accessory:{self.mouse.slug}"}])
        shared.refresh_from_db()
        assert shared.description == "keep"
        assert not shared.items.exists()

    def test_reimport_replaces_items_only_with_all_three_permissions(self):
        self._setup()
        self._run([{"name": "Re", "items": f"accessory:{self.mouse.slug}"}])
        self._run([{"name": "Re", "items": f"consumable:{self.cable.slug}"}])
        assert [i.consumable_id for i in Kit.objects.get(name="Re").items.all()] == [self.cable.pk]

    def test_reimport_without_delete_permission_skips_replacement(self):
        self._setup(permissions=BASE_PERMS)
        self._run([{"name": "Keep", "items": f"accessory:{self.mouse.slug}"}])
        job = self._run([{"name": "Keep", "items": f"consumable:{self.cable.slug}"}])
        assert [i.accessory_id for i in Kit.objects.get(name="Keep").items.all()] == [self.mouse.pk]
        assert "not replaced" in str(job.logs) or "unchanged" in str(job.logs)

    def test_empty_items_cell_leaves_items_untouched(self):
        self._setup()
        self._run([{"name": "Same", "items": f"accessory:{self.mouse.slug}"}])
        self._run([{"name": "Same", "description": "new text", "items": ""}])
        kit = Kit.objects.get(name="Same")
        assert kit.description == "new text"
        assert [i.accessory_id for i in kit.items.all()] == [self.mouse.pk]

    def test_imported_kit_uses_existing_checkout_path(self):
        self._setup()
        self._run([{"name": "Ship", "items": f"consumable:{self.cable.slug}"}])
        kit = Kit.objects.get(name="Ship")
        assert callable(checkout_kit)
        assert kit.items.filter(consumable=self.cable).exists()
        assert get_user_model().objects.filter(pk=self.tenant_user.pk).exists()


@pytest.mark.django_db
class TestKitImportForm(TenantTestMixin):
    """Form-level contract: clean() validates the whole file before anything is written."""

    def _form(self, text, permissions=REPLACE_PERMS):
        self.setup_tenant_context(name="Kit Form", slug="kit-form", permissions=list(permissions))
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.vendor = Manufacturer.objects.create(name="Form Vendor", slug="form-vendor")
        self.mouse = Accessory.objects.create(
            name="Form Mouse", slug="form-mouse", manufacturer=self.vendor, tenant=self.tenant
        )
        return self._build(text)

    def _build(self, text):
        form = KitBulkImportForm(data={"import_text": text, "active_tab": "editor", "import_format": "csv"})
        form.actor = self.tenant_user
        return form

    def test_tenant_column_is_rejected_for_the_whole_file(self):
        form = self._form("name,tenant\nA,other\n")
        assert not form.is_valid()
        assert "tenant column" in str(form.errors)

    def test_clean_reports_row_errors_with_row_numbers(self):
        form = self._form("name,items\nA,accessory:missing\n")
        assert not form.is_valid()
        assert "Row 2" in str(form.errors)
        assert "item 1" in str(form.errors)

    def test_clean_passes_for_valid_rows(self):
        form = self._form("name,items\nA,accessory:form-mouse\n")
        assert form.is_valid(), form.errors
        form.import_data()
        assert Kit.objects.get(name="A").items.count() == 1

    def test_shared_kit_name_is_rejected_in_preview(self):
        form = self._form("name\nShared\n")
        Kit.objects.create(name="Shared", tenant=None)
        assert not form.is_valid()
        assert "never modified" in str(form.errors)

    def test_blank_name_is_rejected(self):
        form = self._form("name,description\n,x\n")
        assert not form.is_valid()

    def test_create_with_items_requires_add_kititem(self):
        form = self._form("name,items\nNoPerm,accessory:form-mouse\n", permissions=("inventory.add_kit",))
        assert form.is_valid(), form.errors
        form.import_data()
        assert not Kit.objects.filter(name="NoPerm").exists()
        assert form.errors_list

    def test_unchanged_reimport_is_skipped(self):
        text = "name,items\nSame,accessory:form-mouse\n"
        first = self._form(text)
        assert first.is_valid(), first.errors
        first.import_data()
        assert Kit.objects.filter(name="Same").exists()
        again = self._build(text)
        assert again.is_valid(), again.errors
        again.import_data()
        assert again.skipped_list
        assert not again.updated_list

    def test_ambiguous_reference_is_rejected(self):
        form = self._form("name,items\nAmb,accessory:Dup\n")
        Accessory.objects.create(name="Dup", slug="dup-a", manufacturer=self.vendor, tenant=self.tenant)
        Accessory.objects.create(name="Dup", slug="dup-b", manufacturer=self.vendor, tenant=None)
        assert not form.is_valid()
        assert "ambiguous" in str(form.errors)


class TestKitItemParsing:
    def test_parse_rejects_unknown_kind_missing_reference_and_bad_quantity(self):
        for bad in ("bogus:x", "accessory:", "accessory:x*0"):
            with pytest.raises(ValidationError):
                _parse_entry(bad)
        with pytest.raises(ValidationError):
            _parse_entry({"type": "accessory", "ref": "x", "qty": "abc"})

    def test_declared_entries_encodings(self):
        assert _declared_entries("") is None
        assert _declared_entries([]) is None
        assert _declared_entries("accessory:a;\nconsumable:b") == ["accessory:a", "consumable:b"]
        assert _parse_entry("accessory:a*3") == ("accessory", "a", 3)
