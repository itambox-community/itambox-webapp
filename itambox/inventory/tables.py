import django_tables2 as tables
from django.contrib.contenttypes.models import ContentType
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_tables2.utils import A

from core.managers import get_current_tenant
from core.tables import ActionsColumn, BaseTable, ColorChipColumn, CountLinkColumn, ToggleColumn
from core.tables.constants import TABLE_EMPTY_VALUE
from extras.tables import TagColumn
from organization.access import resolved_shared_stock_ids
from organization.models import TenantResourceGrant

from .models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
    Kit,
)


class CheckoutActionsColumn(ActionsColumn):
    """Actions column for checkable inventory tables: prepends a per-row
    Check-out button to the standard clone/edit/delete actions, so no separate
    checkout column is needed. Uses the wider sticky variant to fit the button."""

    attrs = {
        "th": {"class": "col-actions-wide text-nowrap"},
        "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
    }

    def get_leading_buttons(self, record, table):
        if getattr(record, "deleted_at", None) is not None:
            return ""
        request = getattr(table, "request", None)
        if not request:
            return ""

        app_label = record._meta.app_label
        model_name = record._meta.model_name
        if not table.has_perm(request.user, f"{app_label}.change_{model_name}", record):
            return ""

        url = getattr(record, "checkout_url", "")
        if not url:
            try:
                url = reverse(f"{app_label}:{model_name}_checkout", kwargs={"pk": record.pk})
            except NoReverseMatch:
                return ""

        title = _("Check-out")
        return format_html(
            '<a class="btn btn-sm btn-soft-success check-action cursor-pointer me-1" role="button" '
            'hx-get="{url}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
            'title="{title}" aria-label="{title}"><i class="mdi mdi-logout me-1"></i>{title}</a>',
            url=url,
            title=title,
        )


class CheckableInventoryTableMixin(tables.Table):
    """
    Mixin for django_tables2 tables (ComponentTable, AccessoryTable, ConsumableTable):
    exposes a permission-aware "Check-out" button inside the actions column (no
    separate checkout column).
    """

    actions = CheckoutActionsColumn()


class SharePoolActionMixin:
    """'Share' row action for stock tables (ADR-0001 phase 4b).

    Gated by organization.add_tenantresourcegrant anchored at the POOL —
    its tenant is the owner, so only owner-side operators see the button.
    """

    def share_pool_html(self, request, record):
        if not request or not self.has_perm(
            request.user,
            "organization.add_tenantresourcegrant",
            record,
        ):
            return ""

        ct = ContentType.objects.get_for_model(type(record))
        share_url = reverse(
            "organization:tenantresourcegrant_add",
            kwargs={"content_type_id": ct.pk, "resource_id": record.pk},
        )
        return format_html(
            '  <a class="btn btn-sm btn-action d-flex align-items-center" href="{}" title="{}">'
            '    <i class="mdi mdi-share-variant-outline me-1"></i> {}'
            "  </a>",
            share_url,
            _("Share this pool with another tenant"),
            _("Share"),
        )

    def shared_checkout_html(self, request, record, *, permission, route, item):
        if not request:
            return ""
        active_tenant = get_current_tenant()
        if active_tenant is None:
            return ""
        cache_key = (
            record._meta.label_lower,
            permission,
            active_tenant.pk,
            getattr(request.user, "pk", None),
        )
        cache = getattr(self, "_shared_checkout_stock_ids", None)
        if cache is None:
            cache = self._shared_checkout_stock_ids = {}
        if cache_key not in cache:
            cache[cache_key] = set(
                resolved_shared_stock_ids(
                    type(record),
                    active_tenant,
                    request.user,
                    TenantResourceGrant.ACCESS_USE,
                    permission,
                )
            )
        if record.pk not in cache[cache_key]:
            return ""
        checkout_url = reverse(route, kwargs={"pk": item.pk})
        checkout_title = _("Check-out")
        return format_html(
            '<a class="btn btn-sm btn-soft-success check-action d-flex align-items-center" role="button" '
            'hx-get="{}?from_location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
            'title="{}" aria-label="{}">'
            '<i class="mdi mdi-logout me-1"></i> {}'
            "</a>",
            checkout_url,
            record.location.pk,
            checkout_title,
            checkout_title,
            checkout_title,
        )


