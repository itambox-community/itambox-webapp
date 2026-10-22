"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import importlib
import mimetypes

from django.apps import apps
from django.conf import settings  # Import settings
from django.conf.urls.static import static  # Import static
from django.contrib import admin
from django.urls import include, path
from django.views.decorators.cache import never_cache
from django.views.generic import RedirectView, TemplateView
from django.views.i18n import JavaScriptCatalog

from assets.views_scan import ScanResolveView
from core.auth.oidc import TenantOIDCAuthorizeView, TenantOIDCCallbackView
from core.schema import schema
from core.views.auth import ITAMboxLoginView, TenantSamlAcsView, TenantSamlLoginView
from core.views.graphql import PrivateGraphQLView
from core.views.mfa import MFASetupView
from extras.attachment_views import (
    FileAttachmentDeleteView,
    FileAttachmentDownloadView,
    FileAttachmentUploadView,
    ImageAttachmentDeleteView,
    ImageAttachmentServeView,
    ImageAttachmentUploadView,
    JournalEntryCreateView,
    JournalEntryListView,
)
from extras.dashboard import views as dashboard_views
from extras.export_views import ObjectExportView
from extras.label_views import LabelPrintView
from itambox.plugins.api_views import PluginDiagnosticsView
from itambox.plugins.runtime import PluginStartupError
from itambox.views.features import ObjectChangeListView, ObjectChangeView
from itambox.views.generic import (
    GenericObjectImportView,
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectBulkPurgeView,
    ObjectBulkRestoreView,
    ObjectPurgeView,
    ObjectRestoreView,
)
from itambox.views.jobs import JobCancelView, JobDetailView, JobListView
from itambox.views.utility import SearchView, health  # Import core views, aliased to avoid clash
from users.table_config_views import table_config

mimetypes.add_type("application/zip", ".zip")


