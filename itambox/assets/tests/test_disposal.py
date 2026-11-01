"""Tests for AssetDisposal model and dispose_asset() service.

Run with:
    pytest assets/tests/test_disposal.py
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import (
    Asset,
    AssetDisposal,
    DataSanitizationMethodChoices,
    DisposalMethodChoices,
    StatusLabel,
)
from assets.services import checkout_asset, dispose_asset
from core.tests.mixins import TenantTestMixin

User = get_user_model()


class AssetDisposalModelTest(TenantTestMixin, TestCase):
    """Model-level unit tests: fields, defaults, OneToOne constraint."""

    def setUp(self):
        self.setup_tenant_context()
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.deployable_status = baker.make(StatusLabel, type="deployable", name="Deployable")
        self.asset = baker.make(
            Asset,
            name="Test Laptop",
            status=self.deployable_status,
            tenant=self.tenant,
        )

    def _make_disposal(self, **kwargs):
        defaults = dict(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RECYCLE,
            disposal_date="2025-06-01",
            data_sanitization_method=DataSanitizationMethodChoices.NIST_PURGE,
        )
        defaults.update(kwargs)
        return AssetDisposal(**defaults)

    def test_str_representation(self):
        d = self._make_disposal()
        d.save()
        self.assertIn("Test Laptop", str(d))
        self.assertIn("Recycle", str(d))

    def test_default_sanitization_method_is_none(self):
        d = AssetDisposal(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.DESTRUCTION,
            disposal_date="2025-06-01",
        )
        self.assertEqual(d.data_sanitization_method, DataSanitizationMethodChoices.NONE)

    def test_default_weee_compliant_is_false(self):
        d = self._make_disposal()
        d.save()
        d.refresh_from_db()
        self.assertFalse(d.weee_compliant)

    def test_one_disposal_per_asset_enforced(self):
        d1 = self._make_disposal()
        d1.save()
        # A second disposal for the same asset should raise IntegrityError via OneToOne
        from django.db import IntegrityError

        d2 = self._make_disposal()
        with self.assertRaises(IntegrityError):
            d2.save()

    def test_blank_optional_fields_allowed(self):
        d = AssetDisposal(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.DONATION,
            disposal_date="2025-06-01",
            sanitization_certificate="",
            sanitized_by="",
            recipient="",
            proceeds=None,
            currency="",
            notes="",
        )
        d.full_clean()  # must not raise
        d.save()
        self.assertIsNotNone(d.pk)

    def test_proceeds_and_currency_stored(self):
        d = self._make_disposal(proceeds=Decimal("1234.56"), currency="USD")
        d.save()
        d.refresh_from_db()
        self.assertEqual(d.proceeds, Decimal("1234.56"))
        self.assertEqual(d.currency, "USD")

    def test_get_absolute_url_resolves(self):
        d = self._make_disposal()
        d.save()
        url = d.get_absolute_url()
        self.assertIn("/disposals/", url)

    def test_tenant_property_proxies_asset_tenant(self):
        d = self._make_disposal()
        d.save()
        self.assertEqual(d.tenant, self.asset.tenant)


class DisposeAssetServiceTest(TenantTestMixin, TestCase):
    """Integration tests for the dispose_asset() service function."""

    def setUp(self):
        self.setup_tenant_context()
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.deployable = baker.make(StatusLabel, type="deployable", name="Deployable")
        self.archived = baker.make(StatusLabel, type="archived", name="Archived")
        self.asset = baker.make(
            Asset,
            name="Disposal Laptop",
            status=self.deployable,
            tenant=self.tenant,
        )

    def test_dispose_creates_disposal_record(self):
        disposal = dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RECYCLE,
            disposal_date="2025-06-10",
            data_sanitization_method=DataSanitizationMethodChoices.NIST_PURGE,
            sanitization_certificate="CERT-001",
            sanitized_by="IT Team",
            user=self.user,
        )
        self.assertIsNotNone(disposal.pk)
        self.assertEqual(disposal.asset, self.asset)
        self.assertEqual(disposal.sanitization_certificate, "CERT-001")

    def test_dispose_stamps_disposed_at_on_asset(self):
        dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.DESTRUCTION,
            disposal_date="2025-06-10",
            user=self.user,
        )
        self.asset.refresh_from_db()
        self.assertIsNotNone(self.asset.disposed_at)

    def test_dispose_transitions_asset_to_archived(self):
        """Asset status must change to an 'archived' type label after disposal."""
        dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RESALE,
            disposal_date="2025-06-10",
            user=self.user,
        )
        self.asset.refresh_from_db()
        self.assertIsNotNone(self.asset.status)
        self.assertEqual(self.asset.status.type, "archived")

    def test_dispose_stamps_disposal_value_from_proceeds(self):
        dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RESALE,
            disposal_date="2025-06-10",
            proceeds=Decimal("500.00"),
            user=self.user,
        )
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.disposal_value, Decimal("500.00"))

    def test_dispose_auto_checks_in_active_assignment(self):
        """Active assignment must be closed before disposal."""
        from organization.models import AssetHolder

        holder = baker.make(AssetHolder, tenant=self.tenant)
        deployed = baker.make(StatusLabel, type="deployed", name="Disposal Deployed")
        checkout_asset(self.asset, holder=holder, user=self.user, status=deployed)
        self.assertTrue(self.asset.active_assignment is not None)

        dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RECYCLE,
            disposal_date="2025-06-10",
            user=self.user,
        )
        self.asset.refresh_from_db()
        self.assertIsNone(self.asset.active_assignment)

    def test_second_disposal_while_active_record_exists_is_rejected(self):
        """A second disposal with an ACTIVE record must be rejected, never replace evidence.

        #496: the predecessor implementation hard-deleted the existing record and
        inserted a replacement, silently destroying disposal evidence.
        """
        first = dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RECYCLE,
            disposal_date="2025-06-10",
            sanitization_certificate="CERT-ORIGINAL",
            user=self.user,
        )
        with self.assertRaises(ValidationError):
            dispose_asset(
                asset=self.asset,
                disposal_method=DisposalMethodChoices.DONATION,
                disposal_date="2025-07-01",
                user=self.user,
            )

        self.asset.refresh_from_db()
        records = AssetDisposal.all_objects.filter(asset=self.asset)
        self.assertEqual(records.count(), 1)
        survivor = records.get()
        self.assertEqual(survivor.pk, first.pk)
        self.assertEqual(survivor.disposal_method, DisposalMethodChoices.RECYCLE)
        self.assertEqual(survivor.sanitization_certificate, "CERT-ORIGINAL")

    def test_currency_stored_on_disposal_record(self):
        disposal = dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.RESALE,
            disposal_date="2025-06-10",
            proceeds=Decimal("999.99"),
            currency="EUR",
            user=self.user,
        )
        self.assertEqual(disposal.currency, "EUR")


class AssetDisposalViewSmokeTest(TenantTestMixin, TestCase):
    """Basic smoke tests for the disposal views (not full form submission)."""

    def setUp(self):
        self.setup_tenant_context()
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.client.force_login(self.user)
        self.deployable = baker.make(StatusLabel, type="deployable", name="Deployable")
        self.asset = baker.make(
            Asset,
            name="View Laptop",
            status=self.deployable,
            tenant=self.tenant,
        )

    def test_disposal_list_view_200(self):
        url = reverse("assets:assetdisposal_list")
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

    def test_disposal_create_view_200(self):
        url = reverse("assets:assetdisposal_create")
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

    def test_asset_dispose_action_view_200(self):
        url = reverse("assets:asset_dispose", kwargs={"pk": self.asset.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])

    def test_disposal_detail_view_200(self):
        baker.make(StatusLabel, type="archived", name="Archived")
        disposal = dispose_asset(
            asset=self.asset,
            disposal_method=DisposalMethodChoices.DESTRUCTION,
            disposal_date="2025-06-01",
            user=self.user,
        )
        url = reverse("assets:assetdisposal_detail", kwargs={"pk": disposal.pk})
        response = self.client.get(url)
        self.assertIn(response.status_code, [200, 302])
