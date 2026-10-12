"""Direct tests for the catalog stages of the Snipe-IT importer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from core.importers.snipeit.client import SnipeITClient
from core.importers.snipeit.contracts import ImportContext, StageReporter
from core.importers.snipeit.stages.asset_models import AssetModelDependencies, AssetModelImporter, _connector_identity
from core.importers.snipeit.stages.catalog import (
    CategoryDependencies,
    CategoryImporter,
    ManufacturerDependencies,
    ManufacturerImporter,
    StatusLabelDependencies,
    StatusLabelImporter,
    SupplierDependencies,
    SupplierImporter,
)
from core.importers.snipeit.stages.custom_fields import (
    CustomFieldDependencies,
    CustomFieldImporter,
    FieldsetDependencies,
    FieldsetImporter,
)
from core.tests.mixins import TenantTestMixin

User = get_user_model()


def _make_client(endpoint: str, rows: list[dict], *, base_url="https://snipe.example") -> SnipeITClient:
    client = SnipeITClient.__new__(SnipeITClient)
    client.base_url = base_url
    client.PAGE_SIZE = 500
    client.context = SimpleNamespace(provider="snipe-it", tenant_id=None, actor_id=None, request_id=None)
    client.retry_budget_factory = lambda: SimpleNamespace()

    def fake_get(path, params=None, **kwargs):
        if path == endpoint:
            return {"total": len(rows), "rows": rows}
        return {"total": 0, "rows": []}

    client._get = fake_get
    return client


@pytest.mark.django_db
class TestSnipeITCatalogStages(TenantTestMixin):
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        from assets.models import Asset

        self.setup_tenant_context(name="Stage Tenant", slug="stage-tenant")
        self.admin = User.objects.create_superuser(username="stage-admin", email="stage@example.com", password="pw")
        self.asset_content_type = ContentType.objects.get_for_model(Asset)

    def _context(self, endpoint, rows, *, dry_run=False, update=False, base_url="https://snipe.example", tenant=None):
        target_tenant = self.tenant if tenant is None else tenant
        return ImportContext(
            client=_make_client(endpoint, rows, base_url=base_url),
            default_tenant=target_tenant,
            user=self.admin,
            dry_run=dry_run,
            update=update,
            map_companies=False,
            reporter=StageReporter(default_tenant=target_tenant, user=self.admin),
        )

    def _run(
        self, importer, endpoint, rows, *, dry_run=False, update=False, base_url="https://snipe.example", tenant=None
    ):
        return importer(
            self._context(endpoint, rows, dry_run=dry_run, update=update, base_url=base_url, tenant=tenant)
        ).run()

    def _create_asset_field(self, name, label=None, **kwargs):
        from extras.models import CustomField

        field = CustomField.objects.create(
            name=name,
            label=label or name,
            activation=CustomField.ACTIVATION_COMPOSED,
            **kwargs,
        )
        field.object_types.set([self.asset_content_type])
        return field

    @staticmethod
    def _active_choices(choice_set):
        from extras.models import CustomFieldChoice

        return choice_set.choices.filter(lifecycle=CustomFieldChoice.LIFECYCLE_ACTIVE).order_by("position")

    def test_status_labels_create_skip_and_update(self):
        from assets.models import StatusLabel

        deps = StatusLabelDependencies({})
        row = {"id": 101, "name": "Stage Ready", "type": "deployable"}
        result = self._run(lambda context: StatusLabelImporter(context, deps), "/api/v1/statuslabels", [row])
        obj = StatusLabel._base_manager.get(name="Stage Ready")
        assert result.counts.as_dict() == {"created": 1, "updated": 0, "skipped": 0, "failed": 0}
        assert deps.status_labels[101] == obj

        skipped = self._run(lambda context: StatusLabelImporter(context, deps), "/api/v1/statuslabels", [row])
        assert skipped.counts.skipped == 1
        assert deps.status_labels[101] == obj

        updated = self._run(
            lambda context: StatusLabelImporter(context, deps),
            "/api/v1/statuslabels",
            [{"id": 101, "name": "Stage Ready", "type": "archived"}],
            update=True,
        )
        obj.refresh_from_db()
        assert updated.counts.updated == 1
        assert obj.type == "archived"

    def test_manufacturers_create_skip_and_update(self):
        from assets.models import Manufacturer

        deps = ManufacturerDependencies({})
        row = {"id": 102, "name": "Stage Acme"}
        created = self._run(lambda context: ManufacturerImporter(context, deps), "/api/v1/manufacturers", [row])
        obj = Manufacturer._base_manager.get(name="Stage Acme")
        assert created.counts.created == 1
        assert deps.manufacturers[102] == obj

        skipped = self._run(lambda context: ManufacturerImporter(context, deps), "/api/v1/manufacturers", [row])
        assert skipped.counts.skipped == 1
        updated = self._run(
            lambda context: ManufacturerImporter(context, deps),
            "/api/v1/manufacturers",
            [row],
            update=True,
        )
        assert updated.counts.updated == 1
        assert deps.manufacturers[102] == obj

    def test_manufacturers_multiple_rows_get_unique_slugs(self):
        """Regression: Manufacturer has no AutoSlugMixin and a partial unique
        index on active slug, so empty slugs collide from the second row on."""
        from assets.models import Manufacturer

        deps = ManufacturerDependencies({})
        rows = [{"id": 201, "name": "Mfr Alpha"}, {"id": 202, "name": "Mfr Beta"}]
        result = self._run(lambda context: ManufacturerImporter(context, deps), "/api/v1/manufacturers", rows)
        assert result.counts.created == 2
        assert result.counts.failed == 0
        slugs = {m.slug for m in Manufacturer._base_manager.filter(name__in=["Mfr Alpha", "Mfr Beta"])}
        assert len(slugs) == 2
        assert all(slugs)
        assert deps.manufacturers[201].slug and deps.manufacturers[202].slug

    def test_categories_create_skip_and_update(self):
        from assets.models import Category

        deps = CategoryDependencies({})
        row = {"id": 103, "name": "Stage Laptops", "category_type": "asset"}
        created = self._run(lambda context: CategoryImporter(context, deps), "/api/v1/categories", [row])
        obj = Category._base_manager.get(name="Stage Laptops")
        assert created.counts.created == 1
        assert obj.applies_to == {"asset": True}
        assert deps.categories[103] == obj

        skipped = self._run(lambda context: CategoryImporter(context, deps), "/api/v1/categories", [row])
        assert skipped.counts.skipped == 1
        updated = self._run(
            lambda context: CategoryImporter(context, deps),
            "/api/v1/categories",
            [{"id": 103, "name": "Stage Laptops", "category_type": "accessory"}],
            update=True,
        )
        obj.refresh_from_db()
        assert updated.counts.updated == 1
        assert obj.applies_to == {"accessory": True}

    def test_suppliers_create_contact_skip_and_update(self):
        from assets.models import Supplier
        from organization.models import Contact, ContactAssignment

        deps = SupplierDependencies({})
        row = {
            "id": 104,
            "name": "Stage Supplier",
            "email": "contact@stage.example",
            "phone": "+49 100",
            "contact": "Stage Contact",
            "url": "https://supplier.example",
            "notes": "Initial notes",
        }
        created = self._run(lambda context: SupplierImporter(context, deps), "/api/v1/suppliers", [row])
        obj = Supplier._base_manager.get(name="Stage Supplier")
        contact = Contact._base_manager.get(name="Stage Contact")
        assert created.counts.created == 1
        assert deps.suppliers[104] == obj
        assert contact.email == "contact@stage.example"
        assert ContactAssignment._base_manager.filter(object_id=obj.pk, priority="primary").count() == 1

        skipped = self._run(lambda context: SupplierImporter(context, deps), "/api/v1/suppliers", [row])
        assert skipped.counts.skipped == 1

        updated = self._run(
            lambda context: SupplierImporter(context, deps),
            "/api/v1/suppliers",
            [{**row, "url": "https://updated.example", "notes": "Updated notes"}],
            update=True,
        )
        obj.refresh_from_db()
        assert updated.counts.updated == 1
        assert obj.website == "https://updated.example"
        assert obj.notes == "Updated notes"

    def test_custom_fields_create_skip_and_update_map_by_db_column(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        row = {
            "id": 105,
            "name": "Stage CPU",
            "db_column_name": "_snipeit_stage_cpu_105",
            "format": "LIST",
            "field_values": "one\n two",
        }
        created = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        obj = CustomField._base_manager.get(name="stage_cpu")
        assert created.counts.created == 1
        assert obj.field_type == "single-select"
        assert obj.activation == CustomField.ACTIVATION_COMPOSED
        assert set(obj.object_types.values_list("app_label", "model")) == {("assets", "asset"), ("assets", "assettype")}
        assert list(self._active_choices(obj.choice_set).values_list("key", flat=True)) == ["one", "two"]
        assert deps.custom_fields["_snipeit_stage_cpu_105"] == obj

        skipped = self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "changed"}],
        )
        assert skipped.counts.skipped == 1
        assert list(self._active_choices(obj.choice_set).values_list("key", flat=True)) == ["one", "two"]

        updated = self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "name": "Updated CPU", "format": "LIST", "field_values": "three"}],
            update=True,
        )
        obj.refresh_from_db()
        assert updated.counts.updated == 1
        assert obj.label == "Updated CPU"
        assert obj.field_type == "single-select"
        assert list(self._active_choices(obj.choice_set).values_list("key", flat=True)) == ["three"]
        retired_choice = obj.choice_set.choices.get(key="one")
        assert retired_choice.label == "one"
        assert retired_choice.lifecycle == retired_choice.LIFECYCLE_DEPRECATED

    def test_custom_field_import_refuses_same_id_from_another_source(self):
        from extras.models import CustomField

        row = {
            "id": 128,
            "name": "Source A CPU",
            "db_column_name": "_snipeit_cross_source_cpu_128",
            "format": "LIST",
            "field_values": "one",
        }
        first_deps = CustomFieldDependencies({})
        created = self._run(
            lambda context: CustomFieldImporter(context, first_deps),
            "/api/v1/fields",
            [row],
            base_url="https://snipe-a.example",
        )
        field = CustomField._base_manager.get(name="cross_source_cpu")

        second_deps = CustomFieldDependencies({})
        result = self._run(
            lambda context: CustomFieldImporter(context, second_deps),
            "/api/v1/fields",
            [{**row, "name": "Source B CPU", "field_values": "two"}],
            update=True,
            base_url="https://snipe-b.example",
        )

        field.refresh_from_db()
        assert created.counts.created == 1
        assert result.counts.failed == 1
        assert field.label == "Source A CPU"
        assert list(self._active_choices(field.choice_set).values_list("key", flat=True)) == ["one"]
        assert second_deps.custom_fields == {}

    def test_custom_field_import_refuses_same_id_from_another_target_tenant(self):
        from extras.models import CustomField
        from organization.models import Tenant

        other_tenant = Tenant.objects.create(name="Other Target Tenant", slug="other-target-tenant")
        row = {
            "id": 129,
            "name": "Tenant-Bound CPU",
            "db_column_name": "_snipeit_cross_tenant_cpu_129",
            "format": "TEXT",
        }
        first_deps = CustomFieldDependencies({})
        self._run(
            lambda context: CustomFieldImporter(context, first_deps),
            "/api/v1/fields",
            [row],
            base_url="https://snipe.example",
            tenant=self.tenant,
        )
        field = CustomField._base_manager.get(name="cross_tenant_cpu")

        second_deps = CustomFieldDependencies({})
        result = self._run(
            lambda context: CustomFieldImporter(context, second_deps),
            "/api/v1/fields",
            [{**row, "name": "Other Tenant CPU"}],
            update=True,
            base_url="https://snipe.example",
            tenant=other_tenant,
        )

        field.refresh_from_db()
        assert result.counts.failed == 1
        assert field.label == "Tenant-Bound CPU"
        assert second_deps.custom_fields == {}

    def test_choice_set_import_refuses_same_id_from_another_source(self):
        from extras.models import CustomField

        first_row = {
            "id": 130,
            "name": "Source A Choice Field",
            "db_column_name": "_snipeit_source_a_choice_130",
            "format": "LIST",
            "field_values": "one",
        }
        first_deps = CustomFieldDependencies({})
        self._run(
            lambda context: CustomFieldImporter(context, first_deps),
            "/api/v1/fields",
            [first_row],
            base_url="https://snipe-a.example",
        )
        first_field = CustomField._base_manager.get(name="source_a_choice")

        second_deps = CustomFieldDependencies({})
        result = self._run(
            lambda context: CustomFieldImporter(context, second_deps),
            "/api/v1/fields",
            [
                {
                    "id": 130,
                    "name": "Source B Choice Field",
                    "db_column_name": "_snipeit_source_b_choice_130",
                    "format": "LIST",
                    "field_values": "two",
                }
            ],
            base_url="https://snipe-b.example",
        )

        first_field.refresh_from_db()
        assert result.counts.failed == 1
        assert list(self._active_choices(first_field.choice_set).values_list("key", flat=True)) == ["one"]
        assert not CustomField._base_manager.filter(name="source_b_choice").exists()
        assert second_deps.custom_fields == {}

    def test_fieldset_import_refuses_same_id_from_another_source(self):
        from extras.models import CustomFieldset

        first_deps = FieldsetDependencies({}, {})
        first = self._run(
            lambda context: FieldsetImporter(context, first_deps),
            "/api/v1/fieldsets",
            [{"id": 131, "name": "Source A Specs", "fields": {"rows": []}}],
            base_url="https://snipe-a.example",
        )
        fieldset = CustomFieldset._base_manager.get(namespace="local", slug="snipeit-131")

        second_deps = FieldsetDependencies({}, {})
        result = self._run(
            lambda context: FieldsetImporter(context, second_deps),
            "/api/v1/fieldsets",
            [{"id": 131, "name": "Source B Specs", "fields": {"rows": []}}],
            update=True,
            base_url="https://snipe-b.example",
        )

        fieldset.refresh_from_db()
        assert first.counts.created == 1
        assert result.counts.failed == 1
        assert fieldset.label == "Source A Specs"
        assert second_deps.fieldsets == {}

    def test_custom_field_import_refuses_managed_choice_set_owner(self):
        from extras.models import CustomField, CustomFieldChoiceSet

        deps = CustomFieldDependencies({})
        row = {
            "id": 126,
            "name": "Managed Choice Row",
            "db_column_name": "_snipeit_managed_choice_row_126",
            "format": "LIST",
            "field_values": "Stable",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        field = CustomField.objects.get(name="managed_choice_row")
        choice_set = field.choice_set
        CustomFieldChoiceSet.objects.filter(pk=choice_set.pk).update(
            management_kind=CustomFieldChoiceSet.MANAGEMENT_CORE
        )

        result = self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [row],
            update=True,
        )

        choice_set.refresh_from_db()
        assert result.counts.failed == 1
        assert choice_set.management_kind == CustomFieldChoiceSet.MANAGEMENT_CORE
        assert field.choice_set_id == choice_set.pk

    def test_custom_field_choice_keys_remain_distinct_for_normalized_label_collisions(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        row = {
            "id": 115,
            "name": "Collision choices",
            "db_column_name": "_snipeit_collision_choices_115",
            "format": "LIST",
            "field_values": "A-B\nA B\nÜber",
        }

        result = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])

        field = CustomField._base_manager.get(name="collision_choices")
        keys = list(self._active_choices(field.choice_set).values_list("key", flat=True))
        assert result.counts.created == 1
        assert len(keys) == 3
        assert len(set(keys)) == 3
        assert "uber" in keys
        assert all(key.isascii() for key in keys)

        row["field_values"] = "A B\nA-B\nÜber"
        reordered = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row], update=True)
        field.refresh_from_db()
        labels_to_keys = dict(field.choice_set.choices.values_list("label", "key"))
        assert reordered.counts.updated == 1
        assert labels_to_keys["A-B"] == keys[0]
        assert labels_to_keys["A B"] == keys[1]

        single_row = {
            "id": 116,
            "name": "Single collision choice",
            "db_column_name": "_snipeit_single_collision_choice_116",
            "format": "LIST",
            "field_values": "A-B",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [single_row])
        single_field = CustomField._base_manager.get(name="single_collision_choice")
        original_key = single_field.choice_set.choices.get(label="A-B").key
        single_row["field_values"] = "A B\nA-B"

        result = self._run(
            lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [single_row], update=True
        )

        single_field.refresh_from_db()
        single_labels_to_keys = dict(single_field.choice_set.choices.values_list("label", "key"))
        assert result.counts.updated == 1
        assert single_labels_to_keys["A-B"] == original_key
        assert len(set(single_labels_to_keys.values())) == 2

    def test_custom_field_dry_run_keeps_single_select_contract_valid(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        row = {
            "id": 170,
            "name": "Dry run choices",
            "db_column_name": "_snipeit_dry_run_choices_170",
            "format": "LIST",
            "field_values": "One\nTwo",
        }

        result = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row], dry_run=True)

        assert result.counts.created == 1
        assert result.counts.failed == 0
        assert deps.custom_fields[row["db_column_name"]].choice_set is not None
        assert not CustomField._base_manager.filter(name="dry_run_choices").exists()

    def test_custom_field_choice_key_survives_label_change(self):
        deps = CustomFieldDependencies({})
        row = {
            "id": 140,
            "db_column_name": "_snipeit_stable_choice_label_140",
            "name": "Stable Choice Labels",
            "format": "LIST",
            "field_values": "One\nTwo",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        field = deps.custom_fields[row["db_column_name"]]
        original_keys = list(self._active_choices(field.choice_set).values_list("key", flat=True))

        self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "Uno\nTwo"}],
            update=True,
        )

        field.refresh_from_db()
        choices = list(self._active_choices(field.choice_set).values_list("key", "label"))
        retired_choice = field.choice_set.choices.get(key=original_keys[0])
        assert choices == [("uno", "Uno"), (original_keys[1], "Two")]
        assert retired_choice.label == "One"
        assert retired_choice.lifecycle == retired_choice.LIFECYCLE_DEPRECATED

    def test_generated_choice_key_collision_never_reuses_different_label(self):
        deps = CustomFieldDependencies({})
        row = {
            "id": 180,
            "db_column_name": "_snipeit_stable_choice_normalization_180",
            "name": "Stable Choice Normalization",
            "format": "LIST",
            "field_values": "A-B",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        field = deps.custom_fields[row["db_column_name"]]
        original_key = field.choice_set.choices.get(label="A-B").key

        self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "A B"}],
            update=True,
        )

        field.refresh_from_db()
        active_choices = dict(self._active_choices(field.choice_set).values_list("label", "key"))
        retired_choice = field.choice_set.choices.get(key=original_key)
        assert "A B" in active_choices
        assert active_choices["A B"] != original_key
        assert retired_choice.label == "A-B"
        assert retired_choice.lifecycle == retired_choice.LIFECYCLE_DEPRECATED

    def test_custom_field_choice_removal_and_unrelated_addition_never_reuses_old_key(self):
        deps = CustomFieldDependencies({})
        row = {
            "id": 141,
            "db_column_name": "_snipeit_stable_choice_collision_141",
            "name": "Stable Choice Collision",
            "format": "LIST",
            "field_values": "One\nTwo",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        field = deps.custom_fields[row["db_column_name"]]
        original_keys = list(self._active_choices(field.choice_set).values_list("key", flat=True))

        self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "Two\nThree"}],
            update=True,
        )

        field.refresh_from_db()
        active_choices = list(self._active_choices(field.choice_set).values_list("key", "label"))
        retired_choice = field.choice_set.choices.get(key=original_keys[0])
        assert active_choices == [(original_keys[1], "Two"), ("three", "Three")]
        assert retired_choice.label == "One"
        assert retired_choice.lifecycle == retired_choice.LIFECYCLE_DEPRECATED

    def test_custom_field_long_names_get_stable_collision_free_keys(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        prefix = "collision_long_" + "x" * 54
        rows = [
            {
                "id": 121,
                "name": "Long field A",
                "db_column_name": prefix + "_a",
                "format": "TEXT",
            },
            {
                "id": 122,
                "name": "Long field B",
                "db_column_name": prefix + "_b",
                "format": "TEXT",
            },
        ]

        result = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", rows)

        fields = list(CustomField._base_manager.filter(label__in=["Long field A", "Long field B"]))
        assert result.counts.created == 2
        assert len(fields) == 2
        assert len({field.name for field in fields}) == 2

    def test_custom_field_import_refuses_unprovenanced_local_collision(self):
        from extras.models import CustomField

        field = self._create_asset_field(
            name="collision_field",
            label="Operator-Owned Field",
            namespace="local",
            lifecycle=CustomField.LIFECYCLE_ACTIVE,
        )
        deps = CustomFieldDependencies({})
        row = {
            "id": 123,
            "db_column_name": "_snipeit_collision_field_123",
            "name": "Imported Collision Field",
            "format": "TEXT",
        }

        result = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row], update=False)

        field.refresh_from_db()
        assert result.counts.failed == 1
        assert field.label == "Operator-Owned Field"
        assert field.connector_identity is None
        assert deps.custom_fields == {}

    def test_custom_field_import_refuses_managed_identity(self):
        from extras.models import CustomField

        field = self._create_asset_field(
            name="managed_stage_field",
            label="Managed Stage Field",
            namespace="local",
            management_kind=CustomField.MANAGEMENT_CORE,
            lifecycle=CustomField.LIFECYCLE_ACTIVE,
        )
        deps = CustomFieldDependencies({})
        row = {"id": 120, "db_column_name": "managed_stage_field", "name": "Managed Stage Field", "format": "TEXT"}

        result = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row], update=True)

        field.refresh_from_db()
        assert result.counts.failed == 1
        assert field.management_kind == CustomField.MANAGEMENT_CORE

    def test_custom_field_import_refuses_deprecated_identity(self):
        from extras.models import CustomField

        row = {
            "id": 121,
            "db_column_name": "deprecated_stage_field",
            "name": "Deprecated Stage Field",
            "format": "TEXT",
        }
        first_deps = CustomFieldDependencies({})
        created = self._run(lambda context: CustomFieldImporter(context, first_deps), "/api/v1/fields", [row])
        field = CustomField._base_manager.get(name="deprecated_stage_field")
        field.lifecycle = CustomField.LIFECYCLE_DEPRECATED
        field.save(update_fields=["lifecycle"])

        second_deps = CustomFieldDependencies({})
        result = self._run(
            lambda context: CustomFieldImporter(context, second_deps), "/api/v1/fields", [row], update=True
        )

        field.refresh_from_db()
        assert created.counts.created == 1
        assert result.counts.failed == 1
        assert field.lifecycle == CustomField.LIFECYCLE_DEPRECATED
        assert second_deps.custom_fields == {}

    def test_choice_reconciliation_avoids_deprecated_position_conflicts(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        row = {
            "id": 125,
            "db_column_name": "_snipeit_choice_position_stability_125",
            "name": "Choice Position Stability",
            "format": "LIST",
            "field_values": "One\nTwo",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "One"}],
            update=True,
        )
        self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "One\nThree"}],
            update=True,
        )
        result = self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{**row, "field_values": "Three"}],
            update=True,
        )

        field = CustomField._base_manager.get(name="choice_position_stability")
        assert result.counts.updated == 1
        assert list(self._active_choices(field.choice_set).values_list("key", flat=True)) == ["three"]

    def test_custom_field_import_refuses_managed_choice_set_identity(self):
        from extras.models import CustomField, CustomFieldChoiceSet

        choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="snipeit-122",
            label="Managed choices",
            management_kind=CustomFieldChoiceSet.MANAGEMENT_CORE,
            lifecycle=CustomFieldChoiceSet.LIFECYCLE_ACTIVE,
        )
        self._create_asset_field(
            name="managed_select_field",
            label="Managed Select Field",
            field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
            choice_set=choice_set,
            max_values=1,
        )
        deps = CustomFieldDependencies({})
        row = {
            "id": 122,
            "db_column_name": "managed_select_field",
            "name": "Managed Select Field",
            "format": "LIST",
            "field_values": "one",
        }

        result = self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row], update=True)

        choice_set.refresh_from_db()
        assert result.counts.failed == 1
        assert choice_set.management_kind == CustomFieldChoiceSet.MANAGEMENT_CORE

    def test_custom_field_import_preserves_choices_when_values_are_omitted(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        row = {
            "id": 123,
            "db_column_name": "_snipeit_omitted_select_123",
            "name": "Omitted Select",
            "format": "LIST",
            "field_values": "one\ntwo",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])

        result = self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [{key: value for key, value in row.items() if key != "field_values"}],
            update=True,
        )

        field = CustomField._base_manager.get(name="omitted_select")
        assert result.counts.updated == 1
        assert list(self._active_choices(field.choice_set).values_list("key", flat=True)) == ["one", "two"]

    def test_custom_field_import_does_not_reuse_deprecated_choice_set_identity(self):
        from extras.models import CustomField

        deps = CustomFieldDependencies({})
        row = {
            "id": 116,
            "name": "Deprecated choices",
            "db_column_name": "_snipeit_deprecated_choices_116",
            "format": "LIST",
            "field_values": "one",
        }
        self._run(lambda context: CustomFieldImporter(context, deps), "/api/v1/fields", [row])
        field = CustomField._base_manager.get(name="deprecated_choices")
        choice_set = field.choice_set
        choice_set.lifecycle = choice_set.LIFECYCLE_DEPRECATED
        choice_set.save(update_fields=["lifecycle"])

        result = self._run(
            lambda context: CustomFieldImporter(context, deps),
            "/api/v1/fields",
            [row],
            update=True,
        )

        choice_set.refresh_from_db()
        assert result.counts.failed == 1
        assert choice_set.lifecycle == choice_set.LIFECYCLE_DEPRECATED

    def test_fieldset_import_refuses_managed_identity(self):
        from extras.models import CustomFieldset

        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="snipeit-124",
            label="Managed Stage Fieldset",
            management_kind=CustomFieldset.MANAGEMENT_CORE,
            lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
        )
        deps = FieldsetDependencies({}, {})
        row = {"id": 124, "name": "Managed Stage Fieldset"}

        result = self._run(lambda context: FieldsetImporter(context, deps), "/api/v1/fieldsets", [row], update=True)

        fieldset.refresh_from_db()
        assert result.counts.failed == 1
        assert fieldset.management_kind == CustomFieldset.MANAGEMENT_CORE

    def test_fieldset_update_with_empty_remote_membership_clears_composition(self):
        from extras.models import CustomFieldset

        field = self._create_asset_field(name="stage_empty_serial", label="Serial")
        deps = FieldsetDependencies({"_snipeit_stage_empty_serial_117": field}, {})
        row = {
            "id": 117,
            "name": "Empty Stage Specs",
            "fields": {"rows": [{"db_column_name": "_snipeit_stage_empty_serial_117"}]},
        }
        self._run(lambda context: FieldsetImporter(context, deps), "/api/v1/fieldsets", [row])
        empty = self._run(
            lambda context: FieldsetImporter(context, deps),
            "/api/v1/fieldsets",
            [{**row, "fields": {"rows": []}}],
            update=True,
        )

        fieldset = CustomFieldset._base_manager.get(namespace="local", slug="snipeit-117")
        assert empty.counts.updated == 1
        assert not fieldset.fields.exists()

    def test_fieldset_partial_dependency_resolution_preserves_existing_composition(self):
        from extras.models import CustomFieldset

        first = self._create_asset_field(name="stage_partial_first", label="First")
        second = self._create_asset_field(name="stage_partial_second", label="Second")
        deps = FieldsetDependencies(
            {
                "_snipeit_stage_partial_first_118": first,
                "_snipeit_stage_partial_second_118": second,
            },
            {},
        )
        row = {
            "id": 118,
            "name": "Partial Stage Specs",
            "fields": {"rows": [{"db_column_name": "_snipeit_stage_partial_first_118"}]},
        }
        self._run(lambda context: FieldsetImporter(context, deps), "/api/v1/fieldsets", [row])

        result = self._run(
            lambda context: FieldsetImporter(context, deps),
            "/api/v1/fieldsets",
            [
                {
                    **row,
                    "fields": {
                        "rows": [
                            {"db_column_name": "_snipeit_stage_partial_second_118"},
                            {"db_column_name": "_snipeit_stage_partial_missing_118"},
                        ]
                    },
                }
            ],
            update=True,
        )

        fieldset = CustomFieldset._base_manager.get(namespace="local", slug="snipeit-118")
        assert result.counts.failed == 1
        assert list(fieldset.fields.all()) == [first]
        assert second not in fieldset.fields.all()

    def test_fieldsets_create_resolves_fields_skip_and_update_does_not_save(self):
        from extras.models import CustomFieldset

        field = self._create_asset_field(name="stage_serial", label="Serial")
        deps = FieldsetDependencies({"_snipeit_stage_serial_106": field}, {})
        row = {
            "id": 106,
            "name": "Stage Specs",
            "fields": {"rows": [{"db_column_name": "_snipeit_stage_serial_106"}]},
        }
        created = self._run(lambda context: FieldsetImporter(context, deps), "/api/v1/fieldsets", [row])
        obj = CustomFieldset._base_manager.get(namespace="local", slug="snipeit-106")
        assert created.counts.created == 1
        assert list(obj.fields.all()) == [field]
        assert deps.fieldsets[106] == obj

        omitted = self._run(
            lambda context: FieldsetImporter(context, deps),
            "/api/v1/fieldsets",
            [{"id": 106, "name": "Stage Specs"}],
            update=True,
        )
        obj.refresh_from_db()
        assert omitted.counts.updated == 1
        assert list(obj.fields.all()) == [field]

        skipped = self._run(lambda context: FieldsetImporter(context, deps), "/api/v1/fieldsets", [row])
        assert skipped.counts.skipped == 1

        other = self._create_asset_field(name="stage_other", label="Other")
        updated = self._run(
            lambda context: FieldsetImporter(context, deps),
            "/api/v1/fieldsets",
            [{**row, "fields": {"rows": [{"db_column_name": "_snipeit_stage_other_106"}]}}],
            update=True,
        )
        obj.refresh_from_db()
        assert updated.counts.failed == 1
        assert list(obj.fields.all()) == [field]
        assert other not in obj.fields.all()

    def test_fieldset_import_does_not_reuse_deprecated_identity(self):
        from extras.models import CustomFieldset

        deps = FieldsetDependencies({}, {})
        row = {"id": 119, "name": "Deprecated Stage Specs", "fields": {"rows": []}}
        self._run(lambda context: FieldsetImporter(context, deps), "/api/v1/fieldsets", [row])
        fieldset = CustomFieldset._base_manager.get(namespace="local", slug="snipeit-119")
        fieldset.lifecycle = fieldset.LIFECYCLE_DEPRECATED
        fieldset.save(update_fields=["lifecycle"])

        result = self._run(
            lambda context: FieldsetImporter(context, deps),
            "/api/v1/fieldsets",
            [row],
            update=True,
        )

        fieldset.refresh_from_db()
        assert result.counts.failed == 1
        assert fieldset.lifecycle == fieldset.LIFECYCLE_DEPRECATED

    def test_asset_model_update_unresolved_fieldset_preserves_composition(self):
        from assets.models import AssetType, AssetTypeFieldset, Category, Manufacturer
        from extras.models import CustomFieldset

        manufacturer = Manufacturer.objects.create(name="Unresolved Model Maker")
        category = Category.objects.create(name="Unresolved Model Category", applies_to={"asset": True})
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="snipeit-127",
            label="Unresolved Specs",
            lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
        )
        deps = AssetModelDependencies({127: manufacturer}, {127: category}, {127: fieldset}, {})
        row = {
            "id": 127,
            "name": "Unresolved Fieldset Model",
            "manufacturer": {"id": 127},
            "category": {"id": 127},
            "fieldset": {"id": 127},
        }
        self._run(lambda context: AssetModelImporter(context, deps), "/api/v1/models", [row])

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [{**row, "fieldset": {"id": 999}}],
            update=True,
        )

        asset_type = AssetType._base_manager.get(model="Unresolved Fieldset Model")
        assert result.counts.failed == 1
        assert list(AssetTypeFieldset.objects.filter(asset_type=asset_type).values_list("fieldset_id", "position")) == [
            (fieldset.pk, 1)
        ]

    def test_asset_model_update_omitted_fieldset_preserves_composition(self):
        from assets.models import AssetType, AssetTypeFieldset, Category, Manufacturer
        from extras.models import CustomFieldset

        manufacturer = Manufacturer.objects.create(name="Omitted Model Maker")
        category = Category.objects.create(name="Omitted Model Category", applies_to={"asset": True})
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="snipeit-126",
            label="Omitted Specs",
            lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
        )
        deps = AssetModelDependencies({126: manufacturer}, {126: category}, {126: fieldset}, {})
        row = {
            "id": 126,
            "name": "Omitted Fieldset Model",
            "manufacturer": {"id": 126},
            "category": {"id": 126},
            "fieldset": {"id": 126},
        }
        self._run(lambda context: AssetModelImporter(context, deps), "/api/v1/models", [row])

        omitted = {key: value for key, value in row.items() if key != "fieldset"}
        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [omitted],
            update=True,
        )

        asset_type = AssetType._base_manager.get(model="Omitted Fieldset Model")
        assert result.counts.updated == 1
        assert list(AssetTypeFieldset.objects.filter(asset_type=asset_type).values_list("fieldset_id", "position")) == [
            (fieldset.pk, 1)
        ]

    def test_asset_models_create_skip_update_and_optional_relations(self):
        from assets.models import AssetType, Category, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Stage Model Maker")
        category = Category.objects.create(name="Stage Model Category", applies_to={"asset": True})
        deps = AssetModelDependencies({107: manufacturer}, {108: category}, {}, {})
        row = {
            "id": 107,
            "name": "Stage ThinkPad",
            "manufacturer": {"id": 107},
            "category": {"id": 108},
            "fieldset": None,
            "eol": "36",
            "model_number": "P" * 120,
        }
        created = self._run(lambda context: AssetModelImporter(context, deps), "/api/v1/models", [row])
        obj = AssetType._base_manager.get(model="Stage ThinkPad")
        assert created.counts.created == 1
        assert obj.manufacturer == manufacturer
        assert obj.category == category
        assert not hasattr(obj, "custom_fieldset")
        assert obj.eol_months == 36
        assert len(obj.part_number) == 100
        assert obj.custom_field_data == {}
        assert obj.connector_identity == _connector_identity(
            {"source_url": "https://snipe.example", "source_id": "107"}
        )
        assert deps.asset_models[107] == obj

        skipped = self._run(lambda context: AssetModelImporter(context, deps), "/api/v1/models", [row])
        assert skipped.counts.skipped == 1

        obj.custom_field_data = {"operator_value": "keep"}
        obj.save(update_fields=["custom_field_data"])
        updated = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [{**row, "name": "Stage ThinkPad Updated", "eol": "48", "model_number": "UPDATED"}],
            update=True,
        )
        obj.refresh_from_db()
        assert updated.counts.updated == 1
        assert obj.model == "Stage ThinkPad Updated"
        assert obj.eol_months == 48
        assert obj.part_number == "UPDATED"

    def test_asset_model_attribute_adoption_preserves_existing_data_and_composition(self):
        from assets.models import AssetType, AssetTypeFieldset, Manufacturer
        from extras.models import CustomFieldset

        manufacturer = Manufacturer.objects.create(name="Adoption Stage Maker")
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="adoption-stage-fields",
            label="Adoption Stage Fields",
            lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
        )
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Adoption Stage Model",
            slug="adoption-stage-model",
            connector_identity=_connector_identity({"source_url": "https://snipe.example", "source_id": "124"}),
            custom_field_data={"operator_value": "keep"},
        )
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=1)
        deps = AssetModelDependencies({124: manufacturer}, {}, {}, {})
        row = {
            "id": 124,
            "name": "Adoption Stage Model",
            "manufacturer": {"id": 124},
            "category": None,
        }

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [row],
            update=True,
        )

        asset_type.refresh_from_db()
        assert result.counts.updated == 1
        assert asset_type.custom_field_data == {"operator_value": "keep"}
        assert asset_type.connector_identity == _connector_identity(
            {"source_url": "https://snipe.example", "source_id": "124"}
        )
        assert list(asset_type.fieldset_memberships.values_list("fieldset_id", flat=True)) == [fieldset.pk]

    def test_asset_model_attribute_match_refuses_unprovenanced_local_asset_type(self):
        from assets.models import AssetType, AssetTypeFieldset, Manufacturer
        from extras.models import CustomFieldset

        manufacturer = Manufacturer.objects.create(name="Unprovenanced Stage Maker")
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="unprovenanced-stage-fields",
            label="Unprovenanced Stage Fields",
            lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
        )
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Unprovenanced Stage Model",
            slug="unprovenanced-stage-model",
            custom_field_data={"operator_value": "keep"},
        )
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=1)
        deps = AssetModelDependencies({126: manufacturer}, {}, {}, {})
        row = {
            "id": 126,
            "name": "Unprovenanced Stage Model",
            "manufacturer": {"id": 126},
            "category": None,
            "fieldset": None,
        }

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [row],
            update=True,
        )

        asset_type.refresh_from_db()
        assert result.counts.failed == 1
        assert asset_type.model == "Unprovenanced Stage Model"
        assert asset_type.custom_field_data == {"operator_value": "keep"}
        assert list(asset_type.fieldset_memberships.values_list("fieldset_id", flat=True)) == [fieldset.pk]
        assert deps.asset_models == {}

    def test_asset_model_attribute_match_refuses_other_source_identity(self):
        from assets.models import AssetType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Foreign Source Stage Maker")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Foreign Source Stage Model",
            slug="foreign-source-stage-model",
            custom_field_data={"snipeit_id": "999", "operator_value": "keep"},
        )
        deps = AssetModelDependencies({125: manufacturer}, {}, {}, {})
        row = {
            "id": 125,
            "name": "Foreign Source Stage Model",
            "manufacturer": {"id": 125},
            "category": None,
        }

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [row],
            update=True,
        )

        asset_type.refresh_from_db()
        assert result.counts.failed == 1
        assert asset_type.custom_field_data == {"snipeit_id": "999", "operator_value": "keep"}
        assert deps.asset_models == {}

    def test_asset_model_import_refuses_library_managed_source_match(self):
        from assets.models import AssetType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Managed Stage Maker")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Managed Stage Model",
            slug="managed-stage-model",
            management_kind=AssetType.MANAGEMENT_LIBRARY,
            custom_field_data={"snipeit_id": "118"},
        )
        deps = AssetModelDependencies({118: manufacturer}, {}, {}, {})
        row = {
            "id": 118,
            "name": "Managed Stage Model",
            "manufacturer": {"id": 118},
            "category": None,
            "fieldset": None,
        }

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [row],
            update=True,
        )

        asset_type.refresh_from_db()
        assert result.counts.failed == 1
        assert asset_type.model == "Managed Stage Model"
        assert deps.asset_models == {}

    def test_asset_model_import_refuses_core_managed_source_match(self):
        from assets.models import AssetType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Core Managed Stage Maker")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Core Managed Stage Model",
            slug="core-managed-stage-model",
            management_kind=AssetType.MANAGEMENT_CORE,
            connector_identity=_connector_identity({"source_url": "https://snipe.example", "source_id": "119"}),
        )
        deps = AssetModelDependencies({119: manufacturer}, {}, {}, {})
        row = {
            "id": 119,
            "name": "Core Managed Stage Model Updated",
            "manufacturer": {"id": 119},
            "category": None,
            "fieldset": None,
        }

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [row],
            update=True,
        )

        asset_type.refresh_from_db()
        assert result.counts.failed == 1
        assert asset_type.model == "Core Managed Stage Model"
        assert deps.asset_models == {}

    def test_asset_model_import_does_not_reuse_deleted_identity(self):
        from assets.models import AssetType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Deleted Model Maker")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Deleted Stage Model",
            slug="deleted-stage-model",
            deleted_at=timezone.now(),
            custom_field_data={"snipeit_id": "120"},
        )
        deps = AssetModelDependencies({120: manufacturer}, {}, {}, {})
        row = {
            "id": 120,
            "name": "Deleted Stage Model",
            "manufacturer": {"id": 120},
            "category": None,
            "fieldset": None,
        }

        result = self._run(
            lambda context: AssetModelImporter(context, deps),
            "/api/v1/models",
            [row],
            update=True,
        )

        asset_type.refresh_from_db()
        assert result.counts.failed == 1
        assert asset_type.deleted_at is not None
        assert deps.asset_models == {}

    @pytest.mark.parametrize(
        ("stage", "endpoint", "dependencies", "model_label", "manager_method", "failure_key", "output_name"),
        [
            (
                StatusLabelImporter,
                "/api/v1/statuslabels",
                StatusLabelDependencies({}),
                "StatusLabel",
                "create",
                "name",
                "status_labels",
            ),
            (
                ManufacturerImporter,
                "/api/v1/manufacturers",
                ManufacturerDependencies({}),
                "Manufacturer",
                "get_or_create",
                "name",
                "manufacturers",
            ),
            (
                CategoryImporter,
                "/api/v1/categories",
                CategoryDependencies({}),
                "Category",
                "create",
                "name",
                "categories",
            ),
            (
                SupplierImporter,
                "/api/v1/suppliers",
                SupplierDependencies({}),
                "Supplier",
                "create",
                "name",
                "suppliers",
            ),
            (
                CustomFieldImporter,
                "/api/v1/fields",
                CustomFieldDependencies({}),
                "CustomField",
                "create",
                "name",
                "custom_fields",
            ),
            (
                FieldsetImporter,
                "/api/v1/fieldsets",
                FieldsetDependencies({}, {}),
                "CustomFieldset",
                "create",
                "name",
                "fieldsets",
            ),
        ],
    )
    def test_catalog_row_failure_does_not_publish_failed_map_entry(
        self, monkeypatch, stage, endpoint, dependencies, model_label, manager_method, failure_key, output_name
    ):
        from django.apps import apps

        model = apps.get_model(
            "assets" if model_label in {"StatusLabel", "Manufacturer", "Category", "Supplier"} else "extras",
            model_label,
        )
        manager = model.objects
        original = getattr(manager, manager_method)

        def fail_bad(*args, **kwargs):
            if kwargs.get(failure_key) in {"Broken", "broken_field", "Broken Fieldset"} or kwargs.get("label") in {
                "Broken",
                "broken_field",
                "Broken Fieldset",
            }:
                raise RuntimeError("bad row")
            if any(
                (kwargs.get("defaults") or {}).get(key) in {"Broken", "broken_field", "Broken Fieldset"}
                for key in ("name", "label")
            ):
                raise RuntimeError("bad row")
            return original(*args, **kwargs)

        monkeypatch.setattr(manager, manager_method, fail_bad)
        if model_label == "CustomField":
            rows = [
                {"id": 1, "db_column_name": "_snipeit_broken_field_1", "name": "Broken", "format": "TEXT"},
                {"id": 2, "db_column_name": "_snipeit_good_field_2", "name": "Good", "format": "TEXT"},
            ]
        elif model_label == "CustomFieldset":
            rows = [{"id": 1, "name": "Broken Fieldset"}, {"id": 2, "name": "Good Fieldset"}]
        else:
            rows = [{"id": 1, "name": "Broken"}, {"id": 2, "name": "Good"}]
        if model_label == "StatusLabel":
            rows[0]["type"] = rows[1]["type"] = "deployable"
        if model_label == "Category":
            rows[0]["category_type"] = rows[1]["category_type"] = "asset"
        if model_label == "Supplier":
            rows[0].update(email="", phone="", contact="")
            rows[1].update(email="", phone="", contact="")

        deps = dependencies

        def importer(context):
            return stage(context, deps)

        result = self._run(importer, endpoint, rows)
        output = getattr(deps, output_name)
        assert result.counts.failed == 1
        assert result.counts.created == 1
        failed_key = "_snipeit_broken_field_1" if model_label == "CustomField" else 1
        good_key = "_snipeit_good_field_2" if model_label == "CustomField" else 2
        assert failed_key not in output
        assert good_key in output

    def test_asset_model_row_failure_does_not_publish_failed_map_entry(self, monkeypatch):
        from assets.models import AssetType, Manufacturer

        manufacturer = Manufacturer.objects.create(name="Stage Failure Maker")
        deps = AssetModelDependencies({1: manufacturer}, {}, {}, {})
        original = AssetType.objects.create

        def fail_bad(*args, **kwargs):
            if kwargs.get("model") == "Broken Model":
                raise RuntimeError("bad row")
            return original(*args, **kwargs)

        monkeypatch.setattr(AssetType.objects, "create", fail_bad)
        rows = [
            {"id": 1, "name": "Broken Model", "manufacturer": {"id": 1}},
            {"id": 2, "name": "Good Model", "manufacturer": {"id": 1}},
        ]
        result = self._run(lambda context: AssetModelImporter(context, deps), "/api/v1/models", rows)
        assert result.counts.failed == 1
        assert result.counts.created == 1
        assert 1 not in deps.asset_models
        assert 2 in deps.asset_models

    def test_all_seven_stages_dry_run_publish_negative_ids_without_writes(self):
        from assets.models import AssetType, Category, Manufacturer, StatusLabel, Supplier
        from extras.models import CustomField, CustomFieldset

        manufacturer = Manufacturer(id=-1, name="Dry Maker")
        custom_field = CustomField(
            id=-2,
            name="dry_field",
            label="Dry Field",
            field_type="text",
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        stages = [
            (
                StatusLabelImporter,
                StatusLabelDependencies({}),
                "/api/v1/statuslabels",
                [{"id": 201, "name": "Dry Status", "type": "deployable"}],
                201,
            ),
            (
                ManufacturerImporter,
                ManufacturerDependencies({}),
                "/api/v1/manufacturers",
                [{"id": 202, "name": "Dry Maker"}],
                202,
            ),
            (
                CategoryImporter,
                CategoryDependencies({}),
                "/api/v1/categories",
                [{"id": 203, "name": "Dry Category", "category_type": "asset"}],
                203,
            ),
            (
                SupplierImporter,
                SupplierDependencies({}),
                "/api/v1/suppliers",
                [{"id": 204, "name": "Dry Supplier", "email": "", "phone": "", "contact": ""}],
                204,
            ),
            (
                CustomFieldImporter,
                CustomFieldDependencies({}),
                "/api/v1/fields",
                [{"id": 205, "name": "Dry Field", "db_column_name": "_snipeit_dry_field_205", "format": "TEXT"}],
                "_snipeit_dry_field_205",
            ),
            (
                FieldsetImporter,
                FieldsetDependencies({"_snipeit_dry_field_205": custom_field}, {}),
                "/api/v1/fieldsets",
                [{"id": 206, "name": "Dry Fieldset"}],
                206,
            ),
            (
                AssetModelImporter,
                AssetModelDependencies({202: manufacturer}, {}, {}, {}),
                "/api/v1/models",
                [{"id": 207, "name": "Dry Model", "manufacturer": {"id": 202}}],
                207,
            ),
        ]
        before = {
            StatusLabel: StatusLabel._base_manager.count(),
            Manufacturer: Manufacturer._base_manager.count(),
            Category: Category._base_manager.count(),
            Supplier: Supplier._base_manager.count(),
            CustomField: CustomField._base_manager.count(),
            CustomFieldset: CustomFieldset._base_manager.count(),
            AssetType: AssetType._base_manager.count(),
        }
        for stage, dependencies, endpoint, rows, source_id in stages:
            result = self._run(
                lambda context, cls=stage, deps=dependencies: cls(context, deps), endpoint, rows, dry_run=True
            )
            output = (
                dependencies.status_labels
                if isinstance(dependencies, StatusLabelDependencies)
                else dependencies.manufacturers
                if isinstance(dependencies, ManufacturerDependencies)
                else dependencies.categories
                if isinstance(dependencies, CategoryDependencies)
                else dependencies.suppliers
                if isinstance(dependencies, SupplierDependencies)
                else dependencies.custom_fields
                if isinstance(dependencies, CustomFieldDependencies)
                else dependencies.fieldsets
                if isinstance(dependencies, FieldsetDependencies)
                else dependencies.asset_models
            )
            assert result.counts.created == 1
            assert output[source_id].id == -rows[0]["id"]
        assert before[StatusLabel] == StatusLabel._base_manager.count()
        assert before[Manufacturer] == Manufacturer._base_manager.count()
        assert before[Category] == Category._base_manager.count()
        assert before[Supplier] == Supplier._base_manager.count()
        assert before[CustomField] == CustomField._base_manager.count()
        assert before[CustomFieldset] == CustomFieldset._base_manager.count()
        assert before[AssetType] == AssetType._base_manager.count()
