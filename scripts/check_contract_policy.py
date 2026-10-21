#!/usr/bin/env python3
"""Blocking gate: the published 1.0 contract still describes this source tree.

Run from the repository root::

    python scripts/check_contract_policy.py
    python scripts/check_contract_policy.py --list

``--list`` prints the surfaces the gate derives so a reviewer can see what the
inventory is being compared against; it checks nothing and always exits 0.

There is deliberately **no write mode**. The manifest is a reviewed,
machine-readable contract surface; generating it at run time would turn the
contract into a mirror of whatever the code happens to say this week, which is
the opposite of a promise. The human-readable policy documents live in the
private ``itambox-community/design-docs`` repository.

The gate imports no Django and touches no database, so it runs on a bare
interpreter alongside the other repository gate suites.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - CLI bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.contract_policy import (  # noqa: E402  (path bootstrap must precede the import)
    ENUM_SOURCES,
    build_parser,
    check_all,
    derived_capabilities,
    derived_custom_permissions,
    derived_enums,
    derived_scim_routes,
    derived_settings,
    derived_webhook_envelope,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _print_derived(root):
    enums = derived_enums(root)
    for source in ENUM_SOURCES:
        print(f"{source.name} [{source.openness}/{source.contract_class}]: {list(enums[source.name])}")
    print(f"settings: {list(derived_settings(root))}")
    print(f"capabilities: {[capability.key for capability in derived_capabilities(root)]}")
    print(f"permissions: {[f'{row.app_label}.{row.codename}' for row in derived_custom_permissions(root)]}")
    envelope = derived_webhook_envelope(root)
    print(f"webhook envelope: {list(envelope.fields)} signed with {envelope.signature_header}")
    print(f"scim routes: {[f'{route.mount}:{route.path}' for route in derived_scim_routes(root)]}")


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    root = Path(arguments.root) if arguments.root else REPO_ROOT
    if arguments.list:
        _print_derived(root)
        return 0
    findings = check_all(root)
    if not findings:
        print("contract policy: the published 1.0 contract matches this source tree.")
        return 0
    print("contract policy: the published 1.0 contract no longer matches this source tree:\n")
    for finding in findings:
        print(f"  {finding.rule} {finding.detail}")
    print(
        "\nUpdate the reviewed surface in scripts/contract_policy_manifest.json "
        "and the corresponding private design document. This gate never edits either surface.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
