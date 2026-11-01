# itambox/assets/search.py
from core.search import SearchIndex, register_search

from .models import Asset, AssetMaintenance, AssetRequest, AssetRole, Category, Manufacturer, Supplier
from .services.specification_consumers.query import apply_specification_filters
from .tables import with_asset_table_relations


def search_assets_by_specification(
    filters,
    *,
    queryset=None,
    tenant_ids=None,
    definitions=None,
):
    """Search Assets with explicit source-qualified specification filters.

    The ordinary global-search term remains separate from specification search;
    callers must provide ``FieldFilter`` objects (or an empty iterable) and may
    pass resolved FieldDefinition DTOs for current/history applicability.
    """

    if queryset is None:
        queryset = Asset.objects.all()
    return apply_specification_filters(
        queryset,
        tuple(filters),
        tenant_ids=tenant_ids,
        definitions=definitions,
    )


@register_search()
class AssetIndex(SearchIndex):
    model = Asset
    fields = (
        "name",
        "asset_tag",
        "serial_number",
        "notes",
    )
    order_by = ("name",)

    def get_table_queryset(self, queryset):
        """Load the asset list's relations for the search result table (#882)."""
        return with_asset_table_relations(queryset)

    def search_specifications(self, filters, queryset=None, *, tenant_ids=None, definitions=None):
        """Use the same source-qualified query path as list filters and reports."""

        return search_assets_by_specification(
            filters,
            queryset=queryset,
            tenant_ids=tenant_ids,
            definitions=definitions,
        )


@register_search()
class AssetRoleIndex(SearchIndex):
    model = AssetRole
    fields = (
        "name",
        "description",
    )
    order_by = ("name",)


@register_search()
class ManufacturerIndex(SearchIndex):
    model = Manufacturer
    fields = (
        "name",
        "description",
    )
    order_by = ("name",)


@register_search()
class SupplierIndex(SearchIndex):
    model = Supplier
    fields = (
        "name",
        "website",
        "address",
        "notes",
    )
    order_by = ("name",)


@register_search()
class CategoryIndex(SearchIndex):
    model = Category
    fields = (
        "name",
        "description",
    )
    order_by = ("name",)


@register_search()
class AssetRequestIndex(SearchIndex):
    model = AssetRequest
    fields = (
        "notes",
        "response_notes",
    )
    order_by = ("-request_date",)


@register_search()
class AssetMaintenanceIndex(SearchIndex):
    model = AssetMaintenance
    fields = ("notes", "supplier__name", "asset__name")
    order_by = ("-start_date",)

    def get_table_queryset(self, queryset):
        """Load the asset and supplier columns with the rows, as the maintenance list does."""
        return queryset.select_related("asset", "supplier")
