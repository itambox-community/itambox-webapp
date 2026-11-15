"""Event outbox regression suite (issues #1091, #1102)."""

import threading
from importlib import import_module
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from model_bakery import baker

from assets.models import Asset, Manufacturer
from extras.models import Event, EventRule
from extras.services import events as svc


def _rule(ct, **kwargs):
    kwargs.setdefault("name", "outbox rule")
    kwargs.setdefault("events", ["create"])
    kwargs.setdefault("action_type", EventRule.ACTION_NOTIFICATION)
    kwargs.setdefault("enabled", True)
    # ``_base_manager`` so a tenant-scoped rule can be created without an ambient scope.
    return EventRule._base_manager.create(model=ct, **kwargs)


class OutboxEmissionTests(TransactionTestCase):
    def setUp(self):
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        _rule(self.ct)

    def test_autocommit_write_inserts_event_in_same_transaction(self):
        with patch.object(svc, "_publish_hint"):
            Manufacturer.objects.create(name="Acme", slug="acme")
        event = Event.objects.get(model=self.ct, action="create")
        self.assertEqual(event.status, Event.STATUS_PENDING)

    def test_rolled_back_write_leaves_no_event(self):
        try:
            with transaction.atomic():
                Manufacturer.objects.create(name="Gone", slug="gone")
                raise RuntimeError("abort")
        except RuntimeError:
            pass
        self.assertFalse(Event.objects.filter(model=self.ct).exists())

    def test_process_death_after_commit_leaves_recoverable_pending_row(self):
        with patch.object(svc, "_publish_hint"):
            with transaction.atomic():
                Manufacturer.objects.create(name="Acme", slug="acme")
        self.assertEqual(Event.objects.filter(status=Event.STATUS_PENDING).count(), 1)

    def test_lost_hint_is_recovered_by_coordinator(self):
        with patch.object(svc, "async_task", side_effect=RuntimeError("broker down")):
            Manufacturer.objects.create(name="Acme", slug="acme")
        result = svc.recover_pending_events()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(Event.objects.get(model=self.ct).status, Event.STATUS_PROCESSED)

    def test_emission_outside_transaction_is_rejected(self):
        manufacturer = Manufacturer.objects.create(name="Acme", slug="acme")
        with patch.object(connection, "in_atomic_block", False):
            with self.assertRaises(svc.EventOutsideTransactionError):
                svc.dispatch_event(Manufacturer, manufacturer, action="create")


class OutboxProcessingTests(TransactionTestCase):
    def setUp(self):
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        _rule(self.ct)
        with patch.object(svc, "_publish_hint"):
            Manufacturer.objects.create(name="Acme", slug="acme")
        self.event = Event.objects.get(model=self.ct)

    def test_failure_retries_with_backoff_without_duplicating_event(self):
        with patch.object(svc, "_execute_event_action", side_effect=RuntimeError("boom")):
            pass
        with patch.object(svc, "process_event_rules", side_effect=RuntimeError("boom")):
            self.assertFalse(svc.process_event(self.event.pk))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PENDING)
        self.assertEqual(self.event.attempts, 1)
        self.assertGreater(self.event.available_at, timezone.now())
        self.assertEqual(Event.objects.filter(model=self.ct).count(), 1)
        self.assertFalse(svc.process_event(self.event.pk))  # not yet due
        Event.objects.filter(pk=self.event.pk).update(available_at=timezone.now())
        self.assertTrue(svc.process_event(self.event.pk))
        self.assertEqual(Event.objects.get(pk=self.event.pk).status, Event.STATUS_PROCESSED)

    def test_attempts_exhausted_goes_dead(self):
        Event.objects.filter(pk=self.event.pk).update(attempts=svc.EVENT_MAX_ATTEMPTS - 1)
        with patch.object(svc, "process_event_rules", side_effect=RuntimeError("boom")):
            svc.process_event(self.event.pk)
        self.assertEqual(Event.objects.get(pk=self.event.pk).status, Event.STATUS_DEAD)

    def test_recovery_finds_event_without_delivery_rows(self):
        self.assertEqual(self.event.deliveries.count() if hasattr(self.event, "deliveries") else 0, 0)
        self.assertEqual(svc.recover_pending_events()["due"], 1)


