"""Contract: every installed model carries an explicit import and export declaration."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from assets.models import AssetRole, Category
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
from core.models import Job
from core.tests.mixins import TenantTestMixin
from organization.models import Contact, ContactAssignment, ContactRole, Region, Role, SiteGroup, Tenant
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


class ReferenceDataImportRouteTests(TenantTestMixin, TestCase):
    REFERENCE_ROUTES = (
        ("assets", "category"),
        ("assets", "assetrole"),
        ("organization", "region"),
        ("organization", "sitegroup"),
        ("organization", "contactrole"),
        ("organization", "contact"),
    )

    def setUp(self):
        super().setUp()
        self.clear_tenant_context()
        self.setup_tenant_context(name="Reference Import Tenant", slug="reference-import-tenant")

    def tearDown(self):
        self.clear_tenant_context()
        super().tearDown()

    def _member(self, username, permissions):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="password",
        )
        role = Role.objects.create(
            tenant=self.tenant,
            name=f"{username} role",
            permissions=sorted(permissions),
        )
        self.grant(user, self.tenant, role)
        return user

    def _import(self, user, app_label, model_name, csv_text, *, headers=None):
        self.client_login_to_tenant(user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": app_label, "model_name": model_name})
        preview = self.client.post(
            url,
            {
                "import_text": csv_text,
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "_preview": "1",
            },
            headers=headers,
        )
        if preview.context["form"].errors:
            return preview, None
        confirmed = self.client.post(
            url,
            {"active_tab": "editor", "_confirm": "1"},
            headers=headers,
        )
        return preview, confirmed

    def test_each_admitted_reference_form_creates_skips_without_change_and_updates_with_change(self):
        models = (
            (
                "assets",
                "category",
                Category,
                ("name", "slug"),
                ("Ref Category", "ref-category"),
                ("Ref Category", "ref-category-v2"),
                "slug",
            ),
            (
                "assets",
                "assetrole",
                AssetRole,
                ("name", "description"),
                ("Ref Role", "before"),
                ("Ref Role", "after"),
                "description",
            ),
            (
                "organization",
                "region",
                Region,
                ("name", "description"),
                ("Ref Region", "before"),
                ("Ref Region", "after"),
                "description",
            ),
            (
                "organization",
                "sitegroup",
                SiteGroup,
                ("name", "description"),
                ("Ref Site Group", "before"),
                ("Ref Site Group", "after"),
                "description",
            ),
            (
                "organization",
                "contactrole",
                ContactRole,
                ("name", "description"),
                ("Ref Contact Role", "before"),
                ("Ref Contact Role", "after"),
                "description",
            ),
            (
                "organization",
                "contact",
                Contact,
                ("name", "description"),
                ("Ref Contact", "before"),
                ("Ref Contact", "after"),
                "description",
            ),
        )
        add_permissions = {f"{app}.add_{model}" for app, model, *_ in models}
        change_permissions = {f"{app}.change_{model}" for app, model, *_ in models}
        adder = self._member("reference-data-adder", add_permissions)
        editor = self._member("reference-data-editor", add_permissions | change_permissions)

        for app_label, model_name, model, fields, initial, changed, updated_field in models:
            with self.subTest(model=f"{app_label}.{model_name}"):
                header = ",".join(fields)
                initial_csv = f"{header}\n{','.join(initial)}\n"
                changed_csv = f"{header}\n{','.join(changed)}\n"

                preview, response = self._import(adder, app_label, model_name, initial_csv)
                self.assertEqual(preview.status_code, 200)
                if response is None:
                    self.fail("A valid import preview must allow confirmation.")
                self.assertEqual(response.status_code, 302)
                created = model._base_manager.get(
                    name=initial[0], **({"tenant": self.tenant} if model is Contact else {})
                )
                created_value = getattr(created, updated_field)
                self.assertEqual(created_value, initial[fields.index(updated_field)])
                self.assertEqual(Job.objects.latest("created").result["imported"], 1)

                preview, response = self._import(adder, app_label, model_name, initial_csv)
                self.assertEqual(preview.status_code, 200)
                self.assertTrue(
                    any(
                        "row 2:" in note.lower()
                        and "you do not have permission to update existing objects" in note.lower()
                        for note in preview.context["preview_notes"]
                    )
                )
                if response is None:
                    self.fail("A valid import preview must allow confirmation.")
                self.assertEqual(response.status_code, 302)
                skipped_job = Job.objects.latest("created")
                self.assertEqual(
                    skipped_job.result,
                    {"imported": 0, "failed": 0, "total": 1, "skipped": 1},
                    skipped_job.logs,
                )
                created.refresh_from_db()
                self.assertEqual(getattr(created, updated_field), created_value)

                preview, response = self._import(editor, app_label, model_name, changed_csv)
                self.assertEqual(preview.status_code, 200)
                if response is None:
                    self.fail("A valid import preview must allow confirmation.")
                self.assertEqual(response.status_code, 302)
                updated_job = Job.objects.latest("created")
                self.assertEqual(updated_job.result["updated"], 1, updated_job.logs)
                created.refresh_from_db()
                self.assertEqual(getattr(created, updated_field), changed[fields.index(updated_field)])

                preview, response = self._import(editor, app_label, model_name, changed_csv)
                self.assertEqual(preview.status_code, 200)
                if response is None:
                    raise AssertionError("A valid unchanged re-import must allow confirmation.")
                self.assertEqual(response.status_code, 302)
                unchanged_job = Job.objects.latest("created")
                self.assertEqual(
                    unchanged_job.result,
                    {"imported": 0, "failed": 0, "total": 1, "skipped": 1},
                    unchanged_job.logs,
                )
                self.assertTrue(any("unchanged" in note.lower() for note in preview.context["preview_notes"]))
                created.refresh_from_db()
                self.assertEqual(getattr(created, updated_field), changed[fields.index(updated_field)])

    def test_all_reference_import_routes_are_denied_in_all_accessible_scope(self):
        permissions = {f"{app_label}.add_{model_name}" for app_label, model_name in self.REFERENCE_ROUTES}
        user = self._member("reference-all-accessible-importer", permissions)
        self.client_login_to_tenant(user, self.tenant)

        for app_label, model_name in self.REFERENCE_ROUTES:
            with self.subTest(model=f"{app_label}.{model_name}"):
                url = reverse("generic_import", kwargs={"app_label": app_label, "model_name": model_name})
                response = self.client.get(f"{url}?switch_all_accessible=1")
                self.assertEqual(response.status_code, 403)

    def test_tenant_column_is_rejected_for_the_whole_file_on_each_reference_route(self):
        permissions = {f"{app_label}.add_{model_name}" for app_label, model_name in self.REFERENCE_ROUTES}
        user = self._member("reference-tenant-column-importer", permissions)

        for app_label, model_name in self.REFERENCE_ROUTES:
            with self.subTest(model=f"{app_label}.{model_name}"):
                model = apps.get_model(app_label, model_name)
                before = model._base_manager.filter(name="Tenant Injection").count()
                preview, response = self._import(
                    user,
                    app_label,
                    model_name,
                    "name,tenant\nTenant Injection,foreign-tenant\n",
                    headers={"accept-language": "de"},
                )
                self.assertEqual(preview.status_code, 200)
                self.assertIsNone(response)
                errors = str(preview.context["form"].errors)
                self.assertIn("Entfernen Sie die Mandantenspalte", errors)
                self.assertNotIn("foreign-tenant", errors)
                self.assertNotIn("Row 2", errors)
                self.assertEqual(model._base_manager.filter(name="Tenant Injection").count(), before)

    def test_contact_rows_land_in_active_tenant_and_never_update_shared_contacts(self):
        global_contact = Contact._base_manager.create(name="Shared contact", description="global copy")
        foreign_tenant = Tenant._base_manager.create(name="Foreign Contact Tenant", slug="foreign-contact-tenant")
        foreign_contact = Contact._base_manager.create(
            name="Shared contact", tenant=foreign_tenant, description="foreign copy"
        )
        user = self._member("tenant-contact-importer", {"organization.add_contact"})

        preview, response = self._import(
            user,
            "organization",
            "contact",
            "name,description\nShared contact,tenant copy\n",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.context["form"].errors)
        self.assertNotIn("foreign copy", preview.content.decode())
        if response is None:
            self.fail("A valid import preview must allow confirmation.")
        self.assertEqual(response.status_code, 302)
        imported = Contact._base_manager.get(name="Shared contact", tenant=self.tenant)
        global_contact.refresh_from_db()
        foreign_contact.refresh_from_db()
        self.assertEqual(imported.description, "tenant copy")
        self.assertEqual(global_contact.description, "global copy")
        self.assertEqual(foreign_contact.description, "foreign copy")

    def test_contact_role_column_is_rejected_per_row_in_preview(self):
        user = self._member("contact-role-importer", {"organization.add_contact"})
        url = reverse("generic_import", kwargs={"app_label": "organization", "model_name": "contact"})
        before_contacts = Contact._base_manager.count()
        before_assignments = ContactAssignment._base_manager.count()
        self.client_login_to_tenant(user, self.tenant)

        response = self.client.post(
            url,
            {
                "import_text": "name,role\nContact with rejected role,No such role\n",
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "_preview": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        messages = " ".join(str(message) for message in response.context["form"].errors["__all__"])
        self.assertIn("Row 2", messages)
        self.assertIn("role assignments are not importable", messages)
        self.assertEqual(Contact._base_manager.count(), before_contacts)
        self.assertEqual(ContactAssignment._base_manager.count(), before_assignments)

    def test_region_and_site_group_unknown_parents_fail_readably_per_row_in_preview(self):
        user = self._member(
            "reference-data-parent-importer",
            {"organization.add_region", "organization.add_sitegroup"},
        )
        self.client_login_to_tenant(user, self.tenant)

        for model_name, model, row in (
            ("region", Region, "Child Region,missing-region"),
            ("sitegroup", SiteGroup, "Child Site Group,missing-site-group"),
        ):
            with self.subTest(model=model_name):
                url = reverse("generic_import", kwargs={"app_label": "organization", "model_name": model_name})
                response = self.client.post(
                    url,
                    {
                        "import_text": f"name,parent\n{row}\n",
                        "active_tab": "editor",
                        "import_format": "csv",
                        "delimiter": ",",
                        "_preview": "1",
                    },
                    headers={"accept-language": "de"},
                )
                self.assertEqual(response.status_code, 200)
                messages = " ".join(str(message) for message in response.context["form"].errors["__all__"])
                self.assertIn("Zeile 2", messages)
                self.assertNotIn("Row 2", messages)
                self.assertIn("parent", messages)
                self.assertIn(row.split(",", 1)[1], messages)
                self.assertFalse(model.objects.filter(name=row.split(",", 1)[0]).exists())
                self.assertFalse(model.objects.filter(name=row.split(",", 1)[1]).exists())

    def test_region_parent_reference_uses_name_before_a_numeric_primary_key(self):
        user = self._member("reference-numeric-parent-importer", {"organization.add_region"})
        primary_key_target = Region.objects.create(name="PK target", slug="pk-target")
        name_target = Region.objects.create(name=str(primary_key_target.pk), slug="numeric-name-target")

        preview, response = self._import(
            user,
            "organization",
            "region",
            f"name,parent\nChild Region,{primary_key_target.pk}\n",
        )

        if response is None:
            self.fail(f"A valid natural-key parent reference must be importable: {preview.context['form'].errors}")
        created = Region.objects.get(name="Child Region")
        self.assertEqual(created.parent_id, name_target.pk)

    def test_site_group_parent_ambiguity_fails_readably_per_row(self):
        user = self._member("reference-ambiguous-parent-importer", {"organization.add_sitegroup"})
        SiteGroup.objects.create(name="North", slug="north-one")
        SiteGroup.objects.create(name="NORTH", slug="north-two")

        preview, response = self._import(
            user,
            "organization",
            "sitegroup",
            "name,parent\nChild Site Group,north\n",
            headers={"accept-language": "de"},
        )

        self.assertIsNone(response)
        messages = " ".join(str(message) for message in preview.context["form"].errors["__all__"])
        self.assertIn("Zeile 2", messages)
        self.assertNotIn("Row 2", messages)
        self.assertIn("parent", messages)
        self.assertIn("north", messages)
        self.assertIn("mehrere Einträge des Typs Standortgruppe stimmen überein", messages)
        self.assertFalse(SiteGroup.objects.filter(name="Child Site Group").exists())

    def test_reference_help_describes_the_natural_key_parent_contract(self):
        user = self._member("reference-parent-help", {"organization.add_region"})
        self.client_login_to_tenant(user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": "organization", "model_name": "region"})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        parent_info = next(field for field in response.context["fields"] if field["name"] == "parent")
        self.assertIn("Slug or Name", parent_info["description"])
        self.assertNotIn("ID", parent_info["description"])

    def test_site_import_remains_closed_in_this_slice(self):
        user = self._member("site-import-boundary", {"organization.add_site"})
        self.client_login_to_tenant(user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": "organization", "model_name": "site"})

        self.assertEqual(self.client.get(url).status_code, 404)


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
