"""Combine several SARIF reports into one analysis payload.

GitHub code scanning identifies an analysis by the pair (tool, category). A
second upload with the same pair in the same workflow run is rejected, so the
per-platform image reports are merged here and uploaded once under the category
that already exists on the default branch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


def merge_documents(paths: list[Path]) -> dict:
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
    return {"version": SARIF_VERSION, "$schema": SARIF_SCHEMA, "runs": runs}


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

    if not merged["runs"]:
        print("sarif merge: no report carried results; nothing written")
        return 0

    args.output.write_text(json.dumps(merged, separators=(",", ":")), encoding="utf-8")
    print(f"sarif merge: wrote {len(merged['runs'])} run(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
