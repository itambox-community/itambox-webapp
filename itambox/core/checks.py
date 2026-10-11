"""
Production configuration-contract system checks (issue #439).

Reporting surface only: enforcement happens during the import of
``core.settings.prod`` (every entry point — Gunicorn, qcluster, management
commands — imports the settings before doing anything else), so a malformed
production configuration can never reach the point where these checks run.

The checks are registered under the ``prod`` tag, so they participate in the
default check surface of every management command as well as in
``manage.py check --deploy`` (which runs every tag). In practice they add to
the operator-facing reporting surface:

* the unset/blank classification of ``ITAMBOX_API_TOKEN_PEPPERS`` and
  ``ITAMBOX_FIELD_ENCRYPTION_KEYS`` as loud warnings with the truthful
  fallback consequences;
* a defensive error classification should a malformed state ever be observed
  on a check surface (unreachable through the normal prod import path, which
  raises first);
* a warning when the tri-state attributes are entirely absent (a custom
  settings module that inherits neither base nor prod).

SECRET_KEY and the database password are deliberately not re-checked here:
Django's own ``security.W009`` deployment check covers the key, and a missing
database password aborts settings import before any check framework runs.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register


def _is_production():
    return not settings.DEBUG


@register("prod")
def check_production_api_token_peppers(app_configs, **kwargs):
    """Warn on the fallback, error on malformed pepper configuration."""
    if not _is_production():
        return []
    state = getattr(settings, "API_TOKEN_PEPPERS_STATE", "unsupported")
    if state == "unset":
        return [
            Warning(
                "ITAMBOX_API_TOKEN_PEPPERS is not set: API-token hashing falls "
                "back to a SECRET_KEY-derived pepper. Tokens hashed under the "
                "fallback stop validating once a dedicated mapping is "
                "configured - re-issue them.",
                id="core.W001",
            )
        ]
    if state == "malformed":
        return [
            Error(
                "ITAMBOX_API_TOKEN_PEPPERS is malformed: "
                f"{getattr(settings, 'API_TOKEN_PEPPERS_ERROR', 'unknown reason')}.",
                id="core.E001",
            )
        ]
    if state == "unsupported":
        return [
            Warning(
                "The API-token pepper configuration state is unavailable in this "
                "settings module; enforcement may be bypassed.",
                id="core.W003",
            )
        ]
    return []


@register("prod")
def check_production_field_encryption_keys(app_configs, **kwargs):
    """Warn on the fallback, error on malformed keyring configuration."""
    if not _is_production():
        return []
    state = getattr(settings, "FIELD_ENCRYPTION_KEYS_STATE", "unsupported")
    if state == "unset":
        return [
            Warning(
                "ITAMBOX_FIELD_ENCRYPTION_KEYS is not set: field encryption "
                "derives its key from SECRET_KEY. Rotating SECRET_KEY makes "
                "all encrypted fields permanently unrecoverable.",
                id="core.W002",
            )
        ]
    if state == "malformed":
        return [
            Error(
                "ITAMBOX_FIELD_ENCRYPTION_KEYS is malformed: "
                f"{getattr(settings, 'FIELD_ENCRYPTION_KEYS_ERROR', 'unknown reason')}.",
                id="core.E002",
            )
        ]
    if state == "unsupported":
        return [
            Warning(
                "The field-encryption keyring configuration state is unavailable in "
                "this settings module; enforcement may be bypassed.",
                id="core.W004",
            )
        ]
    return []


@register()
def check_archive_behaviour_tables(app_configs, **kwargs):
    """Every registered aggregate archive table must match the live model graph (#619).

    A child relation added to an aggregate root without a declared archive
    behaviour (or a declared relation that no longer exists) would let an
    archive silently orphan or ignore it, so the mismatch is a hard error.
    """
    # inline import: app-registry: the archive registry is populated by app ready() hooks.
    from core.archive_handlers import archive_table_problems

    return [Error(problem, id="core.E003") for problem in archive_table_problems()]


@register()
def check_archive_coverage(app_configs, **kwargs):
    """Every soft-deletable model with children is a registered root or a declared leaf (#619)."""
    # inline import: app-registry: the archive registry is populated by app ready() hooks.
    from core.archive_handlers import archive_coverage_problems

    return [Error(problem, id="core.E004") for problem in archive_coverage_problems()]
