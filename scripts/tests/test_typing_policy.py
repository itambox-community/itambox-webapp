"""Behavioural suite for the backend static-typing policy gate.

The gate this suite covers (``scripts/check_typing_policy.py``) is the ratchet
for issue #93: an append-only allowlist of modules that must type-check with
zero diagnostics under an explicitly pinned checker. The suite is stdlib-only
and never invokes mypy, because CI runs it on the bare interpreter before any
dependency is installed -- a broken gate has to be caught before the ~40 minute
suite, not after it. The one thing that genuinely needs mypy is the checker run
itself, and the gate takes an injectable runner so its command line, working
directory, and path translation are assertable without it.

Every negative case below is a way the ratchet could silently stop ratcheting:
a fingerprint that no longer describes the effective policy, a checked module
dropped from the record while the admission high-water mark stays behind, a
tombstone with no reason, an override or a top-level key that relaxes a
normative flag, an unexplained ``Any``, an uncoded suppression, a checker whose
installed version is not the pinned one, or a run on the wrong interpreter or
against the wrong configuration file.

Symbol-scope admissions add their own: a scope shape the gate cannot read, a
named symbol that no longer exists, a fingerprint blind to which symbols were
admitted, and a projection that would let an admitted contract borrow the types
of the debt it was carved out of.
"""

import ast
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest import mock

from scripts import check_typing_policy as gate

REPO_ROOT = Path(__file__).resolve().parents[2]

# The fixture repository's pyproject. Written out literally rather than
# generated from the gate's own constants: a change to the normative flag set
# has to break this file, which is exactly the review event it should be.
FIXTURE_PYPROJECT = """\
[project]
name = "fixture"
version = "0"

[dependency-groups]
dev = [
    "mypy==2.3.0",
    "django-stubs[compatible-mypy]==6.0.7",
    "djangorestframework-stubs[compatible-mypy]==3.17.1",
]

[tool.mypy]
python_version = "3.12"
plugins = ["mypy_django_plugin.main"]
follow_imports = "silent"
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
no_implicit_optional = true
warn_return_any = true
warn_unused_ignores = true
warn_redundant_casts = true
strict_equality = true
disallow_any_generics = true
disallow_any_unimported = true
disallow_untyped_decorators = false
enable_error_code = ["ignore-without-code", "possibly-undefined"]

[tool.django-stubs]
django_settings_module = "core.settings.dev"
"""

CLEAN_MODULE = '''\
"""A checked fixture module."""

from typing import Any


# typing: external-json: the payload arrives as arbitrary parsed JSON and is validated below
def parse(payload: Any) -> str:
    return str(payload)
'''

UNMARKED_MODULE = '''\
"""A checked fixture module with an unexplained Any."""

from typing import Any


def parse(payload: Any) -> str:
    return str(payload)
'''

SYMBOL_MODULE = '''\
"""A module with one admitted contract and unrelated untyped debt."""

from typing import Any


class Contract:
    value: int

    def accepts(self, value: int) -> bool:
        return value == self.value


def unrelated(payload: Any):
    return payload
'''

PROJECTION_MODULE = '''\
"""A module whose admitted contract sits among unrelated debt."""

import json
from decimal import Decimal
from typing import Any

LIMIT = 10
_ENCODERS = [json.dumps]


class Contract:
    """The admitted contract."""

    value: Decimal

    def accepts(self, value: int) -> bool:
        """A docstring, then a body the projection must not keep."""
        scaled = value * LIMIT
        return scaled == self.value


def unrelated(payload: Any):
    return payload


FALLBACK = unrelated
'''


class _FakeRunner:
    """Stands in for ``subprocess.run`` so the gate's command line is assertable."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return type("Completed", (), {"returncode": self.returncode, "args": command})()


def _checked_entry(
    sequence=1,
    path="itambox/pkg/checked.py",
    module="pkg.checked",
    scope="module",
    symbols=None,
):
    return {
        "sequence": sequence,
        "path": path,
        "module": module,
        "issue": "#93",
        "note": "Pilot module: pure parsing, no ORM inference required.",
        "scope": scope,
        "symbols": [] if symbols is None else symbols,
    }


def _legacy_entry(sequence=1, path="itambox/pkg/checked.py", module="pkg.checked"):
    """A schema-v1 checked entry: no scope fields, so the whole module."""
    entry = _checked_entry(sequence=sequence, path=path, module=module)
    return {key: value for key, value in entry.items() if key not in ("scope", "symbols")}


def _withdrawn_entry(sequence=2, path="itambox/pkg/gone.py", module="pkg.gone"):
    return {
        "sequence": sequence,
        "path": path,
        "module": module,
        "issue": "#93",
        "reason": "Withdrawn because a clean signature needs a behaviour change reviewed separately.",
    }


class PolicyFixture:
    """A throwaway repository laid out the way the gate expects to find one."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)
        (self.root / "itambox" / "pkg").mkdir(parents=True, exist_ok=True)
        self.write_pyproject(FIXTURE_PYPROJECT)
        self.write_module("itambox/pkg/checked.py", CLEAN_MODULE)
        self.write_record(checked=[_checked_entry()], withdrawn=[])

    @property
    def record_path(self):
        return self.root / "scripts" / "typing_checked_modules.json"

    def write_pyproject(self, text):
        (self.root / "pyproject.toml").write_text(text, encoding="utf-8")

    def write_module(self, relative_path, text):
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def record(self):
        return json.loads(self.record_path.read_text(encoding="utf-8"))

    def write_record(self, checked=None, withdrawn=None, **overrides):
        """Write a record that is internally consistent unless told otherwise."""
        checked = [_checked_entry()] if checked is None else checked
        withdrawn = [] if withdrawn is None else withdrawn
        policy = gate.load_effective_policy(self.root)
        document = {
            "schema_version": gate.SCHEMA_VERSION,
            "canonical_python": "3.12",
            "next_sequence": max([entry["sequence"] for entry in [*checked, *withdrawn]] or [0]) + 1,
            "policy_sha256": gate.compute_policy_fingerprint(policy, checked, withdrawn),
            "config": gate.derived_config(policy),
            "checked": checked,
            "withdrawn": withdrawn,
        }
        document.update(overrides)
        self.record_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return document

    def rewrite_record(self, mutate):
        document = self.record()
        mutate(document)
        self.record_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def check(self, runner=None):
        """Run the whole gate against the fixture; returns (exit code, findings)."""
        return gate.check_all(self.root, runner=runner or _FakeRunner())


class FixtureTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="typing-policy-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.fixture = PolicyFixture(self.directory)

    def assertRules(self, findings, *rules):
        self.assertEqual(sorted({finding.rule for finding in findings}), sorted(rules), findings)


