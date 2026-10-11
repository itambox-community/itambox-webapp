"""Issue #29 regression suite.

Two related defects on the tenant selector for authenticated non-superusers:

1. Selecting a tenant group made a query-heavy page take ~20s because
   ``TenantScopingQuerySet.filter_by_tenant()`` recomputed the canonical
   ``accessible_tenant_ids(user)`` RBAC walk for *every* tenant-scoped model
   rendered on the page (superusers took a different, cheap branch — matching
   the observed role-dependent slowdown). The resolution is now memoized for the
   request, so it runs once regardless of how many scoped querysets render.

2. Non-superusers had no "All accessible tenants" scope — only a single tenant
   or a single tenant group. A distinct, fail-closed all-accessible scope now
   exists that returns exactly the tenants from the canonical resolver and is
   never equivalent to the superuser/global scope.
"""

import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone, translation

import organization.rbac as rbac
from core.managers import (
    Scope,
    get_current_all_accessible,
    get_current_membership,
    get_current_tenant,
    get_current_tenant_group,
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.tests.mixins import grant
from itambox.middleware import _current_user
from organization.models import (
    Membership,
    Role,
    RoleGrant,
    RoleGrantScope,
    Tenant,
    TenantGroup,
)
from users.models import GroupMembership, UserGroup

User = get_user_model()


class GroupScopeResolutionPerfTests(TestCase):
    """The RBAC resolution must run a bounded number of times per request even
    when many tenant-scoped querysets render under a tenant-group scope."""

    def setUp(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        self.region = TenantGroup.objects.create(name="Region", slug="i29-region")
        self.provider = Tenant.objects.create(
            name="I29 Provider",
            slug="i29-p",
            is_provider=True,
        )
        self.cust_a = Tenant.objects.create(
            name="I29 A",
            slug="i29-a",
            managed_by=self.provider,
            group=self.region,
        )
        self.cust_b = Tenant.objects.create(
            name="I29 B",
            slug="i29-b",
            managed_by=self.provider,
            group=self.region,
        )
        self.role = Role.objects.create(tenant=self.provider, name="Tech", permissions=[])
        self.user = User.objects.create_user(username="i29-staff", password="pw")
        # Managed reach to both customers in the region (no direct customer membership).
        grant(
            self.user,
            self.provider,
            self.role,
            reach=RoleGrant.REACH_MANAGED,
            managed_scope=RoleGrantScope.SCOPE_TENANT,
            assigned_tenants=[self.cust_a, self.cust_b],
        )

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)

    def _activate_group_scope(self):
        _current_user.set(self.user)
        set_current_tenant(None)
        set_current_tenant_group(self.region)

    def test_group_scope_resolves_accessible_tenants_once_per_request(self):
        from assets.models import Asset

        self._activate_group_scope()
        with mock.patch.object(
            rbac,
            "resolve_accessible_tenant_ids_with_expiry",
            wraps=rbac.resolve_accessible_tenant_ids_with_expiry,
        ) as spy:
            for _ in range(8):
                list(Asset.objects.for_scope(Scope.current()))
        self.assertEqual(
            spy.call_count,
            1,
            f"RBAC resolver ran {spy.call_count}x under a group scope; expected exactly one request-local resolution",
        )

    def test_group_scope_tenant_ids_resolve_once_per_request(self):
        """Phase 3 correction for issue #56: ``_group_scope_tenant_ids`` re-ran
        its Tenant intersection SELECT on every call even though the
        underlying ``accessible_tenant_ids`` resolution was already memoized.
        Repeated resolution for the same user/group in one request must not
        repeat that query.
        """
        from core.managers import TenantScopingQuerySet

        self._activate_group_scope()

        def get_descendant_group_ids(group_id):
            return [group_id]

        first = TenantScopingQuerySet._group_scope_tenant_ids(
            self.region,
            get_descendant_group_ids,
            Tenant,
        )
        with self.assertNumQueries(0):
            second = TenantScopingQuerySet._group_scope_tenant_ids(
                self.region,
                get_descendant_group_ids,
                Tenant,
            )
        self.assertEqual(first, second)

    def test_warm_accessible_tenant_cache_costs_zero_database_queries(self):
        from organization.access import accessible_tenant_ids

        accessible_tenant_ids(self.user)
        with self.assertNumQueries(0):
            self.assertEqual(
                accessible_tenant_ids(self.user),
                {self.provider.pk, self.cust_a.pk, self.cust_b.pk},
            )

    def test_group_scope_ten_reads_have_exact_steady_state_query_budget(self):
        from assets.models import Asset

        self._activate_group_scope()
        # Warm both request-local memos, including the group's resolved
        # tenant-id list (phase 3 correction for issue #56). Each later read
        # now costs exactly the requested Asset SELECT — the tenant/group
        # intersection SELECT no longer repeats per read.
        list(Asset.objects.all())
        with self.assertNumQueries(10):
            for _ in range(10):
                list(Asset.objects.all())


class AllAccessibleScopeTests(TestCase):
    """The fail-closed "All accessible tenants" scope for a non-superuser member.

    It must return exactly the tenants the canonical resolver authorizes —
    direct memberships, managed reach, and UserGroup-derived access — and never
    widen into the superuser/global view.
    """

    def setUp(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)

        self.region = TenantGroup.objects.create(name="Region", slug="i29a-region")
        self.region_west = TenantGroup.objects.create(
            name="Region West",
            slug="i29a-west",
            parent=self.region,
        )
        self.provider = Tenant.objects.create(
            name="I29A Provider",
            slug="i29a-p",
            is_provider=True,
        )
        self.cust_a = Tenant.objects.create(  # direct membership
            name="I29A A",
            slug="i29a-a",
            managed_by=self.provider,
            group=self.region,
        )
        self.cust_b = Tenant.objects.create(  # managed reach
            name="I29A B",
            slug="i29a-b",
            managed_by=self.provider,
            group=self.region,
        )
        self.cust_c = Tenant.objects.create(  # UserGroup-derived
            name="I29A C",
            slug="i29a-c",
            managed_by=self.provider,
            group=self.region_west,
        )
        self.cust_d = Tenant.objects.create(  # NOT accessible
            name="I29A D",
            slug="i29a-d",
            managed_by=self.provider,
            group=self.region,
        )
        self.other = Tenant.objects.create(  # unrelated, NOT accessible
            name="I29A Other",
            slug="i29a-other",
        )

        self.tech_role = Role.objects.create(tenant=self.provider, name="Tech", permissions=[])
        self.role_a = Role.objects.create(tenant=self.cust_a, name="A Direct", permissions=[])

        self.member = User.objects.create_user(username="i29a-member", password="pw")
        # 1) direct membership in cust_a
        grant(self.member, self.cust_a, self.role_a)
        # 2) managed reach to cust_b (rides on a provider membership)
        self.managed_grant = grant(
            self.member,
            self.provider,
            self.tech_role,
            reach=RoleGrant.REACH_MANAGED,
            managed_scope=RoleGrantScope.SCOPE_TENANT,
            assigned_tenants=[self.cust_b],
        )
        self.managed_grant.valid_until = timezone.now() + timedelta(seconds=30)
        self.managed_grant.save(update_fields=["valid_until"])
        # 3) UserGroup-derived access to cust_c
        user_group = UserGroup.objects.create(
            name="I29A Team",
            slug="i29a-team",
            tenant=self.provider,
        )
        provider_membership = Membership.objects.get(user=self.member, tenant=self.provider)
        GroupMembership.objects.create(user_group=user_group, membership=provider_membership)
        group_grant = RoleGrant.objects.create(user_group=user_group, role=self.tech_role)
        RoleGrantScope.objects.create(
            role_grant=group_grant,
            scope_type=RoleGrantScope.SCOPE_TENANT,
            tenant=self.cust_c,
        )

        self.superuser = User.objects.create_superuser(
            username="i29a-su",
            email="i29a-su@x.com",
            password="pw",
        )

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)

    def _all_accessible_slugs(self, user):
        _current_user.set(user)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(True)
        return set(Tenant.objects.for_scope(Scope.current()).values_list("slug", flat=True))

    def test_all_accessible_includes_direct_membership(self):
        self.assertIn("i29a-a", self._all_accessible_slugs(self.member))

    def test_all_accessible_includes_managed_reach(self):
        self.assertIn("i29a-b", self._all_accessible_slugs(self.member))

    def test_all_accessible_includes_group_derived(self):
        self.assertIn("i29a-c", self._all_accessible_slugs(self.member))

    def test_all_accessible_excludes_inaccessible_tenants(self):
        slugs = self._all_accessible_slugs(self.member)
        self.assertNotIn("i29a-d", slugs)
        self.assertNotIn("i29a-other", slugs)

    def test_all_accessible_returns_exactly_the_authorized_set(self):
        # provider is accessible via the direct provider membership that carries reach.
        self.assertEqual(
            self._all_accessible_slugs(self.member),
            {"i29a-p", "i29a-a", "i29a-b", "i29a-c"},
        )

    def test_all_accessible_is_never_the_global_scope(self):
        member_slugs = self._all_accessible_slugs(self.member)
        all_slugs = set(Tenant._base_manager.values_list("slug", flat=True))
        # Strict subset — the all-accessible scope excludes tenants the member
        # cannot reach, unlike a superuser's global view.
        self.assertTrue(member_slugs < all_slugs)
        set_current_all_accessible(False)
        _current_user.set(self.superuser)
        # A superuser is unaffected: their global view still spans every tenant.
        self.assertEqual(
            set(Tenant._base_manager.values_list("slug", flat=True)),
            all_slugs,
        )

    def test_all_accessible_scopes_domain_models(self):
        # inline import: assets model only needed for this domain-scoping probe.
        from assets.models import Asset

        asset_a = Asset.objects.create(name="A asset", tenant=self.cust_a)
        asset_b = Asset.objects.create(name="B asset", tenant=self.cust_b)
        asset_d = Asset.objects.create(name="D asset", tenant=self.cust_d)

        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(True)
        visible = set(Asset.objects.for_scope(Scope.current()).values_list("pk", flat=True))
        self.assertEqual(visible, {asset_a.pk, asset_b.pk})
        self.assertNotIn(asset_d.pk, visible)

    def test_all_accessible_fail_closed_without_any_access(self):
        stranger = User.objects.create_user(username="i29a-stranger", password="pw")
        self.assertEqual(self._all_accessible_slugs(stranger), set())

    def test_all_accessible_ambient_check_prebuilds_tenant_permissions_map(self):
        """Phase 3 correction for issue #56: ``_all_accessible_permissions`` iterated
        every accessible tenant via ``_effective_perms_for_tenant`` without ever
        calling ``build_accessible_tenant_permissions_map`` — the corrected
        single-pass map (see test_tenant_permissions_map.py) was defined but
        never wired into the production ambient-permission path, so it stayed
        dormant. Resolving an ambient permission under the all-accessible scope
        must now populate that map as a side effect, not just leave it callable.
        """
        self.role_a.permissions = ["assets.view_asset"]
        self.role_a.save(update_fields=["permissions"])

        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(True)

        self.assertIsNone(self.member.__dict__.get("_tenant_permissions_map"))
        self.assertTrue(self.member.has_perm("assets.view_asset"))

        perm_map = self.member.__dict__.get("_tenant_permissions_map")
        self.assertIsNotNone(perm_map)
        self.assertIn("assets.view_asset", perm_map[self.cust_a.pk][0])

    def test_all_accessible_ten_asset_reads_are_exactly_ten_queries_when_warm(self):
        from assets.models import Asset
        from organization.access import accessible_tenant_ids

        Asset.objects.create(name="A asset", tenant=self.cust_a)
        _current_user.set(self.member)
        set_current_all_accessible(True)
        accessible_tenant_ids(self.member)
        with self.assertNumQueries(10):
            for _ in range(10):
                list(Asset.objects.all())

    def test_all_accessible_tenant_group_models_reuse_warmed_group_projection(self):
        from compliance.models import CustodyTemplate
        from organization.access import accessible_tenant_ids

        CustodyTemplate._base_manager.create(
            name="Group template",
            eula_text="Terms",
            tenant_group=self.region,
        )
        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(True)
        accessible_tenant_ids(self.member)
        list(CustodyTemplate.objects.all())

        with self.assertNumQueries(10):
            for _ in range(10):
                list(CustodyTemplate.objects.all())

    def test_expiring_grant_is_removed_without_save_or_generation_bump(self):
        from organization.access import accessible_tenant_ids

        _current_user.set(self.member)
        set_current_all_accessible(True)
        self.assertIn(self.cust_b.pk, accessible_tenant_ids(self.member))

        after_expiry = self.managed_grant.valid_until + timedelta(seconds=1)
        with (
            mock.patch("organization.access.timezone.now", return_value=after_expiry),
            mock.patch("organization.rbac.timezone.now", return_value=after_expiry),
        ):
            self.assertNotIn(self.cust_b.pk, accessible_tenant_ids(self.member))

    def test_all_accessible_queryset_drops_clock_expired_grant_without_write(self):
        self.assertIn("i29a-b", self._all_accessible_slugs(self.member))

        after_expiry = self.managed_grant.valid_until + timedelta(seconds=1)
        with (
            mock.patch("organization.access.timezone.now", return_value=after_expiry),
            mock.patch("organization.rbac.timezone.now", return_value=after_expiry),
        ):
            visible = set(Tenant.objects.for_scope(Scope.current()).values_list("slug", flat=True))

        self.assertNotIn("i29a-b", visible)
        self.assertEqual(visible, {"i29a-p", "i29a-a", "i29a-c"})

    def test_cache_outage_recomputes_instead_of_serving_local_memo(self):
        from organization.access import accessible_tenant_ids

        with (
            mock.patch(
                "core.authorization_cache.cache.get_many",
                side_effect=RuntimeError("cache unavailable"),
            ),
            mock.patch.object(
                rbac,
                "resolve_accessible_tenant_ids_with_expiry",
                wraps=rbac.resolve_accessible_tenant_ids_with_expiry,
            ) as resolver,
        ):
            accessible_tenant_ids(self.member)
            accessible_tenant_ids(self.member)
        self.assertEqual(resolver.call_count, 2)

    def test_membership_change_invalidates_warmed_accessible_set(self):
        from organization.access import accessible_tenant_ids

        self.assertIn(self.cust_a.pk, accessible_tenant_ids(self.member))
        membership = Membership._base_manager.get(
            user=self.member,
            tenant=self.cust_a,
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        self.assertNotIn(self.cust_a.pk, accessible_tenant_ids(self.member))

    def test_role_grant_delete_invalidates_warmed_accessible_set(self):
        from organization.access import accessible_tenant_ids

        self.assertIn(self.cust_b.pk, accessible_tenant_ids(self.member))
        self.managed_grant.delete()
        self.assertNotIn(self.cust_b.pk, accessible_tenant_ids(self.member))

    def test_role_grant_scope_delete_invalidates_warmed_accessible_set(self):
        from organization.access import accessible_tenant_ids

        self.assertIn(self.cust_b.pk, accessible_tenant_ids(self.member))
        self.managed_grant.scopes.get(tenant=self.cust_b).delete()
        self.assertNotIn(self.cust_b.pk, accessible_tenant_ids(self.member))

    def test_group_membership_delete_invalidates_warmed_accessible_set(self):
        from organization.access import accessible_tenant_ids

        self.assertIn(self.cust_c.pk, accessible_tenant_ids(self.member))
        GroupMembership.objects.get(
            membership__user=self.member,
            user_group__slug="i29a-team",
        ).delete()
        self.assertNotIn(self.cust_c.pk, accessible_tenant_ids(self.member))

    def test_tenant_soft_delete_invalidates_warmed_accessible_set(self):
        from organization.access import accessible_tenant_ids

        self.assertIn(self.cust_b.pk, accessible_tenant_ids(self.member))
        self.cust_b.deleted_at = timezone.now()
        self.cust_b.save(update_fields=["deleted_at"])
        self.assertNotIn(self.cust_b.pk, accessible_tenant_ids(self.member))

    def test_contradictory_tenant_and_all_accessible_scope_fails_closed(self):
        _current_user.set(self.member)
        set_current_tenant(self.cust_a)
        set_current_all_accessible(True)
        self.assertTrue(get_current_all_accessible())
        self.assertEqual(Tenant.objects.for_scope(Scope.current()).count(), 0)


class AllAccessibleAmbientPermTests(TestCase):
    """Under the all-accessible scope the ambient permission gate (list/add/nav
    checks with no object) must aggregate across every accessible tenant, exactly
    like the tenant-group union — otherwise a page 403s because the user's first
    membership tenant happens to carry no permissions."""

    def setUp(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        self.provider = Tenant.objects.create(
            name="I29P Provider",
            slug="i29p-p",
            is_provider=True,
        )
        self.cust = Tenant.objects.create(
            name="I29P Cust",
            slug="i29p-c",
            managed_by=self.provider,
        )
        # The permission is conveyed ONLY to the managed customer, not to the
        # member's own (first) membership tenant, the provider.
        self.role = Role.objects.create(
            tenant=self.provider,
            name="Asset viewer",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "assets.change_asset",
                "assets.delete_asset",
                "assets.checkin_asset",
            ],
        )
        self.member = User.objects.create_user(username="i29p-member", password="pw")
        grant(
            self.member,
            self.provider,
            self.role,
            reach=RoleGrant.REACH_MANAGED,
            managed_scope=RoleGrantScope.SCOPE_TENANT,
            assigned_tenants=[self.cust],
        )

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)

    def _activate_all_accessible(self):
        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(True)

    def test_ambient_module_perm_aggregates_across_accessible_tenants(self):
        # Sanity: the perm is NOT held in the provider (the first membership).
        self.assertFalse(self.member.has_perm("assets.view_asset", obj=self.provider))
        self.assertTrue(self.member.has_perm("assets.view_asset", obj=self.cust))
        self._activate_all_accessible()
        self.assertTrue(self.member.has_module_perms("assets"))

    def test_ambient_object_less_perm_aggregates_across_accessible_tenants(self):
        self._activate_all_accessible()
        self.assertTrue(self.member.has_perm("assets.view_asset"))

    def test_aggregate_scope_allows_tenant_selecting_create_only(self):
        self._activate_all_accessible()
        # Create is safe at the ambient gate because the form must select a
        # concrete tenant and form_valid re-checks add permission on that tenant.
        self.assertTrue(self.member.has_perm("assets.add_asset"))
        # Other mutations remain object-bound; an aggregate ambient check must
        # not authorize an unanchored change/delete/transaction operation.
        self.assertFalse(self.member.has_perm("assets.change_asset"))
        self.assertFalse(self.member.has_perm("assets.delete_asset"))
        self.assertFalse(self.member.has_perm("assets.checkin_asset"))
        # Explicit object checks remain tenant-anchored and therefore safe.
        self.assertTrue(self.member.has_perm("assets.change_asset", obj=self.cust))

    def test_ambient_gate_still_fails_closed_for_unheld_permission(self):
        self._activate_all_accessible()
        self.assertFalse(self.member.has_perm("assets.unheld_asset"))
        self.assertFalse(self.member.has_module_perms("licenses"))


