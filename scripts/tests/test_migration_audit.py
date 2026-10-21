import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.migration_audit import (
    REVIEWED_SEMANTICS,
    build_inventory,
    load_preflight_manifest,
    render_inventory,
    render_preflight_manifest,
    validate_preflight_manifest,
    validate_preflight_manifest_git_objects,
)

PLAIN = """
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = {dependencies!r}
    replaces = {replaces!r}
    operations = []
"""


class MigrationAuditTests(unittest.TestCase):
    def test_inventory_classifies_graph_and_custom_operations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write(
                root,
                "users",
                "0001_baseline",
                """
                from django.db import migrations

                def seed(apps, schema_editor):
                    pass

                def undo(apps, schema_editor):
                    pass

                class Migration(migrations.Migration):
                    dependencies = []
                    operations = [
                        migrations.RunPython(seed, undo),
                        migrations.RunSQL("SELECT 1", reverse_sql=migrations.RunSQL.noop),
                    ]
                """,
            )
            self._write(
                root,
                "beta",
                "0001_baseline",
                """
                from django.conf import settings
                from django.db import migrations

                class Migration(migrations.Migration):
                    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
                    operations = [
                        migrations.SeparateDatabaseAndState(database_operations=[], state_operations=[]),
                        migrations.BtreeGistExtension(),
                    ]
                """,
            )

            inventory = build_inventory(
                root,
                reviewed_semantics={"users.0001_baseline": "seed", "beta.0001_baseline": "state split"},
            )

        by_id = {migration["id"]: migration for migration in inventory["migrations"]}
        users_operations = by_id["users.0001_baseline"]["operations"]
        self.assertEqual(users_operations["RunPython"]["with_reverse"], 1)
        self.assertEqual(users_operations["RunSQL"]["with_noop_reverse"], 1)
        beta_operations = by_id["beta.0001_baseline"]["operations"]
        self.assertEqual(beta_operations["SeparateDatabaseAndState"]["count"], 1)
        self.assertEqual(beta_operations["BtreeGistExtension"]["count"], 1)
        # The swappable user dependency orders every other app after the first users migration.
        self.assertIn(["users.0001_baseline", "beta.0001_baseline"], inventory["graph"]["edges"])
        self.assertEqual(inventory["graph"]["app_leaves"], ["beta.0001_baseline", "users.0001_baseline"])

    def test_render_is_deterministic_and_does_not_import_migrations(self):
        source = """
        raise RuntimeError("this migration must never be imported")
        from django.db import migrations
        class Migration(migrations.Migration):
            dependencies = []
            operations = []
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = self._write(root, "alpha", "0001_baseline", source)
            before = path.read_text(encoding="utf-8")

            first = render_inventory(build_inventory(root, reviewed_semantics={}))
            second = render_inventory(build_inventory(root, reviewed_semantics={}))
            after = path.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertEqual(before, after)
        json.loads(first)

    def test_custom_operations_require_a_reviewed_rationale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write(
                root,
                "alpha",
                "0001_baseline",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = []
                    operations = [migrations.RunSQL("SELECT 1")]
                """,
            )
            with self.assertRaisesRegex(ValueError, "without a reviewed rationale"):
                build_inventory(root, reviewed_semantics={})

    def test_a_rationale_for_a_plain_migration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline")
            with self.assertRaisesRegex(ValueError, "without custom operations"):
                build_inventory(root, reviewed_semantics={"alpha.0001_baseline": "stale"})

    def test_a_replaced_identity_must_not_still_exist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline")
            self._plain(
                root,
                "alpha",
                "0002_next",
                dependencies=[("alpha", "0001_baseline")],
                replaces=[("alpha", "0001_baseline")],
            )
            with self.assertRaisesRegex(ValueError, "still exists"):
                build_inventory(root, reviewed_semantics={})

    def test_an_identity_is_replaced_at_most_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline", replaces=[("alpha", "0001_legacy")])
            self._plain(
                root,
                "alpha",
                "0002_next",
                dependencies=[("alpha", "0001_baseline")],
                replaces=[("alpha", "0001_legacy")],
            )
            with self.assertRaisesRegex(ValueError, "replaced by both"):
                build_inventory(root, reviewed_semantics={})

    def test_a_migration_cannot_replace_another_apps_identity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline", replaces=[("beta", "0001_legacy")])
            with self.assertRaisesRegex(ValueError, "another app"):
                build_inventory(root, reviewed_semantics={})

    def test_a_missing_first_party_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline", dependencies=[("alpha", "0099_missing")])
            with self.assertRaisesRegex(ValueError, "missing first-party migration"):
                build_inventory(root, reviewed_semantics={})

    def test_a_dependency_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline", dependencies=[("beta", "0001_baseline")])
            self._plain(root, "beta", "0001_baseline", dependencies=[("alpha", "0001_baseline")])
            with self.assertRaisesRegex(ValueError, "cycle"):
                build_inventory(root, reviewed_semantics={})

    def test_an_app_with_two_leaves_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._plain(root, "alpha", "0001_baseline")
            self._plain(root, "alpha", "0002_left", dependencies=[("alpha", "0001_baseline")])
            self._plain(root, "alpha", "0002_right", dependencies=[("alpha", "0001_baseline")])
            with self.assertRaisesRegex(ValueError, "exactly one migration leaf"):
                build_inventory(root, reviewed_semantics={})

    def test_every_repository_custom_migration_has_a_reviewed_rationale(self):
        inventory = build_inventory(Path(__file__).resolve().parents[2] / "itambox")
        reviewed = {item["id"] for item in inventory["migrations"] if item["reviewed_semantics"]}
        self.assertEqual(reviewed, set(REVIEWED_SEMANTICS))
        self.assertEqual(inventory["summary"]["replaced_identities"], 262 + 62 + 73)

    def _plain(self, root, app, name, dependencies=(), replaces=()):
        return self._write(root, app, name, PLAIN.format(dependencies=list(dependencies), replaces=list(replaces)))

    def _write(self, root, app, name, source):
        directory = root / app / "migrations"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.py"
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        return path


