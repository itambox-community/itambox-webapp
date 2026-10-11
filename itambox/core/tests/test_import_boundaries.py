"""Architecture boundary tests for resolved import cycles (issue #87, phase D).

Each cycle listed here was previously broken by a function-body import annotated
``# inline import: cycle: ...``. A deferred import hides a cycle; it does not
remove one. These tests pin the *resolved* direction so the cycle cannot be
reintroduced by a later change quietly re-adding the back edge.

The checks are AST-based and read the source tree directly, so they hold
regardless of import order at runtime and need no database.
"""

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

# ``itambox/`` -- the Django project root that holds the app packages.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PROJECT_ROOT.parent


def _module_path(dotted):
    """Return the file backing a first-party dotted module path."""
    candidate = PROJECT_ROOT.joinpath(*dotted.split("."))
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    package_init = candidate / "__init__.py"
    if package_init.is_file():
        return package_init
    raise AssertionError(f"No source file found for module {dotted!r} under {PROJECT_ROOT}")


def _package_of(dotted):
    """Return the package a module's relative imports resolve against."""
    path = _module_path(dotted)
    return dotted if path.name == "__init__.py" else dotted.rpartition(".")[0]


def _imported_modules(node, package):
    """Yield the dotted module names a single import statement can bind.

    ``package`` anchors relative imports, so ``from .services import x`` inside
    ``inventory.models`` is reported as ``inventory.services`` -- a relative
    import closes a cycle exactly as an absolute one does.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
        return
    if not isinstance(node, ast.ImportFrom):
        return
    base = node.module or ""
    if node.level:
        anchor = package.split(".") if package else []
        anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
        base = ".".join([*anchor, base]) if base else ".".join(anchor)
    if not base:
        return
    yield base
    for alias in node.names:
        yield f"{base}.{alias.name}"


def _is_type_checking_guard(node):
    """True for ``if TYPE_CHECKING:`` -- its body never executes at runtime."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_nodes(tree):
    """Walk every node except the bodies of ``if TYPE_CHECKING:`` guards."""
    stack = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        if _is_type_checking_guard(node):
            stack.extend(node.orelse)
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _edges(dotted, top_level_only):
    """Return the set of dotted module names ``dotted`` imports at runtime.

    ``top_level_only`` restricts the scan to module-scope statements (the
    imports that actually execute at import time); otherwise every scope is
    scanned, which is what makes a *deferred* cycle visible. Typing-only
    imports are excluded either way -- they create no runtime edge.
    """
    # source-text: static boundary test inspects production source by design
    tree = ast.parse(_module_path(dotted).read_text(encoding="utf-8"))
    package = _package_of(dotted)
    nodes = tree.body if top_level_only else _runtime_nodes(tree)
    found = set()
    for node in nodes:
        if top_level_only and _is_type_checking_guard(node):
            continue
        found.update(_imported_modules(node, package))
        if top_level_only and isinstance(node, ast.Try):
            # Optional-dependency guards still execute at import time.
            for child in node.body:
                found.update(_imported_modules(child, package))
    return found


def _deferred_imports(importer, imported):
    """True when ``importer`` imports ``imported`` from inside a function body."""
    prefix = f"{imported}."
    deferred = _edges(importer, top_level_only=False) - _edges(importer, top_level_only=True)
    return any(name == imported or name.startswith(prefix) for name in deferred)


def _imports(importer, imported, top_level_only=False):
    """True when ``importer`` imports ``imported`` (or a submodule of it)."""
    prefix = f"{imported}."
    return any(name == imported or name.startswith(prefix) for name in _edges(importer, top_level_only))


def _first_party_top_level_names():
    """Top-level module/package names that resolve inside the project root."""
    names = set()
    for entry in PROJECT_ROOT.iterdir():
        if entry.is_dir() and (entry / "__init__.py").is_file():
            names.add(entry.name)
        elif entry.suffix == ".py":
            names.add(entry.stem)
    return names


class RequestContextLeafTests(SimpleTestCase):
    """``core.context`` -- the leaf that owns the request-scoped contextvars.

    ``core.managers`` used to own the tenant vars and ``itambox.middleware`` the
    user/request-id vars, while each half read the other's: managers reached into
    the middleware for ``get_current_user`` and the middleware imported the
    managers' tenant setters; ``core.auth`` read the managers while the managers
    read ``core.auth.cache``. Those were real cycles that function-body imports
    only hid. The contextvars now live in ``core.context``, which imports nothing
    first-party, so every layer depends on it and none depends on another.
    """

    def test_context_module_imports_nothing_first_party(self):
        first_party = _first_party_top_level_names()
        self.assertIn("core", first_party, "sanity: the project root must be discoverable")
        offenders = sorted(
            name for name in _edges("core.context", top_level_only=False) if name.split(".")[0] in first_party
        )
        self.assertEqual(
            offenders,
            [],
            "core.context must stay a first-party leaf -- any first-party import here "
            f"re-creates the cycle it exists to break (found: {offenders})",
        )

    def test_managers_and_middleware_no_longer_import_each_other(self):
        self.assertFalse(
            _imports("core.managers", "itambox.middleware"),
            "core.managers must not import itambox.middleware, at module scope or deferred",
        )
        self.assertFalse(
            _imports("itambox.middleware", "core.managers"),
            "itambox.middleware must not import core.managers, at module scope or deferred",
        )

    def test_both_sides_read_the_context_leaf_at_module_scope(self):
        for module in ("core.managers", "itambox.middleware", "core.auth", "core.tasks.context", "extras.tasks.alerts"):
            with self.subTest(module=module):
                self.assertTrue(
                    _imports(module, "core.context", top_level_only=True),
                    f"{module} must import core.context at module scope, proving the cycle is gone",
                )

    def test_auth_no_longer_imports_managers_or_middleware(self):
        for importer in ("core.auth", "core.auth.cache"):
            for target in ("core.managers", "itambox.middleware"):
                with self.subTest(importer=importer, target=target):
                    self.assertFalse(
                        _imports(importer, target),
                        f"{importer} must not import {target}, at module scope or deferred",
                    )

    def test_managers_keeps_only_the_app_registry_deferred_auth_edge(self):
        """``core.managers`` -> ``core.auth.cache`` stays deferred, and only for
        the app registry: ``core.auth.__init__`` calls ``get_user_model()`` at
        module scope, so a model-layer module cannot import it at load time.
        That is not the cycle this phase removed -- the back edge was
        ``core.auth`` -> ``core.managers``, and it is gone (asserted above)."""
        self.assertFalse(
            _imports("core.managers", "core.auth", top_level_only=True),
            "core.managers must not import core.auth at module scope (AppRegistryNotReady)",
        )
        # The remaining ``cycle:`` annotations in core.managers are for the
        # separate core.managers <-> organization edge, which this phase does not
        # touch. None of them may name the middleware or the auth package again.
        offending = [
            line.strip()
            for line in _module_path("core.managers").read_text(encoding="utf-8").splitlines()
            if "inline import" in line and "cycle" in line and ("middleware" in line or "core.auth" in line)
        ]
        self.assertEqual(
            offending,
            [],
            "no inline import in core.managers may still be justified as a cycle with the "
            f"middleware or the auth package -- those cycles are resolved (found: {offending})",
        )

    def test_mfa_is_a_policy_leaf(self):
        self.assertFalse(
            _imports("core.mfa", "core.auth"),
            "core.mfa must not import core.auth, at module scope or deferred -- the privilege "
            "classification lives on the policy side of the edge",
        )
        import core.mfa as mfa

        self.assertEqual(mfa.PRIVILEGED_ROLE_NAMES, {"Admin", "Manager"})

    def test_privileged_role_names_has_a_single_home(self):
        import core.mfa as mfa

        self.assertEqual(
            mfa.PRIVILEGED_ROLE_NAMES,
            {"Admin", "Manager"},
            "the classification must remain owned by core.mfa",
        )


