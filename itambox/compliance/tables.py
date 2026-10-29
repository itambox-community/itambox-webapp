import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_tables2.utils import A

from core.tables import ActionsColumn, BaseTable, ToggleColumn

from .models import CustodyReceipt, CustodyTemplate, SignatureProviderConfig


class CustodyTemplateTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("compliance:custodytemplate_detail", args=[A("pk")], verbose_name=_("Name"))
    tenant = tables.Column(verbose_name=_("Tenant"))
    tenant_group = tables.Column(verbose_name=_("Tenant Group"))
    signature_provider = tables.Column(verbose_name=_("Provider"))
    is_active = tables.BooleanColumn(verbose_name=_("Active"))
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = CustodyTemplate
        fields = ("pk", "name", "tenant", "tenant_group", "signature_provider", "is_active", "actions")
        default_columns = ("pk", "name", "tenant", "tenant_group", "signature_provider", "is_active", "actions")


class CustodyReceiptTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    asset = tables.LinkColumn("assets:asset_detail", args=[A("asset__pk")], accessor="asset", verbose_name=_("Asset"))
    holder = tables.LinkColumn(
        "organization:assetholder_detail", args=[A("holder__pk")], accessor="holder", verbose_name=_("Holder")
    )
    custody_template = tables.LinkColumn(
        "compliance:custodytemplate_detail",
        args=[A("custody_template_id")],
        accessor="custody_template.name",
        verbose_name=_("Template"),
    )
    acceptance_status = tables.Column(verbose_name=_("Status"))
    signed_at = tables.DateTimeColumn(format="Y-m-d H:i", verbose_name=_("Signed At"))
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
        model = CustodyReceipt
        fields = ("pk", "asset", "holder", "custody_template", "acceptance_status", "signed_at", "actions")
        default_columns = ("pk", "asset", "holder", "custody_template", "acceptance_status", "signed_at", "actions")

    def render_actions(self, record):

        url = reverse("compliance:custodyreceipt_detail", kwargs={"pk": record.pk})
        return format_html(
            '<a class="btn btn-sm btn-soft-primary" href="{}" title="View Receipt">'
            '<i class="mdi mdi-eye-outline me-1"></i>View'
            "</a>",
            url,
        )

    def render_acceptance_status(self, value):
        badges = {
            "pending": "bg-warning text-warning-invert",
            "accepted": "bg-success text-success-invert",
            "declined": "bg-danger text-danger-invert",
            "superseded": "bg-secondary text-secondary-invert",
        }
        badge_class = badges.get(value, "bg-secondary")

        return format_html('<span class="badge {}">{}</span>', badge_class, value.title())


class SignatureProviderConfigTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("compliance:signatureproviderconfig_detail", args=[A("pk")], verbose_name=_("Name"))
    tenant = tables.Column(verbose_name=_("Tenant"), default=_("Install-wide"))
    provider = tables.Column(verbose_name=_("Provider"))
    is_active = tables.BooleanColumn(verbose_name=_("Active"))
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = SignatureProviderConfig
        fields = ("pk", "name", "tenant", "provider", "is_active", "actions")
        default_columns = ("pk", "name", "tenant", "provider", "is_active", "actions")
