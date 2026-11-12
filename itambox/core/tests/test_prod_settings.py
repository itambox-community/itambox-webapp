"""
Production settings-posture assertions (WS7-4, issue #439).

The whole pytest run executes under ``core.settings`` which resolves to the
*dev* settings module (the loader forces ENV='dev' for test runs), so
``core/settings/prod.py`` is otherwise never imported and its hardening is never
verified. A regression here — a secure cookie flipped off, HSTS dropped,
BasicAuthentication re-added, the sentinel-key guard removed, a malformed
production secret accepted — would ship silently.

These tests import the prod settings module *in isolation*, independent of
``sys.argv``: the environment is patched and ``core.settings.base`` +
``core.settings.prod`` are re-imported fresh so the prod module evaluates against
the patched environment. The live Django ``settings`` object is unaffected — it
copied the dev names into its own namespace at startup and reloading the source
modules here does not re-mutate it. An autouse fixture restores ``sys.modules``
and reloads ``base`` against the real environment after each test so no polluted
module leaks into the rest of the suite.

Entry-path tests (issue #439) additionally spawn bounded subprocesses so the
rejection is proven through the *real* startup commands (settings import,
Gunicorn/WSGI import, management commands, qcluster), not just through a helper.
"""

import importlib
import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import types
import warnings
from pathlib import Path
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from core import checks as production_checks

# A compliant secret key for the positive baseline cases (52 chars, many
# distinct, no forbidden prefix) — any value passing the W009-equivalent
# contract works.
SECURE_KEY = "prod-posture-test-stable-secret-key-0123456789abcdef"
# 32 url-safe-base64-decodable bytes -> a valid Fernet key, so the encryption
# layer treats ITAMBOX_FIELD_ENCRYPTION_KEYS as configured (not derived).
FERNET_KEY = "a" * 43 + "="
# An explicitly configured, compliant database password for the baseline.
DB_PASSWORD = "prod-posture-test-db-password"
# A compliant pepper mapping (50-char secret) for the baseline.
PEPPER_SECRET = "p" * 50
PEPPERS_JSON = json.dumps({"1": PEPPER_SECRET})

# The itambox/ project root, used to launch entry-path subprocesses.
ITAMBOX_DIR = Path(__file__).resolve().parents[2]

# A malformed-configuration marker that must never leak into any diagnostic.
SECRET_MARKER = "s3cr3t-marker-that-must-never-leak-into-diagnostics"

BASELINE_ENV = {
    "ITAMBOX_SECRET_KEY": SECURE_KEY,
    "ITAMBOX_CACHE_BACKEND": "redis",
    "ITAMBOX_FIELD_ENCRYPTION_KEYS": FERNET_KEY,
    "ITAMBOX_DB_PASSWORD": DB_PASSWORD,
    "ITAMBOX_API_TOKEN_PEPPERS": PEPPERS_JSON,
}


def _load_prod(extra_env):
    """
    Re-import core.settings.prod under a patched environment and return the
    freshly evaluated module.

    Baseline env pins the security-relevant inputs (compliant secret key,
    redis cache, explicit encryption key, explicit DB password, explicit
    peppers) so each test starts from a clean, deterministic posture and only
    flips what it asserts on. A ``None`` value in ``extra_env`` removes the
    variable entirely (the "unset" state). ``base`` is reloaded first because
    ``prod`` does ``from .base import *`` and otherwise re-uses the cached
    (startup-time) base namespace instead of re-reading the patched env.
    """
    env = dict(BASELINE_ENV)
    env.update({k: v for k, v in extra_env.items() if v is not None})
    with mock.patch.dict(os.environ, env, clear=False):
        for key, value in extra_env.items():
            if value is None:
                os.environ.pop(key, None)
        for key in ("ITAMBOX_RATELIMIT_USE_X_FORWARDED_FOR", "ITAMBOX_RATELIMIT_NUM_PROXIES"):
            if key not in extra_env:
                os.environ.pop(key, None)
        sys.modules.pop("core.settings.prod", None)
        base = importlib.import_module("core.settings.base")
        with warnings.catch_warnings():
            # base re-warns about a missing secret key etc. when reloaded; the
            # individual tests assert on prod's own logging, not base's warnings.
            warnings.simplefilter("ignore")
            importlib.reload(base)
        return importlib.import_module("core.settings.prod")


