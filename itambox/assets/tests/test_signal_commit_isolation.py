"""Post-commit work of the asset-request signal is isolated per callback.

The signal used to wrap only the *registration* of its ``on_commit`` callbacks in a
broad try/except. Inside a transaction the callbacks run after the commit, outside that
try, so a failing event dispatch escaped into the request after the data was already
saved and skipped the admin notification queued after it. The callbacks are now
registered with ``robust=True``: a failure is logged and the next callback still runs.

Under ``TestCase`` the callbacks run through ``captureOnCommitCallbacks``, which applies
the same robust handling and logs through the ``django.test`` logger.
"""

from unittest import mock

from django.test import TestCase
from model_bakery import baker

from assets.models import AssetRequest, AssetType
from core.tests.mixins import TenantTestMixin

NOTIFY_TASK = "assets.tasks.requests.notify_new_request_task"


class AssetRequestCommitCallbackTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Commit Isolation", slug="commit-isolation")
        self.set_active_tenant(self.tenant)
        self.asset_type = baker.make(AssetType, requestable=True)

    def _make_request(self, **kwargs):
        request_obj = baker.prepare(
            AssetRequest, tenant=self.tenant, requester=self.tenant_user, asset_type=self.asset_type, **kwargs
        )
        request_obj._skip_duplicate_check = True  # a parent and its child request the same type
        request_obj.save()
        return request_obj

    def test_failed_event_dispatch_neither_fails_the_save_nor_skips_the_notification(self):
        with (
            mock.patch("assets.signals.dispatch_event", side_effect=RuntimeError("event bus down")) as dispatch,
            mock.patch("assets.signals.async_task") as enqueue,
            self.assertLogs("django.test", level="ERROR") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            request_obj = self._make_request(parent=None)

        self.assertTrue(AssetRequest.objects.filter(pk=request_obj.pk).exists())
        dispatch.assert_called_once_with(AssetRequest, request_obj, action="create")
        enqueue.assert_called_once_with(NOTIFY_TASK, request_obj.pk)
        self.assertIn("event bus down", "\n".join(logs.output))

    def test_child_requests_do_not_notify(self):
        parent = self._make_request(parent=None)
        with (
            mock.patch("assets.signals.dispatch_event") as dispatch,
            mock.patch("assets.signals.async_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            child = self._make_request(parent=parent)

        dispatch.assert_called_once_with(AssetRequest, child, action="create")
        enqueue.assert_not_called()

    def test_updates_dispatch_nothing_from_this_signal(self):
        request_obj = self._make_request(parent=None)
        with (
            mock.patch("assets.signals.dispatch_event") as dispatch,
            mock.patch("assets.signals.async_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            request_obj.notes = "updated"
            request_obj.save()

        dispatch.assert_not_called()
        enqueue.assert_not_called()
