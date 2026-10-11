from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from extras.api.definition_writes import CustomFieldsetWriteMixin, CustomFieldWriteMixin
from extras.filters import (
    AlertLogFilterSet,
    AlertRuleFilterSet,
    CustomFieldFilterSet,
    CustomFieldsetFilterSet,
    EventRuleFilterSet,
    JournalEntryFilterSet,
    NotificationChannelFilterSet,
    TagFilter,
    WebhookDeliveryFilterSet,
    WebhookEndpointFilterSet,
)
from extras.models import (
    AlertLog,
    AlertRule,
    CustomField,
    CustomFieldset,
    Dashboard,
    EventRule,
    JournalEntry,
    NotificationChannel,
    Tag,
    WebhookDelivery,
    WebhookEndpoint,
)
from extras.tasks.webhooks import redeliver_webhook_delivery, send_webhook_test
from itambox.api.permissions import StrictTenantPermission, TokenPermissions
from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet

from .serializers import (
    AlertLogSerializer,
    AlertRuleSerializer,
    CustomFieldSerializer,
    CustomFieldsetSerializer,
    DashboardSerializer,
    EventRuleSerializer,
    JournalEntrySerializer,
    NotificationChannelSerializer,
    TagSerializer,
    WebhookDeliveryActionSerializer,
    WebhookDeliverySerializer,
    WebhookEndpointSerializer,
)


class WebhookEndpointActionPermissions(TokenPermissions):
    """Use endpoint-change permission for the endpoint's test-send action."""

    def get_required_permissions(self, method, model):
        if getattr(self, "_current_action", None) == "test":
            return [f"{model._meta.app_label}.change_{model._meta.model_name}"]
        return super().get_required_permissions(method, model)

    def has_permission(self, request, view):
        self._current_action = getattr(view, "action", None)
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        self._current_action = getattr(view, "action", None)
        return super().has_object_permission(request, view, obj)


class WebhookDeliveryPermissions(TokenPermissions):
    """Permission mapping for read-only delivery history and redelivery."""

    def get_required_permissions(self, method, model):
        if getattr(self, "_current_action", None) == "redeliver":
            return ["extras.change_webhookendpoint"]
        if method in {"GET", "HEAD", "OPTIONS"}:
            # Delivery history is an operational view of an endpoint, so keep
            # it aligned with the endpoint permission seed data.
            return ["extras.view_webhookendpoint"]
        return super().get_required_permissions(method, model)

    def has_permission(self, request, view):
        self._current_action = getattr(view, "action", None)
        return super().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        self._current_action = getattr(view, "action", None)
        return super().has_object_permission(request, view, obj)


def _has_platform_delivery_view(user):
    """Return whether ``user`` may see system-wide delivery records."""

    return bool(user and user.is_authenticated and user.is_superuser)


def _delivery_queryset_for_user(user):
    """Build a delivery queryset without widening tenant scope accidentally.

    Agent A may provide the model's optional ``visible_to`` helper.  Use it when
    present; otherwise retain the repository's scoped-manager behavior and only
    use ``all_objects`` (which is still tenant-scoped) for platform-authorized
    reads.
    """

    if user.is_superuser:
        # A superuser is the explicit platform-wide exception.  Use Django's
        # unscoped base manager so an active tenant selected in the session does
        # not hide tenant=None operational history.
        # unscoped: platform-authorized delivery listing; non-platform callers take the scoped branch
        return WebhookDelivery._base_manager.all()

    queryset = WebhookDelivery.objects.all()
    visible_to = getattr(queryset, "visible_to", None)
    if callable(visible_to):
        return visible_to(user)

    manager = getattr(WebhookDelivery, "all_objects", None)
    manager_visible_to = getattr(manager, "visible_to", None)
    if callable(manager_visible_to):
        return manager_visible_to(user)

    if _has_platform_delivery_view(user) and manager is not None:
        return manager.all()
    return queryset


def _raise_safe_delivery_error(exc, *, redelivery=False):
    """Translate known task validation failures without returning unsafe text."""

    if redelivery and "Delivery is still in progress." in {str(message) for message in exc.messages}:
        detail = _("Delivery is still in progress.")
    elif redelivery:
        detail = _("Webhook delivery could not be redelivered.")
    else:
        detail = _("Webhook test could not be queued.")
    raise DRFValidationError({"detail": detail}) from exc


