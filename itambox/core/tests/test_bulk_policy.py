"""The list toolbar and the request-selected bulk endpoints agree.

A list offers bulk edit/delete only when the endpoint it posts to serves its
model. The contract test walks every model with a list route, so a new model is
either served (dedicated route, tenant-scoped manager, or classified shared
catalogue) or deliberately listed below -- never a button that answers 404/403.
"""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.template import RequestContext
from django.test import RequestFactory, TestCase
from django.urls import NoReverseMatch, reverse

from assets.models import Asset, AssetType, Category, Manufacturer, StatusLabel
from core.templatetags.helpers import bulk_action_context
from core.tests.mixins import TenantTestMixin
from extras.models import ExportTemplate
from extras.tables import ExportTemplateTable
from itambox.utils import get_model_viewname
from itambox.views.generic.bulk_policy import (
    BULK_ACTIONS,
    BULK_DELETE,
    BULK_EDIT,
    bulk_action_url,
    request_selected_bulk_allowed,
)
from organization.models import Role

User = get_user_model()

# Models with a list route that deliberately offer no bulk action. Each entry
# is a decision; the contract test fails when this set and reality diverge.
EXPECTED_WITHOUT_BULK = {
    BULK_EDIT: {
        "assets.assettype",  # managed definitions: rows are protected field by field
        "compliance.custodyreceipt",  # lifecycle row written only by compliance.services
        "organization.role",  # ownership and permission projection need escalation checks
        "organization.tenantresourcegrant",  # security-critical delegated cross-tenant grant
        "users.usergroup",  # membership escalation guard lives in its dedicated forms
    },
    BULK_DELETE: {
        "compliance.custodyreceipt",
        "organization.tenantresourcegrant",
        "users.user",  # deprovisioning goes through the user services / SCIM, not the generic archive
    },
}

DOMAIN_APPS = {
    "assets",
    "compliance",
    "extras",
    "inventory",
    "licenses",
    "organization",
    "procurement",
    "software",
    "subscriptions",
    "users",
}


def _list_models():
    for model in apps.get_models():
        if model._meta.app_label not in DOMAIN_APPS:
            continue
        try:
            reverse(get_model_viewname(model, "list"))
        except NoReverseMatch:
            continue
        yield model


