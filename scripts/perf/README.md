# Target-volume performance qualification (beta.4)

This folder contains development-only performance tooling. It does not change application behavior. Run it only against an isolated PostgreSQL database and an application process bound to loopback on the dedicated Tyr dev/test host (or an equivalent disposable local stack). The generator rejects non-dev settings and non-local database hosts; the HTTP harness rejects non-loopback URLs; and the WSGI wrapper rejects every non-loopback client, even if Gunicorn is accidentally bound to a public interface. Never point it at the public demo or a production-like service.

## Fixed workload

The default seed creates exactly:

- 100,000 assets
- 2,000 AssetHolder profiles
- 500 tenants in one tenant group (one provider and 499 managed tenants)
- One benchmark membership, read-only role, grant, and two grant scopes (provider-own plus tenant-group)
- 250,000 journal entries
- 500 sites, 1,000 locations, 25 manufacturers, 100 asset types
- 3 global asset custom-field definitions and 300,000 populated custom-field values

Synthetic keys, references, and relationships are deterministic for `--seed 882`. The provider-own scope plus tenant-group scope authorizes all 500 tenants, including the provider itself, so list, filter, detail, REST, GraphQL, and global-search probes execute with a complete explicit tenant-group scope. The generator uses PostgreSQL `bulk_create` in batches of 2,000 and refuses unless `--confirm-disposable` is present, the DB name begins `perf_qual_`, the backend is PostgreSQL, and the core tables are empty. After any partial failure, discard that database and retry with a new disposable name.

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

This section describes the original target-volume `bench_http.py` procedure and the first issue #882 report. The expanded sampling contract for the newer `bench.py` harness appears below and supersedes the HTTP sample-count method for follow-up runs; do not pool results from the two harnesses without labeling their methods.

- HTTP p50 and p95 are in milliseconds. p50 is the median; p95 is the nearest-rank 95th percentile. Each HTTP path has two default warmups and ten timed requests.
- HTTP latency uses an authenticated cookie session and real urllib requests to the running Gunicorn service over loopback. The benchmark is not based on Django's test client.
- The list paths are the asset list's first page and an asset-tag/serial filtered page. Other paths are asset detail, global search, paginated REST list, GraphQL list (25 rows), and GraphQL detail.
- Import latency is the authenticated HTTP CSV preview + confirmation + qcluster execution time through persisted job completion and readback of all requested rows. The default batch is exactly 1,000 rows; the harness fails if the job fails or the persisted row count is short.
- Qcluster throughput is measured by posting authenticated batches to the harness-only WSGI endpoint and waiting until every task has a persisted successful Task row. The JSON includes p50/p95 batch completion latency and aggregate completed tasks/second; `worker_count` is read from the active settings.
- Query-count diagnostics are intentionally separate, in-process Django test-client probes and are labelled as such. They report SQL counts for the asset list, filtered page, and detail paths; do not describe them as HTTP latency. The beta.4 run establishes ceilings for those three paths. Candidate runs must pass `--baseline-results <beta4-results.json>`; the harness reports and exits non-zero if any count increases above the recorded baseline.
- The dataset manifest records exact target counts, seed, database size after generation, and probe IDs. The result records source commit/tree, DB name, web/qcluster worker counts, and per-path raw samples. Retain both manifests and results with the private qualification report.

## Acceptance envelope

Do not set latency ceilings in advance. First run beta.4 and preserve its exact per-path sample distributions and query counts. Then define and document the candidate envelope from the measured baseline and observed run-to-run variability; do not substitute guessed performance numbers. Query-count ceilings are exactly the beta.4 list, filtered-list, and detail counts, with no increase accepted. If a candidate path falls outside the documented latency envelope or a query ceiling, file a separate sanitized defect issue with the measurements before claiming qualification.

---

## HTTP performance benchmark (`bench.py`)

`scripts/perf/bench.py` is development tooling only. It measures the application over authenticated HTTP; the query-count mode is separate and explicitly is not latency. A full run requires a disposable PostgreSQL database named `perf_qual_*`, Django development settings, a benchmark manifest from the dataset seeder, and a benchmark principal with the required read/import permissions.

Credentials are read only from environment variables:

- `ITAMBOX_BENCH_USERNAME`
- `ITAMBOX_BENCH_PASSWORD`

Do not put credentials in command arguments or commit local environment files. The harness never copies credential values to JSON, Markdown, or error output.

### Full run

Run from the repository root. The application server and qcluster must already be running against the same disposable database. `--workers` records the Gunicorn worker count used by the target. `--qcluster-workers` must match Django's configured qcluster worker count when supplied. Concurrency defaults to 1. Import and queue paths write test data, so they require both a loopback target and `--confirm-disposable`.

```sh
export ITAMBOX_ENV=dev
export DJANGO_SETTINGS_MODULE=core.settings.dev
export ITAMBOX_DB_NAME=perf_qual_smoke
# Supply ITAMBOX_BENCH_USERNAME and ITAMBOX_BENCH_PASSWORD from the local secret store.
uv run --locked --group dev python scripts/perf/bench.py \
  --manifest artifacts/perf-manifest.json \
  --base-url http://127.0.0.1:8010 \
  --workers 3 --qcluster-workers 2 \
  --confirm-disposable
```