class PreflightManifestAuditTests(unittest.TestCase):
    def setUp(self):
        self.repository_root = Path(__file__).resolve().parents[2]
        self.source_root = self.repository_root / "itambox"
        self.manifest_path = self.source_root / "core" / "migration_baseline_manifest.json"
        self.inventory = build_inventory(self.source_root)
        self.manifest = load_preflight_manifest(self.manifest_path)

    def test_checked_manifest_matches_the_current_normalized_inventory(self):
        validate_preflight_manifest(self.inventory, self.manifest)
        validate_preflight_manifest_git_objects(self.manifest, self.repository_root)

    def test_unresolvable_predecessor_revision_fails_closed(self):
        with patch("scripts.migration_audit.subprocess.run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, "not a local Git commit"):
                validate_preflight_manifest_git_objects(self.manifest, self.repository_root)
        run.assert_called_once()

    def test_render_preflight_manifest_refreshes_only_source_derived_fields(self):
        stale = json.loads(json.dumps(self.manifest))
        stale["post_transition_ids"] = []
        refreshed = json.loads(render_preflight_manifest(self.inventory, stale))
        self.assertEqual(refreshed["post_transition_ids"], self.manifest["post_transition_ids"])
        self.assertEqual(refreshed["transition_release_sha"], self.manifest["transition_release_sha"])
        self.assertEqual(refreshed["recognized_states"], self.manifest["recognized_states"])
        validate_preflight_manifest(self.inventory, refreshed)

    def test_unrecognized_predecessor_state_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["recognized_states"][0]["state"] = "not-a-real-state"
        with self.assertRaisesRegex(ValueError, "predecessor state"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_unrecognized_predecessor_recognition_state_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["recognized_states"][0]["recognition"]["state"] = "not-a-recognition-state"
        with self.assertRaisesRegex(ValueError, "recognition state"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_missing_predecessor_recognition_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["recognized_states"][0].pop("recognition")
        with self.assertRaisesRegex(ValueError, "recognition must be an object"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_predecessor_recognition_with_unmanifested_post_ids_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["recognized_states"][0]["recognition"]["post_transition_ids"] = ["assets.9999_unmanifested"]
        with self.assertRaisesRegex(ValueError, "recognition post-transition"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_missing_replacement_id_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["replacement_ids"].pop()
        with self.assertRaisesRegex(ValueError, "legacy identities|replacement_ids"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_missing_post_transition_leaf_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["post_transition_leaf_ids"].pop()
        with self.assertRaisesRegex(ValueError, "post_transition_leaf_ids"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_unexpected_transitional_layout_fails_closed_against_normalized_source(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["layout"] = "transitional"
        with self.assertRaisesRegex(ValueError, "layout"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_legacy_identity_drift_between_source_and_manifest_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["historical_ids"].remove(manifest["historical_ids"][0])
        manifest["replacement_target_ids"].remove(manifest["replacement_target_ids"][0])
        with self.assertRaisesRegex(ValueError, "legacy identities"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_checked_inventory_is_current(self):
        checked = (self.repository_root / "scripts" / "migration_audit.json").read_text(encoding="utf-8")
        self.assertEqual(render_inventory(self.inventory), checked)

    def test_unexpected_first_party_baseline_mutation_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["baseline_ids"].append("assets.9999_unmanifested")
        with self.assertRaisesRegex(ValueError, "baseline_ids"):
            validate_preflight_manifest(self.inventory, manifest)


if __name__ == "__main__":
    unittest.main()
