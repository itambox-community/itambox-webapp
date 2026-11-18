# Upgrade qualification record

Each release declares one supported upgrade origin. This page records the
restored-database run behind that declaration, so operators can see what was
actually exercised instead of inferring it from a green build.

A qualification proves that a **restored predecessor database** moves to the
target release through the ordinary commands and serves requests afterwards. It
does not replace the [recovery drill](recovery-drill.md), and it is not a test
against a copy of a specific production database.

## Qualified paths

| Target | Origin | Qualified on | Result |
|---|---|---|---|
| `v1.0.0` | `v1.0.0-rc.2` | 2026-11-13 | Passed |
| `v1.0.2` | `v1.0.1` | 2026-11-16 | Passed (restored-dump drill) |
| `v1.0.3` | `v1.0.2` | 2026-11-18 | Passed (final-candidate restored-dump check) |

`v1.0.3` introduces no database migrations. Its supported origin is `v1.0.2`;
the final candidate restored the v1.0.2 dump and served requests, and the run is
recorded below. The earlier `v1.0.2` run remains the migration-pair evidence.

## `v1.0.3` from `v1.0.2`

- **Target:** the final candidate built from `release/1.0.3` at revision
  `cab19e08b510540521c81d5d692cf8c165478ef2`, local image `itambox-drill-candidate:1.0.3` (image id
  `sha256:33cddffd6cbbf1e67430934db18d9d306936e8e555096dd5c1f6668f53f18911`). The image was built with `ITAMBOX_VERSION=1.0.3`; the
  release workflow rebuilds the published image from the same reviewed source.
- **Origin:** the published `v1.0.2` image
  `ghcr.io/itambox-community/itambox-webapp@sha256:3a4836a6bf17c4bc100d4d18397865ffd22e4cb1758a517c7830929a34188a80`
  (tag `v1.0.2`, revision `0ab2fe6159afd9584c124418d1dab16b5697d5bd`). The
  synthetic installation was dumped with `pg_dump --format=custom`; dump sha256
  `d01296cfe11423279f2bda1b3eafa69f2f4fe028bd3a707731ea62bd50f00a95`, 893,400
  bytes. It contains one tenant, one asset and one user; this is a synthetic
  test installation, not a production dump.
- **Environment:** development host `tyr`, isolated Compose project
  `itambox-drill-103-final`, PostgreSQL 18 image
  `postgres:18@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336`,
  Valkey and the local application only on `127.0.0.1:18082`.

| Step | Command | Result |
|---|---|---|
| Restore | `pg_restore --no-owner --no-privileges` | exit 0 |
| Preflight (candidate) | `manage.py migration_baseline_preflight --format=json` | `current-normalized-baseline`, exit 0 |
| Migrate | `manage.py migrate --noinput` | `No migrations to apply.` |
| System check | `manage.py check` | no issues |
| Model drift | `makemigrations --check --dry-run` | `No changes detected` |
| Login | restored `drill-admin`, HTTP POST | `302` |
| Asset list | `GET /assets/assets/` with the session | `200` |
| Health | `GET /health/` | `200`, `database: ok`, `plugins: ok` |
| Worker | `manage.py qcluster` | started with the candidate |

Restore-first rollback and re-upgrade were exercised from the same pre-upgrade
dump:

| Phase | Action | Result |
|---|---|---|
| Rollback | restored the v1.0.2 dump into clean storage, ran the v1.0.2 image | login `302`, asset list `200` |
| Re-upgrade | restored the dump again, ran the candidate | preflight `current-normalized-baseline`, login `302`, asset list `200` |

**Scope.** The run restored the database only; media was not part of it. The
dataset is synthetic and the application was reachable only on loopback. This is
the final-candidate restore check for `1.0.2 → 1.0.3`; it does not replace the
[recovery drill](recovery-drill.md). Media and external delivery were not
exercised.

## `v1.0.2` from `v1.0.1`

- **Target:** the published `v1.0.2` image
  `ghcr.io/itambox-community/itambox-webapp@sha256:3a4836a6bf17c4bc100d4d18397865ffd22e4cb1758a517c7830929a34188a80`
  (tag `v1.0.2`, revision `0ab2fe6159afd9584c124418d1dab16b5697d5bd`).
- **Origin:** the published `v1.0.1` image
  `ghcr.io/itambox-community/itambox-webapp@sha256:b7ed967fe9958d4d3e1e6b6eb285b4837bcfb065dcedc7cd5e8dd32b350eb171`
  (tag `v1.0.1`, revision `16aa796d261f008408307e103b0f9223e1cdc0f0`), seeded
  with the full demo dataset and dumped with `pg_dump --format=custom`.
  Dump sha256 `e3efee1e4d910f16d26395077f594fae8f9975d813e2907792ab1b7048d845e9`,
  2.0 MiB. This is a synthetic test installation, not a production dump.
- **Environment:** development host `tyr`, isolated Compose project
  `itambox-drill-103`, PostgreSQL 18 image
  `postgres:18@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336`,
  Valkey and a local controlled HTTP receiver. The application was published
  only on `127.0.0.1:18082`; no external recipient was used.

