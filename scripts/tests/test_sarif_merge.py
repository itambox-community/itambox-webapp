"""Contract tests for the SARIF merge that feeds the single code-scanning upload."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.merge_sarif import main, merge_documents


def _write(path: Path, runs: list[dict]) -> Path:
    path.write_text(json.dumps({"version": "2.1.0", "runs": runs}), encoding="utf-8")
    return path


class MergeSarifTests(unittest.TestCase):
    def test_combines_both_platform_runs_into_one_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            amd64 = _write(
                root / "amd64.sarif", [{"tool": {"driver": {"name": "Trivy"}}, "results": [{"ruleId": "A"}]}]
            )
            arm64 = _write(
                root / "arm64.sarif", [{"tool": {"driver": {"name": "Trivy"}}, "results": [{"ruleId": "B"}]}]
            )

            merged = merge_documents([amd64, arm64])

        self.assertEqual(len(merged["runs"]), 2)
        self.assertEqual([run["results"][0]["ruleId"] for run in merged["runs"]], ["A", "B"])
        self.assertEqual(merged["version"], "2.1.0")

    def test_missing_or_empty_reports_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            only = _write(root / "only.sarif", [{"results": [{"ruleId": "A"}]}])
            empty = root / "empty.sarif"
            empty.write_text("", encoding="utf-8")

            merged = merge_documents([only, empty, root / "absent.sarif"])

        self.assertEqual(len(merged["runs"]), 1)

    def test_invalid_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.sarif"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                merge_documents([broken])

    def test_main_writes_a_single_document_and_reports_run_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write(root / "one.sarif", [{"results": []}])
            second = _write(root / "two.sarif", [{"results": []}])
            output = root / "combined.sarif"

            self.assertEqual(main(["--output", str(output), str(first), str(second)]), 0)

            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["runs"]), 2)

    def test_main_writes_nothing_when_no_report_has_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "combined.sarif"

            self.assertEqual(main(["--output", str(output), str(root / "absent.sarif")]), 0)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
