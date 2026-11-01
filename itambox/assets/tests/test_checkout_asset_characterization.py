"""Characterization of ``checkout_asset`` at every decision branch.

These tests pin the service's observable behaviour -- check order, messages,
row locks, persisted fields, the custody receipt and its post-commit
notification -- so the function can be decomposed without changing any of it.
"""

import datetime
import uuid
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from assets import services
from assets.models import (
    Asset,
    AssetAssignment,
    AssetReservation,
    DisposalMethodChoices,
    ReservationStatusChoices,
    StatusLabel,
)
from assets.services import checkout_asset, dispose_asset
from compliance.models import CustodyReceipt, CustodyTemplate
from core.context import set_current_tenant
from core.models import ObjectChange
from itambox.middleware import _current_user, _request_id
from organization.models import AssetHolder, Location, Site, Tenant

User = get_user_model()

# Stand-in for a registered signature provider: any non-None registry entry.
REGISTERED_PROVIDER = object()


class CheckoutAssetFixtureMixin:
    def setUp(self):
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.tenant = baker.make(Tenant)
        self.deployable = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE, name="Char Deployable")
        self.deployed = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYED, name="Char Deployed")
        site = baker.make(Site)
        self.base = baker.make(Location, site=site, tenant=self.tenant, name="Char Base")
        self.target_location = baker.make(Location, site=site, tenant=self.tenant, name="Char Target")
        self.asset = baker.make(Asset, tenant=self.tenant, status=self.deployable, location=self.base, asset_type=None)
        self.holder = self.make_holder("first")

    def make_holder(self, suffix, **extra):
        values = {
            "first_name": "Char",
            "last_name": suffix,
            "upn": f"char-{suffix}@example.test",
            "email": f"char-{suffix}@example.test",
            "tenant": self.tenant,
        }
        values.update(extra)
        return AssetHolder.objects.create(**values)

    def active_assignment(self, asset=None):
        return AssetAssignment.objects.get(asset=asset or self.asset, is_active=True)

    def fresh_asset(self, **extra):
        values = {"tenant": self.tenant, "status": self.deployable, "location": self.base, "asset_type": None}
        values.update(extra)
        self.asset = baker.make(Asset, **values)
        return self.asset


