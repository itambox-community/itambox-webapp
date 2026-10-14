# Operational health

ITAMbox separates four operational signals so deployment probes stay boring
while diagnostics stay rich.

| Surface | Purpose | Contract |
|---|---|---|
| `/health/` | Web traffic readiness | Process and database only. Returns `200` with `{"status": "ok", "checks": {"database": "ok"}}`, or `503` with `"error"` when the database is unreachable. Background workers, the queue and the cache never influence it. |
| Liveness | Process supervision | The container restart policy; no dependency checks and no extra endpoint. |
| `operational_health` | Deeper diagnostics | Management command, read-only, bounded, sanitized. |
| Monitoring | Alerting | Run the command from cron, a systemd timer or an uptime check; no metrics server is shipped. |

## Running the command

```bash
python manage.py operational_health            # human-readable
python manage.py operational_health --json     # stable keys for tooling
python manage.py operational_health --check    # exit code 1 when degraded
```

`--check` exits `0` only when every signal is healthy. Probes are bounded (the
cache roundtrip stops after 3 seconds) and a failing probe produces a reason
code instead of an exception.

The output contains only counts, schedule and task function names, timestamps,
ages and reason codes. It never contains task payloads, arguments, tracebacks,
tenant identifiers, credentials or hostnames, so it can be pasted into a
support issue.

## Signals, thresholds and actions

| Section | Degraded reason | Meaning | Action |
|---|---|---|---|
| `database` | `database_unreachable` | `SELECT 1` failed. | Check the database service. |
| `cache` | `cache_unavailable` | Set/get/delete roundtrip on the django-q cache alias failed or timed out. | Check Valkey. The queue and failure counts still report, because they live in the database. |
| `worker` | `worker_offline` | No cluster heartbeat (heartbeats expire within seconds of a stop). | Start the `worker` service. |
| `worker` | `worker_undetectable` | The cache is process-local (or unreachable), so heartbeats are invisible. Never reported as healthy or offline. | Use a shared cache (Valkey) to make this signal real. |
| `scheduler` | `schedules_overdue` | Schedules overdue by more than `ITAMBOX_HEALTH_SCHEDULE_GRACE_SECONDS` (default 900), listed with name and overdue age. Active scheduled reports show `last_run` and `last_status` counts. | The worker is up but not firing; inspect its logs. |
| `queue` | `queue_backed_up` | Pending tasks exceed `ITAMBOX_HEALTH_QUEUE_BACKLOG_MAX` (1000) **and** the oldest pending task is older than `ITAMBOX_HEALTH_QUEUE_AGE_SECONDS` (1800). Leased (in-flight) rows are counted separately. | Check worker capacity and failures. |
| `jobs` | `jobs_stuck` | First-party jobs pending or running longer than `ITAMBOX_HEALTH_JOB_STUCK_SECONDS` (3600). | Check the worker; inspect the job. |
| `failures` | `failures_elevated` | More than `ITAMBOX_HEALTH_FAILURES_24H_MAX` (10) failed tasks in 24 hours. 24 h / 7 d counts and the latest failure time are reported. | Run `list_failed_tasks`. |

A queue entry whose lease expired becomes pending again, so a task stuck beyond
the django-q `retry` window shows up as growing oldest pending age.

Each `*_unreadable` reason (`queue_unreadable`, `scheduler_unreadable`,
`jobs_unreadable`, `failures_unreadable`) means the table could not be read,
which is distinct from an empty or healthy state.

## Catch-up versus a stall

The configured django-q2 scheduler uses `catch_up=True`: after downtime it
replays missed occurrences for **registered schedules**, one occurrence per
scheduler pass, until it catches up. This does not replay arbitrary queued
tasks or work that was never registered as a schedule. During catch-up,
`next_run` advances and a replay within the grace period is not flagged. A
schedule that remains overdue beyond the grace period is a stall. The
15-minute default tolerates catch-up; raise it if your recovery takes longer.
Before manually invoking a scheduled task, inspect its schedule state and allow
catch-up to finish to avoid duplicating the effect.

## Wiring it up

Run the command about every five minutes and alert on a sustained failure
(for example three consecutive non-zero exits) rather than a single miss: one
transient miss during a restart is normal.

```bash
*/5 * * * * cd /opt/itambox && docker compose exec -T app python manage.py operational_health --check
```

## Intentional stops

During a queue drain or maintenance window (cluster run with the scheduler
disabled) the command reports facts, so expect `worker_offline` or
`schedules_overdue`. There is no automatic suppression; pause the monitor for
the window.

## Failure-state drill

On a running stack, reproduce each state and run `operational_health --check`:

1. Stop `qcluster`: `worker_offline` within seconds.
2. Stop Valkey: `cache_unavailable` and `worker_undetectable`; queue and failure counts remain.
3. Stop the worker and enqueue a backlog above the threshold: `queue_backed_up` once the oldest task passes the age threshold.
