"""Behavior and query locks for the tenant-switcher context processor."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.functional import SimpleLazyObject

from core.tasks.context import TaskContext
from core.tests.mixins import grant
from organization.models import Role, RoleGrant, RoleGrantScope, Tenant, TenantGroup
from organization.services.tenant_archive import archive_tenant
from organization.views.context_processors import tenant_switcher_processor

User = get_user_model()

SWITCHER_KEYS = {
    "all_tenants_switcher",
    "grouped_tenants_switcher",
    "own_tenants_switcher",
    "grouped_managed_tenants_switcher",
    "scope_switch",
}


def _request(user):
    return SimpleNamespace(user=user)


class TenantSwitcherContextProcessorTests(TestCase):
    def test_anonymous_context_has_exact_empty_shape(self):
        context = tenant_switcher_processor(_request(AnonymousUser()))

        self.assertEqual(set(context), SWITCHER_KEYS)
        self.assertEqual(context, {key: [] for key in SWITCHER_KEYS - {"scope_switch"}} | {"scope_switch": {}})

    def test_authenticated_values_are_lazy_and_processor_is_zero_query(self):
        user = User.objects.create_user(username="lazy-switcher-user")

        with CaptureQueriesContext(connection) as queries:
            context = tenant_switcher_processor(_request(user))

        self.assertEqual(len(queries), 0)
        self.assertEqual(set(context), SWITCHER_KEYS)
        self.assertTrue(all(isinstance(value, SimpleLazyObject) for value in context.values()))

    def test_superuser_global_and_grouped_ordering(self):
        alpha = TenantGroup.objects.create(name="alpha Group", slug="alpha-group")
        zulu = TenantGroup.objects.create(name="Zulu Group", slug="zulu-group")
        alpha_b = Tenant.objects.create(name="Beta", slug="beta", group=alpha)
        alpha_a = Tenant.objects.create(name="Alpha", slug="alpha", group=alpha)
        zulu_tenant = Tenant.objects.create(name="Zulu", slug="zulu", group=zulu)
        ungrouped = Tenant.objects.create(name="Ungrouped", slug="ungrouped")
        superuser = User.objects.create_superuser(username="switcher-root", email="root@example.com", password="pw")

        context = tenant_switcher_processor(_request(superuser))

        self.assertEqual(
            [tenant.pk for tenant in context["all_tenants_switcher"]],
            [alpha_a.pk, alpha_b.pk, ungrouped.pk, zulu_tenant.pk],
        )
        self.assertEqual(
            [
                (item["group"].pk if item["group"] else None, [tenant.pk for tenant in item["tenants"]])
                for item in context["grouped_tenants_switcher"]
            ],
            [
                (alpha.pk, [alpha_a.pk, alpha_b.pk]),
                (zulu.pk, [zulu_tenant.pk]),
                (None, [ungrouped.pk]),
            ],
        )
        self.assertEqual(list(context["own_tenants_switcher"]), [])
        self.assertEqual(list(context["grouped_managed_tenants_switcher"]), [])

    def test_direct_and_managed_tenants_remain_distinct_ordered_and_grouped(self):
        alpha = TenantGroup.objects.create(name="alpha Group", slug="member-alpha-group")
        zulu = TenantGroup.objects.create(name="Zulu Group", slug="member-zulu-group")
        provider = Tenant.objects.create(name="Zulu Provider", slug="member-provider", is_provider=True)
        direct = Tenant.objects.create(name="Alpha Direct", slug="member-direct", group=zulu)
        inactive = Tenant.objects.create(name="Inactive Direct", slug="inactive-direct")
        deleted = Tenant.objects.create(name="Deleted Direct", slug="deleted-direct")
        managed_alpha = Tenant.objects.create(
            name="Managed Alpha",
            slug="managed-alpha",
            managed_by=provider,
            group=alpha,
        )
        managed_zulu = Tenant.objects.create(
            name="Managed Zulu",
            slug="managed-zulu",
            managed_by=provider,
            group=zulu,
        )
        unrelated = Tenant.objects.create(name="Unrelated", slug="switcher-unrelated", group=alpha)
        user = User.objects.create_user(username="member-switcher-user")
        for tenant in (provider, direct, deleted):
            role = Role.objects.create(tenant=tenant, name=f"Own Role {tenant.pk}", permissions=[])
            grant(user, tenant, role)
        with TaskContext(operation="test.tenant_switcher.soft_delete_tenant"):
            archive_tenant(deleted)
        inactive_role = Role.objects.create(tenant=inactive, name="Inactive Role", permissions=[])
        inactive_grant = grant(user, inactive, inactive_role)
        inactive_grant.membership.is_active = False
        inactive_grant.membership.save(update_fields=["is_active"])
        managed_role = Role.objects.create(tenant=provider, name="Managed Role", permissions=[])
        grant(
            user,
            provider,
            managed_role,
            reach=RoleGrant.REACH_MANAGED,
            managed_scope=RoleGrantScope.SCOPE_ALL_MANAGED,
        )

        context = tenant_switcher_processor(_request(user))

        self.assertEqual(
            [tenant.pk for tenant in context["own_tenants_switcher"]],
            [provider.pk, direct.pk],
        )
        self.assertEqual(
            [
                (item["group"].pk if item["group"] else None, [tenant.pk for tenant in item["tenants"]])
                for item in context["grouped_managed_tenants_switcher"]
            ],
            [
                (alpha.pk, [managed_alpha.pk]),
                (zulu.pk, [managed_zulu.pk]),
            ],
        )
        visible_ids = {tenant.pk for item in context["grouped_managed_tenants_switcher"] for tenant in item["tenants"]}
        self.assertNotIn(inactive.pk, visible_ids)
        self.assertNotIn(deleted.pk, visible_ids)
        self.assertNotIn(unrelated.pk, visible_ids)

    def test_member_switcher_base_query_count(self):
        provider = Tenant.objects.create(name="Query Provider", slug="query-provider", is_provider=True)
        customer = Tenant.objects.create(
            name="Query Customer",
            slug="query-customer",
            managed_by=provider,
        )
        user = User.objects.create_user(username="query-switcher-user")
        role = Role.objects.create(tenant=provider, name="Query Role", permissions=[])
        grant(
            user,
            provider,
            role,
            reach=RoleGrant.REACH_MANAGED,
            managed_scope=RoleGrantScope.SCOPE_ALL_MANAGED,
        )
        context = tenant_switcher_processor(_request(user))

        with CaptureQueriesContext(connection) as queries:
            own = list(context["own_tenants_switcher"])
            managed = list(context["grouped_managed_tenants_switcher"])

        self.assertEqual([tenant.pk for tenant in own], [provider.pk])
        self.assertEqual(
            [tenant.pk for item in managed for tenant in item["tenants"]],
            [customer.pk],
        )
        self.assertEqual(len(queries), 8)


class TenantSwitcherLazyEvaluationTests(TestCase):
    """Evaluating the lazy switcher values for a plain user reaches the empty branches."""

    def test_regular_user_lazy_values_evaluate_to_exact_empty_lists(self):
        user = User.objects.create_user(username="plain-switcher-user")

        context = tenant_switcher_processor(_request(user))

        for key in SWITCHER_KEYS - {"scope_switch"}:
            self.assertEqual(list(context[key]), [])