@pytest.fixture(autouse=True)
def _restore_settings_modules():
    """
    Reloading core.settings.base/prod under a patched env mutates the cached
    modules. Restore them to the real environment afterwards so the rest of the
    suite (and any code importing those module attributes) sees the true values.
    """
    yield
    sys.modules.pop("core.settings.queue_drain", None)
    sys.modules.pop("core.settings.prod", None)
    base = importlib.import_module("core.settings.base")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        importlib.reload(base)


def _prod_log_records(extra_env):
    """Load prod with a logging handler attached; return captured messages."""
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("core.settings.prod")
    logger.addHandler(handler)
    try:
        prod = _load_prod(extra_env)
    finally:
        logger.removeHandler(handler)
    return records, prod


def _load_settings_namespace(env_value: str, source_module: str) -> types.ModuleType:
    """Execute the settings loader against an isolated, stubbed source module."""
    settings_path = Path(__file__).resolve().parents[1] / "settings" / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"core.settings._coverage_probe_{source_module}", settings_path)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    loader.__package__ = "core.settings"
    fake_source = types.ModuleType(f"core.settings.{source_module}")
    fake_source.__dict__["SETTINGS_LOADER_SOURCE"] = source_module

    with mock.patch.dict(os.environ, {"ITAMBOX_ENV": env_value}):
        with mock.patch.dict(sys.modules, {fake_source.__name__: fake_source}):
            spec.loader.exec_module(loader)

    return loader


class TestSettingsNamespaceLoader:
    def test_prod_environment_imports_the_production_namespace(self):
        loader = _load_settings_namespace("prod", "prod")

        assert loader.ENV == "prod"
        assert loader.__dict__["SETTINGS_LOADER_SOURCE"] == "prod"

    def test_unrecognized_environment_falls_back_to_base_namespace(self):
        loader = _load_settings_namespace("legacy", "base")

        assert loader.ENV == "legacy"
        assert loader.__dict__["SETTINGS_LOADER_SOURCE"] == "base"


