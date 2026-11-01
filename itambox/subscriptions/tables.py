import django_tables2 as tables
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django_tables2.utils import A

from core.html_styles import status_color_class
from core.tables import ActionsColumn, BaseTable, ToggleColumn
from core.tables.constants import TABLE_EMPTY_VALUE
from extras.tables import TagColumn
from itambox.utils import get_status_color

from .models import Subscription, SubscriptionAssignment


class SubscriptionTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("subscriptions:subscription_detail", args=[A("pk")], verbose_name=_("Name"))
    supplier = tables.LinkColumn(
        "assets:supplier_detail",
        args=[A("supplier_id")],
        accessor="supplier.name",
        verbose_name=_("Supplier"),
    )
    status = tables.Column(verbose_name=_("Status"))
    type = tables.Column(verbose_name=_("Type"))
    tenant = tables.Column(accessor="tenant.name", verbose_name=_("Tenant"), orderable=True)
    start_date = tables.DateColumn(format="Y-m-d", verbose_name=_("Start"))
    renewal_date = tables.DateColumn(format="Y-m-d", verbose_name=_("Next Renewal"))
    renewal_cost = tables.Column(verbose_name=_("Renewal Cost"))
    currency = tables.Column(verbose_name=_("Currency"))
    vendor_contract_auto_renews = tables.BooleanColumn(verbose_name=_("Vendor Auto-Renews"), yesno="✓,")
    tags = TagColumn(url_name="subscriptions:subscription_list")

    days_until_renewal = tables.Column(accessor="days_until_renewal", verbose_name=_("Due In"), orderable=False)
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Subscription
        fields = (
            "pk",
            "name",
            "supplier",
            "status",
            "type",
            "tenant",
            "start_date",
            "renewal_date",
            "renewal_cost",
            "currency",
            "vendor_contract_auto_renews",
            "tags",
            "days_until_renewal",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "supplier",
            "status",
            "type",
            "renewal_date",
            "renewal_cost",
            "tags",
            "days_until_renewal",
            "actions",
        )

    def render_status(self, value, record):
        if record and record.status:
            display = record.get_status_display()
            color = get_status_color(record.status)
            color_class, style_block = status_color_class(color)
            return format_html(
                '{}<span class="badge badge-status {}"><span class="badge-status-dot"></span>{}</span>',
                style_block,
                color_class,
                display,
            )
        return TABLE_EMPTY_VALUE

    def render_renewal_cost(self, value, record):
        if value is not None:
            return f"{value:,.2f} {record.currency or 'USD'}"
        return TABLE_EMPTY_VALUE

    def render_days_until_renewal(self, value):
        if value is None:
            return TABLE_EMPTY_VALUE
        if value < 0:
            count = abs(value)
            label = ngettext("%(count)s day overdue", "%(count)s days overdue", count) % {"count": count}
            return format_html('<span class="text-danger fw-bold">{}</span>', label)
        elif value == 0:
            return format_html('<span class="text-warning fw-bold">{}</span>', _("Today"))
        elif value <= 30:
            label = ngettext("%(count)s day", "%(count)s days", value) % {"count": value}
            return format_html('<span class="text-warning">{}</span>', label)
        return ngettext("%(count)s day", "%(count)s days", value) % {"count": value}


class SubscriptionAssignmentTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    subscription = tables.LinkColumn(
        "subscriptions:subscription_detail",
        args=[A("subscription.pk")],
        accessor="subscription.name",
        verbose_name=_("Subscription"),
    )
    assigned_object = tables.Column(verbose_name=_("Assigned To"), orderable=False)
    assigned_date = tables.DateColumn(format="Y-m-d H:i", verbose_name=_("Assigned"))
    assigned_by = tables.Column(accessor="assigned_by.username", verbose_name=_("By"), default=TABLE_EMPTY_VALUE)
    notes = tables.Column(verbose_name=_("Notes"))
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = SubscriptionAssignment
        fields = ("pk", "subscription", "assigned_object", "assigned_date", "assigned_by", "notes", "actions")
        default_columns = ("pk", "subscription", "assigned_object", "assigned_date", "assigned_by", "actions")

    def render_assigned_object(self, value, record):
        obj = record.tenant_safe_assigned_object
        if obj is None:
            return TABLE_EMPTY_VALUE
        return str(obj)


class SupplierSubscriptionTable(BaseTable):
    """Subscriptions of one supplier (supplier detail tab, issue #508)."""

    name = tables.LinkColumn("subscriptions:subscription_detail", args=[A("pk")], verbose_name=_("Name"))
    status = tables.Column(verbose_name=_("Status"))
    renewal_date = tables.DateColumn(format="Y-m-d", verbose_name=_("Next Renewal"))

    class Meta(BaseTable.Meta):
        model = Subscription
        fields = ("name", "status", "renewal_date")