The default base URL is `https://qual.itambox.dev`. Read-only-only runs may target qual with `--paths`; they still require Django development settings and a PostgreSQL manifest database name that matches the configured connection, but do not require the runner's DB host to be loopback because they do not write to it. Use `--base-url http://127.0.0.1:8010` for a loopback-only disposable stack; write paths are intentionally refused for non-loopback hosts. Read-only paths can be selected explicitly, for example:

```sh
uv run --locked --group dev python scripts/perf/bench.py \
  --manifest artifacts/perf-manifest.json \
  --base-url http://127.0.0.1:8010 \
  --paths asset_list_first_page asset_detail global_search \
  --workers 3
```

The default workload reports each of these separately: asset list first page, filtered asset list, asset detail, global search, REST asset list, GraphQL list, GraphQL detail, a 1,000-row CSV import, and qcluster batch throughput. Each HTTP read probe requires at least 100 successful measured samples per revision; `--iterations` defaults to 100 and refuses smaller values. Its `--warmups` requests are run first, excluded from all measured percentiles and reported separately as `warmup_samples_ms`. Failed measured requests remain in `sample_records` with their error and do not count toward `sample_count`; a run with errors exits non-zero. Import and qcluster use their own `--import-repeats` / `--queue-repeats` and the same warmup count. The qcluster batch size is set by `--queue-tasks` (default 1,000). Each result stores p50 (median), nearest-rank p90/p95/p99, max, the raw measured samples and per-iteration records. It also reports a 95% percentile-bootstrap confidence interval for p95 from 2,000 resamples; each path's deterministic seed is recorded in `p95_bootstrap_ci_ms`. Login setup request count is reported separately. Import rows/second and qcluster tasks/second are reported separately. Qcluster tasks are enqueued through Django-Q's async API and drained by qcluster; its HTTP request count is therefore zero, not a missing measurement.

### Measured set, correlation, and limits

The measured set is the successful HTTP probe iterations after warmups. Each `sample_records` row names its phase (`warmup` or `measured`), iteration, action-level elapsed time, errors, and the underlying HTTP requests. Request IDs bind each HTTP response to diagnostics returned by the development-only `perf_wsgi` wrapper. Redirect hops are recorded as distinct requests with a fresh ID for each hop; their per-request `elapsed_ms` is null because the measured sample latency covers the complete client action. Request totals include redirect hops. For instrumented HTTP requests the wrapper records Django cursor query count and total execution time, the number and top five durations/fingerprints of slow queries (50 ms threshold; SQL text and parameters are never persisted), overlapping Python GC pause count/time, and worker PID plus a process-instance ID. `process_observations` summarizes the sampled worker identities and flags a PID seen with a changed process-instance ID. Missing wrapper diagnostics remain explicitly marked unavailable rather than being inferred.

The p95 interval is a percentile bootstrap interval for the observed sample distribution, not a prediction interval. It assumes the requests are representative and does not correct for serial correlation, workload drift, host contention, or changes between revisions. At n=100, nearest-rank p99 is the 99th ordered sample (the second-largest); the separately reported maximum is the largest. Neither is a stable estimate of a 99th-percentile service-level objective at this sample count. The per-request database timer surrounds Django cursor execution and is not `pg_stat_statements`, an execution plan, or a record of SQL statements; fingerprints support grouping without exposing query text. GC pauses are collected only when they overlap an instrumented request in that worker. Worker IDs correlate sampled requests with process instances, but a restart entirely between sampled requests cannot be inferred from this evidence. Keep baseline and candidate runs on the same architecture and label the host/runtime; the bootstrap interval does not make cross-platform comparisons valid.

The raw JSON is written to `artifacts/perf-results.json` by default and a sibling Markdown table to `artifacts/perf-results.md`. The Markdown table is also printed to stdout. Override the JSON path with `--output`; the matching `.md` path is derived from it.

### SQL query counts (not latency)

This mode uses Django's in-process test client and `CaptureQueriesContext`; it is not an HTTP latency measurement. It reports SQL counts separately for the first asset page, filtered asset page, and asset detail. It requires the same disposable database/manifest and only the benchmark username; no password is used.

```sh
uv run --locked --group dev python scripts/perf/bench.py \
  --manifest artifacts/perf-manifest.json \
  --count-queries \
  --output artifacts/perf-query-counts.json
```

Both the JSON `mode` field and Markdown heading say `QUERY COUNTS, NOT LATENCY`. The query-count output does not include p50/p95/p99 values.

### Manifest contract

The JSON manifest must contain a `benchmark` object with `asset_id`, `tenant_group_id`, `filtered_q`, `search_term`, `import_tenant_id`, `asset_type_id`, and `status_id`. It may also provide `asset_tag_prefix`, `filtered_result_text`, `detail_result_text`, and `search_result_text`; the defaults are the labels emitted by the target-volume seeder. If the manifest contains `database`, it must match `ITAMBOX_DB_NAME`.
