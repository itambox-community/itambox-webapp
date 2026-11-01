import logging
import re
from typing import Protocol

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.request import Request

from itambox.ratelimit import get_client_ip
from organization.models import Tenant
from users.models import Token

logger = logging.getLogger("itambox.scim.auth")


class _SCIMAuthenticatedPrincipal(Protocol):
    """Minimal principal contract returned by DRF authentication."""

    is_authenticated: bool


_SCIMAuthenticationResult = tuple[_SCIMAuthenticatedPrincipal, object] | None


class SCIMProviderBearerTokenAuthentication(BaseAuthentication):
    """Bearer-token auth for the provider-level (staff) SCIM endpoint.

    Mirrors ``SCIMBearerTokenAuthentication`` but resolves the managing
    (``is_provider``) tenant from the ``/api/providers/<slug>/scim/`` mount. A
    provider is just a tenant now, so authorization is the SAME standard perm the
    tenant SCIM path uses: the token must be scoped to this provider tenant AND the
    token's user must hold ``organization.change_membership`` inside it (superusers
    bypass both). Writes additionally require ``token.write_enabled``.
    """

    def authenticate(self, request: Request) -> _SCIMAuthenticationResult:
        # 1. Resolve the managing (is_provider) tenant from the URL path. The
        #    /api/providers/<slug>/ mount is kept for stage 2; only its resolution
        #    target changed (Provider model is gone).
        provider_slug = None
        if request.resolver_match and "provider_slug" in request.resolver_match.kwargs:
            provider_slug = request.resolver_match.kwargs["provider_slug"]
        else:
            match = re.search(r"/api/providers/([^/]+)/scim/v2/", request.path)
            if match:
                provider_slug = match.group(1)

        if not provider_slug:
            return None

        # _base_manager: token auth runs before any tenant context exists, so the
        # tenant-scoped default manager would silently return nothing here.
        tenant = Tenant._base_manager.filter(
            is_provider=True,
            deleted_at__isnull=True,
            slug=provider_slug,
        ).first()
        if tenant is None:
            raise exceptions.AuthenticationFailed("Provider not found.")

        auth = get_authorization_header(request).split()

        # Bearer Auth
        if auth and auth[0].lower() == b"bearer":
            if len(auth) == 1:
                raise exceptions.AuthenticationFailed("Invalid token header. No credentials provided.")
            elif len(auth) > 2:
                raise exceptions.AuthenticationFailed("Invalid token header. Token string should not contain spaces.")

            try:
                token_key = auth[1].decode()
            except UnicodeError:
                raise exceptions.AuthenticationFailed(
                    "Invalid token header. Token string should not contain invalid characters."
                ) from None

            token = Token.find_by_key(token_key)
            if token is None:
                raise exceptions.AuthenticationFailed("Invalid token.")

            if token.is_expired:
                raise exceptions.AuthenticationFailed("Token expired.")

            if token.allowed_ips:
                client_ip = get_client_ip(request)
                if not token.validate_client_ip(client_ip):
                    logger.warning(
                        "SCIM provider token %s... rejected: source IP %s not in allowed_ips", token_key[:6], client_ip
                    )
                    raise exceptions.AuthenticationFailed("Source IP address is not permitted to use this token.")

            user = token.user
            if not user.is_active:
                raise exceptions.AuthenticationFailed("User inactive or deleted.")

            # Authorization: superusers always pass. Everyone else must BOTH (a) present
            # a token scoped to THIS provider tenant (a "provider SCIM token" is simply a
            # token whose tenant has is_provider set) and (b) hold the standard
            # organization.change_membership permission inside it — the same real
            # has_perm check the tenant SCIM path performs, resolved against role
            # content, never a role-name match. Fail closed otherwise.
            if token.tenant_id != tenant.pk:
                raise exceptions.AuthenticationFailed("Token is not scoped to this provider.")
            if not user.is_superuser and not user.has_perm("organization.change_membership", obj=tenant):
                raise exceptions.AuthenticationFailed(
                    "User does not have sufficient permissions (organization.change_membership required)."
                )

            # Enforce write_enabled token flag for write methods
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if not token.write_enabled:
                    raise exceptions.AuthenticationFailed("Token does not have write permissions.")

            # Update last_used
            if not token.last_used or (timezone.now() - token.last_used).total_seconds() > 60:
                Token.objects.filter(pk=token.pk).update(last_used=timezone.now())

            return (user, token)

        return None

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"


class SCIMProviderBearerTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = SCIMProviderBearerTokenAuthentication
    name = "SCIMProviderBearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque SCIM token",
            "description": "SCIM provider authentication using the Bearer HTTP authorization scheme.",
        }
