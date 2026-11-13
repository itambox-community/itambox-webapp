"""Group scope must derive group-scoped rows from the permission-narrowed tenant
set, not from every descendant of the selected group (#1088).

A caller with ``assets.view_supplier`` only in tenant A (subgroup GA) must not
see a supplier shared with the sibling subgroup GB, and the same queryset is what
the bulk update/destroy routes filter by primary key.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Supplier
from core.managers import Scope, set_current_tenant, set_current_tenant_group
from core.tests.mixins import grant
from itambox.middleware import _current_user
from organization.models import Membership, Role, Tenant, TenantGroup

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
        self.assertNotIn(self.supplier_root.pk, write_ids)
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
