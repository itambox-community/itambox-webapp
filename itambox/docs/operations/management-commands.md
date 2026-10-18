# Management Commands

ITAMbox ships a set of Django management commands for maintenance, troubleshooting, and data operations. For a local source checkout, synchronize the locked development environment from the repository root, then invoke commands from `itambox/` through uv:

```bash
uv sync --locked --group dev
cd itambox
uv run --locked --group dev python manage.py <command> [options]
```

In the Docker Compose stack, prefix with `docker compose run --rm app`:

```bash
docker compose run --rm app python manage.py <command> [options]
```

The command reference below shows the inner `python manage.py ...` portion for brevity. Wrap local source-checkout invocations with `uv run --locked --group dev`, or use the Docker Compose prefix above.

---

## Command Reference

### `capabilities`

Report every declared capability — its grade (Stable/Beta/Experimental),
activation mode, **current** state, and the kind of source that decides it.
This is the operator's answer to "is this thing on, and what would turn it
on?". It never prints a configured value, and a failing probe is reported by
exception *type* only.

| | |
|---|---|
| **Usage** | `python manage.py capabilities [--format table\|json]` |
| **Production-safe** | Yes (read-only registry inspection; no database writes) |
| **When to use** | Verifying whether an optional capability is active, or diagnosing why a Beta/Experimental surface is missing or off. |

**Options**

| Option | Default | Purpose |
|---|---|---|
| `--format` | `table` | Output format: `table` or `json`. |

**Example**

```text
$ python manage.py capabilities
CAPABILITY                          CLASS   MODE        STATE    SOURCE          VALUE
reporting.designer                  stable  always-on   active   always          present
organization.resource_grants *      stable  always-on   active   always          present
```

A `*` marks security-critical capabilities, which cannot be deactivated.
See [Capability Maturity](capability-maturity.md) for the public contract
behind the grades.

---

### `capture_recovery_evidence`

Emit comparable, non-plaintext evidence for explicit synthetic recovery
canaries: an encrypted license key, SMTP password, webhook secret, API token,
and media attachment.

| | |
|---|---|
| **Usage** | `python manage.py capture_recovery_evidence --revision SHA --license-pk PK --email-settings-pk PK --webhook-pk PK --attachment-pk PK` |
| **Production-safe** | Read-only, but intended only for an approved isolated recovery drill with synthetic canaries. |
| **When to use** | Before and after restore, upgrade, rollback, and re-upgrade in the [Recovery qualification drill](recovery-drill.md). |

The command requires `ITAMBOX_RECOVERY_PROBE_KEY` (at least 32 bytes) and a
synthetic plaintext token in `ITAMBOX_RECOVERY_API_TOKEN`. It emits only
domain-separated HMAC-SHA256 values, ciphertext-at-rest flags, selected object
counts, token-verification status, PostgreSQL version, complete applied-migration
identity, media-name HMAC and byte size, and `declared_revision`. The declared
revision is caller-supplied metadata, not a runtime attestation; bind it to an
OCI image label or exact mounted checkout as required by the drill. The command
never emits protected plaintexts, raw media name, or the probe key.

All PK options are required. License, webhook, attachment target, and API token
must belong to the same non-null tenant. The token must resolve, be unexpired,
belong to an active tenant and active user, and remain accessible to that user.
The command also fails closed if a selected record is missing, a protected value
is empty or not encrypted, or the declared revision is not a full lowercase
40-character Git SHA.

---

### `capture_schema_evidence`

Emit a stable, read-only PostgreSQL schema inventory as compact JSON.

| | |
|---|---|
| **Usage** | `python manage.py capture_schema_evidence` |
| **Production-safe** | Yes (read-only PostgreSQL catalog queries). |
| **When to use** | Comparing fresh-install and upgraded schemas during migration and recovery qualification. |

The inventory includes public columns with PostgreSQL-formatted types and array
dimensions, constraint definitions, index definitions, installed extensions,
PostgreSQL version, Django content-type and permission natural keys, and summary
counts. Physical constraint and index names are omitted while semantic
definitions and duplicate entries are preserved. This allows equivalent
migration histories to compare byte-for-byte without hiding missing or redundant
database and authorization objects.

```bash
python manage.py capture_schema_evidence > schema-evidence.json
```