class DashboardAndResidualImportTests(SimpleTestCase):
    def _function_source(self, relative_path, class_name, function_name):
        # source-text: static boundary test inspects production source by design
        tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
        if class_name is None:
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                    return ast.unparse(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == function_name:
                        return ast.unparse(child)
        raise AssertionError(f"Could not find {class_name}.{function_name}")

    def test_issue_447_residual_imports_are_module_top(self):
        # source-text: static boundary test inspects production source by design
        widgets = ast.parse(_module_path("extras.dashboard.widgets").read_text(encoding="utf-8"))
        top_tenant_imports = [
            node
            for node in widgets.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "organization.models"
            and any(alias.name == "Tenant" for alias in node.names)
        ]
        self.assertEqual(len(top_tenant_imports), 1)
        for function_name in ("_resolve_target_tenant", "get_config_form"):
            source = self._function_source(
                "extras/dashboard/widgets.py",
                "DashboardWidget" if function_name == "get_config_form" else None,
                function_name,
            )
            self.assertNotIn("from organization.models import Tenant", source)

        # source-text: static boundary test inspects production source by design
        inventory = ast.parse((PROJECT_ROOT / "inventory/services.py").read_text(encoding="utf-8"))
        self.assertTrue(
            any(
                isinstance(node, ast.ImportFrom)
                and node.module == "core.managers"
                and any(alias.name == "get_current_tenant" for alias in node.names)
                for node in inventory.body
            )
        )
        recipient = next(
            node
            for node in ast.walk(inventory)
            if isinstance(node, ast.FunctionDef) and node.name == "recipient_assignment_union"
        )
        self.assertFalse(
            any(
                isinstance(node, ast.ImportFrom)
                and node.module == "core.managers"
                and any(alias.name == "get_current_tenant" for alias in node.names)
                for node in ast.walk(recipient)
            )
        )

        # source-text: static boundary test inspects production source by design
        membership = ast.parse((PROJECT_ROOT / "organization/views/membership_views.py").read_text(encoding="utf-8"))
        access_import = next(
            node
            for node in membership.body
            if isinstance(node, ast.ImportFrom) and node.level == 2 and node.module == "access"
        )
        self.assertEqual(
            {alias.name for alias in access_import.names},
            {"accessible_tenant_ids", "get_descendant_tenant_group_ids", "tenant_access_report"},
        )
        context_ids = next(
            node
            for node in ast.walk(membership)
            if isinstance(node, ast.FunctionDef) and node.name == "_context_tenant_ids"
        )
        self.assertFalse(
            any(
                isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "organization.access"
                for node in ast.walk(context_ids)
            )
        )

    def test_dashboard_target_authorization_does_not_use_ambient_catalogue_or_membership_rule(self):
        source = self._function_source("extras/dashboard/widgets.py", "DashboardWidget", "get_config_form")
        self.assertNotIn("Tenant.objects", source)

        create_source = self._function_source("extras/dashboard/views.py", "DashboardCreateView", "post")
        self.assertNotIn("Membership.objects", create_source)


class RequestContextReExportTests(SimpleTestCase):
    """The re-exports must be the *same objects*, not copies.

    ``core.managers`` and ``itambox.middleware`` keep publishing the names their
    long-standing import sites use. Identity is what makes that behaviour-
    preserving: a duplicated ``ContextVar`` would give each importer a private
    slot, so a write through one path would be invisible through another and a
    ``Token`` from one would not reset the other.
    """

    MANAGER_NAMES = (
        "_current_all_accessible",
        "_current_membership",
        "_current_tenant",
        "_current_tenant_group",
        "_descendant_group_ids_cache",
        "get_current_all_accessible",
        "get_current_membership",
        "get_current_scope_conflict",
        "get_current_tenant",
        "get_current_tenant_group",
        "get_current_user",
        "set_current_all_accessible",
        "set_current_membership",
        "set_current_tenant",
        "set_current_tenant_group",
    )
    MIDDLEWARE_NAMES = (
        "_current_user",
        "_request_id",
        "get_current_all_accessible",
        "get_current_membership",
        "get_current_request_id",
        "get_current_tenant",
        "get_current_tenant_group",
        "get_current_user",
        "set_current_all_accessible",
        "set_current_membership",
        "set_current_tenant",
        "set_current_tenant_group",
        "set_current_user",
    )

    def test_managers_re_exports_are_identical_objects(self):
        import core.context as context
        import core.managers as managers

        for name in self.MANAGER_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(managers, name), getattr(context, name))

    def test_middleware_re_exports_are_identical_objects(self):
        import core.context as context
        import itambox.middleware as middleware

        for name in self.MIDDLEWARE_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(middleware, name), getattr(context, name))

    def test_writes_are_visible_through_every_import_path(self):
        import core.context as context
        import core.managers as managers
        import itambox.middleware as middleware

        sentinel = object()
        managers.set_current_tenant(sentinel)
        try:
            self.assertIs(context.get_current_tenant(), sentinel)
            self.assertIs(middleware.get_current_tenant(), sentinel)
        finally:
            managers.set_current_tenant(None)

        middleware.set_current_user(sentinel)
        try:
            self.assertIs(context.get_current_user(), sentinel)
            self.assertIs(managers.get_current_user(), sentinel)
        finally:
            middleware._current_user.set(None)

    def test_tokens_reset_across_import_paths(self):
        """A ``Token`` is bound to one ``ContextVar`` instance; resetting it via a
        different import path only works because they are the same object. This
        is exactly what ``CurrentUserMiddleware.process_response`` relies on."""
        import core.context as context
        import itambox.middleware as middleware

        outer = object()
        context._current_user.set(outer)
        token = middleware._current_user.set(object())
        try:
            self.assertIsNot(context.get_current_user(), outer)
            context._current_user.reset(token)
            self.assertIs(middleware.get_current_user(), outer)
        finally:
            context._current_user.set(None)

    def test_setters_still_invalidate_the_descendant_group_cache(self):
        """Every scope setter clears the memoised descendant-group ids. Losing
        that during the move would leak a previous scope's group set into the
        next one."""
        import core.context as context
        import core.managers as managers

        setters = (
            (managers.set_current_tenant, None),
            (managers.set_current_tenant_group, None),
            (managers.set_current_membership, None),
            (managers.set_current_all_accessible, False),
        )
        for setter, value in setters:
            with self.subTest(setter=setter.__name__):
                context._descendant_group_ids_cache.set({1, 2, 3})
                setter(value)
                self.assertIsNone(managers._descendant_group_ids_cache.get())


