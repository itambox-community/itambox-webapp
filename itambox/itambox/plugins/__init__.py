from django.apps import AppConfig

from itambox.plugins.runtime import PluginStartupError


class PluginConfig(AppConfig):
    """Base class and manifest of an ITAMbox plugin.

    Plugin packages expose a subclass from ``__init__.py`` as ``config``.
    Required manifest fields: ``name`` (the package), ``description`` and
    ``itambox_range``, the supported ITAMbox major.minor range with patch
    tolerance, written ``>=<major.minor>,<<next-major.minor>>`` (for example
    ``>=1.1,<1.2``).

    Resources are declared, never registered imperatively. Each is an optional
    dotted path (or the object itself) with a conventional default location:
    ``template_extensions`` (``template_content.template_extensions``),
    ``menu`` (``navigation.menu``), ``menu_items`` (``navigation.menu_items``),
    ``viewsets`` (``api.viewsets.viewsets``) and ``graphql_schema``
    (``graphql.schema``, queries only).
    """

    default_settings = {}
    required_settings = []
    middleware = []
    django_apps = []

    version = ""
    description = ""
    author = ""
    author_email = ""
    base_url = None
    itambox_range = None

    template_extensions = None
    menu = None
    menu_items = None
    viewsets = None
    graphql_schema = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("ready")
        if original is not None and getattr(original, "_itambox_composed", False):
            return

        def ready(self):
            # inline import: app-registry: assembly loads plugin modules that need the populated app registry
            from itambox.plugins.assembly import assemble_after_last_plugin, compose_plugin

            try:
                compose_plugin(self)
            except PluginStartupError:
                raise
            # broad except: boundary-isolation: plugin code is wrapped with its name and re-raised
            except Exception as exc:
                raise PluginStartupError(self.name, "compose", exc) from exc
            if original is not None:
                try:
                    original(self)
                # broad except: boundary-isolation: plugin code is wrapped with its name and re-raised
                except Exception as exc:
                    raise PluginStartupError(self.name, "ready", exc) from exc
            assemble_after_last_plugin(self)

        ready._itambox_composed = True
        cls.ready = ready
