# Updating a deployment

ITAMbox ships as source and, starting with `v1.0.0-beta.2`, as the official container image published with each tagged release (`ghcr.io/itambox-community/itambox-webapp:<version>`). Starting with `v1.0.0-beta.2`, each tagged release declares a supported upgrade origin — the previously tagged release — and the upgrade path from that origin is verified on the qualification stack. Upgrades from earlier prereleases (`v1.0.0-alpha.*`, `v1.0.0-beta.1`) and version skipping remain unsupported; treat any target revision outside the declared origin as a potentially breaking change and test it against a restored copy of production first.

`v1.0.0` declares `v1.0.0-rc.2` as its supported upgrade origin. The
restored-database run behind that declaration is recorded in the
[upgrade qualification record](upgrade-qualification.md).

Releases that change migrations, storage, encryption, or other recovery-critical
behavior must also pass the isolated [Recovery qualification drill](recovery-drill.md).
That runbook proves predecessor restore, candidate upgrade, fresh-install schema
parity, restore-first rollback, and re-upgrade without exposing protected values.

## Major-version database upgrade (bundled PostgreSQL 16 to 18)

The bundled `db` service in `docker-compose.yml` runs PostgreSQL 18. The image
lays out its data differently: PostgreSQL 18 builds its cluster in
`/var/lib/postgresql/18/docker` and declares `/var/lib/postgresql` as the image
volume, so the Compose mount moved from `/var/lib/postgresql/data` to
`/var/lib/postgresql`.

**Swapping the image tag of a running stack is not an upgrade.** The 16 image
initialized its cluster in the root of the named volume it was given, while the
18 image only looks for a cluster in `/var/lib/postgresql/18/docker`. Started
against an existing 16 volume, the 18 image does not read those files: it
initializes an empty cluster beside them, the container reports healthy, and the
application then runs against an empty database. Nothing is deleted, but nothing
is migrated either. A major-version change moves the data through PostgreSQL
itself, with one of the two paths below.

Both paths are stop-the-world operations for the application. Keep the 16 volume
untouched until the 18 cluster has been verified: it is the fastest rollback.

### Pre-flight

1. Take and verify a complete [backup](backup-restore.md) of the database, media
   and secrets, and record where the restored-copy check was performed.
2. Record the current revision (`git rev-parse HEAD`), the running image
   (`docker compose images`), the exact `db.image` value, and the full
   tag-and-digest reference of the 16 image you are leaving.
3. Confirm the `btree_gist` and `pg_trgm` extensions on the current cluster.
   They are the only extensions the application needs: `btree_gist` backs the
   asset reservation exclusion constraint, `pg_trgm` the global-search trigram
   indexes (migration `assets.0008_asset_search_trigram_indexes`). The 18 image
   ships `postgresql-contrib`, so a restore or `pg_upgrade` carries both over.
4. Plan the window. Path A needs it for the dump and the restore, path B needs
   the 16 and 18 binaries in one place as well as a hard stop of both clusters.
5. Prepare a **new** named volume for the 18 cluster. Do not reuse `pgdata`
   before the upgrade is verified, and never mount an 18 cluster on the volume
   the 16 cluster still needs.

### Path A: logical dump and restore (recommended)

The bundled stack has one application role and one database, and the 18 image
recreates both from the same `.env` values on first start. That makes a
single-database dump the shortest correct path.

```bash
set -Eeuo pipefail

# 1. Stop writers, keep the 16 database running for the dump.
docker compose stop app worker

# 2. Archive-of-record: the whole cluster (roles, databases, privileges).
docker compose exec -T db sh -c 'pg_dumpall -U "$POSTGRES_USER"' > pre18-cluster.sql

# 3. The application database in a restorable format.
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" --format=custom "$POSTGRES_DB"' \
  > pre18-app.dump
```

Then start an empty 18 cluster on a new volume through an override file, so the
checked-in `docker-compose.yml` keeps describing the default layout:

```yaml
# compose.pg18-volume.yml
services:
  db:
    volumes: !override
      - pgdata18:/var/lib/postgresql

volumes:
  pgdata18:
```

Reference that override for **every** Compose command from this point on. The
reliable way is to export `COMPOSE_FILE`, which makes a bare `docker compose`
resolve both files:

