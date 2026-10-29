import datetime
import json
import logging

from croniter import croniter
from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.utils.http import urlencode
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from assets.services.specification_consumers.contracts import FieldReference, parse_filter_document
from assets.services.specification_consumers.exporting import machine_csv_bytes
from assets.tables import AssetTable  # Import AssetTable
from core.events import send_notification_to_channel
from core.managers import Scope, get_current_tenant
from core.reports.rendering import render_report_csv, render_report_html
from core.schedules import (
    SCHEDULED_REPORT_FIRE_KWARG,
    SCHEDULED_REPORT_TASK_PATH,
    register_schedule,
    remove_schedule,
)
from extras.tasks.reports import delivery_ledger_message, generate_scheduled_report_task, retry_failed_deliveries
from itambox.panels import Panel
from itambox.utils import get_model_viewname  # Import the utility function
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from itambox.views.generic.mixins import CapabilityRequiredMixin, is_managed_definition
from itambox.views.generic.service_views import SimplePostView
from itambox.views.generic.utils import safe_return_url
from organization.models import Tenant

from .filters import (
    AlertLogFilterSet,
    AlertRuleFilterSet,
    CustomFieldFilterSet,
    CustomFieldsetFilterSet,
    NotificationChannelFilterSet,
    ReportTemplateFilterSet,
    SavedFilterFilterSet,
    ScheduledReportFilterSet,
    TagFilter,
)
from .forms import (
    AlertLogFilterForm,
    AlertRuleFilterForm,
    AlertRuleForm,
    CustomFieldFilterForm,
    CustomFieldForm,
    CustomFieldsetFilterForm,
    CustomFieldsetForm,
    NotificationChannelFilterForm,
    NotificationChannelForm,
    ReportTemplateFilterForm,
    ReportTemplateForm,
    SavedFilterFilterForm,
    SavedFilterForm,
    ScheduledReportFilterForm,
    ScheduledReportForm,
    TagFilterForm,
    TagForm,
)
from .models import (
    AlertLog,
    AlertRule,
    CustomField,
    CustomFieldset,
    NotificationChannel,
    ReportTemplate,
    SavedFilter,
    ScheduledReport,
    Tag,
)
from .tables import (
    AlertLogTable,
    AlertRuleTable,
    CustomFieldsetTable,
    CustomFieldTable,
    NotificationChannelTable,
    ReportTemplateTable,
    SavedFilterTable,
    ScheduledReportTable,
    TagTable,
)


