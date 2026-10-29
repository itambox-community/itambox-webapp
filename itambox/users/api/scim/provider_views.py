import uuid
from collections.abc import Sequence
from typing import cast

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldError as DjangoFieldError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from drf_spectacular.utils import extend_schema_view
from rest_framework import exceptions, status
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission, IsAuthenticated, OperandHolder, SingleOperandHolder
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.managers import set_current_tenant
from itambox.middleware import set_current_user
from organization.models import Membership, Tenant
from users.api.scim import schema as scim_schema
from users.api.scim.filters import SCIMFilterError, parse_scim_filter, parse_scim_membership_filter
from users.api.scim.identifiers import get_scim_object_or_404
from users.api.scim.provider_authentication import SCIMProviderBearerTokenAuthentication
from users.api.scim.provider_patch import (
    SCIMPatchError,
    get_patch_operations,
    parse_external_id,
    parse_group_patch_operations,
    parse_member_ids,
    parse_user_patch_operations,
    parse_user_resource,
    require_object_document,
    validate_display_name,
)
from users.api.scim.provider_services import (
    apply_provider_group_patch,
    apply_provider_user_patch,
    create_provider_group,
    create_scim_membership,
    ensure_provider_group_external_id_available,
    ensure_provider_group_name_available,
    save_provider_group,
    sync_provider_group_members,
    sync_user_global_active,
)
from users.api.scim.serializers import SCIMGroupSerializer, SCIMServiceProviderConfigSerializer, SCIMUserSerializer
from users.models import User as UserModel
from users.models import UserGroup

User = get_user_model()


class SCIMProviderMixin:
    authentication_classes: Sequence[type[BaseAuthentication]] = [SCIMProviderBearerTokenAuthentication]
    permission_classes: Sequence[type[BasePermission] | OperandHolder | SingleOperandHolder] = [IsAuthenticated]

    def require_group_permission(self, request: Request, permission: str) -> None:
        """Enforce the method-specific UserGroup permission in the provider tenant."""
        if not request.user.has_perm(permission, obj=self.tenant):
            raise exceptions.PermissionDenied(f"{permission} is required for this provider SCIM group operation.")

    def handle_exception(self, exc: Exception) -> Response:

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

    def initial(self, request: Request, *args: object, **kwargs: object) -> None:
        super().initial(request, *args, **kwargs)
        provider_slug = self.kwargs.get("provider_slug")
        if not provider_slug:
            raise exceptions.ValidationError("provider_slug is required")

        # A provider is a Tenant with is_provider=True; the /api/providers/<slug>/
        # mount is kept but resolves against the tenant tree. _base_manager: no tenant
        # context exists yet, so the tenant-scoped default manager would return nothing.
        self.tenant = Tenant.objects.filter(
            is_provider=True,
            deleted_at__isnull=True,
            slug=provider_slug,
        ).first()
        if self.tenant is None:
            raise exceptions.NotFound("Provider not found.")

        # Bind the managing tenant as the current tenant (it IS a tenant now) and the
        # token's owner as the current user so SCIM-driven changelog rows are attributed
        # to the acting service account rather than 'System' (CurrentUserMiddleware
        # captured AnonymousUser before DRF auth ran).
        set_current_tenant(self.tenant)
        if getattr(request, "user", None) and request.user.is_authenticated:
            set_current_user(request.user)
        # Wire request-id so SCIM mutations create ObjectChange records.
        # inline import: app-registry: avoid AppRegistryNotReady at module-load time
        from itambox.middleware import _request_id

        _request_id.set(str(uuid.uuid4()))


@extend_schema_view(get=scim_schema.SCIM_PROVIDER_SERVICE_PROVIDER_CONFIG)
class ProviderServiceProviderConfigView(SCIMProviderMixin, APIView):
    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
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


def _lock_provider_scim_user(user):
    """Reload ``user`` under a row lock before a provider lifecycle mutation.

    Same strategy as ``apply_provider_user_patch``: DELETE and the POST reprovision
    sync must observe committed state, not the request-entry object. Call inside
    ``transaction.atomic()``.
    """
    try:
        return type(user).objects.select_for_update().get(pk=user.pk)
    except UserModel.DoesNotExist as exc:
        raise SCIMPatchError("SCIM user was deleted", status_code=404) from exc


