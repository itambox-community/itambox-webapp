"""
Production settings override.
To use: set DJANGO_SETTINGS_MODULE=core.settings.prod or ITAMBOX_ENV=prod
"""

import os

from django.core.exceptions import ImproperlyConfigured

from core.config_contract import (
    SECRET_KEY_RULE_DIAGNOSTICS,
    ConfigState,
    validate_db_password,
    validate_secret_key,
)
from core.crypto import is_using_derived_encryption_key

# Production settings inherit the full base namespace, then override selected values.
from .base import *  # noqa: F403, F401

# The production configuration contract reads the tri-state parse results from
# ``base``. They are re-imported explicitly so flake8 can resolve them instead
# of reporting star-import F405 identities for the new names.
from .base import (
    API_TOKEN_PEPPERS_ERROR,
    API_TOKEN_PEPPERS_STATE,
    CACHE_BACKEND,
    FIELD_ENCRYPTION_KEYS_ERROR,
    FIELD_ENCRYPTION_KEYS_STATE,
    MIDDLEWARE,
    REST_FRAMEWORK,
    SECRET_KEY,
)

# Hardcoded, not env-toggleable — unlike every other flag in this file, DEBUG
# has no legitimate production use. A leftover/templated .env with
# ITAMBOX_DEBUG=True (e.g. copied from .env.example and only half-edited when
# switching ITAMBOX_ENV to prod) must not be able to flip this on. An operator
# who needs DEBUG=True should run core.settings.dev instead.
DEBUG = False

# ---- Production configuration contract ------------------------------------
# Each check below runs during settings import — the earliest guaranteed point
# before Gunicorn, qcluster, or a management command can serve or process work.
# All diagnostics are secret-free.

# SECRET_KEY: full Django security.W009 parity (>= 50 chars, >= 5 distinct
# characters, no 'django-insecure-' prefix). A missing/empty key materializes
# as the base-settings development fallback and is rejected by the
# forbidden-prefix rule. The rule is named, never the key.
_secret_key_result = validate_secret_key(SECRET_KEY)
if not _secret_key_result.valid:
    rule = _secret_key_result.failed_rule
    # A missing/empty configuration materializes as the base-settings dev
    # fallback; report the honest "missing" rule instead of a prefix rule.
    if rule == "forbidden_prefix" and not os.environ.get("ITAMBOX_SECRET_KEY"):
        rule = "missing"
    # Rotating a rejected operator-provided key is NOT a safe remediation on
    # its own when the SECRET_KEY-derived fallbacks are in use: the operator
    # would silently lose every encrypted field and fallback-hashed API token.
    # Earlier releases accepted any key except the exact dev sentinel, so even
    # a 'django-insecure-' prefixed operator key may underpin derived state —
    # every rejected non-missing rule gets the preservation precondition. Only
    # the absent/empty-development-fallback state (normalized to "missing")
    # carries no operator secret worth preserving.
    preservation_hint = ""
    if rule in ("too_short", "too_few_distinct_chars", "forbidden_prefix") and (
        FIELD_ENCRYPTION_KEYS_STATE == ConfigState.UNSET.value or API_TOKEN_PEPPERS_STATE == ConfigState.UNSET.value
    ):
        preservation_hint = (
            " Keep the current key until you have pinned "
            "ITAMBOX_FIELD_ENCRYPTION_KEYS to the Fernet key derived from it "
            "(see the installation upgrade notes) and re-issued API tokens; "
            "rotating SECRET_KEY destroys encrypted fields and invalidates "
            "fallback-hashed tokens."
        )
    raise ImproperlyConfigured(
        f"ITAMBOX_SECRET_KEY {SECRET_KEY_RULE_DIAGNOSTICS[rule]}. "
        'Generate a value with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
        f"{preservation_hint}"
    )

# Database password: the implicit development default is gone; production
# requires an explicitly configured, non-empty value. "Explicitly configured"
# is the contract — the literal value is deliberately not inspected.
if not validate_db_password(os.environ.get("ITAMBOX_DB_PASSWORD")):
    raise ImproperlyConfigured(
        "ITAMBOX_DB_PASSWORD must be explicitly configured in production. "
        "The bundled Compose stack refuses to start without it (see docker-compose.yml)."
    )

# API-token peppers: only unset/blank may use the warned SECRET_KEY-derived
# fallback; explicitly malformed material is a startup failure and must never
# silently downgrade to {}.
if API_TOKEN_PEPPERS_STATE == ConfigState.MALFORMED.value:
    raise ImproperlyConfigured(
        f"ITAMBOX_API_TOKEN_PEPPERS is malformed: {API_TOKEN_PEPPERS_ERROR}. "
        'Configure a JSON object such as {"1": "<>=50-char secret>"}.'
    )
if API_TOKEN_PEPPERS_STATE == ConfigState.UNSET.value:
    import logging

    logging.getLogger(__name__).warning(
        "ITAMBOX_API_TOKEN_PEPPERS is not set in production: API-token hashing "
        "falls back to a SECRET_KEY-derived pepper. Tokens hashed under the "
        "fallback stop validating once a dedicated mapping is configured. "
        "Configure and back up a dedicated pepper mapping before reissuing tokens hashed with the fallback."
    )

# Field-encryption keyring: only unset/blank may use the warned
# SECRET_KEY-derived fallback; explicitly malformed key material (including
# separator/whitespace-only values) fails before traffic.
if FIELD_ENCRYPTION_KEYS_STATE == ConfigState.MALFORMED.value:
    raise ImproperlyConfigured(f"ITAMBOX_FIELD_ENCRYPTION_KEYS is malformed: {FIELD_ENCRYPTION_KEYS_ERROR}.")

