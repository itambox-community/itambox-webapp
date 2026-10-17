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

---

## What Can Plugins Do?

The supported ITAMbox-owned surface is intentionally small:

- **Plugin metadata and settings** through the documented `PluginConfig` attributes.
- **UI routes** through the optional package `urls.py` URLconf mounted below the plugin base URL.
- **REST viewsets, menus, menu items, and template content** through the four documented registry extension points.
- **GraphQL** through `PluginConfig.graphql_schema`.
- **Middleware and auxiliary Django apps** through the documented configuration attributes.

A plugin may contain other Django/Python code, including models, but those internals are not additional ITAMbox API guarantees. The package layout below is illustrative; only the symbols and paths named in the [API Reference](api_reference.md) are supported.

```text
itambox_esign/
├── __init__.py           # Declares PluginConfig class
├── urls.py               # Web UI routes (optional)
├── navigation.py         # Sidebar menu registration (optional)
├── template_content.py   # Page template injections (optional)
├── models.py             # Database models (optional)
├── graphql/
│   ├── __init__.py
│   └── schema.py         # GraphQL schema extension (optional)
└── api/
    ├── __init__.py
    └── views.py          # REST API viewsets (optional)
```

---

## 1. Creating the PluginConfig

The entry point of any plugin is its `__init__.py` which must subclass `PluginConfig` from `itambox.plugins`.

Create `itambox_esign/__init__.py`:

```python
from itambox.plugins import PluginConfig

class EsignPluginConfig(PluginConfig):
    name = 'itambox_esign'
    verbose_name = 'DocuSign Integration'
    version = '1.0.0'
    author = 'DocuSign Dev Team'
    author_email = 'dev@docusign.com'
    description = 'DocuSign envelopes for assets'
    itambox_range = '>=1.1,<1.2'  # supported ITAMbox major.minor range

    settings_schema = {
        'DOCUSIGN_API_KEY': {'type': 'str', 'required': True, 'secret': True},
        'DOCUSIGN_SANDBOX': {'type': 'bool', 'default': True},
    }

    def ready(self):
        super().ready()
        # Resources (template extensions, menu, viewsets, GraphQL) are declared
        # on the config or in conventional modules; the host registers them.

config = EsignPluginConfig
```

---

## 2. Registering the Plugin

To enable your plugin, add its package name to the `ITAMBOX_PLUGINS` environment
variable (comma-separated). Product source is never edited:

```bash
ITAMBOX_PLUGINS=itambox_esign
```

Operator settings are validated against your `settings_schema`. Supply them as JSON in
`ITAMBOX_PLUGINS_CONFIG`, and inject secrets as single values with
`ITAMBOX_PLUGIN_<NAME>_<KEY>`, which wins over the JSON:

```bash
ITAMBOX_PLUGINS_CONFIG='{"itambox_esign": {"DOCUSIGN_SANDBOX": false}}'
ITAMBOX_PLUGIN_ITAMBOX_ESIGN_DOCUSIGN_API_KEY=your-api-key-here
```

Read the validated values at runtime with `itambox.plugins.get_plugin_config('itambox_esign', 'DOCUSIGN_SANDBOX')`. See [Configuration](api_reference.md#configuration).

## Trust and failure behavior

Plugins are trusted, unsandboxed, in-process Django code. They have the same
Python process and database privileges as ITAMbox and may inject middleware.
Installing or enabling a plugin is therefore equivalent to installing trusted
Python code. There is no capability sandbox, signature verification, or
per-tenant activation.

Activation happens only at startup. If a configured plugin is missing,
incompatible, malformed, or raises while composing its hooks, ITAMbox disables
that plugin, keeps Stable core and other valid plugins running, and publishes a
redacted diagnostic in the UI and through `python manage.py plugins`. A failed
plugin contributes no routes, middleware, REST router, GraphQL schema, menu, or
template hook.

"Disabled" has a precise meaning. A plugin that fails import, configuration or compatibility checks is skipped before Django app population: its `django_apps`, its middleware and its `PluginConfig` are not added to the running application, so it loads no models, no migrations and no app-level signal receivers. Its package `__init__.py` is still imported during that check, so module-level code in it runs. A plugin that fails in `ready()` is different: that failure happens after app population, so models or signal receivers that were registered before the exception may remain loaded. The diagnostic in the UI and `python manage.py plugins` tells you which stage failed.

Removal and reinstall are manual operations. Removing a name from
`PLUGINS`/`ITAMBOX_PLUGINS` does not remove database tables, ContentTypes,
changelog rows, or referencing configuration. ITAMbox 1.0 deliberately does
not automate orphan-data cleanup. Follow the [plugin removal and recovery
runbook](../operations/plugin-runbook.md).
