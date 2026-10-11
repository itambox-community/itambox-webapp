"""Reservation journeys: a reservation holds the asset against every checkout target, not only people."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase

from assets.models import AssetAssignment, AssetReservation, ReservationStatusChoices, StatusLabel
from assets.services import checkout_asset
from organization.models import Location, Site

from .support import JourneyMixin, today


class ReservationJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.make_tenant("journey-reservation")
        deployable = StatusLabel.objects.create(
            name="Journey Ready", slug="journey-ready", type=StatusLabel.TYPE_DEPLOYABLE
        )
        StatusLabel.objects.create(name="Deployed", slug="deployed", type=StatusLabel.TYPE_DEPLOYED)
        self.asset = self.make_asset(status=deployable)
        self.reserved_for = self.make_holder()
        self.reservation = AssetReservation.objects.create(
            asset=self.asset,
            reserved_for=self.reserved_for,
            start_date=today() - timedelta(days=1),
            end_date=today() + timedelta(days=3),
            status=ReservationStatusChoices.ACTIVE,
        )
        site = Site.objects.create(name="Journey Reservation Site", slug="journey-reservation-site")
        self.location = Location.objects.create(
            name="Journey Reservation Loc", slug="journey-reservation-loc", site=site, tenant=self.tenant
        )

    def test_checkout_to_location_during_another_holders_reservation_is_refused(self):
        with self.assertRaises(ValidationError):
            checkout_asset(asset=self.asset, location=self.location, request=None)

        self.assertFalse(AssetAssignment.objects.filter(asset=self.asset, is_active=True).exists())

    def test_checkout_to_another_holder_during_reservation_is_refused(self):
        other = self.make_holder()
        with self.assertRaises(ValidationError):
            checkout_asset(asset=self.asset, holder=other, request=None)

    def test_checkout_to_asset_during_another_holders_reservation_is_refused(self):
        with self.assertRaises(ValidationError):
            checkout_asset(asset=self.asset, asset_target=self.make_asset(), request=None)

    def test_reservation_without_a_holder_blocks_a_location_checkout(self):
        """A reservation whose holder was removed still guards the asset."""
        asset = self.make_asset()
        AssetReservation.objects.create(
            asset=asset,
            reserved_for=None,
            start_date=today() - timedelta(days=1),
            end_date=today() + timedelta(days=3),
            status=ReservationStatusChoices.ACTIVE,
        )

        with self.assertRaises(ValidationError):
            checkout_asset(asset=asset, location=self.location, request=None)

    def test_reserved_holder_can_still_check_out(self):
        checkout_asset(asset=self.asset, holder=self.reserved_for, request=None)
        self.assertTrue(AssetAssignment.objects.filter(asset=self.asset, is_active=True).exists())

    def test_soft_deleted_reservation_does_not_block_holder_checkout(self):
        self.reservation.delete()
        other = self.make_holder()

        checkout_asset(asset=self.asset, holder=other, request=None)

        self.assertTrue(AssetAssignment.objects.filter(asset=self.asset, assigned_user=other, is_active=True).exists())

    def test_soft_deleted_reservation_does_not_block_location_checkout(self):
        self.reservation.delete()

        checkout_asset(asset=self.asset, location=self.location, request=None)

        self.assertTrue(
            AssetAssignment.objects.filter(asset=self.asset, assigned_location=self.location, is_active=True).exists()
        )

    def test_soft_deleted_reservation_does_not_block_asset_target_checkout(self):
        self.reservation.delete()
        target = self.make_asset()

        checkout_asset(asset=self.asset, asset_target=target, request=None)

        self.assertTrue(
            AssetAssignment.objects.filter(asset=self.asset, assigned_asset=target, is_active=True).exists()
        )

    def test_reserved_holder_can_fulfill_replacement_reservation_after_delete(self):
        self.reservation.delete()
        replacement_holder = self.make_holder()
        replacement = AssetReservation.objects.create(
            asset=self.asset,
            reserved_for=replacement_holder,
            start_date=today() - timedelta(days=1),
            end_date=today() + timedelta(days=3),
            status=ReservationStatusChoices.ACTIVE,
        )

        checkout_asset(asset=self.asset, holder=replacement_holder, request=None)

        self.assertIsNone(replacement.deleted_at)
        self.assertTrue(
            AssetAssignment.objects.filter(asset=self.asset, assigned_user=replacement_holder, is_active=True).exists()
        )

    def test_restoring_reservation_reactivates_checkout_restriction(self):
        self.reservation.delete()
        self.reservation.restore()
        other = self.make_holder()

        with self.assertRaises(ValidationError):
            checkout_asset(asset=self.asset, holder=other, request=None)

    def test_live_pending_reservation_blocks_every_checkout_target(self):
        asset = self.make_asset()
        AssetReservation.objects.create(
            asset=asset,
            reserved_for=self.reserved_for,
            start_date=today() - timedelta(days=1),
            end_date=today() + timedelta(days=3),
            status=ReservationStatusChoices.PENDING,
        )
        targets = (
            {"holder": self.make_holder()},
            {"location": self.location},
            {"asset_target": self.make_asset()},
        )

        for target in targets:
            with self.subTest(target=target):
                with self.assertRaises(ValidationError):
                    checkout_asset(asset=asset, request=None, **target)
