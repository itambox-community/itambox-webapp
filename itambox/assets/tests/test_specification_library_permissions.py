"""Focused real-ORM permission contract tests for Type Library commands.

These tests deliberately create the pending manage capability locally because the
parent owns its model Meta/migration/audit registration. They are collection-safe
on this worker and are intended for the parent's migrated execution lane.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from assets.models.catalog import AssetType, Category, Manufacturer
from assets.services.type_library.application import LibraryApplyError, LibraryApplyRequest
from assets.services.type_library.commands import (
    LibraryCommandError,
    apply_library,
    export_library,
    preview_library,
)
from assets.tests.test_type_library_validation import _release_document
from core.context import (
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.models import ObjectChange
from core.tests.mixins import grant
from extras.models import (
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldset,
    SpecificationLibrary,
    SpecificationLibraryRelease,
)
from organization.models import Role, Tenant, TenantGroup
from organization.services.access_scope import authentication_revision_for_actor

_MANAGE_PERMISSION = "manage_specification_library"
_ACTION_MODELS = (
    AssetType,
    Category,
    Manufacturer,
    CustomField,
    CustomFieldset,
    CustomFieldChoiceSet,
    CustomFieldChoice,
)


class LibraryPermissionContractTests(TestCase):
    def setUp(self):
        self.signing_key = b"library-permission-contract-test-key"
        self.document = _release_document()
        self.provider = Tenant.objects.create(name="Library Provider", slug="library-provider", is_provider=True)
        set_current_tenant(self.provider)
        set_current_membership(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)

    def tearDown(self):
        set_current_tenant(None)
        set_current_membership(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)
        super().tearDown()

    @staticmethod
    def _raw(document):
        return json.dumps(document, ensure_ascii=False)

    def _grant_permissions(self, actor, permissions, name):
        role = Role.objects.create(tenant=self.provider, name=name, permissions=list(permissions))
        grant(actor, self.provider, role)
        return role

    def _grant_manage(self, actor):
        return self._grant_permissions(
            actor,
            (f"extras.{_MANAGE_PERMISSION}",),
            f"{actor.username} library manager",
        )

    def _grant_actions(self, actor):
        permissions = (
            f"{model._meta.app_label}.{action}_{model._meta.model_name}"
            for model in _ACTION_MODELS
            for action in ("add", "change")
        )
        return self._grant_permissions(actor, permissions, f"{actor.username} library actions")

    def _actor(self, username, *, staff=False):
        return get_user_model().objects.create_user(username=username, is_staff=staff)

    def _preview(self, actor, document=None):
        return preview_library(
            self._raw(document or self.document),
            actor=actor,
            signing_key=self.signing_key,
        )

    def _request(self, actor, preview):
        fresh = get_user_model().objects.get(pk=actor.pk)
        return LibraryApplyRequest(
            plan=preview.plan,
            token=preview.preview_token,
            actor_id=fresh.pk,
            authentication_revision=authentication_revision_for_actor(fresh),
            access_scope_fingerprint=None,
            signing_key=self.signing_key,
        )

    def test_manage_only_cannot_preview_or_apply_plan_model_actions_but_can_export(self):
        actor = self._actor("library-manage-only")
        self._grant_manage(actor)
        with self.assertRaises(LibraryCommandError) as preview_error:
            self._preview(actor)
        self.assertEqual(preview_error.exception.code, "OBJECT_UNAVAILABLE")

        seed = self._actor("library-seed-superuser")
        seed.is_superuser = True
        seed.save(update_fields=["is_superuser"])
        seed_preview = self._preview(seed)
        apply_library(
            self._raw(self.document),
            self._request(seed, seed_preview),
            actor=seed,
        )
        exported = export_library("acme", actor=actor, mode="original_release")
        self.assertEqual(exported.namespace, "acme")
        self.assertEqual(SpecificationLibraryRelease.objects.count(), 1)

    def test_non_superuser_with_manage_and_global_action_set_can_apply_and_export(self):
        actor = self._actor("library-global-actions")
        self._grant_manage(actor)
        self._grant_actions(actor)

        preview = self._preview(actor)
        result = apply_library(
            self._raw(self.document),
            self._request(actor, preview),
            actor=actor,
        )

        self.assertEqual(result.release, 1)
        self.assertTrue(SpecificationLibrary.objects.filter(namespace="acme").exists())
        self.assertEqual(export_library("acme", actor=actor, mode="original_release").namespace, "acme")

    def test_staff_or_direct_wrong_capability_is_not_a_manage_shortcut(self):
        actor = self._actor("library-staff-with-actions", staff=True)
        self._grant_permissions(
            actor,
            (
                f"extras.change_{SpecificationLibrary._meta.model_name}",
                *(
                    f"{model._meta.app_label}.{action}_{model._meta.model_name}"
                    for model in _ACTION_MODELS
                    for action in ("add", "change")
                ),
            ),
            "Wrong library capability",
        )

        with self.assertRaises(LibraryCommandError) as denied:
            self._preview(actor)
        self.assertEqual(denied.exception.code, "OBJECT_UNAVAILABLE")

    def test_revoke_between_preview_and_apply_leaves_rows_and_audit_unchanged(self):
        actor = self._actor("library-revocation")
        self._grant_manage(actor)
        self._grant_actions(actor)
        preview = self._preview(actor)
        baseline_changes = ObjectChange._base_manager.count()
        baseline_libraries = SpecificationLibrary.objects.count()
        baseline_releases = SpecificationLibraryRelease.objects.count()

        actions_role = Role.objects.get(name=f"{actor.username} library actions")
        actions_role.permissions.remove(f"assets.add_{AssetType._meta.model_name}")
        actions_role.save(update_fields=["permissions"])
        with self.assertRaises(LibraryApplyError) as denied:
            apply_library(
                self._raw(self.document),
                self._request(actor, preview),
                actor=actor,
            )
        self.assertEqual(denied.exception.code, "OBJECT_UNAVAILABLE")
        self.assertEqual(ObjectChange._base_manager.count(), baseline_changes)
        self.assertEqual(SpecificationLibrary.objects.count(), baseline_libraries)
        self.assertEqual(SpecificationLibraryRelease.objects.count(), baseline_releases)

    def test_noop_reapply_needs_manage_but_not_model_action_churn(self):
        actor = self._actor("library-noop")
        self._grant_manage(actor)
        self._grant_actions(actor)
        first_preview = self._preview(actor)
        apply_library(
            self._raw(self.document),
            self._request(actor, first_preview),
            actor=actor,
        )
        actions_role = Role.objects.get(name=f"{actor.username} library actions")
        actions_role.permissions = []
        actions_role.save(update_fields=["permissions"])
        baseline_changes = ObjectChange._base_manager.count()
        second_preview = self._preview(actor)
        result = apply_library(
            self._raw(self.document),
            self._request(actor, second_preview),
            actor=actor,
        )
        self.assertTrue(result.no_op)
        self.assertEqual(ObjectChange._base_manager.count(), baseline_changes)

    def test_customer_only_catalogue_grant_does_not_authorize_global_library(self):
        actor = self._actor("library-customer-only")
        customer = Tenant.objects.create(name="Library Customer", slug="library-customer")
        role = Role.objects.create(
            tenant=customer,
            name="Customer Library Manager",
            permissions=[f"extras.{_MANAGE_PERMISSION}"],
        )
        grant(actor, customer, role)

        with self.assertRaises(LibraryCommandError) as denied:
            self._preview(actor)
        self.assertEqual(denied.exception.code, "OBJECT_UNAVAILABLE")

    def test_aggregate_scope_cannot_authorize_even_with_provider_grant(self):
        actor = self._actor("library-aggregate-only")
        self._grant_manage(actor)
        self._grant_actions(actor)
        group = TenantGroup.objects.create(name="Library aggregate")
        self.provider.group = group
        self.provider.save(update_fields=["group"])

        for name, active_group, all_accessible in (
            ("tenant-group", group, False),
            ("all-accessible", None, True),
        ):
            with self.subTest(scope=name):
                set_current_tenant(None)
                set_current_membership(None)
                set_current_tenant_group(active_group)
                set_current_all_accessible(all_accessible)
                with self.assertRaises(LibraryCommandError) as denied:
                    self._preview(actor)
                self.assertEqual(denied.exception.code, "OBJECT_UNAVAILABLE")
