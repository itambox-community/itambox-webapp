"""Tenant boundary for the user object views (detail, edit, delete).

``User`` carries no ``tenant`` field — a person holds memberships in several
tenants — so neither the scoped managers nor ``has_perm(..., obj=user)`` narrow
a user row: both fall back to the ambient tenant. These tests pin the boundary
that the user object views have to enforce themselves:

* an out-of-scope pk is a 404 (a 403 would confirm the row exists),
* the permission is anchored at a tenant the target is really a member of, so a
  permission held in one tenant cannot act on a user of another,
* a login credential is a global account capability: a tenant-scoped operator
  cannot set one on an existing account.

Auth fixtures follow ``core/tests/test_security_boundaries.py``: real
``Tenant`` + ``Role`` + ``Membership`` rows through ``grant()``, ``force_login``
and an ``active_tenant_id`` (or ``active_all_accessible``) session value that
``TenantMiddleware`` turns into the request scope. Permissions are granted
through roles only — never through a superuser for the positive cases.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.tests.mixins import grant
from organization.models import Role, Tenant

User = get_user_model()

USER_OPERATOR_PERMISSIONS = [
    "users.view_user",
    "users.add_user",
    "users.change_user",
    "users.delete_user",
]


class UserObjectScopeTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Scope A", slug="scope-a")
        self.tenant_b = Tenant.objects.create(name="Scope B", slug="scope-b")

        # Operator: full user administration in tenant A, no permission in
        # tenant B (only a membership there, so B is inside their workspace).
        self.operator = User.objects.create_user(username="operator", password="operator-pass-1")
        self.role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="User admin A",
            permissions=list(USER_OPERATOR_PERMISSIONS),
        )
        grant(self.operator, self.tenant_a, self.role_a)
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="No user rights B", permissions=[])
        grant(self.operator, self.tenant_b, self.role_b)

        # Same-tenant target.
        self.target_a = User.objects.create_user(
            username="target_a",
            password="target-a-pass-1",
            first_name="Ada",
        )
        grant(self.target_a, self.tenant_a, self.role_a)

        # Another tenant's target: must be invisible to the operator.
        self.target_b = User.objects.create_user(
            username="target_b",
            password="target-b-pass-1",
            first_name="Bob",
        )
        grant(self.target_b, self.tenant_b, self.role_b)

        # A person who is a member of BOTH tenants: reachable from either side.
        self.target_ab = User.objects.create_user(username="target_ab", password="target-ab-pass-1")
        grant(self.target_ab, self.tenant_a, self.role_a)
        grant(self.target_ab, self.tenant_b, self.role_b)

        self.superuser = User.objects.create_superuser(
            username="root_user", email="root@example.com", password="root-pass-1"
        )

    # ------------------------------------------------------------------ helpers
    def _login(self, user, *, tenant=None, all_accessible=False):
        self.client.force_login(user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_all_accessible", None)
        if tenant is not None:
            session["active_tenant_id"] = tenant.pk
        if all_accessible:
            session["active_all_accessible"] = True
        session.save()

    def _url(self, name, **kwargs):
        return reverse(f"users:{name}", kwargs=kwargs)

    # ------------------------------------------------------------- anonymous
    def test_anonymous_request_is_redirected_instead_of_erroring(self):
        """The scoped queryset runs before the login gate; an anonymous visitor
        must get the login redirect, not a 404/500 from an empty scope."""
        response = self.client.get(self._url("user_detail", pk=self.target_a.pk))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    # ------------------------------------------------------- same-tenant access
    def test_detail_same_tenant_allowed(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.get(self._url("user_detail", pk=self.target_a.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "target_a")

    def test_edit_post_same_tenant_persists(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.post(
            self._url("user_update", pk=self.target_a.pk),
            {
                "username": "target_a",
                "first_name": "Grace",
                "last_name": "",
                "email": "",
                "is_active": "on",
                "can_login": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.target_a.refresh_from_db()
        self.assertEqual(self.target_a.first_name, "Grace")

    def test_delete_same_tenant_removes_the_account(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.post(self._url("user_delete", pk=self.target_a.pk), {"return_url": ""})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.target_a.pk).exists())

    def test_multi_tenant_person_is_reachable_from_a_shared_tenant(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.get(self._url("user_detail", pk=self.target_ab.pk))
        self.assertEqual(response.status_code, 200)

    # -------------------------------------------------- cross-tenant non-disclosure
    def test_detail_cross_tenant_is_not_found(self):
        self._login(self.operator, tenant=self.tenant_a)
        # The ambient (objectless) gate really does pass — the permission is held
        # in the operator's own tenant. Only the tenant-anchored check may deny.
        self.assertTrue(self.operator.has_perm("users.view_user"))

        response = self.client.get(self._url("user_detail", pk=self.target_b.pk))
        self.assertEqual(response.status_code, 404)

    def test_edit_get_cross_tenant_is_not_found(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.get(self._url("user_update", pk=self.target_b.pk))
        self.assertEqual(response.status_code, 404)

    def test_edit_post_cross_tenant_is_not_found_and_changes_nothing(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.post(
            self._url("user_update", pk=self.target_b.pk),
            {
                "username": "target_b",
                "first_name": "Taken over",
                "last_name": "",
                "email": "",
                "is_active": "on",
                "can_login": "on",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.target_b.refresh_from_db()
        self.assertEqual(self.target_b.first_name, "Bob")

    def test_delete_post_cross_tenant_is_not_found_and_keeps_the_account(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.post(self._url("user_delete", pk=self.target_b.pk), {"return_url": ""})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(User.objects.filter(pk=self.target_b.pk).exists())

    def test_bulk_edit_leaves_cross_tenant_rows_untouched(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.post(
            self._url("user_bulk_edit"),
            {
                "pk": [self.target_a.pk, self.target_b.pk],
                "_selected_fields": ["is_active"],
                "is_active": "False",
                "_apply": "true",
            },
        )
        self.assertEqual(response.status_code, 302)

        self.target_a.refresh_from_db()
        self.target_b.refresh_from_db()
        self.assertFalse(self.target_a.is_active)
        self.assertTrue(self.target_b.is_active)

    # ------------------------------------------- object-anchored permission check
    def test_aggregate_scope_still_anchors_the_permission_at_the_target_tenant(self):
        """Under "all accessible tenants" the ambient gate is a union over every
        accessible tenant, so it passes on the strength of tenant A alone. Acting
        on a tenant-B-only person must still be denied."""
        self._login(self.operator, all_accessible=True)

        # Ambient union: the operator holds the permission somewhere accessible.
        self.assertTrue(self.operator.has_perm("users.view_user"))

        # Reachable through the union, but no view permission inside tenant B.
        self.assertEqual(self.client.get(self._url("user_detail", pk=self.target_b.pk)).status_code, 403)
        self.assertEqual(self.client.get(self._url("user_update", pk=self.target_b.pk)).status_code, 403)
        self.assertEqual(
            self.client.post(self._url("user_delete", pk=self.target_b.pk), {"return_url": ""}).status_code, 403
        )
        self.assertTrue(User.objects.filter(pk=self.target_b.pk).exists())

        # The person shared with tenant A stays fully accessible.
        self.assertEqual(self.client.get(self._url("user_detail", pk=self.target_ab.pk)).status_code, 200)

    def test_single_tenant_scope_without_permission_in_that_tenant_is_denied(self):
        """A membership in B without a user role is not a licence to read B's users."""
        self._login(self.operator, tenant=self.tenant_b)
        response = self.client.get(self._url("user_detail", pk=self.target_b.pk))
        self.assertEqual(response.status_code, 403)

    # ------------------------------------------------------------- password path
    def test_edit_form_hides_the_password_field_from_a_tenant_operator(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.get(self._url("user_update", pk=self.target_a.pk))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", response.context["form"].fields)
        self.assertNotContains(response, 'name="password"')

    def test_tenant_operator_cannot_reset_another_users_password(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.post(
            self._url("user_update", pk=self.target_a.pk),
            {
                "username": "target_a",
                "first_name": "Ada",
                "last_name": "",
                "email": "",
                "is_active": "on",
                "can_login": "on",
                "password": "attacker-chosen-1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.target_a.refresh_from_db()
        self.assertTrue(self.target_a.check_password("target-a-pass-1"))
        self.assertFalse(self.target_a.check_password("attacker-chosen-1"))

    def test_create_form_keeps_the_provisioning_password(self):
        self._login(self.operator, tenant=self.tenant_a)
        response = self.client.get(self._url("user_create"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("password", response.context["form"].fields)
        self.assertContains(response, 'name="password"')

    def test_superuser_can_still_set_a_password_on_an_existing_account(self):
        self._login(self.superuser)
        response = self.client.post(
            self._url("user_update", pk=self.target_b.pk),
            {
                "username": "target_b",
                "first_name": "Bob",
                "last_name": "",
                "email": "",
                "is_active": "on",
                "can_login": "on",
                "password": "rotated-by-root-1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.target_b.refresh_from_db()
        self.assertTrue(self.target_b.check_password("rotated-by-root-1"))
