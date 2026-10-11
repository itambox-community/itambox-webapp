from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.tests.mixins import TenantTestMixin, grant
from extras.models import WebhookDelivery, WebhookEndpoint
from organization.models import Role, Tenant

User = get_user_model()


class WebhookDeliveryUITests(TenantTestMixin, TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Webhook UI Tenant A", slug="webhook-ui-a")
        self.tenant_b = Tenant.objects.create(name="Webhook UI Tenant B", slug="webhook-ui-b")

        view_permissions = ["extras.view_webhookendpoint"]
        operator_permissions = ["extras.view_webhookendpoint", "extras.change_webhookendpoint"]

        self.viewer = User.objects.create_user(username="webhook_ui_viewer", password="pw")
        self.operator = User.objects.create_user(username="webhook_ui_operator", password="pw")
        self.other_operator = User.objects.create_user(username="webhook_ui_other", password="pw")
        grant(
            self.viewer,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="Webhook UI Viewer", permissions=view_permissions),
        )
        grant(
            self.operator,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="Webhook UI Operator", permissions=operator_permissions),
        )
        grant(
            self.other_operator,
            self.tenant_b,
            Role.objects.create(
                tenant=self.tenant_b,
                name="Other Webhook UI Operator",
                permissions=operator_permissions,
            ),
        )

        self.endpoint_a = WebhookEndpoint.objects.create(
            name="Webhook UI endpoint A",
            url="http://8.8.8.8/webhook-ui-a",
            tenant=self.tenant_a,
        )
        self.endpoint_b = WebhookEndpoint.objects.create(
            name="Webhook UI endpoint B",
            url="http://8.8.8.8/webhook-ui-b",
            tenant=self.tenant_b,
        )
        self.success_delivery = self._delivery(status="success", response_code=200)
        self.failed_delivery = self._delivery(
            status="failed",
            error_class="integration.unavailable",
            error_message="The webhook endpoint could not be reached.",
        )
        self.pending_delivery = self._delivery(status="pending")
        self.other_delivery = self._delivery(
            endpoint=self.endpoint_b,
            tenant=self.tenant_b,
            status="dead",
            error_class="integration.request",
            error_message="The webhook delivery was rejected.",
        )

    def _delivery(self, endpoint=None, tenant=None, **kwargs):
        return WebhookDelivery.objects.create(
            delivery_id=str(uuid4()),
            endpoint=endpoint or self.endpoint_a,
            tenant=tenant or self.tenant_a,
            **kwargs,
        )

    def _endpoint_url(self, endpoint=None):
        return reverse(
            "extras:webhookendpoint_detail",
            kwargs={"pk": (endpoint or self.endpoint_a).pk},
        )

    def _test_url(self, endpoint=None):
        return reverse(
            "extras:webhookendpoint_test",
            kwargs={"pk": (endpoint or self.endpoint_a).pk},
        )

    def _redeliver_url(self, delivery):
        return reverse("extras:webhookdelivery_redeliver", kwargs={"pk": delivery.pk})

    def test_endpoint_detail_renders_scoped_history_and_operator_actions(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        response = self.client.get(self._endpoint_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delivery History")
        self.assertContains(response, self.success_delivery.delivery_id[:8])
        self.assertContains(response, "Success")
        self.assertContains(response, "Redeliver</button>")
        self.assertContains(response, self._redeliver_url(self.success_delivery))
        self.assertContains(response, "Send Test Webhook")
        self.assertContains(response, self._test_url())
        self.assertNotContains(response, self.other_delivery.delivery_id[:8])

    def test_view_only_user_sees_history_but_not_mutation_actions(self):
        self.client_login_to_tenant(self.viewer, self.tenant_a)

        response = self.client.get(self._endpoint_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.success_delivery.delivery_id[:8])
        self.assertNotContains(response, "Redeliver</button>")
        self.assertNotContains(response, self._redeliver_url(self.success_delivery))
        self.assertNotContains(response, "Send Test Webhook")
        self.assertNotContains(response, self._test_url())

    def test_test_send_action_queues_delivery_and_redirects_with_message(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        with patch("extras.webhook_views.send_webhook_test") as send_test:
            response = self.client.post(self._test_url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.endpoint_a.get_absolute_url())
        send_test.assert_called_once_with(self.endpoint_a.pk, actor_id=self.operator.pk)
        self.assertIn("Test webhook queued.", [str(message) for message in get_messages(response.wsgi_request)])

    def test_view_only_test_send_is_forbidden_without_side_effect(self):
        self.client_login_to_tenant(self.viewer, self.tenant_a)

        with patch("extras.webhook_views.send_webhook_test") as send_test:
            response = self.client.post(self._test_url())

        self.assertEqual(response.status_code, 403)
        send_test.assert_not_called()

    def test_redeliver_action_records_actor_and_redirects(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        def create_redelivery(delivery_pk, *, actor_id):
            return WebhookDelivery.objects.create(
                delivery_id=str(uuid4()),
                endpoint=self.endpoint_a,
                tenant=self.tenant_a,
                status="pending",
                redelivered_from_id=delivery_pk,
                redelivered_by_id=actor_id,
                redelivered_at=timezone.now(),
            )

        with patch(
            "extras.webhook_views.redeliver_webhook_delivery",
            side_effect=create_redelivery,
        ) as redeliver:
            response = self.client.post(self._redeliver_url(self.failed_delivery))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.endpoint_a.get_absolute_url())
        redeliver.assert_called_once_with(self.failed_delivery.pk, actor_id=self.operator.pk)
        created = WebhookDelivery.objects.exclude(pk=self.success_delivery.pk).filter(
            redelivered_from=self.failed_delivery,
        )
        self.assertEqual(created.count(), 1)
        self.assertEqual(created.get().redelivered_by_id, self.operator.pk)
        self.assertIn(
            "Webhook delivery redelivered.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_other_tenant_redelivery_is_fail_closed(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        with patch("extras.webhook_views.redeliver_webhook_delivery") as redeliver:
            response = self.client.post(self._redeliver_url(self.other_delivery))

        self.assertEqual(response.status_code, 404)
        redeliver.assert_not_called()

    def test_pending_redelivery_does_not_double_fire(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        with patch("extras.webhook_views.redeliver_webhook_delivery") as redeliver:
            response = self.client.post(self._redeliver_url(self.pending_delivery))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.endpoint_a.get_absolute_url())
        redeliver.assert_not_called()
        self.assertIn(
            "Delivery is still in progress.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_delivery_error_is_safe_on_endpoint_page(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)
        self.endpoint_a.secret = "ui-secret-value"
        self.endpoint_a.save(update_fields=["secret"])
        self.failed_delivery.error_message = "The webhook endpoint could not be reached."
        self.failed_delivery.save(update_fields=["error_message"])

        response = self.client.get(self._endpoint_url())

        self.assertContains(response, self.failed_delivery.error_message)
        self.assertNotContains(response, "ui-secret-value")

    def test_tenant_delivery_grant_does_not_expose_system_wide_deliveries(self):
        reader = User.objects.create_user(username="webhook_ui_reader", password="pw")
        grant(
            reader,
            self.tenant_a,
            Role.objects.create(
                tenant=self.tenant_a,
                name="Webhook UI Reader",
                permissions=["extras.view_webhookdelivery", "extras.view_webhookendpoint"],
            ),
        )
        global_delivery = WebhookDelivery.objects.create(
            delivery_id=str(uuid4()), endpoint=self.endpoint_a, tenant=None, status="success", response_code=200
        )

        self.client_login_to_tenant(reader, self.tenant_a)
        response = self.client.get(self._endpoint_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, global_delivery.delivery_id[:8])
        self.assertContains(response, self.failed_delivery.delivery_id[:8])

    def test_superuser_sees_system_wide_deliveries(self):
        superuser = User.objects.create_superuser(username="webhook_ui_platform", password="pw")
        global_delivery = WebhookDelivery.objects.create(
            delivery_id=str(uuid4()), endpoint=self.endpoint_a, tenant=None, status="success", response_code=200
        )

        self.client_login_to_tenant(superuser, self.tenant_a)
        response = self.client.get(self._endpoint_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, global_delivery.delivery_id[:8])

    def test_test_send_failure_shows_safe_message(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        with patch("extras.webhook_views.send_webhook_test", side_effect=RuntimeError("boom")):
            response = self.client.post(self._test_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "The test webhook could not be queued.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_redeliver_unexpected_failure_shows_safe_message(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        with patch("extras.webhook_views.redeliver_webhook_delivery", side_effect=RuntimeError("boom")):
            response = self.client.post(self._redeliver_url(self.failed_delivery))

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "The webhook delivery could not be redelivered.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_redeliver_validation_failure_shows_safe_message(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)

        with patch(
            "extras.webhook_views.redeliver_webhook_delivery",
            side_effect=DjangoValidationError("unexpected validation failure"),
        ):
            response = self.client.post(self._redeliver_url(self.failed_delivery))

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "The webhook delivery could not be redelivered.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_detail_renders_test_send_and_empty_delivery_markers(self):
        self.client_login_to_tenant(self.operator, self.tenant_a)
        test_delivery = self._delivery(status="success", test_send=True, response_code=200)
        WebhookDelivery.objects.create(
            delivery_id="",
            endpoint=self.endpoint_a,
            tenant=self.tenant_a,
            status="pending",
        )

        response = self.client.get(self._endpoint_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, test_delivery.delivery_id[:8])
        self.assertContains(response, "Test webhook")
        self.assertContains(
            response,
            'data-label="Delivery ID"><span class="text-muted">–</span>',
        )