class ScopeConflictBehaviourTests(SimpleTestCase):
    """``get_current_scope_conflict`` moved module but must classify identically."""

    @staticmethod
    def _User(authenticated=True, superuser=False):
        """A stand-in principal -- the classifier only reads these two flags."""
        return SimpleNamespace(is_authenticated=authenticated, is_superuser=superuser)

    def tearDown(self):
        import core.context as context

        context.set_current_tenant(None)
        context.set_current_tenant_group(None)
        context.set_current_all_accessible(False)

    def test_two_active_scopes_conflict_for_a_plain_user(self):
        import core.managers as managers

        managers.set_current_tenant(object())
        managers.set_current_all_accessible(True)
        self.assertTrue(managers.get_current_scope_conflict(self._User()))

    def test_single_scope_anonymous_and_superuser_never_conflict(self):
        import core.managers as managers

        managers.set_current_tenant(object())
        self.assertFalse(managers.get_current_scope_conflict(self._User()))

        managers.set_current_all_accessible(True)
        self.assertFalse(managers.get_current_scope_conflict(self._User(superuser=True)))
        self.assertFalse(managers.get_current_scope_conflict(self._User(authenticated=False)))
        self.assertFalse(managers.get_current_scope_conflict(None))


class ApiSerializerBoundaryTests(SimpleTestCase):
    """``itambox.api.base`` <-> ``itambox.api.utils``.

    ``base`` needed one pure lookup helper out of ``utils``; ``utils`` needs the
    serializer base class for an ``isinstance`` check. The helper now lives in
    the leaf module ``itambox.api.related``, so the edge runs one way only:
    ``utils -> base -> related``.
    """

    def test_related_helper_module_is_a_leaf(self):
        self.assertFalse(
            _imports("itambox.api.related", "itambox.api.base"),
            "itambox.api.related must not depend on itambox.api.base",
        )
        self.assertFalse(
            _imports("itambox.api.related", "itambox.api.utils"),
            "itambox.api.related must not depend on itambox.api.utils",
        )

    def test_base_does_not_import_utils_in_any_scope(self):
        self.assertFalse(
            _imports("itambox.api.base", "itambox.api.utils"),
            "itambox.api.base must not import itambox.api.utils, at module scope or deferred",
        )

    def test_utils_imports_base_at_module_scope(self):
        self.assertTrue(
            _imports("itambox.api.utils", "itambox.api.base", top_level_only=True),
            "itambox.api.utils must import itambox.api.base at module scope, proving the cycle is gone",
        )


class LicensingBoundaryTests(SimpleTestCase):
    """``software.models`` <-> ``licenses.models`` / ``licenses.reconciliation``.

    ``licenses`` owns the FK onto ``software``, so that is the load-bearing
    direction. ``Software.license_count`` is the back edge and resolves its model through
    the app registry; ``licenses.reconciliation`` is a runtime leaf.
    """

    def test_licenses_models_imports_software_models_at_module_scope(self):
        self.assertTrue(
            _imports("licenses.models", "software.models", top_level_only=True),
            "licenses.models owns the FK onto software.models; that edge is the load-bearing direction",
        )

    def test_software_models_does_not_import_licenses_models(self):
        self.assertFalse(
            _imports("software.models", "licenses.models"),
            "software.models must not import licenses.models, at module scope or deferred",
        )

    def test_reconciliation_is_a_runtime_leaf(self):
        for target in ("licenses.models", "software.models"):
            with self.subTest(target=target):
                self.assertFalse(
                    _imports("licenses.reconciliation", target),
                    f"licenses.reconciliation must not import {target} at runtime; it resolves models "
                    "through the app registry so software.models can depend on it",
                )

    def test_software_models_has_no_deferred_licenses_import(self):
        self.assertFalse(
            _deferred_imports("software.models", "licenses"),
            "software.models must not defer a licenses import into a function body",
        )


