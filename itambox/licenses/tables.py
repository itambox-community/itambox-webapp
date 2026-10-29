import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core.tables import ActionsColumn, BaseTable, ToggleColumn
from core.tables.constants import TABLE_EMPTY_VALUE
from extras.tables import TagColumn

from .models import License, LicenseSeatAssignment


class LicenseTable(BaseTable):
    """Table for displaying License entitlements."""

    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn(viewname="licenses:license_detail", args=[tables.A("pk")])
    software = tables.LinkColumn(
        viewname="software:software_detail",
        args=[tables.A("software__pk")],
        accessor="software.name",
        verbose_name=_("Software"),
    )
    license_type = tables.Column(verbose_name=_("Type"))
    seats = tables.Column(verbose_name=_("Total Seats"))
    assigned_seats = tables.Column(accessor="assigned_count", verbose_name=_("Assigned Seats"), orderable=False)
    available_seats = tables.Column(verbose_name=_("Available Seats"), orderable=False)
    tenant = tables.LinkColumn(
        "organization:tenant_detail", args=[tables.A("tenant.pk")], accessor="tenant.name", verbose_name=_("Tenant")
    )
    expiration_date = tables.DateColumn(verbose_name=_("Expiration Date"), format="Y-m-d")
    tags = TagColumn(url_name="licenses:license_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = License
        fields = (
            "pk",
            "name",
            "software",
            "tenant",
            "license_type",
            "seats",
            "assigned_seats",
            "available_seats",
            "purchase_date",
            "expiration_date",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "software",
            "tenant",
            "license_type",
            "seats",
            "assigned_seats",
            "available_seats",
            "expiration_date",
            "tags",
            "actions",
        )

    def render_seats(self, value, record=None):
        if record and value:
            url = f"{reverse('licenses:license_detail', args=[record.pk])}#assignments"
            return format_html('<a href="{}">{}</a>', url, value)
        return value or 0

    def render_assigned_seats(self, value, record=None):
        if record and value:
            url = f"{reverse('licenses:license_detail', args=[record.pk])}#assignments"
            return format_html('<a href="{}">{}</a>', url, value)
        return value or 0

    def render_available_seats(self, value, record=None):
        if record and value is not None:
            url = f"{reverse('licenses:license_detail', args=[record.pk])}#assignments"
            return format_html('<a href="{}">{}</a>', url, value)
        return value or 0


class LicenseSeatAssignmentTable(BaseTable):
    """Table for displaying individual License Seat Assignments."""

    pk = ToggleColumn(accessor="pk")
    license = tables.LinkColumn(
        viewname="licenses:license_detail",
        args=[tables.A("license__pk")],
        accessor="license.name",
        verbose_name=_("License"),
    )
    asset = tables.LinkColumn(
        viewname="assets:asset_detail", args=[tables.A("asset__pk")], accessor="asset.name", verbose_name=_("Asset")
    )
    # Resolves the holder of the seat. Seats assigned directly to a holder show
    # that holder; seats assigned to an *asset* fall back to whoever the asset is
    # currently checked out to (empty_values=() so the renderer runs even when
    # the seat has no direct holder).
    assigned_holder = tables.Column(
        accessor="assigned_holder",
        verbose_name=_("Asset Holder"),
        orderable=False,
        empty_values=(),
    )
    assigned_date = tables.DateTimeColumn(verbose_name=_("Assigned At"), format="Y-m-d H:i")
    notes = tables.Column(verbose_name=_("Notes"))
    actions = tables.TemplateColumn(
        template_code="""
        {% load i18n %}
        <div class="d-flex gap-1 justify-content-end">
            <button hx-post="{% url 'licenses:license_seat_checkin' record.pk %}"
                    hx-confirm="{{ confirm_check_in }}"
                    class="btn btn-sm btn-outline-danger d-flex align-items-center"
                    title="{{ check_in }}">
                <i class="mdi mdi-account-minus-outline me-1"></i>
                {{ check_in }}
            </button>
        </div>
        """,
        extra_context={
            "check_in": _("Check in"),
            "confirm_check_in": _("Are you sure you want to check in this software license seat?"),
        },
        verbose_name=_("Actions"),
        orderable=False,
        attrs={
            "th": {
                "class": "col-actions text-nowrap",
            },
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions"},
        },
    )

    def render_assigned_holder(self, record):

        from organization.models import AssetHolder

        holder = record.assigned_holder
        via_asset = False
        if holder is None and record.asset_id:
            target = record.asset.assigned_to  # the asset's current holder/location/asset
            if isinstance(target, AssetHolder):
                holder = target
                via_asset = True
        if holder is None:
            return TABLE_EMPTY_VALUE
        url = reverse("organization:assetholder_detail", kwargs={"pk": holder.pk})
        label = holder.upn or str(holder)
        if via_asset:
            return format_html(
                '<a href="{}">{}</a> <span class="text-muted small">({})</span>', url, label, _("via asset")
            )
        return format_html('<a href="{}">{}</a>', url, label)

    class Meta(BaseTable.Meta):
        model = LicenseSeatAssignment
        fields = ("pk", "license", "asset", "assigned_holder", "assigned_date", "notes", "actions")
        default_columns = ("pk", "license", "asset", "assigned_holder", "assigned_date", "actions")
