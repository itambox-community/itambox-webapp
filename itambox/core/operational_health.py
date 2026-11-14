"""Read-only operational diagnostics for the background stack.

Backs ``manage.py operational_health``. ``/health/`` stays a database-only
traffic-readiness gate; this module answers the deeper question "is ITAMbox
doing background work?" for operators and monitoring.

Output is sanitized by construction: aggregate counts, schedule and task
function names, timestamps, ages and reason codes. Task payloads, arguments,
tracebacks, tenant identifiers, credentials and hostnames are never read into
the report.

Every probe is bounded and isolated: a failing probe yields a reason code, never
an exception.
"""

import logging
import threading
import time
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.db.models import Count, Min
from django.utils import timezone
from django_q.conf import Conf
from django_q.models import Failure, OrmQ, Schedule
from django_q.status import Stat

from core.models import Job

logger = logging.getLogger(__name__)

CACHE_PROBE_TIMEOUT_SECONDS = 3.0
MAX_LISTED_ITEMS = 20


def _seconds(delta):
    return max(int(delta.total_seconds()), 0)


def _cache_alias():
    return Conf.CACHE or "default"


def _cache_backend_name(alias):
    backend = (settings.CACHES.get(alias, {}) or {}).get("BACKEND", "")
    return backend.rsplit(".", 1)[-1] if backend else ""


def check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: database probe failed")
        return {"state": "error", "reason": "database_unreachable"}
    return {"state": "ok"}


