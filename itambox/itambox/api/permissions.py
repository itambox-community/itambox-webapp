from django.conf import settings
from django.http import Http404
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.permissions import SAFE_METHODS, BasePermission

from core.managers import get_current_all_accessible, get_current_tenant, get_current_tenant_group
from core.tenant_access import accessible_tenant_ids, shared_stock_read_allowed


class IsSuperuser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_superuser)


class IsSuperuserOrReadOnly(BasePermission):
    """Allow normal read authorization, but reserve mutations for superusers."""

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or bool(request.user and request.user.is_superuser)


class TokenPermissions(BasePermission):
    perms_map = {
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": [],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }

    def _queryset(self, view):
        assert hasattr(view, "get_queryset") or hasattr(view, "queryset"), (
            "Cannot apply {} on a view that does not set `.queryset` or have a `.get_queryset()` method."
        ).format(self.__class__.__name__)

        if hasattr(view, "get_queryset"):
            queryset = view.get_queryset()
            assert queryset is not None, "{}.get_queryset() returned None".format(view.__class__.__name__)
            return queryset
        return view.queryset

    def get_required_permissions(self, method, model):
        kwargs = {"app_label": model._meta.app_label, "model_name": model._meta.model_name}

        if method not in self.perms_map:
            raise MethodNotAllowed(method)

        return [perm % kwargs for perm in self.perms_map[method]]

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        qs = self._queryset(view)
        perms = self.get_required_permissions(request.method, qs.model)

        if not perms:
            return True

        # Tenant selection belongs to TenantMiddleware or TokenAuthentication.
        # Permission evaluation must never silently choose the user's first
        # membership: that makes an unbound request's authorization depend on
        # database ordering and can stomp an intentional tenant-group scope.

        tenant = get_current_tenant()
        token_tenant_id = getattr(getattr(request, "auth", None), "tenant_id", None)
        if token_tenant_id is not None:
            # Token requests are single-tenant and must remain pinned to the
            # authenticated token's tenant for the complete request.
            if (
                tenant is None
                or tenant.pk != token_tenant_id
                or getattr(request.auth, "user_id", None) != request.user.pk
            ):
                return False
        elif (
            not request.user.is_superuser
            and tenant is None
            and get_current_tenant_group() is None
            and not get_current_all_accessible()
        ):
            # A session request carries exactly one canonical scope: a single
            # tenant, a tenant group, or the "All accessible tenants" aggregate.
            # None of them active for a non-superuser means the request is
            # unbound — fail closed. All-accessible is a bound scope (not the
            # superuser global view): objectless mutation permissions stay
            # read-only under it in the membership backend, so recognising it
            # here restores its reads without widening write access.
            return False

        return request.user.has_perms(perms)

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False

        qs = self._queryset(view)
        perms = self.get_required_permissions(request.method, qs.model)

        if not perms:
            return True

        # Pass the object so the permission check resolves against the object's own
        # tenant (MembershipBackend._resolve_tenant), making it self-sufficient
        # rather than relying on StrictTenantPermission to catch a tenant mismatch.
        if request.user.has_perms(perms, obj):
            return True
        # Shared pools and recipient-side assignments are
        # READABLE across the boundary — the view permission is then checked
        # in the ACTIVE tenant instead of the (foreign) object tenant.
        if request.method in SAFE_METHODS:
            tenant = get_current_tenant()
            if tenant is not None and StrictTenantPermission._shared_read_allowed(
                obj,
                tenant,
                request.user,
                perms[0],
            ):
                return request.user.has_perms(perms)
        return False


class IsAuthenticatedOrLoginNotRequired(BasePermission):
    def has_permission(self, request, view):
        if not getattr(settings, "LOGIN_REQUIRED", True):
            return True
        return request.user.is_authenticated


class StrictTenantPermission(BasePermission):
    """
    Strict Tenant Security Boundary. Enforces that requests targeting
    specific resources belong strictly to the user's assigned Tenant scope.
    Raises Http404 on violation to prevent primary key enumeration.
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True

        user_tenant = getattr(request, "active_tenant", None) or get_current_tenant()

        if user_tenant is None:
            # No single active tenant. Under the canonical multi-tenant read
            # scopes (tenant group / All accessible tenants) the boundary is the
            # actor's authorized tenant SET, resolved by the same canonical model
            # the scoped managers use — never an AssetHolder profile, which is a
            # domain profile, not an authorization source. With no
            # authorized scope active, fail closed (hidden as 404 to avoid pk
            # enumeration).
            if get_current_tenant_group() is not None or get_current_all_accessible():
                return self._has_multi_tenant_object_permission(request, obj)

            raise Http404()

        if hasattr(obj, "tenant"):
            obj_tenant = obj.tenant
            if obj_tenant is None:
                # Global (tenant=None) objects are READABLE but not mutable by
                # non-superusers.  Hide the mutation as a 404 to avoid leaking
                # the object's existence.
                if request.method not in SAFE_METHODS:
                    raise Http404()
                return True
            # Enforce boundary: Object's tenant must match user's tenant
            if obj_tenant != user_tenant:
                # Two READ-ONLY cross-tenant exceptions —
                # a stock pool shared to the active tenant by a live grant,
                # and an assignment whose TARGET is the active tenant (the
                # recipient side of a granted checkout). Never mutation.
                if request.method in SAFE_METHODS and self._shared_read_allowed(
                    obj,
                    user_tenant,
                    request.user,
                ):
                    return True

                raise Http404()

        return True

    def _has_multi_tenant_object_permission(self, request, obj):
        """Object tenant boundary under a multi-tenant scope (tenant group /
        All accessible tenants), where no single tenant anchors the request.

        ``get_object()`` already constrained the object to the actor's authorized
        tenants through the tenant-scoping queryset; this re-checks the same
        boundary as defense in depth by intersecting the object's OWN tenant with
        the canonical accessible set (``has_perms(obj=...)`` in TokenPermissions
        independently enforces the permission against that same object tenant, so
        object-tenant authorization stays authoritative). Global (tenant=None)
        rows remain readable but not mutable by non-superusers. The aggregate
        scopes never surface cross-tenant shared pools, so a foreign-tenant
        object fails closed here.
        """

        if not hasattr(obj, "tenant"):
            return True
        obj_tenant = obj.tenant
        if obj_tenant is None:
            if request.method not in SAFE_METHODS:
                raise Http404()
            return True
        if obj_tenant.pk in accessible_tenant_ids(request.user):
            return True
        raise Http404()

    @staticmethod
    def _shared_read_allowed(obj, user_tenant, user, perm=None):
        if shared_stock_read_allowed(obj, user_tenant, user, perm):
            return True
        target_tenant_id = getattr(obj, "target_tenant_id", None)
        return target_tenant_id is not None and target_tenant_id == user_tenant.pk
