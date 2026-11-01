from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Count, Q
from django_filters.rest_framework import DjangoFilterBackend

from core.managers import Scope, TenantScopingQuerySet, get_current_tenant
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
        # ContactAssignment is a generic-FK assignment model with no direct or
        # relational `tenant` field of its own (its tenant is derived from the
        # generic-FK *target*, not from `contact.tenant` — see the `tenant`
        # property on the model), so it uses the unscoped default manager and
        # filter_by_tenant() is a no-op: super().get_queryset() returns every
        # tenant's rows. This method is the SOLE list gate. A generic FK cannot
        # be ORM-joined to a tenant column, so for every target ContentType
        # present we collect that model's visible object ids and OR the matches
        # together. Object-level (detail/mutation) boundary is enforced
        # separately by StrictTenantPermission via the model's `tenant` property.
        qs = super().get_queryset()

        if self.request.user.is_superuser:
            return qs

        active_tenant = get_current_tenant()
        if active_tenant is None:
            # Authenticated non-superuser with no resolved tenant: fail closed.
            return qs.none()

        allowed = Q(pk__in=[])
        # Bound iteration to the content types actually referenced by existing
        # assignments rather than every ContentType in the install.
        content_type_ids = qs.values_list("content_type", flat=True).distinct()
        for ct in ContentType.objects.filter(pk__in=list(content_type_ids)):
            model_class = ct.model_class()
            if model_class is None:
                continue

            # Probe the tenant *signal*, not just a DB column. A model is
            # tenant-aware if it has any of: a direct `tenant` field, a declared
            # `tenant_lookup` (ORM path to the owning tenant for relation-scoped
            # rows like AssetAssignment / *Stock / KitItem), or a `tenant`
            # property/attr. Probing only `get_field('tenant')` (the old bug)
            # missed lookup/property models and exposed every such assignment to
            # every tenant.
            tenant_lookup = getattr(model_class, "tenant_lookup", None)
            has_tenant_field = True
            try:
                model_class._meta.get_field("tenant")
            except FieldDoesNotExist:
                has_tenant_field = False
            tenant_aware = has_tenant_field or bool(tenant_lookup) or hasattr(model_class, "tenant")

            if not tenant_aware:
                # Genuine global/shared catalogue (e.g. Manufacturer): rows are
                # not tenant-owned, so their assignments stay visible to all.
                allowed |= Q(content_type=ct)
                continue

            manager = model_class._default_manager
            if isinstance(manager.get_queryset(), TenantScopingQuerySet):
                # The default manager is tenant-scoping: it already unions the
                # active tenant's own rows with global (allow_global_tenant)
                # rows, whether scoping is by direct field or `tenant_lookup`.
                # Letting it do the work avoids over-restricting global
                # catalogue targets to the active tenant (L1) and applies
                # soft-delete filtering for free. Don't add `.filter(tenant=...)`.
                visible_ids = list(manager.get_queryset().for_scope(Scope.current()).values_list("pk", flat=True))
            elif tenant_lookup:
                # Tenant-aware via a relational lookup but the default manager is
                # NOT tenant-scoping: scope explicitly through the lookup path,
                # keeping rows whose parent is global (null tenant) visible.
                visible_ids = list(
                    manager.filter(
                        Q(**{f"{tenant_lookup}_id": active_tenant.pk}) | Q(**{f"{tenant_lookup}__isnull": True})
                    ).values_list("pk", flat=True)
                )
            elif has_tenant_field:
                # Tenant-aware via a real `tenant` DB field on an unscoped
                # manager: scope to the active tenant directly.
                visible_ids = list(manager.filter(tenant=active_tenant).values_list("pk", flat=True))
            else:
                # Tenant signal comes ONLY from a `tenant` *property* (not a DB
                # field) with no `tenant_lookup`. Filtering on `tenant=` here
                # would raise FieldError (500). Without a queryable column we
                # cannot scope in the DB, so fall back to the model's default
                # manager — same resolution path the tenant-scoping branch uses
                # — and rely on StrictTenantPermission for the object-level
                # boundary on detail/mutation.
                visible_ids = list(manager.values_list("pk", flat=True))
            allowed |= Q(content_type=ct, object_id__in=visible_ids)

        return qs.filter(allowed)


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