class BulkPolicyContractTests(TestCase):
    """Every list model is classified, and every rendered button is served."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("bulk-root", "bulk-root@example.com", "pw")

    def test_every_list_model_without_bulk_is_a_recorded_decision(self):
        for action in BULK_ACTIONS:
            with self.subTest(action=action):
                without = {m._meta.label_lower for m in _list_models() if bulk_action_url(m, action) is None}
                self.assertEqual(without, EXPECTED_WITHOUT_BULK[action])

    def test_every_global_button_reaches_a_handler_that_accepts_its_model(self):
        self.client.force_login(self.superuser)
        for model in _list_models():
            for action in BULK_ACTIONS:
                url = bulk_action_url(model, action)
                if url != reverse(action):
                    continue  # dedicated route or no button
                with self.subTest(model=model._meta.label_lower, action=action):
                    response = self.client.post(
                        url,
                        {"model_name": model._meta.label_lower, "pk": ["0"], "return_url": "/"},
                    )
                    # No row matches pk 0: the handler accepts the model and
                    # redirects back with a warning instead of 404/403.
                    self.assertEqual(response.status_code, 302)


class SharedCatalogueBulkTests(TenantTestMixin, TestCase):
    """The twelve formerly broken lists work again, under the same permissions."""

    def setUp(self):
        self.setup_tenant_context(
            permissions=[
                "assets.view_manufacturer",
                "assets.change_manufacturer",
                "assets.delete_manufacturer",
                "assets.view_category",
            ]
        )
        self.dell = Manufacturer.objects.create(name="Dell", slug="dell")
        self.hp = Manufacturer.objects.create(name="HP", slug="hp")

    def test_list_buttons_point_at_the_global_endpoints(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        for list_url in ("/assets/manufacturers/", "/assets/categories/", "/assets/status-labels/"):
            with self.subTest(list_url=list_url):
                html = self.client.get(list_url).content.decode()
                for action in BULK_ACTIONS:
                    self.assertIn('action="' + reverse(action) + '"', html)

    def test_member_with_change_permission_bulk_edits_manufacturers(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        response = self.client.post(
            reverse("bulk_edit"),
            {
                "model_name": "assets.manufacturer",
                "pk": [str(self.dell.pk), str(self.hp.pk)],
                "_selected_fields": ["description"],
                "description": "Preferred vendor",
                "_apply": "1",
                "return_url": "/assets/manufacturers/",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.dell.refresh_from_db()
        self.hp.refresh_from_db()
        self.assertEqual(self.dell.description, "Preferred vendor")
        self.assertEqual(self.hp.description, "Preferred vendor")

    def test_member_with_delete_permission_bulk_deletes_manufacturers(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        response = self.client.post(
            reverse("bulk_delete"),
            {"model_name": "assets.manufacturer", "pk": [str(self.dell.pk)], "_confirm": "1", "return_url": "/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Manufacturer.objects.filter(pk=self.dell.pk).exists())
        self.assertTrue(Manufacturer.objects.filter(pk=self.hp.pk).exists())

    def test_member_without_change_permission_is_refused(self):
        category = Category.objects.create(name="Bulk Policy Probe", slug="bulk-policy-probe")
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        response = self.client.post(
            reverse("bulk_edit"),
            {
                "model_name": "assets.category",
                "pk": [str(category.pk)],
                "_selected_fields": ["description"],
                "description": "changed",
                "_apply": "1",
            },
        )
        self.assertEqual(response.status_code, 403)
        category.refresh_from_db()
        self.assertEqual(category.description, "")

    def test_status_label_bulk_edit_renders_the_form(self):
        label = StatusLabel.objects.create(name="Spare", slug="spare")
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        response = self.client.post(reverse("bulk_edit"), {"model_name": "assets.statuslabel", "pk": [str(label.pk)]})
        self.assertEqual(response.status_code, 200)


class RequestSelectedBulkGuardTests(TenantTestMixin, TestCase):
    """Opening the shared catalogue did not open anything else."""

    def setUp(self):
        self.setup_tenant_context(
            permissions=[
                "extras.view_exporttemplate",
                "extras.change_exporttemplate",
                "extras.delete_exporttemplate",
                "organization.delete_role",
                "organization.change_role",
            ]
        )

    def test_export_templates_stay_superuser_only_on_the_bulk_path(self):
        template = ExportTemplate.objects.create(
            name="CSV",
            template_code="{{ obj }}",
            content_type=ContentType.objects.get_for_model(Asset),
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        edit = self.client.post(
            reverse("bulk_edit"),
            {
                "model_name": "extras.exporttemplate",
                "pk": [str(template.pk)],
                "_selected_fields": ["name"],
                "_apply": "1",
            },
        )
        delete = self.client.post(
            reverse("bulk_delete"),
            {"model_name": "extras.exporttemplate", "pk": [str(template.pk)], "_confirm": "1"},
        )
        self.assertEqual(edit.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        self.assertTrue(ExportTemplate.objects.filter(pk=template.pk).exists())

    def test_role_is_refused_by_both_global_endpoints(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        for action in BULK_ACTIONS:
            with self.subTest(action=action):
                response = self.client.post(
                    reverse(action),
                    {"model_name": "organization.role", "pk": [str(self.tenant_role.pk)], "_confirm": "1"},
                )
                self.assertEqual(response.status_code, 403)
        self.assertTrue(Role.objects.filter(pk=self.tenant_role.pk).exists())

    def test_unknown_models_are_not_resolvable(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        for action in BULK_ACTIONS:
            with self.subTest(action=action):
                response = self.client.post(reverse(action), {"model_name": "contenttypes.contenttype", "pk": ["1"]})
                self.assertEqual(response.status_code, 404)

    def test_managed_definition_models_never_take_the_generic_edit_form(self):
        self.assertFalse(request_selected_bulk_allowed(AssetType, BULK_EDIT))
        self.assertTrue(request_selected_bulk_allowed(AssetType, BULK_DELETE))

    def test_unclassified_tenantless_model_is_refused(self):
        content_type = apps.get_model("contenttypes", "ContentType")
        self.assertFalse(request_selected_bulk_allowed(content_type, BULK_EDIT))
        self.assertFalse(request_selected_bulk_allowed(content_type, BULK_DELETE))

    def test_embedded_table_hides_a_superuser_only_bulk_action(self):
        # The tag serves tables embedded in detail tabs; it must apply the same
        # model-wide policy as the list toolbar and the endpoints.
        request = RequestFactory().get("/extras/export-templates/")
        request.user = self.tenant_user
        context = bulk_action_context(RequestContext(request, {"request": request}), ExportTemplateTable([]))
        self.assertFalse(context["can_change"])
        self.assertFalse(context["can_delete"])