class CheckoutTargetTests(CheckoutAssetFixtureMixin, TestCase):
    def test_missing_target_is_refused_before_any_query(self):
        with CaptureQueriesContext(connection) as queries:
            with self.assertRaisesMessage(ValidationError, "Either holder, location, or asset must be specified."):
                checkout_asset(self.asset, user=self.user)
        self.assertEqual(len(queries), 0)

    def test_holder_checkout_keeps_base_location_and_returns_holder(self):
        result = checkout_asset(self.asset, holder=self.holder, user=self.user)

        self.assertEqual(result, self.holder)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.location, self.base)
        self.assertEqual(self.asset.status, self.deployed)
        assignment = self.active_assignment()
        self.assertEqual(assignment.assigned_user, self.holder)
        self.assertIsNone(assignment.assigned_location)
        self.assertIsNone(assignment.assigned_asset)

    def test_location_checkout_moves_the_asset_and_returns_location(self):
        result = checkout_asset(self.asset, location=self.target_location, user=self.user)

        self.assertEqual(result, self.target_location)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.location, self.target_location)
        assignment = self.active_assignment()
        self.assertEqual(assignment.assigned_location, self.target_location)
        self.assertIsNone(assignment.assigned_user)
        self.assertIsNone(assignment.assigned_asset)

    def test_asset_checkout_inherits_the_parent_location_and_returns_parent(self):
        parent = baker.make(Asset, tenant=self.tenant, status=self.deployable, location=self.target_location)

        result = checkout_asset(self.asset, asset_target=parent, user=self.user)

        self.assertEqual(result, parent)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.location, self.target_location)
        assignment = self.active_assignment()
        self.assertEqual(assignment.assigned_asset, parent)
        self.assertIsNone(assignment.assigned_user)
        self.assertIsNone(assignment.assigned_location)

    def test_holder_wins_over_location_and_asset_when_several_targets_are_given(self):
        parent = baker.make(Asset, tenant=self.tenant, status=self.deployable, location=self.target_location)

        result = checkout_asset(
            self.asset, holder=self.holder, location=self.target_location, asset_target=parent, user=self.user
        )

        self.assertEqual(result, self.holder)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.location, self.base)
        assignment = self.active_assignment()
        self.assertEqual(assignment.assigned_user, self.holder)
        self.assertIsNone(assignment.assigned_location)
        self.assertIsNone(assignment.assigned_asset)

    def test_location_wins_over_asset_target(self):
        parent = baker.make(Asset, tenant=self.tenant, status=self.deployable, location=self.base)

        result = checkout_asset(self.asset, location=self.target_location, asset_target=parent, user=self.user)

        self.assertEqual(result, self.target_location)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.location, self.target_location)
        assignment = self.active_assignment()
        self.assertEqual(assignment.assigned_location, self.target_location)
        self.assertIsNone(assignment.assigned_asset)

    def test_assignment_records_every_supplied_field(self):
        maintenance = baker.make("assets.AssetMaintenance", asset=self.asset)
        expected = datetime.date(2026, 12, 1)
        due = datetime.date(2026, 11, 15)

        checkout_asset(
            self.asset,
            holder=self.holder,
            user=self.user,
            expected_checkin=expected,
            notes="characterization notes",
            is_loan=True,
            due_date=due,
            maintenance=maintenance,
        )

        assignment = self.active_assignment()
        self.assertEqual(assignment.checked_out_by, self.user)
        self.assertEqual(assignment.expected_checkin_date, expected)
        self.assertEqual(assignment.notes, "characterization notes")
        self.assertEqual(assignment.pre_checkout_status, self.deployable)
        self.assertTrue(assignment.is_loan)
        self.assertEqual(assignment.due_date, due)
        self.assertEqual(assignment.maintenance, maintenance)

    def test_checkout_date_as_date_is_bound_to_aware_midnight(self):
        checkout_asset(self.asset, holder=self.holder, user=self.user, checkout_date=datetime.date(2026, 3, 4))

        expected = timezone.make_aware(datetime.datetime(2026, 3, 4, 0, 0))
        self.assertEqual(self.active_assignment().checked_out_at, expected)

    def test_checkout_datetime_is_kept_verbatim(self):
        moment = timezone.make_aware(datetime.datetime(2026, 3, 4, 13, 37))

        checkout_asset(self.asset, holder=self.holder, user=self.user, checkout_date=moment)

        self.assertEqual(self.active_assignment().checked_out_at, moment)

    def test_without_checkout_date_the_model_default_timestamp_applies(self):
        before = timezone.now()
        checkout_asset(self.asset, holder=self.holder, user=self.user)
        after = timezone.now()

        checked_out_at = self.active_assignment().checked_out_at
        self.assertLessEqual(before, checked_out_at)
        self.assertLessEqual(checked_out_at, after)

    def test_explicit_status_overrides_the_default_deployed_label(self):
        custom = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYED, name="Char Custom Deployed")

        checkout_asset(self.asset, holder=self.holder, user=self.user, status=custom)

        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, custom)

    def test_default_status_is_the_first_deployed_label(self):
        checkout_asset(self.asset, location=self.target_location, user=self.user)

        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, StatusLabel.objects.filter(type=StatusLabel.TYPE_DEPLOYED).first())

    def test_caller_instance_is_not_mutated(self):
        checkout_asset(self.asset, location=self.target_location, user=self.user)

        self.assertEqual(self.asset.status, self.deployable)
        self.assertEqual(self.asset.location, self.base)

    def test_checkout_is_logged_as_a_checkout_change(self):
        _current_user.set(self.user)
        _request_id.set(uuid.uuid4())

        checkout_asset(self.asset, holder=self.holder, user=self.user)

        change = ObjectChange.objects.filter(changed_object_id=self.asset.pk, action="checkout").latest("time")
        self.assertEqual(change.prechange_data.get("status"), self.deployable.pk)
        self.assertEqual(change.postchange_data.get("status"), self.deployed.pk)

    def test_asset_outside_the_active_scope_is_not_found(self):
        foreign_tenant = baker.make(Tenant)
        set_current_tenant(foreign_tenant)

        with self.assertRaises(Asset.DoesNotExist):
            checkout_asset(self.asset, holder=self.holder, user=self.user)

        set_current_tenant(None)
        self.assertFalse(AssetAssignment.objects.filter(asset=self.asset).exists())


