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


class SCIMBearerTokenAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> _SCIMAuthenticationResult:
        # 1. Resolve tenant from URL path
        tenant_slug = None
        if request.resolver_match and "tenant_slug" in request.resolver_match.kwargs:
            tenant_slug = request.resolver_match.kwargs["tenant_slug"]
        else:
            match = re.search(r"/api/tenants/([^/]+)/scim/v2/", request.path)
            if match:
                tenant_slug = match.group(1)

        if not tenant_slug:
            return None

        tenant = Tenant._base_manager.filter(
            slug=tenant_slug,
            is_provider=False,
            deleted_at__isnull=True,
        ).first()
        if tenant is None:
            # Do not disclose whether a tenant slug exists to anonymous callers.
            raise exceptions.AuthenticationFailed("Authentication credentials were not provided.")

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
                        "SCIM token %s... rejected: source IP %s not in allowed_ips", token_key[:6], client_ip
                    )
                    raise exceptions.AuthenticationFailed("Source IP address is not permitted to use this token.")

            user = token.user
            if not user.is_active:
                raise exceptions.AuthenticationFailed("User inactive or deleted.")

            # Authorization: superusers always pass. Everyone else must BOTH (a) present a
            # token scoped to THIS tenant and (b) hold the organization.change_membership
            # permission for it — a real has_perm check against the role's actual JSON
            # permissions, never a Role.name string match (that magic-string pattern was a
            # backdoor: any role literally named 'admin'/'owner' granted access regardless
            # of its permissions). Fail closed otherwise.
            if token.tenant_id != tenant.pk:
                raise exceptions.AuthenticationFailed("Token is not scoped to this tenant.")
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


class SCIMBearerTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = SCIMBearerTokenAuthentication
    name = "SCIMBearerAuth"

    def get_security_definition(self, auto_schema: object) -> dict[str, str]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque SCIM token",
            "description": "SCIM tenant authentication using the Bearer HTTP authorization scheme.",
        }
