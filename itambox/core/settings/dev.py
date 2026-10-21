"""
Development settings override.
To use: set DJANGO_SETTINGS_MODULE=core.settings.dev or ITAMBOX_ENV=dev
"""

import os as _os

# Development settings inherit the full base namespace, then override selected values.
from .base import *  # noqa: F403, F401
from .base import DATABASES, INSTALLED_APPS, MIDDLEWARE

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Local/CI Postgres instances typically lack TLS. Override the base default
# ('require') so that dev and test runs connect without a certificate.
# Production keeps 'require' (or overrides via ITAMBOX_DB_SSLMODE env var).

if not _os.environ.get("ITAMBOX_DB_SSLMODE"):
    DATABASES["default"]["OPTIONS"]["sslmode"] = "disable"

# Mailpit local SMTP catcher settings for local development
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "127.0.0.1"
EMAIL_PORT = 1025
EMAIL_USE_TLS = False
EMAIL_USE_SSL = False

# The implicit ``itambox`` database password is a development-only convenience
# fallback. Production requires an explicitly configured ITAMBOX_DB_PASSWORD
# (see prod.py); the bundled Compose stack fails closed without one.
if not DATABASES["default"]["PASSWORD"]:
    DATABASES["default"]["PASSWORD"] = "itambox"

# Add debug_toolbar dynamically for development
if "debug_toolbar" not in INSTALLED_APPS:
    INSTALLED_APPS.append("debug_toolbar")
if "debug_toolbar.middleware.DebugToolbarMiddleware" not in MIDDLEWARE:
    MIDDLEWARE.append("debug_toolbar.middleware.DebugToolbarMiddleware")
