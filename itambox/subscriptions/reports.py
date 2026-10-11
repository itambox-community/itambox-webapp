"""Subscription-domain report providers."""

from django.utils.translation import gettext as _

from core.managers import Scope
from core.reports.charts import generate_bar_chart
from core.reports.contracts import ReportDefinition, ReportRequest, ReportResult, record_window_state
from core.reports.formatting import _format_per_currency, _money, _record_currency
from core.reports.registry import register_report_provider
from core.reports.rows import DEFAULT_GROUP
from subscriptions.models import Subscription

#: Billing cycle -> how many of that cycle make up a month's worth of spend.
#: A one-time charge is not recurring, so it contributes nothing to the
#: estimated monthly figure; an unrecognised cycle is treated as monthly.
_MONTHLY_DIVISORS = {
    "monthly": 1.0,
    "quarterly": 3.0,
    "biannual": 6.0,
    "annual": 12.0,
}


def _monthly_cost(subscription):
    """The subscription's renewal cost amortized to a monthly equivalent."""
    if subscription.billing_cycle == "onetime":
        return 0.0
    if subscription.billing_cycle == "multi_year":
        annual_cost = subscription.annual_cost
        return float(annual_cost / 12) if annual_cost is not None else None
    return float(subscription.renewal_cost) / _MONTHLY_DIVISORS.get(subscription.billing_cycle, 1.0)


def _monthly_spend(queryset, request: ReportRequest):
    """Monthly spend bucketed by currency, and again by (supplier, currency).

    Subscriptions can carry differing ISO currencies and there is no FX source,
    so nothing here is ever summed across currencies: the cards render one
    figure per currency and the chart draws one bar per (supplier, currency).
    """
    by_currency = {}
    by_supplier = {}
    for subscription in queryset:
        if subscription.renewal_cost is None:
            continue
        monthly = _monthly_cost(subscription)
        if monthly is None:
            continue
        currency = _record_currency(subscription.currency, subscription.tenant)
        by_currency[currency] = by_currency.get(currency, 0.0) + monthly
        supplier_name = subscription.supplier.name if subscription.supplier else _("Generic")
        by_supplier[(supplier_name, currency)] = by_supplier.get((supplier_name, currency), 0.0) + monthly
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
    return generate_bar_chart(chart_data, title=_("Monthly Spend by Supplier"))


class SubscriptionRenewalsReportProvider(ReportDefinition):
    """Subscription renewals: active subscriptions and their monthly spend."""

    report_type = "subscription_renewals"
    permission = "subscriptions.view_subscription"
    default_columns = (
        "subscription_name",
        "supplier",
        "agreement_entitled_quantity",
        "billing_cycle",
        "cost",
        "end_date",
    )

    cells = {
        "subscription_name": lambda record, request: record.name or "-",
        "supplier": lambda record, request: record.supplier.name if record.supplier else "-",
        "agreement_entitled_quantity": lambda record, request: (
            str(record.licensed_quantity) if record.licensed_quantity is not None else _("Not set")
        ),
        "billing_cycle": lambda record, request: record.get_billing_cycle_display(),
        "cost": lambda record, request: _money(
            record.renewal_cost, getattr(record, "currency", None), request.active_tenant
        ),
        "end_date": lambda record, request: record.renewal_date.strftime("%Y-%m-%d") if record.renewal_date else "-",
    }

    sample_cells = {
        "subscription_name": "Office 365 E5",
        "supplier": "Microsoft",
        "agreement_entitled_quantity": "120",
        "billing_cycle": "Monthly",
        "cost": "$1,200.00",
        "end_date": "2026-12-31",
    }

    group_resolvers = {
        "supplier": lambda record, request: record.supplier.name if record.supplier else DEFAULT_GROUP,
    }

    sample_group_keys = {"supplier": "Microsoft"}

    def get_queryset(self, request: ReportRequest):
        # 'tenant' is select_related because the currency of a blank-currency
        # subscription is resolved from its own tenant — without it that is N+1.
        queryset = (
            Subscription.objects.for_scope(Scope.current())
            .filter(deleted_at__isnull=True, status="active")
            .select_related("supplier", "tenant")
        )
        return self.scope_to_tenants(queryset, request)

    def build_rows(self, records, request: ReportRequest):
        return [self.row_for(record, request) for record in records]

    def _summary_from_spend(self, queryset, by_currency):
        return [
            {"label": _("Active Subscriptions"), "value": str(queryset.count())},
            {"label": _("Est. Monthly Spend"), "value": _format_per_currency(by_currency)},
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
            spend = _monthly_spend(queryset, request)
        return ReportResult(
            rows=rows,
            summary_cards=self._summary_from_spend(queryset, spend[0])
            if request.template.include_summary_cards
            else [],
            chart_svg=self._chart_from_spend(spend[1], request) if request.template.include_distribution_chart else "",
            truncated=truncated,
            total_rows=total_rows,
        )

    def build_summary(self, queryset, request: ReportRequest):
        if not request.template.include_summary_cards:
            return []
        by_currency, _by_supplier = _monthly_spend(queryset, request)
        return self._summary_from_spend(queryset, by_currency)

    def build_chart(self, queryset, records, request: ReportRequest):
        if not request.template.include_distribution_chart:
            return ""
        _by_currency, by_supplier = _monthly_spend(queryset, request)
        return self._chart_from_spend(by_supplier, request)

    def build_sample_summary(self, request: ReportRequest):
        if not request.template.include_summary_cards:
            return []
        return [
            {"label": _("Active Subscriptions"), "value": "1 (Mock)"},
            {"label": _("Est. Monthly Spend"), "value": "$1,200.00"},
        ]

    def build_sample_chart(self, request: ReportRequest):
        if not request.template.include_distribution_chart:
            return ""
        sample_currency = _record_currency(None, None)
        return _spend_chart({("Microsoft", sample_currency): 1200.0}, request)


register_report_provider(SubscriptionRenewalsReportProvider())
