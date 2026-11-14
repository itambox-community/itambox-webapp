"""Contract tests for the SARIF merge that feeds the single code-scanning upload.

GitHub rejects a second analysis for the same tool/category pair in one run —
including several ``runs`` of the same tool inside one uploaded file. The merge
must therefore produce exactly one run, with a shared rule list and rewritten
rule/artifact indexes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.merge_sarif import main, merge_documents

TOOL = "ITAMbox security gate"


def _rule(identifier: str) -> dict:
    return {"id": identifier, "shortDescription": {"text": "Dependency vulnerability"}}


def _run(rules: list[dict], results: list[dict], artifacts: list[dict] | None = None, name: str = TOOL) -> dict:
    run = {"tool": {"driver": {"name": name, "rules": rules}}, "results": results}
    if artifacts is not None:
        run["artifacts"] = artifacts
    return run


def _result(rule_id: str, rule_index: int | None = None, artifact_index: int | None = None) -> dict:
    result: dict = {"ruleId": rule_id, "message": {"text": rule_id}}
    if rule_index is not None:
        result["ruleIndex"] = rule_index
    if artifact_index is not None:
        result["locations"] = [{"physicalLocation": {"artifactLocation": {"uri": "uv.lock", "index": artifact_index}}}]
    return result


def _write(path: Path, runs: list[dict]) -> Path:
    path.write_text(json.dumps({"version": "2.1.0", "runs": runs}), encoding="utf-8")
    return path


class MergeSarifTests(unittest.TestCase):
    def test_merges_both_platform_reports_into_exactly_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            amd64 = _write(root / "amd64.sarif", [_run([_rule("CVE-A")], [_result("CVE-A", 0)])])
            arm64 = _write(root / "arm64.sarif", [_run([_rule("CVE-B")], [_result("CVE-B", 0)])])

            merged = merge_documents([amd64, arm64])

        self.assertEqual(len(merged["runs"]), 1)
        run = merged["runs"][0]
        self.assertEqual([rule["id"] for rule in run["tool"]["driver"]["rules"]], ["CVE-A", "CVE-B"])
        self.assertEqual([result["ruleId"] for result in run["results"]], ["CVE-A", "CVE-B"])
        self.assertEqual([result["ruleIndex"] for result in run["results"]], [0, 1])

    def test_shared_rules_are_not_duplicated_and_indexes_are_remapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write(root / "one.sarif", [_run([_rule("A"), _rule("B")], [_result("B", 1)])])
            second = _write(
                root / "two.sarif",
                [_run([_rule("B"), _rule("C")], [_result("B", 0), _result("C", 1)])],
            )

            merged = merge_documents([first, second])

        run = merged["runs"][0]
        self.assertEqual([rule["id"] for rule in run["tool"]["driver"]["rules"]], ["A", "B", "C"])
        self.assertEqual([result["ruleIndex"] for result in run["results"]], [1, 1, 2])

    def test_artifact_indexes_are_remapped_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write(
                root / "one.sarif",
                [_run([_rule("A")], [_result("A", 0, 0)], [{"location": {"uri": "uv.lock"}}])],
            )
            second = _write(
                root / "two.sarif",
                [_run([_rule("A")], [_result("A", 0, 0)], [{"location": {"uri": "itambox/package-lock.json"}}])],
            )

            merged = merge_documents([first, second])

        run = merged["runs"][0]
        self.assertEqual(
            [artifact["location"]["uri"] for artifact in run["artifacts"]],
            ["uv.lock", "itambox/package-lock.json"],
        )
        indexes = [result["locations"][0]["physicalLocation"]["artifactLocation"]["index"] for result in run["results"]]
        self.assertEqual(indexes, [0, 1])

    def test_reports_from_different_tools_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write(root / "one.sarif", [_run([_rule("A")], [_result("A")])])
            second = _write(root / "two.sarif", [_run([_rule("B")], [_result("B")], name="Another tool")])

            with self.assertRaises(ValueError):
                merge_documents([first, second])

    def test_missing_or_empty_reports_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            only = _write(root / "only.sarif", [_run([_rule("A")], [_result("A")])])
            empty = root / "empty.sarif"
            empty.write_text("", encoding="utf-8")

            merged = merge_documents([only, empty, root / "absent.sarif"])

        self.assertEqual(len(merged["runs"]), 1)
        self.assertEqual(len(merged["runs"][0]["results"]), 1)

    def test_invalid_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.sarif"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                merge_documents([broken])

    def test_main_writes_one_run_and_reports_the_result_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write(root / "one.sarif", [_run([_rule("A")], [_result("A")])])
            second = _write(root / "two.sarif", [_run([_rule("B")], [_result("B")])])
            output = root / "combined.sarif"

            self.assertEqual(main(["--output", str(output), str(first), str(second)]), 0)

            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(len(payload["runs"][0]["results"]), 2)

    def test_main_writes_nothing_when_no_report_has_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "combined.sarif"

            self.assertEqual(main(["--output", str(output), str(root / "absent.sarif")]), 0)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
