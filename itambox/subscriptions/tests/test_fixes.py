from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assets.models import Supplier
from core.context import (
    _current_user,
    get_current_all_accessible,
    get_current_membership,
    get_current_tenant,
    get_current_tenant_group,
    get_current_user,
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.models import Notification
from core.tests.mixins import grant
from organization.models import Location, Membership, Site, Tenant, TenantGroup
from subscriptions.models import (
    BillingCycleChoices,
    Subscription,
    SubscriptionAssignment,
    SubscriptionStatusChoices,
)
from subscriptions.tasks import check_subscription_expiries_and_reminders

User = get_user_model()


class SubscriptionFixesTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create tenants
        self.tg = TenantGroup.objects.create(name="Group 1", slug="g1")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a", group=self.tg)
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b", group=self.tg)

        # Create users
        self.super_user = User.objects.create_user(
            username="super", password="password", is_staff=True, is_superuser=True
        )
        self.user_a = User.objects.create_user(username="user_a", password="password", is_staff=False)
        self.user_b = User.objects.create_user(username="user_b", password="password", is_staff=False)

        # Create roles
        from organization.models import Role

        self.role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="Role A",
            permissions=[
                "subscriptions.change_subscription",
                "subscriptions.add_subscription",
                "subscriptions.add_subscriptionassignment",
            ],
        )
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="Role B", permissions=[])

        # Create memberships
        grant(self.user_a, self.tenant_a, self.role_a)
        grant(self.user_b, self.tenant_b, self.role_b)
        # super_user is a platform operator who is also a member of tenant_a, so it
        # receives tenant_a's subscription notifications. Expiry/reminder recipients
        # are scoped to staff who are MEMBERS of the subscription's tenant (B7) —
        # a bare is_staff user with no membership is no longer notified.
        grant(self.super_user, self.tenant_a, self.role_a)

        # Create suppliers and subscriptions
        self.supplier_a = Supplier.objects.create(name="Supplier A", tenant=self.tenant_a)
        self.supplier_b = Supplier.objects.create(name="Supplier B", tenant=self.tenant_b)

        self.sub_a = Subscription.objects.create(
            name="Subscription A",
            supplier=self.supplier_a,
            tenant=self.tenant_a,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=date.today() + timedelta(days=10),
            renewal_cost=100.00,
            currency="USD",
            billing_cycle=BillingCycleChoices.MONTHLY,
        )
        self.sub_b = Subscription.objects.create(
            name="Subscription B",
            supplier=self.supplier_b,
            tenant=self.tenant_b,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=date.today() + timedelta(days=10),
            renewal_cost=200.00,
            currency="USD",
            billing_cycle=BillingCycleChoices.MONTHLY,
        )

        # Add permissions
        self.ct_sub = ContentType.objects.get_for_model(Subscription)
        self.perm_change = Permission.objects.get(codename="change_subscription", content_type=self.ct_sub)
        self.perm_add = Permission.objects.get(codename="add_subscription", content_type=self.ct_sub)

        self.ct_assign = ContentType.objects.get_for_model(SubscriptionAssignment)
        self.perm_add_assign = Permission.objects.get(
            codename="add_subscriptionassignment", content_type=self.ct_assign
        )

        self.user_a.user_permissions.add(self.perm_change, self.perm_add, self.perm_add_assign)

    def test_permission_checks_on_lifecycle_views(self):
        # User without change_subscription permission should get 403 Forbidden
        self.client.login(username="user_b", password="password")

        renew_url = reverse("subscriptions:subscription_renew", kwargs={"pk": self.sub_b.pk})
        cancel_url = reverse("subscriptions:subscription_cancel", kwargs={"pk": self.sub_b.pk})
        suspend_url = reverse("subscriptions:subscription_suspend", kwargs={"pk": self.sub_b.pk})
        checkout_url = reverse("subscriptions:subscription_checkout", kwargs={"pk": self.sub_b.pk})

        # Test renew
        self.assertEqual(self.client.get(renew_url).status_code, 403)
        self.assertEqual(self.client.post(renew_url, {"renewal_date": "2027-01-01"}).status_code, 403)

        # Test cancel
        self.assertEqual(self.client.get(cancel_url).status_code, 403)
        self.assertEqual(
            self.client.post(cancel_url, {"cancellation_date": "2026-06-01", "reason": "test"}).status_code, 403
        )

        # Test suspend
        self.assertEqual(self.client.post(suspend_url).status_code, 403)

        # Test checkout
        self.assertEqual(self.client.get(checkout_url).status_code, 403)
        self.assertEqual(self.client.post(checkout_url, {"target_type": "location"}).status_code, 403)

        # User assignment create view check
        assign_create_url = reverse("subscriptions:subscriptionassignment_create")
        self.assertEqual(self.client.get(f"{assign_create_url}?content_type=1&object_id=1").status_code, 403)

    def test_tenant_scoping_on_lifecycle_views(self):
        # User A has change_subscription permission but belongs to Tenant A.
        # User A should NOT be able to access Tenant B's subscription, resulting in a 404.
        self.client.login(username="user_a", password="password")

        renew_url = reverse("subscriptions:subscription_renew", kwargs={"pk": self.sub_b.pk})
        cancel_url = reverse("subscriptions:subscription_cancel", kwargs={"pk": self.sub_b.pk})
        suspend_url = reverse("subscriptions:subscription_suspend", kwargs={"pk": self.sub_b.pk})
        resume_url = reverse("subscriptions:subscription_resume", kwargs={"pk": self.sub_b.pk})
        checkout_url = reverse("subscriptions:subscription_checkout", kwargs={"pk": self.sub_b.pk})

        self.assertEqual(self.client.get(renew_url).status_code, 404)
        self.assertEqual(self.client.post(renew_url, {"renewal_date": "2027-01-01"}).status_code, 404)
        self.assertEqual(self.client.get(cancel_url).status_code, 404)
        self.assertEqual(
            self.client.post(cancel_url, {"cancellation_date": "2026-06-01", "reason": "test"}).status_code, 404
        )
        self.assertEqual(self.client.post(suspend_url).status_code, 404)
        self.assertEqual(self.client.post(resume_url).status_code, 404)
        self.assertEqual(self.client.get(checkout_url).status_code, 404)
        self.assertEqual(self.client.post(checkout_url, {"target_type": "location"}).status_code, 404)

    def test_duplicate_assignment_validation(self):
        # Set up a target object
        site = Site.objects.create(name="Site A", slug="site-a", tenant=self.tenant_a)
        location = Location.objects.create(name="Loc A", slug="loc-a", site=site, tenant=self.tenant_a)
        loc_ct = ContentType.objects.get_for_model(Location)

        # Create initial assignment
        SubscriptionAssignment.objects.create(subscription=self.sub_a, content_type=loc_ct, object_id=location.pk)

        # Login User A who has permissions
        self.client.login(username="user_a", password="password")

        # Test Checkout View Form duplicate check
        checkout_url = reverse("subscriptions:subscription_checkout", kwargs={"pk": self.sub_a.pk})
        resp = self.client.post(checkout_url, {"target_type": "location", "location": location.pk})
        self.assertEqual(resp.status_code, 200)  # Form re-renders on error
        self.assertFormError(resp.context["form"], None, f"This subscription is already assigned to {location}.")

        # Test Assignment Create View duplicate check
        assign_create_url = reverse("subscriptions:subscriptionassignment_create")
        post_url = f"{assign_create_url}?content_type={loc_ct.pk}&object_id={location.pk}"
        resp = self.client.post(post_url, {"subscription": self.sub_a.pk})
        self.assertEqual(resp.status_code, 200)  # Form re-renders on error
        self.assertFormError(resp.context["form"], None, "This subscription is already assigned to this object.")

    def _make_sub(self, name, supplier, tenant, days, owner=None):
        """Create an ACTIVE subscription renewing in ``days`` days (negative = past)."""
        sub = Subscription.objects.create(
            name=name,
            supplier=supplier,
            tenant=tenant,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=date.today() + timedelta(days=max(days, 10)),
            renewal_cost=100.00,
            owner=owner,
        )
        if days < 10:
            # Bypass the pre_save signal using .update() so a past/near renewal
            # date survives (the signal re-derives it for active subscriptions).
            Subscription.objects.filter(pk=sub.pk).update(renewal_date=date.today() + timedelta(days=days))
            sub.refresh_from_db()
        return sub

    @staticmethod
    def _clear_scope_context():
        """Drop every ambient scoping contextvar a request/middleware may have set.

        The daily subscription task is a cross-tenant SYSTEM task: it runs in a
        django-q worker with no tenant, no tenant group, no membership, no
        "all accessible" flag and no bound principal. Clearing all five here
        stops a permissive context leaked by an earlier test from making these
        regression tests pass for the wrong reason (issue #145).
        """
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)

    def _seed_expiry_and_reminder_subscriptions(self):
        """Subscriptions in BOTH tenants, so a single-tenant scope cannot pass."""
        subs = {
            "expired_a": self._make_sub("Expired Sub", self.supplier_a, self.tenant_a, -1),
            "expired_b": self._make_sub("Expired Sub B", self.supplier_b, self.tenant_b, -1, owner=self.user_b),
            "sub_30": self._make_sub("Sub 30", self.supplier_a, self.tenant_a, 30),
            "sub_14": self._make_sub("Sub 14", self.supplier_a, self.tenant_a, 14),
            "sub_7": self._make_sub("Sub 7", self.supplier_a, self.tenant_a, 7),
            "sub_30_b": self._make_sub("Sub 30 B", self.supplier_b, self.tenant_b, 30, owner=self.user_b),
        }
        subs["sub_7"].vendor_contract_auto_renews = False
        subs["sub_7"].save(update_fields=["vendor_contract_auto_renews"])
        Notification.objects.all().delete()
        return subs

    def test_background_task_expiry_and_reminders(self):
        subs = self._seed_expiry_and_reminder_subscriptions()

        # Enter the task exactly as a worker does: no tenant, no group, no
        # membership, no all-accessible flag, no bound user. Asserting the
        # cleared state keeps an order-dependent false pass impossible.
        self._clear_scope_context()
        self.assertIsNone(get_current_tenant())
        self.assertIsNone(get_current_tenant_group())
        self.assertIsNone(get_current_membership())
        self.assertFalse(get_current_all_accessible())
        self.assertIsNone(get_current_user())

        check_subscription_expiries_and_reminders()

        # Both tenants' past-due subscriptions must be auto-expired.
        subs["expired_a"].refresh_from_db()
        subs["expired_b"].refresh_from_db()
        self.assertEqual(subs["expired_a"].status, SubscriptionStatusChoices.EXPIRED)
        self.assertEqual(subs["expired_b"].status, SubscriptionStatusChoices.EXPIRED)

        # Subscriptions that are not due must be left alone.
        for key in ("sub_30", "sub_14", "sub_7", "sub_30_b"):
            subs[key].refresh_from_db()
            self.assertEqual(subs[key].status, SubscriptionStatusChoices.ACTIVE)

        # Notifications are unscoped rows; recipients are staff who are MEMBERS
        # of the subscription's tenant (B7), plus an active owner who remains a
        # member of that same tenant.
        # self.super_user is staff AND a member of tenant_a; tenant_b's
        # subscriptions are delivered through their owner (self.user_b).
        subjects = [n.subject for n in Notification.objects.all()]
        for expected in (
            "Subscription Expired: Expired Sub",
            "Subscription Expired: Expired Sub B",
            "Subscription Renewal Warning: Sub 30 in 30 Days",
            "Subscription Renewal Warning: Sub 14 in 14 Days",
            "Subscription Renewal Warning: Sub 7 in 7 Days",
            "Subscription Renewal Warning: Sub 30 B in 30 Days",
        ):
            self.assertTrue(any(expected in s for s in subjects), f"missing notification: {expected}")

        notification_count = Notification.objects.count()
        check_subscription_expiries_and_reminders()
        self.assertEqual(Notification.objects.count(), notification_count)

    def test_background_task_does_not_notify_a_former_tenant_owner(self):
        self._seed_expiry_and_reminder_subscriptions()
        Membership.objects.filter(user=self.user_b, tenant=self.tenant_b).delete()

        self._clear_scope_context()
        check_subscription_expiries_and_reminders()

        self.assertFalse(Notification.objects.filter(user=self.user_b).exists())

    def test_background_task_enumerates_outside_an_ambient_tenant_scope(self):
        """An inline (Q_CLUSTER sync) run inherits the caller's tenant scope.

        The cross-tenant enumeration must not narrow to it: tenant_a's work has
        to happen even while tenant_b is the active scope.
        """
        subs = self._seed_expiry_and_reminder_subscriptions()

        self._clear_scope_context()
        membership = Membership.objects.get(user=self.user_b, tenant=self.tenant_b)
        _current_user.set(self.user_b)
        set_current_tenant(self.tenant_b)
        set_current_membership(membership)
        try:
            check_subscription_expiries_and_reminders()
        finally:
            self._clear_scope_context()

        subs["expired_a"].refresh_from_db()
        subs["expired_b"].refresh_from_db()
        self.assertEqual(subs["expired_a"].status, SubscriptionStatusChoices.EXPIRED)
        self.assertEqual(subs["expired_b"].status, SubscriptionStatusChoices.EXPIRED)

        subjects = [n.subject for n in Notification.objects.all()]
        self.assertTrue(any("Subscription Renewal Warning: Sub 7 in 7 Days" in s for s in subjects))
        self.assertTrue(any("Subscription Renewal Warning: Sub 30 B in 30 Days" in s for s in subjects))

    def test_background_task_enumerates_with_a_bound_member_principal(self):
        """A bound non-superuser with no active tenant fails the scoped manager closed.

        That is the documented worker condition for this bug class: the default
        manager returns an empty queryset, so nothing expires and no reminder is
        ever sent. The bootstrap query must not depend on the bound principal.
        """
        subs = self._seed_expiry_and_reminder_subscriptions()

        self._clear_scope_context()
        _current_user.set(self.user_a)
        self.assertIsNone(get_current_tenant())
        try:
            check_subscription_expiries_and_reminders()
        finally:
            self._clear_scope_context()

        subs["expired_a"].refresh_from_db()
        subs["expired_b"].refresh_from_db()
        self.assertEqual(subs["expired_a"].status, SubscriptionStatusChoices.EXPIRED)
        self.assertEqual(subs["expired_b"].status, SubscriptionStatusChoices.EXPIRED)

        subjects = [n.subject for n in Notification.objects.all()]
        self.assertTrue(any("Subscription Renewal Warning: Sub 14 in 14 Days" in s for s in subjects))
        self.assertTrue(any("Subscription Renewal Warning: Sub 30 B in 30 Days" in s for s in subjects))

    def test_background_task_recovers_a_missed_renewal_warning_boundary(self):
        today = date(2026, 10, 3)
        subscription = self._make_sub("Missed 30-day warning", self.supplier_a, self.tenant_a, 30)
        renewal_date = today + timedelta(days=29)
        Subscription.objects.filter(pk=subscription.pk).update(renewal_date=renewal_date)
        Notification.objects.all().delete()
        self._clear_scope_context()

        with patch("subscriptions.tasks.timezone.localdate", return_value=today):
            check_subscription_expiries_and_reminders()
            check_subscription_expiries_and_reminders()

        warning = Notification.objects.filter(
            user=self.super_user,
            subject="Subscription Renewal Warning: Missed 30-day warning in 29 Days",
        )
        self.assertEqual(warning.count(), 1)
        self.assertIn(str(renewal_date), warning.get().message)
        self.assertIn("29 days remaining", warning.get().message)

    def test_background_task_starts_a_new_reminder_cycle_when_renewal_date_changes(self):
        today = date(2026, 10, 3)
        subscription = self._make_sub("Changed renewal", self.supplier_a, self.tenant_a, 30)
        first_renewal_date = today + timedelta(days=29)
        Subscription.objects.filter(pk=subscription.pk).update(renewal_date=first_renewal_date)
        Notification.objects.all().delete()
        self._clear_scope_context()

        with patch("subscriptions.tasks.timezone.localdate", return_value=today):
            check_subscription_expiries_and_reminders()

        next_renewal_date = today + timedelta(days=13)
        self.assertTrue(subscription.renew(next_renewal_date))
        with patch("subscriptions.tasks.timezone.localdate", return_value=today):
            check_subscription_expiries_and_reminders()
            check_subscription_expiries_and_reminders()

        warnings = Notification.objects.filter(user=self.super_user, message__contains=subscription.name)
        self.assertEqual(warnings.count(), 2)
        self.assertTrue(warnings.filter(subject__endswith="in 29 Days").exists())
        self.assertTrue(warnings.filter(subject__endswith="in 13 Days").exists())
        self.assertTrue(warnings.filter(message__contains=str(first_renewal_date)).exists())
        self.assertTrue(warnings.filter(message__contains=str(next_renewal_date)).exists())

    def test_background_task_collapses_obsolete_tiers_after_a_multi_tier_outage(self):
        today = date(2026, 10, 3)
        subscription = self._make_sub("Long outage", self.supplier_a, self.tenant_a, 30)
        renewal_date = today + timedelta(days=29)
        Subscription.objects.filter(pk=subscription.pk).update(renewal_date=renewal_date)
        Notification.objects.all().delete()
        self._clear_scope_context()

        with patch("subscriptions.tasks.timezone.localdate", return_value=today):
            check_subscription_expiries_and_reminders()

        recovered_today = renewal_date - timedelta(days=4)
        with patch("subscriptions.tasks.timezone.localdate", return_value=recovered_today):
            check_subscription_expiries_and_reminders()

        warnings = Notification.objects.filter(user=self.super_user, message__contains=subscription.name)
        self.assertEqual(warnings.count(), 2)
        self.assertTrue(warnings.filter(subject__endswith="in 29 Days").exists())
        self.assertTrue(warnings.filter(subject__endswith="in 4 Days").exists())
        self.assertFalse(warnings.filter(subject__endswith="in 14 Days").exists())

    def test_background_task_skips_soft_deleted_subscriptions(self):
        """The bootstrap path is unscoped by tenant, never by soft delete."""
        subs = self._seed_expiry_and_reminder_subscriptions()
        deleted = self._make_sub("Deleted Sub", self.supplier_a, self.tenant_a, -1)
        Subscription.objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())

        self._clear_scope_context()
        check_subscription_expiries_and_reminders()

        deleted.refresh_from_db()
        self.assertEqual(deleted.status, SubscriptionStatusChoices.ACTIVE)
        subs["expired_a"].refresh_from_db()
        self.assertEqual(subs["expired_a"].status, SubscriptionStatusChoices.EXPIRED)

        subjects = [n.subject for n in Notification.objects.all()]
        self.assertFalse(any("Deleted Sub" in s for s in subjects))


