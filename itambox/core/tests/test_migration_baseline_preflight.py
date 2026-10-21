"""Contracts for the read-only migration-baseline recognition preflight."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, InterfaceError, connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from core.migration_preflight import (
    classify_applied_migrations,
    format_table,
    load_manifest,
    recorder_unavailable_result,
)

BETA5_ORIGIN = "v1.0.0-beta.5"
# Rows that `v1.0.0-beta.4` lacks: a beta.4 database is a skipped release for this candidate.
AFTER_BETA4_IDS = (
    "assets.0127_core_vocabulary_german_translations",
    "core.0101_plugin_diagnostics_permission",
    "extras.0130_definition_translations",
)


def _beta5_predecessor(manifest):
    return next(item for item in manifest["recognized_states"] if item["name"] == BETA5_ORIGIN)


def _beta5_recorder(manifest, *, post_transition_ids=None):
    """Recorder rows of a database that is exactly at the declared origin `v1.0.0-beta.5`."""

    rows = set(manifest["historical_ids"]) | set(manifest["replacement_ids"])
    rows |= set(
        _beta5_predecessor(manifest)["recognition"]["post_transition_ids"]
        if post_transition_ids is None
        else post_transition_ids
    )
    return rows


def _current_recorder(manifest):
    """Recorder rows of a fresh install or of a database that finished the candidate's migrations."""

    return set(manifest["historical_ids"]) | set(manifest["replacement_ids"]) | set(manifest["post_transition_ids"])


def _manifest(*, layout="transitional"):
    historical_ids = ["assets.0001_initial", "assets.0002_second"]
    replacement_ids = ["assets.0100_other", "assets.0100_shard"]
    return {
        "schema_version": 1,
        "layout": layout,
        "first_party_apps": ["assets"],
        "historical_ids": historical_ids,
        "replacement_ids": replacement_ids,
        "replacement_target_ids": historical_ids,
        "baseline_ids": replacement_ids,
        "post_transition_ids": ["assets.0101_current", "assets.0102_next"],
        "post_transition_leaf_ids": ["assets.0102_next"],
        "current_leaf_ids": ["assets.0102_next"],
        "transition_release_sha": "b" * 40,
        "recognized_states": [
            {
                "name": "test-pre-squash",
                "revision": "b" * 40,
                "state": "complete-old-history-no-replacement",
                "recognition": {
                    "state": "supported-predecessor-test-pre-squash",
                    "replacement": "absent",
                    "post_transition_ids": [],
                },
            },
            {
                "name": "test-transition",
                "revision": "c" * 40,
                "state": "complete-replacement-recognition",
                "recognition": {
                    "state": "supported-predecessor-test-transition",
                    "replacement": "complete",
                    "post_transition_ids": ["assets.0101_current"],
                },
            },
        ],
    }


