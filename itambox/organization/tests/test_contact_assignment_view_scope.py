"""Permission and tenant-scope boundary of the contact assignment create view (#995).

The view used to require only a session, resolved the client-supplied
``content_type``/``object_id`` pair through ``model_class()`` unscoped, and the
form bound the pair verbatim.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel, Supplier
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


SECRET_NAME = "CAS-SECRET-B-PERSON"
SECRET_EMAIL = "cas-secret-b@example.invalid"
SECRET_PHONE = "+00-CASB"
SECRET_ASSET = "CAS-SECRET-B-ASSET"


class ContactAssignmentReadScopeTests(TestCase):
    """Every UI reader of ContactAssignment shows only rows visible in the scope.

    The fixture mirrors the tenant read audit (F1-F5, F11 and the delete path):
    shared rows (a global contact, a global role, a global manufacturer and
    supplier) carry assignments that point at tenant B's private contact or at
    tenant B's asset. Tenant A must never receive those strings.
    """

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="CAS Tenant A", slug="cas-a")
        self.tenant_b = Tenant.objects.create(name="CAS Tenant B", slug="cas-b")
        self.user = User.objects.create_user(username="cas_member_a", password="password123")
        role = Role.objects.create(
            tenant=self.tenant_a,
            name="CAS Reader",
            permissions=[
                "organization.view_contact",
                "organization.delete_contact",
                "organization.view_contactrole",
                "organization.delete_contactrole",
                "organization.view_contactassignment",
                "organization.delete_contactassignment",
                "assets.view_manufacturer",
                "assets.view_supplier",
                "assets.view_asset",
            ],
        )
        grant(self.user, self.tenant_a, role)

        status = StatusLabel.objects.create(name="CAS Active", slug="cas-active")
        asset_role = AssetRole.objects.create(name="CAS Laptop", slug="cas-laptop")
        self.manufacturer = Manufacturer.objects.create(name="CAS Maker", slug="cas-maker")
        self.supplier = Supplier.objects.create(name="CAS Supplier", slug="cas-supplier")
        asset_type = AssetType.objects.create(manufacturer=self.manufacturer, model="CAS Model")
        self.asset_a = Asset.objects.create(
            name="CAS Own Asset",
            asset_tag="CAS-A",
            status=status,
            asset_role=asset_role,
            asset_type=asset_type,
            tenant=self.tenant_a,
        )
        self.asset_b = Asset.objects.create(
            name=SECRET_ASSET,
            asset_tag="CAS-B",
            status=status,
            asset_role=asset_role,
            asset_type=asset_type,
            tenant=self.tenant_b,
        )
        self.global_contact = Contact.objects.create(name="CAS Global Desk")
        self.own_contact = Contact.objects.create(name="CAS Own Person", tenant=self.tenant_a)
        self.secret_contact = Contact.objects.create(
            name=SECRET_NAME, email=SECRET_EMAIL, phone=SECRET_PHONE, tenant=self.tenant_b
        )
        self.support_role, _created = ContactRole.objects.get_or_create(
            slug="support", defaults={"name": "CAS Support"}
        )
        self.other_role = ContactRole.objects.create(name="CAS Account", slug="cas-account")

        self.global_on_foreign_asset = self._assign(self.global_contact, self.other_role, self.asset_b)
        self.global_on_own_asset = self._assign(self.global_contact, self.other_role, self.asset_a)
        self.secret_on_manufacturer = self._assign(self.secret_contact, self.support_role, self.manufacturer)
        self.secret_on_supplier = self._assign(self.secret_contact, self.other_role, self.supplier, "primary")
        self.own_on_manufacturer = self._assign(self.own_contact, self.other_role, self.manufacturer)
        self.own_on_supplier = self._assign(self.own_contact, self.other_role, self.supplier)

        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

    @staticmethod
    def _assign(contact, role, target, priority=""):
        return ContactAssignment.objects.create(
            contact=contact,
            role=role,
            content_type=ContentType.objects.get_for_model(target),
            object_id=target.pk,
            priority=priority,
        )

    def _assert_no_foreign_strings(self, response):
        body = response.content.decode()
        for secret in (SECRET_NAME, SECRET_EMAIL, SECRET_PHONE, SECRET_ASSET):
            self.assertNotIn(secret, body)

    def test_global_contact_detail_hides_foreign_target(self):
        response = self.client.get(reverse("organization:contact_detail", kwargs={"pk": self.global_contact.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CAS Own Asset")
        self._assert_no_foreign_strings(response)
        rows = [row.record.pk for row in response.context["assignments_table"].rows]
        self.assertEqual(rows, [self.global_on_own_asset.pk])

    def test_global_role_detail_hides_foreign_contact_and_target(self):
        response = self.client.get(reverse("organization:contactrole_detail", kwargs={"pk": self.other_role.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CAS Own Person")
        self._assert_no_foreign_strings(response)
        rows = {row.record.pk for row in response.context["assignments_table"].rows}
        self.assertEqual(rows, {self.global_on_own_asset.pk, self.own_on_manufacturer.pk, self.own_on_supplier.pk})

    def test_manufacturer_contact_tab_hides_foreign_private_contact(self):
        url = reverse("assets:manufacturer_detail", kwargs={"pk": self.manufacturer.pk})
        response = self.client.get(url, {"tab": "contacts"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CAS Own Person")
        self._assert_no_foreign_strings(response)
        self.assertEqual(response.context["contact_assignments"], [self.own_on_manufacturer])
        self.assertNotContains(
            response, reverse("organization:contactassignment_delete", kwargs={"pk": self.secret_on_manufacturer.pk})
        )

    def test_supplier_contact_tab_and_primary_contact_hide_foreign_private_contact(self):
        url = reverse("assets:supplier_detail", kwargs={"pk": self.supplier.pk})
        response = self.client.get(url, {"tab": "contacts"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CAS Own Person")
        self._assert_no_foreign_strings(response)
        self.assertEqual(response.context["contact_assignments"], [self.own_on_supplier])

    def test_asset_support_contact_ignores_foreign_private_contact(self):
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset_a.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["support_contact"], self.own_contact)
        self._assert_no_foreign_strings(response)

    def test_foreign_private_contact_and_target_stay_hidden(self):
        contact_url = reverse("organization:contact_detail", kwargs={"pk": self.secret_contact.pk})
        self.assertEqual(self.client.get(contact_url).status_code, 404)
        asset_url = reverse("assets:asset_detail", kwargs={"pk": self.asset_b.pk})
        self.assertEqual(self.client.get(asset_url).status_code, 404)

    def test_delete_of_foreign_assignment_on_shared_target_is_404(self):
        for assignment in (self.secret_on_manufacturer, self.secret_on_supplier, self.global_on_foreign_asset):
            with self.subTest(assignment=assignment.pk):
                url = reverse("organization:contactassignment_delete", kwargs={"pk": assignment.pk})
                get_response = self.client.get(url)
                self.assertEqual(get_response.status_code, 404)
                self.assertNotContains(get_response, SECRET_NAME, status_code=404)
                self.assertEqual(self.client.post(url, {"confirm": "true"}).status_code, 404)
                self.assertTrue(ContactAssignment.objects.filter(pk=assignment.pk).exists())

    def test_delete_of_visible_assignment_still_works(self):
        url = reverse("organization:contactassignment_delete", kwargs={"pk": self.own_on_manufacturer.pk})

        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {"confirm": "true"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContactAssignment.objects.filter(pk=self.own_on_manufacturer.pk).exists())

    def _delete_messages(self, url):
        response = self.client.post(url, {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_contact_delete_blocker_hides_the_count_of_foreign_assignments(self):
        url = reverse("organization:contact_delete", kwargs={"pk": self.global_contact.pk})

        # A global contact is read-only for tenant users, so the delete is refused
        # before the blocker (and any count) can be computed.
        self.assertEqual(self.client.post(url, {"confirm": "true"}).status_code, 403)
        self.assertTrue(Contact.objects.filter(pk=self.global_contact.pk).exists())

    def test_contact_delete_blocker_keeps_the_count_when_everything_is_visible(self):
        url = reverse("organization:contact_delete", kwargs={"pk": self.own_contact.pk})

        messages = self._delete_messages(url)

        self.assertEqual(messages, ["Cannot delete contact 'CAS Own Person': It has 2 assignments."])
        self.assertTrue(Contact.objects.filter(pk=self.own_contact.pk).exists())

    def test_role_delete_blocker_hides_the_count_of_foreign_assignments(self):
        url = reverse("organization:contactrole_delete", kwargs={"pk": self.other_role.pk})

        messages = self._delete_messages(url)

        self.assertEqual(
            messages,
            ["Cannot delete role 'CAS Account': It is associated with contact assignments outside your scope."],
        )
        self.assertTrue(ContactRole.objects.filter(pk=self.other_role.pk).exists())

    def test_role_delete_blocker_keeps_the_count_when_everything_is_visible(self):
        role = ContactRole.objects.create(name="CAS Visible Role", slug="cas-visible-role")
        self._assign(self.own_contact, role, self.asset_a)
        url = reverse("organization:contactrole_delete", kwargs={"pk": role.pk})

        messages = self._delete_messages(url)

        self.assertEqual(
            messages, ["Cannot delete role 'CAS Visible Role': It is associated with 1 contact assignment."]
        )
        self.assertTrue(ContactRole.objects.filter(pk=role.pk).exists())
