"""Inventory-domain report providers."""

from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from core.managers import Scope
from core.reports.charts import generate_doughnut_chart
from core.reports.contracts import ReportDefinition, ReportRequest, ReportResult
from core.reports.registry import register_report_provider
from core.reports.rows import group_key_for, sample_report_row
from inventory.models import Accessory, Component, Consumable


@dataclass(frozen=True)
class StockedItem:
    """One catalogue item, the item-type label it reports under, and its stock.

    The three stocked catalogues are separate tables with no shared concrete
    model, so an item cannot name its own type — the label travels beside it.
    ``total_stock`` and ``available`` are per-item aggregates rather than
    columns: reading each once here keeps a row, its stock status, and the
    zero-stock tally on one pair of reads per item instead of one per cell.
    """

    item: Any
    type_label: str
    total_stock: int
    available: int

    @classmethod
    def of(cls, item, type_label):
        return cls(item=item, type_label=type_label, total_stock=item.total_stock, available=item.available)


def _stock_status(entry, request):
    """How an item's stock reads against its own safety threshold."""
    if entry.total_stock <= 0:
        return _("Out of Stock")
    if entry.item.min_qty and entry.available <= entry.item.min_qty:
        return _("Low Stock")
    return _("In Stock")


class HardwareInventoryReportProvider(ReportDefinition):
    """Non-asset hardware stock: accessories, consumables, and components.

    The three catalogues are separate models reported as one list, so this
    provider assembles its own result: each catalogue is scoped and capped
    independently, and the SKU counts on the summary cards are the whole scope
    rather than the rendered window.
    """

    report_type = "hardware_inventory"
    permission = (
        "inventory.view_accessory",
        "inventory.view_consumable",
        "inventory.view_component",
    )
    #: All three catalogues are shared-catalogue models: a null-tenant row is a
    #: global item that belongs in every tenant's stock report.
    allow_global_tenant = True
    #: The window is per catalogue, not one first-N slice: the disclosed
    #: numbers must describe that shape.
    truncated_disclosure = gettext_noop(
        "Each inventory catalogue lists at most %(limit)s rows; %(total)s rows match across all catalogues."
    )
    default_columns = (
        "hw_item_type",
        "hw_name",
        "hw_manufacturer",
        "hw_category",
        "hw_total_stock",
        "hw_available",
        "hw_status",
    )

    cells = {
        "hw_item_type": lambda entry, request: entry.type_label,
        "hw_name": lambda entry, request: entry.item.name or "-",
        "hw_manufacturer": lambda entry, request: entry.item.manufacturer.name if entry.item.manufacturer else "-",
        "hw_category": lambda entry, request: entry.item.category.name if entry.item.category else "-",
        "hw_part_number": lambda entry, request: entry.item.part_number or "-",
        "hw_total_stock": lambda entry, request: str(entry.total_stock),
        "hw_available": lambda entry, request: str(entry.available),
        "hw_min_qty": lambda entry, request: str(entry.item.min_qty),
        "hw_status": _stock_status,
    }

    sample_cells = {
        "hw_name": "USB-C Dock (Mock)",
        "hw_manufacturer": "Dell",
        "hw_category": "Docking",
        "hw_part_number": "WD19S",
        "hw_total_stock": "24",
        "hw_available": "18",
        "hw_min_qty": "5",
        "hw_status": "In Stock",
    }

    group_resolvers = {
        "manufacturer": lambda entry, request: (
            entry.item.manufacturer.name if entry.item.manufacturer else _("Generic")
        ),
        "category": lambda entry, request: entry.item.category.name if entry.item.category else _("Uncategorized"),
    }

    def get_queryset(self, request: ReportRequest):
        """The three stocked catalogues, each labelled by the item type it holds.

        This report's scope is three querysets rather than one, so it hands back
        the labelled tuple :meth:`build` caps and flattens.
        """
        return (
            (_("Accessory"), self._catalogue(Accessory, request)),
            (_("Consumable"), self._catalogue(Consumable, request)),
            (_("Component"), self._catalogue(Component, request)),
        )

    def _catalogue(self, model, request: ReportRequest):
        queryset = (
            model.objects.for_scope(Scope.current())
            .filter(deleted_at__isnull=True)
            .select_related("manufacturer", "category")
        )
        queryset = self.scope_to_tenants(queryset, request)
        tenant_ids = tuple(tenant.pk for tenant in request.filter_tenants if getattr(tenant, "pk", None) is not None)
        return queryset.with_counts(tenant_ids=tenant_ids or None)

    def build(self, request: ReportRequest) -> ReportResult:
        catalogues = self.get_queryset(request)
        # Each catalogue carries its own cap: one oversized catalogue must not
        # crowd the other two out of the report entirely.
        records = self._catalogue_records(catalogues)
        rows = list(self.build_rows(records, request))
        if not rows:
            return self.build_sample(request)
        sku_counts = self._sku_counts(catalogues)
        # The window is per catalogue: any catalogue that stops at the limit
        # makes the whole output a capped window over the summed scope.
        truncated = any(count > self.row_limit for count in sku_counts.values())
        return ReportResult(
            rows=rows,
            summary_cards=self._summary_cards(sku_counts, records, request),
            chart_svg=self._chart(sku_counts, request),
            truncated=truncated,
            total_rows=sum(sku_counts.values()) if truncated else None,
        )

    def _catalogue_records(self, catalogues):
        return [
            StockedItem.of(item, type_label)
            for type_label, queryset in catalogues
            for item in queryset[: self.row_limit]
        ]

    def _sku_counts(self, catalogues):
        return {type_label: queryset.count() for type_label, queryset in catalogues}

    def build_rows(self, records, request: ReportRequest):
        return [self.row_for(entry, request) for entry in records]

    def build_summary(self, catalogues, request: ReportRequest):
        catalogues = tuple(catalogues)
        return self._summary_cards(self._sku_counts(catalogues), self._catalogue_records(catalogues), request)

    def build_chart(self, catalogues, records, request: ReportRequest):
        return self._chart(self._sku_counts(tuple(catalogues)), request)

    def group_key(self, record, request: ReportRequest):
        # Without a supported grouping the rows fall back to their own
        # catalogue, which is the division this report is read by.
        return group_key_for(
            request.template.group_by_field,
            self.group_resolvers,
            record,
            request,
            default=record.type_label,
        )

    def sample_row(self, request: ReportRequest):
        # The sample is one accessory, so it groups under that catalogue
        # whichever grouping the template selected.
        cells = {**self.sample_cells, "hw_item_type": _("Accessory")}
        return sample_report_row(cells, request.columns, _("Accessory"))

    def _summary_cards(self, sku_counts, records, request: ReportRequest):
        if not request.template.include_summary_cards:
            return []
        # Stock is a per-item aggregate and cannot be counted in SQL, so the
        # zero-stock tally is over the rendered window the rows already read.
        zero_stock = sum(1 for entry in records if entry.total_stock <= 0)
        return [
            {"label": _("Accessory SKUs"), "value": str(sku_counts.get(_("Accessory"), 0))},
            {"label": _("Consumable SKUs"), "value": str(sku_counts.get(_("Consumable"), 0))},
            {"label": _("Component SKUs"), "value": str(sku_counts.get(_("Component"), 0))},
            {"label": _("Items at Zero Stock"), "value": str(zero_stock)},
        ]

    def _chart(self, sku_counts, request: ReportRequest):
        if not request.template.include_distribution_chart:
            return ""
        chart_data = [{"label": type_label, "value": count} for type_label, count in sku_counts.items() if count > 0]
        return generate_doughnut_chart(chart_data, title=_("Hardware Inventory by Type"))

    def build_sample_summary(self, request: ReportRequest):
        if not request.template.include_summary_cards:
            return []
        return [
            {"label": _("Accessory SKUs"), "value": "1 (Mock)"},
            {"label": _("Consumable SKUs"), "value": "0"},
            {"label": _("Component SKUs"), "value": "0"},
            {"label": _("Items at Zero Stock"), "value": "0"},
        ]

    def build_sample_chart(self, request: ReportRequest):
        if not request.template.include_distribution_chart:
            return ""
        return generate_doughnut_chart([{"label": _("Accessory"), "value": 1}], title=_("Hardware Inventory by Type"))


register_report_provider(HardwareInventoryReportProvider())
