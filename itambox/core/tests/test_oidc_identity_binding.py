from __future__ import annotations

import threading
import time
from datetime import timedelta
from threading import Event
from unittest.mock import Mock, patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

import organization.services.tenant_onboarding as tenant_onboarding
from core import identity_provisioning
from core.auth.oidc import OIDCIdentityProvisioningError, TenantOIDCBackend
from core.identity_provisioning import (
    ExternalIdentityProvisioningCommand,
    ExternalIdentityProvisioningResult,
    IdentityProvisioner,
)
from core.managers import set_current_tenant
from core.models import ObjectChange
from core.oidc_identity import oidc_sensitive_audit
from extras.models import Event as AuditEvent
from organization.models import AssetHolder, Membership, Role, RoleGrant, RoleGrantScope, Tenant
from organization.services.identity_provisioning import organization_identity_provisioner
from users.models import GroupMembership, OIDCIdentity, User, UserGroup

ISSUER = "https://phase-b.example/issuer"
OIDC_CONFIG = {
    "phase-b-customer": {
        "OIDC_OP_ISSUER": ISSUER,
        "OIDC_RP_CLIENT_ID": "phase-b-client",
        "OIDC_RP_CLIENT_SECRET": "phase-b-secret",
        "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://phase-b.example/authorize",
        "OIDC_OP_TOKEN_ENDPOINT": "https://phase-b.example/token",
        "OIDC_OP_USER_ENDPOINT": "https://phase-b.example/userinfo",
        "OIDC_OP_JWKS_ENDPOINT": "https://phase-b.example/jwks",
        "OIDC_GROUP_ROLE_MAPPING": {
            "customer-member": "Member",
            "customer-admin": "Admin",
        },
    },
    "phase-b-provider": {
        "OIDC_OP_ISSUER": ISSUER,
        "OIDC_GROUP_PROVIDER_ROLE_MAPPING": {
            "provider-first": "Provider Staff",
            "provider-second": "Provider Staff Second",
        },
    },
}


class RecordingIdentityProvisioner(IdentityProvisioner):
    """Transparent recording decorator around the real organization service."""

    def __init__(self):
        self.commands: list[ExternalIdentityProvisioningCommand] = []
        self.results: list[ExternalIdentityProvisioningResult] = []

    def provision(self, command: ExternalIdentityProvisioningCommand) -> ExternalIdentityProvisioningResult:
        result = organization_identity_provisioner.provision(command)
        self.commands.append(command)
        self.results.append(result)
        return result


