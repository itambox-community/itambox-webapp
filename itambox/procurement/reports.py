"""Procurement-domain report providers."""

from calendar import monthrange
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

from core.managers import Scope
from core.reports.charts import generate_bar_chart
from core.reports.contracts import ReportDefinition, ReportRequest, ReportResult, record_window_state
from core.reports.formatting import _format_per_currency, _money, _record_currency
from core.reports.registry import register_report_provider
from procurement.models import Contract

#: A contract inside this many days of its end date reads as expiring soon.
CONTRACT_SOON_DAYS = 30


def _multi_year_term_months(contract):
    """Return whole calendar months when contract dates define the term exactly."""
    start_date = contract.start_date
    end_date = contract.end_date
    if not start_date or not end_date or end_date <= start_date:
        return None

    months = (end_date.year - start_date.year) * 12 + end_date.month - start_date.month
    start_is_month_end = start_date.day == monthrange(start_date.year, start_date.month)[1]
    end_is_month_end = end_date.day == monthrange(end_date.year, end_date.month)[1]
    if start_date.day != end_date.day and not (start_is_month_end and end_is_month_end):
        return None
    return months


def _annual_cost(contract):
    """The contract's cost amortized to a yearly equivalent."""
    cost = float(contract.cost)
    cycle = contract.billing_cycle or "annual"
    if cycle == "monthly":
        return cost * 12.0
    if cycle == "quarterly":
        return cost * 4.0
    if cycle == "biannual":
        return cost * 2.0
    if cycle == "multi_year":
        term_months = _multi_year_term_months(contract)
        return cost * 12.0 / term_months if term_months is not None else None
    # 'annual' already is the yearly figure, and a one-time charge has no
    # sensible yearly conversion — both are included as recorded.
    return cost


def _annual_spend(queryset, request: ReportRequest):
    """Annual spend bucketed by currency, and again by (supplier, currency).

    There is no FX source, so spend is never summed across currencies: the card
    renders one figure per currency and the chart draws one bar per pairing.
    """
    by_currency = {}
    by_supplier = {}
    for contract in queryset:
        if contract.cost is None:
            continue
        annual = _annual_cost(contract)
        if annual is None:
            continue
        currency = _record_currency(getattr(contract, "currency", None), request.active_tenant)
        by_currency[currency] = by_currency.get(currency, 0.0) + annual
        supplier_name = contract.supplier.name if contract.supplier else _("Generic")
        by_supplier[(supplier_name, currency)] = by_supplier.get((supplier_name, currency), 0.0) + annual
    return by_currency, by_supplier


def _spend_chart(by_supplier, request: ReportRequest):
    """One bar per (supplier, currency), ISO-qualified when currencies differ."""
    multi_currency = len({currency for _supplier, currency in by_supplier}) > 1
    chart_data = [
        {
            "label": f"{supplier_name} ({currency})" if multi_currency else supplier_name,
            "value": amount,
            "display": _money(amount, currency, request.active_tenant),
        }
        for (supplier_name, currency), amount in by_supplier.items()
    ]
    return generate_bar_chart(chart_data, title=_("Annual Spend by Supplier"))


