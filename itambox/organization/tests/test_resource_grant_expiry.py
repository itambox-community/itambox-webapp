import datetime
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError, close_old_connections, connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from assets.models import Manufacturer
from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from core.tasks.context import TaskContext
from core.tasks.resource_grants import (
    CODE_NO_DUE,
    CODE_PARTIAL,
    CODE_STALE_DELIVERY,
    CODE_SUCCESS,
    CODE_TENANT_UNRESOLVABLE,
    RETRY_DELAYS,
    STATE_COMPLETE,
    STATE_ENQUEUE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    _complete_run,
    _repair_run,
    _retry_or_exhaust,
    coordinate_resource_grant_expiry,
    sweep_expired_resource_grants,
)
from core.tasks.utils import RetryableTaskError, TaskStatus, TerminalTaskError
from core.tests.mixins import TenantTestMixin
from extras.models import Event, EventRule
from inventory.models import Accessory, AccessoryStock
from organization.admin import TenantResourceGrantExpiryRevocationAdmin
from organization.models import (
    Location,
    Membership,
    Role,
    RoleGrant,
    RoleGrantScope,
    Site,
    Tenant,
    TenantGroup,
    TenantResourceGrant,
    TenantResourceGrantExpiryRevocation,
    TenantResourceGrantExpiryRun,
)
from organization.services.resource_grants import (
    InvalidResourceGrantError,
    _validate_expiry_candidate,
    restore_resource_grant,
    revoke_resource_grant,
)


