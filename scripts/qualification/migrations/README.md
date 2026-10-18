# Migration qualification

Historical migration-rehearsal suites moved out of the normal `serial_only`
lane live here. They remain explicitly runnable:

```bash
PYTHONPATH=itambox pytest scripts/qualification/migrations/
```

They are wired into CI when migration files change and during release
qualification. Do not move genuine lock/concurrency coverage into this area.

## Supported-upgrade qualification (P1/P2)

`run-supported-upgrade-p1p2.sh` drives the supported-upgrade qualification for
the normalized migration graph. For both #479 predecessors declared in the
checked baseline manifest — P1 `deef4c8b…` (pre-squash) and P2 `2246573f…`
(transition release) — it constructs the predecessor database from the exact
revision in a clean worktree, loads representative pre-#479 data, upgrades with
the candidate, asserts preflight recognition (`supported-predecessor-*` before,
`current-normalized-baseline` after), re-runs the migration executor for
idempotency, verifies data, composition, constraints, and tenant isolation, runs
the demo seed, and compares schema evidence and the migration recorder against a
fresh install. It exits `0` only when both paths pass end to end
(`SUPPORTED_UPGRADE_PASS`).

The declared release-to-release upgrade origin is a third predecessor in the
same manifest (`v1.0.0-beta.3`, `supported-predecessor-beta3`). Its recorder state
is pinned by `itambox/core/tests/test_migration_baseline_preflight.py`: the
exact predecessor rows are recognized, and partial, skipped, mixed, and unknown
variations of that state stay rejected.

Requirements: a full clone (every declared predecessor revision present), `git`, `uv`,
`python3`, and a reachable PostgreSQL. Database access defaults to the local
PostgreSQL client tools; point them and the application at the server through
the `PG*` / `ITAMBOX_DB_*` environment variables. For a container-hosted
PostgreSQL set `ITAMBOX_QUALIFICATION_PSQL`,
`ITAMBOX_QUALIFICATION_CREATEDB`, and `ITAMBOX_QUALIFICATION_DROPDB`
accordingly (for example `docker exec -i itambox-dev-pg psql -U itambox`).

```bash
scripts/qualification/migrations/run-supported-upgrade-p1p2.sh
# evidence directory: ITAMBOX_QUALIFICATION_EVIDENCE (default: a fresh temp dir)
```
