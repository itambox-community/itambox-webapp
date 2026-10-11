"""Webhook endpoint and delivery presentation owned by extras."""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.generic import View
from django_tables2 import RequestConfig

from core.events import _send_slack_notification, _send_teams_notification
from core.http import request_pinned, webhook_target_kind
from core.managers import Scope
from core.validators import validate_external_url
from core.worker_status import get_worker_status
from extras.forms import WebhookEndpointForm
from extras.models import WebhookDelivery, WebhookEndpoint
from extras.tables import WebhookDeliveryTable, WebhookEndpointTable
from extras.tasks.webhooks import redeliver_webhook_delivery, send_webhook_test
from itambox.panels import Panel
from itambox.views.generic import ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView
from itambox.views.generic.utils import safe_return_url

logger = logging.getLogger(__name__)


def _webhook_deliveries_visible_to(user):
    if user.is_superuser:
        # unscoped: only the explicit platform principal sees all deliveries, including tenant=None history
        return WebhookDelivery._base_manager
    return WebhookDelivery.objects.for_scope(Scope.current()).filter(tenant__isnull=False)


class WorkerStatusContextMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["worker_status"] = get_worker_status()
        return context


@method_decorator(login_required, name="dispatch")
class WebhookEndpointListView(WorkerStatusContextMixin, ObjectListView):
    queryset = WebhookEndpoint.objects.all()
    table = WebhookEndpointTable
    template_name = "generic/object_list.html"
    action_buttons = ("add",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Webhook Endpoints")
        context["is_beta_module"] = True
        return context


@method_decorator(login_required, name="dispatch")
class WebhookEndpointDetailView(WorkerStatusContextMixin, ObjectDetailView):
    queryset = WebhookEndpoint.objects.all()
    layout = (((Panel("info", _("Webhook Endpoint Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        endpoint = self.get_object()
        can_change = self.request.user.has_perm("extras.change_webhookendpoint", obj=endpoint)
        deliveries = (
            _webhook_deliveries_visible_to(self.request.user)
            .filter(endpoint=endpoint)
            .select_related("event", "redelivered_by")
            .order_by("-created_at")
        )
        delivery_table = WebhookDeliveryTable(
            deliveries,
            request=self.request,
            can_redeliver=can_change,
        )
        RequestConfig(self.request, paginate=False).configure(delivery_table)
        context["webhook_delivery_table"] = delivery_table
        context["can_change_webhook"] = can_change
        context["title"] = str(endpoint)
        return context


@method_decorator(login_required, name="dispatch")
class WebhookEndpointTestView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        endpoint = get_object_or_404(WebhookEndpoint.objects.for_scope(Scope.current()).all(), pk=kwargs["pk"])
        if not request.user.has_perm("extras.change_webhookendpoint", obj=endpoint):
            raise PermissionDenied

        try:
            send_webhook_test(endpoint.pk, actor_id=request.user.pk)
        except Exception:  # broad except: render-degrade: show a safe queue failure message
            logger.exception("Webhook test-send failed for endpoint %s", endpoint.pk)
            messages.error(request, _("The test webhook could not be queued."))
        else:
            messages.success(request, _("Test webhook queued."))
        return redirect(endpoint.get_absolute_url())


@method_decorator(login_required, name="dispatch")
class WebhookDeliveryRedeliverView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        delivery = get_object_or_404(
            _webhook_deliveries_visible_to(request.user).select_related("endpoint").all(),
            pk=kwargs["pk"],
        )
        endpoint = delivery.endpoint
        if endpoint is None:
            raise Http404
        if not request.user.has_perm("extras.change_webhookendpoint", obj=endpoint):
            raise PermissionDenied

        if delivery.status == "pending" or (
            delivery.next_retry_at is not None and delivery.next_retry_at > timezone.now()
        ):
            messages.error(request, _("Delivery is still in progress."))
            return redirect(endpoint.get_absolute_url())

        try:
            redeliver_webhook_delivery(delivery.pk, actor_id=request.user.pk)
        except ValidationError as exc:
            if any(str(message) == "Delivery is still in progress." for message in exc.messages):
                messages.error(request, _("Delivery is still in progress."))
            else:
                logger.exception("Webhook redelivery validation failed for delivery %s", delivery.pk)
                messages.error(request, _("The webhook delivery could not be redelivered."))
        except Exception:  # broad except: render-degrade: show a safe redelivery failure message
            logger.exception("Webhook redelivery failed for delivery %s", delivery.pk)
            messages.error(request, _("The webhook delivery could not be redelivered."))
        else:
            messages.success(request, _("Webhook delivery redelivered."))
        return redirect(endpoint.get_absolute_url())


@method_decorator(login_required, name="dispatch")
class WebhookEndpointEditView(ObjectEditView):
    queryset = WebhookEndpoint.objects.all()
    model_form = WebhookEndpointForm

    def post(self, request, *args, **kwargs):
        if "_test" in request.POST:
            self.object = self.get_object() if "pk" in self.kwargs else None
            return self._test_webhook(request)
        return super().post(request, *args, **kwargs)

    def _test_webhook(self, request):
        self_url = safe_return_url(request, request.get_full_path(), reverse("extras:webhookendpoint_list"))
        url = request.POST.get("url", "")
        if not url:
            messages.error(request, _("No URL configured."))
            return redirect(self_url)

        try:
            validate_external_url(url)
        except ValidationError as exc:
            messages.error(request, _("Webhook test blocked: %(reason)s") % {"reason": "; ".join(exc.messages)})
            return redirect(self_url)

        success = False
        try:
            test_payload = str(_("Test notification from ITAMbox"))
            test_title = str(_("ITAMbox Test"))
            target_kind = webhook_target_kind(url)
            if target_kind == "slack":
                success = _send_slack_notification(url, test_payload, test_title)
            elif target_kind == "teams":
                success = _send_teams_notification(url, test_payload, test_title)
            else:
                response = request_pinned(
                    "POST",
                    url,
                    json={"test": True, "message": test_payload},
                    timeout=10,
                )
                success = response.status_code < 400
        except Exception as exc:  # broad except: render-degrade: keep transport failures on the edit form
            messages.error(request, _("Test failed: %(error)s") % {"error": exc})
        if success:
            messages.success(request, _("Webhook test succeeded!"))
        else:
            messages.error(request, _("Webhook test failed."))
        return redirect(self_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Webhook Endpoint") if self.object else _("Create Webhook Endpoint")
        return context


@method_decorator(login_required, name="dispatch")
class WebhookEndpointDeleteView(ObjectDeleteView):
    queryset = WebhookEndpoint.objects.all()
