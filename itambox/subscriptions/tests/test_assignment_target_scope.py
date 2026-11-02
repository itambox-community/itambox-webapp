from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, StatusLabel, Supplier
from core.managers import set_current_tenant
from core.tests.mixins import grant
from organization.models import Role, Tenant
from subscriptions.forms import SubscriptionAssignmentForm, resolve_assignment_target
from subscriptions.models import Subscription, SubscriptionAssignment

User = get_user_model()


class SubscriptionAssignmentTargetScopeTests(TestCase):
    """The assignment create view resolves and authorizes the target in scope (#996)."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="scope-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="scope-b")
        self.user_a = User.objects.create_user(username="scope_user_a", password="password123")
        role = Role.objects.create(
            tenant=self.tenant_a, name="Assigner", permissions=["subscriptions.add_subscriptionassignment"]
        )
        grant(self.user_a, self.tenant_a, role)

        status = StatusLabel.objects.create(name="Active", slug="active-scope", type="deployable")
        self.asset_a = Asset.objects.create(name="Asset A", asset_tag="SCOPE-A", status=status, tenant=self.tenant_a)
        self.asset_b = Asset.objects.create(name="Asset B", asset_tag="SCOPE-B", status=status, tenant=self.tenant_b)
        supplier_a = Supplier.objects.create(name="Supplier A", tenant=self.tenant_a)
        self.sub_a = Subscription.objects.create(name="Sub A", supplier=supplier_a, tenant=self.tenant_a)
        self.sub_global = Subscription.objects.create(
            name="Sub Global", supplier=Supplier.objects.create(name="Supplier G", tenant=None), tenant=None
        )

        self.ct = ContentType.objects.get_for_model(Asset)
        self.url = reverse("subscriptions:subscriptionassignment_create")
        self.client.force_login(self.user_a)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

    def _query(self, asset):
        return "?content_type=%s&object_id=%s" % (self.ct.pk, asset.pk)

    def test_get_foreign_target_is_404_and_does_not_disclose(self):
        response = self.client.get(self.url + self._query(self.asset_b))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Asset B", status_code=404)

    def test_post_foreign_target_is_404_for_global_subscription(self):
        response = self.client.post(self.url + self._query(self.asset_b), {"subscription": self.sub_global.pk})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SubscriptionAssignment.objects.filter(object_id=self.asset_b.pk).exists())

    def test_get_same_tenant_target_renders(self):
        response = self.client.get(self.url + self._query(self.asset_a))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asset A")

    def test_post_same_tenant_target_assigns_subscription(self):
        response = self.client.post(self.url + self._query(self.asset_a), {"subscription": self.sub_a.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            SubscriptionAssignment.objects.filter(subscription=self.sub_a, object_id=self.asset_a.pk).exists()
        )

    def test_form_rejects_foreign_target_and_save_time_revalidation(self):
        set_current_tenant(self.tenant_a)
        self.addCleanup(set_current_tenant, None)
        form = SubscriptionAssignmentForm(
            {"subscription": self.sub_global.pk}, content_type=self.ct, object_id=self.asset_b.pk
        )
        self.assertFalse(form.is_valid())
        self.assertFalse(SubscriptionAssignment.objects.filter(object_id=self.asset_b.pk).exists())

    def test_unsupported_content_type_is_404(self):
        ct = ContentType.objects.get_for_model(Subscription)
        response = self.client.get(self.url + "?content_type=%s&object_id=%s" % (ct.pk, self.sub_a.pk))
        self.assertEqual(response.status_code, 404)

    def test_resolver_rejects_missing_model_class_and_unsupported_model(self):
        ghost = ContentType.objects.create(app_label="ghost_app", model="ghostmodel")
        subscription_ct = ContentType.objects.get_for_model(Subscription)
        set_current_tenant(self.tenant_a)
        self.addCleanup(set_current_tenant, None)
        self.assertIsNone(resolve_assignment_target(None, self.asset_a.pk))
        self.assertIsNone(resolve_assignment_target(ghost, 1))
        self.assertIsNone(resolve_assignment_target(subscription_ct, self.sub_a.pk))
        self.assertIsNone(resolve_assignment_target(self.ct, "not-a-number"))
        self.assertEqual(resolve_assignment_target(self.ct, self.asset_a.pk), self.asset_a)

    def test_content_type_without_model_class_is_404(self):
        ghost = ContentType.objects.create(app_label="ghost_app", model="ghostmodel")
        response = self.client.get(self.url + "?content_type=%s&object_id=1" % ghost.pk)
        self.assertEqual(response.status_code, 404)

    def test_form_with_unresolvable_target_offers_no_subscription(self):
        set_current_tenant(self.tenant_a)
        self.addCleanup(set_current_tenant, None)
        form = SubscriptionAssignmentForm(
            {"subscription": self.sub_a.pk},
            content_type=ContentType.objects.get_for_model(Subscription),
            object_id=self.sub_a.pk,
        )
        self.assertIsNone(form.target_obj)
        self.assertFalse(form.fields["subscription"].queryset.exists())
        self.assertFalse(form.is_valid())

    def _valid_form(self, subscription):
        set_current_tenant(self.tenant_a)
        self.addCleanup(set_current_tenant, None)
        form = SubscriptionAssignmentForm(
            {"subscription": subscription.pk}, content_type=self.ct, object_id=self.asset_a.pk
        )
        self.assertTrue(form.is_valid(), form.errors)
        return form

    def test_save_revalidates_target_that_no_longer_resolves(self):
        form = self._valid_form(self.sub_a)
        with mock.patch("subscriptions.forms.resolve_assignment_target", return_value=None):
            with self.assertRaises(ValidationError):
                form.save()
        self.assertFalse(SubscriptionAssignment.objects.filter(object_id=self.asset_a.pk).exists())

    def test_save_rejects_subscription_of_another_tenant(self):
        supplier_b = Supplier.objects.create(name="Supplier B", tenant=self.tenant_b)
        sub_b = Subscription.objects.create(name="Sub B", supplier=supplier_b, tenant=self.tenant_b)
        form = self._valid_form(self.sub_a)
        form.instance.subscription = sub_b
        with self.assertRaises(ValidationError):
            form.save()
        self.assertFalse(SubscriptionAssignment.objects.filter(object_id=self.asset_a.pk).exists())

    def test_global_subscription_cannot_be_forced_onto_tenant_target(self):
        form = self._valid_form(self.sub_a)
        form.instance.subscription = self.sub_global
        with self.assertRaises(ValidationError):
            form.save()
        self.assertFalse(SubscriptionAssignment.objects.filter(object_id=self.asset_a.pk).exists())

    def test_object_level_permission_denied_for_target_of_other_tenant(self):
        # The user holds the add permission in tenant A only; a target of tenant B
        # that slipped past resolution is refused by the object-level check.
        with mock.patch("subscriptions.forms.resolve_assignment_target", return_value=self.asset_b):
            response = self.client.post(self.url + self._query(self.asset_b), {"subscription": self.sub_global.pk})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SubscriptionAssignment.objects.filter(object_id=self.asset_b.pk).exists())

