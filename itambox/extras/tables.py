# itambox/extras/tables.py
import django_tables2 as tables
from django.apps import apps
from django.core.exceptions import ObjectDoesNotExist
from django.middleware.csrf import get_token
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django_tables2.utils import A

from core.html_styles import color_chip_class, safe_hex_color
from core.tables import ActionsColumn, BaseTable, BooleanColumn, ToggleColumn
from core.tables.constants import TABLE_EMPTY_VALUE
from core.templatetags.utility_tags import localize_journal_comment
from itambox.views.generic.mixins import is_managed_definition

from .models import (
    AlertLog,
    AlertRule,
    CustomField,
    CustomFieldset,
    EventRule,
    ExportTemplate,
    JournalEntry,
    LabelTemplate,
    NotificationChannel,
    ReportTemplate,
    SavedFilter,
    ScheduledReport,
    Tag,
    WebhookDelivery,
    WebhookEndpoint,
)

# =============================================================================
# Custom Columns
# =============================================================================


class TagColumn(tables.ManyToManyColumn):
    """
    A table column which renders linked tags for an object.
    """

    def __init__(self, url_name=None, *args, **kwargs):
        self.url_name = url_name
        # Prevent default linking of ManyToManyColumn
        kwargs.setdefault("linkify_item", False)
        super().__init__(*args, **kwargs)

    def render(self, value):
        if not value:
            return self.default or ""
        tags = list(self.filter(value))
        if not tags:
            return self.default or ""

        limit = 3
        visible_tags = tags[:limit]
        remaining_count = len(tags) - limit

        rendered_tags = []
        for tag in visible_tags:
            color_hex = safe_hex_color(tag.color)
            color_class, style_block = color_chip_class(color_hex)

            # calculate contrast color using YIQ formula
            r = int(color_hex[0:2], 16)
            g = int(color_hex[2:4], 16)
            b = int(color_hex[4:6], 16)
            yiq = ((r * 299) + (g * 587) + (b * 114)) / 1000
            text_class = "color-chip-text-dark" if yiq >= 150 else "color-chip-text-light"

            url = reverse(self.url_name or "extras:tag_list") + "?tag=" + escape(tag.slug)
            rendered_tags.append(
                format_html(
                    '{}<a href="{}" class="badge me-1 {} {}">{}</a>',
                    style_block,
                    url,
                    color_class,
                    text_class,
                    tag.name,
                )
            )

        if remaining_count > 0:
            total_title = ngettext("%(count)s tag total", "%(count)s tags total", len(tags)) % {"count": len(tags)}
            rendered_tags.append(
                format_html('<span class="badge bg-secondary" title="{}">+{}</span>', total_title, remaining_count)
            )

        return mark_safe("".join(rendered_tags))


# =============================================================================
# Model Tables
# =============================================================================


class ExportTemplateTable(BaseTable):
    name = tables.Column(linkify=True)
    content_type = tables.Column(verbose_name=_("Model"))
    file_extension = tables.Column(verbose_name=_("File Type"))
    mime_type = tables.Column()

    class Meta(BaseTable.Meta):
        model = ExportTemplate
        fields = ("name", "content_type", "file_extension", "mime_type")
        sequence = ("name", "content_type", "file_extension", "mime_type")

    def render_content_type(self, value):
        return f"{value.app_label}.{value.model}"


class WebhookEndpointTable(BaseTable):
    name = tables.Column(linkify=True)
    url = tables.Column()
    http_method = tables.Column(verbose_name=_("Method"))
    enabled = BooleanColumn()
    retry_count = tables.Column(verbose_name=_("Retries"))

    class Meta(BaseTable.Meta):
        model = WebhookEndpoint
        fields = ("name", "url", "http_method", "enabled", "retry_count")
        sequence = ("name", "url", "http_method", "enabled", "retry_count")


