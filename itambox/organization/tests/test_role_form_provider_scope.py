"""Regression coverage for the post-collapse role-sharing model (RBAC Stage-2).

Pre-collapse, ``RoleForm`` derived a per-role provider/tenant "scope"
(``Role.scope``) and hid a "capability" checkbox strip (``cap_manage_staff`` and
friends) behind that scope — ``RoleForm.is_provider_scoped`` decided which one to
render. All of that is gone (RBAC_STAGE2_SPEC.md §1, §6): the ``organization.
Provider`` model is deleted, the capability vocabulary (``manage_staff`` /
``manage_provider`` / ``manage_groups`` / ``manage_tenants``) is deleted with it,
and every ``Role`` now belongs to exactly one owning ``tenant`` (NOT NULL) whose
permission set is the single 'app.codename' vocabulary used everywhere else. A
managing (``is_provider``) tenant may additionally *share* one of its roles down
to the tenants it manages via ``Role.shared_with_managed`` — a live shared
definition, not a clone and not a capability grant.

This module now covers the successor invariants:

  (a) the deleted vocabulary really is gone — no ``PROVIDER_CAPABILITIES``, no
      ``RoleForm.is_provider_scoped``, no ``cap_*`` fields ever appear on
      ``RoleForm`` (plain tenant or managing tenant alike), and the matrix /
      custom-permission tables the form builds its checkboxes from never
      reoffer a ``manage_*`` capability codename under another name.
  (b) role pickers inside a managed tenant offer the union of the tenant's own
      roles and the roles its managing tenant shares down (``MembershipForm``'s
      ``roles`` queryset — organization/forms/membership_form.py), and exclude
      the managing tenant's *unshared* roles and any unrelated tenant's roles.
      Sharing is one-directional: the managing tenant's own picker never pulls
      in a managed tenant's locally-owned role.
  (c) a role shared down from the managing tenant is visible (read-only) but not
      editable from a managed tenant: ``RoleDetailView`` reports
      ``role_editable=False`` for it while a locally-owned role reports
      ``True``, and ``RoleEditView`` 404s on the shared role because its
      tenant-scoped queryset never resolves a foreign-tenant row — even for an
      actor who holds ``organization.change_role`` inside the managed tenant.
"""

from importlib import import_module

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from core.tests.mixins import TenantTestMixin
from organization import models as organization_models
from organization.forms import RoleForm
from organization.forms import role_form as role_form_module
from organization.forms.membership_form import MembershipForm
from organization.models import Role, Tenant

# Import the view modules at collection time (no tenant context active) so their
# `queryset = Model.objects.all()` class attributes (RoleEditView) bake UNSCOPED.
# Otherwise the first reverse()/resolve() of the process — possibly triggered by
# an unrelated test running under a tenant context — would import these views
# with that tenant active and freeze the queryset to the wrong tenant, causing
# order-dependent 404s here. Harmless in production (URLconf loads at startup
# with no tenant). See memory: import-baked-view-querysets-tests.
import_module("organization.views")

User = get_user_model()


