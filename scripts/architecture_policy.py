#!/usr/bin/env python
"""Layering, dependency direction, and cycle policy for first-party ITAMbox code.

ITAMbox is one Django project made of a platform substrate and ten domain
applications. The substrate may not name a domain, a model may not reach up into
the presentation it is rendered by, and nothing may import a composition root.
This module encodes that structure as data -- a layer per module, a matrix of
permitted directions, and a closed registry of rule identifiers -- so the gate in
``check_architecture.py`` can decide any edge without judgement.

Three properties shape the implementation:

* **It never imports application code.** Everything is derived from the source
  text through ``ast`` and from the dotted module name. Importing the tree would
  execute Django, and a gate that needs a database is a gate that stops running.
* **It never probes the filesystem while resolving.** Module existence is
  answered only by exact-case membership in the in-memory discovered set. A
  case-insensitive filesystem would otherwise resolve
  ``from assets.models import Asset`` to ``assets.models.asset`` on Windows and
  to ``assets.models`` on Linux, and the two baselines could not be shared.
* **It classifies every participating module or refuses to answer.** There is no
  default layer. A module the rules do not cover is a policy gap to close, not a
  row to accept, and the gate exits 2 rather than reporting a clean graph.

Import direction is checked over two graphs. The module-top graph is what runs
when the module is imported; the effective graph adds function-body imports,
because moving an import into a function defers *when* the coupling happens and
not *whether* it exists. Both block. ``if TYPE_CHECKING:`` imports are in
neither: they never execute, and a typing-only back edge is the sanctioned fix
for a real cycle rather than a defect.
"""

import ast
import collections
import hashlib
import json
from pathlib import Path

try:
    from scripts.check_local_imports import (
        ANNOTATION_PATTERN,
        MARKER_PATTERN,
        POLICY_CATEGORIES,
        _classify_comment,
        _ImportCollector,
        _preceding_comment_block,
        _resolve_annotations,
        _scope_label,
        _statement_comment,
    )
except ModuleNotFoundError:  # direct execution puts scripts/ on sys.path, not the repository root
    from check_local_imports import (
        ANNOTATION_PATTERN,
        MARKER_PATTERN,
        POLICY_CATEGORIES,
        _classify_comment,
        _ImportCollector,
        _preceding_comment_block,
        _resolve_annotations,
        _scope_label,
        _statement_comment,
    )

SCHEMA_VERSION = 2
CANONICAL_PYTHON = (3, 12)

# The Django project directory is both the scanned tree and the dotted-name
# anchor: it is the ``sys.path`` entry, so ``itambox/itambox/middleware.py`` is
# ``itambox.middleware`` and never ``itambox.itambox.middleware``.
SOURCE_ROOT = "itambox"
DEFAULT_TARGETS = ("itambox",)

# Mirrors check_local_imports: generated, vendored, and test paths only.
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "docs",
        "locale",
        "migrations",
        "node_modules",
        "static",
        "templates",
        "tests",
        "venv",
    }
)
EXCLUDED_FILE_NAMES = frozenset({"conftest.py", "manage.py", "tests.py"})
EXCLUDED_FILE_PREFIXES = ("test_",)

MODULE_TOP = "module-top"
FUNCTION_BODY = "function-body"
TYPING_ONLY = "typing-only"
EDGE_KINDS = (MODULE_TOP, FUNCTION_BODY)
GRAPH_NAMES = (MODULE_TOP, "effective")

# ``package-init`` is a sentinel, not a layer: an import-free ``__init__.py`` is
# a namespace marker that couples nothing, so it never meets the matrix.
PACKAGE_INIT = "package-init"
LAYERS = (
    "framework",
    "kernel",
    "platform-service",
    "integration",
    "domain-model",
    "domain-service",
    "presentation",
    "composition",
)

PLATFORM_PACKAGES = frozenset({"core", "itambox"})
DOMAIN_APPS = frozenset(
    {
        "assets",
        "compliance",
        "extras",
        "inventory",
        "licenses",
        "organization",
        "procurement",
        "software",
        "subscriptions",
        "users",
    }
)

# P3: a module whose last segment names one of these is wired *into* the
# project rather than depended *on*. Matched against the whole segment and
# against the tokens either side of its underscores, so ``urls_audits`` and
# ``provider_urls`` both read as URL configuration.
COMPOSITION_LEAF_NAMES = frozenset({"admin", "apps", "asgi", "urls", "wsgi"})

# P4-domain: the first segment after the application name whose normalised form
# appears here decides the layer.
LAYER_KEYWORDS = {
    "api": "presentation",
    "choices": "domain-model",
    "dashboard": "presentation",
    "filters": "presentation",
    "form": "presentation",
    "forms": "presentation",
    "model": "domain-model",
    "models": "domain-model",
    "reconciliation": "domain-service",
    # A domain application's report providers: they read that domain's models
    # and hand rows back to core.reports, which renders them. No presentation
    # segment precedes it in any module name, so ``views.reports`` -- were one
    # ever written -- still reads as presentation on its earlier segment.
    "reports": "domain-service",
    "schema": "presentation",
    "search": "presentation",
    "serializers": "presentation",
    "service": "domain-service",
    "services": "domain-service",
    "signals": "domain-service",
    "tables": "presentation",
    "tasks": "domain-service",
    "templatetags": "presentation",
    "views": "presentation",
    "widgets": "presentation",
}

