from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter
from rest_framework.permissions import BasePermission

from itambox.api.permissions import StrictTenantPermission, TokenPermissions
from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet
from software.filters import SoftwareFilterSet
from software.models import InstalledSoftware, Software

from .serializers import InstalledSoftwareSerializer, SoftwareSerializer


class SoftwareViewSet(ITAMBoxModelViewSet):
    # StrictTenantPermission enforces the object-level tenant boundary so a
    # tenant-A member cannot PATCH/DELETE a tenant-B Software (Software has a
    # `tenant` field that the permission reads). Without it, the override to
    # [TokenPermissions] dropped the global default and left detail mutations
    # cross-tenant. List scoping is already handled by the model's manager.
    permission_classes: list[type[BasePermission]] = [TokenPermissions, StrictTenantPermission]
    queryset = Software.objects.select_related("manufacturer").prefetch_related("tags").all()
    serializer_class: type[SoftwareSerializer] = SoftwareSerializer
    filter_backends: tuple = (DjangoFilterBackend,)
    filterset_class: type[SoftwareFilterSet] = SoftwareFilterSet


class InstalledSoftwareViewSet(ITAMBoxReadOnlyModelViewSet):
    queryset = InstalledSoftware.objects.select_related("asset", "software", "software__manufacturer").all()
    serializer_class = InstalledSoftwareSerializer
    # Wire the backends so the declared filterset_fields/search_fields actually apply (there
    # is no DEFAULT_FILTER_BACKENDS, so without this they were advertised-but-dead config).
    filter_backends = (DjangoFilterBackend, SearchFilter)
    filterset_fields = ["asset_id", "software_id", "software__manufacturer_id", "version_detected"]
    search_fields = ["asset__name", "software__name", "version_detected"]
