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
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from assets.models import AssetType, Manufacturer
from assets.models.catalog import AssetTypeFieldset
from core.importers.bulk_forms import _row_error_messages
from core.models import Job, ObjectChange
from core.tasks.csv_import import import_csv_task
from core.tests.mixins import TenantTestMixin
from inventory.models import Accessory, Consumable
from organization.models import Role, Tenant

IMPORT_PERMISSIONS = (
    "assets.add_assettype",
    "assets.add_manufacturer",
    "inventory.add_accessory",
    "inventory.add_consumable",
)


@pytest.mark.django_db
class TestAutoSlugImport(TenantTestMixin):
    def _import(self, *, app_label, model_name, rows, provider=False):
        self.setup_tenant_context(
            name="Auto Slug Tenant",
            slug="auto-slug-tenant",
            permissions=list(IMPORT_PERMISSIONS),
        )
        if provider:
            self.tenant.is_provider = True
            self.tenant.save(update_fields=["is_provider"])
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
            provider=True,
        )

        assert job.status == Job.STATUS_COMPLETED
        assert job.result == {"imported": 1, "failed": 0, "total": 1}
        assert AssetType.objects.get(model="Fairphone 5").slug == "fairphone-fairphone-5"

    def test_customer_add_grant_cannot_create_asset_type_in_worker(self):
        self._manufacturer()
        content_type = ContentType.objects.get_for_model(AssetType)
        before_types = AssetType.objects.count()
        before_memberships = AssetTypeFieldset.objects.count()
        before_changes = ObjectChange.objects.filter(content_type=content_type).count()

        job = self._import(
            app_label="assets",
            model_name="assettype",
            rows=[{"manufacturer": "Fairphone", "model": "Customer-created type"}],
        )

        assert job.status == Job.STATUS_FAILED
        assert AssetType.objects.count() == before_types
        assert AssetTypeFieldset.objects.count() == before_memberships
        assert ObjectChange.objects.filter(content_type=content_type).count() == before_changes

    def test_provider_grant_outside_active_provider_tenant_cannot_create_asset_type(self):
        self._manufacturer()
        self.setup_tenant_context(
            name="Granted Provider",
            slug="granted-provider",
            permissions=["assets.add_assettype"],
        )
        self.tenant.is_provider = True
        self.tenant.save(update_fields=["is_provider"])
        active_provider = Tenant.objects.create(
            name="Active Provider",
            slug="active-provider",
            is_provider=True,
        )
        active_role = Role.objects.create(
            tenant=active_provider,
            name="Active Provider Member",
            permissions=[],
        )
        self.grant(self.tenant_user, active_provider, active_role)
        job = Job.objects.create(name="Bulk Import", tenant=active_provider, status=Job.STATUS_PENDING)
        content_type = ContentType.objects.get_for_model(AssetType)
        before_types = AssetType.objects.count()
        before_memberships = AssetTypeFieldset.objects.count()
        before_changes = ObjectChange.objects.filter(content_type=content_type).count()

        import_csv_task(
            job_id=job.pk,
            rows_data=[{"manufacturer": "Fairphone", "model": "Wrong-provider type"}],
            app_label="assets",
            model_name="assettype",
            user_id=self.tenant_user.pk,
            tenant_id=active_provider.pk,
        )

        job.refresh_from_db()
        assert job.status == Job.STATUS_FAILED
        assert AssetType.objects.count() == before_types
        assert AssetTypeFieldset.objects.count() == before_memberships
        assert ObjectChange.objects.filter(content_type=content_type).count() == before_changes

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
            provider=True,
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
            provider=True,
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
