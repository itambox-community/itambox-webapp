# ITAMbox Plugin API Reference

!!! warning "Status: Experimental"
    Plugins are opt-in through `settings.PLUGINS` and are not part of the Stable 1.0 compatibility promise. The API below is the bounded Experimental API for plugin API version **1.0**. Pin the ITAMbox revision and the plugin revision together; Experimental interfaces may change in any release.

This page is normative. A symbol, attribute, module, or hook not listed here is private/unstable and may be renamed, moved, or removed without a deprecation period.

## Activation and configuration

Only deployment operators activate plugins by placing an importable package name in `settings.PLUGINS` (normally through the comma-separated `ITAMBOX_PLUGINS` environment variable). Per-plugin values belong in `settings.PLUGINS_CONFIG` and are deep-merged with `default_settings`.

A plugin that cannot be imported, is malformed, fails compatibility checks, or raises during startup is disabled. ITAMbox continues starting and publishes a diagnostic; the failed plugin contributes no middleware, URL, REST router, GraphQL schema, menu, or template hook. This intentional degradation is a supported operator contract — see the [plugin removal and recovery runbook](../operations/plugin-runbook.md) for the operational response.

"Disabled" means different things depending on the stage that failed. A plugin that fails import, configuration or compatibility checks is skipped before Django app population: its `django_apps`, middleware and `PluginConfig` are not added, so it loads no models, migrations or app-level signal receivers (its package `__init__.py` is still imported during the check). A plugin that fails in `ready()` has already been added to the app registry, so models or signal receivers registered before the exception may remain loaded.

## `PluginConfig`

A plugin package's `__init__.py` must expose a class assigned to `config` that subclasses `itambox.plugins.PluginConfig`.

The following metadata and configuration attributes are supported:

| Attribute | Type | Meaning |
|---|---|---|
| `name` | `str` | Django app name. Set this to the plugin package name. |
| `verbose_name` | `str` | Human-readable Django app name. |
| `default_settings` | `dict` | Safe defaults merged with the operator's `PLUGINS_CONFIG` entry. Do not put secrets in source-controlled defaults. |
| `required_settings` | `list[str]` | Keys that must be present and non-`None` after merging. |
| `middleware` | `list[str]` | Importable Django middleware class paths. They are added during startup and removed when this plugin is disabled. |
| `django_apps` | `list[str]` | Additional Django apps required by the plugin. |
| `version` | `str` | Plugin's own release/version identifier. It is not the ITAMbox product version and is not used for host compatibility. |
| `author` | `str` | Plugin author/vendor metadata. |
| `author_email` | `str` | Plugin maintainer contact metadata. |
| `base_url` | `str` or `None` | URL slug for the plugin's optional UI URLconf. Defaults to the package name. |
| `min_version` | `str` or `None` | **ITAMbox product** minimum version. Existing product-version semantics are unchanged. |
| `max_version` | `str` or `None` | **ITAMbox product** maximum version. Existing product-version semantics are unchanged. |
| `min_plugin_api_version` | `str` | Minimum ITAMbox plugin API version supported by the plugin. Required for activation. |
| `max_plugin_api_version` | `str` | Maximum ITAMbox plugin API version supported by the plugin. Required for activation. |
| `graphql_schema` | `str` or `None` | Dotted module path containing optional Strawberry `Query` and/or `Mutation` types. |

`ready()` is the startup composition hook. It may use only the extension points listed below. A failure from `ready()` disables this plugin and is isolated from other plugins and Stable core.

### Version and compatibility matrix

The host publishes one independent API version constant, `itambox.plugins.PLUGIN_API_VERSION`, currently `1.0`. A plugin must declare both `min_plugin_api_version` and `max_plugin_api_version`; omission is a documented Experimental compatibility failure, not an implicit wildcard.

| ITAMbox product | Host plugin API | Plugin metadata | Result |
|---|---|---|---|
| 1.0.x | 1.0 | Product bounds include 1.0.x; API bounds include 1.0 | **Enabled** |
| 1.0.x | 1.0 | Product bounds exclude 1.0.x | **Disabled** as `incompatible-product` |
| 1.0.x | 1.0 | API bounds exclude 1.0 | **Disabled** as `incompatible-plugin-api` |
| 1.0.x | 1.0 | Either API bound is missing or invalid | **Disabled** as `missing-plugin-api` or `invalid-plugin-api` |
| future product | 1.0 | Product bounds use the existing `min_version/max_version` semantics | **Enabled/disabled** according to product bounds |
| any supported product | future API | API bounds do not include the host API | **Disabled**; no product-version inference is performed |

The product fields are deliberately not aliases for the API fields. A plugin that supports ITAMbox 1.0 but only plugin API 2.0 is not compatible, and a plugin that supports plugin API 1.0 does not automatically support a different ITAMbox product release.

Example:

```python
from itambox.plugins import PluginConfig


class ExamplePluginConfig(PluginConfig):
    name = "example_plugin"
    verbose_name = "Example plugin"
    version = "2.4.0"
    author = "Example GmbH"
    author_email = "plugins@example.invalid"

    # Product compatibility: these remain product-version fields.
    min_version = "1.0.0"
    max_version = "1.99.99"

    # Plugin API compatibility: independent from the product version.
    min_plugin_api_version = "1.0"
    max_plugin_api_version = "1.0"


config = ExamplePluginConfig
```

## Supported registry extension points

Use the singleton `itambox.registry.registry` from `ready()`. Only these four `register_plugin_*` methods are supported:

### `register_plugin_template_content(model, content_class)`

Registers a `PluginTemplateContent` subclass for a model label such as `assets.asset`. The class may implement the documented rendering positions (`head`, `navbar`, `alerts`, `buttons`, `left_panel`, `right_panel`, and `full_width_panel`). Its returned content is plugin-authored HTML and must follow ITAMbox's escaping/CSP rules.

### `register_plugin_menu(menu_cls)`

Registers a `PluginNavigationMenu` subclass. Menu classes provide the documented `label`, `icon_class`, and `groups` attributes.

### `register_plugin_menu_item(item_cls)`

Registers a `PluginNavigationItem` subclass for a standalone plugin navigation item. The supported attributes are `link`, `link_text`, `permissions`, `auth_required`, `staff_only`, and `buttons`.

### `register_plugin_viewset(plugin_name, prefix, viewset, basename=None)`

Registers a Django REST Framework viewset class or importable dotted class path. The route is mounted below `/api/plugins/<plugin_name>/`; `prefix` is the route suffix and `basename` is the DRF route basename.

A malformed registration raises during plugin composition, so startup isolation can disable the plugin before the bad contribution is exposed.

A plugin package may also expose an optional `urls.py` module when `base_url` is set (or when it accepts the package-name default). ITAMbox mounts that URLconf below `/plugins/<base_url>/`. The module's `urlpatterns` is the complete supported contract; URLconf helpers and view internals remain plugin-owned. A URL import failure disables the plugin and removes its other registered contributions.

## GraphQL extension

Set `PluginConfig.graphql_schema` to a module path. The module may export a Strawberry `Query` class, a `Mutation` class, or both; ITAMbox composes their fields into the root schema. Plugin schema fields remain subject to the normal authentication, tenant middleware, permission, and operation-budget (depth, alias, document-size, complexity) contracts. Resolvers receive the Strawberry `Info`, whose `context.request` is the Django request.

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
