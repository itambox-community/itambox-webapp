"""ObjectChange uses an explicit-scope manager (follow-up to #618, issue #683)."""

import uuid

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from core.managers import ExplicitScopeManager, Scope, set_current_tenant
from core.models import ObjectChange
from core.tests.mixins import TenantTestMixin
from organization.models import Tenant
from users.models import User


class ObjectChangeExplicitScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="OC A", slug="oc-a")
        cls.tenant_b = Tenant.objects.create(name="OC B", slug="oc-b")
        cls.ct = ContentType.objects.get_for_model(Tenant)
        cls.row_a = cls._make(cls.tenant_a, "a")
        cls.row_b = cls._make(cls.tenant_b, "b")
        cls.row_global = cls._make(None, "global")

    @classmethod
    def _make(cls, tenant, repr_):
        return ObjectChange.objects.create(
            tenant=tenant,
            user_name="tester",
            request_id=uuid.uuid4(),
            action="update",
            changed_object_type=cls.ct,
            changed_object_id=1,
            object_repr=repr_,
        )

    def tearDown(self):
        set_current_tenant(None)

    def test_default_manager_is_explicit(self):
        self.assertIsInstance(ObjectChange.objects, ExplicitScopeManager)
        self.assertTrue(ObjectChange.allow_global_tenant)

    def test_default_manager_has_no_ambient_scope(self):
        set_current_tenant(self.tenant_a)
        pks = set(ObjectChange.objects.values_list("pk", flat=True))
        self.assertEqual(pks, {self.row_a.pk, self.row_b.pk, self.row_global.pk})

    def test_for_scope_tenant_keeps_own_and_global_rows(self):
        scope = Scope(kind=Scope.TENANT, tenant=self.tenant_a)
        pks = set(ObjectChange.objects.for_scope(scope).values_list("pk", flat=True))
        self.assertEqual(pks, {self.row_a.pk, self.row_global.pk})

    def test_for_scope_denied_returns_nothing(self):
        self.assertFalse(ObjectChange.objects.for_scope(Scope(kind=Scope.DENIED)).exists())

    def test_for_scope_system_returns_everything(self):
        self.assertEqual(ObjectChange.objects.for_scope(Scope(kind=Scope.SYSTEM)).count(), 3)

    def test_filter_by_tenant_adapter_matches_for_scope(self):
        set_current_tenant(self.tenant_a)
        self.assertEqual(
            set(ObjectChange.objects.filter_by_tenant().values_list("pk", flat=True)),
            set(ObjectChange.objects.for_scope(Scope.current()).values_list("pk", flat=True)),
        )


class ObjectChangeDetailAccessTests(TenantTestMixin, TestCase):
    """The detail view needs ``core.view_objectchange`` and the active scope (R-0)."""

    SECRET = "tenant-b-secret-snapshot"

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="OCD A", slug="ocd-a")
        cls.tenant_b = Tenant.objects.create(name="OCD B", slug="ocd-b")
        cls.ct = ContentType.objects.get_for_model(Tenant)
        cls.row_a = cls._make(cls.tenant_a, "own-row")
        cls.row_b = cls._make(cls.tenant_b, "foreign-row", {"name": cls.SECRET})
        cls.row_global = cls._make(None, "global-row")

    @classmethod
    def _make(cls, tenant, repr_, postchange=None):
        return ObjectChange.objects.create(
            tenant=tenant,
            user_name="tester",
            request_id=uuid.uuid4(),
            action="update",
            changed_object_type=cls.ct,
            changed_object_id=1,
            object_repr=repr_,
            postchange_data=postchange,
        )

    def tearDown(self):
        self.clear_tenant_context()

    def _user(self, name, permissions):
        user = User.objects.create_user(username=name, email=f"{name}@example.com", password="password")
        self.client_login_to_tenant(user, self.tenant_a, role_permissions=permissions)
        return user

    def _detail(self, row):
        return self.client.get(reverse("objectchange", kwargs={"pk": row.pk}))

    def test_foreign_tenant_row_is_404_without_leak(self):
        self._user("oc-viewer", ["core.view_objectchange"])
        response = self._detail(self.row_b)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.SECRET.encode(), response.content)

    def test_own_and_global_rows_are_visible(self):
        self._user("oc-viewer", ["core.view_objectchange"])
        self.assertEqual(self._detail(self.row_a).status_code, 200)
        self.assertEqual(self._detail(self.row_global).status_code, 200)

    def test_missing_permission_is_403_on_detail_and_list(self):
        self._user("oc-noperm", [])
        self.assertEqual(self._detail(self.row_a).status_code, 403)
        self.assertEqual(self._detail(self.row_b).status_code, 403)
        self.assertEqual(self.client.get(reverse("objectchange_list")).status_code, 403)

    def test_pk_enumeration_cannot_bypass_scope(self):
        self._user("oc-viewer", ["core.view_objectchange"])
        visible = {self.row_a.pk, self.row_global.pk}
        for pk in range(1, ObjectChange.objects.order_by("-pk").first().pk + 3):
            response = self.client.get(reverse("objectchange", kwargs={"pk": pk}))
            self.assertEqual(response.status_code, 200 if pk in visible else 404, pk)
            self.assertNotIn(self.SECRET.encode(), response.content)
