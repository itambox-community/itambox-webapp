from rest_framework.routers import DefaultRouter
from rest_framework.settings import api_settings

from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet
from itambox.plugins.runtime import PluginStartupError
from itambox.registry import registry


class PluginRouter(DefaultRouter):
    """
    Sub-router for REST API viewsets registered by plugins.
    Allows registering viewsets mapped to /api/plugins/<plugin_name>/<prefix>/.
    """

    pass


class _HostOwnedViewSetDefaults:
    """Enforce the host API defaults on every mounted plugin viewset."""

    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
    permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES

    @property
    def schema(self):
        return api_settings.DEFAULT_SCHEMA_CLASS()

    @schema.setter
    def schema(self, _plugin_schema):
        # Router/action kwargs cannot replace the host schema.
        return None

    def get_authenticators(self):
        return [auth() for auth in api_settings.DEFAULT_AUTHENTICATION_CLASSES]

    def get_permissions(self):
        return [permission() for permission in api_settings.DEFAULT_PERMISSION_CLASSES]

    def get_queryset(self):
        queryset = super().get_queryset()
        if hasattr(queryset, "filter_by_tenant"):
            queryset = queryset.filter_by_tenant()
        return queryset


def _host_owned_viewset(plugin_name, viewset):
    if not issubclass(viewset, (ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet)):
        raise TypeError(f"viewset {viewset!r} does not use a supported core model viewset base")
    suffix = plugin_name.replace(".", "_")
    return type(
        f"{viewset.__name__}_{suffix}_HostOwned",
        (_HostOwnedViewSetDefaults, viewset),
        {"__module__": viewset.__module__},
    )


router = PluginRouter()
app_name = "plugins"

for plugin_name, registrations in registry.get_plugin_viewsets().items():
    for prefix, viewset, basename in registrations:
        try:
            router.register(
                f"{plugin_name}/{prefix}",
                _host_owned_viewset(plugin_name, viewset),
                basename=basename,
            )
        # broad except: boundary-isolation: plugin code is wrapped with its name and re-raised
        except Exception as exc:
            raise PluginStartupError(plugin_name, "assemble", exc) from exc

urlpatterns = router.urls
