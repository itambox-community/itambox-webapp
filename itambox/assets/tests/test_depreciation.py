"""
Unit tests for assets.depreciation — pure compute_book_value and resolve_policy.

All tests use simple mock objects (no DB) so they run without --reuse-db overhead.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from assets.depreciation import compute_book_value, resolve_policy


def _policy(months=36, method="straight_line", convention="exclude_purchase_month", threshold=None):
    return SimpleNamespace(
        months=months,
        method=method,
        convention=convention,
        immediate_expense_threshold=threshold,
    )


def _asset(
    purchase_cost=None,
    purchase_date=None,
    salvage_value=None,
    depreciation_override_id=None,
    depreciation_override=None,
    tenant=None,
    asset_type=None,
    in_service_date=None,
    disposed_at=None,
    disposal_value=None,
):
    return SimpleNamespace(
        purchase_cost=Decimal(str(purchase_cost)) if purchase_cost is not None else None,
        purchase_date=purchase_date,
        salvage_value=Decimal(str(salvage_value)) if salvage_value is not None else None,
        depreciation_override_id=depreciation_override_id,
        depreciation_override=depreciation_override,
        tenant=tenant,
        asset_type=asset_type,
        in_service_date=in_service_date,
        disposed_at=disposed_at,
        disposal_value=Decimal(str(disposal_value)) if disposal_value is not None else None,
    )


class TestComputeBookValue(TestCase):
    # --- Golden cases ---

    def test_no_purchase_cost_returns_none(self):
        asset = _asset(purchase_cost=None)
        self.assertIsNone(compute_book_value(asset))

    def test_no_policy_returns_cost(self):
        asset = _asset(purchase_cost=1000, purchase_date=datetime.date(2020, 1, 1))
        result = compute_book_value(asset)
        self.assertEqual(result, Decimal("1000.00"))

    def test_policy_without_clock_start_returns_rounded_purchase_cost(self):
        policy = _policy(months=36)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        asset = _asset(purchase_cost="1000.005", purchase_date=None, in_service_date=None, asset_type=asset_type)

        self.assertEqual(compute_book_value(asset, on_date=datetime.date(2025, 1, 1)), Decimal("1000.01"))

    def test_mid_life_straight_line(self):
        # 36-month policy, purchased 18 months ago (exclude_purchase_month)
        # monthly = (1200-200)/36 = 27.77... per month
        # value = 1200 - 27.77... * 18 = 700.00
        policy = _policy(months=36)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2022, 1, 1)
        on_date = datetime.date(2023, 7, 1)  # 18 months later
        asset = _asset(purchase_cost=1200, salvage_value=200, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        self.assertEqual(result, Decimal("700.00"))

    def test_past_end_returns_salvage(self):
        policy = _policy(months=36)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2020, 1, 1)
        on_date = datetime.date(2024, 1, 1)  # 48 months > 36
        asset = _asset(purchase_cost=1200, salvage_value=200, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        self.assertEqual(result, Decimal("200.00"))

    def test_salvage_floor_respected(self):
        # Odd division that might land below salvage without max()
        policy = _policy(months=7)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2023, 1, 1)
        on_date = datetime.date(2023, 7, 1)  # 6 months
        asset = _asset(purchase_cost=1000, salvage_value=500, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        self.assertGreaterEqual(result, Decimal("500.00"))

    def test_future_purchase_date_returns_cost(self):
        policy = _policy(months=36)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2030, 1, 1)
        on_date = datetime.date(2024, 1, 1)
        asset = _asset(purchase_cost=1000, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        self.assertEqual(result, Decimal("1000.00"))

    def test_rounding_to_two_decimal_places(self):
        # 1000 / 3 months → 333.33... per month; 2 months held → 333.33
        policy = _policy(months=3)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2024, 1, 1)
        on_date = datetime.date(2024, 3, 1)  # 2 months
        asset = _asset(purchase_cost=1000, salvage_value=0, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        # 1000 - (333.333... * 2) = 333.33 (ROUND_HALF_UP)
        self.assertEqual(result, Decimal("333.33"))

    def test_method_none_returns_cost(self):
        policy = _policy(months=36, method="none")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2020, 1, 1)
        on_date = datetime.date(2024, 1, 1)
        asset = _asset(purchase_cost=1000, purchase_date=purchase_date, asset_type=asset_type)
        self.assertEqual(compute_book_value(asset, on_date=on_date), Decimal("1000.00"))

    def test_method_none_ignores_immediate_expense_threshold_after_months_held(self):
        policy = _policy(months=36, method="none", convention="include_purchase_month", threshold=Decimal("800"))
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        asset = _asset(
            purchase_cost=750,
            salvage_value=0,
            purchase_date=datetime.date(2024, 1, 1),
            asset_type=asset_type,
        )
        self.assertEqual(compute_book_value(asset, on_date=datetime.date(2024, 12, 1)), Decimal("750.00"))

    def test_exclude_purchase_month_same_month_no_depreciation(self):
        """Jan purchase, on_date still Jan → 0 months held → full cost."""
        policy = _policy(months=36, convention="exclude_purchase_month")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2024, 1, 15)
        on_date = datetime.date(2024, 1, 31)
        asset = _asset(purchase_cost=1000, purchase_date=purchase_date, asset_type=asset_type)
        self.assertEqual(compute_book_value(asset, on_date=on_date), Decimal("1000.00"))

    def test_include_purchase_month_same_month_one_month_charged(self):
        """Jan purchase, on_date still Jan → 1 month held with include convention."""
        policy = _policy(months=36, convention="include_purchase_month")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2024, 1, 15)
        on_date = datetime.date(2024, 1, 31)
        asset = _asset(purchase_cost=1200, salvage_value=0, purchase_date=purchase_date, asset_type=asset_type)
        # 1 month: 1200 - (1200/36)*1 = 1200 - 33.33 = 1166.67
        result = compute_book_value(asset, on_date=on_date)
        self.assertEqual(result, Decimal("1166.67"))

    def test_include_purchase_month_future_purchase_returns_cost(self):
        """Future purchase with include convention: months_held = max(-1, 0) = 0 → full cost."""
        policy = _policy(months=36, convention="include_purchase_month")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2025, 3, 1)
        on_date = datetime.date(2025, 1, 15)
        asset = _asset(purchase_cost=1000, purchase_date=purchase_date, asset_type=asset_type)
        self.assertEqual(compute_book_value(asset, on_date=on_date), Decimal("1000.00"))

    def test_before_purchase_date_preserves_cost_for_both_conventions(self):
        purchase_date = datetime.date(2026, 10, 20)
        on_date = datetime.date(2026, 10, 1)
        for convention in ("exclude_purchase_month", "include_purchase_month"):
            with self.subTest(convention=convention):
                policy = _policy(months=36, convention=convention, threshold=Decimal("800"))
                asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
                asset = _asset(
                    purchase_cost=500,
                    salvage_value=0,
                    purchase_date=purchase_date,
                    asset_type=asset_type,
                )

                self.assertEqual(compute_book_value(asset, on_date=on_date), Decimal("500.00"))

    def test_before_in_service_date_preserves_cost_for_both_conventions(self):
        purchase_date = datetime.date(2026, 10, 1)
        in_service_date = datetime.date(2026, 10, 20)
        on_date = datetime.date(2026, 10, 1)
        for convention in ("exclude_purchase_month", "include_purchase_month"):
            with self.subTest(convention=convention):
                policy = _policy(months=36, convention=convention, threshold=Decimal("800"))
                asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
                asset = _asset(
                    purchase_cost=500,
                    salvage_value=0,
                    purchase_date=purchase_date,
                    in_service_date=in_service_date,
                    asset_type=asset_type,
                )

                self.assertEqual(compute_book_value(asset, on_date=on_date), Decimal("500.00"))

    def test_include_purchase_month_starts_on_resolved_start_date(self):
        policy = _policy(months=36, convention="include_purchase_month", threshold=Decimal("800"))
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2026, 10, 1)
        in_service_date = datetime.date(2026, 10, 20)

        purchase_start_asset = _asset(purchase_cost=500, purchase_date=purchase_date, asset_type=asset_type)
        in_service_start_asset = _asset(
            purchase_cost=500,
            purchase_date=purchase_date,
            in_service_date=in_service_date,
            asset_type=asset_type,
        )

        self.assertEqual(compute_book_value(purchase_start_asset, on_date=purchase_date), Decimal("0.00"))
        self.assertEqual(compute_book_value(in_service_start_asset, on_date=in_service_date), Decimal("0.00"))

    # --- GWG immediate expense threshold ---

    def test_gwg_at_or_below_threshold_first_month(self):
        """Cost <= threshold, include_purchase_month: months_held=1 in acquisition month → expensed to salvage."""
        policy = _policy(months=36, convention="include_purchase_month", threshold=Decimal("800"))
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2024, 1, 1)
        on_date = datetime.date(2024, 1, 1)
        # include_purchase_month: months_held = max(0+1, 0) = 1 → already >= 1 → salvage
        asset = _asset(purchase_cost=750, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        self.assertEqual(result, Decimal("0.00"))

    def test_gwg_above_threshold_depreciates_normally(self):
        policy = _policy(months=36, convention="exclude_purchase_month", threshold=Decimal("800"))
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2024, 1, 1)
        on_date = datetime.date(2024, 7, 1)  # 6 months
        asset = _asset(purchase_cost=1200, salvage_value=0, purchase_date=purchase_date, asset_type=asset_type)
        result = compute_book_value(asset, on_date=on_date)
        # 1200 - (1200/36)*6 = 1200 - 200 = 1000
        self.assertEqual(result, Decimal("1000.00"))

    # --- Disposal freeze ---

    def test_disposed_returns_disposal_value(self):
        policy = _policy(months=36)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        asset = _asset(
            purchase_cost=1200,
            purchase_date=datetime.date(2022, 1, 1),
            asset_type=asset_type,
            disposed_at=datetime.datetime(2024, 1, 1),
            disposal_value=Decimal("600.00"),
        )
        # Even calling many months later — frozen at 600
        result = compute_book_value(asset, on_date=datetime.date(2030, 1, 1))
        self.assertEqual(result, Decimal("600.00"))

    def test_disposal_value_is_frozen_at_archive_time(self):
        policy = _policy(months=12, convention="exclude_purchase_month")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        asset = _asset(
            purchase_cost=1200,
            salvage_value=0,
            purchase_date=datetime.date(2024, 1, 1),
            asset_type=asset_type,
            disposed_at=datetime.datetime(2025, 1, 15),
            disposal_value=Decimal("100.00"),
        )
        policy.months = 1
        self.assertEqual(compute_book_value(asset, on_date=datetime.date(2035, 1, 1)), Decimal("100.00"))

    def test_undisposed_does_not_return_disposal_value(self):
        policy = _policy(months=36, convention="exclude_purchase_month")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2022, 1, 1)
        on_date = datetime.date(2025, 1, 1)  # 36 months → salvage
        asset = _asset(
            purchase_cost=1200,
            salvage_value=0,
            purchase_date=purchase_date,
            asset_type=asset_type,
            disposed_at=None,
            disposal_value=None,
        )
        result = compute_book_value(asset, on_date=on_date)
        self.assertEqual(result, Decimal("0.00"))

    # --- In-service date ---

    def test_in_service_date_shifts_schedule(self):
        """Depreciation starts from in_service_date, not purchase_date."""
        policy = _policy(months=12, convention="exclude_purchase_month")
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        purchase_date = datetime.date(2024, 1, 1)
        in_service_date = datetime.date(2024, 7, 1)
        on_date = datetime.date(2025, 1, 1)  # 6 months from in_service_date
        asset = _asset(
            purchase_cost=1200,
            salvage_value=0,
            purchase_date=purchase_date,
            in_service_date=in_service_date,
            asset_type=asset_type,
        )
        result = compute_book_value(asset, on_date=on_date)
        # 6 months of 12: 1200 - (1200/12)*6 = 600
        self.assertEqual(result, Decimal("600.00"))

    # --- Float coercion (defensive: callers like the seed assign plain floats) ---

    def test_float_purchase_cost_no_policy(self):
        """A float purchase_cost must not crash on .quantize."""
        asset = SimpleNamespace(
            purchase_cost=1000.0,
            purchase_date=datetime.date(2020, 1, 1),
            salvage_value=None,
            depreciation_override=None,
            tenant=None,
            asset_type=None,
            in_service_date=None,
            disposed_at=None,
            disposal_value=None,
        )
        result = compute_book_value(asset)
        self.assertEqual(result, Decimal("1000.00"))

    def test_float_purchase_cost_and_salvage_straight_line(self):
        """float purchase_cost / Decimal months must not raise TypeError."""
        policy = _policy(months=36)
        asset_type = SimpleNamespace(depreciation_id=1, depreciation=policy)
        asset = SimpleNamespace(
            purchase_cost=1200.0,
            salvage_value=200.0,
            purchase_date=datetime.date(2022, 1, 1),
            depreciation_override=None,
            tenant=None,
            asset_type=asset_type,
            in_service_date=None,
            disposed_at=None,
            disposal_value=None,
        )
        # Same as test_mid_life_straight_line but with raw floats → 700.00
        result = compute_book_value(asset, on_date=datetime.date(2023, 7, 1))
        self.assertEqual(result, Decimal("700.00"))


class TestResolvePolicy(TestCase):
    def _make_asset(self, override=None, tenant_default=None, type_policy=None):
        asset_type = None
        if type_policy is not None:
            asset_type = SimpleNamespace(depreciation_id=1, depreciation=type_policy)
        else:
            asset_type = SimpleNamespace(depreciation_id=None, depreciation=None)

        tenant = None
        if tenant_default is not None:
            tenant = SimpleNamespace(default_depreciation_id=1, default_depreciation=tenant_default)
        else:
            tenant = SimpleNamespace(default_depreciation_id=None, default_depreciation=None)

        override_id = 1 if override is not None else None
        return SimpleNamespace(
            depreciation_override_id=override_id,
            depreciation_override=override,
            tenant=tenant,
            asset_type=asset_type,
        )

    def test_override_wins(self):
        policy_override = _policy(months=12)
        policy_tenant = _policy(months=24)
        policy_type = _policy(months=36)
        asset = self._make_asset(override=policy_override, tenant_default=policy_tenant, type_policy=policy_type)
        policy, rung = resolve_policy(asset)
        self.assertEqual(policy.months, 12)
        self.assertEqual(rung, "override")

    def test_tenant_wins_over_type(self):
        policy_tenant = _policy(months=24)
        policy_type = _policy(months=36)
        asset = self._make_asset(tenant_default=policy_tenant, type_policy=policy_type)
        policy, rung = resolve_policy(asset)
        self.assertEqual(policy.months, 24)
        self.assertEqual(rung, "tenant")

    def test_type_is_last_rung(self):
        policy_type = _policy(months=36)
        asset = self._make_asset(type_policy=policy_type)
        policy, rung = resolve_policy(asset)
        self.assertEqual(policy.months, 36)
        self.assertEqual(rung, "type")

    def test_no_policy_returns_none(self):
        asset = self._make_asset()
        policy, rung = resolve_policy(asset)
        self.assertIsNone(policy)
        self.assertIsNone(rung)
