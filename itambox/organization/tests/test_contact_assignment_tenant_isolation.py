"""Phase-1 cross-tenant boundary tests for ContactAssignment (F2).

ContactAssignment is a generic-FK assignment model with no direct or relational
`tenant` field (Contact itself is not tenant-scoped). Before the fix its API
LIST returned all-tenant rows and StrictTenantPermission passed through because
the model exposed no `tenant`. The fix scopes the LIST per content type in
ContactAssignmentViewSet.get_queryset() and adds a `tenant` property resolving
the generic-FK target's tenant so StrictTenantPermission can enforce the
object-level boundary on detail/mutation.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.api.serializers import SupplierSerializer
from assets.models import (
    Asset,
    AssetAssignment,
    AssetRole,
    AssetType,
    Manufacturer,
    StatusLabel,
    Supplier,
)
from core.context import set_current_tenant, set_current_user
from core.managers import Scope
from core.tests.mixins import grant
from organization.models import (
    Contact,
    ContactAssignment,
    ContactRole,
    Membership,
    Role,
    Tenant,
    TenantGroup,
)
from software.models import Software

User = get_user_model()


class ContactAssignmentCrossTenantTestCase(TestCase):
    def setUp(self):
        # Two tenants
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")

        # Users
        self.user_a = User.objects.create_user(username="user_a", password="password123")
        self.user_b = User.objects.create_user(username="user_b", password="password123")

        # Roles + memberships. ContactAssignment perms (view/delete) are required
        # for the API to reach the queryset/object-permission layer.
        ca_perms = [
            "organization.view_contactassignment",
            "organization.add_contactassignment",
            "organization.change_contactassignment",
            "organization.delete_contactassignment",
        ]
        self.role_a = Role.objects.create(tenant=self.tenant_a, name="Admin", permissions=ca_perms)
        self.membership_a = grant(self.user_a, self.tenant_a, self.role_a).membership
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="Admin", permissions=ca_perms)
        self.membership_b = grant(self.user_b, self.tenant_b, self.role_b).membership

        # Shared asset metadata. Use names/slugs unique to this test: the names
        # are uniquely constrained among active rows, and sibling tests in the
        # full suite (run before this one) commit common names like "Active" /
        # "Technical Support", which would otherwise collide in setUp.
        self.status = StatusLabel.objects.create(name="P1CT Active", slug="p1ct-active")
        self.asset_role = AssetRole.objects.create(name="P1CT Laptop", slug="p1ct-laptop")
        self.mfr = Manufacturer.objects.create(name="P1CT Apple", slug="p1ct-apple")
        self.asset_type = AssetType.objects.create(manufacturer=self.mfr, model="P1CT MacBook")

        # One asset per tenant
        self.asset_a = Asset.objects.create(
            name="Asset of A",
            asset_tag="TAG-A-001",
            status=self.status,
            asset_role=self.asset_role,
            asset_type=self.asset_type,
            tenant=self.tenant_a,
        )
        self.asset_b = Asset.objects.create(
            name="Asset of B",
            asset_tag="TAG-B-001",
            status=self.status,
            asset_role=self.asset_role,
            asset_type=self.asset_type,
            tenant=self.tenant_b,
        )

        # Contacts (Contact has no tenant field — tenant comes from the GFK target)
        self.contact_a = Contact.objects.create(name="P1CT Contact A", email="p1ct-a@example.com")
        self.contact_b = Contact.objects.create(name="P1CT Contact B", email="p1ct-b@example.com")
        self.contact_role = ContactRole.objects.create(name="P1CT Support Role")

        asset_ct = ContentType.objects.get_for_model(Asset)

        # Assignment scoped to tenant A (target asset belongs to tenant A)
        self.assignment_a = ContactAssignment.objects.create(
            contact=self.contact_a,
            role=self.contact_role,
            content_type=asset_ct,
            object_id=self.asset_a.pk,
            priority="primary",
        )
        # Assignment scoped to tenant B (target asset belongs to tenant B)
        self.assignment_b = ContactAssignment.objects.create(
            contact=self.contact_b,
            role=self.contact_role,
            content_type=asset_ct,
            object_id=self.asset_b.pk,
            priority="primary",
        )

        self.list_url = reverse("api:organization_api:contactassignment-list")

    def _login_tenant_a(self):
        self.client.force_login(self.user_a)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

    def _login_tenant_b(self):
        self.client.force_login(self.user_b)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_b.pk
        session.save()

    def test_list_excludes_other_tenant_assignment(self):
        """Tenant B's API list returns only its own assignment, not tenant A's."""
        self._login_tenant_b()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        returned_ids = {row["id"] for row in response.json()["results"]}
        self.assertIn(self.assignment_b.pk, returned_ids)
        self.assertNotIn(self.assignment_a.pk, returned_ids)

    def test_detail_other_tenant_assignment_404(self):
        """Tenant B GET on tenant A's assignment detail is 404 (no enumeration)."""
        self._login_tenant_b()
        detail_url = reverse(
            "api:organization_api:contactassignment-detail",
            kwargs={"pk": self.assignment_a.pk},
        )
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 404)

    def test_delete_other_tenant_assignment_404_and_survives(self):
        """Tenant B DELETE on tenant A's assignment is 404 and the row survives."""
        self._login_tenant_b()
        detail_url = reverse(
            "api:organization_api:contactassignment-detail",
            kwargs={"pk": self.assignment_a.pk},
        )
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(ContactAssignment.objects.filter(pk=self.assignment_a.pk).exists())

    def test_list_excludes_other_tenant_via_tenant_lookup_target(self):
        """H3 regression: a target with NO `tenant` column whose tenant is derived
        through a `tenant_lookup` (AssetAssignment, `asset__tenant`) must NOT leak.

        The old `get_field('tenant')` probe raised FieldDoesNotExist for such
        models and fell through to `allowed |= Q(content_type=ct)`, exposing every
        AssetAssignment-targeted ContactAssignment to every tenant.
        """
        # AssetAssignment has no `tenant` column; its tenant is asset.tenant.
        # Created inactive (no assignee) — valid per clean()/CheckConstraint and
        # still a tenant_lookup target carrying asset.tenant.
        assignment_target_a = AssetAssignment.objects.create(asset=self.asset_a, is_active=False)
        assignment_target_b = AssetAssignment.objects.create(asset=self.asset_b, is_active=False)
        aa_ct = ContentType.objects.get_for_model(AssetAssignment)
        ca_to_a = ContactAssignment.objects.create(
            contact=self.contact_a,
            role=self.contact_role,
            content_type=aa_ct,
            object_id=assignment_target_a.pk,
            priority="primary",
        )
        ca_to_b = ContactAssignment.objects.create(
            contact=self.contact_b,
            role=self.contact_role,
            content_type=aa_ct,
            object_id=assignment_target_b.pk,
            priority="primary",
        )

        self._login_tenant_b()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        returned_ids = {row["id"] for row in response.json()["results"]}
        # Tenant B sees its own tenant_lookup-scoped assignment...
        self.assertIn(ca_to_b.pk, returned_ids)
        # ...but NOT tenant A's (this leaked before the signal-probe fix).
        self.assertNotIn(ca_to_a.pk, returned_ids)

    def test_list_includes_global_catalogue_target(self):
        """L1: a ContactAssignment pointing at a global (tenant=None) catalogue
        target must appear in the active tenant's list.

        The old code resolved ids with `.filter(tenant=active_tenant)`, which
        dropped legitimate global-catalogue rows. The tenant-scoping default
        manager unions own + global rows, so they must stay visible.
        """
        global_software = Software.objects.create(
            name="P1CT Global Tool",
            manufacturer=self.mfr,
            tenant=None,
        )
        sw_ct = ContentType.objects.get_for_model(Software)
        ca_global = ContactAssignment.objects.create(
            contact=self.contact_a,
            role=self.contact_role,
            content_type=sw_ct,
            object_id=global_software.pk,
            priority="primary",
        )

        self._login_tenant_a()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        returned_ids = {row["id"] for row in response.json()["results"]}
        self.assertIn(ca_global.pk, returned_ids)

    def test_create_rejects_cross_tenant_private_contact(self):
        """D6-1: `contact_id` must not accept another tenant's private Contact.

        `ContactAssignmentSerializer.contact_id` used to be built from
        `Contact.objects.all()` — evaluated once at import time, before any
        tenant context exists, so it froze unfiltered forever (`.all()` on an
        already-baked queryset just clones it; it never re-invokes the
        manager's tenant-scoped `get_queryset()`). A Tenant-A user could then
        attach a private Tenant-B contact to their own (Tenant-A) asset, and
        the 201 response leaked that contact's PII.
        """
        private_contact_b = Contact.objects.create(
            name="P1CT Private Contact B",
            email="p1ct-private-b@example.com",
            tenant=self.tenant_b,
        )

        self._login_tenant_a()
        response = self.client.post(
            self.list_url,
            {
                "contact_id": private_contact_b.pk,
                "role_id": self.contact_role.pk,
                "assigned_object_type": "assets.asset",
                "object_id": self.asset_a.pk,
                "priority": "secondary",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("contact_id", response.json())
        self.assertFalse(
            ContactAssignment.objects.filter(
                contact=private_contact_b,
                object_id=self.asset_a.pk,
            ).exists()
        )

    def test_create_accepts_same_tenant_private_contact(self):
        """The fix must not over-scope: a tenant's own private contact stays assignable."""
        private_contact_a = Contact.objects.create(
            name="P1CT Private Contact A",
            email="p1ct-private-a@example.com",
            tenant=self.tenant_a,
        )

        self._login_tenant_a()
        response = self.client.post(
            self.list_url,
            {
                "contact_id": private_contact_a.pk,
                "role_id": self.contact_role.pk,
                "assigned_object_type": "assets.asset",
                "object_id": self.asset_a.pk,
                "priority": "secondary",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ContactAssignment.objects.filter(
                contact=private_contact_a,
                object_id=self.asset_a.pk,
            ).exists()
        )


API_SECRET_NAME = "CAA-SECRET-B-PERSON"
API_SECRET_EMAIL = "caa-secret-b@example.invalid"


class ContactAssignmentScopedReadTests(TestCase):
    """The shared ``visible_in`` helper and the API readers built on it (F4, F6).

    Before the fix the API filtered only the generic-FK target: a tenant-B
    private contact assigned to a global Manufacturer or Supplier was listed for
    every tenant, with its email, and the supplier endpoint nested it too.
    """

    def setUp(self):
        self.group = TenantGroup.objects.create(name="CAA Group", slug="caa-group")
        self.tenant_a = Tenant.objects.create(name="CAA Tenant A", slug="caa-a", group=self.group)
        self.tenant_a2 = Tenant.objects.create(name="CAA Tenant A2", slug="caa-a2", group=self.group)
        self.tenant_b = Tenant.objects.create(name="CAA Tenant B", slug="caa-b")
        self.user = User.objects.create_user(username="caa_member", password="password123")
        perms = ["organization.view_contactassignment", "assets.view_supplier"]
        for tenant in (self.tenant_a, self.tenant_a2):
            grant(self.user, tenant, Role.objects.create(tenant=tenant, name="CAA Reader", permissions=perms))

        self.manufacturer = Manufacturer.objects.create(name="CAA Maker", slug="caa-maker")
        self.supplier = Supplier.objects.create(name="CAA Supplier", slug="caa-supplier")
        self.global_contact = Contact.objects.create(name="CAA Global Desk")
        self.contact_a2 = Contact.objects.create(name="CAA Person A2", tenant=self.tenant_a2)
        self.secret_contact = Contact.objects.create(name=API_SECRET_NAME, email=API_SECRET_EMAIL, tenant=self.tenant_b)
        self.role = ContactRole.objects.create(name="CAA Role", slug="caa-role")
        status = StatusLabel.objects.create(name="CAA Active", slug="caa-active")
        asset_role = AssetRole.objects.create(name="CAA Laptop", slug="caa-laptop")
        asset_type = AssetType.objects.create(manufacturer=self.manufacturer, model="CAA Model")
        self.asset_b = Asset.objects.create(
            name="CAA Asset B",
            asset_tag="CAA-B",
            status=status,
            asset_role=asset_role,
            asset_type=asset_type,
            tenant=self.tenant_b,
        )

        self.global_on_supplier = self._assign(self.global_contact, self.supplier)
        self.global_on_manufacturer = self._assign(self.global_contact, self.manufacturer)
        self.a2_on_supplier = self._assign(self.contact_a2, self.supplier)
        self.secret_on_supplier = self._assign(self.secret_contact, self.supplier)
        self.secret_on_manufacturer = self._assign(self.secret_contact, self.manufacturer)
        self.global_on_foreign_asset = self._assign(self.global_contact, self.asset_b)

    def _assign(self, contact, target):
        return ContactAssignment.objects.create(
            contact=contact,
            role=self.role,
            content_type=ContentType.objects.get_for_model(target),
            object_id=target.pk,
        )

    def _login(self, **session_values):
        self.client.force_login(self.user)
        session = self.client.session
        session.update(session_values)
        session.save()

    def _visible(self, scope):
        return set(ContactAssignment.objects.visible_in(scope).values_list("pk", flat=True))

    def _secret_free(self, response):
        body = response.content.decode()
        self.assertNotIn(API_SECRET_NAME, body)
        self.assertNotIn(API_SECRET_EMAIL, body)
        self.assertNotIn("CAA Asset B", body)

    # -- helper --------------------------------------------------------------

    def test_helper_tenant_scope_requires_visible_target_and_contact(self):
        visible = self._visible(Scope(Scope.TENANT, user=self.user, tenant=self.tenant_a))

        self.assertEqual(visible, {self.global_on_supplier.pk, self.global_on_manufacturer.pk})

    def test_helper_group_and_all_accessible_scopes_union_member_tenants(self):
        expected = {self.global_on_supplier.pk, self.global_on_manufacturer.pk, self.a2_on_supplier.pk}

        self.assertEqual(self._visible(Scope(Scope.GROUP, user=self.user, group=self.group)), expected)
        self.assertEqual(self._visible(Scope(Scope.ALL_ACCESSIBLE, user=self.user)), expected)

    def test_helper_tenant_b_sees_its_own_contact_and_asset(self):
        visible = self._visible(Scope(Scope.TENANT, user=self.user, tenant=self.tenant_b))

        self.assertIn(self.secret_on_supplier.pk, visible)
        self.assertIn(self.global_on_foreign_asset.pk, visible)
        self.assertNotIn(self.a2_on_supplier.pk, visible)

    def test_helper_denied_is_empty_and_system_is_unfiltered(self):
        self.assertEqual(self._visible(Scope(Scope.DENIED, user=self.user)), set())
        self.assertEqual(
            self._visible(Scope(Scope.SYSTEM)), set(ContactAssignment.objects.values_list("pk", flat=True))
        )

    def test_helper_never_widens_for_unknown_or_unscopable_targets(self):
        stale_type = ContentType.objects.create(app_label="organization", model="caagone")
        stale = ContactAssignment.objects.create(
            contact=self.global_contact, role=self.role, content_type=stale_type, object_id=1
        )
        membership = Membership.objects.get(user=self.user, tenant=self.tenant_a)
        unscopable = self._assign(self.global_contact, membership)

        visible = self._visible(Scope(Scope.TENANT, user=self.user, tenant=self.tenant_a))

        self.assertNotIn(stale.pk, visible)
        self.assertNotIn(unscopable.pk, visible)
        self.assertIn(self.global_on_manufacturer.pk, visible)

    def test_helper_skips_targets_with_a_non_integer_primary_key(self):
        session_type = ContentType.objects.get(app_label="sessions", model="session")
        row = ContactAssignment.objects.create(
            contact=self.global_contact, role=self.role, content_type=session_type, object_id=1
        )

        self.assertNotIn(row.pk, self._visible(Scope(Scope.TENANT, user=self.user, tenant=self.tenant_a)))

    # -- REST ----------------------------------------------------------------

    def test_api_list_hides_foreign_private_contact_on_shared_targets(self):
        self._login(active_tenant_id=self.tenant_a.pk)

        response = self.client.get(reverse("api:organization_api:contactassignment-list"))

        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.json()["results"]}
        self.assertEqual(ids, {self.global_on_supplier.pk, self.global_on_manufacturer.pk})
        self._secret_free(response)

    def test_api_detail_of_foreign_private_contact_on_shared_target_is_404(self):
        self._login(active_tenant_id=self.tenant_a.pk)
        url = reverse("api:organization_api:contactassignment-detail", kwargs={"pk": self.secret_on_supplier.pk})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)
        self._secret_free(response)

    def test_api_list_works_in_group_and_all_accessible_scopes(self):
        expected = {self.global_on_supplier.pk, self.global_on_manufacturer.pk, self.a2_on_supplier.pk}
        for session_values in ({"active_tenant_group_id": self.group.pk}, {"active_all_accessible": True}):
            with self.subTest(scope=session_values):
                self._login(**session_values)
                response = self.client.get(reverse("api:organization_api:contactassignment-list"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual({row["id"] for row in response.json()["results"]}, expected)
                self._secret_free(response)
                self.client.logout()

    def test_supplier_api_nests_only_visible_contacts(self):
        self._login(active_tenant_id=self.tenant_a.pk)
        detail = self.client.get(reverse("api:assets_api:supplier-detail", kwargs={"pk": self.supplier.pk}))
        listing = self.client.get(reverse("api:assets_api:supplier-list"), {"slug": self.supplier.slug})

        self.assertEqual(detail.status_code, 200)
        self.assertEqual([row["id"] for row in detail.json()["contacts"]], [self.global_on_supplier.pk])
        self._secret_free(detail)
        self.assertEqual(listing.status_code, 200)
        (row,) = [row for row in listing.json()["results"] if row["id"] == self.supplier.pk]
        self.assertEqual([contact["id"] for contact in row["contacts"]], [self.global_on_supplier.pk])
        self._secret_free(listing)

    def test_supplier_serializer_without_prefetch_scopes_by_itself(self):
        set_current_user(self.user)
        set_current_tenant(self.tenant_a)
        data = SupplierSerializer(Supplier.objects.get(pk=self.supplier.pk), context={"request": None}).data

        self.assertEqual([row["id"] for row in data["contacts"]], [self.global_on_supplier.pk])