# P4-platform: longest dotted-prefix match. ``core`` and ``itambox`` carry the
# residual entry so the table is total over the platform packages.
PLATFORM_LAYER_PREFIXES = {
    "core": "kernel",
    "core.auth": "integration",
    "core.events": "platform-service",
    "core.forms": "presentation",
    "core.importers": "integration",
    "core.integrations": "integration",
    "core.management": "composition",
    "core.navigation": "presentation",
    "core.reports": "platform-service",
    "core.schedules": "platform-service",
    "core.schema": "composition",
    "core.search": "presentation",
    "core.search_backends": "presentation",
    "core.settings": "composition",
    "core.signals": "platform-service",
    "core.tables": "presentation",
    "core.tasks": "platform-service",
    "core.templatetags": "presentation",
    "core.views": "presentation",
    "core.worker_status": "platform-service",
    "itambox": "framework",
    "itambox.context_processors": "presentation",
    "itambox.object_actions": "presentation",
    "itambox.panels": "presentation",
    "itambox.quick_add": "presentation",
    "itambox.views": "presentation",
}

# P0: genuine ambiguity only. Every entry is a reviewed policy decision and part
# of the fingerprint; a rule defect belongs in the tables above instead, because
# an override map that absorbs rule defects rots into a lookup table.
MODULE_LAYER_OVERRIDES = {
    # Shared form/REST input translation; domain code must not depend on it.
    "assets.specification_adapters": "presentation",
    # Read-only display projection of stored specification values for templates.
    "assets.spec_display": "presentation",
    "assets.customfields": "domain-service",
    "assets.depreciation": "domain-service",
    # GraphQL transport and DTO adapters are domain presentation, even though
    # their package name does not expose one of the native presentation segments.
    "assets.graphql_specifications.__init__": "presentation",
    "assets.graphql_specifications.integration": "presentation",
    "assets.graphql_specifications.inputs": "presentation",
    "assets.graphql_specifications.loaders": "presentation",
    "assets.graphql_specifications.readers": "presentation",
    "assets.graphql_specifications.scalars": "presentation",
    "assets.graphql_specifications.types": "presentation",
    "assets.scanning": "domain-service",
    "compliance.checks": "composition",
    "compliance.providers": "domain-service",
    "compliance.registry": "domain-service",
    "core.filters": "presentation",
    "core.graphql_utils": "presentation",
    "core.otp_middleware": "framework",
    "core.paginator": "presentation",
    "extras.customfields": "domain-service",
    "extras.definition_contract": "domain-model",
    "extras.dashboard.utils": "presentation",
    "extras.management.commands.eventrule_withdrawn_report": "composition",
    "extras.utils": "domain-service",
    "organization.access": "domain-service",
    "organization.rbac": "domain-service",
    "procurement.management.commands.reconcile_procurement_legacy": "composition",
    # These Snipe-IT stages translate remote rows into existing domain commands;
    # they are orchestration, not transport/client modules. The client/common
    # importer boundary remains integration-layer code.
    # The stage runner is the matching application orchestration seam; the
    # transport/client modules below it remain integration-layer code.
    "core.importers.snipeit.__init__": "composition",
    "core.importers.snipeit.orchestrator": "composition",
    "core.importers.snipeit.stages.asset_models": "composition",
    "core.importers.snipeit.stages.catalog": "composition",
    "core.importers.snipeit.stages.hardware": "composition",
    "core.importers.snipeit.stages.inventory": "composition",
    "core.importers.snipeit.stages.licenses": "composition",
    "core.importers.snipeit.stages.organization": "composition",
}

# Declared cross-application model coupling. Same-application
# ``domain-model -> domain-model`` is always allowed; anything else needs an
# entry here, which moves the fingerprint and is therefore a reviewed diff.
CROSS_DOMAIN_MODEL_EDGES = frozenset(
    {
        ("assets", "extras"),
        ("licenses", "assets"),
        ("licenses", "extras"),
        ("licenses", "organization"),
        ("licenses", "software"),
        ("software", "extras"),
        ("subscriptions", "assets"),
        ("subscriptions", "extras"),
    }
)

# The repository's ``area:*` labels. Validated as a set rather than by prefix:
# ``area:core`` reads plausible and does not exist, and a baseline row that
# names it would attribute debt to nobody. Refresh with
# ``gh label list --json name -q '.[].name'``.
AREA_LABELS = frozenset(
    {
        "area:api",
        "area:assets",
        "area:auth-rbac",
        "area:ci",
        "area:frontend",
        "area:inventory",
        "area:licenses",
        "area:operations",
        "area:organization",
        "area:plugins",
        "area:procurement",
        "area:release",
        "area:subscriptions",
        "area:testing",
    }
)