```bash
export COMPOSE_FILE=docker-compose.yml:compose.pg18-volume.yml
docker compose config | grep -A1 'source: pgdata18'   # guard: must print pgdata18
docker compose stop db
docker compose up -d db
docker compose ps db
```

Without that reference Compose falls back to the checked-in file, which mounts
`pgdata`. PostgreSQL 18 then initializes a second, empty cluster on `pgdata`, the
container reports healthy, and the dump below is restored into a cluster the
stack is not using. Re-run `docker compose config` before any start and confirm
that the `db` volume resolves to `pgdata18`.

Wait for the container to report healthy before restoring. The database and role
exist but hold no application objects yet, so restore without ownership and
privilege replay:

```bash
docker compose exec -T db \
  sh -c 'pg_restore -U "$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-owner --no-privileges' \
  < pre18-app.dump
```

If the bundled cluster carried extra roles, databases or cluster-wide settings,
reconcile them from `pre18-cluster.sql` by hand: its `CREATE ROLE` and
`CREATE DATABASE` statements collide with the ones the 18 entrypoint already
created from `.env`. Restoring that file wholesale is only correct into a cluster
whose globals were not initialized from the same values.

After the restore, start the application and complete the verification below.
Keep `COMPOSE_FILE` exported for those commands as well.

### Permanent operation on the 18 volume

The override is not a one-off for the migration window. Every later `docker
compose` invocation for this deployment must resolve it, otherwise the stack
starts on `pgdata` and ignores the migrated cluster. Put the setting in the
deployment `.env` next to the database credentials, which Compose already reads
for its own configuration:

```dotenv
COMPOSE_FILE=docker-compose.yml:compose.pg18-volume.yml
```

Keep `compose.pg18-volume.yml` beside `docker-compose.yml` for the lifetime of
the deployment. The checked-in file keeps describing the fresh-install layout;
the override only names this deployment's volume. If the stack is started by a
wrapper script, a systemd unit or CI, set `COMPOSE_FILE` there instead of relying
on an interactive shell. Removing the override later does not migrate anything
back: the 18 image would initialize an empty cluster on `pgdata` on the next
start.

### Path B: `pg_upgrade` (faster, needs the 16 binaries and share files)

`pg_upgrade` rewrites the physical layout instead of replaying SQL. It is much
faster on a large database, but it has more prerequisites than Path A, and it
stops before writing anything if one of them is wrong:

- the PostgreSQL 16 binaries **and** the matching `/usr/share/postgresql/16`
  tree; the server does not start without its `.bki` and locale files,
- a new 18 cluster initialized with the same locale, encoding and data-checksum
  setting as the 16 cluster,
- a **writable copy** of the 16 data directory. `pg_upgrade` starts the old
  server, which writes `postmaster.pid` into its data directory; a read-only
  mount fails with "could not connect to source postmaster".

The original 16 volume stays untouched and remains the rollback. Adjust every
volume name below to the deployment's actual names (`docker volume ls`).

Record the old cluster's settings first, while the 16 stack is still running:

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -Atc \
  "select datcollate, datctype, pg_encoding_to_char(encoding) from pg_database where datname = current_database()"'
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -Atc "show data_checksums"'
```

The bundled 16 cluster reports `off` for `data_checksums`, while the 18 image
enables checksums by default. A mismatch aborts the check with "old cluster does
not use data checksums but the new one does", so initialize the target with
`--no-data-checksums` unless the recorded value is `on`.

Stop the stack and extract both 16 trees from the pinned image:

```bash
docker compose stop app worker db

docker create --name pg16-tree postgres:16@sha256:<previous-16-index-digest> true
mkdir -p pg16/lib pg16/share
docker cp pg16-tree:/usr/lib/postgresql/16/. pg16/lib/
docker cp pg16-tree:/usr/share/postgresql/16/. pg16/share/
docker rm pg16-tree
```

Create and initialize the 18 volume, repeating the recorded locale and encoding:

```bash
docker volume create itambox_pgdata18
docker run --rm -v itambox_pgdata18:/var/lib/postgresql \
  postgres:18@sha256:<current-18-index-digest> \
  bash -c 'chown -R postgres:postgres /var/lib/postgresql && gosu postgres initdb \
    -D /var/lib/postgresql/18/docker -U itambox \
    --locale=en_US.utf8 --encoding=UTF8 --no-data-checksums \
    --auth-local=trust --auth-host=scram-sha-256'
