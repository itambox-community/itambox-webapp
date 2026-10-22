from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, StatusLabel
from core.tests.mixins import grant
from extras.models import LabelTemplate
from organization.models import Role, Tenant

User = get_user_model()


class LabelPrintPermissionTests(TestCase):
    """WS1-2: printing a label exposes an object's identity (name, tag, serial),
    so the label route requires the object's view permission -- not just login --
    and never reaches outside the request's tenant scope."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant", slug="t-label")
        self.other = Tenant.objects.create(name="Other", slug="t-label-other")
        self.status = StatusLabel.objects.create(name="Dep", slug="dep-label", type="deployable")
        self.asset = Asset.objects.create(
            name="Secret Laptop", asset_tag="LBL-1", status=self.status, tenant=self.tenant
        )
        self.foreign = Asset.objects.create(
            name="Foreign Laptop", asset_tag="LBL-2", status=self.status, tenant=self.other
        )
        self.url = reverse("label_print", kwargs={"app_label": "assets", "model_name": "asset"})

    def _login(self, username, perms):
        user = User.objects.create_user(username=username, password="pw")
        role = Role.objects.create(tenant=self.tenant, name=f"role-{username}", permissions=perms)
        grant(user, self.tenant, role)
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()
        return user

    def _print(self, *pks, template=""):
        return self.client.post(self.url, {"pk": list(pks), "_print": "1", "template": template, "layout": "roll"})

    def test_member_without_view_asset_is_denied(self):
        self._login("noview", ["extras.view_dashboard"])  # member, but no view_asset
        self.assertEqual(self.client.get(self.url, {"pk": self.asset.pk}).status_code, 403)
        self.assertEqual(self._print(self.asset.pk).status_code, 403)

    def test_member_with_view_asset_is_allowed(self):
        self._login("canview", ["assets.view_asset"])
        response = self._print(self.asset.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_object_outside_the_tenant_scope_is_refused(self):
        self._login("scoped", ["assets.view_asset"])
        response = self._print(self.asset.pk, self.foreign.pk)
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.get("Content-Type"), "application/pdf")
        self.assertNotContains(response, "Foreign Laptop", status_code=400)

    def test_template_for_another_object_type_is_refused(self):
        self._login("wrongtype", ["assets.view_asset"])
        stock_only = LabelTemplate.objects.create(name="Bins only", barcode_format="qr")
        response = self._print(self.asset.pk, template=str(stock_only.pk))
        self.assertEqual(response.status_code, 400)

    def test_model_without_a_label_is_not_found(self):
        self._login("nolabel", ["assets.view_asset", "assets.view_statuslabel"])
        url = reverse("label_print", kwargs={"app_label": "assets", "model_name": "statuslabel"})
        self.assertEqual(self.client.get(url, {"pk": self.status.pk}).status_code, 404)
