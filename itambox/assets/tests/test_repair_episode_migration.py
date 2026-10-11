"""Rehearsal of the RepairEpisode retirement migration (#644).

The 0122 migration has to translate every beta-era episode onto the
maintenance-anchored model without dropping a fact. These fixtures are the
shapes the beta release could really hold: one maintenance, none, several, a
substitute with and without a matching loan, and a reservation-linked episode.
"""

import datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

MIGRATE_FROM = ("assets", "0121_supplier_scoping_and_commercial_fields")
MIGRATE_TO = ("assets", "0122_repair_maintenance_anchor")


@pytest.mark.serial_only
class RepairEpisodeMigrationTests(TransactionTestCase):
    """Migrate a real predecessor state and assert every outcome."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_leaf)
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([MIGRATE_FROM])
        old_apps = self.executor.loader.project_state([MIGRATE_FROM]).apps

        Asset = old_apps.get_model("assets", "Asset")
        AssetAssignment = old_apps.get_model("assets", "AssetAssignment")
        AssetDisposal = old_apps.get_model("assets", "AssetDisposal")
        AssetMaintenance = old_apps.get_model("assets", "AssetMaintenance")
        AssetReservation = old_apps.get_model("assets", "AssetReservation")
        AssetHolder = old_apps.get_model("organization", "AssetHolder")
        StatusLabel = old_apps.get_model("assets", "StatusLabel")
        RepairEpisode = old_apps.get_model("assets", "RepairEpisode")

        holder = AssetHolder.objects.create(first_name="Mig", last_name="Holder", upn="mig.holder@example.com")

        # The test database already carries the migration-seeded labels (the repair
        # migration is rehearsed by stepping the graph down, not by wiping the rows).
        status = StatusLabel.objects.filter(type="deployable").first()
        if status is None:
            status = StatusLabel.objects.create(name="Available (mig)", slug="available-mig", type="deployable")
        self.assets = {}
        for label in (
            "one_repair",
            "no_repair",
            "several_repairs",
            "substitute_loan",
            "substitute_bare",
            "reserved",
            "ordinary",
        ):
            self.assets[label] = Asset.objects.create(name=f"Laptop {label}", asset_tag=f"MIG-{label}", status=status)
        loaner = Asset.objects.create(name="Loaner", asset_tag="MIG-loaner", status=status)

        def _repair(asset, **kwargs):
            return AssetMaintenance.objects.create(
                asset=asset,
                maintenance_type="repair",
                status="completed",
                start_date=datetime.date(2026, 1, 5),
                completion_date=datetime.date(2026, 1, 20),
                **kwargs,
            )

        # 1. Exactly one repair maintenance: the disposal moves onto it, and the
        #    episode notes are appended to its notes.
        one_repair_asset = self.assets["one_repair"]
        one_episode = RepairEpisode.objects.create(asset=one_repair_asset, notes="Screen replaced under warranty.")
        maintenance = _repair(one_repair_asset, episode=one_episode, notes="Vendor repair")
        disposal_one = AssetDisposal.objects.create(
            asset=one_repair_asset,
            disposal_method="recycle",
            disposal_date=datetime.date(2026, 2, 1),
            episode=one_episode,
        )

        # 2. No repair at all: the episode's disposal cannot be anchored, so the
        #    facts go to a journal entry on the asset.
        no_repair_asset = self.assets["no_repair"]
        no_repair_episode = RepairEpisode.objects.create(asset=no_repair_asset, notes="Paperwork only.")
        disposal_two = AssetDisposal.objects.create(
            asset=no_repair_asset,
            disposal_method="recycle",
            disposal_date=datetime.date(2026, 2, 2),
            episode=no_repair_episode,
        )

        # 3. Several repair maintenance records: no single anchor either.
        several_asset = self.assets["several_repairs"]
        several_episode = RepairEpisode.objects.create(asset=several_asset, notes="Two workshops.")
        first_repair = _repair(several_asset, episode=several_episode)
        second_repair = _repair(several_asset, episode=several_episode)
        disposal_three = AssetDisposal.objects.create(
            asset=several_asset,
            disposal_method="recycle",
            disposal_date=datetime.date(2026, 2, 3),
            episode=several_episode,
        )

        # 4. A substitute that really stood in during the window: its loan links
        #    to the maintenance.
        substitute_asset = self.assets["substitute_loan"]
        substitute_episode = RepairEpisode.objects.create(asset=substitute_asset, substitute_asset=loaner, notes="")
        substitute_repair = _repair(substitute_asset, episode=substitute_episode)
        loan = AssetAssignment.objects.create(
            asset=loaner,
            assigned_user=holder,
            is_loan=True,
            is_active=False,
            checked_out_at=timezone.make_aware(datetime.datetime(2026, 1, 6, 9, 0)),
            checked_in_at=timezone.make_aware(datetime.datetime(2026, 1, 19, 9, 0)),
            due_date=datetime.date(2026, 1, 20),
        )

        # 5. A substitute without any matching loan: the link cannot be invented.
        bare_asset = self.assets["substitute_bare"]
        bare_episode = RepairEpisode.objects.create(asset=bare_asset, substitute_asset=loaner, notes="")
        bare_repair = _repair(bare_asset, episode=bare_episode)

        # 6. A reservation-linked episode: a reservation is a booking, not a
        #    handover, so it stays standing and is reported.
        reserved_asset = self.assets["reserved"]
        reserved_episode = RepairEpisode.objects.create(asset=reserved_asset, notes="")
        reserved_repair = _repair(reserved_asset, episode=reserved_episode)
        reservation = AssetReservation.objects.create(
            asset=reserved_asset,
            start_date=datetime.date(2026, 1, 8),
            end_date=datetime.date(2026, 1, 10),
            episode=reserved_episode,
        )

        # 7. A substitute whose only overlapping assignment is an ordinary one
        #    (is_loan=False): it is not loan evidence and must not be linked.
        ordinary_asset = self.assets["ordinary"]
        ordinary_loaner = Asset.objects.create(name="Loaner 2", asset_tag="MIG-loaner2", status=status)
        ordinary_episode = RepairEpisode.objects.create(
            asset=ordinary_asset, substitute_asset=ordinary_loaner, notes=""
        )
        ordinary_repair = _repair(ordinary_asset, episode=ordinary_episode)
        ordinary_assignment = AssetAssignment.objects.create(
            asset=ordinary_loaner,
            assigned_user=holder,
            is_loan=False,
            is_active=False,
            checked_out_at=timezone.make_aware(datetime.datetime(2026, 1, 6, 9, 0)),
            checked_in_at=timezone.make_aware(datetime.datetime(2026, 1, 19, 9, 0)),
        )

        connection.commit()
        self.expected = {
            "ordinary_repair": ordinary_repair.pk,
            "ordinary_assignment": ordinary_assignment.pk,
            "maintenance": maintenance.pk,
            "disposal_one": disposal_one.pk,
            "disposal_two": disposal_two.pk,
            "disposal_three": disposal_three.pk,
            "first_repair": first_repair.pk,
            "second_repair": second_repair.pk,
            "substitute_repair": substitute_repair.pk,
            "bare_repair": bare_repair.pk,
            "reserved_repair": reserved_repair.pk,
            "loan": loan.pk,
            "reservation": reservation.pk,
            "assets": {label: asset.pk for label, asset in self.assets.items()},
        }

        self.executor = MigrationExecutor(connection)
        self.executor.migrate([MIGRATE_TO])
        self.apps = self.executor.loader.project_state([MIGRATE_TO]).apps

    @staticmethod
    def _restore_leaf():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_the_retired_model_is_gone(self):
        with self.assertRaises(LookupError):
            self.apps.get_model("assets", "RepairEpisode")

    def test_a_single_repair_anchors_its_disposal_and_takes_the_notes(self):
        AssetMaintenance = self.apps.get_model("assets", "AssetMaintenance")
        AssetDisposal = self.apps.get_model("assets", "AssetDisposal")

        maintenance = AssetMaintenance.objects.get(pk=self.expected["maintenance"])
        disposal = AssetDisposal.objects.get(pk=self.expected["disposal_one"])

        self.assertEqual(disposal.maintenance_id, maintenance.pk)
        self.assertIn("Vendor repair", maintenance.notes)
        self.assertIn("Screen replaced under warranty.", maintenance.notes)

    def test_episodes_without_a_single_anchor_become_asset_journal_entries(self):
        JournalEntry = self.apps.get_model("extras", "JournalEntry")
        AssetDisposal = self.apps.get_model("assets", "AssetDisposal")

        comments = {
            entry.object_id: entry.comment
            for entry in JournalEntry.objects.filter(object_id__in=list(self.expected["assets"].values()))
        }
        no_repair = comments[self.expected["assets"]["no_repair"]]
        self.assertIn("Paperwork only.", no_repair)
        self.assertIn("Disposal", no_repair)

        several = comments[self.expected["assets"]["several_repairs"]]
        self.assertIn("2 repair maintenance record(s)", several)
        self.assertIn("Two workshops.", several)

        # The later two dispose rows keep standing, unlinked, and stay visible:
        # nothing was deleted even though it could not be anchored.
        self.assertIsNone(AssetDisposal.objects.get(pk=self.expected["disposal_two"]).maintenance_id)
        self.assertIsNone(AssetDisposal.objects.get(pk=self.expected["disposal_three"]).maintenance_id)

    def test_a_matching_stand_in_loan_links_to_the_repair(self):
        AssetAssignment = self.apps.get_model("assets", "AssetAssignment")
        loan = AssetAssignment.objects.get(pk=self.expected["loan"])
        self.assertEqual(loan.maintenance_id, self.expected["substitute_repair"])

    def test_a_stand_in_without_a_loan_is_reported_not_invented(self):
        AssetAssignment = self.apps.get_model("assets", "AssetAssignment")
        AssetMaintenance = self.apps.get_model("assets", "AssetMaintenance")

        self.assertFalse(AssetAssignment.objects.filter(maintenance_id=self.expected["bare_repair"]).exists())
        notes = AssetMaintenance.objects.get(pk=self.expected["bare_repair"]).notes
        self.assertIn("could not be translated into exactly one loan", notes)

    def test_a_reservation_link_is_reported_and_the_row_survives(self):
        AssetMaintenance = self.apps.get_model("assets", "AssetMaintenance")
        AssetReservation = self.apps.get_model("assets", "AssetReservation")

        self.assertTrue(AssetReservation.objects.filter(pk=self.expected["reservation"]).exists())
        notes = AssetMaintenance.objects.get(pk=self.expected["reserved_repair"]).notes
        self.assertIn("Reservation", notes)

    def test_the_migration_is_recorded_as_applied(self):
        """The rehearsal really applied 0122 rather than silently skipping it."""
        from django.db.migrations.recorder import MigrationRecorder

        applied = set(MigrationRecorder(connection).applied_migrations())
        self.assertIn(MIGRATE_TO, applied)

    def test_an_ordinary_assignment_is_not_mapped_as_a_repair_loan(self):
        AssetAssignment = self.apps.get_model("assets", "AssetAssignment")
        AssetMaintenance = self.apps.get_model("assets", "AssetMaintenance")

        self.assertIsNone(AssetAssignment.objects.get(pk=self.expected["ordinary_assignment"]).maintenance_id)
        notes = AssetMaintenance.objects.get(pk=self.expected["ordinary_repair"]).notes
        self.assertIn("could not be translated into exactly one loan", notes)
