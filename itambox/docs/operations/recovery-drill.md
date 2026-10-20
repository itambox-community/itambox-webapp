# Recovery and upgrade qualification drill

Run this drill before a release that changes migrations, storage, encryption, or
other recovery-critical behavior. It proves that the documented backup set can
restore the previous release, that the restored system can upgrade to the
candidate release, and that rollback does not depend on reversing migrations.

This is a **destructive drill**. Run it only in isolated, synthetic environments.
Never point the commands in this document at production volumes, databases,
media, hostnames, queues, mail relays, or webhook receivers.

## What the drill proves

A successful run demonstrates all of the following:

- the database dump, media archive, runtime secrets, and image/source revision
  form a usable recovery set;
- encrypted SMTP, license, and webhook canaries still decrypt without exposing
  their plaintext values;
- a synthetic API token remains unexpired, active, accessible, and bound to the
  same tenant as the selected tenant-scoped canaries;
- a selected media canary has identical content before and after recovery;
- the exact predecessor can upgrade to the candidate release;
- fresh-install and upgraded PostgreSQL schemas are semantically equivalent;
- a failed candidate can be rolled back by restoring the predecessor recovery
  set into clean storage;
- the rolled-back copy can be upgraded again, proving that the recovery set is
  not a one-use artifact.

The drill does not prove infrastructure-level disaster recovery outside the
ITAMbox stack, such as restoring the container host itself.

## Safety and prerequisites

Obtain approval for the destructive environment changes before starting. Record
that approval with the drill evidence.

Prepare:

1. Three isolated Compose projects with unique names, networks, databases, and
   volumes: predecessor upgrade, fresh candidate, and clean rollback/re-upgrade.
   Only the upgrade project publishes the application port.
2. Egress controls that prevent synthetic email, webhook, and background-job
   traffic from reaching real services.
3. An exact predecessor revision and an exact candidate revision. Use full,
   lowercase 40-character Git SHAs or immutable image digests, never mutable
   branch names or image tags.
4. The complete predecessor recovery set described in
   [Backup and restore](backup-restore.md): PostgreSQL dump, media archive,
   runtime environment/secrets, checksums, and the matching source or image.
5. The same immutable PostgreSQL image digest (therefore the exact same server
   version) and required extensions for both paths. Use the complete `db.image`
   reference from the candidate checkout's `docker-compose.yml` as the approved
   value; see the [release image policy](release-image-policy.md).
6. Four explicit synthetic canaries in one tenant in the predecessor database:
   - a license with a non-empty encrypted product key;
   - email settings with a non-empty encrypted SMTP password;
   - a disabled webhook endpoint with a non-empty encrypted secret;
   - a file attachment with known synthetic content whose target belongs to that
     tenant.
7. A synthetic, unexpired API token bound to the same tenant, with an active user
   who still has tenant access. Its plaintext is available for the duration of
   the drill only. Do not reuse an operator or production token.

Keep all evidence private while the drill runs:

```bash
umask 077
set +x
mkdir -p evidence
```

Do not copy `.env`, dumps, archives, plaintext tokens, encryption keys, or the
probe key into an issue, pull request, CI log, or evidence JSON file.

## Migration baseline recognition gate

The current candidate ships a read-only `migration_baseline_preflight` command and
checked manifest. The gate is a cleanup-release admission check, not a substitute
for the ordinary upgrade. In this Path C drill, a restored `v1.0.0-beta.5`
predecessor is expected to report `supported-predecessor-beta5` (zero:
recognized supported starting point, migration required)
before the normal candidate migration; after that migration completes, rerun the
gate and require `current-normalized-baseline` (zero). Every future release must
run the gate before any migration writes and stop on every non-zero result. The command only reads `django_migrations` and cannot detect a
failed non-atomic migration whose operations ran before its recorder row committed.
For any interrupted or failed run, restore first and compare schema, data, and
protected-canary evidence; do not infer safety from a missing recorder row.

The checked layout is normalized: the prerelease migration files are replaced
by the named baseline migrations that declare them through `replaces`. This gate does
not write recorder rows, change `replaces`, or authorize arbitrary release
skips.

## Record immutable inputs and build exact application images

Record non-secret inputs in `evidence/drill-inputs.txt`. The two project names
must use the dedicated drill prefix; every later Compose operation goes through
the project-bound helper functions below:

```bash
export PREDECESSOR_REVISION='<40-character-sha>'
export CANDIDATE_REVISION='<40-character-sha>'
export UPGRADE_PROJECT='itambox-drill-upgrade-<run-id>'
export FRESH_PROJECT='itambox-drill-fresh-<run-id>'
export ROLLBACK_PROJECT='itambox-drill-rollback-<run-id>'
export COMPOSE_FILE="$PWD/docker-compose.yml"
export DRILL_ENV_FILE="$PWD/.env"
export RECOVERY_SET='<absolute-private-recovery-set-path>'
export DRILL_RUNTIME_DIR="$PWD/evidence/runtime"
export PREDECESSOR_CHECKOUT="$DRILL_RUNTIME_DIR/predecessor"
export CANDIDATE_CHECKOUT="$DRILL_RUNTIME_DIR/candidate"
export PREDECESSOR_IMAGE="itambox-drill-predecessor:$PREDECESSOR_REVISION"
export CANDIDATE_IMAGE="itambox-drill-candidate:$CANDIDATE_REVISION"
# Copy the exact tag-and-digest value from db.image in docker-compose.yml.
export POSTGRES_IMAGE='postgres:18@sha256:<approved-postgres-18-index-digest>'
export UPGRADE_OVERRIDE="$DRILL_RUNTIME_DIR/upgrade-image.yml"
export FRESH_OVERRIDE="$DRILL_RUNTIME_DIR/fresh-image.yml"
export ROLLBACK_OVERRIDE="$DRILL_RUNTIME_DIR/rollback-image.yml"

case "$UPGRADE_PROJECT:$FRESH_PROJECT:$ROLLBACK_PROJECT" in
  itambox-drill-*:itambox-drill-*:itambox-drill-*) ;;
  *) echo 'unsafe drill project names' >&2; exit 1 ;;
esac
test "$UPGRADE_PROJECT" != "$FRESH_PROJECT"
test "$UPGRADE_PROJECT" != "$ROLLBACK_PROJECT"
test "$FRESH_PROJECT" != "$ROLLBACK_PROJECT"
test -f "$COMPOSE_FILE"
test -f "$DRILL_ENV_FILE"
test -d "$RECOVERY_SET"
mkdir -p "$DRILL_RUNTIME_DIR"
printf 'predecessor=%s\ncandidate=%s\npostgres=%s\nupgrade_project=%s\nfresh_project=%s\nrollback_project=%s\n' \
  "$PREDECESSOR_REVISION" "$CANDIDATE_REVISION" "$POSTGRES_IMAGE" \
  "$UPGRADE_PROJECT" "$FRESH_PROJECT" "$ROLLBACK_PROJECT" \
  > evidence/drill-inputs.txt

git cat-file -e "${PREDECESSOR_REVISION}^{commit}"
git cat-file -e "${CANDIDATE_REVISION}^{commit}"
git worktree add --detach "$PREDECESSOR_CHECKOUT" "$PREDECESSOR_REVISION"
git worktree add --detach "$CANDIDATE_CHECKOUT" "$CANDIDATE_REVISION"
test -z "$(git -C "$PREDECESSOR_CHECKOUT" status --porcelain)"
test -z "$(git -C "$CANDIDATE_CHECKOUT" status --porcelain)"

docker build --pull=false \
  --label "org.opencontainers.image.revision=$PREDECESSOR_REVISION" \
  -t "$PREDECESSOR_IMAGE" "$PREDECESSOR_CHECKOUT"
docker build --pull=false \
  --label "org.opencontainers.image.revision=$CANDIDATE_REVISION" \
  -t "$CANDIDATE_IMAGE" "$CANDIDATE_CHECKOUT"

docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "$PREDECESSOR_IMAGE" "$CANDIDATE_IMAGE" >> evidence/drill-inputs.txt
```

Create project-specific image overrides. `--no-build` is used later so Compose
cannot silently rebuild from whichever checkout happens to be current:

