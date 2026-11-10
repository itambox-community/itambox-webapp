"""Discovery of the external identity providers offered on the login page.

A provider is advertised only when its deployment configuration can genuinely
start *and complete* a login: an entry that is disabled, malformed, incomplete,
or points at a tenant that does not exist produces no button at all rather than
a broken one.

The per-tenant configuration dictionaries are keyed by tenant slug
(``ITAMBOX_TENANT_SAML_CONFIGS`` / ``ITAMBOX_TENANT_OIDC_CONFIGS``); SAML
additionally understands the ``default`` key handled by
:func:`core.auth.saml.load_saml_config`. OIDC settings may also come from the
global Django settings, which :class:`core.auth.oidc.TenantOIDCSettingsMixin`
falls back to.
"""

from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.utils.http import urlencode

from core import tenant_scope

SAML = "SAML"
OIDC = "OIDC"

#: SAML config key that is not a tenant slug (see ``load_saml_config``).
DEFAULT_KEY = "default"

#: Settings an OIDC provider must resolve before its action can complete a
#: login. ``OIDC_OP_ISSUER`` is included deliberately: ``TenantOIDCBackend``
#: rejects every token when it is missing, so a button without it always fails.
REQUIRED_OIDC_SETTINGS = (
    "OIDC_RP_CLIENT_ID",
    "OIDC_RP_CLIENT_SECRET",
    "OIDC_OP_AUTHORIZATION_ENDPOINT",
    "OIDC_OP_TOKEN_ENDPOINT",
    "OIDC_OP_USER_ENDPOINT",
    "OIDC_OP_ISSUER",
)


def get_login_providers(next_url="", *, tenant_slug=None):
    """Return the SSO actions to render on the login page.

    Each entry is a dict with ``protocol`` (``"SAML"``/``"OIDC"``), ``key`` (the
    configuration key, i.e. the tenant slug), ``name`` (a distinct, human
    readable label), ``url`` (the entry point, carrying ``next`` when a
    validated destination was supplied), and ``tenant_bound``. Supplying
    ``tenant_slug`` returns only actions pinned to that tenant; in particular,
    deployment-wide OIDC settings are never used as a tenant fallback.
    """
    saml_configs = _config_mapping("ITAMBOX_TENANT_SAML_CONFIGS")
    oidc_configs = _config_mapping("ITAMBOX_TENANT_OIDC_CONFIGS")

    tenant_model = tenant_scope.tenant_model()
    tenants = _tenants_by_slug(tenant_model, set(saml_configs) | set(oidc_configs))
    default_saml_tenant = _default_saml_tenant(tenant_model, saml_configs)
    if default_saml_tenant is not None:
        tenants[default_saml_tenant.slug] = default_saml_tenant

    providers = _saml_login_providers(saml_configs, next_url, tenants, default_saml_tenant, tenant_slug)
    providers.extend(_oidc_login_providers(oidc_configs, next_url, tenants, tenant_slug))

    if _global_oidc_available(tenant_slug, oidc_configs):
        # Single-tenant deployments may configure OIDC entirely in the Django
        # settings; that is the untenanted entry point.
        _add(
            providers,
            OIDC,
            DEFAULT_KEY,
            {},
            tenants,
            _entry_point("oidc_authentication_init", next_url),
            tenant_bound=False,
        )

    providers.sort(key=lambda provider: (provider["name"].lower(), provider["protocol"]))
    return providers


def _saml_login_providers(saml_configs, next_url, tenants, default_saml_tenant, tenant_slug):
    providers = []
    for key, config in saml_configs.items():
        if not _is_usable_saml_config(config):
            continue
        resolved = _saml_entry_point(key, next_url, tenants, default_saml_tenant)
        if resolved is None:
            continue
        key, url = resolved
        if tenant_slug is not None and tenant_slug != key:
            continue
        _add(providers, SAML, key, config, tenants, url, tenant_bound=True)
    return providers


def _oidc_login_providers(oidc_configs, next_url, tenants, tenant_slug):
    providers = []
    for key, config in oidc_configs.items():
        if not _oidc_provider_available(key, config, tenants, tenant_slug):
            continue
        url = _entry_point("oidc_authentication_init_tenant", next_url, tenant_slug=key)
        _add(providers, OIDC, key, config, tenants, url, tenant_bound=True)
    return providers


def _oidc_provider_available(key, config, tenants, tenant_slug):
    if key not in tenants:
        return False
    if tenant_slug is None:
        return is_usable_oidc_config(config)
    return tenant_slug == key and is_usable_tenant_oidc_config(config)


def _global_oidc_available(tenant_slug, oidc_configs):
    return tenant_slug is None and not oidc_configs and is_usable_oidc_config({})


