from collections.abc import Callable
from typing import Protocol, cast

from django.core.exceptions import ImproperlyConfigured
from django.db.models import QuerySet
from rest_framework.generics import GenericAPIView
from rest_framework.routers import DefaultRouter
from rest_framework.settings import api_settings

from core.managers import Scope, TenantScopeDeclaration, TenantScopingQuerySet, tenant_scope_declaration
from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet
from itambox.plugins.runtime import PluginStartupError
from itambox.registry import registry


def _apply_host_tenant_scope(queryset: QuerySet) -> QuerySet:
    model = getattr(queryset, "model", None)
    if model is None:
        raise ImproperlyConfigured("Plugin viewsets must return a model queryset.")
    scope = Scope.current()
    if scope.kind == Scope.SYSTEM:
        return queryset
    declaration = tenant_scope_declaration(model)
    scoped = declaration.strategy != TenantScopeDeclaration.NONE or declaration.has_tenant_group
    if scope.kind != Scope.DENIED and not scoped:
        return queryset
    host_queryset = TenantScopingQuerySet(model=model, using=queryset.db).for_scope(scope)
    return queryset.filter(pk__in=host_queryset.values("pk"))


class PluginRouter(DefaultRouter):
    """
    Sub-router for REST API viewsets registered by plugins.
    Allows registering viewsets mapped to /api/plugins/<plugin_name>/<prefix>/.
    """

    pass


class _SchemaDescriptor(Protocol):
    __get__: Callable[[object | None, type], object]


class _HostOwnedSchemaDescriptor:
    def __init__(self) -> None:
        self._schema = cast(_SchemaDescriptor, api_settings.DEFAULT_SCHEMA_CLASS())

    def __get__(self, instance: object | None, owner: type) -> object:
        if instance is None:
            return self._schema
        return self._schema.__get__(instance, owner)

    def __set__(self, instance: object, _plugin_schema: object) -> None:
        self._schema.__get__(instance, type(instance))


class _HostOwnedViewSetDefaults:
    """Enforce the host API defaults on every mounted plugin viewset."""

    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
    permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES
    schema = _HostOwnedSchemaDescriptor()

    def get_authenticators(self):
        return [auth() for auth in api_settings.DEFAULT_AUTHENTICATION_CLASSES]

    def get_permissions(self):
        return [permission() for permission in api_settings.DEFAULT_PERMISSION_CLASSES]

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        return _apply_host_tenant_scope(queryset)

    def filter_queryset(self, queryset: QuerySet) -> QuerySet:
        model = queryset.model
        filtered_queryset = cast(GenericAPIView, super()).filter_queryset(queryset)
        if getattr(filtered_queryset, "model", None) is not model:
            raise ImproperlyConfigured("Plugin filter backends must preserve the queryset model.")
        return _apply_host_tenant_scope(filtered_queryset)


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
