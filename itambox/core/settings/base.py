"""
Django base settings for ITAMbox.
Contains all common settings shared between dev and prod.
"""

import json
import os
import sys
import warnings as py_warnings
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

from core.config_contract import (
    parse_api_token_peppers,
    parse_field_encryption_keys,
)
from itambox.plugins.utils import load_plugins
from itambox.release import VERSION

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env_path = BASE_DIR / ".env"
if not env_path.exists():
    env_path = BASE_DIR.parent / ".env"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            os.environ.setdefault(k, v)

DEBUG = False

# Public base URL used to construct bearer handoff links (no trailing slash).
# Set in production to e.g. https://itam.example.com. An invalid non-empty value
# is rejected by the compliance system check rather than silently normalised.
ITAMBOX_BASE_URL = os.environ.get("ITAMBOX_BASE_URL", "")

# Password assigned to users created by the demo seed. The deployed demo can set
# this independently without changing the source fallback used by local setups.
SEED_PASSWORD = os.environ.get("ITAMBOX_SEED_PASSWORD", "itambox2026")

# Default display currency (ISO 4217) used when a tenant has not set its own.
# Affects the {% money %} template filter — display only, no exchange rates.
ITAMBOX_DEFAULT_CURRENCY = os.environ.get("ITAMBOX_DEFAULT_CURRENCY", "EUR")

# Bound outbound SMTP so a dead/unreachable mail server can't block a web request
# or background task indefinitely. Django's SMTP EmailBackend defaults its timeout
# to this setting; None (Django's default) means NO socket timeout — a hang risk.
# The NotificationChannel email path (core/events.py) relies on it. Seconds.
EMAIL_TIMEOUT = int(os.environ.get("ITAMBOX_EMAIL_TIMEOUT", "10"))

SECRET_KEY = os.environ.get("ITAMBOX_SECRET_KEY", "")

if not SECRET_KEY:
    import warnings

    SECRET_KEY = "django-insecure-dev-only-change-me-in-production"
    warnings.warn(
        "ITAMBOX_SECRET_KEY environment variable is not set. Using insecure default key. "
        "Do NOT use this configuration in production!",
        UserWarning,
        stacklevel=2,
    )

INSTALLED_APPS = [
    "core.apps.SuperuserAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # MFA (TOTP) for local password login — must follow django.contrib.auth.
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "corsheaders",
    "assets",
    "inventory",
    "compliance",
    "organization",
    "software",
    "subscriptions",
    "licenses",
    "django_tables2",
    "template_partials",
    "django_htmx",
    "django_filters",
    "crispy_forms",
    "crispy_bootstrap5",
    "itambox.apps.ITAMBoxConfig",
    "core.apps.CoreConfig",
    "extras.apps.ExtrasConfig",
    "procurement.apps.ProcurementConfig",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "users",
    "django_q",
    "mozilla_django_oidc",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Every djangosaml2 view reads request.saml_session (outstanding queries,
    # identity cache); without this the SAML entry point raises AttributeError
    # instead of starting a flow. It only writes its cookie while a SAML flow is
    # in progress, so non-SAML requests are unaffected.
    "djangosaml2.middleware.SamlSessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # django-otp must follow AuthenticationMiddleware; our enforcement gate runs
    # right after so request.user.is_verified() is available to it.
    "django_otp.middleware.OTPMiddleware",
    "core.otp_middleware.OTPEnforcementMiddleware",
    "itambox.middleware.RateLimitMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "itambox.middleware.CSPMiddleware",
    "itambox.middleware.CurrentUserMiddleware",
    "itambox.middleware.TenantMiddleware",
]

INTERNAL_IPS = [
    "127.0.0.1",
]

ROOT_URLCONF = "core.urls"

default_loaders = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]

