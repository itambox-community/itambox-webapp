"""Write-stage rollback fingerprints for the #443 organization writer."""

from __future__ import annotations

import hashlib
import json
from unittest import mock

from django.test import TestCase

from core.identity_provisioning import ExternalIdentityProfile, ExternalIdentityProvisioningCommand, ProviderStaffIntent
from core.models import ObjectChange
from core.oidc_identity import oidc_sensitive_audit
from core.tasks.context import TaskContext
from extras.models import Event
from organization.models import AssetHolder, Membership, Role, RoleGrant, RoleGrantScope, Tenant
from organization.services.identity_provisioning import (
    LDAPDirectoryIdentityCommand,
    OrganizationIdentityProvisioner,
    provision_ldap_directory_identity,
)
from users.models import GroupMembership, User


class IdentityServiceRollbackTests(TestCase):
    def setUp(self):
        self.provisioner = OrganizationIdentityProvisioner()
        self.provider = Tenant.objects.create(name="Rollback Provider", slug="rollback-provider", is_provider=True)
        self.customer = Tenant.objects.create(
            name="Rollback Customer",
            slug="rollback-customer",
            managed_by=self.provider,
        )

    def fingerprint(self):
        payload = {
            "tenants": list(
                Tenant._base_manager.order_by("pk").values_list("pk", "deleted_at", "is_provider", "managed_by_id")
            ),
            "memberships": list(
                Membership._base_manager.order_by("pk").values_list("pk", "user_id", "tenant_id", "is_active")
            ),
            "roles": list(
                Role._base_manager.order_by("pk").values_list("pk", "tenant_id", "name", "description", "permissions")
            ),
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
                    "pk", "user_id", "tenant_id", "upn", "email", "deleted_at"
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
            "groups": list(User.objects.order_by("pk").values_list("pk", "username", "email")),
            "user_groups": list(
                GroupMembership._base_manager.order_by("pk").values_list("pk", "user_group_id", "membership_id")
            ),
        }
        return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()

    def customer_command(self, user):
        return ExternalIdentityProvisioningCommand(
            user=user,
            customer_tenant=self.customer,
            profile=ExternalIdentityProfile(
                source="SAML",
                email=user.email,
                upn=f"{user.username}@rollback.invalid",
                first_name="Rollback",
                last_name="User",
            ),
            customer_role_name="Member",
        )

    def fail_at(self, stage, call):
        before = self.fingerprint()

        def checkpoint(current):
            if current == stage:
                raise RuntimeError(stage)

        with (
            mock.patch("organization.services.identity_provisioning._stage_checkpoint", side_effect=checkpoint),
            self.assertRaisesRegex(RuntimeError, stage),
        ):
            call()

        self.assertEqual(self.fingerprint(), before, f"stage {stage} changed durable organization state")

    def test_customer_every_write_stage_rolls_back_exact_fingerprint(self):
        cases = (
            ("customer.role_created", False, False),
            ("customer.membership_created", True, False),
            ("customer.holder_created", True, False),
            ("customer.holder_linked", True, True),
            ("customer.scope_reconciled", True, False),
            ("customer.grant_reconciled", True, False),
        )
        for index, (stage, existing_role, existing_holder) in enumerate(cases):
            with self.subTest(stage=stage):
                user = User.objects.create_user(
                    username=f"customer-rollback-{index}",
                    email=f"customer-rollback-{index}@example.invalid",
                )
                if existing_role:
                    Role.objects.get_or_create(
                        tenant=self.customer,
                        name="Member",
                        defaults={"permissions": ["assets.view_asset"]},
                    )
                if existing_holder:
                    AssetHolder.objects.create(
                        user=None,
                        tenant=self.customer,
                        upn=f"{user.username}@rollback.invalid",
                        email=user.email,
                        first_name="Existing",
                        last_name="Holder",
                    )
                self.fail_at(stage, lambda user=user: self.provisioner.provision(self.customer_command(user)))

    def _provider_setup(self, index):
        user = User.objects.create_user(
            username=f"provider-rollback-{index}",
            email=f"provider-rollback-{index}@example.invalid",
        )
        provider_role = Role.objects.get_or_create(
            tenant=self.provider,
            name="ProviderStaff",
            defaults={"permissions": ["assets.view_asset"]},
        )[0]
        customer_role = Role.objects.get_or_create(
            tenant=self.customer,
            name="Member",
            defaults={"permissions": ["assets.view_asset"]},
        )[0]
        customer_membership = Membership.objects.create(user=user, tenant=self.customer)
        customer_grant = RoleGrant.objects.create(membership=customer_membership, role=customer_role)
        RoleGrantScope.objects.create(role_grant=customer_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        provider_membership = Membership.objects.create(user=user, tenant=self.provider, is_active=False)
        provider_grant = RoleGrant.objects.create(membership=provider_membership, role=provider_role)
        RoleGrantScope.objects.create(role_grant=provider_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        AssetHolder.objects.create(
            user=user,
            tenant=self.customer,
            upn=f"{user.username}@rollback.invalid",
            email=user.email,
            first_name="Provider",
            last_name="Rollback",
        )
        return user

    def test_provider_every_write_stage_rolls_back_exact_fingerprint(self):
        for index, stage in enumerate(
            (
                "provider.membership_activated",
                "provider.customer_retired",
                "provider.holders_unlinked",
            )
        ):
            with self.subTest(stage=stage):
                user = self._provider_setup(index)
                command = ExternalIdentityProvisioningCommand(
                    user=user,
                    customer_tenant=self.customer,
                    profile=ExternalIdentityProfile(
                        source="OIDC",
                        email=user.email,
                        upn=f"{user.username}@rollback.invalid",
                        first_name="Provider",
                        last_name="Rollback",
                    ),
                    customer_role_name="Member",
                    provider_staff=ProviderStaffIntent(
                        provider_tenant=self.provider,
                        role_name="ProviderStaff",
                    ),
                )
                self.fail_at(stage, lambda command=command: self.provisioner.provision(command))

    def test_ldap_every_write_stage_rolls_back_exact_fingerprint(self):
        for index, (stage, existing_role, existing_membership) in enumerate(
            (
                ("ldap.role_created", False, False),
                ("ldap.membership_created", True, False),
                ("ldap.grant_created", True, True),
            )
        ):
            with self.subTest(stage=stage):
                user = User.objects.create_user(
                    username=f"ldap-rollback-{index}",
                    email=f"ldap-rollback-{index}@example.invalid",
                )
                if existing_role:
                    role = Role.objects.get_or_create(
                        tenant=self.customer,
                        name="Member",
                        defaults={"permissions": ["assets.view_asset"]},
                    )[0]
                if existing_membership:
                    membership = Membership.objects.create(user=user, tenant=self.customer)
                    RoleGrant.objects.create(membership=membership, role=role)
                command = LDAPDirectoryIdentityCommand(user=user, tenant=self.customer)
                self.fail_at(stage, lambda command=command: provision_ldap_directory_identity(command))

    def test_ldap_grant_refresh_checkpoint_rolls_back_full_audit_and_profile_state(self):
        actor = User.objects.create_superuser(
            username="ldap-refresh-actor-443",
            email="ldap-refresh-actor-443@example.invalid",
            password="not-used-443",
        )
        user = User.objects.create_user(
            username="ldap-refresh-443",
            email="ldap-refresh-443@example.invalid",
            first_name="Before",
            last_name="Refresh",
        )
        command = LDAPDirectoryIdentityCommand(user=user, tenant=self.customer)
        provision_ldap_directory_identity(command)
        before = self.fingerprint()

        def checkpoint(stage):
            if stage == "ldap.grant_refreshed":
                raise RuntimeError(stage)

        with (
            TaskContext(tenant_id=self.customer.pk, user_id=actor.pk),
            oidc_sensitive_audit(),
            mock.patch("organization.services.identity_provisioning._stage_checkpoint", side_effect=checkpoint),
            self.assertRaisesRegex(RuntimeError, "ldap.grant_refreshed"),
        ):
            provision_ldap_directory_identity(command)

        self.assertEqual(self.fingerprint(), before)
