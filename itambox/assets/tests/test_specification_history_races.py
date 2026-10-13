"""PostgreSQL lock-order regressions for specification-history cleanup."""

from __future__ import annotations

import queue
import threading
import time

import pytest
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase

from assets.models.catalog import AssetType
from assets.services.specifications.commands import cleanup_asset_type_history
from assets.services.specifications.contracts import HistoryCleanupPreviewDTO, OwnerChangedDTO
from assets.services.specifications.locking import SPECIFICATION_CATALOGUE_LOCK_KEY, catalogue_transaction_lock
from assets.tests.test_specification_history_boundaries import HistoryBoundaryFixtureMixin
from core.context import set_current_tenant
from organization.models import Tenant

pytestmark = [pytest.mark.serial_only, pytest.mark.django_db(transaction=True)]


def _start_worker(target, provider_id):
    arrived = queue.Queue()
    results = []
    errors = []

    def worker():
        close_old_connections()
        try:
            set_current_tenant(Tenant._base_manager.get(pk=provider_id))
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                arrived.put(cursor.fetchone()[0])
            results.append(target())
        except Exception as error:  # pragma: no cover - surfaced by _finish
            errors.append(error)
        finally:
            set_current_tenant(None)
            connections["default"].close()

    thread = threading.Thread(target=worker)
    thread.start()
    return thread, arrived.get(timeout=10), results, errors


def _start_advisory_blocker():
    arrived = queue.Queue()
    release = threading.Event()
    errors = []

    def blocker():
        close_old_connections()
        try:
            with transaction.atomic():
                with catalogue_transaction_lock(exclusive=True):
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        arrived.put(cursor.fetchone()[0])
                    release.wait(timeout=30)
        except Exception as error:  # pragma: no cover - surfaced by _finish_blocker
            errors.append(error)
        finally:
            connections["default"].close()

    thread = threading.Thread(target=blocker)
    thread.start()
    return thread, arrived.get(timeout=10), release, errors


def _finish_worker(started):
    thread, _pid, results, errors = started
    thread.join(15)
    assert not thread.is_alive(), "history worker did not terminate after lock release"
    assert not errors, errors
    assert len(results) == 1
    return results[0]


def _finish_blocker(started):
    thread, _pid, release, errors = started
    release.set()
    thread.join(15)
    assert not thread.is_alive(), "lock blocker did not terminate"
    assert not errors, errors


def _assert_advisory_wait(waiting_pid, blocking_pid):
    """Observe a real shared-vs-exclusive PostgreSQL advisory lock wait."""
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                """
                SELECT activity.wait_event_type,
                       pg_blocking_pids(activity.pid),
                       locks.locktype,
                       locks.mode,
                       locks.classid,
                       locks.objid,
                       locks.granted
                FROM pg_stat_activity AS activity
                JOIN pg_locks AS locks ON locks.pid = activity.pid
                WHERE activity.pid = %s AND NOT locks.granted
                """,
                [waiting_pid],
            )
            rows = cursor.fetchall()
            last = rows
            if rows:
                wait_event_type, blockers, locktype, mode, classid, objid, granted = rows[0]
                if (
                    wait_event_type == "Lock"
                    and blocking_pid in blockers
                    and locktype == "advisory"
                    and mode == "ShareLock"
                    and (classid, objid) == SPECIFICATION_CATALOGUE_LOCK_KEY
                    and not granted
                ):
                    print("OBSERVED_HISTORY_CATALOGUE_WAIT", waiting_pid, blocking_pid, rows)
                    return
        threading.Event().wait(0.01)
    pytest.fail(f"backend {waiting_pid} never reached the expected advisory wait: {last}")


def _assert_row_wait(waiting_pid, blocking_pid, table_name):
    """Observe a row lock wait through both pg_blocking_pids and pg_locks."""
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                """
                SELECT activity.wait_event_type,
                       pg_blocking_pids(activity.pid),
                       locks.locktype,
                       locks.mode,
                       locks.granted,
                       locks.relation::regclass::text
                FROM pg_stat_activity AS activity
                JOIN pg_locks AS locks ON locks.pid = activity.pid
                WHERE activity.pid = %s
                """,
                [waiting_pid],
            )
            rows = cursor.fetchall()
            last = rows
            if rows:
                wait_event_type, blockers = rows[0][0], rows[0][1]
                has_waiting_row_lock = any(row[2] in {"tuple", "transactionid"} and not row[4] for row in rows)
                has_table_lock = any(row[3] and row[5] == table_name for row in rows)
                if wait_event_type == "Lock" and blocking_pid in blockers and has_waiting_row_lock and has_table_lock:
                    print("OBSERVED_HISTORY_OWNER_OR_LIBRARY_WAIT", waiting_pid, blocking_pid, table_name, rows)
                    return
        threading.Event().wait(0.01)
    pytest.fail(f"backend {waiting_pid} never reached the expected row wait on {table_name}: {last}")


class TestSpecificationHistoryRaces(HistoryBoundaryFixtureMixin, TransactionTestCase):
    def test_history_cleanup_waits_on_catalogue_then_owner_with_real_connections(self):
        assert connection.vendor == "postgresql"
        preview = self._preview_type(keys=("inactive_history", "deprecated_history", "deprecated_choice_history"))
        assert isinstance(preview, HistoryCleanupPreviewDTO)
        blocker = None
        started = None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    owner_blocker_pid = cursor.fetchone()[0]
                AssetType.all_objects.select_for_update().get(pk=self.asset_type.pk)
                blocker = _start_advisory_blocker()
                started = _start_worker(
                    lambda: cleanup_asset_type_history(
                        actor=self._actor(),
                        asset_type_id=self.asset_type.pk,
                        keys=preview.keys,
                        preview_token=preview.preview_token,
                        expected_resource_revision=preview.expected_resource_revision,
                        expected_definition_revision=preview.expected_definition_revision,
                    ),
                    self.tenant.pk,
                )
                _assert_advisory_wait(started[1], blocker[1])
                blocker[2].set()
                _assert_row_wait(started[1], owner_blocker_pid, AssetType._meta.db_table)
        finally:
            if blocker is not None:
                blocker[2].set()
            if started is not None:
                result = _finish_worker(started)
            if blocker is not None:
                _finish_blocker(blocker)

        assert isinstance(result, OwnerChangedDTO)
        self.asset_type.refresh_from_db()
        assert self.asset_type.custom_field_data == {"active_history": "type-active"}
