import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from assets.api.serializers import AssetSerializer, AssetTypeSerializer
from assets.api.specification_api import SpecificationCommandAPIException, create_fieldset_selection_from_values
from assets.models import Asset, AssetType, AssetTypeFieldset, Manufacturer
from assets.services.specifications.commands import preview_asset_type_create
from assets.services.specifications.contracts import CommandRejectedDTO
from assets.specification_adapters import actor_context_for_user, native_asset_type_create_input, patch_from_mapping
from core.tests.mixins import TenantTestMixin
from extras.api.serializers import CustomFieldSerializer, CustomFieldsetSerializer
from extras.models import CustomField, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField
from itambox.api.viewsets import ITAMBoxModelViewSet
from itambox.views.generic import ObjectDeleteView, ObjectEditView


class CustomFieldAPISerializerContractTests(TenantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.setup_tenant_context(
            name="API serializer contract tenant",
            slug="api-serializer-contract",
            permissions=["assets.change_asset"],
        )
        self.enterContext(self.tenant_context(self.tenant))
        self.asset_ct = ContentType.objects.get_for_model(Asset)
        self.asset_type_ct = ContentType.objects.get_for_model(AssetType)
        self.choice_set = CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="api-stage-choices",
            label="API Stage Choices",
        )

    @staticmethod
    def _serializer_context(user):
        return {"request": SimpleNamespace(user=user)}

    def _assert_structured_specification_error(self, raised, code, *, field_key=None, status_code=400):
        exception = raised.exception
        self.assertIsInstance(exception, SpecificationCommandAPIException)
        self.assertEqual(exception.status_code, status_code)
        error = exception.detail["error"]
        self.assertEqual(error["code"], "SPECIFICATION_VALIDATION_FAILED")
        self.assertTrue(error["issues"])
        issue = error["issues"][0]
        self.assertEqual(issue["code"], code)
        if field_key is not None:
            self.assertEqual(issue["field_key"], field_key)
        return issue

    def _create_precondition_revision(self, *, model, slug, manufacturer):
        """Mint the create precondition through the canonical preview command.

        Direct serializer callers must serve the final precondition contract:
        ``expected_definition_revision`` comes from a preview of the same
        structural input — exactly the value the HTTP gate demands — instead
        of a serializer-local preview fallback.
        """
        preview = preview_asset_type_create(
            actor=actor_context_for_user(self.tenant_admin),
            native=native_asset_type_create_input({"manufacturer": manufacturer, "model": model, "slug": slug}),
            fieldsets=create_fieldset_selection_from_values(None, omitted=True),
            patch=patch_from_mapping(None),
        )
        if isinstance(preview, CommandRejectedDTO):
            self.fail(f"create preview rejected: {preview.issues}")
        return preview.expected_definition_revision

    def test_definition_api_rejects_invalid_regex(self):
        serializer = CustomFieldSerializer(
            data={
                "name": "api_stage_invalid_regex",
                "label": "API Stage Invalid Regex",
                "field_type": "text",
                "activation": CustomField.ACTIVATION_GLOBAL,
                "regex": "[",
                "object_types": ["asset"],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("regex", serializer.errors)

    def test_definition_api_rejects_unknown_validation_rule(self):
        serializer = CustomFieldSerializer(
            data={
                "name": "api_unknown_validation_rule",
                "label": "API Unknown Validation Rule",
                "field_type": "text",
                "activation": CustomField.ACTIVATION_GLOBAL,
                "validation_rule": "unknown-rule",
                "object_types": ["asset"],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("validation_rule", serializer.errors)

    def test_definition_api_rejects_empty_applicability(self):
        serializer = CustomFieldSerializer(
            data={
                "name": "api_empty_applicability",
                "label": "API Empty Applicability",
                "field_type": "text",
                "activation": CustomField.ACTIVATION_GLOBAL,
                "object_types": [],
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("object_types", serializer.errors)

    def test_fieldset_api_rejects_immutable_identity_change(self):
        fieldset = CustomFieldset.objects.create(namespace="local", slug="immutable-api", label="Immutable API")
        serializer = CustomFieldsetSerializer(
            instance=fieldset,
            data={"namespace": "other"},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("namespace", serializer.errors)

    def test_select_definition_requires_choice_set_and_applicable_object_type(self):
        serializer = CustomFieldSerializer(
            data={
                "name": "api_stage_select",
                "label": "API Stage Select",
                "field_type": "single-select",
                "activation": CustomField.ACTIVATION_GLOBAL,
                "max_values": 1,
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("choice_set", serializer.errors)
        self.assertIn("object_types", serializer.errors)

    def test_object_types_are_the_applicability_authority(self):
        serializer = CustomFieldSerializer(
            data={
                "name": "api_stage_asset_type",
                "label": "API Stage Asset Type",
                "field_type": "text",
                "activation": CustomField.ACTIVATION_GLOBAL,
                "object_types": ["assettype"],
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        field = serializer.save()
        self.assertEqual(field.activation, CustomField.ACTIVATION_GLOBAL)
        self.assertEqual(list(field.object_types.values_list("model", flat=True)), ["assettype"])

    def test_valid_select_definition_accepts_matching_contract(self):
        serializer = CustomFieldSerializer(
            data={
                "name": "api_stage_valid_select",
                "label": "API Stage Valid Select",
                "field_type": "single-select",
                "activation": CustomField.ACTIVATION_GLOBAL,
                "max_values": 1,
                "choice_set": self.choice_set.pk,
                "object_types": ["asset"],
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        field = serializer.save()
        self.assertEqual(field.choice_set_id, self.choice_set.pk)
        self.assertEqual(set(field.object_types.values_list("pk", flat=True)), {self.asset_ct.pk})
        self.assertNotIn(self.asset_type_ct.pk, field.object_types.values_list("pk", flat=True))

    def test_api_update_rejects_core_definition(self):
        field = CustomField.objects.create(
            name="api_managed_update",
            label="API Managed Update",
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_LOCAL,
        )
        view = ITAMBoxModelViewSet()
        view.serializer_class = CustomFieldSerializer
        view.queryset = CustomField.objects.all()
        view.request = SimpleNamespace(
            user=SimpleNamespace(is_superuser=True),
            META={"HTTP_IF_MATCH": f"W/{json.dumps(field.updated_at.isoformat())}"},
            query_params={},
        )
        serializer = SimpleNamespace(instance=field, validated_data={})
        serializer.save = lambda **kwargs: field

        original_guard = view._ensure_unmanaged_definition

        def transition_after_initial_check(instance):
            CustomField.objects.filter(pk=instance.pk).update(management_kind=CustomField.MANAGEMENT_CORE)
            return original_guard(instance)

        with patch.object(view, "_ensure_unmanaged_definition", side_effect=transition_after_initial_check) as guard:
            with self.assertRaises(PermissionDenied):
                view.perform_update(serializer)

        self.assertEqual(guard.call_count, 2)
        field.refresh_from_db()
        self.assertEqual(field.management_kind, CustomField.MANAGEMENT_CORE)

    def test_api_delete_rejects_library_definition(self):
        field = CustomField.objects.create(
            name="api_managed_delete",
            label="API Managed Delete",
            namespace="api-contract",
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_LIBRARY,
        )
        view = ITAMBoxModelViewSet()
        view.serializer_class = CustomFieldSerializer
        view.queryset = CustomField.objects.all()
        view.request = SimpleNamespace(
            user=SimpleNamespace(is_superuser=True),
            META={"HTTP_IF_MATCH": f"W/{json.dumps(field.updated_at.isoformat())}"},
            query_params={},
        )

        with self.assertRaises(PermissionDenied):
            view.perform_destroy(field)

        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())
        self.assertEqual(field.lifecycle, CustomField.LIFECYCLE_ACTIVE)

    def test_api_delete_rechecks_locked_managed_definition(self):
        field = CustomField.objects.create(
            name="api_locked_delete",
            label="API Locked Delete",
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_LOCAL,
        )
        view = ITAMBoxModelViewSet()
        view.serializer_class = CustomFieldSerializer
        view.queryset = CustomField.objects.all()
        view.request = SimpleNamespace(
            user=SimpleNamespace(is_superuser=True),
            META={"HTTP_IF_MATCH": f"W/{json.dumps(field.updated_at.isoformat())}"},
            query_params={},
        )
        original_guard = view._ensure_unmanaged_definition

        def transition_after_initial_check(instance):
            CustomField.objects.filter(pk=instance.pk).update(management_kind=CustomField.MANAGEMENT_CORE)
            return original_guard(instance)

        with patch.object(view, "_ensure_unmanaged_definition", side_effect=transition_after_initial_check) as guard:
            with self.assertRaises(PermissionDenied):
                view.perform_destroy(field)

        self.assertEqual(guard.call_count, 2)
        field.refresh_from_db()
        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())
        self.assertEqual(field.management_kind, CustomField.MANAGEMENT_CORE)

    def test_html_edit_rechecks_managed_definition_before_save(self):
        from types import SimpleNamespace

        field = CustomField.objects.create(
            name="html_edit_lock",
            label="HTML edit lock",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        CustomField.objects.filter(pk=field.pk).update(management_kind=CustomField.MANAGEMENT_CORE)
        view = ObjectEditView()
        view.model = CustomField
        view.object = field
        view.request = SimpleNamespace(POST={})
        form = SimpleNamespace(
            instance=field,
            cleaned_data={},
            save=lambda: self.fail("managed definition was saved"),
        )

        with self.assertRaises(PermissionDenied):
            view.form_valid(form)

    def test_html_edit_preserves_validated_scalar_values_under_lock(self):
        field = CustomField.objects.create(
            name="html_scalar_edit",
            label="Old label",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        field.object_types.add(self.asset_ct)

        class BoundForm:
            def __init__(self, instance):
                self.instance = instance
                self.cleaned_data = {}

            def save(self):
                self.instance.save()
                return self.instance

        field.label = "New label"
        form = BoundForm(field)
        view = ObjectEditView()
        view.model = CustomField
        view.object = field
        view.request = RequestFactory().post("/", data={"return_url": "/"})
        view.request._messages = CookieStorage(view.request)

        response = view.form_valid(form)

        self.assertEqual(response.status_code, 302)
        field.refresh_from_db()
        self.assertEqual(field.label, "New label")

    def test_html_delete_rechecks_managed_definition_before_delete(self):
        from types import SimpleNamespace

        field = CustomField.objects.create(
            name="html_delete_lock",
            label="HTML delete lock",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        CustomField.objects.filter(pk=field.pk).update(management_kind=CustomField.MANAGEMENT_CORE)
        view = ObjectDeleteView()
        view.model = CustomField
        view.object = field
        view.request = RequestFactory().post("/")
        view.request._messages = CookieStorage(view.request)

        with self.assertRaises(PermissionDenied):
            view.form_valid(SimpleNamespace())

        field.refresh_from_db()
        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())
        self.assertEqual(field.lifecycle, CustomField.LIFECYCLE_ACTIVE)

    def test_asset_type_api_rejects_noncanonical_custom_field_value(self):
        definition = CustomField.objects.create(
            name="api_integer_value",
            label="API Integer Value",
            field_type=CustomField.FIELD_TYPE_INTEGER,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        definition.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="API Manufacturer")
        expected_definition_revision = self._create_precondition_revision(
            model="API Test Type",
            slug="api-test-type",
            manufacturer=manufacturer,
        )

        serializer = AssetTypeSerializer(
            data={
                "model": "API Test Type",
                "slug": "api-test-type",
                "manufacturer_id": manufacturer.pk,
                "expected_definition_revision": expected_definition_revision,
                "specification_patch": {"set": {"api_integer_value": "1.0"}},
            },
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            serializer.save()
        self._assert_structured_specification_error(raised, "INVALID_TYPE", field_key="api_integer_value")
        self.assertFalse(AssetType.all_objects.filter(model="API Test Type").exists())

    def test_required_asset_type_field_is_required_on_rest_create(self):
        field = CustomField.objects.create(
            name="required_rest_spec",
            label="Required REST specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
            required=True,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Required REST Manufacturer")
        expected_definition_revision = self._create_precondition_revision(
            model="Required REST Type",
            slug="required-rest-type",
            manufacturer=manufacturer,
        )
        serializer = AssetTypeSerializer(
            data={
                "model": "Required REST Type",
                "slug": "required-rest-type",
                "manufacturer_id": manufacturer.pk,
                "expected_definition_revision": expected_definition_revision,
            },
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            serializer.save()
        self._assert_structured_specification_error(raised, "REQUIRED_FIELD", field_key="required_rest_spec")
        self.assertFalse(AssetType.all_objects.filter(model="Required REST Type").exists())

    def test_required_asset_type_field_cannot_be_cleared(self):
        field = CustomField.objects.create(
            name="required_clear_spec",
            label="Required clear specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
            required=True,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Required clear Manufacturer")
        asset_type = AssetType.objects.create(
            model="Required clear Type",
            slug="required-clear-type",
            manufacturer=manufacturer,
            custom_field_data={"required_clear_spec": "keep"},
        )
        original_updated_at = asset_type.updated_at
        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"clear": ["required_clear_spec"]}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            serializer.save()
        self._assert_structured_specification_error(raised, "REQUIRED_FIELD", field_key="required_clear_spec")
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, {"required_clear_spec": "keep"})
        self.assertEqual(asset_type.updated_at, original_updated_at)

    def test_asset_type_temperature_rule_patch_rejects_invalid_range(self):
        minimum = CustomField.objects.get(name="operating_temperature_min")
        maximum = CustomField.objects.get(name="operating_temperature_max")
        CustomFieldsetField.objects.filter(custom_field__in=(minimum, maximum)).delete()
        minimum.refresh_from_db()
        maximum.refresh_from_db()
        self.assertEqual(minimum.activation, CustomField.ACTIVATION_COMPOSED)
        self.assertEqual(maximum.activation, CustomField.ACTIVATION_COMPOSED)
        minimum.object_types.add(self.asset_type_ct)
        maximum.object_types.add(self.asset_type_ct)
        self.assertEqual(maximum.validation_rule, "temperature_max_gte_min")
        manufacturer = Manufacturer.objects.create(name="Temperature Manufacturer")
        asset_type = AssetType.objects.create(
            model="Temperature Type",
            slug="temperature-type",
            manufacturer=manufacturer,
        )
        temperature_fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="temperature-rule",
            label="Temperature rule",
        )
        CustomFieldsetField.objects.create(fieldset=temperature_fieldset, custom_field=minimum, position=10)
        CustomFieldsetField.objects.create(fieldset=temperature_fieldset, custom_field=maximum, position=20)
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=temperature_fieldset, position=10)

        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={
                "specification_patch": {
                    "set": {
                        "operating_temperature_min": "10.0",
                        "operating_temperature_max": "5.0",
                    }
                }
            },
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            serializer.save()
        self._assert_structured_specification_error(
            raised,
            "INVALID_RANGE",
            status_code=400,
        )
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, {})

    def test_retired_custom_field_data_write_is_rejected_by_rest(self):
        field = CustomField.objects.create(
            name="roundtrip_spec",
            label="Roundtrip specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Roundtrip Manufacturer")
        asset_type = AssetType.objects.create(
            model="Roundtrip Type",
            slug="roundtrip-type",
            manufacturer=manufacturer,
            custom_field_data={"roundtrip_spec": "keep", "removed_fieldset": "preserve"},
        )

        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"custom_field_data": asset_type.custom_field_data},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("custom_field_data", serializer.errors)

    def test_supported_rest_roundtrip_preserves_historical_values(self):
        field = CustomField.objects.create(
            name="roundtrip_supported_spec",
            label="Supported roundtrip specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Supported Roundtrip Manufacturer")
        asset_type = AssetType.objects.create(
            model="Supported Roundtrip Type",
            slug="supported-roundtrip-type",
            manufacturer=manufacturer,
            custom_field_data={
                "roundtrip_supported_spec": "keep",
                "removed_fieldset": "preserve",
            },
        )

        representation = AssetTypeSerializer(instance=asset_type).data
        self.assertEqual(
            representation["specifications"],
            {"roundtrip_supported_spec": "keep", "removed_fieldset": "preserve"},
        )
        self.assertIn("removed_fieldset", representation["specification_state"]["historical_keys"])

        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"set": {"roundtrip_supported_spec": "updated"}}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        asset_type.refresh_from_db()
        self.assertEqual(
            asset_type.custom_field_data,
            {"roundtrip_supported_spec": "updated", "removed_fieldset": "preserve"},
        )

    def test_deprecated_specification_is_read_only_through_rest(self):
        field = CustomField.objects.create(
            name="deprecated_spec",
            label="Deprecated specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
            lifecycle=CustomField.LIFECYCLE_DEPRECATED,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Deprecated Manufacturer")
        asset_type = AssetType.objects.create(
            model="Deprecated Type",
            slug="deprecated-type",
            manufacturer=manufacturer,
            custom_field_data={"deprecated_spec": "old"},
        )
        original_updated_at = asset_type.updated_at

        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"set": {"deprecated_spec": "new"}}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            serializer.save()
        self._assert_structured_specification_error(raised, "UNKNOWN_FIELD_KEY", field_key="deprecated_spec")
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, {"deprecated_spec": "old"})
        self.assertEqual(asset_type.updated_at, original_updated_at)

    def test_set_and_clear_are_valid_custom_field_names_inside_patch(self):
        for name in ("set", "clear"):
            field = CustomField.objects.create(
                name=name,
                label=name.title(),
                field_type=CustomField.FIELD_TYPE_TEXT,
                activation=CustomField.ACTIVATION_GLOBAL,
            )
            field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Reserved Name Manufacturer")
        asset_type = AssetType.objects.create(
            model="Reserved Name Type",
            slug="reserved-name-type",
            manufacturer=manufacturer,
            custom_field_data={"clear": "remove", "set": "old"},
        )

        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={
                "specification_patch": {
                    "set": {"set": "new"},
                    "clear": ["clear"],
                }
            },
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, {"set": "new"})

    def test_asset_type_api_patch_preserves_values_and_enforces_composition(self):
        visible = CustomField.objects.create(
            name="visible_spec",
            label="Visible specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        outside = CustomField.objects.create(
            name="outside_spec",
            label="Outside specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        visible.object_types.add(self.asset_type_ct)
        outside.object_types.add(self.asset_type_ct)
        fieldset = CustomFieldset.objects.create(namespace="local", slug="api-composed", label="API composed")
        outside_fieldset = CustomFieldset.objects.create(namespace="local", slug="api-outside", label="API outside")
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=visible, position=10)
        CustomFieldsetField.objects.create(fieldset=outside_fieldset, custom_field=outside, position=10)
        manufacturer = Manufacturer.objects.create(name="Composition API Manufacturer")
        asset_type = AssetType.objects.create(
            model="Composition API Type",
            slug="composition-api-type",
            manufacturer=manufacturer,
            custom_field_data={"visible_spec": "old", "legacy_spec": "preserve"},
        )
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=10)

        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"set": {"visible_spec": "new"}}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        asset_type.refresh_from_db()
        self.assertEqual(
            asset_type.custom_field_data,
            {"visible_spec": "new", "legacy_spec": "preserve"},
        )

        clear_serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"clear": ["visible_spec"]}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )
        self.assertTrue(clear_serializer.is_valid(), clear_serializer.errors)
        clear_serializer.save()
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, {"legacy_spec": "preserve"})

        invalid = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"set": {"outside_spec": "must reject"}}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )
        self.assertTrue(invalid.is_valid(), invalid.errors)
        original_data = dict(asset_type.custom_field_data)
        original_updated_at = asset_type.updated_at
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            invalid.save()
        self._assert_structured_specification_error(raised, "UNKNOWN_FIELD_KEY", field_key="outside_spec")
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, original_data)
        self.assertEqual(asset_type.updated_at, original_updated_at)

    def test_asset_type_api_create_accepts_applicable_custom_field_data(self):
        field = CustomField.objects.create(
            name="create_spec",
            label="Create specification",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Create API Maker")
        expected_definition_revision = self._create_precondition_revision(
            model="Create API Model",
            slug="create-api-model",
            manufacturer=manufacturer,
        )
        serializer = AssetTypeSerializer(
            data={
                "model": "Create API Model",
                "slug": "create-api-model",
                "manufacturer_id": manufacturer.pk,
                "expected_definition_revision": expected_definition_revision,
                "specification_patch": {"set": {"create_spec": "created"}},
            },
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        created = serializer.save()
        self.assertEqual(created.custom_field_data, {"create_spec": "created"})

    def test_asset_type_api_create_rejects_fieldset_only_custom_field_data(self):
        field = CustomField.objects.create(
            name="fieldset_only_create_spec",
            label="Fieldset-only create specification",
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        field.object_types.add(self.asset_type_ct)
        fieldset = CustomFieldset.objects.create(namespace="local", slug="create-only-set", label="Create only set")
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=field, position=10)
        manufacturer = Manufacturer.objects.create(name="Fieldset Create API Maker")
        expected_definition_revision = self._create_precondition_revision(
            model="Fieldset Create API Model",
            slug="fieldset-create-api-model",
            manufacturer=manufacturer,
        )
        serializer = AssetTypeSerializer(
            data={
                "model": "Fieldset Create API Model",
                "slug": "fieldset-create-api-model",
                "manufacturer_id": manufacturer.pk,
                "expected_definition_revision": expected_definition_revision,
                "specification_patch": {"set": {"fieldset_only_create_spec": "must reject"}},
            },
            context=self._serializer_context(self.tenant_admin),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(SpecificationCommandAPIException) as raised:
            serializer.save()
        self._assert_structured_specification_error(raised, "UNKNOWN_FIELD_KEY", field_key="fieldset_only_create_spec")
        self.assertFalse(AssetType.all_objects.filter(model="Fieldset Create API Model").exists())

    def test_asset_api_type_switch_uses_new_composition_for_custom_field_patch(self):
        old_field = CustomField.objects.create(
            name="old_asset_spec",
            label="Old asset specification",
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        new_field = CustomField.objects.create(
            name="new_asset_spec",
            label="New asset specification",
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        old_field.object_types.add(self.asset_ct)
        new_field.object_types.add(self.asset_ct)
        old_set = CustomFieldset.objects.create(namespace="local", slug="old-asset-set", label="Old asset set")
        new_set = CustomFieldset.objects.create(namespace="local", slug="new-asset-set", label="New asset set")
        CustomFieldsetField.objects.create(fieldset=old_set, custom_field=old_field, position=10)
        CustomFieldsetField.objects.create(fieldset=new_set, custom_field=new_field, position=10)
        manufacturer = Manufacturer.objects.create(name="Switch API Maker")
        old_type = AssetType.objects.create(manufacturer=manufacturer, model="Old type", slug="old-type")
        new_type = AssetType.objects.create(manufacturer=manufacturer, model="New type", slug="new-type")
        AssetTypeFieldset.objects.create(asset_type=old_type, fieldset=old_set, position=10)
        AssetTypeFieldset.objects.create(asset_type=new_type, fieldset=new_set, position=10)
        with self.tenant_context(self.tenant):
            asset = Asset.objects.create(
                name="Switchable asset",
                tenant=self.tenant,
                asset_type=old_type,
                custom_field_data={},
            )

        serializer = AssetSerializer(
            instance=asset,
            data={
                "asset_type_id": new_type.pk,
                "specification_patch": {"set": {"new_asset_spec": "new"}},
            },
            partial=True,
            context=self._serializer_context(self.tenant_user),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        asset.refresh_from_db()
        self.assertEqual(asset.asset_type_id, new_type.pk)
        self.assertEqual(asset.custom_field_data, {"new_asset_spec": "new"})

    def test_untyped_asset_api_patch_resolves_global_asset_field(self):
        field = CustomField.objects.create(
            name="untyped_asset_spec",
            label="Untyped asset specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        field.object_types.add(self.asset_ct)
        with self.tenant_context(self.tenant):
            asset = Asset.objects.create(
                name="Untyped asset",
                asset_tag="UNTYPED-1",
                tenant=self.tenant,
                custom_field_data={},
            )

        serializer = AssetSerializer(
            instance=asset,
            data={"specification_patch": {"set": {"untyped_asset_spec": "value"}}},
            partial=True,
            context=self._serializer_context(self.tenant_user),
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        asset.refresh_from_db()
        self.assertEqual(asset.custom_field_data, {"untyped_asset_spec": "value"})

    def test_specification_patch_updates_with_one_model_save(self):
        field = CustomField.objects.create(
            name="single_save_spec",
            label="Single save specification",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        field.object_types.add(self.asset_type_ct)
        manufacturer = Manufacturer.objects.create(name="Single Save Manufacturer")
        asset_type = AssetType.objects.create(
            model="Single Save Type",
            slug="single-save-type",
            manufacturer=manufacturer,
            custom_field_data={"single_save_spec": "old"},
        )
        serializer = AssetTypeSerializer(
            instance=asset_type,
            data={"specification_patch": {"set": {"single_save_spec": "new"}}},
            partial=True,
            context=self._serializer_context(self.tenant_admin),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        # The command reloads and locks its owner; spying on the original
        # Python instance misses the real save. Count the persisted owner write.
        with CaptureQueriesContext(connection) as captured:
            serializer.save()

        writes = [query for query in captured.captured_queries if query["sql"].startswith('UPDATE "assets_assettype" ')]
        self.assertEqual(len(writes), 1)
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.custom_field_data, {"single_save_spec": "new"})

    def test_custom_field_bulk_delete_rejects_managed_definition_selection(self):
        user = get_user_model().objects.create_superuser(
            username="bulk-delete-contract-admin",
            email="bulk-delete-contract-admin@example.com",
            password="test-password",
        )
        self.client.force_login(user)
        field = CustomField.objects.create(
            name="bulk_delete_contract_field",
            label="Bulk Delete Contract Field",
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_CORE,
        )

        response = self.client.post(
            reverse("extras:customfield_bulk_delete"),
            data={
                "pk": [str(field.pk)],
                "_confirm": "1",
                "return_url": reverse("extras:customfield_list"),
            },
        )

        self.assertEqual(response.status_code, 403)
        field.refresh_from_db()
        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())
        self.assertEqual(field.lifecycle, CustomField.LIFECYCLE_ACTIVE)

    def test_custom_field_bulk_delete_locks_confirmed_selection(self):
        user = get_user_model().objects.create_superuser(
            username="bulk-lock-contract-admin",
            email="bulk-lock-contract-admin@example.com",
            password="test-password",
        )
        self.client.force_login(user)
        field = CustomField.objects.create(
            name="bulk_lock_contract_field",
            label="Bulk Lock Contract Field",
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_LOCAL,
        )
        statements = []

        def capture(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            response = self.client.post(
                reverse("extras:customfield_bulk_delete"),
                data={
                    "pk": [str(field.pk)],
                    "_confirm": "1",
                    "return_url": reverse("extras:customfield_list"),
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("FOR UPDATE" in sql.upper() for sql in statements))
        field.refresh_from_db()
        self.assertTrue(CustomField.objects.filter(pk=field.pk).exists())
        self.assertEqual(field.lifecycle, CustomField.LIFECYCLE_ACTIVE)

    def test_custom_field_bulk_edit_rejects_managed_definition_fields(self):
        user = get_user_model().objects.create_superuser(
            username="bulk-contract-admin",
            email="bulk-contract-admin@example.com",
            password="test-password",
        )
        self.client.force_login(user)
        field = CustomField.objects.create(
            name="bulk_contract_field",
            label="Bulk Contract Field",
            activation=CustomField.ACTIVATION_GLOBAL,
            management_kind=CustomField.MANAGEMENT_LOCAL,
        )

        response = self.client.post(
            reverse("extras:customfield_bulk_edit"),
            data={
                "pk": [str(field.pk)],
                "_apply": "1",
                "_selected_fields": ["management_kind"],
                "management_kind": CustomField.MANAGEMENT_CORE,
                "return_url": reverse("extras:customfield_list"),
            },
        )

        self.assertEqual(response.status_code, 403)
        field.refresh_from_db()
        self.assertEqual(field.management_kind, CustomField.MANAGEMENT_LOCAL)
