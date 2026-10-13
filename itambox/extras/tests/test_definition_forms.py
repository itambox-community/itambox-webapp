import json

from django.contrib.contenttypes.models import ContentType
from django.http import QueryDict
from django.test import TestCase

from assets.models import Asset
from extras.forms import CustomFieldForm, CustomFieldsetForm
from extras.models import CustomField, CustomFieldChoice, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField


class CustomDefinitionFormTests(TestCase):
    def setUp(self):
        self.asset_ct = ContentType.objects.get_for_model(Asset)
        self.choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="form-choices",
            label="Form choices",
        )
        CustomFieldChoice.objects.create(
            choice_set=self.choice_set,
            key="one",
            label="One",
            position=10,
        )

    def _asset_applicability_data(self, **overrides):
        data = {
            "name": "form_field",
            "namespace": "local",
            "label": "Form field",
            "help_text": "Help",
            "field_type": CustomField.FIELD_TYPE_TEXT,
            "activation": CustomField.ACTIVATION_GLOBAL,
            "object_types": [str(self.asset_ct.pk)],
            "mappings": "[]",
        }
        data.update(overrides)
        return data

    def test_form_requires_explicit_object_types_as_applicability_authority(self):
        form = CustomFieldForm(data=self._asset_applicability_data(object_types=[]))

        self.assertFalse(form.is_valid())
        self.assertIn("object_types", form.errors)

    def test_temperature_cross_field_rule_is_reserved_to_core_identity(self):
        form = CustomFieldForm(
            data=self._asset_applicability_data(
                name="custom_temperature_max",
                field_type=CustomField.FIELD_TYPE_DECIMAL,
                decimal_scale="2",
                validation_rule="temperature_max_gte_min",
            )
        )

        self.assertFalse(form.is_valid())
        self.assertIn("validation_rule", form.errors)

    def test_definition_form_rejects_invalid_regex(self):
        form = CustomFieldForm(data=self._asset_applicability_data(name="invalid_regex", regex="["))

        self.assertFalse(form.is_valid())
        self.assertIn("regex", form.errors)

    def test_select_requires_choice_set_and_single_select_requires_exactly_one(self):
        missing_choice_set = CustomFieldForm(
            data=self._asset_applicability_data(
                name="select_field",
                field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
                max_values="1",
            )
        )
        self.assertFalse(missing_choice_set.is_valid())
        self.assertIn("choice_set", missing_choice_set.errors)

        wrong_max_values = CustomFieldForm(
            data=self._asset_applicability_data(
                name="select_field",
                field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
                choice_set=str(self.choice_set.pk),
                max_values="2",
            )
        )
        self.assertFalse(wrong_max_values.is_valid())
        self.assertIn("max_values", wrong_max_values.errors)

        valid = CustomFieldForm(
            data=self._asset_applicability_data(
                name="select_field",
                field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
                choice_set=str(self.choice_set.pk),
                max_values="1",
            )
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        saved = valid.save()
        self.assertEqual(saved.choice_set_id, self.choice_set.pk)
        self.assertEqual(saved.object_types.get().model, "asset")

    def test_definition_form_rejects_deprecated_choice_set(self):
        deprecated = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="deprecated-form-choices",
            label="Deprecated form choices",
            lifecycle=CustomFieldChoiceSet.LIFECYCLE_DEPRECATED,
        )
        form = CustomFieldForm(
            data=self._asset_applicability_data(
                name="deprecated_choice_field",
                field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
                choice_set=str(deprecated.pk),
                max_values="1",
            )
        )

        self.assertFalse(form.is_valid())
        self.assertIn("choice_set", form.errors)

    def test_decimal_requires_scale_and_non_decimal_rejects_scale(self):
        missing_scale = CustomFieldForm(
            data=self._asset_applicability_data(name="decimal_field", field_type=CustomField.FIELD_TYPE_DECIMAL)
        )
        self.assertFalse(missing_scale.is_valid())
        self.assertIn("decimal_scale", missing_scale.errors)

        unexpected_scale = CustomFieldForm(
            data=self._asset_applicability_data(
                name="text_field",
                decimal_scale="2",
            )
        )
        self.assertFalse(unexpected_scale.is_valid())
        self.assertIn("decimal_scale", unexpected_scale.errors)

    def test_local_definition_semantic_fields_are_read_only_after_creation(self):
        field = CustomField.objects.create(
            name="local_immutable_field",
            namespace="local",
            label="Local immutable field",
            field_type=CustomField.FIELD_TYPE_DECIMAL,
            activation=CustomField.ACTIVATION_GLOBAL,
            quantity_kind="length",
            canonical_unit="m",
            decimal_scale=2,
            nullable=True,
        )
        field.object_types.add(self.asset_ct)

        form = CustomFieldForm(instance=field)

        for field_name in CustomField.immutable_fields:
            form_name = "choice_set" if field_name == "choice_set_id" else field_name
            if field_name == "connector_identity":
                self.assertNotIn(form_name, form.fields)
            else:
                self.assertTrue(form.fields[form_name].disabled, form_name)
        self.assertFalse(form.fields["label"].disabled)
        self.assertFalse(form.fields["help_text"].disabled)

        tampered = CustomFieldForm(
            data={"name": "tampered_name", "label": field.label},
            instance=field,
        )
        self.assertFalse(tampered.is_valid())
        self.assertIn("name", tampered.errors)

    def test_local_forms_ignore_unexposed_provenance_without_changing_identity(self):
        field = CustomField.objects.create(
            name="form_field", label="Form field", activation=CustomField.ACTIVATION_GLOBAL
        )
        field.object_types.add(self.asset_ct)
        fieldset = CustomFieldset.objects.create(namespace="local", slug="provenance-form", label="Provenance")
        cases = (
            (CustomFieldForm, field, self._asset_applicability_data()),
            (CustomFieldsetForm, fieldset, {"namespace": "local", "slug": fieldset.slug, "label": fieldset.label}),
        )
        for form_class, instance, data in cases:
            with self.subTest(form=form_class.__name__):
                original_identity = instance.connector_identity
                data.update(library_id="999999", connector_identity="forged")
                form = form_class(instance=instance, data=data)
                self.assertNotIn("library_id", form.fields)
                self.assertNotIn("library", form.fields)
                self.assertNotIn("connector_identity", form.fields)
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                instance.refresh_from_db()
                self.assertEqual(instance.connector_identity, original_identity)

    def test_local_fieldset_identity_is_read_only_with_tamper_error(self):
        fieldset = CustomFieldset.objects.create(namespace="local", slug="immutable-form", label="Immutable Form")
        form = CustomFieldsetForm(instance=fieldset)
        self.assertTrue(form.fields["namespace"].disabled)
        self.assertTrue(form.fields["slug"].disabled)

        tampered = CustomFieldsetForm(
            data={"namespace": "other", "slug": fieldset.slug, "label": fieldset.label, "description": ""},
            instance=fieldset,
        )
        self.assertFalse(tampered.is_valid())
        self.assertIn("namespace", tampered.errors)

    def test_fieldset_form_persists_post_order_and_explicit_positions(self):
        first = CustomField.objects.create(
            name="first_field",
            label="First",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        second = CustomField.objects.create(
            name="second_field",
            label="Second",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        data = QueryDict(mutable=True)
        data.update(
            {
                "namespace": "local",
                "slug": "ordered-form",
                "label": "Ordered form",
                "description": "Ordered fields",
                "field_positions": json.dumps({"first_field": 30, "second_field": 10}),
            }
        )
        data.setlist("fields", [str(second.pk), str(first.pk)])

        form = CustomFieldsetForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        fieldset = form.save()
        self.assertEqual(fieldset.label, "Ordered form")
        self.assertEqual(
            list(fieldset.field_memberships.values_list("custom_field__name", "position")),
            [("second_field", 10), ("first_field", 30)],
        )

    def test_fieldset_form_rejects_effective_position_collisions_before_save(self):
        first = CustomField.objects.create(
            name="collision_first",
            label="First",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        second = CustomField.objects.create(
            name="collision_second",
            label="Second",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        data = QueryDict(mutable=True)
        data.update(
            {
                "namespace": "local",
                "slug": "collision-form",
                "label": "Collision form",
                "field_positions": json.dumps({"collision_first": 20}),
            }
        )
        data.setlist("fields", [str(first.pk), str(second.pk)])
        form = CustomFieldsetForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("Field positions must be unique.", form.non_field_errors())

    def test_managed_definitions_are_read_only_in_ordinary_editors(self):
        core_field = CustomField.objects.create(
            name="core_form_field",
            namespace="itambox",
            label="Core form field",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_CORE,
        )
        core_field.object_types.add(self.asset_ct)
        core_field_form = CustomFieldForm(instance=core_field)
        self.assertTrue(all(field.disabled for field in core_field_form.fields.values()))

        core_fieldset = CustomFieldset.objects.create(
            namespace="itambox",
            slug="core-form",
            label="Core form fieldset",
            management_kind=CustomFieldset.MANAGEMENT_CORE,
        )
        core_fieldset_form = CustomFieldsetForm(instance=core_fieldset)
        self.assertTrue(all(field.disabled for field in core_fieldset_form.fields.values()))

        self.assertFalse(CustomFieldsetField.objects.filter(fieldset=core_fieldset).exists())
