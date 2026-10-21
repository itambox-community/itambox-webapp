import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

from scripts.security_gate import (
    SecurityGateError,
    evaluate_gitleaks,
    evaluate_trivy,
    load_suppressions,
    main,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def suppression(**overrides):
    today = date.today()
    value = {
        "id": "SEC-001",
        "tool": "trivy",
        "finding": "CVE-2026-0001",
        "reason": "Upstream fix is not available; affected code is unreachable.",
        "owner": "@itambox/security",
        "scope": {
            "target": "uv.lock",
            "package": "example",
            "version": "1.0.0",
        },
        "review_on": (today + timedelta(days=14)).isoformat(),
        "expires_on": (today + timedelta(days=30)).isoformat(),
        "references": ["https://github.com/itambox-community/itambox-webapp/issues/20"],
    }
    value.update(overrides)
    return value


class SuppressionPolicyTests(unittest.TestCase):
    def write_manifest(self, root, entries):
        path = Path(root) / "suppressions.json"
        path.write_text(json.dumps({"version": 1, "suppressions": entries}), encoding="utf-8")
        return path

    def test_empty_manifest_is_valid(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(load_suppressions(self.write_manifest(root, [])), [])

    def test_required_governance_fields_are_enforced(self):
        for field in ("reason", "owner", "scope", "review_on", "expires_on"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                entry = suppression()
                del entry[field]
                with self.assertRaisesRegex(SecurityGateError, field):
                    load_suppressions(self.write_manifest(root, [entry]))

    def test_expired_overlong_and_duplicate_suppressions_are_rejected(self):
        today = date.today()
        cases = [
            (
                [
                    suppression(
                        review_on=(today - timedelta(days=2)).isoformat(),
                        expires_on=(today - timedelta(days=1)).isoformat(),
                    )
                ],
                "expired",
            ),
            ([suppression(expires_on=(today + timedelta(days=91)).isoformat())], "90 days"),
            ([suppression(review_on=(today - timedelta(days=1)).isoformat())], "review is overdue"),
            ([suppression(), suppression()], "duplicate"),
        ]
        for entries, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(SecurityGateError, message):
                    load_suppressions(self.write_manifest(root, entries))

    def test_gitleaks_scope_rejects_wildcards_and_invalid_lines(self):
        cases = {
            "wildcard path": ({"path": "src/*.py", "rule": "rule", "line": 42}, "broad gitleaks scope"),
            "wildcard rule": ({"path": "src/app.py", "rule": "rule*", "line": 42}, "broad gitleaks scope"),
            "zero line": ({"path": "src/app.py", "rule": "rule", "line": 0}, "positive integer"),
            "string line": ({"path": "src/app.py", "rule": "rule", "line": "42"}, "positive integer"),
            "boolean line": ({"path": "src/app.py", "rule": "rule", "line": True}, "positive integer"),
            "missing line": ({"path": "src/app.py", "rule": "rule"}, "exactly"),
            "extra field": ({"path": "src/app.py", "rule": "rule", "line": 42, "fingerprint": "x"}, "exactly"),
            "empty path": ({"path": "", "rule": "rule", "line": 42}, "non-empty"),
            "integer path": ({"path": 7, "rule": "rule", "line": 42}, "non-empty"),
        }
        for label, (scope, message) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                entry = suppression(tool="gitleaks", finding="rule", scope=scope)
                with self.assertRaisesRegex(SecurityGateError, message):
                    load_suppressions(self.write_manifest(root, [entry]))


class FindingPolicyTests(unittest.TestCase):
    def test_trivy_blocks_unsuppressed_high_and_emits_sarif(self):
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "uv.lock",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0001",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            sarif = Path(root) / "results.sarif"
            result = evaluate_trivy([report], [], sarif)
            self.assertFalse(result.passed)
            self.assertEqual(result.blocking, 1)
            self.assertEqual(json.loads(sarif.read_text())["version"], "2.1.0")

    def test_trivy_sarif_uses_relative_uri_for_image_targets(self):
        target = "itambox:release-rehearsal (debian 12.15)"
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": target,
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0001",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            sarif = Path(root) / "results.sarif"
            evaluate_trivy([report], [], sarif)
            result = json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"][0]

        self.assertEqual(
            result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
            "trivy-targets/itambox%3Arelease-rehearsal%20%28debian%2012.15%29",
        )
        self.assertEqual(result["properties"]["target"], target)

    def test_exact_trivy_suppression_and_low_findings_do_not_block(self):
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "uv.lock",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0001",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "Severity": "HIGH",
                        },
                        {
                            "VulnerabilityID": "CVE-2026-0002",
                            "PkgName": "other",
                            "InstalledVersion": "2.0.0",
                            "Severity": "LOW",
                        },
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "suppressions.json"
            manifest.write_text(json.dumps({"version": 1, "suppressions": [suppression()]}), encoding="utf-8")
            result = evaluate_trivy([report], load_suppressions(manifest), Path(root) / "results.sarif")
            self.assertTrue(result.passed)
            self.assertEqual(result.suppressed, 1)

    def test_trivy_fail_on_any_blocks_every_unsuppressed_severity(self):
        severities = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "uv.lock",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": f"CVE-2026-{index:04d}",
                            "PkgName": f"example{index}",
                            "InstalledVersion": "1.0.0",
                            "Severity": severity,
                        }
                        for index, severity in enumerate(severities, start=1)
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            result = evaluate_trivy([report], [], Path(root) / "results.sarif", fail_on="any")
            self.assertFalse(result.passed)
            self.assertEqual(result.blocking, len(severities))

    def test_trivy_fail_on_medium_keeps_low_and_unknown_informational(self):
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "uv.lock",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0001",
                            "PkgName": "low",
                            "InstalledVersion": "1.0.0",
                            "Severity": "LOW",
                        },
                        {
                            "VulnerabilityID": "CVE-2026-0002",
                            "PkgName": "unknown",
                            "InstalledVersion": "1.0.0",
                            "Severity": "UNKNOWN",
                        },
                        {
                            "VulnerabilityID": "CVE-2026-0003",
                            "PkgName": "medium",
                            "InstalledVersion": "1.0.0",
                            "Severity": "MEDIUM",
                        },
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            sarif = Path(root) / "results.sarif"
            result = evaluate_trivy([report], [], sarif, fail_on="medium")
            self.assertFalse(result.passed)
            self.assertEqual(result.blocking, 1)
            # Informational severities stay retained in the SARIF upload.
            self.assertEqual(len(json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"]), 3)

    def test_trivy_default_policy_blocks_medium_findings(self):
        # Issue 568: the gate default is the canonical dependency-lockfile policy,
        # so an invocation that forgets --fail-on cannot silently fall back to the
        # old HIGH-only behavior.
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "itambox/package-lock.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-85024",
                            "PkgName": "undici",
                            "InstalledVersion": "6.28.0",
                            "Severity": "MEDIUM",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            result = evaluate_trivy([report], [], Path(root) / "results.sarif")
        self.assertFalse(result.passed)
        self.assertEqual(result.blocking, 1)

    def test_trivy_sarif_binds_declared_lockfile_targets_to_tracked_paths(self):
        # Issue 568: findings on the canonical lockfiles must resolve to the real
        # tracked file instead of a synthetic scanner-side directory prefix.
        targets = ("uv.lock", "itambox/package-lock.json", "itambox/tests/e2e/package-lock.json")
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": target,
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": f"CVE-2026-{index:04d}",
                            "PkgName": f"example{index}",
                            "InstalledVersion": "1.0.0",
                            "Severity": "MEDIUM",
                        }
                    ],
                }
                for index, target in enumerate(targets, start=1)
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            sarif = Path(root) / "results.sarif"
            evaluate_trivy([report], [], sarif, expected_targets=set(targets))
            results = json.loads(sarif.read_text(encoding="utf-8"))["runs"][0]["results"]

        self.assertEqual(
            [item["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for item in results],
            list(targets),
        )
        # Rule, package, version, severity and the original target stay attached.
        for index, (item, target) in enumerate(zip(results, targets, strict=True), start=1):
            self.assertEqual(item["ruleId"], f"CVE-2026-{index:04d}")
            self.assertEqual(item["properties"]["target"], target)
            self.assertEqual(item["message"]["text"], f"example{index} 1.0.0 (MEDIUM)")

    def test_trivy_medium_finding_follows_governed_suppression_lifecycle(self):
        # Issue 568: a reviewed suppression can defer a MEDIUM finding, but the
        # governance dates keep applying to it like to every other suppression.
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "itambox/package-lock.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-85024",
                            "PkgName": "undici",
                            "InstalledVersion": "6.28.0",
                            "Severity": "MEDIUM",
                        }
                    ],
                }
            ],
        }
        scope = {"target": "itambox/package-lock.json", "package": "undici", "version": "6.28.0"}
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "suppressions.json"
            manifest.write_text(
                json.dumps({"version": 1, "suppressions": [suppression(finding="CVE-2026-85024", scope=scope)]}),
                encoding="utf-8",
            )
            result = evaluate_trivy([report], load_suppressions(manifest), Path(root) / "results.sarif")
            self.assertTrue(result.passed)
            self.assertEqual(result.suppressed, 1)

        expired = suppression(
            finding="CVE-2026-85024",
            scope=scope,
            review_on=(date.today() - timedelta(days=2)).isoformat(),
            expires_on=(date.today() - timedelta(days=1)).isoformat(),
        )
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "suppressions.json"
            manifest.write_text(json.dumps({"version": 1, "suppressions": [expired]}), encoding="utf-8")
            with self.assertRaisesRegex(SecurityGateError, "expired"):
                load_suppressions(manifest)

    def test_trivy_fail_on_any_respects_governed_suppressions(self):
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "uv.lock",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0001",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "suppressions.json"
            manifest.write_text(json.dumps({"version": 1, "suppressions": [suppression()]}), encoding="utf-8")
            result = evaluate_trivy([report], load_suppressions(manifest), Path(root) / "results.sarif", fail_on="any")
            self.assertTrue(result.passed)
            self.assertEqual(result.suppressed, 1)

    def test_trivy_rejects_unknown_fail_on_policy(self):
        report = {"SchemaVersion": 2, "Results": [{"Target": "uv.lock", "Vulnerabilities": []}]}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(SecurityGateError, "fail-on"):
                evaluate_trivy([report], [], Path(root) / "results.sarif", fail_on="everything")

    def test_trivy_rejects_malformed_reports_and_missing_expected_targets(self):
        with tempfile.TemporaryDirectory() as root:
            sarif = Path(root) / "results.sarif"
            with self.assertRaisesRegex(SecurityGateError, "invalid Trivy report"):
                evaluate_trivy([{}], [], sarif)
            report = {
                "SchemaVersion": 2,
                "Results": [{"Target": "uv.lock", "Vulnerabilities": None}],
            }
            with self.assertRaisesRegex(SecurityGateError, "missing expected Trivy targets"):
                evaluate_trivy(
                    [report],
                    [],
                    sarif,
                    expected_targets={"uv.lock", "itambox/package-lock.json"},
                )
            report["Results"][0]["Vulnerabilities"] = [
                {
                    "VulnerabilityID": "CVE-2026-0001",
                    "PkgName": "example",
                    "InstalledVersion": "1.0.0",
                    "Severity": "UNRECOGNIZED",
                }
            ]
            with self.assertRaisesRegex(SecurityGateError, "invalid Trivy severity"):
                evaluate_trivy([report], [], sarif)
            report["Results"] = [
                {"Target": "uv.lock", "Vulnerabilities": None},
                {"Target": "unexpected.lock", "Vulnerabilities": None},
            ]
            with self.assertRaisesRegex(SecurityGateError, "unexpected Trivy targets"):
                evaluate_trivy([report], [], sarif, expected_targets={"uv.lock"})

    def test_gitleaks_requires_exact_path_rule_and_line_suppression(self):
        finding = {
            "RuleID": "rule",
            "File": "src/app.py",
            "StartLine": 42,
            "Fingerprint": "abc:src/app.py:rule:42",
        }
        self.assertFalse(evaluate_gitleaks([finding], []).passed)
        exact = suppression(
            tool="gitleaks",
            finding="rule",
            scope={"path": "src/app.py", "rule": "rule", "line": 42},
        )
        near_misses = {
            "line": {**exact, "scope": {"path": "src/app.py", "rule": "rule", "line": 43}},
            "path": {**exact, "scope": {"path": "src/other.py", "rule": "rule", "line": 42}},
            "rule": {
                **exact,
                "finding": "other-rule",
                "scope": {"path": "src/app.py", "rule": "other-rule", "line": 42},
            },
        }
        for label, entry in near_misses.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                manifest = Path(root) / "suppressions.json"
                manifest.write_text(json.dumps({"version": 1, "suppressions": [entry]}), encoding="utf-8")
                self.assertFalse(evaluate_gitleaks([finding], load_suppressions(manifest)).passed)
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "suppressions.json"
            manifest.write_text(json.dumps({"version": 1, "suppressions": [exact]}), encoding="utf-8")
            self.assertTrue(evaluate_gitleaks([finding], load_suppressions(manifest)).passed)

    def test_gitleaks_suppression_survives_the_introducing_commit(self):
        # Regression for issue 510: a squash, rebase, or cherry-pick integration
        # re-attributes the finding to a new commit. The reviewed identity is the
        # file/rule/line tuple, so the suppression must keep matching.
        entry = suppression(
            tool="gitleaks",
            finding="generic-api-key",
            scope={"path": "scripts/fixtures/vocabulary.json", "rule": "generic-api-key", "line": 42},
        )
        before = {
            "RuleID": "generic-api-key",
            "File": "scripts/fixtures/vocabulary.json",
            "StartLine": 42,
            "Commit": "oldcommit0",
            "Fingerprint": "oldcommit0:scripts/fixtures/vocabulary.json:generic-api-key:42",
        }
        after = {
            **before,
            "Commit": "newcommit1",
            "Fingerprint": "newcommit1:scripts/fixtures/vocabulary.json:generic-api-key:42",
        }
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "suppressions.json"
            manifest.write_text(json.dumps({"version": 1, "suppressions": [entry]}), encoding="utf-8")
            suppressions = load_suppressions(manifest)
            for finding in (before, after):
                result = evaluate_gitleaks([finding], suppressions)
                self.assertTrue(result.passed)
                self.assertEqual(result.suppressed, 1)