class ContractRenewalsReportProvider(ReportDefinition):
    """Contract renewals: what expires when, and what it costs per year."""

    report_type = "contract_renewals"
    permission = "procurement.view_contract"
    default_columns = (
        "contract_number",
        "contract_name",
        "contract_type",
        "contract_status",
        "contract_supplier",
        "contract_end_date",
        "contract_days_until_expiry",
        "contract_cost",
    )

    cells = {
        "contract_number": lambda record, request: record.contract_number or "-",
        "contract_name": lambda record, request: record.name or "-",
        "contract_type": lambda record, request: record.get_contract_type_display(),
        "contract_status": lambda record, request: record.get_status_display(),
        "contract_supplier": lambda record, request: record.supplier.name if record.supplier else "-",
        "contract_start_date": lambda record, request: (
            record.start_date.strftime("%Y-%m-%d") if record.start_date else "-"
        ),
        "contract_end_date": lambda record, request: record.end_date.strftime("%Y-%m-%d") if record.end_date else "-",
        "contract_renewal_date": lambda record, request: (
            record.renewal_date.strftime("%Y-%m-%d") if record.renewal_date else "-"
        ),
        "contract_days_until_expiry": lambda record, request: (
            str((record.end_date - timezone.localdate(request.as_of)).days) if record.end_date else "-"
        ),
        "contract_cost": lambda record, request: _money(
            record.cost, getattr(record, "currency", None), request.active_tenant
        ),
        "contract_billing_cycle": lambda record, request: record.get_billing_cycle_display(),
        "contract_auto_renew": lambda record, request: _("Yes") if record.auto_renew else _("No"),
        "contract_covered_assets": lambda record, request: str(record.assets.count()),
        "contract_sla_response_time": lambda record, request: record.sla_response_time or "-",
        "contract_sla_resolution_time": lambda record, request: record.sla_resolution_time or "-",
        "contract_coverage_hours": lambda record, request: record.coverage_hours or "-",
    }

    sample_cells = {
        "contract_number": "CTR-MOCK-001",
        "contract_name": "Hardware Support Agreement (Mock)",
        "contract_type": "Support",
        "contract_status": "Active",
        "contract_supplier": "Acme Corp",
        "contract_start_date": "2026-01-01",
        "contract_end_date": "2026-12-31",
        "contract_renewal_date": "2026-11-30",
        "contract_days_until_expiry": "180",
        "contract_cost": "12,000.00 EUR",
        "contract_billing_cycle": "Annual",
        "contract_auto_renew": "Yes",
        "contract_covered_assets": "5",
        "contract_sla_response_time": "4 business hours",
        "contract_sla_resolution_time": "Next business day",
        "contract_coverage_hours": "24x7",
    }

    group_resolvers = {
        "contract_status": lambda record, request: record.get_status_display(),
        "contract_type": lambda record, request: record.get_contract_type_display(),
        "contract_supplier": lambda record, request: record.supplier.name if record.supplier else _("No Supplier"),
    }

    sample_group_keys = {
        "contract_status": "Active",
        "contract_type": "Support",
        "contract_supplier": "Acme Corp",
    }

    def get_queryset(self, request: ReportRequest):
        # select_related 'tenant' avoids an N+1 when a contract's own currency
        # field is blank and the tenant's currency is the fallback.
        queryset = (
            Contract.objects.for_scope(Scope.current())
            .filter(deleted_at__isnull=True)
            .select_related("supplier", "tenant")
            .prefetch_related("assets")
        )
        return self.scope_to_tenants(queryset, request)

    def build_rows(self, records, request: ReportRequest):
        return [self.row_for(record, request) for record in records]

    def _summary_from_spend(self, queryset, request: ReportRequest, by_currency):
        today = timezone.localdate(request.as_of)
        active_contracts = queryset.filter(status="active")
        expiring_soon = active_contracts.filter(
            end_date__gte=today, end_date__lte=today + timedelta(days=CONTRACT_SOON_DAYS)
        )
        return [
            {"label": _("Active Contracts"), "value": str(active_contracts.count())},
            {"label": _("Expiring Within 30 Days"), "value": str(expiring_soon.count())},
            {"label": _("Est. Annual Spend"), "value": _format_per_currency(by_currency)},
        ]

    def _chart_from_spend(self, by_supplier, request: ReportRequest):
        return _spend_chart(by_supplier, request)

    def build(self, request: ReportRequest) -> ReportResult:
        queryset = self.get_queryset(request)
        records = list(queryset[: self.row_limit])
        rows = list(self.build_rows(records, request))
        if not rows:
            return self.build_sample(request)
        truncated, total_rows = record_window_state(queryset, records, self.row_limit)
        spend = None
        if request.template.include_summary_cards or request.template.include_distribution_chart:
            spend = _annual_spend(queryset, request)
        return ReportResult(
            rows=rows,
            summary_cards=self._summary_from_spend(queryset, request, spend[0])
            if request.template.include_summary_cards
            else [],
            chart_svg=self._chart_from_spend(spend[1], request) if request.template.include_distribution_chart else "",
            truncated=truncated,
            total_rows=total_rows,
        )

    def build_summary(self, queryset, request: ReportRequest):
        if not request.template.include_summary_cards:
            return []
        by_currency, _by_supplier = _annual_spend(queryset, request)
        return self._summary_from_spend(queryset, request, by_currency)

    def build_chart(self, queryset, records, request: ReportRequest):
        if not request.template.include_distribution_chart:
            return ""
        _by_currency, by_supplier = _annual_spend(queryset, request)
        return self._chart_from_spend(by_supplier, request)

    def build_sample_summary(self, request: ReportRequest):
        if not request.template.include_summary_cards:
            return []
        return [
            {"label": _("Active Contracts"), "value": "1 (Mock)"},
            {"label": _("Expiring Within 30 Days"), "value": "0 (Mock)"},
            {"label": _("Est. Annual Spend"), "value": _format_per_currency({_record_currency(None, None): 12000.0})},
        ]

    def build_sample_chart(self, request: ReportRequest):
        if not request.template.include_distribution_chart:
            return ""
        return _spend_chart({("Acme Corp", "EUR"): 12000.0}, request)


register_report_provider(ContractRenewalsReportProvider())