# Owner resolution: longest dotted-prefix match on the source module, then on
# the target, then a policy error. Deriving the owner rather than typing it per
# row keeps a hundred-row baseline reviewable and stable across regeneration.
OWNER_BY_MODULE_PREFIX = {
    "assets": "area:assets",
    "compliance": "area:assets",
    "core": "area:operations",
    "core.admin": "area:operations",
    "core.apps": "area:operations",
    "core.auth": "area:auth-rbac",
    "core.context": "area:organization",
    "core.crypto": "area:auth-rbac",
    "core.events": "area:operations",
    "core.filters": "area:frontend",
    "core.forms": "area:frontend",
    "core.forms.tenant": "area:organization",
    "core.importers": "area:operations",
    "core.integrations": "area:operations",
    "core.management": "area:operations",
    "core.managers": "area:organization",
    "core.mfa": "area:auth-rbac",
    "core.mixins": "area:organization",
    "core.models": "area:organization",
    "core.navigation": "area:frontend",
    "core.otp_middleware": "area:auth-rbac",
    "core.paginator": "area:frontend",
    "core.reports": "area:operations",
    "core.schedules": "area:operations",
    "core.schema": "area:operations",
    "core.search": "area:operations",
    "core.search_backends": "area:operations",
    "core.settings": "area:operations",
    "core.tables": "area:frontend",
    "core.tasks": "area:operations",
    "core.templatetags": "area:frontend",
    "core.urls": "area:operations",
    "core.views": "area:frontend",
    "core.worker_status": "area:operations",
    "extras": "area:operations",
    "extras.dashboard": "area:frontend",
    "extras.forms": "area:frontend",
    "extras.tables": "area:frontend",
    "extras.templatetags": "area:frontend",
    "inventory": "area:inventory",
    "itambox": "area:operations",
    "itambox.api": "area:api",
    "itambox.middleware": "area:auth-rbac",
    "itambox.object_actions": "area:frontend",
    "itambox.panels": "area:frontend",
    "itambox.plugins": "area:plugins",
    "itambox.quick_add": "area:frontend",
    "itambox.ratelimit": "area:auth-rbac",
    "itambox.views": "area:frontend",
    "licenses": "area:licenses",
    "organization": "area:organization",
    "procurement": "area:procurement",
    "software": "area:licenses",
    "subscriptions": "area:subscriptions",
    "users": "area:organization",
}

# The closed matrix registry. A forbidden cell without an identifier, or an
# identifier no cell uses, is a policy error: that is what stops the matrix
# being widened without the baseline noticing.
MATRIX_RULES = {
    "R-F1": "framework must not import a domain model",
    "R-F2": "framework must not import a platform service or an integration",
    "R-F3": "framework must not import a domain service, presentation, or composition",
    "R-K1": "kernel must not import a domain model",
    "R-K2": "kernel must not import a platform service or an integration",
    "R-K3": "kernel must not import a domain service, presentation, or composition",
    "R-S1": "a platform service must not import a domain model",
    "R-S2": "a platform service must not import presentation",
    "R-S3": "a platform service must not import a domain service",
    "R-S4": "a platform service must not import composition",
    "R-I1": "an integration must not import a domain model",
    "R-I2": "an integration must not import presentation",
    "R-I3": "an integration must not import a domain service",
    "R-I4": "an integration must not import composition",
    "R-M1": "a domain model must not import presentation",
    "R-M2": "a domain model must not import composition",
    "R-X1": "a domain model must not import a domain service",
    "R-X2": "cross-application model coupling is not declared in CROSS_DOMAIN_MODEL_EDGES",
    "R-X3": "a domain model must not import a platform service or an integration",
    "R-V1": "a domain service must not import presentation",
    "R-V2": "a domain service must not import composition",
    "R-P2": "generic presentation must not import a domain model",
    "R-P3": "generic presentation must not import a domain service",
    "R-P4": "presentation must not import composition",
}

# Rules that do not come from a matrix cell.
STRUCTURAL_RULES = {
    "R-C1": "a new import cycle in the module-top graph",
    "R-CE1": "a new import cycle in the effective graph",
    "R-C2": "a supported cycle claim whose component is not recorded",
    "R-C3": "an inline cycle annotation the measured graph does not support",
    "R-DOC1": "a documentation reference that does not resolve on disk",
}

# Reserved for typing-only coupling. Inactive at schema v1; the loader rejects
# any row citing it, so activating it later is a policy edit, not a schema break.
RESERVED_RULES = frozenset({"R-C4"})

# Forbidden absolutely: the loader rejects a baseline row, ``--write-baseline``
# cannot emit one, and the gate fails on the edge itself.
ABSOLUTE_FORBIDDEN = frozenset({"R-M1"})

PRESENTATION_DOMAIN = "presentation@domain"
PRESENTATION_PLATFORM = "presentation@platform"
MATRIX_ROWS = (
    "framework",
    "kernel",
    "platform-service",
    "integration",
    "domain-model",
    "domain-service",
    PRESENTATION_DOMAIN,
    PRESENTATION_PLATFORM,
    "composition",
)


def _row(**cells):
    """One matrix row: every layer maps to a rule identifier or to ``None``."""
    return {layer: cells.get(layer.replace("-", "_")) for layer in LAYERS}


# Read as *row imports column*. ``framework`` and ``kernel`` recurse into each
# other on purpose: they are one mutually recursive platform substrate and keep
# separate names because their diagnostics differ.
MATRIX = {
    "framework": _row(
        platform_service="R-F2",
        integration="R-F2",
        domain_model="R-F1",
        domain_service="R-F3",
        presentation="R-F3",
        composition="R-F3",
    ),
    "kernel": _row(
        platform_service="R-K2",
        integration="R-K2",
        domain_model="R-K1",
        domain_service="R-K3",
        presentation="R-K3",
        composition="R-K3",
    ),
    "platform-service": _row(
        domain_model="R-S1",
        domain_service="R-S3",
        presentation="R-S2",
        composition="R-S4",
    ),
    "integration": _row(
        domain_model="R-I1",
        domain_service="R-I3",
        presentation="R-I2",
        composition="R-I4",
    ),
    "domain-model": _row(
        platform_service="R-X3",
        integration="R-X3",
        domain_model="R-X2",
        domain_service="R-X1",
        presentation="R-M1",
        composition="R-M2",
    ),
    "domain-service": _row(presentation="R-V1", composition="R-V2"),
    PRESENTATION_DOMAIN: _row(composition="R-P4"),
    PRESENTATION_PLATFORM: _row(domain_model="R-P2", domain_service="R-P3", composition="R-P4"),
    "composition": _row(),
}

