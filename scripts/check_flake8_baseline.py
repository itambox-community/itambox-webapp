#!/usr/bin/env python
"""Fail-closed, identity-based Flake8 debt ratchet.

ITAMbox has pre-existing Flake8 findings. The checked-in baseline records the
identity of each reviewed finding as path, code, message, source, and stable AST
context. Physical row/column numbers are deliberately excluded:
inserting an unrelated line above existing debt must not create a false regression.

A new identity is always a regression, even if it replaces an old finding with
the same error code in the same file. Removed identities make the baseline stale
and require a reviewed cleanup update, so fixed debt never becomes headroom.

The canonical baseline is generated with Python 3.12 and the pinned Flake8
toolchain. The gate refuses to run on any other interpreter: tokenizer
behaviour differs across Python versions (which findings Flake8 reports and
where), so results from a non-canonical interpreter are not comparable to the
baseline. There are no interpreter- or OS-specific exceptions.
"""

import argparse
import ast
import collections
import configparser
import hashlib
import json
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

VIOLATION_RE = re.compile(
    r"^(?P<path>.+?):(?P<row>\d+):(?P<column>\d+): "
    r"(?P<code>[A-Z]+\d+) (?P<message>.*)$"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "scripts" / "flake8_baseline.json"
DEFAULT_TARGETS = ["itambox", "scripts"]
BASELINE_SCHEMA_VERSION = 3
CANONICAL_PYTHON = (3, 12)
REQUIRED_TOOL_VERSIONS = {
    "flake8": "7.3.0",
    "flake8-bugbear": "24.12.12",
    "mccabe": "0.7.0",
    "pycodestyle": "2.14.0",
    "pyflakes": "3.4.0",
}


def verify_toolchain():
    problems = []
    for distribution, expected in REQUIRED_TOOL_VERSIONS.items():
        try:
            actual = version(distribution)
        except PackageNotFoundError:
            problems.append(f"{distribution} is not installed (expected {expected})")
            continue
        if actual != expected:
            problems.append(f"{distribution}=={actual} (expected {expected})")
    return problems


def run_flake8(targets, cwd):
    result = subprocess.run(
        [sys.executable, "-m", "flake8", "--config", "setup.cfg", *targets],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout, result.stderr, result.returncode


def compute_policy_fingerprint(cwd, targets):
    config_path = cwd / "setup.cfg"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except (OSError, configparser.Error) as exc:
        raise ValueError(f"cannot read Flake8 policy {config_path}: {exc}") from exc
    if not parser.has_section("flake8"):
        raise ValueError(f"Flake8 policy {config_path} has no [flake8] section")
    payload = {
        "config": dict(sorted(parser.items("flake8"))),
        "targets": list(targets),
        "tool_versions": dict(sorted(REQUIRED_TOOL_VERSIONS.items())),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalise_path(raw_path, cwd):
    path = Path(raw_path)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(cwd.resolve())
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _source_anchor(code, source_text, source_lines, syntax_tree, row, path):
    """Return a row-stable source anchor for one finding.

    Flake8 may attribute B907 on a multiline f-string to any physical line of
    the construct (the reported row has varied across interpreter releases).
    Anchor B907 to the smallest enclosing statement so the identity does not
    depend on which line is reported, without weakening code/message checks.
    Other checks keep their exact reported source line.
    """
    if code != "B907":
        return source_lines[row - 1]
    candidates = [
        node
        for node in ast.walk(syntax_tree)
        if isinstance(node, ast.stmt) and node.lineno <= row <= getattr(node, "end_lineno", node.lineno)
    ]
    if not candidates:
        raise ValueError(f"cannot anchor B907 row {row} to a statement in {path}")
    statement = min(
        candidates,
        key=lambda node: (
            getattr(node, "end_lineno", node.lineno) - node.lineno,
            getattr(node, "end_col_offset", 0) - node.col_offset,
        ),
    )
    source = ast.get_source_segment(source_text, statement)
    if source is None:
        source = "\n".join(
            source_lines[
                statement.lineno - 1 : getattr(
                    statement,
                    "end_lineno",
                    statement.lineno,
                )
            ]
        )
    return source


def _secondary_context_label(node):
    if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
        return type(node).__name__
    if isinstance(node, ast.ExceptHandler):
        exception_type = ast.dump(node.type, include_attributes=False) if node.type is not None else None
        return f"ExceptHandler:{exception_type}:{node.name}"
    if isinstance(node, ast.Match):
        return f"Match:{ast.dump(node.subject, include_attributes=False)}"
    if isinstance(node, ast.match_case):
        guard = ast.dump(node.guard, include_attributes=False) if node.guard is not None else None
        return f"match_case:{ast.dump(node.pattern, include_attributes=False)}:{guard}"
    return None


def _context_label(node):
    if isinstance(node, ast.Module):
        return "Module"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return f"{type(node).__name__}:{node.name}"
    if isinstance(node, ast.If):
        return f"If:{ast.dump(node.test, include_attributes=False)}"
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return (
            f"{type(node).__name__}:"
            f"{ast.dump(node.target, include_attributes=False)}:"
            f"{ast.dump(node.iter, include_attributes=False)}"
        )
    if isinstance(node, ast.While):
        return f"While:{ast.dump(node.test, include_attributes=False)}"
    if isinstance(node, (ast.With, ast.AsyncWith)):
        items = [
            (
                ast.dump(item.context_expr, include_attributes=False),
                ast.dump(item.optional_vars, include_attributes=False) if item.optional_vars is not None else None,
            )
            for item in node.items
        ]
        return f"{type(node).__name__}:{items!r}"
    return _secondary_context_label(node)


def _statement_distance(node, row):
    start = node.lineno
    end = getattr(node, "end_lineno", start)
    if row < start:
        return start - row
    if row > end:
        return row - end
    return 0


def _statement_for_row(syntax_tree, row, path):
    statements = [node for node in ast.walk(syntax_tree) if isinstance(node, ast.stmt)]
    if not statements:
        return None
    return min(
        statements,
        key=lambda node: (
            _statement_distance(node, row),
            getattr(node, "end_lineno", node.lineno) - node.lineno,
            abs(node.lineno - row),
            node.col_offset,
        ),
    )


def _parent_relations(syntax_tree):
    parents = {}
    for parent in ast.walk(syntax_tree):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                parents[value] = (parent, field)
                continue
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        parents[child] = (parent, field)
    return parents


def _context_label_with_sibling_ordinal(node, parents):
    label = _context_label(node)
    if label is None or node not in parents:
        return label
    parent, field = parents[node]
    siblings = getattr(parent, field, None)
    if not isinstance(siblings, list):
        return label
    equivalent = [sibling for sibling in siblings if isinstance(sibling, ast.AST) and _context_label(sibling) == label]
    if len(equivalent) < 2:
        return label
    return f"{label}#{equivalent.index(node) + 1}"


def _source_context(syntax_tree, row, path):
    statement = _statement_for_row(syntax_tree, row, path)
    if statement is None:
        return "Module:body"
    parents = _parent_relations(syntax_tree)
    parts = []
    statement_label = _context_label_with_sibling_ordinal(statement, parents)
    if statement_label is not None:
        parts.append(f"{statement_label}:self")
    child = statement
    while child in parents:
        parent, field = parents[child]
        label = _context_label_with_sibling_ordinal(parent, parents)
        if label is not None:
            parts.append(f"{label}:{field}")
        child = parent
    if not parts:
        raise ValueError(f"cannot derive finding context for {path}:{row}")
    return "/".join(reversed(parts))


def parse_findings(output, cwd):
    findings = collections.Counter()
    examples = {}
    source_cache = {}
    lines = output.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = VIOLATION_RE.match(line)
        if not match:
            raise ValueError(f"unrecognised flake8 stdout line: {line!r}")
        path = _normalise_path(match.group("path"), cwd)
        source_path = Path(path)
        if not source_path.is_absolute():
            source_path = cwd / source_path
        source_path = source_path.resolve()
        if source_path not in source_cache:
            try:
                source_text = source_path.read_text(encoding="utf-8")
                source_cache[source_path] = (
                    source_text,
                    source_text.splitlines(),
                    ast.parse(source_text, filename=path),
                )
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"cannot read finding source {path}: {exc}") from exc
            except SyntaxError as exc:
                raise ValueError(f"cannot parse finding source {path}: {exc}") from exc
        row = int(match.group("row"))
        source_text, source_lines, syntax_tree = source_cache[source_path]
        if row < 1 or row > len(source_lines):
            raise ValueError(f"finding row {row} is outside {path} ({len(source_lines)} lines)")
        code = match.group("code")
        message = match.group("message")
        source = _source_anchor(
            code,
            source_text,
            source_lines,
            syntax_tree,
            row,
            path,
        )
        context = _source_context(syntax_tree, row, path)
        key = (path, code, message, source, context)
        findings[key] += 1
        examples.setdefault(key, line)
        index += 1
    return findings, examples


def _validate_baseline_header(raw, expected_policy_fingerprint):
    required_top_level = {
        "schema_version",
        "canonical_python",
        "policy_sha256",
        "findings",
    }
    if not isinstance(raw, dict) or set(raw) != required_top_level:
        raise ValueError("baseline has invalid top-level fields")
    if raw["schema_version"] != BASELINE_SCHEMA_VERSION:
        raise ValueError(f"expected Flake8 baseline schema {BASELINE_SCHEMA_VERSION}")
    if raw["canonical_python"] != "3.12":
        raise ValueError("baseline canonical_python must be '3.12'")
    if raw["policy_sha256"] != expected_policy_fingerprint:
        raise ValueError("baseline policy_sha256 does not match the effective Flake8 policy")


def load_baseline(baseline_path, expected_policy_fingerprint):
    raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    _validate_baseline_header(raw, expected_policy_fingerprint)
    rows = raw["findings"]
    if not isinstance(rows, list):
        raise ValueError("baseline findings must be a list")

    baseline = collections.Counter()
    ordered_identities = []
    required = {"path", "code", "message", "source", "context", "count"}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(f"baseline finding {index} has invalid fields")
        count = row["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"baseline finding {index} has invalid count")
        values = (
            row["path"],
            row["code"],
            row["message"],
            row["source"],
            row["context"],
        )
        if not all(isinstance(value, str) for value in values):
            raise ValueError(f"baseline finding {index} has non-string identity")
        if values in baseline:
            raise ValueError(f"baseline finding {index} duplicates an identity")
        baseline[values] = count
        ordered_identities.append(values)
    if ordered_identities != sorted(ordered_identities):
        raise ValueError("baseline findings must be sorted by identity")
    return baseline


def write_baseline(findings, baseline_path, policy_fingerprint):
    rows = [
        {
            "path": path,
            "code": code,
            "message": message,
            "source": source,
            "context": context,
            "count": count,
        }
        for (path, code, message, source, context), count in sorted(findings.items())
    ]
    data = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "canonical_python": "3.12",
        "policy_sha256": policy_fingerprint,
        "findings": rows,
    }
    baseline_path.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {len(rows)} baseline identities ({sum(findings.values())} total violations) to {baseline_path}")


def validate_flake8_result(output, error_output, status, cwd):
    """Return parsed findings or a fail-closed process status."""
    if error_output:
        print(output)
        print(error_output, file=sys.stderr)
        print("flake8 wrote unexpected stderr; refusing to pass", file=sys.stderr)
        return None, None, 2
    if status not in (0, 1):
        print(output)
        print(f"flake8 exited with unexpected status {status}", file=sys.stderr)
        return None, None, status

    try:
        findings, examples = parse_findings(output, cwd)
    except ValueError as exc:
        print(output)
        print(f"could not fingerprint flake8 output: {exc}", file=sys.stderr)
        return None, None, 2
    if status == 1 and not findings:
        print(output)
        print("flake8 failed without any parseable violations; refusing to pass", file=sys.stderr)
        return None, None, 1
    if status == 0 and findings:
        print(output)
        print(
            "flake8 returned success while reporting violations; refusing to pass",
            file=sys.stderr,
        )
        return None, None, 2
    return findings, examples, None


def compare_baseline(findings, baseline):
    regressions = findings - baseline
    stale_entries = baseline - findings
    return regressions, stale_entries


def _counts_by_file_code(findings):
    counts = collections.Counter()
    for (path, code, _message, _source, _context), count in findings.items():
        counts[(path, code)] += count
    return counts


def report_mismatches(
    regressions,
    stale_entries,
    examples,
    baseline,
    findings,
    baseline_path,
):
    before = _counts_by_file_code(baseline)
    after = _counts_by_file_code(findings)
    if stale_entries:
        print("flake8 baseline is stale -- removed violation(s) must update it:\n")
        for key, count in sorted(stale_entries.items()):
            path, code, message, source, context = key
            print(
                f"  {path}: {code} count {before[(path, code)]} -> "
                f"{after[(path, code)]} ({count} removed identity occurrence(s))"
            )
            print(f"    {message} | {source.strip()} | {context}")
        print()
    if regressions:
        print("flake8 baseline exceeded -- new violation identity/identities introduced:\n")
        for key, count in sorted(regressions.items()):
            path, code, _message, _source, _context = key
            print(
                f"  {path}: {code} count {before[(path, code)]} -> "
                f"{after[(path, code)]} ({count} new identity occurrence(s))"
            )
            example = examples.get(key)
            if example is None:
                example = next(
                    (line for candidate, line in examples.items() if candidate[:3] == key[:3]),
                    f"{path}: {code} {key[2]}",
                )
            print(f"    e.g. {example}")
    print(
        "Fix new identities first. After cleanup, regenerate on canonical "
        "Python 3.12 with `python scripts/check_flake8_baseline.py "
        f"--write-baseline` and review the {baseline_path} diff."
    )
    return 1


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="*", default=DEFAULT_TARGETS)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Update the baseline after cleanup; new identities are refused.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help="Path to the baseline JSON file.",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=REPO_ROOT,
        help="Directory Flake8 is invoked from.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if sys.version_info[:2] != CANONICAL_PYTHON:
        print(
            "Refusing to run the flake8 baseline gate outside Python "
            f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]} "
            f"(running {sys.version_info[0]}.{sys.version_info[1]}); "
            "tokenizer differences make findings incomparable to the "
            "canonical baseline.",
            file=sys.stderr,
        )
        return 2
    toolchain_problems = verify_toolchain()
    if toolchain_problems:
        print(
            "flake8 toolchain mismatch; refusing to run an incomplete policy:",
            file=sys.stderr,
        )
        for problem in toolchain_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    try:
        policy_fingerprint = compute_policy_fingerprint(args.cwd, args.targets)
        baseline = load_baseline(args.baseline, policy_fingerprint)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"invalid flake8 baseline {args.baseline}: {exc}", file=sys.stderr)
        return 2

    output, error_output, flake8_status = run_flake8(args.targets, args.cwd)
    findings, examples, failure_status = validate_flake8_result(
        output,
        error_output,
        flake8_status,
        args.cwd,
    )
    if failure_status is not None:
        return failure_status

    if args.write_baseline:
        regressions, _stale = compare_baseline(findings, baseline)
        if regressions:
            return report_mismatches(
                regressions,
                collections.Counter(),
                examples,
                baseline,
                findings,
                args.baseline,
            )
        write_baseline(findings, args.baseline, policy_fingerprint)
        return 0

    regressions, stale_entries = compare_baseline(findings, baseline)
    if regressions or stale_entries:
        return report_mismatches(
            regressions,
            stale_entries,
            examples,
            baseline,
            findings,
            args.baseline,
        )
    print(f"flake8: {sum(findings.values())} violation(s) match the identity baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
