import logging
from dataclasses import dataclass
from typing import Protocol

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.utils import import_from_settings
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView, OIDCAuthenticationRequestView

from core import identity_provisioning, tenant_scope
from core.auth.providers import is_usable_oidc_config
from core.context import get_current_request_id, get_current_user
from core.errors import (
    FailureDisposition,
    IntegrationAuthenticationError,
    IntegrationConfigurationError,
    IntegrationContext,
)
from core.managers import get_current_tenant, set_current_tenant
from core.oidc_identity import (
    oidc_advisory_lock_parts,
    oidc_sensitive_audit,
    validate_oidc_identity,
)

logger = logging.getLogger(__name__)


class OIDCConfigurationError(IntegrationConfigurationError, ImproperlyConfigured):
    code = "oidc.configuration"
    user_message = "OIDC configuration is incomplete or invalid."


class OIDCTokenValidationError(IntegrationAuthenticationError, SuspiciousOperation):
    code = "oidc.token_validation"
    user_message = "OIDC token validation failed."


class OIDCTokenConfigurationError(IntegrationConfigurationError, SuspiciousOperation):
    code = "oidc.token_configuration"
    user_message = "OIDC token validation is not configured securely."


class OIDCIdentityBindingRequiredError(IntegrationAuthenticationError, SuspiciousOperation):
    code = "oidc.identity_binding_required"
    user_message = "OIDC identity requires an explicit operator binding."


class OIDCIdentityProvisioningError(IntegrationAuthenticationError, SuspiciousOperation):
    code = "oidc.identity_provisioning"
    disposition = FailureDisposition.RETRYABLE
    user_message = "OIDC identity provisioning could not be completed."


@dataclass(frozen=True, slots=True)
class VerifiedOIDCIdentity:
    issuer: str
    subject: str


class VerifiedOIDCResolver(Protocol):
    def _resolve_identity_phase_a(
        self, identity: VerifiedOIDCIdentity, claims: dict[str, object]
    ) -> tuple[int, int] | None:
        """Resolve or create the canonical binding in phase A."""

    def _finish_identity_phase_b(
        self,
        binding_id: int,
        expected_user_id: int,
        claims: dict[str, object],
    ) -> object:
        """Finish profile and Organization provisioning in phase B."""


def resolve_verified_oidc_identity(
    resolver: VerifiedOIDCResolver,
    identity: VerifiedOIDCIdentity,
    claims: dict[str, object],
) -> object | None:
    resolved = resolver._resolve_identity_phase_a(identity, claims)
    if resolved is None:
        return None
    binding_id, user_id = resolved
    return resolver._finish_identity_phase_b(binding_id, user_id, claims)


OIDC_LOCK_TIMEOUT = "2000ms"
OIDC_STATEMENT_TIMEOUT = "10000ms"
MAX_USERNAME_ALLOCATION_ATTEMPTS = 1000


def _oidc_context(operation):
    tenant = get_current_tenant()
    actor = get_current_user()
    request_id = get_current_request_id()
    return IntegrationContext(
        provider="oidc",
        operation=operation,
        tenant_id=getattr(tenant, "pk", None),
        actor_id=getattr(actor, "pk", None),
        request_id=str(request_id) if request_id else None,
    )


def _raise_token_validation_error():
    error = OIDCTokenValidationError(context=_oidc_context("token.verify"))
    log_extra = error.log_extra()
    logger.warning(
        "OIDC token validation failed integration=%s",
        log_extra["integration"],
        extra=log_extra,
    )
    raise error


def _raise_token_configuration_error():
    error = OIDCTokenConfigurationError(context=_oidc_context("token.verify"))
    log_extra = error.log_extra()
    logger.error(
        "OIDC token configuration rejected authentication integration=%s",
        log_extra["integration"],
        extra=log_extra,
    )
    raise error


def _identity_error(error_type, operation, *, user_id=None, binding_id=None, exception_type=None):
    error = error_type(context=_oidc_context(operation))
    log_extra = error.log_extra(
        object_id=str(binding_id or user_id) if binding_id or user_id else None,
        exception_type=exception_type,
    )
    logger.warning("OIDC identity operation failed", extra=log_extra)
    return error