RULE_REGISTRY = {
    (row, layer): rule for row, cells in MATRIX.items() for layer, rule in cells.items() if rule is not None
}

DYNAMIC_IMPORT_NAMES = frozenset({"import_module", "import_string"})
DYNAMIC_IMPORT_LAYERS = frozenset({"framework", "kernel", "platform-service"})


class PolicyError(Exception):
    """Raised when the gate cannot produce a trustworthy result."""


Classification = collections.namedtuple("Classification", "module layer origin app")
Edge = collections.namedtuple("Edge", "source target kind path line scope")
Graph = collections.namedtuple(
    "Graph", "modules module_top function_body typing_only evidence census dynamic_imports claims"
)
ModuleIndex = collections.namedtuple("ModuleIndex", "modules packages top_level paths")
Verdict = collections.namedtuple("Verdict", "status rule")
Component = collections.namedtuple("Component", "id graph modules edges")
Claim = collections.namedtuple("Claim", "id source path scope statement line targets")
DynamicImport = collections.namedtuple("DynamicImport", "module path line call")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def is_excluded(relative_path):
    parts = relative_path.split("/")
    if set(parts[:-1]) & EXCLUDED_DIRECTORY_NAMES:
        return True
    name = parts[-1]
    return name in EXCLUDED_FILE_NAMES or name.startswith(EXCLUDED_FILE_PREFIXES)


def _target_directories(root, targets):
    anchor = Path(root) / SOURCE_ROOT
    for target in targets:
        directory = Path(root) / target
        try:
            directory.resolve().relative_to(anchor.resolve())
        except ValueError as exc:
            raise PolicyError(f"target {target!r} is outside the source root {SOURCE_ROOT!r}") from exc
        # A mistyped target, or a wrong working directory, would otherwise walk
        # nothing and read as a tree with no coupling in it. Silence is the one
        # answer a fail-closed gate may never give.
        if not directory.is_dir():
            raise PolicyError(f"target {target!r} does not exist under {Path(root).as_posix()}")
        yield anchor, directory


def module_name_for(relative_to_source_root):
    """``core/views/graphql.py`` -> ``core.views.graphql``; ``__init__`` is a node."""
    return relative_to_source_root[: -len(".py")].replace("/", ".")


def discover_modules(root, targets):
    """Walk the targets and return ``{module: repository-relative posix path}``.

    Pure path walking. A directory is a package because it contains a discovered
    file, never because it carries an ``__init__.py`` -- ``core/views/`` and
    ``users/api/`` are implicit namespace packages, and a discovery rule that
    required an initialiser would silently drop the GraphQL endpoint and the
    whole SCIM surface.
    """
    discovered = {}
    for anchor, directory in _target_directories(root, targets):
        for path in sorted(directory.rglob("*.py"), key=lambda item: item.as_posix()):
            relative_to_root = path.relative_to(root).as_posix()
            relative_to_anchor = path.relative_to(anchor).as_posix()
            if is_excluded(relative_to_anchor):
                continue
            module = module_name_for(relative_to_anchor)
            if module in discovered:
                raise PolicyError(f"two files resolve to the module name {module!r}")
            discovered[module] = relative_to_root
    if not discovered:
        raise PolicyError(f"no first-party module was discovered under {', '.join(targets)}")
    return dict(sorted(discovered.items()))


def build_index(modules):
    """Freeze the discovered set into the shape the resolver answers from."""
    names = frozenset(modules)
    packages = set()
    for name in names:
        parts = name.split(".")
        for depth in range(1, len(parts)):
            packages.add(".".join(parts[:depth]))
    top_level = frozenset(name.split(".")[0] for name in names)
    return ModuleIndex(names, frozenset(packages), top_level, dict(modules))


def _as_index(modules):
    return modules if isinstance(modules, ModuleIndex) else build_index(modules)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _segment_tokens(segment):
    """The whole segment plus the tokens either side of its underscores."""
    tokens = {segment}
    if "_" in segment:
        tokens.add(segment.partition("_")[0])
        tokens.add(segment.rpartition("_")[2])
    return tokens - {""}


def _is_composition_leaf(segment):
    return bool(_segment_tokens(segment) & COMPOSITION_LEAF_NAMES)


def _longest_prefix_value(name, table):
    parts = name.split(".")
    for depth in range(len(parts), 0, -1):
        candidate = ".".join(parts[:depth])
        if candidate in table:
            return table[candidate]
    return None


def _domain_layer(name):
    for segment in name.split(".")[1:]:
        for token in sorted(_segment_tokens(segment)):
            if token in LAYER_KEYWORDS:
                return LAYER_KEYWORDS[token]
    return None


