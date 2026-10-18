# Plugin Migrations

This page describes how database migrations for Experimental plugins relate to
ITAMbox core migrations. Each rule names the code that implements it, so you can
check the claim yourself. Rules marked **policy** are not enforced by an automated
gate; they are conditions a plugin must meet and that reviewers check by hand.

Plugins are Experimental and outside the Stable 1.0 compatibility promise. Read
the [plugin getting started guide](getting_started.md) and the
[API reference](api_reference.md) first.

## Ownership

Plugin models live in the plugin's own Django app. Its migrations are ordinary
Django migrations owned by the plugin, stored in the plugin package, and applied by
the normal `manage.py migrate` run.

- The plugin's apps enter `INSTALLED_APPS` during settings load, before Django
  populates the app registry. `load_plugins()` in `itambox.plugins.utils` merges
  them into the running settings with
  `_append_unique(settings_module.INSTALLED_APPS, plugin_apps)`, where
  `_resolve_plugin` has built the label list from `PluginConfig.django_apps` plus
  the app of the `PluginConfig` subclass.
- A plugin that fails before app population adds no app, models, or migrations
  (see the "Disabled" paragraph in `getting_started.md`).
- Plugin apps can also declare extra Django apps in `PluginConfig.django_apps`
  (Experimental; declared on `PluginConfig` in `itambox.plugins`). Those labels
  count as plugin-owned for the pending-migration report described below.

## Dependencies between core and plugin migrations

- **Core must not depend on plugin migrations.** Core migrations never reference a
  plugin app. **Policy.**
- **A plugin may depend on core migrations, but only up to the latest migration of
  the product range it declares.** The range is `PluginConfig.itambox_range`,
  written as `>=<major.minor>,<<major.minor>` (for example `>=1.1,<1.2`), parsed
  by `parse_itambox_range` and compared with the running version's major.minor
  pair by `_check_version_range` in `itambox.plugins.utils`; the patch level is
  ignored. Outside the range the plugin is skipped before it enters
  `INSTALLED_APPS`. The check covers the product version only, not individual
  migration names, so a dependency on a core migration from a release outside the
  declared range goes undetected. Keep core dependencies inside the declared
  range: that part is **policy**, and no gate verifies it.
- **No data migrations on core tables.** A plugin migration must not read or write
  rows in core tables. **Policy.** No gate detects this today.
- **No foreign key from core to plugin tables.** The direction of every foreign key
  is plugin to core, never core to plugin. Core code therefore cannot reach a
  plugin table through a relation, and removing a plugin cannot break a core
  migration. **Policy.**

## Core migration audit

The core migration audit does not cover plugin migrations.

- `scripts/migration_audit.py` scans only app directories directly below
  `itambox/` in the product source tree
  (`source_root.glob("*/migrations/[0-9]*.py")`, with `source_root` defaulting to
  `repository_root / "itambox"`). Installed plugin packages live outside that
  tree. A plugin package placed *inside* `itambox/` would be scanned, so plugin
  packages are not shipped there.
- The migration baseline preflight (`itambox/core/migration_preflight.py`) reads
  the core manifest `migration_baseline_manifest.json` from its own directory
  (`MANIFEST_PATH = Path(__file__).with_name(...)`). It lists core migration IDs
  and has no plugin-app handling.

Plugin migrations therefore need their own review. Keep them free of core-table
writes, as described above.

## Applied and pending plugin migrations

Two commands report plugin migration state. Both are read-only.

- `manage.py plugins` (the Django command in
  `itambox/core/management/commands/plugins.py`) prints one row per configured
  plugin with the columns `PLUGIN`, `STATE`, `VERSION`, `RANGE`,
  `PENDING MIGRATIONS` and `DETAILS`. The `PENDING MIGRATIONS` cell holds the
  number of plugin migrations that are not yet applied, or `unknown` when the
  database is unreachable. `--format json` emits the same inventory, including
  the full `pending_migrations` list per plugin, plus the startup diagnostics and
  the orphaned-data report.
- `manage.py plugins --check` is intercepted in `manage.py` before Django loads,
  so it also works when a broken plugin makes the settings import abort. It runs
  the start-up path for each configured plugin in a child process, without serving
  requests, and prints a report ending in `Pending plugin migrations:` followed by
  the pending list, `none`, or `unknown (database unavailable)`, plus
  `Startup would succeed.` or `Startup would ABORT.`

