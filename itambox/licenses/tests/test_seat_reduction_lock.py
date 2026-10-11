"""Seat reductions revalidate capacity under the License row lock (#720)."""

import contextvars
import threading
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import TransactionTestCase
from model_bakery import baker
from rest_framework import serializers

from assets.models import StatusLabel
from core.tests.mixins import TenantTestMixin
from itambox.middleware import _current_user, _request_id
from licenses.api.serializers import LicenseSerializer
from licenses.models import License, LicenseSeatAssignment, LicenseTypeChoices
from licenses.services import checkout_license
from organization.models import AssetHolder
from software.models import Software


class SeatReductionLockTests(TenantTestMixin, TransactionTestCase):
    def setUp(self):
        self.setup_tenant_context(name="Seat Lock", slug="seat-lock")
        for kind in ("deployable", "archived", "pending"):
            StatusLabel.objects.get_or_create(type=kind, defaults={"name": f"{kind} seatlock"})
        _current_user.set(self.tenant_admin)
        _request_id.set(uuid.uuid4())
        # Reset before the TransactionTestCase flush re-seeds reference rows, or the
        # change log would reference the already-flushed actor.
        self.addCleanup(_current_user.set, None)
        self.addCleanup(_request_id.set, None)
        with self.tenant_context(self.tenant):
            software = baker.make(Software, name="Lock App", manufacturer__name="Lock Mfr", tenant=self.tenant)
            self.license = baker.make(
                License, software=software, tenant=self.tenant, seats=2, license_type=LicenseTypeChoices.PERPETUAL_SEAT
            )
            self.holders = [
                AssetHolder.objects.create(first_name="H", last_name=str(i), upn=f"h{i}", tenant=self.tenant)
                for i in range(2)
            ]
            checkout_license(self.license, assigned_holder=self.holders[0])

    def _active(self):
        return LicenseSeatAssignment.all_objects.filter(license_id=self.license.pk, deleted_at__isnull=True).count()

    @pytest.mark.serial_only
    def test_stale_reduction_is_rejected_at_save(self):
        with self.tenant_context(self.tenant):
            stale = License.objects.get(pk=self.license.pk)
            stale.seats = 1
            stale.assert_seat_capacity(seats=1)  # passes: one active seat
            checkout_license(self.license, assigned_holder=self.holders[1])  # interleaved commit
            with self.assertRaises(ValidationError):
                stale.save()
            self.assertEqual(License.objects.get(pk=self.license.pk).seats, 2)
            self.assertEqual(self._active(), 2)

    @pytest.mark.serial_only
    def test_serializer_surfaces_race_rejection_as_validation_error(self):
        with self.tenant_context(self.tenant):
            serializer = LicenseSerializer(License.objects.get(pk=self.license.pk), data={"seats": 1}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            checkout_license(self.license, assigned_holder=self.holders[1])
            with self.assertRaises(serializers.ValidationError):
                serializer.save()
            self.assertEqual(self._active(), 2)

    @pytest.mark.serial_only
    def test_reduction_waits_for_in_flight_checkout_lock(self):
        """A reduction blocks on the License row lock held by a checkout, then rejects."""
        holder_ready, release, result = threading.Event(), threading.Event(), {}

        def hold_lock():
            try:
                from django.db import transaction

                with transaction.atomic():
                    License.all_objects.select_for_update().get(pk=self.license.pk)
                    LicenseSeatAssignment.all_objects.create(
                        license_id=self.license.pk, assigned_holder=self.holders[1]
                    )
                    holder_ready.set()
                    release.wait(10)
            finally:
                connections.close_all()

        def reduce():
            try:
                lic = License.all_objects.get(pk=self.license.pk)
                lic.seats = 1
                lic.save()
                result["outcome"] = "saved"
            except ValidationError:
                result["outcome"] = "rejected"
            finally:
                connections.close_all()

        t1 = threading.Thread(target=contextvars.copy_context().run, args=(hold_lock,))
        t1.start()
        self.assertTrue(holder_ready.wait(10))
        t2 = threading.Thread(target=contextvars.copy_context().run, args=(reduce,))
        t2.start()
        t2.join(1.0)
        self.assertTrue(t2.is_alive(), "reduction must wait on the License row lock")
        release.set()
        t1.join(10)
        t2.join(10)
        self.assertEqual(result.get("outcome"), "rejected")
        self.assertEqual(License.all_objects.get(pk=self.license.pk).seats, 2)