class AccessoryTable(CheckableInventoryTableMixin, BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("inventory:accessory_detail", args=[A("pk")], verbose_name=_("Name"))
    manufacturer = tables.Column(linkify=True)
    category = ColorChipColumn(accessor="category", verbose_name=_("Category"), order_by=("category__name",))
    part_number = tables.Column(verbose_name=_("Part Number"))
    total_stock = CountLinkColumn(
        "inventory:accessorystock_list", "accessory", accessor="total_stock", verbose_name=_("Total Stock")
    )
    checked_out_qty = CountLinkColumn(
        "inventory:accessoryassignment_list", "accessory", accessor="checked_out_qty", verbose_name=_("Checked Out")
    )
    available = tables.Column(accessor="available", verbose_name=_("Available"))
    tenant = tables.LinkColumn(
        "organization:tenant_detail", args=[A("tenant.pk")], accessor="tenant.name", verbose_name=_("Tenant")
    )
    tags = TagColumn(url_name="inventory:accessory_list")

    class Meta(BaseTable.Meta):
        model = Accessory
        fields = (
            "pk",
            "name",
            "manufacturer",
            "tenant",
            "category",
            "part_number",
            "total_stock",
            "checked_out_qty",
            "available",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "manufacturer",
            "tenant",
            "category",
            "total_stock",
            "checked_out_qty",
            "available",
            "tags",
            "actions",
        )

    def render_available(self, value, record):
        if value <= 0:
            return format_html(
                '<span class="badge bg-danger-lt text-danger font-weight-bold">0 ({})</span>', _("Empty")
            )
        elif value < record.min_qty:
            return format_html(
                '<span class="badge bg-warning-lt text-warning font-weight-bold">{} ({})</span>', value, _("Low")
            )
        return value


class AccessoryStockTable(SharePoolActionMixin, BaseTable):
    pk = ToggleColumn(accessor="pk")
    accessory = tables.LinkColumn("inventory:accessory_detail", args=[A("accessory.pk")], verbose_name=_("Accessory"))
    location = tables.LinkColumn("organization:location_detail", args=[A("location.pk")], verbose_name=_("Location"))
    qty = tables.Column(verbose_name=_("Quantity"))
    actions = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-actions-wide text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
        },
    )

    class Meta(BaseTable.Meta):
        model = AccessoryStock
        fields = ("pk", "accessory", "location", "qty", "actions")
        default_columns = ("pk", "accessory", "location", "qty", "actions")

    def render_actions(self, record):
        request = getattr(self, "request", None)
        shared_checkout = self.shared_checkout_html(
            request,
            record,
            permission="inventory.add_accessoryassignment",
            route="inventory:accessory_checkout",
            item=record.accessory,
        )
        if shared_checkout:
            return shared_checkout
        can_manage_owner_item = bool(
            request and self.has_perm(request.user, "inventory.change_accessory", record.accessory)
        )
        if not can_manage_owner_item and not shared_checkout:
            shared = self.share_pool_html(request, record)
            return shared or format_html('<span class="text-muted small">{}</span>', _("Not shared"))

        checkout_url = reverse("inventory:accessory_checkout", kwargs={"pk": record.accessory.pk})
        checkout_title = _("Check-out")
        delete_url = reverse("inventory:accessorystock_delete", kwargs={"pk": record.pk})
        add_stock_url = reverse("inventory:accessory_add_stock", kwargs={"pk": record.accessory.pk})

        add_stock_html = ""
        if self.has_perm(request.user, "inventory.change_accessorystock", record.accessory):
            add_stock_label = _("Add stock")
            add_stock_html = format_html(
                '  <button type="button" class="btn btn-sm btn-action d-flex align-items-center" '
                '          hx-get="{}?location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
                '          title="{}" aria-label="{}">'
                '    <i class="mdi mdi-plus me-1"></i> {}'
                "  </button>",
                add_stock_url,
                record.location.pk,
                add_stock_label,
                add_stock_label,
                add_stock_label,
            )

        delete_label = _("Delete")
        return format_html(
            '<div class="d-flex gap-1 justify-content-end">'
            "  {}"
            '  <a class="btn btn-sm btn-soft-success check-action d-flex align-items-center cursor-pointer" role="button" '
            '     hx-get="{}?from_location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
            '     title="{}" aria-label="{}">'
            '    <i class="mdi mdi-logout me-1"></i> {}'
            "  </a>"
            '  <a class="btn btn-sm btn-action btn-action-danger px-2 d-flex align-items-center" href="{}" '
            '     title="{}" aria-label="{}">'
            '    <i class="mdi mdi-trash-can-outline m-0"></i>'
            "  </a>"
            "{}"
            "</div>",
            add_stock_html,
            checkout_url,
            record.location.pk,
            checkout_title,
            checkout_title,
            checkout_title,
            delete_url,
            delete_label,
            delete_label,
            self.share_pool_html(request, record),
        )

    def render_qty(self, value, record):
        return format_html(
            '<span class="stock-adjust-quantity badge bg-blue-lt text-blue font-weight-bold px-2 py-1">{}</span>',
            value,
        )


class AccessoryAssignmentTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    accessory = tables.LinkColumn("inventory:accessory_detail", args=[A("accessory__pk")], verbose_name=_("Accessory"))
    assigned_to = tables.Column(verbose_name=_("Assigned To"), orderable=False, empty_values=())
    qty = tables.Column(verbose_name=_("Qty"))
    assigned_date = tables.DateTimeColumn(format="Y-m-d H:i", verbose_name=_("Date"))
    actions = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-actions text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions"},
        },
    )

    class Meta(BaseTable.Meta):
        model = AccessoryAssignment
        fields = ("pk", "accessory", "assigned_to", "qty", "assigned_date", "actions")
        default_columns = ("pk", "accessory", "assigned_to", "qty", "assigned_date", "actions")

    def render_assigned_to(self, record):
        if record.assigned_holder:
            url = reverse("organization:assetholder_detail", kwargs={"pk": record.assigned_holder.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Holder"), record.assigned_holder)
        elif record.assigned_location:
            url = reverse("organization:location_detail", kwargs={"pk": record.assigned_location.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Location"), record.assigned_location)
        elif record.assigned_asset:
            url = reverse("assets:asset_detail", kwargs={"pk": record.assigned_asset.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Asset"), record.assigned_asset)
        return TABLE_EMPTY_VALUE

    def render_actions(self, record):
        request = getattr(self, "request", None)
        if not request or not self.has_perm(request.user, "inventory.change_accessory", record.accessory):
            return format_html('<span class="text-muted small">{}</span>', _("Not available"))

        url = reverse("inventory:accessory_checkin", kwargs={"pk": record.pk})
        confirm_msg = _("Are you sure you want to check in this accessory assignment?")
        return format_html(
            '<div class="d-flex gap-1 justify-content-end">'
            '  <button hx-post="{0}" hx-confirm="{1}" '
            '          class="btn btn-sm btn-soft-outline-success check-action d-flex align-items-center" '
            '          title="{2}" aria-label="{2}">'
            '    <i class="mdi mdi-keyboard-return me-1"></i> {2}'
            "  </button>"
            "</div>",
            url,
            confirm_msg,
            _("Check in"),
        )


class ConsumableTable(CheckableInventoryTableMixin, BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("inventory:consumable_detail", args=[A("pk")], verbose_name=_("Name"))
    manufacturer = tables.Column(linkify=True)
    category = ColorChipColumn(accessor="category", verbose_name=_("Category"), order_by=("category__name",))
    part_number = tables.Column(verbose_name=_("Part Number"))
    total_stock = CountLinkColumn(
        "inventory:consumablestock_list", "consumable", accessor="total_stock", verbose_name=_("Total Qty")
    )
    consumed_qty = CountLinkColumn(
        "inventory:consumableassignment_list", "consumable", accessor="consumed_qty", verbose_name=_("Consumed")
    )
    available = tables.Column(accessor="available", verbose_name=_("Available"))
    tenant = tables.LinkColumn(
        "organization:tenant_detail", args=[A("tenant.pk")], accessor="tenant.name", verbose_name=_("Tenant")
    )
    tags = TagColumn(url_name="inventory:consumable_list")

    class Meta(BaseTable.Meta):
        model = Consumable
        fields = (
            "pk",
            "name",
            "manufacturer",
            "tenant",
            "category",
            "part_number",
            "total_stock",
            "consumed_qty",
            "available",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "manufacturer",
            "tenant",
            "category",
            "total_stock",
            "consumed_qty",
            "available",
            "tags",
            "actions",
        )

    def render_available(self, value, record):
        if value <= 0:
            return format_html(
                '<span class="badge bg-danger-lt text-danger font-weight-bold">0 ({})</span>', _("Out of stock")
            )
        elif value < record.min_qty:
            return format_html(
                '<span class="badge bg-warning-lt text-warning font-weight-bold">{} ({})</span>', value, _("Low stock")
            )
        return value


class ConsumableStockTable(SharePoolActionMixin, BaseTable):
    pk = ToggleColumn(accessor="pk")
    consumable = tables.LinkColumn(
        "inventory:consumable_detail", args=[A("consumable.pk")], verbose_name=_("Consumable")
    )
    location = tables.LinkColumn("organization:location_detail", args=[A("location.pk")], verbose_name=_("Location"))
    qty = tables.Column(verbose_name=_("Quantity"))
    actions = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-actions-wide text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
        },
    )

    class Meta(BaseTable.Meta):
        model = ConsumableStock
        fields = ("pk", "consumable", "location", "qty", "actions")
        default_columns = ("pk", "consumable", "location", "qty", "actions")

    def render_actions(self, record):
        request = getattr(self, "request", None)
        shared_checkout = self.shared_checkout_html(
            request,
            record,
            permission="inventory.add_consumableassignment",
            route="inventory:consumable_checkout",
            item=record.consumable,
        )
        if shared_checkout:
            return shared_checkout
        if not request or not self.has_perm(request.user, "inventory.change_consumable", record.consumable):
            return (
                shared_checkout
                or self.share_pool_html(request, record)
                or format_html('<span class="text-muted small">{}</span>', _("Not shared"))
            )

        checkout_url = reverse("inventory:consumable_checkout", kwargs={"pk": record.consumable.pk})
        delete_url = reverse("inventory:consumablestock_delete", kwargs={"pk": record.pk})
        add_stock_url = reverse("inventory:consumable_add_stock", kwargs={"pk": record.consumable.pk})
        checkout_title = _("Check-out")

        add_stock_html = ""
        if self.has_perm(request.user, "inventory.change_consumablestock", record.consumable):
            add_stock_label = _("Add stock")
            add_stock_html = format_html(
                '  <button type="button" class="btn btn-sm btn-action d-flex align-items-center" '
                '          hx-get="{}?location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
                '          title="{}" aria-label="{}">'
                '    <i class="mdi mdi-plus me-1"></i> {}'
                "  </button>",
                add_stock_url,
                record.location.pk,
                add_stock_label,
                add_stock_label,
                add_stock_label,
            )

        delete_label = _("Delete")
        return format_html(
            '<div class="d-flex gap-1 justify-content-end">'
            "  {}"
            '  <a class="btn btn-sm btn-soft-success check-action d-flex align-items-center cursor-pointer" role="button" '
            '     hx-get="{}?from_location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
            '     title="{}" aria-label="{}">'
            '    <i class="mdi mdi-logout me-1"></i> {}'
            "  </a>"
            '  <a class="btn btn-sm btn-action btn-action-danger px-2 d-flex align-items-center" href="{}" '
            '     title="{}" aria-label="{}">'
            '    <i class="mdi mdi-trash-can-outline m-0"></i>'
            "  </a>"
            "{}"
            "</div>",
            add_stock_html,
            checkout_url,
            record.location.pk,
            checkout_title,
            checkout_title,
            checkout_title,
            delete_url,
            delete_label,
            delete_label,
            self.share_pool_html(request, record),
        )

    def render_qty(self, value, record):
        return format_html(
            '<span class="stock-adjust-quantity badge bg-blue-lt text-blue font-weight-bold px-2 py-1">{}</span>',
            value,
        )


class ConsumableAssignmentTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    consumable = tables.LinkColumn(
        "inventory:consumable_detail", args=[A("consumable__pk")], verbose_name=_("Consumable")
    )
    assigned_to = tables.Column(verbose_name=_("Consumed By"), orderable=False, empty_values=())
    qty = tables.Column(verbose_name=_("Qty"))
    assigned_date = tables.DateTimeColumn(format="Y-m-d H:i", verbose_name=_("Date"))
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = ConsumableAssignment
        fields = ("pk", "consumable", "assigned_to", "qty", "assigned_date", "actions")
        default_columns = ("pk", "consumable", "assigned_to", "qty", "assigned_date", "actions")

    def render_assigned_to(self, record):
        if record.assigned_holder:
            url = reverse("organization:assetholder_detail", kwargs={"pk": record.assigned_holder.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Holder"), record.assigned_holder)
        elif record.assigned_location:
            url = reverse("organization:location_detail", kwargs={"pk": record.assigned_location.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Location"), record.assigned_location)
        elif record.assigned_asset:
            url = reverse("assets:asset_detail", kwargs={"pk": record.assigned_asset.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Asset"), record.assigned_asset)
        return TABLE_EMPTY_VALUE


class KitTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("inventory:kit_detail", args=[A("pk")], verbose_name=_("Name"))
    description = tables.Column(verbose_name=_("Description"))
    item_count = tables.Column(accessor="item_count", verbose_name=_("Items Count"), orderable=False)
    tenant = tables.LinkColumn(
        "organization:tenant_detail", args=[A("tenant.pk")], accessor="tenant.name", verbose_name=_("Tenant")
    )
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Kit
        fields = ("pk", "name", "tenant", "description", "item_count", "actions")
        default_columns = ("pk", "name", "tenant", "description", "item_count", "actions")

    def render_item_count(self, value, record):
        if record and value:
            url = reverse("inventory:kit_detail", args=[record.pk])
            return format_html('<a href="{}">{}</a>', url, value)
        return value or 0


class ComponentTable(CheckableInventoryTableMixin, BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("inventory:component_detail", args=[A("pk")], verbose_name=_("Name"))
    manufacturer = tables.Column(linkify=True)
    category = ColorChipColumn(accessor="category", verbose_name=_("Category"), order_by=("category__name",))
    part_number = tables.Column(verbose_name=_("Part Number"))
    total_stock = CountLinkColumn(
        "inventory:componentstock_list", "component", verbose_name=_("Total Stock"), orderable=False
    )
    available_stock = tables.Column(verbose_name=_("Available"), orderable=False)
    min_qty = tables.Column(verbose_name=_("Safety Threshold"))
    tenant = tables.Column(linkify=True)
    tags = TagColumn(url_name="inventory:component_list")

    class Meta(BaseTable.Meta):
        model = Component
        fields = (
            "pk",
            "name",
            "manufacturer",
            "category",
            "part_number",
            "total_stock",
            "available_stock",
            "min_qty",
            "tenant",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "manufacturer",
            "category",
            "part_number",
            "total_stock",
            "available_stock",
            "min_qty",
            "tenant",
            "tags",
            "actions",
        )


class ComponentStockTable(SharePoolActionMixin, BaseTable):
    pk = ToggleColumn(accessor="pk")
    component = tables.LinkColumn("inventory:component_detail", args=[A("component.pk")], verbose_name=_("Component"))
    location = tables.LinkColumn("organization:location_detail", args=[A("location.pk")], verbose_name=_("Location"))
    qty = tables.Column(verbose_name=_("Quantity"))
    actions = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-actions-wide text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
        },
    )

    class Meta(BaseTable.Meta):
        model = ComponentStock
        fields = ("pk", "component", "location", "qty", "actions")
        default_columns = ("pk", "component", "location", "qty", "actions")

    def render_actions(self, record):
        request = getattr(self, "request", None)
        shared_checkout = self.shared_checkout_html(
            request,
            record,
            permission="inventory.add_componentallocation",
            route="inventory:component_checkout",
            item=record.component,
        )
        if shared_checkout:
            return shared_checkout
        if not request or not self.has_perm(request.user, "inventory.change_component", record.component):
            return (
                shared_checkout
                or self.share_pool_html(request, record)
                or format_html('<span class="text-muted small">{}</span>', _("Not shared"))
            )

        checkout_url = reverse("inventory:component_checkout", kwargs={"pk": record.component.pk})
        delete_url = reverse("inventory:componentstock_delete", kwargs={"pk": record.pk})
        add_stock_url = reverse("inventory:component_add_stock", kwargs={"pk": record.component.pk})
        checkout_title = _("Check-out")

        add_stock_html = ""
        if self.has_perm(request.user, "inventory.change_componentstock", record.component):
            add_stock_label = _("Add stock")
            add_stock_html = format_html(
                '  <button type="button" class="btn btn-sm btn-action d-flex align-items-center" '
                '          hx-get="{}?location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
                '          title="{}" aria-label="{}">'
                '    <i class="mdi mdi-plus me-1"></i> {}'
                "  </button>",
                add_stock_url,
                record.location.pk,
                add_stock_label,
                add_stock_label,
                add_stock_label,
            )

        delete_label = _("Delete")
        return format_html(
            '<div class="d-flex gap-1 justify-content-end">'
            "  {}"
            '  <a class="btn btn-sm btn-soft-success check-action d-flex align-items-center cursor-pointer" role="button" '
            '     hx-get="{}?from_location={}" hx-target="#modal-placeholder" hx-swap="innerHTML" '
            '     title="{}" aria-label="{}">'
            '    <i class="mdi mdi-logout me-1"></i> {}'
            "  </a>"
            '  <a class="btn btn-sm btn-action btn-action-danger px-2 d-flex align-items-center" href="{}" '
            '     title="{}" aria-label="{}">'
            '    <i class="mdi mdi-trash-can-outline m-0"></i>'
            "  </a>"
            "{}"
            "</div>",
            add_stock_html,
            checkout_url,
            record.location.pk,
            checkout_title,
            checkout_title,
            checkout_title,
            delete_url,
            delete_label,
            delete_label,
            self.share_pool_html(request, record),
        )

    def render_qty(self, value, record):
        return format_html(
            '<span class="stock-adjust-quantity badge bg-blue-lt text-blue font-weight-bold px-2 py-1">{}</span>',
            value,
        )


class ComponentAllocationTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    component = tables.LinkColumn("inventory:component_detail", args=[A("component__pk")], verbose_name=_("Component"))
    assigned_to = tables.Column(verbose_name=_("Assigned To"), orderable=False, empty_values=())
    qty = tables.Column(verbose_name=_("Qty"))
    assigned_date = tables.DateTimeColumn(format="Y-m-d H:i", verbose_name=_("Date"))
    actions = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-actions text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions"},
        },
    )

    class Meta(BaseTable.Meta):
        model = ComponentAllocation
        fields = ("pk", "component", "assigned_to", "qty", "assigned_date", "actions")
        default_columns = ("pk", "component", "assigned_to", "qty", "assigned_date", "actions")

    def render_assigned_to(self, record):
        if record.assigned_holder:
            url = reverse("organization:assetholder_detail", kwargs={"pk": record.assigned_holder.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Holder"), record.assigned_holder)
        elif record.assigned_location:
            url = reverse("organization:location_detail", kwargs={"pk": record.assigned_location.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Location"), record.assigned_location)
        elif record.assigned_asset:
            url = reverse("assets:asset_detail", kwargs={"pk": record.assigned_asset.pk})
            return format_html('<a href="{}">{}: {}</a>', url, _("Asset"), record.assigned_asset)
        return TABLE_EMPTY_VALUE

    def render_actions(self, record):
        request = getattr(self, "request", None)
        if not request or not self.has_perm(request.user, "inventory.change_component", record.component):
            return format_html('<span class="text-muted small">{}</span>', _("Not available"))

        url = reverse("inventory:component_checkin", kwargs={"pk": record.pk})
        confirm_msg = _("Are you sure you want to check in this component allocation?")
        return format_html(
            '<div class="d-flex gap-1 justify-content-end">'
            '  <button hx-post="{0}" hx-confirm="{1}" '
            '          class="btn btn-sm btn-soft-outline-success check-action d-flex align-items-center" '
            '          title="{2}" aria-label="{2}">'
            '    <i class="mdi mdi-keyboard-return me-1"></i> {2}'
            "  </button>"
            "</div>",
            url,
            confirm_msg,
            _("Check in"),
        )
