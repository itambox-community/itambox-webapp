from copy import deepcopy

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.managers import get_current_tenant
from itambox.api.viewsets import ITAMBoxModelViewSet, ITAMBoxReadOnlyModelViewSet
from users.models import Token, UserPreference

from .serializers import (
    GroupSerializer,
    TokenSerializer,
    UserConfigSerializer,
    UserConfigUpdateSerializer,
    UserSerializer,
)

User = get_user_model()


class AuthenticationCheckView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        serializer = UserSerializer(request.user, context={"request": request})
        return Response(serializer.data)


class UserViewSet(ITAMBoxReadOnlyModelViewSet):
    queryset = User.objects.all().prefetch_related("groups")
    serializer_class = UserSerializer

    def get_queryset(self):
        # User has no `tenant` field, so StrictTenantPermission/filter_by_tenant
        # cannot scope this set and the base queryset would expose every tenant's
        # users (incl. is_staff/email) — cross-tenant user enumeration. Scope to
        # users who share the requester's active tenant via the Membership
        # reverse relation (`memberships`). Superusers remain unscoped.
        qs = super().get_queryset()
        if self.request.user.is_superuser:
            return qs
        active_tenant = get_current_tenant()
        if active_tenant is None:
            return qs.none()
        return qs.filter(memberships__tenant=active_tenant).distinct()


class GroupViewSet(ITAMBoxReadOnlyModelViewSet):
    # NOTE: user_count is annotated in get_queryset() (scoped per request), not
    # here — a class-attr Count('user') counts members across ALL tenants and
    # leaks the cross-tenant membership size via the serialized aggregate (L2).
    queryset = Group.objects.all()
    serializer_class = GroupSerializer

    def get_queryset(self):
        # Group has no `tenant` field either; scope to groups that have at least
        # one member in the requester's active tenant. The path Group -> User is
        # the default reverse `user`, then User -> Membership is `memberships`.
        # Superusers remain unscoped; no active tenant fails closed to none().

        qs = super().get_queryset()
        if self.request.user.is_superuser:
            # Global admin: total membership count is acceptable.
            return qs.annotate(user_count=Count("user", distinct=True))
        active_tenant = get_current_tenant()
        if active_tenant is None:
            return qs.none()
        # Count ONLY same-tenant members so the aggregate cannot leak the size of
        # a shared group's membership in other tenants.
        same_tenant = Q(user__memberships__tenant=active_tenant)
        return qs.filter(same_tenant).distinct().annotate(user_count=Count("user", filter=same_tenant, distinct=True))


class TokenViewSet(ITAMBoxModelViewSet):
    serializer_class = TokenSerializer

    def get_queryset(self):
        return Token.objects.select_related("user").filter(user=self.request.user)

    def get_created_response_instance(self, serializer):
        # Token.key is deliberately transient. Keep the saved in-memory object
        # for the 201 response so the plaintext can be returned exactly once.
        return serializer.instance

    def _pin_user(self, serializer):
        # A user must never be able to provision a token bound to another account
        # (privilege escalation). Only superusers may set an explicit `user`;
        # everyone else is pinned to themselves regardless of any supplied user_id.
        if self.request.user.is_superuser and serializer.validated_data.get("user"):
            serializer.save()
        else:
            serializer.save(user=self.request.user)

    def perform_create(self, serializer):
        # Token.tenant is non-nullable, so the shared _tenant_create_kwargs
        # fail-closed guard (which only covers nullable tenant fields) never
        # fires for this viewset. Without an active tenant, Token.save()
        # silently falls back to the first tenant in the database (fail-open).
        # This occurs under a tenant-group scope, where permission
        # checks aggregate over the group subtree but no single tenant anchors
        # the request. There is no "global" token, so fail closed instead.
        if get_current_tenant() is None:
            raise PermissionDenied()
        self._pin_user(serializer)

    def perform_update(self, serializer):
        self._pin_user(serializer)


@extend_schema_view(
    put=extend_schema(request=UserConfigUpdateSerializer),
    patch=extend_schema(request=UserConfigUpdateSerializer),
)
class UserConfigView(RetrieveUpdateAPIView):
    serializer_class = UserConfigSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        preference, _ = UserPreference.objects.get_or_create(user=self.request.user)
        return preference

    def _validate_update(self, request):
        serializer = UserConfigUpdateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        return serializer

    def update(self, request, *args, **kwargs):
        serializer = self._validate_update(request)
        preference = self.get_object()
        preference.data = serializer.validated_data
        preference.save()
        return Response(self.get_serializer(preference).data)

    def partial_update(self, request, *args, **kwargs):
        serializer = self._validate_update(request)
        preference = self.get_object()
        incoming_data = serializer.validated_data
        current_data = deepcopy(preference.data) if isinstance(preference.data, dict) else {}

        if "tables" in incoming_data:
            if not isinstance(current_data.get("tables"), dict):
                current_data["tables"] = {}
            for app_label, models in incoming_data["tables"].items():
                if app_label not in current_data["tables"]:
                    current_data["tables"][app_label] = {}
                for model_name, config in models.items():
                    current_data["tables"][app_label][model_name] = config

        for key, value in incoming_data.items():
            if key != "tables":
                current_data[key] = value

        preference.data = current_data
        preference.save()
        return Response(self.get_serializer(preference).data)
