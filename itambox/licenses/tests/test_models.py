from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset
from organization.models import AssetHolder
from software.models import Software

from ..models import License, LicenseSeatAssignment, LicenseTypeChoices

User = get_user_model()


class LicenseEncryptionTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_staff=True, is_superuser=True)
        self.user.set_password("testpassword")
        self.user.save()
        self.client.login(username=self.user.username, password="testpassword")

        self.software = baker.make(
            Software,
            name="Windows 11 Enterprise",
            manufacturer__name="Microsoft",
            manufacturer__slug="microsoft",
            description="OS",
        )

    def test_license_product_key_encryption_lifecycle(self):
        # 1. Create a license with a plaintext product key
        raw_key = "ABCDE-12345-FGHIJ-67890"
        license_obj = baker.make(
            License,
            name="Windows 11 Volume entitlement",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            product_key=raw_key,
            tenant=None,
        )

        # 2. Verify it is encrypted in the database (i.e. starts with enc$)
        db_record = License.objects.get(pk=license_obj.pk)
        self.assertTrue(db_record.product_key.startswith("enc$"))
        self.assertNotEqual(db_record.product_key, raw_key)

        # 3. Verify it is decrypted seamlessly via the model property
        self.assertEqual(db_record.decrypted_product_key, raw_key)

        # 4. Verify detail view displays the decrypted key
        detail_url = reverse("licenses:license_detail", kwargs={"pk": license_obj.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, raw_key)
        self.assertNotContains(response, db_record.product_key)  # Cipher text shouldn't be exposed

        # 5. Verify edit form loads the decrypted key as initial value
        edit_url = reverse("licenses:license_update", kwargs={"pk": license_obj.pk})
        response = self.client.get(edit_url)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.fields["product_key"].initial, raw_key)


class LicenseSeatAssignmentTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_staff=True, is_superuser=True)
        self.user.set_password("testpassword")
        self.user.save()
        self.client.login(username=self.user.username, password="testpassword")

        self.software = baker.make(
            Software, name="Windows 11", manufacturer__name="Microsoft", manufacturer__slug="microsoft"
        )
        self.asset = baker.make(Asset, name="WS-01", asset_tag="TAG-WS-01", tenant=None)
        self.holder = baker.make(AssetHolder, first_name="John", last_name="Doe")

    def test_seat_assignment_to_asset(self):
        license_obj = baker.make(
            License,
            name="Win11 Volume",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=None,
        )
        assignment = baker.make(
            LicenseSeatAssignment,
            license=license_obj,
            asset=self.asset,
            assigned_holder=None,
        )
        self.assertEqual(assignment.license, license_obj)
        self.assertEqual(assignment.asset, self.asset)

    def test_seat_assignment_to_holder(self):
        license_obj = baker.make(
            License,
            name="Win11 User",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=5,
            tenant=None,
        )
        assignment = baker.make(
            LicenseSeatAssignment,
            license=license_obj,
            assigned_holder=self.holder,
            asset=None,
        )
        self.assertEqual(assignment.assigned_holder, self.holder)

    def test_seat_assignment_single_target_constraint(self):
        license_obj = baker.make(
            License,
            name="Win11 Constraint",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=None,
        )
        obj = LicenseSeatAssignment(license=license_obj, asset=self.asset, assigned_holder=self.holder)
        with self.assertRaises(ValidationError):
            obj.full_clean()
        # save() runs full_clean(); bulk_create() skips it, so this reaches the database constraint.
        with transaction.atomic(), self.assertRaisesRegex(IntegrityError, "chk_assignment_to_one_target"):
            LicenseSeatAssignment.objects.bulk_create([obj])

    def test_available_seats(self):
        license_obj = baker.make(
            License,
            name="Win11 5 Seats",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=5,
            tenant=None,
        )
        self.assertEqual(license_obj.available_seats, 5)
        baker.make(LicenseSeatAssignment, license=license_obj, asset=self.asset, assigned_holder=None)
        self.assertEqual(license_obj.available_seats, 4)

    def test_license_expiration(self):
        yesterday = date.today() - timedelta(days=1)
        license_obj = baker.make(
            License,
            name="Expired License",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=1,
            expiration_date=yesterday,
            tenant=None,
        )
        self.assertIsNotNone(license_obj.expiration_date)
        self.assertLess(license_obj.expiration_date, date.today())

    def test_license_future_expiration(self):
        future = date.today() + timedelta(days=365)
        license_obj = baker.make(
            License,
            name="Active License",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=1,
            expiration_date=future,
            tenant=None,
        )
        self.assertGreater(license_obj.expiration_date, date.today())

    def test_seat_assignment_soft_delete(self):
        license_obj = baker.make(
            License,
            name="SoftDelete Seat",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=2,
            tenant=None,
        )
        assignment = baker.make(
            LicenseSeatAssignment,
            license=license_obj,
            asset=self.asset,
            assigned_holder=None,
        )
        self.assertEqual(license_obj.available_seats, 1)
        assignment.delete()
        self.assertEqual(license_obj.available_seats, 2)

    def test_seat_assignment_absolute_url(self):
        license_obj = baker.make(
            License,
            name="URL Test",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=1,
            tenant=None,
        )
        assignment = baker.make(
            LicenseSeatAssignment,
            license=license_obj,
            asset=self.asset,
            assigned_holder=None,
        )
        url = assignment.get_absolute_url()
        self.assertIsNotNone(url)

    def test_seat_assignment_str(self):
        license_obj = baker.make(
            License,
            name="Str Test",
            software=self.software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=1,
            tenant=None,
        )
        assignment = baker.make(
            LicenseSeatAssignment,
            license=license_obj,
            asset=self.asset,
            assigned_holder=None,
        )
        self.assertIn("Str Test", str(assignment))
