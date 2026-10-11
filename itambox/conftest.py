import os
from pathlib import Path

import pytest

_LOCAL_TEST_CACHE_BACKENDS = frozenset(
    {
        "django.core.cache.backends.dummy.DummyCache",
        "django.core.cache.backends.locmem.LocMemCache",
    }
)


def _xdist_active(config):
    option = getattr(config, "option", config)
    return bool(
        os.environ.get("PYTEST_XDIST_WORKER")
        or getattr(option, "numprocesses", 0)
        or getattr(option, "dist", "no") != "no"
    )


def _select_database_name(config, *, env_var, stable_name):
    configured = os.environ.get(env_var)
    if configured:
        return configured
    if not _xdist_active(config):
        return stable_name

    # A stable serial name preserves --reuse-db, but concurrent xdist
    # invocations must not share the same pytest-django database namespace.
    run_name = f"{stable_name}_pid{os.getpid()}"
    os.environ[env_var] = run_name
    return run_name


def _external_cache_aliases(cache_configs):
    return tuple(
        alias for alias, config in cache_configs.items() if config.get("BACKEND") not in _LOCAL_TEST_CACHE_BACKENDS
    )


def _clear_local_test_caches(cache_handler, cache_configs):
    """Clear only cache backends owned by this pytest process.

    Calling ``clear()`` on django-redis is an unscoped FLUSHDB and can erase
    authorization, rate-limit, session, or application state belonging to
    another worker or service. External backends are rejected at pytest
    configuration time and are deliberately not touched here.
    """
    for alias, config in cache_configs.items():
        if config.get("BACKEND") in _LOCAL_TEST_CACHE_BACKENDS:
            cache_handler[alias].clear()


def _validate_xdist_marker_selection(config):
    if not _xdist_active(config) or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    markexpr = getattr(getattr(config, "option", config), "markexpr", "")
    if "not serial_only" not in markexpr.replace("(", " ").replace(")", " "):
        raise pytest.UsageError(
            "xdist runs require an explicit -m 'not serial_only' selection; "
            "execute serial_only tests in a separate serial lane"
        )


def pytest_configure(config):
    from django.conf import settings

    _validate_xdist_marker_selection(config)

    # Serial runs keep a stable name so --reuse-db can skip rebuilding ~200
    # migrations. Xdist runs receive a process-specific base name first; pytest-
    # django then appends the worker id (e.g. _gw0) to that isolated namespace.
    has_adversarial = any("test_graphql_adversarial" in arg for arg in getattr(config, "args", []))
    if has_adversarial:
        db_name = _select_database_name(
            config,
            env_var="TEST_DATABASE_NAME_ADVERSARIAL",
            stable_name="challenger2_adversarial",
        )
    else:
        db_name = _select_database_name(config, env_var="TEST_DATABASE_NAME", stable_name="challenger2_testing")
    settings.DATABASES["default"]["TEST"]["NAME"] = db_name
    settings.Q_CLUSTER["sync"] = True

    external_aliases = _external_cache_aliases(settings.CACHES)
    if external_aliases:
        aliases = ", ".join(sorted(external_aliases))
        raise pytest.UsageError(
            "pytest test isolation requires process-local cache backends; "
            f"external cache alias(es) are configured: {aliases}. "
            "Use ITAMBOX_CACHE_BACKEND=locmem for the test run."
        )


def _canonical_junit_node_id(nodeid):
    """Convert pytest's path-based node ID to the label emitted by pytest's JUnit writer."""
    path, *suffix = nodeid.split("::")
    if path.endswith(".py"):
        path = path[:-3]
    module = path.replace("\\", ".").replace("/", ".")
    if not suffix:
        return module
    if len(suffix) == 1:
        return f"{module}::{suffix[0]}"
    return f"{'.'.join([module, *suffix[:-1]])}::{suffix[-1]}"


