"""#766 regression: bulk import generates the slug for auto-slug models.

``BulkImportForm._import_row`` validates a mapped row with ``full_clean()``
before ``save()``, while ``AutoSlugMixin.save()`` is what fills the slug in. A
row that omits the slug column therefore failed with ``This field cannot be
blank.`` for every auto-slug model, and ``AssetTypeBulkImportForm`` — which
declared no slug column at all — could not import a single row.

The integration tests drive ``import_csv_task``, the entry point the import view
enqueues, with an actor whose only authority is a role grant (no superuser).
"""

from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from assets.forms.import_forms import LocationBulkImportForm, ManufacturerBulkImportForm
from assets.models import Asset, AssetType, Manufacturer
from core.importers.bulk_forms import _row_error_messages
from core.managers import Scope
from core.models import Job, ObjectChange
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin, grant
from inventory.models import Accessory, Consumable
from licenses.models import License
from organization.models import AssetHolder, Location, Role, Site, Tenant
from software.models import Software

IMPORT_PERMISSIONS = (
    "assets.add_assettype",
    "assets.add_manufacturer",
    "inventory.add_accessory",
    "inventory.add_consumable",
    "organization.add_location",
)


@pytest.mark.django_db
class TestAutoSlugImport(TenantTestMixin):
    def _import(self, *, app_label, model_name, rows):
        self.setup_tenant_context(
            name="Auto Slug Tenant",
            slug="auto-slug-tenant",
            permissions=list(IMPORT_PERMISSIONS),
        )
        self.tenant.is_provider = True
        self.tenant.save(update_fields=["is_provider"])
        self.set_active_tenant(self.tenant, self.tenant_membership)
        if model_name == "location":
            Site.objects.create(name="Import Site", slug="import-site", tenant=self.tenant)
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

    def _manufacturer(self):
        return Manufacturer.objects.create(name="Fairphone", slug="fairphone")

    def test_asset_type_row_without_slug_imports_with_the_generated_slug(self):
        self._manufacturer()

        job = self._import(
            app_label="assets",
            model_name="assettype",
            rows=[{"manufacturer": "Fairphone", "model": "Fairphone 5"}],
        )

        assert job.status == Job.STATUS_COMPLETED
        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert AssetType.objects.get(model="Fairphone 5").slug == "fairphone-fairphone-5"

    def test_accessory_row_without_slug_imports_with_the_generated_slug(self):
        self._manufacturer()

        job = self._import(
            app_label="inventory",
            model_name="accessory",
            rows=[{"name": "Fairphone 5 case", "manufacturer": "Fairphone"}],
        )

        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert Accessory.objects.get(name="Fairphone 5 case").slug == "fairphone-fairphone-5-case"

    def test_consumable_row_without_slug_imports_with_the_generated_slug(self):
        self._manufacturer()

        job = self._import(
            app_label="inventory",
            model_name="consumable",
            rows=[{"name": "Fairphone 5 battery", "manufacturer": "Fairphone"}],
        )

        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert Consumable.objects.get(name="Fairphone 5 battery").slug == "fairphone-fairphone-5-battery"

    def test_generated_slugs_stay_unique_across_rows_of_one_import(self):
        self._manufacturer()

        job = self._import(
            app_label="assets",
            model_name="assettype",
            rows=[
                {"manufacturer": "Fairphone", "model": "Fairphone 5"},
                {"manufacturer": "Fairphone", "model": "Fairphone-5"},
            ],
        )

        assert job.result == {"imported": 2, "failed": 0, "total": 2}
        assert set(
            AssetType.objects.filter(model__in=["Fairphone 5", "Fairphone-5"]).values_list("slug", flat=True)
        ) == {
            "fairphone-fairphone-5",
            "fairphone-fairphone-5-1",
        }

    def test_asset_type_row_with_an_explicit_slug_keeps_it(self):
        self._manufacturer()

        job = self._import(
            app_label="assets",
            model_name="assettype",
            rows=[{"manufacturer": "Fairphone", "model": "Fairphone 5", "slug": "fp5-custom"}],
        )

        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert AssetType.objects.get(model="Fairphone 5").slug == "fp5-custom"

    def test_manufacturer_row_with_an_explicit_slug_keeps_it(self):
        job = self._import(
            app_label="assets",
            model_name="manufacturer",
            rows=[{"name": "Lenovo", "slug": "lenovo-custom"}],
        )

        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert Manufacturer.objects.get(name="Lenovo").slug == "lenovo-custom"

    @pytest.mark.parametrize(
        ("form_class", "app_label", "model_name", "import_format", "import_text", "name", "slug"),
        [
            (
                ManufacturerBulkImportForm,
                "assets",
                "manufacturer",
                "csv",
                "name\nNew Maker",
                "New Maker",
                "new-maker",
            ),
            (
                ManufacturerBulkImportForm,
                "assets",
                "manufacturer",
                "yaml",
                "- name: New Maker\n",
                "New Maker",
                "new-maker",
            ),
            (
                LocationBulkImportForm,
                "organization",
                "location",
                "csv",
                "name,site\nHQ,Import Site",
                "HQ",
                "hq",
            ),
            (
                LocationBulkImportForm,
                "organization",
                "location",
                "yaml",
                "- name: HQ\n  site: Import Site\n",
                "HQ",
                "hq",
            ),
        ],
    )
    def test_manufacturer_and_location_csv_yaml_imports_generate_omitted_slugs(
        self, form_class, app_label, model_name, import_format, import_text, name, slug
    ):
        form = form_class(
            data={"active_tab": "editor", "import_format": import_format, "import_text": import_text, "delimiter": ","}
        )
        assert form.is_valid(), form.errors

        job = self._import(app_label=app_label, model_name=model_name, rows=form._rows_data)

        assert job.status == Job.STATUS_COMPLETED
        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        model = Manufacturer if model_name == "manufacturer" else Location
        assert model.objects.get(name=name).slug == slug

    def test_location_row_with_an_explicit_slug_keeps_it(self):
        job = self._import(
            app_label="organization",
            model_name="location",
            rows=[{"name": "Custom HQ", "site": "Import Site", "slug": "custom-hq-code"}],
        )

        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert Location.objects.get(name="Custom HQ").slug == "custom-hq-code"

    def test_generated_location_slugs_avoid_active_and_soft_deleted_collisions(self):
        self.setup_tenant_context(
            name="Location Slug Tenant",
            slug="location-slug-tenant",
            permissions=list(IMPORT_PERMISSIONS),
        )
        self.set_active_tenant(self.tenant, self.tenant_membership)
        site = Site.objects.create(name="Import Site", slug="import-site", tenant=self.tenant)
        Location.objects.create(name="Existing Active Location", slug="active-collision", site=site, tenant=self.tenant)
        deleted = Location.objects.create(
            name="Existing Deleted Location", slug="deleted-collision", site=site, tenant=self.tenant
        )
        deleted.delete()
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)

        import_csv_task(
            job_id=job.pk,
            rows_data=[
                {"name": "Active Collision", "site": str(site.pk)},
                {"name": "Deleted Collision", "site": str(site.pk)},
            ],
            app_label="organization",
            model_name="location",
            user_id=self.tenant_user.pk,
            tenant_id=self.tenant.pk,
        )

        job.refresh_from_db()
        assert job.status == Job.STATUS_COMPLETED
        assert job.result == {"imported": 2, "failed": 0, "total": 2}
        assert Location.objects.get(name="Active Collision").slug == "active-collision-1"
        assert Location.objects.get(name="Deleted Collision").slug == "deleted-collision-1"

    def test_rejected_row_error_names_the_failing_field(self):
        job = self._import(
            app_label="assets",
            model_name="manufacturer",
            rows=[{"name": "Lenovo", "slug": "Not A Slug!"}],
        )

        assert job.status == Job.STATUS_FAILED
        assert "Row 2: slug: " in job.logs
        assert not Manufacturer.objects.filter(name="Lenovo").exists()

    def _import_task(self, *, tenant, user, app_label, model_name, rows):
        job = Job.objects.create(name="Bulk Import", tenant=tenant, status=Job.STATUS_PENDING)

        import_csv_task(
            job_id=job.pk,
            rows_data=rows,
            app_label=app_label,
            model_name=model_name,
            user_id=user.pk,
            tenant_id=tenant.pk,
        )

        job.refresh_from_db()
        return job

    def _accessory_audit_failures(self, imported, private_note):
        failures = []
        if imported.notes != private_note:
            failures.append("accessory private note was not preserved")
        audit = ObjectChange._base_manager.filter(
            changed_object_type=ContentType.objects.get_for_model(Accessory),
            changed_object_id=imported.pk,
        ).first()
        if audit is None:
            failures.append("accessory import did not write an audit row")
        elif audit.tenant_id != imported.tenant_id:
            failures.append(f"accessory/audit tenant mismatch: {imported.tenant_id} != {audit.tenant_id}")
        return failures

    def test_tenant_imports_bind_owner_for_each_tenant_owned_model(self):
        self.setup_tenant_context(
            name="Tenant Import Owner",
            slug="tenant-import-owner",
            permissions=[
                "assets.add_asset",
                "inventory.add_accessory",
                "inventory.add_consumable",
                "licenses.add_license",
                "organization.add_assetholder",
                "organization.add_location",
            ],
        )
        self.set_active_tenant(self.tenant, self.tenant_membership)
        unrelated_tenant = Tenant.objects.create(name="Unrelated Import Tenant", slug="unrelated-import-tenant")

        manufacturer = self._manufacturer()
        site = Site.objects.create(name="Tenant Import Site", slug="tenant-import-site", tenant=self.tenant)
        asset_location = Location.objects.create(
            name="Tenant Import Location Reference",
            slug="tenant-import-location-reference",
            site=site,
            tenant=self.tenant,
        )
        software = Software.objects.create(
            name="Tenant Import Software",
            manufacturer=manufacturer,
            tenant=self.tenant,
        )
        private_note = "tenant-private-import-note"
        cases = (
            (
                "inventory",
                "accessory",
                Accessory,
                [{"name": "Tenant Import Accessory", "manufacturer": str(manufacturer.pk), "notes": private_note}],
                {"name": "Tenant Import Accessory"},
            ),
            (
                "inventory",
                "consumable",
                Consumable,
                [{"name": "Tenant Import Consumable", "manufacturer": str(manufacturer.pk)}],
                {"name": "Tenant Import Consumable"},
            ),
            (
                "assets",
                "asset",
                Asset,
                [{"name": "Tenant Import Asset", "location": str(asset_location.pk)}],
                {"name": "Tenant Import Asset"},
            ),
            (
                "organization",
                "assetholder",
                AssetHolder,
                [{"first_name": "Tenant", "last_name": "Import Holder", "upn": "tenant-import-holder@example.test"}],
                {"upn": "tenant-import-holder@example.test"},
            ),
            (
                "organization",
                "location",
                Location,
                [{"name": "Tenant Imported Location", "slug": "tenant-imported-location", "site": str(site.pk)}],
                {"name": "Tenant Imported Location"},
            ),
            (
                "licenses",
                "license",
                License,
                [{"name": "Tenant Import License", "software": str(software.pk)}],
                {"name": "Tenant Import License"},
            ),
        )

        owner_scope = Scope(Scope.TENANT, tenant=self.tenant)
        unrelated_scope = Scope(Scope.TENANT, tenant=unrelated_tenant)
        failures = []
        for app_label, model_name, model, rows, lookup in cases:
            job = self._import_task(
                tenant=self.tenant,
                user=self.tenant_user,
                app_label=app_label,
                model_name=model_name,
                rows=rows,
            )
            if job.status != Job.STATUS_COMPLETED:
                failures.append(f"{model_name} import did not complete: {job.logs}")
                continue

            imported = model._base_manager.filter(**lookup).first()
            if imported is None:
                failures.append(f"{model_name} import created no row")
                continue
            if imported.tenant_id != self.tenant.pk:
                failures.append(f"{model_name} owner was {imported.tenant_id}, expected {self.tenant.pk}")
            if not model.objects.for_scope(owner_scope).filter(pk=imported.pk).exists():
                failures.append(f"{model_name} is not visible in its owner tenant")
            if model.objects.for_scope(unrelated_scope).filter(pk=imported.pk).exists():
                failures.append(f"{model_name} is visible in an unrelated tenant")

            if model is Accessory:
                failures.extend(self._accessory_audit_failures(imported, private_note))

        assert not failures, "\\n".join(failures)

    def test_same_consumable_can_be_imported_for_two_tenants(self):
        self.setup_tenant_context(
            name="Consumable Import Owner",
            slug="consumable-import-owner",
            permissions=["inventory.add_consumable"],
        )
        self.set_active_tenant(self.tenant, self.tenant_membership)
        second_tenant = Tenant.objects.create(name="Consumable Import Second", slug="consumable-import-second")
        second_role = Role.objects.create(
            tenant=second_tenant,
            name="Consumable Import Role",
            permissions=["inventory.add_consumable"],
        )
        grant(self.tenant_user, second_tenant, second_role)
        manufacturer = self._manufacturer()
        rows = [{"name": "Same Imported Product", "manufacturer": str(manufacturer.pk), "part_number": "SKU-779"}]

        first_job = self._import_task(
            tenant=self.tenant,
            user=self.tenant_user,
            app_label="inventory",
            model_name="consumable",
            rows=rows,
        )
        second_job = self._import_task(
            tenant=second_tenant,
            user=self.tenant_user,
            app_label="inventory",
            model_name="consumable",
            rows=rows,
        )

        assert first_job.result == {"imported": 1, "failed": 0, "total": 1}, first_job.logs
        assert second_job.result == {"imported": 1, "failed": 0, "total": 1}, second_job.logs
        products = Consumable._base_manager.filter(name="Same Imported Product", manufacturer=manufacturer)
        assert set(products.values_list("tenant_id", flat=True)) == {self.tenant.pk, second_tenant.pk}
        assert products.count() == 2

    def test_asset_import_rejects_a_location_owned_by_another_tenant(self):
        self.setup_tenant_context(
            name="Asset Import Owner",
            slug="asset-import-owner",
            permissions=["assets.add_asset"],
        )
        self.set_active_tenant(self.tenant, self.tenant_membership)
        foreign_tenant = Tenant.objects.create(name="Foreign Asset Import Tenant", slug="foreign-asset-import")
        foreign_site = Site.objects.create(
            name="Foreign Asset Import Site",
            slug="foreign-asset-import-site",
            tenant=foreign_tenant,
        )
        foreign_location = Location.objects.create(
            name="Foreign Asset Import Location",
            slug="foreign-asset-import-location",
            site=foreign_site,
            tenant=foreign_tenant,
        )

        job = self._import_task(
            tenant=self.tenant,
            user=self.tenant_user,
            app_label="assets",
            model_name="asset",
            rows=[{"name": "Asset With Foreign Location", "location": str(foreign_location.pk)}],
        )

        assert job.status == Job.STATUS_FAILED
        assert "different tenant" in job.logs
        assert not Asset._base_manager.filter(name="Asset With Foreign Location").exists()


class TestRowErrorMessages:
    """The row formatter is the operator-facing contract for a rejected row."""

    def test_field_keyed_error_names_the_field(self):
        error = ValidationError({"slug": ["This field cannot be blank."]})

        assert _row_error_messages(error) == ["slug: This field cannot be blank."]

    def test_non_field_error_keeps_its_message(self):
        error = ValidationError({NON_FIELD_ERRORS: ["The row is inconsistent."]})

        assert _row_error_messages(error) == ["The row is inconsistent."]

    def test_plain_message_error_is_reported_unchanged(self):
        assert _row_error_messages(ValidationError("invalid")) == ["invalid"]


class TestConfiguredSlugGeneration:
    @pytest.mark.parametrize(
        ("form_class", "model"),
        [
            (ManufacturerBulkImportForm, Manufacturer),
            (LocationBulkImportForm, Location),
        ],
    )
    def test_non_auto_slug_models_use_the_curated_form_slug_source(self, form_class, model):
        instance = model(name="Example")

        with patch("core.importers.bulk_forms.generate_unique_slug") as generate_unique_slug:
            form_class()._populate_generated_slug(instance)

        generate_unique_slug.assert_called_once_with(instance, "name")
