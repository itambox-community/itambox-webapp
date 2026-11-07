"""Small, settings-safe email-domain normalization helpers.

This module intentionally has no auth or model imports: settings load it before
Django's app registry is ready, and importing ``core.auth`` there would resolve
the configured user model too early.
"""

import ipaddress
import re

PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "126.com",
        "163.com",
        "aol.com",
        "att.net",
        "btinternet.com",
        "daum.net",
        "email.com",
        "fastmail.com",
        "fastmail.fm",
        "foxmail.com",
        "gmail.com",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "googlemail.com",
        "hanmail.net",
        "hotmail.com",
        "icloud.com",
        "inbox.com",
        "live.com",
        "list.ru",
        "mac.com",
        "mail.com",
        "mail.ru",
        "me.com",
        "msn.com",
        "naver.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "rambler.ru",
        "rediffmail.com",
        "rocketmail.com",
        "sina.com",
        "sina.cn",
        "sohu.com",
        "t-online.de",
        "talktalk.net",
        "tuta.com",
        "tuta.io",
        "tutanota.com",
        "tutamail.com",
        "verizon.net",
        "web.de",
        "yahoo.co.jp",
        "yahoo.co.uk",
        "yahoo.com",
        "yahoo.com.au",
        "yahoo.fr",
        "yahoo.in",
        "yandex.com",
        "yandex.ru",
        "yeah.net",
        "ymail.com",
        "zoho.com",
        "zohomail.com",
    }
)

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", re.ASCII)


def normalize_domain(value):
    """Return a validated lowercase IDNA A-label domain, or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("must be a string")
    domain = value.strip()
    if domain.endswith("."):
        domain = domain[:-1]
    if not domain or domain.endswith("."):
        raise ValueError("must contain non-empty domain labels")
    if any(character.isspace() for character in domain) or any(character in domain for character in "@/:\\?#"):
        raise ValueError("must be a domain name, not an address or URL")
    # The standard-library codec implements IDNA 2003, so nameprep folds ``ß``
    # to ``ss`` (``straße.de`` and ``strasse.de`` normalise to the same label).
    # This is intended: normalisation only routes the login page to a tenant's
    # SSO action and never feeds credentials or account matching, and folding
    # makes the visitor's spelling and the mapped spelling meet. Moving to
    # IDNA 2008 would split those two spellings and needs its own
    # compatibility note; do not switch codecs silently.
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("is not a valid IDNA domain") from exc
    if len(domain) > 253:
        raise ValueError("exceeds the 253-character domain limit")
    labels = domain.split(".")
    if len(labels) < 2 or any(len(label) > 63 or not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ValueError("must contain valid DNS labels")
    if _is_ip_literal(domain):
        raise ValueError("IP address literals are not allowed")
    return domain


def _is_ip_literal(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def email_domain(identifier):
    """Return a normalized email domain, or ``None`` when no valid domain exists."""
    if not isinstance(identifier, str):
        return None
    identifier = identifier.strip()
    if "@" not in identifier:
        return None
    local_part, domain = identifier.rsplit("@", 1)
    if not local_part.strip():
        return None
    try:
        normalized = normalize_domain(domain)
    except ValueError:
        return None
    return normalized