The command requires PostgreSQL and fails on another database backend.

---

### `compile_locales`

Compile `.po` translation catalogs to `.mo` binaries using a pure-Python `msgfmt` implementation — no GNU gettext required.

| | |
|---|---|
| **Usage** | `python manage.py compile_locales [LOCALE ...]` |
| **Production-safe** | Yes (read-only on `.po` files; writes `.mo` binaries into the locale tree) |
| **When to use** | After updating translation `.po` files, or after deploying a revision that changed translatable strings. Without compilation, Django serves fallback (English) strings. |

!!! note "Pure-Python, no gettext dependency"
    This command works on Windows hosts where GNU gettext is not installed. It replaces the old `itambox/compile_locale.py` standalone script.

**Options**

| Option | Purpose |
|---|---|
| `LOCALE` (positional, repeatable) | Locale codes to compile (e.g. `de`, `fr`). Omit to compile all discovered locales. |

**Examples**

```bash
# Compile all locales
python manage.py compile_locales

# Compile only German
python manage.py compile_locales de
```

---

### `export_catalogue_schema`

Write the JSON Schema that describes a community catalogue asset type, generated from the core specification vocabulary. The community catalogue repository [type-library](https://github.com/itambox-community/type-library) keeps a copy of this file as `schema/generated_schema.json` and validates every model file against it.

| | |
|---|---|
| **Usage** | `python manage.py export_catalogue_schema [--output PATH]` |
| **Production-safe** | Yes (reads the vocabulary only; writes a single file) |
| **When to use** | Refreshing the schema copy in the catalogue repository after the vocabulary changed in a release. Never edit the copy by hand. |

**Examples**

```bash
# Regenerate the committed copy
python manage.py export_catalogue_schema

# Write to a file for the catalogue repository
python manage.py export_catalogue_schema --output /tmp/generated_schema.json
```

---

### `export_datamodel`

Export the ITAMbox domain model and their direct relations as a Graphviz DOT graph. Useful for documentation, architecture reviews, and onboarding.

| | |
|---|---|
| **Usage** | `python manage.py export_datamodel [--apps ...] [--output PATH] [--hide-cross-cutting]` |
| **Production-safe** | Yes (read-only; only reads the Django model registry) |
| **When to use** | Generating data-model diagrams for documentation, reviewing schema changes, or understanding cross-app relationships. |

**Options**

| Option | Purpose |
|---|---|
| `--apps APP [APP ...]` | Limit the graph to one or more domain apps. Valid choices: `assets`, `compliance`, `core`, `extras`, `inventory`, `licenses`, `organization`, `procurement`, `software`, `subscriptions`, `users`. |
| `--output PATH` | Write DOT output to a file instead of stdout. Parent directories are created automatically. |
| `--hide-cross-cutting` | Omit ubiquitous `Tenant`, `User`, and `Tag` relations for a cleaner business-domain overview. |

**Examples**

```bash
# Full model graph to stdout
python manage.py export_datamodel

# Business-domain overview (no tenant/user/tag noise)
python manage.py export_datamodel --hide-cross-cutting

# Scope to a few apps, write to file
python manage.py export_datamodel --apps organization assets inventory --output docs/data-model.dot

# Render to SVG (requires Graphviz installed)
python manage.py export_datamodel --apps organization assets --output /tmp/model.dot
dot -Tsvg /tmp/model.dot -o docs/model.svg
```

---

### `integrity_report`

Run all tenant-integrity checks across every app and report violations. This is a **read-only** command — it never writes to the database.

| | |
|---|---|
| **Usage** | `python manage.py integrity_report [--json] [--proposals PATH] [--fail-on-findings]` |
| **Production-safe** | Yes (read-only) |
| **When to use** | Auditing multi-tenant data consistency, pre-migration validation, CI gating, or investigating cross-tenant data leaks. |

**Checks performed**

| Check | What it looks for |
|---|---|
| `null_tenant` | Operational rows with `tenant=NULL` |
| `stock_tenant_conflict` | Stock pools with conflicting item/location tenants |
| `cross_tenant_assignment` | Cross-tenant asset assignments |
| `location_site_tenant_mismatch` | Location/Site tenant mismatches |
| `po_tenant_mismatch` | Purchase orders vs destination location |
| `po_line_tenant_mismatch` | Purchase-order lines vs purchase order |
| `po_line_item_tenant_mismatch` | Purchase-order lines vs catalogue item |
| `license_seat_tenant_mismatch` | License seats vs assignment target |
| `custody_tenant_mismatch` | Custody receipts: asset vs holder |
| `rbac_grant_inconsistent` | RBAC grants: role owner vs principal tenant |
| `rbac_group_inconsistent` | User groups: ownership/membership consistency |

Each finding is classified (e.g. sharing-eligible, orphan, mismatch) so operators can decide whether to grant sharing access or fix the data.

**Options**

| Option | Purpose |
|---|---|
| `--json` | Emit the full report as JSON on stdout instead of human-readable text. |
| `--proposals PATH` | Write proposed `TenantResourceGrant` payloads (JSON) to a file for operator review. |
| `--fail-on-findings` | Exit with a non-zero status when any finding is reported — suitable for CI gates. |

**Examples**

```bash
# Readable report
python manage.py integrity_report

# JSON output for scripting
python manage.py integrity_report --json

# CI gate: fail if there are any findings
python manage.py integrity_report --fail-on-findings

# Full audit with proposed grants
python manage.py integrity_report --proposals /tmp/grants.json --fail-on-findings
```

---

### `migration_baseline_preflight`

Inspect the first-party rows in `django_migrations` and fail closed unless the
shipped normalized baseline has been fully recognized. This command is
strictly read-only: it never runs migrations, repairs recorder state, or writes
application data.

| | |
|---|---|
| **Usage** | `python manage.py migration_baseline_preflight [--format table\|json] [--database ALIAS]` |
| **Production-safe** | Yes (read-only migration-recorder inspection). |
| **When to use** | Before a migration cleanup or recovery retry, after a successful ordinary migration run, and in the CI fresh-database qualification. |

The checked manifest recognizes these states: the current normalized baseline,
the supported predecessor states (`supported-predecessor-pre-squash`,
`supported-predecessor-transition-release`, and `supported-predecessor-beta3`
for the declared upgrade origin `v1.0.0-beta.3`), a partial normalized baseline
(replacement rows missing), an incomplete post-transition state,
empty/unmigrated databases, and mixed or unknown first-party rows. The current
baseline and the recognized supported predecessor states return exit code
`0`; predecessor states are recognized as safe to upgrade and still require the
ordinary migration executor, and every other state returns a non-zero status
with a safe reason code and remediation text.

A successful preflight is not crash-recovery evidence and it is not schema or data-semantic parity evidence. It attests only to the completeness of the rows recorded in `django_migrations`; it cannot detect rows created through `migrate --fake`, `--fake-initial`, or direct recorder SQL. If a migration was
interrupted or failed after database operations began, restore the verified
predecessor first and compare schema, data, and protected-canary evidence; a
missing recorder row does not prove that no operation ran. The declared
revision in recovery evidence is metadata and must be bound separately to the
immutable image or checkout.

When the first-party migration universe changes, refresh the checked manifest
from the repository root before running the preflight or CI:

```bash
python scripts/migration_audit.py --write-preflight-manifest
python scripts/migration_audit.py --check
```

The refresh updates only source-derived ID and leaf fields. It preserves the
reviewed layout, transition release SHA, and supported-predecessor identities;
review those metadata fields separately. `--skip-git-identity-check` is for an
explicitly isolated fixture only and must not be used for CI or release work.

---

### `list_failed_tasks`

List recently failed django-q2 background tasks with their function names, timestamps, attempt counts, and tracebacks.

| | |
|---|---|
| **Usage** | `python manage.py list_failed_tasks [--limit N]` |
| **Production-safe** | Yes (read-only) |
| **When to use** | Troubleshooting background job failures, investigating alerting gaps, or verifying task health after a worker outage. |

**Options**

| Option | Default | Purpose |
|---|---|---|
| `--limit N` | `20` | Maximum number of failed tasks to show, most recent first. |

**Examples**

```bash
# Show the 20 most recent failures
python manage.py list_failed_tasks

# Show only the 5 most recent
python manage.py list_failed_tasks --limit 5
```

!!! note "Successful tasks are not persisted"
    django-q2 is configured with `save_limit = -1`, so only **failed** tasks are written to the database. Successful runs are fire-and-forget. The `Failure` table (which this command reads) is pruned by `prune_changelog` according to `ITAMBOX_QTASK_FAILED_RETENTION_DAYS`.

---

### `operational_health`

Report worker, scheduler, queue, cache, job and failure health. See [Operational health](operational-health.md).

| | |
|---|---|
| **Usage** | `python manage.py operational_health [--json] [--check]` |
| **Production-safe** | Yes (read-only, bounded probes) |
| **When to use** | Cron, systemd timers or monitoring that must notice a stopped worker, a stalled scheduler, an unreachable cache or a stuck queue. `/health/` covers database readiness only. |

**Options**

| Option | Default | Purpose |
|---|---|---|
| `--json` | off | Emit machine-readable JSON with stable keys. |
| `--check` | off | Exit non-zero when any signal is degraded. |

---

### `purge_deleted`

Permanently hard-delete soft-deleted rows that are older than the specified retention period. This is the irreversible counterpart to the soft-delete mechanism.

| | |
|---|---|
| **Usage** | `python manage.py purge_deleted [--days N] [--dry-run]` |
| **Production-safe** | **With caution.** Destroys data irreversibly. Always run `--dry-run` first. |
| **When to use** | Reclaiming storage from long-deleted records, enforcing data-retention policies for soft-deleted rows, or preparing for a backup window by removing dead rows. |

The command iterates over every model registered with the `soft_delete` feature in the ITAMbox model registry. All deletes are attributed in the audit trail (`ObjectChange`) with a system-actor marker. The purge spans all tenants; there is no per-tenant scoping.

**Options**

| Option | Default | Purpose |
|---|---|---|
| `--days N` | `30` | Delete objects soft-deleted more than N days ago. |
| `--dry-run` | `false` | Show what would be purged without deleting anything. |

**Examples**

```bash
# See how many rows would be purged with the default 30-day cutoff
python manage.py purge_deleted --dry-run

# Purge rows deleted more than 90 days ago (after verifying with --dry-run)
python manage.py purge_deleted --days 90

# Aggressive 7-day retention with dry-run first
python manage.py purge_deleted --days 7 --dry-run
```

!!! danger "Irreversible — no undo"
    Hard-deleted rows cannot be recovered through the application. Ensure your backup strategy covers the retention window you need before running this command without `--dry-run`. The audit trail records each purge event, but the purged data itself is gone.

---

### `reconcile_procurement_legacy`

Report fulfilment pledges that predate receipt attribution: a request that a partial
purchase-order receipt approved while delivered quantities were still untracked keeps its
historical approval and a blank received quantity, and later receipts are deliberately
never booked against it.

The report classifies each pledge individually by its remaining evidence: a request with
an assigned asset or a fully received line is completed history and stays untouched; a
pledge on a partially received non-serialised line is a demonstrable reconciliation
candidate only when the line's recorded received quantity cannot cover the pledge's own
allocated quantity; pledges the received units could cover in full, lines without any
receipt, and serialised lines without an assigned asset need explicit operator review and
are never closed by the command.

| | |
|---|---|
| **Usage** | `python manage.py reconcile_procurement_legacy [--apply]` |
| **Production-safe** | Read-only by default (a dry-run report). `--apply` only softly closes the demonstrable candidate pledges; completed records stay untouched, ambiguous records require explicit operator review, and recorded quantities, stock, and approval states are never rewritten. |
| **When to use** | After upgrading a deployment that ran the Asset Request Procurement Seam while it was Beta: review the report, close the demonstrable candidates, and decide the review records manually. Re-request the units if they are still needed. |

### `rotate_encryption_keys`

Re-encrypt the fields inventoried below with the current primary Fernet key. Used when rotating `ITAMBOX_FIELD_ENCRYPTION_KEYS`. This command does **not** rotate `WebhookDelivery.target_secret`.

| | |
|---|---|
| **Usage** | `python manage.py rotate_encryption_keys [--dry-run]` |
| **Production-safe** | **With caution.** Mutates encrypted data in-place. Back up the database first. |
| **When to use** | After configuring a new primary key first and retaining the old key as a decryption key. Do not remove the old key after this command: it does not rotate retained `WebhookDelivery.target_secret` snapshots or backups. |

**Encrypted fields covered**

| Model | Field | What it stores |
|---|---|---|
| `licenses.License` | `product_key` | Software license keys |
| `core.EmailSettings` | `smtp_password` | Outbound SMTP credentials |
| `extras.WebhookEndpoint` | `secret` | Webhook signing secrets |

The command reads every row (including soft-deleted rows, across all tenants) for the three fields listed above, decrypts each value with the full keyring, re-encrypts with the current primary key, and saves the new ciphertext via `update()` (bypassing model `save()` to avoid double-encryption). It does **not** include the durable `WebhookDelivery.target_secret` snapshot used for delivery retries. Keep the old key configured while any such retained snapshot or backup may need it; this command does not re-encrypt those snapshots.

**Options**

| Option | Purpose |
|---|---|
| `--dry-run` | Simulate the rotation — report what would change without writing to the database. |

**Examples**

```bash
# Preview what will be rotated
python manage.py rotate_encryption_keys --dry-run

# Execute the rotation
python manage.py rotate_encryption_keys
```

!!! danger "Back up before rotating; do not drop the old key"
    Configure the new key first (`NEW,OLD`), restart all app/worker processes, run `--dry-run`, then execute the rotation and verify decryption. The first key encrypts. The command's inventory is limited to the three fields above: retained webhook-delivery snapshots are encrypted separately and are not re-encrypted here. Keep the old key for those snapshots and any backups that may be restored; command completion alone does not make removing it safe.

!!! warning "Plaintext fallback adoption"
    If a field contains a value that is **not** `enc$`-prefixed (a plaintext fallback from before encryption was enabled), the command treats it as cleartext and encrypts it under the current key. This is intentional: it brings legacy plaintext values into the encryption regime. Review any such rows in the output to ensure they are expected.

---

### `run_jobs`

This command attempts to process pending background jobs from the database job queue (`core.Job` model). It is distinct from the django-q2 task cluster. **Do not use it for production recovery or to drain the queue:** the current implementation may mark a job successful even when no handler ran.

| | |
|---|---|
| **Usage** | `python manage.py run_jobs` |
| **Production-safe** | No — unsafe for queue recovery while the missing-handler behaviour remains. |
| **When to use** | None recommended until the missing-handler behaviour is fixed and verified. |

**Options**

This command takes no arguments or options.

!!! danger "Known unsafe behaviour — not fixed by this documentation"
    The command looks for `_run_<job_name>` on the command class (normalising `:` and spaces to underscores and lowercasing). If no handler exists, `_execute_job()` does nothing, but the caller still marks the job completed with a success result. Do not invoke this command to recover pending work or treat its output as proof of execution. Reconcile affected work using the owning feature's documented recovery procedure; there is no safe generic dispatch recommendation here until the application behaviour is fixed and verified. This warning documents the current behaviour and does not claim the application defect has been fixed.

---

### `sync_tenant_ldap`

Synchronise users from an LDAP directory into a specific tenant. Creates or updates local Django `User` records, establishes `Membership` entries, and grants a default "Member" role.

| | |
|---|---|
| **Usage** | `python manage.py sync_tenant_ldap --tenant SLUG` |
| **Production-safe** | Yes (creates and updates users; does not delete existing users) |
| **When to use** | Initial bulk import of LDAP users into a tenant, periodic synchronisation to pick up new directory entries, or testing LDAP configuration before enabling real-time authentication. |

**Prerequisites**

- Use the locked Linux/WSL development environment or the Docker image; native Windows intentionally excludes `django-auth-ldap` because `python-ldap` has no supported Windows wheel
- `ITAMBOX_TENANT_LDAP_CONFIGS` must contain an entry for the target tenant slug with at minimum `SERVER_URI`, `BIND_DN`, `BIND_PASSWORD`, and `USER_SEARCH_BASE`

**Options**

| Option | Required | Purpose |
|---|---|---|
| `--tenant SLUG` | **Yes** | Slug of the tenant to sync users for. |

**Examples**

```bash
# Sync LDAP users into the "acme-corp" tenant
python manage.py sync_tenant_ldap --tenant acme-corp
```

!!! note "Group filtering"
    If the LDAP configuration includes a `REQUIRE_GROUP` entry, only users whose `memberOf` attribute contains that group DN are synchronised. Omit `REQUIRE_GROUP` to sync all users matching the search filter.

!!! note "Role grants are time-limited for privileged roles"
    Users synchronised via LDAP receive a "Member" role with `SCOPE_OWN` by default. If the role is classified as privileged (requiring MFA), the grant carries a 24-hour lifetime. Manual role grants from the admin UI are never overwritten by the sync.

---

### `validate_role_permissions`

Scan every `Role.permissions` JSON field across all tenants and flag any permission codename that does not match a real `auth.Permission` in the database.

| | |
|---|---|
| **Usage** | `python manage.py validate_role_permissions` |
| **Production-safe** | Yes (read-only) |
| **When to use** | After a migration that renames or removes models, after cleaning up stale permissions, or as a periodic audit to catch typos in permission codenames. |

**Options**

This command takes no arguments or options. It exits with a non-zero status when stale codenames are found (suitable for CI gates).

**Examples**

```bash
# Audit all roles
python manage.py validate_role_permissions

# Use in CI
python manage.py validate_role_permissions || echo "Stale permissions found — review and fix"
```

!!! note "JSONField, no FK integrity"
    `Role.permissions` is a JSONField of `app_label.codename` strings. Unlike a many-to-many through table, there is no database-enforced foreign key to `auth.Permission`. A typo, a renamed model, or a removed permission silently persists as a dead entry that grants no actual access. This command is the safety net.

---

## Commands Documented Elsewhere

The following commands have dedicated documentation pages or are covered in detail in other sections:

| Command | Documented in |
|---|---|
| `import_snipeit` | [Migrate from Snipe-IT](../integration/migrate-from-snipe-it.md) |
| `sync_intune` | [Discovery & Sync](../integration/discovery-sync.md) |
| `prune_changelog` | [Data Retention](data-retention.md) |
| `seed_data` | [Installation](installation.md) — `python manage.py seed_data`, development/demo only; **never run against production** |
| `bind_oidc_identity` | [Upgrades](upgrades.md) — binds an external identity to an existing local user |
| `eventrule_withdrawn_report` | [Webhooks and automation](../usage/webhooks-and-automation.md) — reports active event rules whose conditions are withdrawn for 1.0 |
| `plugins` | [Plugin runbook](plugin-runbook.md) — plugin inventory, plus `--check` for a startup pre-flight |
| `restore_resource_grant` | [Resource-grant expiry](resource-grant-expiry.md) — restores one revoked grant with a corrected or cleared deadline |

---

## Scheduled Commands

The following tasks run automatically via django-q2 `Schedule` entries registered at application startup (`post_migrate` signal). No manual cron configuration is required when using the standard `qcluster` deployment.

| Schedule | Function | Frequency | Registered by |
|---|---|---|---|
| **Daily Alert Rule Evaluation** | `extras.tasks.alerts.evaluate_alert_rules_task` | Daily | `ExtrasConfig._register_alert_schedule` |
| **Daily Changelog & Operational-Data Retention Prune** | `core.tasks.prune_changelog_task` | Daily | `CoreConfig._register_prune_schedule` |
| **Daily Subscription Expiries and Reminders** | `subscriptions.tasks.check_subscription_expiries_and_reminders` | Daily | `SubscriptionsConfig._register_subscription_tasks` |

All schedule registrations are idempotent: they use `core.schedules.register_schedule`, which collapses repeated calls into a single `Schedule` row per function. It is safe to run `migrate` or restart the application multiple times — duplicate schedules are never created.

!!! note "Out-of-band cron fallback"
    If you run background jobs outside the django-q2 cluster (or want a safety net), every scheduled command can also be invoked directly from system cron. The underlying management commands are safe to run ad hoc. See [Data Retention](data-retention.md#scheduling) for a cron example.

### Schedule verification

To verify the schedules are registered, query the django-q2 `Schedule` table:

```bash
python manage.py shell -c "
from django_q.models import Schedule
for s in Schedule.objects.all():
    print(f'{s.name}: {s.func} (next: {s.next_run})')
"
```