class AllAccessibleMiddlewareTests(TestCase):
    """Session/query switching into and out of the all-accessible scope."""

    def setUp(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        self.factory = RequestFactory()
        self.group = TenantGroup.objects.create(name="M Group", slug="i29m-g")
        self.provider = Tenant.objects.create(
            name="I29M Provider",
            slug="i29m-p",
            is_provider=True,
        )
        self.cust = Tenant.objects.create(
            name="I29M Cust",
            slug="i29m-c",
            managed_by=self.provider,
            group=self.group,
        )
        self.role = Role.objects.create(tenant=self.provider, name="Tech", permissions=[])
        self.member = User.objects.create_user(username="i29m-member", password="pw")
        grant(
            self.member,
            self.provider,
            self.role,
            reach=RoleGrant.REACH_MANAGED,
            managed_scope=RoleGrantScope.SCOPE_TENANT,
            assigned_tenants=[self.cust],
        )
        self.superuser = User.objects.create_superuser(
            username="i29m-su",
            email="i29m-su@x.com",
            password="pw",
        )

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)

    def _request(self, user, query="", session=None):
        from importlib import import_module

        from django.conf import settings

        store = import_module(settings.SESSION_ENGINE).SessionStore
        request = self.factory.get("/" + query)
        request.user = user
        request.session = store()
        if session:
            for key, value in session.items():
                request.session[key] = value
        return request

    def _run(self, user, query="", session=None):
        from itambox.middleware import CurrentUserMiddleware, TenantMiddleware

        request = self._request(user, query, session)
        cu = CurrentUserMiddleware(get_response=lambda r: None)
        cu.process_request(request)
        TenantMiddleware(get_response=lambda r: None).process_request(request)
        return request

    def test_switch_all_accessible_query_param_activates_scope(self):
        request = self._run(self.member, "?switch_all_accessible=1")
        self.assertTrue(getattr(request, "active_all_accessible", False))
        self.assertIsNone(request.active_tenant)
        self.assertIsNone(request.active_tenant_group)
        from core.managers import get_current_all_accessible

        self.assertTrue(get_current_all_accessible())
        self.assertTrue(request.session.get("active_all_accessible"))
        self.assertNotIn("active_tenant_id", request.session)

    def test_all_accessible_persists_via_session(self):
        request = self._run(self.member, session={"active_all_accessible": True})
        self.assertTrue(getattr(request, "active_all_accessible", False))
        self.assertIsNone(request.active_tenant)

    def test_switch_single_tenant_clears_all_accessible(self):
        request = self._run(
            self.member,
            f"?switch_tenant={self.cust.pk}",
            session={"active_all_accessible": True},
        )
        self.assertFalse(getattr(request, "active_all_accessible", False))
        self.assertEqual(request.active_tenant, self.cust)
        self.assertNotIn("active_all_accessible", request.session)

    def test_switch_group_clears_all_accessible(self):
        request = self._run(
            self.member,
            f"?switch_tenant_group={self.group.pk}",
            session={"active_all_accessible": True},
        )
        self.assertFalse(getattr(request, "active_all_accessible", False))
        self.assertEqual(request.active_tenant_group, self.group)
        self.assertNotIn("active_all_accessible", request.session)

    def test_all_accessible_fail_closed_when_no_accessible_tenants(self):
        stranger = User.objects.create_user(username="i29m-stranger", password="pw")
        request = self._run(stranger, "?switch_all_accessible=1")
        # No accessible tenants: the scope is refused (fail closed), never global.
        self.assertFalse(getattr(request, "active_all_accessible", False))
        self.assertIsNone(request.active_tenant)
        self.assertIsNone(request.active_tenant_group)

    def test_superuser_switch_all_accessible_stays_global(self):
        request = self._run(self.superuser, "?switch_all_accessible=1")
        self.assertFalse(getattr(request, "active_all_accessible", False))
        self.assertIsNone(request.active_tenant)
        self.assertIsNone(request.active_tenant_group)

    def test_superuser_live_workspace_selections_still_work(self):
        tenant_request = self._run(self.superuser, f"?switch_tenant={self.cust.pk}")
        self.assertEqual(tenant_request.active_tenant, self.cust)
        self.assertIsNone(tenant_request.active_tenant_group)

        group_request = self._run(self.superuser, f"?switch_tenant_group={self.group.pk}")
        self.assertIsNone(group_request.active_tenant)
        self.assertEqual(group_request.active_tenant_group, self.group)

    def test_archived_tenant_session_pin_is_cleared_for_superuser(self):
        Tenant._base_manager.filter(pk=self.cust.pk).update(deleted_at=timezone.now())

        request = self._run(self.superuser, session={"active_tenant_id": self.cust.pk})

        self.assertIsNone(request.active_tenant)
        self.assertNotIn("active_tenant_id", request.session)

    def test_archived_group_session_pin_is_cleared_for_superuser(self):
        TenantGroup._base_manager.filter(pk=self.group.pk).update(deleted_at=timezone.now())

        request = self._run(self.superuser, session={"active_tenant_group_id": self.group.pk})

        self.assertIsNone(request.active_tenant_group)
        self.assertNotIn("active_tenant_group_id", request.session)

    def test_switch_to_archived_tenant_is_not_persisted_for_superuser(self):
        Tenant._base_manager.filter(pk=self.cust.pk).update(deleted_at=timezone.now())

        request = self._run(self.superuser, f"?switch_tenant={self.cust.pk}")

        self.assertIsNone(request.active_tenant)
        self.assertNotIn("active_tenant_id", request.session)

    def test_switch_to_archived_group_is_not_persisted_for_superuser(self):
        TenantGroup._base_manager.filter(pk=self.group.pk).update(deleted_at=timezone.now())

        request = self._run(self.superuser, f"?switch_tenant_group={self.group.pk}")

        self.assertIsNone(request.active_tenant_group)
        self.assertNotIn("active_tenant_group_id", request.session)

    def test_response_restores_all_scope_contextvars(self):
        from itambox.middleware import CurrentUserMiddleware, TenantMiddleware

        request = self._request(
            self.member,
            session={"active_all_accessible": True},
        )
        observed = {}

        def endpoint(inner_request):
            observed["all_accessible"] = get_current_all_accessible()
            observed["tenant"] = get_current_tenant()
            observed["group"] = get_current_tenant_group()
            observed["user"] = _current_user.get()
            return "ok"

        app = CurrentUserMiddleware(TenantMiddleware(endpoint))
        self.assertEqual(app(request), "ok")
        self.assertEqual(
            observed,
            {
                "all_accessible": True,
                "tenant": None,
                "group": None,
                "user": self.member,
            },
        )
        self.assertFalse(get_current_all_accessible())
        self.assertIsNone(get_current_tenant())
        self.assertIsNone(get_current_tenant_group())
        self.assertIsNone(_current_user.get())

    def test_exception_response_restores_all_scope_contextvars(self):
        from django.core.handlers.exception import convert_exception_to_response

        from itambox.middleware import CurrentUserMiddleware, TenantMiddleware

        request = self._request(
            self.member,
            session={"active_all_accessible": True},
        )

        def endpoint(inner_request):
            self.assertTrue(get_current_all_accessible())
            raise RuntimeError("endpoint failed")

        app = CurrentUserMiddleware(
            TenantMiddleware(convert_exception_to_response(endpoint)),
        )
        response = app(request)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(get_current_all_accessible())
        self.assertIsNone(get_current_tenant())
        self.assertIsNone(get_current_tenant_group())
        self.assertIsNone(_current_user.get())

    def test_task_context_clears_then_restores_all_accessible_scope(self):
        from core.tasks.context import TaskContext

        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(True)

        with TaskContext(tenant_id=self.cust.pk, user_id=self.member.pk):
            self.assertFalse(get_current_all_accessible())
            self.assertEqual(get_current_tenant(), self.cust)
            self.assertIsNone(get_current_tenant_group())

        self.assertTrue(get_current_all_accessible())
        self.assertIsNone(get_current_tenant())
        self.assertIsNone(get_current_tenant_group())
        self.assertEqual(_current_user.get(), self.member)

    def test_tenantless_task_context_can_bind_all_accessible_scope_and_restore_it(self):
        from core.tasks.context import TaskContext

        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)

        with TaskContext(tenant_id=None, user_id=self.member.pk, all_accessible=True):
            self.assertIsNone(get_current_tenant())
            self.assertIsNone(get_current_tenant_group())
            self.assertIsNone(get_current_membership())
            self.assertTrue(get_current_all_accessible())
            self.assertEqual(_current_user.get(), self.member)

        self.assertFalse(get_current_all_accessible())
        self.assertIsNone(get_current_tenant())
        self.assertIsNone(get_current_tenant_group())
        self.assertIsNone(get_current_membership())
        self.assertEqual(_current_user.get(), self.member)

    def test_tenant_bound_task_context_never_combines_tenant_and_all_accessible_scope(self):
        from core.tasks.context import TaskContext

        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(True)

        with TaskContext(tenant_id=self.cust.pk, user_id=self.member.pk, all_accessible=True):
            self.assertEqual(get_current_tenant(), self.cust)
            self.assertFalse(get_current_all_accessible())

        self.assertTrue(get_current_all_accessible())
        self.assertIsNone(get_current_tenant())

    def test_task_context_does_not_inherit_membership_from_another_tenant(self):
        from core.tasks.context import TaskContext

        outer_membership = Membership._base_manager.get(
            user=self.member,
            tenant=self.provider,
        )
        _current_user.set(self.member)
        set_current_tenant(self.provider)
        set_current_membership(outer_membership)

        # self.member reaches the customer through a managed grant but has no
        # direct customer membership. The task must resolve the cross-tenant
        # target unscoped and must not carry the provider membership into it.
        with TaskContext(tenant_id=self.cust.pk, user_id=self.member.pk):
            self.assertEqual(get_current_tenant(), self.cust)
            self.assertIsNone(get_current_membership())
            self.assertEqual(_current_user.get(), self.member)

        self.assertEqual(get_current_tenant(), self.provider)
        self.assertEqual(get_current_membership(), outer_membership)

    def test_task_context_never_binds_inactive_membership(self):
        from core.tasks.context import TaskContext

        Membership._base_manager.create(
            user=self.member,
            tenant=self.cust,
            is_active=False,
        )
        with TaskContext(tenant_id=self.cust.pk, user_id=self.member.pk):
            self.assertEqual(get_current_tenant(), self.cust)
            self.assertIsNone(get_current_membership())

    def test_global_task_context_clears_then_restores_wrapping_scope(self):
        from core.tasks.context import TaskContext

        outer_membership = Membership._base_manager.get(
            user=self.member,
            tenant=self.provider,
        )
        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(self.group)
        set_current_membership(outer_membership)
        set_current_all_accessible(False)

        with TaskContext(tenant_id=None, user_id=None):
            self.assertIsNone(get_current_tenant())
            self.assertIsNone(get_current_tenant_group())
            self.assertIsNone(get_current_membership())
            self.assertFalse(get_current_all_accessible())
            self.assertIsNone(_current_user.get())

        self.assertIsNone(get_current_tenant())
        self.assertEqual(get_current_tenant_group(), self.group)
        self.assertEqual(get_current_membership(), outer_membership)
        self.assertEqual(_current_user.get(), self.member)

    def test_task_context_enter_failure_restores_every_ambient_context(self):
        from core.tasks.context import TaskContext
        from itambox.middleware import _request_id

        outer_membership = Membership._base_manager.get(
            user=self.member,
            tenant=self.provider,
        )
        previous_request_id = _request_id.get()
        self.addCleanup(_request_id.set, previous_request_id)
        request_marker = object()
        _request_id.set(request_marker)
        _current_user.set(self.member)
        set_current_tenant(self.provider)
        set_current_tenant_group(self.group)
        set_current_membership(outer_membership)
        set_current_all_accessible(True)

        with (
            mock.patch.object(
                Membership._base_manager,
                "filter",
                side_effect=RuntimeError("membership-db-failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "membership-db-failure"),
        ):
            with TaskContext(tenant_id=self.cust.pk, user_id=self.member.pk):
                self.fail("failed task initialization must not enter the body")

        self.assertIs(_request_id.get(), request_marker)
        self.assertEqual(_current_user.get(), self.member)
        self.assertEqual(get_current_tenant(), self.provider)
        self.assertEqual(get_current_tenant_group(), self.group)
        self.assertEqual(get_current_membership(), outer_membership)
        self.assertTrue(get_current_all_accessible())

    def test_task_context_resolves_target_outside_outer_users_scope(self):
        from core.tasks.context import TaskContext

        target = Tenant._base_manager.create(
            name="Task target",
            slug="i29-task-target",
        )
        task_role = Role.objects.create(
            tenant=target,
            name="Task role",
            permissions=[],
        )
        task_user = User.objects.create_user(
            username="i29-task-user",
            password="pw",
        )
        grant(task_user, target, task_role)

        _current_user.set(self.member)
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(True)

        with TaskContext(tenant_id=target.pk, user_id=task_user.pk):
            self.assertEqual(get_current_tenant(), target)
            self.assertEqual(_current_user.get(), task_user)
            self.assertFalse(get_current_all_accessible())

    def test_task_context_rejects_invalid_tenant_and_restores_outer_scope(self):
        from core.tasks.context import TaskContext

        _current_user.set(self.member)
        set_current_all_accessible(True)
        missing_pk = Tenant._base_manager.order_by("-pk").first().pk + 1000

        with self.assertRaises(Tenant.DoesNotExist):
            with TaskContext(tenant_id=missing_pk, user_id=self.member.pk):
                self.fail("invalid task tenant must not enter the task body")

        self.assertTrue(get_current_all_accessible())
        self.assertEqual(_current_user.get(), self.member)

    def test_task_context_rejects_user_without_target_access(self):
        from core.tasks.context import TaskContext

        stranger = User.objects.create_user(
            username="i29-task-stranger",
            password="pw",
        )
        with self.assertRaises(PermissionDenied):
            with TaskContext(tenant_id=self.cust.pk, user_id=stranger.pk):
                self.fail("unauthorized task principal must not enter the task body")


class AllAccessibleSelectorUITests(TestCase):
    """The workspace switcher offers non-superusers an "All Tenants" entry for
    the all-accessible scope, marks it active under the scope, and never shows it
    to superusers (who keep the distinct "Global View" entry) or to users with no
    accessible tenants."""

    def setUp(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        self.factory = RequestFactory()
        self.tenant_a = Tenant.objects.create(name="UI A", slug="i29ui-a")
        self.role_a = Role.objects.create(tenant=self.tenant_a, name="R", permissions=[])
        self.member = User.objects.create_user(username="i29ui-member", password="pw")
        grant(self.member, self.tenant_a, self.role_a)  # direct membership
        self.superuser = User.objects.create_superuser(
            username="i29ui-su",
            email="i29ui-su@x.com",
            password="pw",
        )
        self.no_access = User.objects.create_user(username="i29ui-none", password="pw")

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)

    def _render(self, user, active_all_accessible=False, active_tenant=None):
        # inline import: template loader only needed for the UI probe.
        from django.template.loader import render_to_string

        request = self.factory.get("/")
        request.user = user
        request.active_tenant = active_tenant
        request.active_tenant_group = None
        request.active_all_accessible = active_all_accessible
        _current_user.set(user)
        return render_to_string(
            "global_includes/_tenant_switcher_list.html",
            request=request,
        )

    @staticmethod
    def _all_accessible_line(html):
        """Return the all-accessible link's opening tag, joining wrapped lines."""
        lines = html.splitlines()
        for index, line in enumerate(lines):
            if "switch_all_accessible" in line:
                parts = [line]
                while ">" not in parts[-1] and index + 1 < len(lines):
                    index += 1
                    parts.append(lines[index])
                return " ".join(part.strip() for part in parts)
        return None

    def test_non_superuser_sees_all_accessible_link(self):
        html = self._render(self.member)
        self.assertIsNotNone(self._all_accessible_line(html))
        self.assertIn("All Tenants", html)
        self.assertNotIn("Global View", html)

    def test_all_accessible_link_marked_active_under_scope(self):
        line = self._all_accessible_line(
            self._render(self.member, active_all_accessible=True),
        )
        self.assertIsNotNone(line)
        self.assertIn("active", line)

    def test_all_accessible_link_not_active_when_single_tenant_selected(self):
        line = self._all_accessible_line(
            self._render(self.member, active_tenant=self.tenant_a),
        )
        self.assertIsNotNone(line)
        self.assertNotIn("active", line)

    def test_superuser_has_no_member_all_accessible_link(self):
        html = self._render(self.superuser)
        # Superusers keep the distinct global entry, never the member scope link.
        self.assertIsNone(self._all_accessible_line(html))

    def test_superuser_global_entry_uses_global_view_label(self):
        html = self._render(self.superuser)
        # Issue #499: the platform-wide scope has its own label; the member
        # label stays reserved for the accessible-tenant scope.
        self.assertIn("Global View", html)
        self.assertNotIn("All Tenants", html)
        self.assertIn("switch_tenant=", html)

    def test_superuser_global_view_label_is_translated(self):
        with translation.override("de"):
            html = self._render(self.superuser)
        self.assertIn("Globale Ansicht", html)

    def test_user_without_access_has_no_all_accessible_link(self):
        html = self._render(self.no_access)
        self.assertIsNone(self._all_accessible_line(html))


class GroupScopeLabelTests(TestCase):
    """Issue #499: a tenant-group scope names the group in the visible label and
    in the visually hidden label that screen readers and automation consume."""

    def setUp(self):
        self.group = TenantGroup.objects.create(name="Label Group", slug="label-group")
        self.superuser = User.objects.create_superuser(
            username="i499-label-su",
            email="i499-label-su@example.com",
            password="pw",
        )

    def test_group_scope_labels_the_group_in_both_labels(self):
        self.client.force_login(self.superuser)
        session = self.client.session
        session["active_tenant_group_id"] = self.group.pk
        session.save()

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        hidden = re.search(r'data-testid="active-tenant"[^>]*>(.*?)</span>', response.content.decode(), re.S)
        self.assertIsNotNone(hidden)
        self.assertIn("Label Group", hidden.group(1))
        self.assertNotIn("Global View", hidden.group(1))
