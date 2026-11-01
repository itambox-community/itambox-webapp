import logging
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldError as DjangoFieldError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q
from drf_spectacular.utils import extend_schema_view
from rest_framework import exceptions, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.managers import set_current_tenant
from itambox.middleware import set_current_user
from organization.models import AssetHolder, Membership, Tenant
from organization.services.identity_provisioning import link_or_create_holder
from users.api.scim import schema as scim_schema
from users.api.scim.authentication import SCIMBearerTokenAuthentication
from users.api.scim.filters import SCIMFilterError, parse_scim_filter, parse_scim_membership_filter
from users.api.scim.identifiers import get_scim_object_or_404
from users.api.scim.provider_patch import (
    UNSET,
    SCIMPatchError,
    get_patch_operations,
    parse_user_patch_operations,
    parse_user_resource,
    require_object_document,
)
from users.api.scim.provider_services import create_scim_membership, sync_user_global_active
from users.api.scim.serializers import SCIMGroupSerializer, SCIMServiceProviderConfigSerializer, SCIMUserSerializer
from users.models import UserGroup

logger = logging.getLogger("itambox.scim.views")
User = get_user_model()


def _save_scim_external_id(user, tenant, external_id):
    if external_id is UNSET:
        return
    membership = Membership.objects.filter(user=user, tenant=tenant).first()
    if membership is None or membership.external_id == external_id:
        return
    membership.external_id = external_id
    try:
        with transaction.atomic():
            membership.save(update_fields=["external_id"])
    except IntegrityError as exc:
        conflict = exceptions.APIException("externalId is already used in this tenant.")
        conflict.status_code = status.HTTP_409_CONFLICT
        conflict.scim_type = "uniqueness"
        raise conflict from exc


def _tenant_conflict(detail):
    conflict = exceptions.APIException(detail)
    conflict.status_code = status.HTTP_409_CONFLICT
    conflict.scim_type = "uniqueness"
    return conflict


def _retry_tenant_correlated_user(tenant, username, external_id):
    if not external_id:
        raise _tenant_conflict("User already exists in this tenant")
    correlated = Membership.objects.select_related("user").filter(tenant=tenant, external_id=external_id).first()
    if correlated is None or correlated.user.username != username:
        raise _tenant_conflict("externalId already identifies a different user in this tenant")
    return correlated.user


def _lock_tenant_scim_user(user, tenant, *, require_membership=True):
    """Reload ``user`` under a row lock before any tenant lifecycle mutation.

    Serializes concurrent lifecycle calls on the same identity the way the provider
    mount does: the request-entry object must never be trusted for the global-flag
    mirror, or a racing deactivate/reactivate pair can converge on a state neither
    request asked for. Call inside ``transaction.atomic()``.
    """
    try:
        locked = type(user).objects.select_for_update().get(pk=user.pk)
    except User.DoesNotExist as exc:
        raise SCIMPatchError("SCIM user was deleted", status_code=404) from exc
    if require_membership and not Membership.objects.filter(user=locked, tenant=tenant).exists():
        raise SCIMPatchError("SCIM user is not a member of this tenant", status_code=404)
    return locked


def _link_scim_holder(user, tenant):
    email = (user.email or "").strip()
    upn = user.username
    return link_or_create_holder(
        user=user,
        tenant_id=tenant.pk,
        upn=upn,
        email=email,
        first_name=user.first_name or user.username,
        last_name=user.last_name or "",
        source="SCIM",
    )


