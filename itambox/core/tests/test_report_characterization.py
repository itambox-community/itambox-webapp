"""Characterization matrix for the public report catalogue.

This test deliberately exercises the pre-provider compiler contract for every
public report identifier.  The expected defaults are the compatibility
baseline for provider extraction: identifiers, default columns, summary-card
labels, fallback rows, grouping, and chart output must not drift.
"""

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase
from django.utils import translation

from assets.models import Manufacturer, Supplier
from core.management.commands._seed.organizations import _seed_saas_suppliers
from core.reports import get_report_provider
from core.reports.formatting import _format_per_currency
from core.tests.mixins import TenantTestMixin, compile_report_with_system_authorization
from extras.models import ReportTemplate
from licenses.models import License, LicenseTypeChoices
from organization.models import TenantGroup
from procurement.reports import _annual_cost, _annual_spend
from procurement.reports import _spend_chart as _contract_spend_chart
from software.models import Software
from subscriptions.models import Subscription
from subscriptions.reports import _monthly_cost, _monthly_spend
from subscriptions.reports import _spend_chart as _subscription_spend_chart

REPORT_CHARACTERIZATIONS = {
    ReportTemplate.REPORT_TYPE_ASSET_SUMMARY: {
        "columns": ["asset_tag", "name", "status", "location", "assigned_to"],
        "headers": ["Asset Tag", "Asset Name", "Status Label", "Location", "Asset Holder"],
        "summary": ["Total Hardware Assets", "Total Acquisition Sum"],
        "summary_values": ["1 (Mock)", "$3,499.00"],
    },
    ReportTemplate.REPORT_TYPE_LICENSE_UTILIZATION: {
        "columns": [
            "license_name",
            "software",
            "seats",
            "assigned_seats",
            "available_seats",
            "utilization_rate",
        ],
        "headers": ["License Name", "Software", "Total Seats", "Assigned Seats", "Available Seats", "Utilization Rate"],
        "summary": ["Total License Products"],
        "summary_values": ["1 (Mock)"],
    },
    ReportTemplate.REPORT_TYPE_SUBSCRIPTION_RENEWALS: {
        "columns": [
            "subscription_name",
            "supplier",
            "agreement_entitled_quantity",
            "billing_cycle",
            "cost",
            "end_date",
        ],
        "headers": [
            "Subscription Name",
            "Supplier",
            "Agreement Entitled Quantity",
            "Billing Cycle",
            "Cost",
            "End Date",
        ],
        "summary": ["Active Subscriptions", "Est. Monthly Spend"],
        "summary_values": ["1 (Mock)", "$1,200.00"],
    },
    ReportTemplate.REPORT_TYPE_ASSET_MAINTENANCE: {
        "columns": ["maintenance_asset", "maintenance_type", "maintenance_status", "maintenance_cost"],
        "headers": ["Asset", "Type", "Status", "Cost"],
        "summary": ["Total Maintenances", "Total Maintenance Cost"],
        "summary_values": ["1 (Mock)", "$250.00"],
    },
    ReportTemplate.REPORT_TYPE_ASSET_DEPRECIATION: {
        "columns": [
            "asset_tag",
            "name",
            "purchase_cost",
            "salvage_value",
            "depreciation_months",
            "current_value",
        ],
        "headers": [
            "Asset Tag",
            "Asset Name",
            "Purchase Cost",
            "Salvage Value",
            "Depreciation Lifespan (Months)",
            "Depreciated Value",
        ],
        "summary": ["Total Depreciable Assets", "Total Acquisition Cost", "Total Current Book Value"],
        "summary_values": ["1 (Mock)", "$2,500.00", "$1,450.00"],
    },
    ReportTemplate.REPORT_TYPE_SOFTWARE_INVENTORY: {
        "columns": [
            "software_name",
            "manufacturer",
            "version",
            "category",
            "license_type",
            "installed_count",
            "license_count",
        ],
        "headers": [
            "Software Product",
            "Manufacturer",
            "Version",
            "Category",
            "License Type",
            "Installed Count",
            "License Count",
        ],
        "summary": ["Total Software Products"],
        "summary_values": ["1 (Mock)"],
    },
    ReportTemplate.REPORT_TYPE_CONTRACT_RENEWALS: {
        "columns": [
            "contract_number",
            "contract_name",
            "contract_type",
            "contract_status",
            "contract_supplier",
            "contract_end_date",
            "contract_days_until_expiry",
            "contract_cost",
        ],
        "headers": [
            "Contract #",
            "Contract Name",
            "Contract Type",
            "Contract Status",
            "Supplier",
            "End Date",
            "Days Until Expiry",
            "Contract Cost",
        ],
        "summary": ["Active Contracts", "Expiring Within 30 Days", "Est. Annual Spend"],
        "summary_values": ["1 (Mock)", "0 (Mock)", "currency"],
    },
    ReportTemplate.REPORT_TYPE_WARRANTY_EXPIRATION: {
        "columns": [
            "warranty_asset",
            "warranty_type",
            "warranty_supplier",
            "warranty_end_date",
            "warranty_days_remaining",
            "warranty_status",
        ],
        "headers": ["Asset", "Warranty Type", "Supplier", "End Date", "Days Remaining", "Status"],
        "summary": ["Total Warranties", "Expiring Within 30 Days", "Already Expired", "Total Warranty Cost"],
        "summary_values": ["1 (Mock)", "0 (Mock)", "0 (Mock)", "€299.00"],
    },
    ReportTemplate.REPORT_TYPE_ASSET_DISPOSAL_EOL: {
        "columns": [
            "disposal_asset",
            "disposal_date",
            "disposal_method",
            "disposal_status",
            "disposal_sanitization_method",
            "disposal_weee_compliant",
            "disposal_proceeds",
        ],
        "headers": [
            "Asset",
            "Disposal Date",
            "Disposal Method",
            "Disposal Status",
            "Data Sanitization Method",
            "WEEE Compliant",
            "Proceeds",
        ],
        "summary": ["Active Disposals", "Active WEEE-compliant disposals", "Proceeds from active disposals"],
        "summary_values": ["1 (Mock)", "1 (Mock)", "150,00\u00a0€"],
    },
    ReportTemplate.REPORT_TYPE_HARDWARE_INVENTORY: {
        "columns": [
            "hw_item_type",
            "hw_name",
            "hw_manufacturer",
            "hw_category",
            "hw_total_stock",
            "hw_available",
            "hw_status",
        ],
        "headers": ["Item Type", "Name", "Manufacturer", "Category", "Total Stock", "Available", "Stock Status"],
        "summary": ["Accessory SKUs", "Consumable SKUs", "Component SKUs", "Items at Zero Stock"],
        "summary_values": ["1 (Mock)", "0", "0", "0"],
    },
    ReportTemplate.REPORT_TYPE_CUSTODY_COMPLIANCE: {
        "columns": [
            "custody_asset",
            "custody_holder",
            "custody_status",
            "custody_accepted_date",
            "custody_eula_version",
            "custody_signature_provider",
        ],
        "headers": ["Asset", "Holder", "Acceptance Status", "Accepted Date", "EULA Version", "Signature Provider"],
        "summary": ["Total Receipts", "Pending Sign-offs", "Acceptance Rate"],
        "summary_values": ["1 (Mock)", "0 (Mock)", "100.0% (Mock)"],
    },
}


