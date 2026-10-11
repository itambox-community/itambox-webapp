"""Tests for the contract_renewals report type.

Mirrors the conventions in core/tests/test_report_tenant_scoping.py:
  - TenantTestMixin for tenant setup
  - model_bakery for object creation
  - build_report_context called with explicit active_tenant (ambient context cleared)
  - asserts on row content, summary card values, and that a non-USD money cell
    does NOT start with '$'
"""

import datetime

from django.test import TestCase
from django.utils import timezone, translation

from assets.models.catalog import Supplier
from core.tests.mixins import TenantTestMixin, compile_report_with_system_authorization
from extras.models import ReportTemplate
from organization.models import Tenant
from procurement.models import Contract, ContractBillingCycleChoices, ContractStatusChoices, ContractTypeChoices


class ContractRenewalsReportTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="ContractTenant", slug="contract-tenant")
        self.set_active_tenant(self.tenant)

        # A second tenant whose contracts must NOT appear in results.
        self.other_tenant = Tenant.objects.create(name="OtherTenant", slug="contract-other-tenant")

        today = timezone.now().date()

        # Supplier is a global catalogue model (no tenant FK).
        self.supplier = Supplier.objects.create(name="Acme Support", slug="acme-support")

        # Active contract with EUR cost — verifies non-'$' money cell.
        self.contract_active = Contract.objects.create(
            tenant=self.tenant,
            name="HW Support Agreement",
            contract_number="CTR-001",
            contract_type=ContractTypeChoices.SUPPORT,
            status=ContractStatusChoices.ACTIVE,
            supplier=self.supplier,
            cost=12000,
            currency="EUR",
            billing_cycle=ContractBillingCycleChoices.ANNUAL,
            start_date=today - datetime.timedelta(days=30),
            end_date=today + datetime.timedelta(days=200),
            auto_renew=True,
        )

        # Active contract expiring within 30 days (exercises summary card).
        self.contract_expiring = Contract.objects.create(
            tenant=self.tenant,
            name="Short Lease",
            contract_number="CTR-002",
            contract_type=ContractTypeChoices.LEASE,
            status=ContractStatusChoices.ACTIVE,
            cost=500,
            currency="EUR",
            billing_cycle=ContractBillingCycleChoices.MONTHLY,
            start_date=today - datetime.timedelta(days=300),
            end_date=today + datetime.timedelta(days=10),
            auto_renew=False,
        )

        # Contract belonging to other_tenant — must be excluded.
        Contract.objects.create(
            tenant=self.other_tenant,
            name="Other Tenant Contract",
            contract_number="CTR-OTHER-001",
            contract_type=ContractTypeChoices.SERVICE,
            status=ContractStatusChoices.ACTIVE,
            cost=9999,
            currency="USD",
            billing_cycle=ContractBillingCycleChoices.ANNUAL,
            start_date=today - datetime.timedelta(days=10),
            end_date=today + datetime.timedelta(days=365),
            auto_renew=False,
        )

        self.template = ReportTemplate.objects.create(
            name="Contract Renewals Test",
            report_type=ReportTemplate.REPORT_TYPE_CONTRACT_RENEWALS,
            included_columns=[
                "contract_number",
                "contract_name",
                "contract_type",
                "contract_status",
                "contract_supplier",
                "contract_end_date",
                "contract_days_until_expiry",
                "contract_cost",
                "contract_billing_cycle",
                "contract_auto_renew",
            ],
            include_summary_cards=True,
            include_distribution_chart=True,
        )

    def test_row_content_and_tenant_isolation(self):
        """Rows contain only the active tenant's contracts; row fields match model data."""
        self.clear_tenant_context()
        _, rows, summary_cards, grouped_data, chart_svg, context_data = compile_report_with_system_authorization(
            self.template, active_tenant=self.tenant
        )

        contract_numbers = [r.get("Contract #") for r in rows]
        self.assertIn("CTR-001", contract_numbers)
        self.assertIn("CTR-002", contract_numbers)
        # Other tenant's contract must NOT appear.
        self.assertNotIn("CTR-OTHER-001", contract_numbers)

        # Verify row fields for the main active contract.
        row_001 = next(r for r in rows if r.get("Contract #") == "CTR-001")
        self.assertEqual(row_001.get("Contract Name"), "HW Support Agreement")
        self.assertEqual(row_001.get("Contract Status"), "Active")
        self.assertEqual(row_001.get("Billing Cycle"), "Annual")
        self.assertEqual(row_001.get("Auto-Renew"), "Yes")
        self.assertEqual(row_001.get("Supplier"), "Acme Support")

    def test_multi_year_contract_spend_uses_start_and_end_dates(self):
        contract_terms = (
            ("CTR-MY-24", 2400, "EUR", datetime.date(2020, 1, 1), datetime.date(2022, 1, 1)),
            ("CTR-MY-36", 3600, "USD", datetime.date(2020, 1, 1), datetime.date(2023, 1, 1)),
            ("CTR-MY-48", 4800, "GBP", datetime.date(2020, 1, 1), datetime.date(2024, 1, 1)),
        )
        for number, cost, currency, start_date, end_date in contract_terms:
            Contract.objects.create(
                tenant=self.tenant,
                name=number,
                contract_number=number,
                contract_type=ContractTypeChoices.SUPPORT,
                status=ContractStatusChoices.ACTIVE,
                supplier=self.supplier,
                cost=cost,
                currency=currency,
                billing_cycle=ContractBillingCycleChoices.MULTI_YEAR,
                start_date=start_date,
                end_date=end_date,
            )
        Contract.objects.create(
            tenant=self.tenant,
            name="Unaligned contract dates",
            contract_number="CTR-MY-UNKNOWN",
            contract_type=ContractTypeChoices.SUPPORT,
            status=ContractStatusChoices.ACTIVE,
            supplier=self.supplier,
            cost=2400,
            currency="USD",
            billing_cycle=ContractBillingCycleChoices.MULTI_YEAR,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2023, 1, 2),
        )

        self.clear_tenant_context()
        with self.tenant_context(self.tenant), translation.override("en"):
            _, _, summary_cards, _, chart_svg, _ = compile_report_with_system_authorization(
                self.template, active_tenant=self.tenant
            )

        summary = {card["label"]: card["value"] for card in summary_cards}["Est. Annual Spend"]
        self.assertIn("19,200.00", summary)
        self.assertIn("€", summary)
        self.assertIn("$1,200.00", summary)
        self.assertIn("£1,200.00", summary)
        self.assertIn("Acme Support (EUR)", chart_svg)
        self.assertIn("Acme Support (USD)", chart_svg)
        self.assertIn("Acme Support (GBP)", chart_svg)
        self.assertIn("13,200.00", chart_svg)
        self.assertIn("$1,200.00", chart_svg)
        self.assertIn("£1,200.00", chart_svg)

    def test_money_cell_not_dollar_prefixed_for_eur(self):
        """A EUR contract cost must NOT start with '$'."""
        self.clear_tenant_context()
        _, rows, *_ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)

        row_001 = next(r for r in rows if r.get("Contract #") == "CTR-001")
        cost_value = row_001.get("Contract Cost", "")
        self.assertNotEqual(cost_value, "-", "Cost should be rendered, not '-'")
        self.assertFalse(
            cost_value.startswith("$"), f"EUR contract cost should not start with '$', got: {cost_value!r}"
        )

    def test_summary_cards_active_and_expiring_counts(self):
        """Summary cards reflect total active contracts and the expiring-soon count."""
        self.clear_tenant_context()
        _, _, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)

        labels = {c["label"]: c["value"] for c in summary_cards}
        self.assertIn("Active Contracts", labels)
        self.assertIn("Expiring Within 30 Days", labels)
        self.assertIn("Est. Annual Spend", labels)

        # Both contracts are active.
        self.assertEqual(labels["Active Contracts"], "2")
        # Only CTR-002 (end_date = today+10) is expiring within 30 days.
        self.assertEqual(labels["Expiring Within 30 Days"], "1")

    def test_annual_spend_card_not_dollar_for_eur(self):
        """The Est. Annual Spend summary card must not start with '$' for EUR-only data."""
        self.clear_tenant_context()
        _, _, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)
        labels = {c["label"]: c["value"] for c in summary_cards}
        spend_value = labels.get("Est. Annual Spend", "")
        self.assertNotEqual(spend_value, "", "Annual spend card must have a value")
        self.assertFalse(
            spend_value.startswith("$"), f"EUR annual spend should not start with '$', got: {spend_value!r}"
        )

    def test_chart_svg_generated(self):
        """A distribution chart is produced when include_distribution_chart is True."""
        self.clear_tenant_context()
        _, _, _, _, chart_svg, _ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)
        self.assertTrue(chart_svg and len(chart_svg) > 0, "Expected a non-empty chart SVG string")

    def test_mock_fallback_when_no_contracts(self):
        """When no contracts match the tenant the mock fallback row is returned."""
        empty_tenant = Tenant.objects.create(name="EmptyTenant", slug="contract-empty")
        _, rows, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=empty_tenant)
        self.assertEqual(len(rows), 1)
        # Mock row sentinel value.
        self.assertEqual(rows[0].get("Contract #"), "CTR-MOCK-001")
        if self.template.include_summary_cards:
            labels = {c["label"]: c["value"] for c in summary_cards}
            self.assertIn("(Mock)", labels.get("Active Contracts", ""))
