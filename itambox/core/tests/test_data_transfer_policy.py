"""Contract: every installed model carries an explicit import and export declaration."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import translation

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
from organization.models import Contact, ContactAssignment, ContactRole, Region, Role, Site, SiteGroup, Tenant
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

    def test_site_is_declared_with_a_curated_importer(self):
        model = apps.get_model("organization", "Site")
        form = get_registered_import_form(model)

        self.assertTrue(policy_for(model).import_)
        if form is None:
            self.fail("Site must have a curated importer.")
        self.assertEqual(form.update_key, ("name",))
        self.assertEqual(form.required_fields, ["name"])
        self.assertEqual(
            form.optional_fields,
            [
                "slug",
                "status",
                "region",
                "group",
                "facility",
                "time_zone",
                "description",
                "physical_address",
                "shipping_address",
                "latitude",
                "longitude",
                "comments",
            ],
        )

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

    def test_reference_import_tenant_column_error_does_not_claim_tenant_assignment(self):
        labels = (
            "assets.category",
            "assets.assetrole",
            "organization.region",
            "organization.site",
            "organization.sitegroup",
            "organization.contactrole",
            "organization.contact",
        )
        for label in labels:
            for column in ("tenant", "tenant_id"):
                with self.subTest(label=label, column=column), translation.override("de"):
                    form_class = get_registered_import_form(apps.get_model(label))
                    if form_class is None:
                        self.fail(f"{label} must have a curated import form")
                    form = form_class(
                        data={
                            "active_tab": "editor",
                            "import_format": "csv",
                            "import_text": f"name,{column}\nExample,foreign-tenant",
                            "delimiter": ",",
                        }
                    )
                    self.assertFalse(form.is_valid())
                    errors = str(form.errors)
                    self.assertIn("Die Spalten „tenant“ und „tenant_id“ werden nicht unterstützt.", errors)
                    self.assertIn("Entfernen Sie sie vor dem Import.", errors)
                    self.assertNotIn("dem aktiven Mandanten zugeordnet", errors)

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


class SiteBulkImportFormInternalBranchTests(SimpleTestCase):
    def setUp(self):
        form_class = get_registered_import_form(Site)
        if form_class is None:
            raise AssertionError("Site must have a curated importer.")
        self.form = form_class()

    def test_validate_row_requires_an_active_tenant(self):
        with patch("assets.forms.import_forms.get_current_tenant", return_value=None):
            with self.assertRaisesMessage(ValidationError, "Select an active tenant before importing sites."):
                self.form._validate_row({}, row_number=2)

    def test_find_existing_requires_an_active_tenant(self):
        with (
            patch("assets.forms.import_forms.get_current_tenant", return_value=None),
            patch.object(Site.objects, "filter") as site_filter,
        ):
            with self.assertRaisesMessage(ValidationError, "Select an active tenant before importing sites."):
                self.form._find_existing({"name": "Example"})
        site_filter.assert_not_called()

    def test_find_existing_returns_none_when_the_update_key_is_missing(self):
        tenant = SimpleNamespace(pk=17)
        with (
            patch("assets.forms.import_forms.get_current_tenant", return_value=tenant),
            patch.object(Site.objects, "filter") as site_filter,
        ):
            self.assertIsNone(self.form._find_existing({}))
        site_filter.assert_not_called()

    def test_find_existing_rejects_multiple_update_key_matches(self):
        tenant = SimpleNamespace(pk=17)
        queryset = MagicMock()
        queryset.__getitem__.return_value = [object(), object()]
        with (
            patch("assets.forms.import_forms.get_current_tenant", return_value=tenant),
            patch.object(Site.objects, "filter", return_value=queryset) as site_filter,
        ):
            with self.assertRaisesMessage(ValidationError, "More than one existing object matches the update key."):
                self.form._find_existing({"name": "Duplicate"})
        site_filter.assert_called_once_with(tenant_id=tenant.pk, name="Duplicate")


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
        ("organization", "site"),
        ("organization", "sitegroup"),
        ("organization", "contactrole"),
        ("organization", "contact"),
    )

    def setUp(self):
        self._language_before_test = translation.get_language()
        translation.activate(settings.LANGUAGE_CODE)
        super().setUp()
        self.clear_tenant_context()
        self.setup_tenant_context(name="Reference Import Tenant", slug="reference-import-tenant")

    def tearDown(self):
        self.clear_tenant_context()
        translation.activate(self._language_before_test or settings.LANGUAGE_CODE)
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
            (
                "organization",
                "site",
                Site,
                ("name", "description"),
                ("Reference Import Site", "before"),
                ("Reference Import Site", "after"),
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
                    name=initial[0], **({"tenant": self.tenant} if model in (Contact, Site) else {})
                )
                created_value = getattr(created, updated_field)
                self.assertEqual(created_value, initial[fields.index(updated_field)])
                self.assertEqual(Job.objects.latest("created").result["imported"], 1)

                preview, response = self._import(
                    adder,
                    app_label,
                    model_name,
                    initial_csv,
                    headers={"accept-language": "de"},
                )
                self.assertEqual(preview.status_code, 200)
                self.assertTrue(
                    any(
                        "zeile 2:" in note.lower()
                        and "diese zeile wird übersprungen" in note.lower()
                        and "berechtigung zum aktualisieren" in note.lower()
                        and "administrator" in note.lower()
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

                preview, response = self._import(
                    editor,
                    app_label,
                    model_name,
                    changed_csv,
                    headers={"accept-language": "de"},
                )
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
                self.assertTrue(
                    any(
                        "zeile 2:" in note.lower()
                        and "datensatz existiert bereits" in note.lower()
                        and "unverändert" in note.lower()
                        and "übersprungen" in note.lower()
                        for note in preview.context["preview_notes"]
                    )
                )
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
                self.assertIn("Die Spalten „tenant“ und „tenant_id“ werden nicht unterstützt.", errors)
                self.assertIn("Entfernen Sie sie vor dem Import.", errors)
                self.assertNotIn("werden immer dem aktiven Mandanten zugeordnet", errors)
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

    def test_asset_role_boolean_help_is_localized(self):
        user = self._member("reference-asset-role-help", {"assets.add_assetrole"})
        self.client_login_to_tenant(user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "assetrole"})
        response = self.client.get(url, headers={"accept-language": "de"})

        self.assertEqual(response.status_code, 200)
        boolean_info = next(field for field in response.context["fields"] if field["name"] == "allows_components")
        self.assertIn("Geben Sie „true“ oder „false“ an.", boolean_info["description"])
        self.assertNotIn("Specify true or false", boolean_info["description"])

    def test_contact_duplicate_name_error_is_tenant_specific_and_actionable(self):
        Contact._base_manager.create(name="Duplicate contact", tenant=self.tenant)
        Contact._base_manager.create(name="Duplicate contact", tenant=self.tenant)
        user = self._member("tenant-contact-duplicate-importer", {"organization.add_contact"})
        preview, response = self._import(
            user,
            "organization",
            "contact",
            "name,description\nDuplicate contact,new description\n",
            headers={"accept-language": "de"},
        )

        self.assertEqual(preview.status_code, 200)
        self.assertIsNone(response)
        messages = " ".join(str(message) for message in preview.context["form"].errors["__all__"])
        self.assertIn("Zeile 2", messages)
        self.assertIn("Im aktiven Mandanten sind mehrere Kontakte mit diesem Namen vorhanden.", messages)
        self.assertIn("Benennen Sie die doppelten Kontakte vor dem Import um.", messages)

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
            headers={"accept-language": "de"},
        )

        self.assertEqual(response.status_code, 200)
        messages = " ".join(str(message) for message in response.context["form"].errors["__all__"])
        self.assertIn("Zeile 2", messages)
        self.assertIn("Kontaktrollen können nicht mit dieser Datei importiert werden", messages)
        self.assertIn("Spalte „role“", messages)
        self.assertIn("nach dem Kontaktimport zu", messages)
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
                self.assertIn("Übergeordnet", messages)
                self.assertNotIn("parent", messages.lower())
                self.assertIn(row.split(",", 1)[1], messages)
                self.assertIn("Prüfen Sie den Slug oder Namen", messages)
                self.assertIn("legen Sie den fehlenden Eintrag vor dem Import an", messages)
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
        self.assertIn("Übergeordnet", messages)
        self.assertNotIn("parent", messages.lower())
        self.assertIn("north", messages)
        self.assertIn("Zu „north“ wurden mehrere Einträge des Typs Standortgruppe gefunden.", messages)
        self.assertIn("eindeutigen Slug oder Namen", messages)
        self.assertFalse(SiteGroup.objects.filter(name="Child Site Group").exists())

    def test_reference_help_describes_the_natural_key_parent_contract(self):
        user = self._member("reference-parent-help", {"organization.add_region"})
        self.client_login_to_tenant(user, self.tenant)
        url = reverse("generic_import", kwargs={"app_label": "organization", "model_name": "region"})

        response = self.client.get(url, headers={"accept-language": "de"})

        self.assertEqual(response.status_code, 200)
        parent_info = next(field for field in response.context["fields"] if field["name"] == "parent")
        self.assertIn("Geben Sie den Slug oder den Namen des übergeordneten Eintrags an.", parent_info["description"])
        self.assertNotIn("ID", parent_info["description"])

    def test_site_import_creates_in_active_tenant_and_uses_site_references(self):
        user = self._member("site-import-positive", {"organization.add_site"})
        region = Region.objects.create(name="Site Import Region", slug="site-import-region")
        group = SiteGroup.objects.create(name="Site Import Group", slug="site-import-group")

        preview, response = self._import(
            user,
            "organization",
            "site",
            "name,status,region,group,facility,time_zone,description,physical_address,shipping_address,"
            "latitude,longitude,comments\n"
            "Imported Campus,planned,site-import-region,Site Import Group,Main Building,Europe/Berlin,"
            "Primary office,1 Example Street,2 Example Street,48.123456,9.123456,Import note\n",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.context["form"].errors)
        if response is None:
            raise AssertionError("A valid site import preview must allow confirmation.")
        self.assertEqual(response.status_code, 302)
        imported = Site._base_manager.get(name="Imported Campus", tenant=self.tenant)
        self.assertEqual(imported.slug, "imported-campus")
        self.assertEqual(imported.status, Site.STATUS_PLANNED)
        self.assertEqual(imported.region_id, region.pk)
        self.assertEqual(imported.group_id, group.pk)
        self.assertEqual(imported.facility, "Main Building")
        self.assertEqual(imported.time_zone, "Europe/Berlin")
        self.assertEqual(imported.description, "Primary office")
        self.assertEqual(imported.physical_address, "1 Example Street")
        self.assertEqual(imported.shipping_address, "2 Example Street")
        self.assertEqual(str(imported.latitude), "48.123456")
        self.assertEqual(str(imported.longitude), "9.123456")
        self.assertEqual(imported.comments, "Import note")

    def test_site_region_and_group_resolution_order_and_numeric_region_name(self):
        user = self._member("site-import-reference-order", {"organization.add_site"})

        for field, model, prefix in (
            ("region", Region, "site-region-order"),
            ("group", SiteGroup, "site-group-order"),
        ):
            with self.subTest(field=field):
                slug_exact_value = prefix + "-slug-exact"
                slug_exact = model.objects.create(name=prefix + " slug exact target", slug=slug_exact_value)
                model.objects.create(name=slug_exact_value, slug=prefix + "-name-exact-target")

                name_exact_value = prefix + "-NameExact"
                name_exact = model.objects.create(name=name_exact_value, slug=prefix + "-exact-name-target")
                model.objects.create(
                    name=prefix + " slug case competitor",
                    slug=name_exact_value.lower(),
                )

                slug_ci_value = prefix + "-SlugCI"
                slug_ci = model.objects.create(
                    name=prefix + " slug ci target",
                    slug=slug_ci_value.lower(),
                )
                model.objects.create(name=slug_ci_value.upper(), slug=prefix + "-name-ci-competitor")

                name_ci_value = prefix + "-NameCI"
                name_ci = model.objects.create(name=name_ci_value.upper(), slug=prefix + "-name-ci-target")

                for suffix, value, expected in (
                    ("slug-exact", slug_exact_value, slug_exact),
                    ("name-exact", name_exact_value, name_exact),
                    ("slug-ci", slug_ci_value, slug_ci),
                    ("name-ci", name_ci_value, name_ci),
                ):
                    site_name = prefix + " imported " + suffix
                    preview, response = self._import(
                        user,
                        "organization",
                        "site",
                        "name," + field + "\n" + site_name + "," + value + "\n",
                    )
                    self.assertEqual(preview.status_code, 200)
                    self.assertFalse(preview.context["form"].errors)
                    if response is None:
                        raise AssertionError("A valid site import preview must allow confirmation.")
                    imported = Site._base_manager.get(name=site_name, tenant=self.tenant)
                    self.assertEqual(getattr(imported, field + "_id"), expected.pk)

        pk_target = Region.objects.create(name="Numeric region pk target", slug="numeric-region-pk-target")
        numeric_name = Region.objects.create(name=str(pk_target.pk), slug="numeric-region-name")
        site_name = "Site with numeric region name"
        preview, response = self._import(
            user,
            "organization",
            "site",
            "name,region\n" + site_name + "," + str(pk_target.pk) + "\n",
        )
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.context["form"].errors)
        if response is None:
            raise AssertionError("A valid site import preview must allow confirmation.")
        imported = Site._base_manager.get(name=site_name, tenant=self.tenant)
        self.assertEqual(imported.region_id, numeric_name.pk)
        self.assertNotEqual(imported.region_id, pk_target.pk)

    def test_unknown_site_region_and_group_fail_per_row_without_creating_references(self):
        user = self._member("site-import-unknown-references", {"organization.add_site"})
        for field, model, value in (
            ("region", Region, "Missing Site Region"),
            ("group", SiteGroup, "Missing Site Group"),
        ):
            with self.subTest(field=field):
                site_name = "Site with missing " + field
                preview, response = self._import(
                    user,
                    "organization",
                    "site",
                    "name," + field + "\n" + site_name + "," + value + "\n",
                )
                self.assertEqual(preview.status_code, 200)
                self.assertIsNone(response)
                messages = " ".join(str(message) for message in preview.context["form"].errors["__all__"])
                self.assertIn("Row 2", messages)
                self.assertIn(field + ": unknown", messages)
                self.assertIn(value, messages)
                self.assertIn("never created by a site import", messages)
                self.assertFalse(Site._base_manager.filter(name=site_name).exists())
                self.assertFalse(model._base_manager.filter(name=value).exists())

    def test_site_parent_column_fails_the_row_with_a_readable_message(self):
        user = self._member("site-import-parent-rejected", {"organization.add_site"})
        site_name = "Site parent must be rejected"
        preview, response = self._import(
            user,
            "organization",
            "site",
            "name,parent\n" + site_name + ",not-a-parent\n",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertIsNone(response)
        messages = " ".join(str(message) for message in preview.context["form"].errors["__all__"])
        self.assertIn("Row 2", messages)
        self.assertIn("parent:", messages)
        self.assertIn("Sites have no parent site", messages)
        self.assertIn("use region or group", messages)
        self.assertIn("locations", messages)
        self.assertFalse(Site._base_manager.filter(name=site_name).exists())

    def test_ambiguous_site_region_and_group_fail_per_row(self):
        user = self._member("site-import-ambiguous-references", {"organization.add_site"})
        value = "SiteReferenceAmbiguous"
        for field, model in (("region", Region), ("group", SiteGroup)):
            with self.subTest(field=field):
                model.objects.create(name=value.lower(), slug=field + "-ambiguous-one")
                model.objects.create(name=value.upper(), slug=field + "-ambiguous-two")
                site_name = "Site with ambiguous " + field
                preview, response = self._import(
                    user,
                    "organization",
                    "site",
                    "name," + field + "\n" + site_name + "," + value + "\n",
                )

                self.assertEqual(preview.status_code, 200)
                self.assertIsNone(response)
                messages = " ".join(str(message) for message in preview.context["form"].errors["__all__"])
                self.assertIn("Row 2", messages)
                self.assertIn(field + ":", messages)
                self.assertIn("matches more than one", messages)
                self.assertIn("unique Slug or Name", messages)
                self.assertFalse(Site._base_manager.filter(name=site_name).exists())

    def test_site_name_collision_in_another_tenant_fails_without_disclosing_tenant(self):
        foreign_tenant = Tenant._base_manager.create(name="Private Site Tenant", slug="private-site-tenant")
        foreign_site = Site._base_manager.create(
            name="Shared Site Name Collision",
            slug="shared-site-name-collision",
            tenant=foreign_tenant,
            description="Keep unchanged",
        )
        user = self._member("site-import-name-collision", {"organization.add_site"})
        preview, response = self._import(
            user,
            "organization",
            "site",
            "name,description\nShared Site Name Collision,attempted overwrite\n",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertIsNone(response)
        messages = " ".join(str(message) for message in preview.context["form"].errors["__all__"])
        self.assertIn("Row 2", messages)
        self.assertIn("name: a site with this name already exists", messages)
        self.assertNotIn(foreign_tenant.name, messages)
        self.assertNotIn(foreign_tenant.name, preview.content.decode())
        self.assertFalse(Site._base_manager.filter(name=foreign_site.name, tenant=self.tenant).exists())
        foreign_site.refresh_from_db()
        self.assertEqual(foreign_site.description, "Keep unchanged")

    def test_site_import_ignores_export_id(self):
        id_target = Site._base_manager.create(name="Site id target", slug="site-id-target", tenant=self.tenant)
        user = self._member("site-import-ignored-id", {"organization.add_site"})
        site_name = "Site id column ignored"
        preview, response = self._import(
            user,
            "organization",
            "site",
            "name,id\n" + site_name + "," + str(id_target.pk) + "\n",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.context["form"].errors)
        if response is None:
            raise AssertionError("A valid site import preview must allow confirmation.")
        self.assertEqual(response.status_code, 302)
        imported = Site._base_manager.get(name=site_name, tenant=self.tenant)
        self.assertNotEqual(imported.pk, id_target.pk)


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
