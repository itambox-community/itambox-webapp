"""Combine several SARIF reports into one single-run analysis payload.

GitHub code scanning identifies an analysis by the pair (tool, category) and
rejects a second analysis for the same pair in one workflow run — including
several ``runs`` of the same tool inside one uploaded file. The per-platform
image reports are therefore merged into **one** run: shared rule list, merged
results, and rewritten rule/artifact indexes.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def _load(paths: list[Path]) -> list[dict]:
    runs: list[dict] = []
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid SARIF JSON: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("runs"), list):
            raise ValueError(f"{path}: not a SARIF document with a runs array")
        runs.extend(document["runs"])
    return runs


def _driver_name(run: dict) -> str:
    tool = run.get("tool") or {}
    driver = tool.get("driver") or {}
    return str(driver.get("name") or "")


def _rule_key(rule: dict) -> str:
    identifier = rule.get("id")
    if identifier:
        return f"id:{identifier}"
    return "rule:" + json.dumps(rule, sort_keys=True)


def _merge_rules(runs: list[dict]) -> tuple[list[dict], list[dict[int, int]]]:
    rules: list[dict] = []
    index_by_key: dict[str, int] = {}
    maps: list[dict[int, int]] = []
    for run in runs:
        run_map: dict[int, int] = {}
        run_rules = (run.get("tool") or {}).get("driver", {}).get("rules") or []
        for position, rule in enumerate(run_rules):
            if not isinstance(rule, dict):
                raise ValueError("SARIF rule entries must be objects")
            key = _rule_key(rule)
            if key not in index_by_key:
                index_by_key[key] = len(rules)
                rules.append(copy.deepcopy(rule))
            run_map[position] = index_by_key[key]
        maps.append(run_map)
    return rules, maps


def _merge_artifacts(runs: list[dict]) -> tuple[list[dict], list[dict[int, int]]]:
    artifacts: list[dict] = []
    index_by_key: dict[str, int] = {}
    maps: list[dict[int, int]] = []
    for run in runs:
        run_map: dict[int, int] = {}
        for position, artifact in enumerate(run.get("artifacts") or []):
            if not isinstance(artifact, dict):
                raise ValueError("SARIF artifact entries must be objects")
            key = json.dumps(artifact, sort_keys=True)
            if key not in index_by_key:
                index_by_key[key] = len(artifacts)
                artifacts.append(copy.deepcopy(artifact))
            run_map[position] = index_by_key[key]
        maps.append(run_map)
    return artifacts, maps


def _remap_artifact(location: object, artifact_map: dict[int, int]) -> None:
    if not isinstance(location, dict):
        return
    artifact = location.get("artifactLocation")
    if isinstance(artifact, dict) and isinstance(artifact.get("index"), int):
        artifact["index"] = artifact_map.get(artifact["index"], artifact["index"])


def _remap_result(result: dict, rule_map: dict[int, int], artifact_map: dict[int, int]) -> dict:
    merged = copy.deepcopy(result)
    if isinstance(merged.get("ruleIndex"), int):
        merged["ruleIndex"] = rule_map.get(merged["ruleIndex"], merged["ruleIndex"])
    rule = merged.get("rule")
    if isinstance(rule, dict) and isinstance(rule.get("index"), int):
        rule["index"] = rule_map.get(rule["index"], rule["index"])
    for location in merged.get("locations") or []:
        if isinstance(location, dict):
            _remap_artifact(location.get("physicalLocation"), artifact_map)
    for location in merged.get("relatedLocations") or []:
        _remap_artifact(location, artifact_map)
    for fix in merged.get("fixes") or []:
        for change in (fix or {}).get("artifactChanges") or []:
            if isinstance(change, dict):
                _remap_artifact(change, artifact_map)
    return merged


def _merge_results(
    runs: list[dict], rule_maps: list[dict[int, int]], artifact_maps: list[dict[int, int]]
) -> list[dict]:
    results: list[dict] = []
    for run, rule_map, artifact_map in zip(runs, rule_maps, artifact_maps, strict=True):
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                raise ValueError("SARIF result entries must be objects")
            results.append(_remap_result(result, rule_map, artifact_map))
    return results


def _run_metadata(first: dict) -> dict:
    metadata: dict = {}
    for key in ("automationDetails", "columnKind", "originalUriBaseIds", "properties"):
        if key in first:
            metadata[key] = copy.deepcopy(first[key])
    return metadata


def merge_documents(paths: list[Path]) -> dict:
    runs = _load(paths)
    if not runs:
        return {"version": SARIF_VERSION, "$schema": SARIF_SCHEMA, "runs": []}

    tool_name = _driver_name(runs[0])
    for run in runs:
        if _driver_name(run) != tool_name:
            raise ValueError(
                f"SARIF reports come from different tools ({tool_name!r} vs {_driver_name(run)!r}); "
                "one analysis cannot merge them"
            )

    rules, rule_maps = _merge_rules(runs)
    artifacts, artifact_maps = _merge_artifacts(runs)
    results = _merge_results(runs, rule_maps, artifact_maps)

    first = runs[0]
    driver = copy.deepcopy((first.get("tool") or {}).get("driver") or {})
    driver.pop("rules", None)
    if rules:
        driver["rules"] = rules

    merged_run: dict = {"tool": {"driver": driver}, "results": results}
    merged_run.update(_run_metadata(first))
    if artifacts:
        merged_run["artifacts"] = artifacts

    return {"version": SARIF_VERSION, "$schema": SARIF_SCHEMA, "runs": [merged_run]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args(argv)

    try:
        merged = merge_documents(args.reports)
    except ValueError as exc:
        print(f"sarif merge error: {exc}", file=sys.stderr)
        return 2

    if not merged["runs"] or not merged["runs"][0]["results"]:
        print("sarif merge: no report carried results; nothing written")
        return 0

    args.output.write_text(json.dumps(merged, separators=(",", ":")), encoding="utf-8")
    print(f"sarif merge: wrote {len(merged['runs'][0]['results'])} result(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
