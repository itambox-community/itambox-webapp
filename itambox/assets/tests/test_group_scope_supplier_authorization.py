"""Group scope must derive group-scoped rows from the permission-narrowed tenant
set, not from every descendant of the selected group (#1088).

A caller with ``assets.view_supplier`` only in tenant A (subgroup GA) must not
see a supplier shared with the sibling subgroup GB, and the same queryset is what
the bulk update/destroy routes filter by primary key.
"""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from assets.models import Manufacturer, Supplier
from core.graphql_utils import scoped_relation
from core.managers import (
    Scope,
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.schema import schema
from core.tests.mixins import grant
from itambox.middleware import _current_user
from licenses.models import License
from organization.models import Membership, Role, Tenant, TenantGroup
from software.models import Software

User = get_user_model()


class GroupScopeSupplierAuthorizationTests(TestCase):
    def setUp(self):
        self.root = TenantGroup.objects.create(name="GS Root", slug="gs-root")
        self.group_a = TenantGroup.objects.create(name="GS A", slug="gs-a", parent=self.root)
        self.group_b = TenantGroup.objects.create(name="GS B", slug="gs-b", parent=self.root)
        self.tenant_a = Tenant.objects.create(name="GS Tenant A", slug="gs-tenant-a", group=self.group_a)
        self.tenant_b = Tenant.objects.create(name="GS Tenant B", slug="gs-tenant-b", group=self.group_b)

        self.user = User.objects.create_user("gs_user", "gs@example.com", "pw")
        role = Role.objects.create(
            tenant=self.tenant_a,
            name="GS Role A",
            permissions=["assets.view_supplier", "assets.change_supplier"],
        )
        grant(self.user, self.tenant_a, role)
        # Reach into B without any grant there.
        Membership.objects.create(user=self.user, tenant=self.tenant_b)

        self.supplier_a = Supplier.objects.create(name="GS Supplier A", slug="gs-supplier-a", tenant=self.tenant_a)
        self.supplier_b = Supplier.objects.create(
            name="GS Supplier GB", slug="gs-supplier-gb", tenant_group=self.group_b
        )
        self.supplier_root = Supplier.objects.create(
            name="GS Supplier Root", slug="gs-supplier-root", tenant_group=self.root
        )
        self.supplier_global = Supplier.objects.create(name="GS Supplier Global", slug="gs-supplier-global")

        self.client.force_login(self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_all_accessible", None)
        session["active_tenant_group_id"] = self.root.pk
        session.save()

    def tearDown(self):
        set_current_tenant_group(None)

    def test_group_scope_list_hides_sibling_subgroup_supplier(self):
        response = self.client.get(reverse("api:assets_api:supplier-list") + "?limit=100")
        self.assertEqual(response.status_code, 200, response.content[:400])
        names = {row["name"] for row in response.json()["results"]}
        self.assertIn("GS Supplier A", names)
        self.assertNotIn("GS Supplier GB", names)

    def test_for_scope_derives_group_rows_from_authorized_tenants(self):
        set_current_tenant_group(self.root)
        user_token = _current_user.set(self.user)
        try:
            ids = set(
                Supplier.objects.for_scope(Scope.current(), required_permission="assets.view_supplier").values_list(
                    "pk", flat=True
                )
            )
        finally:
            _current_user.reset(user_token)
            set_current_tenant_group(None)
        self.assertIn(self.supplier_a.pk, ids)
        self.assertNotIn(self.supplier_b.pk, ids)

    def test_write_scope_requires_permission_in_every_affected_tenant(self):
        set_current_tenant_group(self.root)
        user_token = _current_user.set(self.user)
        try:
            read_ids = set(
                Supplier.objects.for_scope(Scope.current(), required_permission="assets.change_supplier").values_list(
                    "pk", flat=True
                )
            )
            write_ids = set(
                Supplier.objects.for_scope(
                    Scope.current(),
                    required_permission="assets.change_supplier",
                    group_scope="all",
                ).values_list("pk", flat=True)
            )
        finally:
            _current_user.reset(user_token)
            set_current_tenant_group(None)
        # The root-shared supplier is visible from A, but updating it affects
        # tenant B as well -- bulk writes must exclude it.
        self.assertIn(self.supplier_root.pk, read_ids)
        self.assertIn(self.supplier_global.pk, read_ids)
        self.assertNotIn(self.supplier_root.pk, write_ids)
        self.assertNotIn(self.supplier_global.pk, write_ids)
        self.assertIn(self.supplier_a.pk, write_ids)


class TenantScopeGroupRowTests(TestCase):
    def setUp(self):
        self.parent = TenantGroup.objects.create(name="TS Parent", slug="ts-parent")
        self.child = TenantGroup.objects.create(name="TS Child", slug="ts-child", parent=self.parent)
        self.tenant_parent = Tenant.objects.create(name="TS Tenant P", slug="ts-tenant-p", group=self.parent)
        self.tenant_child = Tenant.objects.create(name="TS Tenant C", slug="ts-tenant-c", group=self.child)
        self.user = User.objects.create_user("ts_user", "ts@example.com", "pw")
        role = Role.objects.create(tenant=self.tenant_parent, name="TS Role P", permissions=["assets.view_supplier"])
        grant(self.user, self.tenant_parent, role)
        Membership.objects.create(user=self.user, tenant=self.tenant_child)
        self.child_supplier = Supplier.objects.create(
            name="TS Child Supplier", slug="ts-child-supplier", tenant_group=self.child
        )

    def test_tenant_scope_does_not_see_descendant_group_rows(self):
        set_current_tenant(self.tenant_parent)
        user_token = _current_user.set(self.user)
        try:
            ids = set(
                Supplier.objects.for_scope(Scope.current(), required_permission="assets.view_supplier").values_list(
                    "pk", flat=True
                )
            )
        finally:
            _current_user.reset(user_token)
            set_current_tenant(None)
        self.assertNotIn(self.child_supplier.pk, ids)


class GroupScopedSupplierRelationAuthorizationTests(TestCase):
    """``scoped_relation()`` must hide a group-scoped supplier the list hides too (#1100).

    Mirrors ``GroupScopeSupplierAuthorizationTests``: a root group with two
    sibling subgroups GA/GB, a user authorized for ``assets.view_supplier``
    only in GA's tenant, and a supplier scoped to GB. Under the ambient
    ``has_perm(obj=...)`` check the old ``scoped_relation`` evaluated the
    permission union across the whole active (root) group scope, so holding
    the permission in GA wrongly authorized a supplier anchored to the
    sibling GB. The fix checks membership in the same ``for_scope`` queryset
    the supplier list uses, which already derives group-scoped rows from the
    permission-narrowed tenant set.
    """

    def setUp(self):
        self.root = TenantGroup.objects.create(name="GSR Root", slug="gsr-root")
        self.group_a = TenantGroup.objects.create(name="GSR A", slug="gsr-a", parent=self.root)
        self.group_b = TenantGroup.objects.create(name="GSR B", slug="gsr-b", parent=self.root)
        self.tenant_a = Tenant.objects.create(name="GSR Tenant A", slug="gsr-tenant-a", group=self.group_a)
        self.tenant_b = Tenant.objects.create(name="GSR Tenant B", slug="gsr-tenant-b", group=self.group_b)

        self.user = User.objects.create_user("gsr_user", "gsr@example.com", "pw")
        role = Role.objects.create(
            tenant=self.tenant_a,
            name="GSR Role A",
            permissions=["assets.view_supplier", "licenses.view_license"],
        )
        grant(self.user, self.tenant_a, role)
        # Reach into B (and thus into the root group scope) without a grant there.
        Membership.objects.create(user=self.user, tenant=self.tenant_b)

        self.manufacturer = Manufacturer.objects.create(name="GSR Mfr", slug="gsr-mfr")
        self.software = Software.objects.create(
            name="GSR Software", manufacturer=self.manufacturer, tenant=self.tenant_a
        )

        self.supplier_group_b = Supplier.objects.create(
            name="GSR Supplier GB", slug="gsr-supplier-gb", tenant_group=self.group_b
        )
        self.supplier_tenant_a = Supplier.objects.create(
            name="GSR Supplier A", slug="gsr-supplier-a", tenant=self.tenant_a
        )
        self.supplier_global = Supplier.objects.create(name="GSR Supplier Global", slug="gsr-supplier-global")

        self.license_leak = License.objects.create(
            name="GSR License Leak",
            software=self.software,
            supplier=self.supplier_group_b,
            seats=1,
            tenant=self.tenant_a,
        )
        self.license_own = License.objects.create(
            name="GSR License Own",
            software=self.software,
            supplier=self.supplier_tenant_a,
            seats=1,
            tenant=self.tenant_a,
        )
        self.license_global = License.objects.create(
            name="GSR License Global",
            software=self.software,
            supplier=self.supplier_global,
            seats=1,
            tenant=self.tenant_a,
        )

        self.factory = RequestFactory()

    def tearDown(self):
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_all_accessible(False)
        set_current_membership(None)
        _current_user.set(None)

    def _graphql_context_group(self, user=None):
        user = user or self.user
        _current_user.set(user)
        set_current_tenant(None)
        set_current_all_accessible(False)
        set_current_tenant_group(self.root)
        set_current_membership(None)
        request = self.factory.post("/graphql")
        request.user = user
        request.active_tenant = None
        return request

    def test_group_scoped_supplier_hidden_through_relation_matches_list(self):
        ctx = self._graphql_context_group()
        result = schema.execute_sync(
            f"{{ license(id: {self.license_leak.pk}) {{ supplier {{ id }} }} }}",
            context_value=ctx,
        )
        self.assertIsNone(result.errors)
        self.assertIsNone(result.data["license"]["supplier"])

        # Parity: the REST supplier list hides the same row under the same scope.
        self.client.force_login(self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_all_accessible", None)
        session["active_tenant_group_id"] = self.root.pk
        session.save()
        response = self.client.get(reverse("api:assets_api:supplier-list") + "?limit=100")
        self.assertEqual(response.status_code, 200, response.content[:400])
        names = {row["name"] for row in response.json()["results"]}
        self.assertNotIn("GSR Supplier GB", names)

    def test_authorized_tenant_supplier_reachable_through_relation(self):
        ctx = self._graphql_context_group()
        result = schema.execute_sync(
            f"{{ license(id: {self.license_own.pk}) {{ supplier {{ id }} }} }}",
            context_value=ctx,
        )
        self.assertIsNone(result.errors)
        self.assertEqual(int(result.data["license"]["supplier"]["id"]), self.supplier_tenant_a.pk)

    def test_superuser_relation_matches_list_parity(self):
        superuser = User.objects.create_superuser("gsr_super", "gsr_super@example.com", "pw")
        ctx = self._graphql_context_group(user=superuser)
        result = schema.execute_sync(
            f"{{ license(id: {self.license_leak.pk}) {{ supplier {{ id }} }} }}",
            context_value=ctx,
        )
        self.assertIsNone(result.errors)
        self.assertEqual(int(result.data["license"]["supplier"]["id"]), self.supplier_group_b.pk)

    def test_global_supplier_follows_list_visibility(self):
        ctx = self._graphql_context_group()
        result = schema.execute_sync(
            f"{{ license(id: {self.license_global.pk}) {{ supplier {{ id }} }} }}",
            context_value=ctx,
        )
        self.assertIsNone(result.errors)
        self.assertEqual(int(result.data["license"]["supplier"]["id"]), self.supplier_global.pk)

    def test_relation_check_is_one_exists_query_per_distinct_object(self):
        """Repeated checks on one object reuse the decision; each new object
        costs exactly one indexed EXISTS and no query materializes the table."""
        info = SimpleNamespace(context=self._graphql_context_group())

        def _authorize(obj):
            return scoped_relation(info, obj, "assets.view_supplier")

        with CaptureQueriesContext(connection) as capture:
            self.assertEqual(_authorize(self.supplier_tenant_a), self.supplier_tenant_a)
            self.assertEqual(_authorize(self.supplier_tenant_a), self.supplier_tenant_a)
            self.assertEqual(_authorize(self.supplier_global), self.supplier_global)
            self.assertIsNone(_authorize(self.supplier_group_b))

        supplier_queries = [entry["sql"] for entry in capture.captured_queries if '"assets_supplier"' in entry["sql"]]
        self.assertEqual(len(supplier_queries), 3)
        self.assertTrue(all("LIMIT 1" in sql.upper() for sql in supplier_queries))

    def test_scoped_relation_cache_supports_mutable_and_frozen_contexts(self):
        from types import MappingProxyType

        from core.graphql_utils import _scoped_relation_cache

        mutable = {}
        store = _scoped_relation_cache(SimpleNamespace(context=mutable))
        self.assertIs(mutable["_itambox_scoped_relation_decisions"], store)

        self.assertIsNone(_scoped_relation_cache(SimpleNamespace(context=MappingProxyType({}))))

    def test_relation_authorizes_without_a_cacheable_context(self):
        class FrozenContext:
            __slots__ = ("request",)

        ctx = FrozenContext()
        ctx.request = self._graphql_context_group()
        self.assertEqual(
            scoped_relation(SimpleNamespace(context=ctx), self.supplier_tenant_a, "assets.view_supplier"),
            self.supplier_tenant_a,
        )

    def test_global_supplier_requires_the_supplier_permission(self):
        user = User.objects.create_user("gsr_no_supplier_perm", "gsr_no_supplier_perm@example.com", "pw")
        Membership.objects.create(user=user, tenant=self.tenant_a)
        _current_user.set(user)
        set_current_tenant(None)
        set_current_all_accessible(False)
        set_current_tenant_group(self.root)
        request = self.factory.get("/graphql")
        request.user = user
        info = SimpleNamespace(context=request)
        self.assertIsNone(scoped_relation(info, self.supplier_global, "assets.view_supplier"))

    def test_soft_deleted_supplier_hidden_through_relation_for_superuser(self):
        supplier = Supplier.objects.create(
            name="GSR Supplier Deleted", slug="gsr-supplier-deleted", tenant=self.tenant_a
        )
        license_row = License.objects.create(
            name="GSR License Deleted",
            software=self.software,
            supplier=supplier,
            seats=1,
            tenant=self.tenant_a,
        )
        supplier.soft_delete()

        superuser = User.objects.create_superuser("gsr_super_del", "gsr_super_del@example.com", "pw")
        ctx = self._graphql_context_group(user=superuser)
        result = schema.execute_sync(
            f"{{ license(id: {license_row.pk}) {{ supplier {{ id }} }} }}",
            context_value=ctx,
        )
        self.assertIsNone(result.errors)
        self.assertIsNone(result.data["license"]["supplier"])

    def test_relation_is_none_for_anonymous_user(self):
        request = self.factory.get("/graphql")
        request.user = AnonymousUser()
        info = SimpleNamespace(context=request)
        self.assertIsNone(scoped_relation(info, self.supplier_tenant_a, "assets.view_supplier"))

    def test_relation_fails_closed_when_manager_has_no_for_scope(self):
        class UnscopedRelated:
            _default_manager = SimpleNamespace()

        request = self.factory.get("/graphql")
        request.user = self.user
        _current_user.set(self.user)
        set_current_tenant(None)
        set_current_all_accessible(False)
        set_current_tenant_group(self.root)
        self.assertIsNone(scoped_relation(SimpleNamespace(context=request), UnscopedRelated(), "assets.view_supplier"))
