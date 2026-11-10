"""Regression coverage for issue #183 alert audit and tenant disclosure."""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import ObjectChange
from core.tasks.context import TaskContext
from core.tests.mixins import TenantTestMixin
from extras.models import AlertLog, AlertRule
from organization.models import Tenant

User = get_user_model()


class AlertAuditAndDisclosureTests(TenantTestMixin, TestCase):
    permissions = ["extras.view_alertlog", "extras.change_alertlog"]

    def setUp(self):
        self.setup_tenant_context(
            name="Issue 183 Tenant A",
            slug="issue-183-tenant-a",
            permissions=self.permissions,
        )
        self.tenant_a = self.tenant
        self.tenant_b = Tenant.objects.create(name="Issue 183 Tenant B", slug="issue-183-tenant-b")
        self.rule_a = AlertRule.objects.create(
            tenant=self.tenant_a,
            name="Issue 183 Rule A",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=1,
        )
        self.rule_b = AlertRule._base_manager.create(
            tenant=self.tenant_b,
            name="Issue 183 Rule B",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=1,
        )

    def _alert(self, *, tenant, rule, subject="Issue 183 Alert"):
        return AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject=subject,
            message="test message",
            content_type=ContentType.objects.get_for_model(AlertRule),
            object_id=rule.pk,
        )

    def test_system_create_and_human_lifecycle_each_write_one_audit_row(self):
        with TaskContext(tenant_id=self.tenant_a.pk):
            alert = AlertLog.objects.create(
                tenant=self.tenant_a,
                rule=self.rule_a,
                subject="Low stock",
                message="Stock is low",
                content_type=ContentType.objects.get_for_model(AlertRule),
                object_id=self.rule_a.pk,
            )

        alert_ct = ContentType.objects.get_for_model(AlertLog)
        changes = ObjectChange._base_manager.filter(
            changed_object_type=alert_ct,
            changed_object_id=alert.pk,
        ).order_by("time", "pk")
        self.assertEqual(changes.count(), 1)
        self.assertEqual(changes[0].action, "create")
        self.assertIsNone(changes[0].user)
        self.assertEqual(changes[0].tenant_id, self.tenant_a.pk)
        self.assertIsNotNone(changes[0].request_id)

        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.post(reverse("extras:alertlog_acknowledge", kwargs={"pk": alert.pk}))
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("extras:alertlog_resolve", kwargs={"pk": alert.pk}))
        self.assertEqual(response.status_code, 302)

        changes = ObjectChange._base_manager.filter(
            changed_object_type=alert_ct,
            changed_object_id=alert.pk,
        ).order_by("time", "pk")
        self.assertEqual(changes.count(), 3)
        self.assertEqual([change.action for change in changes], ["create", "update", "update"])
        self.assertIsNone(changes[0].user_id)
        self.assertEqual(changes[1].user_id, self.tenant_user.pk)
        self.assertEqual(changes[2].user_id, self.tenant_user.pk)
        self.assertTrue(all(change.tenant_id == self.tenant_a.pk for change in changes))

    def test_content_object_safe_keeps_global_manufacturer_target(self):
        from assets.models import Manufacturer

        manufacturer = Manufacturer.objects.create(
            name="Issue 183 Global Manufacturer", slug="issue-183-global-manufacturer"
        )
        alert = AlertLog._base_manager.create(
            tenant=self.tenant_a,
            rule=self.rule_a,
            subject="Global target",
            message="global target message",
            content_type=ContentType.objects.get_for_model(Manufacturer),
            object_id=manufacturer.pk,
        )
        self.assertEqual(alert.content_object_safe.pk, manufacturer.pk)

    def test_content_object_safe_never_discloses_foreign_tenant_target(self):
        self.set_active_tenant(self.tenant_a, self.tenant_membership)
        alert = self._alert(tenant=self.tenant_a, rule=self.rule_b, subject="Foreign target")

        self.assertIsNone(alert.content_object_safe)