# Main URL Patterns
urlpatterns = [
    # PWA Routes
    path(
        "manifest.json",
        never_cache(TemplateView.as_view(template_name="manifest.json", content_type="application/json")),
        name="manifest.json",
    ),
    path(
        "service-worker.js",
        never_cache(TemplateView.as_view(template_name="service-worker.js", content_type="application/javascript")),
        name="service-worker.js",
    ),
    path("offline/", TemplateView.as_view(template_name="offline.html"), name="offline"),
    path("admin/", admin.site.urls),
    # Must precede the auth include: our login view renders the configured SSO
    # entry points alongside the local credential form.
    path("accounts/login/", ITAMboxLoginView.as_view(), name="login"),
    path("accounts/", include("django.contrib.auth.urls")),
    # MFA gate (TOTP) for local-password logins; the OTP middleware redirects here.
    path("accounts/mfa/", MFASetupView.as_view(), name="mfa_setup"),
    path("", dashboard_views.DashboardView.as_view(), name="dashboard"),  # Root dashboard (extras owns dashboards)
    # Search Path
    path("search/", SearchView.as_view(), name="search"),
    # Global scan-to-find (resolves a barcode/QR code to an asset URL)
    path("scan/resolve/", ScanResolveView.as_view(), name="scan_resolve"),
    # UI Paths
    path("assets/", include("assets.urls", namespace="assets")),
    path(
        "components/",
        include(
            (
                [
                    path(
                        "",
                        RedirectView.as_view(url="/inventory/inventory/?type=components", permanent=True),
                        name="component_list",
                    ),
                    path(
                        "add/",
                        RedirectView.as_view(url="/inventory/components/add/", permanent=True),
                        name="component_create",
                    ),
                    path(
                        "<int:pk>/",
                        RedirectView.as_view(url="/inventory/components/%(pk)d/", permanent=True),
                        name="component_detail",
                    ),
                    path(
                        "<int:pk>/edit/",
                        RedirectView.as_view(url="/inventory/components/%(pk)d/edit/", permanent=True),
                        name="component_update",
                    ),
                    path(
                        "<int:pk>/delete/",
                        RedirectView.as_view(url="/inventory/components/%(pk)d/delete/", permanent=True),
                        name="component_delete",
                    ),
                    path(
                        "<int:pk>/clone/",
                        RedirectView.as_view(url="/inventory/components/%(pk)d/clone/", permanent=True),
                        name="component_clone",
                    ),
                    path(
                        "<int:pk>/add-stock/",
                        RedirectView.as_view(url="/inventory/components/%(pk)d/add-stock/", permanent=True),
                        name="component_add_stock",
                    ),
                    path(
                        "stocks/",
                        RedirectView.as_view(url="/inventory/component-stocks/", permanent=True),
                        name="componentstock_list",
                    ),
                    path(
                        "stocks/add/",
                        RedirectView.as_view(url="/inventory/component-stocks/add/", permanent=True),
                        name="componentstock_create",
                    ),
                    path(
                        "stocks/<int:pk>/edit/",
                        RedirectView.as_view(url="/inventory/component-stocks/%(pk)d/edit/", permanent=True),
                        name="componentstock_update",
                    ),
                    path(
                        "stocks/<int:pk>/delete/",
                        RedirectView.as_view(url="/inventory/component-stocks/%(pk)d/delete/", permanent=True),
                        name="componentstock_delete",
                    ),
                    path(
                        "stocks/<int:pk>/adjust/",
                        RedirectView.as_view(url="/inventory/component-stocks/%(pk)d/adjust/", permanent=True),
                        name="componentstock_adjust",
                    ),
                    path(
                        "allocations/",
                        RedirectView.as_view(url="/inventory/component-allocations/", permanent=True),
                        name="componentallocation_list",
                    ),
                    path(
                        "allocations/add/",
                        RedirectView.as_view(url="/inventory/component-allocations/add/", permanent=True),
                        name="componentallocation_create",
                    ),
                    path(
                        "allocations/<int:pk>/edit/",
                        RedirectView.as_view(url="/inventory/component-allocations/%(pk)d/edit/", permanent=True),
                        name="componentallocation_update",
                    ),
                    path(
                        "allocations/<int:pk>/delete/",
                        RedirectView.as_view(url="/inventory/component-allocations/%(pk)d/delete/", permanent=True),
                        name="componentallocation_delete",
                    ),
                ],
                "components",
            ),
            namespace="components",
        ),
    ),
    path("inventory/", include("inventory.urls", namespace="inventory")),
    path("compliance/", include("compliance.urls", namespace="compliance")),
    path("organization/", include("organization.urls", namespace="organization")),
    path("extras/", include("extras.urls")),
    path("procurement/", include("procurement.urls", namespace="procurement")),
    path("software/", include("software.urls", namespace="software")),
    path("licenses/", include("licenses.urls", namespace="licenses")),
    path("subscriptions/", include("subscriptions.urls", namespace="subscriptions")),
    path("tables/config/<str:model_name>/", table_config, name="table_config"),
    path("user/", include("users.urls")),  # Include the users app URLs
    # API Paths (prefixed with /api/) - Point directly to the main api.urls
    path("api/", include("itambox.api.urls", namespace="api")),  # Added namespace='api'
    path("api/plugins/", PluginDiagnosticsView.as_view(), name="plugin_diagnostics"),
    path("api/plugins/", include("itambox.plugins.urls", namespace="plugins-api")),
    path("graphql/", PrivateGraphQLView.as_view(schema=schema), name="graphql"),
    # Path for core app non-API views if any (e.g., User Preferences UI view)
    # path('core/', include('core.urls')), # Example if core had UI views
    # Changelog
    path("changelog/", ObjectChangeListView.as_view(), name="objectchange_list"),
    path("changelog/<int:pk>/", ObjectChangeView.as_view(), name="objectchange"),
    # Journal Entries (global activity list)
    path("journal/", JournalEntryListView.as_view(), name="journalentry_list"),
    # Background Jobs & Tasks UI
    path("jobs/", JobListView.as_view(), name="job_list"),
    path("jobs/<int:pk>/", JobDetailView.as_view(), name="job_detail"),
    path("jobs/<int:pk>/cancel/", JobCancelView.as_view(), name="job_cancel"),
    # Bulk Actions
    path("bulk-delete/", ObjectBulkDeleteView.as_view(), name="bulk_delete"),
    path("bulk-edit/", ObjectBulkEditView.as_view(), name="bulk_edit"),
    # Soft Delete Management Actions
    path("object/<int:content_type_id>/<int:object_id>/restore/", ObjectRestoreView.as_view(), name="object_restore"),
    path("object/<int:content_type_id>/<int:object_id>/purge/", ObjectPurgeView.as_view(), name="object_purge"),
    path("object/<int:content_type_id>/bulk-restore/", ObjectBulkRestoreView.as_view(), name="object_bulk_restore"),
    path("object/<int:content_type_id>/bulk-purge/", ObjectBulkPurgeView.as_view(), name="object_bulk_purge"),
    # Export View
    path(
        "export/<str:app_label>/<str:model_name>/<int:template_id>/", ObjectExportView.as_view(), name="object_export"
    ),
    # Generic Import View
    path("import/<str:app_label>/<str:model_name>/", GenericObjectImportView.as_view(), name="generic_import"),
    # i18n
    path("i18n/", include("django.conf.urls.i18n")),
    # JS translation catalog (djangojs domain) consumed by static/src/*.ts via gettext()
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
    # SAML SSO. The tenant-aware entry point and the assertion consumer must
    # precede the djangosaml2 include so a flow started for a tenant keeps that
    # binding through the IdP round trip.
    path("saml2/login/<slug:tenant_slug>/", TenantSamlLoginView.as_view(), name="saml2_login_tenant"),
    path("saml2/acs/", TenantSamlAcsView.as_view(), name="saml2_acs"),
    path("saml2/", include("djangosaml2.urls")),
    # OIDC SSO
    path("oidc/authenticate/", TenantOIDCAuthorizeView.as_view(), name="oidc_authentication_init"),
    path(
        "oidc/authenticate/<str:tenant_slug>/",
        TenantOIDCAuthorizeView.as_view(),
        name="oidc_authentication_init_tenant",
    ),
    path("oidc/callback/", TenantOIDCCallbackView.as_view(), name="oidc_authentication_callback"),
    # Attachments
    path(
        "attachments/image/upload/<str:app_label>/<str:model_name>/<int:object_id>/",
        ImageAttachmentUploadView.as_view(),
        name="image_attachment_upload",
    ),
    path("attachments/image/delete/<int:pk>/", ImageAttachmentDeleteView.as_view(), name="image_attachment_delete"),
    path("attachments/image/<int:pk>/", ImageAttachmentServeView.as_view(), name="image_attachment_serve"),
    path(
        "attachments/file/upload/<str:app_label>/<str:model_name>/<int:object_id>/",
        FileAttachmentUploadView.as_view(),
        name="file_attachment_upload",
    ),
    path("attachments/file/delete/<int:pk>/", FileAttachmentDeleteView.as_view(), name="file_attachment_delete"),
    path("attachments/file/<int:pk>/download/", FileAttachmentDownloadView.as_view(), name="file_attachment_download"),
    # Journal Entries
    path(
        "journal/add/<str:app_label>/<str:model_name>/<int:object_id>/",
        JournalEntryCreateView.as_view(),
        name="journal_entry_add",
    ),
    # Label printing: one route for every labelled model, one object or many.
    path("labels/<str:app_label>/<str:model_name>/", LabelPrintView.as_view(), name="label_print"),
    # Health Check
    path("health/", health, name="health"),
]

