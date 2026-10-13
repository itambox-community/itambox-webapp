"""Independent oracle for the T02 specification-contract fixture corpus.

This module validates a language-neutral JSON fixture corpus that encodes
expected outcomes sourced from the exact contract paragraphs of the
specification architecture (sections 3, 4, 8.2 and 16). The corpus covers
scalar presence and normalization, deterministic composition and historical
validity.

The oracle is independent of any implementation by construction: this module
imports only the Python standard library and never imports, instantiates or
executes application code. Expected outcomes are static JSON values; they are
never produced by calling the implementation under test.
"""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "specification_contract_fixtures"

# Domain issue codes from the adopted interface ledger (T01 contract, section 4.2).
DOMAIN_ISSUE_CODES = frozenset(
    {
        "INVALID_TYPE",
        "INVALID_DECIMAL",
        "INVALID_RANGE",
        "INVALID_DATE",
        "INVALID_CHOICE",
        "REQUIRED_FIELD",
        "UNKNOWN_FIELD_KEY",
        "READ_ONLY_FIELD",
        "CONFLICT_CLEAR_OVERLAP",
        "DUPLICATE_FIELD",
        "IMMUTABLE_DEFINITION",
        "OWNERSHIP_CONFLICT",
        "REFERENCE_CONFLICT",
        "DEPENDENCY_RETIREMENT",
        "UNSUPPORTED_STRUCTURE",
        "STALE_RESOURCE",
        "STALE_DEFINITION",
        "STALE_PLAN",
        "EXPORT_BLOCKED",
        "OBJECT_UNAVAILABLE",
        "MISSING_PRECONDITION",
    }
)

CASE_ID_PATTERN = re.compile(r"^T02-(SCALAR|COMP|HIST)-\d{3}$")


def _load() -> dict:
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    documents = {}
    for name in manifest["corpus_files"]:
        documents[name] = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    return manifest, documents


def _coverage_ids(coverage):
    """Flatten manifest coverage values (plain id lists or nested dicts of id lists)."""
    for value in coverage.values():
        if isinstance(value, dict):
            for nested in value.values():
                yield from nested
        else:
            yield from value


