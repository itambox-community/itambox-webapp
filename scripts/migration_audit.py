#!/usr/bin/env python3
"""Build a deterministic migration inventory without importing Django.

The first-party migration graph is the 1.0 baseline: per app a ``0001_baseline`` (plus the
relation, seed and guard migrations that follow it), each carrying ``replaces`` for the
prerelease identities it supersedes. Those identities are frozen in the checked preflight
manifest; this audit proves the files on disk and the manifest agree.
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 4
OPERATION_TYPES = (
    "RunPython",
    "RunSQL",
    "SeparateDatabaseAndState",
    "BtreeGistExtension",
)

# Reviewed, human-maintained rationale for every migration that does more than the ORM can
# derive from the models. A migration with such operations and no entry here fails the audit,
# so new data or DDL migrations need an explicit review.
REVIEWED_SEMANTICS = {
    "assets.0001_baseline": (
        "Enables the PostgreSQL btree_gist extension required by the asset reservation exclusion constraint."
    ),
    "assets.0004_seed_reference_data": (
        "Seeds the canonical asset status labels and depreciation policies; idempotent and reversible."
    ),
    "assets.0005_core_vocabulary": (
        "Creates the normative core specification vocabulary (choice sets, choices, fields, ordered "
        "fieldsets, category defaults) without creating concrete Asset Types; reconciliation is idempotent "
        "and the reverse is refused."
    ),
    "assets.0006_core_vocabulary_german_translations": (
        "Seeds explicit German translations for the shipped core vocabulary; only the itambox namespace is "
        "matched, tenant-authored definitions are untouched."
    ),
    "assets.0007_asset_type_identity_guard": (
        "Installs the PostgreSQL guard that keeps Asset Type connector identity immutable and refuses hard "
        "deletion of core and library Asset Types; fully reversible."
    ),
    "extras.0003_definition_guards": (
        "Installs the PostgreSQL guards for permanent reusable-definition identities, immutable definition "
        "and choice identities and global-field membership rejection; fully reversible."
    ),
    "extras.0005_label_template_object_types": (
        "Binds every existing label template to assets.asset, the only object type the former engine printed, "
        "and moves the never-rendered Data Matrix format to QR; idempotent, schema reverse is the field removal."
    ),
    "extras.0004_drop_legacy_column_default": (
        "Drops a database default that only databases created by earlier prereleases carry, so both histories "
        "converge on one schema; idempotent."
    ),
    "users.0002_unique_constraint_names": (
        "Renames hash-suffixed scim_id unique constraints created by earlier prereleases to the canonical "
        "name a fresh install gets; idempotent."
    ),
}


def _call_name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_noop(node):
    return (isinstance(node, ast.Name) and node.id == "noop") or (
        isinstance(node, ast.Attribute)
        and node.attr == "noop"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr in {"RunPython", "RunSQL"}
    )


def _literal_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _migration_class(tree):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            return node
    raise ValueError("missing Migration class")


def _assignment(class_node, name):
    for node in class_node.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return node.value
    return None


def _dependencies(node):
    dependencies = []
    special_bootstrap = False
    if not isinstance(node, (ast.List, ast.Tuple)):
        return dependencies, special_bootstrap
    for dependency in node.elts:
        if isinstance(dependency, ast.Tuple) and len(dependency.elts) == 2:
            app = _literal_string(dependency.elts[0])
            name = _literal_string(dependency.elts[1])
            if app and name:
                dependencies.append(f"{app}.{name}")
        elif isinstance(dependency, ast.Call) and _call_name(dependency.func) == "swappable_dependency":
            special_bootstrap = True
    return sorted(dependencies), special_bootstrap


def _replaces(node):
    if node is None:
        return []
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise ValueError("replaces must be a list or tuple of migration targets")
    targets = []
    for target in node.elts:
        if not isinstance(target, ast.Tuple) or len(target.elts) != 2:
            raise ValueError("replaces contains a malformed migration target")
        app = _literal_string(target.elts[0])
        name = _literal_string(target.elts[1])
        if not app or not name:
            raise ValueError("replaces contains a non-literal migration target")
        targets.append(f"{app}.{name}")
    return targets


def _graph_summary(node_ids, edges):
    node_ids = set(node_ids)
    edges = sorted(set(edges))
    targets = {target for _, target in edges}
    sources = {source for source, _ in edges}
    return {
        "edges": [list(edge) for edge in edges],
        "leaves": sorted(node_ids - sources),
        "nodes": sorted(node_ids),
        "roots": sorted(node_ids - targets),
    }


def _has_cycle(node_ids, edges):
    successors = {node_id: [] for node_id in node_ids}
    indegrees = {node_id: 0 for node_id in node_ids}
    for source, target in edges:
        successors[source].append(target)
        indegrees[target] += 1
    ready = sorted(node_id for node_id, degree in indegrees.items() if degree == 0)
    visited = 0
    while ready:
        node_id = ready.pop(0)
        visited += 1
        for target in sorted(successors[node_id]):
            indegrees[target] -= 1
            if indegrees[target] == 0:
                ready.append(target)
                ready.sort()
    return visited != len(indegrees)


def _reverse_argument(call, keyword, position):
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if len(call.args) > position:
        return call.args[position]
    return None


def _operation_summary(operations_node):
    summary = {
        operation: {"with_noop_reverse": 0, "with_reverse": 0, "without_reverse": 0}
        for operation in OPERATION_TYPES[:2]
    }
    summary["SeparateDatabaseAndState"] = {"count": 0}
    summary["BtreeGistExtension"] = {"count": 0}

    if operations_node is None:
        return summary

    for call in (node for node in ast.walk(operations_node) if isinstance(node, ast.Call)):
        operation = _call_name(call.func)
        if operation in {"SeparateDatabaseAndState", "BtreeGistExtension"}:
            summary[operation]["count"] += 1
            continue
        if operation not in {"RunPython", "RunSQL"}:
            continue
        reverse = _reverse_argument(
            call,
            "reverse_code" if operation == "RunPython" else "reverse_sql",
            1,
        )
        if reverse is None:
            classification = "without_reverse"
        elif _is_noop(reverse):
            classification = "with_noop_reverse"
        else:
            classification = "with_reverse"
        summary[operation][classification] += 1

    return summary


def _migration_files(source_root):
    return sorted(Path(source_root).glob("*/migrations/[0-9]*.py"))


def _read_migration(path, source_root):
    app = path.parent.parent.name
    migration_id = f"{app}.{path.stem}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        migration_class = _migration_class(tree)
        dependencies, has_swappable_dependency = _dependencies(_assignment(migration_class, "dependencies"))
        run_before, _ = _dependencies(_assignment(migration_class, "run_before"))
        replaces = _replaces(_assignment(migration_class, "replaces"))
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from error
    return {
        "app": app,
        "dependencies": dependencies,
        "id": migration_id,
        "operations": _operation_summary(_assignment(migration_class, "operations")),
        "path": path.relative_to(Path(source_root).parent).as_posix(),
        "replaces": sorted(replaces),
        "run_before": run_before,
        "swappable_user_dependency": has_swappable_dependency,
    }


def _has_custom_operations(migration):
    operations = migration["operations"]
    return (
        sum(operations["RunPython"].values()) > 0
        or sum(operations["RunSQL"].values()) > 0
        or operations["SeparateDatabaseAndState"]["count"] > 0
        or operations["BtreeGistExtension"]["count"] > 0
    )


def _validate_replacements(migrations):
    current_ids = {migration["id"] for migration in migrations}
    seen = {}
    for migration in migrations:
        for target in migration["replaces"]:
            if target.split(".", 1)[0] != migration["app"]:
                raise ValueError(f"{migration['id']} replaces a migration of another app: {target}")
            if target in current_ids:
                raise ValueError(f"{migration['id']} replaces a migration that still exists: {target}")
            if target in seen:
                raise ValueError(f"{target} is replaced by both {seen[target]} and {migration['id']}")
            seen[target] = migration["id"]


def _validate_reviewed_semantics(migrations, reviewed):
    by_id = {migration["id"]: migration for migration in migrations}
    for migration in migrations:
        if _has_custom_operations(migration) and migration["id"] not in reviewed:
            raise ValueError(f"{migration['id']} has custom operations without a reviewed rationale")
    for migration_id in reviewed:
        if migration_id not in by_id or not _has_custom_operations(by_id[migration_id]):
            raise ValueError(f"reviewed rationale names a migration without custom operations: {migration_id}")


def _graph_edges(migrations):
    node_ids = {migration["id"] for migration in migrations}
    apps = {migration["app"] for migration in migrations}
    user_roots = sorted(migration["id"] for migration in migrations if migration["app"] == "users")
    user_root = user_roots[0] if user_roots else None
    edges = set()
    for migration in migrations:
        for dependency in migration["dependencies"]:
            if dependency.split(".", 1)[0] not in apps:
                continue
            if dependency not in node_ids:
                raise ValueError(f"{migration['id']} depends on a missing first-party migration: {dependency}")
            edges.add((dependency, migration["id"]))
        edges.update((migration["id"], target) for target in migration["run_before"] if target in node_ids)
        if migration["swappable_user_dependency"] and user_root and migration["app"] != "users":
            edges.add((user_root, migration["id"]))
    return node_ids, sorted(edges)


def _app_leaves(node_ids, edges):
    same_app_sources = {source for source, target in edges if source.split(".", 1)[0] == target.split(".", 1)[0]}
    app_leaves = {}
    for migration_id in node_ids - same_app_sources:
        app_leaves.setdefault(migration_id.split(".", 1)[0], []).append(migration_id)
    conflicting = {app: sorted(leaves) for app, leaves in app_leaves.items() if len(leaves) != 1}
    if conflicting:
        raise ValueError(f"every app must end in exactly one migration leaf: {sorted(conflicting.items())}")
    return app_leaves


def _build_graph(migrations):
    node_ids, edges = _graph_edges(migrations)
    if _has_cycle(node_ids, edges):
        raise ValueError("first-party migration graph contains a cycle")
    return _graph_summary(node_ids, edges), _app_leaves(node_ids, edges)


def build_inventory(source_root, reviewed_semantics=None):
    source_root = Path(source_root)
    reviewed = REVIEWED_SEMANTICS if reviewed_semantics is None else reviewed_semantics
    migrations = [_read_migration(path, source_root) for path in _migration_files(source_root)]
    graph, app_leaves = _build_graph(migrations)
    graph["app_leaves"] = sorted(leaves[0] for leaves in app_leaves.values())
    _validate_replacements(migrations)
    _validate_reviewed_semantics(migrations, reviewed)
    replaced = sorted(target for migration in migrations for target in migration["replaces"])
    return {
        "graph": graph,
        "migrations": [
            {
                "dependencies": migration["dependencies"],
                "id": migration["id"],
                "operations": migration["operations"],
                "path": migration["path"],
                "replaces_count": len(migration["replaces"]),
                "reviewed_semantics": reviewed.get(migration["id"]),
                "run_before": migration["run_before"],
            }
            for migration in migrations
        ],
        "replaced_ids": replaced,
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "apps": len({migration["app"] for migration in migrations}),
            "migrations": len(migrations),
            "replacing_migrations": sum(1 for migration in migrations if migration["replaces"]),
            "replaced_identities": len(replaced),
            "reviewed_custom_migrations": len(reviewed),
        },
    }


_PREFLIGHT_MANIFEST_LIST_FIELDS = (
    "first_party_apps",
    "historical_ids",
    "replacement_ids",
    "replacement_target_ids",
    "baseline_ids",
    "post_transition_ids",
    "post_transition_leaf_ids",
    "current_leaf_ids",
)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PREDECESSOR_RECOGNITION_STATE_RE = re.compile(r"^supported-predecessor-[a-z0-9]+(?:-[a-z0-9]+)*$")
SUPPORTED_PREDECESSOR_STATES = frozenset(
    {
        "complete-old-history-no-replacement",
        "complete-replacement-recognition",
    }
)


def load_preflight_manifest(path):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read migration preflight manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("migration preflight manifest must be a JSON object")
    return manifest


def _manifest_list(manifest, key):
    value = manifest.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"migration preflight manifest field {key} must be a list of strings")
    if value != sorted(set(value)):
        raise ValueError(f"migration preflight manifest field {key} must be sorted and unique")
    return value


def _assert_manifest_ids(manifest, key, expected):
    actual = manifest.get(key)
    if actual != expected:
        actual_count = len(actual) if isinstance(actual, list) else "invalid"
        raise ValueError(
            f"migration preflight manifest field {key} mismatch: expected {len(expected)}, got {actual_count}"
        )


def _validate_predecessor_recognition(recognition, recognition_states, post_transition_ids):
    if not isinstance(recognition, dict):
        raise ValueError("migration preflight manifest predecessor recognition must be an object")
    recognition_state = recognition.get("state")
    if (
        not isinstance(recognition_state, str)
        or not _PREDECESSOR_RECOGNITION_STATE_RE.fullmatch(recognition_state)
        or recognition_state in recognition_states
    ):
        raise ValueError("migration preflight manifest predecessor recognition state must be unique and recognized")
    if recognition.get("replacement") not in {"absent", "complete"}:
        raise ValueError("migration preflight manifest predecessor recognition replacement is invalid")
    recognition_post_ids = recognition.get("post_transition_ids")
    if (
        not isinstance(recognition_post_ids, list)
        or any(not isinstance(item, str) for item in recognition_post_ids)
        or recognition_post_ids != sorted(set(recognition_post_ids))
        or not set(recognition_post_ids).issubset(set(post_transition_ids))
    ):
        raise ValueError("migration preflight manifest predecessor recognition post-transition IDs are invalid")
    return recognition_state


def _validate_manifest_predecessors(manifest):
    transition_release_sha = manifest.get("transition_release_sha")
    if not isinstance(transition_release_sha, str) or not _GIT_SHA_RE.fullmatch(transition_release_sha):
        raise ValueError("migration preflight manifest transition_release_sha must be a lowercase 40-character Git SHA")
    predecessors = manifest.get("recognized_states")
    if not isinstance(predecessors, list) or not predecessors:
        raise ValueError("migration preflight manifest recognized_states must be a non-empty list")
    names = []
    revisions = set()
    recognition_states = set()
    for predecessor in predecessors:
        if not isinstance(predecessor, dict):
            raise ValueError("migration preflight manifest predecessor entries must be objects")
        name = predecessor.get("name")
        revision = predecessor.get("revision")
        state = predecessor.get("state")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("migration preflight manifest predecessor names must be unique and non-empty")
        if not isinstance(revision, str) or not _GIT_SHA_RE.fullmatch(revision):
            raise ValueError(
                "migration preflight manifest predecessor revisions must be lowercase 40-character Git SHAs"
            )
        if state not in SUPPORTED_PREDECESSOR_STATES:
            raise ValueError("migration preflight manifest predecessor state is not recognized")
        recognition_state = _validate_predecessor_recognition(
            predecessor.get("recognition"),
            recognition_states,
            manifest["post_transition_ids"],
        )
        names.append(name)
        revisions.add(revision)
        recognition_states.add(recognition_state)
    if transition_release_sha not in revisions:
        raise ValueError("migration preflight manifest transition release is not a named predecessor revision")


def validate_preflight_manifest_git_objects(manifest, repository_root):
    """Require every declared predecessor identity to resolve to a local commit object."""

    revisions = {manifest["transition_release_sha"]}
    revisions.update(predecessor["revision"] for predecessor in manifest["recognized_states"])
    for revision in sorted(revisions):
        try:
            result = subprocess.run(
                ["git", "-C", str(repository_root), "cat-file", "-e", f"{revision}^{{commit}}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ValueError("Git is unavailable for migration preflight identity verification") from exc
        if result.returncode:
            raise ValueError(f"migration preflight manifest revision is not a local Git commit: {revision}")


def _preflight_manifest_expected_ids(inventory, manifest):
    """Source-derived manifest fields.

    The prerelease identities (historical and replacement ids, plus the legacy post-transition
    rows) are reviewed identity metadata; the source proves them by the union of its ``replaces``.
    """

    current_ids = sorted(migration["id"] for migration in inventory["migrations"])
    replaced = set(inventory["replaced_ids"])
    historical = set(manifest.get("historical_ids", ()))
    replacement = set(manifest.get("replacement_ids", ()))
    legacy_post = replaced - historical - replacement
    leaves = inventory["graph"]["app_leaves"]
    return {
        "first_party_apps": sorted({migration_id.split(".", 1)[0] for migration_id in current_ids}),
        "post_transition_ids": sorted(legacy_post | set(current_ids)),
        "post_transition_leaf_ids": leaves,
        "current_leaf_ids": leaves,
    }


def render_preflight_manifest(inventory, manifest):
    """Refresh source-derived manifest fields without changing reviewed identity metadata."""

    rendered = dict(manifest)
    rendered.update(_preflight_manifest_expected_ids(inventory, manifest))
    return json.dumps(rendered, indent=2, sort_keys=True) + "\n"


def validate_preflight_manifest(inventory, manifest):
    if manifest.get("schema_version") != 1:
        raise ValueError("migration preflight manifest schema_version must be 1")
    if manifest.get("layout") != "normalized":
        raise ValueError("migration preflight manifest layout must be normalized")
    for key in _PREFLIGHT_MANIFEST_LIST_FIELDS:
        _manifest_list(manifest, key)
    legacy = set(manifest["historical_ids"]) | set(manifest["replacement_ids"])
    legacy |= set(manifest["post_transition_ids"]) - {migration["id"] for migration in inventory["migrations"]}
    if legacy != set(inventory["replaced_ids"]):
        raise ValueError("migration preflight manifest legacy identities do not match the source replaces")
    for key, expected in _preflight_manifest_expected_ids(inventory, manifest).items():
        _assert_manifest_ids(manifest, key, expected)
    if manifest["baseline_ids"] != manifest["replacement_ids"]:
        raise ValueError("migration preflight manifest baseline IDs must equal the replacement IDs")
    if manifest["replacement_target_ids"] != manifest["historical_ids"]:
        raise ValueError("migration preflight manifest replacement targets must equal the historical IDs")
    _validate_manifest_predecessors(manifest)
    return manifest


def render_inventory(inventory):
    return json.dumps(inventory, indent=2, sort_keys=True) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the audit is stale")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--manifest", type=Path, help="checked runtime preflight manifest")
    parser.add_argument(
        "--skip-git-identity-check",
        action="store_true",
        help="skip Git-object identity verification for an explicitly isolated fixture run",
    )
    parser.add_argument(
        "--write-preflight-manifest",
        action="store_true",
        help="refresh source-derived fields in the checked runtime preflight manifest",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    source_root = args.source_root or repository_root / "itambox"
    canonical_source_root = (repository_root / "itambox").resolve()
    output = args.output or repository_root / "scripts" / "migration_audit.json"
    inventory = build_inventory(source_root)
    manifest_is_requested = source_root.resolve() == canonical_source_root or args.manifest is not None
    if args.write_preflight_manifest and args.check:
        parser.error("--check and --write-preflight-manifest are mutually exclusive")
    if args.write_preflight_manifest and not manifest_is_requested:
        parser.error("--write-preflight-manifest requires the canonical source root or --manifest")
    if manifest_is_requested:
        manifest_path = args.manifest or source_root / "core" / "migration_baseline_manifest.json"
        try:
            manifest = load_preflight_manifest(manifest_path)
            _validate_manifest_predecessors(manifest)
            if args.skip_git_identity_check:
                print("migration preflight manifest Git identity check explicitly skipped", file=sys.stderr)
            else:
                validate_preflight_manifest_git_objects(manifest, repository_root)
            if args.write_preflight_manifest:
                refreshed = json.loads(render_preflight_manifest(inventory, manifest))
                validate_preflight_manifest(inventory, refreshed)
                manifest_path.write_text(json.dumps(refreshed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print(f"wrote migration preflight manifest: {manifest_path}")
                return 0
            validate_preflight_manifest(inventory, manifest)
        except ValueError as exc:
            print(f"migration preflight manifest invalid: {exc}", file=sys.stderr)
            return 1
    else:
        print("migration preflight manifest validation skipped for a non-canonical source root", file=sys.stderr)
    rendered = render_inventory(inventory)
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            print(f"migration audit drift: regenerate with {Path(__file__).name}", file=sys.stderr)
            return 1
        print(f"migration audit is current: {output}")
        return 0
    output.write_text(rendered, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