class CheckoutPreconditionTests(CheckoutAssetFixtureMixin, TestCase):
    def test_non_deployable_status_is_refused_with_its_display_name(self):
        pending = baker.make(StatusLabel, type=StatusLabel.TYPE_PENDING, name="Char Pending")
        Asset.objects.filter(pk=self.asset.pk).update(status=pending)

        with self.assertRaisesMessage(ValidationError, "Cannot check out an asset that is Char Pending."):
            checkout_asset(self.asset, holder=self.holder, user=self.user)

    def test_already_assigned_asset_is_refused_with_its_deployed_status(self):
        checkout_asset(self.asset, holder=self.holder, user=self.user)

        with self.assertRaisesMessage(ValidationError, "Cannot check out an asset that is Char Deployed."):
            checkout_asset(self.asset, location=self.target_location, user=self.user)
        self.assertEqual(AssetAssignment.objects.filter(asset=self.asset).count(), 1)

    def test_active_disposal_message_takes_precedence_over_the_status_message(self):
        baker.make(StatusLabel, type=StatusLabel.TYPE_ARCHIVED, name="Char Archived")
        baker.make(StatusLabel, type=StatusLabel.TYPE_PENDING, name="Char Pending")
        dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RECYCLE,
            disposal_date=datetime.date(2026, 6, 1),
            user=self.user,
        )

        with self.assertRaisesMessage(ValidationError, "Cannot check out an asset that has an active disposal record."):
            checkout_asset(self.asset, holder=self.holder, user=self.user)

    def test_missing_deployed_label_is_refused(self):
        StatusLabel.objects.filter(type=StatusLabel.TYPE_DEPLOYED).delete()

        with self.assertRaisesMessage(ValidationError, "No 'Deployed' Status Label exists. Configure one first."):
            checkout_asset(self.asset, holder=self.holder, user=self.user)
        self.assertFalse(AssetAssignment.objects.filter(asset=self.asset).exists())

    def test_explicit_status_bypasses_the_missing_deployed_label_check(self):
        custom = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYED, name="Char Explicit")
        StatusLabel.objects.filter(type=StatusLabel.TYPE_DEPLOYED).exclude(pk=custom.pk).delete()

        checkout_asset(self.asset, holder=self.holder, user=self.user, status=custom)

        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, custom)

    def test_issuability_is_checked_before_the_deployed_label(self):
        pending = baker.make(StatusLabel, type=StatusLabel.TYPE_PENDING, name="Char Pending")
        Asset.objects.filter(pk=self.asset.pk).update(status=pending)
        StatusLabel.objects.filter(type=StatusLabel.TYPE_DEPLOYED).delete()

        with self.assertRaisesMessage(ValidationError, "Cannot check out an asset that is Char Pending."):
            checkout_asset(self.asset, holder=self.holder, user=self.user)

    def test_deployed_label_is_checked_before_the_reservation(self):
        other = self.make_holder("other")
        today = datetime.date.today()
        AssetReservation.objects.create(
            asset=self.asset,
            reserved_for=other,
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=1),
            status=ReservationStatusChoices.ACTIVE,
        )
        StatusLabel.objects.filter(type=StatusLabel.TYPE_DEPLOYED).delete()

        with self.assertRaisesMessage(ValidationError, "No 'Deployed' Status Label exists. Configure one first."):
            checkout_asset(self.asset, holder=self.holder, user=self.user)