class SecurityGateCommandTests(unittest.TestCase):
    def _run_trivy(self, root, report, *extra):
        report_path = Path(root) / "report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        manifest = Path(root) / "suppressions.json"
        manifest.write_text(json.dumps({"version": 1, "suppressions": []}), encoding="utf-8")
        return main(
            [
                "--manifest",
                str(manifest),
                "trivy",
                "--report",
                str(report_path),
                "--sarif",
                str(Path(root) / "results.sarif"),
                *extra,
            ]
        )

    def test_trivy_cli_applies_the_fail_on_policy(self):
        report = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "uv.lock",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-0001",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "Severity": "LOW",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as root:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self._run_trivy(root, report, "--fail-on", "any"), 1)
            self.assertIn("blocking=1", output.getvalue())

            with redirect_stdout(io.StringIO()):
                self.assertEqual(self._run_trivy(root, report), 0)

    def test_dependency_gate_blocks_unsuppressed_medium_and_passes_clean_scans(self):
        # Issue 568 regression: an unsuppressed MEDIUM finding in a canonical
        # lockfile must produce a nonzero dependency-gate result, while the same
        # inputs with no findings must pass.
        targets = ("uv.lock", "itambox/package-lock.json", "itambox/tests/e2e/package-lock.json")
        medium = {
            "SchemaVersion": 2,
            "Results": [
                {"Target": "uv.lock", "Vulnerabilities": []},
                {
                    "Target": "itambox/package-lock.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-85024",
                            "PkgName": "undici",
                            "InstalledVersion": "6.28.0",
                            "Severity": "MEDIUM",
                        }
                    ],
                },
                {"Target": "itambox/tests/e2e/package-lock.json", "Vulnerabilities": []},
            ],
        }
        clean = {"SchemaVersion": 2, "Results": [{"Target": target, "Vulnerabilities": []} for target in targets]}
        expect = [argument for target in targets for argument in ("--expect-target", target)]
        with tempfile.TemporaryDirectory() as root:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self._run_trivy(root, medium, "--fail-on", "medium", *expect), 1)
            self.assertIn("blocking=1 suppressed=0 MEDIUM=1", output.getvalue())

            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self._run_trivy(root, clean, "--fail-on", "medium", *expect), 0)
            self.assertIn("blocking=0", output.getvalue())

            # The gate default applies the same policy when the flag is omitted.
            with redirect_stdout(io.StringIO()):
                self.assertEqual(self._run_trivy(root, medium, *expect), 1)


