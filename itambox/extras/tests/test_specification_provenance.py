"""Focused PostgreSQL qualification for the T07 provenance cutover."""

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from assets.models import AssetType, Manufacturer
from extras.models import (
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldset,
)


class T07ProvenanceModelTests(TestCase):
    def test_choice_owns_source_identity_and_has_no_obsolete_scalar_metadata(self):
        choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="t07-choice-set",
            label="T07 Choice Set",
            connector_identity="sha256:" + "a" * 64,
        )
        choice = CustomFieldChoice.objects.create(
            choice_set=choice_set,
            key="choice_a",
            label="Choice A",
            position=1,
        )

        self.assertEqual(choice.choice_set_id, choice_set.pk)
        self.assertEqual(choice_set.connector_identity, "sha256:" + "a" * 64)
        field_names = {field.name for field in CustomFieldChoice._meta.concrete_fields}
        for obsolete_name in (
            "management_kind",
            "managed_paths",
            "source_checksum",
            "last_reconciled_at",
            "connector_identity",
        ):
            self.assertNotIn(obsolete_name, field_names)

    def test_model_guards_preserve_definition_identities(self):
        manufacturer = Manufacturer.objects.create(name="T07 Manufacturer", slug="t07-manufacturer")
        field = CustomField.objects.create(
            name="t07_model_field",
            namespace="local",
            label="T07 Model Field",
            activation=CustomField.ACTIVATION_GLOBAL,
            connector_identity="sha256:" + "b" * 64,
        )
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="t07-model-fieldset",
            label="T07 Model Fieldset",
            connector_identity="sha256:" + "c" * 64,
        )
        choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="t07-model-choice-set",
            label="T07 Model Choice Set",
            connector_identity="sha256:" + "d" * 64,
        )
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="T07 Model",
            slug="t07-model",
            management_kind=AssetType.MANAGEMENT_LIBRARY,
            connector_identity="sha256:" + "e" * 64,
        )

        attempted_changes = (
            (field, "name", "t07_model_field_renamed"),
            (fieldset, "slug", "t07-model-fieldset-renamed"),
            (choice_set, "connector_identity", "sha256:" + "f" * 64),
            (asset_type, "connector_identity", "sha256:" + "0" * 64),
        )
        for instance, field_name, replacement in attempted_changes:
            with self.subTest(model=type(instance).__name__, field=field_name):
                setattr(instance, field_name, replacement)
                with self.assertRaises(ValidationError):
                    instance.save()

    def test_queryset_updates_cannot_rewrite_identity_or_source_state(self):
        field = CustomField.objects.create(
            name="t07_queryset_field",
            namespace="local",
            label="T07 Queryset Field",
            activation=CustomField.ACTIVATION_GLOBAL,
            connector_identity="sha256:" + "1" * 64,
        )
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="t07-queryset-fieldset",
            label="T07 Queryset Fieldset",
            connector_identity="sha256:" + "2" * 64,
        )
        choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="t07-queryset-choice-set",
            label="T07 Queryset Choice Set",
            connector_identity="sha256:" + "3" * 64,
        )
        manufacturer = Manufacturer.objects.create(name="T07 Queryset Manufacturer", slug="t07-queryset-manufacturer")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="T07 Queryset Model",
            slug="t07-queryset-model",
            management_kind=AssetType.MANAGEMENT_LIBRARY,
            connector_identity="sha256:" + "4" * 64,
        )

        attempted_updates = (
            (CustomField, field.pk, {"namespace": "queryset-renamed"}),
            (CustomFieldset, fieldset.pk, {"connector_identity": "sha256:" + "5" * 64}),
            (CustomFieldChoiceSet, choice_set.pk, {"slug": "t07-queryset-choice-set-renamed"}),
            (AssetType, asset_type.pk, {"connector_identity": "sha256:" + "6" * 64}),
        )
        for model, pk, values in attempted_updates:
            with (
                self.subTest(model=model.__name__, values=values),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                model._base_manager.filter(pk=pk).update(**values)

    def test_model_and_database_hard_delete_guards_retain_identities(self):
        field = CustomField.objects.create(
            name="t07_delete_field",
            namespace="local",
            label="T07 Delete Field",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="t07-delete-choice-set",
            label="T07 Delete Choice Set",
        )
        choice = CustomFieldChoice.objects.create(
            choice_set=choice_set,
            key="delete_choice",
            label="Delete Choice",
            position=1,
        )

        for instance in (field, choice_set, choice):
            with self.subTest(model=type(instance).__name__), self.assertRaises(ProtectedError):
                instance.delete()

        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomField.objects.filter(pk=field.pk).delete()

        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())

    def test_targeted_sql_cannot_hard_delete_library_managed_asset_type(self):
        manufacturer = Manufacturer.objects.create(name="T07 Delete Manufacturer", slug="t07-delete-manufacturer")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="T07 Protected Model",
            slug="t07-protected-model",
            management_kind=AssetType.MANAGEMENT_LIBRARY,
            connector_identity="sha256:" + "7" * 64,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM assets_assettype WHERE id = %s", [asset_type.pk])

        self.assertTrue(AssetType.all_objects.filter(pk=asset_type.pk).exists())

    def test_legacy_scalar_fields_are_not_runtime_authority(self):
        with self.assertRaises(FieldDoesNotExist):
            CustomFieldChoice._meta.get_field("source_checksum")
        with self.assertRaises(FieldDoesNotExist):
            CustomFieldChoice._meta.get_field("managed_paths")

        self.assertFalse(hasattr(CustomFieldChoice, "source_checksum"))
        self.assertFalse(hasattr(CustomFieldChoice, "managed_paths"))
