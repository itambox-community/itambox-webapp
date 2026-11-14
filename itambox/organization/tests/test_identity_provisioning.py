"""RED/GREEN contract tests for the #443 organization identity writer."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import organization.services.identity_provisioning as identity_service
from core.auth import MembershipBackend
from core.context import get_current_request_id
from core.identity_provisioning import (
    ExternalIdentityProfile,
    ExternalIdentityProvisioningCommand,
    ProviderStaffIntent,
)
from core.mfa import role_is_privileged
from core.models import ObjectChange
from core.oidc_identity import oidc_sensitive_audit
from core.tasks.context import TaskContext
from extras.models import Event
from organization.models import AssetHolder, Membership, Role, RoleGrant, RoleGrantScope, Tenant, TenantGroup
from organization.services.identity_provisioning import (
    LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS,
    LDAP_DIRECTORY_SYNC_REASON,
    IdentityProvisioningError,
    LDAPDirectoryIdentityCommand,
    OrganizationIdentityProvisioner,
    organization_identity_provisioner,
    provision_ldap_directory_identity,
)
from users.models import GroupMembership, User, UserGroup

CANARY_EMAIL = "canary-email-443@example.invalid"
CANARY_UPN = "canary-upn-443@example.invalid"
CANARY_GROUP = "canary-group-443"
CANARY_ROLE = "canary-role-443"
CANARY_DRIVER = "canary-driver-443"


class IdentityServiceCase(TestCase):
    maxDiff = None

    def setUp(self):
        self.provisioner = OrganizationIdentityProvisioner()
        self.provider = Tenant.objects.create(
            name="Provider 443",
            slug="provider-443",
            is_provider=True,
        )
        self.customer = Tenant.objects.create(
            name="Customer 443",
            slug="customer-443",
            managed_by=self.provider,
        )
        self.user = User.objects.create_user(
            username="identity-443",
            email="identity-443@example.invalid",
            first_name="Canonical",
            last_name="User",
        )

    def profile(self, *, source="OIDC", email=None, upn=None, first_name="External", last_name="Identity"):
        return ExternalIdentityProfile(
            source=source,
            email=email or self.user.email,
            upn=upn or f"{self.user.username}@example.invalid",
            first_name=first_name,
            last_name=last_name,
        )

    def command(self, *, user=None, tenant=None, role="Member", profile=None, provider_staff=None):
        return ExternalIdentityProvisioningCommand(
            user=user or self.user,
            customer_tenant=tenant or self.customer,
            profile=profile or self.profile(),
            customer_role_name=role,
            provider_staff=provider_staff,
        )

    def provider_intent(self, *, role_name="ProviderStaff", provider=None):
        return ProviderStaffIntent(provider_tenant=provider or self.provider, role_name=role_name)

    def role(self, tenant, name="Member", permissions=None, **extra):
        return Role.objects.create(
            tenant=tenant,
            name=name,
            permissions=list(permissions if permissions is not None else ["assets.view_asset"]),
            **extra,
        )

    def organization_fingerprint(self):
        payload = {
            "tenants": list(
                Tenant._base_manager.order_by("pk").values_list("pk", "deleted_at", "is_provider", "managed_by_id")
            ),
            "memberships": list(
                Membership._base_manager.order_by("pk").values_list("pk", "user_id", "tenant_id", "is_active")
            ),
            "roles": list(Role._base_manager.order_by("pk").values_list("pk", "tenant_id", "name", "permissions")),
            "grants": list(
                RoleGrant._base_manager.order_by("pk").values_list(
                    "pk", "membership_id", "user_group_id", "role_id", "granted_by_id", "reason", "valid_until"
                )
            ),
            "scopes": list(
                RoleGrantScope._base_manager.order_by("pk").values_list(
                    "pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id"
                )
            ),
            "holders": list(
                AssetHolder._base_manager.order_by("pk").values_list(
                    "pk", "user_id", "tenant_id", "upn", "email", "first_name", "last_name", "deleted_at"
                )
            ),
            "groups": list(UserGroup._base_manager.order_by("pk").values_list("pk", "tenant_id", "name", "is_active")),
            "group_memberships": list(
                GroupMembership._base_manager.order_by("pk").values_list(
                    "pk", "user_group_id", "membership_id", "source"
                )
            ),
            "users": list(
                User._base_manager.order_by("pk").values_list(
                    "pk", "username", "email", "first_name", "last_name", "is_active"
                )
            ),
            "object_changes": list(
                ObjectChange._base_manager.order_by("pk").values_list(
                    "pk",
                    "tenant_id",
                    "user_id",
                    "request_id",
                    "action",
                    "changed_object_type_id",
                    "changed_object_id",
                    "object_repr",
                    "prechange_data",
                    "postchange_data",
                )
            ),
            "events": list(
                Event._base_manager.order_by("pk").values_list(
                    "pk", "model_id", "object_id", "action", "data", "status"
                )
            ),
        }
        encoded = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), payload

    def queries_for(self, command):
        with CaptureQueriesContext(connection) as captured:
            result = self.provisioner.provision(command)
        return result, list(captured.captured_queries)

    def assert_no_sensitive_log_text(self, records):
        for record in records:
            message = record.getMessage() if hasattr(record, "getMessage") else str(record)
            structured = json.dumps(record.__dict__, default=str, sort_keys=True)
            rendered = logging.Formatter(
                "%(levelname)s %(name)s %(message)s "
                "%(source)s %(reason_code)s %(user_id)s %(tenant_id)s "
                "%(customer_tenant_id)s %(provider_tenant_id)s %(role_id)s "
                "%(holder_id)s %(membership_id)s %(exception_type)s",
                defaults={
                    "source": None,
                    "reason_code": None,
                    "user_id": None,
                    "tenant_id": None,
                    "customer_tenant_id": None,
                    "provider_tenant_id": None,
                    "role_id": None,
                    "holder_id": None,
                    "membership_id": None,
                    "exception_type": None,
                },
            ).format(record)
            for text in (message, structured, rendered):
                self.assertNotIn(CANARY_EMAIL, text)
                self.assertNotIn(CANARY_UPN, text)
                self.assertNotIn(CANARY_GROUP, text)
                self.assertNotIn(CANARY_ROLE, text)
                self.assertNotIn(CANARY_DRIVER, text)
            self.assertNotIn(
                "IntegrityError", message.split("exception_type=")[-1] if "exception_type=" in message else ""
            )


class CustomerLifecycleContractTests(IdentityServiceCase):
    def test_first_customer_command_creates_one_role_membership_holder_grant_and_scope(self):
        result = self.provisioner.provision(self.command())

        self.assertEqual(result.mode, "customer")
        self.assertIsNotNone(result.holder_id)
        self.assertIsNotNone(result.membership_id)
        self.assertIsNotNone(result.role_id)
        self.assertEqual(Membership.objects.filter(user=self.user, tenant=self.customer).count(), 1)
        self.assertEqual(AssetHolder.objects.filter(user=self.user, tenant=self.customer).count(), 1)
        self.assertEqual(Role.objects.filter(pk=result.role_id, tenant=self.customer, name="Member").count(), 1)
        grant = RoleGrant.objects.get(membership_id=result.membership_id, role_id=result.role_id)
        self.assertEqual(grant.granted_by_id, None)
        self.assertEqual(list(grant.scopes.values_list("scope_type", flat=True)), [RoleGrantScope.SCOPE_OWN])

    def test_established_nonprivileged_repeat_has_no_organization_writes(self):
        role = self.role(self.customer, permissions=["assets.view_asset"])
        first = self.provisioner.provision(self.command())
        self.assertEqual(first.role_id, role.pk)
        before_hash, before = self.organization_fingerprint()

        with CaptureQueriesContext(connection) as captured:
            result = self.provisioner.provision(self.command())

        after_hash, after = self.organization_fingerprint()
        self.assertEqual(result.mode, "customer")
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before, after)
        self.assertTrue(
            all(
                q["sql"].lstrip().split(None, 1)[0].upper() in {"SELECT", "SAVEPOINT", "RELEASE"}
                for q in captured.captured_queries
                if q["sql"].lstrip()
            )
        )

    def test_privileged_repeat_refreshes_reason_and_one_day_ttl(self):
        role = self.role(self.customer, name="Member", permissions=["assets.view_asset", "assets.change_asset"])
        first = self.provisioner.provision(self.command())
        grant = RoleGrant.objects.get(membership_id=first.membership_id, role_id=role.pk)
        old_expiry = grant.valid_until
        self.assertTrue(role_is_privileged(role))

        before = timezone.now()
        second = self.provisioner.provision(self.command())
        grant.refresh_from_db()

        self.assertEqual(second.membership_id, first.membership_id)
        self.assertEqual(grant.reason, "OIDC group-claim provisioning")
        self.assertIsNotNone(grant.valid_until)
        self.assertGreater(grant.valid_until, old_expiry)
        self.assertGreaterEqual(grant.valid_until, before + timedelta(hours=23))
        self.assertLessEqual(grant.valid_until, before + timedelta(days=1, minutes=1))

    @override_settings(ITAMBOX_SSO_AUTOCREATE_PRIVILEGED_ROLES=False)
    def test_missing_privileged_role_falls_back_to_member_without_permission_catalog_for_existing_member(self):
        member = self.role(self.customer, name="Member", permissions=["assets.view_asset"])

        result, queries = self.queries_for(self.command(role="Manager"))

        self.assertEqual(result.role_id, member.pk)
        self.assertFalse(Role.objects.filter(tenant=self.customer, name="Manager").exists())
        self.assertFalse(any("auth_permission" in query["sql"].lower() for query in queries))

    def test_missing_role_uses_live_permission_policy_once_and_keeps_description(self):
        result, queries = self.queries_for(self.command(role="Manager"))
        role = Role.objects.get(pk=result.role_id)

        self.assertEqual(role.description, "Auto-provisioned Manager role via OIDC")
        self.assertTrue(role.permissions)
        self.assertEqual(sum("auth_permission" in query["sql"].lower() for query in queries), 1)

        with CaptureQueriesContext(connection) as repeat_queries:
            self.provisioner.provision(self.command(role="Manager"))
        self.assertFalse(any("auth_permission" in query["sql"].lower() for query in repeat_queries.captured_queries))

    def test_caller_user_is_never_created_or_profile_updated(self):
        self.user.first_name = "Original"
        self.user.last_name = "Profile"
        self.user.email = "original-443@example.invalid"
        self.user.save(update_fields=["first_name", "last_name", "email"])
        before = (self.user.pk, self.user.username, self.user.email, self.user.first_name, self.user.last_name)

        self.provisioner.provision(
            self.command(
                profile=self.profile(
                    email="different-443@example.invalid",
                    upn="different-upn-443@example.invalid",
                    first_name="Replacement",
                    last_name="Attempt",
                )
            )
        )

        self.user.refresh_from_db()
        self.assertEqual(
            before,
            (self.user.pk, self.user.username, self.user.email, self.user.first_name, self.user.last_name),
        )


class TenantAndLockContractTests(IdentityServiceCase):
    def test_customer_tenant_sql_is_parameterized_ordered_for_share_and_not_exclusive(self):
        role = self.role(self.customer)
        statements = []

        def wrapper(execute, sql, params, many, context):
            statements.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            self.provisioner.provision(self.command())

        tenant_locks = [
            (sql, params)
            for sql, params in statements
            if "organization_tenant" in sql.lower() and "for share" in sql.lower()
        ]
        self.assertEqual(len(tenant_locks), 1)
        sql, params = tenant_locks[0]
        self.assertIn("ORDER BY", sql.upper())
        self.assertNotIn("FOR UPDATE", sql.upper())
        self.assertNotIn("FOR KEY SHARE", sql.upper())
        self.assertIn("%s", sql)
        self.assertEqual(list(params), [self.customer.pk])
        self.assertEqual(role.pk, Role.objects.get(tenant=self.customer, name="Member").pk)

    def test_provider_tenant_sql_has_exact_sorted_ids_and_liveness_columns(self):
        provider_role = self.role(self.provider, name="ProviderStaff")
        statements = []

        def wrapper(execute, sql, params, many, context):
            statements.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            self.provisioner.provision(self.command(provider_staff=self.provider_intent(role_name=provider_role.name)))

        tenant_locks = [
            (sql, params)
            for sql, params in statements
            if "organization_tenant" in sql.lower() and "for share" in sql.lower()
        ]
        self.assertEqual(len(tenant_locks), 1)
        sql, params = tenant_locks[0]
        self.assertEqual(list(params), sorted([self.customer.pk, self.provider.pk]))
        self.assertIn('SELECT "id", "deleted_at", "is_provider", "managed_by_id"', sql)
        self.assertIn("ORDER BY", sql.upper())
        self.assertIn("FOR SHARE", sql.upper())
        self.assertNotIn("FOR UPDATE", sql.upper())
        self.assertNotIn("FOR KEY SHARE", sql.upper())

    def test_missing_deleted_duplicate_and_contradictory_tenant_sets_fail_before_any_write(self):
        deleted = Tenant.objects.create(name="Deleted 443", slug="deleted-443")
        Tenant._base_manager.filter(pk=deleted.pk).update(deleted_at=timezone.now())
        wrong_provider = Tenant.objects.create(name="Wrong Provider 443", slug="wrong-provider-443", is_provider=True)
        cases = (
            ("missing customer", self.command(tenant=SimpleNamespace(pk=99999991))),
            ("deleted customer", self.command(tenant=deleted)),
            (
                "duplicate customer/provider id",
                self.command(provider_staff=self.provider_intent(provider=self.customer, role_name="Missing")),
            ),
            (
                "contradictory provider relationship",
                self.command(provider_staff=self.provider_intent(provider=wrong_provider, role_name="Missing")),
            ),
        )
        for label, command in cases:
            with self.subTest(label=label):
                before_hash, before = self.organization_fingerprint()
                with self.assertRaises(IdentityProvisioningError):
                    self.provisioner.provision(command)
                after_hash, after = self.organization_fingerprint()
                self.assertEqual(before_hash, after_hash)
                self.assertEqual(before, after)

    def test_ordinary_customer_path_queries_group_membership_zero_times(self):
        self.role(self.customer)
        statements = []

        def wrapper(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            self.provisioner.provision(self.command())

        self.assertEqual(sum("users_groupmembership" in sql.lower() for sql in statements), 0)

    def test_provider_path_uses_one_combined_membership_and_one_query_per_lock_table(self):
        self.role(self.customer)
        self.role(self.provider, name="ProviderStaff")
        Membership.objects.create(user=self.user, tenant=self.customer)
        statements = []

        def wrapper(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            self.provisioner.provision(self.command(provider_staff=self.provider_intent()))

        counts = {
            "tenant": sum("for share" in sql.lower() and "organization_tenant" in sql.lower() for sql in statements),
            "membership": sum(
                "organization_membership" in sql.lower() and "for update" in sql.lower() for sql in statements
            ),
            "group_membership": sum(
                "users_groupmembership" in sql.lower() and "for update" in sql.lower() for sql in statements
            ),
            "grant": sum("organization_rolegrant" in sql.lower() and "for update" in sql.lower() for sql in statements),
            "role": sum(
                'from "organization_role"' in sql.lower() and "for update" in sql.lower() for sql in statements
            ),
            "scope": sum(
                "organization_rolegrantscope" in sql.lower() and "for update" in sql.lower() for sql in statements
            ),
            "holder": sum(
                "organization_assetholder" in sql.lower() and "for update" in sql.lower() for sql in statements
            ),
        }
        self.assertEqual(counts["tenant"], 1)
        self.assertEqual(counts["membership"], 1)
        self.assertEqual(counts["group_membership"], 1)
        self.assertLessEqual(counts["grant"], 1)
        self.assertEqual(counts["role"], 1)
        self.assertLessEqual(counts["scope"], 1)
        self.assertEqual(counts["holder"], 1)

    def test_query_work_does_not_scale_with_unrelated_users_or_managed_tenants(self):
        self.role(self.customer)
        base_result, base_queries = self.queries_for(self.command())
        for index in range(12):
            other = User.objects.create_user(username=f"other-{index}", email=f"other-{index}@example.invalid")
            Membership.objects.create(user=other, tenant=self.customer)
        for index in range(12):
            Tenant.objects.create(
                name=f"Managed {index}",
                slug=f"managed-{index}",
                managed_by=self.provider,
            )

        repeat_result, repeat_queries = self.queries_for(self.command())
        self.assertEqual(base_result.membership_id, repeat_result.membership_id)
        self.assertLessEqual(len(repeat_queries), len(base_queries) + 2)
        self.assertLessEqual(
            sum("organization_membership" in query["sql"].lower() for query in repeat_queries),
            1,
        )


class HolderReconciliationTests(IdentityServiceCase):
    def test_existing_linked_user_wins_over_upn_and_email_candidates(self):
        linked = AssetHolder.objects.create(
            user=self.user,
            tenant=self.customer,
            upn="linked-upn-443@example.invalid",
            email="linked-443@example.invalid",
            first_name="Linked",
            last_name="Holder",
        )
        AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn="incoming-upn-443@example.invalid",
            email="incoming-443@example.invalid",
            first_name="UPN",
            last_name="Candidate",
        )

        result = self.provisioner.provision(
            self.command(
                profile=self.profile(
                    email="incoming-443@example.invalid",
                    upn="incoming-upn-443@example.invalid",
                )
            )
        )

        self.assertEqual(result.holder_id, linked.pk)
        linked.refresh_from_db()
        self.assertEqual(linked.user_id, self.user.pk)

    def test_exact_unlinked_upn_is_linked(self):
        candidate = AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn="exact-upn-443@example.invalid",
            email="old-443@example.invalid",
            first_name="Old",
            last_name="Directory",
        )

        result = self.provisioner.provision(
            self.command(profile=self.profile(email="new-443@example.invalid", upn=candidate.upn))
        )

        self.assertEqual(result.holder_id, candidate.pk)
        candidate.refresh_from_db()
        self.assertEqual(candidate.user_id, self.user.pk)

    def test_email_only_hint_with_different_upn_is_not_adopted_but_grants_continue(self):
        candidate = AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn=CANARY_UPN,
            email=CANARY_EMAIL,
            first_name="Ambiguous",
            last_name="Holder",
        )

        with self.assertLogs("itambox.organization.identity", level="WARNING") as logs:
            result = self.provisioner.provision(
                self.command(
                    profile=self.profile(
                        email=CANARY_EMAIL,
                        upn="different-authoritative-upn-443@example.invalid",
                    )
                )
            )

        self.assertIsNone(result.holder_id)
        candidate.refresh_from_db()
        self.assertIsNone(candidate.user_id)
        self.assertEqual(Membership.objects.filter(pk=result.membership_id).count(), 1)
        self.assertEqual(RoleGrant.objects.filter(membership_id=result.membership_id).count(), 1)
        self.assert_no_sensitive_log_text(logs.records)

    def test_service_membership_creation_does_not_trigger_email_only_autolink_after_upn_rejection(self):
        candidate = AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn="stored-holder-upn-443@example.invalid",
            email=self.user.email,
            first_name="Stored",
            last_name="Holder",
        )

        with self.assertLogs("itambox.organization.identity", level="WARNING") as logs:
            result = self.provisioner.provision(
                self.command(
                    profile=self.profile(
                        email=self.user.email,
                        upn="authoritative-user-upn-443@example.invalid",
                    )
                )
            )

        self.assertIsNone(result.holder_id)
        candidate.refresh_from_db()
        self.assertIsNone(candidate.user_id)
        self.assertEqual(Membership.objects.filter(pk=result.membership_id).count(), 1)
        self.assertEqual(RoleGrant.objects.filter(membership_id=result.membership_id).count(), 1)
        self.assert_no_sensitive_log_text(logs.records)

    def test_service_membership_marker_has_no_asset_holder_signal_query_or_write(self):
        candidate = AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn="signal-query-holder-443@example.invalid",
            email=self.user.email,
            first_name="Signal",
            last_name="Holder",
        )

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                membership, created = identity_service._create_membership(
                    user_id=self.user.pk,
                    tenant_id=self.customer.pk,
                )

        self.assertTrue(created)
        self.assertEqual(membership.user_id, self.user.pk)
        self.assertFalse(any("organization_assetholder" in query["sql"].lower() for query in captured.captured_queries))
        candidate.refresh_from_db()
        self.assertIsNone(candidate.user_id)

    def test_linked_other_user_is_never_stolen(self):
        other = User.objects.create_user(username="other-holder-443", email="other-holder-443@example.invalid")
        candidate = AssetHolder.objects.create(
            user=other,
            tenant=self.customer,
            upn=CANARY_UPN,
            email=CANARY_EMAIL,
            first_name="Other",
            last_name="Owner",
        )

        result = self.provisioner.provision(self.command(profile=self.profile(email=CANARY_EMAIL, upn=CANARY_UPN)))

        self.assertIsNone(result.holder_id)
        candidate.refresh_from_db()
        self.assertEqual(candidate.user_id, other.pk)

    def test_holder_integrity_conflict_rereads_only_user_or_upn_and_links_same_upn(self):
        exact = AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn="race-upn-443@example.invalid",
            email="race-443@example.invalid",
            first_name="Race",
            last_name="Holder",
        )
        real_save = AssetHolder.save
        calls = {"count": 0}

        def conflict_once(instance, *args, **kwargs):
            if instance.pk == exact.pk and calls["count"] == 0:
                calls["count"] += 1
                raise IntegrityError(CANARY_DRIVER)
            return real_save(instance, *args, **kwargs)

        with mock.patch.object(AssetHolder, "save", new=conflict_once):
            result = self.provisioner.provision(
                self.command(profile=self.profile(email="race-443@example.invalid", upn=exact.upn))
            )

        exact.refresh_from_db()
        self.assertEqual(result.holder_id, exact.pk)
        self.assertEqual(exact.user_id, self.user.pk)


class GrantConvergenceTests(IdentityServiceCase):
    def test_duplicate_same_role_own_grants_converge_without_moving_unique_scopes(self):
        role = self.role(self.customer)
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        first = RoleGrant.objects.create(membership=membership, role=role)
        second = RoleGrant.objects.create(membership=membership, role=role)
        RoleGrantScope.objects.create(role_grant=first, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope.objects.create(role_grant=second, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope._base_manager.bulk_create(
            [
                RoleGrantScope(
                    role_grant_id=second.pk,
                    scope_type=RoleGrantScope.SCOPE_TENANT,
                    tenant_id=self.customer.pk,
                )
            ]
        )

        result = self.provisioner.provision(self.command())

        grants = list(RoleGrant.objects.filter(membership=membership, role=role).order_by("pk"))
        self.assertEqual([grant.pk for grant in grants], [first.pk, second.pk])
        self.assertEqual(
            set(RoleGrantScope.objects.filter(role_grant=first).values_list("scope_type", flat=True)),
            {RoleGrantScope.SCOPE_OWN},
        )
        self.assertEqual(
            set(RoleGrantScope.objects.filter(role_grant=second).values_list("scope_type", flat=True)),
            {RoleGrantScope.SCOPE_TENANT},
        )
        self.assertEqual(result.membership_id, membership.pk)

    def test_same_role_nonown_only_historical_grants_converge_and_keep_every_scope(self):
        role = self.role(self.customer)
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        legacy = RoleGrant.objects.create(membership=membership, role=role, reason="legacy non-own")
        duplicate = RoleGrant.objects.create(membership=membership, role=role, reason="legacy own")
        RoleGrantScope._base_manager.bulk_create(
            [
                RoleGrantScope(
                    role_grant_id=legacy.pk,
                    scope_type=RoleGrantScope.SCOPE_TENANT,
                    tenant_id=self.customer.pk,
                )
            ]
        )
        RoleGrantScope.objects.create(role_grant=duplicate, scope_type=RoleGrantScope.SCOPE_OWN)

        result = self.provisioner.provision(self.command())

        grants = list(RoleGrant._base_manager.filter(membership=membership, role=role).order_by("pk"))
        self.assertEqual([grant.pk for grant in grants], [legacy.pk, duplicate.pk])
        self.assertEqual(
            set(RoleGrantScope._base_manager.filter(role_grant=legacy).values_list("scope_type", flat=True)),
            {RoleGrantScope.SCOPE_TENANT},
        )
        self.assertEqual(
            set(RoleGrantScope._base_manager.filter(role_grant=duplicate).values_list("scope_type", flat=True)),
            {RoleGrantScope.SCOPE_OWN},
        )
        self.assertEqual(result.membership_id, membership.pk)

    def test_same_provenance_prefers_permanent_over_expired_oldest(self):
        role = self.role(
            self.customer,
            name="Member",
            permissions=["assets.view_asset", "assets.change_asset"],
        )
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        reason = "OIDC group-claim provisioning"
        expired = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=reason,
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrant._base_manager.filter(pk=expired.pk).update(valid_until=timezone.now() - timedelta(minutes=1))
        expired.refresh_from_db()
        permanent = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=reason,
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrant._base_manager.filter(pk=permanent.pk).update(valid_until=None)
        permanent.refresh_from_db()
        RoleGrantScope.objects.create(role_grant=expired, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope.objects.create(role_grant=permanent, scope_type=RoleGrantScope.SCOPE_OWN)

        self.provisioner.provision(self.command())

        grants = list(RoleGrant._base_manager.filter(membership=membership, role=role).order_by("pk"))
        self.assertEqual([grant.pk for grant in grants], [permanent.pk])
        self.assertIsNone(grants[0].valid_until)
        self.assertTrue(grants[0].is_active)
        self.assertEqual(
            list(RoleGrantScope.objects.filter(role_grant=grants[0]).values_list("scope_type", flat=True)),
            [RoleGrantScope.SCOPE_OWN],
        )

    def test_same_provenance_prefers_latest_future_over_expired_oldest_without_shortening_it(self):
        role = self.role(
            self.customer,
            name="Member",
            permissions=["assets.view_asset", "assets.change_asset"],
        )
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        reason = "OIDC group-claim provisioning"
        expired = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=reason,
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrant._base_manager.filter(pk=expired.pk).update(valid_until=timezone.now() - timedelta(minutes=1))
        expired.refresh_from_db()
        future_until = timezone.now() + timedelta(days=3)
        future = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=reason,
            valid_until=future_until,
        )
        RoleGrantScope.objects.create(role_grant=expired, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope.objects.create(role_grant=future, scope_type=RoleGrantScope.SCOPE_OWN)

        self.provisioner.provision(self.command())

        grants = list(RoleGrant._base_manager.filter(membership=membership, role=role).order_by("pk"))
        self.assertEqual([grant.pk for grant in grants], [future.pk])
        self.assertGreaterEqual(grants[0].valid_until, future_until)
        self.assertTrue(grants[0].is_active)

    def test_distinct_manual_and_system_provenance_survive_and_effective_permission_remains(self):
        role = self.role(
            self.customer,
            name="Member",
            permissions=["assets.view_asset", "assets.change_asset"],
        )
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        manual_until = timezone.now() + timedelta(days=2)
        manual = RoleGrant.objects.create(
            membership=membership,
            role=role,
            granted_by=self.user,
            reason="operator-approved manual grant",
            valid_until=manual_until,
        )
        system = RoleGrant.objects.create(
            membership=membership,
            role=role,
            granted_by=None,
            reason="OIDC group-claim provisioning",
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrantScope.objects.create(role_grant=manual, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope.objects.create(role_grant=system, scope_type=RoleGrantScope.SCOPE_OWN)
        manual_before = tuple(
            RoleGrant._base_manager.filter(pk=manual.pk).values_list("pk", "granted_by_id", "reason", "valid_until")
        )

        self.provisioner.provision(self.command())
        self.provisioner.provision(self.command())

        grants = list(RoleGrant._base_manager.filter(membership=membership, role=role).order_by("pk"))
        self.assertEqual({grant.pk for grant in grants}, {manual.pk, system.pk})
        self.assertEqual(
            tuple(
                RoleGrant._base_manager.filter(pk=manual.pk).values_list("pk", "granted_by_id", "reason", "valid_until")
            ),
            manual_before,
        )
        self.assertEqual(
            RoleGrantScope.objects.filter(role_grant__in=grants, scope_type=RoleGrantScope.SCOPE_OWN).count(),
            2,
        )
        fresh_user = User._base_manager.get(pk=self.user.pk)
        self.assertTrue(MembershipBackend().has_perm(fresh_user, "assets.view_asset", obj=self.customer))

    def test_customer_reconciliation_does_not_mutate_inactive_provider_membership_children(self):
        customer_role = self.role(self.customer)
        provider_role = self.role(self.provider, name="ProviderStaff")
        provider_membership = Membership.objects.create(user=self.user, tenant=self.provider, is_active=False)
        provider_grant = RoleGrant.objects.create(
            membership=provider_membership,
            role=provider_role,
            reason="historical provider grant",
        )
        RoleGrantScope.objects.create(role_grant=provider_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope._base_manager.create(
            role_grant_id=provider_grant.pk,
            scope_type=RoleGrantScope.SCOPE_ALL_MANAGED,
        )
        before_grant = tuple(
            RoleGrant._base_manager.filter(pk=provider_grant.pk)
            .values_list("pk", "membership_id", "role_id", "granted_by_id", "reason", "valid_until")
            .get()
        )
        before_scopes = tuple(
            RoleGrantScope._base_manager.filter(role_grant=provider_grant)
            .order_by("pk")
            .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
        )

        result = self.provisioner.provision(self.command())

        self.assertEqual(result.mode, "customer")
        self.assertEqual(
            tuple(
                RoleGrant._base_manager.filter(pk=provider_grant.pk)
                .values_list("pk", "membership_id", "role_id", "granted_by_id", "reason", "valid_until")
                .get()
            ),
            before_grant,
        )
        self.assertEqual(
            tuple(
                RoleGrantScope._base_manager.filter(role_grant=provider_grant)
                .order_by("pk")
                .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
            ),
            before_scopes,
        )
        self.assertEqual(Role.objects.get(pk=result.role_id).pk, customer_role.pk)

    def test_conflicting_own_grant_with_nonown_scope_is_preserved_without_own_scope(self):
        target = self.role(self.customer, name="Member")
        other = self.role(self.customer, name="Other", permissions=["assets.view_asset"])
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        conflicting = RoleGrant.objects.create(membership=membership, role=other)
        RoleGrantScope.objects.create(role_grant=conflicting, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope._base_manager.bulk_create(
            [
                RoleGrantScope(
                    role_grant_id=conflicting.pk,
                    scope_type=RoleGrantScope.SCOPE_TENANT,
                    tenant_id=self.customer.pk,
                )
            ]
        )

        self.provisioner.provision(self.command())

        self.assertFalse(
            RoleGrantScope.objects.filter(role_grant=conflicting, scope_type=RoleGrantScope.SCOPE_OWN).exists()
        )
        self.assertTrue(
            RoleGrantScope.objects.filter(role_grant=conflicting, scope_type=RoleGrantScope.SCOPE_TENANT).exists()
        )
        self.assertEqual(RoleGrant.objects.filter(membership=membership, role=target).count(), 1)

    def test_own_scope_uniqueness_conflict_retries_exact_scope(self):
        role = self.role(self.customer)
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        grant = RoleGrant.objects.create(membership=membership, role=role)
        scope = RoleGrantScope.objects.create(role_grant=grant, scope_type=RoleGrantScope.SCOPE_OWN)
        real_create = RoleGrantScope._base_manager.create
        calls = {"count": 0}

        def conflict_once(**kwargs):
            if calls["count"] == 0 and kwargs.get("role_grant_id") == grant.pk:
                calls["count"] += 1
                raise IntegrityError("scope conflict")
            return real_create(**kwargs)

        with mock.patch.object(RoleGrantScope._base_manager, "create", side_effect=conflict_once):
            result = self.provisioner.provision(self.command())

        self.assertEqual(result.membership_id, membership.pk)
        self.assertEqual(RoleGrantScope.objects.filter(pk=scope.pk).count(), 1)

    def test_conflicting_own_scope_revocation_leaves_audited_tombstone(self):
        self.role(self.customer, name="Member")
        other = self.role(self.customer, name="Other", permissions=["assets.view_asset"])
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        conflicting = RoleGrant.objects.create(membership=membership, role=other)
        own_scope = RoleGrantScope.objects.create(
            role_grant=conflicting,
            scope_type=RoleGrantScope.SCOPE_OWN,
        )
        RoleGrantScope._base_manager.bulk_create(
            [
                RoleGrantScope(
                    role_grant_id=conflicting.pk,
                    scope_type=RoleGrantScope.SCOPE_TENANT,
                    tenant_id=self.customer.pk,
                )
            ]
        )
        actor = User.objects.create_superuser(
            username="scope-revoke-audit-443",
            email="scope-revoke-audit-443@example.invalid",
            password="not-used-443",
        )

        with TaskContext(tenant_id=self.customer.pk, user_id=actor.pk), oidc_sensitive_audit():
            request_id = get_current_request_id()
            result = self.provisioner.provision(self.command())

        self.assertEqual(result.mode, "customer")
        self.assertFalse(RoleGrantScope._base_manager.filter(pk=own_scope.pk).exists())
        changes = list(
            ObjectChange._base_manager.filter(
                changed_object_id=own_scope.pk,
                changed_object_type__app_label="organization",
                changed_object_type__model="rolegrantscope",
            ).order_by("pk")
        )
        self.assertEqual([change.action for change in changes], ["delete"])
        self.assertEqual(changes[0].user_id, actor.pk)
        self.assertEqual(changes[0].request_id, request_id)
        self.assertEqual(changes[0].prechange_data["role_grant"], conflicting.pk)
        self.assertIsNone(changes[0].postchange_data)

    def _provider_grant_fixture(self):
        role = self.role(
            self.provider,
            name="ProviderStaff",
            permissions=["assets.view_asset", "assets.change_asset"],
        )
        membership = Membership.objects.create(user=self.user, tenant=self.provider)
        return role, membership

    def _provision_provider_role(self, role):
        return self.provisioner.provision(self.command(tenant=self.provider, role=role.name))

    def test_provider_grants_do_not_move_expired_all_managed_scope_to_permanent_own(self):
        role, membership = self._provider_grant_fixture()
        own = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=timezone.now() + timedelta(days=1),
        )
        # Create a valid finite privileged row first; raw update explicitly models
        # the historical permanent state that current model validation rejects.
        RoleGrant._base_manager.filter(pk=own.pk).update(valid_until=None)
        own.refresh_from_db()
        expired = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=timezone.now() + timedelta(hours=1),
        )
        RoleGrant._base_manager.filter(pk=expired.pk).update(valid_until=timezone.now() - timedelta(minutes=1))
        expired.refresh_from_db()
        RoleGrantScope.objects.create(role_grant=own, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope.objects.create(role_grant=expired, scope_type=RoleGrantScope.SCOPE_ALL_MANAGED)

        before = (
            own.covers_tenant(self.provider),
            own.covers_tenant(self.customer),
            expired.covers_tenant(self.customer),
        )

        self._provision_provider_role(role)

        own.refresh_from_db()
        self.assertTrue(RoleGrant.objects.filter(pk=expired.pk).exists())
        expired.refresh_from_db()
        after = (
            own.covers_tenant(self.provider),
            own.covers_tenant(self.customer),
            expired.covers_tenant(self.customer),
        )
        self.assertEqual(after, before)
        self.assertTrue(own.covers_tenant(self.provider))
        self.assertFalse(expired.covers_tenant(self.customer))
        self.assertTrue(
            RoleGrantScope.objects.filter(
                role_grant=expired,
                scope_type=RoleGrantScope.SCOPE_ALL_MANAGED,
            ).exists()
        )
        self.assertEqual(
            set(RoleGrant.objects.filter(membership=membership, role=role).values_list("pk", flat=True)),
            {own.pk, expired.pk},
        )

    def test_provider_grants_keep_shorter_tenant_group_scope_on_original_grant(self):
        role, membership = self._provider_grant_fixture()
        group = TenantGroup.objects.create(name="Lifetime group 443", slug="lifetime-group-443")
        group_tenant = Tenant.objects.create(
            name="Lifetime group tenant 443",
            slug="lifetime-group-tenant-443",
            group=group,
            managed_by=self.provider,
        )
        own_until = timezone.now() + timedelta(days=3)
        group_until = timezone.now() + timedelta(hours=1)
        own = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=own_until,
        )
        group_grant = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=group_until,
        )
        RoleGrantScope.objects.create(role_grant=own, scope_type=RoleGrantScope.SCOPE_OWN)
        group_scope = RoleGrantScope.objects.create(
            role_grant=group_grant,
            scope_type=RoleGrantScope.SCOPE_TENANT_GROUP,
            tenant_group=group,
        )

        before = (own.covers_tenant(self.provider), group_grant.covers_tenant(group_tenant))
        self._provision_provider_role(role)

        own.refresh_from_db()
        self.assertTrue(RoleGrant.objects.filter(pk=group_grant.pk).exists())
        group_grant.refresh_from_db()
        group_scope.refresh_from_db()
        after = (own.covers_tenant(self.provider), group_grant.covers_tenant(group_tenant))
        self.assertEqual(after, before)
        self.assertEqual(group_grant.pk, group_scope.role_grant_id)
        self.assertEqual(group_grant.valid_until, group_until)
        self.assertEqual(
            set(RoleGrant.objects.filter(membership=membership, role=role).values_list("pk", flat=True)),
            {own.pk, group_grant.pk},
        )

    def test_provider_grants_with_identical_scope_sets_converge_to_widest_validity(self):
        role, membership = self._provider_grant_fixture()
        short_until = timezone.now() + timedelta(hours=1)
        wide_until = timezone.now() + timedelta(days=3)
        short = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=short_until,
        )
        wide = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=wide_until,
        )
        RoleGrantScope.objects.create(role_grant=short, scope_type=RoleGrantScope.SCOPE_OWN)
        wide_scope = RoleGrantScope.objects.create(role_grant=wide, scope_type=RoleGrantScope.SCOPE_OWN)
        before = (short.covers_tenant(self.provider), wide.covers_tenant(self.provider))

        self._provision_provider_role(role)

        wide.refresh_from_db()
        after = (wide.covers_tenant(self.provider),)
        self.assertEqual(after, (any(before),))
        self.assertEqual(wide.valid_until, wide_until)
        self.assertFalse(RoleGrant.objects.filter(pk=short.pk).exists())
        self.assertFalse(RoleGrantScope.objects.filter(role_grant_id=short.pk).exists())
        self.assertEqual(wide_scope.role_grant_id, wide.pk)
        self.assertEqual(
            list(RoleGrant.objects.filter(membership=membership, role=role).values_list("pk", flat=True)),
            [wide.pk],
        )

    def test_provider_grants_converge_overlapping_scope_and_retain_unique_scope_lifetime(self):
        role, membership = self._provider_grant_fixture()
        group = TenantGroup.objects.create(name="Mixed lifetime group 443", slug="mixed-lifetime-group-443")
        group_tenant = Tenant.objects.create(
            name="Mixed lifetime tenant 443",
            slug="mixed-lifetime-tenant-443",
            group=group,
            managed_by=self.provider,
        )
        wide_until = timezone.now() + timedelta(days=3)
        mixed_until = timezone.now() + timedelta(hours=1)
        wide = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=wide_until,
        )
        mixed = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=mixed_until,
        )
        RoleGrantScope.objects.create(role_grant=wide, scope_type=RoleGrantScope.SCOPE_OWN)
        mixed_own = RoleGrantScope.objects.create(role_grant=mixed, scope_type=RoleGrantScope.SCOPE_OWN)
        mixed_group = RoleGrantScope.objects.create(
            role_grant=mixed,
            scope_type=RoleGrantScope.SCOPE_TENANT_GROUP,
            tenant_group=group,
        )
        before = (wide.covers_tenant(self.provider), mixed.covers_tenant(group_tenant))

        self._provision_provider_role(role)

        wide.refresh_from_db()
        self.assertTrue(RoleGrant.objects.filter(pk=mixed.pk).exists())
        mixed.refresh_from_db()
        mixed_group.refresh_from_db()
        after = (wide.covers_tenant(self.provider), mixed.covers_tenant(group_tenant))
        self.assertEqual(after, before)
        self.assertFalse(RoleGrantScope.objects.filter(pk=mixed_own.pk).exists())
        self.assertEqual(mixed_group.role_grant_id, mixed.pk)
        self.assertEqual(mixed.valid_until, mixed_until)
        self.assertEqual(
            set(RoleGrant.objects.filter(membership=membership, role=role).values_list("pk", flat=True)),
            {wide.pk, mixed.pk},
        )

    def test_provider_grant_convergence_repeat_is_idempotent_and_audit_truthful(self):
        role, membership = self._provider_grant_fixture()
        short_until = timezone.now() + timedelta(hours=1)
        wide_until = timezone.now() + timedelta(days=3)
        short = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=short_until,
        )
        wide = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="OIDC group-claim provisioning",
            valid_until=wide_until,
        )
        short_scope = RoleGrantScope.objects.create(role_grant=short, scope_type=RoleGrantScope.SCOPE_OWN)
        wide_scope = RoleGrantScope.objects.create(role_grant=wide, scope_type=RoleGrantScope.SCOPE_OWN)
        actor = User.objects.create_superuser(
            username="provider-convergence-audit-443",
            email="provider-convergence-audit-443@example.invalid",
            password="not-used-443",
        )

        with TaskContext(tenant_id=self.provider.pk, user_id=actor.pk), oidc_sensitive_audit():
            request_id = get_current_request_id()
            before_effective = (short.covers_tenant(self.provider), wide.covers_tenant(self.provider))
            self._provision_provider_role(role)
            first_changes = list(
                ObjectChange._base_manager.filter(
                    changed_object_id__in=[short.pk, short_scope.pk],
                    changed_object_type__app_label="organization",
                    changed_object_type__model__in=["rolegrant", "rolegrantscope"],
                ).order_by("pk")
            )
            state_after_first = (
                tuple(
                    RoleGrant._base_manager.filter(membership=membership, role=role).values_list("pk", "valid_until")
                ),
                tuple(
                    RoleGrantScope._base_manager.filter(role_grant__membership=membership)
                    .order_by("pk")
                    .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
                ),
            )
            self._provision_provider_role(role)
            second_changes = list(
                ObjectChange._base_manager.filter(
                    changed_object_id__in=[short.pk, short_scope.pk],
                    changed_object_type__app_label="organization",
                    changed_object_type__model__in=["rolegrant", "rolegrantscope"],
                ).order_by("pk")
            )

        self.assertEqual(before_effective, (True, True))
        self.assertEqual(
            [(change.action, change.changed_object_id) for change in first_changes],
            [("delete", short_scope.pk), ("delete", short.pk)],
        )
        self.assertEqual(second_changes, first_changes)
        self.assertTrue(all(change.user_id == actor.pk for change in first_changes))
        self.assertTrue(all(change.request_id == request_id for change in first_changes))
        self.assertTrue(all(change.postchange_data is None for change in first_changes))
        scope_change = next(
            change
            for change in first_changes
            if change.changed_object_type.model == "rolegrantscope" and change.changed_object_id == short_scope.pk
        )
        self.assertEqual(scope_change.prechange_data["role_grant"], short.pk)
        grant_change = next(
            change
            for change in first_changes
            if change.changed_object_type.model == "rolegrant" and change.changed_object_id == short.pk
        )
        self.assertEqual(grant_change.prechange_data["membership"], membership.pk)
        self.assertEqual(
            state_after_first,
            (
                ((wide.pk, wide_until),),
                ((wide_scope.pk, wide.pk, RoleGrantScope.SCOPE_OWN, None, None),),
            ),
        )
        self.assertFalse(RoleGrant.objects.filter(pk=short.pk).exists())
        self.assertTrue(RoleGrant.objects.filter(pk=wide.pk, valid_until=wide_until).exists())


class ProviderContractTests(IdentityServiceCase):
    def provider_setup(self):
        provider_role = self.role(self.provider, name="ProviderStaff", permissions=["assets.view_asset"])
        customer_role = self.role(self.customer)
        customer_membership = Membership.objects.create(user=self.user, tenant=self.customer)
        customer_grant = RoleGrant.objects.create(membership=customer_membership, role=customer_role)
        RoleGrantScope.objects.create(role_grant=customer_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        group = UserGroup.objects.create(tenant=self.customer, name=CANARY_GROUP)
        GroupMembership.objects.create(
            user_group=group,
            membership=customer_membership,
            source=GroupMembership.SOURCE_OIDC,
        )
        holder = AssetHolder.objects.create(
            user=self.user,
            tenant=self.customer,
            upn="provider-transition-443@example.invalid",
            email="provider-transition-443@example.invalid",
            first_name="Transition",
            last_name="Holder",
        )
        provider_membership = Membership.objects.create(user=self.user, tenant=self.provider, is_active=False)
        provider_other_grant = RoleGrant.objects.create(membership=provider_membership, role=provider_role)
        RoleGrantScope.objects.create(role_grant=provider_other_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope._base_manager.create(
            role_grant_id=provider_other_grant.pk,
            scope_type=RoleGrantScope.SCOPE_ALL_MANAGED,
        )
        unrelated = User.objects.create_user(username="unrelated-443", email="unrelated-443@example.invalid")
        unrelated_membership = Membership.objects.create(user=unrelated, tenant=self.customer)
        unrelated_holder = AssetHolder.objects.create(
            user=unrelated,
            tenant=self.customer,
            upn="unrelated-443@example.invalid",
            email="unrelated-443@example.invalid",
            first_name="Unrelated",
            last_name="Holder",
        )
        return (
            provider_role,
            customer_membership,
            group,
            holder,
            provider_membership,
            unrelated,
            unrelated_membership,
            unrelated_holder,
        )

    def test_new_provider_membership_does_not_trigger_email_only_holder_autolink(self):
        provider_role = self.role(self.provider, name="ProviderStaff")
        holder = AssetHolder.objects.create(
            user=None,
            tenant=self.provider,
            upn="provider-signal-holder-443@example.invalid",
            email=self.user.email,
            first_name="Provider",
            last_name="Holder",
        )

        result = self.provisioner.provision(
            self.command(provider_staff=self.provider_intent(role_name=provider_role.name))
        )

        self.assertEqual(result.mode, "provider_staff")
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id)
        self.assertEqual(Membership.objects.filter(user=self.user, tenant=self.provider).count(), 1)

    def test_invalid_provider_mapping_is_terminal_zero_write_existing_user(self):
        self.role(self.customer)
        before_hash, before = self.organization_fingerprint()
        result = self.provisioner.provision(
            self.command(provider_staff=self.provider_intent(role_name="MissingProviderRole"))
        )
        after_hash, after = self.organization_fingerprint()

        self.assertEqual(result.mode, "provider_mapping_rejected")
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before, after)
        self.assertEqual(Membership.objects.filter(user=self.user).count(), 0)

    def test_invalid_provider_mapping_is_terminal_for_a_new_canonical_user_too(self):
        new_user = User.objects.create_user(username="new-rejected-443", email="new-rejected-443@example.invalid")
        before_hash, _ = self.organization_fingerprint()

        result = self.provisioner.provision(
            self.command(
                user=new_user,
                provider_staff=self.provider_intent(role_name="MissingProviderRole"),
            )
        )

        after_hash, _ = self.organization_fingerprint()
        self.assertEqual(result.mode, "provider_mapping_rejected")
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(Membership.objects.filter(user=new_user).count(), 0)
        self.assertEqual(AssetHolder.objects.filter(user=new_user).count(), 0)

    def test_provider_transition_deletes_customer_aggregate_unlinks_holders_and_keeps_unrelated_identity(self):
        (
            provider_role,
            customer_membership,
            group,
            holder,
            provider_membership,
            unrelated,
            unrelated_membership,
            unrelated_holder,
        ) = self.provider_setup()
        provider_grant_before = tuple(
            RoleGrant._base_manager.filter(membership=provider_membership).values_list(
                "pk", "membership_id", "role_id", "granted_by_id", "reason", "valid_until"
            )
        )
        provider_scopes_before = tuple(
            RoleGrantScope._base_manager.filter(role_grant__membership=provider_membership)
            .order_by("pk")
            .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
        )

        result = self.provisioner.provision(
            self.command(provider_staff=self.provider_intent(role_name=provider_role.name))
        )

        self.assertEqual(result.mode, "provider_staff")
        provider_membership.refresh_from_db()
        self.assertTrue(provider_membership.is_active)
        self.assertEqual(
            tuple(
                RoleGrant._base_manager.filter(membership=provider_membership).values_list(
                    "pk", "membership_id", "role_id", "granted_by_id", "reason", "valid_until"
                )
            ),
            provider_grant_before,
        )
        self.assertEqual(
            tuple(
                RoleGrantScope._base_manager.filter(role_grant__membership=provider_membership)
                .order_by("pk")
                .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
            ),
            provider_scopes_before,
        )
        self.assertFalse(Membership.objects.filter(pk=customer_membership.pk).exists())
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id)
        self.assertFalse(GroupMembership.objects.filter(user_group=group).exists())
        self.assertTrue(Membership.objects.filter(pk=unrelated_membership.pk).exists())
        unrelated_holder.refresh_from_db()
        self.assertEqual(unrelated_holder.user_id, unrelated.pk)

    def test_customer_command_is_sticky_provider_only_after_provider_membership_exists(self):
        (
            provider_role,
            customer_membership,
            group,
            holder,
            provider_membership,
            unrelated,
            unrelated_membership,
            unrelated_holder,
        ) = self.provider_setup()
        provider_membership.is_active = True
        provider_membership.save(update_fields=["is_active"])
        before_customer = Membership.objects.filter(user=self.user, tenant=self.customer).count()

        result = self.provisioner.provision(self.command())

        self.assertEqual(result.mode, "provider_staff")
        self.assertEqual(Membership.objects.filter(user=self.user, tenant=self.customer).count(), before_customer)
        self.assertEqual(
            AssetHolder.objects.filter(user=self.user, tenant=self.customer, user__isnull=False).count(), 1
        )

    def test_ldap_and_saml_customer_modes_do_not_apply_oidc_sticky_provider_shortcut(self):
        (
            provider_role,
            customer_membership,
            _group,
            _holder,
            provider_membership,
            _unrelated,
            _unrelated_membership,
            _unrelated_holder,
        ) = self.provider_setup()
        provider_membership.is_active = True
        provider_membership.save(update_fields=["is_active"])
        provider_grant_before = tuple(
            RoleGrant._base_manager.filter(membership=provider_membership).values_list(
                "pk", "membership_id", "role_id", "granted_by_id", "reason", "valid_until"
            )
        )

        for source in ("LDAP", "SAML"):
            with self.subTest(source=source):
                result = self.provisioner.provision(self.command(profile=self.profile(source=source)))
                self.assertEqual(result.mode, "customer")
                self.assertTrue(Membership.objects.filter(pk=customer_membership.pk).exists())
                self.assertTrue(
                    Membership.objects.filter(user=self.user, tenant=self.provider, is_active=True).exists()
                )
                self.assertEqual(
                    tuple(
                        RoleGrant._base_manager.filter(membership=provider_membership).values_list(
                            "pk", "membership_id", "role_id", "granted_by_id", "reason", "valid_until"
                        )
                    ),
                    provider_grant_before,
                )

    def test_provider_transition_failure_after_write_rolls_back_exact_organization_fingerprint(self):
        self.provider_setup()
        before_hash, before = self.organization_fingerprint()

        with (
            mock.patch(
                "organization.services.identity_provisioning._stage_checkpoint",
                side_effect=lambda stage: (
                    (_ for _ in ()).throw(RuntimeError(stage)) if stage == "provider.customer_retired" else None
                ),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.provisioner.provision(self.command(provider_staff=self.provider_intent(role_name="ProviderStaff")))

        after_hash, after = self.organization_fingerprint()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before, after)


class LDAPDirectoryContractTests(IdentityServiceCase):
    def ldap_command(self, *, user=None, tenant=None):
        return LDAPDirectoryIdentityCommand(user=user or self.user, tenant=tenant or self.customer)

    def test_batch_directory_sync_has_fixed_member_role_and_no_asset_holder(self):
        before_holders = AssetHolder.objects.count()
        provision_ldap_directory_identity(self.ldap_command())

        role = Role.objects.get(tenant=self.customer, name="Member")
        membership = Membership.objects.get(user=self.user, tenant=self.customer)
        grant = RoleGrant.objects.get(membership=membership, role=role)

        self.assertEqual(role.description, "Default Standard Member")
        self.assertEqual(set(role.permissions), set(LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS))
        self.assertEqual(grant.reason, LDAP_DIRECTORY_SYNC_REASON)
        self.assertIsNone(grant.granted_by_id)
        self.assertTrue(role_is_privileged(role))
        self.assertIsNotNone(grant.valid_until)
        self.assertEqual(AssetHolder.objects.count(), before_holders)

    def test_public_ldap_conflict_reread_always_restores_own_scope(self):
        role = self.role(self.customer, name="Member", permissions=list(LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS))
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        real_create = RoleGrant._base_manager.create
        real_lock_aggregate = identity_service._lock_aggregate
        injected = {"count": 0}
        conflicts = {"count": 0}

        def inject_exact_row(*args, **kwargs):
            aggregate = real_lock_aggregate(*args, **kwargs)
            if injected["count"] == 0:
                injected["count"] += 1
                locked_membership = aggregate.memberships[self.customer.pk]
                locked_role = next(locked for locked in aggregate.roles if locked.pk == role.pk)
                real_create(
                    membership_id=locked_membership.pk,
                    role_id=locked_role.pk,
                    reason=LDAP_DIRECTORY_SYNC_REASON,
                    valid_until=timezone.now() + timedelta(days=1),
                    granted_by_id=None,
                )
            return aggregate

        def conflict_once(**kwargs):
            if (
                kwargs.get("membership_id") == membership.pk
                and kwargs.get("role_id") == role.pk
                and kwargs.get("reason") == LDAP_DIRECTORY_SYNC_REASON
                and conflicts["count"] == 0
            ):
                conflicts["count"] += 1
                raise IntegrityError(CANARY_DRIVER)
            return real_create(**kwargs)

        with (
            mock.patch.object(identity_service, "_lock_aggregate", side_effect=inject_exact_row),
            mock.patch.object(RoleGrant._base_manager, "create", side_effect=conflict_once),
        ):
            provision_ldap_directory_identity(self.ldap_command())

        grant = RoleGrant.objects.get(membership=membership, role=role, reason=LDAP_DIRECTORY_SYNC_REASON)
        self.assertEqual(injected["count"], 1)
        self.assertEqual(conflicts["count"], 1)
        self.assertIsNone(grant.granted_by_id)
        self.assertIsNotNone(grant.valid_until)
        self.assertEqual(
            list(grant.scopes.values_list("scope_type", flat=True)),
            [RoleGrantScope.SCOPE_OWN],
        )
        self.assertEqual(
            RoleGrant.objects.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).count(),
            1,
        )

    def test_batch_repeat_refreshes_only_ldap_owned_grant_origin(self):
        provision_ldap_directory_identity(self.ldap_command())
        grant = RoleGrant.objects.get(membership__user=self.user, reason=LDAP_DIRECTORY_SYNC_REASON)
        old_pk = grant.pk
        old_expiry = grant.valid_until

        provision_ldap_directory_identity(self.ldap_command())

        refreshed = RoleGrant.objects.get(pk=old_pk)
        self.assertEqual(
            RoleGrant.objects.filter(membership=grant.membership, reason=LDAP_DIRECTORY_SYNC_REASON).count(), 1
        )
        self.assertGreater(refreshed.valid_until, old_expiry)
        self.assertEqual(refreshed.granted_by_id, None)

    def test_active_manual_equivalent_is_reused_without_creating_ldap_owned_row(self):
        role = self.role(self.customer, name="Member", permissions=list(LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS))
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        manual = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="manual equivalent 443",
            valid_until=timezone.now() + timedelta(days=2),
            granted_by=self.user,
        )
        RoleGrantScope.objects.create(role_grant=manual, scope_type=RoleGrantScope.SCOPE_OWN)

        provision_ldap_directory_identity(self.ldap_command())

        self.assertFalse(RoleGrant.objects.filter(membership=membership, reason=LDAP_DIRECTORY_SYNC_REASON).exists())
        manual.refresh_from_db()
        self.assertEqual(manual.reason, "manual equivalent 443")
        self.assertEqual(manual.granted_by_id, self.user.pk)

    def test_expired_manual_equivalent_is_retained_and_new_ldap_grant_is_created(self):
        role = self.role(self.customer, name="Member", permissions=["assets.view_asset"])
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        manual = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason="manual expired 443",
            valid_until=timezone.now() - timedelta(minutes=1),
            granted_by=self.user,
        )
        RoleGrantScope.objects.create(role_grant=manual, scope_type=RoleGrantScope.SCOPE_OWN)

        provision_ldap_directory_identity(self.ldap_command())

        manual.refresh_from_db()
        self.assertLess(manual.valid_until, timezone.now())
        ldap_grant = RoleGrant.objects.get(membership=membership, reason=LDAP_DIRECTORY_SYNC_REASON)
        self.assertIsNone(ldap_grant.granted_by_id)

    def test_ambiguous_ldap_owned_rows_stay_untouched_and_one_fresh_row_converges(self):
        role = self.role(self.customer, name="Member", permissions=list(LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS))
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        expiry = timezone.now() - timedelta(minutes=1)
        first = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=LDAP_DIRECTORY_SYNC_REASON,
            valid_until=timezone.now() + timedelta(days=1),
            granted_by=None,
        )
        second = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=LDAP_DIRECTORY_SYNC_REASON,
            valid_until=timezone.now() + timedelta(days=1),
            granted_by=None,
        )
        RoleGrant._base_manager.filter(pk__in=(first.pk, second.pk)).update(valid_until=expiry)
        first.refresh_from_db()
        second.refresh_from_db()
        RoleGrantScope.objects.create(role_grant=first, scope_type=RoleGrantScope.SCOPE_OWN)
        RoleGrantScope.objects.create(role_grant=second, scope_type=RoleGrantScope.SCOPE_OWN)
        before_rows = list(
            RoleGrant._base_manager.filter(pk__in=(first.pk, second.pk))
            .order_by("pk")
            .values_list("pk", "valid_until", "reason", "granted_by_id")
        )
        before_scopes = list(
            RoleGrantScope._base_manager.filter(role_grant_id__in=(first.pk, second.pk))
            .order_by("pk")
            .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
        )

        provision_ldap_directory_identity(self.ldap_command())

        after_rows = list(
            RoleGrant._base_manager.filter(pk__in=(first.pk, second.pk))
            .order_by("pk")
            .values_list("pk", "valid_until", "reason", "granted_by_id")
        )
        after_scopes = list(
            RoleGrantScope._base_manager.filter(role_grant_id__in=(first.pk, second.pk))
            .order_by("pk")
            .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
        )
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_scopes, before_scopes)
        grants = list(
            RoleGrant._base_manager.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).order_by("pk")
        )
        self.assertEqual(len(grants), 3)
        fresh = grants[-1]
        self.assertNotIn(fresh.pk, {first.pk, second.pk})
        now = timezone.now()
        self.assertGreaterEqual(fresh.valid_until, now + timedelta(hours=23))
        self.assertLessEqual(fresh.valid_until, now + timedelta(days=1, minutes=1))
        self.assertEqual(
            list(RoleGrantScope.objects.filter(role_grant=fresh).values_list("scope_type", flat=True)),
            [RoleGrantScope.SCOPE_OWN],
        )

        provision_ldap_directory_identity(self.ldap_command())
        self.assertEqual(
            RoleGrant.objects.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).count(),
            3,
        )

    def test_ambiguous_ldap_owned_rows_without_own_scope_create_one_fresh_row(self):
        role = self.role(self.customer, name="Member", permissions=["assets.view_asset"])
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        first = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=LDAP_DIRECTORY_SYNC_REASON,
            granted_by=None,
        )
        second = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=LDAP_DIRECTORY_SYNC_REASON,
            granted_by=None,
        )
        before_rows = list(
            RoleGrant._base_manager.filter(pk__in=(first.pk, second.pk))
            .order_by("pk")
            .values_list("pk", "valid_until", "reason", "granted_by_id")
        )

        provision_ldap_directory_identity(self.ldap_command())

        self.assertEqual(
            list(
                RoleGrant._base_manager.filter(pk__in=(first.pk, second.pk))
                .order_by("pk")
                .values_list("pk", "valid_until", "reason", "granted_by_id")
            ),
            before_rows,
        )
        grants = list(
            RoleGrant._base_manager.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).order_by("pk")
        )
        self.assertEqual(len(grants), 3)
        fresh = grants[-1]
        self.assertEqual(
            list(RoleGrantScope.objects.filter(role_grant=fresh).values_list("scope_type", flat=True)),
            [RoleGrantScope.SCOPE_OWN],
        )
        provision_ldap_directory_identity(self.ldap_command())
        self.assertEqual(
            RoleGrant.objects.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).count(),
            3,
        )

    def test_ambiguous_mixed_expired_and_incomplete_ldap_rows_stay_unchanged(self):
        role = self.role(self.customer, name="Member", permissions=["assets.view_asset"])
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        expired = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=LDAP_DIRECTORY_SYNC_REASON,
            granted_by=None,
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrant._base_manager.filter(pk=expired.pk).update(valid_until=timezone.now() - timedelta(minutes=1))
        expired.refresh_from_db()
        incomplete = RoleGrant.objects.create(
            membership=membership,
            role=role,
            reason=LDAP_DIRECTORY_SYNC_REASON,
            granted_by=None,
        )
        RoleGrantScope.objects.create(role_grant=expired, scope_type=RoleGrantScope.SCOPE_OWN)
        before_rows = list(
            RoleGrant._base_manager.filter(pk__in=(expired.pk, incomplete.pk))
            .order_by("pk")
            .values_list("pk", "valid_until", "reason", "granted_by_id")
        )
        before_scopes = list(
            RoleGrantScope._base_manager.filter(role_grant_id=expired.pk).values_list(
                "pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id"
            )
        )

        provision_ldap_directory_identity(self.ldap_command())

        self.assertEqual(
            list(
                RoleGrant._base_manager.filter(pk__in=(expired.pk, incomplete.pk))
                .order_by("pk")
                .values_list("pk", "valid_until", "reason", "granted_by_id")
            ),
            before_rows,
        )
        self.assertEqual(
            list(
                RoleGrantScope._base_manager.filter(role_grant_id=expired.pk).values_list(
                    "pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id"
                )
            ),
            before_scopes,
        )
        self.assertEqual(
            RoleGrant.objects.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).count(),
            3,
        )

    def test_ambiguous_ldap_rows_reuse_active_manual_equivalent_without_new_ldap_row(self):
        role = self.role(self.customer, name="Member", permissions=["assets.view_asset"])
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        old_rows = []
        for _ in range(2):
            grant = RoleGrant.objects.create(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by=None,
            )
            old_rows.append(grant)
        manual = RoleGrant.objects.create(
            membership=membership,
            role=role,
            granted_by=self.user,
            reason="active operator equivalent",
            valid_until=timezone.now() + timedelta(days=2),
        )
        RoleGrantScope.objects.create(role_grant=manual, scope_type=RoleGrantScope.SCOPE_OWN)
        before_manual = tuple(
            RoleGrant._base_manager.filter(pk=manual.pk).values_list("pk", "granted_by_id", "reason", "valid_until")
        )

        provision_ldap_directory_identity(self.ldap_command())

        self.assertEqual(
            RoleGrant.objects.filter(
                membership=membership,
                role=role,
                reason=LDAP_DIRECTORY_SYNC_REASON,
                granted_by__isnull=True,
            ).count(),
            2,
        )
        self.assertEqual(
            tuple(
                RoleGrant._base_manager.filter(pk=manual.pk).values_list("pk", "granted_by_id", "reason", "valid_until")
            ),
            before_manual,
        )
        self.assertEqual(
            {grant.pk for grant in RoleGrant._base_manager.filter(pk__in=[row.pk for row in old_rows])},
            {row.pk for row in old_rows},
        )


class LoggingAndFailureContractTests(IdentityServiceCase):
    def test_holder_integrity_conflict_is_sanitized_and_grants_continue(self):
        self.role(self.customer)
        with (
            mock.patch.object(AssetHolder, "save", side_effect=IntegrityError(CANARY_DRIVER)),
            self.assertLogs("itambox.organization.identity", level="WARNING") as logs,
        ):
            result = self.provisioner.provision(
                self.command(
                    profile=self.profile(email=CANARY_EMAIL, upn=CANARY_UPN),
                )
            )

        self.assertIsNone(result.holder_id)
        self.assertEqual(Membership.objects.filter(pk=result.membership_id).count(), 1)
        self.assertEqual(RoleGrant.objects.filter(membership_id=result.membership_id).count(), 1)
        self.assert_no_sensitive_log_text(logs.records)

    def test_missing_or_unsaved_canonical_user_fails_before_any_organization_write(self):
        unsaved = User(username="unsaved-443")
        before_hash, before = self.organization_fingerprint()

        with self.assertRaises(IdentityProvisioningError):
            self.provisioner.provision(self.command(user=unsaved))

        after_hash, after = self.organization_fingerprint()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before, after)

    def test_provisioner_keeps_command_based_public_call_shape(self):
        signature = inspect.signature(OrganizationIdentityProvisioner.provision)
        self.assertEqual(tuple(signature.parameters), ("self", "command"))

    def test_structured_log_fields_and_rendered_output_are_redacted(self):
        self.role(self.customer)
        AssetHolder.objects.create(
            user=None,
            tenant=self.customer,
            upn="structured-log-other-upn-443@example.invalid",
            email=CANARY_EMAIL,
            first_name="Structured",
            last_name="Canary",
        )
        with self.assertLogs("itambox.organization.identity", level="WARNING") as logs:
            result = self.provisioner.provision(
                self.command(
                    profile=self.profile(
                        email=CANARY_EMAIL,
                        upn=CANARY_UPN,
                    )
                )
            )

        self.assertEqual(result.mode, "customer")
        self.assertTrue(logs.records)
        for record in logs.records:
            self.assertEqual(record.__dict__.get("reason_code"), "holder_email_hint_rejected")
        self.assert_no_sensitive_log_text(logs.records)

    def test_task_and_oidc_context_produce_truthful_redacted_audit_for_success(self):
        actor = User.objects.create_superuser(
            username="audit-actor-443",
            email="audit-actor-443@example.invalid",
            password="not-used-443",
        )
        before_user = tuple(
            User._base_manager.filter(pk=self.user.pk).values_list("pk", "username", "email", "first_name", "last_name")
        )
        before_changes = ObjectChange._base_manager.count()
        before_events = Event._base_manager.count()

        with TaskContext(tenant_id=self.customer.pk, user_id=actor.pk), oidc_sensitive_audit():
            result = self.provisioner.provision(
                self.command(
                    profile=self.profile(email=CANARY_EMAIL, upn=CANARY_UPN),
                )
            )

        self.assertEqual(result.mode, "customer")
        changes = list(ObjectChange._base_manager.filter(pk__gt=before_changes).order_by("pk"))
        events = list(Event._base_manager.filter(pk__gt=before_events).order_by("pk"))
        self.assertTrue(changes)
        self.assertTrue(all(change.user_id == actor.pk for change in changes))
        self.assertTrue(all(change.tenant_id == self.customer.pk for change in changes))
        audit_text = json.dumps(
            [
                {
                    "object_repr": change.object_repr,
                    "prechange_data": change.prechange_data,
                    "postchange_data": change.postchange_data,
                }
                for change in changes
            ],
            default=str,
            sort_keys=True,
        )
        event_text = json.dumps([event.data for event in events], default=str, sort_keys=True)
        self.assertNotIn(CANARY_EMAIL, audit_text)
        self.assertNotIn(CANARY_UPN, audit_text)
        self.assertNotIn(CANARY_EMAIL, event_text)
        self.assertNotIn(CANARY_UPN, event_text)
        self.assertEqual(
            before_user,
            tuple(
                User._base_manager.filter(pk=self.user.pk).values_list(
                    "pk", "username", "email", "first_name", "last_name"
                )
            ),
        )

    def test_provider_rejection_is_zero_write_in_full_task_oidc_fingerprint(self):
        actor = User.objects.create_superuser(
            username="reject-actor-443",
            email="reject-actor-443@example.invalid",
            password="not-used-443",
        )
        before_hash, before = self.organization_fingerprint()
        with TaskContext(tenant_id=self.customer.pk, user_id=actor.pk), oidc_sensitive_audit():
            result = self.provisioner.provision(
                self.command(provider_staff=self.provider_intent(role_name=CANARY_ROLE))
            )
        after_hash, after = self.organization_fingerprint()
        self.assertEqual(result.mode, "provider_mapping_rejected")
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before, after)

    def test_tenant_relationship_validation_is_before_all_writes(self):
        invalid_provider = Tenant.objects.create(name="Not Provider", slug="not-provider")
        before_hash, before = self.organization_fingerprint()

        with self.assertRaises(IdentityProvisioningError):
            self.provisioner.provision(
                self.command(
                    provider_staff=self.provider_intent(provider=invalid_provider, role_name="Missing"),
                )
            )

        after_hash, after = self.organization_fingerprint()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before, after)


@pytest.mark.django_db(transaction=True)
def test_service_module_exposes_real_identity_port_contract_and_separate_ldap_api():
    from core.identity_provisioning import (
        ExternalIdentityProfile,
        ExternalIdentityProvisioningCommand,
        IdentityProvisioner,
        override_identity_provisioner,
        provision_external_identity,
    )

    provider = Tenant.objects.create(
        name="Protocol Provider 443",
        slug="protocol-provider-443",
        is_provider=True,
    )
    customer = Tenant.objects.create(
        name="Protocol Customer 443",
        slug="protocol-customer-443",
        managed_by=provider,
    )
    user = User.objects.create_user(username="protocol-user-443", email="protocol-user-443@example.invalid")
    command = ExternalIdentityProvisioningCommand(
        user=user,
        customer_tenant=customer,
        profile=ExternalIdentityProfile(
            source="OIDC",
            email=user.email,
            upn="protocol-user-443@example.invalid",
            first_name="Protocol",
            last_name="User",
        ),
        customer_role_name="Member",
    )

    protocol_public_members = {name for name in vars(IdentityProvisioner) if not name.startswith("_")}
    assert protocol_public_members == {"provision"}
    assert tuple(inspect.signature(IdentityProvisioner.provision).parameters) == ("self", "command")
    assert tuple(inspect.signature(OrganizationIdentityProvisioner.provision).parameters) == ("self", "command")
    assert organization_identity_provisioner.__class__ is OrganizationIdentityProvisioner
    assert not hasattr(IdentityProvisioner, "provision_ldap_directory_identity")
    assert set(identity_service.__all__) == {
        "IdentityProvisioningError",
        "OrganizationIdentityProvisioner",
        "organization_identity_provisioner",
        "LDAPDirectoryIdentityCommand",
        "provision_ldap_directory_identity",
        "LDAP_DIRECTORY_SYNC_REASON",
        "LDAP_DIRECTORY_SYNC_MEMBER_PERMISSION_LIST",
        "LDAP_DIRECTORY_SYNC_MEMBER_PERMISSIONS",
    }
    assert not any(name.startswith("_") for name in identity_service.__all__)

    with override_identity_provisioner(organization_identity_provisioner):
        result = provision_external_identity(command)

    assert result.mode == "customer"
    membership = Membership.objects.get(user=user, tenant=customer)
    holder = AssetHolder.objects.get(user=user, tenant=customer)
    grant = RoleGrant.objects.get(membership=membership)
    assert result.membership_id == membership.pk
    assert result.holder_id == holder.pk
    assert result.role_id == grant.role_id
    assert list(grant.scopes.values_list("scope_type", flat=True)) == [RoleGrantScope.SCOPE_OWN]
    assert LDAPDirectoryIdentityCommand is not ExternalIdentityProvisioningCommand
    assert callable(provision_ldap_directory_identity)