class GlobalComponentMatcherTests(TestCase):
    def test_global_rule_attributes_component_alert_to_component_tenant(self):
        from assets.models import Manufacturer
        from extras.tasks.alerts import _evaluate_rule, _match_low_stock
        from inventory.models import Component, ComponentStock
        from organization.models import Location, Site

        tenant = Tenant.objects.create(name="Issue 183 Matcher Tenant", slug="issue-183-matcher-tenant")
        manufacturer = Manufacturer.objects.create(name="Issue 183 Matcher Mfr", slug="issue-183-matcher-mfr")
        site = Site.objects.create(name="Issue 183 Matcher Site", slug="issue-183-matcher-site", tenant=tenant)
        location = Location.objects.create(
            name="Issue 183 Matcher Location",
            slug="issue-183-matcher-location",
            site=site,
            tenant=tenant,
        )
        component = Component.objects.create(
            name="Issue 183 Matcher Component",
            slug="issue-183-matcher-component",
            manufacturer=manufacturer,
            tenant=tenant,
            min_qty=5,
        )
        ComponentStock.objects.create(component=component, location=location, qty=1)
        rule = AlertRule.objects.create(
            name="Issue 183 Global Component Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=None,
        )

        matches = [match for match in _match_low_stock(rule) if match["obj"].pk == component.pk]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["tenant"], tenant)

        with TaskContext(tenant_id=None):
            _evaluate_rule(rule, timezone.now().date(), {})
        alert = AlertLog.unscoped.get(rule=rule, object_id=component.pk)
        self.assertEqual(alert.tenant_id, tenant.pk)
        alert_ct = ContentType.objects.get_for_model(AlertLog)
        change = ObjectChange._base_manager.get(
            changed_object_type=alert_ct,
            changed_object_id=alert.pk,
            action="create",
        )
        self.assertEqual(change.tenant_id, tenant.pk)


