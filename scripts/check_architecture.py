#!/usr/bin/env python
"""Fail-closed architecture boundary gate: layering, direction, and cycles.

ITAMbox's structure is declared in ``scripts/architecture_policy.py``: a layer
for every first-party module and a matrix saying which layer may import which.
This gate builds the first-party import graph twice -- once from module-top
imports and once including function-body imports -- and blocks on an import
cycle or on an edge the matrix forbids.

Both graphs block from the first run. Moving an import into a function defers
*when* a coupling happens, not *whether* it exists, so a violation that is only
visible once deferred imports are counted is still a violation. ``TYPE_CHECKING``
imports are in neither graph; they never execute, and a typing-only back edge is
the sanctioned fix for a real cycle.

Accepted debt lives in ``scripts/architecture_baseline.json``. Every row carries
an owning ``area:*`` label and a machine-readable ``disposition``: ``debt`` rows
also record the tracking issue that will remove them and a stated removal
direction, so the baseline is a work list rather than a suppression file, while
``accepted`` rows record a stable rationale and no false removal promise. The
removal issues of every ``debt`` row must be the open ones recorded in the
reviewed ``scripts/architecture_issue_states.json`` snapshot, refreshed with
``--refresh-issue-states``; the gate itself never reaches the network. Two
things can never enter the baseline: a ``domain-model -> presentation`` edge,
which has no representation at any severity, and a newly observed identity,
which has to be hand-reviewed into the file rather than absorbed by
``--write-baseline``.

Three exit codes, and they mean different things. ``0`` is a clean graph. ``1``
is a policy regression a contributor fixes. ``2`` is a result nobody should
trust: a malformed baseline, a drifted policy fingerprint, an unclassifiable
module, a source file that will not parse, or the wrong interpreter.
"""

import argparse
import collections
import contextlib
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from scripts.architecture_policy import (
        ABSOLUTE_FORBIDDEN,
        AREA_LABELS,
        CANONICAL_PYTHON,
        DEFAULT_TARGETS,
        EDGE_KINDS,
        GRAPH_NAMES,
        MATRIX_RULES,
        MODULE_TOP,
        SCHEMA_VERSION,
        STRUCTURAL_RULES,
        PolicyError,
        adjacency_for,
        build_graph,
        classify_claim,
        component_memberships,
        compute_policy_fingerprint,
        exception_id,
        find_cycles,
        is_allowed,
        layer_of,
        owner_for_modules,
        source_module_for_path,
        validate_policy,
    )
except ModuleNotFoundError:  # direct execution puts scripts/ on sys.path, not the repository root
    from architecture_policy import (
        ABSOLUTE_FORBIDDEN,
        AREA_LABELS,
        CANONICAL_PYTHON,
        DEFAULT_TARGETS,
        EDGE_KINDS,
        GRAPH_NAMES,
        MATRIX_RULES,
        MODULE_TOP,
        SCHEMA_VERSION,
        STRUCTURAL_RULES,
        PolicyError,
        adjacency_for,
        build_graph,
        classify_claim,
        component_memberships,
        compute_policy_fingerprint,
        exception_id,
        find_cycles,
        is_allowed,
        layer_of,
        owner_for_modules,
        source_module_for_path,
        validate_policy,
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "architecture_baseline.json"
ISSUE_STATES_PATH = REPO_ROOT / "scripts" / "architecture_issue_states.json"

BASELINE_SECTIONS = ("cycles", "layer_exceptions", "unsupported_cycle_claims")
HEADER_FIELDS = ("schema_version", "canonical_python", "policy_sha256")

ISSUE_STATES_SCHEMA_VERSION = 1

# Every row declares how the gate may treat it. ``debt`` promises removal
# through a live tracking issue; ``accepted`` records intentional architecture
# with a stable rationale and no removal promise. Cycles and unsupported cycle
# claims can never be accepted: they are findings, not policy cells.
DISPOSITIONS = frozenset({"debt", "accepted"})
DEBT_DISPOSITION = "debt"
ACCEPTED_DISPOSITION = "accepted"
SCAFFOLD_DISPOSITION = "TODO"

CYCLE_FIELDS = (
    "id",
    "graph",
    "modules",
    "edges",
    "owner",
    "disposition",
    "removal_issue",
    "removal_direction",
    "accepted_reason",
)
OPTIONAL_CYCLE_FIELDS = ("notes",)
EXCEPTION_FIELDS = (
    "id",
    "rule",
    "source",
    "source_layer",
    "target",
    "target_layer",
    "kind",
    "count",
    "owner",
    "disposition",
    "removal_issue",
    "removal_direction",
    "accepted_reason",
)
EXCEPTION_FIELDS_ACCEPTED = tuple(
    field for field in EXCEPTION_FIELDS if field not in ("removal_issue", "removal_direction")
)
CLAIM_FIELDS = (
    "id",
    "source",
    "path",
    "scope",
    "statement",
    "targets",
    "owner",
    "disposition",
    "removal_issue",
    "removal_direction",
)

# Fields a machine must never author. ``--write-baseline`` carries them forward
# verbatim and the scaffold writes sentinels the loader refuses.
HUMAN_FIELDS = ("removal_issue", "removal_direction", "accepted_reason", "notes", "disposition")
SCAFFOLD_ISSUE = 0
SCAFFOLD_DIRECTION = "TODO"
MINIMUM_REMOVAL_DIRECTION = 40
PLACEHOLDER_DIRECTIONS = frozenset({"tbd", "todo", "n/a", "na", "none", "unknown", "fixme"})

REPORT_ONLY_BANNER = "REPORT ONLY — NOT A PASS"

# Documentation the gate holds to R-DOC1. The public tree only contains the
# root maintainer docs listed here; the internal development prose lives in the
# private itambox-community/design-docs repository and is not part of this link gate.
LINKED_DOC_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "DEVELOPMENT.md", "README.md", "SECURITY.md")
LINKED_DOC_GLOBS = ()
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SKIPPED_LINK_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")

