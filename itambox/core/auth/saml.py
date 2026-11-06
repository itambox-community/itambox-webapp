import logging

import saml2
from django.apps import apps
from django.conf import settings
from django.http import Http404
from djangosaml2.backends import Saml2Backend
from saml2.config import SPConfig

from core import identity_provisioning, tenant_scope
from core.managers import get_current_tenant, set_current_tenant

logger = logging.getLogger(__name__)

#: Session key pinning the tenant a SAML flow was started for. The IdP posts the
#: assertion back to an anonymous, cross-site endpoint (``/saml2/acs/``), so
#: without this pin neither the SP configuration nor JIT provisioning could tell
#: which tenant the assertion belongs to.
SAML_TENANT_SESSION_KEY = "saml_tenant_slug"


def resolve_saml_tenant_slug(request=None):
    """The tenant slug a SAML request belongs to, or ``None``.

    The active tenant context wins; anonymous SAML endpoints have none, so the
    slug pinned by :func:`bind_saml_tenant` is used instead.
    """
    tenant = get_current_tenant()
    if tenant is not None:
        return tenant.slug
    session = getattr(request, "saml_session", None)
    if session is None:
        return None
    return session.get(SAML_TENANT_SESSION_KEY)


def bind_saml_tenant(request, tenant_slug):
    """Bind ``tenant_slug`` to this request and pin it for the ACS callback.

    Raises :class:`~django.http.Http404` for an unknown or deleted tenant rather
    than silently falling back to another tenant's identity provider.
    """
    tenant = _live_tenant(tenant_slug)
    if tenant is None or _configured_saml_tenant(tenant_slug) is None:
        raise Http404(f"No SAML tenant {tenant_slug!r}.")
    request.saml_session[SAML_TENANT_SESSION_KEY] = tenant.slug
    set_current_tenant(tenant)
    return tenant


def restore_saml_tenant(request):
    """Re-activate the pinned tenant, failing closed if it is no longer usable."""
    slug = getattr(request, "saml_session", {}).get(SAML_TENANT_SESSION_KEY)
    if not slug:
        raise Http404("The SAML tenant binding is missing.")
    tenant = _live_tenant(slug)
    if tenant is None or _configured_saml_tenant(slug) is None:
        raise Http404(f"No SAML tenant {slug!r}.")
    set_current_tenant(tenant)
    return tenant


def _live_tenant(slug):
    """Use the registered tenant model at operation time.

    SAML starts and completes on anonymous endpoints, so the lookup must remain
    unscoped and must not retain a model class across app-registry lifecycles.
    """
    tenant_model = tenant_scope.tenant_model()
    return tenant_model._base_manager.filter(slug=slug, deleted_at__isnull=True).first()


def _configured_saml_tenant(slug):
    """Return the enabled config for ``slug`` or a safe single-tenant default."""
    configs = getattr(settings, "ITAMBOX_TENANT_SAML_CONFIGS", {})
    if not isinstance(configs, dict):
        return None
    config = configs.get(slug)
    if not isinstance(config, dict) and _is_sole_live_tenant(slug):
        # A deployment-wide/default config is safe to apply only when exactly
        # one live tenant exists; otherwise the IdP response cannot be scoped.
        config = configs.get("default")
    if not isinstance(config, dict) or config.get("enabled", True) is False:
        return None
    return config


def _is_sole_live_tenant(slug):
    tenant_model = tenant_scope.tenant_model()
    live_slugs = list(
        tenant_model._base_manager.filter(deleted_at__isnull=True).order_by("pk").values_list("slug", flat=True)[:2]
    )
    return live_slugs == [slug]


def _tenant_saml_config(slug):
    """Resolve role-mapping options with the same safe default fallback."""
    tenant_configs = getattr(settings, "ITAMBOX_TENANT_SAML_CONFIGS", {})
    config = tenant_configs.get(slug) if isinstance(tenant_configs, dict) else None
    if not isinstance(config, dict) and _is_sole_live_tenant(slug):
        config = tenant_configs.get("default", {})
    return config if isinstance(config, dict) else {}


