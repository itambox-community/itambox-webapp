from rest_framework.routers import DefaultRouter

from itambox.plugins.runtime import PluginStartupError
from itambox.registry import registry


class PluginRouter(DefaultRouter):
    """
    Sub-router for REST API viewsets registered by plugins.
    Allows registering viewsets mapped to /api/plugins/<plugin_name>/<prefix>/.
    """

    pass


router = PluginRouter()
app_name = "plugins"

for plugin_name, registrations in registry.get_plugin_viewsets().items():
    for prefix, viewset, basename in registrations:
        try:
            router.register(f"{plugin_name}/{prefix}", viewset, basename=basename)
        except Exception as exc:
            raise PluginStartupError(plugin_name, "assemble", exc) from exc

urlpatterns = router.urls