class AlertTaskAuditTests(TestCase):
    def test_task_creation_with_delivery_metadata_writes_one_create_change(self):
        from assets.models import Manufacturer
        from extras.tasks.alerts import _evaluate_rule
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        tenant = Tenant.objects.create(name="Issue 183 Task Tenant", slug="issue-183-task-tenant")
        manufacturer = Manufacturer.objects.create(name="Issue 183 Task Mfr", slug="issue-183-task-mfr")
        site = Site.objects.create(name="Issue 183 Task Site", slug="issue-183-task-site", tenant=tenant)
        location = Location.objects.create(
            name="Issue 183 Task Location",
            slug="issue-183-task-location",
            site=site,
            tenant=tenant,
        )
        accessory = Accessory.objects.create(
            name="Issue 183 Task Accessory",
            slug="issue-183-task-accessory",
            manufacturer=manufacturer,
            tenant=tenant,
            min_qty=5,
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        rule = AlertRule.objects.create(
            name="Issue 183 Task Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )

        with TaskContext(tenant_id=tenant.pk):
            _evaluate_rule(rule, timezone.now().date(), {})

        alert = AlertLog.unscoped.get(rule=rule)
        alert_ct = ContentType.objects.get_for_model(AlertLog)
        changes = ObjectChange._base_manager.filter(
            changed_object_type=alert_ct,
            changed_object_id=alert.pk,
        )
        self.assertEqual(changes.count(), 1)
        self.assertEqual(changes.get().action, "create")
        self.assertEqual(changes.get().tenant_id, tenant.pk)


class AlertDeliveryFailureTests(TransactionTestCase):
    def test_delivery_failure_does_not_rollback_persisted_alert(self):
        from unittest.mock import patch

        from assets.models import Manufacturer
        from extras.tasks.alerts import _evaluate_rule
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        tenant = Tenant.objects.create(name="Issue 183 Delivery Tenant", slug="issue-183-delivery-tenant")
        manufacturer = Manufacturer.objects.create(name="Issue 183 Delivery Mfr", slug="issue-183-delivery-mfr")
        site = Site.objects.create(name="Issue 183 Delivery Site", slug="issue-183-delivery-site", tenant=tenant)
        location = Location.objects.create(
            name="Issue 183 Delivery Location",
            slug="issue-183-delivery-location",
            site=site,
            tenant=tenant,
        )
        accessory = Accessory.objects.create(
            name="Issue 183 Delivery Accessory",
            slug="issue-183-delivery-accessory",
            manufacturer=manufacturer,
            tenant=tenant,
            min_qty=5,
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        rule = AlertRule.objects.create(
            name="Issue 183 Delivery Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )

        with patch("extras.tasks.alerts._dispatch_channels", side_effect=RuntimeError("delivery failure")):
            with TaskContext(tenant_id=tenant.pk):
                _evaluate_rule(rule, timezone.now().date(), {})

        alert = AlertLog.unscoped.get(rule=rule)
        self.assertEqual(alert.status, AlertLog.STATUS_ACTIVE)
        self.assertEqual(alert.delivery_status["__dispatch__"], "terminal")
        self.assertIn("__delivery_id__", alert.delivery_status)
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(alert.last_delivery_error, "dispatch_crash")
        self.assertIsNotNone(alert.last_notified_at)

    def test_post_commit_dispatch_receives_persisted_alert_and_keeps_one_create_audit(self):
        from unittest.mock import patch

        from assets.models import Manufacturer
        from extras.tasks.alerts import _evaluate_rule
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        tenant = Tenant.objects.create(name="Issue 183 Dispatch Tenant", slug="issue-183-dispatch-tenant")
        manufacturer = Manufacturer.objects.create(name="Issue 183 Dispatch Mfr", slug="issue-183-dispatch-mfr")
        site = Site.objects.create(name="Issue 183 Dispatch Site", slug="issue-183-dispatch-site", tenant=tenant)
        location = Location.objects.create(
            name="Issue 183 Dispatch Location",
            slug="issue-183-dispatch-location",
            site=site,
            tenant=tenant,
        )
        accessory = Accessory.objects.create(
            name="Issue 183 Dispatch Accessory",
            slug="issue-183-dispatch-accessory",
            manufacturer=manufacturer,
            tenant=tenant,
            min_qty=5,
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        rule = AlertRule.objects.create(
            name="Issue 183 Dispatch Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )

        with patch("extras.tasks.alerts._dispatch_channels", return_value={"7": "ok"}) as dispatch:
            with TaskContext(tenant_id=tenant.pk):
                _evaluate_rule(rule, timezone.now().date(), {})

        alert = AlertLog.unscoped.get(rule=rule)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[2].pk, alert.pk)
        alert.refresh_from_db()
        # The patched channel returns the legacy "ok" string; the run still
        # records the stable delivery id and the filterable outcome.
        self.assertEqual(alert.delivery_status["7"], "ok")
        self.assertIn("__delivery_id__", alert.delivery_status)
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)
        self.assertEqual(alert.delivery_attempts, 1)
        self.assertIsNotNone(alert.last_delivery_id)
        self.assertIsNotNone(alert.last_notified_at)
        alert_ct = ContentType.objects.get_for_model(AlertLog)
        self.assertEqual(
            ObjectChange._base_manager.filter(
                changed_object_type=alert_ct,
                changed_object_id=alert.pk,
                action="create",
            ).count(),
            1,
        )


class AlertRenotifyDeliveryTests(TransactionTestCase):
    def test_metadata_update_failure_does_not_block_later_dispatch_callback(self):
        from unittest.mock import Mock, patch

        from extras.tasks.alerts import _schedule_alert_dispatch

        tenant = Tenant.objects.create(name="Issue 183 Callback Tenant", slug="issue-183-callback-tenant")
        rule = AlertRule.objects.create(
            name="Issue 183 Callback Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )
        content_type = ContentType.objects.get_for_model(AlertRule)
        first = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="first",
            message="first",
            content_type=content_type,
            object_id=rule.pk,
        )
        second = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="second",
            message="second",
            content_type=content_type,
            object_id=rule.pk + 1,
        )
        match = {"obj": rule, "tenant": tenant, "subject": "alert", "message": "alert"}
        real_filter = AlertLog.unscoped.filter
        # The FIRST TWO filter() calls of the first dispatch are replaced: the
        # idempotency guard (exists()=False) and the claim write, which must
        # fail. Everything after that uses the real queryset so the second
        # callback still runs to completion.
        first_guard = Mock()
        first_guard.exists.return_value = False
        first_guard.update = Mock(side_effect=RuntimeError("metadata update failure"))
        remaining = iter([first_guard, first_guard])

        def flaky_filter(*args, **kwargs):
            try:
                return next(remaining)
            except StopIteration:
                return real_filter(*args, **kwargs)

        with (
            patch("extras.tasks.alerts._dispatch_channels", return_value={"7": "ok"}),
            patch.object(AlertLog.unscoped, "filter", side_effect=flaky_filter),
        ):
            with transaction.atomic():
                _schedule_alert_dispatch(rule, match, first)
                _schedule_alert_dispatch(rule, match, second)

        first.refresh_from_db()
        second.refresh_from_db()
        # The failed claim write is contained: the run that could not claim its
        # alert records nothing (the completion write is fenced to the owning
        # delivery id), and the second callback still runs fully.
        self.assertEqual(first.delivery_status, {})
        self.assertEqual(first.delivery_outcome, AlertLog.DELIVERY_OUTCOME_NONE)
        self.assertEqual(second.delivery_status["7"], "ok")
        self.assertEqual(second.delivery_outcome, AlertLog.DELIVERY_OUTCOME_DELIVERED)

    def test_duplicate_match_schedules_only_one_pending_dispatch(self):
        from unittest.mock import patch

        from extras.tasks.alerts import _evaluate_rule

        tenant = Tenant.objects.create(name="Issue 183 Duplicate Tenant", slug="issue-183-duplicate-tenant")
        rule = AlertRule.objects.create(
            name="Issue 183 Duplicate Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
            renotify_interval_days=0,
        )
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="duplicate",
            message="duplicate",
            content_type=ContentType.objects.get_for_model(AlertRule),
            object_id=rule.pk,
            delivery_status={"__dispatch__": "pending"},
        )
        match = {"obj": rule, "tenant": tenant, "subject": "duplicate", "message": "duplicate"}
        with (
            patch("extras.tasks.alerts._collect_matches", return_value=[match, match]),
            patch("extras.tasks.alerts._dispatch_channels", return_value={"7": "ok"}) as dispatch,
        ):
            with TaskContext(tenant_id=tenant.pk):
                _evaluate_rule(rule, timezone.now().date(), {(rule.pk, alert.content_type_id, alert.object_id): alert})
        dispatch.assert_called_once()

    def test_pending_alert_is_retried_on_next_evaluation(self):
        from unittest.mock import patch

        from extras.tasks.alerts import _evaluate_rule

        tenant = Tenant.objects.create(name="Issue 183 Pending Tenant", slug="issue-183-pending-tenant")
        rule = AlertRule.objects.create(
            name="Issue 183 Pending Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
        )
        alert = AlertLog._base_manager.create(
            tenant=tenant,
            rule=rule,
            subject="pending",
            message="pending",
            content_type=ContentType.objects.get_for_model(AlertRule),
            object_id=rule.pk,
            delivery_status={"__dispatch__": "pending"},
        )
        with patch("extras.tasks.alerts._collect_matches", return_value=[]):
            # A pending row without a current match remains safe and untouched.
            with TaskContext(tenant_id=tenant.pk):
                _evaluate_rule(rule, timezone.now().date(), {(rule.pk, alert.content_type_id, alert.object_id): alert})
        alert.refresh_from_db()
        self.assertEqual(alert.delivery_status, {"__dispatch__": "pending"})

    def test_renotify_delivery_failure_is_post_commit_and_isolated(self):
        from unittest.mock import patch

        from assets.models import Manufacturer
        from extras.tasks.alerts import _evaluate_rule
        from inventory.models import Accessory, AccessoryStock
        from organization.models import Location, Site

        tenant = Tenant.objects.create(name="Issue 183 Renotify Tenant", slug="issue-183-renotify-tenant")
        manufacturer = Manufacturer.objects.create(name="Issue 183 Renotify Mfr", slug="issue-183-renotify-mfr")
        site = Site.objects.create(name="Issue 183 Renotify Site", slug="issue-183-renotify-site", tenant=tenant)
        location = Location.objects.create(
            name="Issue 183 Renotify Location",
            slug="issue-183-renotify-location",
            site=site,
            tenant=tenant,
        )
        accessory = Accessory.objects.create(
            name="Issue 183 Renotify Accessory",
            slug="issue-183-renotify-accessory",
            manufacturer=manufacturer,
            tenant=tenant,
            min_qty=5,
        )
        AccessoryStock.objects.create(accessory=accessory, location=location, qty=1)
        rule = AlertRule.objects.create(
            name="Issue 183 Renotify Rule",
            alert_type=AlertRule.ALERT_TYPE_LOW_STOCK,
            threshold_value=5,
            tenant=tenant,
            renotify_interval_days=7,
        )
        from extras.models import NotificationChannel

        channel = NotificationChannel.objects.create(
            name="Issue 183 Renotify Channel",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=tenant,
            enabled=True,
            config={"recipients": []},
        )
        rule.channels.add(channel)
        # Keep the initial delivery outside the failure patch and make the
        # post-commit boundary explicit for the renotify assertion below.
        with transaction.atomic():
            with TaskContext(tenant_id=tenant.pk):
                _evaluate_rule(rule, timezone.now().date(), {})
        alert = AlertLog.unscoped.get(rule=rule)
        old = timezone.now() - timezone.timedelta(days=10)
        AlertLog.unscoped.filter(pk=alert.pk).update(last_notified_at=old)
        alert.refresh_from_db()

        with patch("extras.tasks.alerts._dispatch_channels", side_effect=RuntimeError("renotify failure")) as dispatch:
            with transaction.atomic():
                with TaskContext(tenant_id=tenant.pk):
                    _evaluate_rule(
                        rule,
                        timezone.now().date(),
                        {
                            (
                                rule.pk,
                                ContentType.objects.get_for_model(accessory).pk,
                                accessory.pk,
                            ): alert
                        },
                    )
        dispatch.assert_called_once()

        alert.refresh_from_db()
        self.assertEqual(alert.delivery_status["__dispatch__"], "terminal")
        self.assertEqual(alert.delivery_outcome, AlertLog.DELIVERY_OUTCOME_FAILED)
        self.assertEqual(alert.last_delivery_error, "dispatch_crash")
        self.assertGreater(alert.last_notified_at, old)


class AlertRuleDetailTenantScopeTests(TenantTestMixin, TestCase):
    """F8: a global rule's detail page lists and counts only in-scope alert logs."""

    permissions = ["extras.view_alertrule", "extras.view_alertlog", "extras.change_alertlog"]

    def setUp(self):
        self.setup_tenant_context(name="F8 Tenant A", slug="f8-tenant-a", permissions=self.permissions)
        self.tenant_a = self.tenant
        self.tenant_b = Tenant.objects.create(name="F8 Tenant B", slug="f8-tenant-b")
        self.rule = AlertRule.objects.create(
            name="F8 Global Rule", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=1
        )
        self.ct = ContentType.objects.get_for_model(AlertRule)
        self.log_a = self._log(self.tenant_a, "F8-SUBJECT-A")
        self.log_b = self._log(self.tenant_b, "F8-SECRET-SUBJECT-B")
        self.url = reverse("extras:alertrule_detail", kwargs={"pk": self.rule.pk})

    def _log(self, tenant, subject):
        return AlertLog._base_manager.create(
            tenant=tenant,
            rule=self.rule,
            subject=subject,
            message=subject + " message",
            content_type=self.ct,
            object_id=tenant.pk,
        )

    def test_detail_lists_and_counts_only_own_logs(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "F8-SUBJECT-A")
        self.assertNotContains(response, "F8-SECRET-SUBJECT-B")
        self.assertEqual(response.context["logs_count"], 1)
        self.assertEqual(response.context["active_logs_count"], 1)
        self.assertNotContains(response, "alerts/%d/acknowledge" % self.log_b.pk)

    def test_foreign_only_logs_render_nothing_and_count_zero(self):
        self.log_a.delete()
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "F8-SECRET-SUBJECT-B")
        self.assertEqual(response.context["logs_count"], 0)
        self.assertEqual(response.context["active_logs_count"], 0)

    def test_foreign_log_acknowledge_post_stays_tenant_bound(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        self.client.post(reverse("extras:alertlog_acknowledge", kwargs={"pk": self.log_b.pk}))
        self.log_b.refresh_from_db()
        self.assertEqual(self.log_b.status, AlertLog.STATUS_ACTIVE)