def _roundtrip(alias, outcome):
    try:
        cache = caches[alias]
        key = "itambox:operational_health:probe"
        started = time.monotonic()
        cache.set(key, "1", 10)
        value = cache.get(key)
        cache.delete(key)
        outcome["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        outcome["ok"] = value == "1"
        if not outcome["ok"]:
            outcome["reason"] = "cache_roundtrip_mismatch"
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        outcome["ok"] = False
        outcome["reason"] = "cache_error"


def check_cache(timeout=CACHE_PROBE_TIMEOUT_SECONDS):
    """Cache roundtrip on the django-q alias, bounded by ``timeout`` seconds."""
    alias = _cache_alias()
    outcome = {}
    worker = threading.Thread(target=_roundtrip, args=(alias, outcome), daemon=True)
    worker.start()
    worker.join(timeout)
    result = {"alias": alias, "backend": _cache_backend_name(alias)}
    if worker.is_alive():
        result.update(state="error", reason="cache_timeout")
    elif outcome.get("ok"):
        result.update(state="ok", latency_ms=outcome["latency_ms"])
    else:
        result.update(state="error", reason=outcome.get("reason", "cache_error"))
    return result


def check_worker(cache):
    """Cluster heartbeat visibility. Never reports ``offline`` when undetectable."""
    alias = cache["alias"]
    backend = (settings.CACHES.get(alias, {}) or {}).get("BACKEND", "").lower()
    result = {"cache_alias": alias, "cache_backend": cache["backend"], "cluster_count": 0}
    if not backend or "locmem" in backend or "dummy" in backend:
        # Separate processes hold separate caches: a running worker is invisible.
        result.update(state="undetectable", reason="process_local_cache")
        return result
    if cache["state"] != "ok":
        result.update(state="undetectable", reason=cache.get("reason", "cache_error"))
        return result
    try:
        clusters = Stat.get_all()
    except Exception:  # broad except: boundary-isolation: cache errors are not enumerable
        result.update(state="undetectable", reason="heartbeat_read_failed")
        return result
    result["cluster_count"] = len(clusters)
    result["state"] = "online" if clusters else "offline"
    return result


def _overdue_item(schedule, now):
    return {
        "name": schedule.name or "",
        "func": schedule.func,
        "overdue_seconds": _seconds(now - schedule.next_run),
    }


def _scheduled_report_summary():
    ScheduledReport = apps.get_model("extras", "ScheduledReport")
    rows = (
        ScheduledReport._base_manager.filter(is_active=True)
        .values("last_status")
        .annotate(total=Count("pk"))
        .order_by("last_status")
    )
    by_status = {(row["last_status"] or "never_run"): row["total"] for row in rows}
    latest = ScheduledReport._base_manager.filter(is_active=True, last_run__isnull=False).order_by("-last_run").first()
    return {
        "active": sum(by_status.values()),
        "by_last_status": by_status,
        "latest_run": latest.last_run.isoformat() if latest else None,
    }


def check_scheduler(now, grace_seconds):
    try:
        cutoff = now - timedelta(seconds=grace_seconds)
        due = Schedule.objects.exclude(repeats=0).filter(next_run__lt=cutoff).order_by("next_run")
        overdue = [_overdue_item(item, now) for item in due[:MAX_LISTED_ITEMS]]
        total = due.count()
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: schedule probe failed")
        return {"state": "error", "reason": "schedule_read_failed"}
    result = {
        "state": "overdue" if total else "ok",
        "grace_seconds": grace_seconds,
        "overdue_count": total,
        "overdue": overdue,
    }
    try:
        result["scheduled_reports"] = _scheduled_report_summary()
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: scheduled report summary failed")
        result["scheduled_reports"] = {"state": "error", "reason": "scheduled_report_read_failed"}
    return result


def check_queue(now, backlog_max, age_seconds):
    try:
        rows = OrmQ.objects.all()
        pending = rows.filter(lock__lte=now)
        leased = rows.filter(lock__gt=now).count()
        backlog = pending.count()
        oldest = pending.aggregate(oldest=Min("lock"))["oldest"]
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: queue probe failed")
        return {"state": "error", "reason": "queue_read_failed"}
    oldest_age = _seconds(now - oldest) if oldest else 0
    backed_up = backlog > backlog_max and oldest_age > age_seconds
    return {
        "state": "backed_up" if backed_up else "ok",
        "backlog": backlog,
        "leased": leased,
        "oldest_pending_age_seconds": oldest_age,
        "backlog_max": backlog_max,
        "age_threshold_seconds": age_seconds,
    }


def check_jobs(now, stuck_seconds):
    try:
        cutoff = now - timedelta(seconds=stuck_seconds)
        base = Job._base_manager
        pending = base.filter(status=Job.STATUS_PENDING, created__lt=cutoff).exclude(scheduled_for__gt=now)
        running = base.filter(status=Job.STATUS_RUNNING, started__lt=cutoff)
        stuck_pending = pending.count()
        stuck_running = running.count()
        oldest = min(
            [d for d in (pending.aggregate(m=Min("created"))["m"], running.aggregate(m=Min("started"))["m"]) if d],
            default=None,
        )
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: job probe failed")
        return {"state": "error", "reason": "job_read_failed"}
    return {
        "state": "stuck" if stuck_pending or stuck_running else "ok",
        "stuck_pending": stuck_pending,
        "stuck_running": stuck_running,
        "oldest_age_seconds": _seconds(now - oldest) if oldest else 0,
        "threshold_seconds": stuck_seconds,
    }


def check_failures(now, max_24h):
    try:
        last_day = Failure.objects.filter(stopped__gte=now - timedelta(hours=24)).count()
        last_week = Failure.objects.filter(stopped__gte=now - timedelta(days=7)).count()
        latest = Failure.objects.order_by("-stopped").values_list("stopped", flat=True).first()
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: failure probe failed")
        return {"state": "error", "reason": "failure_read_failed"}
    return {
        "state": "elevated" if last_day > max_24h else "ok",
        "last_24h": last_day,
        "last_7d": last_week,
        "latest_failure": latest.isoformat() if latest else None,
        "threshold_24h": max_24h,
    }


def check_event_outbox(now):
    """Pending/dead event counts and the age of the oldest due pending event (issue #1091)."""
    Event = apps.get_model("extras", "Event")

    try:
        # unscoped: operational diagnostics span every tenant
        events = Event._base_manager  # unscoped: see above
        pending = events.filter(status=Event.STATUS_PENDING)
        pending_count = pending.count()
        dead_count = events.filter(status=Event.STATUS_DEAD).count()
        oldest_due = pending.filter(available_at__lte=now).aggregate(m=Min("available_at"))["m"]
    except Exception:  # broad except: boundary-isolation: a failing probe is reported as a reason code, never raised
        logger.exception("operational_health: event outbox probe failed")
        return {"state": "error", "reason": "event_outbox_read_failed"}
    oldest_age = _seconds(now - oldest_due) if oldest_due else 0
    state = "ok"
    if dead_count:
        state = "dead_events"
    if oldest_age > 600:
        state = "backed_up"
    return {"state": state, "pending": pending_count, "dead": dead_count, "oldest_due_age_seconds": oldest_age}


_SECTION_REASONS = (
    ("database", "error", "database_unreachable"),
    ("cache", "error", "cache_unavailable"),
    ("worker", "offline", "worker_offline"),
    ("worker", "undetectable", "worker_undetectable"),
    ("scheduler", "overdue", "schedules_overdue"),
    ("scheduler", "error", "scheduler_unreadable"),
    ("queue", "backed_up", "queue_backed_up"),
    ("queue", "error", "queue_unreadable"),
    ("jobs", "stuck", "jobs_stuck"),
    ("jobs", "error", "jobs_unreadable"),
    ("failures", "elevated", "failures_elevated"),
    ("failures", "error", "failures_unreadable"),
    ("event_outbox", "backed_up", "event_outbox_backed_up"),
    ("event_outbox", "dead_events", "event_outbox_dead_events"),
    ("event_outbox", "error", "event_outbox_unreadable"),
)


def degraded_reasons(report):
    """Distinct reason codes for every non-healthy section."""
    return [code for section, state, code in _SECTION_REASONS if report[section]["state"] == state]


def collect(now=None):
    """Return the full, sanitized diagnostics report."""
    now = now or timezone.now()
    cache = check_cache()
    report = {
        "database": check_database(),
        "cache": cache,
        "worker": check_worker(cache),
        "scheduler": check_scheduler(now, settings.ITAMBOX_HEALTH_SCHEDULE_GRACE_SECONDS),
        "queue": check_queue(now, settings.ITAMBOX_HEALTH_QUEUE_BACKLOG_MAX, settings.ITAMBOX_HEALTH_QUEUE_AGE_SECONDS),
        "jobs": check_jobs(now, settings.ITAMBOX_HEALTH_JOB_STUCK_SECONDS),
        "failures": check_failures(now, settings.ITAMBOX_HEALTH_FAILURES_24H_MAX),
        "event_outbox": check_event_outbox(now),
    }
    reasons = degraded_reasons(report)
    return {
        "status": "degraded" if reasons else "ok",
        "reasons": reasons,
        "generated_at": now.isoformat(),
        **report,
    }