```

Copy the 16 data directory into a writable working volume. Size it like the old
volume: the copy doubles the space requirement for the duration of the upgrade.

```bash
docker volume create itambox_pg16work
docker run --rm -v itambox_pgdata:/old:ro -v itambox_pg16work:/work \
  postgres:18@sha256:<current-18-index-digest> \
  bash -c 'cp -a /old/. /work/ && chown -R postgres:postgres /work'
```

Run the compatibility check first, then the same command without `--check`:

```bash
docker run --rm -e PGPASSWORD="${ITAMBOX_DB_PASSWORD:?set ITAMBOX_DB_PASSWORD}" \
  -v itambox_pg16work:/var/lib/postgresql/16/data \
  -v itambox_pgdata18:/var/lib/postgresql \
  -v "$PWD/pg16/lib:/usr/lib/postgresql/16:ro" \
  -v "$PWD/pg16/share:/usr/share/postgresql/16:ro" \
  postgres:18@sha256:<current-18-index-digest> \
  bash -c 'cd /tmp && gosu postgres pg_upgrade \
    --old-bindir=/usr/lib/postgresql/16/bin \
    --new-bindir=/usr/lib/postgresql/18/bin \
    --old-datadir=/var/lib/postgresql/16/data \
    --new-datadir=/var/lib/postgresql/18/docker \
    --old-port=5433 --new-port=5434 \
    --username=itambox --check'
```

`pg_upgrade` starts and stops both clusters itself, so nothing else may listen on
5433 and 5434. It copies by default; `--link` hard-links the files, makes the 16
cluster unusable and removes the fast rollback. The generated
`delete_old_cluster.sh` deletes the working copy, so do not run it while the
original 16 volume is still the rollback.

The upgraded cluster lives at `/var/lib/postgresql/18/docker`, which is exactly
what the `pgdata18:/var/lib/postgresql` mount expects. Start it through the same
`COMPOSE_FILE` override used by Path A, run the statistics refresh the tool
printed, and complete the verification below:

```bash
export COMPOSE_FILE=docker-compose.yml:compose.pg18-volume.yml
docker compose up -d db
docker compose exec -T db vacuumdb -U itambox --all --analyze-in-stages
```

Path B has no Compose-level rollback to the 16 cluster: drop the 18 volume
(`docker volume rm itambox_pgdata18`) and unset `COMPOSE_FILE` before restarting
the 16 stack.

### Verification on the 18 cluster

Run these on both clusters where possible and compare the numbers, not just the
statements. Start with the server and the extension:

```sql
SELECT version();
SELECT extname, extversion FROM pg_extension WHERE extname IN ('btree_gist', 'pg_trgm');
```

Then confirm the object population and the migration recorder:

```sql
SELECT app, count(*) FROM django_migrations GROUP BY app ORDER BY app;
SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace;
SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;
```

At the application level, from the checkout that matches the 18 stack. These
commands assume `COMPOSE_FILE` still resolves the override (exported in Path A,
or set in `.env`); confirm with `docker compose config` first:

```bash
docker compose run --rm app python manage.py migration_baseline_preflight --format=json
docker compose run --rm app python manage.py check
docker compose up -d app worker
docker compose ps
```

`migration_baseline_preflight` must exit `0`, `check` must report no errors, and
`/health/` must answer `200` with `"database": "ok"`. Log in, open the asset list,
and confirm that the reservation overlap constraint still rejects a duplicate
reservation before you drop the 16 volume.

### Rollback

The 16 volume and the 16 image reference recorded in pre-flight are the rollback
source. Because Path A and Path B both wrote only to the new 18 volume, rollback
is a second override plus a restart, and it never touches the 18 cluster:

```bash
cat > compose.pg16-rollback.yml <<'YAML'
services:
  db:
    image: postgres:16@sha256:<previous-16-index-digest>
    volumes: !override
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
YAML

