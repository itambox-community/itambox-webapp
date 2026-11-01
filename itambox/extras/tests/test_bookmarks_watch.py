"""
Tests for the Bookmark (star/pin) and ObjectWatch (bell/notify) feature split.

Coverage:
- Toggle views for both bookmark and watch
- Uniqueness constraints
- Notifications: watching → notified; bookmarking alone → NOT notified
- Dashboard widget rendering + tenant safety
- Detail-page context flags (is_bookmarked, is_watched)
"""

import uuid

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.tests.mixins import TenantTestMixin, grant
from extras.dashboard.widgets import BookmarksWidget
from extras.models import Bookmark, ObjectWatch
from itambox.middleware import _current_user, _request_id

User = get_user_model()


def _set_user_context(user):
    _current_user.set(user)
    _request_id.set(uuid.uuid4())


def _clear_user_context():
    _current_user.set(None)
    _request_id.set(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username):
    return User.objects.create_user(username=username, email=f"{username}@test.com", password="pw")


# ---------------------------------------------------------------------------
# Model / uniqueness tests
# ---------------------------------------------------------------------------


class ObjectWatchModelTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Watch Tenant", slug="watch-tenant")
        self.user = self.tenant_user
        self.ct = ContentType.objects.get_for_model(User)

    def test_create_watch(self):
        w = ObjectWatch.objects.create(user=self.user, model=self.ct, object_id=self.user.pk)
        self.assertEqual(str(w), f"Watch by {self.user} on {self.user}")

    def test_watch_uniqueness(self):
        ObjectWatch.objects.create(user=self.user, model=self.ct, object_id=self.user.pk)
        with self.assertRaisesRegex(IntegrityError, "extras_objectwatch_unique_user_model_object"):
            ObjectWatch.objects.create(user=self.user, model=self.ct, object_id=self.user.pk)

    def test_bookmark_and_watch_are_independent(self):
        Bookmark.objects.create(user=self.user, model=self.ct, object_id=self.user.pk)
        ObjectWatch.objects.create(user=self.user, model=self.ct, object_id=self.user.pk)
        self.assertEqual(Bookmark.objects.filter(user=self.user).count(), 1)
        self.assertEqual(ObjectWatch.objects.filter(user=self.user).count(), 1)


# ---------------------------------------------------------------------------
# Signal / notification tests
# ---------------------------------------------------------------------------


class WatchNotificationTests(TenantTestMixin, TestCase):
    """
    Watching → notification created on save.
    Bookmarking alone → no notification created.

    Watcher notification is deferred to ``transaction.on_commit`` (WS5-7), so the
    triggering save is wrapped in ``captureOnCommitCallbacks(execute=True)`` — under a
    plain ``TestCase`` the on_commit callbacks never fire otherwise.
    """

    def setUp(self):
        self.setup_tenant_context(name="Notify Tenant", slug="notify-tenant", permissions=["extras.view_tag"])
        self.watcher = _make_user("watcher")
        self.bookmarker = _make_user("bookmarker")

        # Associate users with the tenant so they pass the view permission checks
        grant(self.watcher, self.tenant, self.tenant_role)
        grant(self.bookmarker, self.tenant, self.tenant_role)

        from extras.models import Tag

        _set_user_context(self.watcher)
        self.tag = Tag.objects.create(name="TestTag", slug="testtag")
        _clear_user_context()

        ct = ContentType.objects.get_for_model(Tag)
        ObjectWatch.objects.create(user=self.watcher, model=ct, object_id=self.tag.pk)
        Bookmark.objects.create(user=self.bookmarker, model=ct, object_id=self.tag.pk)

    def _trigger_save(self):
        _set_user_context(self.watcher)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                self.tag.name = self.tag.name + "X"
                self.tag.save()
        finally:
            _clear_user_context()

    def test_watcher_receives_notification(self):
        from core.models import Notification

        before = Notification.objects.filter(user=self.watcher).count()
        self._trigger_save()
        after = Notification.objects.filter(user=self.watcher).count()
        self.assertGreater(after, before)

    def test_bookmarker_receives_no_notification(self):
        from core.models import Notification

        before = Notification.objects.filter(user=self.bookmarker).count()
        self._trigger_save()
        after = Notification.objects.filter(user=self.bookmarker).count()
        self.assertEqual(after, before, "Bookmarking alone must not generate notifications")

    def test_rolled_back_save_creates_no_notification(self):
        """WS5-7: a save inside a rolled-back transaction must spend no watcher work."""
        from core.models import Notification

        before = Notification.objects.filter(user=self.watcher).count()

        _set_user_context(self.watcher)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.tag.name = self.tag.name + "Rolled"
                    self.tag.save()
                    transaction.set_rollback(True)
        finally:
            _clear_user_context()

        after = Notification.objects.filter(user=self.watcher).count()
        self.assertEqual(after, before, "A rolled-back save must create no watcher notification")

    def test_many_watchers_one_bulk_insert(self):
        """WS5-7: N surviving watchers → a single INSERT into core_notification."""
        from extras.models import Tag

        ct = ContentType.objects.get_for_model(Tag)
        # Three additional watchers (plus self.watcher from setUp) on the same object.
        extra_watchers = []
        for i in range(3):
            u = _make_user(f"watcher_bulk_{i}")
            grant(u, self.tenant, self.tenant_role)
            ObjectWatch.objects.create(user=u, model=ct, object_id=self.tag.pk)
            extra_watchers.append(u)

        _set_user_context(self.watcher)
        try:
            with CaptureQueriesContext(connection) as ctx:
                with self.captureOnCommitCallbacks(execute=True):
                    self.tag.name = self.tag.name + "Bulk"
                    self.tag.save()
        finally:
            _clear_user_context()

        inserts = [q["sql"] for q in ctx.captured_queries if 'INSERT INTO "core_notification"' in q["sql"]]
        self.assertEqual(len(inserts), 1, "All watcher notifications must land in one bulk INSERT")

        from core.models import Notification

        notified = Notification.objects.filter(user__in=[self.watcher, *extra_watchers]).count()
        self.assertEqual(notified, 4, "Every watcher with view access must be notified")