@pytest.mark.serial_only
class TestSubscriptionReminderConcurrency(TransactionTestCase):
    def test_concurrent_task_runs_persist_one_warning_for_a_cycle(self):
        from organization.models import Role

        today = date(2026, 10, 3)
        renewal_date = today + timedelta(days=29)
        group = TenantGroup.objects.create(name="Reminder group", slug="reminder-group")
        tenant = Tenant.objects.create(name="Reminder tenant", slug="reminder-tenant", group=group)
        staff_user = User.objects.create_user(username="reminder-staff", is_staff=True)
        role = Role.objects.create(tenant=tenant, name="Reminder role", permissions=[])
        grant(staff_user, tenant, role)
        supplier = Supplier.objects.create(name="Reminder supplier", tenant=tenant)
        subscription = Subscription.objects.create(
            name="Concurrent reminder",
            supplier=supplier,
            tenant=tenant,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=renewal_date,
            renewal_cost=100.00,
            currency="USD",
            billing_cycle=BillingCycleChoices.MONTHLY,
        )
        Subscription.objects.filter(pk=subscription.pk).update(renewal_date=renewal_date)
        barrier = Barrier(2)

        def run_task():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                check_subscription_expiries_and_reminders()
            finally:
                close_old_connections()

        with patch("subscriptions.tasks.timezone.localdate", return_value=today):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_task) for _ in range(2)]
                for future in futures:
                    future.result(timeout=30)

        warning = Notification.objects.filter(
            user=staff_user,
            subject="Subscription Renewal Warning: Concurrent reminder in 29 Days",
        )
        self.assertEqual(warning.count(), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.renewal_reminder_cycle_date, renewal_date)
        self.assertEqual(subscription.renewal_reminder_days, 30)