docker compose stop app worker db
export COMPOSE_FILE=docker-compose.yml:compose.pg16-rollback.yml
docker compose up -d
```

Replace the `COMPOSE_FILE` line in `.env` with the rollback value (or remove it
and export it in the shell) so later starts do not drift back to the 18 cluster.
If the upgrade also applied new migrations, restore the pre-upgrade application
revision as well; a schema that the old application does not know is not rolled
back by the volume switch.

Do not point the 16 image at the new volume: the 16 image expects its cluster at
`/var/lib/postgresql/data`, so on the 18 mount it would initialize a second empty
cluster inside the 18 volume and report healthy. If the 16 volume or image
reference is lost, restore the pre-upgrade backup instead and treat the 18
cluster as lost.

Keep the 16 volume until the 18 deployment has served normal traffic for a
release cycle; deleting it is the point at which the upgrade stops being
reversible.

## SAML account binding (#1034)

SAML logins no longer resolve an existing account by NameID alone. An assertion
that names an existing account is accepted only when that account has an active
membership in the tenant the login was started for, and superuser and staff
accounts are never signed in through SAML. This is a deliberate behaviour
change: users who previously signed in through a tenant's identity provider
without a membership in that tenant, and superusers or staff who used SAML,
are now refused. Add the missing membership, or use local password login for
administrative accounts, before upgrading. Single-identity-provider deployments
whose users are already members are unaffected.

A tenant can additionally set `SAML_CREATE_UNKNOWN_USER` to `false` in its
`ITAMBOX_TENANT_SAML_CONFIGS` entry to disable just-in-time account creation;
when the key is absent, creation stays enabled. No schema migration is involved.

## OIDC identity binding migration (#454)

The OIDC `(issuer, subject)` binding migration creates schema only. It performs no automatic backfill and never guesses a binding from email, username, UPN, tenant,
or an existing AssetHolder. Existing OIDC users therefore require an explicit
`bind_oidc_identity` operation before they can use the new login path. Until that
binding exists, legacy logins fail closed; they do not silently fall back to the
predecessor resolver.

### Forward deployment order

1. Export and protect any operator-approved binding plan separately from the
   database backup. Do not derive one automatically from mutable claims.
2. Apply the candidate schema migration (`users.0103_oidcidentity`) while the
   predecessor application is still stopped or controlled. The predecessor does
   not use the new table.
3. Deploy the candidate application code only after the schema migration has
   completed successfully.
4. For every approved existing OIDC user, follow the complete identity-binding
   procedure below. Do not admit OIDC traffic until its dry-run and confirmed
   binding have both been reviewed.
5. Admit OIDC traffic only after the required bindings and a login verification
   have been completed. Unbound legacy users continue to fail closed.

### Bind each existing OIDC identity

Create an operator-reviewed mapping of the intended ITAMbox User primary key to
the exact identity pair `(issuer, subject)`. Obtain the issuer from the active
tenant's `ITAMBOX_TENANT_OIDC_CONFIGS` entry (`OIDC_OP_ISSUER`) or the global
OIDC configuration actually used by the deployment; it must exactly match a
configured issuer. Obtain `sub` from a successfully verified authentication
response for that user from that issuer, using a trusted IdP/admin source. Do
not infer or match identity by email, username, UPN, tenant label, or an
AssetHolder value, and do not treat unverified token claims as evidence.
Independently confirm that the User primary key identifies the intended
account, and have a second operator review the mapping before applying it.

For each reviewed row, set these shell variables to the exact values from that
approved mapping (quote values so shell metacharacters are not interpreted),
then run the dry-run from the candidate checkout's `itambox/` directory:

```bash
OIDC_USER_ID='<internal-user-primary-key>'
OIDC_ISSUER='<exact-configured-issuer>'
OIDC_SUBJECT='<exact-verified-subject>'
OIDC_OPERATOR_ID='<distinct-operator-user-primary-key>'

python manage.py bind_oidc_identity \
  --user "$OIDC_USER_ID" \
  --issuer "$OIDC_ISSUER" \
  --subject "$OIDC_SUBJECT" \
  --operator "$OIDC_OPERATOR_ID" \
  --dry-run
