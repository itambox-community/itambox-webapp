"""Real database behavior for the T09-A locked specification commands."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assets.models.asset import Asset
from assets.models.catalog import AssetType, AssetTypeFieldset, Manufacturer
from assets.models.tagsequence import AssetTagSequence
from assets.services.specifications._command_support import (
    load_effective_definition,
    resource_revision_for_owner,
)
from assets.services.specifications.commands import (
    update_asset_specifications,
    update_asset_type_specifications,
)
from assets.services.specifications.contracts import (
    AssetTypeId,
    CommandRejectedDTO,
    DefinitionRevision,
    DestinationAssetTypeSelectionDTO,
    OwnerChangedDTO,
    OwnerMutationResult,
    OwnerNoOpDTO,
    ResourceRevision,
    SpecificationPatchDTO,
)
from core.context import override_current_tenant_scope
from core.models import ObjectChange
from extras.models import CustomField, CustomFieldset, CustomFieldsetField, Event
from organization.models import Membership, Role, RoleGrant, RoleGrantScope, Tenant
from organization.services.access_scope import (
    AccessScopeResolutionRequestDTO,
    AccessScopeResolvedDTO,
    ActorContextDTO,
    RequestedScopeSelectorDTO,
    ResolvedAccessAuthorizationDTO,
    authentication_revision_for_actor,
    resolve_access_scope,
)

User = get_user_model()


class SpecificationValueCommandTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Value tenant", slug="value-tenant")
        self.other_tenant = Tenant.objects.create(name="Other tenant", slug="other-tenant")
        self.provider = Tenant.objects.create(name="Value provider", slug="value-provider", is_provider=True)
        self.user = User.objects.create_user(username="value-editor")
        membership = Membership.objects.create(user=self.user, tenant=self.tenant)
        role = Role.objects.create(
            tenant=self.tenant,
            name="Value editor",
            permissions=["assets.change_asset"],
        )
        grant = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="T09-A test authorization",
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrantScope.objects.create(
            role_grant=grant,
            scope_type=RoleGrantScope.SCOPE_OWN,
        )
        self.provider_membership = Membership.objects.create(user=self.user, tenant=self.provider)
        self.provider_role = Role.objects.create(
            tenant=self.provider,
            name="Provider value editor",
            permissions=["assets.change_assettype"],
        )
        provider_grant = RoleGrant.objects.create(
            membership=self.provider_membership,
            role=self.provider_role,
            reason="Provider catalogue test authorization",
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrantScope.objects.create(
            role_grant=provider_grant,
            scope_type=RoleGrantScope.SCOPE_OWN,
        )
        self.manufacturer = Manufacturer.objects.create(name="Value maker", slug="value-maker")
        self.type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="Value type",
            slug="value-type",
        )

        AssetTagSequence.objects.create(
            tenant=self.tenant,
            prefix="VALUE-",
            next_value=1,
            zero_padding=6,
        )
        self.asset = Asset.objects.create(
            name="Value asset",
            asset_tag="VALUE-1",
            tenant=self.tenant,
            asset_type=self.type,
        )
        self.asset_type_field = self._field(
            "asset_type_note",
            target=AssetType,
            required=False,
        )
        self.asset_field = self._field(
            "asset_note",
            target=Asset,
            required=False,
        )
        self.asset_type_fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="asset-type-values",
            label="Asset Type values",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        CustomFieldsetField.objects.create(
            fieldset=self.asset_type_fieldset,
            custom_field=self.asset_type_field,
            position=1,
        )
        AssetTypeFieldset.objects.create(
            asset_type=self.type,
            fieldset=self.asset_type_fieldset,
            position=1,
        )
        self.asset_fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="asset-values",
            label="Asset values",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        CustomFieldsetField.objects.create(
            fieldset=self.asset_fieldset,
            custom_field=self.asset_field,
            position=1,
        )
        AssetTypeFieldset.objects.create(
            asset_type=self.type,
            fieldset=self.asset_fieldset,
            position=2,
        )

    def _field(self, name, *, target, required, field_type=CustomField.FIELD_TYPE_TEXT):
        field = CustomField.objects.create(
            name=name,
            namespace="local",
            label=name.replace("_", " ").title(),
            field_type=field_type,
            activation=CustomField.ACTIVATION_COMPOSED,
            required=required,
            management_kind=CustomField.MANAGEMENT_LOCAL,
        )
        field.object_types.add(ContentType.objects.get_for_model(target))
        return field

    def _actor(self, user=None):
        user = user or self.user
        return ActorContextDTO(
            actor_id=user.pk,
            authentication_revision=authentication_revision_for_actor(user),
        )

    def _provider_type_update(
        self,
        *,
        actor: ActorContextDTO,
        asset_type_id: AssetTypeId,
        expected_resource_revision: ResourceRevision,
        expected_definition_revision: DefinitionRevision,
        patch: SpecificationPatchDTO,
    ) -> OwnerMutationResult:
        with override_current_tenant_scope(self.provider, self.provider_membership):
            return update_asset_type_specifications(
                actor=actor,
                asset_type_id=asset_type_id,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                patch=patch,
            )

    def _asset_authorization(self):
        actor = self._actor()
        request = AccessScopeResolutionRequestDTO(
            actor=actor,
            selector=RequestedScopeSelectorDTO(
                mode="tenant",
                tenant_id=self.tenant.pk,
                tenant_group_id=None,
            ),
            operation="update_asset_specifications",
            required_permission="assets.change_asset",
        )
        resolved = resolve_access_scope(request)
        self.assertIsInstance(resolved, AccessScopeResolvedDTO)
        return ResolvedAccessAuthorizationDTO(
            actor=actor,
            request=request,
            initial_scope=resolved.access_scope,
        )

    def _type_plan(self):
        owner = AssetType.all_objects.get(pk=self.type.pk)
        definition, _definitions = load_effective_definition(
            owner.pk,
            "asset_type",
            tuple(owner.custom_field_data),
        )
        return resource_revision_for_owner(owner), definition.revision

    def _asset_plan(self, *, destination_type_id=None):
        owner = Asset._base_manager.get(pk=self.asset.pk)
        destination = owner.asset_type_id if destination_type_id is None else destination_type_id
        definition, _definitions = load_effective_definition(
            destination,
            "asset",
            tuple(owner.custom_field_data),
        )
        return resource_revision_for_owner(owner), definition.revision

    def _changes(self, model, pk):
        content_type = ContentType.objects.get_for_model(model)
        return ObjectChange._base_manager.filter(
            changed_object_type=content_type,
            changed_object_id=pk,
        )

    def test_patch_values_are_recursively_immutable_and_detached(self):
        source = {"choices": ["a"], "nested": {"flag": False}}
        specification_patch = SpecificationPatchDTO(set_values=source, clear_keys=())
        source["choices"].append("b")
        source["nested"]["flag"] = True
        self.assertEqual(specification_patch.set_values["choices"], ("a",))
        self.assertIs(specification_patch.set_values["nested"]["flag"], False)
        with self.assertRaises(TypeError):
            specification_patch.set_values["nested"]["flag"] = True

    def test_staff_and_tenant_permission_cannot_replace_global_type_permission(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.provider_role.permissions = []
        self.provider_role.save(update_fields=["permissions"])
        role = Role.objects.get(tenant=self.tenant, name="Value editor")
        role.permissions = ["assets.change_asset", "assets.change_assettype"]
        role.save(update_fields=["permissions"])
        resource_revision, definition_revision = self._type_plan()
        result = self._provider_type_update(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "denied"}, clear_keys=()),
        )
        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIsNone(result.safe_owner)
        self.assertEqual(result.issues[0].code, "OBJECT_UNAVAILABLE")
        self.type.refresh_from_db()
        self.assertEqual(self.type.custom_field_data, {})

    def test_changed_definition_rejects_stale_plan_before_saving(self):
        resource_revision, definition_revision = self._type_plan()
        CustomField.objects.filter(pk=self.asset_type_field.pk).update(label="Changed label")
        result = self._provider_type_update(
            actor=self._actor(),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "denied"}, clear_keys=()),
        )
        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertEqual([issue.code for issue in result.issues], ["STALE_DEFINITION"])
        self.type.refresh_from_db()
        self.assertEqual(self.type.custom_field_data, {})

    def test_type_update_uses_pure_patch_and_attributes_existing_audit(self):
        resource_revision, definition_revision = self._type_plan()
        result = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(
                set_values={"asset_type_note": "type value"},
                clear_keys=(),
            ),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.type.refresh_from_db()
        self.assertEqual(self.type.custom_field_data["asset_type_note"], "type value")
        change = self._changes(AssetType, self.type.pk).order_by("-pk").first()
        self.assertIsNotNone(change)
        self.assertEqual(change.user_id, self.user.pk)
        self.assertIsNone(change.tenant_id)

    def test_no_op_does_not_advance_timestamp_or_audit_or_event(self):
        resource_revision, definition_revision = self._type_plan()
        first = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "same"}, clear_keys=()),
        )
        self.assertIsInstance(first, OwnerChangedDTO)
        self.type.refresh_from_db()
        before_timestamp = self.type.updated_at
        before_changes = self._changes(AssetType, self.type.pk).count()
        before_events = Event.objects.filter(
            object_id=self.type.pk,
            model=ContentType.objects.get_for_model(AssetType),
        ).count()

        no_op = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=first.resource_revision,
            expected_definition_revision=first.definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "same"}, clear_keys=()),
        )

        self.assertIsInstance(no_op, OwnerNoOpDTO)
        self.type.refresh_from_db()
        self.assertEqual(self.type.updated_at, before_timestamp)
        self.assertEqual(self._changes(AssetType, self.type.pk).count(), before_changes)
        self.assertEqual(
            Event.objects.filter(
                object_id=self.type.pk,
                model=ContentType.objects.get_for_model(AssetType),
            ).count(),
            before_events,
        )

    def test_stale_resource_is_rejected_before_value_write(self):
        resource_revision, definition_revision = self._type_plan()
        changed = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "first"}, clear_keys=()),
        )
        self.assertIsInstance(changed, OwnerChangedDTO)
        before = AssetType.all_objects.get(pk=self.type.pk).custom_field_data

        rejected = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "stale"}, clear_keys=()),
        )

        self.assertIsInstance(rejected, CommandRejectedDTO)
        self.assertEqual([item.code for item in rejected.issues], ["STALE_RESOURCE"])
        self.assertEqual(AssetType.all_objects.get(pk=self.type.pk).custom_field_data, before)

    def test_unknown_history_survives_an_ordinary_patch(self):
        AssetType.all_objects.filter(pk=self.type.pk).update(custom_field_data={"legacy_key": "retained"})
        resource_revision, definition_revision = self._type_plan()

        result = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_note": "current"}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.type.refresh_from_db()
        self.assertEqual(
            self.type.custom_field_data,
            {"legacy_key": "retained", "asset_type_note": "current"},
        )

    def test_unknown_history_cannot_be_set_or_cleared(self):
        AssetType.all_objects.filter(pk=self.type.pk).update(custom_field_data={"legacy_key": "retained"})
        resource_revision, definition_revision = self._type_plan()

        for candidate_patch in (
            SpecificationPatchDTO(set_values={"legacy_key": "changed"}, clear_keys=()),
            SpecificationPatchDTO(set_values={}, clear_keys=("legacy_key",)),
        ):
            result = self._provider_type_update(
                actor=self._actor(self.user),
                asset_type_id=self.type.pk,
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=candidate_patch,
            )
            self.assertIsInstance(result, CommandRejectedDTO)
            self.assertEqual([item.code for item in result.issues], ["UNKNOWN_FIELD_KEY"])
            self.assertEqual(
                AssetType.all_objects.get(pk=self.type.pk).custom_field_data,
                {"legacy_key": "retained"},
            )

    def test_inactive_history_cannot_be_set_or_cleared(self):
        AssetType.all_objects.filter(pk=self.type.pk).update(
            custom_field_data={"asset_type_note": "retained"},
        )
        CustomFieldset.objects.filter(pk=self.asset_type_fieldset.pk).update(
            lifecycle=CustomFieldset.LIFECYCLE_DEPRECATED,
        )
        resource_revision, definition_revision = self._type_plan()

        for candidate_patch in (
            SpecificationPatchDTO(set_values={"asset_type_note": "changed"}, clear_keys=()),
            SpecificationPatchDTO(set_values={}, clear_keys=("asset_type_note",)),
        ):
            result = self._provider_type_update(
                actor=self._actor(self.user),
                asset_type_id=self.type.pk,
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=candidate_patch,
            )
            self.assertIsInstance(result, CommandRejectedDTO)
            self.assertEqual([item.code for item in result.issues], ["UNKNOWN_FIELD_KEY"])
            self.assertEqual(
                AssetType.all_objects.get(pk=self.type.pk).custom_field_data,
                {"asset_type_note": "retained"},
            )

    def test_typed_json_scalars_are_not_a_no_op(self):
        boolean_field = self._field(
            "asset_type_flag",
            target=AssetType,
            required=False,
            field_type=CustomField.FIELD_TYPE_BOOLEAN,
        )
        boolean_fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="asset-type-boolean-values",
            label="Asset Type boolean values",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        CustomFieldsetField.objects.create(
            fieldset=boolean_fieldset,
            custom_field=boolean_field,
            position=1,
        )
        AssetTypeFieldset.objects.create(
            asset_type=self.type,
            fieldset=boolean_fieldset,
            position=3,
        )
        AssetType.all_objects.filter(pk=self.type.pk).update(custom_field_data={"asset_type_flag": 0})
        resource_revision, definition_revision = self._type_plan()

        result = self._provider_type_update(
            actor=self._actor(self.user),
            asset_type_id=self.type.pk,
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_type_flag": False}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        value = AssetType.all_objects.get(pk=self.type.pk).custom_field_data["asset_type_flag"]
        self.assertIs(value, False)

    def test_save_validation_error_rolls_back_side_effects(self):
        authorization = self._asset_authorization()
        sequence = AssetTagSequence.all_objects.get(tenant_id=self.tenant.pk, prefix="VALUE-")
        AssetTagSequence.all_objects.filter(pk=sequence.pk).update(next_value=1)
        resource_revision, definition_revision = self._asset_plan()
        before_changes = self._changes(Asset, self.asset.pk).count()
        before_events = Event.objects.filter(
            object_id=self.asset.pk,
            model=ContentType.objects.get_for_model(Asset),
        ).count()
        save_calls = []

        def failing_save(_owner, *args, **kwargs):
            del args, kwargs
            updated = AssetTagSequence._base_manager.filter(pk=sequence.pk).update(next_value=2)
            save_calls.append(updated)
            raise ValidationError({"asset": "forced failure"})

        with patch.object(Asset, "save", new=failing_save):
            result = update_asset_specifications(
                authorization=authorization,
                asset_id=self.asset.pk,
                destination=DestinationAssetTypeSelectionDTO("keep_current", None),
                expected_resource_revision=resource_revision,
                expected_definition_revision=definition_revision,
                patch=SpecificationPatchDTO(set_values={"asset_note": "rejected"}, clear_keys=()),
            )

        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertEqual([item.code for item in result.issues], ["REFERENCE_CONFLICT"])
        self.assertEqual(save_calls, [1])
        self.assertEqual(Asset._base_manager.get(pk=self.asset.pk).custom_field_data, {})
        self.assertEqual(AssetTagSequence.all_objects.get(pk=sequence.pk).next_value, 1)
        self.assertEqual(self._changes(Asset, self.asset.pk).count(), before_changes)
        self.assertEqual(
            Event.objects.filter(
                object_id=self.asset.pk,
                model=ContentType.objects.get_for_model(Asset),
            ).count(),
            before_events,
        )

    def test_asset_scope_reauthorization_hides_asset_moved_to_other_tenant(self):
        authorization = self._asset_authorization()
        resource_revision, definition_revision = self._asset_plan()
        Asset._base_manager.filter(pk=self.asset.pk).update(tenant_id=self.other_tenant.pk)

        result = update_asset_specifications(
            authorization=authorization,
            asset_id=self.asset.pk,
            destination=DestinationAssetTypeSelectionDTO("keep_current", None),
            expected_resource_revision=resource_revision,
            expected_definition_revision=definition_revision,
            patch=SpecificationPatchDTO(set_values={"asset_note": "blocked"}, clear_keys=()),
        )

        self.assertIsInstance(result, CommandRejectedDTO)
        self.assertIsNone(result.safe_owner)
        self.assertEqual([item.code for item in result.issues], ["OBJECT_UNAVAILABLE"])
        self.assertEqual(Asset._base_manager.get(pk=self.asset.pk).custom_field_data, {})

    def test_asset_type_switch_does_not_copy_type_values(self):
        source_type = self.type
        source_type.custom_field_data = {"asset_type_note": "not an asset value"}
        source_type.save(update_fields=["custom_field_data"])
        destination_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="Destination type",
            slug="destination-type",
        )
        destination_fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="destination-values",
            label="Destination values",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
        )
        CustomFieldsetField.objects.create(
            fieldset=destination_fieldset,
            custom_field=self.asset_field,
            position=1,
        )
        AssetTypeFieldset.objects.create(
            asset_type=destination_type,
            fieldset=destination_fieldset,
            position=1,
        )
        resource_revision, definition_revision = self._asset_plan(
            destination_type_id=destination_type.pk,
        )
        # The expected definition for this operation is the destination
        # definition; obtain the asset's current revision separately.
        owner = Asset._base_manager.get(pk=self.asset.pk)
        destination_definition, _ = load_effective_definition(
            destination_type.pk,
            "asset",
            tuple(owner.custom_field_data),
        )
        result = update_asset_specifications(
            authorization=self._asset_authorization(),
            asset_id=self.asset.pk,
            destination=DestinationAssetTypeSelectionDTO("replace", destination_type.pk),
            expected_resource_revision=resource_revision,
            expected_definition_revision=destination_definition.revision,
            patch=SpecificationPatchDTO(set_values={"asset_note": "asset value"}, clear_keys=()),
        )

        self.assertIsInstance(result, OwnerChangedDTO)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.asset_type_id, destination_type.pk)
        self.assertEqual(self.asset.custom_field_data, {"asset_note": "asset value"})
        self.assertNotIn("asset_type_note", self.asset.custom_field_data)