BrokenLink = collections.namedtuple("BrokenLink", "path line target")
Observed = collections.namedtuple("Observed", "cycles exceptions claims supported_claims broken_links")


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------


def _observed_exceptions(graph):
    """Every forbidden edge, counted by identity, with its evidence lines."""
    counts = collections.Counter()
    locations = collections.defaultdict(list)
    for edge in graph.module_top + graph.function_body:
        verdict = is_allowed(edge.source, edge.target)
        if verdict.status == "allow":
            continue
        identity = (verdict.rule, edge.source, edge.target, edge.kind)
        counts[identity] += 1
        locations[identity].append(f"{edge.path}:{edge.line}")
    return counts, {identity: tuple(sorted(lines)) for identity, lines in locations.items()}


def _observed_claims(graph):
    """Split every ``cycle`` annotation into unsupported and supported ones."""
    module_top, effective = component_memberships(graph)
    unsupported = []
    supported = []
    for claim in graph.claims:
        source = claim.source
        verdict, offenders = classify_claim(claim, source, module_top, effective)
        if verdict == "unsupported":
            unsupported.append((claim, offenders))
        else:
            supported.append((claim, source, verdict))
    return tuple(unsupported), tuple(supported)


def observe(root, targets):
    graph = build_graph(root, targets)
    if graph.census["unclassified"]:
        raise PolicyError(
            "cannot classify "
            + ", ".join(graph.census["unclassified"])
            + "; add a MODULE_LAYER_OVERRIDES entry for each"
        )
    counts, locations = _observed_exceptions(graph)
    unsupported, supported = _observed_claims(graph)
    return graph, Observed(
        cycles=find_cycles(graph),
        exceptions=(counts, locations),
        claims=unsupported,
        supported_claims=supported,
        broken_links=check_doc_links(root),
    )


# --------------------------------------------------------------------------
# Documentation links
# --------------------------------------------------------------------------


def linked_documents(root):
    """Every document ``R-DOC1`` reads, in a stable order.

    Public so the workflow policy suite can assert the CI path filter covers
    all of them rather than restating a handful of literals: a rule that only
    runs when somebody remembered to list its inputs is not a rule.
    """
    paths = [Path(root) / name for name in LINKED_DOC_FILES]
    for pattern in LINKED_DOC_GLOBS:
        paths.extend(sorted(Path(root).glob(pattern), key=lambda item: item.as_posix()))
    return [path for path in paths if path.is_file()]


def check_doc_links(root):
    """Every relative markdown link in the policy documents resolves on disk.

    Absolute links are skipped rather than fetched: a gate that reaches the
    network is a gate that fails on somebody's flaky DNS.
    """
    broken = []
    for path in linked_documents(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise PolicyError(f"cannot read {path.relative_to(root).as_posix()}: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            broken.extend(_broken_links_in(root, path, number, line))
    return tuple(sorted(broken))


def _broken_links_in(root, path, number, line):
    for target in MARKDOWN_LINK_RE.findall(line):
        if target.startswith(SKIPPED_LINK_PREFIXES):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        if resolved.exists():
            continue
        yield BrokenLink(path.relative_to(root).as_posix(), number, target)


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def _require(condition, message):
    if not condition:
        raise PolicyError(message)


def _validate_header(raw, expected_fingerprint, allow_policy_drift=False):
    _require(isinstance(raw, dict), "baseline has invalid top-level fields")
    _require(set(raw) == set(HEADER_FIELDS) | set(BASELINE_SECTIONS), "baseline has invalid top-level fields")
    _require(raw["schema_version"] == SCHEMA_VERSION, f"expected architecture baseline schema {SCHEMA_VERSION}")
    expected_python = f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]}"
    _require(raw["canonical_python"] == expected_python, f"baseline canonical_python must be {expected_python!r}")
    if raw["policy_sha256"] == expected_fingerprint:
        return False
    _require(
        allow_policy_drift,
        "baseline policy_sha256 does not match the effective architecture policy",
    )
    return True


def _validate_owner(row, modules, index, section):
    owner = row["owner"]
    _require(owner in AREA_LABELS, f"{section} row {index} names an owner outside AREA_LABELS: {owner!r}")
    derived = owner_for_modules(modules)
    _require(owner == derived, f"{section} row {index} records owner {owner!r}; the policy derives {derived!r}")


def _validate_removal_metadata(row, index, section):
    issue = row["removal_issue"]
    _require(
        not isinstance(issue, bool) and isinstance(issue, int) and issue > 0,
        f"{section} row {index} has an invalid removal_issue; record the issue that will remove it",
    )
    direction = row["removal_direction"]
    _require(isinstance(direction, str), f"{section} row {index} has a non-string removal_direction")
    _require(
        direction.strip().lower() not in PLACEHOLDER_DIRECTIONS,
        f"{section} row {index} has a placeholder removal_direction; state how the edge goes away",
    )
    _require(
        len(direction.strip()) >= MINIMUM_REMOVAL_DIRECTION,
        f"{section} row {index} has a removal_direction shorter than {MINIMUM_REMOVAL_DIRECTION} characters",
    )


