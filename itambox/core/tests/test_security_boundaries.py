import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.tests.mixins import grant
from organization.models import Role, RoleGrant, RoleGrantScope, Tenant
from users.models import GroupMembership, UserGroup

User = get_user_model()


def grant_group_role(group, membership, role):
    GroupMembership.objects.create(user_group=group, membership=membership)
    role_grant = RoleGrant.objects.create(user_group=group, role=role)
    RoleGrantScope.objects.create(
        role_grant=role_grant,
        scope_type=RoleGrantScope.SCOPE_OWN,
    )
    return role_grant


class SecurityBoundariesTestCase(TestCase):
    def setUp(self):
        # Create Tenants
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")

        # Create Users
        self.user_a = User.objects.create_user(username="user_a", password="password123")
        self.user_b = User.objects.create_user(username="user_b", password="password123")

        # Bind Tenant A user through a canonical direct RoleGrant.
        self.role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="Admin",
            permissions=["assets.view_asset", "assets.add_asset", "assets.change_asset", "assets.delete_asset"],
        )
        self.membership_a = grant(self.user_a, self.tenant_a, self.role_a).membership

        # Bind Tenant B user through a canonical direct RoleGrant.
        self.role_b = Role.objects.create(
            tenant=self.tenant_b,
            name="Admin",
            permissions=["assets.view_asset", "assets.add_asset", "assets.change_asset", "assets.delete_asset"],
        )
        self.membership_b = grant(self.user_b, self.tenant_b, self.role_b).membership

        # Create base metadata
        self.status = StatusLabel.objects.create(name="Active", slug="active")
        self.role = AssetRole.objects.create(name="Laptop", slug="laptop")
        self.mfr = Manufacturer.objects.create(name="Apple", slug="apple")
        self.asset_type = AssetType.objects.create(manufacturer=self.mfr, model="MacBook Pro")

        # Create Asset for Tenant B
        self.asset_b = Asset.objects.create(
            name="Asset of B",
            asset_tag="TAG-B-001",
            status=self.status,
            asset_role=self.role,
            asset_type=self.asset_type,
            tenant=self.tenant_b,
        )

    def test_graphql_cross_tenant_query_denied(self):
        self.client.force_login(self.user_a)

        # Set request's active tenant to tenant_a
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

        # Try querying Tenant B's asset via GraphQL
        query = f"""
        query {{
            asset(
                id: {json.dumps(str(self.asset_b.pk))},
                requestedScope: {{ mode: TENANT, tenantId: {json.dumps(str(self.tenant_a.pk))} }}
            ) {{
                id
                name
            }}
        }}
        """
        response = self.client.post(
            reverse("graphql"), data=json.dumps({"query": query}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["data"]["asset"])

    def test_rest_api_cross_tenant_mutation_denied(self):
        self.client.force_login(self.user_a)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

        # Try putting or deleting Tenant B's asset via REST API
        # First we check view detail is 404/denied
        detail_url = reverse("api:assets_api:asset-detail", kwargs={"pk": self.asset_b.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 404)

        # Try changing it via API PUT
        put_data = {
            "name": "Hacked Name",
            "asset_tag": "TAG-B-001",
            "status": self.status.pk,
            "asset_role": self.role.pk,
            "asset_type": self.asset_type.pk,
            "tenant": self.tenant_b.pk,
        }
        response = self.client.put(detail_url, data=put_data, content_type="application/json")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Security boundary tests for the new multi-role RBAC shape
# ---------------------------------------------------------------------------


class MultiRoleSecurityBoundaryTestCase(TestCase):
    """Cross-cutting security boundary tests for the multi-role RBAC shape.

    Verifies that the additive union never "leaks" across the tenant boundary
    and that every deactivation mechanism correctly shuts down access.
    """

    def setUp(self):
        from core.managers import set_current_membership, set_current_tenant

        self.set_current_tenant = set_current_tenant
        self.set_current_membership = set_current_membership

        self.tenant_a = Tenant.objects.create(name="MRSB-Tenant-A", slug="mrsb-tenant-a")
        self.tenant_b = Tenant.objects.create(name="MRSB-Tenant-B", slug="mrsb-tenant-b")

        self.user = User.objects.create_user(username="mrsb_user", password="pass")

        # Full-access role in Tenant A
        self.role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="MRSB-Full-A",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "assets.delete_asset",
            ],
        )

        # Membership in Tenant A
        self.membership_a = grant(self.user, self.tenant_a, self.role_a).membership
        # A second direct role grant contributes additional permissions additively.
        self.direct_role = Role.objects.create(
            tenant=self.tenant_a,
            name="Direct grants",
            permissions=["assets.delete_asset"],
        )
        grant(self.user, self.tenant_a, self.direct_role)

        # UserGroup in Tenant A (change perm via its membership-backed grant)
        self.group_a = UserGroup.objects.create(
            tenant=self.tenant_a,
            name="MRSB-Group-A",
            is_active=True,
        )
        grant_group_role(self.group_a, self.membership_a, self.role_a)

        # Asset in Tenant B — this is what we must NOT access
        self.status = StatusLabel.objects.create(name="MRSB-Status", slug="mrsb-status", type="deployable")
        self.asset_b = Asset.objects.create(
            name="MRSB-Asset-B",
            asset_tag="MRSB-B-001",
            status=self.status,
            tenant=self.tenant_b,
        )

        set_current_tenant(self.tenant_a)
        set_current_membership(self.membership_a)

    def tearDown(self):
        self.set_current_tenant(None)
        self.set_current_membership(None)

    def _clear_perm_cache(self):
        for attr in list(vars(self.user)):
            if (
                attr.startswith("_perms_tenant_")
                or attr.startswith("_perms_provider_")
                or attr.startswith("_tenant_membership_")
            ):
                delattr(self.user, attr)

    # ------------------------------------------------------------------
    # Tenant boundary
    # ------------------------------------------------------------------

    def test_membership_role_does_not_apply_across_tenants(self):
        """A membership role in tenant A must not grant access to a tenant B object."""
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("assets.view_asset", obj=self.asset_b))

    def test_additional_direct_grant_does_not_apply_across_tenants(self):
        """An additional direct grant in tenant A cannot reach a tenant B object."""
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("assets.delete_asset", obj=self.asset_b))

    def test_usergroup_role_does_not_apply_across_tenants(self):
        """UserGroup membership in tenant A must not grant access to a tenant B object."""
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("assets.change_asset", obj=self.asset_b))

    # ------------------------------------------------------------------
    # is_active gating (membership)
    # ------------------------------------------------------------------

    def test_suspended_membership_denies_direct_and_group_paths(self):
        """Suspending the membership shuts off both its direct grants and any group
        grants whose principal link is backed by that membership."""
        self.membership_a.is_active = False
        self.membership_a.save()
        self._clear_perm_cache()

        self.assertFalse(self.user.has_perm("assets.view_asset"))  # role path
        self.assertFalse(self.user.has_perm("assets.delete_asset"))  # direct grant path
        self.assertFalse(self.user.has_perm("assets.change_asset"))  # group grant path

    # ------------------------------------------------------------------
    # is_active gating (UserGroup)
    # ------------------------------------------------------------------

    def test_inactive_group_excluded_from_union(self):
        """An inactive UserGroup must not contribute its roles, even with an active membership."""
        self.group_a.is_active = False
        self.group_a.save()

        # Remove assignments from membership so group is the only change-perm source
        self.membership_a.role_grants.all().delete()
        self._clear_perm_cache()

        self.assertFalse(self.user.has_perm("assets.change_asset"))

    # ------------------------------------------------------------------
    # Soft-deleted role
    # ------------------------------------------------------------------

    def test_soft_deleted_role_on_membership_contributes_nothing(self):
        """After a role is soft-deleted it must be excluded from all perm paths."""
        # Second role that we will soft-delete
        transient_role = Role.objects.create(
            tenant=self.tenant_a,
            name="Transient Role",
            permissions=["organization.view_tenant"],
        )
        grant(self.user, self.tenant_a, transient_role)
        self._clear_perm_cache()
        self.assertTrue(self.user.has_perm("organization.view_tenant"))

        # Soft-delete the role
        transient_role.delete()
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("organization.view_tenant"))

    def test_soft_deleted_role_on_group_contributes_nothing(self):
        """A soft-deleted role attached only to a UserGroup must not grant perms."""
        group_only_role = Role.objects.create(
            tenant=self.tenant_a,
            name="Group-only Role",
            permissions=["organization.view_tenant"],
        )
        extra_group = UserGroup.objects.create(
            tenant=self.tenant_a,
            name="Extra Group",
            is_active=True,
        )
        grant_group_role(extra_group, self.membership_a, group_only_role)
        self._clear_perm_cache()
        self.assertTrue(self.user.has_perm("organization.view_tenant"))

        group_only_role.delete()
        self._clear_perm_cache()
        self.assertFalse(self.user.has_perm("organization.view_tenant"))

    # ------------------------------------------------------------------
    # No membership at all
    # ------------------------------------------------------------------

    def test_user_with_no_membership_in_tenant_b_denied(self):
        """The user has no Membership in tenant B — all perms must be denied for tenant B objects."""
        self._clear_perm_cache()
        # Explicit obj path
        self.assertFalse(self.user.has_perm("assets.view_asset", obj=self.asset_b))

    # ------------------------------------------------------------------
    # Superuser bypass
    # ------------------------------------------------------------------

    def test_superuser_bypasses_all_checks(self):
        """Superuser must always return True regardless of memberships/groups."""
        su = User.objects.create_superuser(username="mrsb_su", password="pass")
        self.assertTrue(su.has_perm("assets.view_asset"))
        self.assertTrue(su.has_perm("assets.view_asset", obj=self.asset_b))


