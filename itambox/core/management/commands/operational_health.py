"""Report the operational health of workers, scheduler, queue and failures.

Read-only. ``/health/`` stays a database-only readiness gate; this command is
the deeper diagnostic for cron, systemd timers and monitoring. See
docs/operations/operational-health.md.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from core.operational_health import collect

GUIDANCE = {
    "database_unreachable": "Check the database service and ITAMBOX_DB_* settings.",
    "cache_unavailable": "Check the Valkey/Redis service behind the django-q cache alias.",
    "worker_offline": "Start the worker service (python manage.py qcluster).",
    "worker_undetectable": "Worker visibility needs a shared cache (Valkey); a process-local cache hides the heartbeat.",
    "schedules_overdue": "The scheduler is not firing; check the worker and its logs.",
    "scheduler_unreadable": "The Schedule table could not be read.",
    "queue_backed_up": "Tasks are queued but not consumed; check worker capacity and the failure list.",
    "queue_unreadable": "The broker queue could not be read.",
    "jobs_stuck": "Jobs are pending or running beyond the threshold; check the worker.",
    "jobs_unreadable": "The Job table could not be read.",
    "failures_elevated": "Run list_failed_tasks to inspect recent failures.",
    "failures_unreadable": "The failure table could not be read.",
    "event_outbox_backed_up": "Due outbox events are not being processed; check the worker and its logs.",
    "event_outbox_dead_events": "Events exhausted their retries or were unassignable; inspect the event rows and worker logs.",
    "event_outbox_unreadable": "The event outbox table could not be read.",
    "event_rule_errors_recent": "Rule actions failed on recently processed events; inspect the rule and worker logs.",
    "event_rule_errors_read_failed": "The event table could not be read for rule-error diagnostics.",
}


class Command(BaseCommand):
    help = "Report worker, scheduler, queue, cache and failure health (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON.")
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit non-zero when any signal is degraded (for cron, systemd, monitoring).",
        )

    def handle(self, *args, **options):
        report = collect()
        if options["as_json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self._write_text(report)
        if options["check"] and report["status"] != "ok":
            raise CommandError(f"operational health degraded: {', '.join(report['reasons'])}")

    def _write_text(self, report):
        self.stdout.write(f"status: {report['status']}")
        for section in (
            "database",
            "cache",
            "worker",
            "scheduler",
            "queue",
            "jobs",
            "failures",
            "event_outbox",
            "event_rule_errors",
        ):
            data = report[section]
            detail = data.get("reason", "")
            self.stdout.write(f"{section}: {data['state']}" + (f" ({detail})" if detail else ""))
        for item in report["scheduler"].get("overdue", []):
            self.stdout.write(f"  overdue: {item['name'] or item['func']} by {item['overdue_seconds']}s")
        for code in report["reasons"]:
            self.stdout.write(f"- {code}: {GUIDANCE[code]}")