class SCIMTenantMixin:
    authentication_classes = [SCIMBearerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):

        # Both SCIM mounts answer client errors with the SCIM error envelope: capture
        # the shared parser's scimType/status before converting to a DRF exception.
        scim_type = getattr(exc, "scim_type", None)
        scim_status = getattr(exc, "status_code", status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SCIMPatchError):
            exc = exceptions.APIException(detail=str(exc))
            exc.status_code = scim_status
        elif isinstance(exc, DjangoValidationError):
            exc = exceptions.ValidationError(detail=exc.message_dict if hasattr(exc, "message_dict") else exc.messages)
        elif isinstance(exc, DjangoFieldError):
            exc = exceptions.ValidationError(detail=str(exc))

        response = super().handle_exception(exc)
        if response is not None:
            detail = response.data.get("detail") if isinstance(response.data, dict) else str(response.data)
            response.data = {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                "status": str(response.status_code),
                "detail": detail,
            }
            if scim_type is not None:
                response.data["scimType"] = scim_type
        return response

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        tenant_slug = self.kwargs.get("tenant_slug")
        if not tenant_slug:
            raise exceptions.ValidationError("tenant_slug is required")

        try:
            self.tenant = Tenant.objects.get(slug=tenant_slug, is_provider=False, deleted_at__isnull=True)
        except Tenant.DoesNotExist:
            raise exceptions.NotFound("Tenant not found.") from None

        set_current_tenant(self.tenant)
        # DRF authenticated the bearer token in super().initial(); bind the token's
        # owner as the current user so SCIM-driven changelog rows are attributed to
        # the acting service account rather than 'System' (CurrentUserMiddleware
        # captured AnonymousUser before DRF auth ran).
        if getattr(request, "user", None) and request.user.is_authenticated:
            set_current_user(request.user)
        # Ensure ObjectChange records are created for SCIM mutations by wiring
        # a request-id contextvar because SCIM bypasses CurrentUserMiddleware.
        # inline import: app-registry: avoid AppRegistryNotReady at module-load time
        from itambox.middleware import _request_id

        _request_id.set(str(uuid.uuid4()))