class EventRuleTable(BaseTable):
    name = tables.Column(linkify=True)
    model = tables.Column(verbose_name=_("Model"))
    action_type = tables.Column(verbose_name=_("Action"))
    conditions = tables.Column(accessor="conditions_withdrawn", verbose_name=_("Conditions"), orderable=False)
    enabled = BooleanColumn()

    class Meta(BaseTable.Meta):
        model = EventRule
        fields = ("name", "model", "action_type", "conditions", "enabled")
        sequence = ("name", "model", "action_type", "conditions", "enabled")

    def render_model(self, value):
        return f"{value.app_label}.{value.model}"

    def render_action_type(self, value):
        action_map = dict(EventRule.ACTION_TYPE_CHOICES)
        return action_map.get(value, value)

    def render_conditions(self, value):
        if value:
            return format_html('<span class="badge bg-warning">{}</span>', _("Withdrawn"))
        return TABLE_EMPTY_VALUE


class LabelTemplateTable(BaseTable):
    name = tables.Column(linkify=True)
    description = tables.Column()
    page_width = tables.Column(verbose_name=_("Width (in)"))
    page_height = tables.Column(verbose_name=_("Height (in)"))
    barcode_format = tables.Column(verbose_name=_("Barcode"))
    content_types = tables.Column(verbose_name=_("Object Types"), orderable=False, empty_values=())

    class Meta(BaseTable.Meta):
        model = LabelTemplate
        fields = ("name", "description", "content_types", "page_width", "page_height", "barcode_format")
        sequence = ("name", "description", "content_types", "page_width", "page_height", "barcode_format")

    def render_content_types(self, record):
        names = [
            str(ct.model_class()._meta.verbose_name_plural) for ct in record.content_types.all() if ct.model_class()
        ]
        return ", ".join(names) or "—"

    def render_barcode_format(self, value):
        fmt_map = dict(LabelTemplate._meta.get_field("barcode_format").choices)
        return fmt_map.get(value, value)


class TagTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("extras:tag_detail", args=[A("pk")], verbose_name=_("Name"))
    # You might want a column to show count of items tagged with this tag.
    # This can be complex depending on how Tags are related (GenericForeignKey?).
    # For now, let's omit the count.
    # item_count = tables.Column(verbose_name='Tagged Items', orderable=False, empty_values=())
    color = tables.Column(verbose_name=_("Color"), orderable=True)
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Tag
        fields = ("pk", "name", "slug", "color", "description", "actions")
        default_columns = ("pk", "name", "color", "description", "actions")

    def render_color(self, value):
        if value:
            normalized = safe_hex_color(value)
            color_class, style_block = color_chip_class(normalized)
            return format_html('{}<span class="badge {}">&nbsp;</span> #{}', style_block, color_class, normalized)
        return TABLE_EMPTY_VALUE


class ManagedDefinitionActionsColumn(ActionsColumn):
    def render(self, record, table, **kwargs):
        if is_managed_definition(record):
            return format_html(
                '<a class="btn btn-sm btn-action" href="{}?tab=changelog" title="{}" aria-label="{}">'
                '<i class="mdi mdi-history"></i></a>',
                record.get_absolute_url(),
                _("Changelog"),
                _("Changelog"),
            )
        return super().render(record, table, **kwargs)


class CustomFieldTable(BaseTable):
    bulk_edit_enabled = False
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("extras:customfield_detail", args=[A("pk")], verbose_name=_("Name"))
    label = tables.Column(verbose_name=_("Label"))
    management_kind = tables.Column(verbose_name=_("Management"))
    field_type = tables.Column(verbose_name=_("Field Type"))
    required = tables.BooleanColumn(verbose_name=_("Required"))
    object_types = tables.ManyToManyColumn(
        verbose_name=_("Applies To"),
        transform=lambda ct: ct.model_class()._meta.verbose_name.title() if ct.model_class() else ct.model,
        orderable=False,
    )
    actions = ManagedDefinitionActionsColumn()

    class Meta(BaseTable.Meta):
        model = CustomField
        fields = ("pk", "name", "label", "management_kind", "field_type", "required", "object_types", "actions")
        default_columns = (
            "pk",
            "name",
            "label",
            "management_kind",
            "field_type",
            "required",
            "object_types",
            "actions",
        )