partial_loaders = [("template_partials.loader.Loader", default_loaders)]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "itambox.context_processors.settings_processor",
                "itambox.context_processors.notifications_processor",
                "organization.views.context_processors.tenant_switcher_processor",
                "assets.views.context_processors.bulk_scan_capabilities",
                "itambox.context_processors.base_template_processor",
                "itambox.context_processors.plugin_diagnostics_processor",
            ],
            "loaders": partial_loaders,
            "libraries": {
                "navigation": "core.templatetags.navigation",
            },
            "builtins": [
                "core.templatetags.panels",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

DB_ENGINE = os.environ.get("ITAMBOX_DB_ENGINE", "django.db.backends.postgresql")

if "sqlite" in DB_ENGINE:
    raise ImproperlyConfigured(
        "SQLite is strictly deprecated and not supported in ITAMbox. Please use PostgreSQL 15+ for all environments."
    )

DATABASES = {
    "default": {
        "ENGINE": DB_ENGINE,
        "NAME": os.environ.get("ITAMBOX_DB_NAME", "itambox"),
        "USER": os.environ.get("ITAMBOX_DB_USER", "itambox"),
        # The implicit ``itambox`` password is development-only: dev.py applies
        # it as a local convenience fallback, while production requires an
        # explicitly configured ITAMBOX_DB_PASSWORD (see prod.py) and the
        # bundled Compose stack fails closed without one.
        "PASSWORD": os.environ.get("ITAMBOX_DB_PASSWORD", ""),
        "HOST": os.environ.get("ITAMBOX_DB_HOST", "localhost"),
        "PORT": os.environ.get("ITAMBOX_DB_PORT", "5432"),
        "CONN_MAX_AGE": int(os.environ.get("ITAMBOX_DB_CONN_MAX_AGE", "300")),
        "OPTIONS": {
            "sslmode": os.environ.get("ITAMBOX_DB_SSLMODE", "require"),
            # TCP keepalives: detect a dead/dropped Postgres connection (e.g. a
            # backend killed under load) so a libpq recv() blocked waiting for a
            # response fails fast with OperationalError (~25s) instead of hanging
            # the client forever. The lock_timeout/statement_timeout below are
            # SERVER-side and useless once the server has closed the socket; this
            # is the missing client-side guard for the suite hang that pytest
            # --timeout cannot interrupt on Windows (it blocks in C).
            "keepalives": 1,
            "keepalives_idle": 10,
            "keepalives_interval": 5,
            "keepalives_count": 3,
        },
        "TEST": {
            "NAME": "oidc_test_db",
        },
    }
}

# Custom user model: stock AbstractUser + a dedicated ``can_login`` flag (see users.models.User).
AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_L10N = True
USE_TZ = True

LANGUAGES = [
    ("en", _("English")),
    ("de", _("German")),
]

LOCALE_PATHS = [
    BASE_DIR / "locale",
]

STATIC_URL = "/static/"
# STATIC_ROOT is the collectstatic target. Required for `manage.py collectstatic`
# (and therefore for the production image build) — Django raises without it.
STATIC_ROOT = os.environ.get("ITAMBOX_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [
    BASE_DIR / "static",
]

MEDIA_URL = "/media/"
MEDIA_ROOT = os.environ.get("ITAMBOX_MEDIA_ROOT", str(BASE_DIR / "media"))

# Upload size ceilings — kept coherent with the file-attachment validators (10 MB file /
# 5 MB image). Django's 2.5 MB default DATA_UPLOAD_MAX_MEMORY_SIZE would otherwise reject the
# non-file portion of a large multipart upload (and the large import_text textarea) before
# the validators run.
FILE_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024  # 11 MB (> the 10 MB file validator)
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024  # 12 MB

# Local static end-user documentation root
DOCS_ROOT = os.environ.get("ITAMBOX_DOCS_ROOT", BASE_DIR / "docs")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

REST_FRAMEWORK = {
    # drf-spectacular's AutoSchema plus the x-itambox-maturity extension, so a
    # generated client can see which endpoints are still settling.
    "DEFAULT_SCHEMA_CLASS": "itambox.api.openapi.CapabilityAwareAutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "users.api.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "itambox.api.permissions.TokenPermissions",
        "itambox.api.permissions.StrictTenantPermission",
    ],
    "DEFAULT_PAGINATION_CLASS": "itambox.api.pagination.ITAMBoxPagination",
    "EXCEPTION_HANDLER": "itambox.api.exceptions.itambox_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
    },
    "VIEW_NAME_FUNCTION": "itambox.api.utils.get_view_name",
}


