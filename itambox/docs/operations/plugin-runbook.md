# Plugin Removal and Recovery Runbook

Plugins are Experimental, trusted, in-process code. This runbook is for a deployment operator. ITAMbox 1.0 does not provide install, uninstall, upgrade, runtime enable/disable, package-management, or automatic orphan-data cleanup orchestration.

## Recover from an aborted startup

Plugin startup is fail-fast. If a configured plugin cannot be imported, has an invalid manifest or configuration, or fails while its resources are composed or assembled (including an invalid GraphQL type, a URLconf error, or an exception in its `ready()`), the process does not start. The only exception is a version incompatibility: that plugin is skipped with a warning and everything else starts normally.

**The whole recovery path runs outside the web process.** Every step below uses the container log and a one-off management command in the same image. None of it needs the web application, `/health/`, the UI, or the API to be up. A plugin problem can therefore always be diagnosed and fixed while the app is down.

### 1. Locate and read the startup log

The application logs to stdout/stderr, so the container runtime holds the startup log. The error names the plugin, the phase (resolve, compose, ready, assemble), and the cause.

```bash
docker compose logs --tail=200 app worker
```

Without Compose, use the orchestrator's equivalent (`docker logs <container>`, `kubectl logs <pod> --previous`, `journalctl -u <unit>`). If the container restarts in a loop, read the logs of the most recent failed start. Note the plugin name and phase, and do not paste secret values into a ticket.

### 2. Run the pre-flight in the same image and target version

Run `--check` in the **same image tag** that failed to start, so the check resolves plugins against the same product version and the same installed plugin packages. A one-off container does not depend on the stopped web service:

```bash
docker compose run --rm app python manage.py plugins --check
echo "exit code: $?"
```

Outside Docker, run it in the same environment and release as the failing process:

```bash
uv run --locked --no-dev python manage.py plugins --check
```

`--check` is handled before Django loads, so it works even when the settings import itself aborts. It starts the real startup path (resolve, compose, `ready()`, assemble) in a child interpreter, once per configured plugin and once for the whole set, without serving requests, and reports one outcome per plugin plus pending plugin migrations. Add `--format json` for machine-readable output.

| Exit code | Meaning |
| --- | --- |
| `0` | No configured plugin would abort startup. Plugins are `ok` or `skipped`. |
| `1` | At least one plugin, or the combination of plugins, would abort startup. The same condition makes the real start fail. |

Pending migrations are reported, but they do not by themselves make the exit code non-zero. When the database is unreachable they are reported as `unknown (database unavailable)`.

### 3. Interpret the output

| Outcome | Meaning | Effect on startup |
| --- | --- | --- |
| `ok` | Plugin resolved, composed, and assembled. | Starts. |
| `skipped` | Version incompatibility. Output shows the declared range and the running ITAMbox version. | Starts without the plugin, with a warning. |
| `would abort startup` on an extra row (`(plugin set)` or the named plugin) | Every plugin passes on its own, but the combination fails (for example two plugins contribute a conflicting URL or GraphQL name). | Startup aborts. Remove one of the conflicting plugins. |
| `would abort startup` | Failure with plugin name, phase, and cause (for example missing package, import failure, invalid manifest, invalid or missing configuration, compose or assemble error, `ready()` exception). | Startup aborts. |

Pending migrations list plugin migrations that are not yet applied. Plugin migrations are owned by the plugin and are outside the core migration audit. Apply them as part of the planned upgrade, after the plugin set is fixed:

```bash
docker compose run --rm app python manage.py migrate
```

### 4. Fix it

Pick the row that matches the `--check` result. Change one thing at a time, then repeat step 2 until the exit code is `0`.

| `--check` result | Action |
| --- | --- |
| `would abort startup`: package missing or import failure | Install the plugin package into the image (plugins are installed at image build time, not at runtime), or correct the name in `ITAMBOX_PLUGINS`. Rebuild or pull the image, then re-run `--check`. |
| `would abort startup`: invalid manifest, or invalid/missing/unknown configuration | Fix `ITAMBOX_PLUGINS_CONFIG` (JSON object keyed by plugin) or the per-key secret environment override for the named key. Re-run `--check`. |
| `would abort startup`: compose or assemble error, or `ready()` exception | A plugin defect. Install a compatible plugin release that fixes it, or remove the plugin (next row) until one exists. |
| `skipped`: version incompatibility | Install a plugin release whose declared range includes the running ITAMbox version, or accept the skip. This does not block startup. |
| Cannot be fixed now, or the root cause is unknown | Remove the plugin from `ITAMBOX_PLUGINS` (step 5), restart, and investigate in staging. |
| Pending plugin migrations only | Not a startup blocker. Apply with `manage.py migrate` during the planned change. |

### 5. Remove the plugin from `ITAMBOX_PLUGINS`

This is the guaranteed way back to a running system. Edit the deployment configuration (for example `.env`) and delete the plugin's package name from the comma-separated list:

```bash
# before
ITAMBOX_PLUGINS=plugin_a,plugin_b
# after: plugin_b removed
ITAMBOX_PLUGINS=plugin_a
```

Then verify before and after switching over:

```bash
docker compose run --rm app python manage.py plugins --check   # expect exit code 0
docker compose up -d
docker compose logs --tail=50 app
```

