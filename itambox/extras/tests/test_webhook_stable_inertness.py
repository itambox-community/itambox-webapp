from unittest.mock import MagicMock, patch

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TransactionTestCase

from assets.models import Manufacturer
from core.features import STABLE
from extras.models import Event, EventRule, WebhookDelivery, WebhookEndpoint
from extras.services.events import dispatch_event
from extras.tasks.webhooks import recover_pending_webhook_deliveries
from itambox.capabilities import ALWAYS_ON, SOURCE_ALWAYS, registry
from organization.models import Location, Site, Tenant


class WebhookStableInertnessTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.location_ct = ContentType.objects.get_for_model(Location)
        self.tenant = Tenant.objects.create(name="Webhook Tenant", slug="webhook-tenant")
        self.site = Site.objects.create(name="Webhook Site", slug="webhook-site", tenant=self.tenant)

    def test_stable_webhook_capability_is_active_without_configuration_rows(self):
        self.assertEqual(EventRule._base_manager.count(), 0)
        self.assertEqual(WebhookEndpoint._base_manager.count(), 0)

        capability = registry.get("automation.webhooks")
        self.assertEqual(
            (capability.maturity, capability.activation, capability.activation_source),
            (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
        )
        self.assertTrue(registry.state("automation.webhooks").active)

    @patch("core.http.request_pinned")
    def test_dispatch_without_rules_creates_no_deliveries_or_outbound_http(self, request_pinned):
        Manufacturer.objects.create(name="No webhook rules", slug="no-webhook-rules")

        request_pinned.assert_not_called()
        self.assertEqual(WebhookDelivery._base_manager.count(), 0)

    @patch("core.http.request_pinned")
    def test_enabled_endpoint_defaults_alone_do_not_dispatch(self, request_pinned):
        endpoint = WebhookEndpoint.objects.create(
            name="Unsubscribed endpoint",
            url="https://example.com/unsubscribed",
        )
        self.assertTrue(endpoint.enabled)
        self.assertEqual(endpoint.http_method, WebhookEndpoint.HTTP_POST)
        self.assertEqual(endpoint.headers, {})
        self.assertEqual(endpoint.secret, "")
        self.assertEqual(endpoint.retry_count, 3)
        self.assertEqual(endpoint.retry_backoff, 60)

        location = Location(name="Endpoint without a rule")
        location.pk = 987651
        with transaction.atomic():
            dispatch_event(Location, location, Event.ACTION_CREATE)

        request_pinned.assert_not_called()
        self.assertEqual(WebhookDelivery._base_manager.count(), 0)

    @patch("core.http.request_pinned")
    def test_empty_recovery_coordinator_does_not_requeue_or_send(self, request_pinned):
        with patch("extras.tasks.webhooks.async_task") as async_task:
            result = recover_pending_webhook_deliveries()

        self.assertEqual(result, {"dispatched": 0})
        async_task.assert_not_called()
        request_pinned.assert_not_called()
        self.assertEqual(WebhookDelivery._base_manager.count(), 0)

    @patch("core.http.request_pinned")
    def test_rule_dispatch_requires_matching_event(self, request_pinned):
        request_pinned.return_value = MagicMock(status_code=200)
        endpoint = WebhookEndpoint.objects.create(
            name="Action-filter endpoint",
            url="https://example.com/action-filter",
        )
        location = Location.objects.create(
            name="Action-filter location",
            slug="action-filter-location",
            tenant=self.tenant,
            site=self.site,
        )
        EventRule.objects.create(
            name="Create-only webhook rule",
            model=self.location_ct,
            events=[Event.ACTION_CREATE],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=endpoint,
            enabled=True,
        )

        with transaction.atomic():
            dispatch_event(Location, location, Event.ACTION_UPDATE)
        request_pinned.assert_not_called()
        self.assertEqual(WebhookDelivery._base_manager.count(), 0)

        with transaction.atomic():
            dispatch_event(Location, location, Event.ACTION_CREATE)

        request_pinned.assert_called_once()
        delivery = WebhookDelivery._base_manager.get()
        self.assertEqual(delivery.event.action, Event.ACTION_CREATE)
        self.assertEqual(delivery.attempt, 1)
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)

    @patch("core.http.request_pinned")
    def test_disabling_endpoint_stops_later_matching_events(self, request_pinned):
        request_pinned.return_value = MagicMock(status_code=200)
        endpoint = WebhookEndpoint.objects.create(
            name="Disable-after-send endpoint",
            url="https://example.com/disable-after-send",
            enabled=True,
        )
        location = Location.objects.create(
            name="Disable-after-send location",
            slug="disable-after-send-location",
            tenant=self.tenant,
            site=self.site,
        )
        EventRule.objects.create(
            name="Disable-after-send rule",
            model=self.location_ct,
            events=[Event.ACTION_CREATE],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=endpoint,
            enabled=True,
        )

        with transaction.atomic():
            dispatch_event(Location, location, Event.ACTION_CREATE)
        request_pinned.assert_called_once()
        self.assertEqual(WebhookDelivery._base_manager.count(), 1)

        endpoint.enabled = False
        endpoint.save(update_fields=["enabled"])
        with transaction.atomic():
            dispatch_event(Location, location, Event.ACTION_CREATE)

        request_pinned.assert_called_once()
        self.assertEqual(WebhookDelivery._base_manager.count(), 1)