# --------------------------------------------------------------------------------------------- #
# (a) The capability-strip vocabulary has no successor and offers nothing back.
# --------------------------------------------------------------------------------------------- #
class DeletedProviderCapabilityVocabularyTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.clear_tenant_context()
        self.setup_tenant_context()  # self.tenant (plain), self.tenant_admin (superuser)
        self.superuser = self.tenant_admin
        self.provider_tenant = Tenant.objects.create(
            name="Northwind MSP",
            slug="northwind-msp-caps",
            is_provider=True,
        )

    def tearDown(self):
        self.clear_tenant_context()

    def test_provider_capabilities_symbol_does_not_exist(self):
        self.assertFalse(hasattr(role_form_module, "PROVIDER_CAPABILITIES"))

    def test_roleform_has_no_is_provider_scoped_property(self):
        self.assertFalse(hasattr(RoleForm, "is_provider_scoped"))

    def test_roleform_never_offers_a_cap_field_for_a_plain_tenant(self):
        form = RoleForm(user=self.superuser, tenant=self.tenant)
        cap_fields = [name for name in form.fields if name.startswith("cap_")]
        self.assertEqual(cap_fields, [])

    def test_roleform_never_offers_a_cap_field_for_a_managing_tenant(self):
        # A managing tenant only ever gets the ``shared_with_managed`` checkbox —
        # no capability strip resurfaces just because the owner ``is_provider``.
        form = RoleForm(user=self.superuser, tenant=self.provider_tenant)
        cap_fields = [name for name in form.fields if name.startswith("cap_")]
        self.assertEqual(cap_fields, [])
        self.assertIn("shared_with_managed", form.fields)

    def test_shared_with_managed_absent_for_a_plain_tenant(self):
        form = RoleForm(user=self.superuser, tenant=self.tenant)
        self.assertNotIn("shared_with_managed", form.fields)

    def test_matrix_and_custom_permissions_offer_only_current_management_capabilities(self):
        """The MATRIX_MODELS / CUSTOM_PERMISSIONS tables the form builds its
        checkboxes from must never reoffer a deleted ``manage_*`` capability
        codename (``manage_staff``, ``manage_provider``, ``manage_groups``,
        ``manage_tenants``) under any other name."""
        offered_codenames = set()
        for info in role_form_module.MATRIX_MODELS.values():
            app, model = info["app"], info["model_name"]
            offered_codenames.update(
                {
                    f"{app}.view_{model}",
                    f"{app}.add_{model}",
                    f"{app}.change_{model}",
                    f"{app}.delete_{model}",
                }
            )
        offered_codenames.update(full for _field_key, _label, full in role_form_module.get_custom_permissions())
        manage_star = {c for c in offered_codenames if c.split(".", 1)[-1].startswith("manage_")}
        self.assertEqual(manage_star, set())

    def test_role_model_has_no_scope_or_provider_field(self):
        field_names = {f.name for f in Role._meta.get_fields()}
        self.assertNotIn("scope", field_names)
        self.assertNotIn("provider", field_names)
        self.assertIn("tenant", field_names)
        self.assertIn("shared_with_managed", field_names)


