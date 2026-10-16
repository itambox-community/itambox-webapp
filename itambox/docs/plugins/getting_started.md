# Getting Started with ITAMbox Plugins

!!! warning "Trust model: plugins are trusted, unsandboxed application code"
    Plugins are **Experimental**, opt-in, and run as **trusted, unsandboxed,
    in-process Python/Django code** in the application process. Enabling a
    plugin is equivalent to installing trusted application code: it can access
    the application process, the database, stored secrets, and any network
    reachable from the host. Only install plugins from sources you trust, and
    pin both ITAMbox and the plugin revision together.

    Compatibility must be checked **before** deployment: verify the plugin's
    declared `min_version`/`max_version` and plugin API version against the
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
    min_version = '1.0.0-alpha'  # Version constraint checks
    max_version = '1.99.99'      # Product version, not plugin API version
    min_plugin_api_version = '1.0'
    max_plugin_api_version = '1.0'
    graphql_schema = 'itambox_esign.graphql.schema'  # Optional GraphQL hook

    required_settings = ['DOCUSIGN_API_KEY']
    default_settings = {
        'DOCUSIGN_API_KEY': None,
        'DOCUSIGN_SANDBOX': True,
    }

    def ready(self):
        super().ready()
        # Custom registration logic goes here
        # (e.g. registering template injections, viewsets, menus)

config = EsignPluginConfig
```

---

## 2. Registering the Plugin

To enable your plugin, add its package name to the `ITAMBOX_PLUGINS` environment
variable (comma-separated), or configure it directly in `core/settings/base.py`:

```python
# Environment variable (preferred — survives updates):
# ITAMBOX_PLUGINS=itambox_esign

# Or in code:
PLUGINS = [
    'itambox_esign',
]
```

If your plugin requires custom settings, configure them under `PLUGINS_CONFIG`:

```python
PLUGINS_CONFIG = {
    'itambox_esign': {
        'DOCUSIGN_API_KEY': 'your-api-key-here',
        'DOCUSIGN_SANDBOX': True,
    }
}
```

These settings are deep-merged with your plugin's `default_settings` and made available at runtime via `settings.PLUGINS_RESOLVED_CONFIG['itambox_esign']`.

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