@override_settings(ITAMBOX_TENANT_OIDC_CONFIGS=OIDC_CONFIG)
class OIDCPhaseBPortContractTests(TestCase):
    def setUp(self):
        self.provider = Tenant.objects.create(
            name="Phase B Provider",
            slug="phase-b-provider",
            is_provider=True,
        )
        self.customer = Tenant.objects.create(
            name="Phase B Customer",
            slug="phase-b-customer",
            managed_by=self.provider,
        )
        self.customer_role = Role.objects.create(
            tenant=self.customer,
            name="Admin",
            permissions=[],
        )
        self.provider_role = Role.objects.create(
            tenant=self.provider,
            name="Provider Staff",
            permissions=[],
        )
        self.user = User.objects.create_user(
            username="phase-b-user",
            email="old@example.test",
            first_name="Old",
            last_name="Profile",
        )
        self.binding = OIDCIdentity.objects.create(
            user=self.user,
            issuer=ISSUER,
            subject="phase-b-subject",
        )
        set_current_tenant(self.customer)
        self.backend = TenantOIDCBackend()

    def tearDown(self):
        set_current_tenant(None)

    @staticmethod
    def claims():
        return {
            "iss": ISSUER,
            "sub": "phase-b-subject",
            "email": "new@example.test",
            "upn": "new-upn@example.test",
            "given_name": "New",
            "family_name": "Name",
            "groups": ["provider-first", "provider-second", "customer-admin"],
        }

    def test_adapter_owns_normalization_and_calls_sdk_free_port_once(self):
        result = ExternalIdentityProvisioningResult(mode="customer")
        with patch.object(
            identity_provisioning,
            "provision_external_identity",
            return_value=result,
        ) as provision:
            resolved = self.backend._finish_identity_phase_b(
                self.binding.pk,
                self.user.pk,
                self.claims(),
            )

        self.assertEqual(resolved.pk, self.user.pk)
        provision.assert_called_once()
        command = provision.call_args.args[0]
        self.assertEqual(command.user.pk, self.user.pk)
        self.assertEqual(command.customer_tenant.pk, self.customer.pk)
        self.assertEqual(command.profile.source, "OIDC")
        self.assertEqual(command.profile.email, "new@example.test")
        self.assertEqual(command.profile.upn, "new-upn@example.test")
        self.assertEqual(command.profile.first_name, "New")
        self.assertEqual(command.profile.last_name, "Name")
        self.assertEqual(command.customer_role_name, "Admin")
        self.assertIsNotNone(command.provider_staff)
        self.assertEqual(command.provider_staff.provider_tenant.pk, self.provider.pk)
        self.assertEqual(command.provider_staff.role_name, "Provider Staff")
        self.assertNotIn("provider-first", repr(command))
        self.assertNotIn("provider-second", repr(command))

    def test_rejected_provider_intent_refreshes_user_profile_without_organization_audit(self):
        organization_models = (
            Tenant,
            AssetHolder,
            Membership,
            Role,
            RoleGrant,
            RoleGrantScope,
            UserGroup,
            GroupMembership,
        )
        organization_content_types = {ContentType.objects.get_for_model(model).pk for model in organization_models}
        before_organization = (
            AssetHolder.objects.count(),
            Membership.objects.count(),
            Role.objects.count(),
            RoleGrant.objects.count(),
            RoleGrantScope.objects.count(),
        )
        before_organization_audit = (
            tuple(
                ObjectChange.objects.filter(changed_object_type_id__in=organization_content_types)
                .order_by("pk")
                .values_list(
                    "pk",
                    "changed_object_type_id",
                    "changed_object_id",
                    "action",
                    "object_repr",
                    "prechange_data",
                    "postchange_data",
                )
            ),
            tuple(
                AuditEvent.objects.filter(model_id__in=organization_content_types)
                .order_by("pk")
                .values_list("pk", "model_id", "object_id", "action", "data", "status")
            ),
        )
        with patch.object(
            identity_provisioning,
            "provision_external_identity",
            return_value=ExternalIdentityProvisioningResult(mode="provider_mapping_rejected"),
        ) as provision:
            resolved = self.backend._finish_identity_phase_b(
                self.binding.pk,
                self.user.pk,
                self.claims(),
            )

        self.assertEqual(resolved.pk, self.user.pk)
        provision.assert_called_once()
        self.user.refresh_from_db()
        self.assertEqual(
            (self.user.email, self.user.first_name, self.user.last_name),
            ("new@example.test", "New", "Name"),
        )
        self.assertEqual(
            (
                AssetHolder.objects.count(),
                Membership.objects.count(),
                Role.objects.count(),
                RoleGrant.objects.count(),
                RoleGrantScope.objects.count(),
            ),
            before_organization,
        )
        self.assertEqual(
            (
                tuple(
                    ObjectChange.objects.filter(changed_object_type_id__in=organization_content_types)
                    .order_by("pk")
                    .values_list(
                        "pk",
                        "changed_object_type_id",
                        "changed_object_id",
                        "action",
                        "object_repr",
                        "prechange_data",
                        "postchange_data",
                    )
                ),
                tuple(
                    AuditEvent.objects.filter(model_id__in=organization_content_types)
                    .order_by("pk")
                    .values_list("pk", "model_id", "object_id", "action", "data", "status")
                ),
            ),
            before_organization_audit,
        )

    def test_can_login_false_does_not_call_identity_port_or_mutate_phase_b(self):
        self.user.can_login = False
        self.user.save(update_fields=["can_login"])
        before_profile = (self.user.email, self.user.first_name, self.user.last_name)
        before_organization = (
            AssetHolder.objects.count(),
            Membership.objects.count(),
            RoleGrant.objects.count(),
            RoleGrantScope.objects.count(),
        )
        with patch.object(identity_provisioning, "provision_external_identity") as provision:
            with (
                patch.object(self.backend, "get_userinfo", return_value={"email": "disabled-new@example.test"}),
                patch.object(self.backend, "verify_claims", return_value=True),
            ):
                result = self.backend.get_or_create_user(
                    "access-token",
                    "id-token",
                    {"iss": ISSUER, "sub": self.binding.subject},
                )

        self.assertEqual(result.pk, self.user.pk)
        provision.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(
            (self.user.email, self.user.first_name, self.user.last_name),
            before_profile,
        )
        self.assertEqual(
            (
                AssetHolder.objects.count(),
                Membership.objects.count(),
                RoleGrant.objects.count(),
                RoleGrantScope.objects.count(),
            ),
            before_organization,
        )

    def test_can_login_disabled_after_service_rolls_back_phase_b_and_wraps_typed_failure(self):
        before = {
            "organization": (
                AssetHolder.objects.count(),
                Membership.objects.count(),
                Role.objects.count(),
                RoleGrant.objects.count(),
                RoleGrantScope.objects.count(),
            ),
            "profile": (self.user.email, self.user.first_name, self.user.last_name),
            "changes": ObjectChange.objects.count(),
            "events": AuditEvent.objects.count(),
        }

        def provision_then_disable(command):
            result = organization_identity_provisioner.provision(command)
            User._base_manager.filter(pk=self.user.pk).update(can_login=False)
            return result

        with patch.object(
            identity_provisioning,
            "provision_external_identity",
            side_effect=provision_then_disable,
        ) as provision:
            with self.assertRaises(OIDCIdentityProvisioningError):
                self.backend._finish_identity_phase_b(
                    self.binding.pk,
                    self.user.pk,
                    self.claims(),
                )

        provision.assert_called_once()
        self.user.refresh_from_db()
        self.assertTrue(self.user.can_login)
        self.assertEqual(
            (self.user.email, self.user.first_name, self.user.last_name),
            before["profile"],
        )
        self.assertEqual(
            (
                AssetHolder.objects.count(),
                Membership.objects.count(),
                Role.objects.count(),
                RoleGrant.objects.count(),
                RoleGrantScope.objects.count(),
            ),
            before["organization"],
        )
        self.assertEqual(ObjectChange.objects.count(), before["changes"])
        self.assertEqual(AuditEvent.objects.count(), before["events"])


class OIDCPhaseBAuditFailureTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Audit Failure", slug="phase-b-customer")
        self.user = User.objects.create_user(
            username="audit-failure-user",
            email="old-audit@example.test",
            first_name="Old",
            last_name="Audit",
        )
        self.binding = OIDCIdentity.objects.create(
            user=self.user,
            issuer=ISSUER,
            subject="audit-failure-subject",
        )
        set_current_tenant(self.tenant)
        self.backend = TenantOIDCBackend()

    def tearDown(self):
        set_current_tenant(None)

    @override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={"phase-b-customer": OIDC_CONFIG["phase-b-customer"]})
    def test_profile_failure_rolls_back_organization_but_keeps_phase_a_binding(self):
        before = {
            "holders": AssetHolder.objects.count(),
            "memberships": Membership.objects.count(),
            "roles": Role.objects.count(),
            "grants": RoleGrant.objects.count(),
            "scopes": RoleGrantScope.objects.count(),
            "profile": (self.user.email, self.user.first_name, self.user.last_name),
        }
        original_update = self.backend._update_user_profile

        def fail_after_profile_update(user, claims):
            original_update(user, claims)
            raise RuntimeError("profile-stage-canary")

        with (
            patch.object(
                identity_provisioning,
                "provision_external_identity",
                wraps=organization_identity_provisioner.provision,
            ) as provision,
            patch.object(
                self.backend,
                "_update_user_profile",
                side_effect=fail_after_profile_update,
            ),
            self.assertRaises(Exception),
        ):
            with oidc_sensitive_audit():
                self.backend._finish_identity_phase_b(
                    self.binding.pk,
                    self.user.pk,
                    {
                        "iss": ISSUER,
                        "sub": self.binding.subject,
                        "email": "new-audit@example.test",
                        "given_name": "New",
                        "family_name": "Audit",
                    },
                )

        provision.assert_called_once()
        self.user.refresh_from_db()
        self.assertEqual(OIDCIdentity.objects.filter(pk=self.binding.pk, user=self.user).count(), 1)
        self.assertEqual(
            {
                "holders": AssetHolder.objects.count(),
                "memberships": Membership.objects.count(),
                "roles": Role.objects.count(),
                "grants": RoleGrant.objects.count(),
                "scopes": RoleGrantScope.objects.count(),
                "profile": (self.user.email, self.user.first_name, self.user.last_name),
            },
            before,
        )


