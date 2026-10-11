import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.migration_audit import (
    SEMANTIC_DISPOSITIONS,
    build_inventory,
    load_preflight_manifest,
    render_inventory,
    render_preflight_manifest,
    validate_preflight_manifest,
    validate_preflight_manifest_git_objects,
)


class MigrationAuditTests(unittest.TestCase):
    def test_inventory_classifies_graph_and_special_operations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_migration(
                root,
                "users",
                "0001_initial",
                """
                from django.conf import settings
                from django.db import migrations

                def seed(apps, schema_editor):
                    pass

                def undo(apps, schema_editor):
                    pass

                class Migration(migrations.Migration):
                    initial = True
                    dependencies = [
                        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
                    ]
                    operations = [
                        migrations.RunPython(seed, undo),
                        migrations.RunSQL("SELECT 1", reverse_sql=migrations.RunSQL.noop),
                    ]
                """,
            )
            self._write_migration(
                root,
                "beta",
                "0001_initial",
                """
                from django.db import migrations

                class Migration(migrations.Migration):
                    dependencies = [("users", "0001_initial")]
                    operations = [
                        migrations.SeparateDatabaseAndState(
                            database_operations=[],
                            state_operations=[],
                        ),
                        migrations.BtreeGistExtension(),
                    ]
                """,
            )
            self._write_migration(
                root,
                "users",
                "0010_user",
                """
                from django.db import migrations

                class Migration(migrations.Migration):
                    dependencies = []
                    run_before = [("users", "0001_initial")]
                    operations = []
                """,
            )

            inventory = build_inventory(
                root,
                semantic_dispositions={
                    "beta.0001_initial": {
                        "disposition": "safely-replaced-by-final-schema",
                        "rationale": "Synthetic state/database fixture.",
                    },
                    "users.0001_initial": {
                        "disposition": "required-fresh",
                        "rationale": "Synthetic seed fixture.",
                    },
                },
                expected_blockers=[],
            )

        self.assertEqual(inventory["summary"]["first_party_nodes"], 3)
        self.assertEqual(inventory["summary"]["first_party_edges"], 2)
        self.assertEqual(inventory["summary"]["global_roots"], ["users.0010_user"])
        self.assertEqual(inventory["summary"]["global_leaves"], ["beta.0001_initial"])
        self.assertEqual(
            inventory["special_users_bootstrap"]["migration"],
            "users.0010_user",
        )
        self.assertEqual(
            inventory["special_users_bootstrap"]["run_before"],
            ["users.0001_initial"],
        )
        self.assertEqual(
            len(inventory["special_users_bootstrap"]["swappable_dependents"]),
            1,
        )
        users = next(migration for migration in inventory["migrations"] if migration["id"] == "users.0001_initial")
        self.assertEqual(users["operations"]["RunPython"]["with_reverse"], 1)
        self.assertEqual(users["operations"]["RunSQL"]["with_noop_reverse"], 1)
        beta = next(migration for migration in inventory["migrations"] if migration["id"] == "beta.0001_initial")
        self.assertEqual(beta["operations"]["BtreeGistExtension"]["count"], 1)
        self.assertEqual(
            inventory["reviewed_semantics"]["required_fresh"],
            ["users.0001_initial"],
        )

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
            path = self._write_migration(root, "alpha", "0001_initial", source)
            before = path.read_text(encoding="utf-8")

            first = render_inventory(build_inventory(root, semantic_dispositions={}, expected_blockers=[]))
            second = render_inventory(build_inventory(root, semantic_dispositions={}, expected_blockers=[]))
            after = path.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertEqual(before, after)
        json.loads(first)

    def test_replacements_partition_historical_migrations_and_summarize_quotient(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_migration(
                root,
                "alpha",
                "0002_second",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = [("alpha", "0001_initial")]
                    operations = []
                """,
            )
            self._write_migration(
                root,
                "beta",
                "0001_initial",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = [("alpha", "0002_second")]
                    operations = []
                """,
            )
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial"), ("alpha", "0002_second")],
            )
            self._write_replacement(
                root,
                "beta",
                "0001_squashed",
                [("beta", "0001_initial")],
            )

            inventory = build_inventory(root, semantic_dispositions={}, expected_blockers=[])

        self.assertEqual(
            inventory["historical_graph"],
            {
                "edges": [
                    ["alpha.0001_initial", "alpha.0002_second"],
                    ["alpha.0002_second", "beta.0001_initial"],
                ],
                "leaves": ["beta.0001_initial"],
                "nodes": [
                    "alpha.0001_initial",
                    "alpha.0002_second",
                    "beta.0001_initial",
                ],
                "roots": ["alpha.0001_initial"],
            },
        )
        self.assertEqual(
            inventory["effective_graph"],
            {
                "edges": [["alpha.0001_squashed", "beta.0001_squashed"]],
                "leaves": ["beta.0001_squashed"],
                "nodes": ["alpha.0001_squashed", "beta.0001_squashed"],
                "roots": ["alpha.0001_squashed"],
            },
        )
        self.assertEqual(
            [
                (migration["id"], migration["is_replacement"], migration["replaces"])
                for migration in inventory["migrations"]
            ],
            [
                ("alpha.0001_initial", False, []),
                (
                    "alpha.0001_squashed",
                    True,
                    ["alpha.0001_initial", "alpha.0002_second"],
                ),
                ("alpha.0002_second", False, []),
                ("beta.0001_initial", False, []),
                ("beta.0001_squashed", True, ["beta.0001_initial"]),
            ],
        )

    def test_replacement_targets_must_exist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial"), ("alpha", "0099_missing")],
            )

            with self.assertRaisesRegex(ValueError, "unknown replacement targets"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_replacements_must_cover_every_historical_migration(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_plain_migration(root, "alpha", "0002_second")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial")],
            )

            with self.assertRaisesRegex(ValueError, r"replacement coverage incomplete.*alpha\.0002_second"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_empty_replaces_declaration_triggers_coverage_validation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_replacement(root, "alpha", "0001_squashed", [])

            with self.assertRaisesRegex(ValueError, r"replacement coverage incomplete.*alpha\.0001_initial"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_replacement_targets_must_not_be_duplicated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial"), ("alpha", "0001_initial")],
            )

            with self.assertRaisesRegex(ValueError, r"duplicate replacement targets.*alpha\.0001_initial"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_effective_replacement_quotient_must_be_acyclic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_migration(
                root,
                "beta",
                "0001_initial",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = [("alpha", "0001_initial")]
                    operations = []
                """,
            )
            self._write_migration(
                root,
                "beta",
                "0002_second",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = []
                    operations = []
                """,
            )
            self._write_migration(
                root,
                "alpha",
                "0002_second",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = [("beta", "0002_second")]
                    operations = []
                """,
            )
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial"), ("alpha", "0002_second")],
            )
            self._write_replacement(
                root,
                "beta",
                "0001_squashed",
                [("beta", "0001_initial"), ("beta", "0002_second")],
            )

            with self.assertRaisesRegex(ValueError, "effective replacement graph contains a cycle"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_effective_graph_includes_explicit_replacement_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_plain_migration(root, "beta", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial")],
            )
            self._write_replacement(
                root,
                "beta",
                "0001_squashed",
                [("beta", "0001_initial")],
                dependencies=[("alpha", "0001_squashed")],
            )

            inventory = build_inventory(root, semantic_dispositions={}, expected_blockers=[])

        self.assertEqual(
            inventory["effective_graph"]["edges"],
            [["alpha.0001_squashed", "beta.0001_squashed"]],
        )

    def test_effective_graph_includes_explicit_replacement_run_before_edges(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_plain_migration(root, "beta", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial")],
                run_before=[("beta", "0001_squashed")],
            )
            self._write_replacement(
                root,
                "beta",
                "0001_squashed",
                [("beta", "0001_initial")],
            )

            inventory = build_inventory(root, semantic_dispositions={}, expected_blockers=[])

        self.assertEqual(
            inventory["effective_graph"]["edges"],
            [["alpha.0001_squashed", "beta.0001_squashed"]],
        )

    def test_effective_graph_rejects_cycle_in_explicit_and_redirected_edge_union(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_migration(
                root,
                "beta",
                "0001_initial",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = [("alpha", "0001_initial")]
                    operations = []
                """,
            )
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial")],
                dependencies=[("beta", "0001_squashed")],
            )
            self._write_replacement(
                root,
                "beta",
                "0001_squashed",
                [("beta", "0001_initial")],
            )

            with self.assertRaisesRegex(ValueError, "effective replacement graph contains a cycle"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_unknown_first_party_explicit_replacement_dependency_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial")],
                dependencies=[("alpha", "0099_missing")],
            )

            with self.assertRaisesRegex(
                ValueError,
                r"unknown first-party replacement dependency targets.*alpha\.0099_missing",
            ):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_third_party_replacement_dependencies_are_outside_effective_graph(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0001_squashed",
                [("alpha", "0001_initial")],
                dependencies=[("contenttypes", "0002_remove_content_type_name")],
            )

            inventory = build_inventory(root, semantic_dispositions={}, expected_blockers=[])

        self.assertEqual(inventory["effective_graph"]["edges"], [])
        self.assertEqual(inventory["effective_graph"]["nodes"], ["alpha.0001_squashed"])

    def test_issue88_shard_requires_immediate_predecessor_dependency(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_plain_migration(root, "beta", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0000_issue88_shard_01_alpha",
                [("alpha", "0001_initial")],
            )
            self._write_replacement(
                root,
                "beta",
                "0100_issue88_shard_02_beta",
                [("beta", "0001_initial")],
            )

            with self.assertRaisesRegex(ValueError, "issue88 shard lacks immediate predecessor"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_issue88_shard_requires_previous_same_app_dependency(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_plain_migration(root, "alpha", "0002_second")
            self._write_plain_migration(root, "beta", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0000_issue88_shard_01_alpha",
                [("alpha", "0001_initial")],
            )
            self._write_replacement(
                root,
                "beta",
                "0100_issue88_shard_02_beta",
                [("beta", "0001_initial")],
                dependencies=[("alpha", "0000_issue88_shard_01_alpha")],
            )
            self._write_replacement(
                root,
                "alpha",
                "0100_issue88_shard_03_alpha",
                [("alpha", "0002_second")],
                dependencies=[("beta", "0100_issue88_shard_02_beta")],
            )

            with self.assertRaisesRegex(ValueError, "issue88 shard lacks previous same-app shard"):
                build_inventory(root, semantic_dispositions={}, expected_blockers=[])

    def test_post_transition_migration_requires_effective_leaf_dependency(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._write_plain_migration(root, "alpha", "0001_initial")
            self._write_replacement(
                root,
                "alpha",
                "0000_issue88_shard_01_alpha",
                [("alpha", "0001_initial")],
            )
            self._write_migration(
                root,
                "extras",
                "0101_issue88_drop_legacy_webhook_name_like",
                """
                from django.db import migrations
                class Migration(migrations.Migration):
                    dependencies = []
                    operations = [
                        migrations.RunSQL(
                            "DROP INDEX IF EXISTS legacy_index",
                            reverse_sql=migrations.RunSQL.noop,
                        ),
                    ]
                """,
            )
            dispositions = {
                "extras.0101_issue88_drop_legacy_webhook_name_like": {
                    "disposition": "upgrade-only",
                    "rationale": "Synthetic post-transition fixture.",
                },
            }

            with self.assertRaisesRegex(ValueError, "post-transition migration lacks effective leaf dependency"):
                build_inventory(
                    root,
                    semantic_dispositions=dispositions,
                    expected_blockers=[],
                )

    def test_repository_graph_and_reviewed_semantics_are_exact(self):
        repository_root = Path(__file__).resolve().parents[2]

        inventory = build_inventory(repository_root / "itambox")

        self.assertEqual(inventory["summary"]["first_party_nodes"], 0)
        self.assertEqual(inventory["summary"]["first_party_edges"], 0)
        self.assertEqual(inventory["summary"]["replacement_shards"], 62)
        self.assertEqual(inventory["summary"]["replacement_targets"], 262)
        self.assertEqual(inventory["summary"]["explicit_replacement_chain_edges"], 61)
        self.assertEqual(inventory["summary"]["post_transition_migrations"], 65)
        self.assertEqual(
            inventory["post_transition_migrations"],
            [
                "assets.0101_seed_canonical_missing_status",
                "assets.0102_asset_type_composition_schema",
                "assets.0103_asset_type_specification_conversion",
                "assets.0104_asset_type_core_vocabulary",
                "assets.0105_asset_type_provenance_capture",
                "assets.0106_asset_type_composition_cutover",
                "assets.0107_asset_type_specification_guards",
                "assets.0118_assetdisposal_cancellation_reason_and_more",
                "assets.0119_warranty_supplier",
                "assets.0120_repair_episode",
                "assets.0121_supplier_scoping_and_commercial_fields",
                "assets.0122_repair_maintenance_anchor",
                "assets.0123_asset_archive_leaf_markers",
                "assets.0124_tenant_archive_leaf_markers",
                "compliance.0101_alter_custodyreceipt_signed_at",
                "compliance.0102_clear_unsigned_receipt_timestamps",
                "compliance.0103_alter_custodyreceipt_options",
                "compliance.0104_custodysigningsession",
                "compliance.0105_custodyhandoffdelivery",
                "compliance.0106_remove_custodyreceipt_accepted",
                "compliance.0107_custodyreceipt_assignment_superseded",
                "extras.0101_issue88_drop_legacy_webhook_name_like",
                "extras.0102_alter_event_action",
                "extras.0103_remove_reporttemplate_advanced_mode_and_more",
                "extras.0104_issue183_alert_tenant_reconciliation",
                "extras.0105_reporttemplate_advanced_mode_and_more",
                "extras.0106_scheduledreportscopeauthorization",
                "extras.0107_scheduledreportscopeauthorization_revocation",
                "extras.0108_alertlog_delivery_outcome",
                "extras.0109_webhookdelivery",
                "extras.0110_issue445_task_paths",
                "extras.0111_webhookdelivery_target_claim",
                "extras.0112_backfill_webhookdelivery_targets",
                "extras.0113_upgrade_legacy_webhook_retry_schedules",
                "extras.0114_asset_type_definition_library_schema",
                "extras.0115_asset_type_definition_conversion",
                "extras.0116_asset_type_definition_cutover",
                "extras.0117_asset_type_definition_guards",
                "extras.0122_journalentry_tenant_group",
                "extras.0123_pause_flag_suppressed_report_schedules",
                "extras.0124_scheduled_report_fire_identity_delivery_outcomes",
                "extras.0125_scheduled_report_fire_records_retry_hardening",
                "extras.0126_scheduledreport_last_run_archive",
                "extras.0127_retire_report_designer_legacy",
                "extras.0128_tenant_archive_leaf_markers",
                "inventory.0101_alter_accessoryassignment_options_and_more",
                "inventory.0102_kititem_leaf_deleted_at",
                "inventory.0103_stock_leaf_deleted_at",
                "organization.0101_membership_external_id_and_more",
                "organization.0102_alter_tenantresourcegrant_options",
                "organization.0103_tenant_resource_grant_expiry",
                "organization.0104_tenant_archive_leaf_markers",
                "procurement.0101_alter_purchaseorder_options",
                "procurement.0102_fulfillmentlink_qty_received",
                "procurement.0103_remove_fulfillmentlink_unique_request_po_line_link_and_more",
                "software.0101_asset_archive_leaf_markers",
                "subscriptions.0101_remove_subscription_auto_renewal_and_more",
                "subscriptions.0102_commercial_vendor_and_terms",
                "subscriptions.0103_unified_vendor_cutover",
                "subscriptions.0104_subscriptionassignment_leaf_deleted_at",
                "subscriptions.0105_subscription_renewal_reminder_cycle_date_and_more",
                "users.0101_user_scim_id_usergroup_external_id_usergroup_scim_id_and_more",
                "users.0102_token_updated_at",
                "users.0103_oidcidentity",
                "users.0104_tenant_archive_leaf_markers",
            ],
        )
        self.assertEqual(inventory["summary"]["missing_replacement_targets"], 0)
        self.assertEqual(inventory["summary"]["duplicate_replacement_targets"], 0)
        self.assertTrue(inventory["summary"]["effective_replacement_quotient_acyclic"])
        self.assertEqual(
            inventory["summary"]["global_roots"],
            [],
        )
        self.assertEqual(
            inventory["summary"]["global_leaves"],
            [],
        )
        self.assertEqual(
            inventory["summary"]["custom_operation_file_counts"],
            {
                "RunPython": 0,
                "RunSQL": 0,
                "SeparateDatabaseAndState": 0,
                "BtreeGistExtension": 0,
            },
        )
        # The bootstrap migration itself is part of the replaced history now;
        # only its swappable dependents remain visible in the normalized source.
        self.assertIsNone(inventory["special_users_bootstrap"]["migration"])
        self.assertEqual(
            inventory["special_users_bootstrap"]["run_before"],
            [],
        )
        self.assertEqual(
            len(inventory["special_users_bootstrap"]["swappable_dependents"]),
            8,
        )
        self.assertEqual(
            inventory["reviewed_semantics"]["required_fresh"],
            [
                "assets.0100_issue88_shard_42_assets_relations",
                "assets.0100_issue88_shard_43_assets_seed",
                "assets.0101_seed_canonical_missing_status",
                "assets.0104_asset_type_core_vocabulary",
                "assets.0107_asset_type_specification_guards",
                "extras.0108_alertlog_delivery_outcome",
                "extras.0117_asset_type_definition_guards",
            ],
        )
        replacement_extension = next(
            migration
            for migration in inventory["migrations"]
            if migration["id"] == "assets.0100_issue88_shard_42_assets_relations"
        )
        self.assertEqual(
            replacement_extension["operations"]["BtreeGistExtension"]["count"],
            1,
        )
        report_template_retirement = next(
            migration
            for migration in inventory["migrations"]
            if migration["id"] == "extras.0127_retire_report_designer_legacy"
        )
        self.assertEqual(report_template_retirement["operations"]["RunPython"]["with_reverse"], 1)
        self.assertIn(
            "extras.0127_retire_report_designer_legacy",
            inventory["reviewed_semantics"]["upgrade_only"],
        )
        self.assertEqual(
            SEMANTIC_DISPOSITIONS["extras.0127_retire_report_designer_legacy"]["disposition"],
            "upgrade-only",
        )
        self.assertEqual(
            inventory["reviewed_semantics"]["blockers"],
            [
                "organization.0026_remove_tenantrole_tenant_provider_tenant_provider_and_more",
                "organization.0027_drop_legacy_role_models",
                "organization.0034_roleassignment_remove_provider_internal_tenant_and_more",
                "organization.0035_delete_provider_and_more",
                "users.0008_alter_usergroup_options_token_provider_and_more",
                "users.0010_remove_usergroup_users_usergroup_unique_provider_name_active_and_more",
            ],
        )
        self.assertEqual(
            SEMANTIC_DISPOSITIONS["assets.0103_asset_type_specification_conversion"]["disposition"],
            "upgrade-only",
        )
        self.assertEqual(
            SEMANTIC_DISPOSITIONS["assets.0104_asset_type_core_vocabulary"]["disposition"],
            "required-fresh",
        )
        self.assertEqual(
            inventory["reviewed_semantics"]["review_blocker"],
            [],
        )
        self.assertEqual(len(inventory["effective_graph"]["nodes"]), 62)
        self.assertEqual(
            inventory["effective_graph"]["roots"],
            ["users.0000_issue88_shard_01_users_bootstrap"],
        )
        self.assertEqual(
            inventory["effective_graph"]["leaves"],
            ["users.0100_issue88_shard_62_users_relations"],
        )

        custom_operation_ids = {
            migration["id"]
            for migration in inventory["migrations"]
            if any(migration["syntactic_facts"]["has_custom_operations"].values())
        }
        self.assertEqual(set(SEMANTIC_DISPOSITIONS), custom_operation_ids)
        self.assertFalse(set(SEMANTIC_DISPOSITIONS) - custom_operation_ids)
        self.assertEqual(
            {migration["id"] for migration in inventory["migrations"] if migration["reviewed_disposition"] is not None},
            custom_operation_ids,
        )

    @staticmethod
    def _write_migration(root, app, name, source):
        migration_directory = root / app / "migrations"
        migration_directory.mkdir(parents=True, exist_ok=True)
        path = migration_directory / f"{name}.py"
        normalized = textwrap.dedent(source).strip()
        path.write_text(normalized + "\n", encoding="utf-8")
        return path

    def _write_plain_migration(self, root, app, name):
        return self._write_migration(
            root,
            app,
            name,
            """
            from django.db import migrations
            class Migration(migrations.Migration):
                dependencies = []
                operations = []
            """,
        )

    def _write_replacement(self, root, app, name, replaces, dependencies=None, run_before=None):
        dependencies = [] if dependencies is None else dependencies
        run_before = [] if run_before is None else run_before
        return self._write_migration(
            root,
            app,
            name,
            f"""
            from django.db import migrations
            class Migration(migrations.Migration):
                replaces = {replaces!r}
                dependencies = {dependencies!r}
                run_before = {run_before!r}
                operations = []
            """,
        )


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
        manifest["recognized_states"][1]["recognition"]["post_transition_ids"] = ["assets.9999_unmanifested"]
        with self.assertRaisesRegex(ValueError, "recognition post-transition"):
            validate_preflight_manifest(self.inventory, manifest)

    def test_missing_replacement_id_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["replacement_ids"].pop()
        with self.assertRaisesRegex(ValueError, "replacement_ids"):
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

    def test_unexpected_first_party_baseline_mutation_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["baseline_ids"].append("assets.9999_unmanifested")
        with self.assertRaisesRegex(ValueError, "baseline_ids"):
            validate_preflight_manifest(self.inventory, manifest)


if __name__ == "__main__":
    unittest.main()
