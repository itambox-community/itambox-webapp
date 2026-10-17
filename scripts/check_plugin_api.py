#!/usr/bin/env python3
"""Blocking gate: the Stable plugin surface matches the checked-in API inventory.

Run from the repository root::

    python scripts/check_plugin_api.py          # exit 1 on drift
    python scripts/check_plugin_api.py --list   # print the derived surface, exit 0

The surface is derived from source with :mod:`ast`, never by importing Django:
the ``PluginConfig`` attributes, the default resource locations in
``itambox.plugins.assembly``, and the public names of the plugin API modules.
``scripts/plugin_api_manifest.json`` is the reviewed snapshot of that surface.
Any drift fails the gate, so a change to the plugin API must arrive as a
visible manifest edit in the same diff. There is deliberately no write mode.
"""

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "scripts" / "plugin_api_manifest.json"
PLUGIN_PACKAGE = REPO_ROOT / "itambox" / "itambox" / "plugins"
FIXTURE_ROOT = REPO_ROOT / "itambox" / "core" / "tests" / "plugin_fixtures"
FIXTURE_PLUGIN = FIXTURE_ROOT / "fixture_plugin"
API_DOC = REPO_ROOT / "itambox" / "docs" / "plugins" / "api_reference.md"

# Modules whose public top-level names form the plugin API, keyed by dotted name.
PUBLIC_MODULES = {
    "itambox.plugins": "__init__.py",
    "itambox.plugins.navigation": "navigation.py",
    "itambox.plugins.views": "views.py",
}

# Tier rows of the public reference: | `identifier` | Tier | ...
TIER_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(Stable|Experimental|Private)\s*\|", re.MULTILINE)

TIERS = ("Stable", "Experimental", "Private")


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assigned_names(statements):
    names = []
    for statement in statements:
        if isinstance(statement, ast.Assign):
            names.extend(target.id for target in statement.targets if isinstance(target, ast.Name))
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names.append(statement.target.id)
    return names