@pytest.mark.serial_only
@override_settings(ITAMBOX_TENANT_OIDC_CONFIGS=OIDC_CONFIG)
class OIDCPhaseBStaleHandoffConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        set_current_tenant(None)
        self.customer = Tenant.objects.create(name="Stale Handoff Customer", slug="phase-b-customer")
        Role.objects.create(tenant=self.customer, name="Admin", permissions=[])
        self.user = User.objects.create_user(
            username="stale-handoff-user",
            email="old-stale@example.test",
            first_name="Old",
            last_name="Profile",
        )
        self.binding = OIDCIdentity.objects.create(
            user=self.user,
            issuer=ISSUER,
            subject="stale-handoff-subject",
        )
        set_current_tenant(self.customer)
        self.backend = TenantOIDCBackend()

    def tearDown(self):
        set_current_tenant(None)
        connections.close_all()

    def _resolve_after_committed_update(self, update_kwargs, claims=None):
        claims = claims or {
            "iss": ISSUER,
            "sub": self.binding.subject,
            "email": "new-stale@example.test",
            "given_name": "New",
            "family_name": "Profile",
            "groups": [],
        }
        snapshot_ready = Event()
        writer_committed = Event()
        writer_output = {}

        def writer():
            close_old_connections()
            try:
                if not snapshot_ready.wait(timeout=10):
                    raise AssertionError("adapter did not finish its initial User snapshot")
                writer_output["updated"] = User._base_manager.filter(pk=self.user.pk).update(**update_kwargs)
            except Exception as exc:
                writer_output["error"] = exc
            finally:
                writer_committed.set()
                connections.close_all()

        def provision(command):
            snapshot_ready.set()
            if not writer_committed.wait(timeout=10):
                raise AssertionError("concurrent User update did not commit")
            if writer_output.get("error") is not None:
                raise writer_output["error"]
            return organization_identity_provisioner.provision(command)

        writer_thread = threading.Thread(target=writer, name="oidc-stale-handoff-writer")
        writer_thread.start()
        try:
            with (
                patch.object(self.backend, "get_userinfo", return_value=claims),
                patch.object(self.backend, "verify_claims", return_value=True),
                patch.object(
                    identity_provisioning,
                    "provision_external_identity",
                    side_effect=provision,
                ),
            ):
                return self.backend.get_or_create_user(
                    "access-token",
                    "id-token",
                    {"iss": ISSUER, "sub": self.binding.subject},
                )
        finally:
            snapshot_ready.set()
            writer_thread.join(timeout=15)
            self.assertFalse(writer_thread.is_alive())
            self.assertIsNone(writer_output.get("error"))
            connections.close_all()

    def test_committed_can_login_disable_before_service_lock_has_zero_phase_b_delta(self):
        before = {
            "user_profile": User._base_manager.values_list("username", "email", "first_name", "last_name").get(
                pk=self.user.pk
            ),
            "user_count": User.objects.count(),
            "binding_count": OIDCIdentity.objects.count(),
            "organization": (
                AssetHolder.objects.count(),
                Membership.objects.count(),
                RoleGrant.objects.count(),
                RoleGrantScope.objects.count(),
            ),
            "changes": ObjectChange.objects.count(),
            "events": AuditEvent.objects.count(),
        }

        with self.assertRaises(OIDCIdentityProvisioningError):
            self._resolve_after_committed_update({"can_login": False})

        self.user.refresh_from_db()
        self.assertFalse(self.user.can_login)
        self.assertEqual(
            User._base_manager.values_list("username", "email", "first_name", "last_name").get(pk=self.user.pk),
            before["user_profile"],
        )
        self.assertEqual(User.objects.count(), before["user_count"])
        self.assertEqual(OIDCIdentity.objects.count(), before["binding_count"])
        self.assertEqual(
            (
                AssetHolder.objects.count(),
                Membership.objects.count(),
                RoleGrant.objects.count(),
                RoleGrantScope.objects.count(),
            ),
            before["organization"],
        )
        self.assertEqual(ObjectChange.objects.count(), before["changes"])
        self.assertEqual(AuditEvent.objects.count(), before["events"])

    def test_current_unrelated_profile_fields_survive_stale_handoff(self):
        result = self._resolve_after_committed_update(
            {"username": "concurrent-profile-user", "last_name": "Concurrent"},
            {
                "iss": ISSUER,
                "sub": self.binding.subject,
                "email": "claim-wins@example.test",
                "groups": [],
            },
        )

        self.assertEqual(result.pk, self.user.pk)
        self.assertEqual(result.email, "claim-wins@example.test")
        self.assertEqual(result.username, "concurrent-profile-user")
        self.assertEqual(result.last_name, "Concurrent")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "claim-wins@example.test")
        self.assertEqual(self.user.username, "concurrent-profile-user")
        self.assertEqual(self.user.last_name, "Concurrent")
        self.assertEqual(self.user.first_name, "Old")


