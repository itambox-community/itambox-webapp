# Plugin Removal and Recovery Runbook

Plugins are Experimental, trusted, in-process code. This runbook is for a deployment operator. ITAMbox 1.0 does not provide install, uninstall, upgrade, runtime enable/disable, package-management, or automatic orphan-data cleanup orchestration.

## Check the current state

1. Record the deployed ITAMbox revision and the plugin package revision.
2. Run:

   ```bash
   uv run --locked --no-dev python manage.py plugins
   uv run --locked --no-dev python manage.py plugins --format json
   ```

   A failed plugin row identifies the package, failure class, startup stage,
   compatibility outcome, activation state, `settings.PLUGINS` as the source,
   and whether a configured value was present. Error text is redacted and
   suitable for an operator report. Never copy secrets into an incident ticket.
3. Confirm that the application is serving Stable core and that the failed
   plugin's routes, middleware, API router, GraphQL contribution, menu, and
   template hooks are absent.

A plugin that cannot be imported, is malformed, or raises while composing aborts startup with a message naming the plugin and the phase; remove it from `ITAMBOX_PLUGINS` and restart to recover. The one skip case is a version incompatibility: a plugin whose `itambox_range` excludes the running release is skipped with a warning and shown in `manage.py plugins`.

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
   change, restart, and verify:

   ```bash
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