class InventoryStockBoundaryTests(SimpleTestCase):
    """``inventory.models`` <-> ``inventory.services``.

    Six ``save()``/``delete()`` overrides on the assignment models needed
    ``adjust_inventory_stock`` out of the service layer, while the service layer
    imports the models at module scope for the checkout/check-in flows -- so that
    is the load-bearing direction. The bookkeeping helper now lives in the leaf
    module ``inventory.models_stock``, which resolves the stock model through the app
    registry via the ``_stock_model_label`` hook already on
    ``AbstractAssignment``; ``inventory.services`` keeps re-exporting it.
    """

    def test_stock_module_is_a_leaf(self):
        for target in ("inventory.models", "inventory.services", "inventory.abstract_models", "assets.services"):
            with self.subTest(target=target):
                self.assertFalse(
                    _imports("inventory.models_stock", target),
                    f"inventory.models_stock must not import {target}; it is the leaf that breaks the cycle, "
                    "resolving the stock model through the app registry instead",
                )

    def test_inventory_services_imports_models_at_module_scope(self):
        self.assertTrue(
            _imports("inventory.services", "inventory.models", top_level_only=True),
            "inventory.services -> inventory.models is the load-bearing direction",
        )

    def test_models_do_not_import_inventory_services(self):
        self.assertFalse(
            _imports("inventory.models", "inventory.services"),
            "inventory.models must not import inventory.services, at module scope or deferred",
        )

    def test_inventory_models_has_no_deferred_first_party_inventory_import(self):
        for target in ("inventory.services", "inventory.models_stock"):
            with self.subTest(target=target):
                self.assertFalse(
                    _deferred_imports("inventory.models", target),
                    f"inventory.models must import {target} at module scope, not from a function body",
                )

    def test_services_still_exposes_adjust_inventory_stock(self):
        self.assertTrue(
            _imports("inventory.services", "inventory.models_stock", top_level_only=True),
            "inventory.services must keep re-exporting adjust_inventory_stock from inventory.models_stock",
        )

        from inventory.models_stock import adjust_inventory_stock as via_leaf
        from inventory.services import adjust_inventory_stock as via_services

        self.assertIs(via_services, via_leaf, "the published inventory.services call path must stay valid")


class MembershipServiceLayerTests(SimpleTestCase):
    """``organization.services.*`` -> ``organization.forms``.

    The membership/RBAC services (issue #86) exist so the domain decisions no
    longer live in ``MembershipForm``. The form imports the services; the
    services must never import the form, at module scope or deferred, or the
    extraction has only added an indirection to the same cycle.

    ``organization.services.__init__`` additionally keeps its re-export surface
    narrow: ``itambox.views.features`` imports it at module scope, so pulling
    ``membership``/``rolegrants`` (and through them ``core.auth``) into the
    package ``__init__`` would widen that edge for no benefit.
    """

    SERVICE_MODULES = ("organization.services.membership", "organization.services.rolegrants")

    def test_services_do_not_import_the_form_layer(self):
        for module in self.SERVICE_MODULES:
            with self.subTest(module=module):
                self.assertFalse(
                    _imports(module, "organization.forms"),
                    f"{module} must not import organization.forms, at module scope or deferred -- "
                    "the form depends on the service, never the reverse",
                )

    def test_form_imports_the_services_at_module_scope(self):
        self.assertTrue(
            _imports("organization.forms.membership_form", "organization.services", top_level_only=True),
            "organization.forms.membership_form -> organization.services is the load-bearing direction",
        )

    def test_package_init_does_not_pull_in_the_membership_services(self):
        for module in self.SERVICE_MODULES:
            with self.subTest(module=module):
                self.assertFalse(
                    _imports("organization.services", module),
                    f"organization.services.__init__ must not import {module}: extras.export_views "
                    "imports the package at module scope and would gain an edge to core.auth",
                )

    def test_resource_access_helpers_stay_importable_from_the_package(self):
        """The package conversion must keep every published name byte-identical
        for the existing importers (``itambox.views.features``,
        ``organization.views.membership_views``, ``inventory.services``)."""
        import organization.services as services
        import organization.services.resource_access as resource_access

        for name in (
            "visible_to_containers",
            "is_container_scoped_unfiltered",
            "resolve_stock_access",
            "resolved_shared_stock_ids",
            "ResourceAccessDecision",
            "REASON_SAME_TENANT",
            "REASON_DIRECT_GRANT",
            "REASON_GROUP_GRANT",
            "DENIED_NO_ACTIVE_TENANT",
            "DENIED_OWNER_UNRESOLVABLE",
            "DENIED_NO_GRANT",
            "DENIED_INVALID_ACCESS_LEVEL",
            "DENIED_INSUFFICIENT_LEVEL",
            "DENIED_RBAC",
            "DENIED_UNSUPPORTED_RESOURCE",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(services, name), getattr(resource_access, name))


SANCTIONED_RESOURCE_BOUNDARY_CALLS = {
    "is_container_scoped_unfiltered",
    "resolve_stock_access",
    "resolved_shared_stock_ids",
    "shared_stock_read_allowed",
    "visible_to_containers",
}

ASSIGNMENT_MODELS = frozenset({"AccessoryAssignment", "ComponentAllocation", "ConsumableAssignment"})
# Every manager write that reaches the database without a checkout service.
# ``create`` alone left ``get_or_create`` as a documented, in-tree bypass.
ASSIGNMENT_WRITE_METHODS = frozenset(
    {"_raw_delete", "bulk_create", "bulk_update", "create", "delete", "get_or_create", "update", "update_or_create"}
)
ASSIGNMENT_MANAGERS = frozenset({"_base_manager", "_default_manager", "all_objects", "objects"})
ASSIGNMENT_INSTANCE_WRITE_METHODS = frozenset({"delete", "restore", "save"})
ASSIGNMENT_READ_METHODS = frozenset({"first", "get", "last"})
RESOURCE_GRANT_TEST_MANIFEST = json.loads(
    (REPO_ROOT / "scripts" / "resource_grant_test_manifest.json").read_text(encoding="utf-8")
)
MANDATORY_RESOURCE_GRANT_TESTS = tuple(RESOURCE_GRANT_TEST_MANIFEST["mandatory_tests"])