B2_OIDC_CONFIG = {
    "b2-customer": {
        "OIDC_OP_ISSUER": "https://b2.example/issuer",
        "OIDC_RP_CLIENT_ID": "b2-client",
        "OIDC_RP_CLIENT_SECRET": "b2-secret",
        "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://b2.example/authorize",
        "OIDC_OP_TOKEN_ENDPOINT": "https://b2.example/token",
        "OIDC_OP_USER_ENDPOINT": "https://b2.example/userinfo",
        "OIDC_OP_JWKS_ENDPOINT": "https://b2.example/jwks",
        "OIDC_GROUP_ROLE_MAPPING": {},
    }
}


@pytest.mark.serial_only
@override_settings(ITAMBOX_TENANT_OIDC_CONFIGS=B2_OIDC_CONFIG)
class OIDCB2LockCompositionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        set_current_tenant(None)
        self.provider = Tenant.objects.create(
            name="B2 Provider",
            slug="b2-provider",
            is_provider=True,
        )
        self.customer = Tenant.objects.create(
            name="B2 Customer",
            slug="b2-customer",
            managed_by=self.provider,
        )
        Role.objects.create(
            tenant=self.customer,
            name="Member",
            permissions=[],
        )
        administrator = Role.objects.create(
            tenant=self.provider,
            name="B2 Administrator",
            permissions=["organization.add_tenant"],
        )
        self.user = User.objects.create_user(
            username="b2-lock-user",
            email="b2-lock@example.test",
            is_staff=True,
        )
        self.provider_membership = Membership.objects.create(
            user=self.user,
            tenant=self.provider,
            is_active=True,
        )
        self.authorizer = RoleGrant.objects.create(
            membership=self.provider_membership,
            role=administrator,
            granted_by=self.user,
            reason="B2 test authorizer",
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrantScope.objects.create(
            role_grant=self.authorizer,
            scope_type=RoleGrantScope.SCOPE_OWN,
        )
        self.binding = OIDCIdentity.objects.create(
            user=self.user,
            issuer=B2_OIDC_CONFIG["b2-customer"]["OIDC_OP_ISSUER"],
            subject="b2-lock-subject",
        )
        self.events = {
            "onboarding_tenant_locked": Event(),
            "service_tenant_attempted": Event(),
            "service_wait_observed": Event(),
            "binding_locked": Event(),
            "onboarding_user_locked": Event(),
            "oidc_tenant_locked": Event(),
            "onboarding_tenant_attempted": Event(),
            "onboarding_wait_observed": Event(),
        }
        self.outputs = {"onboarding": {}, "oidc": {}}
        self.lock_orders = {"onboarding": [], "oidc": []}

    def tearDown(self):
        set_current_tenant(None)
        connections.close_all()

    def _prepare_worker_connection(self, label):
        connections.close_all()
        connection.ensure_connection()
        backend = connection.connection
        self.assertIsNotNone(backend)
        pid = backend.get_backend_pid()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('application_name', %s, false)",
                [f"itambox443r-oidc-b2-{label}"],
            )
            cursor.execute("SET SESSION lock_timeout = '10s'")
            cursor.execute("SET SESSION statement_timeout = '30s'")
        return pid

    def _wait_for_real_tenant_wait(self, waiting_pid, blocking_pid, expected_lock_clause="for share"):
        deadline = time.monotonic() + 8
        observations = []
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT waiting.pid,
                           waiting.wait_event_type,
                           waiting.wait_event,
                           wait_lock.locktype,
                           wait_lock.relation::regclass::text,
                           wait_lock.page,
                           wait_lock.tuple,
                           blocking.pid,
                           waiting.query,
                           blocking.query,
                           waiting.application_name
                    FROM pg_stat_activity AS waiting
                    JOIN pg_locks AS wait_lock
                      ON wait_lock.pid = waiting.pid
                     AND NOT wait_lock.granted
                    LEFT JOIN LATERAL unnest(pg_blocking_pids(waiting.pid)) AS blocker(pid) ON TRUE
                    LEFT JOIN pg_stat_activity AS blocking ON blocking.pid = blocker.pid
                    WHERE waiting.pid = %s
                      AND waiting.datname = current_database()
                    """,
                    [waiting_pid],
                )
                rows = cursor.fetchall()
            observations.extend(rows)
            for row in rows:
                waiting_query = (row[8] or "").lower()
                if (
                    row[0] == waiting_pid
                    and row[1] == "Lock"
                    and row[3] in {"transactionid", "tuple"}
                    and row[7] == blocking_pid
                    and "organization_tenant" in waiting_query
                    and expected_lock_clause in waiting_query
                ):
                    return
            # The next pg_stat_activity/pg_locks query is the deterministic wait/yield.
        self.fail(f"OIDC Tenant {expected_lock_clause.upper()} wait was not observed: {observations[-3:]}")

    def _onboarding_worker(self):
        try:
            pid = self._prepare_worker_connection("onboarding")
            original_lock_tenants = tenant_onboarding._lock_tenants

            def pause_after_tenant_lock(*, provider_id, tenant_id):
                result = original_lock_tenants(provider_id=provider_id, tenant_id=tenant_id)
                self.events["onboarding_tenant_locked"].set()
                if not self.events["service_wait_observed"].wait(timeout=8):
                    raise AssertionError("OIDC service Tenant wait was not observed")
                return result

            def observe_onboarding_sql(execute, sql, params, many, context):
                normalized = " ".join(sql.split()).lower()
                if 'from "organization_tenant"' in normalized and "for update" in normalized:
                    self.lock_orders["onboarding"].append("tenant")
                if 'from "users_user"' in normalized and "for update" in normalized:
                    self.lock_orders["onboarding"].append("user")
                    self.events["onboarding_user_locked"].set()
                return execute(sql, params, many, context)

            with patch.object(tenant_onboarding, "_lock_tenants", side_effect=pause_after_tenant_lock):
                with connection.execute_wrapper(observe_onboarding_sql):
                    self.outputs["onboarding"]["result"] = tenant_onboarding.onboard_managed_tenant(
                        actor=User._base_manager.get(pk=self.user.pk),
                        provider_id=self.provider.pk,
                        tenant=Tenant._base_manager.get(pk=self.customer.pk),
                    )
            self.outputs["onboarding"]["pid"] = pid
        except Exception as exc:
            self.outputs["onboarding"]["error"] = exc
        finally:
            connections.close_all()

    def _reverse_onboarding_worker(self):
        try:
            pid = self._prepare_worker_connection("onboarding")

            def observe_onboarding_sql(execute, sql, params, many, context):
                normalized = " ".join(sql.split()).lower()
                if 'from "organization_tenant"' in normalized and "for update" in normalized:
                    self.lock_orders["onboarding"].append("tenant")
                    self.events["onboarding_tenant_attempted"].set()
                if 'from "users_user"' in normalized and "for update" in normalized:
                    self.lock_orders["onboarding"].append("user")
                    self.events["onboarding_user_locked"].set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_onboarding_sql):
                self.outputs["onboarding"]["result"] = tenant_onboarding.onboard_managed_tenant(
                    actor=User._base_manager.get(pk=self.user.pk),
                    provider_id=self.provider.pk,
                    tenant=Tenant._base_manager.get(pk=self.customer.pk),
                )
            self.outputs["onboarding"]["pid"] = pid
        except Exception as exc:
            self.outputs["onboarding"]["error"] = exc
        finally:
            connections.close_all()

    def _oidc_worker(self, *, pause_after_tenant=False):
        try:
            pid = self._prepare_worker_connection("oidc")
            tenant = Tenant._base_manager.get(pk=self.customer.pk)
            set_current_tenant(tenant)
            backend = TenantOIDCBackend()
            backend.get_userinfo = Mock(
                return_value={
                    "iss": B2_OIDC_CONFIG["b2-customer"]["OIDC_OP_ISSUER"],
                    "sub": self.binding.subject,
                    "email": "b2-lock-updated@example.test",
                    "groups": [],
                }
            )
            backend.verify_claims = Mock(return_value=True)
            binding_locks = 0

            def observe_oidc_sql(execute, sql, params, many, context):
                nonlocal binding_locks
                normalized = " ".join(sql.split()).lower()
                if 'from "users_oidcidentity"' in normalized and "for update" in normalized:
                    binding_locks += 1
                    if binding_locks >= 2:
                        self.events["binding_locked"].set()
                    self.lock_orders["oidc"].append("binding")
                if 'from "organization_tenant"' in normalized and "for share" in normalized:
                    if pause_after_tenant:
                        result = execute(sql, params, many, context)
                        self.lock_orders["oidc"].append("tenant")
                        self.events["oidc_tenant_locked"].set()
                        if not self.events["onboarding_wait_observed"].wait(timeout=8):
                            raise AssertionError("real onboarding Tenant wait was not observed")
                        return result
                    self.events["service_tenant_attempted"].set()
                    self.lock_orders["oidc"].append("tenant")
                if 'from "users_user"' in normalized and "for update" in normalized:
                    self.lock_orders["oidc"].append("user")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_oidc_sql):
                with identity_provisioning.override_identity_provisioner(organization_identity_provisioner):
                    self.outputs["oidc"]["result"] = backend.get_or_create_user(
                        "access-token",
                        "id-token",
                        {
                            "iss": B2_OIDC_CONFIG["b2-customer"]["OIDC_OP_ISSUER"],
                            "sub": self.binding.subject,
                        },
                    )
            self.outputs["oidc"]["pid"] = pid
        except Exception as exc:
            self.outputs["oidc"]["error"] = exc
        finally:
            set_current_tenant(None)
            connections.close_all()

    def test_binding_to_service_order_composes_with_real_onboarding_without_deadlock(self):
        onboarding = threading.Thread(target=self._onboarding_worker, name="real-b2-onboarding")
        oidc = threading.Thread(target=self._oidc_worker, name="real-b2-oidc")
        oidc_started = False
        onboarding.start()
        try:
            self.assertTrue(self.events["onboarding_tenant_locked"].wait(timeout=8))
            oidc.start()
            oidc_started = True
            self.assertTrue(self.events["binding_locked"].wait(timeout=8))
            self.assertTrue(self.events["service_tenant_attempted"].wait(timeout=8))
            self._wait_for_real_tenant_wait(
                self._oidc_pid_from_activity(),
                self._onboarding_pid_from_activity(),
            )
        finally:
            self.events["service_wait_observed"].set()
            onboarding.join(timeout=15)
            if oidc_started:
                oidc.join(timeout=15)
        self.assertFalse(onboarding.is_alive())
        self.assertFalse(oidc.is_alive())
        self.assertIsNone(self.outputs["onboarding"].get("error"))
        self.assertIsNone(self.outputs["oidc"].get("error"))
        self.assertTrue(self.events["onboarding_user_locked"].is_set())
        self.assertLess(self.lock_orders["onboarding"].index("tenant"), self.lock_orders["onboarding"].index("user"))
        self.assertLess(self.lock_orders["oidc"].index("tenant"), self.lock_orders["oidc"].index("user"))
        self.assertEqual(self.outputs["oidc"]["result"].pk, self.user.pk)

    def test_real_oidc_first_composes_with_real_onboarding_without_deadlock(self):
        oidc = threading.Thread(
            target=self._oidc_worker,
            kwargs={"pause_after_tenant": True},
            name="real-b2-oidc-first",
        )
        onboarding = threading.Thread(target=self._reverse_onboarding_worker, name="real-b2-onboarding-second")
        onboarding_started = False
        oidc.start()
        try:
            self.assertTrue(self.events["oidc_tenant_locked"].wait(timeout=8))
            onboarding.start()
            onboarding_started = True
            self.assertTrue(self.events["onboarding_tenant_attempted"].wait(timeout=8))
            self._wait_for_real_tenant_wait(
                self._onboarding_pid_from_activity(),
                self._oidc_pid_from_activity(),
                expected_lock_clause="for update",
            )
            self.events["onboarding_wait_observed"].set()
        finally:
            self.events["onboarding_wait_observed"].set()
            oidc.join(timeout=15)
            if onboarding_started:
                onboarding.join(timeout=15)
        self.assertFalse(oidc.is_alive())
        self.assertFalse(onboarding.is_alive())
        self.assertIsNone(self.outputs["oidc"].get("error"))
        self.assertIsNone(self.outputs["onboarding"].get("error"))
        self.assertTrue(self.events["onboarding_user_locked"].is_set())
        self.assertLess(self.lock_orders["oidc"].index("tenant"), self.lock_orders["oidc"].index("user"))
        self.assertLess(
            self.lock_orders["onboarding"].index("tenant"),
            self.lock_orders["onboarding"].index("user"),
        )
        self.assertEqual(self.outputs["oidc"]["result"].pk, self.user.pk)
        self.assertEqual(Membership.objects.filter(user=self.user, tenant=self.provider).count(), 1)
        self.assertFalse(Membership.objects.filter(user=self.user, tenant=self.customer).exists())
        self.assertTrue(Membership.objects.get(user=self.user, tenant=self.provider).is_active)
        self.assertFalse(AssetHolder.objects.filter(user=self.user, tenant=self.customer).exists())
        self.assertTrue(RoleGrant.objects.filter(membership__user=self.user, membership__tenant=self.provider).exists())

    def _oidc_pid_from_activity(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name = %s",
                ["itambox443r-oidc-b2-oidc"],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def _onboarding_pid_from_activity(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name = %s",
                ["itambox443r-oidc-b2-onboarding"],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        return row[0]


PROVIDER_ORDER_CONFIG = {
    "phase-b-customer": {
        **OIDC_CONFIG["phase-b-customer"],
        "OIDC_GROUP_ROLE_MAPPING": {
            "customer-admin": "Admin",
            "provider-first": "Admin",
            "provider-second": "Admin",
        },
    },
    "phase-b-provider": {
        "OIDC_OP_ISSUER": ISSUER,
        "OIDC_GROUP_PROVIDER_ROLE_MAPPING": {
            "provider-first": "ProviderFirstRole",
            "provider-second": "ProviderSecondRole",
        },
    },
}


@override_settings(ITAMBOX_TENANT_OIDC_CONFIGS=PROVIDER_ORDER_CONFIG)
class OIDCProviderGroupOrderTests(TestCase):
    def setUp(self):
        set_current_tenant(None)
        self.provider = Tenant.objects.create(
            name="Provider Order Provider",
            slug="phase-b-provider",
            is_provider=True,
        )
        self.customer = Tenant.objects.create(
            name="Provider Order Customer",
            slug="phase-b-customer",
            managed_by=self.provider,
        )
        self.customer_role = Role.objects.create(
            tenant=self.customer,
            name="Admin",
            permissions=[],
        )
        self.provider_first_role = Role.objects.create(
            tenant=self.provider,
            name="ProviderFirstRole",
            permissions=[],
        )
        self.provider_second_role = Role.objects.create(
            tenant=self.provider,
            name="ProviderSecondRole",
            permissions=[],
        )
        self.unrelated_user = User.objects.create_user(
            username="provider-order-unrelated",
            email="provider-order-unrelated@example.test",
            first_name="Unrelated",
            last_name="Identity",
        )
        self.unrelated_membership = Membership.objects.create(
            user=self.unrelated_user,
            tenant=self.customer,
            is_active=True,
        )
        self.unrelated_grant = RoleGrant.objects.create(
            membership=self.unrelated_membership,
            role=self.customer_role,
            reason="Unrelated provider-order fixture",
            valid_until=timezone.now() + timedelta(days=1),
        )
        RoleGrantScope.objects.create(
            role_grant=self.unrelated_grant,
            scope_type=RoleGrantScope.SCOPE_OWN,
        )
        self.unrelated_group = UserGroup.objects.create(
            tenant=self.customer,
            name="Unrelated customer group",
            slug="unrelated-customer-group",
        )
        self.unrelated_group_membership = GroupMembership.objects.create(
            user_group=self.unrelated_group,
            membership=self.unrelated_membership,
        )
        self.unrelated_holder = AssetHolder._base_manager.create(
            user=self.unrelated_user,
            tenant=self.customer,
            first_name="Unrelated",
            last_name="Identity",
            upn=self.unrelated_user.email,
            email=self.unrelated_user.email,
        )
        set_current_tenant(self.customer)

    def tearDown(self):
        set_current_tenant(None)

    @staticmethod
    def _table_fingerprint(model):
        field_names = tuple(field.attname for field in model._meta.concrete_fields)
        return tuple(tuple(row) for row in model._base_manager.order_by("pk").values_list(*field_names))

    def _state_fingerprint(self):
        return {
            "identity": {model._meta.label_lower: self._table_fingerprint(model) for model in (User, OIDCIdentity)},
            "organization": {
                model._meta.label_lower: self._table_fingerprint(model)
                for model in (
                    Tenant,
                    Role,
                    AssetHolder,
                    Membership,
                    RoleGrant,
                    RoleGrantScope,
                    UserGroup,
                    GroupMembership,
                )
            },
            "audit": {model._meta.label_lower: self._table_fingerprint(model) for model in (ObjectChange, AuditEvent)},
        }

    def _assert_preexisting_rows_unchanged(self, before, after):
        for section, tables in before.items():
            for label, rows in tables.items():
                for row in rows:
                    self.assertGreaterEqual(after[section][label].count(row), rows.count(row))

    def _unrelated_snapshot(self):
        return {
            "user": User._base_manager.values_list(
                "pk",
                "username",
                "email",
                "first_name",
                "last_name",
                "can_login",
            ).get(pk=self.unrelated_user.pk),
            "memberships": tuple(
                Membership._base_manager.filter(user=self.unrelated_user)
                .order_by("pk")
                .values_list("pk", "tenant_id", "is_active")
            ),
            "grants": tuple(
                RoleGrant._base_manager.filter(membership__user=self.unrelated_user)
                .order_by("pk")
                .values_list("pk", "membership_id", "role_id")
            ),
            "scopes": tuple(
                RoleGrantScope._base_manager.filter(role_grant__membership__user=self.unrelated_user)
                .order_by("pk")
                .values_list("pk", "role_grant_id", "scope_type", "tenant_id", "tenant_group_id")
            ),
            "groups": tuple(
                GroupMembership._base_manager.filter(membership__user=self.unrelated_user)
                .order_by("pk")
                .values_list("pk", "user_group_id", "membership_id", "source", "external_id")
            ),
            "holders": tuple(
                AssetHolder._base_manager.filter(user=self.unrelated_user)
                .order_by("pk")
                .values_list("pk", "user_id", "tenant_id", "upn", "email", "first_name", "last_name")
            ),
        }

    def _resolve(self, subject, claims, provisioner=organization_identity_provisioner):
        userinfo = {
            "iss": ISSUER,
            "sub": subject,
            **claims,
        }
        backend = TenantOIDCBackend()
        backend.get_userinfo = Mock(return_value=userinfo)
        backend.verify_claims = Mock(return_value=True)
        with identity_provisioning.override_identity_provisioner(provisioner):
            return backend.get_or_create_user(
                "access-token",
                "id-token",
                {"iss": ISSUER, "sub": subject},
            )

    def _create_bound_user(self, username, email, subject, first_name="Initial", last_name="Profile"):
        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )
        OIDCIdentity.objects.create(user=user, issuer=ISSUER, subject=subject)
        return user

    def _assert_provider_only(self, user, unrelated_before):
        self.assertEqual(Membership._base_manager.filter(user=user, tenant=self.provider).count(), 1)
        self.assertEqual(
            Membership._base_manager.filter(user=user, tenant=self.provider, is_active=True).count(),
            1,
        )
        self.assertFalse(Membership._base_manager.filter(user=user, tenant=self.customer).exists())
        self.assertFalse(
            RoleGrant._base_manager.filter(membership__user=user, membership__tenant=self.customer).exists()
        )
        self.assertFalse(
            GroupMembership._base_manager.filter(membership__user=user, user_group__tenant=self.customer).exists()
        )
        self.assertFalse(AssetHolder._base_manager.filter(user=user, tenant=self.customer).exists())
        provider_membership = Membership._base_manager.get(user=user, tenant=self.provider)
        self.assertFalse(RoleGrant._base_manager.filter(membership=provider_membership).exists())
        self.assertEqual(self._unrelated_snapshot(), unrelated_before)

    def test_customer_first_then_provider_and_provider_first_then_stale_customer_are_provider_only(self):
        unrelated_before = self._unrelated_snapshot()
        customer_first = self._create_bound_user(
            "provider-order-customer-first",
            "provider-order-customer-first@example.test",
            "provider-order-customer-first-subject",
        )
        customer_result = self._resolve(
            "provider-order-customer-first-subject",
            {
                "email": customer_first.email,
                "given_name": "Customer",
                "family_name": "First",
                "groups": ["customer-admin"],
            },
        )
        self.assertEqual(customer_result.pk, customer_first.pk)
        provider_result = self._resolve(
            "provider-order-customer-first-subject",
            {
                "email": customer_first.email,
                "given_name": "Provider",
                "family_name": "First",
                "groups": ["provider-first", "provider-second", "customer-admin"],
            },
        )
        self.assertEqual(provider_result.pk, customer_first.pk)
        customer_first_membership = Membership._base_manager.get(user=customer_first, tenant=self.provider)
        self.assertFalse(RoleGrant._base_manager.filter(membership=customer_first_membership).exists())
        self.assertEqual(
            OIDCIdentity.objects.get(issuer=ISSUER, subject="provider-order-customer-first-subject").user_id,
            customer_first.pk,
        )
        self._assert_provider_only(customer_first, unrelated_before)
        self.assertEqual(
            AssetHolder._base_manager.filter(tenant=self.customer, upn=customer_first.email).count(),
            1,
        )
        self.assertIsNone(AssetHolder._base_manager.get(tenant=self.customer, upn=customer_first.email).user_id)

        provider_first = self._create_bound_user(
            "provider-order-provider-first",
            "provider-order-provider-first@example.test",
            "provider-order-provider-first-subject",
        )
        first_provider_result = self._resolve(
            "provider-order-provider-first-subject",
            {
                "email": provider_first.email,
                "given_name": "Provider",
                "family_name": "First",
                "groups": ["provider-first", "provider-second", "customer-admin"],
            },
        )
        self.assertEqual(first_provider_result.pk, provider_first.pk)
        self.assertFalse(
            RoleGrant._base_manager.filter(
                membership__user=provider_first,
                membership__tenant=self.provider,
                role=self.provider_first_role,
            ).exists()
        )
        stale_customer_result = self._resolve(
            "provider-order-provider-first-subject",
            {
                "email": provider_first.email,
                "given_name": "Stale",
                "family_name": "Customer",
                "groups": ["customer-admin"],
            },
        )
        self.assertEqual(stale_customer_result.pk, provider_first.pk)
        self._assert_provider_only(provider_first, unrelated_before)

    def test_provider_claim_order_uses_first_mapping_and_real_role_result(self):
        scenarios = (
            ("provider-first", ("provider-first", "provider-second"), self.provider_first_role),
            ("provider-second", ("provider-second", "provider-first"), self.provider_second_role),
        )
        for label, groups, expected_role in scenarios:
            with self.subTest(order=groups):
                subject = f"provider-order-real-{label}-subject"
                email = f"provider-order-real-{label}@example.test"
                user = self._create_bound_user(
                    f"provider-order-real-{label}",
                    email,
                    subject,
                    first_name="Provider",
                    last_name="Order",
                )
                unrelated_before = self._unrelated_snapshot()
                before = self._state_fingerprint()
                recorder = RecordingIdentityProvisioner()

                resolved = self._resolve(
                    subject,
                    {
                        "email": email,
                        "given_name": "Provider",
                        "family_name": "Order",
                        "groups": list(groups),
                    },
                    recorder,
                )

                self.assertEqual(resolved.pk, user.pk)
                self.assertEqual(len(recorder.commands), 1)
                self.assertEqual(len(recorder.results), 1)
                command = recorder.commands[0]
                self.assertIsNotNone(command.provider_staff)
                self.assertEqual(command.provider_staff.role_name, expected_role.name)
                result = recorder.results[0]
                self.assertEqual(result.mode, "provider_staff")
                self.assertEqual(result.role_id, expected_role.pk)
                self._assert_provider_only(user, unrelated_before)
                after = self._state_fingerprint()
                self._assert_preexisting_rows_unchanged(before, after)
                self.assertEqual(self._unrelated_snapshot(), unrelated_before)

    def test_missing_first_provider_role_is_terminal_without_fallback_for_both_orders(self):
        scenarios = (
            ("provider-first", ("provider-first", "provider-second")),
            ("provider-second", ("provider-second", "provider-first")),
        )
        for label, groups in scenarios:
            first_group, later_group = groups
            missing_role = f"Missing {first_group} role"
            valid_role = self.provider_second_role if later_group == "provider-second" else self.provider_first_role
            settings = {
                **PROVIDER_ORDER_CONFIG,
                "phase-b-provider": {
                    **PROVIDER_ORDER_CONFIG["phase-b-provider"],
                    "OIDC_GROUP_PROVIDER_ROLE_MAPPING": {
                        first_group: missing_role,
                        later_group: valid_role.name,
                    },
                },
            }
            with self.subTest(order=groups, missing_group=first_group):
                existing_subject = f"provider-order-existing-missing-{label}-subject"
                existing = self._create_bound_user(
                    f"provider-order-existing-missing-{label}",
                    f"provider-order-existing-missing-{label}@example.test",
                    existing_subject,
                    first_name="Old",
                    last_name="Profile",
                )
                before = self._state_fingerprint()
                recorder = RecordingIdentityProvisioner()
                with override_settings(ITAMBOX_TENANT_OIDC_CONFIGS=settings):
                    existing_result = self._resolve(
                        existing_subject,
                        {
                            "email": "should-not-update@example.test",
                            "given_name": "Should",
                            "family_name": "NotUpdate",
                            "groups": [*groups, "customer-admin"],
                        },
                        recorder,
                    )

                self.assertEqual(existing_result.pk, existing.pk)
                self.assertEqual(len(recorder.commands), 1)
                self.assertEqual(len(recorder.results), 1)
                self.assertEqual(recorder.commands[0].provider_staff.role_name, missing_role)
                self.assertEqual(recorder.results[0].mode, "provider_mapping_rejected")
                self.assertIsNone(recorder.results[0].role_id)
                existing.refresh_from_db()
                self.assertEqual(
                    (existing.username, existing.email, existing.first_name, existing.last_name),
                    (existing.username, "should-not-update@example.test", "Should", "NotUpdate"),
                )
                self.assertEqual(self._state_fingerprint()["organization"], before["organization"])
                self.assertFalse(Membership._base_manager.filter(user=existing).exists())
                self.assertFalse(AssetHolder._base_manager.filter(user=existing).exists())
                self.assertFalse(RoleGrant._base_manager.filter(membership__user=existing).exists())
                self.assertFalse(GroupMembership._base_manager.filter(membership__user=existing).exists())

                new_subject = f"provider-order-new-missing-{label}-subject"
                before_new = self._state_fingerprint()
                before_user_count = User._base_manager.count()
                before_binding_count = OIDCIdentity._base_manager.count()
                new_recorder = RecordingIdentityProvisioner()
                with override_settings(ITAMBOX_TENANT_OIDC_CONFIGS=settings):
                    new_result = self._resolve(
                        new_subject,
                        {
                            "email": f"provider-order-new-missing-{label}@example.test",
                            "given_name": "New",
                            "family_name": "Missing",
                            "groups": [*groups, "customer-admin"],
                        },
                        new_recorder,
                    )

                self.assertIsNotNone(new_result)
                self.assertEqual(len(new_recorder.commands), 1)
                self.assertEqual(len(new_recorder.results), 1)
                self.assertEqual(new_recorder.commands[0].provider_staff.role_name, missing_role)
                self.assertEqual(new_recorder.results[0].mode, "provider_mapping_rejected")
                self.assertIsNone(new_recorder.results[0].role_id)
                new_binding = OIDCIdentity.objects.get(issuer=ISSUER, subject=new_subject)
                self.assertEqual(new_binding.user_id, new_result.pk)
                new_user = User._base_manager.get(pk=new_result.pk)
                self.assertEqual(new_user.email, f"provider-order-new-missing-{label}@example.test")
                self.assertEqual(new_user.first_name, "New")
                self.assertEqual(new_user.last_name, "Missing")
                self.assertEqual(User._base_manager.count(), before_user_count + 1)
                self.assertEqual(OIDCIdentity._base_manager.count(), before_binding_count + 1)
                after_new = self._state_fingerprint()
                self.assertEqual(after_new["organization"], before_new["organization"])
                self._assert_preexisting_rows_unchanged(before_new, after_new)
                self.assertFalse(Membership._base_manager.filter(user=new_result).exists())
                self.assertFalse(AssetHolder._base_manager.filter(user=new_result).exists())
                self.assertFalse(RoleGrant._base_manager.filter(membership__user=new_result).exists())
                self.assertFalse(GroupMembership._base_manager.filter(membership__user=new_result).exists())