class TestProdSettingsPosture:
    """Security-critical production settings must hold."""

    def test_secure_cookies_enabled(self):
        prod = _load_prod({})
        assert prod.SESSION_COOKIE_SECURE is True
        assert prod.CSRF_COOKIE_SECURE is True

    def test_ssl_redirect_enabled(self):
        prod = _load_prod({})
        assert prod.SECURE_SSL_REDIRECT is True

    def test_queue_drain_is_prod_with_only_scheduler_disabled(self):
        prod = _load_prod({})
        sys.modules.pop("core.settings.queue_drain", None)
        drain = importlib.import_module("core.settings.queue_drain")
        prod_names = {name for name in vars(prod) if name.isupper()}
        drain_names = {name for name in vars(drain) if name.isupper()}
        assert drain_names == prod_names
        for name in sorted(prod_names - {"Q_CLUSTER"}):
            assert getattr(drain, name) == getattr(prod, name), name
        assert drain.Q_CLUSTER == {**prod.Q_CLUSTER, "scheduler": False}

    def test_forwarded_for_trust_is_disabled_by_default(self):
        prod = _load_prod({})
        assert prod.RATELIMIT_USE_X_FORWARDED_FOR is False
        assert prod.RATELIMIT_NUM_PROXIES == 1

    def test_forwarded_for_trust_can_be_enabled_explicitly(self):
        prod = _load_prod(
            {
                "ITAMBOX_RATELIMIT_USE_X_FORWARDED_FOR": "true",
                "ITAMBOX_RATELIMIT_NUM_PROXIES": "1",
            }
        )
        assert prod.RATELIMIT_USE_X_FORWARDED_FOR is True
        assert prod.RATELIMIT_NUM_PROXIES == 1

    def test_forwarded_for_proxy_count_must_be_positive(self):
        with pytest.raises(ValueError, match="ITAMBOX_RATELIMIT_NUM_PROXIES"):
            _load_prod({"ITAMBOX_RATELIMIT_NUM_PROXIES": "0"})

    def test_forwarded_for_proxy_count_must_be_numeric(self):
        with pytest.raises(ValueError, match="ITAMBOX_RATELIMIT_NUM_PROXIES"):
            _load_prod({"ITAMBOX_RATELIMIT_NUM_PROXIES": "not-a-number"})

    def test_hsts_configured(self):
        prod = _load_prod({})
        assert prod.SECURE_HSTS_SECONDS > 0

    def test_content_type_hardening(self):
        prod = _load_prod({})
        # Defensive: only assert when the attribute exists, but this is set
        # unconditionally in prod.py so it must be present.
        assert getattr(prod, "SECURE_CONTENT_TYPE_NOSNIFF", None) is True

    def test_xss_filter_setting_is_not_reintroduced(self):
        # Django 5.2 removed SECURE_BROWSER_XSS_FILTER entirely: the
        # X-XSS-Protection header it used to send is a browser no-op in every
        # supported browser, so the setting configured nothing even before
        # removal. Assert it stays gone rather than silently doing nothing.
        prod = _load_prod({})
        assert not hasattr(prod, "SECURE_BROWSER_XSS_FILTER")

    def test_basic_authentication_dropped(self):
        prod = _load_prod({})
        auth_classes = prod.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]
        assert not any("BasicAuthentication" in cls for cls in auth_classes), (
            "BasicAuthentication must not be a default authentication class in "
            "production (credentials are sent on every request)."
        )
        # Token + session auth must still be present.
        assert any("TokenAuthentication" in cls for cls in auth_classes)

    def test_debug_is_never_true_in_prod(self):
        """
        DEBUG must be hardcoded False in prod.py, not env-toggleable. A stray
        ITAMBOX_DEBUG=True in a leftover/templated .env (e.g. copied from
        .env.example and only half-edited when switching to prod) must not be
        able to flip it on.
        """
        assert _load_prod({}).DEBUG is False
        assert _load_prod({"ITAMBOX_DEBUG": "True"}).DEBUG is False
        assert _load_prod({"ITAMBOX_DEBUG": "true"}).DEBUG is False
        assert _load_prod({"ITAMBOX_DEBUG": "1"}).DEBUG is False

    # ------------------------------------------------------------------
    # SECRET_KEY — full security.W009-equivalent contract
    # ------------------------------------------------------------------

    def test_sentinel_secret_key_raises(self):
        """An unset (sentinel) SECRET_KEY must refuse to boot in prod."""
        with pytest.raises(ImproperlyConfigured, match="ITAMBOX_SECRET_KEY"):
            _load_prod({"ITAMBOX_SECRET_KEY": ""})

    def test_secret_key_w009_equivalence_matrix(self):
        """Every W009-rejected key must fail at import, identifying the rule."""
        cases = [
            # (label, value, expected diagnostic fragment)
            # A missing/empty key materializes as the base-settings dev fallback;
            # prod reports the honest "missing" rule in that case.
            ("missing", None, "missing"),
            ("49-char", "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLM", "at least 50 characters"),
            ("few-distinct", "abcd" * 14, "at least 5 distinct characters"),
            ("forbidden-prefix", "django-insecure-" + "abcdefghij" * 5, "django-insecure-"),
        ]
        for label, value, fragment in cases:
            with pytest.raises(ImproperlyConfigured) as exc_info:
                _load_prod({"ITAMBOX_SECRET_KEY": value})
            message = str(exc_info.value)
            assert "ITAMBOX_SECRET_KEY" in message, label
            assert fragment in message, label
            if value is not None:
                assert value not in message, f"{label}: diagnostic leaked the key"

    def test_valid_secret_key_accepted(self):
        """A 50+ character key with >= 5 distinct chars and no prefix passes."""
        prod = _load_prod({})
        assert prod.SECRET_KEY == SECURE_KEY

    def test_rejected_key_with_unset_fallbacks_warns_about_preservation(self):
        """A short operator key must warn against blindly rotating when the
        SECRET_KEY-derived fallbacks are in use (data-loss precondition)."""
        short_key = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLM"  # 49 chars
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _load_prod({"ITAMBOX_SECRET_KEY": short_key, "ITAMBOX_FIELD_ENCRYPTION_KEYS": None})
        message = str(exc_info.value)
        assert "ITAMBOX_FIELD_ENCRYPTION_KEYS" in message  # preservation precondition named
        assert "Keep the current key" in message
        assert short_key not in message

    def test_rejected_key_with_pinned_fallbacks_has_no_preservation_warning(self):
        """With a pinned keyring and peppers the operator can rotate freely."""
        short_key = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLM"  # 49 chars
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _load_prod({"ITAMBOX_SECRET_KEY": short_key})
        message = str(exc_info.value)
        assert "at least 50 characters" in message
        assert "Keep the current key" not in message
        assert short_key not in message

    def test_prefixed_operator_key_warns_about_preservation(self):
        """An operator-supplied django-insecure-* key may underpin derived state
        (earlier releases accepted any non-sentinel key) — the preservation
        precondition must apply whenever the fallbacks are unset."""
        prefixed_key = "django-insecure-" + "abcdefghij" * 5  # 66 chars, prefix rule
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _load_prod({"ITAMBOX_SECRET_KEY": prefixed_key, "ITAMBOX_FIELD_ENCRYPTION_KEYS": None})
        message = str(exc_info.value)
        assert "django-insecure-" in message
        assert "Keep the current key" in message
        assert prefixed_key not in message

    # ------------------------------------------------------------------
    # Database password — explicitly configured contract
    # ------------------------------------------------------------------

    def test_missing_db_password_raises(self):
        with pytest.raises(ImproperlyConfigured, match="ITAMBOX_DB_PASSWORD"):
            _load_prod({"ITAMBOX_DB_PASSWORD": None})

    def test_blank_db_password_raises(self):
        with pytest.raises(ImproperlyConfigured, match="ITAMBOX_DB_PASSWORD"):
            _load_prod({"ITAMBOX_DB_PASSWORD": "   "})

    def test_explicit_db_password_accepted(self):
        """'explicitly configured' is the contract — the literal value is not banned."""
        prod = _load_prod({"ITAMBOX_DB_PASSWORD": "itambox"})
        assert prod.DATABASES["default"]["PASSWORD"] == "itambox"

    # ------------------------------------------------------------------
    # API-token peppers — unset vs malformed
    # ------------------------------------------------------------------

    def test_missing_peppers_warn_in_prod(self):
        """Unset or blank peppers keep the warned SECRET_KEY-derived fallback in prod."""
        for value in (None, "", "   "):
            records, _ = _prod_log_records({"ITAMBOX_API_TOKEN_PEPPERS": value})
            assert any("ITAMBOX_API_TOKEN_PEPPERS" in msg and "SECRET_KEY" in msg for msg in records), (
                f"Expected a startup warning about the missing peppers fallback (value={value!r})."
            )

    def test_malformed_peppers_raise_in_prod(self):
        too_short_marker = "tooshort-marker-439"
        for raw in ("{not-json", "[1,2]", "{}", json.dumps({"1": too_short_marker}), SECRET_MARKER):
            with pytest.raises(ImproperlyConfigured) as exc_info:
                _load_prod({"ITAMBOX_API_TOKEN_PEPPERS": raw})
            message = str(exc_info.value)
            assert "ITAMBOX_API_TOKEN_PEPPERS" in message
            assert SECRET_MARKER not in message
            assert too_short_marker not in message

    def test_malformed_peppers_never_downgrade_to_empty(self):
        """An explicitly malformed mapping must fail, not silently become {}."""
        with pytest.raises(ImproperlyConfigured):
            _load_prod({"ITAMBOX_API_TOKEN_PEPPERS": "not-json"})

    def test_valid_peppers_produce_no_warning(self):
        records, prod = _prod_log_records({})
        assert prod.API_TOKEN_PEPPERS == {1: PEPPER_SECRET}
        assert not any("ITAMBOX_API_TOKEN_PEPPERS" in msg for msg in records)

    # ------------------------------------------------------------------
    # Field-encryption keyring — unset vs malformed
    # ------------------------------------------------------------------

    def test_derived_encryption_key_warns_in_prod(self):
        """No ITAMBOX_FIELD_ENCRYPTION_KEYS in prod must emit a loud warning."""
        records, _ = _prod_log_records({"ITAMBOX_FIELD_ENCRYPTION_KEYS": ""})
        assert any("ITAMBOX_FIELD_ENCRYPTION_KEYS" in msg for msg in records), (
            "Expected a startup warning about the derived field-encryption key in production."
        )

    def test_malformed_encryption_keys_raise_in_prod(self):
        bad = SECRET_MARKER
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _load_prod({"ITAMBOX_FIELD_ENCRYPTION_KEYS": bad})
        message = str(exc_info.value)
        assert "ITAMBOX_FIELD_ENCRYPTION_KEYS" in message
        assert "index 1" in message
        assert bad not in message

    def test_separator_only_encryption_keys_raise_in_prod(self):
        """Whitespace/separator-only material is explicit malformed config."""
        with pytest.raises(ImproperlyConfigured, match="ITAMBOX_FIELD_ENCRYPTION_KEYS"):
            _load_prod({"ITAMBOX_FIELD_ENCRYPTION_KEYS": "  ,  "})

    def test_invalid_key_at_later_position_identified(self):
        with pytest.raises(ImproperlyConfigured) as exc_info:
            _load_prod({"ITAMBOX_FIELD_ENCRYPTION_KEYS": f"{FERNET_KEY},{SECRET_MARKER}"})
        message = str(exc_info.value)
        assert "index 2" in message
        assert SECRET_MARKER not in message

    # ------------------------------------------------------------------
    # Cache and warnings posture
    # ------------------------------------------------------------------

    def test_locmem_cache_warns_in_prod(self):
        """locmem cache in prod must emit a loud warning (per-worker counters)."""
        records, _ = _prod_log_records({"ITAMBOX_CACHE_BACKEND": "locmem"})
        assert any("locmem" in msg for msg in records), "Expected a startup warning about locmem cache in production."

    def test_no_spurious_warnings_when_hardened(self):
        """A fully hardened prod config (redis + explicit keys) must be quiet."""
        records, prod = _prod_log_records({})
        assert records == [], f"Unexpected prod warnings: {records}"
        assert prod.DEBUG is False