def _config_mapping(setting_name):
    """Return a settings mapping, tolerating a malformed (non-dict) value."""
    configs = getattr(settings, setting_name, {})
    if not isinstance(configs, dict):
        return {}
    return {key: value for key, value in configs.items() if isinstance(key, str)}


def _tenants_by_slug(tenant_model, slugs):
    """Resolve live tenants for the configured slugs.

    ``_base_manager``: the login page is anonymous, so no tenant scope is
    established yet (the same bootstrap lookup ``TenantMiddleware`` performs).
    """
    if not slugs:
        return {}
    tenants = tenant_model._base_manager.filter(slug__in=slugs, deleted_at__isnull=True)
    return {tenant.slug: tenant for tenant in tenants}


def _sole_live_tenant(tenant_model):
    """Return the sole live tenant, or ``None`` when global SAML is ambiguous."""
    tenants = list(tenant_model._base_manager.filter(deleted_at__isnull=True).order_by("pk")[:2])
    return tenants[0] if len(tenants) == 1 else None


def _default_saml_tenant(tenant_model, saml_configs):
    """Resolve an unambiguous tenant for a deployment-wide SAML config."""
    if DEFAULT_KEY not in saml_configs:
        return None
    tenant = _sole_live_tenant(tenant_model)
    if tenant is None or tenant.slug in saml_configs:
        return None
    return tenant


def _saml_entry_point(key, next_url, tenants, default_tenant):
    """Resolve a configured SAML key to a tenant-pinned login action."""
    if key == DEFAULT_KEY:
        if default_tenant is None:
            return None
        key = default_tenant.slug
    elif key not in tenants:
        return None
    return key, _entry_point("saml2_login_tenant", next_url, tenant_slug=key)


def _is_enabled(config):
    return isinstance(config, dict) and config.get("enabled", True) is not False


def _is_usable_saml_config(config):
    """A SAML entry needs IdP metadata and an SP identity to be usable."""
    if not _is_enabled(config):
        return False
    metadata = config.get("metadata")
    if not isinstance(metadata, dict) or not any(metadata.values()):
        return False
    return bool(config.get("entityid") or config.get("base_url"))


def is_usable_oidc_config(config):
    if not _is_enabled(config):
        return False
    if not all(_resolve_oidc_setting(config, name) for name in REQUIRED_OIDC_SETTINGS):
        return False

    sign_algorithm = _resolve_oidc_setting(config, "OIDC_RP_SIGN_ALGO") or "RS256"
    if str(sign_algorithm).upper().startswith(("RS", "ES")):
        return bool(
            _resolve_oidc_setting(config, "OIDC_RP_IDP_SIGN_KEY")
            or _resolve_oidc_setting(config, "OIDC_OP_JWKS_ENDPOINT")
        )
    return True


def is_usable_tenant_oidc_config(config):
    """Check a tenant OIDC provider without borrowing global OIDC settings."""
    if not _is_enabled(config):
        return False
    if not all(_tenant_oidc_setting(config, name) for name in REQUIRED_OIDC_SETTINGS):
        return False

    sign_algorithm = _tenant_oidc_setting(config, "OIDC_RP_SIGN_ALGO") or "RS256"
    if str(sign_algorithm).upper().startswith(("RS", "ES")):
        return bool(
            _tenant_oidc_setting(config, "OIDC_RP_IDP_SIGN_KEY")
            or _tenant_oidc_setting(config, "OIDC_OP_JWKS_ENDPOINT")
        )
    return True


def _resolve_oidc_setting(config, name):
    """Mirror ``TenantOIDCSettingsMixin.get_settings`` resolution order."""
    for candidate in (name, name.lower()):
        if candidate in config:
            return config[candidate]
    return getattr(settings, name, None)


def _tenant_oidc_setting(config, name):
    if not isinstance(config, dict):
        return None
    for candidate in (name, name.lower()):
        if candidate in config:
            return config[candidate]
    return None


def _add(providers, protocol, key, config, tenants, url, *, tenant_bound):
    """Record a provider action, unless its entry point could not be resolved.

    A button with an empty href is exactly the broken placeholder the login page
    must never render, so a missing route drops the provider instead.
    """
    if not url:
        return
    providers.append(
        {
            "protocol": protocol,
            "key": key,
            "name": _display_name(protocol, key, config, tenants),
            "url": url,
            "tenant_bound": tenant_bound,
        }
    )


def _display_name(protocol, key, config, tenants):
    """A label that stays distinct when several providers are configured."""
    label = config.get("display_name") or config.get("label")
    if not label and key in tenants:
        label = tenants[key].name
    if not label:
        return protocol
    return f"{label} ({protocol})"


def _entry_point(url_name, next_url, **kwargs):
    try:
        url = reverse(url_name, kwargs=kwargs)
    except NoReverseMatch:
        return ""
    if next_url:
        url = f"{url}?{urlencode({'next': next_url})}"
    return url
