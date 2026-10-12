"""PostgreSQL-backed regressions for explicit specification composition commands."""

from __future__ import annotations

import traceback
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase

from assets.models.asset import Asset
from assets.models.catalog import AssetType, AssetTypeFieldset, Category, CategoryDefaultFieldset, Manufacturer
from assets.models.tagsequence import AssetTagSequence
from assets.services.specifications._command_support import (
    load_effective_definition,
    load_prospective_definition,
    resource_revision_for_owner,
    stale_revision_issues,
)
from assets.services.specifications.commands import set_asset_type_composition, set_category_defaults
from assets.services.specifications.contracts import (
    CommandRejectedDTO,
    DefinitionRevision,
    ExplicitFieldsetSelectionDTO,
    OwnerChangedDTO,
    OwnerNoOpDTO,
    ResourceRevision,
    SpecificationPatchDTO,
)
from core.models import ObjectChange
from core.tests.mixins import TenantTestMixin
from extras.models import CustomField, CustomFieldset, CustomFieldsetField, Event
from organization.models import Role, Tenant
from organization.services.access_scope import ActorContextDTO, authentication_revision_for_actor

User = get_user_model()


class SpecificationCompositionCommandTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="composition-editor")
        self.provider = Tenant.objects.create(
            name="Composition Provider", slug="composition-provider", is_provider=True
        )
        self.provider_role = Role.objects.create(
            tenant=self.provider,
            name="Composition Editor",
            permissions=["assets.change_assettype", "assets.change_category"],
        )
        self.provider_grant = self.grant(self.user, self.provider, self.provider_role)
        self.set_active_tenant(self.provider, self.provider_grant.membership)
        self.manufacturer = Manufacturer.objects.create(name="Composition maker", slug="composition-maker")
        self.category = Category.objects.create(name="Composition category", slug="composition-category")
        self.type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="Composition type",
            slug="composition-type",
            category=self.category,
        )

        self.first_field = self._field("first_note")
        self.shared_field = self._field("shared_note")
        self.second_field = self._field("second_note")
        self.required_field = self._field("required_note", required=True)
        self.asset_field = self._field("asset_note", target=Asset)

        self.first = self._fieldset("first", (self.first_field, self.shared_field))
        self.second = self._fieldset("second", (self.second_field, self.shared_field))
        self.required = self._fieldset("required", (self.required_field,))
        self.asset_only = self._fieldset("asset-only", (self.asset_field,))
        self.deprecated = CustomFieldset.objects.create(
            namespace="local",
            slug="deprecated",
            label="Deprecated",
            lifecycle=CustomFieldset.LIFECYCLE_DEPRECATED,
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        self.empty_fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="empty",
            label="Empty fieldset",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )

        AssetTypeFieldset.objects.create(asset_type=self.type, fieldset=self.first, position=1)
        CategoryDefaultFieldset.objects.create(category=self.category, fieldset=self.first, position=1)
        self.type.custom_field_data = {"first_note": "type value", "legacy_note": "history"}
        self.type.save(update_fields=["custom_field_data"])

        AssetTagSequence.objects.create(
            tenant=None,
            prefix="COMP-",
            next_value=1,
            zero_padding=4,
        )
        self.tenant = Tenant.objects.create(name="Composition tenant", slug="composition-tenant")
        with self.tenant_context(self.tenant):
            self.asset = Asset.objects.create(
                name="Composition asset",
                asset_tag="COMP-1",
                tenant=self.tenant,
                asset_type=self.type,
                custom_field_data={"asset_note": "asset original", "first_note": "asset history"},
            )

    def tearDown(self):
        self.clear_tenant_context()
        super().tearDown()

    def _field(self, name, *, target=AssetType, required=False):
        field = CustomField.objects.create(
            name=name,
            namespace="local",
            label=name.replace("_", " ").title(),
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
            required=required,
            management_kind=CustomField.MANAGEMENT_LOCAL,
        )
        field.object_types.add(ContentType.objects.get_for_model(target))
        return field

    def _fieldset(self, slug, fields):
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug=slug,
            label=slug.replace("-", " ").title(),
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        for position, field in enumerate(fields, start=1):
            CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=field, position=position)
        return fieldset

    def _actor(self):
        return ActorContextDTO(
            actor_id=self.user.pk,
            authentication_revision=authentication_revision_for_actor(self.user),
        )

    def _asset_snapshot(self):
        with self.tenant_context(self.tenant):
            return Asset.all_objects.filter(pk=self.asset.pk).values().get()

    def _type_plan(self, fieldsets=None):
        owner = AssetType.all_objects.get(pk=self.type.pk)
        if fieldsets is not None:
            definition, _definitions, _graph = load_prospective_definition(
                tuple(f"{fieldset.namespace}/{fieldset.slug}" for fieldset in fieldsets),
                "asset_type",
                tuple(owner.custom_field_data),
            )
        else:
            definition, _definitions = load_effective_definition(
                owner.pk,
                "asset_type",
                tuple(owner.custom_field_data),
            )
        return resource_revision_for_owner(owner), definition.revision

    def _category_plan(self):
        owner = Category.all_objects.get(pk=self.category.pk)
        return resource_revision_for_owner(owner)

    def _changes(self, model, pk):
        return ObjectChange._base_manager.filter(
            changed_object_type=ContentType.objects.get_for_model(model),
            changed_object_id=pk,
        )

    def _selection(self, *fieldsets):
        return ExplicitFieldsetSelectionDTO(tuple(f"{fieldset.namespace}/{fieldset.slug}" for fieldset in fieldsets))

    def test_explicit_selection_is_immutable_and_rejects_omission_duplicates_and_malformed_identities(self):
        empty = ExplicitFieldsetSelectionDTO(())
        self.assertEqual(empty.identities, ())
        with self.assertRaises(FrozenInstanceError):
            empty.identities = ("local/first",)
        with self.assertRaises(TypeError):
            ExplicitFieldsetSelectionDTO()
        with self.assertRaises(TypeError):
            ExplicitFieldsetSelectionDTO(None)
        with self.assertRaises(TypeError):
            ExplicitFieldsetSelectionDTO([])
        with self.assertRaises(ValueError):
            ExplicitFieldsetSelectionDTO(("local/first", "local/first"))
        with self.assertRaises(ValueError):
            ExplicitFieldsetSelectionDTO(("not-qualified",))
        with self.assertRaises(ValueError):
            ExplicitFieldsetSelectionDTO(("local/",))
        with self.assertRaises(ValueError):
            ExplicitFieldsetSelectionDTO(("local/first/extra",))

    def test_type_composition_explicit_empty_clears_membership_but_preserves_stored_history(self):
        resource_revision, definition_revision = self._type_plan(())
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=ExplicitFieldsetSelectionDTO(()),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.type.refresh_from_db()
        self.assertEqual(self.type.fieldset_memberships.count(), 0)
        self.assertEqual(self.type.custom_field_data, {"first_note": "type value", "legacy_note": "history"})
        self.assertEqual(result.owner.owner_kind, "asset_type")
        self.assertEqual(result.owner.owner_id, self.type.pk)

    def test_type_composition_persists_requested_order_as_dense_ordinals(self):
        resource_revision, definition_revision = self._type_plan((self.second, self.first))
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.second, self.first),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(
            list(
                AssetTypeFieldset.objects.filter(asset_type=self.type).values_list(
                    "fieldset__namespace", "fieldset__slug", "position"
                )
            ),
            [("local", "second", 1), ("local", "first", 2)],
        )

    def test_duplicate_field_keys_resolve_once_with_ordered_provenance(self):
        resource_revision, definition_revision = self._type_plan((self.first, self.second))
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.first, self.second),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"shared_note": "shared"}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        owner = AssetType.all_objects.get(pk=self.type.pk)
        definition, _definitions = load_effective_definition(
            owner.pk,
            "asset_type",
            tuple(owner.custom_field_data),
        )
        rendered_shared = [
            field for section in definition.rendered_sections for field in section.fields if field.key == "shared_note"
        ]
        self.assertEqual(len(rendered_shared), 1)
        self.assertEqual(
            tuple(str(identity) for identity in rendered_shared[0].contributing_section_identities),
            ("local/first", "local/second"),
        )
        self.assertEqual(owner.custom_field_data["shared_note"], "shared")

    def test_unknown_or_deprecated_fieldset_is_rejected_without_writes(self):
        resource_revision, definition_revision = self._type_plan()
        before_memberships = list(
            AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")
        )
        for selection in (
            ExplicitFieldsetSelectionDTO(("local/does-not-exist",)),
            self._selection(self.deprecated),
        ):
            result = set_asset_type_composition(
                actor=self._actor(),
                asset_type_id=self.type.pk,
                fieldsets=selection,
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
            )
            self.assertIsInstance(result, CommandRejectedDTO)
            self.assertEqual([issue.code for issue in result.issues], ["REFERENCE_CONFLICT"])
            self.assertEqual(
                list(AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")),
                before_memberships,
            )

    def test_asset_only_fieldset_composes_and_renders_only_for_the_asset_target(self):
        resource_revision, definition_revision = self._type_plan((self.asset_only,))
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.asset_only),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(
            list(AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset__slug", "position")),
            [("asset-only", 1)],
        )
        owner = AssetType.all_objects.get(pk=self.type.pk)
        type_definition, _type_definitions = load_effective_definition(
            owner.pk,
            "asset_type",
            tuple(owner.custom_field_data),
        )
        type_keys = [field.key for section in type_definition.rendered_sections for field in section.fields]
        self.assertNotIn("asset_note", type_keys)
        asset_definition, _asset_definitions = load_effective_definition(
            owner.pk,
            "asset",
            tuple(owner.custom_field_data),
        )
        asset_keys = [field.key for section in asset_definition.rendered_sections for field in section.fields]
        self.assertIn("asset_note", asset_keys)

        category_result = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=self._category_plan(),
            fieldsets=self._selection(self.asset_only),
        )
        self.assertIsInstance(category_result, OwnerChangedDTO)
        self.assertEqual(
            list(
                CategoryDefaultFieldset.objects.filter(category=self.category).values_list("fieldset__slug", "position")
            ),
            [("asset-only", 1)],
        )

    def test_mixed_target_fieldset_composes_with_only_its_applicable_members(self):
        mixed_type_field = self._field("mixed_type_note")
        mixed_asset_field = self._field("mixed_asset_note", target=Asset)
        mixed = self._fieldset("mixed", (mixed_type_field, mixed_asset_field))
        resource_revision, definition_revision = self._type_plan((mixed,))
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(mixed),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(
            list(AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset__slug", "position")),
            [("mixed", 1)],
        )
        owner = AssetType.all_objects.get(pk=self.type.pk)
        definition, _definitions = load_effective_definition(
            owner.pk,
            "asset_type",
            tuple(owner.custom_field_data),
        )
        keys = [field.key for section in definition.rendered_sections for field in section.fields]
        self.assertIn("mixed_type_note", keys)
        self.assertNotIn("mixed_asset_note", keys)
        asset_definition, _asset_definitions = load_effective_definition(
            owner.pk,
            "asset",
            tuple(owner.custom_field_data),
        )
        asset_keys = [field.key for section in asset_definition.rendered_sections for field in section.fields]
        self.assertIn("mixed_asset_note", asset_keys)
        self.assertNotIn("mixed_type_note", asset_keys)

    def test_deprecated_field_in_proposed_fieldset_is_rejected(self):
        CustomField.objects.filter(pk=self.second_field.pk).update(lifecycle="deprecated")
        resource_revision, definition_revision = self._type_plan()
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.second),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )
        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertEqual([issue.code for issue in result.issues], ["REFERENCE_CONFLICT"])
        self.assertEqual(
            list(self.type.fieldset_memberships.values_list("fieldset_id", "position")), [(self.first.pk, 1)]
        )

    def test_proposed_required_definition_rejects_combined_patch_atomically(self):
        resource_revision, definition_revision = self._type_plan((self.required,))
        before_values = AssetType.all_objects.get(pk=self.type.pk).custom_field_data
        before_memberships = list(
            AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")
        )

        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.required),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"required_note": ""}, clear_keys=()),
        )

        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIn("REQUIRED_FIELD", [issue.code for issue in result.issues])
        self.assertEqual(AssetType.all_objects.get(pk=self.type.pk).custom_field_data, before_values)
        self.assertEqual(
            list(AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")),
            before_memberships,
        )

    def test_owner_save_failure_rolls_back_membership_and_audit_side_effects(self):
        resource_revision, definition_revision = self._type_plan((self.second,))
        before_memberships = list(
            AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")
        )
        before_changes = self._changes(AssetType, self.type.pk).count()

        def failing_save(_owner, *args, **kwargs):
            del args, kwargs
            raise ValidationError({"asset": "forced failure"})

        with patch.object(AssetType, "save", new=failing_save):
            result = set_asset_type_composition(
                actor=self._actor(),
                asset_type_id=self.type.pk,
                fieldsets=self._selection(self.second),
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
            )

        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertEqual([issue.code for issue in result.issues], ["REFERENCE_CONFLICT"])
        self.assertEqual(
            list(AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")),
            before_memberships,
        )
        self.assertEqual(self._changes(AssetType, self.type.pk).count(), before_changes)

    def test_true_no_op_preserves_membership_ids_timestamp_audit_and_events(self):
        resource_revision, definition_revision = self._type_plan()
        membership_ids = list(
            AssetTypeFieldset.objects.filter(asset_type=self.type).order_by("position").values_list("pk", flat=True)
        )
        self.type.refresh_from_db()
        before_timestamp = self.type.updated_at
        before_changes = self._changes(AssetType, self.type.pk).count()
        before_events = Event.objects.filter(
            object_id=self.type.pk,
            model=ContentType.objects.get_for_model(AssetType),
        ).count()

        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.first),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerNoOpDTO)
        self.type.refresh_from_db()
        self.assertEqual(
            list(
                AssetTypeFieldset.objects.filter(asset_type=self.type).order_by("position").values_list("pk", flat=True)
            ),
            membership_ids,
        )
        self.assertEqual(self.type.updated_at, before_timestamp)
        self.assertEqual(self._changes(AssetType, self.type.pk).count(), before_changes)
        self.assertEqual(
            Event.objects.filter(
                object_id=self.type.pk,
                model=ContentType.objects.get_for_model(AssetType),
            ).count(),
            before_events,
        )

    def test_stale_resource_and_definition_are_rejected_after_locked_reload(self):
        resource_revision, definition_revision = self._type_plan((self.second,))
        AssetType._base_manager.filter(pk=self.type.pk).update(model="Composition type changed")
        stale_resource = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.second),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )
        self.assertIsInstance(stale_resource, CommandRejectedDTO)
        self.assertEqual([issue.code for issue in stale_resource.issues], ["STALE_RESOURCE"])

        resource_revision, definition_revision = self._type_plan((self.second,))
        CustomField.objects.filter(pk=self.second_field.pk).update(label="Changed after plan")
        stale_definition = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.second),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )
        self.assertIsInstance(stale_definition, CommandRejectedDTO)
        self.assertEqual([issue.code for issue in stale_definition.issues], ["STALE_DEFINITION"])

    def test_staff_authority_cannot_replace_global_type_permission(self):
        self.provider_role.permissions = []
        self.provider_role.save(update_fields=["permissions"])
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        resource_revision, definition_revision = self._type_plan()

        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            fieldsets=self._selection(self.second),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )

        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIsNone(result.safe_owner)
        self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")

    def test_category_defaults_change_only_the_category_and_use_category_owner_identity(self):
        resource_revision = self._category_plan()
        before_type_memberships = list(
            AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")
        )
        before_type_values = AssetType.all_objects.get(pk=self.type.pk).custom_field_data

        result = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=resource_revision,
            fieldsets=self._selection(self.second),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(result.owner.owner_kind, "category")
        self.assertEqual(result.owner.owner_id, self.category.pk)
        self.assertEqual(
            list(
                CategoryDefaultFieldset.objects.filter(category=self.category).values_list("fieldset__slug", "position")
            ),
            [("second", 1)],
        )
        self.assertEqual(
            list(AssetTypeFieldset.objects.filter(asset_type=self.type).values_list("fieldset_id", "position")),
            before_type_memberships,
        )
        self.assertEqual(AssetType.all_objects.get(pk=self.type.pk).custom_field_data, before_type_values)

    def test_category_defaults_reject_stale_resource_and_staff_only_authority(self):
        resource_revision = self._category_plan()
        CategoryDefaultFieldset.objects.create(category=self.category, fieldset=self.second, position=2)
        stale = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=resource_revision,
            fieldsets=self._selection(self.first),
        )
        self.assertIsInstance(stale, CommandRejectedDTO)
        self.assertEqual([issue.code for issue in stale.issues], ["STALE_RESOURCE"])

        self.provider_role.permissions = []
        self.provider_role.save(update_fields=["permissions"])
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        current_revision = resource_revision_for_owner(Category.all_objects.get(pk=self.category.pk))
        denied = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=current_revision,
            fieldsets=self._selection(self.second),
        )
        self.assertIsInstance(denied, CommandRejectedDTO)
        self.assertIsNone(denied.safe_owner)
        self.assertEqual(denied.issues[0].code, "OBJECT_UNAVAILABLE")

    def test_category_no_op_preserves_membership_ids_timestamp_and_audit(self):
        resource_revision = self._category_plan()
        membership_ids = list(
            CategoryDefaultFieldset.objects.filter(category=self.category).values_list("pk", flat=True)
        )
        self.category.refresh_from_db()
        before_timestamp = self.category.updated_at
        before_changes = self._changes(Category, self.category.pk).count()

        result = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=resource_revision,
            fieldsets=self._selection(self.first),
        )

        self.assertIsInstance(result, OwnerNoOpDTO)
        self.category.refresh_from_db()
        self.assertEqual(
            list(CategoryDefaultFieldset.objects.filter(category=self.category).values_list("pk", flat=True)),
            membership_ids,
        )
        self.assertEqual(self.category.updated_at, before_timestamp)
        self.assertEqual(self._changes(Category, self.category.pk).count(), before_changes)

    def test_category_permission_is_rechecked_after_actor_revocation(self):
        self.provider_role.permissions.remove("assets.change_category")
        self.provider_role.save(update_fields=["permissions"])
        resource_revision = self._category_plan()

        result = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=resource_revision,
            fieldsets=self._selection(self.second),
        )

        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIsNone(result.safe_owner)
        self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")

    def test_definition_revision_does_not_depend_on_stored_values(self):
        resource_revision, definition_revision = self._type_plan()
        self.type.custom_field_data = {"first_note": "different", "legacy_note": "history"}
        self.type.save(update_fields=["custom_field_data"])
        _resource_after, definition_after = self._type_plan()
        self.assertNotEqual(resource_revision, _resource_after)
        self.assertEqual(definition_revision, definition_after)

    def test_category_defaults_can_include_empty_fieldset(self):
        resource_revision = self._category_plan()
        result = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=resource_revision,
            fieldsets=self._selection(self.empty_fieldset),
        )
        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(
            list(CategoryDefaultFieldset.objects.filter(category=self.category).values_list("fieldset_id", "position")),
            [(self.empty_fieldset.pk, 1)],
        )

    def test_missing_category_is_nondisclosing(self):
        result = set_category_defaults(
            actor=self._actor(),
            category_id=999999,
            expected_resource_revision="sha256:missing",
            fieldsets=ExplicitFieldsetSelectionDTO(()),
        )
        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIsNone(result.safe_owner)
        self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")

    def test_graph_definition_reads_are_owned_by_loader(self):
        resource_revision, definition_revision = self._type_plan((self.second,))
        reads = []

        def check(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("SELECT") and any(
                table in sql
                for table in ('"extras_customfieldset"', '"extras_customfield"', '"extras_customfieldsetfield"')
            ):
                frames = traceback.extract_stack()
                self.assertTrue(
                    any(frame.filename.replace("\\", "/").endswith("specifications/loader.py") for frame in frames),
                    sql,
                )
                reads.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(check):
            result = set_asset_type_composition(
                actor=self._actor(),
                asset_type_id=self.type.pk,
                fieldsets=self._selection(self.second),
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
            )
        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertTrue(reads)

    def test_type_success_is_once_only_actor_attributed_and_never_propagates_to_asset(self):
        resource_revision, definition_revision = self._type_plan((self.required,))
        before_asset = self._asset_snapshot()
        before_audit = self._changes(AssetType, self.type.pk).count()
        queries = []

        def capture(execute, sql, params, many, context):
            queries.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            result = set_asset_type_composition(
                actor=self._actor(),
                asset_type_id=self.type.pk,
                fieldsets=self._selection(self.required),
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=SpecificationPatchDTO(set_values={"required_note": "present"}, clear_keys=()),
            )
        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(self._asset_snapshot(), before_asset)
        self.assertEqual(self._changes(AssetType, self.type.pk).count(), before_audit + 1)
        self.assertEqual(self._changes(AssetType, self.type.pk).latest("pk").user_id, self.user.pk)
        self.assertEqual(sum(sql.startswith('UPDATE "assets_assettype"') for sql in queries), 1)
        self.assertEqual(sum(sql.startswith('INSERT INTO "assets_assettypefieldset"') for sql in queries), 1)
        self.type.refresh_from_db()
        self.assertEqual(
            self.type.custom_field_data,
            {
                "first_note": "type value",
                "legacy_note": "history",
                "required_note": "present",
            },
        )

    def test_category_explicit_empty_is_actor_attributed_and_does_not_propagate(self):
        before_type = AssetType.all_objects.filter(pk=self.type.pk).values().get()
        before_asset = self._asset_snapshot()
        before_memberships = list(self.type.fieldset_memberships.values())
        before_audit = self._changes(Category, self.category.pk).count()
        result = set_category_defaults(
            actor=self._actor(),
            category_id=self.category.pk,
            expected_resource_revision=self._category_plan(),
            fieldsets=ExplicitFieldsetSelectionDTO(()),
        )
        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertFalse(CategoryDefaultFieldset.objects.filter(category=self.category).exists())
        self.assertEqual(AssetType.all_objects.filter(pk=self.type.pk).values().get(), before_type)
        self.assertEqual(self._asset_snapshot(), before_asset)
        self.assertEqual(list(self.type.fieldset_memberships.values()), before_memberships)
        self.assertEqual(self._changes(Category, self.category.pk).count(), before_audit + 1)
        self.assertEqual(self._changes(Category, self.category.pk).latest("pk").user_id, self.user.pk)

    def test_genuine_tenant_role_is_not_global_catalogue_authority(self):
        self.provider_role.permissions = []
        self.provider_role.save(update_fields=["permissions"])
        role = Role.objects.create(
            tenant=self.tenant,
            name="Tenant catalogue editor",
            permissions=[
                "assets.change_assettype",
                "assets.change_category",
            ],
        )
        grant = self.grant(self.user, self.tenant, role)
        self.assertTrue(grant.membership.is_active)
        with self.tenant_context(self.tenant, grant.membership):
            resource_revision, definition_revision = self._type_plan()
            results = [
                set_asset_type_composition(
                    actor=self._actor(),
                    asset_type_id=self.type.pk,
                    fieldsets=self._selection(self.second),
                    expected_resource_revision=resource_revision,
                    expected_definition_revision=definition_revision,
                    patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
                ),
                set_category_defaults(
                    actor=self._actor(),
                    category_id=self.category.pk,
                    expected_resource_revision=self._category_plan(),
                    fieldsets=self._selection(self.second),
                ),
            ]
        for result in results:
            self.assertIsInstance(result, CommandRejectedDTO)
            self.assertIsNone(result.safe_owner)
            self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")

    def test_inactive_actor_is_denied_even_with_global_permissions(self):
        actor = self._actor()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        resource_revision, definition_revision = self._type_plan()
        results = [
            set_asset_type_composition(
                actor=actor,
                asset_type_id=self.type.pk,
                fieldsets=self._selection(self.second),
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
            ),
            set_category_defaults(
                actor=actor,
                category_id=self.category.pk,
                expected_resource_revision=self._category_plan(),
                fieldsets=self._selection(self.second),
            ),
        ]
        for result in results:
            self.assertIsInstance(result, CommandRejectedDTO)
            self.assertIsNone(result.safe_owner)
            self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")

    def test_category_defaults_take_only_the_owner_row_lock(self):
        other = CustomFieldset.objects.create(
            namespace="other-local",
            slug="empty",
            label="Empty",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        CategoryDefaultFieldset.objects.filter(category=self.category).delete()
        CategoryDefaultFieldset.objects.create(category=self.category, fieldset=other, position=1)
        queries = []

        def capture(execute, sql, params, many, context):
            if "FOR UPDATE" in sql:
                queries.append((sql, params))
            return execute(sql, params, many, context)

        revision = self._category_plan()
        with connection.execute_wrapper(capture):
            result = set_category_defaults(
                actor=self._actor(),
                category_id=self.category.pk,
                expected_resource_revision=revision,
                fieldsets=self._selection(self.empty_fieldset),
            )
        self.assertIsInstance(result, OwnerChangedDTO)
        self.assertEqual(len(queries), 1)
        self.assertIn('FROM "assets_category"', queries[0][0])

    def test_missing_type_is_nondisclosing(self):
        result = set_asset_type_composition(
            actor=self._actor(),
            asset_type_id=999999,
            fieldsets=ExplicitFieldsetSelectionDTO(()),
            expected_resource_revision="sha256:missing",
            expected_definition_revision="missing",
            patch=SpecificationPatchDTO(set_values={}, clear_keys=()),
        )
        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIsNone(result.safe_owner)
        self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")


class SpecificationRevisionPrecedenceTests(SimpleTestCase):
    def test_stale_owner_and_stale_definition_are_reported_together(self):
        issues = stale_revision_issues(
            expected_resource_revision="owner-old",
            actual_resource_revision=ResourceRevision("owner-new"),
            expected_definition_revision="definition-old",
            actual_definition_revision=DefinitionRevision("definition-new"),
        )
        self.assertEqual([issue.code for issue in issues], ["STALE_RESOURCE", "STALE_DEFINITION"])

    def test_definition_staleness_requires_matching_owner_revision(self):
        issues = stale_revision_issues(
            expected_resource_revision="owner-current",
            actual_resource_revision=ResourceRevision("owner-current"),
            expected_definition_revision="definition-old",
            actual_definition_revision=DefinitionRevision("definition-new"),
        )
        self.assertEqual([issue.code for issue in issues], ["STALE_DEFINITION"])
