"""#766 regression: bulk import generates the slug for auto-slug models.

``BulkImportForm._import_row`` validates a mapped row with ``full_clean()``
before ``save()``, while ``AutoSlugMixin.save()`` is what fills the slug in. A
row that omits the slug column therefore failed with ``This field cannot be
blank.`` for every auto-slug model, and ``AssetTypeBulkImportForm`` — which
declared no slug column at all — could not import a single row.

Every test drives ``import_csv_task``, the entry point the import view enqueues,
with an actor whose only authority is a role grant (no superuser).
"""

import pytest
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from assets.models import AssetType, Manufacturer
from core.importers.bulk_forms import _row_error_messages
from core.models import Job
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, Consumable

IMPORT_PERMISSIONS = (
    "assets.add_assettype",
    "assets.add_manufacturer",
    "inventory.add_accessory",
    "inventory.add_consumable",
)


@pytest.mark.django_db
class TestAutoSlugImport(TenantTestMixin):
    def _import(self, *, app_label, model_name, rows):
        self.setup_tenant_context(
            name="Auto Slug Tenant",
            slug="auto-slug-tenant",
            permissions=list(IMPORT_PERMISSIONS),
        )
        self.set_active_tenant(self.tenant, self.tenant_membership)
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
                {"manufacturer": "Fairphone", "model": "Fairphone 5"},
            ],
        )

        assert job.result == {"imported": 2, "failed": 0, "total": 2}
        assert set(AssetType.objects.filter(model="Fairphone 5").values_list("slug", flat=True)) == {
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

    def test_rejected_row_error_names_the_failing_field(self):
        job = self._import(
            app_label="assets",
            model_name="manufacturer",
            rows=[{"name": "Lenovo", "slug": "Not A Slug!"}],
        )

        assert job.status == Job.STATUS_FAILED
        assert "Row 2: slug: " in job.logs
        assert not Manufacturer.objects.filter(name="Lenovo").exists()


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
