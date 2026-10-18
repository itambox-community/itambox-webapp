"""Fail-closed guards for the disposable performance qualification stack."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def is_loopback_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def require_local_database(database_settings: dict[str, object]) -> None:
    host = str(database_settings.get("HOST") or "").strip()
    if not host or host.casefold().rstrip(".") == "localhost" or is_loopback_ip(host):
        return
    raise RuntimeError("performance qualification refuses a non-local database host")


def require_loopback_http_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # Validate port syntax before any request is made.
    except ValueError as error:
        raise ValueError("--base-url must be a valid loopback HTTP origin") from error
    is_loopback_host = hostname is not None and (
        hostname.casefold().rstrip(".") == "localhost" or is_loopback_ip(hostname)
    )
    if (
        parsed.scheme != "http"
        or not is_loopback_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("--base-url must be a loopback-only HTTP origin without credentials or a path")