class TestProductionChecks:
    """The tagged check surface (core/checks.py, tag 'prod') must classify the
    same tri-states it reports: warnings for unset/blank, errors for malformed,
    silence for valid and for development settings."""

    @staticmethod
    def _messages(check_fn, **settings_overrides):
        with override_settings(DEBUG=False, **settings_overrides):
            return check_fn(None)

    def test_pepper_unset_warns(self):
        messages = self._messages(
            production_checks.check_production_api_token_peppers,
            API_TOKEN_PEPPERS_STATE="unset",
        )
        assert len(messages) == 1 and messages[0].id == "core.W001"

    def test_pepper_malformed_errors(self):
        messages = self._messages(
            production_checks.check_production_api_token_peppers,
            API_TOKEN_PEPPERS_STATE="malformed",
            API_TOKEN_PEPPERS_ERROR="must be valid JSON",
        )
        assert len(messages) == 1 and messages[0].id == "core.E001"

    def test_pepper_valid_silent(self):
        assert (
            self._messages(
                production_checks.check_production_api_token_peppers,
                API_TOKEN_PEPPERS_STATE="valid",
                API_TOKEN_PEPPERS_ERROR=None,
            )
            == []
        )

    def test_pepper_unsupported_state_warns(self):
        messages = self._messages(
            production_checks.check_production_api_token_peppers,
            API_TOKEN_PEPPERS_STATE="unsupported",
        )
        assert len(messages) == 1 and messages[0].id == "core.W003"

    def test_field_keys_malformed_errors(self):
        messages = self._messages(
            production_checks.check_production_field_encryption_keys,
            FIELD_ENCRYPTION_KEYS_STATE="malformed",
            FIELD_ENCRYPTION_KEYS_ERROR="invalid Fernet key at index 1",
        )
        assert len(messages) == 1 and messages[0].id == "core.E002"

    def test_field_keys_unset_warns(self):
        messages = self._messages(
            production_checks.check_production_field_encryption_keys,
            FIELD_ENCRYPTION_KEYS_STATE="unset",
        )
        assert len(messages) == 1 and messages[0].id == "core.W002"

    def test_field_keys_valid_silent(self):
        assert (
            self._messages(
                production_checks.check_production_field_encryption_keys,
                FIELD_ENCRYPTION_KEYS_STATE="valid",
                FIELD_ENCRYPTION_KEYS_ERROR=None,
            )
            == []
        )

    def test_field_keys_unsupported_state_warns(self):
        messages = self._messages(
            production_checks.check_production_field_encryption_keys,
            FIELD_ENCRYPTION_KEYS_STATE="unsupported",
        )
        assert len(messages) == 1 and messages[0].id == "core.W004"

    def test_checks_are_silent_in_dev(self):
        """DEBUG=True (development) must never report the check surface."""
        with override_settings(DEBUG=True):
            assert production_checks.check_production_api_token_peppers(None) == []
            assert production_checks.check_production_field_encryption_keys(None) == []


