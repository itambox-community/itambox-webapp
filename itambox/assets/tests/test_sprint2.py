"""Sprint 2 tests: per-record currency fields and new lifecycle states.

Run with:
    pytest assets/tests/test_sprint2.py

Requires: running PostgreSQL (same as all other assets tests).
Uses TenantTestMixin + model_bakery per project convention.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from model_bakery import baker

from assets.choices import StatusTypeChoices
from assets.models import Asset, AssetMaintenance, StatusLabel
from core.tests.mixins import TenantTestMixin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status(type_: str, **kwargs) -> StatusLabel:
    """Create a StatusLabel with the given type."""
    return baker.make(StatusLabel, type=type_, **kwargs)


# ---------------------------------------------------------------------------
# Task 1 — currency field: Asset
# ---------------------------------------------------------------------------


class AssetCurrencyFieldTests(TenantTestMixin, TestCase):
    """CurrencyField on Asset defaults to blank (= use tenant currency)."""

    def setUp(self):
        self.setup_tenant_context()

    def test_currency_defaults_to_blank(self):
        """A freshly created asset should have currency='' (use tenant default)."""
        status = _status("deployable")
        asset = baker.make(Asset, tenant=self.tenant, status=status)
        asset.refresh_from_db()
        self.assertEqual(asset.currency, "")

    def test_currency_can_be_set_explicitly(self):
        """Setting currency='USD' persists correctly."""
        status = _status("deployable")
        asset = baker.make(Asset, tenant=self.tenant, status=status, currency="USD")
        asset.refresh_from_db()
        self.assertEqual(asset.currency, "USD")

    def test_currency_blank_allows_save(self):
        """blank=True means the field is optional; no ValidationError when omitted."""
        status = _status("deployable")
        asset = baker.prepare(Asset, tenant=self.tenant, status=status, currency="")
        # full_clean should not raise for blank currency
        try:
            asset.full_clean()
        except ValidationError as exc:
            if "currency" in exc.message_dict:
                self.fail(f"full_clean raised ValidationError for blank currency: {exc}")

    def test_currency_rejects_invalid_code(self):
        """An unrecognised currency code is rejected by field-level validation."""
        status = _status("deployable")
        asset = baker.prepare(Asset, tenant=self.tenant, status=status, currency="XYZ")
        with self.assertRaises(ValidationError) as cm:
            asset.full_clean()
        self.assertIn("currency", cm.exception.message_dict)

    def test_currency_accepts_all_defined_choices(self):
        """Every code listed in CURRENCY_CHOICES must pass validation."""
        from core.currency import CURRENCY_CHOICES

        status = _status("deployable")
        for code, _ in CURRENCY_CHOICES:
            asset = baker.prepare(Asset, tenant=self.tenant, status=status, currency=code)
            try:
                asset.full_clean()
            except ValidationError as exc:
                if "currency" in exc.message_dict:
                    self.fail("full_clean rejected valid currency code '%s': %s" % (code, exc))


# ---------------------------------------------------------------------------
# Task 1 — currency field: AssetMaintenance
# ---------------------------------------------------------------------------


class AssetMaintenanceCurrencyFieldTests(TenantTestMixin, TestCase):
    """CurrencyField on AssetMaintenance defaults to blank."""

    def setUp(self):
        self.setup_tenant_context()

    def _make_asset(self) -> Asset:
        return baker.make(Asset, tenant=self.tenant, status=_status("deployable"))

    def test_maintenance_currency_defaults_to_blank(self):
        asset = self._make_asset()
        maint = baker.make(AssetMaintenance, asset=asset)
        maint.refresh_from_db()
        self.assertEqual(maint.currency, "")

    def test_maintenance_currency_can_be_set(self):
        asset = self._make_asset()
        maint = baker.make(AssetMaintenance, asset=asset, currency="GBP")
        maint.refresh_from_db()
        self.assertEqual(maint.currency, "GBP")

    def test_maintenance_currency_blank_is_valid(self):
        asset = self._make_asset()
        maint = baker.prepare(AssetMaintenance, asset=asset, currency="")
        try:
            maint.full_clean()
        except ValidationError as exc:
            if "currency" in exc.message_dict:
                self.fail(f"full_clean raised ValidationError for blank currency: {exc}")

    def test_maintenance_currency_rejects_invalid_code(self):
        asset = self._make_asset()
        maint = baker.prepare(AssetMaintenance, asset=asset, currency="NOPE")
        with self.assertRaises(ValidationError) as cm:
            maint.full_clean()
        self.assertIn("currency", cm.exception.message_dict)


# ---------------------------------------------------------------------------
# Task 2 — new lifecycle states in StatusTypeChoices
# ---------------------------------------------------------------------------


class StatusTypeChoicesTests(TestCase):
    """Verify the new choices are present and correctly defined."""

    def test_in_repair_choice_exists(self):
        self.assertIn("in_repair", StatusTypeChoices.values)

    def test_on_order_choice_exists(self):
        self.assertIn("on_order", StatusTypeChoices.values)

    def test_in_repair_label(self):
        self.assertEqual(StatusTypeChoices.IN_REPAIR.label, "In Repair")

    def test_on_order_label(self):
        self.assertEqual(StatusTypeChoices.ON_ORDER.label, "On Order")


# ---------------------------------------------------------------------------
# Task 2 — state machine transitions
# ---------------------------------------------------------------------------


class AssetStateMachineTransitionTests(TenantTestMixin, TestCase):
    """Valid and invalid transitions involving in_repair and on_order."""

    def setUp(self):
        self.setup_tenant_context()
        self.deployable = _status("deployable", name="Deployable")
        self.deployed = _status("deployed", name="Deployed")
        self.pending = _status("pending", name="Pending")
        self.undeployable = _status("undeployable", name="Undeployable")
        self.archived = _status("archived", name="Archived")
        self.in_repair = _status("in_repair", name="In Repair")
        self.on_order = _status("on_order", name="On Order")

    def _asset(self, status: StatusLabel) -> Asset:
        return baker.make(Asset, tenant=self.tenant, status=status)

    # --- Valid transitions ---

    def test_deployed_to_in_repair_requires_checkin(self):
        """Leaving the deployed status is owned by check-in, not by a status edit."""
        asset = self._asset(self.deployable)
        Asset.objects.filter(pk=asset.pk).update(status=self.deployed)
        asset.refresh_from_db()
        asset.status = self.in_repair
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_deployable_to_in_repair(self):
        """A deployable asset can be sent for repair."""
        asset = self._asset(self.deployable)
        asset.status = self.in_repair
        asset.full_clean()

    def test_pending_to_in_repair(self):
        """A pending asset can be sent for repair."""
        asset = self._asset(self.pending)
        asset.status = self.in_repair
        asset.full_clean()

    def test_in_repair_to_deployable(self):
        """Asset returns from repair as deployable."""
        asset = self._asset(self.in_repair)
        asset.status = self.deployable
        asset.full_clean()

    def test_in_repair_to_undeployable(self):
        """Asset returns from repair but is beyond repair → undeployable."""
        asset = self._asset(self.in_repair)
        asset.status = self.undeployable
        asset.full_clean()

    def test_in_repair_to_archived(self):
        """Asset written off directly from repair."""
        asset = self._asset(self.in_repair)
        asset.status = self.archived
        asset.full_clean()

    def test_on_order_to_pending(self):
        """Received order moves to pending."""
        asset = self._asset(self.on_order)
        asset.status = self.pending
        asset.full_clean()

    def test_on_order_to_deployable(self):
        """Received and ready asset can jump straight to deployable."""
        asset = self._asset(self.on_order)
        asset.status = self.deployable
        asset.full_clean()

    def test_pending_to_on_order(self):
        """Asset on hold can be placed on order."""
        asset = self._asset(self.pending)
        asset.status = self.on_order
        asset.full_clean()

    # --- Invalid transitions ---

    def test_on_order_cannot_go_directly_to_deployed(self):
        """on_order → deployed is not a valid transition (must go via pending/deployable)."""
        asset = self._asset(self.on_order)
        asset.status = self.deployed
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_on_order_cannot_go_to_in_repair(self):
        """on_order → in_repair is not allowed."""
        asset = self._asset(self.on_order)
        asset.status = self.in_repair
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_in_repair_cannot_go_to_deployed(self):
        """in_repair → deployed is not valid; must first return to deployable."""
        asset = self._asset(self.in_repair)
        asset.status = self.deployed
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_in_repair_cannot_go_to_pending(self):
        """in_repair → pending is not a valid transition."""
        asset = self._asset(self.in_repair)
        asset.status = self.pending
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_archived_cannot_go_to_in_repair(self):
        """archived → in_repair is not allowed (archived can only return to pending)."""
        asset = self._asset(self.archived)
        asset.status = self.in_repair
        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_archived_cannot_go_to_on_order(self):
        """archived → on_order is not allowed."""
        asset = self._asset(self.archived)
        asset.status = self.on_order
        with self.assertRaises(ValidationError):
            asset.full_clean()

    # --- Same-type no-op ---

    def test_same_status_no_transition_error(self):
        """Saving an asset with the same status should not raise."""
        asset = self._asset(self.in_repair)
        asset.status = self.in_repair  # same object, same type
        asset.full_clean()  # must not raise


# ---------------------------------------------------------------------------
# Task 2 — assignability gate: in_repair and on_order are NOT assignable
# ---------------------------------------------------------------------------


class NonAssignableStatusTests(TenantTestMixin, TestCase):
    """Assets in in_repair or on_order cannot be requested/assigned."""

    def setUp(self):
        self.setup_tenant_context()

    def test_in_repair_asset_not_requestable_via_asset_request(self):
        """AssetRequest.clean() rejects an in_repair asset (only deployable is valid)."""
        from django.contrib.auth import get_user_model

        from assets.models import AssetRequest

        User = get_user_model()
        user = baker.make(User)
        in_repair_status = _status("in_repair", name="In Repair Guard")
        asset = baker.make(
            Asset,
            tenant=self.tenant,
            status=in_repair_status,
            requestable=True,
        )
        request_obj = baker.prepare(
            AssetRequest,
            tenant=self.tenant,
            requester=user,
            asset=asset,
            asset_type=None,
        )
        with self.assertRaises(ValidationError) as cm:
            request_obj.full_clean()
        # The clean method checks status.type != 'deployable'
        self.assertIn("__all__", cm.exception.message_dict)

    def test_on_order_asset_not_requestable_via_asset_request(self):
        """AssetRequest.clean() rejects an on_order asset."""
        from django.contrib.auth import get_user_model

        from assets.models import AssetRequest

        User = get_user_model()
        user = baker.make(User)
        on_order_status = _status("on_order", name="On Order Guard")
        asset = baker.make(
            Asset,
            tenant=self.tenant,
            status=on_order_status,
            requestable=True,
        )
        request_obj = baker.prepare(
            AssetRequest,
            tenant=self.tenant,
            requester=user,
            asset=asset,
            asset_type=None,
        )
        with self.assertRaises(ValidationError) as cm:
            request_obj.full_clean()
        self.assertIn("__all__", cm.exception.message_dict)