def _layer_of_name(name):
    """P3 to P5 for a module that is not a package initialiser."""
    if _is_composition_leaf(name.rpartition(".")[2]):
        return "composition"
    top = name.split(".")[0]
    if top in PLATFORM_PACKAGES:
        return _longest_prefix_value(name, PLATFORM_LAYER_PREFIXES)
    if top in DOMAIN_APPS:
        return _domain_layer(name)
    return None


def layer_of(module):
    """The module's layer, by first match over P0, P2, P3-P5. Never defaults."""
    if module in MODULE_LAYER_OVERRIDES:
        return MODULE_LAYER_OVERRIDES[module]
    name = module[: -len(".__init__")] if module.endswith(".__init__") else module
    layer = _layer_of_name(name)
    if layer is None:
        raise PolicyError(f"cannot classify module {module!r}; add a MODULE_LAYER_OVERRIDES entry")
    return layer


def origin_of(module):
    top = module.split(".")[0]
    if top in PLATFORM_PACKAGES:
        return "platform"
    if top in DOMAIN_APPS:
        return "domain"
    raise PolicyError(f"module {module!r} belongs to no first-party package")


def app_of(module):
    top = module.split(".")[0]
    if top not in PLATFORM_PACKAGES and top not in DOMAIN_APPS:
        raise PolicyError(f"module {module!r} belongs to no first-party package")
    return top


def classify(module):
    return Classification(module, layer_of(module), origin_of(module), app_of(module))


def row_key(module):
    layer = layer_of(module)
    if layer != "presentation":
        return layer
    return PRESENTATION_DOMAIN if origin_of(module) == "domain" else PRESENTATION_PLATFORM


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------


def _package_of(module):
    return module[: -len(".__init__")] if module.endswith(".__init__") else module.rpartition(".")[0]


def _relative_base(source_module, level):
    package = _package_of(source_module)
    parts = package.split(".") if package else []
    if len(parts) < level:
        raise PolicyError(f"relative import in {source_module!r} escapes the top-level package")
    return ".".join(parts[: len(parts) - (level - 1)])


def _node_for(name, index):
    if name in index.modules:
        return name
    initializer = f"{name}.__init__"
    if name in index.packages and initializer in index.modules:
        return initializer
    return None


def _prefix_nodes(name, source_module, index):
    """Initialisers an import executes on its way to ``name`` (rule P-EDGE).

    The importer's own ancestor packages are skipped: by the time a module's
    body runs, every package above it is already in ``sys.modules``, so
    ``assets.forms.asset_form`` importing ``assets.forms.fields`` creates no new
    coupling to ``assets.forms.__init__``. Counting it would turn every ordinary
    re-exporting package into a reported cycle.
    """
    parts = name.split(".")
    nodes = []
    for depth in range(1, len(parts)):
        package = ".".join(parts[:depth])
        initializer = f"{package}.__init__"
        if initializer in index.modules and not source_module.startswith(f"{package}."):
            nodes.append(initializer)
    return nodes


def _longest_resolved(name, index):
    """The deepest first-party prefix of a dotted name, with its graph node."""
    parts = name.split(".")
    for depth in range(len(parts), 0, -1):
        candidate = ".".join(parts[:depth])
        node = _node_for(candidate, index)
        if node is not None:
            return candidate, node
    return None, None


def _resolve_plain_import(node, source_module, index):
    found = []
    for alias in node.names:
        if alias.name.split(".")[0] not in index.top_level:
            continue
        candidate, target = _longest_resolved(alias.name, index)
        if target is None:
            continue
        found.append(target)
        found.extend(_prefix_nodes(candidate, source_module, index))
    return found


def _resolve_from_import(node, source_module, index):
    base = f"{node.module}" if node.level == 0 else _relative_base(source_module, node.level)
    if node.level and node.module:
        base = f"{base}.{node.module}" if base else node.module
    if not base or base.split(".")[0] not in index.top_level:
        return []
    found = []
    for alias in node.names:
        dotted = base if alias.name == "*" else f"{base}.{alias.name}"
        candidate, target = _longest_resolved(dotted, index)
        if target is None:
            continue
        found.append(target)
        found.extend(_prefix_nodes(candidate, source_module, index))
    return found


