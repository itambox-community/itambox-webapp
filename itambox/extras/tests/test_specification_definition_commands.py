from dataclasses import replace
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase

from core.context import (
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.tests.mixins import grant
from extras.services._definition_command_support import (
    DefinitionCommandError,
    _canonical,
    close_command_error,
    close_integrity_error,
    close_validation_error,
    ensure_actor,
    identity_for,
    issue,
    lock_choice_dependencies,
    lock_field_dependencies,
    lock_one,
    lock_rows,
    map_database_error,
    map_validation_error,
    mapping_values,
    model_kind,
    reload_actor,
    require_positive_id,
    require_revision,
    resolve_content_types,
    resolve_field_identities,
    resource_revision_for_definition,
    validate_choice_identity,
    validate_field_key,
    validate_local_definition,
    validate_namespace,
    validate_qualified_identity,
)
from extras.services.definition_command_contracts import (
    CustomFieldChoiceCreateInputDTO,
    CustomFieldChoiceSetCreateInputDTO,
    CustomFieldChoiceSetUpdateInputDTO,
    CustomFieldChoiceUpdateInputDTO,
    CustomFieldCreateInputDTO,
    CustomFieldsetCreateInputDTO,
    CustomFieldsetUpdateInputDTO,
    CustomFieldUpdateInputDTO,
)
from extras.services.definition_commands import (
    create_custom_field,
    create_custom_field_choice,
    create_custom_field_choice_set,
    create_custom_fieldset,
    deprecate_custom_field,
    deprecate_custom_field_choice,
    deprecate_custom_field_choice_set,
    deprecate_custom_fieldset,
    replace_custom_fieldset_memberships,
    update_custom_field,
    update_custom_field_choice,
    update_custom_field_choice_set,
    update_custom_fieldset,
)
from organization.models import Role, Tenant
from organization.services.access_scope import ActorContextDTO, authentication_revision_for_actor
from organization.services.catalogue_authorization import has_provider_catalogue_permission

_DEFINITION_PERMISSIONS = (
    "extras.add_customfield",
    "extras.change_customfield",
    "extras.add_customfieldset",
    "extras.change_customfieldset",
    "extras.add_customfieldchoiceset",
    "extras.change_customfieldchoiceset",
    "extras.add_customfieldchoice",
    "extras.change_customfieldchoice",
)


def _provider_definition_authority(user):
    provider = Tenant.objects.create(
        name=f"{user.username} Provider",
        slug=f"{user.username}-provider",
        is_provider=True,
    )
    role = Role.objects.create(
        tenant=provider,
        name=f"{user.username} Definition Manager",
        permissions=list(_DEFINITION_PERMISSIONS),
    )
    grant(user, provider, role)
    set_current_tenant(provider)
    set_current_membership(None)
    set_current_tenant_group(None)
    set_current_all_accessible(False)
    return provider, role


class FieldDefinitionCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="definition-admin", password="test-password")
        self.provider, self.role = _provider_definition_authority(self.user)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)
        super().tearDown()

    def actor(self):
        self.user.refresh_from_db()
        return ActorContextDTO(
            actor_id=self.user.pk,
            authentication_revision=authentication_revision_for_actor(self.user),
        )

    def field_input(self):
        return CustomFieldCreateInputDTO(
            namespace="acme",
            local_key="rack_owner",
            label="Rack owner",
            help_text="The operator-owned rack identifier.",
            field_type="text",
            activation="global",
            object_types=("assets.assettype",),
        )

    def composed_field_input(self, local_key="rack_owner", label="Rack owner"):
        return replace(
            self.field_input(),
            local_key=local_key,
            label=label,
            activation="composed",
        )

    def test_create_field_requires_global_permission_and_persists_qualified_key_and_targets(self):
        result = create_custom_field(actor=self.actor(), definition=self.field_input())

        self.assertEqual(result.outcome, "created")
        self.assertEqual(result.identity, "acme/acme__rack_owner")
        self.assertEqual(result.lifecycle, "active")
        self.assertTrue(result.resource_revision)

        from extras.models import CustomField

        field = CustomField.objects.get(pk=result.definition_id)
        self.assertEqual(field.name, "acme__rack_owner")
        self.assertEqual(field.namespace, "acme")
        self.assertEqual(list(field.object_types.values_list("app_label", "model")), [("assets", "assettype")])

    def test_staff_or_tenant_local_authority_without_global_model_permission_is_denied(self):
        from extras.models import CustomField

        self.role.permissions = []
        self.role.save(update_fields=["permissions"])
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

        result = create_custom_field(actor=self.actor(), definition=self.field_input())

        self.assertEqual(result.outcome, "rejected")
        self.assertEqual([issue.code for issue in result.issues], ["OBJECT_UNAVAILABLE"])
        self.assertFalse(CustomField.objects.filter(name="acme__rack_owner").exists())

    def test_stale_update_rejects_without_timestamp_or_label_write(self):
        from extras.models import CustomField

        created = create_custom_field(actor=self.actor(), definition=self.field_input())
        field = CustomField.objects.get(pk=created.definition_id)
        old_updated_at = field.updated_at

        result = update_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision="sha256:stale",
            changes=CustomFieldUpdateInputDTO(label="Changed label"),
        )

        self.assertEqual(result.outcome, "rejected")
        self.assertEqual([issue.code for issue in result.issues], ["STALE_RESOURCE"])
        field.refresh_from_db()
        self.assertEqual(field.label, "Rack owner")
        self.assertEqual(field.updated_at, old_updated_at)

    def test_deprecate_field_is_explicit_and_idempotent(self):
        from extras.models import CustomField

        created = create_custom_field(actor=self.actor(), definition=self.field_input())
        field = CustomField.objects.get(pk=created.definition_id)

        result = deprecate_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision=created.resource_revision,
        )

        self.assertEqual(result.outcome, "changed")
        field.refresh_from_db()
        self.assertEqual(field.lifecycle, CustomField.LIFECYCLE_DEPRECATED)
        self.assertIsNotNone(field.deprecated_at)

        repeated = deprecate_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision=result.resource_revision,
        )
        self.assertEqual(repeated.outcome, "no_op")

    def test_fieldset_creation_and_membership_replacement_preserve_order(self):
        from extras.models import CustomField, CustomFieldsetField

        first = create_custom_field(actor=self.actor(), definition=self.composed_field_input())
        second = create_custom_field(
            actor=self.actor(),
            definition=self.composed_field_input(local_key="rack_location", label="Rack location"),
        )
        fieldset = create_custom_fieldset(
            actor=self.actor(),
            definition=CustomFieldsetCreateInputDTO(
                namespace="acme",
                slug="hardware",
                label="Hardware",
                field_identities=(first.identity,),
            ),
        )

        self.assertEqual(fieldset.outcome, "created")
        self.assertEqual(
            list(
                CustomFieldsetField.objects.filter(fieldset_id=fieldset.definition_id).values_list(
                    "custom_field__name", "position"
                )
            ),
            [("acme__rack_owner", 1)],
        )

        replacement = replace_custom_fieldset_memberships(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            field_identities=(second.identity, first.identity),
            expected_resource_revision=fieldset.resource_revision,
        )

        self.assertEqual(replacement.outcome, "changed")
        self.assertEqual(
            list(
                CustomFieldsetField.objects.filter(fieldset_id=fieldset.definition_id)
                .order_by("position")
                .values_list("custom_field__name", "position")
            ),
            [("acme__rack_location", 1), ("acme__rack_owner", 2)],
        )
        self.assertGreater(replacement.version, fieldset.version)

        stale = replace_custom_fieldset_memberships(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            field_identities=(first.identity,),
            expected_resource_revision=fieldset.resource_revision,
        )
        self.assertEqual(stale.outcome, "rejected")
        self.assertEqual([issue.code for issue in stale.issues], ["STALE_RESOURCE"])
        self.assertEqual(
            list(
                CustomField.objects.filter(pk__in=[first.definition_id, second.definition_id])
                .order_by("name")
                .values_list("name", flat=True)
            ),
            ["acme__rack_location", "acme__rack_owner"],
        )

    def test_choice_set_and_choice_lifecycle_use_global_permissions_and_revisions(self):
        from extras.models import CustomFieldChoice, CustomFieldChoiceSet

        choice_set = create_custom_field_choice_set(
            actor=self.actor(),
            definition=CustomFieldChoiceSetCreateInputDTO(
                namespace="acme",
                slug="ownership",
                label="Ownership",
            ),
        )
        self.assertEqual(choice_set.outcome, "created")

        choice = create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=choice_set.definition_id,
                key="operator",
                label="Operator owned",
                position=1,
            ),
        )
        self.assertEqual(choice.outcome, "created")
        self.assertEqual(choice.identity, "acme/ownership#operator")
        self.assertEqual(CustomFieldChoice.objects.get(pk=choice.definition_id).key, "operator")

        updated_choice = update_custom_field_choice(
            actor=self.actor(),
            choice_id=choice.definition_id,
            expected_resource_revision=choice.resource_revision,
            changes=CustomFieldChoiceUpdateInputDTO(label="Operator-managed"),
        )
        self.assertEqual(updated_choice.outcome, "changed")
        deprecated_choice = deprecate_custom_field_choice(
            actor=self.actor(),
            choice_id=choice.definition_id,
            expected_resource_revision=updated_choice.resource_revision,
        )
        self.assertEqual(deprecated_choice.outcome, "changed")

        choice_set_row = CustomFieldChoiceSet.objects.get(pk=choice_set.definition_id)
        current_choice_set_revision = resource_revision_for_definition(choice_set_row)
        retired = deprecate_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set_row.pk,
            expected_resource_revision=current_choice_set_revision,
        )
        self.assertEqual(retired.outcome, "changed")
        choice_set_row.refresh_from_db()
        self.assertEqual(choice_set_row.lifecycle, CustomFieldChoiceSet.LIFECYCLE_DEPRECATED)

    def test_choice_set_deprecation_rejects_active_select_field_dependency(self):
        choice_set = create_custom_field_choice_set(
            actor=self.actor(),
            definition=CustomFieldChoiceSetCreateInputDTO(
                namespace="acme",
                slug="status",
                label="Status",
            ),
        )
        create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=choice_set.definition_id,
                key="active",
                label="Active",
                position=1,
            ),
        )
        field = create_custom_field(
            actor=self.actor(),
            definition=replace(
                self.field_input(),
                local_key="status",
                label="Status",
                field_type="single-select",
                max_values=1,
                choice_set_id=choice_set.definition_id,
            ),
        )
        self.assertEqual(field.outcome, "created")

        from extras.models import CustomFieldChoiceSet

        current_choice_set_revision = resource_revision_for_definition(
            CustomFieldChoiceSet.objects.get(pk=choice_set.definition_id)
        )
        rejected = deprecate_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision=current_choice_set_revision,
        )

        self.assertEqual(rejected.outcome, "rejected")
        self.assertEqual([issue.code for issue in rejected.issues], ["DEPENDENCY_RETIREMENT"])
        from extras.models import CustomFieldChoiceSet

        self.assertEqual(
            CustomFieldChoiceSet.objects.get(pk=choice_set.definition_id).lifecycle,
            CustomFieldChoiceSet.LIFECYCLE_ACTIVE,
        )


