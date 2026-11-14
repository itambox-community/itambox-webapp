"""Issue #1090: notification recipients and inbox reads follow live tenant grants."""

import ast
import pathlib

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.events import send_notification_to_channel
from core.models import Notification
from core.tests.mixins import grant
from extras.models import NotificationChannel
from organization.models import Membership, Role, RoleGrant, Tenant

User = get_user_model()


class NotificationAccessMixin:
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Alpha", slug="notif-alpha")
        self.tenant_b = Tenant.objects.create(name="Beta", slug="notif-beta")
        self.role_a = Role.objects.create(tenant=self.tenant_a, name="A", permissions=["assets.view_asset"])
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="B", permissions=["assets.view_asset"])
        self.user = User.objects.create_user(username="multi", password="pw")
        self.grant_a = grant(self.user, self.tenant_a, self.role_a)
        self.grant_b = grant(self.user, self.tenant_b, self.role_b)

    def make_provider(self):
        """Tenant B becomes a provider managing tenant A, so a B grant can reach A."""
        Tenant.objects.filter(pk=self.tenant_b.pk).update(is_provider=True)
        Tenant.objects.filter(pk=self.tenant_a.pk).update(managed_by=self.tenant_b)
        self.tenant_a.refresh_from_db()
        self.tenant_b.refresh_from_db()

    def row(self, user=None, kind="tenant", tenant=None, **extra):
        return Notification.objects.create(
            user=user or self.user,
            subject="s",
            message="m",
            kind=kind,
            tenant=tenant if kind == "tenant" else None,
            **extra,
        )

    def channel(self, tenant):
        return NotificationChannel.objects.create(
            name=f"feed-{tenant.slug}",
            channel_type=NotificationChannel.TYPE_IN_APP,
            tenant=tenant,
        )

    def assertBadgeMatchesList(self, user, expected):
        visible = Notification.objects.visible_to(user)
        self.assertEqual(visible.count(), expected)
        self.assertEqual(visible.filter(is_read=False).count(), expected)
        self.client.force_login(user)
        response = self.client.get(reverse("users:user_notifications"))
        self.assertEqual(len(response.context["notifications"]), expected)


class SenderTests(NotificationAccessMixin, TestCase):
    def test_deactivated_membership_in_a_gets_no_a_notification_but_b_still_works(self):
        Membership.objects.filter(user=self.user, tenant=self.tenant_a).update(is_active=False)
        send_notification_to_channel(self.channel(self.tenant_a), "A msg", "body")
        send_notification_to_channel(self.channel(self.tenant_b), "B msg", "body")
        self.assertFalse(Notification.objects.filter(user=self.user, subject="A msg").exists())
        self.assertTrue(Notification.objects.filter(user=self.user, subject="B msg", tenant=self.tenant_b).exists())

    def test_membership_without_any_grant_receives_nothing(self):
        RoleGrant.objects.filter(membership__user=self.user, membership__tenant=self.tenant_a).delete()
        self.assertTrue(Membership.objects.filter(user=self.user, tenant=self.tenant_a, is_active=True).exists())
        send_notification_to_channel(self.channel(self.tenant_a), "A msg", "body")
        self.assertFalse(Notification.objects.filter(user=self.user, subject="A msg").exists())

    def test_delegated_grant_without_direct_membership_still_receives(self):
        delegate = User.objects.create_user(username="delegate", password="pw")
        self.make_provider()
        manager_role = Role.objects.create(tenant=self.tenant_b, name="Mgr", permissions=["assets.view_asset"])
        delegated = grant(delegate, self.tenant_b, manager_role, reach="assigned", assigned_tenants=[self.tenant_a])
        self.assertIsNotNone(delegated)
        self.assertFalse(Membership.objects.filter(user=delegate, tenant=self.tenant_a).exists())
        send_notification_to_channel(self.channel(self.tenant_a), "Delegated", "body")
        self.assertTrue(Notification.objects.filter(user=delegate, subject="Delegated", tenant=self.tenant_a).exists())
        self.assertBadgeMatchesList(delegate, 1)

    def test_writers_stamp_kind_and_tenant(self):
        send_notification_to_channel(self.channel(self.tenant_a), "Stamped", "body")
        row = Notification.objects.get(user=self.user, subject="Stamped")
        self.assertEqual((row.kind, row.tenant_id), ("tenant", self.tenant_a.pk))