def _raise_identity_error(error_type, operation, *, user_id=None, binding_id=None, exception_type=None):
    raise _identity_error(
        error_type,
        operation,
        user_id=user_id,
        binding_id=binding_id,
        exception_type=exception_type,
    ) from None


def _raise_claims_validation_error():
    _raise_identity_error(OIDCTokenValidationError, "claims.verify")


def _set_oidc_transaction_timeouts(cursor: object) -> None:
    if transaction.get_connection().vendor != "postgresql":
        raise RuntimeError("OIDC identity binding requires PostgreSQL.")
    cursor.execute("SET LOCAL lock_timeout = %s", [OIDC_LOCK_TIMEOUT])
    cursor.execute("SET LOCAL statement_timeout = %s", [OIDC_STATEMENT_TIMEOUT])


def _acquire_oidc_identity_lock(cursor: object, identity: VerifiedOIDCIdentity) -> None:
    lock_parts = oidc_advisory_lock_parts(identity.issuer, identity.subject)
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s::integer, %s::integer)",
        lock_parts,
    )


def _oidc_identity_model():
    return apps.get_model("users", "OIDCIdentity")


def _usable_tenant_oidc_config(tenant):
    """Return the tenant's usable OIDC config, or ``None`` to fail closed."""
    configs = getattr(settings, "ITAMBOX_TENANT_OIDC_CONFIGS", {})
    if not isinstance(configs, dict):
        return None
    config = configs.get(tenant.slug)
    if not isinstance(config, dict) or not is_usable_oidc_config(config):
        return None
    return config


def _get_usable_oidc_tenant(tenant_slug, session):
    """Resolve a live, configured tenant without falling back to global OIDC."""
    tenant_model = tenant_scope.tenant_model()
    try:
        tenant = tenant_model._base_manager.get(slug=tenant_slug, deleted_at__isnull=True)
    except tenant_model.DoesNotExist:
        session.pop("oidc_tenant_slug", None)
        raise Http404(f"Tenant {tenant_slug!r} does not exist.") from None
    if _usable_tenant_oidc_config(tenant) is None:
        session.pop("oidc_tenant_slug", None)
        raise Http404(f"OIDC is not configured for tenant {tenant_slug!r}.")
    return tenant


class TenantOIDCSettingsMixin:
    """Shared mixin providing tenant-aware OIDC settings resolution.

    Used by TenantOIDCBackend, TenantOIDCAuthorizeView, and
    TenantOIDCCallbackView to avoid duplicating get_settings() logic.
    """

    @classmethod
    def get_settings(cls, attr, *args):
        tenant = get_current_tenant()
        if tenant:
            tenant_configs = getattr(settings, "ITAMBOX_TENANT_OIDC_CONFIGS", {})
            tenant_config = tenant_configs.get(tenant.slug)
            if tenant_config and attr in tenant_config:
                return tenant_config[attr]
            if tenant_config and attr.lower() in tenant_config:
                return tenant_config[attr.lower()]

        # Defaults for algorithm and scopes. ``import_from_settings`` must get
        # the default so an explicit global setting still wins, exactly as it
        # does for every other OIDC setting and during provider discovery.
        if attr == "OIDC_RP_SIGN_ALGO":
            args = args or ("RS256",)
        elif attr == "OIDC_RP_SCOPES":
            args = args or ("openid email profile",)
        elif attr in ("OIDC_RP_IDP_SIGN_KEY", "OIDC_OP_JWKS_ENDPOINT"):
            # Optional settings: mirror the upstream OIDCAuthenticationBackend
            # __init__ defaults (None). TenantOIDCBackend deliberately skips
            # super().__init__(), so without these defaults a lazy attribute
            # read raises OIDCConfigurationError before the JWKS fallback can
            # run for RS/ES-signed providers configured with JWKS only.
            args = args or (None,)

        # Fallback to global django settings
        try:
            return import_from_settings(attr, *args)
        except ImproperlyConfigured as exc:
            raise OIDCConfigurationError(context=_oidc_context("settings.resolve")) from exc

    def __getattr__(self, name):
        if name.startswith("OIDC_"):
            return self.get_settings(name)
        raise AttributeError("'%s' object has no attribute '%s'" % (self.__class__.__name__, name))