def _write_node_id_manifest(items, *, xdist_active=False):
    """Write an opt-in serial collection manifest without allowing worker races."""
    manifest_path = os.environ.get("ITAMBOX_NODE_ID_MANIFEST")
    if (
        not manifest_path
        or os.environ.get("ITAMBOX_NODE_ID_MANIFEST_WRITE") != "1"
        or xdist_active
        or os.environ.get("PYTEST_XDIST_WORKER")
        or os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    ):
        return

    node_ids = sorted(_canonical_junit_node_id(item.nodeid) for item in items)
    if not node_ids:
        raise pytest.UsageError("node-ID manifest requested, but pytest collected no tests")
    if len(node_ids) != len(set(node_ids)):
        raise pytest.UsageError("node-ID manifest requested, but pytest collected duplicate node IDs")

    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(node_ids) + "\n", encoding="utf-8", newline="\n")


def pytest_collection_finish(session):
    _write_node_id_manifest(session.items, xdist_active=_xdist_active(session.config))


@pytest.fixture(scope="session", autouse=True)
def _isolate_media_root(tmp_path_factory):
    """Give every pytest process/xdist worker a disposable media root."""
    from django.test.utils import override_settings

    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    media_root = tmp_path_factory.mktemp(f"itambox-media-{worker_id}")
    with override_settings(MEDIA_ROOT=str(media_root)):
        yield


@pytest.fixture(scope="session", autouse=True)
def _prime_urlconf_without_tenant_context():
    """Import the root URLconf (and thus every view module) once at session start,
    while NO tenant context is active.

    Many views carry a class-level ``queryset = Model.objects.all()``. That
    attribute is evaluated when the view module is imported, and the tenant-scoping
    manager reads the *current* tenant context at that moment. On a real server the
    URLconf loads at startup with no active request, so those querysets bake
    UNSCOPED (per-request ``filter_by_tenant()`` then scopes them correctly). In the
    test process, without this, whichever test first calls ``reverse()`` inside an
    active tenant context would freeze every view's queryset to that tenant —
    producing order-dependent cross-tenant 404s (see the "import-baked view
    querysets" hazard). Forcing the import here makes the bake deterministic and
    context-free, matching production.
    """
    from django.urls import get_resolver

    get_resolver().url_patterns  # noqa: B018 — accessing the property triggers the full import
    yield


def _reset_test_context():
    """Reset every request/task ContextVar and fail if any reset cannot run."""
    from django.conf import settings
    from django.core.cache import caches

    from core.authorization_cache import _request_invalidation_state
    from core.context import (
        _csp_nonce,
        _current_all_accessible,
        _current_membership,
        _current_tenant,
        _current_tenant_group,
        _current_user,
        _descendant_group_ids_cache,
        _issued_system_authorizations,
        _request_id,
        _system_authorization_scope,
    )

    # inline import: app-registry: reset the model-level request cache only after Django app setup.
    from core.models import _user_validation_cache
    from core.navigation.menu import get_menus

    # inline import: app-registry: reset the organization access cache only after Django app setup.
    from organization.access import _descendant_group_ids_cache as _access_descendant_group_ids_cache

    resets = (
        ("current_tenant", lambda: _current_tenant.set(None)),
        ("current_tenant_group", lambda: _current_tenant_group.set(None)),
        ("current_membership", lambda: _current_membership.set(None)),
        ("current_all_accessible", lambda: _current_all_accessible.set(False)),
        ("descendant_group_ids_cache", lambda: _descendant_group_ids_cache.set(None)),
        ("access_descendant_group_ids_cache", lambda: _access_descendant_group_ids_cache.set(None)),
        ("current_user", lambda: _current_user.set(None)),
        ("request_id", lambda: _request_id.set(None)),
        ("csp_nonce", lambda: _csp_nonce.set(None)),
        ("system_authorization_scope", lambda: _system_authorization_scope.set(None)),
        ("issued_system_authorizations", lambda: _issued_system_authorizations.set(())),
        ("request_invalidation_state", lambda: _request_invalidation_state.set(None)),
        ("user_validation_cache", lambda: _user_validation_cache.set(None)),
        ("navigation_menus_cache", get_menus.cache_clear),
        ("local_cache_aliases", lambda: _clear_local_test_caches(caches, settings.CACHES)),
    )
    failures = []
    for name, reset in resets:
        try:
            reset()
        except Exception as exc:  # pragma: no cover - exercised through the failure-path test
            failures.append((name, exc))
    if failures:
        details = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in failures)
        raise AssertionError(f"Test context cleanup failed: {details}") from failures[0][1]


@pytest.fixture(autouse=True)
def clear_thread_locals():
    yield
    _reset_test_context()
