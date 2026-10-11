#!/usr/bin/env python3
"""Build a deterministic migration inventory without importing Django."""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 3
OPERATION_TYPES = (
    "RunPython",
    "RunSQL",
    "SeparateDatabaseAndState",
    "BtreeGistExtension",
)
POST_TRANSITION_MIGRATIONS = {
    "assets.0101_seed_canonical_missing_status",
    "assets.0102_asset_type_composition_schema",
    "assets.0103_asset_type_specification_conversion",
    "assets.0104_asset_type_core_vocabulary",
    "assets.0105_asset_type_provenance_capture",
    "assets.0106_asset_type_composition_cutover",
    "assets.0107_asset_type_specification_guards",
    "assets.0118_assetdisposal_cancellation_reason_and_more",
    "assets.0119_warranty_supplier",
    "assets.0120_repair_episode",
    "assets.0121_supplier_scoping_and_commercial_fields",
    "assets.0122_repair_maintenance_anchor",
    "assets.0123_asset_archive_leaf_markers",
    "assets.0124_tenant_archive_leaf_markers",
    "compliance.0101_alter_custodyreceipt_signed_at",
    "compliance.0102_clear_unsigned_receipt_timestamps",
    "compliance.0103_alter_custodyreceipt_options",
    "compliance.0104_custodysigningsession",
    "compliance.0105_custodyhandoffdelivery",
    "compliance.0106_remove_custodyreceipt_accepted",
    "compliance.0107_custodyreceipt_assignment_superseded",
    "extras.0101_issue88_drop_legacy_webhook_name_like",
    "extras.0102_alter_event_action",
    "extras.0103_remove_reporttemplate_advanced_mode_and_more",
    "extras.0104_issue183_alert_tenant_reconciliation",
    "extras.0105_reporttemplate_advanced_mode_and_more",
    "extras.0106_scheduledreportscopeauthorization",
    "extras.0107_scheduledreportscopeauthorization_revocation",
    "extras.0108_alertlog_delivery_outcome",
    "extras.0109_webhookdelivery",
    "extras.0110_issue445_task_paths",
    "extras.0111_webhookdelivery_target_claim",
    "extras.0112_backfill_webhookdelivery_targets",
    "extras.0113_upgrade_legacy_webhook_retry_schedules",
    "extras.0114_asset_type_definition_library_schema",
    "extras.0115_asset_type_definition_conversion",
    "extras.0116_asset_type_definition_cutover",
    "extras.0117_asset_type_definition_guards",
    "extras.0122_journalentry_tenant_group",
    "extras.0123_pause_flag_suppressed_report_schedules",
    "extras.0124_scheduled_report_fire_identity_delivery_outcomes",
    "extras.0125_scheduled_report_fire_records_retry_hardening",
    "extras.0126_scheduledreport_last_run_archive",
    "extras.0127_retire_report_designer_legacy",
    "inventory.0101_alter_accessoryassignment_options_and_more",
    "inventory.0102_kititem_leaf_deleted_at",
    "inventory.0103_stock_leaf_deleted_at",
    "organization.0101_membership_external_id_and_more",
    "organization.0102_alter_tenantresourcegrant_options",
    "organization.0103_tenant_resource_grant_expiry",
    "organization.0104_tenant_archive_leaf_markers",
    "procurement.0101_alter_purchaseorder_options",
    "procurement.0102_fulfillmentlink_qty_received",
    "procurement.0103_remove_fulfillmentlink_unique_request_po_line_link_and_more",
    "subscriptions.0101_remove_subscription_auto_renewal_and_more",
    "subscriptions.0102_commercial_vendor_and_terms",
    "subscriptions.0103_unified_vendor_cutover",
    "software.0101_asset_archive_leaf_markers",
    "extras.0128_tenant_archive_leaf_markers",
    "subscriptions.0104_subscriptionassignment_leaf_deleted_at",
    "subscriptions.0105_subscription_renewal_reminder_cycle_date_and_more",
    "users.0101_user_scim_id_usergroup_external_id_usergroup_scim_id_and_more",
    "users.0102_token_updated_at",
    "users.0103_oidcidentity",
    "users.0104_tenant_archive_leaf_markers",
}
ISSUE88_SHARD_RE = re.compile(r"issue88_shard_(\d{2})(?:_|$)")
ALLOWED_DISPOSITIONS = {
    "required-fresh",
    "upgrade-only",
    "safely-replaced-by-final-schema",
    "review-blocker",
}
EXPECTED_BLOCKERS = {
    "organization.0026_remove_tenantrole_tenant_provider_tenant_provider_and_more",
    "organization.0027_drop_legacy_role_models",
    "organization.0034_roleassignment_remove_provider_internal_tenant_and_more",
    "organization.0035_delete_provider_and_more",
    "users.0008_alter_usergroup_options_token_provider_and_more",
    "users.0010_remove_usergroup_users_usergroup_unique_provider_name_active_and_more",
}