class TenantOIDCBackend(TenantOIDCSettingsMixin, OIDCAuthenticationBackend):
    def __init__(self, *args, **kwargs):
        # Do not call super().__init__() because it assigns settings statically
        self.UserModel = get_user_model()

    def verify_token(self, token, **kwargs):
        """Verify the ID token and additionally enforce audience / issuer.

        mozilla_django_oidc validates the signature (rejecting alg downgrade to
        ``none``/HS256 via its alg-match check, with the RS256 default supplied
        by :class:`TenantOIDCSettingsMixin`) and the nonce, but it calls the JWT
        decoder with ``verify_aud=False`` and never checks the issuer. That
        leaves the RP open to token-substitution / confused-deputy attacks where
        a token minted for a *different* client of the same IdP — or by an
        unexpected issuer — is replayed here. Enforce both.
        """
        payload = super().verify_token(token, **kwargs)

        client_id = self.get_settings("OIDC_RP_CLIENT_ID")
        aud = payload.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        if client_id not in aud_list:
            _raise_token_validation_error()

        # Per the spec, when present `azp` must identify this client.
        azp = payload.get("azp")
        if azp is not None and azp != client_id:
            _raise_token_validation_error()

        # Issuer validation is MANDATORY. If the tenant config omits OIDC_OP_ISSUER,
        # authentication is rejected rather than accepting tokens from any issuer —
        # which would be an open door for token-substitution attacks across IdP clients.
        # Operators must configure OIDC_OP_ISSUER for every tenant that uses OIDC.
        expected_iss = self.get_settings("OIDC_OP_ISSUER", None)
        if not expected_iss:
            _raise_token_configuration_error()
        if payload.get("iss") != expected_iss:
            _raise_token_validation_error()
        try:
            validate_oidc_identity(payload.get("iss"), payload.get("sub"))
        except ValidationError:
            _raise_token_validation_error()

        return payload

    def has_perm(self, user_obj, perm, obj=None):
        return False

    def has_module_perms(self, user_obj, app_label):
        return False

    def get_group_permissions(self, user_obj, obj=None):
        return set()

    def get_all_permissions(self, user_obj, obj=None):
        return set()

    def authenticate(self, request, **kwargs):
        # ``can_login=False`` bars all interactive login, including OIDC/SSO. super() runs the
        # full OIDC code-exchange and user resolution; gate the resolved user post-hoc.
        user = super().authenticate(request, **kwargs)
        if user and not getattr(user, "can_login", True):
            return None
        return user

    def get_or_create_user(self, access_token: object, id_token: object, payload: object) -> object | None:
        """Resolve one canonical User from the verified ID-token identity."""
        identity = self._verified_identity(payload)
        user_info = self.get_userinfo(access_token, id_token, payload)
        if not isinstance(user_info, dict) or not self.verify_claims(user_info):
            _raise_claims_validation_error()
        self._validate_userinfo_identity(user_info, identity)

        try:
            return resolve_verified_oidc_identity(self, identity, user_info)
        except SuspiciousOperation:
            raise
        # broad except: boundary-isolation: adapter errors are not enumerable; raise a safe typed failure
        except Exception as exc:
            raise _identity_error(
                OIDCIdentityProvisioningError,
                "identity.provision",
                exception_type=type(exc).__name__,
            ) from None

    def _verified_identity(self, payload):
        if not isinstance(payload, dict):
            _raise_token_validation_error()
        try:
            issuer, subject = validate_oidc_identity(payload.get("iss"), payload.get("sub"))
        except ValidationError:
            _raise_token_validation_error()

        expected_issuer = self.get_settings("OIDC_OP_ISSUER", None)
        if not expected_issuer:
            _raise_token_configuration_error()
        if issuer != expected_issuer:
            _raise_token_validation_error()
        return VerifiedOIDCIdentity(issuer=issuer, subject=subject)

    def _validate_userinfo_identity(self, user_info, identity):
        if "iss" in user_info and user_info["iss"] != identity.issuer:
            _raise_token_validation_error()
        if "sub" in user_info and user_info["sub"] != identity.subject:
            _raise_token_validation_error()

    def _select_identity_binding(self, identity, *, for_update=False):
        IdentityModel = _oidc_identity_model()
        queryset = IdentityModel._base_manager.select_related("user")
        if for_update:
            queryset = queryset.select_for_update(of=("self",))
        return queryset.filter(issuer=identity.issuer, subject=identity.subject).first()

    def _resolve_identity_phase_a(self, identity, user_info):
        with oidc_sensitive_audit():
            with transaction.atomic():
                with transaction.get_connection().cursor() as cursor:
                    _set_oidc_transaction_timeouts(cursor)
                    _acquire_oidc_identity_lock(cursor, identity)

                binding = self._select_identity_binding(identity, for_update=True)
                if binding is not None:
                    return binding.pk, binding.user_id

                if self._former_oidc_candidates(user_info).exists():
                    _raise_identity_error(
                        OIDCIdentityBindingRequiredError,
                        "identity.legacy_candidate",
                    )

                if not self.get_settings("OIDC_CREATE_USER", True):
                    return None

                return self._create_user_and_binding(identity, user_info)

    def _former_oidc_candidates(self, claims):
        email = claims.get("email")
        if isinstance(email, str) and email:
            users = self.UserModel._base_manager.filter(email__iexact=email)
            if users.exists():
                return users

        username = email or claims.get("sub") or "oidc_user"
        if not isinstance(username, str) or not username:
            username = "oidc_user"
        return self.UserModel._base_manager.filter(username=username)

    def _username_candidates(self, claims):
        base_username = self.get_username(claims)
        if not isinstance(base_username, str) or not base_username:
            base_username = "oidc_user"
        max_length = self.UserModel._meta.get_field("username").max_length or 150
        base_username = base_username[:max_length]
        yield base_username
        for counter in range(1, MAX_USERNAME_ALLOCATION_ATTEMPTS):
            suffix = f"_{counter}"
            yield f"{base_username[: max_length - len(suffix)]}{suffix}"

    def _create_user_and_binding(self, identity, claims):
        IdentityModel = _oidc_identity_model()
        email = self._claim_text(claims, "email")
        first_name = self._claim_text(claims, "given_name", "first_name")
        last_name = self._claim_text(claims, "family_name", "last_name")

        for username in self._username_candidates(claims):
            try:
                with transaction.atomic():
                    with oidc_sensitive_audit():
                        user = self.UserModel.objects.create_user(
                            username=username,
                            email=email,
                            first_name=first_name,
                            last_name=last_name,
                        )
                        binding = IdentityModel.objects.create(
                            user=user,
                            issuer=identity.issuer,
                            subject=identity.subject,
                        )
                return binding.pk, user.pk
            except IntegrityError:
                binding = self._select_identity_binding(identity, for_update=True)
                if binding is not None:
                    return binding.pk, binding.user_id
                if self.UserModel._base_manager.filter(username=username).exists():
                    continue
                _raise_identity_error(
                    OIDCIdentityProvisioningError,
                    "identity.create",
                    exception_type="IntegrityError",
                )

        _raise_identity_error(OIDCIdentityProvisioningError, "identity.username_allocation")

    def _finish_identity_phase_b(self, binding_id, expected_user_id, claims):
        with oidc_sensitive_audit():
            with transaction.atomic():
                with transaction.get_connection().cursor() as cursor:
                    _set_oidc_transaction_timeouts(cursor)

                IdentityModel = _oidc_identity_model()
                binding = IdentityModel._base_manager.select_for_update(of=("self",)).filter(pk=binding_id).first()
                if binding is None or binding.user_id != expected_user_id:
                    _raise_identity_error(
                        OIDCIdentityProvisioningError,
                        "identity.binding_changed",
                        user_id=expected_user_id,
                        binding_id=binding_id,
                    )

                # The binding lock is the only adapter-owned row lock in Phase B.
                # The organization port owns the normative Tenant -> User -> aggregate
                # lock order; loading User here must remain non-locking.
                user = self.UserModel._base_manager.get(pk=binding.user_id)
                if not getattr(user, "can_login", True):
                    return user

                current_tenant = get_current_tenant()
                if current_tenant is None:
                    self._update_user_profile(user, claims)
                    return user

                tenant_model = tenant_scope.tenant_model()
                customer_tenant = tenant_model._base_manager.get(pk=current_tenant.pk)
                groups = self._normalized_groups(claims)
                provider_tenant = None
                if customer_tenant.managed_by_id and groups:
                    provider_tenant = tenant_model._base_manager.get(pk=customer_tenant.managed_by_id)

                command = identity_provisioning.ExternalIdentityProvisioningCommand(
                    user=user,
                    customer_tenant=customer_tenant,
                    profile=self._external_identity_profile(user, claims),
                    customer_role_name=self._customer_role_name(customer_tenant, groups),
                    provider_staff=self._provider_staff_intent(
                        customer_tenant,
                        provider_tenant,
                        groups,
                    ),
                )
                try:
                    result = identity_provisioning.provision_external_identity(command)
                except ValidationError as exc:
                    _raise_identity_error(
                        OIDCIdentityProvisioningError,
                        "identity.provision",
                        user_id=binding.user_id,
                        binding_id=binding.pk,
                        exception_type=type(exc).__name__,
                    )

                current_user = self.UserModel._base_manager.get(pk=binding.user_id)
                if not getattr(current_user, "can_login", True):
                    _raise_identity_error(
                        OIDCIdentityProvisioningError,
                        "identity.disabled",
                        user_id=current_user.pk,
                        binding_id=binding.pk,
                    )
                if getattr(result, "mode", None) == "provider_mapping_rejected":
                    # Organization owns only the terminal access decision here;
                    # the verified canonical User profile remains adapter-owned.
                    self._update_user_profile(current_user, claims)
                    return current_user

                self._update_user_profile(current_user, claims)
                return current_user

    @staticmethod
    def _claim_text(claims, *names):
        for name in names:
            value = claims.get(name)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _normalized_groups(claims):
        groups = claims.get("groups", [])
        if isinstance(groups, str):
            return [groups]
        if not isinstance(groups, list):
            return []
        return [group for group in groups if isinstance(group, str)]

    @staticmethod
    def _customer_role_name(customer_tenant, groups):
        configs = getattr(settings, "ITAMBOX_TENANT_OIDC_CONFIGS", {})
        tenant_config = configs.get(customer_tenant.slug, {}) if isinstance(configs, dict) else {}
        mapping = tenant_config.get("OIDC_GROUP_ROLE_MAPPING", {}) if isinstance(tenant_config, dict) else {}
        if not isinstance(mapping, dict):
            mapping = {}
        mapped_roles = {
            mapping[group].lower() for group in groups if group in mapping and isinstance(mapping[group], str)
        }
        for role_name in ("admin", "manager", "member"):
            if role_name in mapped_roles:
                return role_name.title()
        return "Member"

    @staticmethod
    def _provider_staff_intent(customer_tenant, provider_tenant, groups):
        if provider_tenant is None or not customer_tenant.managed_by_id:
            return None
        configs = getattr(settings, "ITAMBOX_TENANT_OIDC_CONFIGS", {})
        provider_config = configs.get(provider_tenant.slug, {}) if isinstance(configs, dict) else {}
        mapping = (
            provider_config.get("OIDC_GROUP_PROVIDER_ROLE_MAPPING", {}) if isinstance(provider_config, dict) else {}
        )
        if not isinstance(mapping, dict):
            mapping = {}
        for group in groups:
            if group in mapping:
                role_name = mapping[group] if isinstance(mapping[group], str) else ""
                return identity_provisioning.ProviderStaffIntent(
                    provider_tenant=provider_tenant,
                    role_name=role_name,
                )
        return None

    def _external_identity_profile(self, user, claims):
        email = self._claim_text(claims, "email") or getattr(user, "email", "") or None
        upn = self._claim_text(claims, "upn", "email") or getattr(user, "email", "") or None
        first_name = self._claim_text(claims, "given_name", "first_name") or user.first_name or "OIDC"
        last_name = self._claim_text(claims, "family_name", "last_name") or user.last_name or "User"
        return identity_provisioning.ExternalIdentityProfile(
            source="OIDC",
            email=email,
            upn=upn,
            first_name=first_name,
            last_name=last_name,
        )

    def _resolve_verified_identity(self, identity: VerifiedOIDCIdentity, claims: dict[str, object]) -> object | None:
        """Resolve and provision only after the caller supplies verified identity data."""
        return resolve_verified_oidc_identity(self, identity, claims)

    def filter_users_by_claims(self, claims):
        _raise_identity_error(OIDCIdentityBindingRequiredError, "identity.unverified_helper")

    def get_username(self, claims):
        email = claims.get("email")
        sub = claims.get("sub")
        return email or sub or "oidc_user"

    def create_user(self, claims):
        _raise_identity_error(OIDCIdentityBindingRequiredError, "identity.unverified_helper")

    def _update_user_profile(self, user, claims):
        update_fields = []
        email = self._claim_text(claims, "email")
        first_name = self._claim_text(claims, "given_name", "first_name")
        last_name = self._claim_text(claims, "family_name", "last_name")

        if email and user.email != email:
            user.email = email
            update_fields.append("email")
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            update_fields.append("first_name")
        if last_name and user.last_name != last_name:
            user.last_name = last_name
            update_fields.append("last_name")
        if update_fields:
            user.save(update_fields=update_fields)
        return user

    def update_user(self, user, claims):
        _raise_identity_error(OIDCIdentityBindingRequiredError, "identity.unverified_helper")


