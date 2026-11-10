"""Authentication configuration checks for tenant login discovery."""

from xml.etree import ElementTree

from django.conf import settings
from django.core.checks import Warning, register

from core.auth import providers
from core.auth.discovery import login_domains_configured


@register()
def check_tenant_login_discovery(app_configs, **kwargs):
    """Report missing tenant-bound login providers and duplicate IdP identities."""
    return [*_mapped_provider_warnings(), *_missing_domain_warnings(), *_duplicate_idp_warnings()]


def _mapped_provider_warnings():
    mapping = _setting_mapping("ITAMBOX_TENANT_LOGIN_DOMAINS")
    if not mapping:
        return []
    warnings = []
    for slug, config in mapping.items():
        allowed = _configured_provider_types(slug)
        method = config.get("method") if isinstance(config, dict) else None
        if method:
            allowed &= {str(method).upper()}
        if not allowed:
            warnings.append(
                Warning(
                    f"Tenant login domain mapping for slug {slug!r} has no usable tenant-bound SSO configuration.",
                    id="core.W005",
                )
            )
    return warnings


def _missing_domain_warnings():
    if not login_domains_configured():
        return []
    mapped_slugs = set(_setting_mapping("ITAMBOX_TENANT_LOGIN_DOMAINS"))
    unclaimed_slugs = _configured_sso_slugs() - mapped_slugs
    return [
        Warning(
            f"Tenant SSO configuration for slug {slug!r} has no login domain while discovery is enabled.",
            id="core.W006",
        )
        for slug in sorted(unclaimed_slugs)
    ]


def _configured_sso_slugs():
    slugs = set()
    for slug, config in _setting_mapping("ITAMBOX_TENANT_SAML_CONFIGS").items():
        if slug != providers.DEFAULT_KEY and providers._is_usable_saml_config(config):
            slugs.add(slug)
    for slug, config in _setting_mapping("ITAMBOX_TENANT_OIDC_CONFIGS").items():
        if slug != providers.DEFAULT_KEY and providers.is_usable_oidc_config(config):
            slugs.add(slug)
    return slugs


def _configured_provider_types(slug):
    provider_types = set()
    saml_configs = _setting_mapping("ITAMBOX_TENANT_SAML_CONFIGS")
    # The resolver lets a tenant without its own SAML entry inherit ``default``
    # (it additionally requires a sole live tenant, which a startup check must
    # not query the database for). Counting the inheritance here avoids a false
    # W005 on a healthy single-tenant default-SAML install.
    saml_config = saml_configs.get(slug, saml_configs.get(providers.DEFAULT_KEY))
    if saml_config is not None and providers._is_usable_saml_config(saml_config):
        provider_types.add(providers.SAML)
    oidc_config = _setting_mapping("ITAMBOX_TENANT_OIDC_CONFIGS").get(slug)
    if oidc_config is not None and providers.is_usable_tenant_oidc_config(oidc_config):
        provider_types.add(providers.OIDC)
    return provider_types


def _duplicate_idp_warnings():
    if not login_domains_configured():
        return []
    identities = {}
    for slug, config in _setting_mapping("ITAMBOX_TENANT_OIDC_CONFIGS").items():
        if slug == providers.DEFAULT_KEY or not providers.is_usable_oidc_config(config):
            continue
        issuer = providers._resolve_oidc_setting(config, "OIDC_OP_ISSUER")
        if issuer:
            _record_identity(identities, (providers.OIDC, str(issuer).strip()), slug)
    for slug, config in _setting_mapping("ITAMBOX_TENANT_SAML_CONFIGS").items():
        if slug == providers.DEFAULT_KEY or not providers._is_usable_saml_config(config):
            continue
        for identity in _saml_identities(config):
            _record_identity(identities, (providers.SAML, identity), slug)
    return [
        Warning(
            f"The same {protocol} identity is configured for multiple tenants: {', '.join(sorted(slugs))}.",
            id="core.W007",
        )
        for (protocol, _identity), slugs in identities.items()
        if len(slugs) > 1
    ]


def _saml_identities(config):
    explicit_identity = config.get("idp_entity_id") or config.get("idp_entityid")
    if explicit_identity:
        return {str(explicit_identity).strip()}
    metadata = config.get("metadata")
    if not isinstance(metadata, dict):
        return set()
    remote = metadata.get("remote", ())
    if isinstance(remote, dict):
        remote = (remote,)
    if not isinstance(remote, (list, tuple)):
        return set()
    identities = {str(item["url"]).strip() for item in remote if isinstance(item, dict) and item.get("url")}
    return identities | _inline_saml_identities(metadata.get("inline"))


def _inline_saml_identities(sources):
    if isinstance(sources, str):
        sources = (sources,)
    if not isinstance(sources, (list, tuple)):
        return set()
    identities = set()
    for source in sources:
        try:
            root = ElementTree.fromstring(source)
        except (ElementTree.ParseError, TypeError, ValueError):
            continue
        for element in root.iter():
            entity_id = element.get("entityID")
            if element.tag.rsplit("}", 1)[-1] == "EntityDescriptor" and entity_id:
                identities.add(entity_id.strip())
    return identities


def _record_identity(identities, identity, slug):
    identities.setdefault(identity, set()).add(slug)


def _setting_mapping(name):
    value = getattr(settings, name, {})
    return value if isinstance(value, dict) else {}
