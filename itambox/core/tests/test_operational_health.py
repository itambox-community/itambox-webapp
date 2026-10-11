"""Operational health diagnostics (issue #587) and the frozen /health/ contract."""

import json
from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_q.models import OrmQ, Schedule, Task

from core import operational_health as oh
from core.models import Job

# Checks here assert on global operational state (scheduler/queue/cache/worker rows)
# and wall-clock timings; keep them out of the parallel xdist lane (issue #750).
pytestmark = pytest.mark.serial_only

SHARED = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": "redis://x"}}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _ok_cache():
    return {"state": "ok", "alias": "default", "backend": "RedisCache", "latency_ms": 1.0}


class HealthEndpointContractTests(TestCase):
    def test_ok_payload(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "checks": {"database": "ok"}})

    def test_database_failure_is_503(self):
        with mock.patch("django.db.connection.cursor", side_effect=OperationalError("down")):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error", "checks": {"database": "error"}})

    def test_async_degradation_does_not_influence_health(self):
        OrmQ.objects.create(key="x", payload="p", lock=timezone.now() - timedelta(days=2))
        Schedule.objects.create(func="a.b", name="old", next_run=timezone.now() - timedelta(days=2))
        with mock.patch("django_q.status.Stat.get_all", side_effect=RuntimeError("cache down")):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "checks": {"database": "ok"}})


class WorkerTests(TestCase):
    @override_settings(CACHES=SHARED)
    def test_running(self):
        with mock.patch("django_q.status.Stat.get_all", return_value=[object(), object()]):
            result = oh.check_worker(_ok_cache())
        self.assertEqual((result["state"], result["cluster_count"]), ("online", 2))

    @override_settings(CACHES=SHARED)
    def test_stopped(self):
        with mock.patch("django_q.status.Stat.get_all", return_value=[]):
            result = oh.check_worker(_ok_cache())
        self.assertEqual((result["state"], result["cluster_count"]), ("offline", 0))

    @override_settings(CACHES=LOCMEM)
    def test_locmem_is_undetectable_not_offline(self):
        result = oh.check_worker({"state": "ok", "alias": "default", "backend": "LocMemCache"})
        self.assertEqual((result["state"], result["reason"]), ("undetectable", "process_local_cache"))

    @override_settings(CACHES=SHARED)
    def test_cache_failure_is_undetectable_with_cache_reason(self):
        cache = {"state": "error", "reason": "cache_error", "alias": "default", "backend": "RedisCache"}
        result = oh.check_worker(cache)
        self.assertEqual((result["state"], result["reason"]), ("undetectable", "cache_error"))


class CacheTests(TestCase):
    def test_roundtrip_ok(self):
        self.assertEqual(oh.check_cache()["state"], "ok")

    def test_connection_failure_is_reported_not_raised(self):
        with mock.patch("django.core.cache.backends.locmem.LocMemCache.set", side_effect=ConnectionError("boom")):
            result = oh.check_cache()
        self.assertEqual((result["state"], result["reason"]), ("error", "cache_error"))
        self.assertNotIn("boom", json.dumps(result))

    def test_hang_is_bounded(self):
        import time

        with mock.patch("django.core.cache.backends.locmem.LocMemCache.set", side_effect=lambda *a, **k: time.sleep(2)):
            started = time.monotonic()
            result = oh.check_cache(timeout=0.2)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result["reason"], "cache_timeout")


class SchedulerTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def _schedule(self, name, overdue, **kw):
        return Schedule.objects.create(func="x.y", name=name, next_run=self.now - timedelta(seconds=overdue), **kw)

    def test_within_grace_not_flagged(self):
        self._schedule("recent", 899)
        self.assertEqual(oh.check_scheduler(self.now, 900)["state"], "ok")

    def test_boundary_is_not_flagged_beyond_is(self):
        self._schedule("edge", 900)
        self.assertEqual(oh.check_scheduler(self.now, 900)["state"], "ok")
        self._schedule("late", 901)
        result = oh.check_scheduler(self.now, 900)
        self.assertEqual(result["state"], "overdue")
        self.assertEqual(result["overdue"][0]["name"], "late")
        self.assertEqual(result["overdue"][0]["overdue_seconds"], 901)

    def test_exhausted_schedule_ignored(self):
        self._schedule("done", 5000, repeats=0)
        self.assertEqual(oh.check_scheduler(self.now, 900)["state"], "ok")

    def test_catch_up_within_grace_not_flagged(self):
        # After downtime the replayed occurrence has advanced next_run to a recent slot.
        self._schedule("replayed", 60)
        self.assertEqual(oh.check_scheduler(self.now, 900)["overdue_count"], 0)

    def test_overdue_distinct_from_offline_worker(self):
        self._schedule("stalled", 5000)
        with mock.patch.object(oh, "check_cache", return_value=_ok_cache()), override_settings(CACHES=SHARED):
            with mock.patch("django_q.status.Stat.get_all", return_value=[object()]):
                report = oh.collect(self.now)
        self.assertIn("schedules_overdue", report["reasons"])
        self.assertNotIn("worker_offline", report["reasons"])

    def test_scheduled_report_summary_present(self):
        self.assertEqual(oh.check_scheduler(self.now, 900)["scheduled_reports"]["active"], 0)


class QueueAndJobTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def _queue(self, n, age, leased=False):
        lock = self.now + timedelta(seconds=600) if leased else self.now - timedelta(seconds=age)
        OrmQ.objects.bulk_create([OrmQ(key="k", payload="secret-payload", lock=lock) for _ in range(n)])

    def test_empty_queue_ok(self):
        self.assertEqual(oh.check_queue(self.now, 5, 60)["state"], "ok")

    def test_backlog_without_age_ok_and_age_without_backlog_ok(self):
        self._queue(6, 10)
        self.assertEqual(oh.check_queue(self.now, 5, 60)["state"], "ok")
        OrmQ.objects.all().delete()
        self._queue(5, 500)
        self.assertEqual(oh.check_queue(self.now, 5, 60)["state"], "ok")

    def test_backed_up(self):
        self._queue(6, 500)
        result = oh.check_queue(self.now, 5, 60)
        self.assertEqual((result["state"], result["backlog"]), ("backed_up", 6))
        self.assertEqual(result["oldest_pending_age_seconds"], 500)

    def test_leased_counted_separately(self):
        self._queue(2, 0, leased=True)
        result = oh.check_queue(self.now, 5, 60)
        self.assertEqual((result["backlog"], result["leased"]), (0, 2))

    def test_read_failure_distinct_from_empty(self):
        with mock.patch("django_q.models.OrmQ.objects") as objects:
            objects.all.side_effect = OperationalError("x")
            result = oh.check_queue(self.now, 5, 60)
        self.assertEqual((result["state"], result["reason"]), ("error", "queue_read_failed"))

    def test_jobs_thresholds(self):
        fresh = Job.objects.create(name="fresh")
        old_pending = Job.objects.create(name="old-pending")
        old_running = Job.objects.create(name="old-running", status=Job.STATUS_RUNNING)
        past = self.now - timedelta(seconds=3601)
        Job.objects.filter(pk=old_pending.pk).update(created=past)
        Job.objects.filter(pk=old_running.pk).update(started=past)
        result = oh.check_jobs(self.now, 3600)
        self.assertEqual((result["state"], result["stuck_pending"], result["stuck_running"]), ("stuck", 1, 1))
        Job.objects.exclude(pk=fresh.pk).delete()
        self.assertEqual(oh.check_jobs(self.now, 3600)["state"], "ok")


class FailureTests(TestCase):
    def _failure(self, hours):
        stopped = timezone.now() - timedelta(hours=hours)
        Task.objects.create(
            id=f"t{hours}",
            name=f"n{hours}",
            func="a.b",
            success=False,
            started=stopped,
            stopped=stopped,
            args=("secret-arg",),
            kwargs={"token": "secret"},
            result="Traceback secret",
        )

    def test_counts_and_boundary(self):
        self._failure(1)
        self._failure(48)
        now = timezone.now()
        self.assertEqual(oh.check_failures(now, 1)["state"], "ok")
        self.assertEqual(oh.check_failures(now, 0)["state"], "elevated")
        result = oh.check_failures(now, 1)
        self.assertEqual((result["last_24h"], result["last_7d"]), (1, 2))


class CommandTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("operational_health", *args, stdout=out)
        return out.getvalue()

    def test_json_keys_and_check_green_when_healthy(self):
        with mock.patch.object(oh, "check_worker", return_value={"state": "online", "cluster_count": 1}):
            data = json.loads(self._run("--json", "--check"))
        self.assertEqual(data["status"], "ok")
        for key in ("database", "cache", "worker", "scheduler", "queue", "jobs", "failures", "reasons"):
            self.assertIn(key, data)

    def test_check_fails_on_offline_worker(self):
        with mock.patch.object(oh, "check_worker", return_value={"state": "offline", "cluster_count": 0}):
            with self.assertRaises(CommandError):
                self._run("--check")
            self.assertIn("worker_offline", self._run())

    def test_check_fails_on_cache_failure_and_queue_still_reported(self):
        OrmQ.objects.create(key="k", payload="p", lock=timezone.now())
        with mock.patch("django.core.cache.backends.locmem.LocMemCache.set", side_effect=ConnectionError):
            data = json.loads(self._run("--json"))
            with self.assertRaises(CommandError):
                self._run("--check")
        self.assertEqual(data["cache"]["state"], "error")
        self.assertEqual(data["queue"]["backlog"], 1)

    def test_database_down_is_reported_without_exception(self):
        with mock.patch("django.db.connection.cursor", side_effect=OperationalError("down")):
            report = oh.collect()
        self.assertIn("database_unreachable", report["reasons"])

    def test_output_is_sanitized(self):
        past = timezone.now() - timedelta(days=2)
        OrmQ.objects.create(key="k", payload="PAYLOAD-SECRET", lock=past)
        Schedule.objects.create(func="x.y", name="s", next_run=past)
        Task.objects.create(
            id="s1",
            name="n",
            func="a.b",
            success=False,
            started=past,
            stopped=past,
            args=("ARG-SECRET",),
            kwargs={"k": "KWARG-SECRET"},
            result="TRACEBACK-SECRET",
        )
        Job.objects.create(name="JOB-NAME", data={"x": "JOB-DATA-SECRET"})
        text = self._run("--json") + self._run()
        for secret in (
            "PAYLOAD-SECRET",
            "ARG-SECRET",
            "KWARG-SECRET",
            "TRACEBACK-SECRET",
            "JOB-DATA-SECRET",
            "JOB-NAME",
        ):
            self.assertNotIn(secret, text)
