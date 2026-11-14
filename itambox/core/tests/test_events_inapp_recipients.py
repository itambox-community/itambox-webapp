"""Audit fix A: a tenant-scoped in-app NotificationChannel resolves its recipients
via Membership (``memberships``), not via the old AssetHolder-profile join.

The old join silently dropped tenant members who had no AssetHolder profile (e.g.
an admin who never holds hardware), so they never received in-app alerts for their
own tenant. Membership is the correct source of truth for "who belongs to a tenant".
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.events import send_notification_to_channel
from core.models import Notification
from core.tests.mixins import grant
from extras.models import NotificationChannel
from organization.models import Role, Tenant

User = get_user_model()


class InAppChannelRecipientTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme")
        self.other_tenant = Tenant.objects.create(name="Globex", slug="globex")
        role = Role.objects.create(tenant=self.tenant, name="R", permissions=["assets.view_asset"])

        # A member of `tenant` with NO AssetHolder profile — the case the old
        # asset_holder_profiles join dropped.
        self.member = User.objects.create_user(username="member", password="pw", is_active=True)
        grant(self.member, self.tenant, role)

        # A member of a DIFFERENT tenant — must NOT receive this channel's notice.
        other_role = Role.objects.create(tenant=self.other_tenant, name="R", permissions=["assets.view_asset"])
        self.outsider = User.objects.create_user(username="outsider", password="pw", is_active=True)
        grant(self.outsider, self.other_tenant, other_role)

        self.channel = NotificationChannel.objects.create(
            name="Acme Feed",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=self.tenant,
        )

    def test_tenant_member_without_holder_profile_receives_notification(self):
        ok = send_notification_to_channel(self.channel, "Subj", "Body")
        self.assertTrue(ok)
        self.assertTrue(Notification.objects.filter(user=self.member, subject="Subj").exists())

    def test_member_of_other_tenant_is_not_notified(self):
        send_notification_to_channel(self.channel, "Subj", "Body")
        self.assertFalse(Notification.objects.filter(user=self.outsider).exists())

    def test_explicit_recipients_are_bounded_by_the_channel_tenant(self):
        self.channel.config = {"recipient_users": [self.outsider.pk, self.member.pk]}
        self.channel.save()

        send_notification_to_channel(self.channel, "Explicit", "Body")

        self.assertTrue(Notification.objects.filter(user=self.member, subject="Explicit").exists())
        self.assertFalse(Notification.objects.filter(user=self.outsider, subject="Explicit").exists())

    def test_explicit_recipients_outside_the_tenant_are_skipped_entirely(self):
        self.channel.config = {"recipient_users": [self.outsider.pk]}
        self.channel.save()

        send_notification_to_channel(self.channel, "Explicit", "Body")

        self.assertFalse(Notification.objects.filter(subject="Explicit").exists())

    def test_explicit_recipients_on_a_platform_channel_require_staff(self):
        staff = User.objects.create_user(username="staffer", password="pw", is_active=True, is_staff=True)
        platform_channel = NotificationChannel.objects.create(
            name="Global Feed",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=None,
        )
        platform_channel.config = {"recipient_users": [staff.pk, self.member.pk]}
        platform_channel.save()

        send_notification_to_channel(platform_channel, "Explicit", "Body")

        self.assertTrue(Notification.objects.filter(user=staff, subject="Explicit").exists())
        self.assertFalse(Notification.objects.filter(user=self.member, subject="Explicit").exists())