class ReadPredicateTests(NotificationAccessMixin, TestCase):
    def test_deactivated_membership_hides_a_rows_but_keeps_b(self):
        self.row(tenant=self.tenant_a)
        self.row(tenant=self.tenant_b)
        Membership.objects.filter(user=self.user, tenant=self.tenant_a).update(is_active=False)
        self.assertEqual(
            list(Notification.objects.visible_to(self.user).values_list("tenant_id", flat=True)),
            [self.tenant_b.pk],
        )
        self.assertBadgeMatchesList(self.user, 1)

    def test_removed_last_grant_hides_rows(self):
        self.row(tenant=self.tenant_a)
        self.grant_a.delete()
        self.assertBadgeMatchesList(self.user, 0)

    def test_delegated_access_reads_rows(self):
        delegate = User.objects.create_user(username="delegate2", password="pw")
        self.make_provider()
        manager_role = Role.objects.create(tenant=self.tenant_b, name="Mgr", permissions=["assets.view_asset"])
        grant(delegate, self.tenant_b, manager_role, reach="assigned", assigned_tenants=[self.tenant_a])
        self.row(user=delegate, tenant=self.tenant_a)
        self.assertBadgeMatchesList(delegate, 1)

    def test_global_row_follows_the_staff_flag(self):
        self.user.is_staff = True
        self.user.save()
        self.row(kind="global")
        self.assertBadgeMatchesList(self.user, 1)
        self.user.is_staff = False
        self.user.save()
        self.assertBadgeMatchesList(self.user, 0)

    def test_personal_row_is_visible_to_its_recipient_only(self):
        other = User.objects.create_user(username="other", password="pw")
        self.row(kind="personal")
        self.assertEqual(Notification.objects.visible_to(self.user).count(), 1)
        self.assertEqual(Notification.objects.visible_to(other).count(), 0)

    def test_historical_rows_are_hidden(self):
        legacy = Notification.objects.create(user=self.user, subject="old", message="m")
        self.assertEqual(legacy.kind, "legacy")
        self.assertBadgeMatchesList(self.user, 0)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("users:view_notification", args=[legacy.pk])).status_code, 404)

    def test_open_path_refuses_a_row_of_a_revoked_tenant(self):
        row = self.row(tenant=self.tenant_a)
        self.grant_a.delete()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("users:view_notification", args=[row.pk])).status_code, 404)

    def test_superuser_sees_live_tenants_only(self):
        root = User.objects.create_superuser(username="root", password="pw", email="r@example.test")
        self.row(user=root, tenant=self.tenant_a)
        self.assertEqual(Notification.objects.visible_to(root).count(), 1)
        Tenant.objects.filter(pk=self.tenant_a.pk).update(deleted_at="2020-01-01T00:00:00Z")
        self.assertEqual(Notification.objects.visible_to(root).count(), 0)

    def test_anonymous_and_inactive_users_see_nothing(self):
        self.row(tenant=self.tenant_a)
        self.user.is_active = False
        self.user.save()
        self.assertEqual(Notification.objects.visible_to(self.user).count(), 0)
        self.assertEqual(Notification.objects.visible_to(None).count(), 0)


class ConstraintTests(NotificationAccessMixin, TestCase):
    def test_tenant_kind_requires_a_tenant_and_other_kinds_forbid_one(self):
        for kwargs in (
            {"kind": "tenant", "tenant": None},
            {"kind": "personal", "tenant": self.tenant_a},
            {"kind": "global", "tenant": self.tenant_a},
            {"kind": "personal", "user": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(IntegrityError), transaction.atomic():
                Notification.objects.create(**{"user": self.user, "subject": "s", "message": "m", **kwargs})

    def test_deleting_a_tenant_deletes_its_rows(self):
        self.row(tenant=self.tenant_a)
        Tenant._base_manager.filter(pk=self.tenant_a.pk).delete()
        self.assertFalse(Notification.objects.filter(kind="tenant", tenant_id=self.tenant_a.pk).exists())


class WriterGuardTests(TestCase):
    """E7: new writers must name a ``kind``; enforced here, not at runtime."""

    def test_every_notification_writer_passes_kind(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        offenders = []
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).parts
            if "tests" in parts or "migrations" in parts or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))  # source-text: static writer audit
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                direct = isinstance(func, ast.Name) and func.id == "Notification"
                create = (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"create", "get_or_create"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "objects"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "Notification"
                )
                if (direct or create) and not any(kw.arg in {"kind", "defaults"} for kw in node.keywords):
                    offenders.append(f"{'/'.join(parts)}:{node.lineno}")
        self.assertEqual(offenders, [])
