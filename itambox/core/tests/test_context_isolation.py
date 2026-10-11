"""Regression tests for request/tenant context isolation under repeated execution."""

import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase

import conftest
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
    override_current_tenant_scope,
)
from core.managers import get_current_membership, get_current_tenant
from core.models import _user_validation_cache
from core.navigation.menu import get_menus
from core.settings import base as base_settings
from core.tasks.context import TaskContext
from core.tests.mixins import TenantTestMixin
from organization.access import _descendant_group_ids_cache as _access_descendant_group_ids_cache


class TenantContextIsolationTests(TenantTestMixin, SimpleTestCase):
    def test_failed_nested_task_context_never_logs_outer_request_id(self):
        outer_request_id = uuid.uuid4()
        _request_id.set(outer_request_id)
        with patch.object(TaskContext, "_resolve_principal_and_tenant", side_effect=PermissionDenied):
            with patch("core.tasks.context.logger.error") as error:
                with pytest.raises(PermissionDenied):
                    with TaskContext(tenant_id=999, operation="test.bad_task"):
                        pass
        self.assertEqual(_request_id.get(), outer_request_id)
        logged_request_id = error.call_args.kwargs["extra"]["request_id"]
        self.assertNotEqual(logged_request_id, str(outer_request_id))

    def test_test_fixture_exposes_an_explicit_context_reset(self):
        self.assertTrue(callable(getattr(conftest, "_reset_test_context", None)))
        self.assertTrue(callable(getattr(conftest, "_write_node_id_manifest", None)))
        self.assertEqual(
            conftest._canonical_junit_node_id("itambox/core/tests/test_context.py::Case::test_one[param]"),
            "itambox.core.tests.test_context.Case::test_one[param]",
        )

    def test_django_q_tasks_run_inline_in_every_pytest_worker(self):
        self.assertTrue(settings.Q_CLUSTER["sync"])

    def test_test_settings_configuration_is_a_noop_outside_testing(self):
        q_sync = base_settings.Q_CLUSTER["sync"]
        conn_max_age = base_settings.DATABASES["default"]["CONN_MAX_AGE"]
        database_options = base_settings.DATABASES["default"]["OPTIONS"]["options"]

        base_settings._configure_test_environment(False)

        self.assertEqual(base_settings.Q_CLUSTER["sync"], q_sync)
        self.assertEqual(base_settings.DATABASES["default"]["CONN_MAX_AGE"], conn_max_age)
        self.assertEqual(base_settings.DATABASES["default"]["OPTIONS"]["options"], database_options)

    def test_node_id_manifest_writer_is_serial_only_and_deterministic(self):
        previous_manifest = os.environ.get("ITAMBOX_NODE_ID_MANIFEST")
        previous_write = os.environ.get("ITAMBOX_NODE_ID_MANIFEST_WRITE")
        previous_worker = os.environ.get("PYTEST_XDIST_WORKER")
        previous_worker_count = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "nodeids.txt"
            try:
                os.environ["ITAMBOX_NODE_ID_MANIFEST"] = str(manifest_path)
                os.environ["ITAMBOX_NODE_ID_MANIFEST_WRITE"] = "1"
                os.environ.pop("PYTEST_XDIST_WORKER", None)
                os.environ.pop("PYTEST_XDIST_WORKER_COUNT", None)
                conftest._write_node_id_manifest([SimpleNamespace(nodeid="z::test"), SimpleNamespace(nodeid="a::test")])
                self.assertEqual(manifest_path.read_text(encoding="utf-8"), "a::test\nz::test\n")

                manifest_path.write_text("sentinel\n", encoding="utf-8")
                os.environ["PYTEST_XDIST_WORKER"] = "gw0"
                conftest._write_node_id_manifest([SimpleNamespace(nodeid="different::test")])
                self.assertEqual(manifest_path.read_text(encoding="utf-8"), "sentinel\n")
            finally:
                if previous_manifest is None:
                    os.environ.pop("ITAMBOX_NODE_ID_MANIFEST", None)
                else:
                    os.environ["ITAMBOX_NODE_ID_MANIFEST"] = previous_manifest
                if previous_write is None:
                    os.environ.pop("ITAMBOX_NODE_ID_MANIFEST_WRITE", None)
                else:
                    os.environ["ITAMBOX_NODE_ID_MANIFEST_WRITE"] = previous_write
                if previous_worker is None:
                    os.environ.pop("PYTEST_XDIST_WORKER", None)
                else:
                    os.environ["PYTEST_XDIST_WORKER"] = previous_worker
                if previous_worker_count is None:
                    os.environ.pop("PYTEST_XDIST_WORKER_COUNT", None)
                else:
                    os.environ["PYTEST_XDIST_WORKER_COUNT"] = previous_worker_count

    def test_test_fixture_reset_clears_every_request_and_task_contextvar(self):
        values = (
            (_current_tenant, object()),
            (_current_tenant_group, object()),
            (_current_membership, object()),
            (_current_all_accessible, True),
            (_descendant_group_ids_cache, {1, 2}),
            (_access_descendant_group_ids_cache, {3, 4}),
            (_current_user, object()),
            (_request_id, object()),
            (_csp_nonce, "nonce"),
            (_system_authorization_scope, object()),
            (_issued_system_authorizations, (object(),)),
            (_request_invalidation_state, (object(), {}, 1)),
            (_user_validation_cache, ("request", {1})),
        )
        for variable, value in values:
            variable.set(value)

        conftest._reset_test_context()

        self.assertIsNone(_current_tenant.get())
        self.assertIsNone(_current_tenant_group.get())
        self.assertIsNone(_current_membership.get())
        self.assertFalse(_current_all_accessible.get())
        self.assertIsNone(_descendant_group_ids_cache.get())
        self.assertIsNone(_access_descendant_group_ids_cache.get())
        self.assertIsNone(_current_user.get())
        self.assertIsNone(_request_id.get())
        self.assertIsNone(_csp_nonce.get())
        self.assertIsNone(_system_authorization_scope.get())
        self.assertEqual(_issued_system_authorizations.get(), ())
        self.assertIsNone(_request_invalidation_state.get())
        self.assertIsNone(_user_validation_cache.get())

    def test_test_fixture_reset_reports_a_failed_contextvar_reset(self):
        import core.context

        original = core.context._current_tenant

        class BrokenContextVar:
            def set(self, _value):
                raise RuntimeError("reset failed")

        core.context._current_tenant = BrokenContextVar()
        try:
            with pytest.raises(AssertionError, match="current_tenant.*reset failed"):
                conftest._reset_test_context()
        finally:
            core.context._current_tenant = original

    def test_test_fixture_reset_clears_global_navigation_menu_cache(self):
        get_menus()
        self.assertGreater(get_menus.cache_info().currsize, 0)

        conftest._reset_test_context()

        self.assertEqual(get_menus.cache_info().currsize, 0)

    def test_test_fixture_reset_clears_django_cache_aliases(self):
        cache.set("issue21-context-isolation", "leak", timeout=None)
        self.assertEqual(cache.get("issue21-context-isolation"), "leak")

        conftest._reset_test_context()

        self.assertIsNone(cache.get("issue21-context-isolation"))

    def test_local_cache_cleanup_never_calls_external_backend(self):
        class RecordingCache:
            def __init__(self):
                self.cleared = False

            def clear(self):
                self.cleared = True

        local_cache = RecordingCache()
        external_cache = RecordingCache()
        conftest._clear_local_test_caches(
            {"local": local_cache, "external": external_cache},
            {
                "local": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
                "external": {"BACKEND": "django_redis.cache.RedisCache"},
            },
        )

        self.assertTrue(local_cache.cleared)
        self.assertFalse(external_cache.cleared)

    def test_xdist_database_name_is_run_specific_without_explicit_override(self):
        previous = os.environ.pop("TEST_DATABASE_NAME", None)
        config = SimpleNamespace(option=SimpleNamespace(numprocesses=2, dist="loadscope"))
        try:
            selected = conftest._select_database_name(
                config,
                env_var="TEST_DATABASE_NAME",
                stable_name="challenger2_testing",
            )
            self.assertRegex(selected, r"^challenger2_testing_pid\d+$")
            self.assertEqual(os.environ["TEST_DATABASE_NAME"], selected)
        finally:
            if previous is None:
                os.environ.pop("TEST_DATABASE_NAME", None)
            else:
                os.environ["TEST_DATABASE_NAME"] = previous

    def test_xdist_rejects_runs_without_parallel_marker_selector(self):
        config = SimpleNamespace(option=SimpleNamespace(numprocesses=2, dist="loadscope", markexpr=""))
        with patch.dict(os.environ, {"PYTEST_XDIST_WORKER": ""}, clear=False):
            with pytest.raises(pytest.UsageError, match="serial_only"):
                conftest._validate_xdist_marker_selection(config)

    def test_nested_tenant_context_restores_enclosing_context(self):
        outer_tenant = object()
        outer_membership = object()
        inner_tenant = object()
        inner_membership = object()

        with self.tenant_context(outer_tenant, outer_membership):
            self.assertIs(get_current_tenant(), outer_tenant)
            self.assertIs(get_current_membership(), outer_membership)

            with self.tenant_context(inner_tenant, inner_membership):
                self.assertIs(get_current_tenant(), inner_tenant)
                self.assertIs(get_current_membership(), inner_membership)

            self.assertIs(get_current_tenant(), outer_tenant)
            self.assertIs(get_current_membership(), outer_membership)

        self.assertIsNone(get_current_tenant())
        self.assertIsNone(get_current_membership())

    def test_override_current_tenant_scope_restores_all_outer_values(self):
        outer_tenant = object()
        outer_group = object()
        outer_membership = object()
        outer_cache = {"outer": [1]}
        _current_tenant.set(outer_tenant)
        _current_tenant_group.set(outer_group)
        _current_membership.set(outer_membership)
        _current_all_accessible.set(True)
        _descendant_group_ids_cache.set(outer_cache)
        inner_tenant = object()
        inner_membership = object()

        with override_current_tenant_scope(inner_tenant, inner_membership):
            self.assertIs(_current_tenant.get(), inner_tenant)
            self.assertIsNone(_current_tenant_group.get())
            self.assertIs(_current_membership.get(), inner_membership)
            self.assertFalse(_current_all_accessible.get())
            self.assertIsNone(_descendant_group_ids_cache.get())

        self.assertIs(_current_tenant.get(), outer_tenant)
        self.assertIs(_current_tenant_group.get(), outer_group)
        self.assertIs(_current_membership.get(), outer_membership)
        self.assertTrue(_current_all_accessible.get())
        self.assertIs(_descendant_group_ids_cache.get(), outer_cache)

    def test_override_current_tenant_scope_restores_nested_and_exception_state(self):
        outer_tenant = object()
        outer_group = object()
        outer_membership = object()
        outer_cache = {"outer": [2]}
        _current_tenant.set(outer_tenant)
        _current_tenant_group.set(outer_group)
        _current_membership.set(outer_membership)
        _current_all_accessible.set(True)
        _descendant_group_ids_cache.set(outer_cache)

        with self.assertRaisesRegex(RuntimeError, "inner failure"):
            with override_current_tenant_scope(object()):
                with override_current_tenant_scope(object(), object()):
                    self.assertIsNone(_current_tenant_group.get())
                    self.assertFalse(_current_all_accessible.get())
                    self.assertIsNone(_descendant_group_ids_cache.get())
                    raise RuntimeError("inner failure")

        self.assertIs(_current_tenant.get(), outer_tenant)
        self.assertIs(_current_tenant_group.get(), outer_group)
        self.assertIs(_current_membership.get(), outer_membership)
        self.assertTrue(_current_all_accessible.get())
        self.assertIs(_descendant_group_ids_cache.get(), outer_cache)
