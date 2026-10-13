"""Policy checks over the CI workflow itself.

The quality gates are only as good as the workflow that runs them. Two failure
modes are invisible in a green tick and cannot be caught by testing the gate
scripts:

* **A skipped gate reads as a passed gate.** A step with no ``if:`` inherits
  ``success()``, so one failing step silently skips every step after it. The
  three post-suite gates answer independent questions -- did the suite run, did
  coverage slip, is the changed code tested -- and a run that regressed on two
  of them has to report both.
* **A hand-enumerated test list rots.** A new ``scripts/tests/test_*.py`` suite
  that nobody remembers to add to the workflow never runs, and a gate whose
  tests never run is not a gate.
* **A filtered trigger skips the whole suite.** A change that matches no
  ``on.pull_request.paths`` entry leaves every job of this workflow
  unreported, and a required check that never runs blocks the merge: the
  pull request trigger is unconditional, and scope lives inside the jobs.

The workflow is read as text rather than parsed as YAML on purpose: this suite
is stdlib-only, and CI runs it on the bare interpreter before any dependency is
installed, precisely so a broken gate is caught before the ~40 minute suite.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
E2E_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
PRE_COMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"
MAKEFILE_PATH = REPO_ROOT / "Makefile"

STEP_START = "      - "
FIELD_INDENT = "        "
BLOCK_INDENT = "          "

# The gates that run after the lanes, and the script each one invokes.
POST_SUITE_GATES = {
    "Check the lane matrix gate": "scripts/check_xdist_matrix.py",
    "Certify the run and publish durations": "scripts/check_test_report.py",
}

# The coverage gates consume the combined report of both lanes plus the
# qualification suites, so they live in the downstream coverage-gates job.
COVERAGE_GATES = {
    "Check the global coverage ratchet": "scripts/check_coverage_baseline.py",
    "Check differential coverage for changed production code": "scripts/check_diff_coverage.py",
}

# Both lanes must have succeeded: the gates read files the lanes write, and
# the combined coverage report exists only when both lanes ran.
LANES_SUCCEEDED = "steps.parallel.conclusion == 'success' && steps.serial.conclusion == 'success'"

# The coverage-gates job consumes the test job's artifacts, so its gates are
# conditioned on that job's result instead of its sibling steps.
LANES_RESULT_SUCCEEDED = "needs.test.result == 'success'"


class AccessibilityE2EWorkflowPolicyTests(unittest.TestCase):
    def test_accessibility_browser_contracts_run_before_merge(self):
        workflow = E2E_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertRegex(workflow, r"(?m)^  pull_request:\n    branches: \[main\]$")
        self.assertIn("node run-selected.mjs", workflow)
        self.assertIn("name: E2E / Gate", workflow)


def _assign_field(step, body):
    """Record one ``key: value`` field; return the key when it opens a block scalar."""
    key, _, value = body.partition(":")
    key = key.strip()
    value = value.strip()
    if value in {"|", ">", "|-", ">-"}:
        step[key] = ""
        return key
    step[key] = value
    return None


def parse_steps(workflow_text):
    """Extract the steps of every job as ``{field: value}`` mappings.

    A deliberately small reader for the one shape this workflow uses: steps at
    six spaces, their fields at eight, block scalars at ten or more. Nested
    mappings (``with:``) are skipped -- no assertion here needs them.
    """
    steps = []
    current = None
    block_key = None
    for line in workflow_text.splitlines():
        if line.startswith(STEP_START) and not line.startswith(STEP_START + " "):
            current = {}
            steps.append(current)
            block_key = _assign_field(current, line[len(STEP_START) :])
            continue
        if current is None:
            continue
        if line.startswith(BLOCK_INDENT):
            if block_key is not None:
                current[block_key] = (current[block_key] + "\n" + line.strip()).strip()
            continue
        if line.startswith(FIELD_INDENT):
            block_key = _assign_field(current, line[len(FIELD_INDENT) :])
            continue
        if not line.strip():
            continue
        current = None
        block_key = None
    return steps


def load_steps():
    return parse_steps(WORKFLOW_PATH.read_text(encoding="utf-8"))


def step_named(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"the CI workflow has no step named {name!r}")


def step_block(workflow_text, name):
    """The raw text of one step, from its ``- name:`` line to the next step.

    ``parse_steps`` deliberately skips nested mappings, so assertions about
    fields under ``with:`` (``if-no-files-found``, for instance) need the
    unparsed block.
    """
    lines = workflow_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if (
            line.startswith(STEP_START)
            and not line.startswith(STEP_START + " ")
            and line[len(STEP_START) :].strip() == f"name: {name}"
        ):
            start = index
            break
    if start is None:
        raise AssertionError(f"the CI workflow has no step named {name!r}")
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith(STEP_START) and not line.startswith(STEP_START + " "):
            return "\n".join(lines[start:index])
        if line and not line.startswith(" "):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


class SystemDependencyBootstrapTests(unittest.TestCase):
    """A slow hosted-runner mirror must not consume the whole CI job timeout."""

    def test_apt_update_is_bounded_and_falls_back_from_the_azure_mirror(self):
        for workflow_path in (WORKFLOW_PATH, E2E_WORKFLOW_PATH):
            with self.subTest(workflow=workflow_path.name):
                steps = parse_steps(workflow_path.read_text(encoding="utf-8"))
                run = step_named(steps, "Install system deps for python-ldap").get("run", "")

                for required in (
                    "set -Eeuo pipefail",
                    "Acquire::Retries=3",
                    "Acquire::http::Timeout=15",
                    "Acquire::https::Timeout=15",
                    "timeout --kill-after=10s 180s",
                    "timeout --kill-after=10s 300s",
                    "azure.archive.ubuntu.com/ubuntu",
                    "archive.ubuntu.com/ubuntu",
                ):
                    with self.subTest(required=required):
                        self.assertIn(required, run)


class PostSuiteGateIndependenceTests(unittest.TestCase):
    """One failing gate must not skip the next and hide its finding."""

    def setUp(self):
        self.steps = load_steps()

    def test_every_post_suite_gate_runs_on_its_own_condition(self):
        for name, script in POST_SUITE_GATES.items():
            with self.subTest(gate=name):
                step = step_named(self.steps, name)
                self.assertIn(script, step.get("run", ""))
                condition = step.get("if", "")
                self.assertIn(
                    "always()",
                    condition,
                    "without always() this gate inherits success() and is skipped by any earlier failure",
                )
                self.assertIn(LANES_SUCCEEDED, condition)
        for name, script in COVERAGE_GATES.items():
            with self.subTest(gate=name):
                step = step_named(self.steps, name)
                self.assertIn(script, step.get("run", ""))
                condition = step.get("if", "")
                self.assertIn(
                    "always()",
                    condition,
                    "without always() this gate inherits success() and is skipped by any earlier failure",
                )
                self.assertIn(LANES_RESULT_SUCCEEDED, condition)

    def test_only_the_differential_gate_is_restricted_to_pull_requests(self):
        """On a push to a protected branch there is no "changed code" range."""
        for name in dict(**POST_SUITE_GATES, **COVERAGE_GATES):
            with self.subTest(gate=name):
                condition = step_named(self.steps, name).get("if", "")
                pull_request_only = "github.event_name == 'pull_request'" in condition
                self.assertEqual(pull_request_only, "check_diff_coverage.py" in step_named(self.steps, name)["run"])

    def test_artifacts_are_not_uploaded_when_the_suite_was_never_attempted(self):
        """`if-no-files-found: error` would turn a skipped suite into an upload failure."""
        step = step_named(self.steps, "Upload coverage and duration artifacts")
        condition = step.get("if", "")
        self.assertIn("always()", condition)
        self.assertIn(
            "steps.parallel.conclusion != 'skipped' || steps.serial.conclusion != 'skipped'",
            condition,
            "lanes that never ran wrote no report; uploading is an error, not a missing artifact",
        )

    def test_the_manifest_step_precedes_both_lanes(self):
        """The completeness manifest must be recorded before the lanes run."""
        names = [step.get("name") for step in self.steps if step.get("name")]
        collect = names.index("Collect the serial node IDs for the completeness manifest")
        self.assertLess(collect, names.index("Run the parallel lane"))
        self.assertLess(collect, names.index("Run the serial-only lane"))

    def test_the_lanes_use_the_marker_split(self):
        """The xdist marker selection is enforced in the workflow, not just conftest."""
        parallel = step_named(self.steps, "Run the parallel lane").get("run", "")
        serial = step_named(self.steps, "Run the serial-only lane").get("run", "")
        self.assertIn("-n auto", parallel)
        self.assertIn("-m 'not serial_only'", parallel)
        self.assertIn("-m serial_only", serial)
        self.assertNotIn("-n auto", serial)

    def test_the_matrix_gate_reads_both_lane_reports(self):
        """Disjointness can only be checked against both lane reports."""
        gate = step_named(self.steps, "Check the lane matrix gate")
        run = gate.get("run", "")
        self.assertIn("--xdist artifacts/junit-parallel.xml", run)
        self.assertIn("--serial artifacts/junit-serial-only.xml", run)


class PullRequestSuiteScopeTests(unittest.TestCase):
    """Required checks must be reportable for every pull request.

    A required check that never runs blocks the merge, so this workflow must
    not path-filter its pull request trigger: every change runs the full
    suite. That supersedes the former per-directory filter and the tests
    that enumerated it, because a hand-maintained list of inputs quietly
    stops covering the ones somebody forgot to add.
    """

    def test_the_pull_request_trigger_is_not_path_filtered(self):
        before_jobs = WORKFLOW_PATH.read_text(encoding="utf-8").split("jobs:", 1)[0]
        self.assertIn("pull_request:", before_jobs)
        self.assertNotIn("paths:", before_jobs)
        self.assertNotIn("paths-ignore:", before_jobs)

    def test_the_merge_group_trigger_is_present(self):
        before_jobs = WORKFLOW_PATH.read_text(encoding="utf-8").split("jobs:", 1)[0]
        self.assertIn("merge_group:", before_jobs)


class GateSuiteDiscoveryTests(unittest.TestCase):
    """A hand-written list of test modules is a list that goes stale silently."""

    def setUp(self):
        self.workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_no_ci_step_names_an_individual_gate_suite_module(self):
        named = sorted(set(re.findall(r"scripts\.tests\.\w+", self.workflow_text)))
        self.assertEqual(
            named,
            [],
            "the workflow enumerates individual suites; a new scripts/tests/test_*.py would "
            "then be silently excluded. Run them by discovery instead.",
        )

    def test_migration_baseline_preflight_runs_after_fresh_migrate(self):
        steps = load_steps()
        names = [step.get("name") for step in steps if step.get("name")]
        preflight = step_named(steps, "Verify migration baseline recognition")
        self.assertIn("migration_baseline_preflight --format=json", preflight.get("run", ""))
        self.assertLess(
            names.index("Apply migrations to a fresh database"), names.index("Verify migration baseline recognition")
        )
        self.assertLess(names.index("Verify migration baseline recognition"), names.index("Run Django system checks"))

    def test_the_exception_policy_gate_runs_in_ci(self):
        gate = step_named(load_steps(), "Check the exception policy gate")
        self.assertIn("scripts/check_exception_policy.py", gate.get("run", ""))

    def test_pre_commit_runs_the_same_exception_gate(self):
        config = PRE_COMMIT_PATH.read_text(encoding="utf-8")
        self.assertIn("id: exception-policy", config)
        self.assertIn("entry: python scripts/check_exception_policy.py", config)
        self.assertIn("language_version: python3.12", config)

    def test_openapi_gate_uses_canonical_hash_seed_and_always_uploads_failure_artifacts(self):
        steps = load_steps()
        gate = step_named(steps, "Check deterministic OpenAPI schema and diagnostics baseline")
        self.assertEqual(gate.get("id"), "openapi")
        self.assertIn('PYTHONHASHSEED: "0"', self.workflow_text)
        self.assertIn('PYTHONPATH: ""', self.workflow_text)
        self.assertIn("scripts/check_openapi_schema.py", gate.get("run", ""))
        self.assertIn("artifacts/openapi/schema.generated.yaml", gate.get("run", ""))
        self.assertNotIn(".artifacts/openapi", self.workflow_text)

        upload = step_named(steps, "Upload OpenAPI generation artifacts")
        condition = upload.get("if", "")
        self.assertIn("always()", condition)
        self.assertIn("steps.openapi.conclusion != 'skipped'", condition)

    def test_the_architecture_boundary_gate_runs_in_ci(self):
        gate = step_named(load_steps(), "Check the architecture boundary gate")
        self.assertIn("scripts/check_architecture.py", gate.get("run", ""))

    def test_the_architecture_gate_runs_after_the_local_import_gate(self):
        """The two import-shaped gates stay adjacent and in dependency order."""
        names = [step.get("name") for step in load_steps() if step.get("name")]

        self.assertLess(
            names.index("Check the local-import policy gate"),
            names.index("Check the architecture boundary gate"),
        )

    def test_the_architecture_gate_is_not_wired_in_report_only_mode(self):
        """`--report-only` always exits 0; wiring it would be a silent pass."""
        gate = step_named(load_steps(), "Check the architecture boundary gate")

        self.assertNotIn("--report-only", gate.get("run", ""))
        self.assertNotIn("--report-only", PRE_COMMIT_PATH.read_text(encoding="utf-8"))

    def test_pre_commit_runs_the_same_architecture_gate(self):
        config = PRE_COMMIT_PATH.read_text(encoding="utf-8")

        self.assertIn("id: architecture-policy", config)
        self.assertIn("entry: python scripts/check_architecture.py", config)
        self.assertIn("pass_filenames: false", config)
        self.assertIn("always_run: true", config)

    def test_the_makefile_exposes_the_architecture_targets(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        phony = next(line for line in makefile.splitlines() if line.startswith(".PHONY:"))

        for target in ("architecture-check", "architecture-baseline"):
            with self.subTest(target=target):
                self.assertIn(target, phony)
                self.assertIn(f"\n{target}:\n", makefile)
                self.assertIn(f"make {target}", makefile)

    def test_internal_development_docs_are_not_in_the_mkdocs_nav(self):
        navigation = (REPO_ROOT / "itambox" / "mkdocs.yml").read_text(encoding="utf-8")

        self.assertNotIn("\n  - Development:", navigation)
        self.assertNotIn("'development/architecture-policy.md'", navigation)
        self.assertIn("omitted_files: warn", navigation)

    def test_the_discovery_invocation_reaches_every_suite_on_disk(self):
        """The arguments CI passes must actually load every suite that exists."""
        step = step_named(load_steps(), "Check the repository gate suites")
        command = " ".join(step.get("run", "").split())
        match = re.search(
            r"python -m unittest discover --start-directory (?P<start>\S+) "
            r"--top-level-directory (?P<top>\S+) --pattern '(?P<pattern>\S+)'",
            command,
        )
        self.assertIsNotNone(match, f"expected a unittest discovery invocation, got {command!r}")

        start_dir = REPO_ROOT / match.group("start")
        loader = unittest.TestLoader()
        discovered = {
            test.__class__.__module__
            for suite in loader.discover(
                start_dir=str(start_dir),
                top_level_dir=str(REPO_ROOT / match.group("top")),
                pattern=match.group("pattern"),
            )
            for test in _flatten(suite)
        }
        on_disk = {f"scripts.tests.{path.stem}" for path in sorted(start_dir.glob("test_*.py"))}

        self.assertEqual(loader.errors, [], "a suite failed to import under CI's discovery arguments")
        self.assertEqual(discovered, on_disk)


class TypingPolicyWiringTests(unittest.TestCase):
    """The typing gate must run the same way from CI, the Makefile, and pre-commit."""

    GATE = "scripts/check_typing_policy.py"
    STEP = "Check the static typing policy gate"

    def setUp(self):
        self.workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.steps = load_steps()

    def test_ci_runs_the_gate_after_installing_dependencies_and_before_the_suite(self):
        """The plugin needs the dev environment; a failure must precede the slow work."""
        step = step_named(self.steps, self.STEP)
        self.assertIn(self.GATE, step.get("run", ""))

        names = [step.get("name") for step in self.steps if step.get("name")]
        self.assertLess(names.index("Install dependencies"), names.index(self.STEP))
        self.assertLess(
            names.index(self.STEP), names.index("Collect the serial node IDs for the completeness manifest")
        )
        self.assertLess(names.index(self.STEP), names.index("Apply migrations to a fresh database"))

    def test_ci_does_not_wire_the_gate_in_its_read_only_mode(self):
        """`--list` checks nothing and always exits 0; wiring it is a silent pass."""
        self.assertNotIn("--list", step_named(self.steps, self.STEP).get("run", ""))
        pre_commit = PRE_COMMIT_PATH.read_text(encoding="utf-8")
        pre_commit_lines = pre_commit.splitlines()
        hook_start = next(index for index, line in enumerate(pre_commit_lines) if line.strip() == "- id: typing-policy")
        hook_end = next(
            (
                index
                for index in range(hook_start + 1, len(pre_commit_lines))
                if re.match(r"^\s*-\s+id:", pre_commit_lines[index])
            ),
        )
        typing_hook = "\n".join(pre_commit_lines[hook_start:hook_end])
        self.assertNotIn("--list", typing_hook)
        self.assertNotIn(f"{self.GATE} --list", MAKEFILE_PATH.read_text(encoding="utf-8"))

    def test_pre_commit_runs_the_same_gate_in_the_full_dev_environment(self):
        config = PRE_COMMIT_PATH.read_text(encoding="utf-8")

        self.assertIn("id: typing-policy", config)
        self.assertIn(f"entry: uv run --locked --group dev python {self.GATE}", config)
        self.assertNotIn(f"--only-group dev python {self.GATE}", config)

    def test_the_makefile_exposes_the_typecheck_target(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        phony = next(line for line in makefile.splitlines() if line.startswith(".PHONY:"))

        self.assertIn("typecheck", phony)
        self.assertIn("\ntypecheck:\n", makefile)
        self.assertIn("make typecheck", makefile)

    def test_the_typing_policy_is_not_in_the_public_mkdocs_nav(self):
        navigation = (REPO_ROOT / "itambox" / "mkdocs.yml").read_text(encoding="utf-8")

        self.assertNotIn("'development/typing-policy.md'", navigation)


class CoverageLaneSeparationTests(unittest.TestCase):
    """The two coverage gates must read comparable, genuinely required inputs.

    One report cannot answer both questions. The global ratchet compares a
    stable population against a recorded baseline, so it must not read a union
    whose composition depends on whether the optional migration/seed
    qualification ran on this change set. The differential gate asks whether
    the changed code is exercised, so it must read exactly that union. Feeding
    both gates the same report is what made a single head look like a baseline
    regression on one gate and a coverage shortfall on the other.
    """

    def setUp(self):
        self.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.steps = load_steps()

    def test_the_ratchet_reads_the_ordinary_lane_report(self):
        """Only the parallel and serial lanes feed the baseline comparison."""
        step = step_named(self.steps, "Check the global coverage ratchet")
        run = step.get("run", "")
        self.assertIn("--coverage-json artifacts/coverage-lanes.json", run)
        self.assertNotIn(
            "coverage-combined.json",
            run,
            "the optional qualification must not move the recorded baseline",
        )
        self.assertIn("artifacts/coverage-lanes.json", step.get("if", ""))

    def test_the_differential_gate_reads_the_union_report(self):
        """Qualification coverage counts for the changed lines it exercises."""
        run = step_named(self.steps, "Check differential coverage for changed production code").get("run", "")
        self.assertIn("--coverage-json artifacts/coverage-combined.json", run)
        self.assertNotIn("coverage-lanes.json", run)

    def test_the_ordinary_report_is_rendered_before_the_combine_consumes_it(self):
        """`coverage combine` removes `.coverage.combined`, so order matters."""
        names = [step.get("name") for step in self.steps if step.get("name")]
        self.assertLess(
            names.index("Render the ordinary lane report"),
            names.index("Combine lane and qualification coverage"),
            "the ordinary report must be rendered before its input is consumed",
        )
        run = step_named(self.steps, "Render the ordinary lane report").get("run", "")
        self.assertIn("--data-file=../artifacts/.coverage.combined", run)
        self.assertIn("-o ../artifacts/coverage-lanes.json", run)

    def test_both_reports_are_published_under_unambiguous_names(self):
        for artifact, report in (
            ("coverage-lanes-json", "coverage-lanes.json"),
            ("coverage-combined-json", "coverage-combined.json"),
        ):
            with self.subTest(artifact=artifact):
                block = step_block(
                    self.text,
                    "Upload the ordinary lane report"
                    if "lanes" in artifact
                    else "Upload the differential union report",
                )
                self.assertIn(f"name: {artifact}", block)
                self.assertIn(f"artifacts/{report}", block)

    def test_the_union_report_exists_even_without_the_qualification(self):
        """Without it the union is the ordinary population, not a missing file."""
        run = step_named(self.steps, "Combine lane and qualification coverage").get("run", "")
        self.assertIn("cp ../artifacts/coverage-lanes.json ../artifacts/coverage-combined.json", run)

    def test_the_relevance_decision_fails_closed(self):
        """An uncomputable change set is not an irrelevant change set."""
        run = step_named(self.steps, "Determine relevance").get("run", "")
        self.assertIn("set -Eeuo pipefail", run)
        self.assertIn("git cat-file -e", run)
        self.assertIn("::error::", run)
        self.assertIn("exit 1", run)
        self.assertLess(
            run.index("git cat-file -e"),
            run.index("no qualification-relevant path"),
            "the change set must be known before it may be reported as irrelevant",
        )

    def test_the_qualification_job_publishes_its_relevance_decision(self):
        self.assertIn("steps.relevance.outputs.enabled", self.text)
        self.assertIn("needs.migration-qualification.outputs.enabled", self.text)

    def test_a_required_qualification_that_wrote_nothing_fails_the_run(self):
        """An empty placeholder would silently drop qualification coverage."""
        block = step_block(self.text, "Upload qualification coverage")
        self.assertIn("if [ ! -s ../artifacts/.coverage.qualification ]", block)
        self.assertIn("exit 1", block)
        self.assertNotIn(": > artifacts/.coverage.qualification", block)
        self.assertIn("if-no-files-found: error", step_block(self.text, "Upload qualification coverage artifact"))

    def test_the_consumer_rejects_a_missing_or_empty_qualification_artifact(self):
        step = step_named(self.steps, "Verify the required qualification coverage")
        self.assertIn("enabled == 'true'", step.get("if", ""))
        self.assertIn("if [ ! -s artifacts/qualification-coverage/.coverage.qualification ]", step.get("run", ""))
        self.assertIn("exit 1", step.get("run", ""))

    def test_qualification_steps_are_never_blanket_error_tolerated(self):
        """`continue-on-error` would make a required artifact optional again."""
        steps = (
            "Download lane coverage",
            "Download qualification coverage",
            "Verify the required qualification coverage",
        )
        for name in steps:
            with self.subTest(step=name):
                self.assertNotIn("continue-on-error", step_named(self.steps, name))
        # The consumers of the artifacts run on their own condition, so a
        # failure upstream cannot skip them and read as a pass.
        for name in ("Verify the required qualification coverage", "Combine lane and qualification coverage"):
            with self.subTest(step=name):
                self.assertIn(
                    "always()",
                    step_named(self.steps, name).get("if", ""),
                    "a skipped gate reads as a passed gate",
                )

    def test_a_legitimately_irrelevant_change_skips_the_qualification_cleanly(self):
        """Skip, do not fail: the relevance output is the single decision point."""
        for name in (
            "Download qualification coverage",
            "Verify the required qualification coverage",
        ):
            with self.subTest(step=name):
                condition = step_named(self.steps, name).get("if", "")
                self.assertIn(
                    "enabled == 'true'",
                    condition,
                    "the download must be gated on the relevance decision, not on a file probe",
                )


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


if __name__ == "__main__":
    unittest.main()
