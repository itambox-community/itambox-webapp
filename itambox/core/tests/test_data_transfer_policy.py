"""Contract: every installed model carries an explicit import and export declaration."""

from django.apps import apps
from django.test import SimpleTestCase

from core.data_transfer import (
    DECLARATIONS,
    DENIED,
    EXPORT_SCOPES,
    R_AUTHZ,
    R_CONFIG,
    R_DEDICATED,
    R_NO_SCOPE,
    R_PERSONAL,
    R_SECRET,
    R_SYSTEM,
    SCOPE_CONTAINER,
    SCOPE_GLOBAL,
    SCOPE_MANAGER,
    SCOPE_OWNER,
    policy_for,
    policy_for_label,
    required_export_permissions,
)
from core.importers.bulk_forms import get_registered_import_form, is_model_importable
from organization.services import is_container_scoped_unfiltered

# Lifecycle rows a domain service owns. A raw-field import could put them into
# states the service never produces, so they must never be importable.
SERVICE_OWNED = (
    "assets.assetassignment",
    "assets.assetdisposal",
    "assets.assetreservation",
    "compliance.custodyreceipt",
    "compliance.custodysigningsession",
    "inventory.accessoryassignment",
    "inventory.componentallocation",
    "inventory.consumableassignment",
    "licenses.licenseseatassignment",
    "subscriptions.subscriptionassignment",
)

# Authentication, RBAC, and generated or system data stay out of both directions
# of the generic importer.
SECURITY_SENSITIVE = (
    "organization.membership",
    "organization.role",
    "organization.rolegrant",
    "organization.rolegrantscope",
    "organization.tenant",
    "organization.tenantresourcegrant",
    "users.groupmembership",
    "users.token",
    "users.user",
    "users.usergroup",
)


class DataTransferPolicyContractTests(SimpleTestCase):
    def test_asset_maintenance_is_declared_with_a_curated_importer(self):
        model = apps.get_model("assets", "AssetMaintenance")
        form = get_registered_import_form(model)

        self.assertTrue(policy_for(model).import_)
        self.assertIsNotNone(form)
        self.assertEqual(form.update_key, ())

    def test_every_installed_model_has_an_explicit_declaration(self):
        labels = {model._meta.label_lower for model in apps.get_models()}
        undeclared = sorted(labels - set(DECLARATIONS))
        self.assertEqual(undeclared, [], "declare import and export for these models in core/data_transfer.py")

    def test_no_declaration_is_stale(self):
        labels = {model._meta.label_lower for model in apps.get_models()}
        self.assertEqual(sorted(set(DECLARATIONS) - labels), [])

    def test_undeclared_models_fail_closed(self):
        self.assertIs(policy_for_label("nowhere.ghost"), DENIED)
        self.assertIs(policy_for(None), DENIED)
        self.assertFalse(DENIED.import_)
        self.assertFalse(DENIED.export)

    def test_import_declaration_and_curated_form_agree_exactly(self):
        for model in apps.get_models():
            label = model._meta.label_lower
            with self.subTest(label=label):
                declared = policy_for(model).import_
                registered = get_registered_import_form(model) is not None
                self.assertEqual(declared, registered, f"{label}: import_ and @register_import_form must match")
                self.assertEqual(is_model_importable(model), declared)

    def test_service_owned_and_security_sensitive_models_are_never_importable(self):
        for label in SERVICE_OWNED + SECURITY_SENSITIVE:
            with self.subTest(label=label):
                self.assertFalse(policy_for_label(label).import_)
                self.assertFalse(is_model_importable(apps.get_model(label)))

    def test_every_curated_form_names_real_fields_and_a_declared_update_key(self):
        for model in apps.get_models():
            form = get_registered_import_form(model)
            if form is None:
                continue
            with self.subTest(label=model._meta.label_lower):
                for name in form.update_key:
                    field = model._meta.get_field(name)
                    self.assertFalse(field.primary_key, "an update key must be a natural key, never the pk")
                    columns = list(form.required_fields) + list(form.optional_fields)
                self.assertIn(name, columns, "an update key must be an importable column")


