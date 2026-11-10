from django.db.models import Count, Q
from django_filters.rest_framework import DjangoFilterBackend

from core.managers import Scope
from itambox.api.permissions import (
    IsSuperuserOrReadOnly,
    StrictTenantPermission,
    TokenPermissions,
)
from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet
from organization.filters import (
    AssetHolderFilterSet,
    ContactAssignmentFilterSet,
    ContactFilterSet,
    ContactRoleFilterSet,
    CostCenterFilterSet,
    LocationFilterSet,
    RegionFilterSet,
    SiteFilterSet,
    SiteGroupFilterSet,
    TenantFilterSet,
    TenantGroupFilterSet,
)
from organization.models import (
    AssetHolder,
    Contact,
    ContactAssignment,
    ContactRole,
    CostCenter,
    Location,
    Region,
    Site,
    SiteGroup,
    Tenant,
    TenantGroup,
    TenantResourceGrant,
)
from organization.services.resource_access import visible_to_containers

from .filters import TenantResourceGrantAuditFilterSet
from .permissions import TenantResourceGrantAuditPermission
from .serializers import (
    AssetHolderSerializer,
    ContactAssignmentSerializer,
    ContactRoleSerializer,
    ContactSerializer,
    CostCenterSerializer,
    LocationSerializer,
    RegionSerializer,
    SiteGroupSerializer,
    SiteSerializer,
    TenantGroupSerializer,
    TenantResourceGrantAuditSerializer,
    TenantSerializer,
)


class SiteViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Site.objects.select_related("region", "group", "tenant")
    serializer_class = SiteSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = SiteFilterSet


class RegionViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = (
        Region.objects.select_related("parent")
        .prefetch_related("sites")
        .annotate(site_count=Count("sites", filter=Q(sites__deleted_at__isnull=True)))
    )
    serializer_class = RegionSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = RegionFilterSet


class SiteGroupViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = (
        SiteGroup.objects.select_related("parent")
        .prefetch_related("sites")
        .annotate(site_count=Count("sites", filter=Q(sites__deleted_at__isnull=True)))
    )
    serializer_class = SiteGroupSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = SiteGroupFilterSet


class LocationViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = (
        Location.objects.select_related("site", "parent", "tenant")
        .prefetch_related("assets")
        .annotate(asset_count=Count("assets", filter=Q(assets__deleted_at__isnull=True)))
    )
    serializer_class = LocationSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = LocationFilterSet


class TenantGroupViewSet(ITAMBoxModelViewSet):
    permission_classes = [
        TokenPermissions,
        StrictTenantPermission,
        IsSuperuserOrReadOnly,
    ]
    queryset = (
        TenantGroup.objects.select_related("parent")
        .prefetch_related("tenants")
        .annotate(tenant_count=Count("tenants", filter=Q(tenants__deleted_at__isnull=True)))
    )
    serializer_class = TenantGroupSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = TenantGroupFilterSet


class TenantViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Tenant.objects.select_related("group")
    serializer_class = TenantSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = TenantFilterSet


class AssetHolderViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = (
        AssetHolder.objects.select_related("tenant")
        .prefetch_related("asset_assignments")
        .annotate(
            assignment_count=Count("asset_assignments", filter=Q(asset_assignments__is_active=True)),
        )
    )
    serializer_class = AssetHolderSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AssetHolderFilterSet


class ContactViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = Contact.objects.prefetch_related("tags").all()
    serializer_class = ContactSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ContactFilterSet


class ContactRoleViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = ContactRole.objects.all()
    serializer_class = ContactRoleSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ContactRoleFilterSet


class ContactAssignmentViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = ContactAssignment.objects.select_related("contact", "role", "content_type").prefetch_related(
        "contact__tags"
    )
    serializer_class = ContactAssignmentSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = ContactAssignmentFilterSet

    def get_queryset(self):
        # ContactAssignment has no tenant column, so its default manager is
        # unscoped and super().get_queryset() returns every tenant's rows. This
        # method is the SOLE list/detail gate: visible_in() keeps a row only
        # when its generic-FK target AND its contact are visible in the active
        # scope (single tenant, tenant group or all accessible tenants). A
        # missing scope resolves to DENIED and yields nothing. Object-level
        # mutation checks stay with StrictTenantPermission.
        qs = super().get_queryset()

        if self.request.user.is_superuser:
            return qs
        return qs.visible_in(Scope.current())


class CostCenterViewSet(ITAMBoxModelViewSet):
    permission_classes = [TokenPermissions, StrictTenantPermission]
    queryset = (
        CostCenter.objects.select_related("tenant", "parent")
        .prefetch_related("children")
        .annotate(
            child_count=Count("children", filter=Q(children__deleted_at__isnull=True)),
        )
    )
    serializer_class = CostCenterSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = CostCenterFilterSet


class TenantResourceGrantAuditViewSet(ITAMBoxReadOnlyModelViewSet):
    permission_classes = [TenantResourceGrantAuditPermission]
    # unscoped: audit listing includes soft-deleted grants; visibility is narrowed by visible_to_containers
    queryset = TenantResourceGrant._base_manager.all()
    serializer_class = TenantResourceGrantAuditSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = TenantResourceGrantAuditFilterSet

    def get_queryset(self):
        # unscoped: audit listing includes soft-deleted grants; visibility is narrowed by visible_to_containers
        queryset = TenantResourceGrant._base_manager.all().select_related(
            "tenant",
            "grantee_tenant",
            "grantee_tenant_group",
            "resource_type",
            "granted_by",
        )
        return visible_to_containers(
            self.request.user,
            queryset,
            "organization.view_tenantresourcegrant",
            request=self.request,
        )