```bash
write_image_override() {
  output="$1"
  application_image="$2"
  cat > "$output" <<EOF
services:
  app:
    image: $application_image
  worker:
    image: $application_image
  db:
    image: $POSTGRES_IMAGE
EOF
}
write_image_override "$UPGRADE_OVERRIDE" "$PREDECESSOR_IMAGE"
write_image_override "$FRESH_OVERRIDE" "$CANDIDATE_IMAGE"
write_image_override "$ROLLBACK_OVERRIDE" "$PREDECESSOR_IMAGE"

upgrade_compose() {
  docker compose -p "$UPGRADE_PROJECT" -f "$COMPOSE_FILE" \
    -f "$UPGRADE_OVERRIDE" "$@"
}
fresh_compose() {
  docker compose -p "$FRESH_PROJECT" -f "$COMPOSE_FILE" \
    -f "$FRESH_OVERRIDE" "$@"
}
rollback_compose() {
  docker compose -p "$ROLLBACK_PROJECT" -f "$COMPOSE_FILE" \
    -f "$ROLLBACK_OVERRIDE" "$@"
}
verify_app_revision() {
  project="$1"
  expected_revision="$2"
  app_container="$(docker ps -q \
    --filter "label=com.docker.compose.project=$project" \
    --filter 'label=com.docker.compose.service=app')"
  test -n "$app_container"
  actual_revision="$(docker inspect --format \
    '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "$app_container")"
  test "$actual_revision" = "$expected_revision"
}

restore_drill_project() {
  local project="$1"
  local override="$2"
  local media_restore_dir
  local -a compose
  case "$project:$override" in
    "$UPGRADE_PROJECT:$UPGRADE_OVERRIDE"|\
    "$ROLLBACK_PROJECT:$ROLLBACK_OVERRIDE") ;;
    *) echo 'unsafe restore target' >&2; return 1 ;;
  esac

  compose=(
    docker compose -p "$project" -f "$COMPOSE_FILE" -f "$override"
  )
  # Verify the recovery-set checksums before using any artifact.
  (cd "$RECOVERY_SET" && sha256sum -c SHA256SUMS)
  cmp -- "$RECOVERY_SET/environment" "$DRILL_ENV_FILE"

  "${compose[@]}" up -d --no-build db valkey
  "${compose[@]}" stop app worker
  "${compose[@]}" create --force-recreate app worker
  "${compose[@]}" exec -T db sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
  "${compose[@]}" exec -T db sh -c \
    'pg_restore -U "$POSTGRES_USER" --no-owner -d "$POSTGRES_DB"' \
    < "$RECOVERY_SET/database.dump"

  media_restore_dir="$(mktemp -d "$DRILL_RUNTIME_DIR/media-restore.XXXXXX")"
  trap 'rm -rf -- "$media_restore_dir"' RETURN
  tar -xzf "$RECOVERY_SET/media.tar.gz" -C "$media_restore_dir"
  "${compose[@]}" cp "$media_restore_dir/." app:/app/media/
  "${compose[@]}" run --rm --no-deps --user root app \
    chown -R appuser:appuser /app/media
  rm -rf -- "$media_restore_dir"
  trap - RETURN
}
```

Record all resulting application image IDs and the resolved PostgreSQL digest.
The probe JSON contains only `declared_revision`; trust it only together with the
independently verified runtime image label and recorded image ID.

## Prepare version-independent probes

The management commands are delivered by the candidate and therefore do not
exist in an immutable predecessor checkout. Extract the exact candidate command
files to a private host directory and mount them read-only into every probe
container. This adds probe code without changing predecessor image or source:

```bash
export PROBE_DIR="$PWD/evidence/probes"
mkdir -p "$PROBE_DIR"
git show "$CANDIDATE_REVISION:itambox/core/management/commands/capture_recovery_evidence.py" \
  > "$PROBE_DIR/capture_recovery_evidence.py"
git show "$CANDIDATE_REVISION:itambox/core/management/commands/capture_schema_evidence.py" \
  > "$PROBE_DIR/capture_schema_evidence.py"

export RECOVERY_PROBE_MOUNT="$PROBE_DIR/capture_recovery_evidence.py:/app/core/management/commands/capture_recovery_evidence.py:ro"
export SCHEMA_PROBE_MOUNT="$PROBE_DIR/capture_schema_evidence.py:/app/core/management/commands/capture_schema_evidence.py:ro"

git show "$CANDIDATE_REVISION:itambox/core/migration_preflight.py" \
  > "$PROBE_DIR/migration_preflight.py"
git show "$CANDIDATE_REVISION:itambox/core/management/commands/migration_baseline_preflight.py" \
  > "$PROBE_DIR/migration_baseline_preflight.py"
git show "$CANDIDATE_REVISION:itambox/core/migration_baseline_manifest.json" \
  > "$PROBE_DIR/migration_baseline_manifest.json"
sha256sum "$PROBE_DIR"/*.py "$PROBE_DIR/migration_baseline_manifest.json" > evidence/probe-sha256.txt

export MIGRATION_PREFLIGHT_MODULE_MOUNT="$PROBE_DIR/migration_preflight.py:/app/core/migration_preflight.py:ro"
export MIGRATION_PREFLIGHT_COMMAND_MOUNT="$PROBE_DIR/migration_baseline_preflight.py:/app/core/management/commands/migration_baseline_preflight.py:ro"
export MIGRATION_PREFLIGHT_MANIFEST_MOUNT="$PROBE_DIR/migration_baseline_manifest.json:/app/core/migration_baseline_manifest.json:ro"
```

