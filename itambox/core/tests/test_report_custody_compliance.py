"""Tests for the Custody & EULA Sign-off Compliance report type."""

from django.test import TestCase
from model_bakery import baker

from assets.models import Asset, StatusLabel
from compliance.models import CustodyReceipt
from core.tests.mixins import TenantTestMixin, compile_report_with_system_authorization
from extras.models import ReportTemplate
from organization.models import AssetHolder, Tenant


def _summary(cards, label):
    return next(c["value"] for c in cards if c["label"] == label)


class CustodyComplianceReportTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Custody Tenant", slug="custody-tenant")
        self.tenant_b = Tenant.objects.create(name="Other Tenant", slug="custody-other")
        self.set_active_tenant(self.tenant)

        self.status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.asset = baker.make(Asset, tenant=self.tenant, asset_tag="CST-001", status=self.status)
        self.asset_b = baker.make(Asset, tenant=self.tenant_b, asset_tag="CST-B01", status=self.status)
        self.holder = baker.make(AssetHolder, tenant=self.tenant)

        # One accepted receipt for tenant A
        self.accepted_receipt = CustodyReceipt.objects.create(
            asset=self.asset,
            holder=self.holder,
            acceptance_status=CustodyReceipt.STATUS_ACCEPTED,
            eula_version="2.0",
            signature_provider="local",
            qms_reference="QMS-001",
        )
        # One pending receipt for tenant A
        self.pending_receipt = CustodyReceipt.objects.create(
            asset=self.asset,
            holder=self.holder,
            acceptance_status=CustodyReceipt.STATUS_PENDING,
            eula_version="2.0",
            signature_provider="local",
        )
        # One receipt for tenant B — must NOT appear in tenant A report
        self.receipt_b = CustodyReceipt.objects.create(
            asset=self.asset_b,
            holder=baker.make(AssetHolder, tenant=self.tenant_b),
            acceptance_status=CustodyReceipt.STATUS_ACCEPTED,
            eula_version="1.0",
            signature_provider="docusign",
        )

        self.template = ReportTemplate.objects.create(
            name="Custody Compliance Test",
            report_type=ReportTemplate.REPORT_TYPE_CUSTODY_COMPLIANCE,
            included_columns=[
                "custody_asset",
                "custody_holder",
                "custody_status",
                "custody_accepted_date",
                "custody_eula_version",
                "custody_signature_provider",
                "custody_qms_reference",
            ],
            include_summary_cards=True,
        )

    def test_row_content_and_tenant_isolation(self):
        """Rows are scoped to the active tenant; tenant B's receipt is excluded."""
        self.clear_tenant_context()
        _, rows, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)
        # Only 2 rows (the 2 tenant-A receipts), not 3
        self.assertEqual(len(rows), 2)

        accepted_rows = [r for r in rows if r.get("Acceptance Status") == "Accepted"]
        self.assertEqual(len(accepted_rows), 1)
        accepted_row = accepted_rows[0]
        self.assertEqual(accepted_row["EULA Version"], "2.0")
        self.assertEqual(accepted_row["Signature Provider"], "local")
        self.assertEqual(accepted_row["QMS Reference"], "QMS-001")
        # accepted_date is None on this receipt so should render as '-'
        self.assertEqual(accepted_row["Accepted Date"], "-")

        pending_rows = [r for r in rows if r.get("Acceptance Status") == "Pending"]
        self.assertEqual(len(pending_rows), 1)

    def test_summary_cards(self):
        """Summary cards show correct totals for the scoped tenant."""
        self.clear_tenant_context()
        _, _, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)
        self.assertEqual(_summary(summary_cards, "Total Receipts"), "2")
        self.assertEqual(_summary(summary_cards, "Pending Sign-offs"), "1")
        self.assertEqual(_summary(summary_cards, "Acceptance Rate"), "50.0%")

    def test_no_money_cell_starts_with_dollar(self):
        """Custody compliance has no monetary columns; no cell should start with '$'."""
        self.clear_tenant_context()
        _, rows, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=self.tenant)
        for row in rows:
            for key, val in row.items():
                if key == "_group_by":
                    continue
                self.assertFalse(
                    str(val).startswith("$"), msg="Cell '%s' = '%s' unexpectedly starts with '$'" % (key, val)
                )
        for card in summary_cards:
            self.assertFalse(
                str(card["value"]).startswith("$"),
                msg="Summary card '%s' = '%s' unexpectedly starts with '$'" % (card["label"], card["value"]),
            )

    def test_filter_tenants_scoping(self):
        """filter_tenants parameter correctly scopes to the specified tenants."""
        self.clear_tenant_context()
        _, rows, summary_cards, *_ = compile_report_with_system_authorization(
            self.template, filter_tenants=[self.tenant]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(_summary(summary_cards, "Total Receipts"), "2")

    def test_mock_fallback_when_no_receipts(self):
        """When the tenant has no receipts, the mock fallback row is returned."""
        empty_tenant = Tenant.objects.create(name="Empty Tenant", slug="custody-empty")
        self.clear_tenant_context()
        _, rows, summary_cards, *_ = compile_report_with_system_authorization(self.template, active_tenant=empty_tenant)
        self.assertEqual(len(rows), 1)
        # Mock row should not contain real receipt identifiers
        mock_status = rows[0].get("Acceptance Status")
        self.assertIsNotNone(mock_status)
        # Summary cards mock values contain '(Mock)'
        total_val = _summary(summary_cards, "Total Receipts")
        self.assertIn("Mock", total_val)