class MigrationBaselineClassifierTests(SimpleTestCase):
    def assert_state(self, applied, expected_state, *, layout="transitional", exit_code=None):
        result = classify_applied_migrations(set(applied), _manifest(layout=layout))
        self.assertEqual(result.state, expected_state)
        if exit_code is not None:
            self.assertEqual(result.exit_code, exit_code)
        return result

    def test_complete_replacement_recognition_requires_both_transition_sets(self):
        result = self.assert_state(
            {
                "assets.0001_initial",
                "assets.0002_second",
                "assets.0100_other",
                "assets.0100_shard",
                "assets.0101_current",
                "assets.0102_next",
            },
            "complete-replacement-recognition",
            exit_code=0,
        )
        self.assertEqual(result.reason_code, "REPLACEMENT_RECOGNIZED")

    def test_complete_old_history_without_replacement_is_rejected(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0002_second"},
            "complete-old-history-no-replacement",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "TRANSITION_RELEASE_REQUIRED")
        self.assertIn("ordinary", result.remediation)

    def test_partial_old_history_is_rejected(self):
        result = self.assert_state({"assets.0001_initial"}, "partial-old-history", exit_code=1)
        self.assertEqual(result.reason_code, "PARTIAL_OLD_HISTORY")

    def test_partial_replacement_set_is_rejected(self):
        result = self.assert_state({"assets.0100_shard"}, "partial-replacement-set", exit_code=1)
        self.assertEqual(result.reason_code, "PARTIAL_REPLACEMENT_SET")

    def test_partial_post_transition_state_is_rejected(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0002_second", "assets.0100_other", "assets.0100_shard"},
            "partial-post-transition-state",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_INCOMPLETE")

    def test_mixed_or_unknown_first_party_rows_are_rejected(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0100_shard", "assets.9999_unknown"},
            "mixed-or-unknown-first-party-state",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "UNKNOWN_FIRST_PARTY_MIGRATION")
        self.assertEqual(result.unexpected_ids, ("assets.9999_unknown",))

    def test_empty_database_is_rejected_without_being_called_normalized(self):
        result = self.assert_state(set(), "empty-or-unmigrated", exit_code=1)
        self.assertEqual(result.reason_code, "NO_FIRST_PARTY_MIGRATIONS")

    def test_normalized_layout_is_a_distinct_success_state_and_allows_stale_history(self):
        result = self.assert_state(
            {
                "assets.0001_initial",
                "assets.0100_other",
                "assets.0100_shard",
                "assets.0101_current",
                "assets.0102_next",
            },
            "current-normalized-baseline",
            layout="normalized",
            exit_code=0,
        )
        self.assertEqual(result.reason_code, "NORMALIZED_BASELINE")

    def test_normalized_layout_rejects_an_incomplete_baseline(self):
        result = self.assert_state(
            {"assets.0100_shard"},
            "partial-normalized-baseline",
            layout="normalized",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "NORMALIZED_BASELINE_INCOMPLETE")

    def test_transitional_full_replacement_without_history_is_not_normalized(self):
        result = self.assert_state(
            {"assets.0100_other", "assets.0100_shard", "assets.0101_current"},
            "normalized-baseline-not-current",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "NORMALIZED_LAYOUT_NOT_CURRENT")

    def test_transitional_replacement_with_incomplete_history_is_mixed(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0100_other", "assets.0100_shard"},
            "mixed-or-unknown-first-party-state",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "REPLACEMENT_WITH_INCOMPLETE_HISTORY")

    def test_complete_history_with_post_transition_rows_is_mixed(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0002_second", "assets.0101_current"},
            "mixed-or-unknown-first-party-state",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_WITHOUT_REPLACEMENT")

    def test_partial_history_with_post_transition_rows_is_mixed(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0101_current"},
            "mixed-or-unknown-first-party-state",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_WITH_INCOMPLETE_BASELINE")

    def test_normalized_layout_rejects_post_transition_without_baseline(self):
        result = self.assert_state(
            {"assets.0101_current"},
            "mixed-or-unknown-first-party-state",
            layout="normalized",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_WITHOUT_BASELINE")

    def test_normalized_layout_rejects_complete_baseline_without_post_transition(self):
        result = self.assert_state(
            {"assets.0100_other", "assets.0100_shard"},
            "partial-post-transition-state",
            layout="normalized",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_INCOMPLETE")

    def test_transitional_post_transition_without_any_baseline_is_unrecognized(self):
        result = self.assert_state(
            {"assets.0101_current"},
            "mixed-or-unknown-first-party-state",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "BASELINE_STATE_UNRECOGNIZED")

    def test_normalized_layout_recognizes_the_pre_squash_predecessor_shape(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0002_second"},
            "supported-predecessor-test-pre-squash",
            layout="normalized",
            exit_code=0,
        )
        self.assertEqual(result.reason_code, "SUPPORTED_PREDECESSOR_TEST_PRE_SQUASH")
        self.assertIn("ordinary", result.remediation)

    def test_normalized_layout_recognizes_the_transition_predecessor_shape(self):
        result = self.assert_state(
            {
                "assets.0001_initial",
                "assets.0002_second",
                "assets.0100_other",
                "assets.0100_shard",
                "assets.0101_current",
            },
            "supported-predecessor-test-transition",
            layout="normalized",
            exit_code=0,
        )
        self.assertEqual(result.reason_code, "SUPPORTED_PREDECESSOR_TEST_TRANSITION")
        self.assertIn("ordinary", result.remediation)

    def test_normalized_layout_rejects_the_pre_squash_shape_with_a_stale_post_row(self):
        result = self.assert_state(
            {"assets.0001_initial", "assets.0002_second", "assets.0101_current"},
            "mixed-or-unknown-first-party-state",
            layout="normalized",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_WITHOUT_BASELINE")

    def test_normalized_layout_rejects_a_partial_predecessor_history(self):
        result = self.assert_state(
            {"assets.0001_initial"},
            "partial-normalized-baseline",
            layout="normalized",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "NORMALIZED_BASELINE_INCOMPLETE")
        self.assertFalse(result.state.startswith("supported-predecessor"))

    def test_normalized_layout_rejects_the_transition_shape_with_a_missing_transition_row(self):
        result = self.assert_state(
            {
                "assets.0001_initial",
                "assets.0002_second",
                "assets.0100_other",
                "assets.0100_shard",
                "assets.0102_next",
            },
            "partial-post-transition-state",
            layout="normalized",
            exit_code=1,
        )
        self.assertEqual(result.reason_code, "POST_TRANSITION_INCOMPLETE")


class MigrationBaselineManifestTests(SimpleTestCase):
    def test_checked_manifest_has_the_expected_current_layout_and_complete_sets(self):
        manifest = load_manifest()
        self.assertEqual(manifest["layout"], "normalized")
        self.assertEqual(len(manifest["historical_ids"]), 262)
        self.assertEqual(len(manifest["replacement_ids"]), 62)
        self.assertEqual(len(manifest["replacement_target_ids"]), 262)
        self.assertEqual(manifest["baseline_ids"], manifest["replacement_ids"])
        self.assertEqual(manifest["current_leaf_ids"], manifest["post_transition_leaf_ids"])

    def test_checked_manifest_matches_the_migration_modules_on_disk(self):
        # The named baseline replaces every prerelease identity; the manifest keeps those identities
        # as the legacy rows and lists the baseline's own migrations as the current rows.
        manifest = load_manifest()
        loader = MigrationLoader(None, ignore_no_migrations=True)
        apps = set(manifest["first_party_apps"])
        on_disk = {f"{app}.{name}" for app, name in loader.disk_migrations if app in apps}
        replaced = {
            f"{app}.{name}"
            for key, migration in loader.disk_migrations.items()
            if key[0] in apps
            for app, name in migration.replaces
        }
        legacy = set(manifest["historical_ids"]) | set(manifest["replacement_ids"])
        self.assertTrue(on_disk.issubset(manifest["post_transition_ids"]))
        self.assertEqual(replaced, legacy | (set(manifest["post_transition_ids"]) - on_disk))
        self.assertFalse(on_disk & replaced)
        app_leaves = sorted(f"{app}.{name}" for app in apps for _, name in loader.graph.leaf_nodes(app) if _ == app)
        self.assertEqual(manifest["current_leaf_ids"], app_leaves)

    def test_checked_manifest_declares_only_the_release_origin_as_a_predecessor(self):
        manifest = load_manifest()
        self.assertEqual([item["name"] for item in manifest["recognized_states"]], [BETA5_ORIGIN])
        origin = _beta5_predecessor(manifest)
        self.assertEqual(origin["state"], "complete-replacement-recognition")
        self.assertEqual(origin["recognition"]["state"], "supported-predecessor-beta5")
        self.assertEqual(origin["recognition"]["replacement"], "complete")
        self.assertEqual(len(origin["recognition"]["post_transition_ids"]), 73)
        self.assertEqual(manifest["transition_release_sha"], origin["revision"])
        # The origin's own rows are a strict subset of the candidate's: the candidate adds the baseline.
        self.assertLess(set(origin["recognition"]["post_transition_ids"]), set(manifest["post_transition_ids"]))

    def test_checked_manifest_recognizes_the_declared_upgrade_origin_recorder_shape(self):
        # A beta.5 database records the 262 replaced historical identities, the 62 replacement shards
        # and that release's own 73 post-transition rows (397 rows in total).
        manifest = load_manifest()
        applied = _beta5_recorder(manifest)
        self.assertEqual(len(applied), 397)

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "supported-predecessor-beta5")
        self.assertEqual(result.reason_code, "SUPPORTED_PREDECESSOR_BETA5")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.missing_ids, ())
        self.assertEqual(result.counts["post_transition_observed"], 73)
        self.assertEqual(result.counts["post_transition_expected"], len(manifest["post_transition_ids"]))
        # Migration is still required: only the ordinary executor may run.
        self.assertIn(BETA5_ORIGIN, result.remediation)
        self.assertIn("ordinary migration executor", result.remediation)

    def test_checked_manifest_recognizes_a_finished_upgrade_and_a_fresh_install_alike(self):
        # Django records every replaced identity next to the baseline, so a fresh install and an
        # upgraded beta.5 database end in the same recorder state.
        manifest = load_manifest()
        applied = _current_recorder(manifest)
        self.assertEqual(len(applied), 262 + 62 + len(manifest["post_transition_ids"]))

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "current-normalized-baseline")
        self.assertEqual(result.reason_code, "NORMALIZED_BASELINE")
        self.assertEqual(result.exit_code, 0)

    def test_checked_manifest_rejects_a_partial_declared_origin_set(self):
        manifest = load_manifest()
        origin_ids = _beta5_predecessor(manifest)["recognition"]["post_transition_ids"]
        applied = _beta5_recorder(manifest, post_transition_ids=origin_ids[:-1])

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "partial-post-transition-state")
        self.assertEqual(result.reason_code, "POST_TRANSITION_INCOMPLETE")
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.state.startswith("supported-predecessor"))

    def test_checked_manifest_rejects_the_previous_release_as_a_skipped_origin(self):
        # A v1.0.0-beta.4 database lacks three of the beta.5 rows. Version skipping is unsupported.
        manifest = load_manifest()
        origin_ids = _beta5_predecessor(manifest)["recognition"]["post_transition_ids"]
        applied = _beta5_recorder(
            manifest, post_transition_ids=[item for item in origin_ids if item not in AFTER_BETA4_IDS]
        )
        self.assertEqual(len(applied), 394)

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.state, "partial-post-transition-state")
        self.assertEqual(set(AFTER_BETA4_IDS) - set(result.missing_ids), set())
        self.assertFalse(result.state.startswith("supported-predecessor"))

    def test_checked_manifest_rejects_an_interrupted_upgrade(self):
        # The declared origin plus only some of the candidate's own rows is a partially applied
        # migration run: restore-first, never an ordinary retry.
        manifest = load_manifest()
        candidate_rows = sorted(
            set(manifest["post_transition_ids"])
            - set(_beta5_predecessor(manifest)["recognition"]["post_transition_ids"])
        )
        self.assertTrue(candidate_rows)
        applied = _beta5_recorder(manifest) | set(candidate_rows[: len(candidate_rows) // 2])

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.state.startswith("supported-predecessor"))
        self.assertNotEqual(result.state, "current-normalized-baseline")

    def test_checked_manifest_rejects_the_declared_origin_with_a_missing_replacement_shard(self):
        manifest = load_manifest()
        applied = _beta5_recorder(manifest) - {manifest["replacement_ids"][0]}

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "mixed-or-unknown-first-party-state")
        self.assertEqual(result.reason_code, "POST_TRANSITION_WITHOUT_BASELINE")
        self.assertEqual(result.exit_code, 1)

    def test_checked_manifest_rejects_the_declared_origin_rows_without_the_replacement_layer(self):
        manifest = load_manifest()
        applied = set(manifest["historical_ids"]) | set(
            _beta5_predecessor(manifest)["recognition"]["post_transition_ids"]
        )

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "mixed-or-unknown-first-party-state")
        self.assertEqual(result.reason_code, "POST_TRANSITION_WITHOUT_BASELINE")
        self.assertEqual(result.exit_code, 1)

    def test_checked_manifest_rejects_an_unknown_row_on_the_declared_origin(self):
        manifest = load_manifest()
        applied = _beta5_recorder(manifest) | {"assets.9999_unknown"}

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "mixed-or-unknown-first-party-state")
        self.assertEqual(result.reason_code, "UNKNOWN_FIRST_PARTY_MIGRATION")
        self.assertEqual(result.unexpected_ids, ("assets.9999_unknown",))
        self.assertEqual(result.exit_code, 1)

    def test_checked_manifest_rejects_a_partial_replacement_set_without_post_rows(self):
        manifest = load_manifest()
        applied = set(manifest["historical_ids"]) | {manifest["replacement_ids"][0]}

        result = classify_applied_migrations(applied, manifest)

        self.assertEqual(result.state, "partial-normalized-baseline")
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.state.startswith("supported-predecessor"))

    def test_checked_manifest_rejects_malformed_shape(self):
        cases = (
            ("schema", lambda m: m.update(schema_version=2)),
            ("layout", lambda m: m.update(layout="unknown")),
            ("apps_type", lambda m: m.update(first_party_apps="assets")),
            ("duplicate_apps", lambda m: m.update(first_party_apps=["assets", "assets"])),
            ("unsorted_baseline", lambda m: m.update(baseline_ids=["assets.0100_shard", "assets.0100_other"])),
            ("target_set", lambda m: m.update(replacement_target_ids=["assets.0001_initial"])),
            ("baseline_set", lambda m: m.update(baseline_ids=["assets.0100_shard"])),
            ("leaf_set", lambda m: m.update(post_transition_leaf_ids=["assets.0001_initial"])),
            (
                "baseline_unknown",
                lambda m: m.update(layout="normalized", baseline_ids=["assets.0100_other", "assets.9999_base"]),
            ),
            ("transition_sha", lambda m: m.update(transition_release_sha="z" * 40)),
            ("no_predecessors", lambda m: m.update(recognized_states=[])),
            ("predecessor_type", lambda m: m.update(recognized_states=["bad"])),
            ("predecessor_name", lambda m: m.update(recognized_states=[{"revision": "b" * 40, "state": "old"}])),
            (
                "predecessor_revision",
                lambda m: m.update(recognized_states=[{"name": "old", "revision": "bad", "state": "old"}]),
            ),
            (
                "predecessor_state",
                lambda m: m.update(recognized_states=[{"name": "old", "revision": "b" * 40, "state": "unrecognized"}]),
            ),
            (
                "transition_predecessor",
                lambda m: m.update(recognized_states=[{"name": "old", "revision": "a" * 40, "state": "old"}]),
            ),
            ("predecessor_recognition_missing", lambda m: m["recognized_states"][0].pop("recognition")),
            (
                "predecessor_recognition_state",
                lambda m: m["recognized_states"][0]["recognition"].update(state="not-a-recognition-state"),
            ),
            (
                "predecessor_recognition_duplicate",
                lambda m: m["recognized_states"][1]["recognition"].update(
                    state=m["recognized_states"][0]["recognition"]["state"]
                ),
            ),
            (
                "predecessor_recognition_replacement",
                lambda m: m["recognized_states"][0]["recognition"].update(replacement="partial"),
            ),
            (
                "predecessor_recognition_post_ids",
                lambda m: m["recognized_states"][0]["recognition"].update(
                    post_transition_ids=["assets.9999_unmanifested"]
                ),
            ),
            (
                "predecessor_recognition_post_duplicate",
                lambda m: m["recognized_states"][1]["recognition"].update(
                    post_transition_ids=["assets.0101_current", "assets.0101_current"]
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                manifest = _manifest()
                mutate(manifest)
                with self.assertRaises(ValueError):
                    classify_applied_migrations(set(), manifest)

    def test_load_manifest_rejects_invalid_json_and_non_object(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "manifest.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_manifest(path)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_manifest(path)


class MigrationBaselineCommandTests(SimpleTestCase):
    def test_command_emits_only_safe_json_and_never_records_state(self):
        manifest = _manifest()
        applied = {
            (item.split(".", 1)[0], item.split(".", 1)[1])
            for item in manifest["historical_ids"] + manifest["replacement_ids"] + manifest["post_transition_ids"]
        }
        stdout = io.StringIO()
        with (
            patch("core.management.commands.migration_baseline_preflight.load_manifest", return_value=manifest),
            patch("core.management.commands.migration_baseline_preflight.MigrationRecorder") as recorder_cls,
        ):
            recorder = recorder_cls.return_value
            recorder.has_table.return_value = True
            recorder.applied_migrations.return_value = applied
            call_command("migration_baseline_preflight", format="json", stdout=stdout)
            recorder.record_applied.assert_not_called()
            recorder.record_unapplied.assert_not_called()
            recorder.ensure_schema.assert_not_called()
            recorder.Migration.objects.create.assert_not_called()
            recorder.Migration.objects.update.assert_not_called()
            recorder.Migration.objects.delete.assert_not_called()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "complete-replacement-recognition")
        self.assertEqual(payload["reason_code"], "REPLACEMENT_RECOGNIZED")
        rendered = stdout.getvalue().lower()
        for forbidden in ("password", "database_url", "secret", "token"):
            self.assertNotIn(forbidden, rendered)

    def test_command_reports_the_declared_upgrade_origin_as_supported(self):
        manifest = load_manifest()
        applied = {(item.split(".", 1)[0], item.split(".", 1)[1]) for item in _beta5_recorder(manifest)}
        stdout = io.StringIO()
        with patch("core.management.commands.migration_baseline_preflight.MigrationRecorder") as recorder_cls:
            recorder = recorder_cls.return_value
            recorder.has_table.return_value = True
            recorder.applied_migrations.return_value = applied
            call_command("migration_baseline_preflight", format="json", stdout=stdout)
            recorder.record_applied.assert_not_called()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "supported-predecessor-beta5")
        self.assertEqual(payload["reason_code"], "SUPPORTED_PREDECESSOR_BETA5")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["counts"]["post_transition_observed"], 73)
        self.assertIn("ordinary", payload["remediation"])

    def test_command_rejects_unsupported_state_with_safe_remediation(self):
        stdout = io.StringIO()
        with (
            patch("core.management.commands.migration_baseline_preflight.load_manifest", return_value=_manifest()),
            patch("core.management.commands.migration_baseline_preflight.MigrationRecorder") as recorder_cls,
        ):
            recorder = recorder_cls.return_value
            recorder.has_table.return_value = True
            recorder.applied_migrations.return_value = {
                ("assets", "0001_initial"),
                ("assets", "0002_second"),
            }
            with self.assertRaises(CommandError) as raised:
                call_command("migration_baseline_preflight", format="json", stdout=stdout)

        self.assertIn("TRANSITION_RELEASE_REQUIRED", str(raised.exception))
        self.assertIn("ordinary", str(raised.exception))

    def test_command_rejects_missing_recorder_table(self):
        stdout = io.StringIO()
        with (
            patch("core.management.commands.migration_baseline_preflight.load_manifest", return_value=_manifest()),
            patch("core.management.commands.migration_baseline_preflight.MigrationRecorder") as recorder_cls,
        ):
            recorder_cls.return_value.has_table.return_value = False
            with self.assertRaises(CommandError) as raised:
                call_command("migration_baseline_preflight", format="json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "migration-recorder-missing")
        self.assertIn("MIGRATION_RECORDER_TABLE_MISSING", str(raised.exception))

    def test_command_rejects_unavailable_recorder_without_database_details(self):
        stdout = io.StringIO()
        with (
            patch("core.management.commands.migration_baseline_preflight.load_manifest", return_value=_manifest()),
            patch("core.management.commands.migration_baseline_preflight.MigrationRecorder") as recorder_cls,
        ):
            recorder_cls.return_value.has_table.side_effect = DatabaseError("database failure")
            with self.assertRaises(CommandError) as raised:
                call_command("migration_baseline_preflight", format="json", stdout=stdout)

        self.assertEqual(json.loads(stdout.getvalue())["state"], "migration-recorder-unavailable")
        self.assertIn("MIGRATION_RECORDER_UNAVAILABLE", str(raised.exception))
        self.assertNotIn("database failure", str(raised.exception))

    def test_command_rejects_interface_error_without_database_details(self):
        stdout = io.StringIO()
        with (
            patch("core.management.commands.migration_baseline_preflight.load_manifest", return_value=_manifest()),
            patch("core.management.commands.migration_baseline_preflight.MigrationRecorder") as recorder_cls,
        ):
            recorder_cls.return_value.has_table.side_effect = InterfaceError("connection to server at db failed")
            with self.assertRaises(CommandError) as raised:
                call_command("migration_baseline_preflight", format="json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state"], "migration-recorder-unavailable")
        self.assertIn("MIGRATION_RECORDER_UNAVAILABLE", str(raised.exception))
        self.assertNotIn("connection to server", stdout.getvalue())
        self.assertNotIn("connection to server", str(raised.exception))

    def test_command_rejects_manifest_validation_failures_with_structured_safe_json(self):
        for detail in ("synthetic unreadable manifest", "synthetic semantic manifest violation"):
            with self.subTest(detail=detail):
                stdout = io.StringIO()
                with patch(
                    "core.management.commands.migration_baseline_preflight.load_manifest",
                    side_effect=ValueError(detail),
                ):
                    with self.assertRaises(CommandError) as raised:
                        call_command("migration_baseline_preflight", format="json", stdout=stdout)

                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["state"], "migration-preflight-manifest-invalid")
                self.assertEqual(payload["reason_code"], "MIGRATION_PREFLIGHT_MANIFEST_INVALID")
                self.assertEqual(payload["exit_code"], 1)
                self.assertEqual(payload["counts"], {})
                self.assertIn("MIGRATION_PREFLIGHT_MANIFEST_INVALID", str(raised.exception))
                self.assertNotIn(detail, stdout.getvalue())

    def test_format_table_is_safe_and_contains_ids_and_counts(self):
        result = recorder_unavailable_result(missing_table=True)
        rendered = format_table(result)
        self.assertIn("migration-recorder-missing", rendered)
        self.assertIn("MIGRATION_RECORDER_TABLE_MISSING", rendered)
        self.assertNotIn("password", rendered.lower())

        missing = classify_applied_migrations({"assets.0001_initial"}, _manifest())
        unexpected = classify_applied_migrations({"assets.9999_unknown"}, _manifest())
        self.assertIn("missing_ids:", format_table(missing))
        self.assertIn("unexpected_ids:", format_table(unexpected))


@pytest.mark.serial_only
class PostgreSQLMigrationBaselineCommandTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        if connection.vendor != "postgresql":
            self.skipTest("migration-baseline integration requires PostgreSQL")

    def test_current_database_recognition_is_read_only(self):
        recorder = MigrationRecorder(connection)
        before = recorder.applied_migrations()
        applied_ids = {f"{app_label}.{migration_name}" for app_label, migration_name in before}
        manifest = load_manifest()
        self.assertTrue(set(manifest["historical_ids"]).issubset(applied_ids))
        self.assertTrue(set(manifest["replacement_ids"]).issubset(applied_ids))
        self.assertTrue(set(manifest["post_transition_ids"]).issubset(applied_ids))
        stdout = io.StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("migration_baseline_preflight", format="json", stdout=stdout)
        after = recorder.applied_migrations()

        self.assertEqual(before, after)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "current-normalized-baseline")
        statements = [query["sql"].lstrip().upper() for query in queries.captured_queries]
        self.assertTrue(statements)
        self.assertTrue(all(statement.startswith("SELECT") for statement in statements), statements)