def _validate_cycle_row(row, index):
    _require(isinstance(row, dict), f"cycles row {index} is not an object")
    _require(
        set(CYCLE_FIELDS) <= set(row) <= set(CYCLE_FIELDS) | set(OPTIONAL_CYCLE_FIELDS),
        f"cycles row {index} has invalid fields",
    )
    _require(
        row["disposition"] == DEBT_DISPOSITION,
        f"cycles row {index} records disposition {row['disposition']!r}; a cycle is always debt",
    )
    _require(row["graph"] in GRAPH_NAMES, f"cycles row {index} names an unknown graph {row['graph']!r}")
    modules = row["modules"]
    _require(isinstance(modules, list) and len(modules) >= 2, f"cycles row {index} needs at least two modules")
    _require(modules == sorted(modules), f"cycles row {index} has unsorted modules")
    _validate_cycle_edges(row, index, set(modules))
    _require(
        len(modules) < 3 or str(row.get("notes", "")).strip(),
        f"cycles row {index} spans {len(modules)} modules and needs notes naming its keystone edges",
    )
    _require(str(row["accepted_reason"]).strip(), f"cycles row {index} has an empty accepted_reason")
    _validate_owner(row, tuple(modules), index, "cycles")
    _validate_removal_metadata(row, index, "cycles")
    identity = "|".join([row["graph"], *modules])
    _require(row["id"] == identity, f"cycles row {index} has a hand-edited identity")
    return identity


def _validate_cycle_edges(row, index, members):
    edges = row["edges"]
    _require(isinstance(edges, list) and edges, f"cycles row {index} records no edges")
    for edge in edges:
        _require(isinstance(edge, dict) and set(edge) == {"source", "target", "kind"}, f"cycles row {index} edges")
        _require(edge["kind"] in EDGE_KINDS, f"cycles row {index} edges name an unknown kind")
        _require(
            edge["source"] in members and edge["target"] in members,
            f"cycles row {index} edges leave the component",
        )
    ordered = [(edge["source"], edge["target"], edge["kind"]) for edge in edges]
    _require(ordered == sorted(ordered), f"cycles row {index} has unsorted edges")


def _validate_exception_row(row, index):
    _require(isinstance(row, dict), f"layer_exceptions row {index} is not an object")
    disposition = row.get("disposition")
    _require(
        disposition in DISPOSITIONS,
        f"layer_exceptions row {index} records an invalid disposition {disposition!r}; "
        "triage the row as debt or accepted",
    )
    if disposition == ACCEPTED_DISPOSITION:
        _require(
            set(row) == set(EXCEPTION_FIELDS_ACCEPTED),
            f"layer_exceptions row {index} has invalid fields for disposition 'accepted'; "
            "an accepted exception records no removal_issue or removal_direction",
        )
    else:
        _require(set(row) == set(EXCEPTION_FIELDS), f"layer_exceptions row {index} fields")
    rule = row["rule"]
    _require(rule in MATRIX_RULES, f"layer_exceptions row {index} names an unknown rule {rule!r}")
    _require(
        rule not in ABSOLUTE_FORBIDDEN,
        f"layer_exceptions row {index} records {rule}, which has no baseline representation at any severity",
    )
    _require(row["kind"] in EDGE_KINDS, f"layer_exceptions row {index} names an unknown kind")
    count = row["count"]
    _require(
        not isinstance(count, bool) and isinstance(count, int) and count > 0,
        f"layer_exceptions row {index} has an invalid count",
    )
    _validate_exception_layers(row, index)
    _require(str(row["accepted_reason"]).strip(), f"layer_exceptions row {index} has an empty accepted_reason")
    _validate_owner(row, (row["source"], row["target"]), index, "layer_exceptions")
    if disposition == DEBT_DISPOSITION:
        _validate_removal_metadata(row, index, "layer_exceptions")
    identity = exception_id(rule, row["source"], row["target"], row["kind"])
    _require(row["id"] == identity, f"layer_exceptions row {index} has a hand-edited identity")
    return identity


def _validate_exception_layers(row, index):
    for end in ("source", "target"):
        recorded = row[f"{end}_layer"]
        actual = layer_of(row[end])
        _require(
            recorded == actual,
            f"layer_exceptions row {index} records {end}_layer {recorded!r}; the policy classifies {actual!r}",
        )


def _validate_claim_row(row, index):
    _require(isinstance(row, dict) and set(row) == set(CLAIM_FIELDS), f"unsupported_cycle_claims row {index} fields")
    _require(
        row["disposition"] == DEBT_DISPOSITION,
        f"unsupported_cycle_claims row {index} records disposition {row['disposition']!r}; "
        "a cycle claim is always debt",
    )
    targets = row["targets"]
    _require(isinstance(targets, list), f"unsupported_cycle_claims row {index} has invalid targets")
    _require(targets == sorted(targets), f"unsupported_cycle_claims row {index} has unsorted targets")
    # The owning area is the source's, so the source has to be recorded -- and it
    # has to be the module the path actually denotes, or the owner is derived
    # from a claim about provenance nobody checked.
    _require(isinstance(row["source"], str) and row["source"], f"unsupported_cycle_claims row {index} has no source")
    expected = source_module_for_path(row["path"])
    _require(
        row["source"] == expected,
        f"unsupported_cycle_claims row {index} records source {row['source']!r}, "
        f"but {row['path']} is module {expected!r}",
    )
    _validate_owner(row, (row["source"], *targets), index, "unsupported_cycle_claims")
    _validate_removal_metadata(row, index, "unsupported_cycle_claims")
    identity = "|".join((row["path"], row["scope"], row["statement"]))
    _require(row["id"] == identity, f"unsupported_cycle_claims row {index} has a hand-edited identity")
    return identity


SECTION_VALIDATORS = {
    "cycles": _validate_cycle_row,
    "layer_exceptions": _validate_exception_row,
    "unsupported_cycle_claims": _validate_claim_row,
}


def _validate_section(section, rows):
    _require(isinstance(rows, list), f"baseline {section} must be a list")
    identities = []
    for index, row in enumerate(rows):
        identity = SECTION_VALIDATORS[section](row, index)
        _require(identity not in identities, f"baseline {section} row {index} duplicates an identity")
        identities.append(identity)
    _require(identities == sorted(identities), f"baseline {section} rows must be sorted by identity")
    return {identity: row for identity, row in zip(identities, rows, strict=True)}