class CheckoutReservationTests(CheckoutAssetFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.reserved_for = self.make_holder("reserved")
        self.today = datetime.date.today()

    def reserve(self, *, reserved_for="default", status=ReservationStatusChoices.ACTIVE, start=-1, end=3):
        return AssetReservation.objects.create(
            asset=self.asset,
            reserved_for=self.reserved_for if reserved_for == "default" else reserved_for,
            start_date=self.today + timedelta(days=start),
            end_date=self.today + timedelta(days=end),
            status=status,
        )

    def expected_message(self, reservation):
        return (
            f"Asset is reserved for {reservation.reserved_for} until {reservation.end_date} "
            "and cannot be checked out to a different holder."
        )

    def assert_refused(self, reservation, **target):
        with self.assertRaisesMessage(ValidationError, self.expected_message(reservation)):
            checkout_asset(self.asset, user=self.user, **target)
        self.assertFalse(AssetAssignment.objects.filter(asset=self.asset).exists())

    def test_other_holder_is_refused_during_an_active_reservation(self):
        reservation = self.reserve()
        self.assert_refused(reservation, holder=self.holder)

    def test_pending_reservation_also_blocks(self):
        reservation = self.reserve(status=ReservationStatusChoices.PENDING)
        self.assert_refused(reservation, holder=self.holder)

    def test_location_target_is_refused_even_for_the_reserved_window(self):
        reservation = self.reserve()
        self.assert_refused(reservation, location=self.target_location)

    def test_asset_target_is_refused_during_a_reservation(self):
        reservation = self.reserve()
        parent = baker.make(Asset, tenant=self.tenant, status=self.deployable, location=self.base)
        self.assert_refused(reservation, asset_target=parent)

    def test_reserved_holder_passes(self):
        self.reserve()

        checkout_asset(self.asset, holder=self.reserved_for, user=self.user)

        self.assertEqual(self.active_assignment().assigned_user, self.reserved_for)

    def test_holderless_reservation_blocks_every_holder(self):
        reservation = self.reserve(reserved_for=None)
        self.assert_refused(reservation, holder=self.holder)

    def test_window_bounds_are_inclusive(self):
        reservation = self.reserve(start=0, end=0)
        self.assert_refused(reservation, holder=self.holder)

    def test_future_reservation_does_not_block(self):
        self.reserve(start=1, end=4)

        checkout_asset(self.asset, holder=self.holder, user=self.user)

        self.assertEqual(self.active_assignment().assigned_user, self.holder)

    def test_past_reservation_does_not_block(self):
        self.reserve(start=-5, end=-1)

        checkout_asset(self.asset, location=self.target_location, user=self.user)

        self.assertEqual(self.active_assignment().assigned_location, self.target_location)

    def test_fulfilled_and_cancelled_reservations_do_not_block(self):
        for status in (ReservationStatusChoices.FULFILLED, ReservationStatusChoices.CANCELLED):
            with self.subTest(status=status):
                self.fresh_asset()
                self.reserve(status=status)

                checkout_asset(self.asset, holder=self.holder, user=self.user)

                self.assertEqual(self.active_assignment().assigned_user, self.holder)

    def test_soft_deleted_reservation_does_not_block(self):
        self.reserve().delete()

        checkout_asset(self.asset, location=self.target_location, user=self.user)

        self.assertEqual(self.active_assignment().assigned_location, self.target_location)


class CheckoutLockingTests(CheckoutAssetFixtureMixin, TestCase):
    def test_asset_row_is_locked_before_the_reservation_rows(self):
        with CaptureQueriesContext(connection) as queries:
            checkout_asset(self.asset, holder=self.holder, user=self.user)

        locking = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        self.assertGreaterEqual(len(locking), 2, locking)
        self.assertIn('FROM "assets_asset"', locking[0])
        self.assertIn('FROM "assets_assetreservation"', locking[1])

    def test_assignment_is_created_after_both_locks(self):
        with CaptureQueriesContext(connection) as queries:
            checkout_asset(self.asset, location=self.target_location, user=self.user)

        sqls = [q["sql"] for q in queries]
        lock_indexes = [i for i, sql in enumerate(sqls) if "FOR UPDATE" in sql]
        insert_index = next(i for i, sql in enumerate(sqls) if sql.startswith('INSERT INTO "assets_assetassignment"'))
        self.assertLess(max(lock_indexes[:2]), insert_index)

    def test_failed_reservation_check_writes_nothing(self):
        other = self.make_holder("other")
        today = datetime.date.today()
        AssetReservation.objects.create(
            asset=self.asset,
            reserved_for=other,
            start_date=today,
            end_date=today,
            status=ReservationStatusChoices.ACTIVE,
        )

        with CaptureQueriesContext(connection) as queries:
            with self.assertRaises(ValidationError):
                checkout_asset(self.asset, holder=self.holder, user=self.user)

        writes = [q["sql"] for q in queries if q["sql"].startswith(("INSERT", "UPDATE"))]
        self.assertEqual(writes, [])


class CheckoutCustodyTests(CheckoutAssetFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.category = baker.make("assets.Category")
        asset_type = baker.make("assets.AssetType", category=self.category)
        self.asset_type = asset_type
        self.fresh_asset(asset_type=asset_type)
        self.request = SimpleNamespace(user=self.user)
        self.email_config = SimpleNamespace(enabled=True, from_address="itambox@example.test", test_recipient="")

    def make_template(self, **extra):
        values = {
            "tenant": self.tenant,
            "category": self.category,
            "is_active": True,
            "require_acceptance": True,
            "email_signature_request": False,
            "signature_provider": "local",
            "eula_text": "Char EULA",
            "disclaimer": "Char disclaimer",
            "qms_reference": "QMS-CHAR",
        }
        values.update(extra)
        return baker.make(CustodyTemplate, **values)

    def checkout_with_mail(self, *, email_config="default", provider=REGISTERED_PROVIDER, target=None, **kwargs):
        """Check out with patched mail settings; return the scheduled custody notifications."""
        config = self.email_config if email_config == "default" else email_config
        target = target if target is not None else {"holder": self.holder}
        with (
            patch("core.models.EmailSettings.load", return_value=config),
            patch("compliance.registry.signature_providers.get", return_value=provider),
            self.captureOnCommitCallbacks(execute=False) as callbacks,
        ):
            checkout_asset(self.asset, user=self.user, **target, **kwargs)
        return [
            callback
            for callback in callbacks
            if getattr(callback, "func", None) is services._run_scoped_custody_notification
        ]

    def test_receipt_copies_the_template_and_binds_the_assignment(self):
        template = self.make_template()

        checkout_asset(self.asset, holder=self.holder, user=self.user)

        receipt = CustodyReceipt.objects.get(asset=self.asset, holder=self.holder)
        self.assertEqual(receipt.assignment, self.active_assignment())
        self.assertEqual(receipt.custody_template, template)
        self.assertEqual(receipt.signature_provider, "local")
        self.assertEqual(receipt.eula_text, "Char EULA")
        self.assertEqual(receipt.disclaimer, "Char disclaimer")
        self.assertEqual(receipt.qms_reference, "QMS-CHAR")

    def test_asset_without_type_uses_a_category_less_template(self):
        Asset.objects.filter(pk=self.asset.pk).update(asset_type=None)
        template = self.make_template(category=None)

        checkout_asset(self.asset, holder=self.holder, user=self.user)

        self.assertEqual(CustodyReceipt.objects.get(asset=self.asset).custody_template, template)

    def test_suppressed_receipt_is_not_created(self):
        self.make_template()

        checkout_asset(self.asset, holder=self.holder, user=self.user, _suppress_custody_receipt=True)

        self.assertTrue(AssetAssignment.objects.filter(asset=self.asset, is_active=True).exists())
        self.assertFalse(CustodyReceipt.objects.filter(asset=self.asset).exists())

    def test_template_without_acceptance_creates_no_receipt(self):
        self.make_template(require_acceptance=False, email_signature_request=True)

        callbacks = self.checkout_with_mail(request=self.request)

        self.assertFalse(CustodyReceipt.objects.filter(asset=self.asset).exists())
        self.assertEqual(callbacks, [])

    def test_no_matching_template_creates_no_receipt(self):
        self.make_template(is_active=False)

        checkout_asset(self.asset, holder=self.holder, user=self.user)

        self.assertFalse(CustodyReceipt.objects.filter(asset=self.asset).exists())

    def test_location_and_asset_targets_never_create_a_receipt(self):
        self.make_template(email_signature_request=True)

        for target in ({"location": self.target_location}, {"asset_target": self.fresh_asset()}):
            with self.subTest(target=sorted(target)):
                self.fresh_asset(asset_type=self.asset_type)

                callbacks = self.checkout_with_mail(request=self.request, target=target)

                self.assertFalse(CustodyReceipt.objects.filter(asset=self.asset).exists())
                self.assertEqual(callbacks, [])

    def test_signature_request_is_scheduled_after_commit_with_the_scoped_payload(self):
        self.make_template(email_signature_request=True)

        with patch("assets.services.send_mail") as send_mail:
            callbacks = self.checkout_with_mail(request=self.request)
            send_mail.assert_not_called()

        receipt = CustodyReceipt.objects.get(asset=self.asset)
        self.assertEqual(len(callbacks), 1)
        callback = callbacks[0]
        self.assertIsInstance(callback, partial)
        self.assertIs(callback.func, services._run_scoped_custody_notification)
        tenant, membership, asset, scheduled_receipt, recipient, from_address, request = callback.args
        self.assertIsNone(tenant)
        self.assertIsNone(membership)
        self.assertEqual(asset.pk, self.asset.pk)
        self.assertEqual(scheduled_receipt, receipt)
        self.assertEqual(recipient, self.holder.email)
        self.assertEqual(from_address, "itambox@example.test")
        self.assertIs(request, self.request)

    def test_signature_request_captures_the_active_tenant(self):
        self.make_template(email_signature_request=True)
        set_current_tenant(self.tenant)
        try:
            callbacks = self.checkout_with_mail(request=self.request)
        finally:
            set_current_tenant(None)

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0].args[0], self.tenant)

    def test_without_request_no_signature_request_is_scheduled(self):
        self.make_template(email_signature_request=True)

        callbacks = self.checkout_with_mail()

        self.assertTrue(CustodyReceipt.objects.filter(asset=self.asset).exists())
        self.assertEqual(callbacks, [])

    def test_template_without_email_request_schedules_nothing(self):
        self.make_template(email_signature_request=False)

        with patch("core.models.EmailSettings.load") as load:
            callbacks = self.checkout_with_mail(request=self.request)

        load.assert_not_called()
        self.assertEqual(callbacks, [])

    def test_unusable_email_settings_schedule_nothing(self):
        self.make_template(email_signature_request=True)
        cases = {
            "missing": None,
            "disabled": SimpleNamespace(enabled=False, from_address="itambox@example.test"),
            "no sender": SimpleNamespace(enabled=True, from_address=""),
        }
        for label, config in cases.items():
            with self.subTest(label):
                self.fresh_asset(asset_type=self.asset_type)

                callbacks = self.checkout_with_mail(email_config=config, request=self.request)

                self.assertEqual(callbacks, [])
                self.assertTrue(CustodyReceipt.objects.filter(asset=self.asset).exists())

    def test_holder_without_address_rejects_the_whole_checkout(self):
        self.make_template(email_signature_request=True)
        AssetHolder.objects.filter(pk=self.holder.pk).update(email="")
        self.holder.refresh_from_db()

        with self.assertRaisesMessage(ValidationError, "The holder has no e-mail address."):
            self.checkout_with_mail(request=self.request)

    def test_unregistered_signature_provider_is_logged_without_rolling_back(self):
        self.make_template(email_signature_request=True, signature_provider="char-missing")

        with self.assertLogs("assets.services", level="ERROR") as logs:
            callbacks = self.checkout_with_mail(request=self.request, provider=None)

        self.assertEqual(callbacks, [])
        self.assertTrue(AssetAssignment.objects.filter(asset=self.asset, is_active=True).exists())
        receipt = CustodyReceipt.objects.get(asset=self.asset)
        output = "\n".join(logs.output)
        self.assertIn(f"asset_id={self.asset.pk}", output)
        self.assertIn(f"receipt_id={receipt.pk}", output)
        self.assertIn(f"tenant_id={self.tenant.pk}", output)
        self.assertIn(f"actor_id={self.user.pk}", output)
        self.assertIn("exception_type=LookupError", output)

    def test_unexpected_notification_error_is_logged_without_rolling_back(self):
        self.make_template(email_signature_request=True)

        with (
            patch("core.models.EmailSettings.load", side_effect=RuntimeError("settings offline")),
            self.assertLogs("assets.services", level="ERROR") as logs,
        ):
            checkout_asset(self.asset, holder=self.holder, user=self.user, request=self.request)

        self.assertTrue(CustodyReceipt.objects.filter(asset=self.asset).exists())
        self.assertIn("exception_type=RuntimeError", "\n".join(logs.output))