```

Review the sanitized result and confirm the reviewed mapping still identifies
the intended account. Then repeat the same invocation with `--confirm` instead
of `--dry-run`. The command requires `--user`, `--issuer`, `--subject`, and
exactly one of those modes; the configured issuer is checked, an identity
already bound to another User is rejected, and the optional operator must be a
different User from the target. Do not put raw tokens or secrets in command
arguments or retained logs. Verify the resulting binding and perform an OIDC
login check for the intended account before admitting traffic. Repeat the
reviewed dry-run/confirm process separately for each user; never automate
email-based matching.

### Rollback and irreversible data loss

Reversing `users.0103_oidcidentity` permanently drops every binding created after
this migration. Reverse does not restore, reconstruct, or preserve those rows;
export them first if they must be recreated. A database backup is the preferred
rollback source. Without an export or backup, all bindings must be manually
recreated through the explicit command after a later forward deployment.

If a schema reverse is unavoidable, stop writers, roll back the application code
to the predecessor first, verify that no candidate code can query
`users_oidcidentity`, and only then reverse the migration. Never leave candidate
code running against a database in which the binding table has already been
dropped. A schema reverse while candidate code is active causes missing-table
failures; a code rollback before schema reverse leaves the table unused but
available for recovery.

The predecessor code is not a security-equivalent rollback: once it is serving,
its email/username claim resolution behavior is reopened. That can relink a
login through mutable claims and is the security consequence of rolling back the
binding code. Treat this as a temporary security regression, keep OIDC traffic
blocked unless explicitly accepted, and redeploy the binding code/schema in the
forward order above as soon as possible.

## Report Designer promotion and scheduled deliveries (#565)

The Report Designer was promoted from Beta to Stable and the
`ITAMBOX_FEATURE_REPORT_DESIGNER` activation flag (with its alias
`ITAMBOX_REPORT_DESIGNER_ENABLED`) is removed: designer routes and downloads no
longer require an activation setting. Stable availability must not resume
outbound deliveries that the disabled flag had suppressed, so the upgrade
carries a one-time transition for schedules whose delivery the flag was
skipping:

1. Keep `ITAMBOX_FEATURE_REPORT_DESIGNER` in place through the first upgraded
   start. The transition reads it once to tell an enabled deployment (delivery
   keeps running, nothing changes) from a disabled one; application code
   ignores the variable afterwards, and it can be removed then.
2. On a deployment that ran the designer disabled, the transition pauses
   registered, active schedules outside the migration-managed delivery
   exception set that were being skipped:
   their django-q row is removed and `is_active` is cleared, while the row, its
   configuration, and its `last_run`/`last_status` history are preserved.
   The exception set kept delivering under the disabled flag; its provenance
   marker is retired by the later #586 transition below. Already-inactive or
   unregistered schedules are not touched either.
3. Review **Extras → Scheduled Reports** after the upgrade and re-enable the
   schedules that should resume; reactivating a schedule re-registers its
   django-q row through the normal save path.

### Rollback

The transition migration refuses to reverse: un-pausing by migration could
re-arm deliveries nobody consented to. Roll back by restoring the database
backup taken before the upgrade, or leave the paused schedules in place and
re-enable them explicitly; a code-only rollback keeps the paused state, which
the predecessor application treats as an ordinary inactive schedule.

## Report Designer schema retirement (#586)

The Report Designer now has one canonical schema. Before upgrading, resolve any
live template that still uses the removed Legacy CSV Shape. The `0127`
migration scans live rows and refuses before changing the schema if any remain;
the error lists every affected template as `<name> (pk=<pk>)`. On the
pre-upgrade deployment, clear the old shape for those records through the form
or Django shell:

```python
from extras.models import ReportTemplate

