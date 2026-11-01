import hashlib
import hmac
import json
import uuid
from unittest.mock import MagicMock, patch

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from assets.models import Manufacturer
from core.events import send_notification_to_channel
from core.models import Notification
from extras.models import Event, EventRule, NotificationChannel, WebhookDelivery, WebhookEndpoint
from extras.services.events import _check_conditions, _enqueue_delivery, dispatch_event
from organization.models import Location, Tenant


class EventsSystemTestCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.manufacturer_ct = ContentType.objects.get_for_model(Manufacturer)

    def _subscribe(self, events):
        """Events are only recorded for subscribed models (#621)."""
        return EventRule.objects.create(
            name="subscription",
            model=self.manufacturer_ct,
            events=events,
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )

    @override_settings(Q_CLUSTER={"sync": False})
    def test_webhook_enqueue_waits_for_commit(self):
        delivery = WebhookDelivery._base_manager.create(
            delivery_id=str(uuid.uuid4()),
            status=WebhookDelivery.STATUS_PENDING,
            test_send=True,
            payload_timestamp=timezone.now(),
        )
        with patch("extras.services.events.async_task") as async_task:
            with transaction.atomic():
                _enqueue_delivery(delivery, actor_id=None, request_id="issue445-on-commit")
                async_task.assert_not_called()
            async_task.assert_called_once()
        assertions = async_task.call_args.args[1]
        self.assertEqual(assertions.delivery_pk, delivery.pk)
        self.assertEqual(async_task.call_args.kwargs["request_id"], "issue445-on-commit")

    @patch("core.http.request_pinned")
    def test_event_dispatch_on_create_update_delete(self, mock_request_pinned):
        self._subscribe(["create", "update", "delete"])
        # Create
        mfr = Manufacturer.objects.create(name="Lenovo", slug="lenovo")

        # Verify event was dispatched (should have created an Event)
        event_create = Event.objects.filter(model=self.manufacturer_ct, object_id=mfr.pk, action="create").first()
        self.assertIsNotNone(event_create)
        self.assertEqual(event_create.data, {"app_label": "assets", "model_name": "manufacturer"})

        # Update
        mfr.name = "Lenovo Inc"
        mfr.save()
        event_update = Event.objects.filter(model=self.manufacturer_ct, object_id=mfr.pk, action="update").first()
        self.assertIsNotNone(event_update)

        # Delete
        mfr_pk = mfr.pk
        mfr.delete()
        event_delete = Event.objects.filter(model=self.manufacturer_ct, object_id=mfr_pk, action="delete").first()
        self.assertIsNotNone(event_delete)

    def test_delete_event_preserves_original_object_id_until_commit(self):
        self._subscribe(["delete"])
        mfr = Manufacturer.objects.create(name="Commit Identity Mfr", slug="commit-identity-mfr")
        object_id = mfr.pk

        with transaction.atomic():
            mfr.delete()
            self.assertFalse(
                Event.objects.filter(model=self.manufacturer_ct, object_id=object_id, action="delete").exists()
            )

        self.assertTrue(Event.objects.filter(model=self.manufacturer_ct, object_id=object_id, action="delete").exists())

    def test_delete_rollback_does_not_emit_event(self):
        mfr = Manufacturer.objects.create(name="Rollback Mfr", slug="rollback-mfr")
        object_id = mfr.pk

        with self.assertRaisesRegex(RuntimeError, "rollback event"):
            with transaction.atomic():
                mfr.delete()
                raise RuntimeError("rollback event")

        self.assertFalse(
            Event.objects.filter(model=self.manufacturer_ct, object_id=object_id, action="delete").exists()
        )

    @patch("extras.services.events.process_event_rules")
    def test_explicit_object_id_overrides_instance_primary_key(self, process_event_rules):
        self._subscribe(["delete"])
        mfr = Manufacturer.objects.create(name="Explicit Identity Mfr", slug="explicit-identity-mfr")
        explicit_object_id = mfr.pk + 1000
        process_event_rules.reset_mock()

        dispatch_event(Manufacturer, mfr, "delete", object_id=explicit_object_id)

        event = Event.objects.get(model=self.manufacturer_ct, object_id=explicit_object_id, action="delete")
        self.assertEqual(event.object_id, explicit_object_id)
        process_event_rules.assert_called_once()

    def test_missing_object_id_is_skipped_without_creating_event(self):
        unsaved = Manufacturer(name="Missing Identity Mfr", slug="missing-identity-mfr")

        with self.assertLogs("extras.services.events", level="ERROR") as logs:
            dispatch_event(Manufacturer, unsaved, "delete")

        self.assertTrue(any("missing object id" in message for message in logs.output))
        self.assertFalse(Event.objects.filter(model=self.manufacturer_ct, action="delete").exists())

    def test_event_rule_conditions_evaluation(self):
        """B7: authored conditions now fail closed for 1.0, so no notification is dispatched."""
        # Create a rule with an "and" condition
        EventRule.objects.create(
            name="Test Rule with Conditions",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            action_config={
                "level": "warning",
                "subject": "Alert: {event.action} on {event.model.model}",
                "body": "Details: {data}",
            },
            conditions={
                "type": "and",
                "rules": [
                    {"field": "model_name", "op": "eq", "value": "manufacturer"},
                    {"field": "app_label", "op": "contains", "value": "asset"},
                ],
            },
            enabled=True,
        )

        # Fire event manually
        event = Event.objects.create(
            model=self.manufacturer_ct,
            object_id=999,
            action="create",
            data={"app_label": "assets", "model_name": "manufacturer"},
        )
        # Authored conditions are withdrawn and must not dispatch.
        dispatch_event(Manufacturer, event, "create")

        notification = Notification.objects.filter(level="warning").first()
        self.assertIsNone(notification)

    def test_unknown_or_incomplete_conditions_fail_closed(self):
        cases = [
            {"field": "model_name", "op": "unknown", "value": "manufacturer"},
            {"op": "eq", "value": "manufacturer"},
            {"field": "model_name", "value": "manufacturer"},
        ]

        for index, conditions in enumerate(cases):
            with self.subTest(conditions=conditions):
                rule = EventRule.objects.create(
                    name=f"Fail-closed condition rule {index}",
                    model=self.manufacturer_ct,
                    events=["create"],
                    action_type=EventRule.ACTION_NOTIFICATION,
                    action_config={"subject": f"FAIL-CLOSED-{index}"},
                    conditions=conditions,
                    enabled=True,
                )
                event = Event.objects.create(
                    model=self.manufacturer_ct,
                    object_id=1000 + index,
                    action="create",
                    data={"app_label": "assets", "model_name": "manufacturer"},
                )

                self.assertFalse(_check_conditions(conditions, event))
                dispatch_event(Manufacturer, event, "create")

                self.assertFalse(Notification.objects.filter(subject=rule.action_config["subject"]).exists())
        self.assertFalse(_check_conditions([], event))

    def test_empty_conditions_continue_to_match(self):
        for index, conditions in enumerate(({}, {"rules": []})):
            with self.subTest(conditions=conditions):
                rule = EventRule.objects.create(
                    name=f"Empty condition rule {index}",
                    model=self.manufacturer_ct,
                    events=["create"],
                    action_type=EventRule.ACTION_NOTIFICATION,
                    action_config={"subject": f"EMPTY-CONDITIONS-{index}"},
                    conditions=conditions,
                    enabled=True,
                )
                event = Event.objects.create(
                    model=self.manufacturer_ct,
                    object_id=1100 + index,
                    action="create",
                    data={"app_label": "assets", "model_name": "manufacturer"},
                )

                self.assertTrue(_check_conditions(conditions, event))
                dispatch_event(Manufacturer, event, "create")

                self.assertTrue(Notification.objects.filter(subject=rule.action_config["subject"]).exists())

        # ``None`` cannot be persisted (the column is NOT NULL); the fail-open
        # evaluation path for a missing payload is exercised directly instead.
        self.assertTrue(_check_conditions(None, event))

    def test_flat_condition_does_not_dispatch(self):
        rule = EventRule.objects.create(
            name="Flat condition rule",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            action_config={"subject": "FLAT-CONDITION"},
            conditions={"field": "model_name", "op": "eq", "value": "manufacturer"},
            enabled=True,
        )
        event = Event.objects.create(
            model=self.manufacturer_ct,
            object_id=1200,
            action="create",
            data={"app_label": "assets", "model_name": "manufacturer"},
        )

        dispatch_event(Manufacturer, event, "create")

        self.assertFalse(Notification.objects.filter(subject=rule.action_config["subject"]).exists())

    @patch("extras.services.events.async_task")
    def test_webhook_rule_with_conditions_does_not_enqueue_task(self, mock_async_task):
        EventRule.objects.create(
            name="Webhook condition rule",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            action_config={"url": "https://example.com/webhook"},
            conditions={"rules": [{"field": "model_name", "op": "eq", "value": "manufacturer"}]},
            enabled=True,
        )
        event = Event.objects.create(
            model=self.manufacturer_ct,
            object_id=1300,
            action="create",
            data={"app_label": "assets", "model_name": "manufacturer"},
        )

        dispatch_event(Manufacturer, event, "create")

        mock_async_task.assert_not_called()

    def test_event_rule_conditions_withdrawn_truth_table(self):
        cases = [
            ({}, False),
            (None, False),
            ({"rules": []}, False),
            ({"type": "and", "rules": []}, False),
            ({"rules": [{"field": "model_name", "op": "eq", "value": "manufacturer"}]}, True),
            ({"field": "model_name", "op": "eq", "value": "manufacturer"}, True),
            ([], True),
            ("authored", True),
        ]

        for conditions, expected in cases:
            with self.subTest(conditions=conditions):
                rule = EventRule(conditions=conditions)
                self.assertEqual(rule.conditions_withdrawn, expected)

    def test_event_rule_conditions_json_renders_pretty_json(self):
        rule = EventRule(conditions={"rules": [{"field": "model_name", "op": "eq", "value": "manufacturer"}]})

        self.assertEqual(
            rule.conditions_json,
            '{\n  "rules": [\n    {\n      "field": "model_name",\n      "op": "eq",\n      "value": "manufacturer"\n    }\n  ]\n}',
        )

    def test_event_rules_scoped_to_instance_tenant_in_system_context(self):
        """WS5-1: in a system context (no active tenant/user) a save must fire ONLY the
        rules belonging to the saved object's OWN tenant (plus global rules), never every
        tenant's rules. Reproduces the cross-tenant dispatch the unscoped manager allowed.
        Also covers WS5-2: a tenant rule's notification fans out to the rule's members, not a
        global user=None broadcast."""
        from django.contrib.auth import get_user_model

        from core.managers import set_current_tenant
        from core.tests.mixins import grant
        from organization.models import Location, Role, Tenant

        tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        member_a = get_user_model().objects.create_user(username="member_a", password="pw")
        grant(member_a, tenant_a, Role.objects.create(tenant=tenant_a, name="R", permissions=[]))
        loc_ct = ContentType.objects.get_for_model(Location)

        EventRule.objects.create(
            name="A rule",
            tenant=tenant_a,
            model=loc_ct,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            action_config={"subject": "A-FIRED", "body": "x"},
            enabled=True,
        )
        EventRule.objects.create(
            name="B rule",
            tenant=tenant_b,
            model=loc_ct,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            action_config={"subject": "B-FIRED", "body": "x"},
            enabled=True,
        )
        EventRule.objects.create(
            name="Global rule",
            tenant=None,
            model=loc_ct,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            action_config={"subject": "GLOBAL-FIRED", "body": "x"},
            enabled=True,
        )

        # A Location owned by tenant A, dispatched in a no-tenant / no-user system context.
        set_current_tenant(None)
        loc = Location(name="Site A", tenant=tenant_a)
        loc.pk = 987654  # dispatch only needs pk + tenant_id; no real save required

        dispatch_event(Location, loc, "create")

        # WS5-2: tenant-A rule fans out to tenant-A members (not a global user=None row).
        self.assertTrue(Notification.objects.filter(subject="A-FIRED", user=member_a).exists())
        self.assertFalse(Notification.objects.filter(subject="A-FIRED", user__isnull=True).exists())
        # A truly global (tenant=None) rule still broadcasts as user=None.
        self.assertTrue(Notification.objects.filter(subject="GLOBAL-FIRED", user__isnull=True).exists())
        self.assertFalse(
            Notification.objects.filter(subject="B-FIRED").exists(),
            "Tenant B's event rule must NOT fire for a tenant-A object in a system context.",
        )

    @patch("core.http.request_pinned")
    def test_webhook_delivery_with_hmac_signature(self, mock_request_pinned):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_request_pinned.return_value = mock_response

        # Create webhook rule
        EventRule.objects.create(
            name="Test Webhook Rule",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            action_config={
                "url": "https://example.com/webhook-receiver",
                "method": "POST",
                "secret": "mysecretkey",
                "headers": {"X-Custom-Header": "CustomValue"},
            },
            enabled=True,
        )

        event = Event.objects.create(
            model=self.manufacturer_ct,
            object_id=101,
            action="create",
            data={"app_label": "assets", "model_name": "manufacturer"},
        )

        # Execute event rule action (under atomic on_commit context)
        with transaction.atomic():
            dispatch_event(Manufacturer, event, "create")

        # Verify webhook request parameters. request_pinned(method, url, headers=..., data=...,
        # timeout=...) — method/url are positional, the rest are kwargs.
        self.assertTrue(mock_request_pinned.called)
        call_args, call_kwargs = mock_request_pinned.call_args
        self.assertEqual(call_args[0], "POST")
        self.assertEqual(call_args[1], "https://example.com/webhook-receiver")

        # Verify HMAC signature
        headers = call_kwargs["headers"]
        self.assertEqual(headers["X-Custom-Header"], "CustomValue")
        self.assertIn("X-Hub-Signature-256", headers)

        body = call_kwargs["data"]
        expected_sig = hmac.new(b"mysecretkey", body.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Hub-Signature-256"], f"sha256={expected_sig}")

        payload = json.loads(body)
        dispatched_event = Event.objects.filter(
            model=self.manufacturer_ct,
            object_id=event.pk,
            action="create",
        ).latest("pk")
        self.assertEqual(
            {"schema_version", "event_id", "delivery_id", "attempt", "tenant"},
            set(payload) - {"event", "model", "object_id", "timestamp", "data"},
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["event_id"], dispatched_event.pk)
        self.assertEqual(payload["attempt"], 1)
        self.assertIsNone(payload["tenant"])
        self.assertEqual(payload["event"], "create")
        self.assertEqual(payload["model"], "assets.manufacturer")
        self.assertEqual(payload["object_id"], event.pk)
        self.assertEqual(payload["data"], {"app_label": "assets", "model_name": "manufacturer"})
        self.assertNotIn("mysecretkey", body)
        uuid.UUID(payload["delivery_id"])

    @patch("core.http.request_pinned")
    def test_webhook_envelope_is_present_in_platform_payloads(self, mock_request_pinned):
        mock_response = MagicMock(status_code=200)
        mock_request_pinned.return_value = mock_response

        envelope_tenant_a = Tenant.objects.create(name="Envelope Tenant A", slug="envelope-tenant-a")
        envelope_tenant_b = Tenant.objects.create(name="Envelope Tenant B", slug="envelope-tenant-b")

        for index, url in enumerate(
            (
                "https://hooks.slack.com/services/test",
                "https://tenant.webhook.office.com/webhookb2/test",
            ),
            start=1,
        ):
            from extras.tasks.webhooks import WebhookDeliveryAssertions, send_webhook_task

            tenant = envelope_tenant_a if index == 1 else envelope_tenant_b
            event = Event.objects.create(
                model=ContentType.objects.get_for_model(Manufacturer),
                object_id=index,
                action="create",
                data={"app_label": "assets", "model_name": "manufacturer"},
            )
            endpoint = WebhookEndpoint.objects.create(
                name=f"Envelope {index}",
                url=url,
                http_method="POST",
                headers={},
                secret="",
                retry_count=0,
                retry_backoff=0,
            )
            delivery = WebhookDelivery.objects.create(
                endpoint=endpoint,
                delivery_id=str(uuid.uuid4()),
                event=event,
                tenant=tenant,
                test_send=False,
                attempt=1,
                status=WebhookDelivery.STATUS_PENDING,
                target_url=endpoint.url,
                target_http_method=endpoint.http_method,
                target_headers=endpoint.headers,
                target_secret=endpoint.secret,
                target_enabled=True,
                target_tenant_id=endpoint.tenant_id,
                target_retry_count=endpoint.retry_count,
                target_retry_backoff=endpoint.retry_backoff,
            )
            assertions = WebhookDeliveryAssertions(
                delivery_pk=delivery.pk,
                delivery_id=uuid.UUID(str(delivery.delivery_id)),
                webhook_endpoint_id=endpoint.pk,
                event_id=event.pk,
                tenant_id=tenant.pk,
                test_send=False,
            )
            send_webhook_task(assertions=assertions, attempt=0)

            payload = mock_request_pinned.call_args.kwargs["json"]
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["event_id"], event.pk)
            self.assertEqual(payload["delivery_id"], str(delivery.delivery_id))
            self.assertEqual(payload["attempt"], 1)
            self.assertEqual(payload["tenant"], tenant.pk)
            self.assertEqual(payload["text"], f"Event: create on manufacturer (ID: {index})")
            if index == 1:
                self.assertIn("text", payload)
                self.assertNotIn("@type", payload)
            else:
                self.assertEqual(payload["@type"], "MessageCard")
                self.assertEqual(payload["@context"], "https://schema.org/extensions")
                self.assertEqual(payload["summary"], f"Event: create on manufacturer (ID: {index})")
                self.assertEqual(payload["themeColor"], "0076D7")
                self.assertEqual(payload["title"], "ITAMbox Notification")

    @patch("core.http.request_pinned")
    def test_webhook_tenant_comes_from_object_not_ambient_context(self, mock_request_pinned):
        mock_request_pinned.return_value = MagicMock(status_code=200)
        from core.managers import set_current_tenant

        tenant_a = Tenant.objects.create(name="Envelope A", slug="envelope-a")
        tenant_b = Tenant.objects.create(name="Envelope B", slug="envelope-b")
        endpoint = WebhookEndpoint.objects.create(
            name="Global envelope endpoint",
            url="https://example.com/envelope",
            tenant=None,
        )
        EventRule.objects.create(
            name="Global envelope rule",
            model=ContentType.objects.get_for_model(Location),
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=endpoint,
            tenant=None,
            enabled=True,
        )
        location = Location(name="Tenant A location", tenant=tenant_a)
        location.pk = 876543

        set_current_tenant(tenant_b)
        try:
            dispatch_event(Location, location, "create")
        finally:
            set_current_tenant(None)

        payload = json.loads(mock_request_pinned.call_args.kwargs["data"])
        self.assertEqual(payload["tenant"], tenant_a.pk)
        self.assertNotEqual(payload["tenant"], tenant_b.pk)

    def test_cross_tenant_rule_endpoint_validation_remains_enforced(self):
        tenant_a = Tenant.objects.create(name="Validation A", slug="validation-a")
        tenant_b = Tenant.objects.create(name="Validation B", slug="validation-b")
        endpoint = WebhookEndpoint.objects.create(
            name="Tenant B endpoint",
            url="https://example.com/tenant-b",
            tenant=tenant_b,
        )
        rule = EventRule(
            name="Tenant A rule",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=endpoint,
            tenant=tenant_a,
        )

        with self.assertRaises(ValidationError):
            rule.full_clean()

    @patch("core.http.request_pinned")
    def test_webhook_delivery_via_linked_endpoint(self, mock_request_pinned):
        # A rule linked to a WebhookEndpoint sources URL/method/headers/secret/retry from
        # the endpoint — no url/secret needed in action_config.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_request_pinned.return_value = mock_response

        endpoint = WebhookEndpoint.objects.create(
            name="Linked Endpoint",
            url="https://example.com/linked-receiver",
            http_method="POST",
            secret="endpoint-secret",
            headers={"X-From": "endpoint"},
            retry_count=5,
            retry_backoff=30,
        )
        EventRule.objects.create(
            name="Linked Webhook Rule",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=endpoint,
            action_config={},
            enabled=True,
        )
        event = Event.objects.create(
            model=self.manufacturer_ct,
            object_id=202,
            action="create",
            data={"app_label": "assets", "model_name": "manufacturer"},
        )

        with transaction.atomic():
            dispatch_event(Manufacturer, event, "create")

        self.assertTrue(mock_request_pinned.called)
        call_args, call_kwargs = mock_request_pinned.call_args
        self.assertEqual(call_args[1], "https://example.com/linked-receiver")
        self.assertEqual(call_args[0], "POST")

        headers = call_kwargs["headers"]
        self.assertEqual(headers["X-From"], "endpoint")
        body = call_kwargs["data"]
        expected_sig = hmac.new(
            endpoint.secret_decrypted.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-Hub-Signature-256"], f"sha256={expected_sig}")

    @patch("core.http.request_pinned")
    def test_disabled_linked_endpoint_suppresses_delivery(self, mock_request_pinned):
        endpoint = WebhookEndpoint.objects.create(
            name="Disabled Endpoint",
            url="https://example.com/disabled",
            http_method="POST",
            enabled=False,
        )
        EventRule.objects.create(
            name="Rule With Disabled Endpoint",
            model=self.manufacturer_ct,
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=endpoint,
            enabled=True,
        )
        event = Event.objects.create(
            model=self.manufacturer_ct,
            object_id=203,
            action="create",
            data={"app_label": "assets", "model_name": "manufacturer"},
        )

        with transaction.atomic():
            dispatch_event(Manufacturer, event, "create")

        self.assertFalse(mock_request_pinned.called)

    def test_legacy_script_rule_does_not_crash(self):
        # Rows with action_type='script' may exist in the DB from before the action was
        # removed. They must be silently skipped without raising.
        EventRule.objects.filter(pk__gt=0).delete()
        EventRule.objects.create(
            name="Legacy Script Rule",
            model=self.manufacturer_ct,
            events=["update"],
            action_type="script",  # no longer a valid choice, but old rows may exist
            action_config={"script": "legacy.py"},
            enabled=True,
        )
        mfr = Manufacturer.objects.create(name="LegacyTest", slug="legacy-test-mfr")
        # Must not raise; a script-only rule can never act, so no Event is recorded (#621).
        dispatch_event(Manufacturer, mfr, "update")
        self.assertFalse(Event.objects.filter(model=self.manufacturer_ct, object_id=mfr.pk, action="update").exists())

    @patch("core.http.request_pinned")
    def test_notification_channels(self, mock_request_pinned):
        from django.contrib.auth import get_user_model

        User = get_user_model()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_request_pinned.return_value = mock_resp

        # Slack Channel
        slack_channel = NotificationChannel.objects.create(
            name="Slack Devs",
            channel_type=NotificationChannel.TYPE_SLACK,
            config={"webhook_url": "https://hooks.slack.com/services/abc"},
        )

        # Teams Channel
        teams_channel = NotificationChannel.objects.create(
            name="Teams Alerts",
            channel_type=NotificationChannel.TYPE_TEAMS,
            config={"webhook_url": "https://webhook.office.com/webhookb2/xyz"},
        )

        # In-App Channel — needs a staff user to receive the notification
        staff_user = User.objects.create_user(username="channel_staff", password="x", is_staff=True, is_active=True)
        in_app_channel = NotificationChannel.objects.create(
            name="In-App Feed", channel_type=NotificationChannel.TYPE_IN_APP
        )

        # Test Slack sending. _post_pinned calls request_pinned('POST', url, json=payload,
        # timeout=10) — method/url positional, url is the second positional arg.
        res = send_notification_to_channel(slack_channel, "Subject Slack", "Body Slack")
        self.assertTrue(res)
        call_args, call_kwargs = mock_request_pinned.call_args
        self.assertEqual(call_args[0], "POST")
        self.assertIn("hooks.slack.com", call_args[1])
        self.assertEqual(call_kwargs["timeout"], 10)

        # Test Teams sending
        res = send_notification_to_channel(teams_channel, "Subject Teams", "Body Teams")
        self.assertTrue(res)
        call_args, call_kwargs = mock_request_pinned.call_args
        self.assertEqual(call_args[0], "POST")
        self.assertIn("webhook.office.com", call_args[1])

        # Test In-App Notification creation — creates one notification per resolved user
        initial_count = Notification.objects.count()
        res = send_notification_to_channel(in_app_channel, "Subject In-App", "Body In-App")
        self.assertTrue(res)
        self.assertGreater(Notification.objects.count(), initial_count)
        notif = Notification.objects.filter(user=staff_user).latest("pk")
        self.assertEqual(notif.subject, "Subject In-App")


class LeanEventPipelineTests(TransactionTestCase):
    """No Event row or rule transaction for saves that no rule could act on (#621)."""

    def setUp(self):
        super().setUp()
        self.ct = ContentType.objects.get_for_model(Manufacturer)

    def _rule(self, **overrides):
        values = {
            "name": "lean rule",
            "model": self.ct,
            "events": ["create"],
            "action_type": EventRule.ACTION_NOTIFICATION,
            "enabled": True,
        }
        values.update(overrides)
        return EventRule.objects.create(**values)

    def test_no_rules_writes_no_event_and_opens_no_locked_transaction(self):
        with patch("extras.services.events.process_event_rules") as process:
            Manufacturer.objects.create(name="Lean None", slug="lean-none")
        self.assertFalse(Event.objects.filter(model=self.ct).exists())
        process.assert_not_called()

    def test_rule_for_other_action_writes_no_event(self):
        self._rule(events=["delete"])
        Manufacturer.objects.create(name="Lean Other Action", slug="lean-other-action")
        self.assertFalse(Event.objects.filter(model=self.ct, action="create").exists())

    def test_rule_for_other_model_writes_no_event(self):
        self._rule(model=ContentType.objects.get_for_model(Location))
        Manufacturer.objects.create(name="Lean Other Model", slug="lean-other-model")
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

    def test_disabled_and_soft_deleted_rules_write_no_event(self):
        self._rule(enabled=False, name="off")
        self._rule(name="gone").delete()
        Manufacturer.objects.create(name="Lean Disabled", slug="lean-disabled")
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

    def test_withdrawn_conditions_write_no_event(self):
        self._rule(conditions={"rules": [{"field": "model_name", "op": "eq", "value": "manufacturer"}]})
        Manufacturer.objects.create(name="Lean Withdrawn", slug="lean-withdrawn")
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

    def test_other_tenant_rule_writes_no_event_but_global_rule_does(self):
        tenant = Tenant.objects.create(name="Lean Tenant", slug="lean-tenant")
        self._rule(tenant=tenant, name="tenant rule")
        Manufacturer.objects.create(name="Lean Tenantless", slug="lean-tenantless")
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

        self._rule(name="global rule")
        mfr = Manufacturer.objects.create(name="Lean Global Hit", slug="lean-global-hit")
        self.assertTrue(Event.objects.filter(model=self.ct, object_id=mfr.pk, action="create").exists())

    def test_rule_writes_invalidate_the_cached_index(self):
        Manufacturer.objects.create(name="Lean Before", slug="lean-before")
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

        rule = self._rule()
        mfr = Manufacturer.objects.create(name="Lean After Create", slug="lean-after-create")
        self.assertTrue(Event.objects.filter(model=self.ct, object_id=mfr.pk).exists())

        rule.enabled = False
        rule.save()
        other = Manufacturer.objects.create(name="Lean After Disable", slug="lean-after-disable")
        self.assertFalse(Event.objects.filter(model=self.ct, object_id=other.pk).exists())

        rule.enabled = True
        rule.save()
        rule.delete()
        last = Manufacturer.objects.create(name="Lean After Delete", slug="lean-after-delete")
        self.assertFalse(Event.objects.filter(model=self.ct, object_id=last.pk).exists())

    def test_per_save_event_overhead_is_bounded(self):
        Manufacturer.objects.create(name="Lean Warm", slug="lean-warm")  # warm ContentType + index caches
        with CaptureQueriesContext(connection) as queries:
            dispatch_event(Manufacturer, Manufacturer(pk=1), "create")
        # Index served from cache: no Event insert, no select_for_update, no rule query.
        self.assertEqual(len(queries), 0, [q["sql"] for q in queries.captured_queries])

    def test_eligible_save_keeps_the_event_pipeline(self):
        self._rule()
        mfr = Manufacturer.objects.create(name="Lean Eligible", slug="lean-eligible")
        event = Event.objects.get(model=self.ct, object_id=mfr.pk, action="create")
        self.assertTrue(event.processed)
        self.assertEqual(event.data, {"app_label": "assets", "model_name": "manufacturer"})