class OutboxTenantSelectionTests(TransactionTestCase):
    """The #1102 defect itself: a repaired (``tenant_id`` set) event must select
    its tenant's rule, where a NULL-tenant event matches only global rules."""

    def setUp(self):
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        self.tenant = baker.make("organization.Tenant")
        self.other_tenant = baker.make("organization.Tenant")
        self.tenant_rule = _rule(self.ct, name="tenant rule", tenant=self.tenant)
        self.other_rule = _rule(self.ct, name="other rule", tenant=self.other_tenant)
        self.global_rule = _rule(self.ct, name="global rule", tenant=None)

    def _event(self, tenant_id):
        return Event.objects.create(
            model=self.ct,
            object_id=1,
            action="create",
            data={},
            status=Event.STATUS_PENDING,
            tenant_id=tenant_id,
        )

    def test_repaired_event_selects_tenant_specific_rule(self):
        event = self._event(self.tenant.pk)
        selected = list(svc._eligible_rules(event, event.tenant_id))
        self.assertIn(self.tenant_rule, selected)
        self.assertIn(self.global_rule, selected)
        self.assertNotIn(self.other_rule, selected)

    def test_unrepaired_null_tenant_event_misses_every_tenant_rule(self):
        event = self._event(None)
        selected = list(svc._eligible_rules(event, event.tenant_id))
        self.assertNotIn(self.tenant_rule, selected)
        self.assertNotIn(self.other_rule, selected)
        self.assertIn(self.global_rule, selected)

    def test_pending_null_tenant_event_with_missing_object_is_failed_closed(self):
        """The live guard must not run global rules for an unresolved legacy row."""
        asset_ct = ContentType.objects.get_for_model(Asset)
        event = Event._base_manager.create(
            model=asset_ct,
            object_id=999_999_999,
            action="create",
            data={},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertFalse(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_DEAD)
        self.assertEqual(event.last_error_class, "outbox.backfill_unassigned")

    def test_pending_null_tenant_event_with_anchor_is_repaired_before_processing(self):
        asset_ct = ContentType.objects.get_for_model(Asset)
        asset = baker.make(Asset, tenant=self.tenant)
        _rule(asset_ct, name="asset rule", tenant=self.tenant)
        event = Event._base_manager.create(
            model=asset_ct,
            object_id=asset.pk,
            action="create",
            data={},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertTrue(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.tenant_id, self.tenant.pk)
        self.assertEqual(event.status, Event.STATUS_PROCESSED)

    def test_pending_null_tenant_event_for_tenantless_model_stays_global(self):
        manufacturer = Manufacturer.objects.create(name="Globex", slug="globex")
        event = Event._base_manager.create(
            model=self.ct,
            object_id=manufacturer.pk,
            action="create",
            data={},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertTrue(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_PROCESSED)

    def test_missing_object_of_tenantless_model_stays_global(self):
        """A delete event for a global model has no row left; the contract, not the row, decides."""
        event = Event._base_manager.create(
            model=self.ct,
            object_id=999_999_999,
            action="delete",
            data={},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertTrue(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_PROCESSED)

    def test_tenantless_row_without_provenance_fails_closed(self):
        """A NULL tenant on a row that can be global is only legitimate when the
        emitter recorded that scope; otherwise it is unresolved legacy data."""
        from django.apps import apps as django_apps

        supplier_model = django_apps.get_model("assets", "Supplier")
        supplier = baker.make(supplier_model)
        event = Event._base_manager.create(
            model=ContentType.objects.get_for_model(supplier_model),
            object_id=supplier.pk,
            action="create",
            data={"app_label": "assets", "model_name": "supplier"},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertFalse(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_DEAD)
        self.assertEqual(event.last_error_class, "outbox.backfill_unassigned")

    def test_declared_global_event_without_row_runs_global_rules(self):
        from django.apps import apps as django_apps

        supplier_model = django_apps.get_model("assets", "Supplier")
        _rule(ContentType.objects.get_for_model(supplier_model), name="supplier global rule", tenant=None)
        event = Event._base_manager.create(
            model=ContentType.objects.get_for_model(supplier_model),
            object_id=999_999_997,
            action="delete",
            data={"tenant_scope": "global"},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertTrue(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.status, Event.STATUS_PROCESSED)

    def test_indirect_tenant_anchor_is_repaired_live(self):
        """Lookup-only models have no ``tenant_id``; the guard must walk the lookup."""
        from django.apps import apps as django_apps

        asset = baker.make(Asset, tenant=self.tenant)
        maintenance = baker.make(django_apps.get_model("assets", "AssetMaintenance"), asset=asset)
        maintenance_ct = ContentType.objects.get_for_model(type(maintenance))
        _rule(maintenance_ct, name="maintenance rule", tenant=self.tenant)
        event = Event._base_manager.create(
            model=maintenance_ct,
            object_id=maintenance.pk,
            action="create",
            data={"tenant_scope": "tenant"},
            status=Event.STATUS_PENDING,
            tenant_id=None,
        )

        self.assertTrue(svc.process_event(event.pk))
        event.refresh_from_db()
        self.assertEqual(event.tenant_id, self.tenant.pk)
        self.assertEqual(event.status, Event.STATUS_PROCESSED)


class OutboxClaimRaceTests(TransactionTestCase):
    """Two real connections: a second worker's ``skip_locked`` claim of a locked
    event returns nothing, so the event is processed exactly once (#1102)."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        _rule(self.ct)
        with patch.object(svc, "_publish_hint"):
            Manufacturer.objects.create(name="Acme", slug="acme")
        self.event = Event.objects.get(model=self.ct)

    def test_second_worker_skips_claimed_event(self):
        a_locked = threading.Event()
        release_a = threading.Event()
        b_done = threading.Event()
        b_result = {}

        def worker_a():
            try:
                with transaction.atomic():
                    # Hold the row lock exactly as ``process_event`` does.
                    Event._base_manager.select_for_update(of=("self",)).get(pk=self.event.pk)
                    a_locked.set()
                    release_a.wait(timeout=20)
            finally:
                connections["default"].close()

        def worker_b():
            try:
                a_locked.wait(timeout=20)
                # The real claim path: ``skip_locked`` must find nothing and return False.
                b_result["processed"] = svc.process_event(self.event.pk)
            finally:
                b_done.set()
                connections["default"].close()

        thread_a = threading.Thread(target=worker_a)
        thread_b = threading.Thread(target=worker_b)
        thread_a.start()
        thread_b.start()
        self.assertTrue(b_done.wait(timeout=25), "worker B did not finish")
        release_a.set()
        thread_a.join(25)
        thread_b.join(25)
        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())

        # B skipped the locked row...
        self.assertFalse(b_result["processed"])
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.STATUS_PENDING)
        # ...and the event is still claimable exactly once now that A released it.
        self.assertTrue(svc.process_event(self.event.pk))
        self.assertEqual(Event.objects.get(pk=self.event.pk).status, Event.STATUS_PROCESSED)
        self.assertEqual(Event.objects.filter(model=self.ct).count(), 1)


@pytest.mark.serial_only
class OutboxTenantBackfillMigrationTests(TransactionTestCase):
    """Real migration test over pre-0007 rows.

    ``django-test-migrations`` is not a dependency, so the migration state is
    driven with ``MigrationExecutor`` directly: roll extras back to 0006, build
    ``pending`` events with ``tenant_id IS NULL`` against the historical schema,
    then migrate forward to 0007 and assert the repair.
    """

    migrate_from = ("extras", "0006_event_outbox")
    migrate_to = ("extras", "0007_backfill_event_tenant")

    def setUp(self):
        super().setUp()
        self.apps = self._migrate_to_node(self.migrate_from)

    def tearDown(self):
        # Restore the database to the latest migration for the rest of the suite.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _migrate_to_node(self, extras_node):
        """Migrate every app to its current leaf, except ``extras`` to ``extras_node``.

        Migrating a single app target would unapply every other app's migrations,
        which drops the tables this test builds its historical rows in.
        """
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        targets = [(app_label, name) for app_label, name in executor.loader.graph.leaf_nodes() if app_label != "extras"]
        targets.append(extras_node)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def _migrate_forward(self):
        return self._migrate_to_node(self.migrate_to)

    def _ct(self, app_label, model):
        ContentType = self.apps.get_model("contenttypes", "ContentType")
        ct, _created = ContentType.objects.get_or_create(app_label=app_label, model=model)
        return ct

    def _event(self, ct, object_id):
        Event = self.apps.get_model("extras", "Event")
        return Event.objects.create(
            model=ct,
            object_id=object_id,
            action="create",
            data={},
            status="pending",
            tenant_id=None,
            available_at=timezone.now(),
        )

    def setUpHistoricalData(self):
        Tenant = self.apps.get_model("organization", "Tenant")
        Asset = self.apps.get_model("assets", "Asset")
        AssetMaintenance = self.apps.get_model("assets", "AssetMaintenance")
        HistManufacturer = self.apps.get_model("assets", "Manufacturer")

        self.tenant_direct = baker.make(Tenant)
        self.tenant_indirect = baker.make(Tenant)

        # Direct ``tenant_id`` anchor.
        asset = baker.make(Asset, tenant=self.tenant_direct, asset_tag="DIRECT-1")
        self.direct_event = self._event(self._ct("assets", "asset"), asset.pk)

        # Indirect anchor through ``tenant_lookup = 'asset__tenant'``.
        other_asset = baker.make(Asset, tenant=self.tenant_indirect, asset_tag="INDIRECT-1")
        maintenance = baker.make(AssetMaintenance, asset=other_asset)
        self.indirect_event = self._event(self._ct("assets", "assetmaintenance"), maintenance.pk)

        # Tenant-less model: no anchor at all -> unassignable.
        manufacturer = baker.make(HistManufacturer)
        self.global_model_event = self._event(self._ct("assets", "manufacturer"), manufacturer.pk)

        # Deleted / missing object: the row no longer exists.
        self.missing_event = self._event(self._ct("assets", "asset"), 999_999_999)

        # Soft-deleted object: still resolvable through _base_manager.
        deleted_asset = baker.make(Asset, tenant=self.tenant_direct, asset_tag="DELETED-1")
        Asset.objects.filter(pk=deleted_asset.pk).update(deleted_at=timezone.now())
        self.soft_deleted_event = self._event(self._ct("assets", "asset"), deleted_asset.pk)

        # Global model whose object is already gone: still a global event.
        self.missing_global_event = self._event(self._ct("assets", "manufacturer"), 999_999_998)

        # Tenant-capable model whose object is gone: legacy NULL has no proof of scope.
        self.missing_supplier_event = self._event(self._ct("assets", "supplier"), 999_999_996)

        # Tenant-capable row that is tenantless now: migration-time state is no proof either.
        HistSupplier = self.apps.get_model("assets", "Supplier")
        self.tenantless_supplier_event = self._event(self._ct("assets", "supplier"), baker.make(HistSupplier).pk)

        # Unknown model: a ContentType whose model is not installed.
        self.unknown_event = self._event(self._ct("assets", "ghostmodel"), 1)

        Event = self.apps.get_model("extras", "Event")
        self.assigned_event = Event.objects.create(
            model=self._ct("assets", "asset"),
            object_id=asset.pk,
            action="create",
            data={},
            status="pending",
            tenant_id=self.tenant_indirect.pk,
            available_at=timezone.now(),
        )
        self.processed_event = Event.objects.create(
            model=self._ct("assets", "asset"),
            object_id=asset.pk,
            action="create",
            data={},
            status="processed",
            tenant_id=None,
            available_at=timezone.now(),
        )

    def test_backfill_assigns_and_fails_closed(self):
        self.setUpHistoricalData()
        new_apps = self._migrate_forward()
        Event = new_apps.get_model("extras", "Event")

        direct = Event.objects.get(pk=self.direct_event.pk)
        self.assertEqual(direct.status, "pending")
        self.assertEqual(direct.tenant_id, self.tenant_direct.pk)

        indirect = Event.objects.get(pk=self.indirect_event.pk)
        self.assertEqual(indirect.status, "pending")
        self.assertEqual(indirect.tenant_id, self.tenant_indirect.pk)

        soft_deleted = Event.objects.get(pk=self.soft_deleted_event.pk)
        self.assertEqual(soft_deleted.status, "pending")
        self.assertEqual(soft_deleted.tenant_id, self.tenant_direct.pk)

        for pk in (
            self.missing_event.pk,
            self.missing_supplier_event.pk,
            self.tenantless_supplier_event.pk,
            self.unknown_event.pk,
        ):
            dead = Event.objects.get(pk=pk)
            self.assertEqual(dead.status, "dead")
            self.assertEqual(dead.last_error_class, "outbox.backfill_unassigned")
            self.assertIsNone(dead.tenant_id)

        # A model with no tenant anchor stays a legitimate global event.
        for pk in (self.global_model_event.pk, self.missing_global_event.pk):
            global_event = Event.objects.get(pk=pk)
            self.assertEqual(global_event.status, "pending")
            self.assertIsNone(global_event.tenant_id)

        # Already-assigned and non-pending rows are untouched.
        self.assertEqual(Event.objects.get(pk=self.assigned_event.pk).tenant_id, self.tenant_indirect.pk)
        processed = Event.objects.get(pk=self.processed_event.pk)
        self.assertEqual(processed.status, "processed")
        self.assertIsNone(processed.tenant_id)

        # Acceptance: no tenant-anchored pending event is left without a tenant.
        self.assertFalse(
            Event.objects.filter(
                status="pending", tenant_id__isnull=True, model_id=self._ct("assets", "asset").pk
            ).exists()
        )

    def test_repair_crosses_a_full_batch_of_global_rows(self):
        """Keyset pagination must not stop at a full batch of kept global rows."""
        self.setUpHistoricalData()
        Event = self.apps.get_model("extras", "Event")
        Asset = self.apps.get_model("assets", "Asset")
        Manufacturer = self.apps.get_model("assets", "Manufacturer")
        manufacturer_ct = self._ct("assets", "manufacturer")
        manufacturer = baker.make(Manufacturer)
        Event.objects.bulk_create(
            [
                Event(
                    model=manufacturer_ct,
                    object_id=manufacturer.pk,
                    action="create",
                    data={},
                    status="pending",
                    tenant_id=None,
                    available_at=timezone.now(),
                )
                for _ in range(505)
            ]
        )
        asset = baker.make(Asset, tenant=self.tenant_direct, asset_tag="BEHIND-BATCH")
        late_event = self._event(self._ct("assets", "asset"), asset.pk)

        new_apps = self._migrate_forward()
        LateEvent = new_apps.get_model("extras", "Event")
        repaired = LateEvent.objects.get(pk=late_event.pk)
        self.assertEqual(repaired.status, "pending")
        self.assertEqual(repaired.tenant_id, self.tenant_direct.pk)

    def test_backfill_is_idempotent(self):
        self.setUpHistoricalData()
        new_apps = self._migrate_forward()
        Event = new_apps.get_model("extras", "Event")
        before = {e.pk: (e.status, e.tenant_id, e.last_error_class) for e in Event.objects.all()}

        migration = import_module("extras.migrations.0007_backfill_event_tenant")
        with transaction.atomic():
            migration.backfill_event_tenant(new_apps, None)

        after = {e.pk: (e.status, e.tenant_id, e.last_error_class) for e in Event.objects.all()}
        self.assertEqual(before, after)

    def test_reverse_is_a_noop(self):
        self.setUpHistoricalData()
        new_apps = self._migrate_forward()
        Event = new_apps.get_model("extras", "Event")
        self.assertEqual(Event.objects.get(pk=self.direct_event.pk).tenant_id, self.tenant_direct.pk)

        # Reverse to 0006 runs 0007's documented no-op reverse: no error, data intact.
        reverted_apps = self._migrate_to_node(self.migrate_from)
        RevEvent = reverted_apps.get_model("extras", "Event")
        self.assertEqual(RevEvent.objects.get(pk=self.direct_event.pk).tenant_id, self.tenant_direct.pk)
        global_event = RevEvent.objects.get(pk=self.global_model_event.pk)
        self.assertEqual(global_event.status, "pending")
        self.assertIsNone(global_event.tenant_id)


class OutboxRuleIndexCacheTests(TransactionTestCase):
    """#1103: the transactional write path must build the rule index once, not per dispatch."""

    def setUp(self):
        cache.delete(svc._RULE_INDEX_CACHE_KEY)
        self.ct = ContentType.objects.get_for_model(Manufacturer)
        _rule(self.ct, name="cached rule", tenant=None)

    def tearDown(self):
        cache.delete(svc._RULE_INDEX_CACHE_KEY)

    def test_hundred_dispatches_in_one_transaction_build_once(self):
        with patch.object(svc, "_build_rule_index", wraps=svc._build_rule_index) as build:
            with transaction.atomic():
                for _ in range(100):
                    self.assertTrue(svc.has_eligible_rules(self.ct.pk, None, "create"))

        self.assertEqual(build.call_count, 1)

    def test_committed_transaction_publishes_the_index(self):
        with transaction.atomic():
            svc.has_eligible_rules(self.ct.pk, None, "create")

        cached = cache.get(svc._RULE_INDEX_CACHE_KEY)
        self.assertIn((self.ct.pk, None, "create"), cached)

    def test_rollback_publishes_nothing(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                svc.has_eligible_rules(self.ct.pk, None, "create")
                raise RuntimeError("rollback")

        self.assertIsNone(cache.get(svc._RULE_INDEX_CACHE_KEY))

    def test_rule_write_inside_transaction_rebuilds_before_publishing(self):
        with patch.object(svc, "_build_rule_index", wraps=svc._build_rule_index) as build:
            with transaction.atomic():
                svc.has_eligible_rules(self.ct.pk, None, "create")
                _rule(self.ct, name="late rule", events=["update"], tenant=None)
                self.assertTrue(svc.has_eligible_rules(self.ct.pk, None, "update"))

        self.assertEqual(build.call_count, 2)
        cached = cache.get(svc._RULE_INDEX_CACHE_KEY)
        self.assertIn((self.ct.pk, None, "update"), cached)

    def test_cache_outage_falls_back_to_the_database(self):
        with (
            patch.object(svc.cache, "get", side_effect=RuntimeError("down")),
            patch.object(svc.cache, "set", side_effect=RuntimeError("down")),
        ):
            self.assertTrue(svc.has_eligible_rules(self.ct.pk, None, "create"))