class HappyPathTests(FixtureTestCase):
    def test_a_consistent_record_passes(self):
        self.assertEqual(self.fixture.check(), ())

    def test_the_checker_is_invoked_from_the_working_directory_with_the_root_config(self):
        runner = _FakeRunner()
        self.assertEqual(self.fixture.check(runner=runner), ())

        ((command, kwargs),) = runner.calls
        self.assertIn("mypy", command)
        self.assertIn("--config-file", command)
        config_argument = command[command.index("--config-file") + 1]
        self.assertEqual(Path(config_argument), (Path(self.directory) / "pyproject.toml").resolve())
        self.assertIn("--no-incremental", command)
        self.assertEqual(Path(kwargs["cwd"]), (Path(self.directory) / "itambox").resolve())
        # Repo-relative in the record, working-directory-relative on the command
        # line: mypy resolves `pkg.checked` the way Django does only from itambox/.
        self.assertIn("pkg/checked.py", command)
        self.assertNotIn("itambox/pkg/checked.py", command)
        self.assertEqual(kwargs["env"]["ITAMBOX_ENV"], "dev")
        self.assertEqual(kwargs["env"]["PYTHONPATH"], "")
        self.assertEqual(kwargs["env"]["MYPYPATH"], "")

    def test_checker_diagnostics_fail_the_gate(self):
        """Exit 1 is mypy's "I checked, and the code has diagnostics"."""
        findings = self.fixture.check(runner=_FakeRunner(returncode=1))
        self.assertRules(findings, "T-RUN1")

    def test_a_checker_that_could_not_run_is_not_reported_as_a_diagnostic(self):
        """Exit >= 2 is a crash, a bad config, or a broken plugin -- not a result."""
        for returncode in (2, 3):
            with self.subTest(returncode=returncode), self.assertRaises(gate.PolicyError) as caught:
                self.fixture.check(runner=_FakeRunner(returncode=returncode))
            self.assertIn(str(returncode), str(caught.exception))


class SymbolScopeTests(FixtureTestCase):
    def _write_symbol_record(self, symbols=("Contract",)):
        self.fixture.write_module("itambox/pkg/symbol.py", SYMBOL_MODULE)
        self.fixture.write_record(
            checked=[
                _checked_entry(
                    path="itambox/pkg/symbol.py",
                    module="pkg.symbol",
                    scope="symbols",
                    symbols=list(symbols),
                )
            ],
            withdrawn=[],
        )

    def test_a_symbol_scope_can_admit_one_contract_while_unselected_debt_remains(self):
        self._write_symbol_record()

        self.assertEqual(self.fixture.check(), ())

    def test_unselected_explicit_any_is_not_scanned(self):
        self._write_symbol_record()

        self.assertEqual(gate.check_markers(self.fixture.root, gate.load_record(self.fixture.root)), ())

    def test_explicit_any_inside_a_selected_symbol_fails(self):
        self.fixture.write_module("itambox/pkg/symbol.py", SYMBOL_MODULE.replace("value: int", "value: Any"))
        self.fixture.write_record(
            checked=[
                _checked_entry(
                    path="itambox/pkg/symbol.py",
                    module="pkg.symbol",
                    scope="symbols",
                    symbols=["Contract"],
                )
            ],
            withdrawn=[],
        )

        self.assertRules(self.fixture.check(), "T-ANY1")

    def test_a_missing_top_level_symbol_fails(self):
        self._write_symbol_record(symbols=("Missing",))

        findings = self.fixture.check()

        self.assertRules(findings, "T-SYM1")
        self.assertIn("Missing", findings[0].detail)

    def test_a_symbol_scope_requires_at_least_one_symbol(self):
        self.fixture.write_record(checked=[_checked_entry(scope="symbols", symbols=[])], withdrawn=[])

        with self.assertRaises(gate.PolicyError):
            self.fixture.check()

    def test_a_module_scope_cannot_name_symbols(self):
        self.fixture.write_record(checked=[_checked_entry(scope="module", symbols=["Contract"])], withdrawn=[])

        with self.assertRaises(gate.PolicyError):
            self.fixture.check()

    def test_symbol_scope_uses_a_temporary_projection_as_the_mypy_input(self):
        self._write_symbol_record()

        test_case = self

        class ShadowInspectingRunner(_FakeRunner):
            def __call__(self, command, **kwargs):
                test_case.assertNotIn("--shadow-file", command)
                self.projection_path = Path(command[-1])
                self.projection_exists_during_run = self.projection_path.is_file()
                return super().__call__(command, **kwargs)

        runner = ShadowInspectingRunner()
        self.assertEqual(self.fixture.check(runner=runner), ())
        self.assertTrue(runner.projection_exists_during_run)
        self.assertEqual(runner.projection_path.name, "pkg__symbol.py")

    def test_a_whole_module_admission_is_invoked_without_a_shadow_file(self):
        """The module-scope command line is the one it has always been."""
        runner = _FakeRunner()
        self.assertEqual(self.fixture.check(runner=runner), ())

        ((command, _kwargs),) = runner.calls
        self.assertNotIn("--shadow-file", command)

    def test_the_projection_is_temporary_and_never_written_into_the_repository(self):
        self._write_symbol_record()
        before = {path for path in Path(self.directory).rglob("*") if path.is_file()}

        class ShadowInspectingRunner(_FakeRunner):
            def __call__(self, command, **kwargs):
                self.projection_path = Path(command[-1])
                return super().__call__(command, **kwargs)

        runner = ShadowInspectingRunner()
        self.assertEqual(self.fixture.check(runner=runner), ())

        self.assertFalse(runner.projection_path.is_file())
        self.assertFalse(runner.projection_path.is_relative_to(Path(self.directory)))
        self.assertEqual({path for path in Path(self.directory).rglob("*") if path.is_file()}, before)

    def test_a_suppression_outside_the_admitted_symbols_is_not_scanned(self):
        self.fixture.write_module(
            "itambox/pkg/symbol.py", SYMBOL_MODULE.replace("return payload", "return payload  # type: ignore")
        )
        self.fixture.write_record(
            checked=[
                _checked_entry(path="itambox/pkg/symbol.py", module="pkg.symbol", scope="symbols", symbols=["Contract"])
            ],
            withdrawn=[],
        )

        self.assertEqual(self.fixture.check(), ())

    def test_a_suppression_inside_an_admitted_symbol_is_scanned(self):
        self.fixture.write_module(
            "itambox/pkg/symbol.py",
            SYMBOL_MODULE.replace("return value == self.value", "return value == self.value  # type: ignore"),
        )
        self.fixture.write_record(
            checked=[
                _checked_entry(path="itambox/pkg/symbol.py", module="pkg.symbol", scope="symbols", symbols=["Contract"])
            ],
            withdrawn=[],
        )

        self.assertRules(self.fixture.check(), "T-IGN1")

    def test_a_malformed_scope_or_symbol_shape_fails_closed(self):
        """A scope the gate cannot read is never defaulted to "the whole module"."""
        for scope, symbols in (
            ("Symbols", ["Contract"]),
            ("everything", []),
            (gate.SYMBOL_SCOPE, "Contract"),
            (gate.SYMBOL_SCOPE, ["Second", "First"]),
            (gate.SYMBOL_SCOPE, ["First", "First"]),
            (gate.SYMBOL_SCOPE, ["not an identifier"]),
            (gate.SYMBOL_SCOPE, [None]),
        ):
            with self.subTest(scope=scope, symbols=symbols):
                self.fixture.write_record(checked=[_checked_entry(scope=scope, symbols=symbols)], withdrawn=[])

                with self.assertRaises(gate.PolicyError):
                    self.fixture.check()

    def test_a_record_at_the_current_schema_must_state_what_each_entry_admits(self):
        self.fixture.write_record(checked=[_legacy_entry()], withdrawn=[])

        with self.assertRaises(gate.PolicyError):
            self.fixture.check()