def _dispositions(disposition, rationale, migration_ids):
    return {migration_id: {"disposition": disposition, "rationale": rationale} for migration_id in migration_ids}


# This is a checked, human-reviewed policy. It is intentionally independent of
# migration/function names, reversibility syntax, and operation implementation.
SEMANTIC_DISPOSITIONS = {
    **_dispositions(
        "required-fresh",
        (
            "Adds additive WP-13 delivery observability fields and idempotently derives the filterable "
            "delivery_outcome from existing per-channel payloads without mutating delivery_status; fully "
            "reversible."
        ),
        {
            "extras.0108_alertlog_delivery_outcome",
        },
    ),
    **_dispositions(
        "required-fresh",
        (
            "Creates and reconciles the normative final core vocabulary (active specification fields, the "
            "retired foundation tombstone, ordered choice sets and fieldsets, field memberships, and category "
            "defaults) without creating concrete Asset Types."
        ),
        {
            "assets.0104_asset_type_core_vocabulary",
        },
    ),
    **_dispositions(
        "required-fresh",
        ("Deterministically recreates the two required asset seed datasets on the replacement path."),
        {
            "assets.0100_issue88_shard_43_assets_seed",
        },
    ),
    **_dispositions(
        "required-fresh",
        ("Enables the PostgreSQL btree_gist extension required by the asset reservation exclusion constraint."),
        {
            "assets.0100_issue88_shard_42_assets_relations",
        },
    ),
    **_dispositions(
        "required-fresh",
        (
            "Installs the PostgreSQL guard that keeps Asset Type specification identity columns immutable "
            "after creation and refuses hard deletion of core and library Asset Type identities."
        ),
        {
            "assets.0107_asset_type_specification_guards",
        },
    ),
    **_dispositions(
        "required-fresh",
        (
            "Installs the PostgreSQL guards for permanent reusable-definition identities, immutable definition "
            "and choice identities, global-field membership rejection, and the append-only legacy provenance "
            "and specification library archives."
        ),
        {
            "extras.0117_asset_type_definition_guards",
        },
    ),
    **_dispositions(
        "required-fresh",
        ("Pre-provisions the canonical global Missing status outside tenant-scoped audit mutation paths."),
        {
            "assets.0101_seed_canonical_missing_status",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Archives the legacy evidence state of every existing Asset Type and reusable definition as "
            "immutable SpecificationLibraryLegacyProvenance rows before the transitional columns are removed; "
            "supported upgrade states carry no library-managed ownership or reconciliation evidence, so every "
            "archived row is uninitialized."
        ),
        {
            "assets.0105_asset_type_provenance_capture",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Backfills tenant attribution for legacy tenant-less alerts while marking ambiguous or unresolved "
            "targets for operator review."
        ),
        {
            "extras.0104_issue183_alert_tenant_reconciliation",
        },
    ),
    **_dispositions(
        "upgrade-only",
        ("Clears misleading signed_at values on non-accepted custody receipts; pending receipts must stay unsigned."),
        {
            "compliance.0102_clear_unsigned_receipt_timestamps",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Links existing custody receipts to their custody period where unambiguous and marks pending "
            "receipts whose period already ended as superseded; fresh installs have no receipts."
        ),
        {
            "compliance.0107_custodyreceipt_assignment_superseded",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Converts pre-#479 definition identities, field types, relational choices, memberships, and stored "
            "JSON values into the final specification contract while preserving unknown keys and aborting on "
            "ambiguity, and copies the legacy singular Asset Type fieldset relation into the final ordered "
            "composition; a fresh install has nothing to convert or copy."
        ),
        {
            "assets.0103_asset_type_specification_conversion",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Translates beta-era repair episodes onto the maintenance anchor: a disposal linked to the episode "
            "moves to the episode's single repair maintenance, the stand-in's overlapping loan links to it, and "
            "everything that cannot be mapped unambiguously (no or several repair maintenances, a reservation "
            "link, the episode notes, a substitute without a matching loan) is appended to the maintenance notes "
            "or written as a journal entry on the asset. The mapping is lossy, so rollback is restore-first."
        ),
        {
            "assets.0122_repair_maintenance_anchor",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Copies endpoint configuration into immutable webhook delivery target snapshots; endpoint-less "
            "history remains unbound because exact legacy rule provenance cannot be reconstructed safely."
        ),
        {
            "extras.0112_backfill_webhookdelivery_targets",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Normalizes predecessor definition ownership, applicability, lifecycle, and ordered fieldset "
            "membership positions to the final contract and installs the deferred owner/position uniqueness as "
            "immediate; a fresh install has nothing to normalize."
        ),
        {
            "extras.0115_asset_type_definition_conversion",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Populates stable opaque SCIM IDs idempotently and adds scoped group/user correlation fields while "
            "preserving legacy principals for the 1.x dual-read window."
        ),
        {
            "users.0101_user_scim_id_usergroup_external_id_usergroup_scim_id_and_more",
        },
    ),
    **_dispositions(
        "upgrade-only",
        ("Preserves or transforms data/content types for an existing installation."),
        {
            "extras.0101_issue88_drop_legacy_webhook_name_like",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Preserves subscription renewal-term values while normalizing removed legacy lifecycle states "
            "during 1.0 upgrade."
        ),
        {
            "subscriptions.0101_remove_subscription_auto_renewal_and_more",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Restores durable report-designer fields, recovers serialized legacy values, stamps only the "
            "bounded live scheduled set, and reports out-of-bound custom HTML templates."
        ),
        {
            "extras.0105_reporttemplate_advanced_mode_and_more",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Retains physical legacy report-designer columns while removing them from the historical ORM state "
            "before the durable 1.x contract is restored."
        ),
        {
            "extras.0103_remove_reporttemplate_advanced_mode_and_more",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Rewrites the twelve canonical django-q Schedule.func values to their issue-#445 domain-owner "
            "paths forward and back, preserving every other schedule field, PK and row multiplicity."
        ),
        {
            "extras.0110_issue445_task_paths",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Transplants subscription provider profiles into the tenant-scoped supplier catalogue (explicit "
            "link, scope-and-name match, or create), rebinds every subscription including soft-deleted ones, "
            "copies tag links, and repoints generic contact assignments. The merge is lossy, so rollback is "
            "restore-first."
        ),
        {
            "subscriptions.0103_unified_vendor_cutover",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Validates and upgrades delayed legacy webhook retry schedules to assertion-only payloads, moves "
            "exact endpoint-less targets into encrypted durable snapshots, and irreversibly removes queue "
            "secrets."
        ),
        {
            "extras.0113_upgrade_legacy_webhook_retry_schedules",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Pauses registered, active, non-grandfathered report schedules once on deployments that ran the "
            "report designer disabled, so removing the feature flag cannot silently resume suppressed "
            "outbound deliveries; rows and history are preserved and re-enabling resumes deliberately."
        ),
        {
            "extras.0123_pause_flag_suppressed_report_schedules",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Reconciles the django-q registration rows for the Scheduled Reports promotion: marks every "
            "report schedule row with the fire-identity kwarg so a redelivered occurrence can never dispatch "
            "twice, and collapses duplicate registration rows onto the surviving row with the schedule FK "
            "re-pointed; saved schedules, active states, approvals, archives, and next-run state are otherwise "
            "untouched, so no dormant or paused schedule is resumed and no delivery state is rewritten."
        ),
        {
            "extras.0124_scheduled_report_fire_identity_delivery_outcomes",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Registers each schedule's newest accepted occurrence as a per-occurrence fire record so the "
            "idempotency fence becomes exact-match (out-of-order replays and parallel workers can never "
            "discard an older occurrence), and adds the delivery-retry hardening columns to the generation "
            "archive (archived generation scope snapshot plus the lease-fenced retry claim token); saved "
            "schedules, approvals, archives, and delivery outcomes are otherwise untouched."
        ),
        {
            "extras.0125_scheduled_report_fire_records_retry_hardening",
        },
    ),
    **_dispositions(
        "upgrade-only",
        (
            "Refuses live templates using the unsupported legacy CSV shape before dropping both retired fields; "
            "canonical rows and custom HTML are preserved, and reverse requires restore-first rollback."
        ),
        {
            "extras.0127_retire_report_designer_legacy",
        },
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


def _validate_issue88_shard_chain(replacement_migrations):
    issue88_shards = []
    for migration in replacement_migrations:
        match = ISSUE88_SHARD_RE.search(migration["id"])
        if match:
            issue88_shards.append((int(match.group(1)), migration))
    if not issue88_shards:
        return 0
    if len(issue88_shards) != len(replacement_migrations):
        raise ValueError("issue88 replacements must not mix with unnumbered shards")

    issue88_shards.sort(key=lambda item: item[0])
    ordinals = [ordinal for ordinal, _ in issue88_shards]
    expected_ordinals = list(range(1, len(issue88_shards) + 1))
    if ordinals != expected_ordinals:
        raise ValueError(f"issue88 shard ordinals must be contiguous: expected={expected_ordinals}, actual={ordinals}")

    previous_by_app = {}
    for index, (_, migration) in enumerate(issue88_shards):
        dependencies = set(migration["dependencies"])
        if index:
            predecessor = issue88_shards[index - 1][1]["id"]
            if predecessor not in dependencies:
                raise ValueError(f"issue88 shard lacks immediate predecessor: {migration['id']} -> {predecessor}")
        app = migration["id"].split(".", 1)[0]
        previous_same_app = previous_by_app.get(app)
        if previous_same_app and previous_same_app not in dependencies:
            raise ValueError(f"issue88 shard lacks previous same-app shard: {migration['id']} -> {previous_same_app}")
        previous_by_app[app] = migration["id"]
    return max(0, len(issue88_shards) - 1)


def _validate_post_transition_dependencies(
    post_transition_migrations,
    effective_graph,
    first_party_apps,
):
    if not post_transition_migrations:
        return
    effective_nodes = set(effective_graph["nodes"])
    effective_leaves = set(effective_graph["leaves"])
    post_transition_ids = {migration["id"] for migration in post_transition_migrations}
    known_predecessors = effective_nodes | post_transition_ids
    for migration in post_transition_migrations:
        dependencies = set(migration["dependencies"])
        unknown_dependencies = sorted(
            dependency
            for dependency in dependencies
            if dependency.split(".", 1)[0] in first_party_apps and dependency not in known_predecessors
        )
        if unknown_dependencies:
            raise ValueError(f"unknown first-party post-transition dependency targets: {unknown_dependencies}")
        missing_leaves = sorted(effective_leaves - dependencies)
        if missing_leaves:
            raise ValueError(
                f"post-transition migration lacks effective leaf dependency: {migration['id']} -> {missing_leaves}"
            )


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


def build_inventory(  # noqa: C901 - graph validation is intentionally one coordinated pass
    source_root,
    semantic_dispositions=None,
    expected_blockers=None,
):
    source_root = Path(source_root)
    semantic_dispositions = SEMANTIC_DISPOSITIONS if semantic_dispositions is None else semantic_dispositions
    expected_blockers = EXPECTED_BLOCKERS if expected_blockers is None else set(expected_blockers)
    migrations = []
    for path in sorted(source_root.glob("*/migrations/[0-9]*.py")):
        app = path.parent.parent.name
        migration_id = f"{app}.{path.stem}"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            migration_class = _migration_class(tree)
        except (SyntaxError, ValueError) as error:
            raise ValueError(f"{path}: {error}") from error
        dependencies, has_swappable_dependency = _dependencies(_assignment(migration_class, "dependencies"))
        run_before, _ = _dependencies(_assignment(migration_class, "run_before"))
        replaces_node = _assignment(migration_class, "replaces")
        try:
            replaces = _replaces(replaces_node)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
        special_bootstrap = app == "users" and "users.0001_initial" in run_before
        operations = _operation_summary(_assignment(migration_class, "operations"))
        custom_operation_presence = {
            "RunPython": sum(operations["RunPython"].values()) > 0,
            "RunSQL": sum(operations["RunSQL"].values()) > 0,
            "SeparateDatabaseAndState": operations["SeparateDatabaseAndState"]["count"] > 0,
            "BtreeGistExtension": operations["BtreeGistExtension"]["count"] > 0,
        }
        reviewed_disposition = semantic_dispositions.get(migration_id)
        migrations.append(
            {
                "dependencies": dependencies,
                "id": migration_id,
                "is_replacement": replaces_node is not None,
                "operations": operations,
                "path": path.relative_to(source_root.parent).as_posix(),
                "replaces": replaces,
                "reviewed_disposition": reviewed_disposition,
                "run_before": run_before,
                "special_users_bootstrap": special_bootstrap,
                "swappable_user_dependency": has_swappable_dependency,
                "syntactic_facts": {
                    "has_custom_operations": custom_operation_presence,
                },
            }
        )

    replacement_migrations = [migration for migration in migrations if migration["is_replacement"]]
    post_transition_migrations = [
        migration for migration in migrations if migration["id"] in POST_TRANSITION_MIGRATIONS
    ]
    historical_migrations = [
        migration
        for migration in migrations
        if not migration["is_replacement"] and migration["id"] not in POST_TRANSITION_MIGRATIONS
    ]
    node_ids = {migration["id"] for migration in historical_migrations}
    user_bootstraps = sorted(
        migration["id"] for migration in historical_migrations if migration["special_users_bootstrap"]
    )
    user_bootstrap = user_bootstraps[0] if len(user_bootstraps) == 1 else None
    edges = {
        (dependency, migration["id"])
        for migration in historical_migrations
        for dependency in migration["dependencies"]
        if dependency in node_ids
    }
    edges.update(
        (migration["id"], target)
        for migration in historical_migrations
        for target in migration["run_before"]
        if target in node_ids
    )
    if user_bootstrap:
        edges.update(
            (user_bootstrap, migration["id"])
            for migration in historical_migrations
            if migration["swappable_user_dependency"]
        )
    edges = sorted(edges)
    historical_graph = _graph_summary(node_ids, edges)
    first_party_apps = {migration["id"].split(".", 1)[0] for migration in migrations}
    explicit_replacement_chain_edges = 0

    if replacement_migrations:
        explicit_replacement_chain_edges = _validate_issue88_shard_chain(replacement_migrations)
        replacement_targets = [target for migration in replacement_migrations for target in migration["replaces"]]
        unknown_targets = sorted(set(replacement_targets) - node_ids)
        if unknown_targets and node_ids:
            # A normalized layout deletes the replaced history, so the replacement targets then
            # carry the frozen historical identities and cannot be resolved against source files.
            raise ValueError(f"unknown replacement targets: {unknown_targets}")
        duplicate_targets = sorted(
            target for target in set(replacement_targets) if replacement_targets.count(target) > 1
        )
        if duplicate_targets:
            raise ValueError(f"duplicate replacement targets: {duplicate_targets}")
        uncovered = sorted(node_ids - set(replacement_targets))
        if uncovered:
            raise ValueError(f"replacement coverage incomplete: {uncovered}")

        replacement_by_target = {
            target: migration["id"] for migration in replacement_migrations for target in migration["replaces"]
        }
        effective_node_ids = {migration["id"] for migration in replacement_migrations}
        unknown_dependency_targets = sorted(
            {
                dependency
                for migration in replacement_migrations
                for dependency in migration["dependencies"]
                if dependency.split(".", 1)[0] in first_party_apps and dependency not in effective_node_ids
            }
        )
        if unknown_dependency_targets:
            raise ValueError(f"unknown first-party replacement dependency targets: {unknown_dependency_targets}")
        unknown_run_before_targets = sorted(
            {
                target
                for migration in replacement_migrations
                for target in migration["run_before"]
                if target.split(".", 1)[0] in first_party_apps
                and target not in effective_node_ids
                and target not in replacement_by_target
            }
        )
        if unknown_run_before_targets:
            raise ValueError(f"unknown first-party replacement run_before targets: {unknown_run_before_targets}")
        effective_edges = {
            (replacement_by_target[source], replacement_by_target[target])
            for source, target in edges
            if replacement_by_target[source] != replacement_by_target[target]
        }
        effective_edges.update(
            (dependency, migration["id"])
            for migration in replacement_migrations
            for dependency in migration["dependencies"]
            if dependency in effective_node_ids
        )
        effective_edges.update(
            (
                migration["id"],
                replacement_by_target.get(target, target),
            )
            for migration in replacement_migrations
            for target in migration["run_before"]
            if target in effective_node_ids or target in replacement_by_target
            if migration["id"] != replacement_by_target.get(target, target)
        )
        if _has_cycle(effective_node_ids, effective_edges):
            raise ValueError("effective replacement graph contains a cycle")
        effective_graph = _graph_summary(effective_node_ids, effective_edges)
    else:
        effective_graph = historical_graph

    _validate_post_transition_dependencies(
        post_transition_migrations,
        effective_graph,
        first_party_apps,
    )

    targets = {target for _, target in edges}
    sources = {source for source, _ in edges}
    by_id = {migration["id"]: migration for migration in migrations}
    custom_operation_ids = {
        migration["id"]
        for migration in migrations
        if any(migration["syntactic_facts"]["has_custom_operations"].values())
    }
    policy_ids = set(semantic_dispositions)
    malformed_policy = sorted(
        migration_id
        for migration_id, policy in semantic_dispositions.items()
        if set(policy) != {"disposition", "rationale"}
        or not isinstance(policy["rationale"], str)
        or not policy["rationale"].strip()
    )
    if malformed_policy:
        raise ValueError(f"malformed semantic policy entries: {malformed_policy}")
    invalid_dispositions = {
        migration_id: policy["disposition"]
        for migration_id, policy in semantic_dispositions.items()
        if policy["disposition"] not in ALLOWED_DISPOSITIONS
    }
    known_historical_ids = node_ids | {
        target for migration in replacement_migrations for target in migration["replaces"]
    }
    missing_blockers = expected_blockers - known_historical_ids
    if invalid_dispositions:
        raise ValueError(f"invalid semantic dispositions: {invalid_dispositions}")
    if custom_operation_ids != policy_ids:
        raise ValueError(
            "semantic policy coverage mismatch: "
            f"unclassified={sorted(custom_operation_ids - policy_ids)}, "
            f"without_custom_operations={sorted(policy_ids - custom_operation_ids)}"
        )
    if missing_blockers:
        raise ValueError(f"unknown expected blockers: {sorted(missing_blockers)}")

    per_app = {}
    for migration in historical_migrations:
        app = migration["id"].split(".", 1)[0]
        app_ids = {item["id"] for item in historical_migrations if item["id"].startswith(f"{app}.")}
        app_edges = {(source, target) for source, target in edges if source in app_ids and target in app_ids}
        app_sources = {source for source, _ in app_edges}
        app_targets = {target for _, target in app_edges}
        per_app[app] = {
            "local_roots": sorted(app_ids - app_targets),
            "local_leaves": sorted(app_ids - app_sources),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "historical_graph": historical_graph,
        "effective_graph": effective_graph,
        "post_transition_migrations": [migration["id"] for migration in post_transition_migrations],
        "summary": {
            "first_party_edges": len(edges),
            "first_party_nodes": len(historical_migrations),
            "replacement_shards": len(replacement_migrations),
            "replacement_targets": sum(len(migration["replaces"]) for migration in replacement_migrations),
            "post_transition_migrations": len(post_transition_migrations),
            "explicit_replacement_chain_edges": explicit_replacement_chain_edges,
            "missing_replacement_targets": 0,
            "duplicate_replacement_targets": 0,
            "effective_replacement_quotient_acyclic": True,
            "custom_operation_file_counts": {
                operation: sum(
                    migration["syntactic_facts"]["has_custom_operations"][operation]
                    for migration in historical_migrations
                )
                for operation in OPERATION_TYPES
            },
            "global_leaves": sorted(node_ids - sources),
            "global_roots": sorted(node_ids - targets),
            "per_app_local_roots_and_leaves": per_app,
        },
        "reviewed_semantics": {
            "blockers": sorted(expected_blockers),
            **{
                disposition.replace("-", "_"): sorted(
                    migration_id
                    for migration_id, policy in semantic_dispositions.items()
                    if policy["disposition"] == disposition
                )
                for disposition in sorted(ALLOWED_DISPOSITIONS)
            },
        },
        "special_users_bootstrap": {
            "migration": user_bootstrap,
            "run_before": (by_id[user_bootstrap]["run_before"] if user_bootstrap else []),
            "swappable_dependents": sorted(
                migration_id
                for migration_id, migration in by_id.items()
                if not migration["is_replacement"] and migration["swappable_user_dependency"]
            ),
        },
        "migrations": migrations,
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


def _post_transition_graph(inventory):
    post_ids = set(inventory["post_transition_migrations"])
    effective_ids = set(inventory["effective_graph"]["nodes"])
    edges = {tuple(edge) for edge in inventory["effective_graph"]["edges"]}
    by_id = {migration["id"]: migration for migration in inventory["migrations"]}
    for migration_id in post_ids:
        migration = by_id[migration_id]
        for dependency in migration["dependencies"]:
            if dependency in effective_ids or dependency in post_ids:
                edges.add((dependency, migration_id))
        for target in migration["run_before"]:
            if target in effective_ids or target in post_ids:
                edges.add((migration_id, target))
    return _graph_summary(effective_ids | post_ids, edges)


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


def _preflight_manifest_expected_ids(inventory):
    post_ids = set(inventory["post_transition_migrations"])
    replacement_migrations = [migration for migration in inventory["migrations"] if migration["is_replacement"]]
    replacement_ids = sorted(migration["id"] for migration in replacement_migrations)
    replacement_target_ids = sorted(target for migration in replacement_migrations for target in migration["replaces"])
    historical_ids = sorted(
        {
            migration["id"]
            for migration in inventory["migrations"]
            if not migration["is_replacement"] and migration["id"] not in post_ids
        }
        | set(replacement_target_ids)
    )
    post_transition_ids = sorted(post_ids)
    current_graph = _post_transition_graph(inventory)
    post_transition_leaf_ids = sorted(post_ids - {source for source, _ in current_graph["edges"] if source in post_ids})
    return {
        "first_party_apps": sorted({migration["id"].split(".", 1)[0] for migration in inventory["migrations"]}),
        "historical_ids": historical_ids,
        "replacement_ids": replacement_ids,
        "replacement_target_ids": replacement_target_ids,
        "baseline_ids": replacement_ids,
        "post_transition_ids": post_transition_ids,
        "post_transition_leaf_ids": post_transition_leaf_ids,
        "current_leaf_ids": current_graph["leaves"],
    }


def render_preflight_manifest(inventory, manifest):
    """Refresh source-derived manifest fields without changing reviewed identity metadata."""

    rendered = dict(manifest)
    rendered.update(_preflight_manifest_expected_ids(inventory))
    return json.dumps(rendered, indent=2, sort_keys=True) + "\n"


def _expected_source_layout(inventory):
    """Derive the reviewed layout from the source graph.

    A source without historical migration files but with replacement migrations is the
    normalized layout: the replaced history has been deleted and only the replacement
    shards carry it. Any other source keeps the transitional layout.
    """

    has_historical = bool(inventory["historical_graph"]["nodes"])
    has_replacements = any(migration["is_replacement"] for migration in inventory["migrations"])
    if not has_historical and has_replacements:
        return "normalized"
    return "transitional"


def validate_preflight_manifest(inventory, manifest):
    if manifest.get("schema_version") != 1:
        raise ValueError("migration preflight manifest schema_version must be 1")
    if manifest.get("layout") != _expected_source_layout(inventory):
        raise ValueError("migration preflight manifest layout must match the current migration source")
    for key in _PREFLIGHT_MANIFEST_LIST_FIELDS:
        _manifest_list(manifest, key)
    for key, expected in _preflight_manifest_expected_ids(inventory).items():
        _assert_manifest_ids(manifest, key, expected)
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