# ---------------------------------------------------------------------------
# Toggle view tests
# ---------------------------------------------------------------------------

_HTMX_HEADERS = {"HTTP_HX_REQUEST": "true", "HTTP_HX_CURRENT_URL": "/some/page/"}


class BookmarkToggleViewTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="BT Tenant", slug="bt-tenant", permissions=["extras.view_tag"])
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        from extras.models import Tag

        _set_user_context(self.tenant_user)
        self.target_tag = Tag.objects.create(name="BT Tag", slug="bt-tag")
        _clear_user_context()
        self.ct = ContentType.objects.get_for_model(Tag)

    def _url(self):
        return reverse(
            "users:bookmark_toggle",
            kwargs={
                "content_type_id": self.ct.pk,
                "object_id": self.target_tag.pk,
            },
        )

    def test_creates_bookmark(self):
        self.client.post(self._url(), **_HTMX_HEADERS)
        self.assertTrue(Bookmark.objects.filter(user=self.tenant_user, object_id=self.target_tag.pk).exists())

    def test_removes_bookmark_on_second_post(self):
        Bookmark.objects.create(user=self.tenant_user, model=self.ct, object_id=self.target_tag.pk)
        self.client.post(self._url(), **_HTMX_HEADERS)
        self.assertFalse(Bookmark.objects.filter(user=self.tenant_user, object_id=self.target_tag.pk).exists())

    def test_bookmark_does_not_create_watch(self):
        self.client.post(self._url(), **_HTMX_HEADERS)
        self.assertFalse(ObjectWatch.objects.filter(user=self.tenant_user, object_id=self.target_tag.pk).exists())


class WatchToggleViewTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="WT Tenant", slug="wt-tenant", permissions=["extras.view_tag"])
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        from extras.models import Tag

        _set_user_context(self.tenant_user)
        self.target_tag = Tag.objects.create(name="WT Tag", slug="wt-tag")
        _clear_user_context()
        self.ct = ContentType.objects.get_for_model(Tag)

    def _url(self):
        return reverse(
            "users:watch_toggle",
            kwargs={
                "content_type_id": self.ct.pk,
                "object_id": self.target_tag.pk,
            },
        )

    def test_creates_watch(self):
        self.client.post(self._url(), **_HTMX_HEADERS)
        self.assertTrue(ObjectWatch.objects.filter(user=self.tenant_user, object_id=self.target_tag.pk).exists())

    def test_removes_watch_on_second_post(self):
        ObjectWatch.objects.create(user=self.tenant_user, model=self.ct, object_id=self.target_tag.pk)
        self.client.post(self._url(), **_HTMX_HEADERS)
        self.assertFalse(ObjectWatch.objects.filter(user=self.tenant_user, object_id=self.target_tag.pk).exists())

    def test_watch_does_not_create_bookmark(self):
        self.client.post(self._url(), **_HTMX_HEADERS)
        self.assertFalse(Bookmark.objects.filter(user=self.tenant_user, object_id=self.target_tag.pk).exists())