def load_baseline(baseline_path, expected_fingerprint, allow_policy_drift=False):
    """Read the recorded debt, refusing anything that cannot be trusted.

    ``allow_policy_drift`` is used only when regenerating. Editing the policy
    must not become an amnesty for debt added in the same commit: the recorded
    *rows* stay valid when only the fingerprint moved, so the ratchet still runs
    against them and still refuses net-new identities. Without it a reviewed
    policy edit could never be re-stamped, because the load that has to precede
    the write is the load the stale fingerprint rejects.
    """
    try:
        raw = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read baseline {baseline_path}: {exc}") from exc
    if _validate_header(raw, expected_fingerprint, allow_policy_drift):
        print(
            "architecture policy: baseline policy fingerprint changed; regenerating against the "
            "recorded identities so newly added debt is still refused."
        )
    return {section: _validate_section(section, raw[section]) for section in BASELINE_SECTIONS}


def write_baseline(rows, baseline_path, fingerprint):
    document = {
        "schema_version": SCHEMA_VERSION,
        "canonical_python": f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]}",
        "policy_sha256": fingerprint,
    }
    for section in BASELINE_SECTIONS:
        document[section] = [rows[section][identity] for identity in sorted(rows[section])]
    try:
        Path(baseline_path).write_text(
            json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        # An unwritable path is a result nobody should trust, not a policy
        # regression a contributor fixes -- and never a raw traceback.
        raise PolicyError(f"cannot write baseline {baseline_path}: {exc}") from exc
    return sum(len(document[section]) for section in BASELINE_SECTIONS)


# --------------------------------------------------------------------------
# Issue-state snapshot
# --------------------------------------------------------------------------

SNAPSHOT_KEYS = frozenset({"schema_version", "refreshed_at", "issues"})


def _validate_issue_states(raw, path):
    """The snapshot is numbers and states only -- no titles, no bodies, no tokens."""
    _require(
        isinstance(raw, dict) and set(raw) == SNAPSHOT_KEYS,
        f"issue-state snapshot {path} has invalid top-level fields",
    )
    _require(
        raw["schema_version"] == ISSUE_STATES_SCHEMA_VERSION,
        f"expected issue-state snapshot schema {ISSUE_STATES_SCHEMA_VERSION}",
    )
    _require(
        isinstance(raw["refreshed_at"], str) and raw["refreshed_at"],
        f"issue-state snapshot {path} has no refreshed_at",
    )
    issues = raw["issues"]
    _require(isinstance(issues, dict), f"issue-state snapshot {path} issues must be an object")
    parsed = {}
    for key, state in issues.items():
        try:
            number = int(key)
        except (TypeError, ValueError) as exc:
            raise PolicyError(f"issue-state snapshot {path} has a non-numeric issue key {key!r}") from exc
        _require(number > 0, f"issue-state snapshot {path} records invalid issue number {number}")
        _require(
            state in ("open", "closed"),
            f"issue-state snapshot {path} records invalid state {state!r} for issue {number}",
        )
        parsed[number] = state
    return parsed


def load_issue_states(path):
    """The parsed snapshot, or ``None`` when the file does not exist yet."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read issue-state snapshot {path}: {exc}") from exc
    return _validate_issue_states(raw, path)


def _debt_rows(recorded):
    for section in BASELINE_SECTIONS:
        for row in recorded[section].values():
            if row.get("disposition", DEBT_DISPOSITION) == DEBT_DISPOSITION:
                yield section, row


def _validate_liveness(recorded, states, snapshot_path):
    """A debt row may only reference an issue the snapshot records as open.

    The snapshot is reviewed and checked in, never fetched here: local and
    offline runs stay deterministic and network-free.
    """
    referenced = {}
    for section, row in _debt_rows(recorded):
        referenced.setdefault(row["removal_issue"], []).append(f"{section}:{row['id']}")
    if not referenced:
        return
    if states is None:
        raise PolicyError(
            f"no issue-state snapshot at {snapshot_path}; active-debt rows reference "
            "tracking issues -- run `python scripts/check_architecture.py "
            "--refresh-issue-states` and commit the reviewed snapshot"
        )
    missing = sorted(number for number in referenced if number not in states)
    if missing:
        raise PolicyError(
            "issue-state snapshot does not record tracked issue(s) "
            + ", ".join(f"#{number}" for number in missing)
            + "; refresh it with --refresh-issue-states"
        )
    extra = sorted(set(states) - set(referenced))
    if extra:
        raise PolicyError(
            "issue-state snapshot records issue(s) no baseline row references: "
            + ", ".join(f"#{number}" for number in extra)
            + "; refresh it with --refresh-issue-states"
        )
    closed = sorted(number for number, state in states.items() if state != "open")
    if closed:
        example = referenced[closed[0]][0]
        raise PolicyError(
            "active-debt baseline row(s) reference closed or non-open issue(s) "
            + ", ".join(f"#{number}" for number in closed)
            + f" (e.g. {example}); re-track the debt on an open issue, or refresh the snapshot"
        )


def _fetch_issue_states(numbers, runner=subprocess.run):
    """Query the gh CLI for one state per referenced issue; never stores a token.

    The GitHub issues endpoint also serves pull requests, so this verifies the
    reference is a real issue (no ``pull_request`` key), exists (non-zero gh
    exit), and reports an open/closed state -- a PR is never an acceptable
    removal tracker.
    """
    states = {}
    for number in sorted(numbers):
        command = [
            "gh",
            "api",
            f"repos/itambox/itambox-webapp/issues/{number}",
            "--jq",
            "{state, pull_request}",
        ]
        try:
            completed = runner(command, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise PolicyError(
                "cannot run the gh CLI; install GitHub CLI and authenticate, or record "
                "reviewed issue states in the snapshot by hand"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise PolicyError(f"cannot read state for issue #{number}: {detail[:200]}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"cannot parse issue-state response for issue #{number}: {exc}") from exc
        if payload.get("pull_request") is not None:
            raise PolicyError(
                f"tracking reference #{number} is a pull request, not an issue; "
                "active-debt rows must reference a real open issue"
            )
        state = payload.get("state")
        _require(state in ("open", "closed"), f"unexpected state {state!r} for issue #{number}")
        states[number] = state
    return states


def write_issue_states(path, states):
    document = {
        "schema_version": ISSUE_STATES_SCHEMA_VERSION,
        "refreshed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "issues": {str(number): states[number] for number in sorted(states)},
    }
    try:
        Path(path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise PolicyError(f"cannot write issue-state snapshot {path}: {exc}") from exc
    return len(states)


def refresh_issue_states(baseline_path, snapshot_path, fingerprint, runner=subprocess.run):
    """Freeze the state of every referenced tracking issue into the snapshot.

    Deterministic when nothing changed: an identical issues map leaves the file
    untouched, so a CI drift check that runs this command and then ``git diff``
    only fails on real drift. Returns True when the snapshot was rewritten.
    """
    recorded = load_baseline(baseline_path, fingerprint, allow_policy_drift=False)
    referenced = {row["removal_issue"] for _section, row in _debt_rows(recorded)}
    states = _fetch_issue_states(referenced, runner=runner)
    existing = load_issue_states(snapshot_path)
    if existing == states:
        print(f"Issue-state snapshot {snapshot_path} is already current.")
        return False
    written = write_issue_states(snapshot_path, states)
    print(f"Wrote issue-state snapshot for {written} issue(s) to {snapshot_path}.")
    if not referenced:
        print("No baseline row references a tracking issue; the snapshot is empty.")
    return True


# --------------------------------------------------------------------------
# Row construction
# --------------------------------------------------------------------------


def _cycle_row(component, recorded):
    row = {
        "id": component.id,
        "graph": component.graph,
        "modules": list(component.modules),
        "edges": [{"source": source, "target": target, "kind": kind} for source, target, kind in component.edges],
        "owner": owner_for_modules(component.modules),
        "disposition": SCAFFOLD_DISPOSITION,
        "removal_issue": SCAFFOLD_ISSUE,
        "removal_direction": SCAFFOLD_DIRECTION,
        "accepted_reason": SCAFFOLD_DIRECTION,
    }
    if len(component.modules) >= 3:
        row["notes"] = SCAFFOLD_DIRECTION
    return _carry_forward(row, recorded)


def _exception_row(identity, count, recorded):
    rule, source, target, kind = identity
    row = {
        "id": exception_id(rule, source, target, kind),
        "rule": rule,
        "source": source,
        "source_layer": layer_of(source),
        "target": target,
        "target_layer": layer_of(target),
        "kind": kind,
        "count": count,
        "owner": owner_for_modules((source, target)),
        "disposition": SCAFFOLD_DISPOSITION,
        "removal_issue": SCAFFOLD_ISSUE,
        "removal_direction": SCAFFOLD_DIRECTION,
        "accepted_reason": SCAFFOLD_DIRECTION,
    }
    return _carry_forward(row, recorded)


def _claim_row(claim, offenders, recorded):
    """One row per annotation group, recording the targets the finding names.

    ``offenders`` -- not every module the group resolves to. A group can name
    one target the graph does support and one it does not; recording both would
    make the row disagree with the diagnostic and with ``--format json``, and
    would ratchet against a set the gate never reports.
    """
    row = {
        "id": claim.id,
        "source": claim.source,
        "path": claim.path,
        "scope": claim.scope,
        "statement": claim.statement,
        "targets": sorted(offenders),
        "owner": owner_for_modules((claim.source, *sorted(offenders))),
        "disposition": SCAFFOLD_DISPOSITION,
        "removal_issue": SCAFFOLD_ISSUE,
        "removal_direction": SCAFFOLD_DIRECTION,
    }
    return _carry_forward(row, recorded)


def _carry_forward(row, recorded):
    """Human metadata survives regeneration byte for byte; nothing else does."""
    if recorded is None:
        return row
    for field in HUMAN_FIELDS:
        if field in row and field in recorded:
            row[field] = recorded[field]
    if row.get("disposition") == ACCEPTED_DISPOSITION:
        # An accepted exception records no removal promise. The debt-shaped
        # scaffold fields must not reappear on a regeneration.
        row.pop("removal_issue", None)
        row.pop("removal_direction", None)
    return row


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _compare_counted(observed, recorded):
    regressions = {}
    stale = {}
    for identity, count in sorted(observed.items()):
        previous = recorded.get(identity)
        if previous is None or count > previous:
            regressions[identity] = count
    for identity, previous in sorted(recorded.items()):
        current = observed.get(identity)
        if current is None or current < previous:
            stale[identity] = previous
    return regressions, stale


def _compare_present(observed, recorded):
    regressions = [identity for identity in sorted(observed) if identity not in recorded]
    stale = [identity for identity in sorted(recorded) if identity not in observed]
    return regressions, stale


def _compare_membered(observed, recorded):
    """Ratchet identities that carry a *set*, not merely their own existence.

    An unsupported cycle claim is keyed on ``(path, scope, statement)`` anchored
    on the first statement of an annotation group, and the inline-import policy
    lets the rest of a contiguous group inherit that one comment. So an import
    written directly under a recorded claim adds an unsupported target without
    moving any identity. Comparing presence alone would absorb it; comparing the
    set refuses it, exactly as ``count`` refuses one more forbidden edge.

    Both halves can be true of one identity at once -- a claim that swaps one
    unsupported target for another has debt to refuse and debt to normalise.
    """
    regressions = {}
    stale = {}
    for identity, members in sorted(observed.items()):
        previous = recorded.get(identity)
        if previous is None:
            regressions[identity] = members
        elif members - previous:
            regressions[identity] = members - previous
    for identity, previous in sorted(recorded.items()):
        current = observed.get(identity)
        if current is None:
            stale[identity] = previous
        elif previous - current:
            stale[identity] = previous - current
    return regressions, stale


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _print_absolute(rows, locations):
    print("architecture policy: forbidden edge(s) for which no exception is permitted:\n")
    for rule, source, target, kind in rows:
        print(f"  {rule} {source} -> {target} ({kind})")
        print(f"    {MATRIX_RULES[rule]}")
        for location in locations[(rule, source, target, kind)]:
            print(f"    at {location}")
    print()


def _print_exception_regressions(rows, locations):
    print("architecture policy: new forbidden cross-layer import(s):\n")
    for (rule, source, target, kind), count in sorted(rows.items()):
        print(f"  {rule} {source} [{layer_of(source)}] -> {target} [{layer_of(target)}] ({kind}, {count})")
        print(f"    {MATRIX_RULES[rule]}")
        for location in locations[(rule, source, target, kind)]:
            print(f"    at {location}")
    print()


def _print_cycle_regressions(components):
    print("architecture policy: new import cycle(s):\n")
    for component in components:
        rule = "R-C1" if component.graph == MODULE_TOP else "R-CE1"
        print(f"  {rule} {component.graph} cycle over {len(component.modules)} module(s)")
        print(f"    {STRUCTURAL_RULES[rule]}")
        for source, target, kind in component.edges:
            print(f"    {source} -> {target} ({kind})")
    print()


def _print_claim_regressions(rows):
    print("architecture policy: inline cycle annotation(s) the measured graph does not support:\n")
    for claim, added in rows:
        print(f"  R-C3 {claim.path}:{claim.line}")
        print(f"    scope: {claim.scope or '<module>'}")
        print(f"    statement: {claim.statement}")
        named = ", ".join(sorted(added)) if added else "no first-party module"
        print(f"    claims a cycle with {named}, which shares no strongly connected component with it")
    print(
        "\nA group inherits one comment, so an import added under a recorded claim is a new "
        "target on the same finding.\nHoist the import, or re-categorise it as app-registry, "
        "heavy-import, or optional-dependency.\n"
    )


def _print_unrecorded_components(rows):
    print("architecture policy: supported cycle claim(s) whose component is not recorded:\n")
    for claim, component in rows:
        print(f"  R-C2 {claim.path}:{claim.line}")
        print(f"    {STRUCTURAL_RULES['R-C2']}: {component}")
    print()


def _print_broken_links(rows):
    print("architecture policy: documentation reference(s) that do not resolve:\n")
    for link in rows:
        print(f"  R-DOC1 {link.path}:{link.line} -> {link.target}")
    print(f"\n{STRUCTURAL_RULES['R-DOC1']}. Fix the link; it is not baselineable.\n")


def _print_stale(sections):
    print("architecture baseline is stale -- recorded debt is no longer present:\n")
    for section, identities in sections:
        for identity in identities:
            name = identity if isinstance(identity, str) else " | ".join(identity)
            print(f"  {section}: {name}{_member_detail(identities, identity, 'no longer unsupported')}")
    print()


def _member_detail(identities, identity, label):
    """Name the members that moved, for an identity that itself did not.

    Only the set-ratcheted sections carry one; a counted or presence-only
    identity says everything it has to say in its own name.
    """
    members = identities.get(identity) if isinstance(identities, dict) else None
    if not isinstance(members, frozenset):
        return ""
    return f" ({label}: {', '.join(sorted(members)) or 'no first-party module'})"


def _print_regeneration_hint(baseline_path):
    print(
        "After a reviewed cleanup, regenerate on canonical Python "
        f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]} with "
        "`python scripts/check_architecture.py --write-baseline` and review the "
        f"{baseline_path} diff. New debt is never absorbed: hand-review the row "
        "into the file first, with an owner, a disposition, a removal issue, and a "
        "removal direction."
    )


def _print_summary(graph, observed):
    counts, _locations = observed.exceptions
    print(
        "architecture: {modules} module(s); {top} module-top and {eff} effective edge identities; "
        "{cycles} recorded cycle(s); {exceptions} recorded layer exception(s); {claims} recorded cycle claim(s).".format(
            modules=graph.census["discovered"],
            top=len({(edge.source, edge.target) for edge in graph.module_top}),
            eff=len({(edge.source, edge.target) for edge in graph.module_top + graph.function_body}),
            cycles=len(observed.cycles),
            exceptions=len(counts),
            claims=len(observed.claims),
        )
    )
    print(
        f"  informational: {len(graph.typing_only)} typing-only edge(s), "
        f"{len(graph.dynamic_imports)} dynamic import site(s), "
        f"{len(graph.census['inert'])} import-free package initialiser(s)."
    )


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def _json_payload(graph, observed):
    counts, locations = observed.exceptions
    return {
        "census": graph.census,
        "cycles": [
            {
                "id": component.id,
                "graph": component.graph,
                "modules": list(component.modules),
                "edges": [{"source": s, "target": t, "kind": k} for s, t, k in component.edges],
            }
            for component in observed.cycles
        ],
        "documentation": [
            {"path": link.path, "line": link.line, "target": link.target} for link in observed.broken_links
        ],
        "dynamic_imports": [
            {"module": site.module, "path": site.path, "line": site.line, "call": site.call}
            for site in graph.dynamic_imports
        ],
        "layer_exceptions": [
            {
                "id": exception_id(*identity),
                "rule": identity[0],
                "source": identity[1],
                "target": identity[2],
                "kind": identity[3],
                "count": count,
                "locations": list(locations[identity]),
            }
            for identity, count in sorted(counts.items())
        ],
        "typing_only": [
            {"source": edge.source, "target": edge.target, "path": edge.path, "line": edge.line}
            for edge in graph.typing_only
        ],
        "unsupported_cycle_claims": [
            {
                "id": claim.id,
                "source": claim.source,
                "path": claim.path,
                "line": claim.line,
                "targets": list(offenders),
            }
            for claim, offenders in observed.claims
        ],
    }


# --------------------------------------------------------------------------
# Explain
# --------------------------------------------------------------------------


def _shortest_path(adjacency, source, target):
    if source not in adjacency:
        return None
    queue = collections.deque([[source]])
    seen = {source}
    while queue:
        path = queue.popleft()
        if path[-1] == target:
            return path
        for neighbour in adjacency.get(path[-1], ()):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append([*path, neighbour])
    return None


def report_explain(graph, source, target, output_format="text"):
    """The shortest chain between two modules, in each blocking graph.

    Answers in whichever format the caller asked for. ``--format json`` promises
    stdout to exactly one parseable document, and a query mode that printed
    prose instead would be the one place that promise did not hold.
    """
    paths = [
        (name, _shortest_path(adjacency_for(edges), source, target))
        for name, edges in ((MODULE_TOP, graph.module_top), ("effective", graph.module_top + graph.function_body))
    ]
    if output_format == "json":
        payload = {
            "explain": {
                "source": source,
                "target": target,
                "paths": {name: list(path) if path else None for name, path in paths},
            }
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for name, path in paths:
        print(f"  {name}: {' -> '.join(path) if path else 'no path'}")
    return 0


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------


def _observed_rows(graph, observed, recorded):
    """The rows a regeneration would write, with human metadata carried over."""
    counts, _locations = observed.exceptions
    rows = {section: {} for section in BASELINE_SECTIONS}
    for component in observed.cycles:
        rows["cycles"][component.id] = _cycle_row(component, recorded["cycles"].get(component.id))
    for identity, count in counts.items():
        if identity[0] in ABSOLUTE_FORBIDDEN:
            continue
        key = exception_id(*identity)
        rows["layer_exceptions"][key] = _exception_row(identity, count, recorded["layer_exceptions"].get(key))
    for claim, offenders in observed.claims:
        rows["unsupported_cycle_claims"][claim.id] = _claim_row(
            claim, offenders, recorded["unsupported_cycle_claims"].get(claim.id)
        )
    return rows


def _diff(observed, recorded):
    counts, _locations = observed.exceptions
    baselineable = {
        exception_id(*identity): count for identity, count in counts.items() if identity[0] not in ABSOLUTE_FORBIDDEN
    }
    exception_regressions, exception_stale = _compare_counted(
        baselineable, {identity: row["count"] for identity, row in recorded["layer_exceptions"].items()}
    )
    cycle_regressions, cycle_stale = _compare_present(
        {component.id: component for component in observed.cycles}, recorded["cycles"]
    )
    claim_regressions, claim_stale = _compare_membered(
        {claim.id: frozenset(offenders) for claim, offenders in observed.claims},
        {identity: frozenset(row["targets"]) for identity, row in recorded["unsupported_cycle_claims"].items()},
    )
    return {
        "layer_exceptions": (exception_regressions, exception_stale),
        "cycles": (cycle_regressions, cycle_stale),
        "unsupported_cycle_claims": (claim_regressions, claim_stale),
    }


def _refuse_downgraded_components(observed, recorded):
    """A hard cycle cannot be recorded as a soft one."""
    module_top = {frozenset(component.modules) for component in observed.cycles if component.graph == MODULE_TOP}
    for identity, row in sorted(recorded["cycles"].items()):
        if row["graph"] != MODULE_TOP and frozenset(row["modules"]) in module_top:
            raise PolicyError(
                f"baseline records {identity} on the effective graph, but the component is visible "
                "in the module-top graph; a module-top cycle cannot be downgraded"
            )


def _supported_claim_components(graph, observed):
    """Map each supported claim to the component identity it relies on."""
    module_top, effective = component_memberships(graph)
    resolved = []
    for claim, source, verdict in observed.supported_claims:
        membership = module_top if verdict == "supported-module-top" else effective
        resolved.append((claim, membership[source]))
    return resolved


def _report_violations(graph, observed, recorded, baseline_path):
    counts, locations = observed.exceptions
    absolute = sorted(identity for identity in counts if identity[0] in ABSOLUTE_FORBIDDEN)
    diff = _diff(observed, recorded)
    unrecorded = [pair for pair in _supported_claim_components(graph, observed) if pair[1] not in recorded["cycles"]]

    failed = bool(absolute or observed.broken_links or unrecorded)
    if absolute:
        _print_absolute(absolute, locations)
    if diff["cycles"][0]:
        _print_cycle_regressions([component for component in observed.cycles if component.id in diff["cycles"][0]])
        failed = True
    if diff["layer_exceptions"][0]:
        regressions = {
            identity: diff["layer_exceptions"][0][exception_id(*identity)]
            for identity in counts
            if exception_id(*identity) in diff["layer_exceptions"][0]
        }
        _print_exception_regressions(regressions, locations)
        failed = True
    added_targets = diff["unsupported_cycle_claims"][0]
    if added_targets:
        _print_claim_regressions(
            [(claim, added_targets[claim.id]) for claim, _offenders in observed.claims if claim.id in added_targets]
        )
        failed = True
    if unrecorded:
        _print_unrecorded_components(unrecorded)
    if observed.broken_links:
        _print_broken_links(observed.broken_links)
    stale = [(section, diff[section][1]) for section in BASELINE_SECTIONS if diff[section][1]]
    if stale:
        _print_stale(stale)
        failed = True
    if failed:
        _print_regeneration_hint(baseline_path)
    return 1 if failed else 0


def _run_write(args, graph, observed, recorded, bootstrapping):
    counts, locations = observed.exceptions
    absolute = sorted(identity for identity in counts if identity[0] in ABSOLUTE_FORBIDDEN)
    if absolute:
        _print_absolute(absolute, locations)
        print("These are refused in every mode, including a bootstrap. The baseline was not written.")
        return 1
    rows = _observed_rows(graph, observed, recorded)
    if not bootstrapping:
        diff = _diff(observed, recorded)
        new = {section: diff[section][0] for section in BASELINE_SECTIONS if diff[section][0]}
        if new:
            _print_new_identity_refusal(new)
            return 1
    written = write_baseline(rows, args.baseline, compute_policy_fingerprint(args.targets))
    print(f"Wrote {written} baseline row(s) to {args.baseline}")
    if bootstrapping:
        print(
            "\nThis is a scaffold, not a pass. Every row carries removal_issue 0, "
            'removal_direction "TODO", and disposition "TODO", which the loader '
            "refuses, so the gate stays red until each row is triaged by hand with "
            "the issue that will remove it, a concrete removal_direction of at least "
            f"{MINIMUM_REMOVAL_DIRECTION} characters."
        )
        return 1
    return 0


def _print_new_identity_refusal(new):
    print("architecture policy: --write-baseline refuses newly observed debt:\n")
    for section, identities in sorted(new.items()):
        for identity in sorted(identities):
            print(f"  {section}: {identity}{_member_detail(identities, identity, 'added')}")
    print(
        "\nNew debt must be hand-reviewed into the baseline, never absorbed: add the row "
        "with an owner, a removal issue, and a removal direction in a diff a reviewer sees, "
        "then re-run --write-baseline to normalise ordering and stamp the fingerprint."
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="*", default=list(DEFAULT_TARGETS))
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH, help="Path to the baseline JSON file.")
    parser.add_argument(
        "--issue-states",
        type=Path,
        default=ISSUE_STATES_PATH,
        help="Path to the issue-state snapshot JSON file.",
    )
    parser.add_argument(
        "--refresh-issue-states",
        action="store_true",
        help="Query the gh CLI for every referenced tracking issue and freeze its state "
        "in the reviewed issue-state snapshot.",
    )
    parser.add_argument("--cwd", type=Path, default=REPO_ROOT, help="Repository root the targets resolve against.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Normalise the baseline after a reviewed cleanup; new identities are refused.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Inventory mode for triage. Never a pass, and never wired into CI or pre-commit.",
    )
    parser.add_argument(
        "--explain",
        nargs=2,
        metavar=("SOURCE", "TARGET"),
        help="Print the shortest import path between two modules in each graph.",
    )
    arguments = parser.parse_args(argv)
    if not arguments.targets:
        arguments.targets = list(DEFAULT_TARGETS)
    return arguments


def _is_canonical_python():
    if sys.version_info[:2] != CANONICAL_PYTHON:
        print(
            "Refusing to run the architecture boundary gate outside Python "
            f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]} "
            f"(running {sys.version_info[0]}.{sys.version_info[1]}); "
            "AST shape and statement normalisation differ across interpreter "
            "versions, so findings would not be comparable to the canonical baseline.",
            file=sys.stderr,
        )
        return False
    return True


def _empty_recorded():
    return {section: {} for section in BASELINE_SECTIONS}


def _decide(args, graph, observed, fingerprint):
    """Everything that can print a human diagnostic, and the exit code it means."""
    bootstrapping = args.write_baseline and not args.baseline.exists()
    report_only = args.report_only and not args.baseline.exists()
    recorded = (
        _empty_recorded()
        if bootstrapping or report_only
        else load_baseline(args.baseline, fingerprint, allow_policy_drift=args.write_baseline)
    )
    _validate_liveness(recorded, load_issue_states(args.issue_states), args.issue_states)

    _refuse_downgraded_components(observed, recorded)
    if args.write_baseline:
        return _run_write(args, graph, observed, recorded, bootstrapping)

    exit_code = _report_violations(graph, observed, recorded, args.baseline)
    if exit_code == 0 and args.format == "text":
        _print_summary(graph, observed)
    return 0 if args.report_only else exit_code


def _run_gate(args):
    validate_policy()
    fingerprint = compute_policy_fingerprint(args.targets)
    graph, observed = observe(args.cwd, args.targets)

    if args.explain:
        return report_explain(graph, args.explain[0], args.explain[1], args.format)
    if args.format != "json":
        return _decide(args, graph, observed, fingerprint)

    # JSON mode owns stdout completely: one document, nothing appended. The
    # findings are already in the payload, so the human narrative is not
    # suppressed -- it is written to stderr, where a caller piping stdout into a
    # parser still sees it. Redirecting the whole decision is deliberate: a
    # per-call-site rule is one somebody forgets at the next diagnostic.
    print(json.dumps(_json_payload(graph, observed), indent=2, sort_keys=True))
    with contextlib.redirect_stdout(sys.stderr):
        return _decide(args, graph, observed, fingerprint)


def main(argv=None):
    args = parse_args(argv)
    if not _is_canonical_python():
        return 2
    if args.report_only:
        print(REPORT_ONLY_BANNER, file=sys.stderr)
    try:
        if args.refresh_issue_states:
            if args.format == "json":
                raise PolicyError("cannot combine --refresh-issue-states with --format json")
            fingerprint = compute_policy_fingerprint(args.targets)
            refresh_issue_states(args.baseline, args.issue_states, fingerprint)
            return 0
        return _run_gate(args)
    except PolicyError as exc:
        print(f"architecture gate failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.report_only:
            print(REPORT_ONLY_BANNER, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
