"""
Tests for Part 6: audit cadence (Category.audit_interval_months, Asset.audit_due_date,
Asset.audit_overdue, AssetFilterSet audit_due filter, alert rule respects per-category cadence).
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from assets.filters import AssetFilterSet
from assets.models import Asset, AssetType, Category, Manufacturer, StatusLabel
from core.tests.mixins import TenantTestMixin

User = get_user_model()


def _make_category(interval_months=None):
    cat = baker.make(Category, audit_interval_months=interval_months)
    return cat


def _make_asset_type(category):
    mfr = baker.make(Manufacturer)
    return baker.make(AssetType, manufacturer=mfr, category=category)


class AuditDueDatePropertyTests(TestCase):
    """Unit tests for Asset.audit_due_date and Asset.audit_overdue properties."""

    def setUp(self):
        self.status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.now = timezone.now()

    def _make(self, interval_months=None, last_audited=None, created_at_offset_days=None):
        cat = _make_category(interval_months)
        at = _make_asset_type(cat)
        asset = baker.make(Asset, asset_type=at, status=self.status, last_audited=last_audited)
        if created_at_offset_days is not None:
            Asset.objects.filter(pk=asset.pk).update(created_at=self.now - timedelta(days=created_at_offset_days))
            asset.refresh_from_db()
        return asset

    def test_no_category_gives_none(self):
        asset = baker.make(Asset, asset_type=None, status=self.status)
        self.assertIsNone(asset.audit_due_date)
        self.assertFalse(asset.audit_overdue)

    def test_category_without_interval_gives_none(self):
        asset = self._make(interval_months=None)
        self.assertIsNone(asset.audit_due_date)
        self.assertFalse(asset.audit_overdue)

    def test_due_date_from_last_audited(self):
        audited_at = timezone.make_aware(datetime(2026, 1, 15, 12))
        asset = self._make(interval_months=1, last_audited=audited_at)
        expected = timezone.make_aware(datetime(2026, 2, 15, 12))
        self.assertEqual(asset.audit_due_date, expected)

    def test_twelve_month_interval_advances_one_calendar_year(self):
        audited_at = timezone.make_aware(datetime(2026, 1, 1, 12))
        asset = self._make(interval_months=12, last_audited=audited_at)

        self.assertEqual(asset.audit_due_date, timezone.make_aware(datetime(2027, 1, 1, 12)))

    def test_month_end_and_leap_year_dates_clamp_to_last_day(self):
        cases = (
            (datetime(2024, 1, 31, 12), 1, datetime(2024, 2, 29, 12)),
            (datetime(2025, 1, 31, 12), 1, datetime(2025, 2, 28, 12)),
            (datetime(2024, 2, 29, 12), 12, datetime(2025, 2, 28, 12)),
        )
        for audited_at, interval_months, expected in cases:
            with self.subTest(audited_at=audited_at, interval_months=interval_months):
                asset = self._make(
                    interval_months=interval_months,
                    last_audited=timezone.make_aware(audited_at),
                )
                self.assertEqual(asset.audit_due_date, timezone.make_aware(expected))

    def test_overdue_boundary_uses_due_calendar_date(self):
        audited_at = timezone.make_aware(datetime(2026, 1, 31))
        asset = self._make(interval_months=1, last_audited=audited_at)
        due_at = timezone.make_aware(datetime(2026, 2, 28))
        next_day = timezone.make_aware(datetime(2026, 3, 1))

        with patch("django.utils.timezone.now", return_value=due_at):
            self.assertFalse(asset.audit_overdue)
        with patch("django.utils.timezone.now", return_value=next_day):
            self.assertTrue(asset.audit_overdue)

    def test_due_date_falls_back_to_created_at(self):
        asset = self._make(interval_months=1, last_audited=None, created_at_offset_days=5)
        due = asset.audit_due_date
        self.assertIsNotNone(due)
        # created 5 days ago, interval 30 days → due in ~25 days
        self.assertGreater(due, self.now)

    def test_overdue_when_past_due(self):
        audited_at = self.now - timedelta(days=35)
        asset = self._make(interval_months=1, last_audited=audited_at)
        self.assertTrue(asset.audit_overdue)

    def test_not_overdue_when_within_interval(self):
        audited_at = self.now - timedelta(days=5)
        asset = self._make(interval_months=1, last_audited=audited_at)
        self.assertFalse(asset.audit_overdue)


class AuditDueFilterTests(TenantTestMixin, TestCase):
    """AssetFilterSet.filter_audit_due — overdue and up-to-date segmentation."""

    def setUp(self):
        self.setup_tenant_context()
        self.status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.now = timezone.now()

        # Category with 1-month interval
        self.cat_with_interval = _make_category(interval_months=1)
        self.at_interval = _make_asset_type(self.cat_with_interval)

        # Category with no interval
        self.cat_no_interval = _make_category(interval_months=None)
        self.at_no_interval = _make_asset_type(self.cat_no_interval)

    def _make_asset(self, asset_type, last_audited=None):
        return baker.make(
            Asset,
            asset_type=asset_type,
            status=self.status,
            last_audited=last_audited,
            tenant=self.tenant,
        )

    def _filter(self, value):
        qs = Asset.objects.filter(tenant=self.tenant)
        fs = AssetFilterSet(data={"audit_due": value}, queryset=qs)
        return list(fs.qs)

    def test_overdue_filter_returns_overdue_assets(self):
        overdue = self._make_asset(self.at_interval, last_audited=self.now - timedelta(days=40))
        fresh = self._make_asset(self.at_interval, last_audited=self.now - timedelta(days=5))
        no_cadence = self._make_asset(self.at_no_interval)

        results = self._filter("true")
        self.assertIn(overdue, results)
        self.assertNotIn(fresh, results)
        self.assertNotIn(no_cadence, results)

    def test_up_to_date_filter_excludes_overdue(self):
        overdue = self._make_asset(self.at_interval, last_audited=self.now - timedelta(days=40))
        fresh = self._make_asset(self.at_interval, last_audited=self.now - timedelta(days=5))

        results = self._filter("false")
        self.assertNotIn(overdue, results)
        self.assertIn(fresh, results)

    def test_filter_uses_calendar_month_deadline(self):
        audited_at = timezone.make_aware(datetime(2026, 1, 31))
        asset = self._make_asset(self.at_interval, last_audited=audited_at)
        due_at = timezone.make_aware(datetime(2026, 2, 28))
        next_day = timezone.make_aware(datetime(2026, 3, 1))

        with patch("django.utils.timezone.now", return_value=due_at):
            self.assertNotIn(asset, self._filter("true"))
        with patch("django.utils.timezone.now", return_value=next_day):
            self.assertIn(asset, self._filter("true"))

    def test_category_alert_uses_calendar_month_deadline(self):
        from extras.models import AlertRule
        from extras.tasks.alerts import _match_audit_overdue

        audited_at = timezone.make_aware(datetime(2026, 1, 31))
        asset = self._make_asset(self.at_interval, last_audited=audited_at)
        due_at = timezone.make_aware(datetime(2026, 2, 28))
        next_day = timezone.make_aware(datetime(2026, 3, 1))
        rule = baker.make(
            AlertRule,
            alert_type=AlertRule.ALERT_TYPE_AUDIT_OVERDUE,
            threshold_value=365,
            tenant=None,
            is_active=True,
        )

        due_day_matches = _match_audit_overdue(rule, due_at.date())
        next_day_matches = _match_audit_overdue(rule, next_day.date())

        self.assertNotIn(asset.pk, {match["obj"].pk for match in due_day_matches})
        self.assertIn(asset.pk, {match["obj"].pk for match in next_day_matches})

    def test_null_last_audited_counts_as_overdue(self):
        never_audited = self._make_asset(self.at_interval, last_audited=None)
        # created_at is effectively now, so if interval=1mo it's not actually overdue yet
        # But we need to verify it's included in the overdue bucket when created_at is old.
        # Reset created_at to be older than the interval.
        Asset.objects.filter(pk=never_audited.pk).update(created_at=self.now - timedelta(days=45))
        never_audited.refresh_from_db()
        # With no last_audited, audit_due_date uses created_at.
        # created_at is 45 days ago, interval is 30 days → overdue.
        results = self._filter("true")
        self.assertIn(never_audited, results)

    def test_no_filter_value_returns_all(self):
        a1 = self._make_asset(self.at_interval, last_audited=self.now - timedelta(days=40))
        a2 = self._make_asset(self.at_no_interval)
        results = self._filter("")
        # Empty string → no filter applied
        self.assertIn(a1, results)
        self.assertIn(a2, results)


class AlertRuleCadenceTests(TestCase):
    """_match_audit_overdue in core/tasks/alerts.py respects per-category cadence."""

    def setUp(self):
        self.today = timezone.now().date()
        self.now = timezone.now()
        self.status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)

    def _make_asset(self, interval_months, last_audited_days_ago):
        cat = _make_category(interval_months=interval_months)
        at = _make_asset_type(cat)
        last_audited = (self.now - timedelta(days=last_audited_days_ago)) if last_audited_days_ago is not None else None
        return baker.make(Asset, asset_type=at, status=self.status, last_audited=last_audited)

    def test_category_cadence_overrides_rule_threshold(self):
        """Asset is within rule threshold but overdue per its category cadence."""
        from extras.models import AlertRule
        from extras.tasks.alerts import _match_audit_overdue

        # Global rule: 180-day threshold
        rule = baker.make(
            AlertRule, alert_type=AlertRule.ALERT_TYPE_AUDIT_OVERDUE, threshold_value=180, tenant=None, is_active=True
        )

        # Asset: 45 days since last audit, category says every 30 days → overdue
        asset_overdue_by_cat = self._make_asset(interval_months=1, last_audited_days_ago=45)
        # Asset: 45 days since last audit, no category interval → NOT overdue per 180-day rule
        no_cat_asset = self._make_asset(interval_months=None, last_audited_days_ago=45)

        results = _match_audit_overdue(rule, self.today)
        result_ids = {m["obj"].pk for m in results}

        self.assertIn(asset_overdue_by_cat.pk, result_ids)
        self.assertNotIn(no_cat_asset.pk, result_ids)

    def test_category_cadence_suppresses_false_positive(self):
        """Asset is beyond rule threshold but NOT overdue per its category cadence."""
        from extras.models import AlertRule
        from extras.tasks.alerts import _match_audit_overdue

        # Global rule: 30-day threshold
        rule = baker.make(
            AlertRule, alert_type=AlertRule.ALERT_TYPE_AUDIT_OVERDUE, threshold_value=30, tenant=None, is_active=True
        )

        # Asset: 45 days since last audit, but category says every 90 days → still fine
        asset_ok = self._make_asset(interval_months=3, last_audited_days_ago=45)

        results = _match_audit_overdue(rule, self.today)
        result_ids = {m["obj"].pk for m in results}

        self.assertNotIn(asset_ok.pk, result_ids)