Both paths read the same diagnostics: `plugin_app_labels` and
`pending_migrations` in `itambox.plugins.diagnostics` (the latter builds the
migration plan with `MigrationExecutor.migration_plan(loader.graph.leaf_nodes())`
and keeps the entries whose app label belongs to a plugin). The pre-flight report
is produced by `run_preflight` and `format_report` in
`itambox.plugins.preflight`.

The report shows **pending** migrations only. It does not list applied ones. To see
the applied state of a plugin app, use `manage.py showmigrations <app_label>`.

Pending migrations do not change the exit code of `plugins --check`; only a
startup abort does (`report["abort"]`, which the command turns into
`CommandError(..., returncode=1)`). Apply pending plugin migrations with
`manage.py migrate` as part of the planned upgrade, after the plugin set is fixed.
The [plugin removal and recovery runbook](../operations/plugin-runbook.md)
describes this order.

## Models and API boundary

- **`PluginModel` is private.** It is an abstract base class in
  `itambox.plugins.models` and is listed as Private in the
  [API reference](api_reference.md). It may change without notice.
- **`PluginModel` does not make a model tenant-scoped by itself.** It extends
  `core.models.StandardModel`, which supplies change logging, journaling, tagging,
  export and clone behaviour plus the `BaseModel` timestamps. It adds no tenant
  field.
- **Plugin models that store tenant data use the core scoping shape.** Declare the
  `tenant` foreign key to `organization.Tenant` and assign a core scoping manager
  from `core.managers` (`TenantScopingManager`, `TenantScopingSoftDeleteManager`
  or the explicit-scope variants) as the default manager, the way core models do
  (see `assets.models.asset`). Querysets then narrow to the active tenant and
  scope exactly as core models do. **Policy** for a plugin model that keeps tenant
  data without these pieces.
- **Plugin models are not covered by the API.** Plugin REST resources are the
  supported write surface only when declared through the documented viewsets, which
  enforce core tenant scope after `get_queryset()` (see the `viewsets` row in the
  API reference). Models are not exposed automatically, `PluginModel` itself is
  not part of the API contract, and migration automation for plugins sits with the
  rest of package management, which the API reference lists as private/unstable.

## Implementation map

| Rule | Where it is implemented |
|---|---|
| Plugin app added to `INSTALLED_APPS` only when the plugin loads | `itambox.plugins.utils` (`load_plugins`, `_resolve_plugin`) |
| Version range against the product version | `itambox.plugins.utils` (`parse_itambox_range`, `_check_version_range`), `PluginConfig.itambox_range` in `itambox.plugins` |
| Plugin app labels and declared `django_apps` | `itambox.plugins.diagnostics` (`plugin_app_labels`) |
| Pending plugin migrations (read-only) | `itambox.plugins.diagnostics` (`pending_migrations`) |
| `plugins` inventory table and `plugins --check` | `itambox/core/management/commands/plugins.py`, `itambox.plugins.preflight`, interception in `manage.py` |
| Core audit scope excludes plugin packages | `scripts/migration_audit.py` (`*/migrations/` below `itambox/`) |
| Core manifest ignores plugin apps | `itambox/core/migration_preflight.py` (`MANIFEST_PATH`) |
| `PluginModel` private and not in the API | `itambox.plugins.models`, `itambox/docs/plugins/api_reference.md` |
| Tenant scoping and change history for plugin models | `core.models` (`StandardModel`), `core.managers` (scoping managers) |
| Core never depends on plugin migrations; no core-table data migrations; no core-to-plugin foreign key | **Policy**, reviewed by hand |
| No dependency on core migrations past the declared range | **Policy**, reviewed by hand |

## Checklist for a plugin with migrations

1. Put every model in the plugin's own app and generate its migrations there.
2. Declare the product range in `itambox_range` and test against that version.
3. Keep foreign keys pointing from plugin to core only.
4. Do not write to core tables in any migration.
5. Run `manage.py plugins --check` and confirm the pending list before upgrading.
6. Run `manage.py migrate`, then `manage.py plugins` to confirm that nothing is pending.
