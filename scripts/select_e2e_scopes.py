#!/usr/bin/env python
"""Select fail-closed Playwright scopes from an exact Git change set.

The module intentionally depends only on the Python standard library so the
detector can run before application dependencies are installed.  The JSON
selection is the authoritative artifact; workflow outputs are only projections
of a selection which has already passed :func:`validate_selection`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = 1
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
RULE_ID_RE = re.compile(r"[a-z0-9][a-z0-9:-]*\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SPEC_SUFFIXES = (".spec.ts", ".test.ts")
CHANGE_STATUSES = frozenset({"A", "M", "D", "R", "C"})
SELECTIVE_EVENTS = frozenset({"pull_request"})
# merge_group carries no narrow PR range (the queue owns the synthetic head),
# so it is an authoritative full event like push/schedule/dispatch.
AUTHORITATIVE_FULL_EVENTS = frozenset(
    {"push", "schedule", "workflow_dispatch", "workflow_call", "release", "merge_group"}
)
KNOWN_EVENTS = SELECTIVE_EVENTS | AUTHORITATIVE_FULL_EVENTS
PROVENANCE_SCHEMA = 2

REQUIRED_APP_SCOPES = frozenset(
    {
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
    }
)
REQUIRED_CONTRACT_SCOPES = frozenset(
    {
        "contract:generic-object",
        "contract:tenant-isolation",
        "contract:auth-rbac",
        "contract:soft-delete",
        "contract:asset-custody",
        "contract:cross-app",
        "contract:jobs",
    }
)
REQUIRED_SHARED_SCOPES = frozenset({"smoke", "legacy-smoke", "layout", "a11y"})
REQUIRED_CATALOG_SCOPES = REQUIRED_APP_SCOPES | REQUIRED_CONTRACT_SCOPES | REQUIRED_SHARED_SCOPES | {"all"}

MAP_KEYS = frozenset(
    {
        "schema",
        "spec_root",
        "full_spec_path",
        "always_run_scopes",
        "full_scopes",
        "scopes",
        "rules",
        "known_production_roots",
        "rollback",
    }
)
SCOPE_KEYS = frozenset({"path", "kind", "owner"})
RULE_KEYS = frozenset({"id", "decision", "patterns", "scopes"})
ROLLBACK_KEYS = frozenset({"force_full_pr_selection", "switch_path"})
SELECTION_KEYS = frozenset(
    {
        "schema",
        "mode",
        "event_name",
        "base_sha",
        "head_sha",
        "merge_base_sha",
        "changed_path_digest",
        "scopes",
        "spec_paths",
        "reasons",
    }
)


class ScopeMapError(ValueError):
    """The repository-owned scope map is malformed or has drifted."""


class SelectionError(ValueError):
    """A change set or selection artifact cannot be trusted."""


@dataclass(frozen=True, order=True)
class ChangeRecord:
    """One normalized record from ``git diff --name-status -z``."""

    status: str
    old_path: str | None = None
    new_path: str | None = None

    def identities(self) -> tuple[str, ...]:
        values = [value for value in (self.old_path, self.new_path) if value is not None]
        return tuple(sorted(set(values)))

    def as_dict(self) -> dict[str, str | None]:
        return {"new_path": self.new_path, "old_path": self.old_path, "status": self.status}


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON representation used by policy artifacts."""

    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not permitted")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def load_json_file(path: str | Path, *, label: str = "JSON artifact") -> Any:
    """Load strict UTF-8 JSON, rejecting duplicate keys and non-finite numbers."""

    source = Path(path)
    try:
        raw = source.read_bytes()
    except (OSError, ValueError) as exc:
        raise SelectionError(f"cannot read {label} at {source}: {exc}") from exc
    if not raw:
        raise SelectionError(f"{label} at {source} is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SelectionError(f"{label} at {source} is not UTF-8") from exc
    if text.startswith("\ufeff"):
        raise SelectionError(f"{label} at {source} must not contain a UTF-8 BOM")
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SelectionError(f"cannot parse {label} at {source} as strict JSON: {exc}") from exc


def _expect_exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ValueError(f"{label} has invalid keys: {'; '.join(details)}")


def _expect_list(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _expect_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonblank string")
    if CONTROL_RE.search(value):
        raise ValueError(f"{label} contains a control character")
    return value


def _validate_relative_path(value: Any, label: str, *, allow_glob: bool = False) -> str:
    path = _expect_string(value, label)
    if "\\" in path:
        raise ValueError(f"{label} must use POSIX separators")
    if path.startswith("/") or re.match(r"[A-Za-z]:", path):
        raise ValueError(f"{label} must be repository-relative")
    if path.endswith("/") or "//" in path:
        raise ValueError(f"{label} is not normalized")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must not contain empty, dot, or parent segments")
    if not allow_glob and any(character in path for character in "*?["):
        raise ValueError(f"{label} must not contain glob metacharacters")
    return path


def _validate_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase 40-character Git SHA")
    return value


def _validate_digest(value: Any, label: str = "changed_path_digest") -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ValueError(f"{label} must be sha256 followed by a lowercase 64-character digest")
    return value


def _validate_sorted_unique_strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    rows = _expect_list(value, label, nonempty=nonempty)
    if any(not isinstance(row, str) or not row for row in rows):
        raise ValueError(f"{label} must contain nonblank strings")
    if rows != sorted(rows):
        raise ValueError(f"{label} must use canonical lexical sorting")
    if len(rows) != len(set(rows)):
        raise ValueError(f"{label} must not contain duplicates")
    for row in rows:
        if CONTROL_RE.search(row):
            raise ValueError(f"{label} contains a control character")
    return rows


def _validate_unique_strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    rows = _expect_list(value, label, nonempty=nonempty)
    if any(not isinstance(row, str) or not row or CONTROL_RE.search(row) for row in rows):
        raise ValueError(f"{label} must contain control-free nonblank strings")
    if len(rows) != len(set(rows)):
        raise ValueError(f"{label} must not contain duplicates")
    return rows


def _glob_star(pattern: str, index: int) -> tuple[str, int]:
    next_index = index + 1
    while next_index < len(pattern) and pattern[next_index] == "*":
        next_index += 1
    if next_index < len(pattern) and pattern[next_index] == "/":
        return "(?:[^/]+/)*", next_index + 1
    return ".*", next_index


def _glob_class(pattern: str, index: int) -> tuple[str, int]:
    end = pattern.find("]", index + 1)
    if end < 0:
        raise ValueError(f"glob pattern {pattern!r} has an unterminated character class")
    content = pattern[index + 1 : end]
    if not content or "/" in content or "\\" in content:
        raise ValueError(f"glob pattern {pattern!r} has an invalid character class")
    if content.startswith("!"):
        content = "^" + content[1:]
    elif content.startswith("^"):
        content = "\\" + content
    return "[" + content + "]", end + 1


def _glob_piece(pattern: str, index: int) -> tuple[str, int]:
    character = pattern[index]
    if character == "*":
        return _glob_star(pattern, index)
    if character == "?":
        return "[^/]", index + 1
    if character == "[":
        return _glob_class(pattern, index)
    return re.escape(character), index + 1


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile the small, path-aware glob grammar used by the scope map."""

    pieces = ["^"]
    index = 0
    while index < len(pattern):
        piece, index = _glob_piece(pattern, index)
        pieces.append(piece)
    pieces.append("$")
    return re.compile("".join(pieces))


def _matches(path: str, pattern: str) -> bool:
    return _glob_regex(pattern).fullmatch(path) is not None


def _literal_prefix(pattern: str) -> tuple[str, ...]:
    result = []
    for segment in pattern.split("/"):
        if any(character in segment for character in "*?["):
            break
        result.append(segment)
    return tuple(result)


def _patterns_may_overlap(first: str, second: str) -> bool:
    """Conservatively identify possible overlap for safe-ignore validation."""

    first_prefix = _literal_prefix(first)
    second_prefix = _literal_prefix(second)
    common = min(len(first_prefix), len(second_prefix))
    if first_prefix[:common] != second_prefix[:common]:
        return False
    if _matches(first, second) or _matches(second, first):
        return True
    # A shared literal prefix followed by a wildcard is deliberately treated as
    # overlap.  Safe-ignore declarations must be obviously disjoint from code.
    return True


def _pattern_covers(container: str, target: str) -> bool:
    if container == target:
        return True
    if not any(character in target for character in "*?[") and _matches(target, container):
        return True
    if container.endswith("/**"):
        prefix = container[:-3].rstrip("/")
        target_prefix = "/".join(_literal_prefix(target))
        return target_prefix == prefix or target_prefix.startswith(prefix + "/")
    return False


def _contained_path(root: Path, relative: str, label: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside {resolved_root}") from exc
    return candidate


def _discover_specs(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.name.endswith(SPEC_SUFFIXES) else []
    if not path.is_dir():
        return []
    return sorted(
        candidate for candidate in path.rglob("*") if candidate.is_file() and candidate.name.endswith(SPEC_SUFFIXES)
    )


def _validate_scope_entry(
    name: str,
    entry: Any,
    spec_root: Path,
    full_path: Path,
    require_spec_evidence: bool,
) -> str:
    _expect_string(name, "scope name")
    if not isinstance(entry, dict):
        raise ValueError(f"scope {name!r} must be an object")
    required = {"path", "kind"}
    if not required.issubset(entry) or not set(entry).issubset(SCOPE_KEYS):
        raise ValueError(f"scope {name!r} must contain path/kind and only supported keys")
    scope_path = _validate_relative_path(entry["path"], f"scope {name!r} path")
    kind = _expect_string(entry["kind"], f"scope {name!r} kind")
    if kind not in {"app", "contract", "qualification", "special", "workflow"}:
        raise ValueError(f"scope {name!r} has unsupported kind {kind!r}")
    if "owner" in entry:
        _expect_string(entry["owner"], f"scope {name!r} owner")
    target = _contained_path(spec_root, scope_path, f"scope {name!r} path")
    try:
        target.relative_to(full_path)
    except ValueError as exc:
        raise ValueError(f"scope {name!r} path must be beneath 'spec'") from exc
    if require_spec_evidence and not _discover_specs(target):
        raise ValueError(f"scope {name!r} path {scope_path!r} has no discovered .spec.ts/.test.ts evidence")
    return scope_path


def _validate_scope_entries(
    document: Mapping[str, Any], repo_root: str | Path, require_spec_evidence: bool
) -> tuple[dict[str, str], Path, Path]:
    spec_root_value = _validate_relative_path(document["spec_root"], "scope map spec_root")
    full_spec_path = _validate_relative_path(document["full_spec_path"], "scope map full_spec_path")
    spec_root = _contained_path(Path(repo_root), spec_root_value, "scope map spec_root")
    full_path = _contained_path(spec_root, full_spec_path, "scope map full_spec_path")
    scopes = document["scopes"]
    if not isinstance(scopes, dict) or not scopes:
        raise ValueError("scope map scopes must be a nonempty object")
    scope_paths = {
        name: _validate_scope_entry(name, entry, spec_root, full_path, require_spec_evidence)
        for name, entry in scopes.items()
    }
    return scope_paths, spec_root, full_path


def _validate_scope_lists(
    document: Mapping[str, Any], scopes: Mapping[str, Any], require_catalog_scopes: bool
) -> tuple[list[str], list[str]]:
    always = _validate_unique_strings(document["always_run_scopes"], "always_run_scopes", nonempty=True)
    full_scopes = _validate_unique_strings(document["full_scopes"], "full_scopes", nonempty=True)
    for name in always + full_scopes:
        if name not in scopes and not (not require_catalog_scopes and name == "all"):
            raise ValueError(f"declared scope {name!r} is not defined")
    return always, full_scopes


def _validate_rule(
    rule: Any, index: int, scopes: Mapping[str, Any], full_scopes: Sequence[str]
) -> tuple[str, str, list[tuple[str, str]], set[str]]:
    label = f"scope map rule {index}"
    if not isinstance(rule, dict):
        raise ValueError(f"{label} must be an object")
    required = {"id", "decision", "patterns"}
    if not required.issubset(rule) or not set(rule).issubset(RULE_KEYS):
        raise ValueError(f"{label} must contain id/decision/patterns and only supported keys")
    rule_id = _expect_string(rule["id"], f"{label} id")
    if not RULE_ID_RE.fullmatch(rule_id):
        raise ValueError(f"{label} id is not a stable policy token")
    decision = rule["decision"]
    if decision not in {"full", "selected", "safe_ignore"}:
        raise ValueError(f"rule {rule_id!r} has unsupported decision {decision!r}")
    patterns = _validate_unique_strings(rule["patterns"], f"rule {rule_id!r} patterns", nonempty=True)
    validated: list[tuple[str, str]] = []
    for pattern in patterns:
        _validate_relative_path(pattern, f"rule {rule_id!r} pattern", allow_glob=True)
        _glob_regex(pattern)
        validated.append((rule_id, pattern))
    selected_scope_owners: set[str] = set()
    selected = rule.get("scopes")
    if decision == "selected":
        selected = _validate_unique_strings(selected, f"rule {rule_id!r} scopes", nonempty=True)
        unknown = sorted(set(selected) - set(scopes))
        if unknown:
            raise ValueError(f"rule {rule_id!r} names unknown scopes {unknown}")
        forbidden = sorted(set(selected) & set(full_scopes))
        if forbidden:
            raise ValueError(f"selected rule {rule_id!r} names full-only scopes {forbidden}")
        selected_scope_owners.update(selected)
    elif selected is not None:
        raise ValueError(f"rule {rule_id!r} may not declare scopes for decision {decision!r}")
    return rule_id, decision, validated, selected_scope_owners


def _validate_rules(
    document: Mapping[str, Any], scopes: Mapping[str, Any], full_scopes: Sequence[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], set[str]]:
    rules = _expect_list(document["rules"], "scope map rules", nonempty=True)
    rule_ids: set[str] = set()
    non_safe: list[tuple[str, str]] = []
    safe: list[tuple[str, str]] = []
    selected_owners: set[str] = set()
    for index, rule in enumerate(rules):
        rule_id, decision, patterns, owners = _validate_rule(rule, index, scopes, full_scopes)
        if rule_id in rule_ids:
            raise ValueError(f"duplicate rule id {rule_id!r}")
        rule_ids.add(rule_id)
        (safe if decision == "safe_ignore" else non_safe).extend(patterns)
        selected_owners.update(owners)
    return safe, non_safe, selected_owners


def _validate_known_roots(document: Mapping[str, Any]) -> list[str]:
    roots = _validate_unique_strings(document["known_production_roots"], "known_production_roots", nonempty=True)
    for pattern in roots:
        _validate_relative_path(pattern, "known production root", allow_glob=True)
        _glob_regex(pattern)
    return roots


def _validate_safe_boundaries(
    safe: Sequence[tuple[str, str]], non_safe: Sequence[tuple[str, str]], roots: Sequence[str]
) -> None:
    for safe_id, safe_pattern in safe:
        for other_id, other_pattern in non_safe:
            if _patterns_may_overlap(safe_pattern, other_pattern):
                raise ValueError(
                    f"safe-ignore rule {safe_id!r} pattern {safe_pattern!r} may overlap "
                    f"non-safe rule {other_id!r} pattern {other_pattern!r}"
                )
        for root_pattern in roots:
            if _patterns_may_overlap(safe_pattern, root_pattern):
                raise ValueError(
                    f"safe-ignore rule {safe_id!r} pattern {safe_pattern!r} may overlap "
                    f"known production root {root_pattern!r}"
                )


def _validate_rollback(document: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> None:
    rollback = document["rollback"]
    if not isinstance(rollback, dict):
        raise ValueError("rollback must be an object")
    _expect_exact_keys(rollback, ROLLBACK_KEYS, "rollback")
    if type(rollback["force_full_pr_selection"]) is not bool:
        raise ValueError("rollback.force_full_pr_selection must be boolean")
    switch_path = _validate_relative_path(rollback["switch_path"], "rollback.switch_path")
    covered = any(
        rule["decision"] == "full" and any(_matches(switch_path, pattern) for pattern in rule["patterns"])
        for rule in rules
    )
    if not covered:
        raise ValueError("rollback.switch_path must be covered by an explicit full rule")


def _validate_owned_spec_paths(
    scope_paths: Mapping[str, str], spec_root: Path, full_path: Path, owner_names: set[str], label: str
) -> None:
    owner_root = full_path / ("apps" if label == "app" else "contracts")
    if not owner_root.is_dir():
        raise ValueError(f"catalog {label} spec root {owner_root} does not exist")
    roots = {name: _contained_path(spec_root, scope_paths[name], f"scope {name!r} path") for name in owner_names}
    for spec in _discover_specs(owner_root):
        owners = [name for name, root in roots.items() if spec == root or root in spec.parents]
        if len(owners) != 1:
            relative = spec.relative_to(spec_root).as_posix()
            raise ValueError(
                f"{label}-owned spec {relative!r} must have exactly one primary owner; found {sorted(owners)}"
            )


def _validate_catalog_basics(
    document: Mapping[str, Any], scope_paths: Mapping[str, str], always: Sequence[str], full_scopes: Sequence[str]
) -> None:
    missing = sorted(REQUIRED_CATALOG_SCOPES - set(scope_paths))
    if missing:
        raise ValueError(f"scope map is missing required catalog scopes {missing}")
    if list(always) != ["legacy-smoke", "smoke"]:
        raise ValueError("catalog always_run_scopes must contain exactly legacy-smoke and smoke")
    if list(full_scopes) != ["all"]:
        raise ValueError("catalog full_scopes must be ['all']")
    if document["spec_root"] != "itambox/tests/e2e" or document["full_spec_path"] != "spec":
        raise ValueError("catalog spec_root/full_spec_path must be itambox/tests/e2e and spec")
    if scope_paths["all"] != document["full_spec_path"]:
        raise ValueError("special scope 'all' must map to the complete spec path")


def _validate_catalog_owned_declarations(document: Mapping[str, Any]) -> None:
    expected_shared = {
        "a11y": ("spec/accessibility", "accessibility"),
        "layout": ("spec/layout", "layout"),
        "legacy-smoke": ("spec/legacy-smoke", "legacy-smoke"),
        "smoke": ("spec/smoke", "smoke"),
    }
    for name, (expected_path, owner) in expected_shared.items():
        entry = document["scopes"][name]
        if entry["path"] != expected_path or entry["kind"] != "qualification" or entry.get("owner") != owner:
            raise ValueError(f"scope {name!r} must explicitly own {expected_path}")
    for name in sorted(REQUIRED_APP_SCOPES):
        owner = name.split(":", 1)[1]
        entry = document["scopes"][name]
        expected_path = f"spec/apps/{owner}"
        if entry["path"] != expected_path or entry["kind"] != "app" or entry.get("owner") != owner:
            raise ValueError(f"scope {name!r} must explicitly own {expected_path}")
    for name in sorted(REQUIRED_CONTRACT_SCOPES):
        owner = name.split(":", 1)[1]
        entry = document["scopes"][name]
        expected_path = f"spec/contracts/{owner}"
        if entry["path"] != expected_path or entry["kind"] != "contract" or entry.get("owner") != owner:
            raise ValueError(f"scope {name!r} must explicitly own {expected_path}")


def _validate_catalog_scope_overlap(scope_paths: Mapping[str, str]) -> None:
    owned_names = sorted(REQUIRED_APP_SCOPES | REQUIRED_CONTRACT_SCOPES | REQUIRED_SHARED_SCOPES)
    for index, first_name in enumerate(owned_names):
        first_path = scope_paths[first_name]
        for second_name in owned_names[index + 1 :]:
            second_path = scope_paths[second_name]
            overlaps = (
                first_path == second_path
                or first_path.startswith(second_path + "/")
                or second_path.startswith(first_path + "/")
            )
            if overlaps:
                raise ValueError(
                    f"scope ownership paths overlap: {first_name!r}={first_path!r}, {second_name!r}={second_path!r}"
                )


def _validate_catalog_contract(
    document: Mapping[str, Any],
    scope_paths: Mapping[str, str],
    spec_root: Path,
    full_path: Path,
    always: Sequence[str],
    full_scopes: Sequence[str],
    selected_scope_owners: set[str],
    require_spec_evidence: bool,
) -> None:
    _validate_catalog_basics(document, scope_paths, always, full_scopes)
    _validate_catalog_owned_declarations(document)
    _validate_catalog_scope_overlap(scope_paths)
    if require_spec_evidence:
        _validate_owned_spec_paths(scope_paths, spec_root, full_path, REQUIRED_APP_SCOPES, "app")
        _validate_owned_spec_paths(scope_paths, spec_root, full_path, REQUIRED_CONTRACT_SCOPES, "contract")
    missing_rules = sorted((REQUIRED_APP_SCOPES | REQUIRED_CONTRACT_SCOPES) - selected_scope_owners)
    if missing_rules:
        raise ValueError(f"app/contract scopes have no explicit selection rule: {missing_rules}")


def validate_scope_map(
    document: Any,
    repo_root: str | Path,
    require_catalog_scopes: bool = True,
    *,
    require_spec_evidence: bool = True,
) -> dict[str, Any]:
    """Validate the complete scope-map policy and its on-disk spec evidence."""

    try:
        if not isinstance(document, dict):
            raise ValueError("scope map must be a JSON object")
        _expect_exact_keys(document, MAP_KEYS, "scope map")
        if type(document["schema"]) is not int or document["schema"] != SCHEMA:
            raise ValueError(f"scope map schema must be {SCHEMA}")
        scope_paths, spec_root, full_path = _validate_scope_entries(document, repo_root, require_spec_evidence)
        always, full_scopes = _validate_scope_lists(document, document["scopes"], require_catalog_scopes)
        safe_patterns, non_safe_patterns, selected_owners = _validate_rules(document, document["scopes"], full_scopes)
        roots = _validate_known_roots(document)
        _validate_safe_boundaries(safe_patterns, non_safe_patterns, roots)
        _validate_rollback(document, document["rules"])
        if require_catalog_scopes:
            _validate_catalog_contract(
                document,
                scope_paths,
                spec_root,
                full_path,
                always,
                full_scopes,
                selected_owners,
                require_spec_evidence,
            )
        return document
    except ScopeMapError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ScopeMapError(str(exc)) from exc


def load_scope_map(
    path: str | Path,
    repo_root: str | Path,
    *,
    require_catalog_scopes: bool = True,
    require_spec_evidence: bool = True,
) -> dict[str, Any]:
    """Load a strict-JSON ``.yaml`` scope map and validate it."""

    try:
        document = load_json_file(path, label="E2E scope map")
    except SelectionError as exc:
        raise ScopeMapError(str(exc)) from exc
    return validate_scope_map(
        document,
        repo_root,
        require_catalog_scopes=require_catalog_scopes,
        require_spec_evidence=require_spec_evidence,
    )


def _change_from_mapping(value: Mapping[str, Any]) -> ChangeRecord:
    allowed = {"status", "old_path", "new_path"}
    if not set(value).issubset(allowed) or "status" not in value:
        raise SelectionError("change records may contain only status, old_path, and new_path")
    raw_status = value["status"]
    if not isinstance(raw_status, str):
        raise SelectionError("change status must be a string")
    match = re.fullmatch(r"([AMDCR]|[RC]\d{1,3})", raw_status)
    if match is None:
        raise SelectionError(f"unsupported Git change status {raw_status!r}")
    if raw_status[0] in {"R", "C"} and len(raw_status) > 1 and int(raw_status[1:]) > 100:
        raise SelectionError(f"unsupported Git similarity score {raw_status!r}")
    old_path, new_path = value.get("old_path"), value.get("new_path")
    try:
        old_path = _validate_relative_path(old_path, "old changed path") if old_path is not None else None
        new_path = _validate_relative_path(new_path, "new changed path") if new_path is not None else None
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    return ChangeRecord(status=raw_status[0], old_path=old_path, new_path=new_path)


def _validate_change_shape(record: ChangeRecord) -> ChangeRecord:
    expected = {
        "A": (False, True),
        "M": (False, True),
        "D": (True, False),
        "R": (True, True),
        "C": (True, True),
    }
    if record.status not in expected:
        raise SelectionError(f"unsupported Git change status {record.status!r}")
    identities = (record.old_path is not None, record.new_path is not None)
    if identities != expected[record.status]:
        raise SelectionError(f"status {record.status!r} has an invalid old/new path identity")
    try:
        for path, label in ((record.old_path, "old changed path"), (record.new_path, "new changed path")):
            if path is not None:
                _validate_relative_path(path, label)
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    return record


def _normalise_change(value: ChangeRecord | Mapping[str, Any]) -> ChangeRecord:
    if isinstance(value, ChangeRecord):
        return _validate_change_shape(value)
    if isinstance(value, Mapping):
        return _validate_change_shape(_change_from_mapping(value))
    raise SelectionError("change records must be mappings or ChangeRecord values")


def _parse_git_status(status_token: str) -> str:
    match = re.fullmatch(r"([AMD]|[RC]\d{1,3})", status_token)
    if match is None:
        raise SelectionError(f"unsupported or malformed Git status {status_token!r}")
    if status_token[0] in {"R", "C"} and int(status_token[1:]) > 100:
        raise SelectionError(f"unsupported Git similarity score {status_token!r}")
    return status_token[0]


def _change_from_git_tokens(status: str, paths: Sequence[str]) -> ChangeRecord:
    expected_count = 2 if status in {"R", "C"} else 1
    if len(paths) != expected_count:
        raise SelectionError(f"Git status {status!r} is missing a path identity")
    if status == "D":
        raw = {"status": status, "old_path": paths[0]}
    elif status in {"R", "C"}:
        raw = {"status": status, "old_path": paths[0], "new_path": paths[1]}
    else:
        raw = {"status": status, "new_path": paths[0]}
    return _normalise_change(raw)


def parse_name_status_z(data: bytes) -> list[ChangeRecord]:
    """Parse NUL-delimited ``git --name-status -z -M -C`` output."""

    if not isinstance(data, bytes):
        raise SelectionError("Git name-status input must be bytes")
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise SelectionError("Git name-status output is not NUL terminated")
    try:
        tokens = data.decode("utf-8", errors="strict").split("\0")[:-1]
    except UnicodeDecodeError as exc:
        raise SelectionError("Git path output is not valid UTF-8") from exc
    result: list[ChangeRecord] = []
    index = 0
    while index < len(tokens):
        status = _parse_git_status(tokens[index])
        index += 1
        path_count = 2 if status in {"R", "C"} else 1
        paths = tokens[index : index + path_count]
        index += path_count
        result.append(_change_from_git_tokens(status, paths))
    return result


def changed_path_digest(changes: Iterable[ChangeRecord | Mapping[str, Any]]) -> str:
    """Hash a canonical sorted status/old/new change identity list."""

    records = sorted(
        (_normalise_change(change) for change in changes),
        key=lambda row: (row.status, row.old_path or "", row.new_path or ""),
    )
    payload = json.dumps(
        [record.as_dict() for record in records],
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _rule_specificity(rule: Mapping[str, Any], path: str) -> tuple[int, int]:
    matching = [pattern for pattern in rule["patterns"] if _matches(path, pattern)]
    if not matching:
        return (-1, -1)
    return max((len(_literal_prefix(pattern)), len(pattern)) for pattern in matching)


def _classify_identity(
    document: Mapping[str, Any],
    path: str,
    status: str,
    *,
    old_path: str | None,
    new_path: str | None,
) -> dict[str, Any]:
    matches = [rule for rule in document["rules"] if any(_matches(path, pattern) for pattern in rule["patterns"])]
    priority = {"safe_ignore": 1, "selected": 2, "full": 3}
    if matches:
        highest = max(priority[rule["decision"]] for rule in matches)
        candidates = [rule for rule in matches if priority[rule["decision"]] == highest]
        best_specificity = max(_rule_specificity(rule, path) for rule in candidates)
        winners = [rule for rule in candidates if _rule_specificity(rule, path) == best_specificity]
        signatures = {(rule["decision"], tuple(rule.get("scopes", []))) for rule in winners}
        if len(signatures) != 1:
            raise SelectionError(f"path {path!r} has ambiguous primary classifications")
        rule = sorted(winners, key=lambda item: item["id"])[0]
        reason: dict[str, Any] = {
            "matched_rule": rule["id"],
            "new_path": new_path,
            "old_path": old_path,
            "path": path,
            "status": status,
        }
        if rule["decision"] == "full":
            reason["escalation"] = "full"
        elif rule["decision"] == "safe_ignore":
            reason["safe_ignore"] = True
        else:
            reason["selected"] = sorted(rule["scopes"])
        return reason

    known = any(_matches(path, pattern) for pattern in document["known_production_roots"])
    return {
        "escalation": "full",
        "matched_rule": "unknown-production" if known else "unknown-path",
        "new_path": new_path,
        "old_path": old_path,
        "path": path,
        "status": status,
    }


def _selection_reason_sort_key(reason: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(reason.get("path", "")), str(reason.get("status", "")), str(reason.get("matched_rule", "")))


def _normalise_changes(changes: Iterable[ChangeRecord | Mapping[str, Any]]) -> list[ChangeRecord]:
    normalized = sorted(
        (_normalise_change(change) for change in changes),
        key=lambda row: (row.status, row.old_path or "", row.new_path or ""),
    )
    identities = [(record.status, path) for record in normalized for path in record.identities()]
    if len(identities) != len(set(identities)):
        raise SelectionError("change set contains duplicate status/path identities")
    return normalized


def _build_reasons(document: Mapping[str, Any], records: Sequence[ChangeRecord]) -> list[dict[str, Any]]:
    reasons = [
        _classify_identity(
            document,
            path,
            record.status,
            old_path=record.old_path,
            new_path=record.new_path,
        )
        for record in records
        for path in record.identities()
    ]
    return sorted(reasons, key=_selection_reason_sort_key)


def _selection_mode(
    document: Mapping[str, Any],
    event_name: str,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str,
    records: Sequence[ChangeRecord],
    reasons: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    empty_diff = not records
    if empty_diff and event_name in SELECTIVE_EVENTS and not (base_sha == head_sha == merge_base_sha):
        raise SelectionError("an empty pull-request diff is inconsistent with distinct base/head identity")
    rollback_full = bool(document["rollback"]["force_full_pr_selection"] and event_name in SELECTIVE_EVENTS)
    if rollback_full:
        full_reasons = [
            {
                "escalation": "full",
                "matched_rule": "rollback-force-full",
                "new_path": record.new_path,
                "old_path": record.old_path,
                "path": path,
                "status": record.status,
            }
            for record in records
            for path in record.identities()
        ]
        return "full", sorted(full_reasons, key=_selection_reason_sort_key)
    if event_name in AUTHORITATIVE_FULL_EVENTS or empty_diff:
        return "full", list(reasons)
    if any(reason.get("escalation") == "full" for reason in reasons):
        return "full", list(reasons)
    if reasons and all(reason.get("safe_ignore") is True for reason in reasons):
        return "none", list(reasons)
    return "selected", list(reasons)


def _selection_paths(
    document: Mapping[str, Any], mode: str, reasons: Sequence[Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    if mode == "full":
        return sorted(document["full_scopes"]), [document["full_spec_path"]]
    if mode == "none":
        return [], []
    scope_set = set(document["always_run_scopes"])
    for reason in reasons:
        scope_set.update(reason.get("selected", []))
    scopes = sorted(scope_set)
    paths = sorted({document["scopes"][scope]["path"] for scope in scopes})
    return scopes, paths


def build_selection(
    document: Mapping[str, Any],
    repo_root: str | Path,
    *,
    event_name: str,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str,
    changes: Iterable[ChangeRecord | Mapping[str, Any]],
) -> dict[str, Any]:
    """Build and validate a deterministic selection from already-resolved inputs."""

    try:
        _validate_selection_identities(event_name, base_sha, head_sha, merge_base_sha)
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    normalized = _normalise_changes(changes)
    digest = changed_path_digest(normalized)
    reasons = _build_reasons(document, normalized)
    mode, reasons = _selection_mode(document, event_name, base_sha, head_sha, merge_base_sha, normalized, reasons)
    scopes, spec_paths = _selection_paths(document, mode, reasons)
    selection = {
        "schema": SCHEMA,
        "mode": mode,
        "event_name": event_name,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "changed_path_digest": digest,
        "scopes": scopes,
        "spec_paths": spec_paths,
        "reasons": reasons,
    }
    validate_selection(selection, repo_root, document)
    return selection


def _validate_selection_identities(event_name: str, base_sha: str, head_sha: str, merge_base_sha: str) -> None:
    if event_name not in KNOWN_EVENTS:
        raise ValueError(f"unsupported E2E event {event_name!r}")
    for value, label in ((base_sha, "base_sha"), (head_sha, "head_sha"), (merge_base_sha, "merge_base_sha")):
        _validate_sha(value, label)


def _validate_reason_identities(reason: Mapping[str, Any]) -> None:
    _validate_relative_path(reason["path"], "selection reason path")
    for key in ("old_path", "new_path"):
        value = reason[key]
        if value is not None:
            _validate_relative_path(value, f"selection reason {key}")
    status = reason["status"]
    if status not in CHANGE_STATUSES:
        raise ValueError("selection reason has invalid status")
    expected = {
        "A": (None, reason["new_path"]),
        "M": (None, reason["new_path"]),
        "D": (reason["old_path"], None),
        "R": (reason["old_path"], reason["new_path"]),
        "C": (reason["old_path"], reason["new_path"]),
    }[status]
    if (reason["old_path"], reason["new_path"]) != expected:
        raise ValueError(f"selection reason has invalid old/new identities for {status}")
    if reason["path"] not in {reason["old_path"], reason["new_path"]}:
        raise ValueError("selection reason path must be one of its old/new identities")


def _validate_reason_decision(
    reason: Mapping[str, Any],
    document: Mapping[str, Any],
    aggregate_scopes: set[str],
    *,
    allow_narrow_full_reason: bool,
) -> None:
    variant = set(reason) - {"new_path", "old_path", "path", "status", "matched_rule"}
    if variant == {"selected"}:
        selected = _validate_sorted_unique_strings(reason["selected"], "reason selected scopes", nonempty=True)
        if not allow_narrow_full_reason and not set(selected).issubset(aggregate_scopes):
            raise ValueError("selection reason names a scope absent from the aggregate selection")
        return
    if variant == {"safe_ignore"}:
        if reason["safe_ignore"] is not True:
            raise ValueError("safe-ignore reason must contain true")
        return
    if variant != {"escalation"} or reason["escalation"] != "full":
        raise ValueError("selection reason has invalid decision keys")


def _validate_reason(
    reason: Any,
    document: Mapping[str, Any],
    aggregate_scopes: set[str],
    *,
    allow_narrow_full_reason: bool = False,
) -> None:
    if not isinstance(reason, dict):
        raise ValueError("selection reasons must be objects")
    base = {"new_path", "old_path", "path", "status", "matched_rule"}
    if not base.issubset(reason):
        raise ValueError("selection reason has invalid keys")
    _validate_reason_identities(reason)
    rule_ids = {rule["id"] for rule in document["rules"]} | {
        "unknown-production",
        "unknown-path",
        "rollback-force-full",
    }
    if reason["matched_rule"] not in rule_ids:
        raise ValueError(f"selection reason names unknown rule {reason['matched_rule']!r}")
    _validate_reason_decision(
        reason,
        document,
        aggregate_scopes,
        allow_narrow_full_reason=allow_narrow_full_reason,
    )


def _selection_header(
    selection: Mapping[str, Any], document: Mapping[str, Any]
) -> tuple[str, list[str], list[str], list[Any]]:
    _expect_exact_keys(selection, SELECTION_KEYS, "selection")
    if type(selection["schema"]) is not int or selection["schema"] != SCHEMA:
        raise ValueError(f"selection schema must be {SCHEMA}")
    mode = selection["mode"]
    if mode not in {"none", "selected", "full"}:
        raise ValueError(f"selection mode {mode!r} is unsupported")
    if selection["event_name"] not in KNOWN_EVENTS:
        raise ValueError(f"selection event {selection['event_name']!r} is unsupported")
    for key in ("base_sha", "head_sha", "merge_base_sha"):
        _validate_sha(selection[key], key)
    _validate_digest(selection["changed_path_digest"])
    scopes = _validate_sorted_unique_strings(selection["scopes"], "selection scopes")
    spec_paths = _validate_sorted_unique_strings(selection["spec_paths"], "selection spec_paths")
    unknown = sorted(set(scopes) - set(document["scopes"]) - set(document["full_scopes"]))
    if unknown:
        raise ValueError(f"selection contains unknown scopes {unknown}")
    reasons = _expect_list(selection["reasons"], "selection reasons")
    if reasons != sorted(reasons, key=_selection_reason_sort_key):
        raise ValueError("selection reasons must use canonical path/status/rule sorting")
    for reason in reasons:
        _validate_reason(reason, document, set(scopes), allow_narrow_full_reason=mode == "full")
    return mode, scopes, spec_paths, reasons


def _validate_none_selection(scopes: Sequence[str], spec_paths: Sequence[str], reasons: Sequence[Any]) -> None:
    if scopes or spec_paths:
        raise ValueError("none selection must have no scopes or spec paths")
    if any(reason.get("safe_ignore") is not True for reason in reasons):
        raise ValueError("none selection may contain only safe-ignore reasons")


def _validate_full_selection(
    scopes: Sequence[str],
    spec_paths: Sequence[str],
    document: Mapping[str, Any],
    repo_root: str | Path,
) -> None:
    if list(scopes) != sorted(document["full_scopes"]):
        raise ValueError("full selection must contain exactly the configured full scopes")
    if list(spec_paths) != [document["full_spec_path"]]:
        raise ValueError("full selection must contain exactly the complete spec root")
    spec_root = _contained_path(Path(repo_root), document["spec_root"], "scope map spec_root")
    if not _discover_specs(_contained_path(spec_root, spec_paths[0], "full spec path")):
        raise ValueError("full selection has no discoverable spec evidence")


def _validate_selected_selection(
    scopes: Sequence[str],
    spec_paths: Sequence[str],
    reasons: Sequence[Any],
    document: Mapping[str, Any],
    repo_root: str | Path,
) -> None:
    if not scopes or not spec_paths:
        raise ValueError("selected mode requires nonempty scopes and spec paths")
    if not set(document["always_run_scopes"]).issubset(scopes):
        raise ValueError("selected mode is missing an always-run product scope")
    expected_paths = sorted({document["scopes"][scope]["path"] for scope in scopes})
    if list(spec_paths) != expected_paths:
        raise ValueError("selected spec paths do not exactly match selected scope paths")
    spec_root = _contained_path(Path(repo_root), document["spec_root"], "scope map spec_root")
    for relative in spec_paths:
        _validate_relative_path(relative, "selected spec path")
        target = _contained_path(spec_root, relative, "selected spec path")
        if not _discover_specs(target):
            raise ValueError(f"selected spec path {relative!r} has no discoverable tests")
    selected_reason_scopes = {scope for reason in reasons for scope in reason.get("selected", [])}
    if not selected_reason_scopes:
        raise ValueError("selected mode has no product classification reason")


def validate_selection(selection: Any, repo_root: str | Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate canonical selection structure, identities, modes, and spec paths."""

    try:
        if not isinstance(selection, dict):
            raise ValueError("selection must be a JSON object")
        mode, scopes, spec_paths, reasons = _selection_header(selection, document)
        if mode == "none":
            _validate_none_selection(scopes, spec_paths, reasons)
        elif mode == "full":
            _validate_full_selection(scopes, spec_paths, document, repo_root)
        else:
            _validate_selected_selection(scopes, spec_paths, reasons, document, repo_root)
        return selection
    except SelectionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectionError(str(exc)) from exc


def load_selection(path: str | Path, repo_root: str | Path, document: Mapping[str, Any]) -> dict[str, Any]:
    selection = load_json_file(path, label="E2E selection")
    validate_selection(selection, repo_root, document)
    if canonical_json(selection).encode("utf-8") != Path(path).read_bytes():
        raise SelectionError("E2E selection artifact is not canonical JSON")
    return selection


def _git(repo_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise SelectionError(f"cannot execute Git: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = message[0][:300] if message else "unknown Git error"
        raise SelectionError(f"Git command failed: {detail}")
    return result.stdout


def _resolve_commit(repo_root: Path, sha: str, label: str) -> str:
    try:
        _validate_sha(sha, label)
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    resolved = _git(repo_root, ["rev-parse", "--verify", f"{sha}^{{commit}}"])
    try:
        value = resolved.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SelectionError(f"Git returned a non-ASCII {label}") from exc
    if value != sha:
        raise SelectionError(f"{label} did not resolve to the exact requested commit object")
    return value


def collect_git_changes(
    repo_root: str | Path,
    *,
    event_name: str,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str | None = None,
) -> tuple[str, list[ChangeRecord]]:
    """Resolve exact commit objects and collect a merge-base-aware change set."""

    root = Path(repo_root).resolve()
    if event_name not in KNOWN_EVENTS:
        raise SelectionError(f"unsupported E2E event {event_name!r}")
    _resolve_commit(root, base_sha, "base_sha")
    _resolve_commit(root, head_sha, "head_sha")
    raw_merge = _git(root, ["merge-base", "--", base_sha, head_sha])
    try:
        actual_merge = raw_merge.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SelectionError("Git returned a non-ASCII merge base") from exc
    try:
        _validate_sha(actual_merge, "computed merge_base_sha")
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    if merge_base_sha is not None and merge_base_sha != actual_merge:
        raise SelectionError("provided merge_base_sha does not equal Git's exact merge base")
    _resolve_commit(root, actual_merge, "merge_base_sha")
    raw_changes = _git(root, ["diff", "--name-status", "-z", "-M", "-C", actual_merge, head_sha, "--"])
    return actual_merge, parse_name_status_z(raw_changes)


def build_e2e_identity(
    repo_root: str | Path,
    *,
    event_name: str,
    base_sha: str,
    head_sha: str,
    checkout_sha: str,
    synthetic_merge_sha: str | None = None,
) -> dict[str, str | None]:
    """Derive the canonical E2E provenance from independent Git inputs."""

    if event_name not in KNOWN_EVENTS:
        raise SelectionError(f"unsupported E2E event {event_name!r}")
    try:
        _validate_sha(checkout_sha, "tested checkout SHA")
        if synthetic_merge_sha is not None:
            _validate_sha(synthetic_merge_sha, "synthetic merge SHA")
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    if event_name == "pull_request":
        if synthetic_merge_sha is None:
            raise SelectionError("pull-request provenance is missing synthetic merge SHA")
        if checkout_sha != head_sha:
            raise SelectionError("pull-request E2E checkout must be the raw PR head")
    elif synthetic_merge_sha is not None:
        raise SelectionError("non-pull-request provenance must not contain synthetic merge SHA")
    if checkout_sha != head_sha:
        raise SelectionError("tested checkout SHA must equal the selected PR head")

    merge_base_sha, changes = collect_git_changes(
        repo_root,
        event_name=event_name,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    return {
        "provenance_schema": PROVENANCE_SCHEMA,
        "event_name": event_name,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "changed_path_digest": changed_path_digest(changes),
        "tested_checkout_sha": checkout_sha,
        "tested_checkout_kind": "head",
        "synthetic_merge_sha": synthetic_merge_sha,
    }


def _write_canonical(path: str | Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json(value)
    target.write_text(content, encoding="utf-8", newline="\n")
    if target.read_bytes() != content.encode("utf-8"):
        raise SelectionError(f"failed to verify canonical output at {target}")


def selection_summary(selection: Mapping[str, Any]) -> str:
    lines = [
        f"## E2E selection: {selection['mode']}",
        "",
        f"- Event: `{selection['event_name']}`",
        f"- Base: `{selection['base_sha']}`",
        f"- Head: `{selection['head_sha']}`",
        f"- Merge base: `{selection['merge_base_sha']}`",
        f"- Changed identities: {len(selection['reasons'])}",
        f"- Scopes: {', '.join(selection['scopes']) if selection['scopes'] else '(none)'}",
        f"- Spec paths: {', '.join(selection['spec_paths']) if selection['spec_paths'] else '(none)'}",
    ]
    if selection["reasons"]:
        lines.extend(["", "### Classification", ""])
        for reason in selection["reasons"]:
            if reason.get("escalation") == "full":
                outcome = "full"
            elif reason.get("safe_ignore") is True:
                outcome = "safe-ignore"
            else:
                outcome = ", ".join(reason["selected"])
            lines.append(f"- `{reason['status']} {reason['path']}` -> `{reason['matched_rule']}` ({outcome})")
    return "\n".join(lines) + "\n"


def _append_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def _github_outputs(selection: Mapping[str, Any], artifact_path: Path) -> str:
    values = {
        "base_sha": selection["base_sha"],
        "changed_path_digest": selection["changed_path_digest"],
        "head_sha": selection["head_sha"],
        "merge_base_sha": selection["merge_base_sha"],
        "mode": selection["mode"],
        "scopes_json": json.dumps(selection["scopes"], separators=(",", ":")),
        "selection_artifact": artifact_path.as_posix(),
        "selection_valid": "true",
        "spec_paths_json": json.dumps(selection["spec_paths"], separators=(",", ":")),
    }
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select and validate fail-closed E2E scopes from exact Git base/head commits."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    parser.add_argument(
        "--scope-map",
        type=Path,
        default=Path("scripts/e2e_scope_map.yaml"),
        help="strict-JSON scope map (relative paths are resolved beneath --repo-root)",
    )
    parser.add_argument(
        "--event-name", required=True, choices=sorted(KNOWN_EVENTS), help="GitHub event policy identity"
    )
    parser.add_argument("--base-sha", required=True, help="exact 40-character base commit SHA")
    parser.add_argument("--head-sha", required=True, help="exact 40-character head commit SHA")
    parser.add_argument("--merge-base-sha", help="optional expected merge base; mismatch fails closed")
    parser.add_argument(
        "--output",
        "--selection-output",
        dest="output",
        type=Path,
        required=True,
        help="canonical selection JSON artifact to create",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None,
        help="optional GitHub output file to append validated scalar projections",
    )
    parser.add_argument(
        "--github-step-summary",
        "--summary",
        dest="summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None,
        help="optional Markdown job-summary file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    scope_map_path = args.scope_map if args.scope_map.is_absolute() else repo_root / args.scope_map
    output_path = args.output if args.output.is_absolute() else repo_root / args.output
    try:
        scope_map = load_scope_map(scope_map_path, repo_root)
        merge_base, changes = collect_git_changes(
            repo_root,
            event_name=args.event_name,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            merge_base_sha=args.merge_base_sha,
        )
        selection = build_selection(
            scope_map,
            repo_root,
            event_name=args.event_name,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            merge_base_sha=merge_base,
            changes=changes,
        )
        _write_canonical(output_path, selection)
        load_selection(output_path, repo_root, scope_map)
        if args.github_output:
            _append_text(args.github_output, _github_outputs(selection, output_path))
        summary = selection_summary(selection)
        if args.summary:
            _append_text(args.summary, summary)
        print(summary, end="")
        return 0
    except (ScopeMapError, SelectionError, OSError) as exc:
        print(f"E2E scope selection failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