class TestProductionEntryPaths:
    """
    Rejected configurations must fail through the real startup entry points,
    not only through a helper (issue #439 design contract).

    Each case spawns a bounded subprocess that imports ``core.settings.prod``
    exactly as Gunicorn, qcluster, and management commands do. The settings
    import itself is the enforcement point, so every command fails before it
    can serve or process work.
    """

    @staticmethod
    def _run(code, extra_env, timeout=90):
        # The child environment is an explicit allowlist, NOT the operator's
        # environment: real secrets on the runner must never reach the child
        # (or its error output). Only what Django needs to boot is inherited.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "PYTHONUNBUFFERED": "1",
            "DJANGO_SETTINGS_MODULE": "core.settings.prod",
        }
        env.update(BASELINE_ENV)
        env.update({k: v for k, v in extra_env.items() if v is not None})
        for key, value in extra_env.items():
            if value is None:
                env.pop(key, None)
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=ITAMBOX_DIR,
            env=env,
        )

    _IMPORT_CODE = "import core.settings.prod"  # settings-import entry path
    _WSGI_CODE = "import core.wsgi"  # Gunicorn app-import entry path
    _CHECK_CODE = (
        "from django.core.management import execute_from_command_line; "
        "execute_from_command_line(['manage.py', 'check'])"
    )  # management-command/preflight entry path
    _QCLUSTER_CODE = (
        "from django.core.management import execute_from_command_line; "
        "execute_from_command_line(['manage.py', 'qcluster'])"
    )  # worker entry path

    @pytest.mark.parametrize(
        "label,code",
        [
            ("settings-import", _IMPORT_CODE),
            ("wsgi", _WSGI_CODE),
            ("management-command", _CHECK_CODE),
        ],
    )
    def test_malformed_peppers_fail_before_entry(self, label, code):
        result = self._run(code, {"ITAMBOX_API_TOKEN_PEPPERS": SECRET_MARKER})
        assert result.returncode != 0, f"{label}: malformed peppers were accepted"
        combined = result.stderr + result.stdout
        assert "ITAMBOX_API_TOKEN_PEPPERS" in combined, f"{label}: missing diagnostic"
        assert SECRET_MARKER not in combined, f"{label}: diagnostic leaked the secret"

    @pytest.mark.parametrize(
        "label,code",
        [
            ("settings-import", _IMPORT_CODE),
            ("wsgi", _WSGI_CODE),
            ("management-command", _CHECK_CODE),
        ],
    )
    def test_malformed_encryption_keys_fail_before_entry(self, label, code):
        result = self._run(code, {"ITAMBOX_FIELD_ENCRYPTION_KEYS": SECRET_MARKER})
        assert result.returncode != 0, f"{label}: malformed keyring was accepted"
        combined = result.stderr + result.stdout
        assert "ITAMBOX_FIELD_ENCRYPTION_KEYS" in combined, f"{label}: missing diagnostic"
        assert SECRET_MARKER not in combined, f"{label}: diagnostic leaked the secret"

    @pytest.mark.parametrize(
        "label,code",
        [
            ("settings-import", _IMPORT_CODE),
            ("wsgi", _WSGI_CODE),
            ("management-command", _CHECK_CODE),
        ],
    )
    def test_missing_db_password_fails_before_entry(self, label, code):
        result = self._run(code, {"ITAMBOX_DB_PASSWORD": None})
        assert result.returncode != 0, f"{label}: missing DB password was accepted"
        combined = result.stderr + result.stdout
        assert "ITAMBOX_DB_PASSWORD" in combined, f"{label}: missing diagnostic"

    @pytest.mark.parametrize("code", [_IMPORT_CODE, _WSGI_CODE])
    def test_compliant_config_survives_settings_import(self, code):
        """A fully hardened config must import cleanly through both entry paths."""
        result = self._run(code, {})
        assert result.returncode == 0, f"compliant config rejected: {result.stderr[-2000:]}"

    @pytest.mark.serial_only
    def test_malformed_config_exits_qcluster_promptly(self):
        """A malformed config must stop the worker before it processes work.

        The compliant-worker liveness proof lives in the Docker smoke test
        (scripts/docker-smoke-test.sh -> verify_worker_stable): an ephemeral
        stack with generated secrets keeps the real qcluster container running.
        Starting a live worker inside pytest would dequeue real tasks from the
        shared database, so only the prompt-exit property is proven here.
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "PYTHONUNBUFFERED": "1",
            "DJANGO_SETTINGS_MODULE": "core.settings.prod",
        }
        env.update(BASELINE_ENV)
        env["ITAMBOX_API_TOKEN_PEPPERS"] = SECRET_MARKER
        proc = subprocess.Popen(
            [sys.executable, "-c", self._QCLUSTER_CODE],
            cwd=ITAMBOX_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                pytest.fail("qcluster did not exit promptly for malformed peppers")
        finally:
            # Kill the whole process tree so a hypothetical survivor can never
            # orphan django-q workers against a database.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        assert proc.returncode != 0, "qcluster accepted malformed peppers"
        combined = out + err
        assert "ITAMBOX_API_TOKEN_PEPPERS" in combined
        assert SECRET_MARKER not in combined