def load_saml_config(request=None):
    """
    Dynamically constructs and returns the pysaml2 SPConfig object
    configured specifically for the active tenant context.

    djangosaml2 always calls its configured loader with the current request, so
    the tenant a flow was started for is honoured even on the anonymous
    endpoints (``/saml2/login/``, ``/saml2/acs/``, ``/saml2/metadata/``).
    """
    tenant_slug = resolve_saml_tenant_slug(request)
    saml_configs = getattr(settings, "ITAMBOX_TENANT_SAML_CONFIGS", {})

    tenant_config = None
    if tenant_slug:
        tenant_config = _configured_saml_tenant(tenant_slug)
        if tenant_config is None:
            raise Http404(f"SAML is not configured for tenant {tenant_slug!r}.")

    if tenant_config is None:
        if "default" in saml_configs:
            tenant_config = saml_configs["default"]
        elif saml_configs:
            first_key = list(saml_configs.keys())[0]
            tenant_config = saml_configs[first_key]
        else:
            # Fallback metadata/entityid configuration to allow initialization
            tenant_config = {"entityid": "https://itambox.local/saml2/metadata/", "metadata": {"local": []}}

    # Resolve active hosts/base URLs
    base_url = tenant_config.get("base_url")
    if not base_url:
        base_url = f"https://{tenant_slug or 'itambox'}.local"

    sp_config = {
        "entityid": tenant_config.get("entityid", f"{base_url}/saml2/metadata/"),
        "service": {
            "sp": {
                "name": "ITAMbox SP",
                "endpoints": {
                    "assertion_consumer_service": [
                        (f"{base_url}/saml2/acs/", saml2.BINDING_HTTP_POST),
                    ],
                    "single_logout_service": [
                        (f"{base_url}/saml2/ls/", saml2.BINDING_HTTP_REDIRECT),
                    ],
                },
                # Secure by default: reject forged/unsigned assertions. A tenant may
                # explicitly relax these in its SAML config, but the defaults must be
                # safe so an unsigned, unsolicited assertion cannot mint an admin.
                "allow_unsolicited": tenant_config.get("allow_unsolicited", False),
                "authn_requests_signed": tenant_config.get("authn_requests_signed", False),
                "logout_requests_signed": tenant_config.get("logout_requests_signed", False),
                "want_assertions_signed": tenant_config.get("want_assertions_signed", True),
                "want_response_signed": tenant_config.get("want_response_signed", True),
            },
        },
        "metadata": tenant_config.get("metadata", {}),
        "debug": settings.DEBUG,
    }

    # Load and compile Saml2 SPConfig
    config = SPConfig()
    config.load(sp_config)
    return config


def _saml_attribute(ava, aliases):
    if isinstance(aliases, str):
        aliases = [aliases]
    for alias in aliases:
        value = ava.get(alias)
        if value:
            if isinstance(value, list):
                value = value[0]
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value)
    return None


def _saml_groups(ava):
    groups = ava.get("groups") or ava.get("memberOf") or ava.get("User.Groups") or []
    if isinstance(groups, str):
        groups = [groups]
    elif not isinstance(groups, list):
        groups = []

    normalized = []
    for group in groups:
        if isinstance(group, bytes):
            normalized.append(group.decode("utf-8"))
        else:
            normalized.append(str(group))
    return normalized


def _saml_profile(user, ava):
    email = _saml_attribute(ava, ["email", "mail", "User.Email"]) or user.email
    first_name = _saml_attribute(ava, ["givenName", "first_name", "User.FirstName"]) or user.first_name or "SAML"
    last_name = _saml_attribute(ava, ["sn", "last_name", "User.LastName"]) or user.last_name or "User"
    upn = _saml_attribute(ava, ["upn", "userPrincipalName", "uid", "nameidentifier"]) or email

    if not upn:
        upn = email or f"{user.username}@saml"
    if not email:
        email = f"{user.username}@saml.local"

    return identity_provisioning.ExternalIdentityProfile(
        source="SAML",
        email=email,
        upn=upn,
        first_name=first_name,
        last_name=last_name,
    )