@extend_schema_view(get=scim_schema.SCIM_TENANT_SERVICE_PROVIDER_CONFIG)
class ServiceProviderConfigView(SCIMTenantMixin, APIView):
    def get(self, request, *args, **kwargs):
        config_data = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 1000, "maxPayloadSize": 1048576},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "name": "OAuth Bearer Token",
                    "description": "External identity provisioning via Bearer Token",
                    "specUri": "http://tools.ietf.org/html/rfc6750",
                    "type": "oauthbearertoken",
                    "primary": True,
                },
            ],
        }
        serializer = SCIMServiceProviderConfigSerializer(data=config_data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema_view(
    get=scim_schema.SCIM_TENANT_USER_LIST,
    post=scim_schema.SCIM_TENANT_USER_CREATE,
)
class SCIMUserListView(SCIMTenantMixin, APIView):
    def get(self, request, *args, **kwargs):
        filter_str = request.query_params.get("filter")
        try:
            q_obj = parse_scim_filter(filter_str, "user")
        except SCIMFilterError as e:
            # Log the parser detail server-side; return a generic client message
            # so the raw (echoed) filter expression is not reflected back.
            logger.warning("Rejected SCIM filter: %s", e)
            return Response(
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                    "status": "400",
                    "detail": "Invalid SCIM filter.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership_q = parse_scim_membership_filter(filter_str)
        scoped_membership_prefetch = Prefetch(
            "memberships",
            queryset=Membership.objects.filter(tenant=self.tenant),
            to_attr="_scim_memberships",
        )
        if membership_q is not None:
            scoped_memberships = Membership.objects.filter(user=OuterRef("pk"), tenant=self.tenant).filter(membership_q)
            queryset = (
                User.objects.filter(memberships__tenant=self.tenant).filter(Exists(scoped_memberships)).distinct()
            )
        else:
            queryset = User.objects.filter(Q(memberships__tenant=self.tenant) & q_obj).distinct()
        queryset = queryset.prefetch_related("groups", scoped_membership_prefetch)

        try:
            start_index = int(request.query_params.get("startIndex", 1))
        except ValueError:
            start_index = 1
        try:
            count = int(request.query_params.get("count", 50))
        except ValueError:
            count = 50
        count = min(count, 200)  # Enforce maxResults upper bound

        if start_index < 1:
            start_index = 1

        total_results = queryset.count()
        sliced_queryset = queryset[start_index - 1 : start_index - 1 + count]

        serializer = SCIMUserSerializer(
            sliced_queryset,
            many=True,
            context={"request": request, "tenant_slug": self.tenant.slug, "tenant": self.tenant},
        )

        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
                "totalResults": total_results,
                "itemsPerPage": len(serializer.data),
                "startIndex": start_index,
                "Resources": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, *args, **kwargs):
        # The tenant mount shares the provider mount's strict user-document parser:
        # one frozen request subset, one validation path, one error envelope.
        document = require_object_document(request.data)
        patch = parse_user_resource(document)
        username = patch.username
        email = patch.email
        first_name = patch.first_name
        last_name = patch.last_name
        active = patch.active if isinstance(patch.active, bool) else True
        external_id = patch.external_id if isinstance(patch.external_id, str) else None
        user = User.objects.filter(username=username).first()
        correlated_membership = (
            Membership.objects.select_related("user").filter(tenant=self.tenant, external_id=external_id).first()
            if external_id
            else None
        )
        response_status = status.HTTP_201_CREATED

        if correlated_membership:
            if correlated_membership.user.username != username:
                return Response(
                    {
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                        "status": "409",
                        "scimType": "uniqueness",
                        "detail": "externalId already identifies a different user in this tenant",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            user = correlated_membership.user
            response_status = status.HTTP_200_OK
        elif user:
            membership = Membership.objects.filter(user=user, tenant=self.tenant).first()
            if membership:
                if external_id and membership.external_id == external_id:
                    response_status = status.HTTP_200_OK
                else:
                    return Response(
                        {
                            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                            "status": "409",
                            "scimType": "uniqueness",
                            "detail": "User already exists in this tenant",
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
            else:
                try:
                    with transaction.atomic():
                        # Serialize with concurrent lifecycle calls on the same identity
                        # (same user-row lock as PUT/PATCH).
                        user = _lock_tenant_scim_user(user, self.tenant, require_membership=False)
                        # SCIM provisions identity only: a bare membership with NO RoleGrant
                        # rows — permissions are granted in-app.
                        create_scim_membership(
                            user=user,
                            tenant_id=self.tenant.pk,
                            is_active=active,
                            external_id=external_id,
                        )
                        _link_scim_holder(user, self.tenant)
                        # Reprovisioning reconciles the global account flag: an active
                        # re-provision restores the login a full de-provision correctly
                        # cleared, with no manual intervention.
                        sync_user_global_active(user)
                except IntegrityError:
                    user = _retry_tenant_correlated_user(self.tenant, username, external_id)
                    with transaction.atomic():
                        user = _lock_tenant_scim_user(user, self.tenant, require_membership=False)
                        sync_user_global_active(user)
                    response_status = status.HTTP_200_OK
        else:
            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=username,
                        email=email,
                        first_name=first_name,
                        last_name=last_name,
                        is_active=active,
                    )
                    user.set_unusable_password()
                    user.save()

                    # See comment above: bare membership, assignments granted in-app.
                    create_scim_membership(
                        user=user,
                        tenant_id=self.tenant.pk,
                        is_active=active,
                        external_id=external_id,
                    )
                    _link_scim_holder(user, self.tenant)
            except IntegrityError:
                user = _retry_tenant_correlated_user(self.tenant, username, external_id)
                response_status = status.HTTP_200_OK

        serializer = SCIMUserSerializer(
            user, context={"request": request, "tenant_slug": self.tenant.slug, "tenant": self.tenant}
        )
        return Response(serializer.data, status=response_status)


@extend_schema_view(
    get=scim_schema.SCIM_TENANT_USER_DETAIL,
    put=scim_schema.SCIM_TENANT_USER_REPLACE,
    patch=scim_schema.SCIM_TENANT_USER_UPDATE,
    delete=scim_schema.SCIM_TENANT_USER_DELETE,
)
class SCIMUserDetailView(SCIMTenantMixin, APIView):
    def get(self, request, pk, *args, **kwargs):
        user = get_scim_object_or_404(User.objects.filter(memberships__tenant=self.tenant).distinct(), pk)
        serializer = SCIMUserSerializer(
            user, context={"request": request, "tenant_slug": self.tenant.slug, "tenant": self.tenant}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _apply_scim_identity(self, user, patch):
        """Apply a parsed SCIM user document/patch to the tenant's User row.

        The tenant mount shares the provider mount's strict parser, so ``patch``
        carries only the attributes the request supplied (``UNSET`` otherwise)
        from the one frozen request subset both mounts accept.

        A SCIM token is bound to exactly one tenant. Concurrent lifecycle calls
        serialize on the user row: the request-entry object is replaced by a fresh
        ``select_for_update()`` reload (the same strategy the provider mount applies),
        so the global-flag mirror always recomputes from membership state this request
        actually observed.

        - ``active`` is applied PER-TENANT: it (de)activates this tenant's membership only.
          A multi-tenant user is therefore never globally locked out by one tenant's token
          (which would deny access in the other tenant). The global ``User.is_active`` is
          reconciled to mirror whether the user has any active membership left, so a fully
          de-provisioned user can no longer authenticate at all. Access gates
          (MembershipBackend, TenantMiddleware) honour the membership flag, so an
          ``active=false`` here genuinely revokes access in this tenant — unlike before,
          when it was silently dropped for shared users.
        - identity (username/email/name) must NEVER be rewritten for a user who is also a
          member of another tenant — that is a cross-tenant write on a shared principal
          (it would hijack their identity in the other tenant). Those changes apply only to
          a user whose sole membership is this tenant, and collide as a SCIM ``uniqueness``
          conflict instead of surfacing as a server error. (DELETE still drops the membership.)
        """
        user = _lock_tenant_scim_user(user, self.tenant)
        has_other = Membership.objects.filter(user=user).exclude(tenant=self.tenant).exists()

        if patch.active is not UNSET:
            membership = Membership.objects.filter(user=user, tenant=self.tenant).first()
            if membership is not None and membership.is_active != patch.active:
                membership.is_active = patch.active
                membership.save(update_fields=["is_active"])
            sync_user_global_active(user)

        _save_scim_external_id(user, self.tenant, patch.external_id)

        if has_other:
            # Keep this tenant's AssetHolder linked, but leave the shared global identity alone.
            _link_scim_holder(user, self.tenant)
            return user

        # Sole-tenant user: the global identity is safe to update.
        identity_fields = []
        for field_name in ("username", "email", "first_name", "last_name"):
            value = getattr(patch, field_name)
            if value is not UNSET:
                setattr(user, field_name, value)
                identity_fields.append(field_name)
        if identity_fields:
            try:
                user.save(update_fields=identity_fields)
            except IntegrityError as exc:
                conflict = exceptions.APIException("User identity conflicts with an existing user")
                conflict.status_code = status.HTTP_409_CONFLICT
                conflict.scim_type = "uniqueness"
                raise conflict from exc
        _link_scim_holder(user, self.tenant)
        return user

    def put(self, request, pk, *args, **kwargs):
        document = require_object_document(request.data)
        user = get_scim_object_or_404(User.objects.filter(memberships__tenant=self.tenant).distinct(), pk)
        patch = parse_user_resource(document)
        with transaction.atomic():
            user = self._apply_scim_identity(user, patch)

        serializer = SCIMUserSerializer(
            user, context={"request": request, "tenant_slug": self.tenant.slug, "tenant": self.tenant}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, pk, *args, **kwargs):
        user = get_scim_object_or_404(User.objects.filter(memberships__tenant=self.tenant).distinct(), pk)
        # Strict, shared parse: unsupported operations and paths are rejected with a
        # SCIM error instead of being silently dropped (one frozen policy both mounts).
        patch = parse_user_patch_operations(get_patch_operations(request.data))
        with transaction.atomic():
            user = self._apply_scim_identity(user, patch)

        serializer = SCIMUserSerializer(
            user, context={"request": request, "tenant_slug": self.tenant.slug, "tenant": self.tenant}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, pk, *args, **kwargs):
        user = get_scim_object_or_404(User.objects.filter(memberships__tenant=self.tenant).distinct(), pk)
        with transaction.atomic():
            # Same serialization as PUT/PATCH: lock the user row before touching the
            # membership so a racing reactivation cannot interleave with this removal.
            user = _lock_tenant_scim_user(user, self.tenant, require_membership=False)
            # Remove only the membership for the current tenant. Delete per-instance
            # so each removal is change-logged (QuerySet.delete() bypasses
            # ChangeLoggingMixin / SoftDeleteMixin entirely).
            for membership in Membership.objects.filter(user=user, tenant=self.tenant):
                membership.delete()
            # Revoke the login association but retain the holder as the durable
            # owner of every outstanding assignment and offboarding obligation.
            for holder in AssetHolder.objects.select_for_update().filter(user=user, tenant=self.tenant).order_by("pk"):
                holder.user = None
                holder.save(update_fields=["user", "updated_at"])
            # If user has no remaining memberships, deactivate instead of hard-deleting
            if not Membership.objects.filter(user=user).exists():
                user.is_active = False
                user.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=scim_schema.SCIM_TENANT_GROUP_LIST,
    post=scim_schema.SCIM_TENANT_GROUP_CREATE,
)
class SCIMGroupListView(SCIMTenantMixin, APIView):
    def get(self, request, *args, **kwargs):
        if not request.user.has_perm("users.view_usergroup", obj=self.tenant):
            raise exceptions.PermissionDenied("users.view_usergroup is required for this tenant SCIM group operation.")
        filter_str = request.query_params.get("filter")
        try:
            q_obj = parse_scim_filter(filter_str, "group")
        except SCIMFilterError as e:
            # Log the parser detail server-side; return a generic client message
            # so the raw (echoed) filter expression is not reflected back.
            logger.warning("Rejected SCIM filter: %s", e)
            return Response(
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                    "status": "400",
                    "detail": "Invalid SCIM filter.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Tenant SCIM exposes only groups owned by this tenant. Provider groups
        # projected here remain private to the provider administration surface.
        queryset = UserGroup.objects.filter(tenant=self.tenant).filter(q_obj)

        try:
            start_index = int(request.query_params.get("startIndex", 1))
        except ValueError:
            start_index = 1
        try:
            count = int(request.query_params.get("count", 50))
        except ValueError:
            count = 50
        count = min(count, 200)  # Enforce maxResults upper bound

        if start_index < 1:
            start_index = 1

        total_results = queryset.count()
        sliced_queryset = queryset[start_index - 1 : start_index - 1 + count]

        serializer = SCIMGroupSerializer(
            sliced_queryset, many=True, context={"request": request, "tenant_slug": self.tenant.slug}
        )

        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
                "totalResults": total_results,
                "itemsPerPage": len(serializer.data),
                "startIndex": start_index,
                "Resources": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, *args, **kwargs):
        # Tenant SCIM exposes owned groups read-only; group authorization stays an
        # explicit in-app administrative operation.
        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                "status": "403",
                "detail": "User groups cannot be created via tenant SCIM.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )


@extend_schema_view(
    get=scim_schema.SCIM_TENANT_GROUP_DETAIL,
    put=scim_schema.SCIM_TENANT_GROUP_REPLACE,
    patch=scim_schema.SCIM_TENANT_GROUP_UPDATE,
    delete=scim_schema.SCIM_TENANT_GROUP_DELETE,
)
class SCIMGroupDetailView(SCIMTenantMixin, APIView):
    def get(self, request, pk, *args, **kwargs):
        if not request.user.has_perm("users.view_usergroup", obj=self.tenant):
            raise exceptions.PermissionDenied("users.view_usergroup is required for this tenant SCIM group operation.")
        group = get_scim_object_or_404(
            UserGroup.objects.filter(tenant=self.tenant),
            pk,
        )
        serializer = SCIMGroupSerializer(group, context={"request": request, "tenant_slug": self.tenant.slug})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request, pk, *args, **kwargs):
        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                "status": "403",
                "detail": "User groups cannot be modified via tenant SCIM.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    def patch(self, request, pk, *args, **kwargs):
        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                "status": "403",
                "detail": "User groups cannot be modified via tenant SCIM.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    def delete(self, request, pk, *args, **kwargs):
        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                "status": "403",
                "detail": "User groups cannot be deleted via tenant SCIM.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