class DefinitionValidatorUnitTests(SimpleTestCase):
    """Table-driven coverage for the pure validation, mapping and guard helpers."""

    def test_actor_and_issue_guards_reject_wrong_types(self):
        actor = ActorContextDTO(actor_id=1, authentication_revision="sha256:revision")
        ensure_actor(actor)
        for invalid in (None, "actor", {"actor_id": 1}):
            with self.assertRaises(TypeError):
                ensure_actor(invalid)

        self.assertEqual(issue("INVALID_TYPE").code, "INVALID_TYPE")
        self.assertEqual(issue("INVALID_TYPE", path=("definition",)).path, ("definition",))
        self.assertEqual(issue("INVALID_TYPE", message_key="contract").message_key, "contract")

    def test_revision_and_identifier_guards(self):
        self.assertEqual(require_revision("sha256:abc"), "sha256:abc")
        for invalid in ("", None, 7, b"sha256:abc", False):
            with self.assertRaises(TypeError):
                require_revision(invalid)
        self.assertEqual(require_positive_id(3, "field_id"), 3)
        for invalid in (0, -1, "3", None, True):
            with self.assertRaises(TypeError):
                require_positive_id(invalid, "field_id")

    def test_namespace_field_key_and_identity_validation(self):
        self.assertIsNone(validate_namespace("acme"))
        self.assertIsNone(validate_namespace("acme-corp"))
        for namespace in ("Acme", "acme_corp", "acme corp", "", "-acme", "acme-", "a" * 63, None):
            with self.assertRaises(DefinitionCommandError) as context:
                validate_namespace(namespace)
            self.assertEqual([item.code for item in context.exception.issues], ["INVALID_TYPE"])
        for reserved in ("itambox", "catalog"):
            with self.assertRaises(DefinitionCommandError) as context:
                validate_namespace(reserved)
            self.assertEqual([item.code for item in context.exception.issues], ["IMMUTABLE_DEFINITION"])

        self.assertEqual(validate_field_key("rack_owner", "acme-corp"), "acme_corp__rack_owner")
        for invalid in ("Rack", "", "_rack", "rack-owner", "rack owner", 7, None, "k" * 65):
            with self.assertRaises(DefinitionCommandError) as context:
                validate_field_key(invalid, "acme")
            self.assertEqual([item.code for item in context.exception.issues], ["INVALID_TYPE"])
        with self.assertRaises(DefinitionCommandError):
            validate_field_key("k" * 60, "a" * 20)

        self.assertEqual(
            validate_qualified_identity("acme/acme__rack_owner", "field_identities"),
            ("acme", "acme__rack_owner"),
        )
        self.assertEqual(
            validate_choice_identity("acme/ownership#operator", "replacement_identity"),
            ("acme", "ownership", "operator"),
        )
        for invalid in ("acme", "/rack", "acme/", "Acme/rack", "", 7, None):
            with self.assertRaises(DefinitionCommandError) as context:
                validate_qualified_identity(invalid, "field_identities")
            self.assertEqual([item.code for item in context.exception.issues], ["INVALID_TYPE"])
        for invalid in ("acme/ownership", "acme/ownership#a#b", "acme/ownership#", "acme/ownership#OPERATOR", "", 5):
            with self.assertRaises(DefinitionCommandError) as context:
                validate_choice_identity(invalid, "replacement_identity")
            self.assertEqual([item.code for item in context.exception.issues], ["INVALID_TYPE"])

    def test_definition_guards_and_error_mapping(self):
        from types import SimpleNamespace

        validate_local_definition(SimpleNamespace(management_kind="local"))
        for definition in (SimpleNamespace(management_kind="library"), SimpleNamespace(management_kind="core")):
            with self.assertRaises(DefinitionCommandError) as context:
                validate_local_definition(definition)
            self.assertEqual([item.code for item in context.exception.issues], ["IMMUTABLE_DEFINITION"])

        with self.assertRaises(TypeError):
            identity_for(object())
        with self.assertRaises(TypeError):
            model_kind(object())

        for identities in ([], {"acme/acme__rack_owner"}, "acme/acme__rack_owner"):
            with self.assertRaises(DefinitionCommandError) as context:
                resolve_field_identities(identities, using="default")
            self.assertEqual([item.code for item in context.exception.issues], ["REFERENCE_CONFLICT"])

        for object_types in (
            (),
            [],
            ("assets",),
            ("assets.assettype.extra",),
            (7,),
        ):
            with self.assertRaises(DefinitionCommandError) as context:
                resolve_content_types(object_types, using="default")
            self.assertEqual([item.code for item in context.exception.issues], ["REFERENCE_CONFLICT"])

        self.assertEqual(_canonical(Decimal("2.500")), "2.500")
        self.assertEqual(_canonical(date(2026, 9, 10)), "2026-09-10")
        self.assertEqual(_canonical((1, 2)), [1, 2])
        self.assertEqual(_canonical([True, None]), [True, None])
        self.assertEqual(_canonical(1.5), 1.5)
        self.assertEqual(_canonical(7), 7)
        self.assertEqual(_canonical(False), False)
        self.assertEqual(_canonical("keep"), "keep")
        self.assertEqual(_canonical({1, 2}), "{1, 2}")
        self.assertEqual(_canonical({"nested": (Decimal("1.0"),)}), {"nested": ["1.0"]})
        self.assertEqual(mapping_values(({"level": 2, "ratio": Decimal("1.50")},)), [{"level": 2, "ratio": "1.50"}])
        self.assertEqual(mapping_values([{"level": 2}]), [{"level": 2}])

        plain = map_validation_error(ValidationError("boom"))
        self.assertEqual([item.code for item in plain], ["REFERENCE_CONFLICT"])
        self.assertEqual([item.path for item in plain], [("definition",)])
        mapped = map_validation_error(ValidationError({"name": ["duplicate"], "label": ["blank"]}))
        self.assertEqual([item.path for item in mapped], [("label",), ("name",)])
        self.assertEqual(
            [item.code for item in map_database_error(IntegrityError("duplicate"))], ["REFERENCE_CONFLICT"]
        )

        from extras.models import CustomField

        placeholder = CustomField(namespace="acme", name="acme__rejected")
        closed = close_validation_error(placeholder, ValidationError("boom"))
        self.assertEqual(closed.outcome, "rejected")
        self.assertEqual([item.code for item in closed.issues], ["REFERENCE_CONFLICT"])
        self.assertEqual(close_integrity_error(placeholder, IntegrityError("duplicate")).outcome, "rejected")
        for code in ("OBJECT_UNAVAILABLE", "INVALID_TYPE"):
            self.assertEqual(
                [item.code for item in close_command_error(placeholder, DefinitionCommandError(issue(code))).issues],
                [code],
            )