def _saml_role_name(tenant, groups):
    tenant_config = _tenant_saml_config(tenant.slug)
    group_role_mapping = tenant_config.get("SAML_GROUP_ROLE_MAPPING", {})

    user_roles = []
    for group in groups:
        if group in group_role_mapping:
            mapped_role = group_role_mapping[group]
            if isinstance(mapped_role, str):
                user_roles.append(mapped_role.lower())

    for priority_role in ("admin", "manager", "member"):
        if priority_role in user_roles:
            return {"admin": "Admin", "manager": "Manager", "member": "Member"}[priority_role]
    return "Member"


def _has_active_membership(user, tenant):
    if tenant is None:
        return False
    membership_model = apps.get_model("organization", "Membership")
    return membership_model._base_manager.filter(user=user, tenant=tenant, is_active=True).exists()


class TenantSaml2Backend(Saml2Backend):
    """SAML authentication with tenant-aware configuration and JIT handoff."""

    def authenticate(self, request, session_info=None, attribute_mapping=None, create_unknown_user=True, **kwargs):
        user = super().authenticate(request, session_info, attribute_mapping, create_unknown_user, **kwargs)
        # ``can_login=False`` bars ALL interactive login, including SSO (SSO backends do not
        # route through ModelBackend.user_can_authenticate).
        if user and not getattr(user, "can_login", True):
            return None
        if user and session_info:
            self.sync_saml_user_profile_and_memberships(user, session_info)
        return user

    def get_or_create_user(
        self,
        user_lookup_key,
        user_lookup_value,
        create_unknown_user,
        idp_entityid,
        attributes,
        attribute_mapping,
        request,
    ):
        """Resolve the account for an assertion, bound to the tenant the flow started for.

        The NameID lookup upstream is global. An existing account is therefore only
        accepted when it holds an active membership in the asserting tenant and is
        neither a superuser nor staff; anything else is refused with ``(None, False)``
        (never raised into the ACS view). Unknown identities are provisioned only
        while both the global setting and the tenant's ``SAML_CREATE_UNKNOWN_USER``
        key (default ``True``) allow it.
        """
        tenant = get_current_tenant()
        tenant_allows_create = True
        if tenant is not None:
            tenant_allows_create = _tenant_saml_config(tenant.slug).get("SAML_CREATE_UNKNOWN_USER", True) is not False
        user, created = super().get_or_create_user(
            user_lookup_key,
            user_lookup_value,
            bool(create_unknown_user) and tenant_allows_create,
            idp_entityid,
            attributes,
            attribute_mapping,
            request,
        )
        if user is None or created:
            return user, created
        if user.is_superuser or user.is_staff or not _has_active_membership(user, tenant):
            logger.warning("Refused SAML assertion for an account outside the asserting tenant.")
            return None, False
        return user, created

    def sync_saml_user_profile_and_memberships(self, user, session_info):
        tenant = get_current_tenant()
        if not tenant:
            return None

        ava = session_info.get("ava", {})
        profile = _saml_profile(user, ava)
        role_name = _saml_role_name(tenant, _saml_groups(ava))
        command = identity_provisioning.ExternalIdentityProvisioningCommand(
            user=user,
            customer_tenant=tenant,
            profile=profile,
            customer_role_name=role_name,
        )
        return identity_provisioning.provision_external_identity(command)

    def has_perm(self, user_obj, perm, obj=None):
        return False

    def has_module_perms(self, user_obj, app_label):
        return False

    def get_group_permissions(self, user_obj, obj=None):
        return set()

    def get_all_permissions(self, user_obj, obj=None):
        return set()
