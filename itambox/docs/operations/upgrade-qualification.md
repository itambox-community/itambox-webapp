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
| `v1.0.2` | `v1.0.1` | 2026-11-15 | Passed (migration-state rehearsal) |

## `v1.0.2` from `v1.0.1`

- **Target:** the `1.0.2` candidate at `ea4d3854` (main after #1100–#1103),
  before the release tag.
- **Origin:** a disposable database built by running the `v1.0.1`
  (`16aa796d`) migrations and seeding synthetic rows, not a `pg_dump` of a
  production database.
- **Environment:** development host `tyr`, PostgreSQL 18 container
  `itambox-dev-pg`, database `itambox_upgrade_rehearsal` (dropped afterwards).

Five `pending` events with `tenant_id IS NULL` and no `tenant_scope` provenance
were created with the 1.0.1 ORM: an asset with a tenant, a tenant-owned
supplier, a global supplier, a manufacturer without a tenant anchor, and an
asset whose row was missing.

| Step | Command | Result |
|---|---|---|
| 1.0.1 schema | `manage.py migrate --noinput` at `16aa796d` | applied |
| Seed | five NULL-tenant events | `pending`, `tenant_id NULL` |
| Migrate | `manage.py migrate --noinput` at `ea4d3854` | `core.0005_import_preview`, `extras.0007_backfill_event_tenant` applied |
| System check | `manage.py check` | no issues |
| Model drift | `makemigrations --check --dry-run` | `No changes detected` |

State after the upgrade:

| Object | Status | Tenant | Interpretation |
|---|---|---|---|
| asset with tenant | `pending` | reconstructed | direct anchor repaired |
| tenant-owned supplier | `pending` | reconstructed | direct anchor repaired |
| global supplier, unproven | `dead` | NULL | `outbox.backfill_unassigned` |
| manufacturer, no anchor | `pending` | NULL | legitimate global event |
| asset row missing | `dead` | NULL | `outbox.backfill_unassigned` |

**Scope.** Synthetic dataset only. The run exercised the predecessor schema, the
two new migrations, post-upgrade system checks, and the resulting outbox state.
It did not exercise rule or webhook fan-out after the upgrade, media files, or a
restored production dump. Treat it as the migration-state qualification for
1.0.1 → 1.0.2; a restored-dump run remains the stronger evidence class.


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
