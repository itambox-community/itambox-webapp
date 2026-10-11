from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from core.tests.mixins import TenantTestMixin, grant
from extras.api.serializers import WebhookDeliverySerializer
from extras.models import WebhookDelivery, WebhookEndpoint
from organization.models import Role, Tenant

User = get_user_model()


class WebhookDeliveryAPITests(TenantTestMixin, APITestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Delivery Tenant A", slug="delivery-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Delivery Tenant B", slug="delivery-tenant-b")

        view_permissions = ["extras.view_webhookendpoint"]
        operator_permissions = ["extras.view_webhookendpoint", "extras.change_webhookendpoint"]

        self.view_user = User.objects.create_user(username="delivery_viewer", password="pw")
        self.operator = User.objects.create_user(username="delivery_operator", password="pw")
        self.other_operator = User.objects.create_user(username="delivery_other_operator", password="pw")
        grant(
            self.view_user,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="Delivery Viewer", permissions=view_permissions),
        )
        grant(
            self.operator,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="Delivery Operator", permissions=operator_permissions),
        )
        grant(
            self.other_operator,
            self.tenant_b,
            Role.objects.create(tenant=self.tenant_b, name="Other Delivery Operator", permissions=operator_permissions),
        )
        self.superuser = User.objects.create_superuser(username="delivery_superuser", password="pw")

        self.endpoint_a = WebhookEndpoint.objects.create(
            name="Delivery endpoint A",
            url="http://8.8.8.8/delivery-a",
            tenant=self.tenant_a,
        )
        self.endpoint_b = WebhookEndpoint.objects.create(
            name="Delivery endpoint B",
            url="http://8.8.8.8/delivery-b",
            tenant=self.tenant_b,
        )
        self.global_endpoint = WebhookEndpoint.objects.create(
            name="Global delivery endpoint",
            url="http://8.8.8.8/delivery-global",
            tenant=None,
        )

        self.delivery_a = self._delivery(
            tenant=self.tenant_a,
            endpoint=self.endpoint_a,
            status="success",
            response_code=200,
        )
        self.delivery_a_failed = self._delivery(
            tenant=self.tenant_a,
            endpoint=self.endpoint_a,
            status="dead",
            error_class="integration.unavailable",
            error_message="The webhook endpoint could not be reached.",
        )
        self.delivery_b = self._delivery(tenant=self.tenant_b, endpoint=self.endpoint_b, status="success")
        self.global_delivery = self._delivery(tenant=None, endpoint=self.global_endpoint, status="success")

    def _delivery(self, **kwargs):
        return WebhookDelivery.objects.create(delivery_id=str(uuid4()), **kwargs)

    def _login(self, user, tenant):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()
        membership = user.memberships.filter(tenant=tenant, is_active=True).first()
        self.set_active_tenant(tenant, membership)

    @staticmethod
    def _rows(response):
        data = response.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    @staticmethod
    def _ids(response):
        return {row["id"] for row in WebhookDeliveryAPITests._rows(response)}

    def _list_url(self):
        return reverse("api:extras_api:webhookdelivery-list")

    def _detail_url(self, pk):
        return reverse("api:extras_api:webhookdelivery-detail", kwargs={"pk": pk})

    def _redeliver_url(self, pk):
        return reverse("api:extras_api:webhookdelivery-redeliver", kwargs={"pk": pk})

    def _endpoint_test_url(self, pk):
        return reverse("api:extras_api:webhookendpoint-test", kwargs={"pk": pk})

    def test_tenant_operator_list_excludes_other_tenant_and_global_rows(self):
        self._login(self.operator, self.tenant_a)

        response = self.client.get(self._list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = self._ids(response)
        self.assertIn(self.delivery_a.pk, ids)
        self.assertIn(self.delivery_a_failed.pk, ids)
        self.assertNotIn(self.delivery_b.pk, ids)
        self.assertNotIn(self.global_delivery.pk, ids)

    def test_superuser_sees_system_wide_rows(self):
        self.client.force_authenticate(self.superuser)

        response = self.client.get(self._list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.global_delivery.pk, self._ids(response))

    def test_detail_hides_other_tenant_and_system_wide_pk_guesses(self):
        self._login(self.operator, self.tenant_a)

        self.assertEqual(self.client.get(self._detail_url(self.delivery_b.pk)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.get(self._detail_url(self.global_delivery.pk)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_filters_endpoint_status_and_test_send(self):
        test_delivery = self._delivery(tenant=self.tenant_a, endpoint=self.endpoint_a, status="failed", test_send=True)
        self._login(self.operator, self.tenant_a)

        endpoint_response = self.client.get(self._list_url(), {"endpoint": self.endpoint_a.pk})
        status_response = self.client.get(self._list_url(), {"status": "dead"})
        test_response = self.client.get(self._list_url(), {"test_send": "true"})

        self.assertEqual(
            self._ids(endpoint_response),
            {self.delivery_a.pk, self.delivery_a_failed.pk, test_delivery.pk},
        )
        self.assertEqual(self._ids(status_response), {self.delivery_a_failed.pk})
        self.assertEqual(self._ids(test_response), {test_delivery.pk})

    def test_redeliver_returns_new_delivery_id(self):
        new_delivery = self._delivery(tenant=self.tenant_a, endpoint=self.endpoint_a, status="pending")
        self._login(self.operator, self.tenant_a)

        with patch("extras.api.views.redeliver_webhook_delivery", return_value=new_delivery) as redeliver:
            response = self.client.post(self._redeliver_url(self.delivery_a_failed.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["id"], new_delivery.pk)
        self.assertEqual(response.data["delivery_id"], new_delivery.delivery_id)
        redeliver.assert_called_once_with(self.delivery_a_failed.pk, actor_id=self.operator.pk)

    def test_redeliver_requires_change_permission(self):
        self._login(self.view_user, self.tenant_a)

        response = self.client.post(self._redeliver_url(self.delivery_a_failed.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_redeliver_hides_other_tenant_delivery(self):
        self._login(self.operator, self.tenant_a)

        response = self.client.post(self._redeliver_url(self.delivery_b.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_redeliver_pending_delivery_returns_safe_400(self):
        self._login(self.operator, self.tenant_a)

        with patch(
            "extras.api.views.redeliver_webhook_delivery",
            side_effect=DjangoValidationError("Delivery is still in progress."),
        ):
            response = self.client.post(self._redeliver_url(self.delivery_a.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Delivery is still in progress.")

    def test_test_send_returns_test_delivery_id(self):
        test_delivery = self._delivery(tenant=self.tenant_a, endpoint=self.endpoint_a, status="pending", test_send=True)
        self._login(self.operator, self.tenant_a)

        with patch("extras.api.views.send_webhook_test", return_value=test_delivery) as test_send:
            response = self.client.post(self._endpoint_test_url(self.endpoint_a.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["id"], test_delivery.pk)
        self.assertEqual(response.data["delivery_id"], test_delivery.delivery_id)
        test_send.assert_called_once_with(self.endpoint_a.pk, actor_id=self.operator.pk)

    def test_test_send_requires_change_permission(self):
        self._login(self.view_user, self.tenant_a)

        response = self.client.post(self._endpoint_test_url(self.endpoint_a.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_test_send_hides_other_tenant_endpoint(self):
        self._login(self.operator, self.tenant_a)

        response = self.client.post(self._endpoint_test_url(self.endpoint_b.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_serializer_exposes_no_endpoint_secrets_or_url(self):
        self.endpoint_a.headers = {"Authorization": "Bearer header-secret"}
        self.endpoint_a.secret = "endpoint-secret"
        data = WebhookDeliverySerializer(self.delivery_a).data
        rendered = str(data)

        self.assertNotIn("header-secret", rendered)
        self.assertNotIn("endpoint-secret", rendered)
        self.assertNotIn(self.endpoint_a.url, rendered)
        self.assertNotIn("headers", data)
        self.assertNotIn("secret", data)
        self.assertNotIn("url", data)

    def test_openapi_contains_delivery_list_and_actions(self):
        # Lazy import: drf_spectacular must not load before DRF has finished
        # initializing, otherwise it trips the TokenAuthentication import cycle.
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema["paths"]

        delivery_paths = [path for path in paths if "webhook-deliveries" in path]
        endpoint_test_paths = [path for path in paths if "webhook-endpoints" in path and path.endswith("/test/")]
        self.assertTrue(delivery_paths)
        self.assertTrue(any("redeliver" in path for path in delivery_paths))
        self.assertTrue(endpoint_test_paths)

    def test_test_send_on_system_wide_endpoint_hides_for_tenant_operator(self):
        self._login(self.operator, self.tenant_a)

        response = self.client.post(self._endpoint_test_url(self.global_endpoint.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_test_send_on_system_wide_endpoint_allows_superuser(self):
        self._login(self.superuser, self.tenant_a)

        response = self.client.post(self._endpoint_test_url(self.global_endpoint.pk), format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            response.data["delivery_id"],
            WebhookDelivery._base_manager.get(pk=response.data["id"]).delivery_id,
        )

    def test_superuser_list_includes_system_wide_deliveries(self):
        self._login(self.superuser, self.tenant_a)

        response = self.client.get(reverse("api:extras_api:webhookdelivery-list"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.global_delivery.delivery_id, [row["delivery_id"] for row in response.data["results"]])

    def test_tenant_grant_of_view_webhookdelivery_stays_scoped(self):
        """#717: a tenant-scoped read grant is not platform-wide authority."""
        reader = User.objects.create_user(username="delivery_dual_reader", password="pw")
        grant(
            reader,
            self.tenant_a,
            Role.objects.create(
                tenant=self.tenant_a,
                name="Delivery Dual Reader",
                permissions=["extras.view_webhookendpoint", "extras.view_webhookdelivery"],
            ),
        )
        self._login(reader, self.tenant_a)

        response = self.client.get(self._list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = self._ids(response)
        self.assertIn(self.delivery_a.pk, ids)
        self.assertNotIn(self.delivery_b.pk, ids)
        self.assertNotIn(self.global_delivery.pk, ids)
        self.assertEqual(self.client.get(self._detail_url(self.delivery_b.pk)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.get(self._detail_url(self.global_delivery.pk)).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertIn(
            self.client.post(self._redeliver_url(self.delivery_b.pk), format="json").status_code,
            {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND},
        )