class CustomFieldsetTable(BaseTable):
    bulk_edit_enabled = False
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn(
        "extras:customfieldset_detail",
        args=[A("pk")],
        accessor="label",
        text=lambda record: record.label or record.slug,
        empty_values=(),
        order_by=("label",),
        verbose_name=_("Name"),
    )
    management_kind = tables.Column(verbose_name=_("Management"))
    fields_count = tables.Column(verbose_name=_("Fields Count"), orderable=False)
    actions = ManagedDefinitionActionsColumn()

    class Meta(BaseTable.Meta):
        model = CustomFieldset
        fields = ("pk", "name", "management_kind", "fields_count", "actions")
        default_columns = ("pk", "name", "management_kind", "fields_count", "actions")

    def render_fields_count(self, value, record=None):
        return value or 0


class SavedFilterTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("extras:savedfilter_detail", args=[A("pk")], verbose_name=_("Name"))
    content_type = tables.Column(verbose_name=_("Object Type"), accessor="content_type")
    shared = BooleanColumn(verbose_name=_("Shared"))
    enabled = BooleanColumn(verbose_name=_("Enabled"))
    tenant = tables.Column(verbose_name=_("Tenant"), accessor="tenant.name", linkify=False)
    created_by = tables.Column(verbose_name=_("Created By"), accessor="created_by")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = SavedFilter
        fields = ("pk", "name", "content_type", "shared", "enabled", "tenant", "created_by", "actions")
        default_columns = ("pk", "name", "content_type", "shared", "enabled", "tenant", "created_by", "actions")

    def render_content_type(self, value):
        model = value.model_class()
        return model._meta.verbose_name.title() if model else value.model

    def render_tenant(self, value):
        return value or format_html('<span class="badge bg-secondary">{}</span>', _("Global"))


class JournalEntryTable(BaseTable):
    """Global activity list of journal entries across every object.

    There is no per-entry detail page (journaling is an inline-add feature), so
    the Object column links straight to the journaled object's detail view.
    """

    created = tables.DateTimeColumn(verbose_name=_("Created"), format="Y-m-d H:i:s", linkify=False)
    content_object = tables.Column(
        verbose_name=_("Object"),
        accessor="content_object",
        orderable=False,
        empty_values=(),
    )
    model = tables.Column(verbose_name=_("Object Type"), linkify=False)
    user = tables.Column(verbose_name=_("User"), linkify=False)
    comment = tables.Column(verbose_name=_("Comment"), orderable=False)

    class Meta(BaseTable.Meta):
        model = JournalEntry
        fields = ("created", "content_object", "model", "user", "comment")
        default_columns = ("created", "content_object", "model", "user", "comment")
        order_by = ("-created",)

    def render_content_object(self, value):
        if value is None:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)
        get_url = getattr(value, "get_absolute_url", None)
        url = None
        if get_url is not None:
            try:
                url = get_url()
            except NoReverseMatch:
                url = None
        if url:
            return format_html('<a href="{}">{}</a>', url, str(value))
        return str(value)

    def render_model(self, value):
        model = value.model_class()
        return model._meta.verbose_name.title() if model else value.model

    def render_comment(self, value):
        return Truncator(str(localize_journal_comment(str(value)))).chars(120)


class WebhookDeliveryActionsColumn(tables.Column):
    empty_values = ()
    orderable = False
    verbose_name = ""
    attrs = {
        "th": {"class": "col-actions text-nowrap"},
        "td": {"class": "text-end text-nowrap noprint p-1 col-actions"},
    }

    def render(self, value, record, bound_column, **kwargs):
        table = bound_column._table
        if not getattr(table, "can_redeliver", False) or record.status not in {"failed", "dead", "success"}:
            return ""

        request = getattr(table, "request", None)
        csrf_token = get_token(request) if request is not None else ""
        url = reverse("extras:webhookdelivery_redeliver", kwargs={"pk": record.pk})
        return format_html(
            '<form method="post" action="{}" class="d-inline">'
            '<input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '<button type="submit" class="btn btn-sm btn-outline-warning">'
            '<i class="mdi mdi-refresh"></i> {}'
            "</button></form>",
            url,
            csrf_token,
            _("Redeliver"),
        )