The restored dataset contained 825 assets and 13 tenants. Four legacy events
with `tenant_id IS NULL` and no `tenant_scope` provenance were created on the
1.0.1 installation: one direct asset anchor, one indirect assignment anchor,
one global manufacturer event, and one event whose asset row was missing.

| Step | Command | Result |
|---|---|---|
| Preflight (predecessor) | `manage.py migration_baseline_preflight --format=json` | `partial-post-transition-state`, exit 1; missing `core.0005_import_preview`, `extras.0007_backfill_event_tenant` (expected) |
| Migrate | `manage.py migrate --noinput` | both migrations applied |
| Preflight (candidate) | `manage.py migration_baseline_preflight --format=json` | `current-normalized-baseline`, exit 0 |
| System check | `manage.py check` | no issues |
| Model drift | `makemigrations --check --dry-run` | `No changes detected` |
| Login | restored `drill-admin`, HTTP POST | `302` |
| Asset list | `GET /assets/assets/` with the session | `200` |
| Worker | `manage.py qcluster` | cluster running; event processing observed |

After migration (and after the worker processed the queue), the legacy events
had the following state:

| Object | Status | Tenant | Interpretation |
|---|---|---|---|
| asset event (direct anchor) | `processed` | 2 | tenant reconstructed |
| assignment event (indirect anchor) | `processed` | 2 | tenant reconstructed through `asset__tenant` |
| manufacturer event (no tenant anchor) | `processed` | NULL | legitimate global event |
| event for a missing asset | `dead` | NULL | `outbox.backfill_unassigned`; no rule fired |

The worker processed a new asset-create event and the event rule created a
`WebhookDelivery` row. The unmodified image correctly rejected the loopback
receiver with `integration.configuration` because the production SSRF guard
requires a globally routable target. To exercise the delivery state machine and
the receiver in isolation, the same delivery row was then reset and the task was
run with a test-only `request_pinned` transport override that sent the real
payload to the local receiver; it returned HTTP `200` and the receiver recorded
the event payload. The override is drill-only and is not evidence that the
production SSRF guard permits private endpoints.

Restore-first rollback and re-upgrade were both exercised from the same
pre-upgrade dump:

| Phase | Action | Result |
|---|---|---|
| Rollback | restored `v1.0.1` dump into a clean database, ran the `v1.0.1` image | login `302`, asset list `200` |
| Re-upgrade | restored the dump again, ran `v1.0.2` migrations | preflight `current-normalized-baseline`, login `302`, asset list `200` |

**Scope.** The run restored the database only; the media volume was not part of
it. The dataset is synthetic and the receiver was local. The real delivery
attempt against the unmodified image terminated at the SSRF boundary, so the
successful transport probe uses a test-only override; production webhook
authorization and SSRF behavior were not changed or weakened. This is the
restored-dump qualification for `1.0.1 → 1.0.2`; it does not replace the
[recovery drill](recovery-drill.md).

## `v1.0.0` from `v1.0.0-rc.2`

- **Target:** `v1.0.0`, image
  `ghcr.io/itambox-community/itambox-webapp@sha256:93ee2f30ed0c1093ce715e495241d2d153242f1460a22eb3c7c02d30e7629280`
  (revision `c3f21297616e8f4c015efeb33074700ac7db8d66`).
- **Origin:** a `pg_dump --format=custom` archive of the `v1.0.0-rc.2`
  qualification database, sha256
  `c94c4280961ffc250ed3656b8e5d6ef4a1ed100c6ce19c8affb08948aeab37ea`, 2.0 MB.
- **Environment:** a disposable Compose stack with the published target image and
  a fresh PostgreSQL 18 cluster, reachable only on a loopback port.

The migration files are identical between the two tags. The run still restored
the predecessor and executed the ordinary path, because that also exercises the
migration recorder, the production settings and the application stack.

| Step | Command | Result |
|---|---|---|
| Restore | `pg_restore --no-owner --no-privileges` | exit 0 |
| Preflight | `manage.py migration_baseline_preflight --format=json` | `current-normalized-baseline`, exit 0 |
| Migrate | `manage.py migrate --noinput` | `No migrations to apply.` |
| System check | `manage.py check` | no issues |
| Health | `GET /health/` | `200` with `database: ok`, `plugins: ok` |
| Login | existing restored user, seed credential | authenticated |
| Asset list | `GET /assets/assets/` with the session | `200` |
| Worker | `manage.py qcluster` | cluster running with four processes |

Recorded rows, source to target: migrations 474 to 474, assets 825 to 825,
tenants 13 to 13, users 52 to 52.

**Scope.** The run restored the database only. Media files were not part of it;
restore the media volume as described in [backup and restore](backup-restore.md)
when a deployment stores attachments. The stack is disposable and is removed
with `docker compose down -v` once the evidence is recorded.

**Reproduce.** Follow [Path A in the upgrade
runbook](upgrades.md#path-a-logical-dump-and-restore-recommended) with the target
image and the predecessor dump, and record the same commands and counts.
