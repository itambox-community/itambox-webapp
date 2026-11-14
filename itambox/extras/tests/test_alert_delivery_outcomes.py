"extras/tests/test_alert_delivery_outcomes.py (migration rehearsals live under scripts/qualification/migrations/)."

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.events import DeliveryDisposition, DeliveryResult
from core.models import Notification
from core.tests.mixins import TenantTestMixin, grant
from extras.filters import AlertLogFilterSet
from extras.models import AlertLog, AlertRule, NotificationChannel
from extras.tables import AlertLogTable
from extras.tasks.alerts import (
    _delivery_error,
    _delivery_outcome,
    _dispatch_channels,
    _evaluate_rule,
    _schedule_alert_dispatch,
)
from organization.models import Role, Tenant

User = get_user_model()


def _tenant_member(tenant, username):
    """Create an active user who is a member of ``tenant`` (in-app delivery scope)."""
    user = User.objects.create_user(username=username, password="x")
    role = Role.objects.create(tenant=tenant, name=f"{username} role", permissions=["assets.view_asset"])
    grant(user, tenant, role)
    return user


class DeliveryOutcomeDerivationTests(SimpleTestCase):
    """Pure derivation helpers: filterable outcome + typed error from payloads."""

    def test_empty_payload_is_none(self):
        self.assertEqual(_delivery_outcome({}), AlertLog.DELIVERY_OUTCOME_NONE)
        self.assertEqual(_delivery_outcome(None), AlertLog.DELIVERY_OUTCOME_NONE)
        self.assertIsNone(_delivery_error({}))
        self.assertIsNone(_delivery_error(None))

    def test_pending_and_crash_markers_are_respected(self):
        self.assertEqual(_delivery_outcome({"__dispatch__": "pending"}), AlertLog.DELIVERY_OUTCOME_PENDING)
        self.assertEqual(_delivery_outcome({"__dispatch__": "terminal"}), AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(_delivery_error({"__dispatch__": "terminal"}), "dispatch_crash")
        self.assertIsNone(_delivery_error({"__dispatch__": "pending"}))

    def test_no_channels_payload_is_none(self):
        payload = {"__no_channels__": "no channels attached to this rule"}
        self.assertEqual(_delivery_outcome(payload), AlertLog.DELIVERY_OUTCOME_NONE)
        self.assertIsNone(_delivery_error(payload))

    def test_legacy_string_payloads_derive_truthfully(self):
        self.assertEqual(_delivery_outcome({"7": "ok"}), AlertLog.DELIVERY_OUTCOME_DELIVERED)
        self.assertEqual(_delivery_outcome({"7": "failed"}), AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(_delivery_outcome({"7": "error: SMTP rejected"}), AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(_delivery_outcome({"7": "retryable"}), AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(_delivery_error({"7": "retryable"}), "retryable")
        self.assertIsNone(_delivery_error({"7": "ok"}))

    def test_structured_payloads_derive_truthfully(self):
        delivered = {"7": {"disposition": "success", "operation": "in_app.deliver", "delivery_id": "run-1"}}
        failed = {
            "7": {
                "disposition": "terminal",
                "operation": "email.deliver",
                "delivery_id": "run-1",
                "error_class": "SMTPException",
            }
        }
        mixed = {
            "7": {"disposition": "success", "operation": "in_app.deliver"},
            "9": {"disposition": "terminal", "operation": "slack.deliver", "error_class": "http_4xx"},
        }
        self.assertEqual(_delivery_outcome(delivered), AlertLog.DELIVERY_OUTCOME_DELIVERED)
        self.assertEqual(_delivery_outcome(failed), AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(_delivery_outcome(mixed), AlertLog.DELIVERY_OUTCOME_DELIVERED)
        self.assertEqual(_delivery_error(failed), "SMTPException")
        self.assertEqual(_delivery_error(mixed), "http_4xx")
        self.assertIsNone(_delivery_error(delivered))

    def test_structured_retryable_failure_is_failed_and_typed(self):
        payload = {"7": {"disposition": "retryable", "operation": "slack.deliver", "delivery_id": "run-1"}}
        self.assertEqual(_delivery_outcome(payload), AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(_delivery_error(payload), "retryable")

    def test_unknown_values_are_failed_by_default(self):
        self.assertEqual(_delivery_outcome({"7": "mystery"}), AlertLog.DELIVERY_OUTCOME_FAILED)


class ChannelDeliveryOutcomeTests(TestCase):
    """Typed structured per-channel outcomes recorded by the dispatch boundary."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="WP-13 Channel Tenant", slug="wp-13-channel-tenant")
        self.user = _tenant_member(self.tenant, "wp13-channel")
        self.rule = AlertRule.objects.create(
            name="WP-13 Channel Rule", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=1, tenant=self.tenant
        )
        self.match = {"subject": "WP-13", "message": "body", "tenant": self.tenant}

    def _channel(self, channel_type, **config):
        if channel_type == NotificationChannel.TYPE_IN_APP:
            config.setdefault("recipient_users", [self.user.pk])
        channel = NotificationChannel.objects.create(
            name=f"WP-13 {channel_type}", channel_type=channel_type, tenant=self.tenant, enabled=True, config=config
        )
        self.rule.channels.add(channel)
        return channel

    def test_successful_channel_records_structured_entry(self):
        channel = self._channel(NotificationChannel.TYPE_IN_APP)
        delivery = _dispatch_channels(self.rule, self.match, None, delivery_id="run-7")
        entry = delivery[str(channel.pk)]
        self.assertEqual(entry["disposition"], DeliveryDisposition.SUCCESS.value)
        self.assertEqual(entry["operation"], "in_app.deliver")
        self.assertEqual(entry["delivery_id"], "run-7")
        self.assertIn("attempted_at", entry)
        self.assertNotIn("error_class", entry)
        self.assertNotIn("message", entry)

    def test_terminal_failure_records_typed_error_and_safe_message_only(self):
        channel = self._channel(NotificationChannel.TYPE_IN_APP)
        terminal = DeliveryResult(
            "slack.deliver", DeliveryDisposition.TERMINAL, True, "Notification delivery was rejected.", "SMTPException"
        )
        with self._patched_sender(terminal):
            delivery = _dispatch_channels(self.rule, self.match, None, delivery_id="run-7")
        entry = delivery[str(channel.pk)]
        self.assertEqual(entry["disposition"], DeliveryDisposition.TERMINAL.value)
        self.assertEqual(entry["error_class"], "SMTPException")
        self.assertEqual(entry["message"], "Notification delivery was rejected.")

    def test_non_user_visible_failure_keeps_no_message(self):
        channel = self._channel(NotificationChannel.TYPE_IN_APP)
        retryable = DeliveryResult("slack.deliver", DeliveryDisposition.RETRYABLE)
        with self._patched_sender(retryable):
            delivery = _dispatch_channels(self.rule, self.match, None, delivery_id="run-7")
        entry = delivery[str(channel.pk)]
        self.assertEqual(entry["disposition"], DeliveryDisposition.RETRYABLE.value)
        self.assertNotIn("message", entry)

    def test_unexpected_exception_becomes_typed_terminal_entry(self):
        channel = self._channel(NotificationChannel.TYPE_IN_APP)
        with self._patched_sender(RuntimeError("backend exploded")):
            delivery = _dispatch_channels(self.rule, self.match, None, delivery_id="run-7")
        entry = delivery[str(channel.pk)]
        self.assertEqual(entry["disposition"], DeliveryDisposition.TERMINAL.value)
        self.assertEqual(entry["error_class"], "unexpected_channel_error")

    def _patched_sender(self, result):
        from unittest.mock import patch

        if isinstance(result, BaseException):
            return patch("extras.tasks.alerts.send_notification_to_channel", side_effect=result)
        return patch("extras.tasks.alerts.send_notification_to_channel", return_value=result)


class AlertDispatchObservabilityTests(TransactionTestCase):
    """Full evaluation loop: attempts, delivery ids, outcomes, idempotency."""

    def _setup(self, renotify_interval_days=0):
        tenant = Tenant.objects.create(name="WP-13 Observability Tenant", slug="wp-13-observability-tenant")
        user = _tenant_member(tenant, "wp13-obs")
        rule = AlertRule.objects.create(
            name="WP-13 Observability Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
            renotify_interval_days=renotify_interval_days,
        )
        channel = NotificationChannel.objects.create(
            name="WP-13 in-app",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=tenant,
            enabled=True,
            config={"recipient_users": [user.pk]},
        )
        rule.channels.add(channel)
        return (tenant, user, rule, channel)

    def test_full_evaluation_persists_attempts_id_and_outcome(self):
        from unittest.mock import patch

        from assets.models import Manufacturer
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        tenant, _user, rule, channel = self._setup()
        manufacturer = Manufacturer.objects.create(name="WP-13 Mfr", slug="wp-13-mfr")
        site = Site.objects.create(name="WP-13 Site", slug="wp-13-site", tenant=tenant)
        location = Location.objects.create(name="WP-13 Loc", slug="wp-13-loc", tenant=tenant, site=site)
        accessory = Accessory.objects.create(
            name="WP-13 Accessory", slug="wp-13-accessory", manufacturer=manufacturer, tenant=tenant, min_qty=5
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        with patch("extras.tasks.alerts.uuid4", return_value="run-42"):
            with transaction.atomic():
                from core.tasks.context import TaskContext

                with TaskContext(tenant_id=tenant.pk):
                    _evaluate_rule(rule, timezone.now().date(), {})
        alert = AlertLog.unscoped.get(rule=rule)
        self.assertEqual(alert.delivery_attempts, 1)
        self.assertEqual(alert.last_delivery_id, "run-42")
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)
        self.assertIsNone(alert.last_delivery_error)
        entry = alert.delivery_status[str(channel.pk)]
        self.assertEqual(entry["disposition"], DeliveryDisposition.SUCCESS.value)
        self.assertEqual(alert.delivery_status["__delivery_id__"], "run-42")

    def test_repeated_invocation_of_same_run_is_idempotent(self):
        from unittest.mock import patch

        from core.tasks.context import TaskContext

        tenant, user, rule, channel = self._setup()
        content_type = ContentType.objects.get_for_model(AlertRule)
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="idem",
            message="idem",
            content_type=content_type,
            object_id=rule.pk,
            delivery_status={"__dispatch__": "pending"},
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_PENDING,
        )
        match = {"obj": rule, "tenant": tenant, "subject": "idem", "message": "idem"}
        with patch("extras.tasks.alerts.uuid4", side_effect=["run-1", "run-1"]):
            with TaskContext(tenant_id=tenant.pk):
                with transaction.atomic():
                    _schedule_alert_dispatch(rule, match, alert)
                    _schedule_alert_dispatch(rule, match, alert)
        self.assertEqual(
            Notification.objects.filter(user=user, subject="idem").count(),
            1,
            "replayed delivery run must not duplicate in-app notifications",
        )
        alert.refresh_from_db()
        self.assertEqual(alert.delivery_attempts, 1)
        self.assertEqual(alert.last_delivery_id, "run-1")
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)

    def test_renotify_starts_fresh_attempt_with_new_id(self):
        from unittest.mock import patch

        from core.tasks.context import TaskContext

        tenant, _user, rule, channel = self._setup(renotify_interval_days=1)
        content_type = ContentType.objects.get_for_model(AlertRule)
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="renotify",
            message="renotify",
            content_type=content_type,
            object_id=rule.pk,
            delivery_status={"7": "ok"},
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_DELIVERED,
            delivery_attempts=1,
            last_delivery_id="old-run",
            last_notified_at=timezone.now() - timezone.timedelta(days=2),
        )
        match = {"obj": rule, "tenant": tenant, "subject": "renotify", "message": "renotify"}
        existing = {(rule.pk, alert.content_type_id, alert.object_id): alert}
        with (
            patch("extras.tasks.alerts.uuid4", return_value="fresh-run"),
            patch("extras.tasks.alerts._collect_matches", return_value=[match]),
        ):
            with TaskContext(tenant_id=tenant.pk):
                with transaction.atomic():
                    _evaluate_rule(rule, timezone.now().date(), existing)
        alert.refresh_from_db()
        self.assertEqual(alert.delivery_attempts, 2)
        self.assertEqual(alert.last_delivery_id, "fresh-run")
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)


class AlertDeliveryFilterTests(TestCase):
    def test_delivery_outcome_filter_finds_fired_but_undelivered(self):
        tenant = Tenant.objects.create(name="WP-13 Filter Tenant", slug="wp-13-filter-tenant")
        rule = AlertRule.objects.create(
            name="WP-13 Filter Rule", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=1, tenant=tenant
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        delivered = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="delivered",
            message="m",
            content_type=content_type,
            object_id=1,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_DELIVERED,
        )
        failed = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="failed",
            message="m",
            content_type=content_type,
            object_id=2,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_FAILED,
        )
        pending = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="pending",
            message="m",
            content_type=content_type,
            object_id=3,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_PENDING,
        )
        filterset = AlertLogFilterSet(
            {"delivery_outcome": [AlertLog.DELIVERY_OUTCOME_FAILED]}, queryset=AlertLog._base_manager.all()
        )
        self.assertEqual(set(filterset.qs.values_list("pk", flat=True)), {failed.pk})
        all_outcomes = AlertLogFilterSet({}, queryset=AlertLog._base_manager.all())
        self.assertEqual(set(all_outcomes.qs.values_list("pk", flat=True)), {delivered.pk, failed.pk, pending.pk})


class AlertDeliveryApiTests(TenantTestMixin, APITestCase):
    permissions = ["extras.view_alertlog"]

    def setUp(self):
        self.setup_tenant_context(name="WP-13 API A", slug="wp-13-api-a", permissions=self.permissions)
        self.tenant_a = self.tenant
        self.tenant_b = Tenant.objects.create(name="WP-13 API B", slug="wp-13-api-b")
        self.rule_a = AlertRule.objects.create(
            tenant=self.tenant_a, name="WP-13 API Rule A", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=1
        )
        self.rule_b = AlertRule._base_manager.create(
            tenant=self.tenant_b, name="WP-13 API Rule B", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=1
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        self.alert_a_delivered = AlertLog._base_manager.create(
            tenant=self.tenant_a,
            rule=self.rule_a,
            subject="A delivered",
            message="m",
            content_type=content_type,
            object_id=1,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_DELIVERED,
            delivery_attempts=1,
            last_delivery_id="run-a",
            last_delivery_error=None,
        )
        self.alert_a_failed = AlertLog._base_manager.create(
            tenant=self.tenant_a,
            rule=self.rule_a,
            subject="A failed",
            message="m",
            content_type=content_type,
            object_id=2,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_FAILED,
            delivery_attempts=1,
            last_delivery_id="run-b",
            last_delivery_error="SMTPException",
        )
        self.alert_b_failed = AlertLog._base_manager.create(
            tenant=self.tenant_b,
            rule=self.rule_b,
            subject="B failed secret",
            message="B secret",
            content_type=content_type,
            object_id=2,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_FAILED,
            last_delivery_error="SMTPException",
        )
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

    def _list(self):
        return reverse("api:extras_api:alertlog-list")

    @staticmethod
    def _rows(response):
        data = response.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def test_api_serializes_delivery_observability_fields(self):
        response = self.client.get(self._list())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = next((r for r in self._rows(response) if r["id"] == self.alert_a_failed.pk))
        self.assertEqual(row["delivery_outcome"], AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(row["delivery_attempts"], 1)
        self.assertEqual(row["last_delivery_id"], "run-b")
        self.assertEqual(row["last_delivery_error"], "SMTPException")
        self.assertIn("delivery_status", row)

    def test_delivery_outcome_filter_is_applied_within_tenant(self):
        response = self.client.get(self._list(), {"delivery_outcome": AlertLog.DELIVERY_OUTCOME_FAILED})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = self._rows(response)
        self.assertEqual({row["id"] for row in rows}, {self.alert_a_failed.pk})
        self.assertNotIn("B failed secret", str(response.data))
        self.assertNotIn(self.alert_a_delivered.pk, {row["id"] for row in rows})

    def test_tenant_b_failure_payload_is_never_visible_to_tenant_a(self):
        response = self.client.get(self._list(), {"delivery_outcome": AlertLog.DELIVERY_OUTCOME_FAILED})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotIn(self.alert_b_failed.pk, {row["id"] for row in self._rows(response)})
        self.assertNotIn("B secret", str(response.data))

    def test_no_manual_redelivery_route_exists(self):
        with self.assertRaises(NoReverseMatch):
            reverse("extras:alertlog_redeliver", kwargs={"pk": self.alert_a_failed.pk})


class AlertLifecycleIndependentOfDeliveryTests(TransactionTestCase):
    """Delivery failure must not gate creation, acknowledgement, or resolution."""

    def test_lifecycle_survives_every_channel_failing(self):
        from unittest.mock import patch

        from assets.models import Manufacturer
        from core.events import DeliveryDisposition, DeliveryResult
        from core.tasks.context import TaskContext
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        tenant = Tenant.objects.create(name="WP-13 Lifecycle Tenant", slug="wp-13-lifecycle-tenant")
        user = _tenant_member(tenant, "wp13-lifecycle")
        rule = AlertRule.objects.create(
            name="WP-13 Lifecycle Rule", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=5, tenant=tenant
        )
        channel = NotificationChannel.objects.create(
            name="WP-13 failing channel",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=tenant,
            enabled=True,
            config={"recipient_users": [user.pk]},
        )
        rule.channels.add(channel)
        manufacturer = Manufacturer.objects.create(name="WP-13 Lifecycle Mfr", slug="wp-13-lifecycle-mfr")
        site = Site.objects.create(name="WP-13 Lifecycle Site", slug="wp-13-lifecycle-site", tenant=tenant)
        location = Location.objects.create(
            name="WP-13 Lifecycle Loc", slug="wp-13-lifecycle-loc", tenant=tenant, site=site
        )
        accessory = Accessory.objects.create(
            name="WP-13 Lifecycle Accessory",
            slug="wp-13-lifecycle-accessory",
            manufacturer=manufacturer,
            tenant=tenant,
            min_qty=5,
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        failure = DeliveryResult("in_app.deliver", DeliveryDisposition.TERMINAL, True, "backend down", "timeout")
        with patch("extras.tasks.alerts.send_notification_to_channel", return_value=failure):
            with TaskContext(tenant_id=tenant.pk):
                with transaction.atomic():
                    fresh = _evaluate_rule(rule, timezone.now().date(), {})
        alert = AlertLog.unscoped.get(rule=rule)
        self.assertEqual(fresh, 1)
        self.assertEqual(alert.status, AlertLog.STATUS_ACTIVE, "creation is not gated by delivery")
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(alert.last_delivery_error, "timeout")
        alert.status = AlertLog.STATUS_ACKNOWLEDGED
        alert.acknowledged_by = user
        alert.save(update_fields=["status", "acknowledged_by"])
        alert.refresh_from_db()
        self.assertEqual(alert.status, AlertLog.STATUS_ACKNOWLEDGED)
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_FAILED)
        alert.status = AlertLog.STATUS_RESOLVED
        alert.resolved_by = user
        alert.save(update_fields=["status", "resolved_by"])
        alert.refresh_from_db()
        self.assertEqual(alert.status, AlertLog.STATUS_RESOLVED)
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(alert.delivery_attempts, 1)


class AlertDeliveryTableRenderTests(TestCase):
    def test_render_delivery_badges(self):
        tenant = Tenant.objects.create(name="WP-13 Table Tenant", slug="wp-13-table-tenant")
        rule = AlertRule.objects.create(
            name="WP-13 Table Rule", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=1, tenant=tenant
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        table = AlertLogTable([])
        delivered = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="d",
            message="m",
            content_type=content_type,
            object_id=1,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_DELIVERED,
            delivery_status={"7": {"disposition": "success", "operation": "in_app.deliver"}},
        )
        self.assertIn("badge bg-success", table.render_delivery(delivered))
        failed = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="f",
            message="m",
            content_type=content_type,
            object_id=2,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_FAILED,
            delivery_status={
                "7": {"disposition": "terminal", "operation": "email.deliver", "error_class": "SMTPException"}
            },
        )
        rendered = table.render_delivery(failed)
        self.assertIn("badge bg-danger", rendered)
        self.assertIn("SMTPException", rendered)
        pending = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="p",
            message="m",
            content_type=content_type,
            object_id=3,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_PENDING,
            delivery_status={"__dispatch__": "pending"},
        )
        self.assertIn("badge bg-info", table.render_delivery(pending))
        no_channels = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="n",
            message="m",
            content_type=content_type,
            object_id=4,
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_NONE,
            delivery_status={"__no_channels__": "no channels attached to this rule"},
        )
        self.assertIn("badge bg-secondary", table.render_delivery(no_channels))


class AlertStableChannelGateTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.create(name="Issue 567 delivery tenant", slug="issue-567-delivery-tenant")
        self.rule = AlertRule.objects.create(
            name="Issue 567 tenant rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=self.tenant,
        )
        self.match = {"subject": "Issue 567 alert", "message": "Low stock", "tenant": self.tenant}

    def _channel(self, name, *, enabled, tenant=None):
        channel = NotificationChannel.objects.create(
            name=name,
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=self.tenant if tenant is None else tenant,
            enabled=enabled,
        )
        return channel

    def test_disabled_channel_is_skipped_and_enabled_sibling_still_delivers(self):
        from unittest.mock import patch

        disabled = self._channel("Issue 567 disabled sibling", enabled=False)
        enabled = self._channel("Issue 567 enabled sibling", enabled=True)
        self.rule.channels.add(disabled, enabled)
        success = DeliveryResult("in_app.deliver", DeliveryDisposition.SUCCESS)

        with patch("extras.tasks.alerts.send_notification_to_channel", return_value=success) as sender:
            delivery = _dispatch_channels(self.rule, self.match, None, delivery_id="enabled-sibling-run")

        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0].pk, enabled.pk)
        self.assertIn(str(enabled.pk), delivery)
        self.assertNotIn(str(disabled.pk), delivery)

    def test_all_disabled_channels_record_disabled_reason_and_none_outcome(self):
        from unittest.mock import patch

        self.rule.channels.add(
            self._channel("Issue 567 disabled A", enabled=False),
            self._channel("Issue 567 disabled B", enabled=False),
        )

        with patch("extras.tasks.alerts.send_notification_to_channel") as sender:
            delivery = _dispatch_channels(self.rule, self.match, None)

        self.assertEqual(delivery, {"__no_enabled_channels__": "every attached channel is disabled"})
        self.assertEqual(_delivery_outcome(delivery), AlertLog.DELIVERY_OUTCOME_NONE)
        sender.assert_not_called()

    def test_out_of_scope_channel_attachments_never_dispatch(self):
        from unittest.mock import patch

        from core.tasks.context import TaskContext

        tenant_rule = AlertRule._base_manager.get(pk=self.rule.pk)
        global_rule = AlertRule._base_manager.create(
            name="Issue 567 platform rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=None,
        )
        global_channel = NotificationChannel._base_manager.create(
            name="Issue 567 platform channel",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=None,
            enabled=True,
        )
        tenant_channel = NotificationChannel._base_manager.create(
            name="Issue 567 tenant channel",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=self.tenant,
            enabled=True,
        )
        with TaskContext(tenant_id=None, user_id=None):
            tenant_rule.channels.add(global_channel)
            global_rule.channels.add(tenant_channel)

        with patch("extras.tasks.alerts.send_notification_to_channel") as sender:
            with TaskContext(tenant_id=self.tenant.pk, user_id=None):
                tenant_delivery = _dispatch_channels(tenant_rule, self.match, None)
            with TaskContext(tenant_id=None, user_id=None):
                global_delivery = _dispatch_channels(global_rule, self.match, None)

        self.assertEqual(tenant_delivery, {"__no_channels__": "no channels attached to this rule"})
        self.assertEqual(global_delivery, {"__no_channels__": "no channels attached to this rule"})
        sender.assert_not_called()

    def test_reenabling_a_channel_restores_delivery(self):
        from unittest.mock import patch

        from assets.models import Manufacturer
        from extras.tasks.alerts import run_alert_rule_now
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        channel = self._channel("Issue 567 re-enabled channel", enabled=False)
        self.rule.channels.add(channel)
        disabled_delivery = _dispatch_channels(self.rule, self.match, None)
        self.assertEqual(disabled_delivery, {"__no_enabled_channels__": "every attached channel is disabled"})
        self.assertEqual(_delivery_outcome(disabled_delivery), AlertLog.DELIVERY_OUTCOME_NONE)

        channel.enabled = True
        channel.save(update_fields=["enabled"])
        manufacturer = Manufacturer.objects.create(name="Issue 567 stock maker", slug="issue-567-stock-maker")
        site = Site.objects.create(name="Issue 567 stock site", slug="issue-567-stock-site", tenant=self.tenant)
        location = Location.objects.create(
            name="Issue 567 stock location", slug="issue-567-stock-location", site=site, tenant=self.tenant
        )
        accessory = Accessory.objects.create(
            name="Issue 567 low stock accessory",
            slug="issue-567-low-stock-accessory",
            manufacturer=manufacturer,
            tenant=self.tenant,
            min_qty=5,
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        success = DeliveryResult("in_app.deliver", DeliveryDisposition.SUCCESS)

        with patch("extras.tasks.alerts.send_notification_to_channel", return_value=success) as sender:
            run_alert_rule_now(self.rule.pk)

        sender.assert_called_once()
        alert = AlertLog._base_manager.get(rule=self.rule)
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)
        self.assertEqual(alert.delivery_status[str(channel.pk)]["disposition"], DeliveryDisposition.SUCCESS.value)


class AlertDispatchConcurrencyTests(TransactionTestCase):
    """Parallel evaluations planning a dispatch for the same alert state must
    deliver exactly once: the dispatch claim is a single conditional UPDATE
    gated on the planned-from notification state (issue #567, WP4)."""

    def test_parallel_dispatch_plans_deliver_exactly_once(self):
        import threading
        from unittest.mock import patch

        from django.db import connection

        tenant = Tenant.objects.create(name="Concurrency Tenant", slug="concurrency-tenant")
        rule = AlertRule.objects.create(
            name="Concurrency Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="concurrency",
            message="concurrency",
            content_type=content_type,
            object_id=rule.pk,
        )
        match = {"obj": rule, "tenant": tenant, "subject": "concurrency", "message": "concurrency"}
        calls = []
        calls_lock = threading.Lock()

        def counting_dispatch(rule_arg, match_arg, alert_log_arg, delivery_id=None):
            with calls_lock:
                calls.append(delivery_id)
            return {"1": "ok"}

        barrier = threading.Barrier(2)
        errors = []

        def run_dispatch():
            try:
                barrier.wait(timeout=10)
                _schedule_alert_dispatch(rule, match, alert, None)
            except Exception as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)
            finally:
                connection.close()

        with patch("extras.tasks.alerts._dispatch_channels", side_effect=counting_dispatch):
            threads = [threading.Thread(target=run_dispatch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1, "exactly one parallel dispatch may deliver")
        alert.refresh_from_db()
        self.assertEqual(alert.delivery_attempts, 1)
        self.assertEqual(alert.last_delivery_id, calls[0])
        self.assertEqual((alert.delivery_status or {}).get("__delivery_id__"), calls[0])

    def test_in_flight_claim_is_not_recovered_by_a_parallel_evaluation(self):
        """A pending marker whose claim is still fresh must not be re-dispatched.

        The first evaluation claims its run and blocks inside the channel
        sender; a second evaluation observing the same pending alert must back
        off instead of planning and claiming a duplicate delivery.
        """
        import threading
        from unittest.mock import patch

        from django.db import connection

        from extras.tasks.alerts import _renotify_when_due

        tenant = Tenant.objects.create(name="In-flight Tenant", slug="in-flight-tenant")
        rule = AlertRule.objects.create(
            name="In-flight Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
            renotify_interval_days=1,
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="in-flight",
            message="in-flight",
            content_type=content_type,
            object_id=rule.pk,
        )
        match = {"obj": rule, "tenant": tenant, "subject": "in-flight", "message": "in-flight"}
        calls = []
        calls_lock = threading.Lock()
        sender_entered = threading.Event()
        release_sender = threading.Event()

        def blocking_dispatch(rule_arg, match_arg, alert_log_arg, delivery_id=None):
            with calls_lock:
                calls.append(delivery_id)
            sender_entered.set()
            release_sender.wait(timeout=15)
            return {"1": "ok"}

        snapshot = AlertLog._base_manager.get(pk=alert.pk)
        errors = []

        def run_first_dispatch():
            try:
                _schedule_alert_dispatch(rule, match, snapshot, None)
            except Exception as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)
            finally:
                connection.close()

        with patch("extras.tasks.alerts._dispatch_channels", side_effect=blocking_dispatch):
            thread = threading.Thread(target=run_first_dispatch)
            thread.start()
            self.assertTrue(sender_entered.wait(timeout=10), "first dispatch must reach the sender")
            try:
                claimed = AlertLog._base_manager.get(pk=alert.pk)
                # The first evaluation has claimed the run and is inside the sender.
                self.assertEqual(claimed.delivery_attempts, 1)
                self.assertEqual(claimed.delivery_status.get("__dispatch__"), "pending")
                self.assertIn("__claimed_at__", claimed.delivery_status)
                first_run_id = claimed.last_delivery_id
                self.assertEqual(len(calls), 1)

                # A parallel evaluation sees the same pending alert and must not
                # plan or claim a second dispatch while the claim is in flight.
                scheduled = set()
                _renotify_when_due(rule, match, claimed, timezone.now(), ("key",), scheduled)
                self.assertEqual(scheduled, set(), "an in-flight claim must not be re-dispatched")
                still = AlertLog._base_manager.get(pk=alert.pk)
                self.assertEqual(still.delivery_attempts, 1)
                self.assertEqual(still.last_delivery_id, first_run_id)
                self.assertEqual(len(calls), 1)
            finally:
                release_sender.set()
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1, "exactly one outbound delivery")
        final = AlertLog._base_manager.get(pk=alert.pk)
        self.assertEqual(final.delivery_attempts, 1)
        self.assertEqual(final.last_delivery_id, calls[0])
        self.assertEqual((final.delivery_status or {}).get("__delivery_id__"), calls[0])
        self.assertEqual(final.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)

    def test_stale_claim_is_recovered_after_the_lease(self):
        """A crashed run's claim is still recovered once the lease expired."""
        from unittest.mock import patch

        from extras.tasks.alerts import _DISPATCH_CLAIM_LEASE, _renotify_when_due

        tenant = Tenant.objects.create(name="Stale claim tenant", slug="stale-claim-tenant")
        rule = AlertRule.objects.create(
            name="Stale claim rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
            renotify_interval_days=1,
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        stale_at = timezone.now() - _DISPATCH_CLAIM_LEASE - timezone.timedelta(minutes=5)
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="stale",
            message="stale",
            content_type=content_type,
            object_id=rule.pk,
            delivery_status={
                "__dispatch__": "pending",
                "__delivery_id__": "dead-run",
                "__claimed_at__": stale_at.isoformat(),
            },
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_PENDING,
            delivery_attempts=1,
            last_delivery_id="dead-run",
            last_notified_at=stale_at,
        )
        match = {"obj": rule, "tenant": tenant, "subject": "stale", "message": "stale"}
        calls = []

        def counting_dispatch(rule_arg, match_arg, alert_log_arg, delivery_id=None):
            calls.append(delivery_id)
            return {"1": "ok"}

        with patch("extras.tasks.alerts._dispatch_channels", side_effect=counting_dispatch):
            scheduled = set()
            _renotify_when_due(rule, match, alert, timezone.now(), ("key",), scheduled)

        self.assertEqual(scheduled, {("key",)}, "a stale claim must be recovered")
        self.assertEqual(len(calls), 1, "a crashed claim is recovered exactly once")
        self.assertNotEqual(calls[0], "dead-run")
        recovered = AlertLog._base_manager.get(pk=alert.pk)
        self.assertEqual(recovered.last_delivery_id, calls[0])
        self.assertEqual(recovered.delivery_attempts, 2)
        self.assertEqual(recovered.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)

    def test_completion_update_is_fenced_to_its_delivery_id(self):
        """A superseded run must not overwrite a newer attempt's final state."""
        import threading
        from unittest.mock import patch

        from django.db import connection

        tenant = Tenant.objects.create(name="Fence Tenant", slug="fence-tenant")
        rule = AlertRule.objects.create(
            name="Fence Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="fence",
            message="fence",
            content_type=content_type,
            object_id=rule.pk,
        )
        match = {"obj": rule, "tenant": tenant, "subject": "fence", "message": "fence"}
        calls = []
        sender_entered = threading.Event()
        release_sender = threading.Event()

        def blocking_dispatch(rule_arg, match_arg, alert_log_arg, delivery_id=None):
            calls.append(delivery_id)
            sender_entered.set()
            release_sender.wait(timeout=15)
            return {"1": "ok"}

        snapshot = AlertLog._base_manager.get(pk=alert.pk)
        errors = []

        def run_dispatch():
            try:
                _schedule_alert_dispatch(rule, match, snapshot, None)
            except Exception as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)
            finally:
                connection.close()

        with patch("extras.tasks.alerts._dispatch_channels", side_effect=blocking_dispatch):
            thread = threading.Thread(target=run_dispatch)
            thread.start()
            self.assertTrue(sender_entered.wait(timeout=10), "dispatch must reach the sender")
            try:
                # A newer attempt supersedes the in-flight run while it is
                # blocked inside the sender (e.g. a takeover after the lease).
                AlertLog._base_manager.filter(pk=alert.pk).update(
                    last_delivery_id="successor-run",
                    delivery_status={"__dispatch__": "pending", "__delivery_id__": "successor-run"},
                )
            finally:
                release_sender.set()
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        fenced = AlertLog._base_manager.get(pk=alert.pk)
        self.assertEqual(fenced.last_delivery_id, "successor-run")
        self.assertEqual((fenced.delivery_status or {}).get("__delivery_id__"), "successor-run")
        self.assertNotEqual(fenced.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)
