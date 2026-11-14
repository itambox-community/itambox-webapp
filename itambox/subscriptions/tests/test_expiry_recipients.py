"""B7 regression: the subscription expiry task must notify only same-tenant staff.

The daily task previously notified every platform-wide is_staff user with a
subscription's per-tenant financials. Recipients are now scoped to staff who are
members of the subscription's tenant.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from core.models import Notification
from core.tests.mixins import TenantTestMixin
from organization.models import Role, Tenant
from subscriptions.models import Subscription, SubscriptionStatusChoices

User = get_user_model()


class SubscriptionExpiryRecipientTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Tenant A", slug="sub-tenant-a", permissions=["assets.view_asset"])
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="sub-tenant-b")

        # A staff user in each tenant.
        self.staff_a = User.objects.create_user(username="sub_staff_a", password="x", is_staff=True)
        self.grant(self.staff_a, self.tenant, self.tenant_role)
        self.staff_b = User.objects.create_user(username="sub_staff_b", password="x", is_staff=True)
        role_b = Role.objects.create(tenant=self.tenant_b, name="B role", permissions=[])
        self.grant(self.staff_b, self.tenant_b, role_b)

        self.set_active_tenant(self.tenant)
        # Renewal exactly 30 days out → the reminder branch fires and stays
        # 'active' (a past date would be auto-expired on save). The reminder and
        # expiry branches share the same recipient-scoping query under test.
        self.sub = baker.make(
            Subscription,
            name="Acme Sub",  # short: the notification subject is a 255-char field
            tenant=self.tenant,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=timezone.now().date() + timedelta(days=30),
            owner=None,
        )

    def test_only_same_tenant_staff_notified(self):
        self.clear_tenant_context()
        from subscriptions.tasks import check_subscription_expiries_and_reminders

        check_subscription_expiries_and_reminders()

        self.assertTrue(Notification.objects.filter(user=self.staff_a).exists())
        self.assertFalse(Notification.objects.filter(user=self.staff_b).exists())