Use these same files for predecessor, restored, upgraded, rollback, and fresh
captures. If a command is incompatible with the predecessor model API, the drill
fails closed; update the probe compatibly and review its new checksum rather than
modifying the predecessor.

```bash
(cd "$RECOVERY_SET" && sha256sum -c SHA256SUMS)
```

A checksum proves integrity, not confidentiality. Keep the recovery set on an
encrypted filesystem or encrypt it before moving it off the host.

## Create short-lived comparison credentials

`capture_recovery_evidence` emits HMACs rather than raw values or unkeyed
hashes. The same temporary probe key must be supplied to every recovery probe in
one drill. Never retain that key with the evidence; without it, the saved HMACs
cannot be used for practical offline guessing of low-entropy canaries.

```bash
export ITAMBOX_RECOVERY_PROBE_KEY="$(openssl rand -hex 32)"
read -rsp 'Synthetic recovery API token: ' ITAMBOX_RECOVERY_API_TOKEN
printf '\n'
export ITAMBOX_RECOVERY_API_TOKEN

export RECOVERY_LICENSE_PK='<pk>'
export RECOVERY_EMAIL_SETTINGS_PK='<pk>'
export RECOVERY_WEBHOOK_PK='<pk>'
export RECOVERY_ATTACHMENT_PK='<pk>'
```

Create one random, non-owning probe role in each drill database after its schema
exists. The function sends the password over stdin to `psql`; it never places the
password in a process argument. Default privileges keep later candidate-created
tables readable when migrations use the same database owner.

```bash
export PROBE_DB_USER='itambox_recovery_probe'
export PROBE_DB_PASSWORD="$(openssl rand -hex 32)"
export PROBE_ENV_FILE="$(mktemp)"
export PROBE_ENV_OVERRIDE="$DRILL_RUNTIME_DIR/probe-env.yml"
chmod 600 "$PROBE_ENV_FILE"
printf '%s=%s\n' \
  ITAMBOX_RECOVERY_PROBE_KEY "$ITAMBOX_RECOVERY_PROBE_KEY" \
  ITAMBOX_RECOVERY_API_TOKEN "$ITAMBOX_RECOVERY_API_TOKEN" \
  ITAMBOX_DB_USER "$PROBE_DB_USER" \
  ITAMBOX_DB_PASSWORD "$PROBE_DB_PASSWORD" \
  > "$PROBE_ENV_FILE"
cat > "$PROBE_ENV_OVERRIDE" <<EOF
services:
  app:
    env_file:
      - $PROBE_ENV_FILE
EOF
chmod 600 "$PROBE_ENV_OVERRIDE"

probe_compose() {
  project="$1"
  override="$2"
  shift 2
  docker compose -p "$project" -f "$COMPOSE_FILE" -f "$override" \
    -f "$PROBE_ENV_OVERRIDE" "$@"
}

grant_probe_read_access() {
  project="$1"
  override="$2"
  {
    printf "\\set probe_password '%s'\n" "$PROBE_DB_PASSWORD"
    cat <<'SQL'
SELECT format(
  'CREATE ROLE itambox_recovery_probe LOGIN PASSWORD %L', :'probe_password'
) WHERE NOT EXISTS (
  SELECT FROM pg_roles WHERE rolname = 'itambox_recovery_probe'
) \gexec
ALTER ROLE itambox_recovery_probe LOGIN PASSWORD :'probe_password';
SELECT format(
  'GRANT CONNECT ON DATABASE %I TO itambox_recovery_probe', current_database()
) \gexec
GRANT USAGE ON SCHEMA public TO itambox_recovery_probe;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO itambox_recovery_probe;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO itambox_recovery_probe;
SQL
  } | docker compose -p "$project" -f "$COMPOSE_FILE" -f "$override" \
    exec -T db sh -c \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}
```

The role has only `CONNECT`, schema `USAGE`, and `SELECT` on the synthetic drill
database; it must not own objects or receive write privileges. Remove its private
env file when the drill completes.

