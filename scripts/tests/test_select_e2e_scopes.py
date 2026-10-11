"""Contract tests for the repository-owned fail-closed E2E selector."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.select_e2e_scopes import (
    ScopeMapError,
    SelectionError,
    _matches,
    build_e2e_identity,
    build_selection,
    canonical_json,
    collect_git_changes,
    parse_name_status_z,
    validate_scope_map,
    validate_selection,
)

BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40


def _map_document(root: Path) -> dict:
    scope_paths = {
        "smoke": "spec/smoke",
        "legacy-smoke": "spec/legacy-smoke",
        "app:assets": "spec/apps/assets",
        "contract:asset-custody": "spec/contracts/asset-custody",
        "layout": "spec/layout",
        "a11y": "spec/accessibility",
    }
    for relative in scope_paths.values():
        path = root / "itambox" / "tests" / "e2e" / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "contract.spec.ts").write_text(
            "import { test } from '@playwright/test';\ntest('real');\n", encoding="utf-8"
        )
    return {
        "schema": 1,
        "spec_root": "itambox/tests/e2e",
        "full_spec_path": "spec",
        "always_run_scopes": ["smoke", "legacy-smoke"],
        "full_scopes": ["all"],
        "scopes": {name: {"path": path, "kind": "workflow"} for name, path in scope_paths.items()},
        "rules": [
            {
                "id": "full-policy",
                "decision": "full",
                "patterns": ["scripts/**", ".github/workflows/**", "src/shared/**"],
            },
            {
                "id": "assets-source",
                "decision": "selected",
                "patterns": ["src/assets/**"],
                "scopes": ["app:assets", "contract:asset-custody"],
            },
            {
                "id": "assets-spec",
                "decision": "selected",
                "patterns": ["itambox/tests/e2e/spec/apps/assets/**"],
                "scopes": ["app:assets"],
            },
            {
                "id": "asset-contract-spec",
                "decision": "selected",
                "patterns": ["itambox/tests/e2e/spec/contracts/asset-custody/**"],
                "scopes": ["contract:asset-custody"],
            },
            {
                "id": "layout-spec",
                "decision": "selected",
                "patterns": ["itambox/tests/e2e/spec/layout/**"],
                "scopes": ["layout"],
            },
            {
                "id": "safe-maintainer-metadata",
                "decision": "safe_ignore",
                "patterns": [".github/ISSUE_TEMPLATE/**"],
            },
        ],
        "known_production_roots": ["src/**"],
        "rollback": {
            "force_full_pr_selection": False,
            "switch_path": "scripts/e2e_scope_map.yaml",
        },
    }


class NameStatusParsingTests(unittest.TestCase):
    def test_parses_add_modify_delete_rename_and_copy_from_nul_git_output(self):
        data = b"A\0src/new.py\0M\0src/changed.py\0D\0src/old.py\0R100\0src/a.py\0src/b.py\0C087\0src/c.py\0src/d.py\0"
        changes = parse_name_status_z(data)
        self.assertEqual(
            [(item.status, item.old_path, item.new_path) for item in changes],
            [
                ("A", None, "src/new.py"),
                ("M", None, "src/changed.py"),
                ("D", "src/old.py", None),
                ("R", "src/a.py", "src/b.py"),
                ("C", "src/c.py", "src/d.py"),
            ],
        )

    def test_rejects_malformed_status_and_path_identity(self):
        for data in (b"R100\0only-old\0", b"Z\0src/file.py\0", b"M\0../outside.py\0", b"M\0a\\b.py\0", b"M\0\0"):
            with self.subTest(data=data):
                with self.assertRaises(SelectionError):
                    parse_name_status_z(data)


class GitIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "--initial-branch=main"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "E2E Test"], cwd=self.root, check=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def commit(self, message):
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=self.root, check=True, capture_output=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()

    def test_one_file_pr_diff_cannot_inherit_stale_files_or_change_logical_merge_base(self):
        for relative, content in (
            ("itambox/compliance/tests/test_lifecycle_successor.py", "deadline = 10\\n"),
            ("itambox/organization/models.py", "unchanged organization source\\n"),
            ("scripts/coverage_baseline.json", "{}\\n"),
            ("scripts/resource_grant_test_manifest.json", "{}\\n"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        base = self.commit("base")
        target = self.root / "itambox/compliance/tests/test_lifecycle_successor.py"
        target.write_text("deadline = 30\\n", encoding="utf-8")
        head = self.commit("head")

        merge_base, changes = collect_git_changes(
            self.root,
            event_name="pull_request",
            base_sha=base,
            head_sha=head,
        )
        self.assertEqual(merge_base, base)
        self.assertEqual(
            [(change.status, change.new_path) for change in changes],
            [("M", "itambox/compliance/tests/test_lifecycle_successor.py")],
        )

    def test_raw_head_identity_records_synthetic_merge_without_using_it_for_diff(self):
        (self.root / "file.txt").write_text("base\\n", encoding="utf-8")
        base = self.commit("base")
        (self.root / "file.txt").write_text("head\\n", encoding="utf-8")
        head = self.commit("head")
        synthetic = "f" * 40

        identity = build_e2e_identity(
            self.root,
            event_name="pull_request",
            base_sha=base,
            head_sha=head,
            checkout_sha=head,
            synthetic_merge_sha=synthetic,
        )
        selection = build_selection(
            _map_document(self.root),
            self.root,
            event_name="pull_request",
            base_sha=base,
            head_sha=head,
            merge_base_sha=base,
            changes=[{"status": "M", "new_path": "file.txt"}],
        )

        for key in ("event_name", "base_sha", "head_sha", "merge_base_sha", "changed_path_digest"):
            self.assertEqual(identity[key], selection[key])
        self.assertEqual(identity["provenance_schema"], 2)
        self.assertEqual(identity["head_sha"], head)
        self.assertEqual(identity["merge_base_sha"], base)
        self.assertEqual(identity["tested_checkout_sha"], head)
        self.assertEqual(identity["tested_checkout_kind"], "head")
        self.assertEqual(identity["synthetic_merge_sha"], synthetic)

    def test_raw_head_identity_rejects_synthetic_merge_as_tested_checkout(self):
        (self.root / "file.txt").write_text("base\\n", encoding="utf-8")
        base = self.commit("base")
        (self.root / "file.txt").write_text("head\\n", encoding="utf-8")
        head = self.commit("head")
        synthetic = "f" * 40

        with self.assertRaises(SelectionError):
            build_e2e_identity(
                self.root,
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                checkout_sha=synthetic,
                synthetic_merge_sha=synthetic,
            )


class ScopeMapValidationTests(unittest.TestCase):
    def test_real_map_declares_every_required_scope_and_is_strict_json(self):
        repo = Path(__file__).resolve().parents[2]
        path = repo / "scripts" / "e2e_scope_map.yaml"
        document = json.loads(path.read_text(encoding="utf-8"))
        validate_scope_map(document, repo)
        required = {
            "app:organization",
            "app:assets",
            "app:inventory",
            "app:software",
            "app:licenses",
            "app:subscriptions",
            "app:procurement",
            "app:compliance",
            "app:extras",
            "app:users",
            "app:core",
            "app:itambox",
            "smoke",
            "legacy-smoke",
            "contract:generic-object",
            "contract:tenant-isolation",
            "contract:auth-rbac",
            "contract:soft-delete",
            "contract:asset-custody",
            "contract:cross-app",
            "contract:jobs",
            "layout",
            "a11y",
        }
        self.assertTrue(required.issubset(document["scopes"]))

    def test_aggregate_bulk_basket_regression_has_a_focused_scope(self):
        root = Path(__file__).resolve().parents[2]
        document = json.loads((root / "scripts" / "e2e_scope_map.yaml").read_text(encoding="utf-8"))
        selection = build_selection(
            document,
            root,
            event_name="pull_request",
            base_sha=BASE,
            head_sha=HEAD,
            merge_base_sha=MERGE,
            changes=[
                {
                    "status": "A",
                    "old_path": None,
                    "new_path": "itambox/tests/e2e/spec/regressions/aggregate-bulk-basket-switch-back.spec.ts",
                }
            ],
        )
        self.assertEqual(selection["mode"], "selected")
        self.assertEqual(
            selection["scopes"],
            ["legacy-smoke", "regression:aggregate-bulk-basket", "smoke"],
        )
        self.assertEqual(
            selection["spec_paths"],
            [
                "spec/legacy-smoke",
                "spec/regressions/aggregate-bulk-basket-switch-back.spec.ts",
                "spec/smoke",
            ],
        )

    def test_real_map_explicitly_classifies_every_tracked_known_production_file(self):
        root = Path(__file__).resolve().parents[2]
        document = json.loads((root / "scripts" / "e2e_scope_map.yaml").read_text(encoding="utf-8"))
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
        uncovered: list[str] = []
        for raw_path in tracked:
            if not raw_path:
                continue
            path = raw_path.decode("utf-8")
            if not any(_matches(path, pattern) for pattern in document["known_production_roots"]):
                continue
            if not any(_matches(path, pattern) for rule in document["rules"] for pattern in rule["patterns"]):
                uncovered.append(path)
        self.assertEqual(uncovered, [], "known production files must not rely on the unknown fallback")

    def test_real_map_covers_the_required_risk_matrix(self):
        root = Path(__file__).resolve().parents[2]
        document = json.loads((root / "scripts" / "e2e_scope_map.yaml").read_text(encoding="utf-8"))
        cases = (
            ("itambox/assets/models.py", "selected", {"app:assets", "contract:asset-custody"}),
            ("itambox/templates/assets/asset.html", "selected", {"app:assets"}),
            ("itambox/tests/e2e/spec/apps/assets/asset-lifecycle.spec.ts", "selected", {"app:assets"}),
            (
                "itambox/tests/e2e/spec/contracts/asset-custody/asset-action.spec.ts",
                "selected",
                {"contract:asset-custody"},
            ),
            ("itambox/assets/services/custody.py", "selected", {"contract:cross-app"}),
            ("itambox/organization/models.py", "full", {"all"}),
            ("itambox/organization/rbac.py", "full", {"all"}),
            ("itambox/organization/services/membership.py", "full", {"all"}),
            ("itambox/itambox/middleware.py", "full", {"all"}),
            ("itambox/users/api/authentication.py", "full", {"all"}),
            ("itambox/users/api/scim/provider_authentication.py", "full", {"all"}),
            ("itambox/users/backends.py", "full", {"all"}),
            ("itambox/users/models.py", "full", {"all"}),
            ("itambox/core/views/generic.py", "full", {"all"}),
            ("itambox/static/src/scss/base.scss", "full", {"all"}),
            ("itambox/static/src/ts/core.ts", "full", {"all"}),
            ("itambox/tests/e2e/fixtures/test.ts", "full", {"all"}),
            ("itambox/tests/e2e/helpers/errors.ts", "full", {"all"}),
            ("itambox/tests/e2e/playwright.config.ts", "full", {"all"}),
            ("itambox/tests/e2e/package-lock.json", "full", {"all"}),
            ("scripts/select_e2e_scopes.py", "full", {"all"}),
            ("scripts/certify_e2e_run.py", "full", {"all"}),
            ("scripts/check_e2e_gate.py", "full", {"all"}),
            ("scripts/e2e_scope_map.yaml", "full", {"all"}),
            (".github/workflows/e2e.yml", "full", {"all"}),
            ("itambox/assets/migrations/0001_initial.py", "full", {"all"}),
            ("itambox/core/management/commands/seed_data.py", "full", {"all"}),
            (".github/PULL_REQUEST_TEMPLATE.md", "none", set()),
            ("itambox/templates/new-domain/runtime.html", "full", {"all"}),
            ("unclassified-maintainer.txt", "full", {"all"}),
        )
        for path, mode, required_scopes in cases:
            with self.subTest(path=path):
                selection = build_selection(
                    document,
                    root,
                    event_name="pull_request",
                    base_sha=BASE,
                    head_sha=HEAD,
                    merge_base_sha=MERGE,
                    changes=[{"status": "M", "new_path": path}],
                )
                self.assertEqual(selection["mode"], mode)
                self.assertTrue(required_scopes.issubset(selection["scopes"]))
                if mode != "full" or path.startswith(("itambox/", "scripts/", ".github/")):
                    self.assertNotEqual(selection["reasons"][0]["matched_rule"], "unknown-path")

    def test_rejects_duplicate_rule_ids_and_safe_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _map_document(root)
            document["rules"].append(copy.deepcopy(document["rules"][-1]))
            with self.assertRaises(ScopeMapError):
                validate_scope_map(document, root, require_catalog_scopes=False)

            document = _map_document(root)
            document["rules"][0]["patterns"] = ["src/**"]
            document["rules"][-1]["patterns"] = ["src/**"]
            with self.assertRaises(ScopeMapError):
                validate_scope_map(document, root, require_catalog_scopes=False)

    def test_rejects_scope_path_outside_e2e_root_and_empty_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = _map_document(root)
            document["scopes"]["app:assets"]["path"] = "../outside"
            with self.assertRaises(ScopeMapError):
                validate_scope_map(document, root, require_catalog_scopes=False)

            document = _map_document(root)
            (root / "itambox/tests/e2e/spec/apps/assets/contract.spec.ts").unlink()
            with self.assertRaises(ScopeMapError):
                validate_scope_map(document, root, require_catalog_scopes=False)


class SelectionClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.document = _map_document(self.root)
        validate_scope_map(self.document, self.root, require_catalog_scopes=False)

    def tearDown(self):
        self.tempdir.cleanup()

    def select(self, records, event_name="pull_request", **kwargs):
        return build_selection(
            self.document,
            self.root,
            event_name=event_name,
            base_sha=kwargs.get("base_sha", BASE),
            head_sha=kwargs.get("head_sha", HEAD),
            merge_base_sha=kwargs.get("merge_base_sha", MERGE),
            changes=records,
        )

    def test_app_local_python_gets_smoke_legacy_owner_and_contract(self):
        selection = self.select([{"status": "M", "new_path": "src/assets/services.py"}])
        self.assertEqual(selection["mode"], "selected")
        self.assertEqual(
            selection["scopes"],
            ["app:assets", "contract:asset-custody", "legacy-smoke", "smoke"],
        )
        self.assertEqual(
            selection["spec_paths"],
            ["spec/apps/assets", "spec/contracts/asset-custody", "spec/legacy-smoke", "spec/smoke"],
        )

    def test_safe_only_is_none_but_safe_plus_app_is_selected(self):
        safe = {"status": "M", "new_path": ".github/ISSUE_TEMPLATE/bug.yml"}
        selection = self.select([safe])
        self.assertEqual(selection["mode"], "none")
        self.assertEqual(selection["scopes"], [])
        self.assertTrue(selection["reasons"][0]["safe_ignore"])

        mixed = self.select([safe, {"status": "M", "new_path": "src/assets/services.py"}])
        self.assertEqual(mixed["mode"], "selected")
        self.assertEqual(mixed["scopes"], ["app:assets", "contract:asset-custody", "legacy-smoke", "smoke"])

    def test_shared_unknown_and_authoritative_events_are_full(self):
        for record in (
            {"status": "M", "new_path": "src/shared/runtime.ts"},
            {"status": "M", "new_path": "src/unknown-production.py"},
            {"status": "M", "new_path": "unknown/nonproduction.txt"},
        ):
            with self.subTest(record=record):
                selection = self.select([record])
                self.assertEqual(selection["mode"], "full")
                self.assertEqual(selection["scopes"], ["all"])
                self.assertEqual(selection["spec_paths"], ["spec"])
        full = self.select([], event_name="push", base_sha=HEAD, head_sha=HEAD, merge_base_sha=HEAD)
        self.assertEqual(full["mode"], "full")
        self.assertEqual(full["changed_path_digest"].split(":", 1)[0], "sha256")
        merge_queue = self.select([], event_name="merge_group", base_sha=HEAD, head_sha=HEAD, merge_base_sha=HEAD)
        self.assertEqual(merge_queue["mode"], "full")
        self.assertEqual(merge_queue["scopes"], ["all"])

    def test_delete_and_rename_classify_old_and_new_identities(self):
        deleted = self.select([{"status": "D", "old_path": "src/assets/removed.py"}])
        self.assertEqual(deleted["mode"], "selected")
        self.assertIn("app:assets", deleted["scopes"])

        renamed = self.select([{"status": "R", "old_path": "src/assets/old.py", "new_path": "src/unknown/new.py"}])
        self.assertEqual(renamed["mode"], "full")
        identities = {reason["path"] for reason in renamed["reasons"]}
        self.assertEqual(identities, {"src/assets/old.py", "src/unknown/new.py"})

    def test_multi_copy_source_fanout_is_valid_and_deterministic(self):
        records = [
            {"status": "C", "old_path": "src/assets/source.py", "new_path": "src/assets/copy-a.py"},
            {"status": "C", "old_path": "src/assets/source.py", "new_path": "src/assets/copy-b.py"},
        ]
        with self.assertRaisesRegex(SelectionError, "duplicate status/path identities"):
            self.select(records)

    def test_input_order_permutation_is_byte_identical(self):
        records = [
            {"status": "M", "new_path": "src/assets/a.py"},
            {"status": "M", "new_path": ".github/ISSUE_TEMPLATE/x.yml"},
        ]
        first = canonical_json(self.select(records))
        second = canonical_json(self.select(list(reversed(records))))
        self.assertEqual(first, second)

    def test_empty_pr_diff_with_mismatched_identity_fails(self):
        with self.assertRaises(SelectionError):
            self.select([], base_sha=BASE, head_sha=HEAD, merge_base_sha=MERGE)

    def test_force_full_rollback_switch_is_policy_driven(self):
        self.document["rollback"]["force_full_pr_selection"] = True
        selection = self.select([{"status": "M", "new_path": "src/assets/services.py"}])
        self.assertEqual(selection["mode"], "full")
        self.assertEqual(selection["scopes"], ["all"])
        self.assertTrue(any(reason["matched_rule"] == "rollback-force-full" for reason in selection["reasons"]))


class SelectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.document = _map_document(self.root)
        validate_scope_map(self.document, self.root, require_catalog_scopes=False)
        self.valid = build_selection(
            self.document,
            self.root,
            event_name="pull_request",
            base_sha=BASE,
            head_sha=HEAD,
            merge_base_sha=MERGE,
            changes=[{"status": "M", "new_path": "src/assets/services.py"}],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_rejects_duplicate_or_unknown_scope_and_duplicate_spec_path(self):
        for mutate in (
            lambda value: value["scopes"].append("smoke"),
            lambda value: value["scopes"].append("unknown"),
            lambda value: value["spec_paths"].append(value["spec_paths"][0]),
        ):
            candidate = copy.deepcopy(self.valid)
            mutate(candidate)
            with self.subTest(candidate=candidate):
                with self.assertRaises(SelectionError):
                    validate_selection(candidate, self.root, self.document)

    def test_rejects_none_with_scopes_and_full_without_complete_root(self):
        candidate = copy.deepcopy(self.valid)
        candidate["mode"] = "none"
        candidate["scopes"] = ["smoke"]
        with self.assertRaises(SelectionError):
            validate_selection(candidate, self.root, self.document)

        candidate = copy.deepcopy(self.valid)
        candidate["mode"] = "full"
        candidate["scopes"] = ["all"]
        candidate["spec_paths"] = ["spec/apps/assets"]
        with self.assertRaises(SelectionError):
            validate_selection(candidate, self.root, self.document)

    def test_rejects_malformed_digest_identity_or_missing_selected_tests(self):
        for key, value in (("head_sha", "bad"), ("changed_path_digest", "sha256:bad"), ("event_name", "bad")):
            candidate = copy.deepcopy(self.valid)
            candidate[key] = value
            with self.subTest(key=key):
                with self.assertRaises(SelectionError):
                    validate_selection(candidate, self.root, self.document)

        candidate = copy.deepcopy(self.valid)
        candidate["spec_paths"] = ["spec/apps/assets/missing"]
        with self.assertRaises(SelectionError):
            validate_selection(candidate, self.root, self.document)


if __name__ == "__main__":
    unittest.main()
