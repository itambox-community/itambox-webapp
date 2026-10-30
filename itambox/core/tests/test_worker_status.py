"""``get_worker_status`` reports the django-q worker and degrades per probe."""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase, override_settings

from core.worker_status import get_worker_status

SHARED = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "cache_table"}}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class WorkerStatusTests(TestCase):
    @override_settings(CACHES=SHARED)
    def test_live_clusters_are_counted(self):
        with patch("core.worker_status.Stat.get_all", return_value=[object(), object()]):
            status = get_worker_status()
        self.assertEqual((status["detectable"], status["online"], status["cluster_count"]), (True, True, 2))
        self.assertEqual(status["queued_tasks"], 0)

    @override_settings(CACHES=LOCMEM)
    def test_process_local_cache_is_not_reported_as_offline(self):
        with patch("core.worker_status.Stat.get_all", return_value=[]):
            status = get_worker_status()
        self.assertFalse(status["detectable"])

    @override_settings(CACHES=SHARED)
    def test_unreachable_heartbeat_cache_degrades_to_offline_and_is_logged(self):
        with (
            patch("core.worker_status.Stat.get_all", side_effect=ConnectionError("cache down")),
            self.assertLogs("core.worker_status", level="WARNING") as logs,
        ):
            status = get_worker_status()
        self.assertEqual((status["online"], status["cluster_count"]), (False, 0))
        self.assertIn("heartbeats", logs.output[0])

    def test_unreadable_queue_leaves_the_count_unknown_and_is_logged(self):
        with (
            patch("core.worker_status.OrmQ.objects") as objects,
            self.assertLogs("core.worker_status", level="WARNING") as logs,
        ):
            objects.count.side_effect = OperationalError("db down")
            status = get_worker_status()
        self.assertIsNone(status["queued_tasks"])
        self.assertIn("queued", logs.output[0])

    def test_unexpected_queue_error_is_not_swallowed(self):
        with patch("core.worker_status.OrmQ.objects") as objects:
            objects.count.side_effect = RuntimeError("bug")
            with self.assertRaises(RuntimeError):
                get_worker_status()
