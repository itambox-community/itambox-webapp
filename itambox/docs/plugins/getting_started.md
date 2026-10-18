# Getting Started with ITAMbox Plugins

!!! warning "Trust model: plugins are trusted, unsandboxed application code"
    Plugins are **Experimental**, opt-in, and run as **trusted, unsandboxed,
    in-process Python/Django code** in the application process. Enabling a
    plugin is equivalent to installing trusted application code: it can access
    the application process, the database, stored secrets, and any network
    reachable from the host. Only install plugins from sources you trust, and
    pin both ITAMbox and the plugin revision together.

    Compatibility must be checked **before** deployment: verify the plugin's
    declared `itambox_range` against the
    deployed ITAMbox revision, and test the combination in a non-production
    environment first. Plugins are not covered by the Stable 1.0 compatibility
    promise; Experimental interfaces may change in any release.

    Installation, upgrade, disablement, and removal are **operator
    responsibilities**: activation happens through `ITAMBOX_PLUGINS` (see the
    [plugin removal and recovery runbook](../operations/plugin-runbook.md)).
    Removal does **not** clean up automatically — a plugin's database tables,
    Django `ContentType` rows, changelog rows, and configuration entries remain
    after the package is removed from `ITAMBOX_PLUGINS`.

ITAMbox has a bounded Experimental plugin system. Plugins are ordinary Django apps that the host loads at startup and composes through a small set of documented extension points; the API is kept deliberately small so that it can be versioned and kept compatible. Read the [API Reference](api_reference.md) before writing a plugin. Anything not listed there is private/unstable.

For how plugin database migrations relate to core migrations, and how to check pending plugin migrations, see [Plugin Migrations](migrations.md).

---

## What Can Plugins Do?

The supported ITAMbox-owned surface is intentionally small:

- **Plugin settings** use `PluginConfig.settings_schema`, operator JSON in `ITAMBOX_PLUGINS_CONFIG`, and per-key secret overrides in `ITAMBOX_PLUGIN_<NAME>_<KEY>`.
- **REST resources** are declared through `PluginConfig.viewsets` (or the conventional `api/viewsets.py` module) and must use the host-owned ITAMbox viewset bases.
- **Model permission targets** are declared through `PluginConfig.permission_targets` (or the conventional `permissions.py` module) and must name models owned by that plugin.
- **UI routes** use the optional package `urls.py` URLconf mounted below the plugin base URL.
- **Menus, menu items, and template content** use the documented `PluginConfig` attributes or conventional modules.
- **GraphQL** uses `PluginConfig.graphql_schema` for query fields only; plugin mutation contributions are rejected.
- **Middleware and auxiliary Django apps** use the documented configuration attributes.

A plugin may contain other Django/Python code, including models, but those internals are not additional ITAMbox API guarantees. The package layout below is illustrative; only the symbols and paths named in the [API Reference](api_reference.md) are supported.

```text
example_plugin/
├── __init__.py           # Declares PluginConfig class
├── urls.py               # Web UI routes (optional)
├── navigation.py         # Sidebar menu registration (optional)
├── template_content.py   # Page template injections (optional)
├── models.py             # Database models (optional)
├── graphql/
│   ├── __init__.py
│   └── schema.py         # GraphQL schema extension (optional)
├── api/
│   ├── __init__.py
│   ├── viewsets.py       # viewsets declaration (optional)
│   └── views.py          # REST viewset classes (optional)
└── permissions.py       # targets declaration (optional)
```

---

## 1. Creating the PluginConfig

The entry point of any plugin is its `__init__.py` which must subclass `PluginConfig` from `itambox.plugins`.

Create `example_plugin/__init__.py`:

```python
from itambox.plugins import PluginConfig

class ExamplePluginConfig(PluginConfig):
    name = 'example_plugin'
    verbose_name = 'Example Integration'
    version = '1.0.0'
    author = 'Example Dev Team'
    author_email = 'dev@example.test'
    description = 'Example integration for assets'
    itambox_range = '>=1.0,<1.1'  # example range for the current 1.0 line

    settings_schema = {
        'api_key': {'type': 'str', 'required': True, 'secret': True},
        'sandbox': {'type': 'bool', 'default': True},
    }
    viewsets = 'example_plugin.api.viewsets.viewsets'
    permission_targets = 'example_plugin.permissions.targets'

    def ready(self):
        super().ready()
        # The host validates and registers declared resources before ready().

config = ExamplePluginConfig
```

---

## 2. Registering the Plugin

To enable your plugin, add its package name to the `ITAMBOX_PLUGINS` environment
variable (comma-separated). Product source is never edited:

```bash
ITAMBOX_PLUGINS=example_plugin
```

Operator settings are validated against your `settings_schema`. Supply them as JSON in
`ITAMBOX_PLUGINS_CONFIG`, and inject secrets as single values with
`ITAMBOX_PLUGIN_<NAME>_<KEY>`, which wins over the JSON:

```bash
ITAMBOX_PLUGINS_CONFIG='{"example_plugin": {"sandbox": false}}'
ITAMBOX_PLUGIN_EXAMPLE_PLUGIN_API_KEY=your-api-key-here
```

Read the validated values at runtime with `itambox.plugins.get_plugin_config('example_plugin', 'sandbox')`. See [Configuration](api_reference.md#configuration).

## Trust and failure behavior

Plugins are trusted, unsandboxed, in-process Django code. They have the same
Python process and database privileges as ITAMbox and may inject middleware.
Installing or enabling a plugin is therefore equivalent to installing trusted
Python code. There is no capability sandbox, signature verification, or
per-tenant activation.

Activation happens only at startup. A plugin whose declared `itambox_range`
excludes the running ITAMbox release is skipped with a warning; the application
starts without that plugin. A missing package, import or manifest error, invalid
configuration, or failure during composition, `ready()`, or assembly aborts
startup; ITAMbox does not keep serving core with the failed plugin silently
disabled.

Before restarting, run `python manage.py plugins --check` in the same image and
configuration. It runs the startup preflight in a child process and exits nonzero
when the application would abort. The ordinary `python manage.py plugins`
inventory is available when the application settings load successfully. See
the [plugin removal and recovery runbook](../operations/plugin-runbook.md).

"Skipped" specifically means version incompatibility: the plugin is not
activated or added to `INSTALLED_APPS`, and its declared resources are not
registered. Other startup failures abort the process. A failure in `ready()`
happens after Django app population, so it is not a safe plugin-disable or
rollback mechanism; use the preflight and recovery procedure instead.

Verify a plugin against a real start with the [test helper](testing.md).

Removal and reinstall are manual operations. Removing a name from
`PLUGINS`/`ITAMBOX_PLUGINS` does not remove database tables, ContentTypes,
changelog rows, or referencing configuration. ITAMbox 1.0 deliberately does
not automate orphan-data cleanup. Follow the [plugin removal and recovery
runbook](../operations/plugin-runbook.md).