@pytest.mark.serial_only
class ResourceGrantExpiryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = Tenant.objects.create(name="Expiry Owner", slug="expiry-owner")
        cls.grantee = Tenant.objects.create(name="Expiry Grantee", slug="expiry-grantee")
        site = Site.objects.create(name="Expiry Site", slug="expiry-site", tenant=cls.owner)
        location = Location.objects.create(
            name="Expiry Location",
            slug="expiry-location",
            site=site,
            tenant=cls.owner,
        )
        manufacturer = Manufacturer.objects.create(name="Expiry Manufacturer", slug="expiry-manufacturer")
        accessory = Accessory.objects.create(
            name="Expiry Accessory",
            slug="expiry-accessory",
            tenant=cls.owner,
            manufacturer=manufacturer,
        )
        cls.stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        cls.content_type = ContentType.objects.get_for_model(AccessoryStock)

    def _grant(self, *, valid_until=None, deleted_at=None, **extra):
        grant = TenantResourceGrant(
            tenant=self.owner,
            grantee_tenant=self.grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=valid_until,
            **extra,
        )
        grant.save()
        if deleted_at is not None:
            TenantResourceGrant._base_manager.filter(pk=grant.pk).update(deleted_at=deleted_at)
            grant.refresh_from_db()
        return grant

    def _run(self, cutoff):
        return TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.owner,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + datetime.timedelta(minutes=1),
        )

    def test_s1_null_deadline_is_skipped(self):
        self._grant()
        cutoff = timezone.now()
        result = sweep_expired_resource_grants(self.owner.pk, self._run(cutoff).pk, 1)
        assert result.code == CODE_NO_DUE
        assert result.status.value == "skipped"
        assert ObjectChange._base_manager.filter(changed_object_id=self.stock.pk, action="delete").count() == 0

    def test_s2_future_deadline_is_skipped(self):
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff + datetime.timedelta(minutes=1))
        result = sweep_expired_resource_grants(self.owner.pk, self._run(cutoff).pk, 1)
        grant.refresh_from_db()
        assert grant.is_active
        assert result.code == CODE_NO_DUE

    def test_s3_exact_deadline_revokes_once_with_evidence(self):
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff)
        run = self._run(cutoff)
        result = sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        grant.refresh_from_db()
        run.refresh_from_db()
        evidence = TenantResourceGrantExpiryRevocation._base_manager.get(run=run, grant=grant)
        assert result.code == CODE_SUCCESS
        assert grant.deleted_at == evidence.revoked_at
        assert evidence.triggering_valid_until == cutoff
        assert evidence.object_change.user_id is None
        assert evidence.object_change.action == ObjectChangeActionChoices.ACTION_DELETE
        assert run.revoked_count == 1

    def test_s4_overdue_deadline_is_preserved_as_evidence(self):
        cutoff = timezone.now()
        deadline = cutoff - datetime.timedelta(hours=1)
        grant = self._grant(valid_until=deadline)
        run = self._run(cutoff)
        sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        evidence = TenantResourceGrantExpiryRevocation._base_manager.get(run=run, grant=grant)
        assert evidence.triggering_valid_until == deadline

    def test_d9_expiry_event_is_minimal_and_emitted_once(self):
        # Events are only recorded for models some rule subscribes to (#621).
        EventRule.objects.create(
            name="grant lifecycle",
            model=ContentType.objects.get_for_model(TenantResourceGrant),
            events=["delete"],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff)
        run = self._run(cutoff)
        with self.captureOnCommitCallbacks(execute=True):
            sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        grant_type = ContentType.objects.get_for_model(TenantResourceGrant)
        events = Event._base_manager.filter(model=grant_type, object_id=grant.pk, action=Event.ACTION_DELETE)
        assert events.count() == 1
        assert events.get().data == {
            "app_label": "organization",
            "model_name": "tenantresourcegrant",
            "tenant_scope": "tenant",
        }

    def test_s5_deleted_grant_is_not_eligible(self):
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff - datetime.timedelta(minutes=1), deleted_at=cutoff)
        run = self._run(cutoff)
        result = sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        grant.refresh_from_db()
        assert result.code == CODE_NO_DUE
        assert grant.deleted_at == cutoff

    def test_s6_other_tenant_is_not_counted(self):
        other = Tenant.objects.create(name="Expiry Other", slug="expiry-other")
        other_grantee = Tenant.objects.create(name="Expiry Other Grantee", slug="expiry-other-grantee")
        cutoff = timezone.now()
        self._grant(valid_until=cutoff - datetime.timedelta(minutes=1))
        other_grant = TenantResourceGrant(
            tenant=other,
            grantee_tenant=other_grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=cutoff - datetime.timedelta(minutes=1),
        )
        # bulk_create bypasses the model clean() ownership proof on purpose:
        # this row is a foreign-tenant grant and must be exercisable as data.
        TenantResourceGrant._base_manager.bulk_create([other_grant])
        run = self._run(cutoff)
        sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        other_grant.refresh_from_db()
        assert other_grant.deleted_at is None

    def test_s7_malformed_grant_stays_live(self):
        cutoff = timezone.now()
        # A grant whose resource type is outside the approved allowlist is
        # malformed but insertable (limit_choices_to is not a DB constraint);
        # bulk_create bypasses the model clean() allowlist check.
        foreign_type = ContentType.objects.get_for_model(Accessory)
        malformed = TenantResourceGrant(
            tenant=self.owner,
            grantee_tenant=self.grantee,
            resource_type=foreign_type,
            resource_id=self.stock.pk,
            valid_until=cutoff - datetime.timedelta(minutes=1),
        )
        TenantResourceGrant._base_manager.bulk_create([malformed])
        run = self._run(cutoff)
        with pytest.raises(TerminalTaskError):
            # The malformed-only run is a terminal typed task boundary.
            sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        malformed.refresh_from_db()
        assert malformed.deleted_at is None

    def test_s8_unrelated_role_deadline_is_not_queried(self):
        role = Role.objects.create(tenant=self.owner, name="Expiry unrelated role", permissions=[])
        user = self._user("expiry-unrelated")
        membership = Membership.objects.create(user=user, tenant=self.owner)
        role_grant = RoleGrant.objects.create(
            membership=membership,
            role=role,
            valid_until=timezone.now() - datetime.timedelta(minutes=1),
        )
        RoleGrantScope.objects.create(role_grant=role_grant, scope_type=RoleGrantScope.SCOPE_OWN)
        run = self._run(timezone.now())
        result = sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        assert result.code == CODE_NO_DUE
        # RoleGrant has no deleted_at; the sweep must simply not touch it.
        assert RoleGrant.objects.filter(pk=role_grant.pk).exists()

    def test_s10_second_run_loser_has_no_invented_revocation_count(self):
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff)
        first = self._run(cutoff)
        second = self._run(cutoff + datetime.timedelta(hours=1))
        sweep_expired_resource_grants(self.owner.pk, first.pk, 1)
        loser = sweep_expired_resource_grants(self.owner.pk, second.pk, 1)
        second.refresh_from_db()
        assert loser.status.value == "skipped"
        assert second.revoked_count == 0
        assert TenantResourceGrantExpiryRevocation._base_manager.filter(grant=grant).count() == 1

    def test_s12_scope_failure_and_missing_run_are_typed_and_redacted(self):
        other = Tenant.objects.create(name="Expiry Scope Other", slug="expiry-scope-other")
        run = self._run(timezone.now())
        mismatched = sweep_expired_resource_grants(other.pk, run.pk, 1)
        run.refresh_from_db()
        assert mismatched.code == CODE_TENANT_UNRESOLVABLE
        assert run.state == TenantResourceGrantExpiryRun.STATE_COMPLETE
        assert run.outcome == "terminal"
        missing = sweep_expired_resource_grants(self.owner.pk, run.pk + 100000, 1)
        assert missing.code == CODE_TENANT_UNRESOLVABLE

    def test_s11_transient_failure_retries_remaining_rows_without_duplicate_evidence(self):
        cutoff = timezone.now()
        second_grantee = Tenant.objects.create(name="Expiry Retry Grantee", slug="expiry-retry-grantee")
        first_grant = self._grant(valid_until=cutoff)
        second_grant = TenantResourceGrant._base_manager.create(
            tenant=self.owner,
            grantee_tenant=second_grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=cutoff,
        )
        run = self._run(cutoff)
        from organization.services.resource_grants import revoke_resource_grant

        calls = 0

        def fail_second_call(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OperationalError("transient database boundary")
            return revoke_resource_grant(*args, **kwargs)

        with patch("organization.services.resource_grants.revoke_resource_grant", side_effect=fail_second_call):
            with pytest.raises(RetryableTaskError):
                with self.captureOnCommitCallbacks(execute=True):
                    sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        run.refresh_from_db()
        assert run.state == TenantResourceGrantExpiryRun.STATE_QUEUED
        assert run.outcome == TaskStatus.RETRYABLE.value
        assert run.generation == 2
        assert run.revoked_count == 1
        TenantResourceGrantExpiryRun._base_manager.filter(pk=run.pk).update(
            next_retry_at=cutoff - datetime.timedelta(seconds=1),
            dispatch_stale_at=cutoff - datetime.timedelta(seconds=1),
        )
        retry = sweep_expired_resource_grants(self.owner.pk, run.pk, 2)
        run.refresh_from_db()
        assert retry.code == CODE_SUCCESS
        assert run.outcome == TaskStatus.SUCCESS.value
        assert TenantResourceGrantExpiryRevocation._base_manager.filter(run=run).count() == 2
        assert (
            TenantResourceGrant._base_manager.filter(
                pk__in=[first_grant.pk, second_grant.pk], deleted_at__isnull=True
            ).count()
            == 0
        )

    def test_s19_enqueue_failure_is_persisted_after_commit(self):
        with patch("core.tasks.resource_grants.async_task", side_effect=RuntimeError("queue secret")):
            with self.captureOnCommitCallbacks(execute=True):
                coordinate_resource_grant_expiry()
        run = TenantResourceGrantExpiryRun._base_manager.filter(tenant=self.owner).latest("pk")
        assert run.state == TenantResourceGrantExpiryRun.STATE_ENQUEUE_FAILED
        assert run.outcome == "retryable"
        assert run.error_code == "resource_grant_expiry_enqueue_failed"
        assert "queue secret" not in (run.error_message or "")

    def test_s20_completed_run_rejects_stale_status_write(self):
        cutoff = timezone.now()
        run = self._run(cutoff)
        sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        run.refresh_from_db()
        outcome = run.outcome
        assert not _complete_run(
            run.pk,
            1,
            outcome=TaskStatus.SKIPPED,
            code="resource_grant_expiry_succeeded",
            message="",
            counts={"revoked": 0, "remaining_due": 0, "invalid": 0},
        )
        run.refresh_from_db()
        assert run.outcome == outcome

    def test_s22_every_invalid_state_combination_is_rejected(self):
        invalid_states = (
            {"state": "running", "outcome": "success", "finished_at": None},
            {"state": "complete", "outcome": "retryable", "finished_at": timezone.now()},
            {"state": "enqueue_failed", "outcome": None, "finished_at": None},
            {"state": "complete", "outcome": "success", "finished_at": None},
        )
        for index, values in enumerate(invalid_states):
            with self.subTest(index=index), pytest.raises(IntegrityError), transaction.atomic():
                TenantResourceGrantExpiryRun._base_manager.create(
                    tenant=self.owner,
                    schedule_slot=timezone.now() + datetime.timedelta(seconds=index + 1),
                    cutoff=timezone.now(),
                    lease_expires_at=timezone.now() if values["state"] == "running" else None,
                    **values,
                )

    def test_integrity_valid_excludes_corrupt_bulk_evidence(self):
        other = Tenant.objects.create(name="Expiry Evidence Other", slug="expiry-evidence-other")
        other_grantee = Tenant.objects.create(name="Expiry Evidence Grantee", slug="expiry-evidence-grantee")
        cutoff = timezone.now()
        run = self._run(cutoff)
        grant = self._grant(valid_until=cutoff)
        foreign_grant = TenantResourceGrant(
            tenant=other,
            grantee_tenant=other_grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=cutoff,
        )
        # bulk_create bypasses the ownership proof: the row is intentionally
        # foreign and must exist as data for the corrupt-evidence scenario.
        TenantResourceGrant._base_manager.bulk_create([foreign_grant])
        wrong_change = ObjectChange._base_manager.create(
            tenant=self.owner,
            changed_object_type=ContentType.objects.get_for_model(TenantResourceGrant),
            changed_object_id=grant.pk,
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            object_repr="corrupt evidence",
            object_type_repr="organization | tenantresourcegrant",
            user_name="System",
            request_id=uuid.uuid4(),
        )
        TenantResourceGrantExpiryRevocation._base_manager.bulk_create(
            [
                TenantResourceGrantExpiryRevocation(
                    run=run,
                    grant=foreign_grant,
                    triggering_valid_until=cutoff,
                    revoked_at=cutoff,
                    request_id=uuid.uuid4(),
                ),
                TenantResourceGrantExpiryRevocation(
                    run=run,
                    grant=grant,
                    object_change=wrong_change,
                    triggering_valid_until=cutoff,
                    revoked_at=cutoff + datetime.timedelta(seconds=1),
                    request_id=uuid.uuid4(),
                ),
            ]
        )
        assert TenantResourceGrantExpiryRevocation._base_manager.integrity_valid().filter(run=run).count() == 0

    def test_s23_stale_queued_generation_is_repaired_after_status_write_loss(self):
        cutoff = timezone.now() - datetime.timedelta(minutes=2)
        run = self._run(cutoff)
        run.dispatch_stale_at = cutoff
        run.save(update_fields=["dispatch_stale_at"])
        with patch("core.tasks.resource_grants._enqueue_now"):
            delivery = _repair_run(run, timezone.now())
        run.refresh_from_db()
        assert delivery == (run.pk, 2)
        assert run.generation == 2
        assert sweep_expired_resource_grants(self.owner.pk, run.pk, 1).code == CODE_STALE_DELIVERY

    def test_s9_second_run_is_idempotent(self):
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff)
        first = self._run(cutoff)
        sweep_expired_resource_grants(self.owner.pk, first.pk, 1)
        second = self._run(cutoff + datetime.timedelta(hours=1))
        sweep_expired_resource_grants(self.owner.pk, second.pk, 1)
        assert TenantResourceGrantExpiryRevocation._base_manager.filter(grant=grant).count() == 1

    def test_s17_duplicate_delivery_is_stale(self):
        cutoff = timezone.now()
        run = self._run(cutoff)
        result = sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        duplicate = sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        assert result.status.value == "skipped"
        assert duplicate.code != CODE_SUCCESS

    def test_s18_stale_completion_does_not_change_new_generation(self):
        now = timezone.now()
        run = self._run(now)
        run.state = TenantResourceGrantExpiryRun.STATE_RUNNING
        run.lease_expires_at = now + datetime.timedelta(minutes=5)
        run.save(update_fields=["state", "lease_expires_at"])
        run.refresh_from_db()
        run.generation += 1
        run.state = TenantResourceGrantExpiryRun.STATE_QUEUED
        run.lease_expires_at = None
        run.next_retry_at = None
        run.dispatch_stale_at = now + datetime.timedelta(minutes=1)
        run.save(update_fields=["generation", "state", "lease_expires_at", "next_retry_at", "dispatch_stale_at"])
        assert not TenantResourceGrantExpiryRun._base_manager.filter(
            pk=run.pk, state=TenantResourceGrantExpiryRun.STATE_RUNNING, generation=1
        ).update(state=TenantResourceGrantExpiryRun.STATE_COMPLETE)
        run.refresh_from_db()
        assert run.generation == 2

    def test_s20_completed_run_counts_are_durable(self):
        cutoff = timezone.now()
        run = self._run(cutoff)
        run.state = TenantResourceGrantExpiryRun.STATE_COMPLETE
        run.outcome = "skipped"
        run.finished_at = cutoff
        run.next_retry_at = None
        run.save(update_fields=["state", "outcome", "finished_at", "next_retry_at"])
        assert TenantResourceGrantExpiryRun._base_manager.get(pk=run.pk).outcome == "skipped"

    def test_s21_stale_target_still_revokes(self):
        cutoff = timezone.now()
        grant = self._grant(valid_until=cutoff)
        self.stock.delete()
        run = self._run(cutoff)
        sweep_expired_resource_grants(self.owner.pk, run.pk, 1)
        grant.refresh_from_db()
        assert grant.deleted_at is not None

    def test_s22_constraint_rejects_invalid_run_state(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            TenantResourceGrantExpiryRun._base_manager.create(
                tenant=self.owner,
                schedule_slot=timezone.now(),
                cutoff=timezone.now(),
                state=TenantResourceGrantExpiryRun.STATE_RUNNING,
                outcome="success",
            )

    def test_s23_coordinator_repairs_stale_queue(self):
        cutoff = timezone.now()
        run = self._run(cutoff)
        run.dispatch_stale_at = cutoff - datetime.timedelta(minutes=1)
        run.save(update_fields=["dispatch_stale_at"])
        with patch("core.tasks.resource_grants.async_task"):
            with self.captureOnCommitCallbacks(execute=True):
                coordinate_resource_grant_expiry()
        run.refresh_from_db()
        assert run.generation >= 2

    def _terminal_run(self, finished_at, *, suffix, outcome="success"):
        return TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.owner,
            schedule_slot=finished_at.replace(minute=0, second=0, microsecond=0)
            + datetime.timedelta(hours=len(suffix)),
            cutoff=finished_at,
            state=TenantResourceGrantExpiryRun.STATE_COMPLETE,
            outcome=outcome,
            finished_at=finished_at,
        )

    def test_retention_prunes_terminal_runs_and_evidence_only(self):
        old = timezone.now() - datetime.timedelta(days=2)
        grant = self._grant(valid_until=old, deleted_at=old)
        terminal = self._terminal_run(old, suffix="old")
        evidence = TenantResourceGrantExpiryRevocation._base_manager.create(
            run=terminal,
            grant=grant,
            triggering_valid_until=old,
            revoked_at=old,
            request_id=uuid.uuid4(),
        )
        queued = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.owner,
            schedule_slot=old + datetime.timedelta(hours=10),
            cutoff=old,
            state=TenantResourceGrantExpiryRun.STATE_QUEUED,
            outcome=TaskStatus.RETRYABLE.value,
            next_retry_at=old,
        )
        running = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.owner,
            schedule_slot=old + datetime.timedelta(hours=11),
            cutoff=old,
            state=TenantResourceGrantExpiryRun.STATE_RUNNING,
            lease_expires_at=timezone.now() + datetime.timedelta(minutes=5),
        )
        enqueue_failed = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.owner,
            schedule_slot=old + datetime.timedelta(hours=12),
            cutoff=old,
            state=TenantResourceGrantExpiryRun.STATE_ENQUEUE_FAILED,
            outcome=TaskStatus.RETRYABLE.value,
        )
        late = self._terminal_run(timezone.now(), suffix="late", outcome="partial")
        call_command(
            "prune_changelog",
            classes="changelog",
            changelog_days=1,
            tenant=self.owner.slug,
        )
        assert not TenantResourceGrantExpiryRun._base_manager.filter(pk=terminal.pk).exists()
        assert not TenantResourceGrantExpiryRevocation._base_manager.filter(pk=evidence.pk).exists()
        for preserved in (queued, running, enqueue_failed, late):
            assert TenantResourceGrantExpiryRun._base_manager.filter(pk=preserved.pk).exists()

    def test_retention_zero_tenant_override_is_a_legal_hold(self):
        old = timezone.now() - datetime.timedelta(days=2)
        terminal = self._terminal_run(old, suffix="legal-hold")
        self.owner.changelog_retention_days = 0
        self.owner.save(update_fields=["changelog_retention_days", "updated_at"])
        call_command(
            "prune_changelog",
            classes="changelog",
            changelog_days=1,
            tenant=self.owner.slug,
        )
        assert TenantResourceGrantExpiryRun._base_manager.filter(pk=terminal.pk).exists()

    def _user(self, suffix):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(username=suffix, password="x")