# ---------------------------------------------------------------------------
# Security boundary tests for the user object views (detail / edit / delete)
# ---------------------------------------------------------------------------


class UserObjectViewTenantBoundaryTestCase(TestCase):
    """The tenant boundary of the user object views, pinned at the boundary layer.

    ``User`` has no ``tenant`` field, so the generic tenant scoping (queryset
    managers, ``has_perm(..., obj=...)``) cannot narrow a user row: both resolve
    the tenant from ``obj.tenant``, find none, and fall back to the ambient
    tenant. The user object views therefore carry their own membership-derived
    boundary; this class pins it next to the other cross-tenant invariants.
    The exhaustive matrix lives in ``users/tests/test_user_object_scope.py``.
    """

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="UOV-A", slug="uov-a")
        self.tenant_b = Tenant.objects.create(name="UOV-B", slug="uov-b")

        self.operator = User.objects.create_user(username="uov_operator", password="password123")
        self.role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="UOV-User-Admin-A",
            permissions=["users.view_user", "users.change_user", "users.delete_user"],
        )
        grant(self.operator, self.tenant_a, self.role_a)

        self.target_a = User.objects.create_user(username="uov_target_a", password="password123")
        grant(self.target_a, self.tenant_a, self.role_a)

        self.target_b = User.objects.create_user(username="uov_target_b", password="password123")
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="UOV-No-Rights-B", permissions=[])
        grant(self.target_b, self.tenant_b, self.role_b)

    def _login(self, user, tenant):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

    def test_cross_tenant_user_objects_are_not_reachable(self):
        self._login(self.operator, self.tenant_a)
        # The objectless gate passes on the strength of tenant A alone.
        self.assertTrue(self.operator.has_perm("users.view_user"))
        self.assertTrue(self.operator.has_perm("users.change_user"))

        self.assertEqual(
            self.client.get(reverse("users:user_detail", kwargs={"pk": self.target_b.pk})).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("users:user_update", kwargs={"pk": self.target_b.pk})).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("users:user_delete", kwargs={"pk": self.target_b.pk}), {}).status_code,
            404,
        )
        self.assertTrue(User.objects.filter(pk=self.target_b.pk).exists())

    def test_same_tenant_user_object_stays_reachable(self):
        self._login(self.operator, self.tenant_a)
        self.assertEqual(
            self.client.get(reverse("users:user_detail", kwargs={"pk": self.target_a.pk})).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("users:user_update", kwargs={"pk": self.target_a.pk})).status_code,
            200,
        )