@extend_schema_view(
    get=scim_schema.SCIM_PROVIDER_USER_LIST,
    post=scim_schema.SCIM_PROVIDER_USER_CREATE,
)
class SCIMProviderUserListView(SCIMProviderMixin, APIView):
    def _retry_correlated_user(self, username: str, external_id: str) -> UserModel:
        if not external_id:
            raise SCIMPatchError("User already exists", scim_type="uniqueness", status_code=409)
        correlated = (
            Membership.objects.select_related("user").filter(tenant=self.tenant, external_id=external_id).first()
        )
        if correlated is None:
            raise SCIMPatchError(
                "User correlation raced with another resource", scim_type="uniqueness", status_code=409
            )
        if correlated.user.username != username:
            raise SCIMPatchError(
                "externalId already identifies a different user",
                scim_type="uniqueness",
                status_code=409,
            )
        return correlated.user

    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        try:
            q_obj = parse_scim_filter(request.query_params.get("filter"), "user")
        except SCIMFilterError:
            return Response(
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                    "status": "400",
                    "detail": "Invalid SCIM filter.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership_q = parse_scim_membership_filter(request.query_params.get("filter"))
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
        queryset = queryset.prefetch_related(scoped_membership_prefetch)

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
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "tenant": self.tenant,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
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

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        document = require_object_document(request.data)
        patch = parse_user_resource(document)
        username = cast(str, patch.username)
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
                        "detail": "externalId already identifies a different user in this provider",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            user = correlated_membership.user
            response_status = status.HTTP_200_OK
        elif user:
            existing = Membership.objects.filter(user=user, tenant=self.tenant).first()
            if existing:
                if external_id and existing.external_id == external_id:
                    response_status = status.HTTP_200_OK
                else:
                    return Response(
                        {
                            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                            "status": "409",
                            "scimType": "uniqueness",
                            "detail": "User already exists in this provider",
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
            else:
                try:
                    with transaction.atomic():
                        # Serialize with concurrent lifecycle calls on the same identity
                        # (same user-row lock as PATCH).
                        user = _lock_provider_scim_user(user)
                        # SCIM provisions identity only: a bare membership at the managing
                        # tenant with NO RoleGrant rows — zero permissions and zero reach
                        # until granted in-app.
                        create_scim_membership(
                            user=user,
                            tenant_id=self.tenant.pk,
                            is_active=active,
                            external_id=external_id,
                        )
                        # Reprovisioning reconciles the global account flag: an active
                        # re-provision restores the login a full de-provision correctly
                        # cleared, with no manual intervention.
                        sync_user_global_active(user)
                except IntegrityError:
                    user = self._retry_correlated_user(username, external_id or "")
                    with transaction.atomic():
                        user = _lock_provider_scim_user(user)
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
            except IntegrityError:
                user = self._retry_correlated_user(username, external_id or "")
                response_status = status.HTTP_200_OK

        serializer = SCIMUserSerializer(
            user,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "tenant": self.tenant,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=response_status)


@extend_schema_view(
    get=scim_schema.SCIM_PROVIDER_USER_DETAIL,
    put=scim_schema.SCIM_PROVIDER_USER_REPLACE,
    patch=scim_schema.SCIM_PROVIDER_USER_UPDATE,
    delete=scim_schema.SCIM_PROVIDER_USER_DELETE,
)
class SCIMProviderUserDetailView(SCIMProviderMixin, APIView):
    def _staff_queryset(self) -> QuerySet[UserModel]:
        return User.objects.filter(memberships__tenant=self.tenant).distinct()

    def get(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        user = get_scim_object_or_404(self._staff_queryset(), pk)
        serializer = SCIMUserSerializer(
            user,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "tenant": self.tenant,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        document = require_object_document(request.data)
        user = get_scim_object_or_404(self._staff_queryset(), pk)
        patch = parse_user_resource(document)
        user = apply_provider_user_patch(user, self.tenant, patch, actor=request.user)

        serializer = SCIMUserSerializer(
            user,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "tenant": self.tenant,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        user = get_scim_object_or_404(self._staff_queryset(), pk)

        patch = parse_user_patch_operations(get_patch_operations(request.data))
        user = apply_provider_user_patch(user, self.tenant, patch, actor=request.user)

        serializer = SCIMUserSerializer(
            user,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "tenant": self.tenant,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        user = get_scim_object_or_404(self._staff_queryset(), pk)
        with transaction.atomic():
            # Serialize with concurrent lifecycle calls on the same identity (the same
            # user-row lock the provider PATCH service takes).
            user = _lock_provider_scim_user(user)
            # Remove only the membership at this managing tenant. Delete per-instance so
            # each removal is change-logged (QuerySet.delete() bypasses
            # ChangeLoggingMixin). Deleting the membership CASCADEs its RoleGrant
            # rows, revoking own AND managed reach in one stroke.
            for membership in Membership.objects.filter(user=user, tenant=self.tenant):
                membership.delete()
            # Fully de-provisioned user (no memberships anywhere): deactivate the
            # account instead of hard-deleting (same rule as the tenant SCIM path).
            if not Membership.objects.filter(user=user).exists():
                user.is_active = False
                user.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=scim_schema.SCIM_PROVIDER_GROUP_LIST,
    post=scim_schema.SCIM_PROVIDER_GROUP_CREATE,
)
class SCIMProviderGroupListView(SCIMProviderMixin, APIView):
    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        self.require_group_permission(request, "users.view_usergroup")
        queryset = UserGroup.objects.filter(tenant=self.tenant)
        try:
            queryset = queryset.filter(parse_scim_filter(request.query_params.get("filter"), "group")).distinct()
        except SCIMFilterError:
            return Response(
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                    "status": "400",
                    "detail": "Invalid SCIM filter.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

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
            sliced_queryset,
            many=True,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
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

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        self.require_group_permission(request, "users.add_usergroup")
        document = require_object_document(request.data)
        name = document.get("displayName")
        if not name:
            return Response(
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                    "status": "400",
                    "detail": "displayName is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        name = validate_display_name(name)
        external_id = parse_external_id(document.get("externalId"))
        correlated_group = (
            UserGroup.objects.filter(tenant=self.tenant, external_id=external_id).first() if external_id else None
        )
        if correlated_group is not None:
            if correlated_group.name != name:
                return Response(
                    {
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                        "status": "409",
                        "scimType": "uniqueness",
                        "detail": "externalId already identifies a different group in this provider",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            serializer = SCIMGroupSerializer(
                correlated_group,
                context={
                    "request": request,
                    "tenant_slug": self.tenant.slug,
                    "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
                },
            )
            return Response(serializer.data, status=status.HTTP_200_OK)

        ensure_provider_group_name_available(self.tenant, name)
        ensure_provider_group_external_id_available(self.tenant, external_id)
        member_ids = set(parse_member_ids(document.get("members", [])))

        group = create_provider_group(
            self.tenant,
            name,
            member_ids,
            actor=request.user,
            external_id=external_id,
        )

        serializer = SCIMGroupSerializer(
            group,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=scim_schema.SCIM_PROVIDER_GROUP_DETAIL,
    put=scim_schema.SCIM_PROVIDER_GROUP_REPLACE,
    patch=scim_schema.SCIM_PROVIDER_GROUP_UPDATE,
    delete=scim_schema.SCIM_PROVIDER_GROUP_DELETE,
)
class SCIMProviderGroupDetailView(SCIMProviderMixin, APIView):
    def get(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        self.require_group_permission(request, "users.view_usergroup")
        group = get_scim_object_or_404(UserGroup.objects.filter(tenant=self.tenant), pk)
        serializer = SCIMGroupSerializer(
            group,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        self.require_group_permission(request, "users.change_usergroup")
        document = require_object_document(request.data)
        name = document.get("displayName")
        if not name:
            return Response(
                {
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                    "status": "400",
                    "detail": "displayName is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        name = validate_display_name(name)
        external_id = parse_external_id(document.get("externalId"))
        member_ids = set(parse_member_ids(document.get("members", [])))

        with transaction.atomic():
            group = get_scim_object_or_404(
                UserGroup.objects.select_for_update().filter(tenant=self.tenant),
                pk,
            )
            ensure_provider_group_name_available(self.tenant, name, exclude_pk=group.pk)
            ensure_provider_group_external_id_available(self.tenant, external_id, exclude_pk=group.pk)
            group.name = name
            group.external_id = external_id
            save_provider_group(group, self.tenant, actor=request.user)
            sync_provider_group_members(
                self.tenant,
                group,
                member_ids,
                actor=request.user,
            )

        serializer = SCIMGroupSerializer(
            group,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        self.require_group_permission(request, "users.change_usergroup")
        group = get_scim_object_or_404(UserGroup.objects.filter(tenant=self.tenant), pk)

        patch = parse_group_patch_operations(get_patch_operations(request.data))
        group = apply_provider_group_patch(
            self.tenant,
            group,
            patch,
            actor=request.user,
        )

        serializer = SCIMGroupSerializer(
            group,
            context={
                "request": request,
                "tenant_slug": self.tenant.slug,
                "scim_base_path": f"/api/providers/{self.tenant.slug}/scim/v2",
            },
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request: Request, pk: str, *args: object, **kwargs: object) -> Response:
        self.require_group_permission(request, "users.delete_usergroup")
        group = get_scim_object_or_404(UserGroup.objects.filter(tenant=self.tenant), pk)
        with transaction.atomic():
            # Soft-delete via the model's delete() for change-logging.
            group.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