class ExportContractTests(SimpleTestCase):
    def test_exportable_models_declare_a_known_scope(self):
        for label, policy in DECLARATIONS.items():
            with self.subTest(label=label):
                if policy.export:
                    self.assertIn(policy.scope, EXPORT_SCOPES)
                else:
                    self.assertIsNone(policy.scope)
                    self.assertTrue(policy.reason, "a denied model records why")

    def test_manager_scope_models_use_a_tenant_scoped_default_manager(self):
        for model in apps.get_models():
            policy = policy_for(model)
            if policy.scope != SCOPE_MANAGER:
                continue
            with self.subTest(label=model._meta.label_lower):
                self.assertTrue(hasattr(model.objects, "filter_by_tenant"))

    def test_global_scope_models_are_tenantless(self):
        for model in apps.get_models():
            if policy_for(model).scope != SCOPE_GLOBAL:
                continue
            with self.subTest(label=model._meta.label_lower):
                self.assertNotIn("tenant", {f.name for f in model._meta.get_fields()})

    def test_container_scope_models_resolve_through_visible_to_containers(self):
        for model in apps.get_models():
            if policy_for(model).scope != SCOPE_CONTAINER:
                continue
            with self.subTest(label=model._meta.label_lower):
                self.assertTrue(is_container_scoped_unfiltered(model))

    def test_unscoped_container_models_are_never_exportable_without_container_scope(self):
        for model in apps.get_models():
            if is_container_scoped_unfiltered(model):
                with self.subTest(label=model._meta.label_lower):
                    self.assertIn(policy_for(model).scope, (None, SCOPE_CONTAINER))

    def test_owner_scope_names_a_real_user_foreign_key(self):
        for model in apps.get_models():
            policy = policy_for(model)
            if policy.scope != SCOPE_OWNER:
                continue
            with self.subTest(label=model._meta.label_lower):
                field = model._meta.get_field(policy.owner_field)
                self.assertEqual(field.related_model._meta.label_lower, "users.user")

    def test_secret_authorization_and_evidence_models_are_denied(self):
        for label in tuple(
            x for x in SECURITY_SENSITIVE if x not in ("organization.membership", "organization.tenant")
        ) + (
            "compliance.custodyreceipt",
            "compliance.custodysigningsession",
            "compliance.custodyhandoffdelivery",
            "extras.webhookendpoint",
            "core.emailsettings",
        ):
            with self.subTest(label=label):
                self.assertFalse(policy_for_label(label).export)
        self.assertTrue(policy_for_label("assets.asset").export)

    def test_dedicated_export_permission_is_required_when_declared(self):
        for model in apps.get_models():
            perms = required_export_permissions(model)
            meta = model._meta
            with self.subTest(label=meta.label_lower):
                self.assertEqual(perms[0], f"{meta.app_label}.view_{meta.model_name}")
                declared = any(code == f"export_{meta.model_name}" for code, _n in meta.permissions)
                self.assertEqual(len(perms) == 2, declared)


class ExportInventoryDetailTests(SimpleTestCase):
    """The inventory is the review artifact: every denial says why, every
    allowance declares a complete contract."""

    def test_every_denial_carries_a_known_reason_code(self):
        known = {R_AUTHZ, R_CONFIG, R_DEDICATED, R_NO_SCOPE, R_PERSONAL, R_SECRET, R_SYSTEM}
        for label, policy in DECLARATIONS.items():
            if policy.export:
                continue
            with self.subTest(label=label):
                self.assertTrue(
                    any(policy.reason.startswith(code + ":") for code in known),
                    f"{label}: reason must open with a known code, got {policy.reason!r}",
                )

    def test_every_allowance_declares_a_permission_that_exists(self):
        for model in apps.get_models():
            if not policy_for(model).export:
                continue
            meta = model._meta
            with self.subTest(label=meta.label_lower):
                perms = required_export_permissions(model)
                self.assertEqual(perms[0], f"{meta.app_label}.view_{meta.model_name}")
                declared = {code for code, _name in meta.permissions}
                for perm in perms:
                    codename = perm.split(".", 1)[1]
                    self.assertIn(codename, {"view_" + meta.model_name} | declared)

    def test_dedicated_export_surfaces_are_denied_or_not_weaker(self):
        """Where a model keeps a dedicated ``export_<model>`` permission, the
        generic gate demands it too — or the model is denied outright."""
        for model in apps.get_models():
            dedicated = any(code == f"export_{model._meta.model_name}" for code, _n in model._meta.permissions)
            if not dedicated:
                continue
            with self.subTest(label=model._meta.label_lower):
                policy = policy_for(model)
                perms = required_export_permissions(model)
                self.assertIn(f"{model._meta.app_label}.export_{model._meta.model_name}", perms)
                if not policy.export:
                    self.assertTrue(policy.reason, "a denied dedicated-export model records why")

    def test_named_same_pattern_models_are_denied(self):
        """The models issue #585 names as unsafe or ambiguous must be explicit
        DENY entries, never resolved by a fallback rule."""
        for label in (
            "compliance.assetaudit",
            "compliance.custodyreceipt",
            "compliance.custodyhandoffdelivery",
            "compliance.custodysigningsession",
            "core.job",
            "core.recyclebin",
            "extras.dashboard",
            "extras.bookmark",
            "extras.objectwatch",
            "extras.fileattachment",
            "extras.imageattachment",
            "users.usergroup",
        ):
            with self.subTest(label=label):
                policy = policy_for_label(label)
                if label in ("extras.bookmark", "extras.objectwatch"):
                    self.assertEqual(policy.scope, SCOPE_OWNER)
                else:
                    self.assertFalse(policy.export)
