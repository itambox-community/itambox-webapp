"""Measured and frozen SQL ceilings for the #443 organization writer."""

from __future__ import annotations

import hashlib
import json

from django.db import connection
from django.test import TestCase

from core.identity_provisioning import ExternalIdentityProfile, ExternalIdentityProvisioningCommand, ProviderStaffIntent
from organization.models import AssetHolder, Membership, Role, RoleGrant, RoleGrantScope, Tenant
from organization.services.identity_provisioning import (
    LDAPDirectoryIdentityCommand,
    OrganizationIdentityProvisioner,
    provision_ldap_directory_identity,
)
from users.models import GroupMembership, User, UserGroup

# These are deliberately ceilings rather than a timing target. They freeze the
# measured shape while allowing PostgreSQL savepoint syntax to differ between
# Django patch releases. Growth requires an explicit service/test review.
FROZEN_QUERY_CEILINGS = {
    "customer_first": 32,
    "customer_established_repeat": 12,
    "provider_transition": 35,
    "ldap_first": 35,
    "ldap_repeat": 18,
}


class IdentityServiceQueryContractTests(TestCase):
    def setUp(self):
        self.provisioner = OrganizationIdentityProvisioner()
        self.provider = Tenant.objects.create(name="Query Provider", slug="query-provider", is_provider=True)
        self.customer = Tenant.objects.create(
            name="Query Customer",
            slug="query-customer",
            managed_by=self.provider,
        )
        self.user = User.objects.create_user(username="query-user-443", email="query-user-443@example.invalid")

    def command(self, *, role="Member", provider_staff=None, user=None):
        chosen = user or self.user
        return ExternalIdentityProvisioningCommand(
            user=chosen,
            customer_tenant=self.customer,
            profile=ExternalIdentityProfile(
                source="OIDC",
                email=chosen.email,
                upn=f"{chosen.username}@query.invalid",
                first_name="Query",
                last_name="User",
            ),
            customer_role_name=role,
            provider_staff=provider_staff,
        )

    def capture(self, callable_, *args, **kwargs):
        statements = []

        def wrapper(execute, sql, params, many, context):
            statements.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            result = callable_(*args, **kwargs)
        return result, statements

    @staticmethod
    def signature(statements):
        verbs = []
        normalized = []
        for sql, _params in statements:
            compact = " ".join(sql.split())
            normalized.append(compact)
            first = compact.split(" ", 1)[0].upper() if compact else ""
            verbs.append(first)
        digest = hashlib.sha256("\n".join(normalized).encode()).hexdigest()
        return {"queries": len(statements), "verbs": verbs, "sql_sha256": digest}

    def test_customer_first_and_repeat_stay_within_frozen_ceilings(self):
        Role.objects.create(tenant=self.customer, name="Member", permissions=["assets.view_asset"])
        _, first_statements = self.capture(self.provisioner.provision, self.command())
        _, repeat_statements = self.capture(self.provisioner.provision, self.command())

        first = self.signature(first_statements)
        repeat = self.signature(repeat_statements)
        print(f"QUERY_CHARACTERIZATION customer_first={json.dumps(first, sort_keys=True)}")
        print(f"QUERY_CHARACTERIZATION customer_established_repeat={json.dumps(repeat, sort_keys=True)}")
        self.assertLessEqual(first["queries"], FROZEN_QUERY_CEILINGS["customer_first"])
        self.assertLessEqual(repeat["queries"], FROZEN_QUERY_CEILINGS["customer_established_repeat"])
        self.assertFalse(any(verb in {"INSERT", "UPDATE", "DELETE"} for verb in repeat["verbs"]))

    def test_provider_transition_stays_within_frozen_ceiling_and_lock_order(self):
        provider_role = Role.objects.create(
            tenant=self.provider,
            name="ProviderStaff",
            permissions=["assets.view_asset"],
        )
        customer_role = Role.objects.create(
            tenant=self.customer,
            name="Member",
            permissions=["assets.view_asset"],
        )
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        grant = RoleGrant.objects.create(membership=membership, role=customer_role)
        RoleGrantScope.objects.create(role_grant=grant, scope_type=RoleGrantScope.SCOPE_OWN)
        holder = AssetHolder.objects.create(
            user=self.user,
            tenant=self.customer,
            upn="query-transition-443@example.invalid",
            email="query-transition-443@example.invalid",
            first_name="Query",
            last_name="Holder",
        )
        provider_membership = Membership.objects.create(user=self.user, tenant=self.provider, is_active=False)
        provider_grant = RoleGrant.objects.create(membership=provider_membership, role=provider_role)
        RoleGrantScope.objects.create(role_grant=provider_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        group = UserGroup.objects.create(tenant=self.customer, name="Query Group")
        GroupMembership.objects.create(user_group=group, membership=membership)

        result, statements = self.capture(
            self.provisioner.provision,
            self.command(
                provider_staff=ProviderStaffIntent(provider_tenant=self.provider, role_name=provider_role.name),
            ),
        )
        signature = self.signature(statements)
        print(f"QUERY_CHARACTERIZATION provider_transition={json.dumps(signature, sort_keys=True)}")
        self.assertEqual(result.mode, "provider_staff")
        self.assertLessEqual(signature["queries"], FROZEN_QUERY_CEILINGS["provider_transition"])
        tenant_lock = [sql for sql, _params in statements if "FOR SHARE" in sql.upper()]
        self.assertEqual(len(tenant_lock), 1)
        self.assertNotIn("FOR UPDATE", tenant_lock[0].upper())
        self.assertNotIn("FOR KEY SHARE", tenant_lock[0].upper())
        self.assertIn("%s", tenant_lock[0])

        lock_sequence = []
        for sql, _params in statements:
            normalized = " ".join(sql.split()).lower()
            if "organization_tenant" in normalized and "for share" in normalized:
                lock_sequence.append("tenant")
            elif 'from "users_user"' in normalized and "for update" in normalized:
                lock_sequence.append("user")
            elif 'from "organization_membership"' in normalized and "for update" in normalized:
                lock_sequence.append("membership")
            elif 'from "users_groupmembership"' in normalized and "for update" in normalized:
                lock_sequence.append("group_membership")
            elif 'from "organization_rolegrant"' in normalized and "for update" in normalized:
                lock_sequence.append("grant")
            elif 'from "organization_role"' in normalized and "for update" in normalized:
                lock_sequence.append("role")
            elif 'from "organization_rolegrantscope"' in normalized and "for update" in normalized:
                lock_sequence.append("scope")
            elif 'from "organization_assetholder"' in normalized and "for update" in normalized:
                lock_sequence.append("holder")
        self.assertEqual(
            lock_sequence,
            ["tenant", "user", "membership", "group_membership", "grant", "role", "scope", "holder"],
        )
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id)

    def test_ldap_first_and_repeat_stay_within_frozen_ceilings_without_holder_queries(self):
        first_result, first_statements = self.capture(
            provision_ldap_directory_identity,
            LDAPDirectoryIdentityCommand(user=self.user, tenant=self.customer),
        )
        repeat_result, repeat_statements = self.capture(
            provision_ldap_directory_identity,
            LDAPDirectoryIdentityCommand(user=self.user, tenant=self.customer),
        )
        first = self.signature(first_statements)
        repeat = self.signature(repeat_statements)
        print(f"QUERY_CHARACTERIZATION ldap_first={json.dumps(first, sort_keys=True)}")
        print(f"QUERY_CHARACTERIZATION ldap_repeat={json.dumps(repeat, sort_keys=True)}")
        self.assertIsNone(first_result)
        self.assertIsNone(repeat_result)
        self.assertLessEqual(first["queries"], FROZEN_QUERY_CEILINGS["ldap_first"])
        self.assertLessEqual(repeat["queries"], FROZEN_QUERY_CEILINGS["ldap_repeat"])
        self.assertFalse(
            any(
                verb in {"INSERT", "UPDATE", "DELETE"}
                for (sql, _params), verb in zip(first_statements, first["verbs"], strict=True)
                if "organization_assetholder" in sql.lower()
            )
        )
        self.assertFalse(
            any(
                verb in {"INSERT", "UPDATE", "DELETE"}
                for (sql, _params), verb in zip(repeat_statements, repeat["verbs"], strict=True)
                if "organization_assetholder" in sql.lower()
            )
        )

    def test_permission_table_is_read_only_for_missing_role(self):
        _, first_statements = self.capture(self.provisioner.provision, self.command(role="Manager"))
        _, repeat_statements = self.capture(self.provisioner.provision, self.command(role="Manager"))
        self.assertEqual(sum("auth_permission" in sql.lower() for sql, _params in first_statements), 1)
        self.assertEqual(sum("auth_permission" in sql.lower() for sql, _params in repeat_statements), 0)

    def test_ldap_existing_role_has_one_full_lock_sequence_without_role_preread(self):
        role = Role.objects.create(
            tenant=self.customer,
            name="Member",
            permissions=["assets.view_asset"],
        )
        membership = Membership.objects.create(user=self.user, tenant=self.customer)
        grant = RoleGrant.objects.create(membership=membership, role=role)
        RoleGrantScope.objects.create(role_grant=grant, scope_type=RoleGrantScope.SCOPE_OWN)

        _, statements = self.capture(
            provision_ldap_directory_identity,
            LDAPDirectoryIdentityCommand(user=self.user, tenant=self.customer),
        )

        labels = []
        for sql, _params in statements:
            normalized = " ".join(sql.split()).lower()
            if "organization_tenant" in normalized and "for share" in normalized:
                labels.append("tenant")
            elif 'from "users_user"' in normalized and "for update" in normalized:
                labels.append("user")
            elif 'from "organization_membership"' in normalized and "for update" in normalized:
                labels.append("membership")
            elif 'from "organization_rolegrant"' in normalized and "for update" in normalized:
                labels.append("grant")
            elif 'from "organization_role"' in normalized and (
                '"name" = %s' in normalized or "for update" in normalized
            ):
                labels.append("role")
            elif 'from "organization_rolegrantscope"' in normalized and "for update" in normalized:
                labels.append("scope")

        self.assertEqual(labels, ["tenant", "user", "membership", "grant", "role", "scope"])

    def test_ldap_missing_role_locks_name_before_create_and_rereads_only_on_conflict(self):
        role_events = []

        def wrapper(execute, sql, params, many, context):
            normalized = " ".join(sql.split()).lower()
            if (
                'from "organization_role"' in normalized and ('"name" = %s' in normalized or "for update" in normalized)
            ) or normalized.startswith('insert into "organization_role"'):
                role_events.append(
                    "select_for_update"
                    if normalized.startswith("select") and "for update" in normalized
                    else "select"
                    if normalized.startswith("select")
                    else "insert"
                )
            return execute(sql, params, many, context)

        with connection.execute_wrapper(wrapper):
            provision_ldap_directory_identity(
                LDAPDirectoryIdentityCommand(user=self.user, tenant=self.customer),
            )

        self.assertEqual(role_events, ["select_for_update", "insert"])
