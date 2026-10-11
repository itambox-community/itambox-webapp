"""#727: real PostgreSQL arbitration of competing repair loan operations."""

import datetime
import threading
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from model_bakery import baker

from assets.models import Asset, AssetAssignment, AssetMaintenance, AssetType, StatusLabel
from assets.services import checkout_asset, complete_repair, issue_repair_loaner
from core.tasks.context import TaskContext
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder

pytestmark = [pytest.mark.serial_only]


def _race(tenant_id, user_id, *calls):
    """Run every call on its own connection, released together by a barrier."""
    barrier = threading.Barrier(len(calls))
    outcomes = [None] * len(calls)

    def worker(index, call):
        close_old_connections()
        try:
            barrier.wait(timeout=20)
            with TaskContext(tenant_id=tenant_id, user_id=user_id, operation="issue727_race"):
                outcomes[index] = call()
        except Exception as error:  # noqa: BLE001 - reported to the caller
            outcomes[index] = error
        finally:
            connections["default"].close()

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive(), "worker did not terminate"
    return outcomes


class RepairLoanConcurrencyTests(TenantTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(connection.vendor, "postgresql")
        if getattr(self, "tenant", None) is None:
            self.setup_tenant_context()
        self.set_active_tenant(self.tenant)
        suffix = uuid.uuid4().hex[:8]
        self.deployable = StatusLabel.objects.create(name=f"Deployable {suffix}", type="deployable")
        StatusLabel.objects.create(name=f"Deployed {suffix}", type="deployed")
        self.asset_type = baker.make(AssetType)
        self.holder = baker.make(AssetHolder, tenant=self.tenant)
        self.asset = self._asset(f"FAIL-{suffix}")
        checkout_asset(self.asset, holder=self.holder, user=self.tenant_user)
        self.maintenance = AssetMaintenance.objects.create(
            asset=self.asset,
            maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            status="scheduled",
            start_date=datetime.date(2026, 1, 10),
            completion_date=datetime.date(2026, 1, 20),
        )

    def _asset(self, tag):
        return Asset.objects.create(
            asset_tag=tag, name=tag, status=self.deployable, tenant=self.tenant, asset_type=self.asset_type
        )

    def _open_loans(self):
        return AssetAssignment.objects.filter(maintenance=self.maintenance, is_loan=True, is_active=True).count()

    def test_competing_loaner_issuance_yields_one_open_loan(self):
        first, second = self._asset("LOAN-A"), self._asset("LOAN-B")
        outcomes = _race(
            self.tenant.pk,
            self.tenant_user.pk,
            lambda: issue_repair_loaner(self.maintenance, first, self.tenant_user),
            lambda: issue_repair_loaner(self.maintenance, second, self.tenant_user),
        )
        failures = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual(len(failures), 1, outcomes)
        self.assertIsInstance(failures[0], ValidationError)
        self.assertEqual(self._open_loans(), 1)

    def test_competing_completions_yield_one_lifecycle_transition(self):
        loaner = self._asset("LOAN-A")
        issue_repair_loaner(self.maintenance, loaner, self.tenant_user)
        before = AssetAssignment.objects.filter(asset=self.asset).count()
        outcomes = _race(
            self.tenant.pk,
            self.tenant_user.pk,
            lambda: complete_repair(self.maintenance, "return", self.tenant_user),
            lambda: complete_repair(self.maintenance, "return", self.tenant_user),
        )
        failures = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual(len(failures), 1, outcomes)
        self.assertIsInstance(failures[0], ValidationError)
        self.assertEqual(self._open_loans(), 0)
        # One hand-back only: the original assignment was closed and exactly one new one opened.
        self.assertEqual(AssetAssignment.objects.filter(asset=self.asset).count(), before + 1)
        self.assertEqual(AssetAssignment.objects.filter(asset=self.asset, is_active=True).count(), 1)
