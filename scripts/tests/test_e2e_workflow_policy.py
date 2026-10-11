"""Text-level contract tests for the stable E2E workflow topology.

This suite intentionally uses only the standard library so it can run before
application dependencies are installed.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"


class E2EWorkflowTopologyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.release = RELEASE.read_text(encoding="utf-8")

    def test_has_detector_conditional_execution_and_always_running_gate(self):
        for job in ("detect-e2e-scope", "e2e-selected", "e2e-gate"):
            self.assertRegex(self.workflow, rf"(?m)^  {re.escape(job)}:")
        self.assertIn("name: E2E / Gate", self.workflow)
        self.assertRegex(self.workflow, r"(?ms)  e2e-gate:.*?if:\s*always\(\)")
        self.assertRegex(self.workflow, r"(?ms)  e2e-gate:.*?needs:.*detect-e2e-scope")
        self.assertRegex(self.workflow, r"(?ms)  e2e-gate:.*?e2e-selected")
        self.assertRegex(self.workflow, r"(?ms)  e2e-selected:.*?needs:\s*detect-e2e-scope")
        self.assertRegex(self.workflow, r"(?ms)  e2e-selected:.*?needs\.detect-e2e-scope\.result == 'success'")

    def test_authoritative_full_events_and_reusable_release_call_exist(self):
        self.assertIn("schedule:", self.workflow)
        self.assertIn("cron: '17 2 * * *'", self.workflow)
        self.assertIn("workflow_call:", self.workflow)
        self.assertIn("merge_group:", self.workflow)
        self.assertRegex(self.workflow, r"(?m)^\s+branches: \[master, main\]$")
        self.assertIn("authoritative full", self.workflow.lower())
        self.assertRegex(self.release, r"(?ms)^  e2e-qualification:.*?uses:\s*\./\.github/workflows/e2e\.yml")
        self.assertRegex(self.release, r"(?ms)^  prepare-release:.*?needs:.*e2e-qualification")

    def test_detector_is_full_history_and_public_self_contained(self):
        detector = self.workflow.split("  detect-e2e-scope:", 1)[1].split("\n  e2e-selected:", 1)[0]
        self.assertIn("fetch-depth: 0", detector)
        self.assertIn("scripts/select_e2e_scopes.py", detector)
        self.assertIn("scripts/e2e_scope_map.yaml", detector)
        self.assertIn("scripts/tests/test_", detector)
        self.assertIn("GITHUB_OUTPUT", detector)
        self.assertIn("GITHUB_STEP_SUMMARY", detector)
        self.assertIn("if-no-files-found: error", detector)
        self.assertNotIn("design-docs", self.workflow)
        self.assertNotIn("itambox/design-docs", self.workflow)

    def test_execution_uses_validated_wrapper_and_certification(self):
        execution = self.workflow.split("  e2e-selected:", 1)[1].split("\n  e2e-gate:", 1)[0]
        for required in (
            "run-selected.mjs",
            "certify_e2e_run.py",
            "PLAYWRIGHT_JSON_OUTPUT_NAME",
            "fetch-depth: 0",
            "seed_data --force",
            "::add-mask::",
            "npx playwright install --with-deps chromium",
            "E2E_NO_WEBSERVER",
            "E2E_SCIM_TOKEN",
            "redacted",
            "if-no-files-found: error",
        ):
            with self.subTest(required=required):
                self.assertIn(required, execution)
        self.assertRegex(execution, r"mock-oauth2-server:6\.0\.0@sha256:[0-9a-f]{64}(?:\s|$)")
        self.assertNotIn("npm test\n", execution)

    def test_pull_request_uses_raw_head_checkout_and_independent_identity_inputs(self):
        raw_head_ref = (
            "ref: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}"
        )
        for job in ("detect-e2e-scope", "e2e-selected", "e2e-gate"):
            with self.subTest(job=job):
                self.assertRegex(
                    self.workflow,
                    rf"(?ms)^  {re.escape(job)}:.*?{re.escape(raw_head_ref)}",
                )
        for required in ("E2E_EVENT_NAME", "E2E_PR_BASE_SHA", "E2E_PR_HEAD_SHA", "E2E_WORKFLOW_SHA"):
            with self.subTest(required=required):
                self.assertIn(required, self.workflow)
        self.assertIn("tested_checkout_sha", self.workflow)
        self.assertIn("synthetic_merge_sha", self.workflow)

    def test_gate_consumes_recorded_current_identity_instead_of_rebuilding_from_selection(self):
        gate = self.workflow.split("  e2e-gate:", 1)[1]
        self.assertIn("e2e-current-identity.json", gate)
        self.assertRegex(
            gate, r"(?ms)current_identity\s*=\s*load\(Path\(\"artifacts/run/e2e-current-identity.json\"\)\)"
        )
        self.assertIn("build_e2e_identity", gate)
        self.assertIn('["git", "rev-parse", "HEAD"]', gate)
        self.assertIn("E2E gate provenance mismatch for", gate)
        self.assertIn('"selection": execution.get("selection")', gate)
        self.assertNotIn('"selection": selection,\n                  "tested_checkout_sha": execution.get', gate)
        self.assertEqual(gate.count("current ="), 2)

    def test_gate_delegates_policy_to_stdlib_script_and_fails_closed(self):
        gate = self.workflow.split("  e2e-gate:", 1)[1]
        self.assertIn("scripts/check_e2e_gate.py", gate)
        self.assertIn("always()", gate)
        self.assertIn("detector_result", gate)
        self.assertIn("execution_result", gate)
        self.assertIn("selection_artifact", gate)
        self.assertIn("certification_artifact", gate)

    def test_no_workflow_level_pr_path_filter_can_omit_the_stable_gate(self):
        before_jobs = self.workflow.split("jobs:", 1)[0]
        self.assertNotIn("paths:", before_jobs)
        self.assertNotIn("paths-ignore:", before_jobs)

    def test_release_qualification_does_not_inherit_repository_secrets(self):
        self.assertNotRegex(self.release, r"(?m)^\s+secrets:\s+inherit\s*$")


if __name__ == "__main__":
    unittest.main()