class SchemaVersionTests(FixtureTestCase):
    """A v1 record admits whole modules and keeps working, unchanged and un-re-recorded."""

    def test_a_legacy_record_still_passes_and_checks_the_whole_module(self):
        self.fixture.write_record(checked=[_legacy_entry()], withdrawn=[], schema_version=gate.LEGACY_SCHEMA_VERSION)

        self.assertEqual(self.fixture.check(), ())

        self.fixture.write_module("itambox/pkg/checked.py", UNMARKED_MODULE)
        self.assertRules(self.fixture.check(), "T-ANY1")

    def test_a_legacy_record_may_not_declare_a_scope(self):
        self.fixture.write_record(checked=[_checked_entry()], withdrawn=[], schema_version=gate.LEGACY_SCHEMA_VERSION)

        with self.assertRaises(gate.PolicyError):
            self.fixture.check()

    def test_the_fingerprint_covers_the_scope_and_the_admitted_symbols(self):
        policy = gate.load_effective_policy(self.fixture.root)
        fingerprints = {
            label: gate.compute_policy_fingerprint(policy, [entry], [])
            for label, entry in {
                "legacy": _legacy_entry(),
                "module": _checked_entry(),
                "one symbol": _checked_entry(scope="symbols", symbols=["Contract"]),
                "two symbols": _checked_entry(scope="symbols", symbols=["Contract", "Second"]),
            }.items()
        }

        self.assertEqual(len(set(fingerprints.values())), len(fingerprints), fingerprints)


class ProjectionTests(unittest.TestCase):
    """What mypy is handed for a symbol scope, and what it is deliberately not."""

    def _project(self, source, symbols):
        tree = ast.parse(source)
        nodes, findings = gate._resolve_symbols("pkg/symbol.py", tree, symbols)
        self.assertEqual(findings, [])
        return gate.project_symbols(source, tree, nodes)

    def test_the_projection_is_valid_python(self):
        compile(self._project(PROJECTION_MODULE, ["Contract"]), "pkg/symbol.py", "exec")

    def test_bodies_are_replaced_by_a_raise_and_signatures_are_kept(self):
        projection = self._project(PROJECTION_MODULE, ["Contract"])

        self.assertIn("def accepts(self, value: int) -> bool:", projection)
        self.assertIn("raise NotImplementedError", projection)
        self.assertNotIn("scaled = value * LIMIT", projection)
        self.assertNotIn("A docstring", projection)

    def test_imports_needed_by_a_signature_are_kept(self):
        projection = self._project(PROJECTION_MODULE, ["Contract"])

        self.assertIn("from decimal import Decimal", projection)
        self.assertNotIn("import json", projection)
        self.assertNotIn("from typing import Any", projection)
        self.assertNotIn("LIMIT = 10", projection)
        self.assertNotIn("_ENCODERS = [json.dumps]", projection)

    def test_unadmitted_definitions_and_what_depends_on_them_are_dropped(self):
        """An admitted symbol may not borrow an unadmitted neighbour's types."""
        projection = self._project(PROJECTION_MODULE, ["Contract"])

        self.assertNotIn("def unrelated", projection)
        self.assertNotIn("FALLBACK", projection)

    def test_line_numbers_are_preserved_so_a_diagnostic_points_at_the_real_line(self):
        projection = self._project(PROJECTION_MODULE, ["Contract"])
        source_lines = PROJECTION_MODULE.splitlines()
        projected_lines = projection.splitlines()

        self.assertEqual(len(projected_lines), len(source_lines))
        index = source_lines.index("    def accepts(self, value: int) -> bool:")
        self.assertEqual(projected_lines[index], source_lines[index])

    def test_a_body_sharing_the_header_line_keeps_its_signature(self):
        source = "class Contract:\n    def accepts(self) -> int: return 1\n"

        projection = self._project(source, ["Contract"])

        self.assertIn("def accepts(self) -> int: raise NotImplementedError", projection)
        compile(projection, "pkg/symbol.py", "exec")

    def test_a_type_checking_import_block_is_kept(self):
        source = (
            "from __future__ import annotations\n"
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n    from decimal import Decimal\n\n\n"
            "def total() -> Decimal:\n    return Decimal(0)\n"
        )

        projection = self._project(source, ["total"])

        self.assertIn("if TYPE_CHECKING:", projection)
        self.assertIn("from decimal import Decimal", projection)
        self.assertNotIn("return Decimal(0)", projection)


