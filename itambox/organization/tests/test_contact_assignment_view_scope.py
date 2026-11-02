"""Permission and tenant-scope boundary of the contact assignment create view (#995).

The view used to require only a session, resolved the client-supplied
``content_type``/``object_id`` pair through ``model_class()`` unscoped, and the
form bound the pair verbatim.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from core.managers import set_current_tenant
from core.tests.mixins import grant
from organization.forms.contactrole_form import ContactAssignmentForm, resolve_assignment_target
from organization.models import Contact, ContactAssignment, ContactRole, Role, Tenant
from organization.views import contactrole_views

User = get_user_model()


class ContactAssignmentCreateViewScopeTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="CA995 Tenant A", slug="ca995-a")
        self.tenant_b = Tenant.objects.create(name="CA995 Tenant B", slug="ca995-b")

        self.privileged = User.objects.create_user(username="ca995_priv", password="password123")
        self.unprivileged = User.objects.create_user(username="ca995_none", password="password123")
        self.roleless = User.objects.create_user(username="ca995_roleless", password="password123")

        role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="CA995 Contact Admin",
            permissions=["organization.add_contactassignment", "assets.view_asset"],
        )
        grant(self.privileged, self.tenant_a, role_a)
        view_only = Role.objects.create(
            tenant=self.tenant_a, name="CA995 Viewer", permissions=["organization.view_contactassignment"]
        )
        grant(self.unprivileged, self.tenant_a, view_only)

        status = StatusLabel.objects.create(name="CA995 Active", slug="ca995-active")
        asset_role = AssetRole.objects.create(name="CA995 Laptop", slug="ca995-laptop")
        mfr = Manufacturer.objects.create(name="CA995 Maker", slug="ca995-maker")
        asset_type = AssetType.objects.create(manufacturer=mfr, model="CA995 Model")
        self.asset_a = Asset.objects.create(
            name="CA995 Asset A",
            asset_tag="CA995-A",
            status=status,
            asset_role=asset_role,
            asset_type=asset_type,
            tenant=self.tenant_a,
        )
        self.asset_b = Asset.objects.create(
            name="CA995 Asset B secret",
            asset_tag="CA995-B",
            status=status,
            asset_role=asset_role,
            asset_type=asset_type,
            tenant=self.tenant_b,
        )
        self.contact = Contact.objects.create(name="CA995 Contact", email="ca995@example.com")
        self.contact_role = ContactRole.objects.create(name="CA995 Role")
        self.asset_ct = ContentType.objects.get_for_model(Asset)
        self.url = reverse("organization:contactassignment_create")

    def _login(self, user, tenant):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

    def _query(self, asset):
        return {"content_type": self.asset_ct.pk, "object_id": asset.pk}

    def _post_data(self, asset):
        return {
            "contact": self.contact.pk,
            "role": self.contact_role.pk,
            "priority": "primary",
            **self._query(asset),
        }

    def _assignments(self, asset):
        return ContactAssignment.objects.filter(content_type=self.asset_ct, object_id=asset.pk)

    def test_member_without_permission_gets_403_on_get(self):
        self._login(self.unprivileged, self.tenant_a)
        response = self.client.get(self.url, self._query(self.asset_a))
        self.assertEqual(response.status_code, 403)

    def test_member_without_permission_gets_403_on_post(self):
        self._login(self.unprivileged, self.tenant_a)
        response = self.client.post(self.url, self._post_data(self.asset_a))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self._assignments(self.asset_a).exists())

    def test_user_without_any_membership_gets_403(self):
        self.client.force_login(self.roleless)
        self.assertEqual(self.client.get(self.url, self._query(self.asset_a)).status_code, 403)
        self.assertEqual(self.client.post(self.url, self._post_data(self.asset_a)).status_code, 403)
        self.assertFalse(self._assignments(self.asset_a).exists())

    def test_foreign_target_get_is_404_without_disclosure(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.get(self.url, self._query(self.asset_b))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "CA995 Asset B secret", status_code=404)

    def test_foreign_target_post_is_404_and_binds_nothing(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.post(self.url, self._post_data(self.asset_b))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._assignments(self.asset_b).exists())

    def test_unknown_target_is_404(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.get(self.url, {"content_type": self.asset_ct.pk, "object_id": 999999})
        self.assertEqual(response.status_code, 404)

    def test_same_tenant_get_renders_target(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.get(self.url, self._query(self.asset_a))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CA995 Asset A")

    def test_same_tenant_post_creates_assignment(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.post(self.url, self._post_data(self.asset_a))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self._assignments(self.asset_a).filter(contact=self.contact).exists())

    def test_form_rejects_tampered_foreign_target(self):
        """A form bound to a foreign pair fails validation and cannot save."""
        self._login(self.privileged, self.tenant_a)
        request_user_scope = self.client.get(self.url, self._query(self.asset_a))
        self.assertEqual(request_user_scope.status_code, 200)

        set_current_tenant(self.tenant_a)
        form = ContactAssignmentForm(
            {"contact": self.contact.pk, "role": self.contact_role.pk, "priority": "primary"},
            content_type=self.asset_ct,
            object_id=self.asset_b.pk,
        )
        self.assertFalse(form.is_valid())
        self.assertFalse(self._assignments(self.asset_b).exists())

    def test_non_integer_content_type_is_404(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.get(self.url, {"content_type": "abc", "object_id": self.asset_a.pk})
        self.assertEqual(response.status_code, 404)

    def test_content_type_without_model_class_is_404(self):
        stale = ContentType.objects.create(app_label="organization", model="ca995gone")
        self.assertIsNone(stale.model_class())
        self._login(self.privileged, self.tenant_a)
        response = self.client.get(self.url, {"content_type": stale.pk, "object_id": 1})
        self.assertEqual(response.status_code, 404)

    def test_non_integer_object_id_is_404(self):
        self._login(self.privileged, self.tenant_a)
        response = self.client.get(self.url, {"content_type": self.asset_ct.pk, "object_id": "abc"})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(self.url, {**self._post_data(self.asset_a), "object_id": "abc"})
        self.assertEqual(response.status_code, 404)

    def test_object_level_permission_denial_is_403_and_writes_nothing(self):
        """Model permission without permission on the target's own tenant is refused."""
        self._login(self.privileged, self.tenant_a)
        with patch.object(contactrole_views, "resolve_assignment_target", return_value=self.asset_b):
            get_response = self.client.get(self.url, self._query(self.asset_b))
            post_response = self.client.post(self.url, self._post_data(self.asset_b))
        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertFalse(self._assignments(self.asset_b).exists())

    def test_resolver_handles_models_without_tenant_scoping_manager(self):
        """A target model whose queryset lacks filter_by_tenant resolves by pk alone."""
        content_type = ContentType.objects.get_for_model(ContentType)
        self.assertFalse(hasattr(ContentType._default_manager.all(), "filter_by_tenant"))
        self.assertEqual(resolve_assignment_target(content_type, self.asset_ct.pk), self.asset_ct)

    def test_resolver_returns_none_without_content_type(self):
        self.assertIsNone(resolve_assignment_target(None, 1))