class TenantOIDCAuthorizeView(TenantOIDCSettingsMixin, OIDCAuthenticationRequestView):
    def __init__(self, *args, **kwargs):
        # Bypass parent oidc request view static settings lookup in init
        super(OIDCAuthenticationRequestView, self).__init__(*args, **kwargs)

    @property
    def OIDC_OP_AUTH_ENDPOINT(self):
        return self.get_settings("OIDC_OP_AUTHORIZATION_ENDPOINT")

    @OIDC_OP_AUTH_ENDPOINT.setter
    def OIDC_OP_AUTH_ENDPOINT(self, value):
        pass

    @property
    def OIDC_RP_CLIENT_ID(self):
        return self.get_settings("OIDC_RP_CLIENT_ID")

    @OIDC_RP_CLIENT_ID.setter
    def OIDC_RP_CLIENT_ID(self, value):
        pass

    def dispatch(self, request, *args, **kwargs):
        tenant_slug = kwargs.pop("tenant_slug", None) or request.GET.get("tenant")

        tenant = None
        if tenant_slug:
            tenant = _get_usable_oidc_tenant(tenant_slug, request.session)
            request.session["oidc_tenant_slug"] = tenant.slug

        if not tenant:
            sess_tenant_slug = request.session.get("oidc_tenant_slug")
            if sess_tenant_slug:
                tenant = _get_usable_oidc_tenant(sess_tenant_slug, request.session)

        if tenant:
            set_current_tenant(tenant)

        return super().dispatch(request, *args, **kwargs)


class TenantOIDCCallbackView(TenantOIDCSettingsMixin, OIDCAuthenticationCallbackView):
    def dispatch(self, request, *args, **kwargs):
        tenant_slug = request.session.get("oidc_tenant_slug")
        if tenant_slug:
            tenant = _get_usable_oidc_tenant(tenant_slug, request.session)
            set_current_tenant(tenant)
        return super().dispatch(request, *args, **kwargs)

    def login_success(self):
        tenant_slug = self.request.session.get("oidc_tenant_slug")
        if tenant_slug:
            tenant = _get_usable_oidc_tenant(tenant_slug, self.request.session)
            self.request.session["active_tenant_id"] = tenant.pk
        return super().login_success()
