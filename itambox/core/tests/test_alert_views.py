"""Tests for Alert Center bulk acknowledge/resolve views."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase
from django.urls import reverse

from core.tests.mixins import grant
from extras.models import AlertLog, AlertRule
from organization.models import Role, Tenant

User = get_user_model()


class AlertBulkActionViewTests(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="bulkadmin", password="x", email="a@b.com")
        self.client = Client()
        self.client.force_login(self.admin)
        self.tenant = Tenant.objects.create(name="Bulk Tenant", slug="bulk-tenant")
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()
        self.rule = AlertRule.objects.create(
            tenant=self.tenant,
            name="Bulk Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
        )
        self.ct = ContentType.objects.get_for_model(AlertRule)

    def _log(self, oid, status=AlertLog.STATUS_ACTIVE):
        return AlertLog.objects.create(
            tenant=self.tenant,
            rule=self.rule,
            subject="s",
            message="m",
            content_type=self.ct,
            object_id=oid,
            status=status,
        )

    def test_bulk_acknowledge_transitions_active_logs(self):
        l1, l2 = self._log(1), self._log(2)
        resp = self.client.post(reverse("extras:alertlog_bulk_acknowledge"), {"pk": [l1.pk, l2.pk]})
        self.assertEqual(resp.status_code, 302)
        l1.refresh_from_db()
        l2.refresh_from_db()
        self.assertEqual(l1.status, AlertLog.STATUS_ACKNOWLEDGED)
        self.assertEqual(l2.status, AlertLog.STATUS_ACKNOWLEDGED)
        self.assertEqual(l1.acknowledged_by, self.admin)

    def test_bulk_resolve_transitions_active_and_acknowledged(self):
        active = self._log(1, status=AlertLog.STATUS_ACTIVE)
        ack = self._log(2, status=AlertLog.STATUS_ACKNOWLEDGED)
        resp = self.client.post(reverse("extras:alertlog_bulk_resolve"), {"pk": [active.pk, ack.pk]})
        self.assertEqual(resp.status_code, 302)
        active.refresh_from_db()
        ack.refresh_from_db()
        self.assertEqual(active.status, AlertLog.STATUS_RESOLVED)
        self.assertEqual(ack.status, AlertLog.STATUS_RESOLVED)
        self.assertEqual(active.resolved_by, self.admin)
        self.assertIsNotNone(active.resolved_at)

    def test_bulk_acknowledge_skips_resolved(self):
        resolved = self._log(1, status=AlertLog.STATUS_RESOLVED)
        self.client.post(reverse("extras:alertlog_bulk_acknowledge"), {"pk": [resolved.pk]})
        resolved.refresh_from_db()
        # Resolved alerts are not eligible for acknowledgement.
        self.assertEqual(resolved.status, AlertLog.STATUS_RESOLVED)

    def test_bulk_action_with_no_selection_redirects(self):
        resp = self.client.post(reverse("extras:alertlog_bulk_resolve"), {})
        self.assertEqual(resp.status_code, 302)

    def test_bulk_acknowledge_htmx_returns_trigger(self):
        l1 = self._log(1)
        resp = self.client.post(
            reverse("extras:alertlog_bulk_acknowledge"),
            {"pk": [l1.pk]},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertIn("tableRefreshRequired", resp["HX-Trigger"])
        l1.refresh_from_db()
        self.assertEqual(l1.status, AlertLog.STATUS_ACKNOWLEDGED)


class AlertRuleRunNowViewTests(TestCase):
    """Run-now keeps the ``extras.change_alertrule`` gate and stays tenant-scoped (issue #567)."""

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.create(name="Run Now Tenant", slug="run-now-tenant")
        self.rule = AlertRule.objects.create(
            tenant=self.tenant,
            name="Run Now Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
        )
        self.url = reverse("extras:alertrule_run", kwargs={"pk": self.rule.pk})

    def _client_for(self, user):
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()
        return client

    def test_anonymous_is_sent_to_login(self):
        response = Client().post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_without_change_permission_the_action_is_denied(self):
        role = Role.objects.create(tenant=self.tenant, name="Alert viewer role", permissions=[])
        user = User.objects.create_user(username="alertviewer", password="x", email="viewer@example.com")
        grant(user, self.tenant, role)
        response = self._client_for(user).post(self.url)
        self.assertEqual(response.status_code, 403)

    def test_change_permission_queues_the_single_rule_evaluation(self):
        user = User.objects.create_superuser(username="alertrunner", password="x", email="runner@example.com")
        with patch("extras.views.async_task") as async_task:
            response = self._client_for(user).post(self.url)
        self.assertEqual(response.status_code, 302)
        async_task.assert_called_once_with("extras.tasks.alerts.run_alert_rule_now", self.rule.pk)

    def test_rule_outside_the_active_tenant_is_not_reachable(self):
        other = Tenant.objects.create(name="Other Tenant", slug="other-run-tenant")
        other_rule = AlertRule.objects.create(
            tenant=other,
            name="Other Run Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
        )
        user = User.objects.create_superuser(username="alertroamer", password="x", email="roamer@example.com")
        response = self._client_for(user).post(reverse("extras:alertrule_run", kwargs={"pk": other_rule.pk}))
        self.assertEqual(response.status_code, 404)