Removing a plugin does not delete its tables, `ContentType` rows, or changelog rows. See [Disable or remove a plugin](#disable-or-remove-a-plugin).

## Check the current state

1. Record the deployed ITAMbox revision and the plugin package revision.
2. Run:

   ```bash
   uv run --locked --no-dev python manage.py plugins
   uv run --locked --no-dev python manage.py plugins --format json
   ```

   Each configured plugin is listed with its manifest version, declared
   `itambox_range`, state (`active` or `skipped`), and pending migrations. A
   `skipped` row also names the failure class, the stage and the compatibility
   outcome. The command also lists orphaned data (tables, content types and
   migration records no installed app owns), read-only. Error text is redacted
   and suitable for an operator report. Never copy secrets into an incident
   ticket.
3. `/health/` reports `checks.plugins` as `ok`, or `skipped` when a plugin was
   skipped for a version incompatibility. It is informational and never fails
   the probe: a process that answers has a consistent plugin set.

### Read plugin diagnostics in the application

The plugin diagnostics notice and the read-only API inventory are available to
authenticated users holding `core.view_plugin_diagnostics` in their active
tenant. Superusers retain access automatically. To grant access, create or edit
a role in **Organization > Roles & Permissions**, select **Can view plugin
diagnostics** under custom permissions, save the role, and assign it to the
intended operator.

`GET /api/plugins/` returns the configured plugins' manifest versions and
compatibility ranges, active or skipped state, the running ITAMbox version,
contributed resources by extension point, and applied and pending migrations.
A user without the permission receives HTTP 403; other methods are not
available. The endpoint is read-only and does not include plugin settings or
configuration values. If the database is unavailable, migration fields are
`null` rather than an empty list.

This permission controls the notice and the read-only inventory only; plugin
activation and recovery remain deployment operations. A plugin that cannot be
imported, is malformed, or raises while composing aborts startup with a message
naming the plugin and the phase; remove it from `ITAMBOX_PLUGINS` and restart to
recover. The one skip case is a version incompatibility: a plugin whose
`itambox_range` excludes the running release is skipped with a warning and shown
in `manage.py plugins`.

## Disable or remove a plugin

1. Remove the package name from `settings.PLUGINS` or `ITAMBOX_PLUGINS` in the
   deployment configuration. This is the only 1.0 activation control.
2. Keep the package revision and configuration under version control until the
   retention/reinstall decision is complete. Do not attempt a runtime toggle.
3. Restart the application using the deployment's normal, reversible restart
   procedure. Verify `manage.py plugins` reports the plugin as not configured.
4. Preserve the plugin's database tables, Django `ContentType` rows, changelog
   rows, and referencing configuration. Removing it from `PLUGINS` does **not**
   remove any of these. This orphan-data behavior is intentional in 1.0 so that
   a later reinstall can recover the data.

There is no supported automatic cleanup command. If permanent deletion is
approved later, take a database backup, identify every plugin-owned table,
ContentType, changelog/reference row, and configuration entry, and perform the
cleanup as a separately reviewed migration or database operation. Do not infer
ownership from table names alone, and do not run destructive SQL as part of
normal plugin removal.

## Recover a failed plugin

1. Keep the failing plugin out of `PLUGINS` while investigating.
2. Compare the plugin's declared `itambox_range` with the deployed ITAMbox
   release.
3. Fix the package, configuration, middleware, URL, or schema error in a
   development/staging deployment. Pin the tested plugin revision.
4. Re-add the package name to `PLUGINS` only during the planned deployment
   change. Run the pre-flight before the restart, then restart and verify:

   ```bash
   uv run --locked --no-dev python manage.py plugins --check   # before restart, expect exit code 0
   uv run --locked --no-dev python manage.py plugins
   uv run --locked --no-dev python manage.py check
   ```

5. Exercise the plugin's supported routes and extension points, plus the Stable
   core smoke/test subset. A plugin that fails again aborts startup again; remove it
   from `PLUGINS` to restore service.

## Migrate API imports in extensions

The `itambox.api` package no longer re-exports API classes or helpers. Plugins
using imports such as `from itambox.api import BaseModelSerializer` must import
from the concrete module instead:

```python
from itambox.api.base import BaseModelSerializer, ValidatedModelSerializer
from itambox.api.fields import ChoiceField, ContentTypeField, RelatedObjectCountField, SerializedPKRelatedField
from itambox.api.gfk_fields import GFKSerializerField
from itambox.api.mixins import BulkDestroyModelMixin, BulkUpdateModelMixin, ETagMixin, ObjectValidationMixin
from itambox.api.pagination import ITAMBoxPagination
from itambox.api.routers import ITAMBoxRouter
from itambox.api.utils import get_serializer_for_model, get_view_name
from itambox.api.viewsets import BaseViewSet, ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet
```

Import only the names the extension uses. Update any dotted configuration paths
to these concrete modules as well. Test the updated plugin in staging before
re-enabling it; the package-level imports are not a compatibility fallback.
This migration does not expand the [documented plugin API](../plugins/api_reference.md);
API infrastructure outside that inventory remains private/unstable.

## Security boundary

A plugin has the same process privileges, database access, filesystem access,
and ability to add middleware as the host application. There is no sandbox,
capability restriction, signature verification, or tenant-level activation.
Installing or enabling one is equivalent to installing trusted Python code.
Only deployment operators may change `PLUGINS`; ordinary tenant users cannot
activate, disable, or clear these diagnostics.
