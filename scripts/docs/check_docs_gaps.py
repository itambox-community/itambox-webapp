#!/usr/bin/env python3
"""Compare user-facing documentation with source-derived application contracts.

Run from the repository root with:
    uv run --locked --group dev python scripts/docs/check_docs_gaps.py --check

The checker uses Django's URL resolver and app registry but does not access the
network or open database connections. It checks API collection routes, runtime
settings, management commands, explicit custom permission codenames, the public
module index, and supported migration-predecessor declarations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = ROOT / "itambox"
DOCS_ROOT = APP_ROOT / "docs"


def normalized_route(path: str) -> str:
    """Return a route path without anchors and with normalized converter names."""
    path = path.replace("^", "").replace("$", "")
    return re.sub(r"<([a-z_]+):[a-z_]+>", r"<\1>", path)


def collect_api_routes(url_patterns: list[Any]) -> set[tuple[str, str]]:
    """Return ``(name, path)`` for documented API collection routes."""
    routes: set[tuple[str, str]] = set()

    def visit(patterns: list[Any], prefix: str = "") -> None:
        for pattern in patterns:
            # A resolver with nested patterns is an include, not an endpoint.
            nested = getattr(pattern, "url_patterns", None)
            if nested is not None:
                visit(nested, prefix + str(pattern.pattern))
                continue
            name = getattr(pattern, "name", None)
            path = prefix + str(pattern.pattern)
            # Skip DRF's format-suffix aliases: they republish an existing
            # route, so they are not a separate documented endpoint.
            if "drf_format_suffix" in path or "(?P<format>" in path:
                continue
            if name and name.endswith("-list") and path.startswith("api/"):
                routes.add((name, normalized_route(path)))
            elif name in {"api-root", "auth-check", "status", "api-status"} and path.startswith("api/"):
                routes.add((name, normalized_route(path)))

    visit(url_patterns)
    return routes


def first_party_module_labels(app_registry: Any) -> set[str]:
    """Return the app labels whose code lives inside the application tree."""
    labels: set[str] = set()
    for config in app_registry.get_app_configs():
        try:
            Path(config.path).resolve().relative_to(APP_ROOT.resolve())
        except ValueError:
            continue
        labels.add(config.label)
    return labels


def load_django_inventory() -> tuple[list[tuple[str, str]], set[str], set[str]]:
    """Return API collection routes, module labels, and explicit custom permissions."""
    os.environ["ITAMBOX_ENV"] = "dev"
    os.environ["DJANGO_SETTINGS_MODULE"] = "core.settings.dev"
    sys.path.insert(0, str(APP_ROOT))

    import django

    django.setup()

    from django.apps import apps
    from django.urls import get_resolver

    routes = collect_api_routes(get_resolver().url_patterns)
    permissions: set[str] = set()
    for model in apps.get_models():
        permissions.update(f"{model._meta.app_label}.{codename}" for codename, _ in model._meta.permissions)
    return sorted(routes), first_party_module_labels(apps), permissions


def all_document_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in DOCS_ROOT.rglob("*.md"))


def source_setting_names() -> set[str]:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"(?m)^([A-Z0-9_]*ITAMBOX[A-Z0-9_]*)=", template))
    settings_dir = APP_ROOT / "core" / "settings"
    for path in settings_dir.rglob("*.py"):
        names.update(re.findall(r"\bITAMBOX_[A-Z0-9_]+\b", path.read_text(encoding="utf-8")))
    plugin_schema = APP_ROOT / "itambox" / "plugins" / "settings_schema.py"
    if "ITAMBOX_PLUGIN_" in plugin_schema.read_text(encoding="utf-8"):
        names.add("ITAMBOX_PLUGIN_<NAME>_<KEY>")
    return names


def management_command_names() -> set[str]:
    return {
        path.stem
        for path in APP_ROOT.rglob("management/commands/*.py")
        if path.parent.name == "commands" and path.stem != "__init__"
    }


def module_is_documented(label: str, index: str) -> bool:
    if label in {"core", "itambox"}:
        return True
    if re.search(rf"\b{re.escape(label)}\b", index, flags=re.IGNORECASE):
        return True
    aliases = {
        "extras": ("customisation", "customization", "automation"),
        "subscriptions": ("saas",),
    }
    return any(re.search(rf"\b{re.escape(alias)}\b", index, flags=re.IGNORECASE) for alias in aliases.get(label, ()))


def documented_command(command: str, text: str) -> bool:
    return re.search(rf"manage\.py\s+{re.escape(command)}\b", text) is not None


def route_gaps(routes: list[tuple[str, str]], developer_guide: str, scim_guide: str) -> list[dict[str, str]]:
    gaps = []
    normalized_scim = re.sub(r"<(?:tenant|provider)_slug>", "<slug>", scim_guide)
    for route_name, route in routes:
        if f"/{route}" in developer_guide:
            continue
        if "/scim/v2/" in route:
            base = f"/{route.rsplit('/', 1)[0]}/".casefold()
            resource = route.rsplit("/", 1)[-1].casefold()
            if base in normalized_scim and resource in normalized_scim:
                continue
        gaps.append(
            {
                "severity": "MEDIUM",
                "check": "API route",
                "item": f"{route_name} -> /{route}",
                "document": "integration/developer_guide.md",
            }
        )
    return gaps


def setting_gaps(documented: str) -> list[dict[str, str]]:
    return [
        {
            "severity": "MEDIUM",
            "check": "Setting",
            "item": name,
            "document": "operations/installation.md or a focused guide",
        }
        for name in sorted(source_setting_names())
        if name.casefold() not in documented.casefold()
    ]


def command_gaps(documented: str) -> list[dict[str, str]]:
    return [
        {
            "severity": "MEDIUM",
            "check": "Management command",
            "item": command,
            "document": "operations/management-commands.md or a focused guide",
        }
        for command in sorted(management_command_names())
        if not documented_command(command, documented)
    ]


def permission_gaps(permissions: set[str], documented: str) -> list[dict[str, str]]:
    return [
        {
            "severity": "MEDIUM",
            "check": "Custom permission codename",
            "item": permission,
            "document": "security/deployment-security.md",
        }
        for permission in sorted(permissions)
        if permission.casefold() not in documented.casefold()
    ]


def module_gaps(modules: set[str], index: str) -> list[dict[str, str]]:
    return [
        {"severity": "HIGH", "check": "Application module", "item": label, "document": "index.md"}
        for label in sorted(modules - {"core", "itambox"})
        if not module_is_documented(label, index)
    ]


def upgrade_gaps(manifest_path: Path, upgrades: str) -> list[dict[str, str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gaps = []
    for predecessor in manifest.get("recognized_states", []):
        recognition = predecessor.get("recognition", {})
        state = recognition.get("state", "")
        if state and state.casefold() not in upgrades:
            gaps.append(
                {
                    "severity": "HIGH",
                    "check": "Supported upgrade origin",
                    "item": state,
                    "document": "operations/upgrades.md",
                }
            )
        origin = predecessor.get("name", "").replace("issue88-", "")
        pattern = r"\b" + re.escape(origin).replace(r"\-", "[- ]") + r"\b"
        if origin and not re.search(pattern, upgrades):
            gaps.append(
                {
                    "severity": "HIGH",
                    "check": "Supported upgrade origin",
                    "item": origin,
                    "document": "operations/upgrades.md",
                }
            )
    return gaps


def plugin_preflight_gap(documented: str) -> list[dict[str, str]]:
    source = (APP_ROOT / "core" / "management" / "commands" / "plugins.py").read_text(encoding="utf-8")
    if '"--check"' in source and "manage.py plugins --check" not in documented.casefold():
        return [
            {
                "severity": "HIGH",
                "check": "Plugin preflight option",
                "item": "plugins --check",
                "document": "operations/plugin-runbook.md",
            }
        ]
    return []


def collect_gaps() -> list[dict[str, str]]:
    documented = all_document_text()
    index = (DOCS_ROOT / "index.md").read_text(encoding="utf-8")
    developer_guide = (DOCS_ROOT / "integration" / "developer_guide.md").read_text(encoding="utf-8").casefold()
    scim_guide = (DOCS_ROOT / "integration" / "scim.md").read_text(encoding="utf-8").casefold()
    upgrades = (DOCS_ROOT / "operations" / "upgrades.md").read_text(encoding="utf-8").casefold()
    routes, modules, permissions = load_django_inventory()
    manifest_path = APP_ROOT / "core" / "migration_baseline_manifest.json"
    gaps = route_gaps(routes, developer_guide, scim_guide)
    gaps.extend(setting_gaps(documented))
    gaps.extend(command_gaps(documented))
    gaps.extend(permission_gaps(permissions, documented))
    gaps.extend(module_gaps(modules, index))
    gaps.extend(upgrade_gaps(manifest_path, upgrades))
    gaps.extend(plugin_preflight_gap(documented))
    return sorted(gaps, key=lambda gap: (gap["severity"], gap["check"], gap["item"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if any source-derived documentation gap exists.")
    parser.add_argument("--json", action="store_true", help="Emit the gap list as JSON.")
    args = parser.parse_args()
    gaps = collect_gaps()

    if args.json:
        print(json.dumps({"gap_count": len(gaps), "gaps": gaps}, indent=2, sort_keys=True))
    elif gaps:
        print(f"Documentation gaps: {len(gaps)}")
        for gap in gaps:
            print(f"{gap['severity']} | {gap['check']} | {gap['item']} | {gap['document']}")
    else:
        print("PASS: no source-derived documentation gaps found.")

    return 1 if args.check and gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