class RoleFormCustomPermissionExposureTests(TenantTestMixin, TestCase):
    """Custom ``Meta.permissions`` must surface in the role editor (regression #296)."""

    def setUp(self):
        self.clear_tenant_context()
        self.setup_tenant_context(name="Custom Perm Tenant", slug="custom-perm-tenant")

    def test_prepare_and_export_custody_receipt_permissions_are_offered(self):
        # Regression for #296: prepare/export custody receipts (added via
        # Meta.permissions in issue #259) were missing from the role editor's
        # hard-coded custom-permission list and could never be granted via UI.
        form = RoleForm(user=self.tenant_admin, tenant=self.tenant)
        offered = {full for _field_key, _label, full in form._custom_permissions}
        self.assertIn("compliance.prepare_custodyreceipt", offered)
        self.assertIn("compliance.export_custodyreceipt", offered)
        self.assertIn("perm_compliance_prepare_custodyreceipt", form.fields)
        self.assertIn("perm_compliance_export_custodyreceipt", form.fields)

    def test_role_editor_covers_every_non_crud_permission(self):
        # Every live permission except a model's actual default CRUD codenames
        # must be offered, including custom permissions that happen to start
        # with a CRUD action (for example core.view_plugin_diagnostics).
        actions = ("view", "add", "change", "delete")
        permissions = Permission.objects.select_related("content_type")
        expected = {
            f"{permission.content_type.app_label}.{permission.codename}"
            for permission in permissions
            if permission.codename not in {f"{action}_{permission.content_type.model}" for action in actions}
        }
        offered = {full for _field_key, _label, full in role_form_module.get_custom_permissions()}
        self.assertEqual(offered, expected)
        self.assertIn("core.view_plugin_diagnostics", offered)

    def test_custom_permission_grant_persists_full_codename(self):
        # Granting a custom permission through the form stores the full
        # "app.codename" in Role.permissions (prepare custody receipts).
        form = RoleForm(
            user=self.tenant_admin,
            tenant=self.tenant,
            data={
                "name": "Custody Operator",
                "description": "",
                "perm_compliance_prepare_custodyreceipt": "on",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        role = form.save()
        self.assertIn("compliance.prepare_custodyreceipt", role.permissions)
        # An unselected custom permission stays unset.
        self.assertNotIn("compliance.export_custodyreceipt", role.permissions)

    def test_organization_models_has_no_provider_model(self):
        self.assertFalse(hasattr(organization_models, "Provider"))


# --------------------------------------------------------------------------------------------- #
# (b) Role pickers inside a managed tenant offer own ∪ shared-in roles.
# --------------------------------------------------------------------------------------------- #
class ManagedTenantRolePickerTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.clear_tenant_context()
        self.setup_tenant_context(name="Unrelated Co", slug="unrelated-co-picker")
        self.superuser = self.tenant_admin
        # self.tenant / self.tenant_role (from the mixin) double as the "unrelated
        # tenant" fixture: neither manages nor is managed by anything below.

        self.provider_tenant = Tenant.objects.create(
            name="Northwind MSP",
            slug="northwind-msp-picker",
            is_provider=True,
        )
        self.managed_tenant = Tenant.objects.create(
            name="Acme Customer",
            slug="acme-customer-picker",
            managed_by=self.provider_tenant,
        )
        self.local_role = Role.objects.create(
            tenant=self.managed_tenant,
            name="Local Ops",
            permissions=[],
        )
        self.shared_role = Role.objects.create(
            tenant=self.provider_tenant,
            name="MSP Technician",
            shared_with_managed=True,
            permissions=[],
        )
        self.private_role = Role.objects.create(
            tenant=self.provider_tenant,
            name="MSP Internal Admin",
            shared_with_managed=False,
            permissions=[],
        )

    def tearDown(self):
        self.clear_tenant_context()

    def test_role_picker_offers_own_plus_shared_in_roles(self):
        form = MembershipForm(user=self.superuser, tenant=self.managed_tenant)
        offered = set(form.fields["own_roles"].queryset)
        self.assertIn(self.local_role, offered)
        self.assertIn(self.shared_role, offered)

    def test_role_picker_excludes_managing_tenants_unshared_role(self):
        form = MembershipForm(user=self.superuser, tenant=self.managed_tenant)
        offered = set(form.fields["own_roles"].queryset)
        self.assertNotIn(self.private_role, offered)

    def test_role_picker_excludes_unrelated_tenants_role(self):
        form = MembershipForm(user=self.superuser, tenant=self.managed_tenant)
        offered = set(form.fields["own_roles"].queryset)
        self.assertNotIn(self.tenant_role, offered)

    def test_managing_tenants_own_picker_never_offers_a_managed_tenants_local_role(self):
        """Sharing is one-directional: the managing tenant's own picker never
        pulls in a managed tenant's locally-owned role."""
        form = MembershipForm(user=self.superuser, tenant=self.provider_tenant)
        offered = set(form.fields["own_roles"].queryset)
        self.assertNotIn(self.local_role, offered)
        self.assertIn(self.shared_role, offered)
        self.assertIn(self.private_role, offered)

    def test_a_standalone_tenants_picker_offers_only_its_own_roles(self):
        """A tenant with no ``managed_by`` sees only its own roles — no managing
        tenant to inherit shared definitions from."""
        form = MembershipForm(user=self.superuser, tenant=self.tenant)
        offered = set(form.fields["own_roles"].queryset)
        self.assertEqual(offered, {self.tenant_role})


# --------------------------------------------------------------------------------------------- #
# (c) A role shared down is visible but not editable from the managed tenant.
# --------------------------------------------------------------------------------------------- #
class SharedRoleNotEditableFromManagedTenantTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.clear_tenant_context()
        self.provider_tenant = Tenant.objects.create(
            name="Northwind MSP",
            slug="northwind-msp-edit",
            is_provider=True,
        )
        self.managed_tenant = Tenant.objects.create(
            name="Acme Customer",
            slug="acme-customer-edit",
            managed_by=self.provider_tenant,
        )
        self.local_role = Role.objects.create(
            tenant=self.managed_tenant,
            name="Local Ops Edit",
            permissions=[],
        )
        self.shared_role = Role.objects.create(
            tenant=self.provider_tenant,
            name="MSP Technician Edit",
            shared_with_managed=True,
            permissions=[],
        )
        self.superuser = User.objects.create_superuser(
            username="su_shared_role_edit",
            email="su_shared_role_edit@example.com",
            password="pw",
        )
        self.client.force_login(self.superuser)

    def tearDown(self):
        self.clear_tenant_context()

    def _url(self, name, pk, tenant):
        # Drive the request's active tenant deterministically through the
        # middleware's ?switch_tenant= param rather than relying on the test
        # client's session persistence (fragile across this suite's test
        # ordering — see the import-baked-view-querysets-tests memory note).
        return f"{reverse(name, kwargs={'pk': pk})}?switch_tenant={tenant.pk}"

    def test_local_role_detail_reports_editable(self):
        resp = self.client.get(self._url("organization:role_detail", self.local_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["role_editable"])

    def test_shared_role_detail_is_visible_but_reports_not_editable(self):
        resp = self.client.get(self._url("organization:role_detail", self.shared_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["role_editable"])

    def test_managed_tenant_role_list_contains_only_locally_owned_roles(self):
        url = reverse("organization:role_list") + f"?switch_tenant={self.managed_tenant.pk}"

        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        listed_ids = set(resp.context["table"].data.data.values_list("pk", flat=True))
        self.assertIn(self.local_role.pk, listed_ids)
        self.assertNotIn(self.shared_role.pk, listed_ids)

    def test_local_role_edit_view_reachable_from_its_own_tenant(self):
        resp = self.client.get(self._url("organization:role_update", self.local_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)

    def test_shared_role_edit_view_404s_from_the_managed_tenant(self):
        resp = self.client.get(self._url("organization:role_update", self.shared_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 404)

    def test_shared_role_edit_view_404s_even_for_an_actor_who_holds_change_role_locally(self):
        """Holding ``organization.change_role`` inside the managed tenant is not
        enough — ``RoleEditView``'s tenant-scoped queryset never resolves a
        foreign-tenant row, so only the owning (managing) tenant's own admins can
        reach the edit form for a shared role."""
        actor = User.objects.create_user(
            username="managed_admin_edit",
            email="managed_admin_edit@example.com",
            password="pw",
        )
        admin_role = Role.objects.create(
            tenant=self.managed_tenant,
            name="Local Admin Edit",
            permissions=["organization.view_role", "organization.change_role"],
        )
        self.grant(actor, self.managed_tenant, admin_role)
        self.client.force_login(actor)

        resp = self.client.get(self._url("organization:role_update", self.shared_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 404)

    def test_shared_role_is_editable_from_its_own_owning_tenant(self):
        """Control: the same role IS editable when the active tenant is the one
        that actually owns it."""
        resp = self.client.get(self._url("organization:role_detail", self.shared_role.pk, self.provider_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["role_editable"])

        resp = self.client.get(self._url("organization:role_update", self.shared_role.pk, self.provider_tenant))
        self.assertEqual(resp.status_code, 200)

    # ----------------------------------------------------------------------------------- #
    # RBAC_STAGE3_SPEC.md §4 — read-only banner + action gating on a shared-in role.
    # ----------------------------------------------------------------------------------- #
    def test_shared_role_detail_shows_the_shared_by_banner_from_the_managed_tenant(self):
        resp = self.client.get(self._url("organization:role_detail", self.shared_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This role is shared by")
        self.assertContains(resp, self.provider_tenant.name)

    def test_local_role_detail_has_no_shared_by_banner(self):
        resp = self.client.get(self._url("organization:role_detail", self.local_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "This role is shared by")

    def test_shared_role_owning_tenant_detail_has_no_shared_by_banner(self):
        """Control: viewed from its OWN tenant (role_editable=True), the banner
        must not render even though the role itself is shared_with_managed."""
        resp = self.client.get(self._url("organization:role_detail", self.shared_role.pk, self.provider_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "This role is shared by")

    def test_shared_role_detail_hides_edit_and_delete_actions_even_for_a_superuser(self):
        """MembershipBackend.has_perm short-circuits True for any superuser
        regardless of obj/tenant, so can_change/can_delete (and therefore
        action_urls.edit/delete) would otherwise populate here even though
        RoleEditView 404s on this exact pk from the managed tenant (see
        test_shared_role_edit_view_404s_from_the_managed_tenant above).
        role_editable is the authoritative gate that must suppress both so the
        page never offers a link that immediately 404s."""
        resp = self.client.get(self._url("organization:role_detail", self.shared_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["role_editable"])
        self.assertIsNone(resp.context["action_urls"]["edit"])
        self.assertIsNone(resp.context["action_urls"]["delete"])
        self.assertFalse(resp.context["can_change"])
        self.assertFalse(resp.context["can_delete"])
        self.assertNotContains(resp, "mdi-pencil-outline")

    def test_local_role_detail_still_offers_edit_action(self):
        """Control: a locally-owned role's Edit action is untouched by the gate."""
        resp = self.client.get(self._url("organization:role_detail", self.local_role.pk, self.managed_tenant))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["role_editable"])
        self.assertIsNotNone(resp.context["action_urls"]["edit"])


# --------------------------------------------------------------------------------------------- #
# Form layer: the shared_with_managed switch's widget attrs, help text, and visibility
# (RBAC_STAGE3_SPEC.md §4). role_form.html renders the switch highlighted directly under
# name/description and verbatim-renders the field's help_text — this module owns the copy
# and the widget attrs that drive that rendering.
# --------------------------------------------------------------------------------------------- #
class SharedWithManagedSwitchFormLayerTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.clear_tenant_context()
        self.setup_tenant_context(name="Plain Co", slug="plain-co-switch")  # self.tenant, self.tenant_admin
        self.superuser = self.tenant_admin
        self.provider_tenant = Tenant.objects.create(
            name="Northwind MSP",
            slug="northwind-msp-switch",
            is_provider=True,
        )

    def tearDown(self):
        self.clear_tenant_context()

    def test_switch_widget_renders_as_a_bootstrap_switch(self):
        form = RoleForm(user=self.superuser, tenant=self.provider_tenant)
        attrs = form.fields["shared_with_managed"].widget.attrs
        self.assertEqual(attrs.get("role"), "switch")
        self.assertEqual(attrs.get("class"), "form-check-input")

    def test_switch_help_text_matches_the_binding_spec_wording(self):
        form = RoleForm(user=self.superuser, tenant=self.provider_tenant)
        self.assertEqual(
            str(form.fields["shared_with_managed"].help_text),
            "Managed tenants can assign this role to their own members; only you can edit it.",
        )

    def test_switch_absent_for_a_plain_tenant_even_for_a_superuser(self):
        form = RoleForm(user=self.superuser, tenant=self.tenant)
        self.assertNotIn("shared_with_managed", form.fields)

    def test_switch_visibility_on_edit_follows_the_instances_own_tenant_not_the_context(self):
        """A role already owned by a plain tenant never regrows the switch just
        because a provider tenant happens to be passed as ``tenant=`` context —
        edit-time owner resolution is locked to ``instance.tenant``."""
        role = Role.objects.create(tenant=self.tenant, name="Plain Role Switch", permissions=[])
        form = RoleForm(instance=role, user=self.superuser, tenant=self.provider_tenant)
        self.assertNotIn("shared_with_managed", form.fields)

    def test_switch_present_and_checked_on_a_shared_roles_edit_form(self):
        role = Role.objects.create(
            tenant=self.provider_tenant,
            name="MSP Role Switch",
            permissions=[],
            shared_with_managed=True,
        )
        form = RoleForm(instance=role, user=self.superuser, tenant=self.provider_tenant)
        self.assertIn("shared_with_managed", form.fields)
        self.assertTrue(form["shared_with_managed"].value())