```bash
capture_recovery() {
  project="$1"
  override="$2"
  revision="$3"
  output="$4"
  probe_compose "$project" "$override" \
    run --rm -T --no-deps --no-build \
    -v "$RECOVERY_PROBE_MOUNT" \
    -v "${project}_media:/app/media:ro" \
    app python manage.py capture_recovery_evidence \
    --revision "$revision" \
    --license-pk "$RECOVERY_LICENSE_PK" \
    --email-settings-pk "$RECOVERY_EMAIL_SETTINGS_PK" \
    --webhook-pk "$RECOVERY_WEBHOOK_PK" \
    --attachment-pk "$RECOVERY_ATTACHMENT_PK" \
    > "$output"
}
capture_upgrade_recovery() {
  capture_recovery "$UPGRADE_PROJECT" "$UPGRADE_OVERRIDE" "$1" "$2"
}
capture_rollback_recovery() {
  capture_recovery "$ROLLBACK_PROJECT" "$ROLLBACK_OVERRIDE" "$1" "$2"
}

# Run this immediately before creating the predecessor recovery set, while the
# seeded predecessor source stack is healthy.
grant_probe_read_access "$UPGRADE_PROJECT" "$UPGRADE_OVERRIDE"
capture_upgrade_recovery \
  "$PREDECESSOR_REVISION" evidence/predecessor-before.json
```

Create and checksum the predecessor recovery set now. Only after its restore
artifacts and checksums have been verified, remove the isolated seeded source
volumes so Phase 1 cannot accidentally reuse them:

```bash
upgrade_compose down -v --remove-orphans
test -z "$(docker ps -aq \
  --filter "label=com.docker.compose.project=$UPGRADE_PROJECT")"
test -z "$(docker volume ls -q \
  --filter "label=com.docker.compose.project=$UPGRADE_PROJECT")"
```

The command fails closed if an ID is missing, a protected value is empty or not
encrypted at rest, tenant-bound canaries disagree, the API token is unusable, or
the declared revision is not a full Git SHA. Its versioned JSON also records the
PostgreSQL version, complete applied-migration identity, media-name/content
HMACs, media byte size, and selected object counts. Treat
`declared_revision` as metadata; bind it to the runtime using the verified image
revision recorded in the drill manifest.

## Phase 1: restore the predecessor

Restore database and media into the clean upgrade project with the
project-bound helper. It verifies the recovery-set manifest and requires the
active synthetic environment to match the backed-up environment byte-for-byte.
The helper does not start app/worker:

```bash
restore_drill_project "$UPGRADE_PROJECT" "$UPGRADE_OVERRIDE"
if ! docker compose -p "$UPGRADE_PROJECT" -f "$COMPOSE_FILE" -f "$UPGRADE_OVERRIDE" \
  run --rm -T --no-deps --no-build \
  -v "$MIGRATION_PREFLIGHT_MODULE_MOUNT" \
  -v "$MIGRATION_PREFLIGHT_COMMAND_MOUNT" \
  -v "$MIGRATION_PREFLIGHT_MANIFEST_MOUNT" \
  app python manage.py migration_baseline_preflight --format=json \
  > evidence/predecessor-migration-preflight.json \
  2> evidence/predecessor-migration-preflight.stderr; then
  echo 'predecessor preflight did not recognize the supported starting point' >&2
  exit 1
fi
python -c "import json; r=json.load(open('evidence/predecessor-migration-preflight.json')); assert r['state'] == 'supported-predecessor-beta5', r['state']; assert r['exit_code'] == 0, r['exit_code']"
upgrade_compose up -d --no-build app worker
verify_app_revision "$UPGRADE_PROJECT" "$PREDECESSOR_REVISION"
upgrade_compose exec -T app python manage.py check
upgrade_compose exec -T app python manage.py migrate --check
upgrade_compose ps app worker
grant_probe_read_access "$UPGRADE_PROJECT" "$UPGRADE_OVERRIDE"
capture_upgrade_recovery \
  "$PREDECESSOR_REVISION" evidence/predecessor-restored.json
```

Repeat the documented health, login, media-download, and API-authentication
checks. The two recovery evidence files must match exactly. Their
`declared_revision` values are identical because both describe the predecessor:

```bash
python - <<'PY'
import json
from pathlib import Path
before = json.loads(Path('evidence/predecessor-before.json').read_text())
after = json.loads(Path('evidence/predecessor-restored.json').read_text())
if before != after:
    raise SystemExit('restored predecessor evidence differs from source')
print('predecessor restore evidence: identical')
PY
```