class SpecificationContractFixtureCorpusTest(unittest.TestCase):
    """Conformance and coverage checks over the T02 fixture corpus."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.documents = _load()
        cls.cases = {}
        for name, doc in cls.documents.items():
            for case in doc["cases"]:
                cls.cases[case["id"]] = (name, case)

    def test_corpus_files_parse_and_declare_language_neutrality(self):
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertIs(self.manifest["language_neutral"], True)
        self.assertEqual(self.manifest["task"], "T02")
        for name, doc in self.documents.items():
            self.assertEqual(doc["schema_version"], 1, name)
            self.assertIs(doc["language_neutral"], True, name)
            self.assertGreaterEqual(len(doc["cases"]), 1, name)

    def test_case_ids_are_unique_and_well_formed(self):
        ids = [case["id"] for document in self.documents.values() for case in document["cases"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate case ids in corpus")
        for case_id in ids:
            self.assertRegex(case_id, CASE_ID_PATTERN, case_id)

    def test_every_case_has_required_oracle_fields(self):
        for case_id, (name, case) in self.cases.items():
            for field in (
                "id",
                "invariant",
                "source_section",
                "initial_state",
                "operation",
                "expected_result",
                "expected_unchanged",
            ):
                self.assertIn(field, case, f"{case_id} in {name} lacks {field}")
            self.assertIsInstance(case["invariant"], str, case_id)
            self.assertIsInstance(case["source_section"], str, case_id)
            self.assertIsInstance(case["initial_state"], dict, case_id)
            self.assertIsInstance(case["operation"], dict, case_id)
            self.assertIsInstance(case["expected_result"], dict, case_id)
            self.assertIsInstance(case["expected_unchanged"], dict, case_id)
        self.assertGreaterEqual(len(self.cases), 1)

    def test_rejected_cases_use_stable_issue_codes_and_paths(self):
        for case_id, (_name, case) in self.cases.items():
            result = case["expected_result"]
            if result.get("outcome") != "rejected":
                continue
            error = result.get("error")
            self.assertIsInstance(error, str, case_id)
            self.assertIn(error, DOMAIN_ISSUE_CODES, f"{case_id} error {error!r}")
            if "path" in result:
                self.assertIsInstance(result["path"], list, case_id)
                for element in result["path"]:
                    self.assertIsInstance(element, str, case_id)

    def test_accepted_patch_cases_declare_stored_state(self):
        for case_id, (_name, case) in self.cases.items():
            result = case["expected_result"]
            if case["operation"].get("kind") == "patch" and result.get("outcome") == "accepted":
                self.assertIn("stored", result, case_id)

    def test_expected_unchanged_matches_initial_state_byte_for_value(self):
        for case_id, (_name, case) in self.cases.items():
            initial = case["initial_state"]
            unchanged = case["expected_unchanged"]
            for key, value in unchanged.items():
                if key in initial:
                    self.assertEqual(
                        initial[key],
                        value,
                        f"{case_id}: expected_unchanged[{key!r}] diverges from initial_state",
                    )

    def test_duplicate_field_renders_once_with_four_unique_fields_and_all_sources(self):
        case = self.cases["T02-COMP-003"][1]
        composition = self.documents["composition.json"]
        result = case["expected_result"]
        unchanged = case["expected_unchanged"]
        duplicate = result["duplicate_field"]

        unique_keys = unchanged["field_keys"]
        self.assertEqual(unchanged["total_rendered_field_count"], 4)
        self.assertEqual(unchanged["total_rendered_field_count"], len(unique_keys))
        self.assertEqual(len(unique_keys), len(set(unique_keys)))
        self.assertIs(duplicate["rendered_once"], True)
        self.assertIn(duplicate["key"], unique_keys)

        placement_sources = [
            fieldset_identity
            for fieldset_identity, fieldset in composition["context"]["fieldsets"].items()
            if duplicate["key"] in fieldset["ordinals"]
        ]
        self.assertEqual(placement_sources, duplicate["contributing_section_identities"])
        self.assertEqual(duplicate["first_placement_section_identity"], placement_sources[0])
        self.assertEqual(len(placement_sources), 2)

    def test_removed_deprecated_choice_cannot_be_reintroduced_after_removal(self):
        case = self.cases["T02-HIST-004"][1]
        history = self.documents["history.json"]
        initial = case["initial_state"]
        operation = case["operation"]
        result = case["expected_result"]
        unchanged = case["expected_unchanged"]

        self.assertIn("old_tag_a", history["context"]["fields"]["tags"]["choice_set"]["deprecated"])
        self.assertIn("before_removal", initial)
        self.assertIn("old_tag_a", initial["before_removal"]["tags"])
        self.assertIn("before_reintroduction", initial)
        self.assertNotIn("old_tag_a", initial["before_reintroduction"]["tags"])

        self.assertEqual(operation["kind"], "patch_sequence")
        steps = operation["steps"]
        self.assertGreaterEqual(len(steps), 2)
        self.assertNotIn("old_tag_a", steps[0]["set"]["tags"])
        self.assertIn("old_tag_a", steps[1]["set"]["tags"])
        self.assertEqual(result["removal_result"]["outcome"], "accepted")
        self.assertEqual(result["removal_result"]["stored"], initial["before_reintroduction"])
        self.assertEqual(result["rejected_step"], 1)
        self.assertEqual(result["error"], "INVALID_CHOICE")
        self.assertEqual(result["stored_after_rejected_attempt"], initial["before_reintroduction"])
        self.assertEqual(unchanged["stored_after_rejected_attempt"], initial["before_reintroduction"])

    def test_unchanged_invalid_history_is_invalid_against_declared_decimal(self):
        case = self.cases["T02-HIST-007"][1]
        history = self.documents["history.json"]
        field_key = "cost"
        definition = history["context"]["fields"].get(field_key)
        invalid_value = case["initial_state"].get(field_key)
        result = case["expected_result"]
        preserved_invalid = result["preserved_invalid"]

        self.assertIsNotNone(definition)
        self.assertEqual(definition["type"], "decimal")
        self.assertEqual(definition["scale"], 2)
        self.assertIsInstance(invalid_value, str)
        decimal_pattern = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
        self.assertIsNotNone(re.fullmatch(decimal_pattern, "12.30"))
        self.assertIsNone(re.fullmatch(decimal_pattern, invalid_value))
        self.assertNotIn(field_key, case["operation"]["set"])
        self.assertEqual(preserved_invalid["field"], field_key)
        self.assertEqual(preserved_invalid["value"], invalid_value)
        self.assertEqual(preserved_invalid["state"], "invalid")
        self.assertEqual(preserved_invalid["reason_codes"], ["INVALID_STORED_VALUE"])
        self.assertEqual(result["stored"][field_key], invalid_value)
        self.assertEqual(case["expected_unchanged"][field_key], invalid_value)

    def test_category_defaults_distinguish_omitted_from_explicit_empty(self):
        case = self.cases["T02-COMP-011"][1]
        operation = case["operation"]
        result = case["expected_result"]
        defaults = case["initial_state"]["category_default_memberships"]
        inputs = operation["creation_inputs"]
        outcomes = result["creation_outcomes"]

        self.assertEqual(inputs["omitted"]["fieldsets"], "omitted")
        self.assertEqual(inputs["explicit_empty"]["fieldsets"], [])
        self.assertEqual(result["outcome"], "accepted")
        self.assertIs(outcomes["omitted"]["create_consumed_defaults"], True)
        self.assertEqual(outcomes["omitted"]["memberships"], defaults)
        self.assertIs(outcomes["omitted"]["preview_token_required"], True)
        self.assertIs(outcomes["omitted"]["category_default_snapshot_revision_required"], True)
        self.assertIs(outcomes["explicit_empty"]["create_consumed_defaults"], False)
        self.assertEqual(outcomes["explicit_empty"]["memberships"], [])
        self.assertIs(outcomes["explicit_empty"]["preview_token_required"], False)
        self.assertIs(outcomes["explicit_empty"]["category_default_snapshot_revision_required"], False)
        self.assertIsNone(outcomes["explicit_empty"]["preview_token"])
        self.assertIsNone(outcomes["explicit_empty"]["category_default_snapshot_revision"])

    def test_history_cases_define_all_known_field_references(self):
        history = self.documents["history.json"]
        fields = history["context"]["fields"]
        case_fields = {
            "T02-HIST-007": ("cost", "owner_note", "status"),
            "T02-HIST-009": ("f3_location", "f4_cost"),
            "T02-HIST-012": ("f4_cost", "legacy_note"),
            "T02-HIST-015": ("status", "cost", "owner_note"),
        }
        for case_id, field_keys in case_fields.items():
            for field_key in field_keys:
                self.assertIn(field_key, fields, f"{case_id}: {field_key} lacks a local definition")
                self.assertIsInstance(fields[field_key], dict, f"{case_id}: malformed {field_key} definition")
        self.assertNotIn("ghost_key", fields)
        for case_id in ("T02-HIST-009", "T02-HIST-012"):
            initial = self.cases[case_id][1]["initial_state"]
            self.assertNotIn("f4_cost", initial["effective_field_keys"])
            self.assertIn("f4_cost", initial["removed_composition_field_keys"])

    def test_manifest_required_categories_are_covered(self):
        coverage = self.manifest["coverage"]
        for category in self.manifest["required_categories"]:
            value = coverage.get(category)
            self.assertIsNotNone(value, f"required category {category} missing")
            ids = list(_coverage_ids({category: value}))
            self.assertGreaterEqual(len(ids), 1, f"required category {category} empty")
            for case_id in ids:
                self.assertIn(case_id, self.cases, f"{category}: unknown case {case_id}")

    def test_manifest_covers_every_case_and_only_existing_cases(self):
        coverage = self.manifest["coverage"]
        referenced = set()
        for case_id in _coverage_ids(coverage):
            self.assertIn(case_id, self.cases, f"manifest references unknown case {case_id}")
            referenced.add(case_id)
        missing = set(self.cases) - referenced
        self.assertEqual(missing, set(), f"cases not covered by manifest: {sorted(missing)}")

    def test_manifest_corpus_files_all_exist(self):
        for name in self.manifest["corpus_files"]:
            self.assertIn(name, self.documents, name)
            self.assertTrue((FIXTURE_ROOT / name).is_file(), name)

    def test_corpus_is_implementation_independent(self):
        # Django may already be loaded by pytest; test the oracle's own imports
        # in a fresh interpreter instead of inspecting the runner's ambient state.
        probe = (
            "import json, runpy, sys; "
            "oracle = runpy.run_path(sys.argv[1], run_name='fixture_oracle'); "
            "oracle['_load'](); "
            "blocked = {'django', 'assets', 'itambox', 'extras', 'organization'}; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name.split('.', 1)[0] in blocked)))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", probe, str(Path(__file__).resolve())],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