class TagViewSet(ITAMBoxModelViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = TagFilter


class CustomFieldViewSet(CustomFieldWriteMixin, ITAMBoxModelViewSet):
    queryset = CustomField.objects.all()
    serializer_class = CustomFieldSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = CustomFieldFilterSet


class CustomFieldsetViewSet(CustomFieldsetWriteMixin, ITAMBoxModelViewSet):
    queryset = CustomFieldset.objects.prefetch_related("fields").all()
    serializer_class = CustomFieldsetSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = CustomFieldsetFilterSet


class DashboardViewSet(ITAMBoxModelViewSet):
    serializer_class = DashboardSerializer

    def get_queryset(self):
        return Dashboard.objects.select_related("user").filter(user=self.request.user)


class WebhookEndpointViewSet(ITAMBoxModelViewSet):
    permission_classes = [WebhookEndpointActionPermissions, StrictTenantPermission]
    queryset = WebhookEndpoint.objects.all()
    serializer_class = WebhookEndpointSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = WebhookEndpointFilterSet

    @extend_schema(
        request=None,
        responses={status.HTTP_202_ACCEPTED: WebhookDeliveryActionSerializer},
        summary=_("Send a test webhook"),
    )
    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        endpoint = self.get_object()

        try:
            delivery = send_webhook_test(endpoint.pk, actor_id=request.user.pk)
        except DjangoValidationError as exc:
            _raise_safe_delivery_error(exc)
        except DjangoPermissionDenied:
            raise Http404 from None
        return Response(
            WebhookDeliveryActionSerializer(delivery).data,
            status=status.HTTP_202_ACCEPTED,
        )


class WebhookDeliveryViewSet(ITAMBoxReadOnlyModelViewSet):
    permission_classes = [WebhookDeliveryPermissions, StrictTenantPermission]
    queryset = WebhookDelivery.objects.all()
    serializer_class = WebhookDeliverySerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = WebhookDeliveryFilterSet

    def get_queryset(self):
        queryset = _delivery_queryset_for_user(self.request.user)
        queryset = queryset.select_related(
            "endpoint",
            "event",
            "tenant",
            "redelivered_from",
            "redelivered_by",
        )
        if not _has_platform_delivery_view(self.request.user):
            # Tenant-scoped operators must never discover system-wide rows by
            # guessing a delivery primary key.  This filter remains explicit
            # even when the model manager already fails closed by default.
            queryset = queryset.filter(tenant__isnull=False)
        return queryset

    @extend_schema(
        request=None,
        responses={status.HTTP_202_ACCEPTED: WebhookDeliveryActionSerializer},
        summary=_("Redeliver a webhook delivery"),
    )
    @action(detail=True, methods=["post"])
    def redeliver(self, request, pk=None):
        delivery = self.get_object()

        try:
            new_delivery = redeliver_webhook_delivery(delivery.pk, actor_id=request.user.pk)
        except DjangoValidationError as exc:
            _raise_safe_delivery_error(exc, redelivery=True)
        except DjangoPermissionDenied:
            raise Http404 from None
        return Response(
            WebhookDeliveryActionSerializer(new_delivery).data,
            status=status.HTTP_202_ACCEPTED,
        )


class EventRuleViewSet(ITAMBoxModelViewSet):
    queryset = EventRule.objects.select_related("model", "webhook").all()
    serializer_class = EventRuleSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = EventRuleFilterSet


class NotificationChannelViewSet(ITAMBoxModelViewSet):
    queryset = NotificationChannel.objects.all()
    serializer_class = NotificationChannelSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = NotificationChannelFilterSet


class AlertLogViewSet(ITAMBoxReadOnlyModelViewSet):
    http_method_names = ["get", "head", "options"]
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = (
        AlertLog.objects.filter(tenant__isnull=False)
        .select_related("rule", "content_type", "acknowledged_by", "resolved_by", "tenant")
        .all()
    )
    serializer_class = AlertLogSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AlertLogFilterSet


class AlertRuleViewSet(ITAMBoxModelViewSet):
    queryset = AlertRule.objects.prefetch_related("channels").all()
    serializer_class = AlertRuleSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AlertRuleFilterSet


class JournalEntryViewSet(ITAMBoxModelViewSet):
    queryset = JournalEntry.objects.select_related("model", "user", "tenant").all()
    serializer_class = JournalEntrySerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = JournalEntryFilterSet