class DefinitionLifecycleCommandTests(TestCase):
    """Lifecycle, replacement, rejection and no-write behaviour of the definition commands."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="definition-command-admin", password="test-password")
        self.provider, self.role = _provider_definition_authority(self.user)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)
        super().tearDown()

    def actor(self):
        self.user.refresh_from_db()
        return ActorContextDTO(
            actor_id=self.user.pk, authentication_revision=authentication_revision_for_actor(self.user)
        )

    def field_input(self, local_key="rack_owner", **overrides):
        definition = CustomFieldCreateInputDTO(
            namespace="acme",
            local_key=local_key,
            label="Rack owner",
            object_types=("assets.assettype",),
        )
        return replace(definition, **overrides) if overrides else definition

    def create_field(self, local_key="rack_owner", **overrides):
        result = create_custom_field(actor=self.actor(), definition=self.field_input(local_key, **overrides))
        self.assertEqual(result.outcome, "created", result)
        return result

    def create_fieldset(self, slug="hardware", **overrides):
        definition = CustomFieldsetCreateInputDTO(namespace="acme", slug=slug, label="Hardware")
        result = create_custom_fieldset(
            actor=self.actor(), definition=replace(definition, **overrides) if overrides else definition
        )
        self.assertEqual(result.outcome, "created", result)
        return result

    def create_choice_set(self, slug="ownership", **overrides):
        definition = CustomFieldChoiceSetCreateInputDTO(namespace="acme", slug=slug, label="Ownership")
        result = create_custom_field_choice_set(
            actor=self.actor(),
            definition=replace(definition, **overrides) if overrides else definition,
        )
        self.assertEqual(result.outcome, "created", result)
        return result

    def create_choice(self, choice_set_id, key, position, **overrides):
        definition = CustomFieldChoiceCreateInputDTO(
            choice_set_id=choice_set_id,
            key=key,
            label=key.title(),
            position=position,
        )
        result = create_custom_field_choice(
            actor=self.actor(), definition=replace(definition, **overrides) if overrides else definition
        )
        self.assertEqual(result.outcome, "created", result)
        return result

    def current_revision(self, model, definition_id):
        from django.apps import apps

        return resource_revision_for_definition(apps.get_model(f"extras.{model}").objects.get(pk=definition_id))

    def test_create_field_rejects_malformed_definitions_without_writing(self):
        from extras.models import CustomField

        for definition, expected in (
            (replace(self.field_input(), namespace="itambox"), "IMMUTABLE_DEFINITION"),
            (replace(self.field_input(), namespace="Acme"), "INVALID_TYPE"),
            (replace(self.field_input(), local_key="Rack"), "INVALID_TYPE"),
            (replace(self.field_input(), object_types=()), "REFERENCE_CONFLICT"),
            (replace(self.field_input(), object_types=("assets.assettype", "assets.assettype")), "REFERENCE_CONFLICT"),
            (replace(self.field_input(), object_types=("assets.missingtype",)), "REFERENCE_CONFLICT"),
            (replace(self.field_input(), field_type="unsupported"), "REFERENCE_CONFLICT"),
        ):
            result = create_custom_field(actor=self.actor(), definition=definition)
            self.assertEqual(result.outcome, "rejected", definition)
            self.assertEqual([item.code for item in result.issues], [expected], definition)
            self.assertFalse(CustomField.objects.filter(namespace="acme").exists(), definition)

    def test_create_field_rejects_inactive_choice_set_reference(self):
        from extras.models import CustomField, CustomFieldChoiceSet

        choice_set = self.create_choice_set()
        CustomFieldChoiceSet.objects.filter(pk=choice_set.definition_id).update(
            lifecycle=CustomFieldChoiceSet.LIFECYCLE_DEPRECATED
        )

        result = create_custom_field(
            actor=self.actor(),
            definition=replace(
                self.field_input(local_key="rack_state"),
                field_type="single-select",
                choice_set_id=choice_set.definition_id,
            ),
        )

        self.assertEqual(result.outcome, "rejected")
        self.assertEqual([item.code for item in result.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomField.objects.filter(name="acme__rack_state").exists())

    def test_create_field_with_replacement_requires_an_active_target(self):
        from extras.models import CustomField

        target = self.create_field(local_key="rack_location", label="Rack location")

        created = create_custom_field(
            actor=self.actor(),
            definition=replace(self.field_input(local_key="rack_backup"), replaced_by=target.identity),
        )
        self.assertEqual(created.outcome, "created")
        self.assertEqual(CustomField.objects.get(pk=created.definition_id).replaced_by, target.identity)

        rejected = create_custom_field(
            actor=self.actor(),
            definition=replace(self.field_input(local_key="rack_spare"), replaced_by="acme/acme__missing"),
        )
        self.assertEqual(rejected.outcome, "rejected")
        self.assertEqual([item.code for item in rejected.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomField.objects.filter(name="acme__rack_spare").exists())

    def test_update_field_applies_each_change_and_rejects_stale_inputs(self):
        from extras.models import CustomField

        created = self.create_field()
        field = CustomField.objects.get(pk=created.definition_id)

        no_op = update_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision=created.resource_revision,
            changes=CustomFieldUpdateInputDTO(),
        )
        self.assertEqual(no_op.outcome, "no_op")
        self.assertEqual(no_op.version, created.version)

        revision = created.resource_revision
        for changes in (
            CustomFieldUpdateInputDTO(label="Rack steward"),
            CustomFieldUpdateInputDTO(help_text="Owner of the rack."),
            CustomFieldUpdateInputDTO(required=True),
            CustomFieldUpdateInputDTO(mappings=({"level": 2, "ratio": "1.50", "since": "2026-09-10"},)),
            CustomFieldUpdateInputDTO(activation="composed"),
        ):
            result = update_custom_field(
                actor=self.actor(),
                field_id=field.pk,
                expected_resource_revision=revision,
                changes=changes,
            )
            self.assertEqual(result.outcome, "changed", changes)
            revision = result.resource_revision

        field.refresh_from_db()
        self.assertEqual(field.label, "Rack steward")
        self.assertEqual(field.help_text, "Owner of the rack.")
        self.assertTrue(field.required)
        self.assertEqual(field.mappings, [{"level": 2, "ratio": "1.50", "since": "2026-09-10"}])
        self.assertEqual(field.activation, CustomField.ACTIVATION_COMPOSED)
        self.assertGreater(field.version, created.version)

        unchanged_types = update_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision=revision,
            changes=CustomFieldUpdateInputDTO(object_types=("assets.assettype",)),
        )
        self.assertEqual(unchanged_types.outcome, "no_op")

        changed_types = update_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision=unchanged_types.resource_revision,
            changes=CustomFieldUpdateInputDTO(object_types=("assets.asset",)),
        )
        self.assertEqual(changed_types.outcome, "changed")
        self.assertEqual(
            sorted(CustomField.objects.get(pk=field.pk).object_types.values_list("app_label", "model")),
            [("assets", "asset")],
        )

        stale = update_custom_field(
            actor=self.actor(),
            field_id=field.pk,
            expected_resource_revision=created.resource_revision,
            changes=CustomFieldUpdateInputDTO(label="Should not apply"),
        )
        self.assertEqual(stale.outcome, "rejected")
        self.assertEqual([item.code for item in stale.issues], ["STALE_RESOURCE"])
        self.assertEqual(CustomField.objects.get(pk=field.pk).label, "Rack steward")

    def test_update_field_rejects_unknown_or_duplicated_replacements(self):
        from extras.models import CustomField

        first = self.create_field()
        second = self.create_field(local_key="rack_location", label="Rack location")

        unknown = update_custom_field(
            actor=self.actor(),
            field_id=second.definition_id,
            expected_resource_revision=second.resource_revision,
            changes=CustomFieldUpdateInputDTO(replaced_by="acme/acme__missing"),
        )
        self.assertEqual(unknown.outcome, "rejected")
        self.assertEqual([item.code for item in unknown.issues], ["REFERENCE_CONFLICT"])

        self_reference = update_custom_field(
            actor=self.actor(),
            field_id=second.definition_id,
            expected_resource_revision=second.resource_revision,
            changes=CustomFieldUpdateInputDTO(replaced_by=second.identity),
        )
        self.assertEqual(self_reference.outcome, "rejected")
        self.assertEqual([item.code for item in self_reference.issues], ["REFERENCE_CONFLICT"])
        self.assertIsNone(CustomField.objects.get(pk=second.definition_id).replaced_by)
        self.assertEqual(first.identity, "acme/acme__rack_owner")

    def test_deprecate_field_reports_stale_idempotent_and_conflicting_replacement(self):
        from extras.models import CustomField

        source = self.create_field()
        target = self.create_field(local_key="rack_location", label="Rack location")
        other = self.create_field(local_key="rack_region", label="Rack region")

        stale = deprecate_custom_field(
            actor=self.actor(),
            field_id=source.definition_id,
            expected_resource_revision="sha256:stale",
            replacement_identity=target.identity,
        )
        self.assertEqual(stale.outcome, "rejected")
        self.assertEqual([item.code for item in stale.issues], ["STALE_RESOURCE"])
        self.assertEqual(CustomField.objects.get(pk=source.definition_id).lifecycle, CustomField.LIFECYCLE_ACTIVE)

        changed = deprecate_custom_field(
            actor=self.actor(),
            field_id=source.definition_id,
            expected_resource_revision=self.current_revision("CustomField", source.definition_id),
            replacement_identity=target.identity,
        )
        self.assertEqual(changed.outcome, "changed")
        deprecated = CustomField.objects.get(pk=source.definition_id)
        self.assertEqual(deprecated.lifecycle, CustomField.LIFECYCLE_DEPRECATED)
        self.assertEqual(deprecated.replaced_by, target.identity)
        self.assertIsNotNone(deprecated.deprecated_at)

        repeated = deprecate_custom_field(
            actor=self.actor(),
            field_id=source.definition_id,
            expected_resource_revision=changed.resource_revision,
            replacement_identity=target.identity,
        )
        self.assertEqual(repeated.outcome, "no_op")

        conflicting = deprecate_custom_field(
            actor=self.actor(),
            field_id=source.definition_id,
            expected_resource_revision=changed.resource_revision,
            replacement_identity=other.identity,
        )
        self.assertEqual(conflicting.outcome, "rejected")
        self.assertEqual([item.code for item in conflicting.issues], ["IMMUTABLE_DEFINITION"])
        self.assertEqual(CustomField.objects.get(pk=source.definition_id).replaced_by, target.identity)

    def test_field_replacement_cycles_are_rejected(self):
        from extras.models import CustomField

        first = self.create_field()
        second = self.create_field(local_key="rack_location", label="Rack location")

        linked = update_custom_field(
            actor=self.actor(),
            field_id=second.definition_id,
            expected_resource_revision=second.resource_revision,
            changes=CustomFieldUpdateInputDTO(replaced_by=first.identity),
        )
        self.assertEqual(linked.outcome, "changed")

        cycle = deprecate_custom_field(
            actor=self.actor(),
            field_id=first.definition_id,
            expected_resource_revision=self.current_revision("CustomField", first.definition_id),
            replacement_identity=second.identity,
        )
        self.assertEqual(cycle.outcome, "rejected")
        self.assertEqual([item.code for item in cycle.issues], ["REFERENCE_CONFLICT"])
        unchanged = CustomField.objects.get(pk=first.definition_id)
        self.assertEqual(unchanged.lifecycle, CustomField.LIFECYCLE_ACTIVE)
        self.assertIsNone(unchanged.replaced_by)

    def test_fieldset_update_replacement_and_deprecation_are_explicit(self):
        from extras.models import CustomFieldset

        member = self.create_field(local_key="rack_owner", activation="composed")
        fieldset = self.create_fieldset(field_identities=(member.identity,))
        successor = self.create_fieldset(slug="hardware_v2", label="Hardware v2")

        stale = update_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision="sha256:stale",
            changes=CustomFieldsetUpdateInputDTO(label="Hardware set"),
        )
        self.assertEqual(stale.outcome, "rejected")
        self.assertEqual([item.code for item in stale.issues], ["STALE_RESOURCE"])

        changed = update_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision=fieldset.resource_revision,
            changes=CustomFieldsetUpdateInputDTO(
                label="Hardware set",
                description="Rack hardware fields",
                replaced_by=successor.identity,
            ),
        )
        self.assertEqual(changed.outcome, "changed")
        row = CustomFieldset.objects.get(pk=fieldset.definition_id)
        self.assertEqual(row.label, "Hardware set")
        self.assertEqual(row.description, "Rack hardware fields")
        self.assertEqual(row.replaced_by, successor.identity)

        no_op = update_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision=changed.resource_revision,
            changes=CustomFieldsetUpdateInputDTO(label="Hardware set"),
        )
        self.assertEqual(no_op.outcome, "no_op")

        stale_deprecation = deprecate_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision="sha256:stale",
            replacement_identity=successor.identity,
        )
        self.assertEqual(stale_deprecation.outcome, "rejected")
        self.assertEqual([item.code for item in stale_deprecation.issues], ["STALE_RESOURCE"])

        deprecated = deprecate_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision=self.current_revision("CustomFieldset", fieldset.definition_id),
            replacement_identity=successor.identity,
        )
        self.assertEqual(deprecated.outcome, "changed")

        repeated = deprecate_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision=deprecated.resource_revision,
            replacement_identity=successor.identity,
        )
        self.assertEqual(repeated.outcome, "no_op")

        conflicting = deprecate_custom_fieldset(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            expected_resource_revision=deprecated.resource_revision,
            replacement_identity=member.identity,
        )
        self.assertEqual(conflicting.outcome, "rejected")
        self.assertEqual([item.code for item in conflicting.issues], ["IMMUTABLE_DEFINITION"])

    def test_membership_replacement_rejects_unusable_members_and_preserves_members(self):
        from extras.models import CustomFieldsetField

        composed = self.create_field(local_key="rack_owner", activation="composed")
        global_field = self.create_field(local_key="rack_region", label="Rack region")
        fieldset = self.create_fieldset(field_identities=(composed.identity,))

        for field_identities, expected in (
            (("acme/acme__missing",), "REFERENCE_CONFLICT"),
            ((composed.identity, composed.identity), "REFERENCE_CONFLICT"),
            ((global_field.identity,), "REFERENCE_CONFLICT"),
            ((7,), "INVALID_TYPE"),
        ):
            result = replace_custom_fieldset_memberships(
                actor=self.actor(),
                fieldset_id=fieldset.definition_id,
                field_identities=field_identities,
                expected_resource_revision=self.current_revision("CustomFieldset", fieldset.definition_id),
            )
            self.assertEqual(result.outcome, "rejected", field_identities)
            self.assertEqual([item.code for item in result.issues], [expected], field_identities)
        self.assertEqual(
            list(
                CustomFieldsetField.objects.filter(fieldset_id=fieldset.definition_id).values_list(
                    "custom_field__name", flat=True
                )
            ),
            ["acme__rack_owner"],
        )

        retired = self.create_field(local_key="rack_zone", label="Rack zone", activation="composed")
        deprecate_custom_field(
            actor=self.actor(),
            field_id=retired.definition_id,
            expected_resource_revision=retired.resource_revision,
        )
        rejected = replace_custom_fieldset_memberships(
            actor=self.actor(),
            fieldset_id=fieldset.definition_id,
            field_identities=(retired.identity,),
            expected_resource_revision=self.current_revision("CustomFieldset", fieldset.definition_id),
        )
        self.assertEqual(rejected.outcome, "rejected")
        self.assertEqual([item.code for item in rejected.issues], ["REFERENCE_CONFLICT"])

    def test_choice_set_update_deprecation_and_replacement(self):
        from extras.models import CustomFieldChoiceSet

        choice_set = self.create_choice_set()
        successor = self.create_choice_set(slug="ownership_v2", label="Ownership v2")

        no_op = update_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision=choice_set.resource_revision,
            changes=CustomFieldChoiceSetUpdateInputDTO(),
        )
        self.assertEqual(no_op.outcome, "no_op")

        stale = update_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision="sha256:stale",
            changes=CustomFieldChoiceSetUpdateInputDTO(label="Ownership model"),
        )
        self.assertEqual(stale.outcome, "rejected")
        self.assertEqual([item.code for item in stale.issues], ["STALE_RESOURCE"])

        changed = update_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision=choice_set.resource_revision,
            changes=CustomFieldChoiceSetUpdateInputDTO(label="Ownership model", replaced_by=successor.identity),
        )
        self.assertEqual(changed.outcome, "changed")
        row = CustomFieldChoiceSet.objects.get(pk=choice_set.definition_id)
        self.assertEqual(row.label, "Ownership model")
        self.assertEqual(row.replaced_by, successor.identity)

        stale_deprecation = deprecate_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision="sha256:stale",
            replacement_identity=successor.identity,
        )
        self.assertEqual(stale_deprecation.outcome, "rejected")
        self.assertEqual([item.code for item in stale_deprecation.issues], ["STALE_RESOURCE"])

        deprecated = deprecate_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision=changed.resource_revision,
            replacement_identity=successor.identity,
        )
        self.assertEqual(deprecated.outcome, "changed")
        self.assertEqual(
            CustomFieldChoiceSet.objects.get(pk=choice_set.definition_id).lifecycle,
            CustomFieldChoiceSet.LIFECYCLE_DEPRECATED,
        )

        repeated = deprecate_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision=deprecated.resource_revision,
            replacement_identity=None,
        )
        self.assertEqual(repeated.outcome, "no_op")

        conflicting = deprecate_custom_field_choice_set(
            actor=self.actor(),
            choice_set_id=choice_set.definition_id,
            expected_resource_revision=deprecated.resource_revision,
            replacement_identity="acme/hardware",
        )
        self.assertEqual(conflicting.outcome, "rejected")
        self.assertEqual([item.code for item in conflicting.issues], ["IMMUTABLE_DEFINITION"])

    def test_choice_lifecycle_replacement_cycles_and_guard_paths(self):
        from extras.models import CustomFieldChoice

        choice_set = self.create_choice_set(slug="status", label="Status")
        first = self.create_choice(choice_set.definition_id, "active", 1)
        second = self.create_choice(choice_set.definition_id, "retired", 2)
        third = self.create_choice(choice_set.definition_id, "vendor", 3)

        linked = update_custom_field_choice(
            actor=self.actor(),
            choice_id=second.definition_id,
            expected_resource_revision=second.resource_revision,
            changes=CustomFieldChoiceUpdateInputDTO(replaced_by=first.identity),
        )
        self.assertEqual(linked.outcome, "changed")

        chained = update_custom_field_choice(
            actor=self.actor(),
            choice_id=third.definition_id,
            expected_resource_revision=third.resource_revision,
            changes=CustomFieldChoiceUpdateInputDTO(label="Vendor owned", position=4, replaced_by=second.identity),
        )
        self.assertEqual(chained.outcome, "changed")
        row = CustomFieldChoice.objects.get(pk=third.definition_id)
        self.assertEqual(row.label, "Vendor owned")
        self.assertEqual(row.position, 4)
        self.assertEqual(row.replaced_by, second.identity)

        no_op = update_custom_field_choice(
            actor=self.actor(),
            choice_id=third.definition_id,
            expected_resource_revision=chained.resource_revision,
            changes=CustomFieldChoiceUpdateInputDTO(),
        )
        self.assertEqual(no_op.outcome, "no_op")

        stale = update_custom_field_choice(
            actor=self.actor(),
            choice_id=third.definition_id,
            expected_resource_revision="sha256:stale",
            changes=CustomFieldChoiceUpdateInputDTO(label="Ignored"),
        )
        self.assertEqual(stale.outcome, "rejected")
        self.assertEqual([item.code for item in stale.issues], ["STALE_RESOURCE"])

        unknown_target = update_custom_field_choice(
            actor=self.actor(),
            choice_id=third.definition_id,
            expected_resource_revision=chained.resource_revision,
            changes=CustomFieldChoiceUpdateInputDTO(replaced_by="acme/status#missing"),
        )
        self.assertEqual(unknown_target.outcome, "rejected")
        self.assertEqual([item.code for item in unknown_target.issues], ["REFERENCE_CONFLICT"])

        foreign_owner = update_custom_field_choice(
            actor=self.actor(),
            choice_id=third.definition_id,
            expected_resource_revision=chained.resource_revision,
            changes=CustomFieldChoiceUpdateInputDTO(replaced_by="acme/ownership#active"),
        )
        self.assertEqual(foreign_owner.outcome, "rejected")
        self.assertEqual([item.code for item in foreign_owner.issues], ["REFERENCE_CONFLICT"])

        cycle = deprecate_custom_field_choice(
            actor=self.actor(),
            choice_id=first.definition_id,
            expected_resource_revision=self.current_revision("CustomFieldChoice", first.definition_id),
            replacement_identity=second.identity,
        )
        self.assertEqual(cycle.outcome, "rejected")
        self.assertEqual([item.code for item in cycle.issues], ["REFERENCE_CONFLICT"])
        self.assertIsNone(CustomFieldChoice.objects.get(pk=first.definition_id).replaced_by)

        stale_deprecation = deprecate_custom_field_choice(
            actor=self.actor(),
            choice_id=second.definition_id,
            expected_resource_revision="sha256:stale",
            replacement_identity=third.identity,
        )
        self.assertEqual(stale_deprecation.outcome, "rejected")
        self.assertEqual([item.code for item in stale_deprecation.issues], ["STALE_RESOURCE"])

        deprecated = deprecate_custom_field_choice(
            actor=self.actor(),
            choice_id=second.definition_id,
            expected_resource_revision=linked.resource_revision,
        )
        self.assertEqual(deprecated.outcome, "changed")
        self.assertEqual(
            CustomFieldChoice.objects.get(pk=second.definition_id).lifecycle,
            CustomFieldChoice.LIFECYCLE_DEPRECATED,
        )

        repeated = deprecate_custom_field_choice(
            actor=self.actor(),
            choice_id=second.definition_id,
            expected_resource_revision=deprecated.resource_revision,
        )
        self.assertEqual(repeated.outcome, "no_op")

        conflicting = deprecate_custom_field_choice(
            actor=self.actor(),
            choice_id=second.definition_id,
            expected_resource_revision=deprecated.resource_revision,
            replacement_identity=third.identity,
        )
        self.assertEqual(conflicting.outcome, "rejected")
        self.assertEqual([item.code for item in conflicting.issues], ["IMMUTABLE_DEFINITION"])

    def test_create_choice_rejects_missing_or_inactive_choice_sets(self):
        from extras.models import CustomFieldChoice, CustomFieldChoiceSet

        missing = create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=10**9,
                key="active",
                label="Active",
                position=1,
            ),
        )
        self.assertEqual(missing.outcome, "rejected")
        self.assertEqual([item.code for item in missing.issues], ["OBJECT_UNAVAILABLE"])
        self.assertIsNone(missing.definition_id)

        choice_set = self.create_choice_set(slug="state", label="State")
        CustomFieldChoiceSet.objects.filter(pk=choice_set.definition_id).update(
            lifecycle=CustomFieldChoiceSet.LIFECYCLE_DEPRECATED
        )
        inactive = create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=choice_set.definition_id,
                key="active",
                label="Active",
                position=1,
            ),
        )
        self.assertEqual(inactive.outcome, "rejected")
        self.assertEqual([item.code for item in inactive.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomFieldChoice.objects.filter(key="active").exists())

    def test_create_choice_requires_an_active_in_set_replacement(self):
        from extras.models import CustomFieldChoice

        choice_set = self.create_choice_set(slug="lifecycle", label="Lifecycle")
        target = self.create_choice(choice_set.definition_id, "draft", 1)

        created = create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=choice_set.definition_id,
                key="provisional",
                label="Provisional",
                position=2,
                replaced_by=target.identity,
            ),
        )
        self.assertEqual(created.outcome, "created")
        self.assertEqual(CustomFieldChoice.objects.get(pk=created.definition_id).replaced_by, target.identity)

        rejected = create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=choice_set.definition_id,
                key="ghost",
                label="Ghost",
                position=3,
                replaced_by="acme/lifecycle#missing",
            ),
        )
        self.assertEqual(rejected.outcome, "rejected")
        self.assertEqual([item.code for item in rejected.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomFieldChoice.objects.filter(key="ghost").exists())

    def test_definition_commands_surface_model_validation_errors(self):
        from extras.models import CustomField, CustomFieldChoice, CustomFieldChoiceSet, CustomFieldset

        field = create_custom_field(
            actor=self.actor(),
            definition=replace(self.field_input(local_key="rack_state"), field_type="single-select"),
        )
        self.assertEqual(field.outcome, "rejected")
        self.assertEqual({item.code for item in field.issues}, {"REFERENCE_CONFLICT"})
        self.assertFalse(CustomField.objects.filter(name="acme__rack_state").exists())

        fieldset = create_custom_fieldset(
            actor=self.actor(),
            definition=CustomFieldsetCreateInputDTO(namespace="acme", slug="overlong", label="l" * 201),
        )
        self.assertEqual(fieldset.outcome, "rejected")
        self.assertEqual([item.code for item in fieldset.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomFieldset.objects.filter(slug="overlong").exists())

        choice_set = create_custom_field_choice_set(
            actor=self.actor(),
            definition=CustomFieldChoiceSetCreateInputDTO(namespace="acme", slug="overlong", label="l" * 201),
        )
        self.assertEqual(choice_set.outcome, "rejected")
        self.assertEqual([item.code for item in choice_set.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomFieldChoiceSet.objects.filter(slug="overlong").exists())

        active_set = self.create_choice_set(slug="validated", label="Validated")
        choice = create_custom_field_choice(
            actor=self.actor(),
            definition=CustomFieldChoiceCreateInputDTO(
                choice_set_id=active_set.definition_id,
                key="valid",
                label="l" * 201,
                position=1,
            ),
        )
        self.assertEqual(choice.outcome, "rejected")
        self.assertEqual([item.code for item in choice.issues], ["REFERENCE_CONFLICT"])
        self.assertFalse(CustomFieldChoice.objects.filter(key="valid").exists())

    def test_mappings_are_persisted_with_json_payload(self):
        from extras.models import CustomField

        created = self.create_field()
        result = update_custom_field(
            actor=self.actor(),
            field_id=created.definition_id,
            expected_resource_revision=created.resource_revision,
            changes=CustomFieldUpdateInputDTO(
                mappings=(
                    {
                        "ratio": "2.500",
                        "level": 3,
                        "enabled": True,
                        "score": 1.5,
                        "absent": None,
                        "labels": ["rack", "row"],
                    },
                )
            ),
        )
        self.assertEqual(result.outcome, "changed")
        self.assertEqual(
            CustomField.objects.get(pk=created.definition_id).mappings,
            [
                {
                    "absent": None,
                    "enabled": True,
                    "labels": ["rack", "row"],
                    "level": 3,
                    "ratio": "2.500",
                    "score": 1.5,
                }
            ],
        )

    def test_lock_helpers_and_actor_guards_report_unavailable_objects(self):
        from extras.models import CustomField

        for call in (
            lambda: lock_one(CustomField, 10**9, using="default"),
            lambda: lock_field_dependencies(10**9, using="default"),
            lambda: lock_choice_dependencies(10**9, using="default"),
        ):
            with self.assertRaises(DefinitionCommandError) as context:
                call()
            self.assertEqual([item.code for item in context.exception.issues], ["OBJECT_UNAVAILABLE"])
        self.assertEqual(lock_rows(CustomField, (), using="default"), [])

        choice_set = self.create_choice_set(slug="states", label="States")
        select_field = self.create_field(
            local_key="state",
            field_type="single-select",
            choice_set_id=choice_set.definition_id,
            max_values=1,
        )
        locked = lock_field_dependencies(select_field.definition_id, using="default")
        self.assertEqual(locked.pk, select_field.definition_id)

        actor = self.actor()
        self.assertIsNone(reload_actor(replace(actor, actor_id=10**9)))
        self.assertIsNone(reload_actor(replace(actor, authentication_revision="stale-revision")))
        principal = reload_actor(actor)
        self.assertEqual(principal.pk, self.user.pk)
        self.assertTrue(has_provider_catalogue_permission(principal, "extras.change_customfield"))
        self.assertFalse(has_provider_catalogue_permission(principal, "extras.delete_customfield"))
        self.assertFalse(has_provider_catalogue_permission(principal, "extras.frobnicate_customfield"))