ALLOWED_HOSTS = os.environ.get("ITAMBOX_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# Origins trusted for unsafe (POST/PUT/...) cross-origin requests. Behind an
# HTTPS proxy on a custom domain Django needs the scheme-qualified host here,
# e.g. ITAMBOX_CSRF_TRUSTED_ORIGINS=https://itam.example.com
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.environ.get("ITAMBOX_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()
]

SECURE_SSL_REDIRECT = os.environ.get("ITAMBOX_SECURE_SSL_REDIRECT", "True").lower() in ("true", "1", "t")
SECURE_HSTS_SECONDS = int(os.environ.get("ITAMBOX_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get("ITAMBOX_HSTS_INCLUDE_SUBDOMAINS", "True").lower() in ("true", "1", "t")
SECURE_HSTS_PRELOAD = os.environ.get("ITAMBOX_HSTS_PRELOAD", "True").lower() in ("true", "1", "t")
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Cloudflare Tunnel is the only trusted proxy in the demo deployment. Keep
# forwarded-client-IP handling disabled by default so directly reachable
# deployments cannot be bypassed with a forged X-Forwarded-For header.
RATELIMIT_USE_X_FORWARDED_FOR = os.environ.get("ITAMBOX_RATELIMIT_USE_X_FORWARDED_FOR", "False").lower() in (
    "true",
    "1",
    "t",
)
try:
    RATELIMIT_NUM_PROXIES = int(os.environ.get("ITAMBOX_RATELIMIT_NUM_PROXIES", "1"))
except ValueError as exc:
    raise ValueError("ITAMBOX_RATELIMIT_NUM_PROXIES must be a positive integer") from exc
if RATELIMIT_NUM_PROXIES < 1:
    raise ValueError("ITAMBOX_RATELIMIT_NUM_PROXIES must be a positive integer")

# Cap idle session lifetime in production (default 8h) — the 2-week Django
# default is too long for a multi-tenant asset system. Override via env.
SESSION_COOKIE_AGE = int(os.environ.get("ITAMBOX_SESSION_COOKIE_AGE", "28800"))

# Enforce TOTP MFA in production for superuser/owner/admin password logins.
# Override with ITAMBOX_REQUIRE_MFA=False to temporarily disable.
MFA_ENFORCED = os.environ.get("ITAMBOX_REQUIRE_MFA", "True").lower() in ("true", "1", "t")

# ------------------------------------------------------------------------------
# Static files: served by WhiteNoise straight from gunicorn (compressed +
# content-hashed). Add the middleware immediately after SecurityMiddleware.
# ------------------------------------------------------------------------------
if "whitenoise.middleware.WhiteNoiseMiddleware" not in MIDDLEWARE:
    _security_idx = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
    MIDDLEWARE.insert(_security_idx + 1, "whitenoise.middleware.WhiteNoiseMiddleware")

# Compression-only WhiteNoise storage (no manifest hashing). Manifest storage
# post-processes every CSS/JS file and hard-fails collectstatic when a vendored
# stylesheet references an asset that isn't shipped (e.g. a .css.map source map).
# Compression keeps assets small and WhiteNoise still serves them with ETag-based
# caching, without the brittle reference resolution.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# ------------------------------------------------------------------------------
# Email — env driven. Without this, Django falls back to SMTP on localhost:25
# and password resets / invitations / alert + report notifications fail.
# ------------------------------------------------------------------------------
EMAIL_BACKEND = os.environ.get("ITAMBOX_EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("ITAMBOX_EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("ITAMBOX_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("ITAMBOX_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("ITAMBOX_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("ITAMBOX_EMAIL_USE_TLS", "True").lower() in ("true", "1", "t")
EMAIL_USE_SSL = os.environ.get("ITAMBOX_EMAIL_USE_SSL", "False").lower() in ("true", "1", "t")
DEFAULT_FROM_EMAIL = os.environ.get("ITAMBOX_DEFAULT_FROM_EMAIL", "ITAMbox <no-reply@localhost>")
SERVER_EMAIL = os.environ.get("ITAMBOX_SERVER_EMAIL", DEFAULT_FROM_EMAIL)

# Drop BasicAuthentication in production — token + session auth is sufficient.
# BasicAuthentication transmits credentials on every request and is only needed
# for dev tooling (browsable API, curl one-liners).
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "users.api.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
}

# Rate limiting and SAML replay protection share the 'default' cache. Under
# multi-worker gunicorn a per-process LocMemCache makes counters per-worker
# (login limit x workers) and weakens SAML replay protection. Warn loudly.
if CACHE_BACKEND == "locmem":
    import logging

    logging.getLogger(__name__).warning(
        "ITAMBOX_CACHE_BACKEND=locmem in production: rate-limit counters and SAML "
        "replay protection are per-worker. Set ITAMBOX_CACHE_BACKEND=redis "
        "(+ ITAMBOX_REDIS_URL) for multi-worker deployments."
    )

# Field encryption falls back to deriving its key from SECRET_KEY when no
# stable ITAMBOX_FIELD_ENCRYPTION_KEYS is set. In that mode, rotating SECRET_KEY
# silently makes every encrypted field (License.product_key, EmailSettings
# smtp_password, WebhookEndpoint.secret) permanently unrecoverable. Warn loudly.

if is_using_derived_encryption_key():
    import logging

    logging.getLogger(__name__).warning(
        "ITAMBOX_FIELD_ENCRYPTION_KEYS is not set in production: field encryption "
        "derives its key from SECRET_KEY. Rotating SECRET_KEY will make all "
        "encrypted fields (License.product_key, EmailSettings smtp_password, "
        "WebhookEndpoint.secret) permanently unrecoverable. Set a stable "
        "ITAMBOX_FIELD_ENCRYPTION_KEYS value and back it up."
    )
