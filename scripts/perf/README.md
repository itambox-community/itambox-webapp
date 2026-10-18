# Target-volume performance qualification (beta.4)

This folder contains development-only performance tooling. It does not change application behavior. Run it only against an isolated PostgreSQL database and an application process bound to loopback on the dedicated Tyr dev/test host (or an equivalent disposable local stack). The generator rejects non-dev settings and non-local database hosts; the HTTP harness rejects non-loopback URLs; and the WSGI wrapper rejects every non-loopback client, even if Gunicorn is accidentally bound to a public interface. Never point it at the public demo or a production-like service.

## Fixed workload

The default seed creates exactly:

- 100,000 assets
- 2,000 AssetHolder profiles
- 500 tenants in one tenant group
- 250,000 journal entries
- 500 sites, 1,000 locations, 25 manufacturers, 100 asset types
- 3 global asset custom-field definitions and 300,000 populated custom-field values

Synthetic keys, references, and relationships are deterministic for `--seed 882`. Assets span the 500-tenant group, so list, filter, detail, REST, GraphQL, and global-search probes execute with an explicit tenant-group scope. The generator uses PostgreSQL `bulk_create` in batches of 2,000 and refuses unless `--confirm-disposable` is present, the DB name begins `perf_qual_`, the backend is PostgreSQL, and the core tables are empty. After any partial failure, discard that database and retry with a new disposable name.

`--scale` is only for smoke testing. The manifest records `target_deviation` for any non-default count. Do not cite a scaled run as target-volume qualification.

## Runtime contract

Use the repository's pinned `uv` environment and the isolated settings overlay:

```sh
export ITAMBOX_ENV=dev
export ITAMBOX_SECRET_KEY="$(openssl rand -hex 32)"
export PERF_WEB_WORKERS=2
export PERF_Q_WORKERS=2
read -rsp 'Disposable benchmark superuser password: ' PERF_ADMIN_PASSWORD
export PERF_ADMIN_PASSWORD
```

The intended stack is two Gunicorn worker processes and two qcluster workers, both on the same dedicated host and PostgreSQL 16 instance. `queue_settings.py` only overrides qcluster worker count and successful-task retention for the disposable run; it does not modify the application defaults. `perf_wsgi.py` adds a password-authenticated, harness-only task enqueue URL and must only be used on a loopback-bound disposable stack. It queues `operator.add` tasks and exposes no application data.

## Reproduction commands

Run the following separately for beta.4 and the candidate, changing the source directory, exact revision label, and database name. Use the exact `v1.0.0-beta.4` tag for the baseline and the reviewed candidate SHA for the second run. Keep one measured database active at a time.

```sh
# From the selected source snapshot's repository root:
uv sync --locked --group dev
export PYTHONPATH=itambox:.

docker exec itambox-dev-pg psql -U itambox -d postgres -v ON_ERROR_STOP=1 \
  -c 'CREATE DATABASE perf_qual_beta4 OWNER itambox;'

ITAMBOX_DB_NAME=perf_qual_beta4 DJANGO_SETTINGS_MODULE=core.settings.dev \
  uv run --locked --group dev python -m django migrate

ITAMBOX_DB_NAME=perf_qual_beta4 DJANGO_SETTINGS_MODULE=core.settings.dev \
  uv run --locked --group dev python \
  scripts/perf/seed_target_volume.py --confirm-disposable \
  --manifest-out artifacts/perf-beta4-dataset.json

# Start these in separate terminals/sessions from the repository root:
ITAMBOX_ENV=dev DJANGO_SETTINGS_MODULE=scripts.perf.queue_settings \
ITAMBOX_DB_NAME=perf_qual_beta4 PERF_Q_WORKERS=2 \
  uv run --locked --group dev python -m django qcluster

ITAMBOX_ENV=dev DJANGO_SETTINGS_MODULE=scripts.perf.queue_settings \
ITAMBOX_DB_NAME=perf_qual_beta4 PERF_Q_WORKERS=2 PERF_WEB_WORKERS=2 \
  uv run --locked --group dev gunicorn scripts.perf.perf_wsgi:application \
  --bind 127.0.0.1:8000 --workers 2 --access-logfile - --error-logfile -

# In a third terminal, after both services are healthy:
ITAMBOX_DB_NAME=perf_qual_beta4 PERF_WEB_WORKERS=2 PERF_Q_WORKERS=2 \
  uv run --locked --group dev python \
  scripts/perf/bench_http.py --manifest artifacts/perf-beta4-dataset.json \
  --base-url http://127.0.0.1:8000 --repeats 10 --warmups 2 \
  --import-repeats 5 --import-rows 1000 --queue-repeats 5 --queue-tasks 1000 \
  --output artifacts/perf-beta4-results.json
```

For the candidate, repeat with a fresh database such as `perf_qual_candidate`, a fresh manifest/output filename, and the exact candidate source. Shut down only the processes you started, then drop only your benchmark DB after preserving the JSON evidence:

```sh
docker exec itambox-dev-pg psql -U itambox -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE perf_qual_beta4;'
```

## Measurements and interpretation

- HTTP p50 and p95 are in milliseconds. p50 is the median; p95 is the nearest-rank 95th percentile. Each HTTP path has two default warmups and ten timed requests.
- HTTP latency uses an authenticated cookie session and real urllib requests to the running Gunicorn service over loopback. The benchmark is not based on Django's test client.
- The list paths are the asset list's first page and an asset-tag/serial filtered page. Other paths are asset detail, global search, paginated REST list, GraphQL list (25 rows), and GraphQL detail.
- Import latency is the authenticated HTTP CSV preview + confirmation + qcluster execution time through persisted job completion and readback of all requested rows. The default batch is exactly 1,000 rows; the harness fails if the job fails or the persisted row count is short.
- Qcluster throughput is measured by posting authenticated batches to the harness-only WSGI endpoint and waiting until every task has a persisted successful Task row. The JSON includes p50/p95 batch completion latency and aggregate completed tasks/second; `worker_count` is read from the active settings.
- Query-count diagnostics are intentionally separate, in-process Django test-client probes and are labelled as such. They report SQL counts for the asset list, filtered page, and detail paths; do not describe them as HTTP latency. The beta.4 run establishes ceilings for those three paths. Candidate runs must pass `--baseline-results <beta4-results.json>`; the harness reports and exits non-zero if any count increases above the recorded baseline.
- The dataset manifest records exact target counts, seed, database size after generation, and probe IDs. The result records source commit/tree, DB name, web/qcluster worker counts, and per-path raw samples. Retain both manifests and results with the private qualification report.

## Acceptance envelope

Do not set latency ceilings in advance. First run beta.4 and preserve its exact per-path sample distributions and query counts. Then define and document the candidate envelope from the measured baseline and observed run-to-run variability; do not substitute guessed performance numbers. Query-count ceilings are exactly the beta.4 list, filtered-list, and detail counts, with no increase accepted. If a candidate path falls outside the documented latency envelope or a query ceiling, file a separate sanitized defect issue with the measurements before claiming qualification.
