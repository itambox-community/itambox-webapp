"""Watcher notifications run once per commit and never fail the triggering save.

``_defer_notify_watchers`` assumed ``on_commit`` raises outside a transaction and then
called ``_notify_watchers`` inline from its except branch. ``on_commit`` does not raise
there: it runs the callback at once. So a failing notification ran a second time and the
second failure was swallowed at debug level. It is now registered once with
``robust=True``; a failure is logged with its traceback.

Under ``TestCase`` the callbacks run through ``captureOnCommitCallbacks``, which applies
the same robust handling and logs through the ``django.test`` logger.
"""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import NoReverseMatch

from core.models import Notification
from core.tests.mixins import TenantTestMixin, grant
from extras.models import ObjectWatch, Tag

from .test_bookmarks_watch import _clear_user_context, _make_user, _set_user_context


class WatchNotificationIsolationTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Watch Isolation", slug="watch-isolation", permissions=["extras.view_tag"])
        self.watcher = _make_user("isolation-watcher")
        grant(self.watcher, self.tenant, self.tenant_role)

        _set_user_context(self.watcher)
        self.tag = Tag.objects.create(name="IsolationTag", slug="isolation-tag")
        _clear_user_context()
        ObjectWatch.objects.create(
            user=self.watcher, model=ContentType.objects.get_for_model(Tag), object_id=self.tag.pk
        )

    def _save_tag(self):
        _set_user_context(self.watcher)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                self.tag.name = self.tag.name + "X"
                self.tag.save()
        finally:
            _clear_user_context()

    def test_a_failing_notification_runs_once_is_logged_and_keeps_the_save(self):
        with mock.patch(
            "extras.signals._notify_watchers", side_effect=RuntimeError("notification store down")
        ) as notify:
            with self.assertLogs("django.test", level="ERROR") as logs:
                self._save_tag()

        notify.assert_called_once_with(Tag, self.tag, "updated")
        self.assertIn("notification store down", "\n".join(logs.output))
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "IsolationTagX")

    def test_an_object_without_a_detail_url_still_notifies_without_a_link(self):
        with mock.patch.object(Tag, "get_absolute_url", side_effect=NoReverseMatch("no detail view")):
            self._save_tag()

        notification = Notification.objects.filter(user=self.watcher).latest("pk")
        self.assertEqual(notification.target_url, "")
