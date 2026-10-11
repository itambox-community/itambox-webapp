"""Merge-queue support contract.

GitHub's merge queue tests every entry as a synthetic merge commit and only
reports checks that workflows run for the ``merge_group`` event. Every workflow
that carries a required status check must therefore declare the trigger, or
the queue can never advance an entry. The E2E scope selector must also accept
``merge_group`` as a known event (authoritative full scope for v1).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.select_e2e_scopes import AUTHORITATIVE_FULL_EVENTS, KNOWN_EVENTS

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REQUIRED_CHECK_WORKFLOWS = ("ci.yml", "security.yml", "e2e.yml")


class MergeQueueTriggerTests(unittest.TestCase):
    def test_required_check_workflows_run_on_merge_group(self):
        for name in REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding="utf-8")
                self.assertRegex(text, r"(?m)^  merge_group:$")

    def test_e2e_selector_accepts_merge_group_as_authoritative_full(self):
        self.assertIn("merge_group", KNOWN_EVENTS)
        self.assertIn("merge_group", AUTHORITATIVE_FULL_EVENTS)


if __name__ == "__main__":
    unittest.main()