SPECTACULAR_SETTINGS = {
    "TITLE": "ITAMbox API",
    "DESCRIPTION": "IT Asset Management API",
    "VERSION": VERSION,
    "COMPONENT_SPLIT_REQUEST": True,  # Critical for clean client gen
    "ENUM_ADD_EXPLICIT_BLANK_NULL_CHOICE": False,
    # ``status`` is used by several models with different choice sets. Keep
    # the asset-request values under an explicit, reviewable identity instead
    # of accepting drf-spectacular's hash-suffixed collision name.
    "ENUM_NAME_OVERRIDES": {
        "AssetRequestStatusEnum": [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("procurement", "Awaiting Procurement"),
            ("denied", "Denied"),
            ("fulfilled", "Fulfilled"),
            ("cancelled", "Cancelled"),
        ],
    },
    "SCHEMA_PATH_PREFIX": r"/api/",
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
}


PAGINATE_COUNT = 50
MAX_PAGE_SIZE = 1000

# Upper bound for the list-page row counter (EnhancedPaginator). A plain
# SELECT COUNT(*) scans the whole filtered table on every list view, which is
# slow at hundreds of thousands of rows. The paginator counts only up to this
# many rows; below the cap the displayed total is exact (small tables and tests
# are unaffected), above it the UI shows "<cap>+". Set 0 to disable capping.
ITAMBOX_PAGINATOR_COUNT_CAP = int(os.environ.get("ITAMBOX_PAGINATOR_COUNT_CAP", "100000"))

CACHE_BACKEND = os.environ.get("ITAMBOX_CACHE_BACKEND", "locmem")
if CACHE_BACKEND == "redis":
    CACHES = {
        "default": {
            # django-redis backend: it accepts the CLIENT_CLASS option below.
            # Django's built-in django.core.cache.backends.redis.RedisCache does
            # NOT, and passes it through to the redis client (TypeError).
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": os.environ.get("ITAMBOX_REDIS_URL", "redis://127.0.0.1:6379/1"),
            "TIMEOUT": int(os.environ.get("ITAMBOX_CACHE_TIMEOUT", "300")),
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "itambox-cache",
        }
    }

SEARCH_BACKEND = "core.search_backends.DatabaseBackend"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# djangosaml2 may initiate authentication with an auto-submitted HTTP-POST form.
# Pin our nonce-bearing project template so it never falls back to pysaml2's
# inline body-onload form, which the application CSP correctly blocks.
SAML_POST_BINDING_FORM_TEMPLATE = "djangosaml2/post_binding_form.html"
# Its custom handler marks only SAML responses so our CSP middleware can permit
# an HTTPS IdP target without relaxing form-action on the rest of the application.
SAML_CSP_HANDLER = "itambox.middleware.allow_saml_https_form_action"

AUTHENTICATION_BACKENDS = [
    # MembershipBackend is the single authorization path: all permissions resolve
    # through RoleGrant + RoleGrantScope rows.
    "core.auth.MembershipBackend",
    "core.auth.ldap.MultiTenantLDAPBackend",
    "core.auth.saml.TenantSaml2Backend",
    "core.auth.oidc.TenantOIDCBackend",
    "core.auth.PasswordLoginOnlyBackend",
]

# Enforce TOTP MFA for local-password logins by superusers/owner/admin roles
# (OTPEnforcementMiddleware). Off by default so dev/test behave as before; prod
# turns it on (see prod.py). SSO/LDAP/SAML/OIDC delegate MFA to the IdP and are
# always exempt regardless of this flag.
MFA_ENFORCED = os.environ.get("ITAMBOX_REQUIRE_MFA", "False").lower() in ("true", "1", "t")

DEFAULT_PAGINATE_COUNT = 25
PAGINATE_COUNT_CHOICES = (
    (25, "25"),
    (50, "50"),
    (100, "100"),
    (250, "250"),
    (500, "500"),
    (1000, "1000"),
)