ReportTemplate._base_manager.filter(deleted_at__isnull=True, advanced_mode=True).update(advanced_mode=False)
```

Templates with the default CSV shape and templates whose only special state
was the provenance marker need no rewrite. The migration drops that marker and
the old shape field; canonical selected-column CSV applies to every template
after the upgrade. Custom HTML in `template_content` is preserved and remains
sandboxed.

The reverse is refused because the removed column values cannot be
reconstructed. Roll back by restoring the pre-upgrade database backup first,
then roll back the application code; do not use a schema reverse for this
migration.

## Scheduled Reports promotion (#570)

Scheduled Reports is promoted from Beta to Stable and is always available: no
flag, probe, or environment variable switches it on or off. The promotion
activates nothing: no django-q row is created, no `next_run` is written, and no
`is_active` flag is changed by the upgrade. Schedules paused by the #565
transition above stay paused, and schedules that were never registered stay
unregistered until an operator saves or re-enables them. Existing schedules
keep their registration and their next run.

The upgrade also arms the delivery improvements before the first fire:

1. Every registered report schedule row gains the `intended_fire_at` task
   keyword argument, so a broker redelivery is recognized as a duplicate from
   the first fire after the upgrade instead of dispatching a second delivery.
2. Duplicate registration rows for one schedule (possible before registrations
   were race-safe) are collapsed onto the row the schedule references (falling
   back to the oldest), with references to a removed duplicate re-pointed;
   activation state, delivery history, and other schedules are untouched.
3. Archive rows gain the per-target delivery ledger; rows from before the
   upgrade keep a blank ledger and are never rewritten.
4. Each schedule's newest accepted occurrence is registered as a fire record,
   so idempotency stays exact-match per occurrence across the upgrade and an
   older, still-unaccepted occurrence is never dropped behind a newer one.
5. Archive rows gain the generation-scope snapshot (the tenant scope a
   retained file is valid under) and the retry-claim columns; rows from before
   the upgrade keep empty values and are never rewritten.
6. Schedules gain the retry binding that ties **Retry delivery** to the archive
   of the newest completed run; rows from before the upgrade keep an unbound
   (null) reference, so their retry action stays unavailable until the next
   completed run and old failures are recovered with **Run now** instead of a
   stale redelivery.

### Rollback

The migrations reverse cleanly: the ledger, snapshot, claim, and retry-binding
columns are dropped, the fire records are removed with the table that carried them, and
the injected task keyword argument is cleared again, because the predecessor
task signature does not accept it. A code-only rollback without the schema
rollback would leave the keyword argument in place and fail every report fire,
so reverse the migrations (or restore the pre-upgrade backup) when rolling
back. Collapsed duplicate rows are not resurrected.

## Preflight

1. Select and review an exact target commit.
2. Review the repository-root `CHANGELOG.md`, migrations, and configuration changes between the deployed and target commits.
3. Capture the current revision with `git rev-parse HEAD` and retain or export the currently running application image; rebuilding an old commit later may resolve newer base images or dependency versions.
4. Take a complete [database, media, and secret backup](backup-restore.md) and verify that it can be read.
5. Plan a maintenance window; prerelease migrations are not guaranteed to be compatible with the old application.

6. If the target changes the migration baseline, run the read-only recognition
   preflight described below before allowing the candidate to perform any
   migration work.

## Migration baseline recognition preflight

The normalized baseline retains the issue-#88 replacement layer: the historical
migration files are replaced by the 62 replacement shards, which declare the
262 replaced identities through `replaces`. Before any migration work, a
recovery retry, or a cleanup release, run this command against the intended
database from the exact candidate checkout:

```bash
cd itambox
uv run --locked --no-sync python manage.py migration_baseline_preflight --format=json
```

The command reads only first-party migration-recorder rows and the checked
runtime manifest. Exit code `0` attests recorder-row recognition only: it does
not prove schema or data parity and cannot detect rows created with `migrate
--fake`, `--fake-initial`, or direct recorder SQL. Exit code `0` covers two
recognized shapes:

- `current-normalized-baseline`: every replaced prerelease identity, every
  migration of the named baseline and every current app leaf is recorded; no
  migration action is required. A fresh install and a finished upgrade of the
  declared origin end in this same recorder state.
- A supported predecessor state declared in the checked manifest: the named
  `supported-predecessor-beta5` state, the recorder of a database that has not
  yet applied the post-transition migrations. The declared upgrade origin for
  this release is `v1.0.0-beta.6`; a beta.6 database already records those
  migrations and reports `current-normalized-baseline`. A database at the named
  predecessor state is recognized and safe to upgrade with the ordinary
  migration executor; the migration itself is still required.

A non-zero result is a stop condition. It distinguishes a partial normalized
baseline (for example a database that never ran the replacement layer, or only
part of it), an incomplete or interrupted post-transition state, empty/unmigrated
databases, and unknown or mixed first-party rows — including the states of every
earlier prerelease, which are not supported origins. Remediation is to restore
the verified predecessor and investigate the schema/data evidence as directed by
the state.

The command does not validate that an image or running process matches a
caller-declared revision. Bind the exact full Git SHA to the immutable image or
checkout separately. A failed or interrupted migration can have applied
non-atomic operations before its recorder row commits; a missing row therefore
never proves that no schema/data change occurred. Use restore-first rollback and
fresh schema/protected-canary comparisons before retrying.

This prerelease still supports no arbitrary version skipping. The supported
starting points for a release are the previously tagged release — the supported
upgrade-origin chain begins at `v1.0.0-beta.2`, and each release's changelog
states its declared origin — and, in addition, the predecessor revision
recorded in the checked manifest (`recognized_states`): the declared upgrade
origin is currently `v1.0.0-beta.6`, and the named `supported-predecessor-beta5`
state remains recognized for a database that has not yet applied the
post-transition migrations. The stable baseline replaces the whole prerelease
migration history, so a database from `v1.0.0-beta.4` or earlier must first be
upgraded to `v1.0.0-beta.5`; the preflight rejects it. Databases
created by transitional development states are explicitly not supported; the
executor fails closed with a clear recorder inconsistency instead of rewriting
their history.

## Source-built Compose update

```bash
set -Eeuo pipefail
writers_stopped=false