class WebhookDeliveryTable(BaseTable):
    delivery_id = tables.Column(
        verbose_name=_("Delivery ID"),
        orderable=False,
        # Render even for blank ids: empty_values would skip render_delivery_id
        # and leave the raw table dash instead of the muted marker.
        empty_values=(),
    )
    event = tables.Column(
        verbose_name=_("Event / Action"),
        accessor="event",
        orderable=False,
        # Render even for event-less test sends: empty_values would skip
        # render_event entirely and show a plain dash for test deliveries.
        empty_values=(),
    )
    status = tables.Column(verbose_name=_("Status"))
    attempt = tables.Column(verbose_name=_("Attempt"))
    response_code = tables.Column(verbose_name=_("Response"))
    error_message = tables.Column(verbose_name=_("Error"), accessor="error_message", orderable=False)
    next_retry_at = tables.DateTimeColumn(verbose_name=_("Next Retry"), format="Y-m-d H:i:s")
    test_send = tables.Column(verbose_name=_("Test"), orderable=False)
    redelivered_at = tables.DateTimeColumn(verbose_name=_("Redelivered"), format="Y-m-d H:i:s")
    created_at = tables.DateTimeColumn(verbose_name=_("Created"), format="Y-m-d H:i:s")
    actions = WebhookDeliveryActionsColumn()

    class Meta(BaseTable.Meta):
        model = WebhookDelivery
        fields = (
            "delivery_id",
            "event",
            "status",
            "attempt",
            "response_code",
            "error_message",
            "next_retry_at",
            "test_send",
            "redelivered_at",
            "created_at",
            "actions",
        )
        default_columns = fields
        order_by = ("-created_at",)

    def __init__(self, *args, can_redeliver=False, **kwargs):
        self.can_redeliver = can_redeliver
        self.request = kwargs.get("request")
        super().__init__(*args, **kwargs)

    def render_delivery_id(self, value):
        if not value:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)
        return format_html('<code title="{}">{}</code>', value, str(value)[:8])

    def render_event(self, value, record):
        if record.test_send:
            return _("Test webhook")
        if value is None:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)
        return value.get_action_display()

    def render_status(self, value, record):
        colors = {
            "pending": "info",
            "success": "success",
            "failed": "warning",
            "dead": "danger",
        }
        color = colors.get(value, "secondary")
        return format_html('<span class="badge bg-{}">{}</span>', color, record.get_status_display())

    def render_response_code(self, value):
        return value if value is not None else format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)

    def render_error_message(self, value):
        if not value:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)
        truncated = Truncator(str(value)).chars(80)
        return format_html('<span title="{}">{}</span>', value, truncated)

    def render_test_send(self, value):
        if value:
            return format_html('<span class="badge bg-info">{}</span>', _("Test"))
        return format_html('<span class="text-muted">{}</span>', _("No"))


# =============================================================================
# Alerting Tables
# =============================================================================


class AlertRuleTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.Column(linkify=True)
    alert_type = tables.Column(verbose_name=_("Alert Type"))
    threshold_value = tables.Column(verbose_name=_("Threshold"))
    severity = tables.Column()
    is_active = BooleanColumn()
    is_muted = BooleanColumn(verbose_name=_("Muted"))
    tenant = tables.Column(verbose_name=_("Tenant"), accessor="tenant.name", linkify=False)
    actions = tables.TemplateColumn(
        template_code="""
        <div class="d-flex gap-1 justify-content-end">
            <form method="post" action="{% url 'extras:alertrule_run' record.pk %}" class="d-inline">
                {% csrf_token %}
                <input type="hidden" name="return_url" value="{{ request.get_full_path }}">
                <button type="submit"
                        class="btn btn-sm btn-outline-primary btn-icon"
                        title="{{ run_now }}"
                        aria-label="{{ run_now }}">
                    <i class="mdi mdi-play-circle-outline"></i>
                </button>
            </form>
            <a class="btn btn-sm btn-outline-secondary btn-icon"
               href="{% url 'extras:alertrule_update' record.pk %}"
               title="{{ edit }}"
               aria-label="{{ edit }}">
                <i class="mdi mdi-pencil-outline"></i>
            </a>
        </div>
        """,
        extra_context={"run_now": _("Run now"), "edit": _("Edit")},
        verbose_name=_("Actions"),
        orderable=False,
        attrs={
            "th": {"class": "col-actions text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions"},
        },
    )

    class Meta(BaseTable.Meta):
        model = AlertRule
        fields = (
            "pk",
            "name",
            "alert_type",
            "threshold_value",
            "severity",
            "is_active",
            "is_muted",
            "tenant",
            "actions",
        )
        sequence = (
            "pk",
            "name",
            "alert_type",
            "threshold_value",
            "severity",
            "is_active",
            "is_muted",
            "tenant",
            "actions",
        )

    def render_severity(self, value, record):
        color = "secondary"
        if value == AlertRule.SEVERITY_INFO:
            color = "info"
        elif value == AlertRule.SEVERITY_WARNING:
            color = "warning"
        elif value == AlertRule.SEVERITY_CRITICAL:
            color = "danger"
        return format_html('<span class="badge bg-{}">{}</span>', color, record.get_severity_display())


class NotificationChannelTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.Column(linkify=False)
    channel_type = tables.Column(verbose_name=_("Channel Type"))
    enabled = BooleanColumn()
    tenant = tables.Column(verbose_name=_("Tenant"), accessor="tenant.name", linkify=False)
    actions = tables.TemplateColumn(
        template_code="""
        <div class="d-flex gap-1 justify-content-end">
            <form method="post" action="{% url 'extras:notificationchannel_test' record.pk %}" class="d-inline">
                {% csrf_token %}
                <button type="submit"
                        class="btn btn-sm btn-outline-info btn-icon"
                        title="{{ send_test_notification }}"
                        aria-label="{{ send_test_notification }}">
                    <i class="mdi mdi-send-outline"></i>
                </button>
            </form>
            <a class="btn btn-sm btn-outline-secondary btn-icon"
               href="{% url 'extras:notificationchannel_update' record.pk %}"
               title="{{ edit }}"
               aria-label="{{ edit }}">
                <i class="mdi mdi-pencil-outline"></i>
            </a>
            <a class="btn btn-sm btn-outline-danger btn-icon"
               href="{% url 'extras:notificationchannel_delete' record.pk %}"
               title="{{ delete }}"
               aria-label="{{ delete }}">
                <i class="mdi mdi-trash-can-outline"></i>
            </a>
        </div>
        """,
        extra_context={
            "send_test_notification": _("Send test notification"),
            "edit": _("Edit"),
            "delete": _("Delete"),
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

    class Meta(BaseTable.Meta):
        model = NotificationChannel
        fields = ("pk", "name", "channel_type", "enabled", "tenant", "actions")
        sequence = ("pk", "name", "channel_type", "enabled", "tenant", "actions")


class AlertLogTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    created_at = tables.DateTimeColumn(verbose_name=_("Date"), format="Y-m-d H:i:s")
    rule = tables.Column(linkify=True)
    subject = tables.Column(linkify=False)
    severity = tables.Column()
    status = tables.Column()
    delivery = tables.Column(verbose_name=_("Delivery"), orderable=False, empty_values=(), accessor="delivery_status")
    actions = tables.TemplateColumn(
        template_code="""
        <div class="d-flex gap-1 justify-content-end">
            {% if record.status == 'active' %}
                <form method="post" action="{% url 'extras:alertlog_acknowledge' record.pk %}" class="d-inline">
                    {% csrf_token %}
                    <input type="hidden" name="return_url" value="{{ request.get_full_path }}">
                    <button type="submit" class="btn btn-sm btn-outline-warning" title="{{ acknowledge }}">
                        <i class="mdi mdi-eye-outline"></i>
                        {{ acknowledge }}
                    </button>
                </form>
            {% endif %}
            {% if record.status != 'resolved' %}
                <form method="post" action="{% url 'extras:alertlog_resolve' record.pk %}" class="d-inline">
                    {% csrf_token %}
                    <input type="hidden" name="return_url" value="{{ request.get_full_path }}">
                    <button type="submit" class="btn btn-sm btn-outline-success" title="{{ resolve }}">
                        <i class="mdi mdi-check"></i>
                        {{ resolve }}
                    </button>
                </form>
            {% endif %}
        </div>
        """,
        extra_context={"acknowledge": _("Acknowledge"), "resolve": _("Resolve")},
        verbose_name=_("Actions"),
        orderable=False,
        attrs={
            "th": {
                "class": "col-actions-wide text-nowrap",
            },
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
        },
    )

    class Meta(BaseTable.Meta):
        model = AlertLog
        fields = ("pk", "created_at", "rule", "subject", "severity", "status", "delivery", "actions")
        sequence = ("pk", "created_at", "rule", "subject", "severity", "status", "delivery", "actions")
        empty_text = _("All clear. No alerts match the current filters.")

    def render_severity(self, value, record):
        color = "secondary"
        if value == AlertRule.SEVERITY_INFO:
            color = "info"
        elif value == AlertRule.SEVERITY_WARNING:
            color = "warning"
        elif value == AlertRule.SEVERITY_CRITICAL:
            color = "danger"
        return format_html('<span class="badge bg-{}">{}</span>', color, record.get_severity_display())

    def render_status(self, value, record):
        color = "secondary"
        if value == AlertLog.STATUS_ACTIVE:
            color = "danger"
        elif value == AlertLog.STATUS_ACKNOWLEDGED:
            color = "warning"
        elif value == AlertLog.STATUS_RESOLVED:
            color = "success"
        return format_html('<span class="badge bg-{}">{}</span>', color, record.get_status_display())

    def render_delivery(self, record):
        outcome = record.delivery_outcome or AlertLog.DELIVERY_OUTCOME_NONE
        statuses = record.delivery_status or {}
        if outcome == AlertLog.DELIVERY_OUTCOME_NONE:
            if statuses.get("__no_channels__"):
                return format_html(
                    '<span class="badge bg-secondary" title="{}">{}</span>',
                    _("No channels attached to this rule"),
                    _("None"),
                )
            if statuses.get("__no_enabled_channels__"):
                return format_html(
                    '<span class="badge bg-secondary" title="{}">{}</span>',
                    _("Every attached channel is disabled"),
                    _("None"),
                )
            return format_html(
                '<span class="text-muted" title="{}">{}</span>',
                _("No delivery planned (muted rule or never dispatched)"),
                _("Not sent"),
            )
        if outcome == AlertLog.DELIVERY_OUTCOME_PENDING:
            return format_html('<span class="badge bg-info" title="{}">{}</span>', _("Dispatch pending"), _("Pending"))
        if outcome == AlertLog.DELIVERY_OUTCOME_DELIVERED:
            failed = _failed_channel_summary(statuses)
            if failed:
                return format_html(
                    '<span class="badge bg-warning text-dark" title="{}">{}</span>',
                    f"{_('Delivered with channel failures')}: {failed}",
                    _("Delivered with failures"),
                )
            return format_html(
                '<span class="badge bg-success" title="{}">{}</span>', _("All channels delivered"), _("Delivered")
            )
        failed = _failed_channel_summary(statuses) or _("Delivery failed")
        return format_html('<span class="badge bg-danger" title="{}">{}</span>', failed, _("Failed"))


def _failed_channel_summary(statuses):
    """Human summary of failed channel outcomes (structured or legacy payload)."""
    parts = []
    for key, value in statuses.items():
        if key.startswith("__"):
            continue
        if isinstance(value, dict):
            disposition = value.get("disposition")
            if disposition in ("retryable", "terminal"):
                parts.append(f"{key}: {value.get('error_class') or disposition}")
        elif isinstance(value, str) and value != "ok":
            parts.append(f"{key}: {value}")
    return "; ".join(parts)


# =============================================================================
# Reporting Tables
# =============================================================================


class ReportTemplateTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.Column(linkify=True)
    report_type = tables.Column(verbose_name=_("Type"))

    class Meta(BaseTable.Meta):
        model = ReportTemplate
        fields = ("pk", "name", "description", "report_type")
        sequence = ("pk", "name", "description", "report_type")


class ScheduledReportTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.Column(linkify=False)
    report = tables.Column(linkify=True)
    recipients = tables.Column()
    format = tables.Column()
    is_active = BooleanColumn()
    last_run = tables.DateTimeColumn(format="Y-m-d H:i:s")
    last_status = tables.Column()
    next_run = tables.Column(verbose_name=_("Next run"), orderable=False, empty_values=())
    scope = tables.Column(accessor="pk", verbose_name=_("Scope"), orderable=False, empty_values=())

    def render_next_run(self, record):
        """Show the registered next occurrence; dormant schedules have none."""
        schedule = record.schedule
        if schedule is None or schedule.next_run is None:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)
        return timezone.localtime(schedule.next_run).strftime("%Y-%m-%d %H:%M:%S")

    def render_scope(self, record, value):
        try:
            authorization = record.scope_authorization
        except ObjectDoesNotExist:
            authorization = None
        url = reverse("extras:scheduledreport_scope_approval", kwargs={"pk": record.pk})
        if authorization is None:
            if not record.scope_requires_authorization():
                return format_html('<span class="badge bg-secondary">{}</span>', _("Single tenant"))
            return format_html(
                '<span class="badge bg-warning">{}</span> <a href="{}">{}</a>',
                _("Approval required"),
                url,
                _("Approve"),
            )
        if authorization.is_revoked():
            return format_html(
                '<span class="badge bg-danger">{}</span> <a href="{}">{}</a>',
                _("Revoked"),
                url,
                _("Review"),
            )
        if not self._authorization_is_current(authorization, record):
            return format_html(
                '<span class="badge bg-danger">{}</span> <a href="{}">{}</a>',
                _("Scope changed"),
                url,
                _("Review"),
            )
        return format_html(
            '<span class="badge bg-success">{}</span> <a href="{}">{}</a>',
            _("Approved"),
            url,
            _("Manage"),
        )

    def _authorization_is_current(self, authorization, record):
        # Generation compares the stored snapshot against the LIVE scope
        # tenants; mirror that so a soft-deleted scope tenant reads as a scope
        # change instead of a green "Approved".
        Tenant = apps.get_model("organization", "Tenant")
        live_ids = sorted(
            Tenant._base_manager.filter(
                pk__in=record.effective_scope_tenant_ids(), deleted_at__isnull=True
            ).values_list("pk", flat=True)
        )
        return sorted(set(authorization.scope_tenant_ids)) == live_ids

    actions = tables.TemplateColumn(
        template_code="""
        <div class="d-flex gap-1 justify-content-end">
            <form method="post" action="{% url 'extras:scheduledreport_trigger' record.pk %}" class="d-inline">
                {% csrf_token %}
                <input type="hidden" name="return_url" value="{{ request.get_full_path }}">
                <button type="submit" class="btn btn-sm btn-outline-primary d-flex align-items-center" title="{{ run_now }}">
                    <i class="mdi mdi-play"></i>
                    <span class="ms-1 d-none d-md-inline">{{ run_now }}</span>
                </button>
            </form>
            {% if record.delivery_retryable %}
            <form method="post" action="{% url 'extras:scheduledreport_retry_delivery' record.pk %}" class="d-inline">
                {% csrf_token %}
                <input type="hidden" name="return_url" value="{{ request.get_full_path }}">
                <button type="submit" class="btn btn-sm btn-outline-warning d-flex align-items-center" title="{{ retry_delivery }}">
                    <i class="mdi mdi-refresh"></i>
                    <span class="ms-1 d-none d-md-inline">{{ retry_delivery }}</span>
                </button>
            </form>
            {% endif %}
            <a class="btn btn-sm btn-outline-secondary btn-icon"
               href="{% url 'extras:scheduledreport_update' record.pk %}"
               title="{{ edit }}">
                <i class="mdi mdi-pencil-outline"></i>
            </a>
            <a class="btn btn-sm btn-outline-danger btn-icon"
               href="{% url 'extras:scheduledreport_delete' record.pk %}"
               title="{{ delete }}">
                <i class="mdi mdi-trash-can-outline"></i>
            </a>
        </div>
        """,
        extra_context={
            "run_now": _("Run now"),
            "retry_delivery": _("Retry delivery"),
            "edit": _("Edit"),
            "delete": _("Delete"),
        },
        verbose_name=_("Actions"),
        orderable=False,
        attrs={
            "th": {
                "class": "col-actions-wide text-nowrap",
            },
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
        },
    )

    class Meta(BaseTable.Meta):
        model = ScheduledReport
        fields = (
            "pk",
            "name",
            "report",
            "recipients",
            "format",
            "is_active",
            "last_run",
            "last_status",
            "next_run",
            "scope",
            "actions",
        )
        sequence = (
            "pk",
            "name",
            "report",
            "recipients",
            "format",
            "is_active",
            "last_run",
            "last_status",
            "next_run",
            "scope",
            "actions",
        )