class RecordIntegrityTests(FixtureTestCase):
    def test_fingerprint_drift_fails(self):
        self.fixture.rewrite_record(lambda document: document.update(policy_sha256="0" * 64))
        self.assertRules(self.fixture.check(), "T-REC2")

    def test_a_fingerprint_mismatch_publishes_the_expected_value(self):
        """There is no write mode, so the reviewer needs the hash to paste in."""
        policy = gate.load_effective_policy(self.fixture.root)
        expected = gate.compute_policy_fingerprint(policy, [_checked_entry()], [])
        self.fixture.rewrite_record(lambda document: document.update(policy_sha256="0" * 64))

        findings = self.fixture.check()
        self.assertRules(findings, "T-REC2")
        self.assertTrue(all(expected in finding.detail for finding in findings), findings)

    def test_relaxing_a_normative_flag_fails_even_when_the_record_is_re_recorded(self):
        """Re-recording the fingerprint must not be a way to weaken the policy."""
        self.fixture.write_pyproject(
            FIXTURE_PYPROJECT.replace("disallow_untyped_defs = true", "disallow_untyped_defs = false")
        )
        self.fixture.write_record()

        self.assertRules(self.fixture.check(), "T-CFG2")

    def test_dropping_a_normative_flag_entirely_fails(self):
        self.fixture.write_pyproject(FIXTURE_PYPROJECT.replace("strict_equality = true\n", ""))
        self.fixture.write_record()

        self.assertRules(self.fixture.check(), "T-CFG2")

    def test_disallow_untyped_calls_is_not_part_of_the_first_ratchet(self):
        self.assertNotIn("disallow_untyped_calls", gate.REQUIRED_FLAGS)

    def test_the_required_error_codes_are_the_ones_pyproject_and_the_document_name(self):
        self.assertEqual(sorted(gate.REQUIRED_ERROR_CODES), ["ignore-without-code", "possibly-undefined"])

    def test_dropping_a_required_error_code_fails_even_when_the_record_is_re_recorded(self):
        for code in ("ignore-without-code", "possibly-undefined"):
            with self.subTest(code=code):
                self.fixture.write_pyproject(FIXTURE_PYPROJECT.replace(json.dumps(code), '"redundant-expr"'))
                self.fixture.write_record()

                self.assertRules(self.fixture.check(), "T-CFG2")

    def test_an_unpermitted_django_stubs_option_is_refused(self):
        self.fixture.write_pyproject(
            FIXTURE_PYPROJECT.replace(
                "[tool.django-stubs]",
                "[tool.django-stubs]\nstrict_settings = false",
            )
        )
        with self.assertRaises(gate.PolicyError):
            gate.load_effective_policy(self.fixture.root)

    def test_a_top_level_key_outside_the_policy_fails_even_when_the_record_is_re_recorded(self):
        """`[tool.mypy] ignore_errors = true` silences every checked module at once."""
        for line in ("ignore_errors = true", 'disable_error_code = ["ignore-without-code"]'):
            with self.subTest(line=line):
                self.fixture.write_pyproject(
                    FIXTURE_PYPROJECT.replace("[tool.django-stubs]", f"{line}\n\n[tool.django-stubs]")
                )
                self.fixture.write_record()

                findings = self.fixture.check()
                self.assertRules(findings, "T-CFG2")
                self.assertIn(line.split(" =")[0], findings[0].detail)

    def test_the_permitted_top_level_keys_are_the_normative_flags_and_nothing_else(self):
        self.assertEqual(gate.PERMITTED_FLAG_KEYS, frozenset(gate.REQUIRED_FLAGS) | {"enable_error_code"})

    def test_a_config_block_that_disagrees_with_pyproject_fails(self):
        def stale_settings_module(document):
            document["config"]["settings_module"] = "core.settings.prod"

        self.fixture.rewrite_record(stale_settings_module)
        self.assertRules(self.fixture.check(), "T-REC2", "T-CFG5")

    def test_a_checker_version_bump_that_is_not_recorded_fails(self):
        self.fixture.write_pyproject(FIXTURE_PYPROJECT.replace("mypy==2.3.0", "mypy==2.4.0"))
        self.assertRules(self.fixture.check(), "T-REC2", "T-CFG5")

    def test_an_unpinned_checker_dependency_fails(self):
        self.fixture.write_pyproject(FIXTURE_PYPROJECT.replace("mypy==2.3.0", "mypy>=2.3,<3.0"))
        with self.assertRaises(gate.PolicyError):
            gate.load_effective_policy(self.fixture.root)

    def _with_two_checked_modules(self):
        self.fixture.write_module("itambox/pkg/second.py", CLEAN_MODULE)
        self.fixture.write_record(
            checked=[_checked_entry(), _checked_entry(sequence=2, path="itambox/pkg/second.py", module="pkg.second")]
        )
        self.assertEqual(self.fixture.check(), ())

    def _keep_only(self, sequences, **overrides):
        """Re-record the whole document around a subset of the checked rows."""
        remaining = [entry for entry in self.fixture.record()["checked"] if entry["sequence"] in sequences]
        policy = gate.load_effective_policy(self.fixture.root)
        self.fixture.rewrite_record(
            lambda document: document.update(
                checked=remaining,
                policy_sha256=gate.compute_policy_fingerprint(policy, remaining, []),
                **overrides,
            )
        )

    def test_removing_a_row_while_next_sequence_stays_stale_fails(self):
        """The high-water mark is what the ledger detects a deletion by."""
        self._with_two_checked_modules()
        self._keep_only({1})

        findings = self.fixture.check()
        self.assertRules(findings, "T-REC3")
        self.assertIn("next_sequence", findings[0].detail)

    def test_removing_a_row_that_is_not_the_last_one_fails_on_the_gap(self):
        """Rewinding next_sequence cannot close a hole in the middle of the run."""
        self._with_two_checked_modules()
        self._keep_only({2}, next_sequence=2)

        self.assertRules(self.fixture.check(), "T-REC3")

    def test_the_ledger_does_not_detect_a_rewound_terminal_row(self):
        """A documented limitation, asserted so nobody claims otherwise.

        Deleting the *last* admitted row, decrementing ``next_sequence``, and
        re-recording the fingerprint leaves a self-consistent record. The gate
        cannot see it without git history, and this policy deliberately does not
        consult git history. What the ledger buys is that hiding a withdrawal
        takes three coordinated edits to a reviewed file, all of them visible in
        the diff -- not that it is impossible. See
        the private design-docs typing-policy.md, "Monotonicity".
        """
        self._with_two_checked_modules()
        self._keep_only({1}, next_sequence=2)

        self.assertEqual(self.fixture.check(), ())

    def test_withdrawing_a_module_with_a_tombstone_passes(self):
        self.fixture.write_record(
            checked=[_checked_entry()],
            withdrawn=[_withdrawn_entry(sequence=2, path="itambox/pkg/second.py", module="pkg.second")],
        )
        self.assertEqual(self.fixture.check(), ())

    def test_a_tombstone_may_name_a_path_that_no_longer_exists(self):
        """That is a tombstone's entire purpose; only `checked` paths must exist."""
        self.fixture.write_record(
            checked=[_checked_entry()],
            withdrawn=[_withdrawn_entry(sequence=2, path="itambox/pkg/deleted.py", module="pkg.deleted")],
        )
        self.assertEqual(self.fixture.check(), ())

    def test_a_tombstone_without_a_long_reason_fails(self):
        entry = _withdrawn_entry(sequence=2)
        entry["reason"] = "too short"
        self.fixture.write_record(checked=[_checked_entry()], withdrawn=[entry])

        self.assertRules(self.fixture.check(), "T-REC5")

    def test_a_tombstone_without_an_issue_fails(self):
        entry = _withdrawn_entry(sequence=2)
        entry["issue"] = ""
        self.fixture.write_record(checked=[_checked_entry()], withdrawn=[entry])

        self.assertRules(self.fixture.check(), "T-REC5")

    def test_a_missing_checked_path_fails(self):
        (self.fixture.root / "itambox" / "pkg" / "checked.py").unlink()
        self.assertRules(self.fixture.check(), "T-REC4")

    def test_a_checked_entry_whose_module_contradicts_its_path_fails(self):
        self.fixture.write_record(checked=[_checked_entry(module="pkg.something_else")])
        self.assertRules(self.fixture.check(), "T-REC6")

    def test_duplicate_sequences_fail(self):
        self.fixture.write_module("itambox/pkg/second.py", CLEAN_MODULE)
        self.fixture.write_record(
            checked=[_checked_entry(), _checked_entry(sequence=1, path="itambox/pkg/second.py", module="pkg.second")],
            next_sequence=2,
        )
        self.assertRules(self.fixture.check(), "T-REC3")

    def test_an_unknown_schema_version_fails(self):
        self.fixture.rewrite_record(lambda document: document.update(schema_version=99))
        with self.assertRaises(gate.PolicyError):
            self.fixture.check()

    def test_there_is_no_write_mode(self):
        """Admitting a module is a reviewed edit, so the gate cannot make one."""
        parser = gate.build_parser()
        for flag in ("--write-baseline", "--write-record", "--fix"):
            with (
                self.subTest(flag=flag),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args([flag])


class InstalledCheckerTests(FixtureTestCase):
    """The pinned triple decides what "clean" means, so it must be the one that runs."""

    PINNED = {"mypy": "2.3.0", "django-stubs": "6.0.7", "djangorestframework-stubs": "3.17.1"}

    def setUp(self):
        super().setUp()
        self.policy = gate.load_effective_policy(self.fixture.root)

    def test_installed_versions_matching_the_pins_are_accepted(self):
        self.assertIsNone(gate.verify_installed_checker(self.policy, version_lookup=self.PINNED.__getitem__))

    def test_a_checker_distribution_that_is_not_installed_is_refused(self):
        def lookup(distribution):
            if distribution == "django-stubs":
                raise PackageNotFoundError(distribution)
            return self.PINNED[distribution]

        with self.assertRaises(gate.PolicyError) as caught:
            gate.verify_installed_checker(self.policy, version_lookup=lookup)
        self.assertIn("django-stubs", str(caught.exception))

    def test_an_installed_version_that_is_not_the_pinned_one_is_refused(self):
        versions = {**self.PINNED, "mypy": "2.4.0"}

        with self.assertRaises(gate.PolicyError) as caught:
            gate.verify_installed_checker(self.policy, version_lookup=versions.__getitem__)
        message = str(caught.exception)
        self.assertIn("2.4.0", message)
        self.assertIn("2.3.0", message)

    def test_a_real_checker_run_verifies_the_installed_distributions_first(self):
        verified = []
        with (
            mock.patch.object(gate, "verify_installed_checker", verified.append),
            mock.patch.object(gate.subprocess, "run", _FakeRunner()),
        ):
            findings = gate.run_checker(self.fixture.root, self.policy, ["itambox/pkg/checked.py"])

        self.assertEqual(findings, ())
        self.assertEqual(verified, [self.policy])

    def test_an_injected_runner_does_not_require_an_installed_checker(self):
        """CI runs this suite on the bare interpreter, before mypy exists at all."""

        def refuse(policy):
            raise AssertionError("the installed checker must not be inspected when no checker is invoked")

        with mock.patch.object(gate, "verify_installed_checker", refuse):
            self.assertEqual(self.fixture.check(), ())


class ListingTests(FixtureTestCase):
    """``--list`` is how a reviewer finds the value to paste into the record."""

    def _listing(self):
        policy = gate.load_effective_policy(self.fixture.root)
        record = gate.load_record(self.fixture.root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            gate._print_listing(self.fixture.root, policy, record)
        return output.getvalue()

    def test_the_listing_publishes_the_recorded_and_the_expected_fingerprint(self):
        policy = gate.load_effective_policy(self.fixture.root)
        expected = gate.compute_policy_fingerprint(policy, [_checked_entry()], [])
        self.fixture.rewrite_record(lambda document: document.update(policy_sha256="0" * 64))

        listing = self._listing()
        self.assertIn("0" * 64, listing)
        self.assertIn(expected, listing)

    def test_the_listing_names_the_checked_and_withdrawn_modules(self):
        listing = self._listing()
        self.assertIn("itambox/pkg/checked.py", listing)
        self.assertIn("mypy==2.3.0", listing)


class OverrideTests(FixtureTestCase):
    def _with_override(self, body):
        self.fixture.write_pyproject(FIXTURE_PYPROJECT + body)
        self.fixture.write_record()

    def test_ignore_errors_for_a_checked_module_fails(self):
        self._with_override('\n[[tool.mypy.overrides]]\nmodule = ["pkg.checked"]\nignore_errors = true\n')
        self.assertRules(self.fixture.check(), "T-CFG3")

    def test_a_wildcard_override_that_captures_a_checked_module_fails(self):
        self._with_override('\n[[tool.mypy.overrides]]\nmodule = ["pkg.*"]\ndisallow_untyped_defs = false\n')
        self.assertRules(self.fixture.check(), "T-CFG3")

    def test_a_missing_stub_override_for_a_third_party_module_is_allowed(self):
        self._with_override(
            '\n[[tool.mypy.overrides]]\nmodule = ["some_untyped_dependency.*"]\nignore_missing_imports = true\n'
        )
        self.assertEqual(self.fixture.check(), ())

    def test_a_missing_stub_override_may_name_a_checked_module_without_relaxing_it(self):
        self._with_override('\n[[tool.mypy.overrides]]\nmodule = ["pkg.checked"]\nignore_missing_imports = true\n')
        self.assertEqual(self.fixture.check(), ())


class ExplicitAnyMarkerTests(FixtureTestCase):
    def test_an_unmarked_explicit_any_fails(self):
        self.fixture.write_module("itambox/pkg/checked.py", UNMARKED_MODULE)
        self.assertRules(self.fixture.check(), "T-ANY1")

    def test_an_unrecognised_category_always_fails(self):
        self.fixture.write_module(
            "itambox/pkg/checked.py",
            UNMARKED_MODULE.replace("def parse", "# typing: because-it-is-hard: no\ndef parse"),
        )
        self.assertRules(self.fixture.check(), "T-ANY2")

    def test_a_marker_without_a_reason_fails(self):
        self.fixture.write_module(
            "itambox/pkg/checked.py",
            UNMARKED_MODULE.replace("def parse", "# typing: external-json:\ndef parse"),
        )
        self.assertRules(self.fixture.check(), "T-ANY2")

    def test_a_trailing_marker_on_the_annotation_line_is_accepted(self):
        source = (
            "from typing import Any\n\nvalue: Any = None  # typing: sentinel: absent-vs-null cannot be a union yet\n"
        )
        self.fixture.write_module("itambox/pkg/checked.py", source)
        self.assertEqual(self.fixture.check(), ())

    def test_a_class_marker_covers_the_fields_it_introduces(self):
        source = (
            "from typing import Any\n\n\n"
            "# typing: sentinel: every field is Any only because the UNSET sentinel has no union form yet\n"
            "class Patch:\n"
            "    first: Any = None\n"
            "    second: Any = None\n"
        )
        self.fixture.write_module("itambox/pkg/checked.py", source)
        self.assertEqual(self.fixture.check(), ())

    def test_a_marker_on_one_function_does_not_cover_the_next(self):
        source = CLEAN_MODULE + "\n\ndef other(payload: Any) -> str:\n    return str(payload)\n"
        self.fixture.write_module("itambox/pkg/checked.py", source)
        findings = self.fixture.check()
        self.assertRules(findings, "T-ANY1")
        self.assertIn("other", findings[0].detail)

    def test_importing_any_is_not_itself_an_explicit_any(self):
        self.fixture.write_module("itambox/pkg/checked.py", "from typing import Any  # noqa: F401\n")
        self.assertEqual(self.fixture.check(), ())

    def test_qualified_typing_any_is_detected(self):
        self.fixture.write_module(
            "itambox/pkg/checked.py", "import typing\n\n\ndef f(x: typing.Any) -> None:\n    pass\n"
        )
        self.assertRules(self.fixture.check(), "T-ANY1")

    def test_any_imported_under_an_alias_is_detected(self):
        self.fixture.write_module("itambox/pkg/checked.py", "from typing import Any as JSON\n\nvalue: JSON = None\n")
        self.assertRules(self.fixture.check(), "T-ANY1")

    def test_unchecked_modules_are_not_scanned(self):
        self.fixture.write_module("itambox/pkg/unchecked.py", UNMARKED_MODULE)
        self.assertEqual(self.fixture.check(), ())


class SuppressionGrammarTests(FixtureTestCase):
    def _module_with(self, comment):
        return f"from typing import Any  # noqa: F401\n\nvalue = 1  {comment}\n"

    def test_a_bare_ignore_fails(self):
        self.fixture.write_module("itambox/pkg/checked.py", self._module_with("# type: ignore"))
        self.assertRules(self.fixture.check(), "T-IGN1")

    def test_a_coded_ignore_without_a_category_fails(self):
        self.fixture.write_module("itambox/pkg/checked.py", self._module_with("# type: ignore[attr-defined]"))
        self.assertRules(self.fixture.check(), "T-IGN1")

    def test_a_coded_and_categorised_ignore_passes(self):
        self.fixture.write_module(
            "itambox/pkg/checked.py",
            self._module_with("# type: ignore[attr-defined]  # typing: third-party-untyped: dependency ships no stubs"),
        )
        self.assertEqual(self.fixture.check(), ())

    def test_a_file_level_mypy_directive_fails_closed(self):
        self.fixture.write_module("itambox/pkg/checked.py", "# mypy: ignore-errors\n\nvalue = 1\n")
        self.assertRules(self.fixture.check(), "T-CFG4")

    def test_the_reason_may_sit_on_the_preceding_line(self):
        source = (
            "from typing import Any  # noqa: F401\n\n"
            "# typing: django-plugin-limit: the plugin cannot infer this manager's row type\n"
            "value = 1  # type: ignore[attr-defined]\n"
        )
        self.fixture.write_module("itambox/pkg/checked.py", source)
        self.assertEqual(self.fixture.check(), ())

    def test_an_unrecognised_ignore_category_fails(self):
        self.fixture.write_module(
            "itambox/pkg/checked.py",
            self._module_with("# type: ignore[attr-defined]  # typing: convenient: it was faster"),
        )
        self.assertRules(self.fixture.check(), "T-IGN2")


class InterpreterAndConfigTests(unittest.TestCase):
    def test_a_non_canonical_interpreter_is_refused(self):
        self.assertIsNone(gate.refuse_non_canonical_interpreter((3, 12, 1)))
        for version in ((3, 11, 9), (3, 13, 0)):
            with self.subTest(version=version):
                message = gate.refuse_non_canonical_interpreter(version)
                self.assertIsNotNone(message)
                self.assertIn("3.12", message)

    def test_a_configuration_file_without_a_mypy_section_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
            with self.assertRaises(gate.PolicyError):
                gate.load_effective_policy(root)

    def test_a_missing_configuration_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(gate.PolicyError):
                gate.load_effective_policy(Path(directory))

    def test_the_platform_banner_names_linux_as_the_authority(self):
        self.assertIn("Linux", gate.platform_banner("Windows"))
        self.assertIsNone(gate.platform_banner("Linux"))


class PathTranslationTests(unittest.TestCase):
    """The record is repo-relative; mypy runs from itambox/. Both must be true."""

    def test_repo_relative_paths_translate_to_the_working_directory(self):
        self.assertEqual(
            gate.to_working_directory_paths(["itambox/users/api/scim/provider_patch.py"]),
            ["users/api/scim/provider_patch.py"],
        )

    def test_a_path_outside_the_working_directory_is_refused(self):
        with self.assertRaises(gate.PolicyError):
            gate.to_working_directory_paths(["scripts/check_typing_policy.py"])

    def test_a_windows_separator_in_the_record_is_refused(self):
        with self.assertRaises(gate.PolicyError):
            gate.to_working_directory_paths(["itambox\\users\\api.py"])

    def test_module_names_are_derived_from_the_path(self):
        self.assertEqual(
            gate.module_for_path("itambox/users/api/scim/provider_patch.py"), "users.api.scim.provider_patch"
        )
        self.assertEqual(gate.module_for_path("itambox/core/__init__.py"), "core")

    def test_the_working_directory_and_config_path_are_repository_relative_constants(self):
        self.assertEqual(gate.WORKING_DIRECTORY, "itambox")
        self.assertEqual(gate.CONFIG_FILE, "pyproject.toml")


class CommittedRecordTests(unittest.TestCase):
    """The record checked into this repository must satisfy its own policy."""

    def setUp(self):
        self.policy = gate.load_effective_policy(REPO_ROOT)
        self.record = gate.load_record(REPO_ROOT)

    def test_the_committed_record_is_internally_consistent(self):
        self.assertEqual(gate.check_record(REPO_ROOT, self.policy, self.record), ())

    def test_the_committed_record_lists_the_slice_zero_through_slice_fourteen_admissions(self):
        self.assertEqual(
            [entry["path"] for entry in self.record["checked"]],
            sorted(
                [
                    "itambox/assets/api/serializers.py",
                    "itambox/assets/graphql_specifications/scalars.py",
                    "itambox/assets/model_book_value.py",
                    "itambox/compliance/audit_services.py",
                    "itambox/inventory/models_stock.py",
                    "itambox/core/auth/oidc.py",
                    "itambox/core/context.py",
                    "itambox/core/graphql_scalars.py",
                    "itambox/core/identity_provisioning.py",
                    "itambox/core/management/commands/bind_oidc_identity.py",
                    "itambox/core/models.py",
                    "itambox/core/oidc_identity.py",
                    "itambox/core/provider_slot.py",
                    "itambox/core/reports/contracts.py",
                    "itambox/core/reports/orchestration.py",
                    "itambox/core/reports/registry.py",
                    "itambox/core/restore_authority.py",
                    "itambox/extras/services/labels.py",
                    "itambox/extras/tasks/alerts.py",
                    "itambox/extras/tasks/labels.py",
                    "itambox/assets/tasks/checkin.py",
                    "itambox/assets/tasks/checkout.py",
                    "itambox/core/tasks/context.py",
                    "itambox/core/tasks/csv_import.py",
                    "itambox/assets/tasks/depreciation.py",
                    "itambox/assets/tasks/disposal.py",
                    "itambox/assets/tasks/intune_sync.py",
                    "itambox/assets/tasks/labels.py",
                    "itambox/core/tasks/ldap.py",
                    "itambox/extras/tasks/reports.py",
                    "itambox/core/tasks/retention.py",
                    "itambox/core/tasks/utils.py",
                    "itambox/extras/tasks/webhooks.py",
                    "itambox/core/tenant_access.py",
                    "itambox/extras/api/serializers.py",
                    "itambox/inventory/api/serializers.py",
                    "itambox/itambox/api/base.py",
                    "itambox/itambox/api/fields.py",
                    "itambox/itambox/api/serializers/__init__.py",
                    "itambox/licenses/api/serializers.py",
                    "itambox/organization/access.py",
                    "itambox/organization/api/serializers.py",
                    "itambox/organization/forms/role_matrix.py",
                    "itambox/organization/services/__init__.py",
                    "itambox/organization/services/errors.py",
                    "itambox/organization/services/identity_provisioning.py",
                    "itambox/organization/services/membership.py",
                    "itambox/organization/services/resource_access.py",
                    "itambox/organization/services/restore_authority.py",
                    "itambox/organization/services/role_grant_validation.py",
                    "itambox/organization/services/role_permission_policy.py",
                    "itambox/organization/services/rolegrants.py",
                    "itambox/organization/services/tenant_access.py",
                    "itambox/procurement/api/serializers.py",
                    "itambox/subscriptions/api/serializers.py",
                    "itambox/subscriptions/api/views.py",
                    "itambox/users/api/authentication.py",
                    "itambox/users/api/scim/authentication.py",
                    "itambox/users/api/scim/filters.py",
                    "itambox/users/api/scim/identifiers.py",
                    "itambox/users/api/scim/provider_authentication.py",
                    "itambox/users/api/scim/provider_patch.py",
                    "itambox/users/api/scim/provider_services.py",
                    "itambox/users/api/scim/provider_views.py",
                    "itambox/users/api/scim/serializers.py",
                    "itambox/users/models.py",
                    "itambox/users/signals.py",
                ]
            ),
        )
        self.assertEqual(
            {entry["path"]: (entry["scope"], entry["symbols"]) for entry in self.record["checked"]},
            {
                "itambox/assets/graphql_specifications/scalars.py": ("module", []),
                "itambox/assets/api/serializers.py": (
                    "symbols",
                    [
                        "AssetAssignmentSerializer",
                        "AssetCheckOutAPISerializer",
                        "AssetSerializer",
                        "StatusLabelSerializer",
                    ],
                ),
                "itambox/assets/model_book_value.py": (
                    "symbols",
                    ["compute_book_value", "resolve_policy"],
                ),
                "itambox/compliance/audit_services.py": (
                    "symbols",
                    [
                        "AuditClassification",
                        "AuditCloseResult",
                        "AuditFlagResult",
                        "AuditRehomeResult",
                        "ReconciliationReportV2",
                        "classify_session_audits",
                        "close_audit_session",
                        "commit_audit_basket",
                        "expected_assets_queryset",
                        "flag_missing_assets",
                        "read_reconciliation_report",
                        "rehome_audit_session_mismatches",
                    ],
                ),
                "itambox/core/auth/oidc.py": (
                    "symbols",
                    [
                        "VerifiedOIDCIdentity",
                        "VerifiedOIDCResolver",
                        "_acquire_oidc_identity_lock",
                        "_set_oidc_transaction_timeouts",
                        "resolve_verified_oidc_identity",
                    ],
                ),
                "itambox/core/context.py": ("symbols", ["SystemAuthorizationContext"]),
                "itambox/core/management/commands/bind_oidc_identity.py": (
                    "symbols",
                    ["configured_oidc_issuers", "validate_oidc_identity_input"],
                ),
                "itambox/core/models.py": ("symbols", ["write_object_change"]),
                "itambox/core/graphql_scalars.py": ("module", []),
                "itambox/core/identity_provisioning.py": ("module", []),
                "itambox/core/oidc_identity.py": ("module", []),
                "itambox/core/provider_slot.py": ("module", []),
                "itambox/core/reports/contracts.py": (
                    "symbols",
                    ["ReportDefinition", "ReportRequest", "ReportResult"],
                ),
                "itambox/core/reports/orchestration.py": (
                    "symbols",
                    ["_group_rows", "_resolve_report_scope", "build_report_context"],
                ),
                "itambox/core/reports/registry.py": (
                    "symbols",
                    [
                        "discover_report_providers",
                        "get_registered_report_types",
                        "get_report_provider",
                        "register_report_provider",
                    ],
                ),
                "itambox/core/restore_authority.py": ("module", []),
                "itambox/extras/tasks/alerts.py": (
                    "symbols",
                    ["evaluate_alert_rules_task", "run_alert_rule_now"],
                ),
                "itambox/assets/tasks/checkin.py": ("symbols", ["_parse_date", "bulk_checkin_task"]),
                "itambox/assets/tasks/checkout.py": ("symbols", ["_parse_date", "bulk_checkout_task"]),
                "itambox/core/tasks/csv_import.py": ("symbols", ["import_csv_task"]),
                "itambox/assets/tasks/depreciation.py": ("symbols", ["calculate_depreciation"]),
                "itambox/assets/tasks/disposal.py": (
                    "symbols",
                    ["_parse_date", "_parse_proceeds", "bulk_dispose_task"],
                ),
                "itambox/assets/tasks/intune_sync.py": (
                    "symbols",
                    [
                        "IntuneAppPayload",
                        "IntuneDevicePayload",
                        "IntuneSyncResult",
                        "_IntuneAsset",
                        "_IntuneTenant",
                        "_create_asset",
                        "_run_sync",
                        "_slugify",
                        "_stamp_discovery_facts",
                        "_sync_device_software",
                        "sync_tenant_intune",
                    ],
                ),
                "itambox/assets/tasks/labels.py": ("symbols", ["generate_label_pdf_batch_task"]),
                "itambox/extras/services/labels.py": (
                    "symbols",
                    [
                        "LabelContent",
                        "LabelNotApplicable",
                        "barcode_data_uri",
                        "chunk_list",
                        "label_print_css",
                        "safe_label_measurement",
                    ],
                ),
                "itambox/extras/tasks/labels.py": ("symbols", ["generate_label_pdf_batch_task"]),
                "itambox/core/tasks/ldap.py": ("symbols", ["sync_tenant_ldap_task"]),
                "itambox/users/api/scim/provider_patch.py": ("module", []),
                "itambox/core/tasks/context.py": ("symbols", ["TaskContext"]),
                "itambox/extras/tasks/reports.py": (
                    "symbols",
                    ["_ReportOutput", "generate_scheduled_report_task"],
                ),
                "itambox/core/tasks/retention.py": ("symbols", ["prune_changelog_task"]),
                "itambox/core/tasks/utils.py": ("symbols", ["reverse_job_detail"]),
                "itambox/extras/tasks/webhooks.py": ("symbols", ["send_webhook_task"]),
                "itambox/core/tenant_access.py": ("module", []),
                "itambox/extras/api/serializers.py": (
                    "symbols",
                    [
                        "EventRuleSerializer",
                        "JournalEntrySerializer",
                        "NotificationChannelSerializer",
                        "WebhookEndpointSerializer",
                    ],
                ),
                "itambox/inventory/models_stock.py": (
                    "symbols",
                    ["adjust_inventory_stock", "require_authorized_assignment_creation"],
                ),
                "itambox/inventory/api/serializers.py": (
                    "symbols",
                    [
                        "AccessorySerializer",
                        "ComponentSerializer",
                        "ConsumableSerializer",
                        "_AssignmentAvailabilityMixin",
                        "_accessory_category_queryset",
                        "_component_category_queryset",
                        "_consumable_category_queryset",
                    ],
                ),
                "itambox/itambox/api/base.py": (
                    "symbols",
                    ["BaseModelSerializer", "ValidatedModelSerializer"],
                ),
                "itambox/itambox/api/fields.py": (
                    "symbols",
                    [
                        "ChoiceField",
                        "ContentTypeField",
                        "RelatedObjectCountField",
                        "SerializedPKRelatedField",
                        "validate_gfk_target_tenant",
                    ],
                ),
                "itambox/itambox/api/serializers/__init__.py": (
                    "symbols",
                    ["GenericObjectSerializer", "NestedUserSerializer", "ObjectChangeSerializer"],
                ),
                "itambox/licenses/api/serializers.py": (
                    "symbols",
                    ["LicenseSeatAssignmentSerializer", "LicenseSerializer"],
                ),
                "itambox/organization/access.py": (
                    "symbols",
                    [
                        "ResourceAccessDecision",
                        "accessible_tenant_ids",
                        "accessible_tenant_ids_with_expiry",
                        "authorize_tenant_operation",
                        "get_ancestor_tenant_group_ids",
                        "get_descendant_tenant_group_ids",
                        "managed_accessible_tenant_ids",
                        "resolve_stock_access",
                        "resolved_shared_stock_ids",
                        "shared_resource_ids",
                        "shared_stock_read_allowed",
                        "tenant_access_report",
                    ],
                ),
                "itambox/organization/api/serializers.py": (
                    "symbols",
                    [
                        "ContactAssignmentSerializer",
                        "ContactRoleSerializer",
                        "ContactSerializer",
                        "NestedTenantGroupSerializer",
                        "NestedTenantSerializer",
                        "TenantSerializer",
                    ],
                ),
                "itambox/organization/forms/role_matrix.py": ("symbols", ["_validate_declarations"]),
                "itambox/organization/services/__init__.py": ("module", []),
                "itambox/organization/services/errors.py": ("module", []),
                "itambox/organization/services/identity_provisioning.py": ("module", []),
                "itambox/organization/services/membership.py": (
                    "symbols",
                    [
                        "MembershipIntent",
                        "MembershipWritePlan",
                        "MembershipWriteResult",
                        "NewIdentitySpec",
                        "_MembershipActor",
                        "_MembershipUser",
                        "apply_membership_grants",
                        "authorize_membership_write",
                        "execute_membership_write",
                        "may_manage_memberships",
                        "plan_membership_write",
                        "resolve_identity",
                    ],
                ),
                "itambox/organization/services/resource_access.py": (
                    "symbols",
                    ["_ContainerPermissionActor", "is_container_scoped_unfiltered", "visible_to_containers"],
                ),
                "itambox/organization/services/restore_authority.py": ("module", []),
                "itambox/organization/services/role_grant_validation.py": ("module", []),
                "itambox/organization/services/role_permission_policy.py": ("module", []),
                "itambox/organization/services/rolegrants.py": (
                    "symbols",
                    [
                        "GrantChange",
                        "GrantPlan",
                        "GrantSyncResult",
                        "ManagedGrantSpec",
                        "OwnGrantSpec",
                        "ValidatedGrantPlan",
                        "assignable_roles_qs",
                        "live_managed_grants",
                        "live_own_grants",
                        "managed_target_tenants_qs",
                        "requested_tenant_ids_for",
                        "role_assignable_in",
                        "sync_membership_grants",
                        "validate_grant_plan",
                    ],
                ),
                "itambox/organization/services/tenant_access.py": ("module", []),
                "itambox/procurement/api/serializers.py": (
                    "symbols",
                    [
                        "ContractSerializer",
                        "PurchaseOrderLineSerializer",
                        "PurchaseOrderReceiveSerializer",
                        "PurchaseOrderSerializer",
                    ],
                ),
                "itambox/subscriptions/api/serializers.py": (
                    "symbols",
                    ["SubscriptionAssignmentSerializer", "SubscriptionSerializer"],
                ),
                "itambox/users/api/authentication.py": (
                    "symbols",
                    ["TokenAuthentication", "TokenAuthenticationScheme"],
                ),
                "itambox/users/api/scim/authentication.py": (
                    "symbols",
                    [
                        "SCIMBearerTokenAuthentication",
                        "SCIMBearerTokenAuthenticationScheme",
                        "_SCIMAuthenticatedPrincipal",
                    ],
                ),
                "itambox/users/api/scim/filters.py": (
                    "symbols",
                    [
                        "_SCIMQuery",
                        "_build_filter_query",
                        "_normalize_filter_value",
                        "_parse_id_filter",
                        "_reject_oversized_filter",
                        "parse_scim_filter",
                        "parse_scim_membership_filter",
                    ],
                ),
                "itambox/subscriptions/api/views.py": (
                    "symbols",
                    ["SubscriptionStatusCompatibilitySerializer"],
                ),
                "itambox/users/api/scim/identifiers.py": (
                    "symbols",
                    ["get_scim_object_or_404", "identifier_lookup", "identifier_lookup_or_none"],
                ),
                "itambox/users/api/scim/provider_authentication.py": (
                    "symbols",
                    ["SCIMProviderBearerTokenAuthentication", "_SCIMAuthenticatedPrincipal"],
                ),
                "itambox/users/api/scim/provider_services.py": (
                    "symbols",
                    [
                        "apply_provider_group_patch",
                        "apply_provider_user_patch",
                        "create_provider_group",
                        "ensure_provider_group_external_id_available",
                        "ensure_provider_group_name_available",
                        "save_provider_group",
                        "sync_provider_group_members",
                    ],
                ),
                "itambox/users/api/scim/provider_views.py": (
                    "symbols",
                    [
                        "ProviderServiceProviderConfigView",
                        "SCIMProviderGroupDetailView",
                        "SCIMProviderGroupListView",
                        "SCIMProviderMixin",
                        "SCIMProviderUserDetailView",
                        "SCIMProviderUserListView",
                    ],
                ),
                "itambox/users/api/scim/serializers.py": (
                    "symbols",
                    [
                        "SCIMEmailSerializer",
                        "SCIMGroupReferenceSerializer",
                        "SCIMGroupSerializer",
                        "SCIMMetaSerializer",
                        "SCIMNameSerializer",
                        "SCIMUserSerializer",
                        "_SCIMGroupResource",
                        "_SCIMMembershipResource",
                        "_SCIMUserResource",
                    ],
                ),
                "itambox/users/models.py": ("symbols", ["OIDCIdentity"]),
                "itambox/users/signals.py": (
                    "symbols",
                    ["user_post_delete", "user_post_save", "user_pre_save"],
                ),
            },
        )

    def test_the_committed_record_admits_the_report_provider_contract_slice(self):
        entries = {entry["path"]: entry for entry in self.record["checked"]}

        expected = {
            "itambox/core/reports/contracts.py": [
                "ReportDefinition",
                "ReportRequest",
                "ReportResult",
            ],
            "itambox/core/reports/orchestration.py": [
                "_group_rows",
                "_resolve_report_scope",
                "build_report_context",
            ],
            "itambox/core/reports/registry.py": [
                "discover_report_providers",
                "get_registered_report_types",
                "get_report_provider",
                "register_report_provider",
            ],
        }

        self.assertEqual(
            {path: (entries[path]["scope"], entries[path]["symbols"]) for path in expected},
            {path: ("symbols", symbols) for path, symbols in expected.items()},
        )

    def test_the_committed_record_admits_the_scim_provider_mutation_service_slice(self):
        entries = {entry["path"]: entry for entry in self.record["checked"]}
        path = "itambox/users/api/scim/provider_services.py"
        expected = [
            "apply_provider_group_patch",
            "apply_provider_user_patch",
            "create_provider_group",
            "ensure_provider_group_external_id_available",
            "ensure_provider_group_name_available",
            "save_provider_group",
            "sync_provider_group_members",
        ]

        self.assertEqual(entries[path]["scope"], "symbols")
        self.assertEqual(entries[path]["symbols"], expected)

    def test_the_committed_record_admits_the_provider_framework_and_user_view_classes(self):
        entries = {entry["path"]: entry for entry in self.record["checked"]}
        path = "itambox/users/api/scim/provider_views.py"

        self.assertEqual(entries[path]["scope"], "symbols")
        self.assertEqual(
            entries[path]["symbols"],
            [
                "ProviderServiceProviderConfigView",
                "SCIMProviderGroupDetailView",
                "SCIMProviderGroupListView",
                "SCIMProviderMixin",
                "SCIMProviderUserDetailView",
                "SCIMProviderUserListView",
            ],
        )

    def test_the_committed_pilot_satisfies_the_marker_and_suppression_grammars(self):
        self.assertEqual(gate.check_markers(REPO_ROOT, self.record), ())

    def test_the_committed_record_pins_the_resolved_checker_triple(self):
        self.assertEqual(
            self.record["config"]["checker"],
            {"mypy": "2.3.0", "django-stubs": "6.0.7", "djangorestframework-stubs": "3.17.1"},
        )


if __name__ == "__main__":
    unittest.main()