class ReportSpendNormalizationUnitTests(unittest.TestCase):
    def test_subscription_terms_missing_values_zero_and_currency_buckets(self):
        supplier = Supplier(name="Term Vendor", slug="term-vendor")
        subscriptions = [
            Subscription(
                name=str(term),
                supplier=supplier,
                renewal_cost=Decimal(str(cost)),
                currency=currency,
                billing_cycle="multi_year",
                term_months=term,
            )
            for term, cost, currency in (
                (24, 2400, "EUR"),
                (36, 3600, "USD"),
                (48, 4800, "EUR"),
                (None, 2400, "USD"),
                (24, 0, "GBP"),
            )
        ]
        self.assertEqual(
            [_monthly_cost(subscription) for subscription in subscriptions],
            [100.0, 100.0, 100.0, None, 0.0],
        )
        self.assertEqual(_monthly_cost(SimpleNamespace(renewal_cost=1200, billing_cycle="annual")), 100.0)
        self.assertEqual(_monthly_cost(SimpleNamespace(renewal_cost=1200, billing_cycle="onetime")), 0.0)

        request = SimpleNamespace(active_tenant=None)
        by_currency, by_supplier = _monthly_spend(subscriptions, request)
        self.assertEqual(by_currency, {"EUR": 200.0, "USD": 100.0, "GBP": 0.0})
        self.assertEqual(
            by_supplier,
            {
                ("Term Vendor", "EUR"): 200.0,
                ("Term Vendor", "USD"): 100.0,
                ("Term Vendor", "GBP"): 0.0,
            },
        )
        summary = _format_per_currency(by_currency)
        chart = _subscription_spend_chart(by_supplier, request)
        self.assertIn("200.00", summary)
        self.assertIn("€", summary)
        self.assertIn("$100.00", summary)
        self.assertIn("£0.00", summary)
        self.assertIn("Term Vendor (EUR)", chart)
        self.assertIn("Term Vendor (USD)", chart)
        self.assertIn("200.00", chart)
        self.assertIn("$100.00", chart)

    def test_contract_terms_are_derived_from_calendar_dates(self):
        supplier = Supplier(name="Vendor", slug="vendor")
        contracts = [
            SimpleNamespace(
                cost=cost,
                currency=currency,
                billing_cycle="multi_year",
                start_date=date(2020, 1, 1),
                end_date=date(2020 + years, 1, 1),
                supplier=supplier,
            )
            for years, cost, currency in ((2, 2400, "EUR"), (3, 3600, "USD"), (4, 4800, "GBP"))
        ]
        unaligned_dates = SimpleNamespace(
            cost=2400,
            currency="USD",
            billing_cycle="multi_year",
            start_date=date(2020, 1, 1),
            end_date=date(2023, 1, 2),
            supplier=supplier,
        )
        zero_cost = SimpleNamespace(
            cost=0,
            currency="JPY",
            billing_cycle="multi_year",
            start_date=date(2020, 1, 1),
            end_date=date(2022, 1, 1),
            supplier=supplier,
        )
        month_end_dates = SimpleNamespace(
            cost=2500,
            currency="CAD",
            billing_cycle="multi_year",
            start_date=date(2020, 1, 31),
            end_date=date(2022, 2, 28),
            supplier=supplier,
        )
        missing_dates = SimpleNamespace(
            cost=2400,
            currency="CAD",
            billing_cycle="multi_year",
            start_date=None,
            end_date=date(2022, 1, 1),
            supplier=supplier,
        )
        self.assertEqual([_annual_cost(contract) for contract in contracts], [1200.0, 1200.0, 1200.0])
        self.assertIsNone(_annual_cost(unaligned_dates))
        self.assertEqual(_annual_cost(zero_cost), 0.0)
        self.assertEqual(_annual_cost(month_end_dates), 1200.0)
        self.assertIsNone(_annual_cost(missing_dates))

        request = SimpleNamespace(active_tenant=None)
        by_currency, by_supplier = _annual_spend(
            [*contracts, unaligned_dates, zero_cost, month_end_dates, missing_dates], request
        )
        self.assertEqual(by_currency, {"EUR": 1200.0, "USD": 1200.0, "GBP": 1200.0, "JPY": 0.0, "CAD": 1200.0})
        self.assertEqual(by_supplier[("Vendor", "JPY")], 0.0)
        summary = _format_per_currency(by_currency)
        chart = _contract_spend_chart(by_supplier, request)
        self.assertIn("€", summary)
        self.assertIn("$1,200.00", summary)
        self.assertIn("£1,200.00", summary)
        self.assertIn("Vendor (EUR)", chart)
        self.assertIn("Vendor (USD)", chart)
        self.assertIn("Vendor (GBP)", chart)


class ReportCompilerCharacterizationTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Report Characterization Tenant", slug="report-characterization")
        self.clear_tenant_context()

    def test_seed_saas_supplier_scopes_and_commercial_fields(self):
        group = TenantGroup.objects.create(name="Report Supplier Group", slug="report-supplier-group")
        supplier_data = [
            ("Amazon Web Services", "aws-org", "https://console.aws.amazon.com"),
            ("Google Cloud Platform", "gcp-org", "https://console.cloud.google.com"),
            ("Cloudflare", "cloudflare", "https://dash.cloudflare.com"),
        ]

        suppliers = _seed_saas_suppliers(Supplier, supplier_data, self.tenant, group)
        repeated_suppliers = _seed_saas_suppliers(Supplier, supplier_data, self.tenant, group)

        self.assertEqual(suppliers["Amazon Web Services"].tenant_id, None)
        self.assertEqual(suppliers["Google Cloud Platform"].tenant, self.tenant)
        self.assertEqual(suppliers["Cloudflare"].tenant_group, group)
        self.assertEqual(suppliers["Cloudflare"].account_id, "cloudflare")
        self.assertEqual(suppliers["Cloudflare"].portal_url, "https://dash.cloudflare.com")
        self.assertEqual(
            {name: supplier.pk for name, supplier in suppliers.items()},
            {name: supplier.pk for name, supplier in repeated_suppliers.items()},
        )

    def test_every_public_report_identifier_preserves_compiler_output_contract(self):
        self.assertEqual(
            set(REPORT_CHARACTERIZATIONS),
            {identifier for identifier, _label in ReportTemplate.REPORT_TYPE_CHOICES},
        )

        for report_type, expected in REPORT_CHARACTERIZATIONS.items():
            with self.subTest(report_type=report_type):
                template = ReportTemplate(
                    name=f"Characterization {report_type}",
                    report_type=report_type,
                    included_columns=[],
                    include_summary_cards=True,
                    include_distribution_chart=True,
                )
                with translation.override("en"):
                    headers, rows, summary_cards, grouped_data, chart_svg, context_data = (
                        compile_report_with_system_authorization(template, active_tenant=self.tenant)
                    )

                self.assertEqual(headers, expected["headers"])
                self.assertEqual(list(rows[0]), expected["headers"] + ["_group_by"])
                self.assertEqual(
                    [card["label"] for card in summary_cards],
                    expected["summary"],
                )
                for card, expected_value in zip(summary_cards, expected["summary_values"], strict=False):
                    if expected_value == "currency":
                        self.assertIn("12", str(card["value"]))
                        self.assertIn("€", str(card["value"]))
                    else:
                        self.assertEqual(card["value"], expected_value)
                self.assertEqual(list(grouped_data), ["General"])
                self.assertEqual(grouped_data["General"], rows)
                self.assertIn("<svg", chart_svg)
                self.assertEqual(context_data["headers"], headers)
                self.assertEqual(context_data["grouped_data"], grouped_data)
                self.assertEqual(context_data["summary_cards"], summary_cards)

    def test_subscription_currency_summary_handles_real_rows(self):
        supplier = Supplier.objects.create(name="Characterization Supplier", tenant=self.tenant)
        Subscription.objects.create(
            name="Characterization Subscription",
            supplier=supplier,
            tenant=self.tenant,
            renewal_date=date.today(),
            renewal_cost=Decimal("120.00"),
            currency="EUR",
            billing_cycle="monthly",
        )
        template = ReportTemplate(
            name="Characterization subscription with data",
            report_type=ReportTemplate.REPORT_TYPE_SUBSCRIPTION_RENEWALS,
            included_columns=[],
            include_summary_cards=True,
            include_distribution_chart=True,
        )

        with self.tenant_context(self.tenant), translation.override("en"):
            _headers, _rows, summary_cards, _grouped, _chart, _context = compile_report_with_system_authorization(
                template, active_tenant=self.tenant
            )

        self.assertEqual(summary_cards[0]["value"], "1")
        self.assertIn("120", str(summary_cards[1]["value"]))

    def test_multi_year_subscription_spend_uses_recorded_term(self):
        supplier = Supplier.objects.create(name="Term Vendor", slug="term-vendor", tenant=self.tenant)
        subscriptions = [
            Subscription.objects.create(
                name="Two-year EUR",
                supplier=supplier,
                tenant=self.tenant,
                renewal_cost=Decimal("2400.00"),
                currency="EUR",
                billing_cycle="multi_year",
                term_months=24,
            ),
            Subscription.objects.create(
                name="Three-year USD",
                supplier=supplier,
                tenant=self.tenant,
                renewal_cost=Decimal("3600.00"),
                currency="USD",
                billing_cycle="multi_year",
                term_months=36,
            ),
            Subscription.objects.create(
                name="Four-year EUR",
                supplier=supplier,
                tenant=self.tenant,
                renewal_cost=Decimal("4800.00"),
                currency="EUR",
                billing_cycle="multi_year",
                term_months=48,
            ),
            Subscription.objects.create(
                name="Unknown-term USD",
                supplier=supplier,
                tenant=self.tenant,
                renewal_cost=Decimal("2400.00"),
                currency="USD",
                billing_cycle="multi_year",
            ),
            Subscription.objects.create(
                name="Free multi-year",
                supplier=supplier,
                tenant=self.tenant,
                renewal_cost=Decimal("0.00"),
                currency="GBP",
                billing_cycle="multi_year",
                term_months=24,
            ),
        ]
        self.assertEqual(
            [_monthly_cost(subscription) for subscription in subscriptions],
            [100.0, 100.0, 100.0, None, 0.0],
        )

        template = ReportTemplate(
            name="Term-aware subscription spend",
            report_type=ReportTemplate.REPORT_TYPE_SUBSCRIPTION_RENEWALS,
            included_columns=[],
            include_summary_cards=True,
            include_distribution_chart=True,
        )
        with self.tenant_context(self.tenant), translation.override("en"):
            _headers, _rows, summary_cards, _grouped, chart_svg, _context = compile_report_with_system_authorization(
                template, active_tenant=self.tenant
            )

        summary = summary_cards[1]["value"]
        self.assertIn("200.00", summary)
        self.assertIn("€", summary)
        self.assertIn("$100.00", summary)
        self.assertIn("£0.00", summary)
        self.assertNotIn("166.67", summary)
        self.assertIn("Term Vendor (EUR)", chart_svg)
        self.assertIn("Term Vendor (USD)", chart_svg)
        self.assertIn("200.00", chart_svg)
        self.assertIn("$100.00", chart_svg)
        self.assertNotIn("$166.67", chart_svg)

    def test_subscription_entitlement_reports_agreement_quantity_not_license_seats(self):
        """A 120 agreement entitlement reports 120 even when linked licenses total 115 seats."""
        supplier = Supplier.objects.create(name="Entitlement Supplier", tenant=self.tenant)
        subscription = Subscription.objects.create(
            name="Entitlement Subscription",
            supplier=supplier,
            tenant=self.tenant,
            renewal_date=date.today(),
            renewal_cost=Decimal("120.00"),
            currency="EUR",
            billing_cycle="monthly",
            licensed_quantity=120,
        )
        manufacturer = Manufacturer.objects.create(name="Seat Vendor", slug="seat-vendor")
        software = Software.objects.create(name="Seat Suite", manufacturer=manufacturer, tenant=self.tenant)
        for license_name, seats in (("Seat block A", 100), ("Seat block B", 15)):
            License.objects.create(
                name=license_name,
                software=software,
                license_type=LicenseTypeChoices.SUBSCRIPTION_SEAT,
                seats=seats,
                tenant=self.tenant,
                subscription=subscription,
            )

        template = ReportTemplate(
            name="Entitlement characterization",
            report_type=ReportTemplate.REPORT_TYPE_SUBSCRIPTION_RENEWALS,
            included_columns=[],
            include_summary_cards=False,
            include_distribution_chart=False,
        )
        with self.tenant_context(self.tenant), translation.override("en"):
            headers, rows, *_ = compile_report_with_system_authorization(template, active_tenant=self.tenant)

        self.assertEqual(subscription.total_seats, 115, "Fixture: linked licenses must total 115 seats")
        self.assertEqual(
            headers,
            ["Subscription Name", "Supplier", "Agreement Entitled Quantity", "Billing Cycle", "Cost", "End Date"],
        )
        self.assertEqual(rows[0]["Agreement Entitled Quantity"], "120")

        seat_labels = {"Total Seats", "Assigned Seats", "Available Seats"}
        self.assertFalse(seat_labels & set(rows[0]), "The subscription report must not expose license-seat columns")
        provider_cells = get_report_provider(ReportTemplate.REPORT_TYPE_SUBSCRIPTION_RENEWALS).cells
        self.assertIn("agreement_entitled_quantity", provider_cells)
        self.assertEqual(set(provider_cells) & {"seats", "assigned_seats", "available_seats"}, set())

    def test_subscription_entitlement_renders_not_set_and_exact_zero(self):
        """Entitlement None renders 'Not set' and a set 0 renders '0', never the seat total."""
        supplier = Supplier.objects.create(name="Fallback Supplier", tenant=self.tenant)
        unset_subscription = Subscription.objects.create(
            name="Entitlement Unset",
            supplier=supplier,
            tenant=self.tenant,
            renewal_date=date.today(),
            renewal_cost=Decimal("10.00"),
            currency="EUR",
            billing_cycle="monthly",
        )
        zero_subscription = Subscription.objects.create(
            name="Entitlement Zero",
            supplier=supplier,
            tenant=self.tenant,
            renewal_date=date.today(),
            renewal_cost=Decimal("10.00"),
            currency="EUR",
            billing_cycle="monthly",
            licensed_quantity=0,
        )
        manufacturer = Manufacturer.objects.create(name="Fallback Vendor", slug="fallback-vendor")
        software = Software.objects.create(name="Fallback Suite", manufacturer=manufacturer, tenant=self.tenant)
        for subscription, label in ((unset_subscription, "Unset"), (zero_subscription, "Zero")):
            for block, seats in (("A", 100), ("B", 15)):
                License.objects.create(
                    name=f"{label} seat block {block}",
                    software=software,
                    license_type=LicenseTypeChoices.SUBSCRIPTION_SEAT,
                    seats=seats,
                    tenant=self.tenant,
                    subscription=subscription,
                )

        template = ReportTemplate(
            name="Entitlement fallback characterization",
            report_type=ReportTemplate.REPORT_TYPE_SUBSCRIPTION_RENEWALS,
            included_columns=[],
            include_summary_cards=False,
            include_distribution_chart=False,
        )
        with self.tenant_context(self.tenant), translation.override("en"):
            _headers, rows, *_ = compile_report_with_system_authorization(template, active_tenant=self.tenant)

        entitlement_by_name = {row["Subscription Name"]: row["Agreement Entitled Quantity"] for row in rows}
        self.assertEqual(unset_subscription.total_seats, 115, "Fixture: linked licenses total 115 seats")
        self.assertEqual(zero_subscription.total_seats, 115, "Fixture: linked licenses total 115 seats")
        self.assertEqual(entitlement_by_name["Entitlement Unset"], "Not set")
        self.assertEqual(entitlement_by_name["Entitlement Zero"], "0")