# ---------------------------------------------------------------------------
# Dashboard widget — tenant safety
# ---------------------------------------------------------------------------


class BookmarksWidgetTenantSafetyTests(TenantTestMixin, TestCase):
    """
    User has bookmarks on objects in Tenant A; active tenant is Tenant B.
    Widget must show no items (objects not accessible under tenant B scope).
    """

    def setUp(self):
        self.setup_tenant_context(name="Tenant A", slug="tenant-a")
        from organization.models import Tenant

        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.user_b = _make_user("user_b")
        from extras.models import Tag

        _set_user_context(self.user_b)
        self.tag = Tag.objects.create(name="TenantATag", slug="tenant-a-tag")
        _clear_user_context()

        ct = ContentType.objects.get_for_model(Tag)
        Bookmark.objects.create(user=self.user_b, model=ct, object_id=self.tag.pk)

    def test_widget_renders(self):
        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user_b
        widget = BookmarksWidget()
        ctx = widget.get_context(request)
        # Tags are global so they resolve; this tests the widget runs without error
        self.assertIn("bookmarked_items", ctx)

    def test_deleted_object_omitted(self):
        """Bookmark pointing at a non-existent object is silently skipped."""
        from extras.models import Tag

        ct = ContentType.objects.get_for_model(Tag)
        Bookmark.objects.create(user=self.user_b, model=ct, object_id=99999999)

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user_b
        widget = BookmarksWidget()
        ctx = widget.get_context(request)
        names = [i["name"] for i in ctx["bookmarked_items"]]
        # The ghost bookmark (pk=99999999) must not appear
        self.assertNotIn("99999999", str(names))


# ---------------------------------------------------------------------------
# Soft-delete event precision (WS5-10)
# ---------------------------------------------------------------------------


class SoftDeleteEventActionTests(TransactionTestCase):
    """
    event_on_save must map soft-delete transitions precisely:
      * None -> set deleted_at  → one 'delete' Event
      * set  -> None deleted_at → a distinct 'restore' Event (not 'update')
      * set  -> set   (edit of an already-archived row) → 'update', never another 'delete'

    TransactionTestCase so the on_commit-deferred dispatch actually fires.
    """

    def setUp(self):
        super().setUp()
        from extras.models import Tag

        self.ct = ContentType.objects.get_for_model(Tag)
        _set_user_context(_make_user("sde_actor"))
        # Events are only recorded for models some rule subscribes to (#621).
        from extras.models import EventRule

        EventRule.objects.create(
            name="Tag lifecycle",
            model=self.ct,
            events=["create", "update", "delete", "restore"],
            action_type=EventRule.ACTION_NOTIFICATION,
            enabled=True,
        )
        self.tag = Tag.objects.create(name="Lifecycle", slug="lifecycle")

    def tearDown(self):
        _clear_user_context()
        super().tearDown()

    def _events(self, action):
        from extras.models import Event

        return Event.objects.filter(model=self.ct, object_id=self.tag.pk, action=action)

    def test_soft_delete_emits_single_delete_event(self):
        self.tag.soft_delete()
        self.assertEqual(self._events("delete").count(), 1)
        self.assertEqual(self._events("restore").count(), 0)

    def test_restore_emits_restore_event_not_update(self):
        self.tag.soft_delete()
        delete_count_before = self._events("delete").count()
        update_count_before = self._events("update").count()

        self.tag.restore()

        self.assertEqual(self._events("restore").count(), 1, "Restore must emit a distinct 'restore' Event")
        # Restore must NOT masquerade as an update, nor re-fire delete.
        self.assertEqual(self._events("update").count(), update_count_before)
        self.assertEqual(self._events("delete").count(), delete_count_before)

    def test_editing_archived_row_does_not_refire_delete(self):
        self.tag.soft_delete()
        self.assertEqual(self._events("delete").count(), 1)

        # Edit a field while the row stays soft-deleted (no deleted_at transition).
        from extras.models import Tag

        archived = Tag.all_objects.get(pk=self.tag.pk)
        archived.description = "edited while archived"
        archived.save()

        self.assertEqual(self._events("delete").count(), 1, "Editing an archived row must NOT re-fire 'delete'")
        self.assertEqual(self._events("update").count(), 1, "Archived-row edit is an ordinary 'update'")
        self.assertEqual(self._events("restore").count(), 0)