def _propagate_grant_aliases(tree, grant_names):
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value_path = _attribute_path(node.value)
            if value_path is None:
                continue
            is_grant = value_path in grant_names or value_path.rsplit(".", 1)[-1] == "TenantResourceGrant"
            if not is_grant:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                target_path = _attribute_path(target)
                if target_path and target_path not in grant_names:
                    grant_names.add(target_path)
                    changed = True


def _grant_import_names(tree):
    grant_names = {"TenantResourceGrant"}
    organization_model_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "organization.models":
            grant_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "TenantResourceGrant"
            )
        if isinstance(node, ast.Import):
            organization_model_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "organization.models"
            )
    _propagate_grant_aliases(tree, grant_names)
    return grant_names, organization_model_modules


def _attribute_path(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_path(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _raw_grant_manager_violation(node, grant_names, organization_model_modules):
    if not isinstance(node, ast.Attribute) or node.attr not in {"objects", "_base_manager"}:
        return None
    target = node.value
    if isinstance(target, ast.Name) and target.id in grant_names:
        return node.lineno, "raw-grant-manager"
    target_path = _attribute_path(target)
    if any(target_path == f"{module}.TenantResourceGrant" for module in organization_model_modules):
        return node.lineno, "qualified-grant-manager"
    return None


def _assignment_model_aliases(tree):
    """Local names bound to an assignment model, ``as`` aliases included."""
    aliases = set(ASSIGNMENT_MODELS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name in ASSIGNMENT_MODELS)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value_path = _attribute_path(node.value)
            if not value_path or value_path.rsplit(".", 1)[-1] not in aliases:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                target_path = _attribute_path(target)
                if target_path and target_path not in aliases:
                    aliases.add(target_path)
                    changed = True
    return aliases


def _assignment_manager_owner(node):
    """The model expression behind a manager, through any chained queryset call.

    ``Model.objects.create`` and ``Model.objects.filter(...).create`` are the
    same bypass; only the depth of the chain differs.
    """
    while True:
        if isinstance(node, ast.Call):
            node = node.func
            continue
        if not isinstance(node, ast.Attribute):
            return None
        if node.attr in ASSIGNMENT_MANAGERS:
            return node.value
        node = node.value


def _assignment_write_violation(node, aliases):
    """One manager-level assignment write, however the model is spelled."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    method = node.func.attr
    if method not in ASSIGNMENT_WRITE_METHODS:
        return None
    model = _assignment_manager_owner(node.func.value)
    if model is None:
        return None
    if isinstance(model, ast.Name):
        return (node.lineno, f"direct-{method}") if model.id in aliases else None
    path = _attribute_path(model)
    if path and path.rsplit(".", 1)[-1] in ASSIGNMENT_MODELS:
        return node.lineno, f"qualified-{method}"
    return None


def _is_assignment_model_expr(node, aliases):
    path = _attribute_path(node)
    return bool(
        (isinstance(node, ast.Name) and node.id in aliases) or (path and path.rsplit(".", 1)[-1] in ASSIGNMENT_MODELS)
    )


def _assignment_target_paths(target):
    if isinstance(target, (ast.Tuple, ast.List)):
        return [path for element in target.elts for path in _assignment_target_paths(element)]
    path = _attribute_path(target)
    return [path] if path else []


def _assignment_call_kind(value, aliases, queryset_paths):
    if not isinstance(value, ast.Call):
        return None
    if isinstance(value.func, ast.Name) and value.func.id in aliases:
        return "instance"
    if isinstance(value.func, ast.Name) and value.func.id == "get_object_or_404" and value.args:
        first = value.args[0]
        owner = _assignment_manager_owner(first)
        if (
            _is_assignment_model_expr(first, aliases)
            or _is_assignment_model_expr(owner, aliases)
            or _attribute_path(first) in queryset_paths
        ):
            return "instance"
    if not isinstance(value.func, ast.Attribute):
        return None
    owner = _assignment_manager_owner(value.func.value)
    receiver_path = _attribute_path(value.func.value)
    if not (_is_assignment_model_expr(owner, aliases) or receiver_path in queryset_paths):
        return None
    return "instance" if value.func.attr in ASSIGNMENT_READ_METHODS else "queryset"


def _assignment_dataflow_paths(tree, aliases):
    instance_paths = set()
    queryset_paths = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                targets = [path for target in targets for path in _assignment_target_paths(target)]
                value_path = _attribute_path(node.value)
                kind = (
                    "instance" if value_path in instance_paths else "queryset" if value_path in queryset_paths else None
                )
                owner = _assignment_manager_owner(node.value) if not isinstance(node.value, ast.Call) else None
                if kind is None and _is_assignment_model_expr(owner, aliases):
                    kind = "queryset"
                kind = kind or _assignment_call_kind(node.value, aliases, queryset_paths)
                selected = instance_paths if kind == "instance" else queryset_paths if kind == "queryset" else None
                if selected is not None:
                    before = len(selected)
                    selected.update(targets)
                    changed = changed or len(selected) != before
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                owner = _assignment_manager_owner(node.iter)
                if _is_assignment_model_expr(owner, aliases) or _attribute_path(node.iter) in queryset_paths:
                    before = len(instance_paths)
                    instance_paths.update(_assignment_target_paths(node.target))
                    changed = changed or len(instance_paths) != before
    return instance_paths, queryset_paths


def _assignment_write_violations(source, filename="<mutation>"):
    # source-text: static boundary test inspects production source by design
    tree = ast.parse(source, filename=filename)
    aliases = _assignment_model_aliases(tree)
    violations = [
        violation for node in ast.walk(tree) if (violation := _assignment_write_violation(node, aliases)) is not None
    ]
    instance_paths, queryset_paths = _assignment_dataflow_paths(tree, aliases)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = _attribute_path(node.func.value)
        if receiver in instance_paths and node.func.attr in ASSIGNMENT_INSTANCE_WRITE_METHODS:
            violations.append((node.lineno, f"instance-{node.func.attr}"))
        if receiver in queryset_paths and node.func.attr in ASSIGNMENT_WRITE_METHODS:
            violations.append((node.lineno, f"queryset-alias-{node.func.attr}"))
    return violations


def _called_name(node):
    if not isinstance(node, ast.Call):
        return None
    return node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)


def _forbidden_capability_symbols(source, path, allowed_symbols):
    return [symbol for symbol, allowed in allowed_symbols.items() if symbol in source and path not in allowed]


def _forwards_system_overallocation(node):
    return (
        isinstance(node, ast.Call)
        and _called_name(node) == "create_component_allocation"
        and any(keyword.arg == "system_allow_overallocate" or keyword.arg is None for keyword in node.keywords)
    )


def _resource_grant_boundary_violations(source, filename="<mutation>"):
    # source-text: static boundary test inspects production source by design
    tree = ast.parse(source, filename=filename)
    nodes = list(ast.walk(tree))
    grant_names, organization_model_modules = _grant_import_names(tree)
    violations = [
        (node.lineno, "dynamic-grant-model")
        for node in nodes
        if isinstance(node, ast.Constant) and node.value == "TenantResourceGrant"
    ]
    violations.extend(
        violation
        for node in nodes
        if (violation := _raw_grant_manager_violation(node, grant_names, organization_model_modules))
    )
    if not any(_called_name(node) in SANCTIONED_RESOURCE_BOUNDARY_CALLS for node in nodes):
        violations.append((1, "missing-sanctioned-boundary-call"))
    return violations


class TenantResourceGrantBoundaryTests(SimpleTestCase):
    """Freeze the sanctioned shared-resource authorization call graph (#194)."""

    def test_production_shared_resource_id_calls_stay_inside_canonical_resolver(self):
        allowed = {
            PROJECT_ROOT / "organization" / "access.py",
            PROJECT_ROOT / "organization" / "services" / "resource_access.py",
        }
        bypasses = []

        for path in PROJECT_ROOT.rglob("*.py"):
            relative_parts = path.relative_to(PROJECT_ROOT).parts
            if "tests" in relative_parts or "migrations" in relative_parts or path in allowed:
                continue
            # source-text: static boundary test inspects production source by design
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "shared_resource_ids" for alias in node.names
                ):
                    bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if isinstance(node, ast.Constant) and node.value == "shared_resource_ids":
                    bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if called_name == "shared_resource_ids":
                    bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

        self.assertEqual(
            bypasses,
            [],
            "shared_resource_ids is only a candidate preselector inside the canonical "
            f"resource-access resolver; direct production bypasses: {bypasses}",
        )

    def test_exposed_stock_surfaces_do_not_query_resource_grants_directly(self):
        boundary_files = (
            PROJECT_ROOT / "inventory" / "services.py",
            PROJECT_ROOT / "inventory" / "tables.py",
            PROJECT_ROOT / "inventory" / "forms" / "base_forms.py",
            PROJECT_ROOT / "inventory" / "views" / "accessory_views.py",
            PROJECT_ROOT / "inventory" / "views" / "component_views.py",
            PROJECT_ROOT / "inventory" / "views" / "consumable_views.py",
            PROJECT_ROOT / "itambox" / "api" / "permissions.py",
            PROJECT_ROOT / "extras" / "export_views.py",
        )
        bypasses = []
        for path in boundary_files:
            for line, reason in _resource_grant_boundary_violations(
                path.read_text(encoding="utf-8"), filename=str(path)
            ):
                bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{line}:{reason}")
        raw_only_surfaces = (
            PROJECT_ROOT / "inventory" / "forms" / "accessory_forms.py",
            PROJECT_ROOT / "inventory" / "forms" / "component_forms.py",
            PROJECT_ROOT / "inventory" / "forms" / "consumable_forms.py",
        )
        for path in raw_only_surfaces:
            for line, reason in _resource_grant_boundary_violations(
                path.read_text(encoding="utf-8"), filename=str(path)
            ):
                if reason != "missing-sanctioned-boundary-call":
                    bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{line}:{reason}")

        self.assertEqual(
            bypasses,
            [],
            "exposed stock surfaces must use resolve_stock_access or "
            f"resolved_shared_stock_ids, never raw grant managers: {bypasses}",
        )

    def test_assignment_mutation_surfaces_call_canonical_services(self):
        """API, request, import, and seed writers stay above inventory services."""
        surface_calls = {
            PROJECT_ROOT / "inventory" / "api" / "views.py": {"checkout_inventory_item"},
            PROJECT_ROOT / "assets" / "views" / "request_views.py": {"checkout_inventory_item"},
            PROJECT_ROOT / "core" / "importers" / "snipeit" / "common.py": {
                "_checkout_inventory_item",
                "_create_component_allocation",
                "checkout_inventory_item",
                "create_component_allocation",
            },
            PROJECT_ROOT / "core" / "management" / "commands" / "_seed" / "assets.py": {"create_component_allocation"},
            PROJECT_ROOT / "core" / "management" / "commands" / "_seed" / "inventory.py": {"checkout_inventory_item"},
        }
        for path, sanctioned_calls in surface_calls.items():
            with self.subTest(surface=path.relative_to(PROJECT_ROOT)):
                # source-text: static boundary test inspects production source by design
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                called = {name for node in ast.walk(tree) if (name := _called_name(node))}
                self.assertTrue(
                    called & sanctioned_calls,
                    f"{path.relative_to(PROJECT_ROOT)} bypasses canonical inventory mutation services",
                )
                raw_grant_bypasses = [
                    (line, reason)
                    for line, reason in _resource_grant_boundary_violations(
                        path.read_text(encoding="utf-8"), filename=str(path)
                    )
                    if reason != "missing-sanctioned-boundary-call"
                ]
                self.assertEqual(raw_grant_bypasses, [], path.relative_to(PROJECT_ROOT))

    def test_manifest_mandatory_selector_matches_complete_manifest(self):
        self.assertTrue(all((REPO_ROOT / relative).is_file() for relative in MANDATORY_RESOURCE_GRANT_TESTS))
        manifest_coverage = set(RESOURCE_GRANT_TEST_MANIFEST["changed_tests"]) | set(
            RESOURCE_GRANT_TEST_MANIFEST["baseline_tests"]
        )
        self.assertEqual(set(MANDATORY_RESOURCE_GRANT_TESTS), manifest_coverage)
        self.assertTrue(
            all((REPO_ROOT / relative).is_file() for relative in RESOURCE_GRANT_TEST_MANIFEST["supporting_files"])
        )

        if (REPO_ROOT / ".git").exists():
            base = RESOURCE_GRANT_TEST_MANIFEST["base_commit"]
            diff = subprocess.run(
                ["git", "diff", "--name-only", "--find-renames=20%", base, "--"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            candidates = {path.replace("\\", "/") for path in diff}
            candidates.update(line[3:].replace("\\", "/") for line in status if line.startswith("?? "))
            changed_tests = {
                path
                for path in candidates
                if path.endswith(".py") and ("/tests/test_" in path or path.startswith("scripts/tests/test_"))
            }
            self.assertEqual(changed_tests, set(RESOURCE_GRANT_TEST_MANIFEST["changed_tests"]))

    def test_corruption_fixture_is_outside_the_production_image_copy_context(self):
        helper = REPO_ROOT / "scripts" / "tests" / "assignment_corruption.py"
        self.assertTrue(helper.is_file())
        self.assertNotIn(PROJECT_ROOT, helper.parents)
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        production_copies = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("COPY ")]
        self.assertFalse(
            any(line.startswith("COPY scripts") or line.startswith("COPY . ") for line in production_copies)
        )

    def test_boundary_gate_rejects_alias_dynamic_and_omission_mutations(self):
        mutations = {
            "alias": "from organization.models import TenantResourceGrant as Grant\nGrant.objects.all()",
            "qualified": "import organization.models as models\nmodels.TenantResourceGrant.objects.all()",
            "fully-qualified": (
                "import organization.models\n"
                "resolve_stock_access(user, stock, level, perm)\n"
                "organization.models.TenantResourceGrant.objects.all()"
            ),
            "content-type": (
                "from django.contrib.contenttypes.models import ContentType\n"
                "from organization.models import TenantResourceGrant as Grant\n"
                "resolve_stock_access(user, stock, level, perm)\n"
                "Grant.objects.filter(resource_type=ContentType.objects.get_for_model(Model))"
            ),
            "dynamic": "resolve_stock_access(user, stock, level, perm)\ngetattr(models, 'TenantResourceGrant')",
            "alias-propagation": (
                "from organization.models import TenantResourceGrant\n"
                "Grant = TenantResourceGrant\n"
                "resolve_stock_access(user, stock, level, perm)\n"
                "Grant.objects.all()"
            ),
            "omission": "def visible_surface(queryset):\n    return queryset.all()",
        }
        for mutation, source in mutations.items():
            with self.subTest(mutation=mutation):
                self.assertTrue(_resource_grant_boundary_violations(source))

    def test_assignment_write_capabilities_stay_on_sanctioned_seams(self):
        write_module = PROJECT_ROOT / "inventory" / "services.py"
        validation_module = PROJECT_ROOT / "inventory" / "forms" / "component_forms.py"
        bypasses = []
        for path in PROJECT_ROOT.rglob("*.py"):
            relative_parts = path.relative_to(PROJECT_ROOT).parts
            if "tests" in relative_parts or "migrations" in relative_parts:
                continue
            source = path.read_text(encoding="utf-8")
            # source-text: static boundary test inspects production source by design
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                for alias in node.names:
                    if alias.name == "authorized_assignment_write" and path != write_module:
                        bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:write-permit")
                    if alias.name == "authorized_assignment_validation" and path != validation_module:
                        bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:validation-permit")
            if path != write_module:
                for line, reason in _assignment_write_violations(source, filename=str(path)):
                    bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{line}:{reason}")
        self.assertEqual(bypasses, [], f"assignment writes bypass sanctioned services: {bypasses}")

    def test_internal_cascade_and_overallocation_capabilities_stay_on_exact_seams(self):
        allowed_symbols = {
            "authorized_assignment_write": {
                PROJECT_ROOT / "inventory" / "models_assignment_write.py",
                PROJECT_ROOT / "inventory" / "services.py",
            },
            "authorized_assignment_hard_purge": {
                PROJECT_ROOT / "inventory" / "models_assignment_write.py",
                PROJECT_ROOT / "inventory" / "services.py",
            },
            "assignment_hard_purge_is_permitted": {
                PROJECT_ROOT / "inventory" / "models_assignment_write.py",
                PROJECT_ROOT / "inventory" / "models.py",
            },
            "purge_inventory_assignment": {
                PROJECT_ROOT / "inventory" / "services.py",
                PROJECT_ROOT / "inventory" / "apps.py",
            },
        }
        overallocate_root = PROJECT_ROOT / "core" / "management" / "commands" / "_seed" / "assets.py"
        bypasses = []
        for path in PROJECT_ROOT.rglob("*.py"):
            relative_parts = path.relative_to(PROJECT_ROOT).parts
            if "tests" in relative_parts or "migrations" in relative_parts:
                continue
            source = path.read_text(encoding="utf-8")
            for symbol in _forbidden_capability_symbols(source, path, allowed_symbols):
                bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{symbol}")
            # source-text: static boundary test inspects production source by design
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if _forwards_system_overallocation(node) and path != overallocate_root:
                    bypasses.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:overallocate")
        self.assertEqual(bypasses, [], f"internal assignment capabilities escaped their exact seams: {bypasses}")

    def test_capability_gate_rejects_qualified_permits_and_kwargs_forwarding(self):
        rogue_path = PROJECT_ROOT / "rogue.py"
        allowed = {
            "authorized_assignment_write": {
                PROJECT_ROOT / "inventory" / "models_assignment_write.py",
                PROJECT_ROOT / "inventory" / "services.py",
            }
        }
        qualified = (
            "import inventory.models_assignment_write as maw\n"
            "with maw.authorized_assignment_write(row):\n"
            "    row.save()"
        )
        self.assertEqual(_forbidden_capability_symbols(qualified, rogue_path, allowed), ["authorized_assignment_write"])

        forwarding = (
            'kwargs = {"system_allow_overallocate": True}\ncreate_component_allocation(**kwargs)',
            "system_allow_overallocate = True\n"
            "create_component_allocation(**{'system_allow_overallocate': system_allow_overallocate})",
        )
        for source in forwarding:
            with self.subTest(source=source):
                # source-text: static boundary test inspects production source by design
                self.assertTrue(any(_forwards_system_overallocation(node) for node in ast.walk(ast.parse(source))))

    def test_assignment_write_gate_rejects_alias_qualified_and_method_mutations(self):
        mutations = {
            "create": "AccessoryAssignment.objects.create(qty=1)",
            "get-or-create": "AccessoryAssignment.objects.get_or_create(qty=1)",
            "update-or-create": "ConsumableAssignment.objects.update_or_create(qty=1)",
            "bulk-create": "ComponentAllocation.objects.bulk_create([])",
            "update": "AccessoryAssignment.objects.filter(qty=1).update(qty=2)",
            "bulk-update": "ConsumableAssignment.objects.bulk_update([], ['qty'])",
            "delete": "ComponentAllocation.objects.filter(qty=1).delete()",
            "alias": ("from inventory.models import AccessoryAssignment as Alloc\nAlloc.objects.get_or_create(qty=1)"),
            "qualified": ("import inventory.models\ninventory.models.ComponentAllocation.objects.create(qty=1)"),
            "aliased-module": ("import inventory.models as m\nm.ConsumableAssignment._base_manager.create(qty=1)"),
            "all-objects": "AccessoryAssignment.all_objects.get_or_create(qty=1)",
            "default-manager": "AccessoryAssignment._default_manager.create(qty=1)",
            "chained-queryset": "AccessoryAssignment.objects.filter(qty=1).get_or_create(qty=1)",
            "read-mutate-save": (
                "assignment = AccessoryAssignment.objects.get(pk=1)\nassignment.target_tenant_id = 2\nassignment.save()"
            ),
            "read-instance-delete": "assignment = ComponentAllocation.objects.get(pk=1)\nassignment.delete()",
            "get-object-queryset": (
                "assignment = get_object_or_404(AccessoryAssignment.objects.all(), pk=1)\nassignment.save()"
            ),
            "queryset-alias": (
                "rows = ComponentAllocation.objects.filter(qty=1)\nassignment = rows.get(pk=1)\nassignment.delete()"
            ),
            "instance-alias": (
                "assignment = ConsumableAssignment.objects.get(pk=1)\nother = assignment\nother.restore()"
            ),
            "loop-target": "for assignment in AccessoryAssignment.objects.all():\n    assignment.save()",
            "queryset-alias-update": ("qs = AccessoryAssignment.objects.filter(pk=1)\nqs.update(qty=2)"),
            "queryset-alias-delete": ("qs = ComponentAllocation.objects.filter(pk=1)\nqs.delete()"),
            "queryset-alias-raw-delete": (
                "qs = ConsumableAssignment.objects.filter(pk=1)\nqs._raw_delete(using='default')"
            ),
            "manager-alias-create": "manager = AccessoryAssignment.objects\nmanager.create(qty=1)",
            "model-alias-create": "Alias = AccessoryAssignment\nAlias.objects.create(qty=1)",
        }
        for mutation, source in mutations.items():
            with self.subTest(mutation=mutation):
                self.assertTrue(_assignment_write_violations(source), mutation)

    def test_assignment_write_gate_ignores_reads_and_unrelated_models(self):
        allowed = {
            "read": "AccessoryAssignment.objects.filter(qty=1).first()",
            "unrelated-model": "AccessoryStock.objects.get_or_create(qty=1)",
            "unrelated-qualified": "import inventory.models\ninventory.models.AccessoryStock.objects.create(qty=1)",
            "similar-name": "AccessoryAssignmentReport.objects.create(qty=1)",
        }
        for name, source in allowed.items():
            with self.subTest(allowed=name):
                self.assertEqual(_assignment_write_violations(source), [], name)


class KitCheckoutBoundaryTests(SimpleTestCase):
    """Keep ``inventory.models`` independent of ``assets``."""

    def test_assets_services_imports_inventory_at_module_scope(self):
        self.assertFalse(
            _imports("assets.services", "inventory.models"),
            "kit stock families must stay behind inventory.services",
        )

    def test_inventory_models_does_not_import_assets(self):
        self.assertFalse(
            _imports("inventory.models", "assets"),
            "inventory.models must not import anything from assets, at module scope or deferred",
        )


class SnipeITImporterBoundaryTests(SimpleTestCase):
    """Freeze the inverted importer-to-inventory dependency."""

    IMPORTER = "core.importers.snipeit"
    COMMAND = "core.management.commands.import_snipeit"

    def test_importer_does_not_import_inventory_services(self):
        import core.importers.snipeit as pkg

        package_root = Path(pkg.__file__).parent
        paths = [Path(pkg.__file__)] + sorted(package_root.rglob("*.py"))
        for path in paths:
            relative = path.relative_to(package_root).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            module = ".".join(("core", "importers", "snipeit", *parts))
            with self.subTest(module=module, target="inventory.services"):
                self.assertFalse(
                    _imports(module, "inventory.services"),
                    f"{module} must receive inventory service callables through injection",
                )
            with self.subTest(module=module, target="assets.services"):
                # T01 native writers use the canonical specification commands,
                # DTO contracts, and issue-message renderer; stock/checkout
                # operations still belong to the injected root.
                specification_modules = (
                    "assets.services.specification_writers",
                    "assets.services.specifications._command_support",
                    "assets.services.specifications.commands",
                    "assets.services.specifications.contracts",
                    "assets.services.specifications.messages",
                )
                direct_service_edges = {
                    edge
                    for edge in _edges(module, top_level_only=False)
                    if (edge == "assets.services" or edge.startswith("assets.services."))
                    and not any(edge == name or edge.startswith(name + ".") for name in specification_modules)
                }
                self.assertFalse(
                    direct_service_edges,
                    f"{module} must receive the asset checkout service through injection",
                )

    def test_command_owns_the_inventory_services_edge(self):
        self.assertTrue(
            _imports(self.COMMAND, "inventory.services", top_level_only=True),
            f"{self.COMMAND} must remain the inventory-service composition root",
        )
        self.assertIn(
            "assets.services.checkout_asset",
            _edges(self.COMMAND, top_level_only=True),
            f"{self.COMMAND} must own the asset checkout-service composition-root edge",
        )
