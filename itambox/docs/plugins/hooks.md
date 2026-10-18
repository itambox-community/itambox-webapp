# ITAMbox Plugin Extension Hooks

!!! warning "Status: Experimental"
    Only the resources declared on `PluginConfig` documented here are supported. Other hooks and internals are private/unstable and may change without notice.

This document details the developer hooks available to extend the web UI, menus, REST API, and GraphQL schema.

---

## 1. Template Injection

Plugins can inject custom HTML content into core detail page templates using `PluginTemplateContent`.

### Injection Points

Inherit from `itambox.plugins.views.PluginTemplateContent` and override any of the following methods to return HTML/safe string content:

- `head()`: Injects custom CSS/JS assets or metadata into the page's `<head>`.
- `navbar()`: Injects links or badges into the navigation header.
- `alerts()`: Injects custom warning/info banner alerts at the top of the main container.
- `buttons()`: Injects buttons into the page action button cluster.
- `left_panel()`: Injects a card/panel into the left detail page column.
- `right_panel()`: Injects a card/panel into the right detail page sidebar.
- `full_width_panel()`: Injects a card/panel spanning the full width at the bottom of the page.

### Example Template Injector

Create `example_esign/template_content.py`:

```python
from django.utils.html import format_html
from itambox.plugins.views import PluginTemplateContent

class AssetEsignContent(PluginTemplateContent):
    def head(self):
        return '<link rel="stylesheet" href="/static/example_esign/esign.css">'

    def left_panel(self):
        asset = self.context['object']
        # format_html escapes interpolated values and marks only this
        # application-authored HTML structure as safe.
        return format_html(
            '<div class="card mb-3">'
            '<div class="card-header"><h3 class="card-title">E-Signature Status</h3></div>'
            '<div class="card-body">No pending envelopes for {}.</div>'
            '</div>',
            asset.name,
        )
```

The plugin hook's result is rendered as trusted HTML. Keep markup static and
escape every value derived from an asset, user, or other data with
`format_html()` placeholders (or render an autoescaped Django template). Do not
interpolate data into an f-string or mark an interpolated string safe: doing so
can inject attacker-controlled HTML into the page.

Declare the model on the class (`models = ['assets.asset']`) and export `template_extensions = [AssetEsignContent]` from `template_content.py`; the host registers it.

---

## 2. Sidebar Navigation

Plugins can register menu items and custom menus to display on the ITAMbox sidebar.

### Menus and Items

- `PluginNavigationItem`: Represents an individual navigation link.
  - `link` (str): Reversed URL name (e.g. `'plugins:example_esign:dashboard'`) or a raw URL path starting with `/`.
  - `link_text` (str): Visual text shown in the sidebar.
  - `permissions` (tuple): Required permissions to view.
  - `auth_required` (bool): If True, only logged-in users see it.
  - `staff_only` (bool): If True, only staff users see it.
- `PluginNavigationMenu`: Represents a parent dropdown menu grouping multiple items.
  - `label` (str): Display label of the dropdown menu.
  - `icon_class` (str): Material Design Icon class string (e.g. `'mdi mdi-file-sign'`).
  - `groups` (tuple): Nested tuples of `EsignGroup` classes.

### Example Menu Definition

Create `example_esign/navigation.py`:

```python
from itambox.plugins.navigation import PluginNavigationMenu, PluginNavigationItem

class EsignNavigationItem(PluginNavigationItem):
    link = 'plugins:example_esign:dashboard'
    link_text = 'Signed Envelopes'

class EsignGroup:
    label = 'E-Signature'
    items = (EsignNavigationItem,)

class EsignNavigationMenu(PluginNavigationMenu):
    label = 'E-Signature'
    icon_class = 'mdi mdi-file-sign'
    groups = (EsignGroup,)
```

Export `menu = EsignNavigationMenu` (and optionally `menu_items = [...]`) from `navigation.py`; the host registers them.

---

## 3. Web UI Routing

To serve web pages, define views in `views.py` and register URL routes in `urls.py`.

### Example Routing

Create `example_esign/urls.py`:

```python
from django.urls import path
from .views import EsignDashboardView

app_name = 'example_esign'

urlpatterns = [
    path('dashboard/', EsignDashboardView.as_view(), name='dashboard'),
]
```

These views are dynamically mounted under `/plugins/example_esign/` and can be reversed in views/templates as `plugins:example_esign:dashboard`.

---

## 4. REST API resources

Plugin viewsets are declared resources; plugins do not register routes directly
with ITAMbox's central router. Export a `viewsets` list from `api/viewsets.py`,
or point `PluginConfig.viewsets` at that attribute:

```python
viewsets = [
    ('audit-records', 'example_plugin.api.views.AuditRecordViewSet'),
]
```

Each viewset must derive from `ITAMBoxModelViewSet` or
`ITAMBoxReadOnlyModelViewSet`. The host owns route mounting, authentication,
permission checks, schema generation, and tenant-scoping lifecycle; plugin
viewsets cannot override its reserved request and authorization methods.

Declare plugin-owned models through `PluginConfig.permission_targets` (or the
conventional `permissions.py` `targets` attribute), for example
`['example_plugin.auditrecord']`. The host validates ownership and registers
the model's permission targets for role configuration. See the normative
[Plugin API Reference](api_reference.md) for the supported declarations.

---

## 5. GraphQL Schema Extension

To extend the GraphQL schema, set `graphql_schema` in your config class and define a Strawberry `Query` type (queries only).

### Example Schema

Create `example_esign/graphql/schema.py`:

```python
import strawberry


@strawberry.type
class EsignStatusType:
    status: str


@strawberry.type
class Query:
    @strawberry.field
    def esign_status(self) -> EsignStatusType:
        return EsignStatusType(status="Sent")
```

ITAMbox merges this query alongside all core GraphQL queries at startup. An import error, a `Mutation` type or an invalid type aborts startup naming the plugin.
