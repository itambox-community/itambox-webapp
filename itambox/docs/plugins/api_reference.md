# ITAMbox Plugin API Reference

!!! warning "Status: Experimental"
    Plugins are opt-in through `settings.PLUGINS` and are not part of the Stable compatibility promise. Pin the ITAMbox revision and the plugin revision together; Experimental interfaces may change in any release.

This page is normative. A symbol, attribute, module, or hook not listed here is private/unstable and may be renamed, moved, or removed without a deprecation period.

## Activation and configuration

Only deployment operators activate plugins by placing an importable package name in `settings.PLUGINS` (normally through the comma-separated `ITAMBOX_PLUGINS` environment variable). Per-plugin values come from the operator configuration channel described under [Configuration](#configuration) and are validated against the plugin's `settings_schema`.

A missing package, an import failure, an invalid manifest or invalid configuration, or an error raised while the plugin is composed aborts startup with a message that names the plugin and the phase (`resolve`, `compose`, `ready`, `assemble`). Fix the configuration or remove the plugin from `ITAMBOX_PLUGINS`. The single exception is a version incompatibility: a plugin whose `itambox_range` excludes the running release is skipped with a logged warning and an entry in `manage.py plugins`, before it enters `INSTALLED_APPS`; the rest of the system starts normally. See the [plugin runbook](../operations/plugin-runbook.md).

## Configuration

Operators configure plugins through one channel and never edit product source.

1. `ITAMBOX_PLUGINS_CONFIG`: a JSON object keyed by plugin package name, in the same style as the `ITAMBOX_TENANT_*_CONFIGS` variables.

    ```bash
    ITAMBOX_PLUGINS=example_plugin
    ITAMBOX_PLUGINS_CONFIG='{"example_plugin": {"endpoint": "https://example.test", "retries": 5}}'
    ```

2. Per-key environment overrides, so a secret store can inject single values: `ITAMBOX_PLUGIN_<NAME>_<KEY>`. `<NAME>` is the package name and `<KEY>` the schema key, both upper-cased with every character outside `A-Z0-9` replaced by `_` (`example_plugin` / `api_key` becomes `ITAMBOX_PLUGIN_EXAMPLE_PLUGIN_API_KEY`). An override wins over the JSON value. Override values are text and are converted to the declared type; `bool` accepts `true/false/1/0/yes/no/on/off`, `list` and `dict` take JSON. This `ITAMBOX_PLUGIN_<NAME>_<KEY>` family is a published naming rule: it is stable for 1.x. Two keys (of the same or of different configured plugins) whose names normalise to the same variable abort startup with an error naming both plugins, both keys and the variable, whether or not the variable is set.

A plugin declares what it accepts with `settings_schema`, a mapping of key to a spec:

| Spec field | Meaning |
|---|---|
| `type` | `str` (default), `int`, `float`, `bool`, `list` or `dict`. |
| `default` | Used when the operator sets nothing. Not allowed together with `required` or `secret`. |
| `required` | The operator must supply a value. |
| `secret` | The value is never written to logs, diagnostics or error messages. Any non-empty string and any `int` or `float` value is redacted, whether it comes from the JSON or from an environment override; `bool` values are not. |
| `choices` | Allowed values (scalar types). |

```python
settings_schema = {
    "endpoint": {"type": "str", "default": "https://api.example.test"},
    "retries": {"type": "int", "default": 3},
    "api_key": {"type": "str", "required": True, "secret": True},
}
```

Validation runs in the resolve phase. A missing required key, a wrong type, a value outside `choices`, a key the schema does not declare, or a malformed schema aborts startup with a message that names the plugin and the key but never a configured value. When plugin code fails during startup, the `PluginStartupError` message replaces every `secret` value with `[redacted]`. The original exception stays chained as its cause, so a full traceback printed by Python or Django still shows the plugin's own exception text; keep secrets out of the messages your plugin raises. Read validated values with `itambox.plugins.get_plugin_config(plugin_name, key)`.

## `PluginConfig`

A plugin package's `__init__.py` must expose a class assigned to `config` that subclasses `itambox.plugins.PluginConfig`.

Required manifest fields: `name` (the package), `description` and `itambox_range`. Everything a plugin contributes is **declared** on the config and registered by the host; plugins never call the registry.

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | Django app name; must equal the package name. |
| `description` | `str` | Required non-empty summary. |
| `itambox_range` | `str` | Required supported ITAMbox range `>=<major.minor>,<<next-major.minor>>`, for example `>=1.1,<1.2`. Patch releases inside the range are accepted. Malformed ranges abort startup. |
| `verbose_name`, `version`, `author`, `author_email` | `str` | Metadata. `version` is the plugin's own release. |
| `settings_schema` | `dict` | Declares the operator settings (see [Configuration](#configuration)). Replaces `default_settings` / `required_settings`, which a plugin must no longer declare. |
| `middleware`, `django_apps` | `list[str]` | Added at startup. |
| `base_url` | `str` or `None` | URL slug for the optional `urls.py` (default: package name). |
| `template_extensions` | list or dotted path | `PluginTemplateContent` subclasses; each names its `models = ["app.model"]`. Default: `<pkg>.template_content.template_extensions`. |
| `menu`, `menu_items` | class(es) or dotted path | `PluginNavigationMenu` / `PluginNavigationItem`. Default: `<pkg>.navigation.menu` / `menu_items`. |
| `viewsets` | list of `(prefix, viewset[, basename])` or dotted path | Viewsets must derive from the core API viewset base; mounted at `/api/plugins/<name>/<prefix>/`. Default: `<pkg>.api.viewsets.viewsets`. |
| `graphql_schema` | module path | Module exporting a Strawberry `Query` (queries only; a `Mutation` aborts startup). Default: `<pkg>.graphql.schema`. |

Startup runs in phases: **resolve** (settings time: import, validate, version range), **compose** (host validates and registers declared resources), **ready** (your own `ready()`, after compose), **assemble** (GraphQL schema trial build, URL mounting; registrations are then frozen). Every phase failure names the plugin.

### Version compatibility

Compatibility is the single `itambox_range` against the running ITAMbox `major.minor`. There is no separate plugin API version: the product version is the contract.

Example:

```python
from itambox.plugins import PluginConfig


class ExamplePluginConfig(PluginConfig):
    name = "example_plugin"
    verbose_name = "Example plugin"
    description = "Example integration"
    version = "2.4.0"
    itambox_range = ">=1.1,<1.2"


config = ExamplePluginConfig
```

## Extension points

Declare resources on the config (see the table above). The host validates and registers them; the registry has no plugin-facing API.

- **Template extensions**: `PluginTemplateContent` subclasses implement `head`, `navbar`, `alerts`, `buttons`, `left_panel`, `right_panel`, `full_width_panel`. Returned content is plugin-authored HTML and must follow ITAMbox's escaping/CSP rules.
- **Navigation**: a `PluginNavigationMenu` (`label`, `icon_class`, `groups`) and standalone `PluginNavigationItem`s (`link`, `link_text`, `permissions`, `auth_required`, `staff_only`, `buttons`).
- **REST viewsets**: mounted below `/api/plugins/<plugin_name>/`.

A plugin package may also expose an optional `urls.py` module when `base_url` is set (or when it accepts the package-name default). ITAMbox mounts that URLconf below `/plugins/<base_url>/`. The module's `urlpatterns` is the complete supported contract; URLconf helpers and view internals remain plugin-owned. A URL import failure aborts startup naming the plugin.

## GraphQL extension

Set `graphql_schema` (or use `graphql/schema.py`) to a module exporting a Strawberry `Query`. Queries only: plugin mutations are not supported and abort startup. Fields remain subject to the normal authentication, tenant, permission and operation-budget contracts.

## Private and unstable surface

Everything not explicitly listed in this document is private/unstable, including:

- other modules and names below `itambox.*`, all `core.*` names, and all domain-app internals;
- `Registry` storage attributes, getters, reset/transaction helpers, and registration ownership internals;
- direct mutation of registries, settings, URL patterns, GraphQL root types, or Django app registries;
- undocumented plugin lifecycle ordering, install/uninstall/upgrade orchestration, runtime enable/disable, and per-tenant activation;
- `PluginModel` and any model mixins unless a future API version lists them explicitly;
- undocumented template context, navigation internals, API router internals, and middleware ordering beyond the declared `middleware` list;
- package management, migration automation, orphan-data cleanup, sandboxing, capability restrictions, and signature verification.

The Experimental label is intentional: this list is the compatibility boundary, not a promise that plugin behavior is Stable.