@pytest.mark.serial_only
class ResourceGrantExpiryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = Tenant.objects.create(name="Expiry Race Owner", slug="expiry-race-owner")
        self.grantee = Tenant.objects.create(name="Expiry Race Grantee", slug="expiry-race-grantee")
        site = Site.objects.create(name="Expiry Race Site", slug="expiry-race-site", tenant=self.owner)
        location = Location.objects.create(
            name="Expiry Race Location",
            slug="expiry-race-location",
            site=site,
            tenant=self.owner,
        )
        manufacturer = Manufacturer.objects.create(name="Expiry Race Manufacturer", slug="expiry-race-manufacturer")
        accessory = Accessory.objects.create(
            name="Expiry Race Accessory",
            slug="expiry-race-accessory",
            tenant=self.owner,
            manufacturer=manufacturer,
        )
        self.stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        self.content_type = ContentType.objects.get_for_model(AccessoryStock)

    def test_s10_real_two_worker_race_has_one_transition(self):
        cutoff = timezone.now()
        grant = TenantResourceGrant._base_manager.create(
            tenant=self.owner,
            grantee_tenant=self.grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=cutoff,
        )
        runs = [
            TenantResourceGrantExpiryRun._base_manager.create(
                tenant=self.owner,
                schedule_slot=cutoff + datetime.timedelta(hours=index),
                cutoff=cutoff,
                dispatch_stale_at=cutoff + datetime.timedelta(minutes=1),
            )
            for index in (0, 1)
        ]

        def invoke(run_id):
            close_old_connections()
            try:
                return sweep_expired_resource_grants(self.owner.pk, run_id, 1)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(invoke, (runs[0].pk, runs[1].pk)))
        grant.refresh_from_db()
        assert grant.deleted_at is not None
        assert TenantResourceGrantExpiryRevocation._base_manager.filter(grant=grant).count() == 1
        assert (
            ObjectChange._base_manager.filter(
                changed_object_id=grant.pk,
                action=ObjectChangeActionChoices.ACTION_DELETE,
            ).count()
            == 1
        )
        assert sorted(result.status.value for result in results) == ["skipped", "success"]