def derive_config_attributes(plugin_package=PLUGIN_PACKAGE):
    """Every attribute a plugin declares on its ``PluginConfig`` subclass base."""
    tree = _parse(plugin_package / "__init__.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PluginConfig":
            return sorted(set(_assigned_names(node.body)))
    raise RuntimeError("PluginConfig class not found in itambox/plugins/__init__.py")


def derive_resource_defaults(plugin_package=PLUGIN_PACKAGE):
    """Resource key -> conventional location, read from the assembly default table."""
    tree = _parse(plugin_package / "assembly.py")
    for node in tree.body:
        value = None
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_DEFAULTS" for t in node.targets):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "_DEFAULTS":
            value = node.value
        if value is not None:
            table = ast.literal_eval(value)
            return {
                key: module if attribute is None else f"{module}.{attribute}"
                for key, (module, attribute) in sorted(table.items())
            }
    raise RuntimeError("_DEFAULTS table not found in itambox/plugins/assembly.py")


def derive_public_symbols(plugin_package=PLUGIN_PACKAGE):
    """Dotted module -> sorted public top-level classes and functions of the API modules."""
    surface = {}
    for module, filename in PUBLIC_MODULES.items():
        tree = _parse(plugin_package / filename)
        surface[module] = sorted(
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
        )
    return surface


def fixture_exercised_names(fixture_root=FIXTURE_ROOT):
    """Every name assigned anywhere in the fixture tree (the fixtures exercise these)."""
    names = set()
    for path in fixture_root.rglob("*.py"):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def derive_surface():
    return {
        "config_attributes": derive_config_attributes(),
        "resource_defaults": derive_resource_defaults(),
        "public_symbols": derive_public_symbols(),
    }


def parse_doc_tiers(text):
    """Tier -> set of identifiers listed in the public reference tier table."""
    tiers = {tier: set() for tier in TIERS}
    for identifier, tier in TIER_ROW.findall(text):
        tiers[tier].add(identifier)
    return tiers


def check(manifest, derived, doc_tiers, fixture_names, fixture_plugin=FIXTURE_PLUGIN):
    """Return the list of drift problems; an empty list means the surface is pinned."""
    problems = []
    stable = manifest["stable"]
    experimental = manifest["experimental"]["config_attributes"]

    # 1. Every PluginConfig attribute is pinned to exactly one tier.
    pinned_attributes = set(stable["config_attributes"]) | set(experimental)
    for name in sorted(set(derived["config_attributes"]) - pinned_attributes):
        problems.append(f"PluginConfig attribute {name!r} is not in the manifest (add it to a tier)")
    for name in sorted(pinned_attributes - set(derived["config_attributes"])):
        problems.append(f"manifest pins PluginConfig attribute {name!r} that no longer exists")
    overlap = set(stable["config_attributes"]) & set(experimental)
    for name in sorted(overlap):
        problems.append(f"PluginConfig attribute {name!r} is pinned to two tiers")

    # 2. Default resource locations match the manifest exactly.
    if derived["resource_defaults"] != stable["resource_defaults"]:
        problems.append(
            f"resource defaults drifted: derived {derived['resource_defaults']} "
            f"!= manifest {stable['resource_defaults']}"
        )
    for key in stable["resource_defaults"]:
        if key not in stable["config_attributes"]:
            problems.append(f"resource {key!r} is not a pinned PluginConfig attribute")

    # 3. Public symbols of the API modules match the manifest exactly.
    if derived["public_symbols"] != stable["public_symbols"]:
        problems.append(
            f"public symbols drifted: derived {derived['public_symbols']} != manifest {stable['public_symbols']}"
        )

    # 4. The fixture plugin exercises every Stable extension point (C10).
    for key in stable["resource_defaults"]:
        if key == "graphql_schema":
            covered = (fixture_plugin / "graphql" / "schema.py").is_file()
        else:
            covered = key in fixture_names
        if not covered:
            problems.append(f"fixture plugin does not exercise the Stable resource {key!r}")
    if "settings_schema" not in fixture_names:
        problems.append("no fixture declares the Stable settings_schema")
    if not (fixture_plugin / "urls.py").is_file():
        problems.append("fixture plugin does not exercise the Stable UI urlconf (urls.py)")

    # 5. The public reference tier table lists exactly the manifest's documented identifiers.
    for tier in TIERS:
        listed = set(doc_tiers.get(tier, set()))
        expected = set(manifest["documented"][tier])
        for identifier in sorted(expected - listed):
            problems.append(f"public reference lacks {identifier!r} under tier {tier}")
        for identifier in sorted(listed - expected):
            problems.append(f"public reference lists {identifier!r} under tier {tier} but the manifest does not")

    # 6. Every Stable resource, public symbol and settings entry point is documented as Stable.
    required = {f"PluginConfig.{key}" for key in stable["resource_defaults"]}
    required.add("PluginConfig.settings_schema")
    required.add("get_plugin_config")
    required.add("urls.py")
    required.update(name for names in stable["public_symbols"].values() for name in names)
    for identifier in sorted(required - set(manifest["documented"]["Stable"])):
        problems.append(f"Stable surface member {identifier!r} is missing from the Stable tier documentation")

    return problems


def load_manifest(path=MANIFEST):
    return json.loads(path.read_text(encoding="utf-8"))


def run_check():
    manifest = load_manifest()
    derived = derive_surface()
    doc_tiers = parse_doc_tiers(API_DOC.read_text(encoding="utf-8"))
    return check(manifest, derived, doc_tiers, fixture_exercised_names())


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--list" in arguments:
        print(json.dumps(derive_surface(), indent=2, sort_keys=True))
        return 0
    problems = run_check()
    if problems:
        print("Plugin API inventory drift (update scripts/plugin_api_manifest.json and the reference deliberately):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Plugin API inventory matches the Stable surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
