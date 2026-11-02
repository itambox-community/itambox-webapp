from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, StatusLabel, Supplier
from core.managers import set_current_tenant
from core.tests.mixins import grant
from organization.models import Role, Tenant
from subscriptions.forms import SubscriptionAssignmentForm
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