class TagDetailView(ObjectDetailView):
    queryset = Tag.objects.all()
    template_name = "extras/tags/tag_detail.html"

    layout = (((Panel("info", _("Tag Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tag = self.object

        # Fetch related assets using the related_name from Asset.tags
        related_assets = tag.assets.all()

        # Create and configure the assets table
        assets_table = AssetTable(related_assets, request=self.request)
        # Disable pagination for related table
        assets_table.configure(self.request, paginate=False)

        context["assets_table"] = assets_table
        return context


class TagCreateView(ObjectEditView):
    model_form = TagForm
    template_name = "generic/object_edit.html"
    default_return_url = "extras:tag_list"


class TagUpdateView(ObjectEditView):
    queryset = Tag.objects.all()
    model_form = TagForm
    template_name = "generic/object_edit.html"
    default_return_url = "extras:tag_list"


class TagDeleteView(ObjectDeleteView):
    queryset = Tag.objects.all()
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("extras:tag_list")


# Refactor tag_list to CBV
class TagListView(ObjectListView):
    queryset = Tag.objects.all()
    filterset = TagFilter
    filterset_form = TagFilterForm  # Assuming TagFilterForm exists
    table = TagTable
    action_buttons = ("add",)  # Add create button
    template_name = "generic/object_list.html"  # Use base template


class TagBulkEditView(ObjectBulkEditView):
    queryset = Tag.objects.all()


class TagBulkDeleteView(ObjectBulkDeleteView):
    queryset = Tag.objects.all()


# Custom Fields
class ManagedDefinitionDetailView(ObjectDetailView):
    def _build_mutation_context(self, obj, app_label, model_name):
        context = super()._build_mutation_context(obj, app_label, model_name)
        if is_managed_definition(obj):
            context.update(can_change=False, can_delete=False, edit_url=None, delete_url=None)
        return context


class CustomFieldListView(ObjectListView):
    queryset = CustomField.objects.all()
    filterset = CustomFieldFilterSet
    filterset_form = CustomFieldFilterForm
    table = CustomFieldTable
    action_buttons = ("add",)


class CustomFieldDetailView(ManagedDefinitionDetailView):
    queryset = CustomField.objects.all()

    layout = (((Panel("info", _("Custom Field Details")),),),)


class CustomFieldEditView(ObjectEditView):
    queryset = CustomField.objects.all()
    model = CustomField
    model_form = CustomFieldForm
    template_name = "generic/object_edit.html"
    default_return_url = "extras:customfield_list"


class CustomFieldDeleteView(ObjectDeleteView):
    queryset = CustomField.objects.all()
    model = CustomField
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("extras:customfield_list")


class CustomFieldBulkEditView(ObjectBulkEditView):
    queryset = CustomField.objects.all()


class CustomFieldBulkDeleteView(ObjectBulkDeleteView):
    queryset = CustomField.objects.all()


# Custom Fieldsets
class CustomFieldsetListView(ObjectListView):
    queryset = CustomFieldset.objects.annotate(fields_count=Count("fields"))
    filterset = CustomFieldsetFilterSet
    filterset_form = CustomFieldsetFilterForm
    table = CustomFieldsetTable
    action_buttons = ("add",)


class CustomFieldsetDetailView(ManagedDefinitionDetailView):
    queryset = CustomFieldset.objects.all().prefetch_related("fields", "asset_type_memberships")

    layout = (((Panel("info", _("Custom Field Set Details")),),),)

    def get_object_display(self, obj):
        return obj.label or obj.slug


class CustomFieldsetEditView(ObjectEditView):
    queryset = CustomFieldset.objects.all()
    model = CustomFieldset
    model_form = CustomFieldsetForm
    template_name = "generic/object_edit.html"
    default_return_url = "extras:customfieldset_list"


class CustomFieldsetDeleteView(ObjectDeleteView):
    queryset = CustomFieldset.objects.all()
    model = CustomFieldset
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("extras:customfieldset_list")


class CustomFieldsetBulkEditView(ObjectBulkEditView):
    queryset = CustomFieldset.objects.all()


class CustomFieldsetBulkDeleteView(ObjectBulkDeleteView):
    queryset = CustomFieldset.objects.all()


# =============================================================================
# Saved Filters
# =============================================================================


class SavedFilterListView(ObjectListView):
    queryset = SavedFilter.objects.select_related("content_type", "tenant", "created_by")
    filterset = SavedFilterFilterSet
    filterset_form = SavedFilterFilterForm
    table = SavedFilterTable
    action_buttons = ("add",)
    template_name = "generic/object_list.html"


class SavedFilterDetailView(ObjectDetailView):
    queryset = SavedFilter.objects.all()

    layout = (((Panel("info", _("Saved Filter Details")),),),)


class SavedFilterEditView(ObjectEditView):
    queryset = SavedFilter.objects.all()
    model = SavedFilter
    model_form = SavedFilterForm
    template_name = "generic/object_edit.html"
    default_return_url = "extras:savedfilter_list"


class SavedFilterDeleteView(ObjectDeleteView):
    queryset = SavedFilter.objects.all()
    model = SavedFilter
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("extras:savedfilter_list")


class SavedFilterSaveView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Quick-save the current list-view filter as a named SavedFilter.

    POST-only. The list view's filter offcanvas hx-includes the filter form
    (``.filter-form-sidebar``), so the POST carries the filter fields' CURRENT
    values (whether or not "Apply" was clicked) alongside the save controls.
    We persist those filter params and redirect back to the list with
    ``?filter=<new pk>`` so the freshly saved filter applies immediately.

    Save-control fields use an ``sf_`` prefix so they never collide with a
    filterset field of the same name (e.g. a model whose filter has ``name``).
    """

    permission_required = ("extras.add_savedfilter",)

    # POST keys that are save-form controls or list chrome, NOT filter params.
    NON_FILTER_PARAMS = frozenset(
        {
            "sf_name",
            "sf_shared",
            "sf_is_global",
            "model",
            "return_url",
            "csrfmiddlewaretoken",
            "page",
            "per_page",
            "sort",
            "deleted",
            "filter",
        }
    )

    def post(self, request, *args, **kwargs):
        name = (request.POST.get("sf_name") or "").strip()
        model_str = (request.POST.get("model") or "").strip()
        is_global = request.POST.get("sf_is_global") in ("1", "true", "on", "yes")
        shared = request.POST.get("sf_shared") in ("1", "true", "on", "yes")

        content_type = self._resolve_content_type(model_str)
        if not name or content_type is None:
            return self._respond(request, model_str, None, "Provide a name and a valid model to save the filter.")

        parameters = self._parse_parameters(request.POST)

        tenant = get_current_tenant()
        if is_global and request.user.is_superuser:
            tenant = None

        saved = SavedFilter.objects.create(
            name=name,
            content_type=content_type,
            parameters=parameters,
            shared=shared,
            created_by=request.user,
            tenant=tenant,
        )

        return self._respond(request, model_str, saved.pk, None)

    def _resolve_content_type(self, model_str):
        if "." not in model_str:
            return None
        app_label, model_name = model_str.split(".", 1)
        try:
            return ContentType.objects.get_by_natural_key(app_label, model_name)
        except ContentType.DoesNotExist:
            return None

    def _parse_parameters(self, post):
        """Filter params = POST minus control/chrome keys and empty values."""
        params = {}
        for key in post.keys():
            if key in self.NON_FILTER_PARAMS:
                continue
            values = [v for v in post.getlist(key) if v not in (None, "")]
            if not values:
                continue
            params[key] = values if len(values) > 1 else values[0]
        return params

    def _list_url(self, request, model_str):
        return_url = request.POST.get("return_url")
        if return_url:
            # Same-host only — guard against an attacker-supplied external return_url.
            return safe_return_url(request, return_url.split("?", 1)[0], reverse("extras:savedfilter_list"))
        content_type = self._resolve_content_type(model_str)
        if content_type is not None:
            model = content_type.model_class()
            if model is not None:
                try:
                    return reverse(get_model_viewname(model, "list"))
                except Exception:
                    pass
        return reverse("extras:savedfilter_list")

    def _respond(self, request, model_str, pk, error):
        """Redirect to the list (with ?filter=<pk> on success). HTMX submissions
        get a 204 + HX-Redirect so the browser performs a full navigation and the
        list's ?filter load hook re-applies the saved filter."""
        list_url = self._list_url(request, model_str)
        target = f"{list_url}?{urlencode({'filter': pk})}" if pk else list_url
        if error:
            messages.error(request, error)
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return redirect(target)


# =============================================================================
# Alerting Views
# =============================================================================


logger = logging.getLogger(__name__)


@method_decorator(login_required, name="dispatch")
class AlertRuleListView(ObjectListView):
    queryset = AlertRule.objects.all()
    filterset = AlertRuleFilterSet
    filterset_form = AlertRuleFilterForm
    table = AlertRuleTable
    template_name = "core/alerts/alert_rule_list.html"
    action_buttons = ("add",)

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Alert Rules"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Alert Rules")
        return context


@method_decorator(login_required, name="dispatch")
class AlertRuleDetailView(ObjectDetailView):
    queryset = AlertRule.objects.all()
    template_name = "core/alerts/alert_rule_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        context["title"] = _("Alert Rule: %(name)s") % {"name": obj.name}
        context["logs_count"] = obj.logs.count()
        context["active_logs_count"] = obj.logs.filter(status="active").count()
        return context


@method_decorator(login_required, name="dispatch")
class AlertRuleCreateView(ObjectEditView):
    queryset = AlertRule.objects.all()
    model_form = AlertRuleForm
    template_name = "core/alerts/alert_rule_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Create Alert Rule")
        return context


@method_decorator(login_required, name="dispatch")
class AlertRuleUpdateView(ObjectEditView):
    queryset = AlertRule.objects.all()
    model_form = AlertRuleForm
    template_name = "core/alerts/alert_rule_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Alert Rule: %(name)s") % {"name": self.object.name}
        return context


@method_decorator(login_required, name="dispatch")
class AlertRuleDeleteView(ObjectDeleteView):
    queryset = AlertRule.objects.all()
    template_name = "core/alerts/alert_rule_confirm_delete.html"


class AlertRuleBulkDeleteView(ObjectBulkDeleteView):
    queryset = AlertRule.objects.all()


class AlertRuleRunNowView(SimplePostView):
    """Evaluate a single alert rule immediately, on demand.

    The evaluation is enqueued as a background task rather than run inline:
    run_alert_rule_now() deliberately clears the tenant, membership and current-
    user contextvars without restoring them (it is designed to run standalone in
    a worker), so running it inside the request would contaminate the request's
    context for the remainder of the response.
    """

    queryset = AlertRule.objects.all()
    permission_required = ("extras.change_alertrule",)

    def perform_action(self, rule, request):
        from django_q.tasks import async_task

        rule_id = rule.pk
        async_task("extras.tasks.alerts.run_alert_rule_now", rule_id)
        return {"message": f"Evaluation queued for '{rule.name}'. New alerts will appear shortly."}

    def get_success_redirect(self, obj, result):
        return redirect(
            safe_return_url(
                self.request,
                self.request.POST.get("return_url"),
                reverse("extras:alertrule_detail", kwargs={"pk": obj.pk}),
            )
        )


@method_decorator(login_required, name="dispatch")
class AlertLogListView(ObjectListView):
    queryset = (
        AlertLog.objects.filter(tenant__isnull=False).select_related("rule", "content_type").order_by("-created_at")
    )
    table = AlertLogTable
    template_name = "core/alerts/alert_list.html"
    action_buttons = ()
    filterset = AlertLogFilterSet
    filterset_form = AlertLogFilterForm

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Alerts Center"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Alerts Center")

        from core.managers import get_current_tenant

        current_tenant = get_current_tenant()

        active_qs = AlertLog.objects.filter(tenant__isnull=False, status=AlertLog.STATUS_ACTIVE)
        acknowledged_qs = AlertLog.objects.filter(tenant__isnull=False, status=AlertLog.STATUS_ACKNOWLEDGED)

        if current_tenant:
            active_qs = active_qs.filter(tenant=current_tenant)
            acknowledged_qs = acknowledged_qs.filter(tenant=current_tenant)

        context["active_alerts_count"] = active_qs.count()
        context["acknowledged_alerts_count"] = acknowledged_qs.count()
        return context


class _TenantBoundAlertActionMixin:
    # Keep superuser single-alert mutations fail-closed without an active tenant.

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_superuser and get_current_tenant() is None:
            return queryset.none()
        return queryset


class AlertAcknowledgeView(_TenantBoundAlertActionMixin, SimplePostView):
    queryset = AlertLog.objects.filter(tenant__isnull=False)
    permission_required = ("extras.change_alertlog",)

    def perform_action(self, alert, request):
        if alert.status == AlertLog.STATUS_ACTIVE:
            alert.status = AlertLog.STATUS_ACKNOWLEDGED
            alert.acknowledged_by = request.user
            alert.save(update_fields=["status", "acknowledged_by"])
        return {"message": f"Alert '{alert.subject}' acknowledged."}

    def get_success_redirect(self, obj, result):
        return redirect(
            safe_return_url(
                self.request,
                self.request.POST.get("return_url"),
                reverse("extras:alertlog_list"),
            )
        )


class AlertResolveView(_TenantBoundAlertActionMixin, SimplePostView):
    queryset = AlertLog.objects.filter(tenant__isnull=False)
    permission_required = ("extras.change_alertlog",)

    def perform_action(self, alert, request):
        if alert.status in [AlertLog.STATUS_ACTIVE, AlertLog.STATUS_ACKNOWLEDGED]:
            alert.status = AlertLog.STATUS_RESOLVED
            alert.resolved_by = request.user
            alert.resolved_at = timezone.now()
            alert.save(update_fields=["status", "resolved_by", "resolved_at"])
        return {"message": f"Alert '{alert.subject}' marked as resolved."}

    def get_success_redirect(self, obj, result):
        return redirect(
            safe_return_url(
                self.request,
                self.request.POST.get("return_url"),
                reverse("extras:alertlog_list"),
            )
        )


class _BulkAlertActionView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Apply a status transition to many AlertLogs selected in the Alert Center.

    Reads the checked ``pk`` list (gathered by batch-actions.ts) and transitions
    eligible logs. Tenant-scoped: AlertLog.objects only exposes the current
    tenant's logs, so a user can never act on another tenant's alerts.
    """

    permission_required = ("extras.change_alertlog",)
    hx_trigger = "tableRefreshRequired"
    eligible_statuses = ()

    def apply(self, queryset, user):
        raise NotImplementedError

    def success_message(self, count):
        raise NotImplementedError

    def post(self, request, *args, **kwargs):
        pks = request.POST.getlist("pk")
        return_url = safe_return_url(request, request.POST.get("return_url"), reverse("extras:alertlog_list"))

        if not pks:
            return self._respond(request, gettext("No alerts selected."), "warning", return_url)

        try:
            unique_pks = {int(value) for value in pks}
        except (TypeError, ValueError):
            unique_pks = set()
        with transaction.atomic():
            locked_qs = (
                AlertLog.objects.for_scope(Scope.current()).select_for_update().filter(pk__in=unique_pks).order_by("pk")
            )
            # A null-tenant row marked unresolved is never safe to mutate,
            # including for superusers: reconciliation has not established an
            # owner, so bulk actions must fail closed rather than guess.
            locked_qs = locked_qs.exclude(tenant__isnull=True, tenant_resolution_status="unresolved")
            current_tenant = get_current_tenant()
            if request.user.is_superuser and current_tenant is None:
                # TenantScopingManager intentionally gives superusers a global
                # queryset without a scope. A bulk mutation must never use that
                # global path: without an active tenant, fail closed.
                locked_qs = locked_qs.none()
            elif not request.user.is_superuser:
                locked_qs = locked_qs.filter(tenant__isnull=False)
            locked_alerts = list(locked_qs)
            # Materialize the locked rows before comparing the selection. Django
            # strips FOR UPDATE from aggregate COUNT queries, so COUNT() cannot
            # prove all-or-safe semantics under READ COMMITTED.
            if not unique_pks or len(locked_alerts) != len(unique_pks):
                return self._respond(
                    request,
                    gettext("The selection contains an alert that is not accessible in the active tenant."),
                    "danger",
                    return_url,
                )
            eligible = locked_alerts
            if self.eligible_statuses:
                eligible = [alert for alert in locked_alerts if alert.status in self.eligible_statuses]
            count = self.apply(eligible, request.user)
        return self._respond(request, self.success_message(count), "success", return_url)

    def _respond(self, request, message, level, return_url):
        if getattr(request, "htmx", False):
            resp = HttpResponse(status=204)
            resp["HX-Trigger"] = json.dumps(
                {
                    self.hx_trigger: None,
                    "showMessage": {"message": message, "level": level},
                }
            )
            return resp
        # Django messages has no Bootstrap-style ``danger`` helper.
        if level == "danger":
            level = "error"
        getattr(messages, level)(request, message)
        return redirect(return_url)


class AlertBulkAcknowledgeView(_BulkAlertActionView):
    eligible_statuses = (AlertLog.STATUS_ACTIVE,)

    def apply(self, queryset, user):
        count = 0
        for alert in queryset:
            alert.status = AlertLog.STATUS_ACKNOWLEDGED
            alert.acknowledged_by = user
            alert.save(update_fields=["status", "acknowledged_by"])
            count += 1
        return count

    def success_message(self, count):
        return ngettext("%(count)s alert acknowledged.", "%(count)s alerts acknowledged.", count) % {"count": count}


class AlertBulkResolveView(_BulkAlertActionView):
    eligible_statuses = (AlertLog.STATUS_ACTIVE, AlertLog.STATUS_ACKNOWLEDGED)

    def apply(self, queryset, user):
        count = 0
        for alert in queryset:
            alert.status = AlertLog.STATUS_RESOLVED
            alert.resolved_by = user
            alert.resolved_at = timezone.now()
            alert.save(update_fields=["status", "resolved_by", "resolved_at"])
            count += 1
        return count

    def success_message(self, count):
        return ngettext("%(count)s alert resolved.", "%(count)s alerts resolved.", count) % {"count": count}


@method_decorator(login_required, name="dispatch")
class NotificationChannelListView(ObjectListView):
    queryset = NotificationChannel.objects.all()
    filterset = NotificationChannelFilterSet
    filterset_form = NotificationChannelFilterForm
    table = NotificationChannelTable
    template_name = "core/alerts/notificationchannel_list.html"
    action_buttons = ("add",)

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Notification Channels"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Notification Channels")
        return context


@method_decorator(login_required, name="dispatch")
class NotificationChannelCreateView(ObjectEditView):
    queryset = NotificationChannel.objects.all()
    model_form = NotificationChannelForm
    template_name = "core/alerts/notificationchannel_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Create Notification Channel")
        return context


@method_decorator(login_required, name="dispatch")
class NotificationChannelUpdateView(ObjectEditView):
    queryset = NotificationChannel.objects.all()
    model_form = NotificationChannelForm
    template_name = "core/alerts/notificationchannel_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Notification Channel: %(name)s") % {"name": self.object.name}
        return context


@method_decorator(login_required, name="dispatch")
class NotificationChannelDeleteView(ObjectDeleteView):
    queryset = NotificationChannel.objects.all()
    template_name = "core/alerts/notificationchannel_confirm_delete.html"


class NotificationChannelBulkDeleteView(ObjectBulkDeleteView):
    queryset = NotificationChannel.objects.all()


class NotificationChannelTestView(SimplePostView):
    """Send a test notification through a channel and report success/failure inline."""

    queryset = NotificationChannel.objects.all()
    permission_required = ("extras.change_notificationchannel",)

    def perform_action(self, channel, request):

        ok = send_notification_to_channel(
            channel,
            subject=str(_("ITAMbox Test Notification")),
            body=_("This is a test message sent to channel '%(name)s' (%(type)s).")
            % {
                "name": channel.name,
                "type": channel.get_channel_type_display(),
            },
        )
        if ok:
            return {"message": f"Test notification sent successfully via '{channel.name}'."}
        raise Exception(f"Channel '{channel.name}' returned a delivery failure.")

    def get_success_redirect(self, obj, result):
        return redirect(reverse("extras:notificationchannel_list"))


# =============================================================================
# Reporting Views
# =============================================================================

#: The report designer routes below edit, preview, render, or schedule a
#: ReportTemplate. Both the designer and the scheduled-report capability are
#: Stable (always-on), so those routes are open by contract; each surface's
#: capability gate stays wired so the routes and the published registry state
#: cannot drift apart. The Stable curated report catalogue
#: (`reporting.curated`) remains independent.
REPORT_DESIGNER_CAPABILITY = "reporting.designer"
REPORTING_SCHEDULED_CAPABILITY = "reporting.scheduled"


@method_decorator(login_required, name="dispatch")
class ReportTemplateListView(CapabilityRequiredMixin, ObjectListView):
    capability_key = REPORT_DESIGNER_CAPABILITY
    queryset = ReportTemplate.objects.all()
    filterset = ReportTemplateFilterSet
    filterset_form = ReportTemplateFilterForm
    table = ReportTemplateTable
    template_name = "core/reports/report_template_list.html"
    action_buttons = ("add",)

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Report Templates"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Report Templates")
        return context


@method_decorator(login_required, name="dispatch")
class ReportTemplateDetailView(CapabilityRequiredMixin, ObjectDetailView):
    capability_key = REPORT_DESIGNER_CAPABILITY
    queryset = ReportTemplate.objects.all()
    template_name = "core/reports/report_template_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        context["title"] = _("Report Template: %(name)s") % {"name": obj.name}
        context["schedules"] = obj.schedules.all()
        return context


@method_decorator(login_required, name="dispatch")
class ReportTemplateCreateView(CapabilityRequiredMixin, ObjectEditView):
    capability_key = REPORT_DESIGNER_CAPABILITY
    queryset = ReportTemplate.objects.all()
    model_form = ReportTemplateForm
    template_name = "core/reports/report_template_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Create Report Template")
        return context


@method_decorator(login_required, name="dispatch")
class ReportTemplateUpdateView(CapabilityRequiredMixin, ObjectEditView):
    capability_key = REPORT_DESIGNER_CAPABILITY
    queryset = ReportTemplate.objects.all()
    model_form = ReportTemplateForm
    template_name = "core/reports/report_template_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Report Template: %(name)s") % {"name": self.object.name}
        return context


@method_decorator(login_required, name="dispatch")
class ReportTemplateDeleteView(CapabilityRequiredMixin, ObjectDeleteView):
    capability_key = REPORT_DESIGNER_CAPABILITY
    queryset = ReportTemplate.objects.all()
    template_name = "core/reports/report_template_confirm_delete.html"


class ReportTemplateBulkDeleteView(CapabilityRequiredMixin, ObjectBulkDeleteView):
    capability_key = REPORT_DESIGNER_CAPABILITY
    queryset = ReportTemplate.objects.all()


@method_decorator(login_required, name="dispatch")
class ScheduledReportListView(CapabilityRequiredMixin, ObjectListView):
    capability_key = REPORTING_SCHEDULED_CAPABILITY
    queryset = ScheduledReport.objects.select_related("report", "schedule").prefetch_related("scope_authorization")
    filterset = ScheduledReportFilterSet
    filterset_form = ScheduledReportFilterForm
    table = ScheduledReportTable
    template_name = "core/reports/report_list.html"
    action_buttons = ("add",)

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Scheduled Reports"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Scheduled Reports")
        context["templates"] = ReportTemplate.objects.for_scope(Scope.current()).all()
        return context


def _initial_next_run(sched_report, now=None):
    """Frozen V1 first-run anchoring for a schedule registration.

    With a start time, the next run is the next occurrence of that time of day
    (today if it is still ahead, else tomorrow). Without one, the schedule is
    due immediately. A custom cron expression without a start time anchors at
    the next cron occurrence instead. The django-q library owns every further
    advance (calendar-aware months/years, DST-stable local wall time); this
    helper only computes the anchor a (re)registration starts from.
    """
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    if sched_report.start_time is None:
        if sched_report.frequency == ScheduledReport.FREQUENCY_CRON and sched_report.cron_expression:
            try:
                # Cron expressions are interpreted in the project's local wall
                # time (matching the django-q scheduler), so anchor against the
                # local representation of ``now``.
                return croniter(sched_report.cron_expression, local_now).get_next(datetime.datetime)
            except (KeyError, ValueError):
                # An invalid expression is rejected by the model/form; fall back
                # to the plain due-now anchor instead of failing registration.
                return now
        return now
    next_run = timezone.make_aware(
        datetime.datetime.combine(local_now.date(), sched_report.start_time), timezone.get_current_timezone()
    )
    if next_run < now:
        next_run += datetime.timedelta(days=1)
    return next_run


def handle_report_scheduling(sched_report, *, reanchor=True):
    """Sync the django-q registration row for one scheduled report.

    Registration is name-keyed (``scheduled_report_<pk>``), advisory-locked and
    idempotent: concurrent saves collapse onto a single row, duplicate rows
    left by the pre-promotion registration are removed, and the row always
    carries the ``intended_fire_at`` kwarg so the worker can claim each
    occurrence. Deactivating a schedule removes the registration and keeps the
    saved schedule and its records.

    ``reanchor`` controls the first-run anchor: creating or re-activating a
    schedule, or changing its frequency, cron expression, or start time,
    anchors the next run (see ``_initial_next_run``); editing anything else
    keeps the live next run. Without ``reanchor`` the row is only created
    (due immediately) if it went missing.
    Django-q registration is only needed when a schedule actually saves; the
    library import stays local so view import time is unaffected.
    """
    # inline import: app-registry: avoid AppRegistryNotReady at app-load time
    from django_q.models import Schedule

    schedule_name = f"scheduled_report_{sched_report.pk}"
    if not sched_report.is_active:
        if sched_report.schedule_id is not None:
            sched_report.schedule = None
            sched_report.save(update_fields=["schedule"])
        remove_schedule(SCHEDULED_REPORT_TASK_PATH, name=schedule_name)
        return

    # Map frequency choice to django-q Schedule type
    freq_mapping = {
        "once": Schedule.ONCE,
        "hourly": Schedule.HOURLY,
        "daily": Schedule.DAILY,
        "weekly": Schedule.WEEKLY,
        "biweekly": "BW",
        "monthly": Schedule.MONTHLY,
        "quarterly": "Q",
        "yearly": "Y",
        "cron": Schedule.CRON,
    }
    q_freq = freq_mapping.get(sched_report.frequency, Schedule.WEEKLY)

    defaults = {
        "args": str(sched_report.pk),
        "schedule_type": q_freq,
        "repeats": -1,
        "cron": sched_report.cron_expression if q_freq == Schedule.CRON else "",
        "intended_date_kwarg": SCHEDULED_REPORT_FIRE_KWARG,
    }
    if reanchor:
        defaults["next_run"] = _initial_next_run(sched_report)

    q_schedule = register_schedule(SCHEDULED_REPORT_TASK_PATH, name=schedule_name, defaults=defaults)
    if q_schedule is not None and sched_report.schedule_id != q_schedule.pk:
        sched_report.schedule = q_schedule
        sched_report.save(update_fields=["schedule"])


@method_decorator(login_required, name="dispatch")
class ScheduledReportCreateView(CapabilityRequiredMixin, ObjectEditView):
    capability_key = REPORTING_SCHEDULED_CAPABILITY
    queryset = ScheduledReport.objects.all()
    model_form = ScheduledReportForm
    template_name = "core/reports/report_schedule_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Schedule a Report")
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        handle_report_scheduling(self.object)
        return response


@method_decorator(login_required, name="dispatch")
class ScheduledReportUpdateView(CapabilityRequiredMixin, ObjectEditView):
    capability_key = REPORTING_SCHEDULED_CAPABILITY
    queryset = ScheduledReport.objects.all()
    model_form = ScheduledReportForm
    template_name = "core/reports/report_schedule_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Schedule: %(name)s") % {"name": self.object.name}
        return context

    def form_valid(self, form):
        # Decided before the save: anchoring the next run on every save would
        # silently move the cadence of an edited schedule. Only schedule-shape
        # changes (frequency, cron expression, start time) or a fresh
        # registration re-anchor; everything else keeps the live next run.
        previous = (
            ScheduledReport.objects.for_scope(Scope.current())
            .filter(pk=self.object.pk)
            .values("frequency", "cron_expression", "start_time", "is_active", "schedule_id")
            .first()
            or {}
        )
        schedule_shape_changed = any(
            previous.get(field) != getattr(self.object, field)
            for field in ("frequency", "cron_expression", "start_time")
        )
        reanchor = not previous.get("is_active") or previous.get("schedule_id") is None or schedule_shape_changed
        response = super().form_valid(form)
        handle_report_scheduling(self.object, reanchor=reanchor)
        return response


@method_decorator(login_required, name="dispatch")
class ScheduledReportDeleteView(CapabilityRequiredMixin, ObjectDeleteView):
    capability_key = REPORTING_SCHEDULED_CAPABILITY
    queryset = ScheduledReport.objects.all()
    template_name = "core/reports/report_schedule_confirm_delete.html"


class ScheduledReportBulkDeleteView(CapabilityRequiredMixin, ObjectBulkDeleteView):
    capability_key = REPORTING_SCHEDULED_CAPABILITY
    queryset = ScheduledReport.objects.all()


@method_decorator(login_required, name="dispatch")
class ScheduledReportScopeApprovalView(CapabilityRequiredMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    """Approve or revoke the durable cross-tenant scope approval of a schedule.

    Approval requires the same cross-tenant report permission the model gate
    enforces, and is refused when the acting principal's reach does not cover
    every tenant in scope — an ineffective approval would only delay the
    fail-closed ``report.scope_unauthorized`` terminal state to delivery time.
    """

    capability_key = REPORTING_SCHEDULED_CAPABILITY
    permission_required = ("reports.view_cross_tenant_reports",)
    template_name = "core/reports/report_schedule_scope_approval.html"

    def get_queryset(self):
        return (
            ScheduledReport.objects.for_scope(Scope.current())
            .select_related("report")
            .prefetch_related(
                "filter_tenants",
                "scope_authorization__authorized_by",
                "scope_authorization__revoked_by",
            )
        )

    def get_object(self):
        return get_object_or_404(self.get_queryset(), pk=self.kwargs.get("pk"))

    def _stored_authorization(self, sched):
        try:
            return sched.scope_authorization
        except ObjectDoesNotExist:
            return None

    def _scope_tenants(self, sched):
        Tenant = apps.get_model("organization", "Tenant")
        scope_tenant_ids = sched.effective_scope_tenant_ids()
        if not scope_tenant_ids:
            return []
        # Live tenants only: generation resolves the same way, so a
        # soft-deleted scope tenant must not read as approvable here.
        # unscoped: the stored scope tenant ids resolve to live tenants irrespective of the viewer's active scope
        return list(Tenant._base_manager.filter(pk__in=scope_tenant_ids, deleted_at__isnull=True).order_by("name"))

    def _principal_covers_scope(self, principal, scope_tenants):
        # An approval only takes effect when the principal holds the
        # cross-tenant permission on EVERY tenant in scope; delivery re-checks
        # this per tenant, so refuse to store an approval that cannot work.
        if not scope_tenants or not getattr(principal, "is_active", False):
            return False
        return all(principal.has_perm("reports.view_cross_tenant_reports", obj=tenant) for tenant in scope_tenants)

    def _approval_would_be_effective(self, sched, scope_tenants):
        return self._principal_covers_scope(self.request.user, scope_tenants)

    def get_context_data(self, **kwargs):
        sched = self.object = self.get_object()
        authorization = self._stored_authorization(sched)
        scope_tenants = self._scope_tenants(sched)
        # The page mirrors generation: the current scope is the LIVE tenant
        # set, so a soft-deleted scope tenant reads as a scope change, not as
        # "still in effect".
        scope_tenant_ids = sorted({tenant.pk for tenant in scope_tenants})
        authorization_is_current = bool(
            authorization
            and not authorization.is_revoked()
            and sorted(set(authorization.scope_tenant_ids)) == scope_tenant_ids
        )
        # Generation also fails closed when the stored authorizer lost reach or
        # became inactive; surface that state so the page answers the question
        # an operator actually opens it for.
        stored_approval_is_effective = bool(
            authorization_is_current and self._principal_covers_scope(authorization.authorized_by, scope_tenants)
        )
        return {
            "object": sched,
            "title": _("Scope Approval: %(name)s") % {"name": sched.name},
            "requires_authorization": sched.scope_requires_authorization(),
            "scope_tenants": scope_tenants,
            "authorization": authorization,
            "authorization_is_current": authorization_is_current,
            "stored_approval_is_effective": stored_approval_is_effective,
            "stored_scope_tenant_names": self._stored_scope_tenant_names(authorization),
            "approval_would_be_effective": self._approval_would_be_effective(sched, scope_tenants),
            "return_url": safe_return_url(
                self.request, self.request.GET.get("return_url"), reverse("extras:scheduledreport_list")
            ),
        }

    def _stored_scope_tenant_names(self, authorization):
        if authorization is None or not authorization.scope_tenant_ids:
            return []
        Tenant = apps.get_model("organization", "Tenant")
        # unscoped: stored approval scope is displayed as persisted, irrespective of the viewer's active scope
        tenants = Tenant._base_manager.filter(pk__in=authorization.scope_tenant_ids).order_by("name")
        by_pk = {tenant.pk: tenant.name for tenant in tenants}
        return [by_pk.get(pk, f"#{pk}") for pk in authorization.scope_tenant_ids]

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, self.get_context_data())

    def _error_redirect(self, request):
        return_url = safe_return_url(
            request,
            request.POST.get("return_url") or request.GET.get("return_url"),
            None,
        )
        target = reverse("extras:scheduledreport_scope_approval", kwargs={"pk": self.kwargs["pk"]})
        if return_url:
            target = f"{target}?{urlencode({'return_url': return_url})}"
        return redirect(target)

    def post(self, request, *args, **kwargs):
        # The model is resolved lazily: extending the module-level extras.models
        # import line would churn the flake8 E402 baseline identity for views.py.
        scope_authorization_model = apps.get_model("extras", "ScheduledReportScopeAuthorization")
        sched = self.object = self.get_object()
        action = request.POST.get("action")
        return_url = safe_return_url(request, request.POST.get("return_url"), reverse("extras:scheduledreport_list"))
        try:
            if action == "approve":
                self._approve_scope(sched, request, scope_authorization_model)
                messages.success(request, _("Cross-tenant scope of '%(name)s' approved.") % {"name": sched.name})
            elif action == "revoke":
                scope_authorization_model.revoke(sched, request.user)
                messages.success(
                    request, _("Cross-tenant scope approval of '%(name)s' revoked.") % {"name": sched.name}
                )
            else:
                messages.error(request, _("Unknown scope approval action."))
            return redirect(return_url)
        except PermissionDenied as error:
            messages.error(request, str(error) or _("You are not permitted to change this scope approval."))
            return self._error_redirect(request)
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
            return self._error_redirect(request)

    def _approve_scope(self, sched, request, scope_authorization_model):
        scope_tenant_ids = sched.effective_scope_tenant_ids()
        scope_tenants = self._scope_tenants(sched)
        if not scope_tenant_ids:
            raise ValidationError(
                _("This schedule has no tenant targets, so a cross-tenant approval cannot be stored.")
            )
        if len(scope_tenants) != len(set(scope_tenant_ids)):
            raise ValidationError(
                _("Some tenants in this scope are no longer available, so the approval would not take effect.")
            )
        if not self._approval_would_be_effective(sched, scope_tenants):
            missing = [
                tenant.name
                for tenant in scope_tenants
                if not request.user.has_perm("reports.view_cross_tenant_reports", obj=tenant)
            ]
            raise ValidationError(
                _(
                    "Your permission does not cover these tenants: %(tenants)s. "
                    "Update the scope or ask an authorized administrator to approve it."
                )
                % {"tenants": ", ".join(missing)}
            )
        # approve() snapshots the scope itself; atomically re-verify the
        # stored snapshot matches the scope we reach-checked, so a concurrent
        # scope edit cannot leave an unverified authorization behind.
        with transaction.atomic():
            authorization = scope_authorization_model.approve(sched, request.user)
            if sorted(set(authorization.scope_tenant_ids)) != sorted({tenant.pk for tenant in scope_tenants}):
                # The scope changed between the reach check and the snapshot.
                raise ValidationError(
                    _("The scope changed while approving; please review the scope and approve again.")
                )


@method_decorator(login_required, name="dispatch")
class ReportTriggerImmediateView(CapabilityRequiredMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    capability_key = REPORTING_SCHEDULED_CAPABILITY
    permission_required = ("extras.view_scheduledreport",)

    def has_permission(self):
        perms = self.get_permission_required()
        try:
            obj = get_object_or_404(ScheduledReport, pk=self.kwargs.get("pk"))
        except Http404:
            return False
        return self.request.user.has_perms(perms, obj=obj)

    def post(self, request, pk):
        sched = get_object_or_404(ScheduledReport, pk=pk)

        # Trigger report generation synchronously for immediate visual feedback in the UI
        success = generate_scheduled_report_task(sched.pk, invoked_by_user_id=request.user.pk)
        sched.refresh_from_db()
        status = sched.last_status or ""
        if status == "partial":
            messages.warning(
                request,
                _("Scheduled report '%(name)s' was generated but delivered only partially: %(error)s")
                % {"name": sched.name, "error": _last_delivery_detail(sched)},
            )
        elif status == "failed":
            messages.error(
                request,
                _("Scheduled report '%(name)s' was generated but all deliveries failed: %(error)s")
                % {"name": sched.name, "error": _last_delivery_detail(sched)},
            )
        elif success:
            messages.success(
                request, _("Scheduled report '%(name)s' generated and sent successfully.") % {"name": sched.name}
            )
        else:
            messages.error(
                request,
                _("Failed to generate scheduled report '%(name)s': %(error)s")
                % {"name": sched.name, "error": _last_run_failure_detail(sched)},
            )

        return redirect(
            safe_return_url(request, request.POST.get("return_url"), reverse("extras:scheduledreport_list"))
        )


def _last_delivery_detail(sched):
    """Render the last run's delivery outcome for operator messages.

    New rows carry a per-target ledger on the archive; older rows may still
    embed the detail in the status token, which stays readable.
    """
    archive = sched.archives.order_by("-generated_at").first()
    if archive is not None and archive.delivery_targets:
        return delivery_ledger_message(archive.delivery_targets)
    _kind, _separator, legacy_detail = (sched.last_status or "").partition(":")
    legacy_detail = legacy_detail.strip()
    return legacy_detail or _("Check logs.")


def _last_run_failure_detail(sched):
    """Render the last run's failure detail for operator messages."""
    status = sched.last_status or ""
    _kind, _separator, detail = status.partition(":")
    detail = detail.strip()
    if detail:
        return detail
    archive = sched.archives.order_by("-generated_at").first()
    if archive is not None and archive.error_message:
        return archive.error_message
    return status or _("Check logs.")


@method_decorator(login_required, name="dispatch")
class ScheduledReportRetryDeliveryView(CapabilityRequiredMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    """Recover a partially failed delivery without repeating successful sends.

    Redelivers only the failed targets of the newest archived run, after
    re-validating the archived generation scope and while the schedule is
    active. Mirrors the ``Run now`` gating: visible schedule plus the change
    permission.
    """

    capability_key = REPORTING_SCHEDULED_CAPABILITY
    permission_required = ("extras.change_scheduledreport",)

    def has_permission(self):
        perms = self.get_permission_required()
        try:
            obj = get_object_or_404(ScheduledReport, pk=self.kwargs.get("pk"))
        except Http404:
            return False
        return self.request.user.has_perms(perms, obj=obj)

    def post(self, request, pk):
        sched = get_object_or_404(ScheduledReport, pk=pk)
        outcome = retry_failed_deliveries(sched)
        if outcome.code == "retry.no_archive":
            messages.warning(
                request,
                _(
                    "Scheduled report '%(name)s' has no retained archived output to redeliver; "
                    "use Run now to generate it again."
                )
                % {"name": sched.name},
            )
        elif outcome.code == "retry.no_recorded_failures":
            messages.info(
                request,
                _("Scheduled report '%(name)s' has no recorded failed deliveries to retry.") % {"name": sched.name},
            )
        elif outcome.code == "retry.no_retained_output":
            messages.warning(
                request,
                _(
                    "Retry delivery for '%(name)s' could not read the retained archive output; "
                    "use Run now to generate it again."
                )
                % {"name": sched.name},
            )
        elif outcome.code == "retry.inactive":
            messages.error(
                request,
                _(
                    "Retry delivery for '%(name)s' was refused: the schedule is inactive; "
                    "reactivate it before retrying its delivery."
                )
                % {"name": sched.name},
            )
        elif outcome.code == "retry.in_progress":
            messages.info(
                request,
                _("Retry delivery for '%(name)s' is already in progress; the running attempt performs the deliveries.")
                % {"name": sched.name},
            )
        elif outcome.code == "retry.scope_unauthorized":
            messages.error(
                request,
                _(
                    "Retry delivery for '%(name)s' was refused: the cross-tenant scope of the archived "
                    "run is no longer covered by a current approval."
                )
                % {"name": sched.name},
            )
        elif outcome.code == "retry.completed":
            messages.success(
                request,
                _("Failed deliveries of scheduled report '%(name)s' were retried successfully.") % {"name": sched.name},
            )
        else:
            messages.warning(
                request,
                _("Retry delivery for '%(name)s' left targets failing: %(error)s")
                % {"name": sched.name, "error": outcome.detail or _("Check logs.")},
            )

        return redirect(
            safe_return_url(request, request.POST.get("return_url"), reverse("extras:scheduledreport_list"))
        )


def _specification_inputs(request, *, source):
    """Parse explicit report specification DTOs at the public view boundary."""
    raw_filters = source.get("specification_filters")
    if raw_filters:
        try:
            specification_filters = parse_filter_document(raw_filters)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"specification_filters": str(exc)}) from exc
    else:
        specification_filters = ()

    raw_references = source.get("specification_export_references")
    if not raw_references:
        return specification_filters, ()

    try:
        document = json.loads(raw_references)
        if isinstance(document, dict):
            if set(document) != {"references"}:
                raise ValueError("specification export references have unknown properties")
            document = document["references"]
        if not isinstance(document, list):
            raise ValueError("specification export references require a JSON list")
        references = tuple(
            FieldReference.from_column_id(item) if isinstance(item, str) else FieldReference.from_mapping(item)
            for item in document
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError({"specification_export_references": str(exc)}) from exc
    return specification_filters, references


@method_decorator(login_required, name="dispatch")
class ReportTemplatePreviewView(CapabilityRequiredMixin, PermissionRequiredMixin, View):
    capability_key = REPORT_DESIGNER_CAPABILITY
    permission_required = ()

    def has_permission(self):
        return self.request.user.has_perm("extras.add_reporttemplate") or self.request.user.has_perm(
            "extras.change_reporttemplate"
        )

    def post(self, request, *args, **kwargs):
        report_type = request.POST.get("report_type")
        style_preset = request.POST.get("style_preset", "default")
        included_columns = request.POST.getlist("included_columns")
        include_summary_cards = (
            request.POST.get("include_summary_cards") == "on" or request.POST.get("include_summary_cards") == "true"
        )
        include_distribution_chart = (
            request.POST.get("include_distribution_chart") == "on"
            or request.POST.get("include_distribution_chart") == "true"
        )
        group_by_field = request.POST.get("group_by_field", "")
        template_content = request.POST.get("template_content", "")
        description = request.POST.get("description", "")

        # Resolve active tenant for preview scoping
        selected_tenant_id = request.POST.get("tenant")
        active_tenant = None
        if selected_tenant_id and request.user.is_superuser:
            active_tenant = Tenant.objects.filter(pk=selected_tenant_id).first()
        else:
            from core.managers import get_current_tenant

            active_tenant = get_current_tenant()

        # Resolve multi-tenant filter scoping constellation for preview
        selected_filter_tenant_ids = request.POST.getlist("filter_tenants")
        filter_tenants = []
        if selected_filter_tenant_ids and request.user.is_superuser:
            filter_tenants = list(Tenant.objects.filter(pk__in=selected_filter_tenant_ids))

        # Create dynamic in-memory ReportTemplate object
        template_instance = ReportTemplate(
            name=request.POST.get("name", "Preview Report"),
            description=description,
            report_type=report_type,
            included_columns=included_columns,
            include_summary_cards=include_summary_cards,
            include_distribution_chart=include_distribution_chart,
            group_by_field=group_by_field,
            style_preset=style_preset,
            template_content=template_content,
        )

        # inline imports: heavy-import: report provider discovery is only needed for this preview request
        from core.reports import build_report_context

        try:
            specification_filters, specification_export_references = _specification_inputs(request, source=request.POST)
            template_instance.full_clean(validate_constraints=False)
            _headers, _rows, _summary_cards, _grouped_data, _chart_svg, context_data = build_report_context(
                template_instance,
                active_tenant=active_tenant,
                filter_tenants=filter_tenants,
                specification_filters=specification_filters,
                specification_export_references=specification_export_references,
            )

            context_data["request"] = request
            rendered_html = render_report_html(context_data, template_instance)

            return HttpResponse(rendered_html)
        except PermissionError:
            return HttpResponse(gettext("You may not view this report's data."), status=403)
        except ValidationError as exc:
            details = escape("; ".join(str(message) for message in exc.messages))
            return HttpResponse(
                f"<h3>{gettext('Invalid report template configuration.')}</h3><p>{details}</p>", status=400
            )
        except Exception:
            # Full detail (with traceback) goes to the server log; the client gets a
            # generic message so exception text is never reflected in the response.
            logger.exception("Template Render Error in preview")
            return HttpResponse(
                f"<h3>{gettext('Template render failed. See the server log for details.')}</h3>", status=400
            )


def _apply_report_disclosure_headers(response, context_data):
    """Expose the output-window facts on every report download response.

    Machine-format exports deliberately carry no in-file decorators; the
    HTTP response (and the scheduled mail body) state the window instead,
    and the published contract documents that machine exports cover the
    compiled window rather than the full population.
    """
    if context_data.get("truncated"):
        response["X-Report-Truncated"] = "true"
        response["X-Report-Row-Window"] = str(context_data.get("row_limit", ""))
        if context_data.get("total_rows") is not None:
            response["X-Report-Total-Rows"] = str(context_data["total_rows"])
    if context_data.get("is_sample"):
        response["X-Report-Sample"] = "true"
    return response


@method_decorator(login_required, name="dispatch")
class ReportTemplateDownloadView(CapabilityRequiredMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    capability_key = REPORT_DESIGNER_CAPABILITY
    permission_required = ("extras.view_reporttemplate",)

    def has_permission(self):
        perms = self.get_permission_required()
        try:
            obj = get_object_or_404(ReportTemplate, pk=self.kwargs.get("pk"))
        except Http404:
            return False
        return self.request.user.has_perms(perms, obj=obj)

    def get(self, request, pk, *args, **kwargs):
        # objects automatically handles tenant scoping!
        template = get_object_or_404(ReportTemplate.objects.for_scope(Scope.current()).all(), pk=pk)

        # Enforce multi-tenant thread-local active tenant binding
        from core.managers import get_current_tenant

        active_tenant = get_current_tenant()

        # inline imports: heavy-import: report provider discovery is only needed for this export request
        from core.reports import build_report_context

        try:
            filter_tenants = self._persisted_scope_tenants(template)
            specification_filters, specification_export_references = _specification_inputs(request, source=request.GET)
            headers, rows, _summary_cards, _grouped_data, _chart_svg, context_data = build_report_context(
                template,
                active_tenant=active_tenant,
                filter_tenants=filter_tenants,
                specification_filters=specification_filters,
                specification_export_references=specification_export_references,
            )

            format_type = request.GET.get("format", "html").lower()
            from core.csv_utils import safe_csv_filename

            safe_name = safe_csv_filename(template.name).lower().replace(" ", "_")
            stamp = f"{timezone.now():%Y%m%d}"

            machine_response = self._machine_format_response(format_type, context_data, safe_name, stamp)
            if machine_response is not None:
                return machine_response

            if format_type == "csv":
                response = HttpResponse(
                    render_report_csv(
                        headers,
                        rows,
                        disclosure_text=context_data.get("disclosure_text", ""),
                    ),
                    content_type="text/csv",
                )
                response["Content-Disposition"] = f'attachment; filename="{safe_name}_{stamp}.csv"'
                return _apply_report_disclosure_headers(response, context_data)

            if format_type == "xlsx":
                from core.reports.exporters import XLSX_MIME, report_xlsx_bytes

                machine_export = context_data.get("specification_export")
                export_headers = machine_export.columns if machine_export is not None else headers
                export_rows = machine_export.rows if machine_export is not None else rows
                response = HttpResponse(
                    report_xlsx_bytes(
                        export_headers,
                        export_rows,
                        sheet_title=template.name,
                        disclosure_text=context_data.get("disclosure_text", ""),
                    ),
                    content_type=XLSX_MIME,
                )
                response["Content-Disposition"] = f'attachment; filename="{safe_name}_{stamp}.xlsx"'
                return _apply_report_disclosure_headers(response, context_data)

            # HTML render — shared by the html and pdf formats.
            context_data["request"] = request
            rendered_html = render_report_html(context_data, template)

            if format_type == "pdf":
                from core.reports.exporters import PDF_MIME, report_pdf_bytes

                response = HttpResponse(report_pdf_bytes(rendered_html), content_type=PDF_MIME)
                disposition = "inline" if request.GET.get("print") == "true" else "attachment"
                response["Content-Disposition"] = f'{disposition}; filename="{safe_name}_{stamp}.pdf"'
                return _apply_report_disclosure_headers(response, context_data)

            response = HttpResponse(rendered_html, content_type="text/html")
            disposition = "inline" if request.GET.get("print") == "true" else "attachment"
            response["Content-Disposition"] = f'{disposition}; filename="{safe_name}_{stamp}.html"'
            return _apply_report_disclosure_headers(response, context_data)
        except PermissionError:
            return HttpResponse(gettext("You may not view this report's data."), status=403)
        except Exception:
            # Full detail (with traceback) goes to the server log; the client gets a
            # generic message so exception text is never reflected in the response.
            logger.exception("Template Render Error in download")
            return HttpResponse(
                f"<h3>{gettext('Template render failed. See the server log for details.')}</h3>", status=400
            )

    def _persisted_scope_tenants(self, template):
        """Resolve the persisted constellation through unscoped reads.

        The ambient tenant would silently truncate the pinned scope and a
        truncated constellation is a different report. A partially deleted
        constellation fails closed instead of silently compiling a different
        population, mirroring the scheduled-report scope resolver: the
        persisted constellation is the report's identity, so dropping one
        pinned tenant would change the requested reporting population. Raises
        ``PermissionError`` for the caller's 403 branch.
        """
        pinned_ids = template.persisted_filter_tenant_ids()
        if not pinned_ids:
            return []
        # inline import: heavy-import: organization models are only needed for tenant resolution
        from django.apps import apps as django_apps

        Tenant = django_apps.get_model("organization", "Tenant")
        # unscoped: persisted report scope is compiled as stored; authorization is checked by the caller
        filter_tenants = list(Tenant._base_manager.filter(pk__in=pinned_ids, deleted_at__isnull=True).order_by("pk"))
        if len(filter_tenants) != len(set(pinned_ids)):
            logger.error(
                "Report template scope tenants are soft-deleted; refusing compilation",
                extra={"operation": "reports.download", "reporttemplate_id": template.pk},
            )
            raise PermissionError("Report template scope tenants are soft-deleted")
        return filter_tenants

    def _machine_format_response(self, format_type, context_data, safe_name, stamp):
        """Machine-format downloads: the provider's own export bytes, undecorated.

        ``machine_csv`` is a machine contract and requires the provider export
        (400 without one); ``csv`` keeps returning the machine bytes whenever
        the provider built an export. Disclosure travels in the response
        headers instead of the file body so the machine bytes stay
        byte-stable. Returns ``None`` when the format is not a machine format.
        """
        machine_export = context_data.get("specification_export")
        if format_type == "machine_csv":
            if machine_export is None:
                return HttpResponse(gettext("This report does not provide a machine-format export."), status=400)
            return self._attachment_response(machine_csv_bytes(machine_export), safe_name, stamp, context_data)
        if format_type == "csv" and machine_export is not None:
            return self._attachment_response(machine_csv_bytes(machine_export), safe_name, stamp, context_data)
        return None

    def _attachment_response(self, payload, safe_name, stamp, context_data):
        response = HttpResponse(payload, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{safe_name}_{stamp}.csv"'
        return _apply_report_disclosure_headers(response, context_data)