@pytest.mark.serial_only
class ResourceGrantRollbackTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Expiry Rollback Owner",
            slug="expiry-rollback-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        self.grantee = Tenant.objects.create(name="Expiry Rollback Grantee", slug="expiry-rollback-grantee")
        site = Site.objects.create(name="Expiry Rollback Site", slug="expiry-rollback-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Expiry Rollback Location",
            slug="expiry-rollback-location",
            site=site,
            tenant=self.tenant,
        )
        manufacturer = Manufacturer.objects.create(
            name="Expiry Rollback Manufacturer", slug="expiry-rollback-manufacturer"
        )
        accessory = Accessory.objects.create(
            name="Expiry Rollback Accessory",
            slug="expiry-rollback-accessory",
            tenant=self.tenant,
            manufacturer=manufacturer,
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        content_type = ContentType.objects.get_for_model(AccessoryStock)
        cutoff = timezone.now()
        self.grant = TenantResourceGrant._base_manager.create(
            tenant=self.tenant,
            grantee_tenant=self.grantee,
            resource_type=content_type,
            resource_id=stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=cutoff,
        )
        run = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.tenant,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + datetime.timedelta(minutes=1),
        )
        sweep_expired_resource_grants(self.tenant.pk, run.pk, 1)
        # Events are only recorded for models some rule subscribes to (#621).
        EventRule.objects.create(
            name="grant lifecycle",
            model=ContentType.objects.get_for_model(TenantResourceGrant),
            events=["restore"],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )

    def test_s16_command_restores_one_grant_after_clearing_deadline(self):
        evidence_count = TenantResourceGrantExpiryRevocation._base_manager.filter(grant=self.grant).count()
        with self.captureOnCommitCallbacks(execute=True):
            call_command(
                "restore_resource_grant",
                grant=self.grant.pk,
                tenant=self.tenant.pk,
                user=self.tenant_user.pk,
                clear_deadline=True,
                confirm=True,
            )
        self.grant.refresh_from_db()
        assert self.grant.deleted_at is None
        assert self.grant.valid_until is None
        assert TenantResourceGrantExpiryRevocation._base_manager.filter(grant=self.grant).count() == evidence_count
        assert ObjectChange._base_manager.filter(
            changed_object_id=self.grant.pk,
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            user_id=self.tenant_user.pk,
        ).exists()
        grant_type = ContentType.objects.get_for_model(TenantResourceGrant)
        assert (
            Event._base_manager.filter(
                model=grant_type,
                object_id=self.grant.pk,
                action=Event.ACTION_RESTORE,
            ).count()
            == 1
        )


class ResourceGrantExpiryCandidateValidationTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Candidate Validation Owner",
            slug="candidate-validation-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        self.grantee = Tenant.objects.create(name="Candidate Validation Grantee", slug="candidate-validation-grantee")
        self.approved_type = ContentType.objects.get_for_model(AccessoryStock)
        self.foreign_type = ContentType.objects.get_for_model(Accessory)

    def _candidate(self, **extra):
        fields = {
            "tenant": self.tenant,
            "grantee_tenant": self.grantee,
            "resource_type": self.approved_type,
            "resource_id": 1,
            "access_level": TenantResourceGrant.ACCESS_VIEW,
        }
        fields.update(extra)
        return TenantResourceGrant(**fields)

    def test_both_grantee_shapes_are_invalid(self):
        group = TenantGroup.objects.create(name="Candidate Validation Group")
        with self.assertRaises(InvalidResourceGrantError):
            _validate_expiry_candidate(self._candidate(grantee_tenant_group=group))

    def test_neither_grantee_shape_is_invalid(self):
        with self.assertRaises(InvalidResourceGrantError):
            _validate_expiry_candidate(self._candidate(grantee_tenant=None))

    def test_missing_resource_type_is_invalid(self):
        with self.assertRaises(InvalidResourceGrantError):
            _validate_expiry_candidate(self._candidate(resource_type=None))

    def test_unapproved_resource_type_is_invalid(self):
        with self.assertRaises(InvalidResourceGrantError):
            _validate_expiry_candidate(self._candidate(resource_type=self.foreign_type))

    def test_cutoff_skips_null_and_future_deadlines(self):
        cutoff = timezone.now()
        self.assertFalse(_validate_expiry_candidate(self._candidate(valid_until=None), cutoff=cutoff))
        self.assertFalse(
            _validate_expiry_candidate(self._candidate(valid_until=cutoff + datetime.timedelta(hours=1)), cutoff=cutoff)
        )
        self.assertTrue(
            _validate_expiry_candidate(
                self._candidate(valid_until=cutoff - datetime.timedelta(minutes=1)), cutoff=cutoff
            )
        )


class ResourceGrantExpiryServiceGuardTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Service Guard Owner",
            slug="service-guard-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        self.grantee = Tenant.objects.create(name="Service Guard Grantee", slug="service-guard-grantee")
        site = Site.objects.create(name="Service Guard Site", slug="service-guard-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Service Guard Location", slug="service-guard-location", site=site, tenant=self.tenant
        )
        manufacturer = Manufacturer.objects.create(name="Service Guard Manufacturer", slug="service-guard-manufacturer")
        accessory = Accessory.objects.create(
            name="Service Guard Accessory",
            slug="service-guard-accessory",
            tenant=self.tenant,
            manufacturer=manufacturer,
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        self.content_type = ContentType.objects.get_for_model(AccessoryStock)
        self.stock = stock

    def _grant(self, **extra):
        grant = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=self.grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            **extra,
        )
        grant.save()
        return grant

    def test_revoke_requires_an_active_request_context(self):
        grant = self._grant()
        with self.assertRaises(PermissionDenied):
            revoke_resource_grant(grant.pk, user=self.tenant_user, active_tenant=self.tenant)

    def test_revoke_actorless_requires_system_authorization(self):
        grant = self._grant()
        with TaskContext(tenant_id=self.tenant.pk, operation="background_task") as context:
            with self.assertRaises(PermissionDenied):
                revoke_resource_grant(grant.pk, user=None, active_tenant=context.tenant)

    def test_revoke_rejects_combined_human_and_system_authorization(self):
        grant = self._grant()
        with TaskContext(tenant_id=self.tenant.pk, operation="background_task") as context:
            system = context.authorize_system(
                permission="organization.delete_tenantresourcegrant",
                operation="organization.resource_grant.expire",
                reason="service guard test",
            )
            with self.assertRaises(PermissionDenied):
                revoke_resource_grant(
                    grant.pk,
                    user=self.tenant_user,
                    active_tenant=context.tenant,
                    system_authorization=system,
                )

    def test_revoke_missing_grant_raises_does_not_exist(self):
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.tenant_user.pk, operation="rollback") as context:
            with self.assertRaises(TenantResourceGrant.DoesNotExist):
                revoke_resource_grant(999_999, user=context.user, active_tenant=context.tenant)

    def test_revoke_already_revoked_returns_none(self):
        grant = self._grant()
        TenantResourceGrant._base_manager.filter(pk=grant.pk).update(deleted_at=timezone.now())
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.tenant_user.pk, operation="rollback") as context:
            self.assertIsNone(revoke_resource_grant(grant.pk, user=context.user, active_tenant=context.tenant))

    def test_revoke_inactive_owner_tenant_is_denied(self):
        dormant = Tenant.objects.create(name="Service Guard Dormant", slug="service-guard-dormant")
        dormant_grantee = Tenant.objects.create(
            name="Service Guard Dormant Grantee", slug="service-guard-dormant-grantee"
        )
        grant = TenantResourceGrant(
            tenant=dormant,
            grantee_tenant=dormant_grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
        )
        # bulk_create bypasses the ownership proof on purpose: the row is a
        # foreign-tenant grant owned by a tenant that is about to go dormant.
        TenantResourceGrant._base_manager.bulk_create([grant])
        Tenant._base_manager.filter(pk=dormant.pk).update(deleted_at=timezone.now())
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.tenant_user.pk, operation="rollback") as context:
            with self.assertRaises(PermissionDenied):
                revoke_resource_grant(grant.pk, user=context.user, active_tenant=context.tenant)

    def test_revoke_outside_active_tenant_is_denied(self):
        grant = self._grant()
        other = Tenant.objects.create(name="Service Guard Other", slug="service-guard-other")
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.tenant_user.pk, operation="rollback") as context:
            with self.assertRaises(PermissionDenied):
                revoke_resource_grant(grant.pk, user=context.user, active_tenant=other)

    def test_revoke_cutoff_ineligible_grant_returns_none(self):
        grant = self._grant()
        cutoff = timezone.now() + datetime.timedelta(hours=1)
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.tenant_user.pk, operation="rollback") as context:
            self.assertIsNone(
                revoke_resource_grant(grant.pk, user=context.user, active_tenant=context.tenant, cutoff=cutoff)
            )

    def test_revoke_unauthorized_operator_is_denied(self):
        grant = self._grant()
        user = get_user_model().objects.create_user(username="service-guard-noperm", password="test-password-123")
        # A canonical live RoleGrant proves tenant access; the empty role keeps
        # the revocation permission absent so the domain service remains the
        # authorization boundary exercised by this test.
        role = Role.objects.create(tenant=self.tenant, name="Service Guard No Revoke", permissions=[])
        self.grant(user, self.tenant, role)
        with TaskContext(tenant_id=self.tenant.pk, user_id=user.pk, operation="rollback") as context:
            with self.assertRaises(PermissionDenied):
                revoke_resource_grant(grant.pk, user=context.user, active_tenant=context.tenant)

    def test_revoke_system_authorization_request_mismatch_is_denied(self):
        grant = self._grant()
        with TaskContext(tenant_id=self.tenant.pk, operation="background_task") as issuer:
            system = issuer.authorize_system(
                permission="organization.delete_tenantresourcegrant",
                operation="organization.resource_grant.expire",
                reason="service guard test",
            )
        with TaskContext(tenant_id=self.tenant.pk, operation="background_task") as consumer:
            with self.assertRaises(PermissionDenied):
                revoke_resource_grant(
                    grant.pk,
                    user=None,
                    active_tenant=consumer.tenant,
                    system_authorization=system,
                )

    def test_revoke_expiry_evidence_tenant_mismatch_raises_integrity_error(self):
        grant = self._grant(valid_until=timezone.now() - datetime.timedelta(hours=1))
        other = Tenant.objects.create(name="Service Guard Run Owner", slug="service-guard-run-owner")
        cutoff = timezone.now()
        run = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=other,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + datetime.timedelta(minutes=1),
        )
        with TaskContext(tenant_id=self.tenant.pk, operation="background_task") as context:
            system = context.authorize_system(
                permission="organization.delete_tenantresourcegrant",
                operation="organization.resource_grant.expire",
                reason="service guard test",
            )
            with self.assertRaises(IntegrityError):
                revoke_resource_grant(
                    grant.pk,
                    user=None,
                    active_tenant=context.tenant,
                    system_authorization=system,
                    expiry_run=run,
                )
        grant.refresh_from_db()
        assert grant.deleted_at is None


class ResourceGrantRestoreGuardTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Restore Guard Owner",
            slug="restore-guard-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        self.grantee = Tenant.objects.create(name="Restore Guard Grantee", slug="restore-guard-grantee")
        site = Site.objects.create(name="Restore Guard Site", slug="restore-guard-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Restore Guard Location", slug="restore-guard-location", site=site, tenant=self.tenant
        )
        manufacturer = Manufacturer.objects.create(name="Restore Guard Manufacturer", slug="restore-guard-manufacturer")
        accessory = Accessory.objects.create(
            name="Restore Guard Accessory",
            slug="restore-guard-accessory",
            tenant=self.tenant,
            manufacturer=manufacturer,
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        self.content_type = ContentType.objects.get_for_model(AccessoryStock)
        self.stock = stock

    def _grant(self, *, deleted_at=None, **extra):
        grant = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=self.grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            **extra,
        )
        grant.save()
        if deleted_at is not None:
            TenantResourceGrant._base_manager.filter(pk=grant.pk).update(deleted_at=deleted_at)
            grant.refresh_from_db()
        return grant

    def test_restore_requires_a_live_human_principal(self):
        self._grant(deleted_at=timezone.now())
        with self.assertRaises(PermissionDenied):
            restore_resource_grant(grant_id=1, tenant_id=self.tenant.pk, user_id=None, valid_until=None)
        with self.assertRaises(PermissionDenied):
            restore_resource_grant(grant_id=1, tenant_id=None, user_id=self.tenant_user.pk, valid_until=None)

    def test_restore_operator_without_permission_is_denied(self):
        user = get_user_model().objects.create_user(username="restore-guard-noperm", password="test-password-123")
        with self.assertRaises(PermissionDenied):
            restore_resource_grant(grant_id=1, tenant_id=self.tenant.pk, user_id=user.pk, valid_until=None)

    def test_restore_rejects_past_and_naive_deadlines(self):
        with self.assertRaises(ValidationError):
            restore_resource_grant(
                grant_id=1,
                tenant_id=self.tenant.pk,
                user_id=self.tenant_user.pk,
                valid_until=timezone.now() - datetime.timedelta(minutes=1),
            )
        with self.assertRaises(ValidationError):
            restore_resource_grant(
                grant_id=1,
                tenant_id=self.tenant.pk,
                user_id=self.tenant_user.pk,
                valid_until=datetime.datetime.now(),
            )

    def test_restore_missing_or_foreign_grant_raises_does_not_exist(self):
        with self.assertRaises(TenantResourceGrant.DoesNotExist):
            restore_resource_grant(
                grant_id=999_999,
                tenant_id=self.tenant.pk,
                user_id=self.tenant_user.pk,
                valid_until=None,
            )
        foreign = Tenant.objects.create(name="Restore Guard Foreign", slug="restore-guard-foreign")
        foreign_grantee = Tenant.objects.create(
            name="Restore Guard Foreign Grantee", slug="restore-guard-foreign-grantee"
        )
        foreign_grant = TenantResourceGrant(
            tenant=foreign,
            grantee_tenant=foreign_grantee,
            resource_type=self.content_type,
            resource_id=self.stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
        )
        TenantResourceGrant._base_manager.bulk_create([foreign_grant])
        TenantResourceGrant._base_manager.filter(pk=foreign_grant.pk).update(deleted_at=timezone.now())
        with self.assertRaises(TenantResourceGrant.DoesNotExist):
            restore_resource_grant(
                grant_id=foreign_grant.pk,
                tenant_id=self.tenant.pk,
                user_id=self.tenant_user.pk,
                valid_until=None,
            )

    def test_restore_live_grant_is_rejected(self):
        grant = self._grant()
        with self.assertRaises(ValidationError):
            restore_resource_grant(
                grant_id=grant.pk,
                tenant_id=self.tenant.pk,
                user_id=self.tenant_user.pk,
                valid_until=None,
            )


class ResourceGrantRestoreCommandGuardTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Restore Command Owner",
            slug="restore-command-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        grantee = Tenant.objects.create(name="Restore Command Grantee", slug="restore-command-grantee")
        site = Site.objects.create(name="Restore Command Site", slug="restore-command-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Restore Command Location", slug="restore-command-location", site=site, tenant=self.tenant
        )
        manufacturer = Manufacturer.objects.create(
            name="Restore Command Manufacturer", slug="restore-command-manufacturer"
        )
        accessory = Accessory.objects.create(
            name="Restore Command Accessory",
            slug="restore-command-accessory",
            tenant=self.tenant,
            manufacturer=manufacturer,
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        content_type = ContentType.objects.get_for_model(AccessoryStock)
        self.live_grant = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=grantee,
            resource_type=content_type,
            resource_id=stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
        )
        self.live_grant.save()

    def test_command_refuses_without_confirm(self):
        with self.assertRaises(CommandError):
            call_command("restore_resource_grant", grant=1, tenant=self.tenant.pk, user=self.tenant_user.pk)

    def test_command_rejects_unresolvable_tenant_and_user(self):
        with self.assertRaises(CommandError):
            call_command("restore_resource_grant", grant=1, tenant=999_999, user=self.tenant_user.pk, confirm=True)
        with self.assertRaises(CommandError):
            call_command("restore_resource_grant", grant=1, tenant=self.tenant.pk, user=999_999, confirm=True)

    def test_command_rejects_invalid_deadline_values(self):
        with self.assertRaises(CommandError):
            call_command(
                "restore_resource_grant",
                grant=1,
                tenant=self.tenant.pk,
                user=self.tenant_user.pk,
                valid_until="not-a-datetime",
                confirm=True,
            )
        with self.assertRaises(CommandError):
            call_command(
                "restore_resource_grant",
                grant=1,
                tenant=self.tenant.pk,
                user=self.tenant_user.pk,
                valid_until="2026-08-16T12:00:00",
                confirm=True,
            )

    def test_command_reports_service_failure_as_command_error(self):
        with self.assertRaises(CommandError):
            call_command(
                "restore_resource_grant",
                grant=self.live_grant.pk,
                tenant=self.tenant.pk,
                user=self.tenant_user.pk,
                clear_deadline=True,
                confirm=True,
            )


class ResourceGrantExpiryIdentityImmutabilityTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Identity Immutability Owner",
            slug="identity-immutability-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        self.grantee = Tenant.objects.create(name="Identity Immutability Grantee", slug="identity-immutability-grantee")
        site = Site.objects.create(
            name="Identity Immutability Site", slug="identity-immutability-site", tenant=self.tenant
        )
        location = Location.objects.create(
            name="Identity Immutability Location", slug="identity-immutability-location", site=site, tenant=self.tenant
        )
        manufacturer = Manufacturer.objects.create(
            name="Identity Immutability Manufacturer", slug="identity-immutability-manufacturer"
        )
        accessory = Accessory.objects.create(
            name="Identity Immutability Accessory",
            slug="identity-immutability-accessory",
            tenant=self.tenant,
            manufacturer=manufacturer,
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        content_type = ContentType.objects.get_for_model(AccessoryStock)
        grant = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=self.grantee,
            resource_type=content_type,
            resource_id=stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
        )
        grant.save()
        self.grant = grant
        cutoff = timezone.now()
        self.run = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.tenant,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + datetime.timedelta(minutes=1),
        )
        self.evidence = TenantResourceGrantExpiryRevocation._base_manager.create(
            run=self.run,
            grant=grant,
            triggering_valid_until=cutoff,
            revoked_at=cutoff,
            request_id=uuid.uuid4(),
        )

    def test_run_identity_fields_are_immutable(self):
        self.run.schedule_slot = self.run.schedule_slot + datetime.timedelta(hours=1)
        with self.assertRaises(ValidationError):
            self.run.save()
        # The unchanged save path is the other branch of the identity guard.
        self.run.refresh_from_db()
        self.run.save()

    def test_revocation_identity_fields_are_immutable(self):
        self.evidence.revoked_at = timezone.now()
        with self.assertRaises(ValidationError):
            self.evidence.save()
        # The unchanged save path is the other branch of the identity guard.
        self.evidence.refresh_from_db()
        self.evidence.save()

    def test_run_string_and_absolute_url(self):
        assert "resource grant expiry" in str(self.run)
        assert self.run.get_absolute_url().endswith(f"/{self.run.pk}/")

    def test_revocation_string(self):
        assert "Expiry revocation" in str(self.evidence)


class ResourceGrantExpiryAdminAndTableTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Admin Table Owner",
            slug="admin-table-owner",
            permissions=["organization.view_tenantresourcegrant"],
        )
        grantee = Tenant.objects.create(name="Admin Table Grantee", slug="admin-table-grantee")
        site = Site.objects.create(name="Admin Table Site", slug="admin-table-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Admin Table Location", slug="admin-table-location", site=site, tenant=self.tenant
        )
        manufacturer = Manufacturer.objects.create(name="Admin Table Manufacturer", slug="admin-table-manufacturer")
        accessory = Accessory.objects.create(
            name="Admin Table Accessory", slug="admin-table-accessory", tenant=self.tenant, manufacturer=manufacturer
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        content_type = ContentType.objects.get_for_model(AccessoryStock)
        cutoff = timezone.now()
        grant = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=grantee,
            resource_type=content_type,
            resource_id=stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=cutoff - datetime.timedelta(minutes=1),
        )
        grant.save()
        self.grant = grant
        self.run = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=self.tenant,
            schedule_slot=cutoff.replace(minute=0, second=0, microsecond=0),
            cutoff=cutoff,
            dispatch_stale_at=cutoff + datetime.timedelta(minutes=1),
        )

    def _sweep(self):
        with self.captureOnCommitCallbacks(execute=True):
            sweep_expired_resource_grants(self.tenant.pk, self.run.pk, 1)
        return TenantResourceGrantExpiryRevocation._base_manager.get(grant=self.grant)

    def test_admin_queryset_applies_integrity_valid(self):
        evidence = self._sweep()
        admin_instance = TenantResourceGrantExpiryRevocationAdmin(TenantResourceGrantExpiryRevocation, admin.site)
        request = RequestFactory().get("/admin/")
        qs = admin_instance.get_queryset(request)
        self.assertIn(evidence.pk, set(qs.values_list("pk", flat=True)))
        # A corrupt row (foreign run tenant) drops out of the admin view.
        foreign = Tenant.objects.create(name="Admin Table Foreign", slug="admin-table-foreign")
        foreign_run = TenantResourceGrantExpiryRun._base_manager.create(
            tenant=foreign,
            schedule_slot=self.run.schedule_slot,
            cutoff=self.run.cutoff,
            dispatch_stale_at=self.run.dispatch_stale_at,
        )
        corrupt = TenantResourceGrantExpiryRevocation._base_manager.create(
            run=foreign_run,
            grant=self.grant,
            triggering_valid_until=timezone.now(),
            revoked_at=timezone.now(),
            request_id=uuid.uuid4(),
        )
        qs = admin_instance.get_queryset(request)
        self.assertNotIn(corrupt.pk, set(qs.values_list("pk", flat=True)))

    def test_revocation_table_renders_link_and_retained_branches(self):
        from organization.tables import TenantResourceGrantExpiryRevocationTable

        evidence = self._sweep()
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        # Link branches: the grant audit link and the bound audit change link.
        html = TenantResourceGrantExpiryRevocationTable([evidence]).as_html(request)
        self.assertIn(f"resource-grant-audit/{self.grant.pk}/", html)
        self.assertIn(evidence.object_change.get_absolute_url(), html)
        # A pruned audit change renders the retained-evidence branch.
        TenantResourceGrantExpiryRevocation._base_manager.filter(pk=evidence.pk).update(object_change=None)
        evidence.refresh_from_db()
        html = TenantResourceGrantExpiryRevocationTable([evidence]).as_html(request)
        self.assertIn("Retained evidence", html)


class ResourceGrantExpiryCoordinatorBranchTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Coordinator Branch Owner",
            slug="coordinator-branch-owner",
            permissions=["organization.delete_tenantresourcegrant"],
        )
        cutoff = timezone.now()
        self.cutoff = cutoff

    def _run(self, *, state, generation=1, attempt_count=1, **extra):
        fields = {
            "tenant": self.tenant,
            "schedule_slot": self.cutoff.replace(minute=0, second=0, microsecond=0),
            "cutoff": self.cutoff,
            "dispatch_stale_at": self.cutoff + datetime.timedelta(minutes=1),
            "state": state,
            "generation": generation,
            "attempt_count": attempt_count,
        }
        fields.update(extra)
        return TenantResourceGrantExpiryRun._base_manager.create(**fields)

    def test_repair_run_handles_failed_enqueue_state(self):
        run = self._run(state=STATE_ENQUEUE_FAILED, attempt_count=2, outcome="retryable")
        delivery = _repair_run(run, timezone.now())
        assert delivery == (run.pk, 2)

    def test_repair_run_handles_stale_running_lease(self):
        run = self._run(
            state=STATE_RUNNING,
            lease_expires_at=timezone.now() - datetime.timedelta(seconds=1),
        )
        delivery = _repair_run(run, timezone.now())
        assert delivery == (run.pk, 2)

    def test_repair_run_skips_healthy_queued_run(self):
        run = self._run(state=STATE_QUEUED, dispatch_stale_at=timezone.now() + datetime.timedelta(minutes=5))
        assert _repair_run(run, timezone.now()) is None

    def test_retry_or_exhaust_exhausts_finite_policy(self):
        run = self._run(
            state=STATE_RUNNING,
            attempt_count=len(RETRY_DELAYS) + 1,
            lease_expires_at=timezone.now() + datetime.timedelta(minutes=5),
        )
        counts = {"revoked": 0, "remaining_due": 0, "invalid": 0}
        assert _retry_or_exhaust(run.pk, run.generation, RuntimeError("boom"), counts) == "exhausted"
        run.refresh_from_db()
        assert run.state == STATE_COMPLETE

    def test_retry_or_exhaust_ignores_unknown_run(self):
        counts = {"revoked": 0, "remaining_due": 0, "invalid": 0}
        assert _retry_or_exhaust(999_999, 1, RuntimeError("boom"), counts) is None

    def test_partial_outcome_when_invalid_and_revoked_coexist(self):
        grantee = Tenant.objects.create(name="Coordinator Branch Grantee", slug="coordinator-branch-grantee")
        site = Site.objects.create(name="Coordinator Branch Site", slug="coordinator-branch-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Coordinator Branch Location", slug="coordinator-branch-location", site=site, tenant=self.tenant
        )
        manufacturer = Manufacturer.objects.create(
            name="Coordinator Branch Manufacturer", slug="coordinator-branch-manufacturer"
        )
        accessory = Accessory.objects.create(
            name="Coordinator Branch Accessory",
            slug="coordinator-branch-accessory",
            tenant=self.tenant,
            manufacturer=manufacturer,
        )
        stock = AccessoryStock.objects.create(accessory=accessory, location=location, qty=2)
        approved_type = ContentType.objects.get_for_model(AccessoryStock)
        due = self.cutoff - datetime.timedelta(minutes=1)
        valid = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=grantee,
            resource_type=approved_type,
            resource_id=stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=due,
        )
        valid.save()
        foreign_type = ContentType.objects.get_for_model(Accessory)
        malformed = TenantResourceGrant(
            tenant=self.tenant,
            grantee_tenant=grantee,
            resource_type=foreign_type,
            resource_id=stock.pk,
            access_level=TenantResourceGrant.ACCESS_VIEW,
            valid_until=due,
        )
        TenantResourceGrant._base_manager.bulk_create([malformed])
        run = self._run(state=STATE_QUEUED)
        with self.captureOnCommitCallbacks(execute=True):
            result = sweep_expired_resource_grants(self.tenant.pk, run.pk, 1)
        assert result.code == CODE_PARTIAL
        run.refresh_from_db()
        assert run.state == STATE_COMPLETE
        assert run.outcome == TaskStatus.PARTIAL.value
