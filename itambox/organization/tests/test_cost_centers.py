"""
Tests for the CostCenter model and CRUD stack.

Covers:
- Model defaults
- __str__ and full_path
- Unique code constraint per tenant (including soft-delete resurrection)
- Self-parent cycle guard (model clean + form validation)
- Multi-hop ancestor cycle guard
- Basic CRUD views (list / detail / create / edit / delete)
- Tenant scoping (explicit scoping)
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from core.managers import Scope
from organization.models import CostCenter, Tenant

User = get_user_model()


class CostCenterModelTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")

    def _make(self, **kwargs):
        defaults = dict(tenant=self.tenant, name="Engineering", slug="engineering", code="CC-001")
        defaults.update(kwargs)
        return CostCenter.objects.create(**defaults)

    # --- defaults ---

    def test_is_active_default_true(self):
        cc = self._make()
        self.assertTrue(cc.is_active)

    def test_str_includes_code_and_name(self):
        cc = self._make(code="CC-100", name="Finance")
        self.assertIn("CC-100", str(cc))
        self.assertIn("Finance", str(cc))

    def test_str_name_only_when_no_code(self):
        cc = self._make(code="")
        self.assertEqual(str(cc), "Engineering")

    # --- hierarchy helpers ---

    def test_depth_top_level(self):
        cc = self._make()
        self.assertEqual(cc.depth, 0)

    def test_depth_child(self):
        parent = self._make(name="Parent", slug="parent-cc", code="CC-P")
        child = self._make(name="Child", slug="child-cc", code="CC-C", parent=parent)
        self.assertEqual(child.depth, 1)

    def test_full_path(self):
        parent = self._make(name="Finance", slug="finance", code="CC-FIN")
        child = self._make(name="Payroll", slug="payroll", code="CC-PAY", parent=parent)
        self.assertEqual(child.full_path, "Finance / Payroll")

    # --- unique code per tenant ---

    def test_code_unique_within_tenant(self):

        self._make(name="Engineering", slug="engineering", code="CC-001")
        with self.assertRaisesRegex(IntegrityError, "organization_costcenter_unique_tenant_code_active"):
            self._make(name="Other", slug="other", code="CC-001")

    def test_code_same_across_different_tenants(self):
        other_tenant = Tenant.objects.create(name="Globex", slug="globex")
        cc_a = self._make(name="Eng", slug="eng-a", code="CC-001")
        cc_b = CostCenter.objects.create(tenant=other_tenant, name="Eng", slug="eng-b", code="CC-001")
        self.assertEqual(cc_a.code, cc_b.code)

    def test_soft_deleted_code_can_be_reused(self):
        cc = self._make()
        cc.deleted_at = __import__("django.utils.timezone", fromlist=["now"]).now()
        cc.save()
        # Should not raise — soft-deleted row no longer occupies the unique slot
        new_cc = self._make(slug="engineering-new")
        self.assertEqual(new_cc.code, "CC-001")

    # --- cycle / self-parent guard (model.clean) ---

    def test_clean_raises_on_self_parent(self):
        cc = self._make()
        cc.parent = cc
        with self.assertRaises(ValidationError):
            cc.clean()

    def test_clean_raises_on_ancestor_cycle(self):
        grandparent = self._make(name="A", slug="a", code="CC-A")
        parent = self._make(name="B", slug="b", code="CC-B", parent=grandparent)
        child = self._make(name="C", slug="c", code="CC-C", parent=parent)
        # Make grandparent a child of child → cycle
        grandparent.parent = child
        with self.assertRaises(ValidationError):
            grandparent.clean()


class CostCenterFormTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")
        self.cc = CostCenter.objects.create(tenant=self.tenant, name="Root", slug="root", code="CC-ROOT")

    def test_form_rejects_self_parent(self):
        from organization.forms.costcenter_form import CostCenterForm

        form = CostCenterForm(
            instance=self.cc,
            data={
                "tenant": self.tenant.pk,
                "name": "Root",
                "slug": "root",
                "code": "CC-ROOT",
                "parent": self.cc.pk,
                "description": "",
                "is_active": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("parent", form.errors)

    def test_form_rejects_ancestor_cycle(self):
        from organization.forms.costcenter_form import CostCenterForm

        child = CostCenter.objects.create(
            tenant=self.tenant, name="Child", slug="child-cc", code="CC-C", parent=self.cc
        )
        # Try to set parent's parent = child → cycle
        form = CostCenterForm(
            instance=self.cc,
            data={
                "tenant": self.tenant.pk,
                "name": "Root",
                "slug": "root",
                "code": "CC-ROOT",
                "parent": child.pk,
                "description": "",
                "is_active": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("parent", form.errors)


class CostCenterViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass", is_superuser=True, is_staff=True)
        self.client.force_login(self.user)
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")
        self.cc = CostCenter.objects.create(tenant=self.tenant, name="Engineering", slug="engineering", code="CC-ENG")

    def test_list_view(self):
        url = reverse("organization:costcenter_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_view(self):
        url = reverse("organization:costcenter_detail", kwargs={"pk": self.cc.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_create_view_post(self):
        url = reverse("organization:costcenter_create")
        response = self.client.post(
            url,
            {
                "tenant": self.tenant.pk,
                "name": "Finance",
                "slug": "finance",
                "code": "CC-FIN",
                "description": "",
                "is_active": True,
            },
        )
        if response.status_code != 302:
            form = response.context.get("form")
            if form:
                self.fail(f"Form invalid. Errors: {form.errors}")
        self.assertTrue(CostCenter.objects.filter(code="CC-FIN").exists())

    def test_edit_view_post(self):
        url = reverse("organization:costcenter_update", kwargs={"pk": self.cc.pk})
        response = self.client.post(
            url,
            {
                "tenant": self.tenant.pk,
                "name": "Engineering Updated",
                "slug": "engineering-updated",
                "code": "CC-ENG",
                "description": "",
                "is_active": True,
            },
        )
        if response.status_code != 302:
            form = response.context.get("form")
            if form:
                self.fail(f"Form invalid. Errors: {form.errors}")
        self.cc.refresh_from_db()
        self.assertEqual(self.cc.name, "Engineering Updated")

    def test_delete_view_post(self):
        url = reverse("organization:costcenter_delete", kwargs={"pk": self.cc.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CostCenter.objects.filter(pk=self.cc.pk).exists())


class CostCenterTenantScopingTests(TestCase):
    """Verify explicit scoping filters correctly."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.cc_a = CostCenter.objects.create(tenant=self.tenant_a, name="CC A", slug="cc-a", code="CC-A")
        self.cc_b = CostCenter.objects.create(tenant=self.tenant_b, name="CC B", slug="cc-b", code="CC-B")

    def tearDown(self):
        from core.managers import set_current_tenant

        set_current_tenant(None)

    def test_tenant_a_sees_only_own_cost_centers(self):
        from core.managers import set_current_tenant

        set_current_tenant(self.tenant_a)
        qs = list(CostCenter.objects.for_scope(Scope.current()))
        self.assertIn(self.cc_a, qs)
        self.assertNotIn(self.cc_b, qs)

    def test_tenant_b_sees_only_own_cost_centers(self):
        from core.managers import set_current_tenant

        set_current_tenant(self.tenant_b)
        qs = list(CostCenter.objects.for_scope(Scope.current()))
        self.assertIn(self.cc_b, qs)
        self.assertNotIn(self.cc_a, qs)

    def test_no_tenant_sees_all(self):
        from core.managers import set_current_tenant

        set_current_tenant(None)
        qs = list(CostCenter.objects.for_scope(Scope.current()))
        self.assertIn(self.cc_a, qs)
        self.assertIn(self.cc_b, qs)