class SecurityAutomationContractTests(unittest.TestCase):
    def test_security_workflow_covers_canonical_inputs_without_leaking_reports(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
        for source in ("uv.lock", "itambox/package-lock.json", "itambox/tests/e2e/package-lock.json"):
            self.assertIn(source, workflow)
        self.assertIn("pull_request:", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("--redact", workflow)
        self.assertIn("$RUNNER_TEMP/gitleaks.json", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("cat $RUNNER_TEMP", workflow)
        self.assertNotIn("tee ", workflow)
        self.assertIn("security-events: write", workflow)
        self.assertIn("github.actor != 'dependabot[bot]'", workflow)
        self.assertIn("uv lock --check", workflow)
        self.assertIn("--include-dev-deps", workflow)
        self.assertEqual(workflow.count("--expect-target"), 3)
        self.assertIn("--fail-on medium", workflow)
        self.assertEqual(workflow.count("--fail-on medium"), 1)
        self.assertIn("merge_group:", workflow)
        self.assertIn("github.event.merge_group.base_sha", workflow)
        self.assertIn("github.event.merge_group.head_sha", workflow)
        range_selector = workflow.split("Select reviewed commit range", 1)[1].split(
            "Scan commits without exposing findings", 1
        )[0]
        # The reviewed range must never fall back to the full history on the events that can
        # reach protected main: pull requests use the PR range, merge-queue groups use the
        # merge-group range. A full-history scan (--all) surfaces historical, unsuppressed
        # findings and ejects the queue entry with "failed_checks".
        self.assertIn("${MG_BASE}..${MG_HEAD}", range_selector)
        self.assertLess(range_selector.index("merge_group"), range_selector.index('value="--all"'))

    def test_release_rehearsal_scans_the_same_image_before_draft_creation(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(workflow.count("--fail-on any"), 2)
        self.assertGreaterEqual(workflow.count("--ignore-unfixed"), 2)
        for image in ("itambox:release-rehearsal", "itambox:${RELEASE_VERSION}"):
            build = workflow.index(image)
            scan = workflow.index('security-tools/trivy" image', build)
            self.assertGreater(workflow.index(image, scan), scan)
        self.assertLess(
            workflow.index('security-tools/trivy" image', workflow.index("prepare-release:")),
            workflow.index("gh release create"),
        )
        self.assertIn("security-events: write", workflow)
        self.assertIn("id: image-gate", workflow)
        self.assertIn("steps.image-gate.outputs.sarif == 'true'", workflow)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", workflow)
        self.assertEqual(workflow.count("category: trivy-draft-image"), 2)
        self.assertNotIn("category: trivy-release-image", workflow)
        upload = workflow.index("category: trivy-draft-image")
        self.assertLess(workflow.index("id: image-gate"), upload)
        self.assertLess(upload, workflow.index("Enforce release-image gate"))

    def test_manual_draft_retains_image_sarif_before_archiving(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        prepare_release = workflow[workflow.index("prepare-release:") :]
        self.assertIn("security-events: write", prepare_release)
        self.assertIn("id: draft-image-gate", prepare_release)
        self.assertIn("steps.draft-image-gate.outputs.sarif == 'true'", prepare_release)
        # Every image upload in this workflow uses the analysis identity that code
        # scanning already knows on the default branch. A second category for the
        # same image makes code scanning report "configuration not found" instead
        # of comparing the pull request against the branch.
        self.assertIn("category: trivy-draft-image", prepare_release)
        self.assertNotIn("category: trivy-release-image", workflow)
        self.assertEqual(workflow.count("category: trivy-draft-image"), 2)
        self.assertLess(
            prepare_release.index("category: trivy-draft-image"),
            prepare_release.index("docker save"),
        )

    def test_image_scan_uploads_share_the_default_branch_configuration(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        rehearsal = workflow[: workflow.index("prepare-release:")]
        prepare_release = workflow[workflow.index("prepare-release:") :]
        # A code scanning configuration is the pair (tool, category). Both image
        # scans therefore have to use the one category that already exists on the
        # default branch; the rehearsal upload is the one the pull request run
        # produces, and it is the upload code scanning compares against the branch.
        self.assertEqual(workflow.count("category: trivy-draft-image"), 2)
        self.assertEqual(workflow.count("category: trivy-release-image"), 0)
        self.assertIn("category: trivy-draft-image", rehearsal)
        self.assertIn("category: trivy-draft-image", prepare_release)

    def test_installer_pins_tool_versions_and_literal_checksums(self):
        installer = (REPOSITORY_ROOT / "scripts" / "install_security_tools.sh").read_text(encoding="utf-8")
        self.assertIn("TRIVY_VERSION=0.72.0", installer)
        self.assertIn("GITLEAKS_VERSION=8.30.1", installer)
        self.assertIn("bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea", installer)
        self.assertIn("551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb", installer)

    def test_drift_workflow_rescans_the_current_image_under_the_release_policy(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "image-drift.yml").read_text(encoding="utf-8")

        self.assertIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("--ignore-unfixed", workflow)
        self.assertIn("--fail-on any", workflow)
        self.assertIn("security-events: write", workflow)
        self.assertIn("category: trivy-draft-image", workflow)
        self.assertIn("itambox:drift-scan", workflow)


if __name__ == "__main__":
    unittest.main()