report_failed_upgrade() {
  rc=$?
  trap - EXIT
  if ((rc != 0)) && [[ "$writers_stopped" == true ]]; then
    echo "Upgrade failed after writers were stopped. Do not admit traffic; inspect the stack and follow the rollback plan." >&2
  fi
  exit "$rc"
}
trap report_failed_upgrade EXIT

# Record the rollback revision before changing the checkout.
ROLLBACK_REVISION=$(git rev-parse HEAD)
printf 'rollback revision: %s\n' "$ROLLBACK_REVISION"

# Fetch and select the reviewed target revision explicitly.
git fetch origin
TARGET_REVISION='full-reviewed-target-commit-sha'
git checkout --detach "$TARGET_REVISION"

# Build the new application and frontend assets from source.
docker compose build --pull

# Stop writers, migrate with the new image, and restart.
writers_stopped=true
docker compose stop app worker
docker compose run --rm app python manage.py migrate
docker compose up -d
writers_stopped=false

# Inspect startup after the deployment.
docker compose ps
docker compose logs --tail=100 app worker
```

`collectstatic` runs while the application image is built, so the included stack does not need a separate post-deployment collection step.

## Post-upgrade notes

Event rule conditions are withdrawn for the 1.0 release. Existing condition JSON
is preserved and remains readable, but rules with authored conditions will not
dispatch. After upgrading, identify affected active rules with:

```bash
python manage.py eventrule_withdrawn_report
```

## Rollback

Do not assume a Django migration can be reversed safely. A migration may be irreversible or may discard data when reversed. The reliable rollback is the complete pre-update set:

1. Stop the application and worker.
2. Restore the retained previous image, or check out and rebuild the recorded previous revision. A rebuild is not necessarily byte-identical when base images or ranged dependencies have moved.
3. Restore the matching database, media, and secrets using [Backup and restore](backup-restore.md).
4. Start the prior application and verify login, attachments, encrypted fields, and background processing.

If an update fails before any migration runs, returning to the previous source revision and rebuilding may be sufficient. Once a migration starts, use the tested backup-based rollback unless that specific migration has been reviewed and proven reversible.

## Identifying the deployed revision

`/api/status/`, the login page, and the application footer report version metadata. This identifies the declared software version, not the exact deployed Git commit. `/api/status/` follows the deployment's API authentication policy; use an authenticated browser session or a client that reads credentials from an approved secret source when authentication is required. Do not place an API token literally in a command line. Record the source revision separately in the deployment system:

```bash
git rev-parse HEAD
git status --short
docker compose images
```

Keep the checkout clean and pin the recorded commit so a later rebuild uses the same source.