# Mount plugin UI URLconfs (eager: an import or URLconf error aborts startup
# and names the plugin).
plugin_ui_patterns = []
for plugin_name in getattr(settings, "PLUGINS_ACTIVE", []):
    plugin_config = apps.get_app_config(plugin_name)
    base_url = getattr(plugin_config, "base_url", None) or plugin_name
    url_module_name = f"{plugin_name}.urls"
    try:
        importlib.import_module(url_module_name)
    except ModuleNotFoundError as exc:
        if exc.name == url_module_name:
            continue
        raise PluginStartupError(plugin_name, "assemble", exc) from exc
    except Exception as exc:  # broad except: cleanup-reraise: re-raised wrapped with the plugin name and phase
        raise PluginStartupError(plugin_name, "assemble", exc) from exc
    plugin_ui_patterns.append(path(f"{base_url}/", include((url_module_name, plugin_name), namespace=plugin_name)))

if plugin_ui_patterns:
    urlpatterns.append(path("plugins/", include((plugin_ui_patterns, "plugins"), namespace="plugins")))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Only mount the toolbar when it is actually installed (dev settings). Guarding
# on DEBUG alone is unsafe: DEBUG can be True under base/prod settings, where
# debug_toolbar is not in INSTALLED_APPS, which would crash at import time.
if settings.DEBUG and "debug_toolbar" in settings.INSTALLED_APPS:
    import debug_toolbar

    urlpatterns += [
        path("__debug__/", include(debug_toolbar.urls)),
    ]


# Custom error handlers — render the branded pages in templates/errors/.
handler404 = "itambox.views.errors.handler404"
handler500 = "itambox.views.errors.handler500"
handler403 = "itambox.views.errors.handler403"