def resolve_import(node, source_module, modules):
    """Return the first-party modules one import statement couples to.

    Absolute first, with no implicit-relative fallback: ``core/http.py`` exists,
    and ``import http`` inside ``core`` is still the standard library.
    """
    index = _as_index(modules)
    if isinstance(node, ast.Import):
        found = _resolve_plain_import(node, source_module, index)
    elif isinstance(node, ast.ImportFrom):
        found = _resolve_from_import(node, source_module, index)
    else:
        found = []
    return tuple(sorted(set(found) - {source_module}))


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _is_type_checking_test(test):
    """The two spellings the policy recognises, and nothing else.

    Matching on the attribute *name* alone would accept ``shim.TYPE_CHECKING``
    and delete the guarded import from both blocking graphs -- including a
    ``domain-model -> presentation`` edge, which is the one finding that has no
    baseline representation at any severity. This is the only rule in the gate
    whose widening is silent, so the accepted forms are enumerated rather than
    pattern-matched: a bare ``TYPE_CHECKING``, or ``TYPE_CHECKING`` read off the
    ``typing`` module itself. Every other guard falls through to ordinary scope
    rules and keeps blocking.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


class _GraphCollector(ast.NodeVisitor):
    """Record every import statement with the scope that decides its kind."""

    def __init__(self):
        self.scope = []
        self.function_depth = 0
        self.typing_depth = 0
        self.imports = []
        self.dynamic = []

    def _visit_scope(self, node):
        deferring = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        self.scope.append(node)
        self.function_depth += 1 if deferring else 0
        self.generic_visit(node)
        self.function_depth -= 1 if deferring else 0
        self.scope.pop()

    visit_ClassDef = _visit_scope
    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_If(self, node):
        # Only ``TYPE_CHECKING`` is special-cased, and only its own body. No
        # condition is ever evaluated, so both legs of a settings switch are
        # emitted and an unrecognised guard yields a blocking edge.
        guarded = _is_type_checking_test(node.test)
        self.typing_depth += 1 if guarded else 0
        for statement in node.body:
            self.visit(statement)
        self.typing_depth -= 1 if guarded else 0
        for statement in node.orelse:
            self.visit(statement)

    def _kind(self):
        if self.typing_depth:
            return TYPING_ONLY
        return FUNCTION_BODY if self.function_depth else MODULE_TOP

    def _visit_import(self, node):
        scope = "/".join(_scope_label(item) for item in self.scope)
        self.imports.append((node, self._kind(), scope))

    visit_Import = _visit_import
    visit_ImportFrom = _visit_import

    def visit_Call(self, node):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name in DYNAMIC_IMPORT_NAMES:
            self.dynamic.append((node.lineno, name))
        self.generic_visit(node)


def _parse_sources(root, modules):
    parsed = {}
    for module, relative_path in modules.items():
        path = Path(root) / relative_path
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PolicyError(f"cannot read {relative_path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=relative_path)
        except SyntaxError as exc:
            raise PolicyError(f"cannot parse {relative_path}: {exc}") from exc
        collector = _GraphCollector()
        collector.visit(tree)
        parsed[module] = (tree, source.splitlines(), collector)
    return parsed


def _importful_initialisers(parsed, index):
    """Initialisers with at least one first-party import of their own.

    An import-free ``__init__.py`` couples nothing, so importing through it
    creates no edge -- which is also what keeps the inert sentinel out of the
    matrix.
    """
    importful = set()
    for module, (_tree, _lines, collector) in parsed.items():
        if not module.endswith(".__init__"):
            continue
        if any(resolve_import(node, module, index) for node, _kind, _scope in collector.imports):
            importful.add(module)
    return importful


def _module_edges(module, collector, index, live, path):
    edges = []
    for node, kind, scope in collector.imports:
        for target in resolve_import(node, module, index):
            if target not in live:
                continue
            edges.append(Edge(module, target, kind, path, node.lineno, scope))
    return edges


def _census(modules, inert, unclassified):
    layers = collections.Counter()
    for module in modules:
        if module in inert or module in unclassified:
            continue
        layers[layer_of(module)] += 1
    return {
        "discovered": len(modules),
        "classified": len(modules) - len(inert) - len(unclassified),
        "inert": tuple(sorted(inert)),
        "unclassified": tuple(sorted(unclassified)),
        "layers": dict(sorted(layers.items())),
    }


def _unclassified_modules(modules, inert):
    unclassified = []
    for module in modules:
        if module in inert:
            continue
        try:
            layer_of(module)
            origin_of(module)
        except PolicyError:
            unclassified.append(module)
    return unclassified


def _dynamic_import_sites(parsed, modules, unclassified):
    sites = []
    for module in sorted(modules):
        if module in unclassified:
            continue
        collector = parsed[module][2]
        if not collector.dynamic or not _watches_dynamic_imports(module):
            continue
        for line, call in collector.dynamic:
            sites.append(DynamicImport(module, modules[module], line, call))
    return tuple(sites)


def _watches_dynamic_imports(module):
    try:
        layer = layer_of(module)
    except PolicyError:
        return False
    if layer in DYNAMIC_IMPORT_LAYERS:
        return True
    return layer == "presentation" and origin_of(module) == "platform"


def build_graph(root, targets):
    """Discover, parse once, and return the three edge sets with their evidence."""
    modules = discover_modules(root, targets)
    index = build_index(modules)
    parsed = _parse_sources(root, modules)
    inert = {module for module in modules if module.endswith(".__init__")} - _importful_initialisers(parsed, index)
    live = set(modules) - inert

    evidence = []
    for module in sorted(modules):
        evidence.extend(_module_edges(module, parsed[module][2], index, live, modules[module]))
    evidence.sort(key=lambda edge: (edge.source, edge.target, edge.kind, edge.path, edge.line))

    unclassified = _unclassified_modules(modules, inert)
    return Graph(
        modules=modules,
        module_top=tuple(edge for edge in evidence if edge.kind == MODULE_TOP),
        function_body=tuple(edge for edge in evidence if edge.kind == FUNCTION_BODY),
        typing_only=tuple(edge for edge in evidence if edge.kind == TYPING_ONLY),
        evidence=tuple(evidence),
        census=_census(modules, inert, unclassified),
        dynamic_imports=_dynamic_import_sites(parsed, modules, set(unclassified)),
        claims=collect_cycle_claims(parsed, index, live),
    )


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


def adjacency_for(edges):
    """Sorted adjacency, so component and member ordering are both stable."""
    neighbours = collections.defaultdict(set)
    for edge in edges:
        neighbours[edge.source].add(edge.target)
        neighbours.setdefault(edge.target, set())
    return {node: tuple(sorted(targets)) for node, targets in sorted(neighbours.items())}


def strongly_connected_components(adjacency):
    """Iterative Tarjan. A self-import is not a component; a 1,000-node chain
    must not exhaust the recursion limit."""
    index_of = {}
    low = {}
    on_stack = set()
    stack = []
    components = []
    counter = 0

    for root in sorted(adjacency):
        if root in index_of:
            continue
        work = [(root, iter(adjacency.get(root, ())))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index_of[node]:
                    components.append(_pop_component(stack, on_stack, node))
                continue
            if child not in index_of:
                index_of[child] = low[child] = counter
                counter += 1
                stack.append(child)
                on_stack.add(child)
                work.append((child, iter(adjacency.get(child, ()))))
            elif child in on_stack:
                low[node] = min(low[node], index_of[child])
    return tuple(sorted(component for component in components if len(component) > 1))


def _pop_component(stack, on_stack, root):
    members = []
    while True:
        member = stack.pop()
        on_stack.discard(member)
        members.append(member)
        if member == root:
            break
    return tuple(sorted(members))


def component_id(graph_name, modules):
    return "|".join((graph_name, *sorted(modules)))


def _component_edges(modules, edges):
    members = set(modules)
    return tuple(
        sorted(
            {
                (edge.source, edge.target, edge.kind)
                for edge in edges
                if edge.source in members and edge.target in members
            }
        )
    )


def find_cycles(graph):
    """Components of both blocking graphs; a module-top cycle is reported once."""
    module_top = strongly_connected_components(adjacency_for(graph.module_top))
    effective_edges = graph.module_top + graph.function_body
    effective = strongly_connected_components(adjacency_for(effective_edges))
    recorded = {frozenset(modules) for modules in module_top}
    components = [
        Component(component_id(MODULE_TOP, modules), MODULE_TOP, modules, _component_edges(modules, graph.module_top))
        for modules in module_top
    ]
    components.extend(
        Component(component_id("effective", modules), "effective", modules, _component_edges(modules, effective_edges))
        for modules in effective
        if frozenset(modules) not in recorded
    )
    return tuple(sorted(components))


def component_memberships(graph):
    """The two membership maps ``R-C3`` compares, one per blocking graph.

    Unlike :func:`find_cycles` this keeps a component in *both* maps when it is
    visible in both graphs: a claim supported at module top is still supported
    in the effective graph, and collapsing the two would misreport it.
    """
    module_top = strongly_connected_components(adjacency_for(graph.module_top))
    effective = strongly_connected_components(adjacency_for(graph.module_top + graph.function_body))
    return (
        {module: component_id(MODULE_TOP, members) for members in module_top for module in members},
        {module: component_id("effective", members) for members in effective for module in members},
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def is_allowed(source, target):
    """Decide one edge. ``status`` is ``allow``, ``forbid``, or ``absolute``."""
    if source == target:
        return Verdict("allow", None)
    source_row = row_key(source)
    target_layer = layer_of(target)
    if source_row == "domain-model" and target_layer == "domain-model":
        return _cross_domain_verdict(source, target)
    rule = MATRIX[source_row][target_layer]
    if rule is None:
        return Verdict("allow", None)
    return Verdict("absolute" if rule in ABSOLUTE_FORBIDDEN else "forbid", rule)


def _cross_domain_verdict(source, target):
    pair = (app_of(source), app_of(target))
    if pair[0] == pair[1] or pair in CROSS_DOMAIN_MODEL_EDGES:
        return Verdict("allow", None)
    return Verdict("forbid", "R-X2")


def exception_id(rule, source, target, kind):
    return "|".join((rule, source, target, kind))


def is_baselineable(rule):
    return rule in MATRIX_RULES and rule not in ABSOLUTE_FORBIDDEN


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def owner_for_modules(modules):
    """Longest dotted-prefix match, source first, then target. No default."""
    for module in modules:
        owner = _longest_prefix_value(module, OWNER_BY_MODULE_PREFIX)
        if owner is not None:
            return owner
    raise PolicyError(f"no owning area label resolves for {', '.join(modules) or '<no modules>'}")


# ---------------------------------------------------------------------------
# Inline cycle claims
# ---------------------------------------------------------------------------


def _has_own_annotation(lines, node):
    for comment in (_statement_comment(lines, node), _preceding_comment_block(lines, node)):
        category, problem = _classify_comment(comment)
        if category is not None or problem is not None:
            return True
    return False


def _group_statements(relative_path, lines, collected):
    """Split ``cycle``-annotated imports into the groups one comment covers."""
    resolved, _problems = _resolve_annotations(relative_path, lines, collected)
    groups = []
    current = None
    for node, scope in sorted(collected, key=lambda item: item[0].lineno):
        if resolved.get(id(node)) != "cycle":
            current = None
            continue
        continues = current is not None and current[-1][0].end_lineno == node.lineno - 1 and current[-1][1] == scope
        if continues and not _has_own_annotation(lines, node):
            current.append((node, scope))
            continue
        current = [(node, scope)]
        groups.append(current)
    return groups


def collect_cycle_claims(parsed, index, live):
    """Every ``# inline import: cycle:`` group, with the modules it names.

    ``check_local_imports`` stays the sole owner of the grammar and of the four
    categories; a malformed annotation is its finding, not this gate's.
    """
    claims = []
    for module in sorted(parsed):
        tree, lines, _collector = parsed[module]
        relative_path = index.paths[module]
        collector = _ImportCollector()
        collector.visit(tree)
        if not collector.imports:
            continue
        for group in _group_statements(relative_path, lines, collector.imports):
            claims.append(_claim_for(module, relative_path, group, index, live))
    return tuple(sorted(claims))


def _claim_for(module, relative_path, group, index, live):
    anchor, scope = group[0]
    targets = set()
    for node, _scope in group:
        targets.update(target for target in resolve_import(node, module, index) if target in live)
    statement = ast.unparse(anchor)
    return Claim(
        id="|".join((relative_path, scope, statement)),
        source=module,
        path=relative_path,
        scope=scope,
        statement=statement,
        line=anchor.lineno,
        targets=tuple(sorted(targets)),
    )


def source_module_for_path(relative_path):
    """The dotted module a repository-relative source path denotes.

    Pure and total over paths inside the source root, so a recorded claim's
    ``source`` can be checked against its own ``path`` without a graph. That is
    what stops the two fields drifting apart: an owner derived from a ``source``
    nobody verified is an owner nobody can trust.
    """
    prefix = f"{SOURCE_ROOT}/"
    if not relative_path.startswith(prefix) or not relative_path.endswith(".py"):
        raise PolicyError(f"path {relative_path!r} is not a module inside the source root {SOURCE_ROOT!r}")
    return module_name_for(relative_path[len(prefix) :])


def classify_claim(claim, source, module_top_membership, effective_membership):
    """``supported-module-top``, ``supported-effective``, or ``unsupported``."""
    if not claim.targets:
        return "unsupported", ()
    unsupported = tuple(
        target for target in claim.targets if effective_membership.get(source) != effective_membership.get(target)
    )
    if unsupported or effective_membership.get(source) is None:
        return "unsupported", unsupported or claim.targets
    at_module_top = module_top_membership.get(source)
    if at_module_top is not None and all(
        module_top_membership.get(target) == at_module_top for target in claim.targets
    ):
        return "supported-module-top", ()
    return "supported-effective", ()


# ---------------------------------------------------------------------------
# Policy integrity and fingerprint
# ---------------------------------------------------------------------------


def validate_policy():
    """The registry is closed: no unnamed forbidden cell, no unused rule."""
    if set(MATRIX) != set(MATRIX_ROWS):
        raise PolicyError("the matrix rows and the declared row set disagree")
    for row, cells in MATRIX.items():
        if set(cells) != set(LAYERS):
            raise PolicyError(f"matrix row {row!r} does not cover every layer")
    used = set(RULE_REGISTRY.values())
    if used != set(MATRIX_RULES):
        missing = sorted(set(MATRIX_RULES) - used) or sorted(used - set(MATRIX_RULES))
        raise PolicyError(f"matrix rule registry is not closed: {', '.join(missing)}")
    if not ABSOLUTE_FORBIDDEN <= set(MATRIX_RULES):
        raise PolicyError("an absolutely forbidden rule names no matrix cell")
    if set(OWNER_BY_MODULE_PREFIX.values()) - AREA_LABELS:
        raise PolicyError("the owner table names a label outside AREA_LABELS")
    if set(MODULE_LAYER_OVERRIDES.values()) - set(LAYERS):
        raise PolicyError("a layer override names an unknown layer")


def _policy_payload(targets):
    return {
        "schema_version": SCHEMA_VERSION,
        "canonical_python": f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]}",
        "source_root": SOURCE_ROOT,
        "targets": list(targets),
        "layers": list(LAYERS),
        "matrix_rows": list(MATRIX_ROWS),
        "matrix": {row: dict(sorted(cells.items())) for row, cells in sorted(MATRIX.items())},
        "matrix_rules": dict(sorted(MATRIX_RULES.items())),
        "structural_rules": dict(sorted(STRUCTURAL_RULES.items())),
        "reserved_rules": sorted(RESERVED_RULES),
        "absolute_forbidden": sorted(ABSOLUTE_FORBIDDEN),
        "platform_packages": sorted(PLATFORM_PACKAGES),
        "domain_apps": sorted(DOMAIN_APPS),
        "composition_leaf_names": sorted(COMPOSITION_LEAF_NAMES),
        "layer_keywords": dict(sorted(LAYER_KEYWORDS.items())),
        "platform_layer_prefixes": dict(sorted(PLATFORM_LAYER_PREFIXES.items())),
        "module_layer_overrides": dict(sorted(MODULE_LAYER_OVERRIDES.items())),
        "cross_domain_model_edges": sorted(CROSS_DOMAIN_MODEL_EDGES),
        "area_labels": sorted(AREA_LABELS),
        "owner_by_module_prefix": dict(sorted(OWNER_BY_MODULE_PREFIX.items())),
        "excluded_directory_names": sorted(EXCLUDED_DIRECTORY_NAMES),
        "excluded_file_names": sorted(EXCLUDED_FILE_NAMES),
        "excluded_file_prefixes": sorted(EXCLUDED_FILE_PREFIXES),
        "dynamic_import_names": sorted(DYNAMIC_IMPORT_NAMES),
        "marker_pattern": MARKER_PATTERN,
        "annotation_pattern": ANNOTATION_PATTERN,
        "annotation_categories": sorted(POLICY_CATEGORIES),
        "typing_only_blocks": False,
    }


def compute_policy_fingerprint(targets):
    """Bind a baseline to the policy that produced it."""
    validate_policy()
    canonical = json.dumps(_policy_payload(targets), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