def _load_tenant_json_config(environment_name):
    raw = os.environ.get(environment_name, "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured(
            f"{environment_name} contains invalid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    if not isinstance(value, dict):
        raise ImproperlyConfigured(f"{environment_name} must contain a JSON object.")
    return value


def _load_requisition_auto_approval_thresholds():
    canonical_name = "ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS"
    legacy_name = "REQUISITION_AUTO_APPROVAL_THRESHOLDS"
    raw = os.environ.get(canonical_name)
    if raw is None:
        raw = os.environ.get(legacy_name)
        if raw is not None:
            py_warnings.warn(
                f"{legacy_name} is deprecated; configure {canonical_name} instead. "
                "The legacy fallback will be removed in ITAMbox 2.0.",
                UserWarning,
                stacklevel=2,
            )
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured(
            f"{canonical_name} contains invalid JSON at line {exc.lineno}, column {exc.colno}."
        ) from exc
    if not isinstance(value, dict):
        raise ImproperlyConfigured(f"{canonical_name} must contain a JSON object.")
    supported_items = {"accessory", "consumable"}
    if set(value) - supported_items:
        raise ImproperlyConfigured(f"{canonical_name} supports only the 'accessory' and 'consumable' keys.")
    if any(
        isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0 for threshold in value.values()
    ):
        raise ImproperlyConfigured(f"{canonical_name} thresholds must be non-negative integers.")
    return value


ITAMBOX_TENANT_LDAP_CONFIGS = _load_tenant_json_config("ITAMBOX_TENANT_LDAP_CONFIGS")
ITAMBOX_TENANT_SAML_CONFIGS = _load_tenant_json_config("ITAMBOX_TENANT_SAML_CONFIGS")
ITAMBOX_TENANT_OIDC_CONFIGS = _load_tenant_json_config("ITAMBOX_TENANT_OIDC_CONFIGS")
ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS = _load_requisition_auto_approval_thresholds()

# Intune discovery connector — per-tenant config.
# Keys per tenant slug: azure_tenant_id, client_id, client_secret,
#   create_missing (bool, default false), default_status (StatusLabel slug, default "deployable"),
#   sync_software (bool, default true).
ITAMBOX_TENANT_INTUNE_CONFIGS = _load_tenant_json_config("ITAMBOX_TENANT_INTUNE_CONFIGS")


# SAML SSO Configuration Loader
SAML_CONFIG_LOADER = "core.auth.saml.load_saml_config"
SAML_USE_NAME_ID_AS_USERNAME = True
SAML_CREATE_UNKNOWN_USER = True
SAML_ATTRIBUTE_MAPPING = {
    "email": ("email",),
    "first_name": ("first_name", "givenName"),
    "last_name": ("last_name", "sn"),
}


# ==============================================================================
# Django-Q2 Background Tasks Cluster Settings
# ==============================================================================
Q_CLUSTER = {
    "name": "ITAMbox-Cluster",
    "workers": 2,  # Safe count for local developer workloads
    "recycle": 500,
    "timeout": 600,
    "retry": 660,
    "compress": True,
    "cpu_affinity": 1,
    "label": "Django Q Cluster",
    "orm": "default",  # Standard database ORM broker
    # Don't persist successful tasks (we only fire-and-forget async_task, never
    # fetch results); failures are always saved -- django-q2 itself never
    # prunes them. That used to mean the failure table grew forever; it's now
    # closed by the `prune_changelog` management command (core/management/
    # commands/prune_changelog.py), which deletes Failure rows older than
    # ITAMBOX_QTASK_FAILED_RETENTION_DAYS (default 90; 0 = unlimited) on the
    # daily schedule CoreConfig registers in core/apps.py, so list_failed_tasks
    # stays complete for recent failures without unbounded growth.
    "save_limit": -1,
    # Frozen V1 scheduled-report contract: after worker downtime the scheduler
    # replays the OLDEST missed occurrence, one replay per scheduling pass,
    # until the schedule catches up, and each occurrence is delivered at most
    # once. Pinned explicitly so a future library default cannot silently
    # change the documented replay behavior.
    "catch_up": True,
}


def _is_test_invocation():
    argv_identifies_tests = "test" in sys.argv or any("test" in arg or "pytest" in arg for arg in sys.argv)
    xdist_identifies_tests = any(os.environ.get(name) for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID"))
    explicit_non_production = (
        os.environ.get("ITAMBOX_ENV") == "dev" or os.environ.get("DJANGO_SETTINGS_MODULE") == "core.settings.dev"
    )
    return argv_identifies_tests or (explicit_non_production and xdist_identifies_tests)


_TEST_INVOCATION = _is_test_invocation()


def _configure_test_environment(is_test_invocation):
    if not is_test_invocation:
        return

    Q_CLUSTER["sync"] = True

    # --- Test-suite DB hardening (guards against an unbounded, order-dependent
    # suite hang). Two layered fixes:
    #   1. CONN_MAX_AGE=0 — kill persistent connections in tests. With the prod
    #      default (300s) a connection that opened a transaction/held a lock
    #      survives the test that created it and lingers into later tests.
    #   2. lock_timeout — a TransactionTestCase teardown TRUNCATEs every table;
    #      that TRUNCATE needs ACCESS EXCLUSIVE and, with no timeout, waits
    #      FOREVER on any lock such a lingering session still holds (a hang that
    #      blocks in libpq's C recv, so pytest --timeout can't even kill it).
    #      30s lock_timeout turns that 1h+ deadlock into a fast, diagnosable
    #      OperationalError. statement_timeout (10min) is a generous backstop for
    #      any other runaway query — high enough to never trip a real test or a
    #      migration statement during test-DB build.
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"]["OPTIONS"]["options"] = "-c lock_timeout=30000 -c statement_timeout=600000"


_configure_test_environment(_TEST_INVOCATION)

# ==============================================================================
# Data Retention (changelog / operational-data pruning)
# ==============================================================================
# Retention windows for `prune_changelog` (core/management/commands/
# prune_changelog.py), scheduled to run daily via the Schedule CoreConfig
# registers in core/apps.py. Each is a day count measured against the row's
# own timestamp (ObjectChange.time / AlertLog.created_at /
# Notification.created_at / Event.timestamp / django-q2 Failure.stopped). 0
# means unlimited: that data class is never pruned by age. The changelog also
# supports a per-tenant override -- Tenant.changelog_retention_days
# (organization/models.py; null = use this setting, 0 = unlimited/legal hold)
# -- which always wins over this setting for that tenant's rows.
ITAMBOX_CHANGELOG_RETENTION_DAYS = int(os.environ.get("ITAMBOX_CHANGELOG_RETENTION_DAYS", "365"))
ITAMBOX_ALERTLOG_RETENTION_DAYS = int(os.environ.get("ITAMBOX_ALERTLOG_RETENTION_DAYS", "180"))
ITAMBOX_NOTIFICATION_RETENTION_DAYS = int(os.environ.get("ITAMBOX_NOTIFICATION_RETENTION_DAYS", "90"))
# Default 0 = retain / never prune.  Pruning starts only when an operator
# explicitly configures a positive value.
ITAMBOX_EVENT_RETENTION_DAYS = int(os.environ.get("ITAMBOX_EVENT_RETENTION_DAYS", "0"))
# Pruned against django-q2's own Failure proxy model (Task rows with
# success=False). See the `save_limit` comment on Q_CLUSTER above.
ITAMBOX_QTASK_FAILED_RETENTION_DAYS = int(os.environ.get("ITAMBOX_QTASK_FAILED_RETENTION_DAYS", "90"))

# ------------------------------------------------------------------------------
# Operational health thresholds (`manage.py operational_health`)
# ------------------------------------------------------------------------------
# Read-only diagnostics for the worker, scheduler, queue and failure state; see
# docs/operations/operational-health.md. `/health/` is unaffected by these.
ITAMBOX_HEALTH_SCHEDULE_GRACE_SECONDS = int(os.environ.get("ITAMBOX_HEALTH_SCHEDULE_GRACE_SECONDS", "900"))
ITAMBOX_HEALTH_QUEUE_BACKLOG_MAX = int(os.environ.get("ITAMBOX_HEALTH_QUEUE_BACKLOG_MAX", "1000"))
ITAMBOX_HEALTH_QUEUE_AGE_SECONDS = int(os.environ.get("ITAMBOX_HEALTH_QUEUE_AGE_SECONDS", "1800"))
ITAMBOX_HEALTH_JOB_STUCK_SECONDS = int(os.environ.get("ITAMBOX_HEALTH_JOB_STUCK_SECONDS", "3600"))
ITAMBOX_HEALTH_FAILURES_24H_MAX = int(os.environ.get("ITAMBOX_HEALTH_FAILURES_24H_MAX", "10"))

ALLOW_GLOBAL_CUSTODY_TEMPLATES = os.environ.get("ITAMBOX_ALLOW_GLOBAL_CUSTODY_TEMPLATES", "True") == "True"
REQUIRE_CUSTODY_SIGNIN = os.environ.get("ITAMBOX_REQUIRE_CUSTODY_SIGNIN", "True") == "True"
CUSTODY_SIGNING_SESSION_TTL = timedelta(minutes=30)

# Server-side peppers used to HMAC-hash API tokens at rest (NetBox v4.5 style).
# JSON object of {"<numeric id>": "<>=50-char secret>"}; the highest id hashes
# new tokens, older ids remain valid so peppers can be rotated. When unset, the
# token model falls back to a SECRET_KEY-derived pepper (fine for dev/tests);
# production should set ITAMBOX_API_TOKEN_PEPPERS to a dedicated, secret value.
#
# The parse result is tri-state (unset | valid | malformed) and retained on the
# module so production settings can warn on unset and refuse to import on
# explicitly malformed material instead of silently downgrading to {}.
_pepper_keyring = parse_api_token_peppers(os.environ.get("ITAMBOX_API_TOKEN_PEPPERS"))
API_TOKEN_PEPPERS = _pepper_keyring.peppers
API_TOKEN_PEPPERS_STATE = _pepper_keyring.state.value
API_TOKEN_PEPPERS_ERROR = _pepper_keyring.error

# Field-encryption keyring parse state (core.crypto owns the key resolution and
# derives the SECRET_KEY fallback; production settings use these two states to
# warn on unset and refuse to import on malformed keyring material).
_field_keyring = parse_field_encryption_keys(os.environ.get("ITAMBOX_FIELD_ENCRYPTION_KEYS"))
FIELD_ENCRYPTION_KEYS_STATE = _field_keyring.state.value
FIELD_ENCRYPTION_KEYS_ERROR = _field_keyring.error

# ==============================================================================
# Plugins Configuration
# ==============================================================================
# Plugins are optional Django apps resolved at settings time (see
# itambox/plugins/utils.py). They are configured via a comma-separated env var
# so the core image boots without bundling any plugin. A missing package, an
# import failure or an invalid manifest aborts startup with a message naming the
# plugin and the phase. The single exception is a version incompatibility: a
# plugin whose declared ITAMbox range excludes this release is skipped with a
# warning before it enters INSTALLED_APPS.
#   Example: ITAMBOX_PLUGINS=example_plugin
PLUGINS = [p.strip() for p in os.environ.get("ITAMBOX_PLUGINS", "").split(",") if p.strip()]

IS_TESTING = _TEST_INVOCATION

# Operator configuration channel: one JSON object keyed by plugin package name,
# validated against each plugin's ``settings_schema``. Single values (secrets)
# can instead be injected as ITAMBOX_PLUGIN_<NAME>_<KEY>, which wins over the
# JSON entry. The core ships no plugin configuration.
#   Example: ITAMBOX_PLUGINS_CONFIG='{"example_plugin": {"endpoint": "https://x"}}'
ITAMBOX_PLUGINS_CONFIG = _load_tenant_json_config("ITAMBOX_PLUGINS_CONFIG")
PLUGINS_CONFIG: dict[str, dict] = ITAMBOX_PLUGINS_CONFIG

# Load and validate plugins dynamically


load_plugins(sys.modules[__name__])

# Strawberry-Django exposes model ``help_text`` as the GraphQL field description,
# keeping the descriptions the schema published before the Strawberry port.
STRAWBERRY_DJANGO = {
    "FIELD_DESCRIPTION_FROM_HELP_TEXT": True,
}

# ==============================================================================
# Logging
# ==============================================================================
# Console logging is the right default for containers (the orchestrator
# captures stdout/stderr). Level is env-tunable; Django request 5xx errors and
# our app loggers are surfaced explicitly.
LOG_LEVEL = os.environ.get("ITAMBOX_LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "itambox.logging_filters.RequestIDFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} [{request_id}] {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "itambox": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}


# ==============================================================================
# CORS Configuration
# ==============================================================================
CORS_ALLOW_ALL_ORIGINS = os.environ.get("ITAMBOX_CORS_ALLOW_ALL_ORIGINS", "False").lower() in ("true", "1", "yes")
CORS_ALLOWED_ORIGINS = [
    origin.strip() for origin in os.environ.get("ITAMBOX_CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()
]
