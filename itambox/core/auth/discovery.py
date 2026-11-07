"""Resolve an email domain to a configured, tenant-bound SSO action."""

import logging
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from core.auth.providers import get_login_providers
from core.email_domains import PUBLIC_EMAIL_DOMAINS, email_domain, normalize_domain

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginRoute:
    """A password fallback, one configured redirect, or a neutral provider choice."""

    mode: str
    url: str
    actions: tuple


def login_domains_configured():
    """Whether setting-based login discovery is active."""
    mapping = getattr(settings, "ITAMBOX_TENANT_LOGIN_DOMAINS", {})
    return isinstance(mapping, dict) and bool(mapping)


def _tenant_slug_for_domain(domain):
    """Return the exact mapped slug only while its tenant is live."""
    normalized_domain = _claimable_login_domain(domain)
    if normalized_domain is None:
        return None
    slug = _mapped_tenant_slug(normalized_domain)
    if slug is None:
        return None
    return _live_mapped_tenant_slug(normalized_domain, slug)


def _claimable_login_domain(domain):
    try:
        normalized_domain = normalize_domain(domain)
    except ValueError:
        return None
    return None if normalized_domain in PUBLIC_EMAIL_DOMAINS else normalized_domain


def _mapped_tenant_slug(domain):
    mapping = getattr(settings, "ITAMBOX_TENANT_LOGIN_DOMAINS", {})
    if not isinstance(mapping, dict):
        return None
    for slug, config in mapping.items():
        if _matches_domain_config(slug, config, domain):
            return slug
    return None


def _matches_domain_config(slug, config, expected_domain):
    if not isinstance(slug, str) or not isinstance(config, dict):
        return False
    domains = config.get("domains")
    return isinstance(domains, (tuple, list)) and any(
        _matches_domain(candidate, expected_domain) for candidate in domains
    )


def _live_mapped_tenant_slug(domain, slug):
    try:
        tenant_model = apps.get_model("organization", "Tenant")
    except LookupError:
        logger.warning("Login discovery domain %s maps to unavailable tenant slug %s.", domain, slug)
        return None
    is_live = tenant_model._base_manager.filter(slug=slug, deleted_at__isnull=True).exists()
    if is_live:
        return slug
    logger.warning("Login discovery domain %s maps to missing or deleted tenant slug %s.", domain, slug)
    return None


def resolve_login_route(identifier, next_url=""):
    """Resolve an identifier to one tenant-bound SSO action or password login.

    The local part is used only as input to domain extraction and is never
    queried or logged. Provider destinations are produced by ``reverse()`` in
    ``get_login_providers``; the caller cannot supply a tenant or destination.
    """
    domain = email_domain(identifier)
    if domain is None:
        logger.info("Login discovery has no claimable email domain; using password login.")
        return _password_route()

    tenant_slug = _tenant_slug_for_domain(domain)
    if tenant_slug is None:
        logger.info("Login discovery found no live tenant mapping for domain %s; using password login.", domain)
        return _password_route()

    safe_next = _safe_next_url(next_url)
    actions = get_login_providers(safe_next, tenant_slug=tenant_slug)
    actions = tuple(
        action
        for action in actions
        if action.get("tenant_bound") and action.get("url") and _matches_method(action, tenant_slug)
    )
    if not actions:
        config = getattr(settings, "ITAMBOX_TENANT_LOGIN_DOMAINS", {}).get(tenant_slug, {})
        method = config.get("method") if isinstance(config, dict) else None
        logger.warning(
            "Login discovery domain %s maps to tenant slug %s but has no usable %s provider.",
            domain,
            tenant_slug,
            method or "SSO",
        )
        return _password_route()
    if len(actions) == 1:
        logger.info(
            "Login discovery resolved domain %s for tenant slug %s with one %s provider; redirecting.",
            domain,
            tenant_slug,
            actions[0]["protocol"],
        )
        return LoginRoute(mode="redirect", url=actions[0]["url"], actions=actions)

    neutral_actions = tuple({"protocol": action["protocol"], "url": action["url"]} for action in actions)
    logger.info(
        "Login discovery resolved domain %s for tenant slug %s with %d providers; presenting choices.",
        domain,
        tenant_slug,
        len(actions),
    )
    return LoginRoute(mode="choose", url=reverse("login"), actions=neutral_actions)


def _matches_domain(candidate, expected):
    try:
        return normalize_domain(candidate) == expected
    except ValueError:
        return False


def _matches_method(action, tenant_slug):
    mapping = getattr(settings, "ITAMBOX_TENANT_LOGIN_DOMAINS", {})
    config = mapping.get(tenant_slug, {}) if isinstance(mapping, dict) else {}
    method = config.get("method") if isinstance(config, dict) else None
    return not method or str(action.get("protocol", "")).lower() == str(method).lower()


def _safe_next_url(value):
    if not isinstance(value, str) or not value:
        return ""
    allowed_hosts = {host for host in getattr(settings, "ALLOWED_HOSTS", ()) if host != "*"}
    if not url_has_allowed_host_and_scheme(value, allowed_hosts=allowed_hosts):
        return ""
    return value


def _password_route():
    return LoginRoute(mode="password", url=reverse("login"), actions=())