A mismatch is a failed restore. Stop and diagnose it before testing an upgrade.

## Phase 2: upgrade the restored predecessor

Before changing the restored copy, capture its forensic migration plan and
canonical schema, then stop every predecessor writer:

```bash
upgrade_compose exec -T app python manage.py showmigrations --plan \
  > evidence/predecessor-migrations.txt
probe_compose "$UPGRADE_PROJECT" "$UPGRADE_OVERRIDE" \
  run --rm -T --no-deps --no-build \
  -v "$SCHEMA_PROBE_MOUNT" \
  app python manage.py capture_schema_evidence \
  > evidence/predecessor-schema.json
upgrade_compose stop app worker
test -z "$(upgrade_compose ps -q app worker)"
```

Select the already-built candidate image by rewriting only the upgrade project's
image override. Keep the restored database, media, and recovery secrets
unchanged. Never use `migrate --fake` or edit an applied migration to make the
drill pass. `scripts/migration_audit.py` is a repository-root audit tool, not a
runtime management command: the minimal runtime image does not contain the
repository-root `scripts/` directory. Run it from the exact candidate checkout
on the host (or in a separately prepared audit environment that has that exact
checkout and its Git metadata), not with `docker compose run ... app`.

```bash
write_image_override "$UPGRADE_OVERRIDE" "$CANDIDATE_IMAGE"
upgrade_compose config --images > evidence/upgrade-images.txt
grep -Fx "$CANDIDATE_IMAGE" evidence/upgrade-images.txt
(cd "$CANDIDATE_CHECKOUT" && python scripts/migration_audit.py --check)
upgrade_compose run --rm -T --no-deps --no-build \
  app python manage.py migrate --plan \
  > evidence/candidate-migrate-plan.txt
if ! upgrade_compose run --rm -T --no-deps --no-build \
  app python manage.py migrate --noinput; then
  echo 'candidate migration failed; keep writers stopped and restore clean' >&2
  exit 1
fi
upgrade_compose up -d --no-build app worker
verify_app_revision "$UPGRADE_PROJECT" "$CANDIDATE_REVISION"
upgrade_compose exec -T app python manage.py check
upgrade_compose exec -T app python manage.py migrate --check
upgrade_compose run --rm -T --no-deps --no-build \
  app python manage.py migration_baseline_preflight --format=json \
  > evidence/upgraded-migration-preflight.json \
  2> evidence/upgraded-migration-preflight.stderr
upgrade_compose ps app worker
upgrade_compose logs --tail=100 app worker
```

Do not restart the predecessor image after a migration failure. Preserve the
failed database for forensics and follow Phase 5 against clean replacement
storage.

Inspect worker and application logs locally for migration, decryption, queue,
and startup failures. Record only a pass/fail summary; do not retain raw logs
until they have been checked for protected data. Repeat the health, login,
media-download, and API-authentication checks. Capture:

```bash
upgrade_compose exec -T app python manage.py showmigrations --plan \
  > evidence/upgraded-migrations.txt
probe_compose "$UPGRADE_PROJECT" "$UPGRADE_OVERRIDE" \
  run --rm -T --no-deps --no-build \
  -v "$SCHEMA_PROBE_MOUNT" \
  app python manage.py capture_schema_evidence \
  > evidence/upgraded-schema.json
capture_upgrade_recovery \
  "$CANDIDATE_REVISION" evidence/upgraded-recovery.json
```

The candidate revision is expected to differ. All recovery-critical fields must
remain equal:

```bash
python - <<'PY'
import json
from pathlib import Path
before = json.loads(Path('evidence/predecessor-restored.json').read_text())
after = json.loads(Path('evidence/upgraded-recovery.json').read_text())
keys = (
    'counts', 'ciphertext_at_rest', 'protected_value_hmacs',
    'api_token_verified', 'media',
)
changed = [key for key in keys if before[key] != after[key]]
if changed:
    raise SystemExit(f'upgrade changed recovery evidence: {changed}')
print('upgrade recovery evidence: preserved')
PY
```

If the release intentionally changes an object count, document and independently
review that expected delta rather than weakening the comparison.

## Phase 3: compare with a fresh candidate install

Build the second isolated project from empty database and media volumes using
the already verified candidate image. The app service is intentionally not
published, so its base `8000` mapping cannot collide with the upgrade project:

```bash
fresh_compose up -d --no-build db valkey
(cd "$CANDIDATE_CHECKOUT" && python scripts/migration_audit.py --check)
fresh_compose run --rm -T --no-deps --no-build \
  app python manage.py migrate --noinput
fresh_compose run --rm -T --no-deps --no-build \
  app python manage.py check
fresh_compose run --rm -T --no-deps --no-build \
  app python manage.py migrate --check
fresh_compose run --rm -T --no-deps --no-build \
  app python manage.py migration_baseline_preflight --format=json \
  > evidence/fresh-migration-preflight.json \
  2> evidence/fresh-migration-preflight.stderr
fresh_compose run --rm -T --no-deps --no-build \
  app python manage.py showmigrations --plan \
  > evidence/fresh-migrations.txt
grant_probe_read_access "$FRESH_PROJECT" "$FRESH_OVERRIDE"
probe_compose "$FRESH_PROJECT" "$FRESH_OVERRIDE" \
  run --rm -T --no-deps --no-build \
  -v "$SCHEMA_PROBE_MOUNT" \
  app python manage.py capture_schema_evidence \
  > evidence/fresh-schema.json
```

Do not give the fresh probe role migration or ownership privileges.

`capture_schema_evidence` removes physical constraint and index names while
preserving table names, complete formatted column types and array dimensions,
constraint/index semantics, extension versions, content-type/permission natural
keys, ordering, and duplicate definitions. The outputs therefore compare
semantic schemas and Django authorization metadata without hiding missing or
redundant objects:

```bash
cmp evidence/fresh-schema.json evidence/upgraded-schema.json
```

The raw `showmigrations` files are retained for forensic review, not compared
byte-for-byte: a historical predecessor path and a coordinated replacement path
can legitimately record different migration rows. The checked
`scripts/migration_audit.py --check` effective graph plus byte-identical schema,
content-type, and permission evidence form the parity gate. Raw
`pg_dump --schema-only` files may be retained as additional forensic evidence,
but their hashes are not the parity gate because physical object names can vary
across equivalent histories.

Any unexplained schema, content-type, permission, effective-graph, or extension
difference fails the release gate.

## Phase 4: exercise the rollback trigger

Exercise a controlled failure only after successful candidate evidence has been
captured. Do not modify a tracked migration, corrupt the database, or change the
recovery set. Stop the isolated candidate application and worker, then prove that
the externally used health gate fails:

```bash
export DRILL_HEALTH_URL='<isolated-candidate-health-url>'
upgrade_compose stop app worker
if curl --fail --silent --show-error "$DRILL_HEALTH_URL"; then
  echo 'health gate unexpectedly remained available' >&2
  exit 1
fi
printf 'controlled candidate outage detected as expected\n'
```

Treat the candidate as failed from this point onward. Keep database and media
volumes unchanged for forensics and continue directly to restore-first.

## Phase 5: restore-first rollback and re-upgrade

Do not use reverse migrations as the primary rollback mechanism. A release may
contain irreversible migrations, and old code may not understand a partially
reversed schema. The failed upgrade project remains stopped and untouched; all
restore work uses the clean rollback project through the same project-bound
helper:

```bash
restore_drill_project "$ROLLBACK_PROJECT" "$ROLLBACK_OVERRIDE"
rollback_compose up -d --no-build app worker
verify_app_revision "$ROLLBACK_PROJECT" "$PREDECESSOR_REVISION"
rollback_compose exec -T app python manage.py check
rollback_compose exec -T app python manage.py migrate --check
grant_probe_read_access "$ROLLBACK_PROJECT" "$ROLLBACK_OVERRIDE"
capture_rollback_recovery \
  "$PREDECESSOR_REVISION" evidence/rollback-restored.json
cmp evidence/predecessor-restored.json evidence/rollback-restored.json
```

Re-upgrade that restored copy with writers stopped and the same candidate image:

```bash
rollback_compose stop app worker
test -z "$(rollback_compose ps -q app worker)"
write_image_override "$ROLLBACK_OVERRIDE" "$CANDIDATE_IMAGE"
rollback_compose config --images > evidence/rollback-upgrade-images.txt
grep -Fx "$CANDIDATE_IMAGE" evidence/rollback-upgrade-images.txt
(cd "$CANDIDATE_CHECKOUT" && python scripts/migration_audit.py --check)
rollback_compose run --rm -T --no-deps --no-build \
  app python manage.py migrate --plan \
  > evidence/rollback-candidate-migrate-plan.txt
if ! rollback_compose run --rm -T --no-deps --no-build \
  app python manage.py migrate --noinput; then
  echo 'rollback re-upgrade failed; keep all writers stopped' >&2
  exit 1
fi
rollback_compose up -d --no-build app worker
verify_app_revision "$ROLLBACK_PROJECT" "$CANDIDATE_REVISION"
rollback_compose exec -T app python manage.py check
rollback_compose exec -T app python manage.py migrate --check
probe_compose "$ROLLBACK_PROJECT" "$ROLLBACK_OVERRIDE" \
  run --rm -T --no-deps --no-build \
  -v "$SCHEMA_PROBE_MOUNT" \
  app python manage.py capture_schema_evidence \
  > evidence/rollback-reupgraded-schema.json
capture_rollback_recovery \
  "$CANDIDATE_REVISION" evidence/rollback-reupgraded-recovery.json
cmp evidence/upgraded-schema.json evidence/rollback-reupgraded-schema.json
cmp evidence/upgraded-recovery.json evidence/rollback-reupgraded-recovery.json
```

The rollback is successful only when the predecessor is operational from clean
storage. The re-upgrade is successful only when it reaches byte-identical
candidate evidence without manual database edits.

## Pass/fail record

A release passes this drill only when all boxes are true:

- [ ] recovery-set checksums verified;
- [ ] exact predecessor and candidate revisions recorded;
- [ ] predecessor restored into clean database and media volumes;
- [ ] health, login, media download, and API authentication passed;
- [ ] protected-value HMACs, token verification, media HMAC, and required counts
      matched before and after restore;
- [ ] upgrade plan reviewed and candidate migrations completed;
- [ ] recovery evidence survived the upgrade;
- [ ] fresh and upgraded canonical schema evidence matched;
- [ ] effective migration audit passed; raw path identities were retained;
- [ ] content types and permissions matched;
- [ ] controlled data-neutral failure followed the rollback branch;
- [ ] predecessor rollback from clean storage passed;
- [ ] re-upgrade produced the same evidence as the first upgrade;
- [ ] no plaintext secret or recovery artifact entered logs, issues, PRs, or CI.

Record non-secret evidence summaries, exact revisions, timings, deviations, and
reviewer sign-off in the tracking issue. Keep raw recovery artifacts only in the
approved encrypted backup location.

Finally remove the short-lived probe material from the shell and disk:

```bash
rm -f -- "$PROBE_ENV_FILE" "$PROBE_ENV_OVERRIDE"
unset ITAMBOX_RECOVERY_PROBE_KEY ITAMBOX_RECOVERY_API_TOKEN \
  PROBE_DB_PASSWORD
test ! -e "$PROBE_ENV_FILE"
test ! -e "$PROBE_ENV_OVERRIDE"
```

## Cleanup

Keep both drill projects and their evidence until the result has been reviewed.
Cleanup is a separate approved destructive action. Confirm the exact Compose
project names before removing anything, and never run `docker compose down -v`
against the source, predecessor backup, or any non-drill project.

After separate cleanup approval, re-run the allowlist guard and remove only the
three explicitly named projects. Preserve the encrypted recovery set according
to its retention policy; remove raw evidence only after the accepted summary has
been recorded:

```bash
case "$UPGRADE_PROJECT:$FRESH_PROJECT:$ROLLBACK_PROJECT" in
  itambox-drill-*:itambox-drill-*:itambox-drill-*) ;;
  *) echo 'unsafe cleanup project names' >&2; exit 1 ;;
esac

upgrade_compose down -v --remove-orphans
fresh_compose down -v --remove-orphans
rollback_compose down -v --remove-orphans

for project in "$UPGRADE_PROJECT" "$FRESH_PROJECT" "$ROLLBACK_PROJECT"; do
  test -z "$(docker ps -aq \
    --filter "label=com.docker.compose.project=$project")"
  test -z "$(docker volume ls -q \
    --filter "label=com.docker.compose.project=$project")"
done

git worktree remove "$PREDECESSOR_CHECKOUT"
git worktree remove "$CANDIDATE_CHECKOUT"
case "$DRILL_RUNTIME_DIR" in "$PWD"/evidence/runtime) ;;
  *) echo 'unsafe runtime directory' >&2; exit 1 ;;
esac
rm -rf -- "$DRILL_RUNTIME_DIR"
test ! -e "$DRILL_RUNTIME_DIR"
```

Remove the two exact drill application images only when no retained forensic
container references them. Never broaden cleanup to image or volume globs.
