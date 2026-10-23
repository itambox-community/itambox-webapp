"""Labels are one feature for every labelled model.

The contract test walks the label registry, so a model registered as labelled
is either fully wired -- tenant-scoped, a code that scans back to exactly that
row, the print route, the list and detail buttons -- or the suite fails.
"""

import io
import json

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker
from pypdf import PdfReader

from assets.models import Asset, StatusLabel
from core.models import Job
from core.tasks.utils import TaskStatus
from core.tests.mixins import grant
from extras.forms import LabelTemplateForm
from extras.models import LabelTemplate
from extras.tasks.labels import generate_label_pdf_batch_task
from inventory.models import Accessory
from itambox.labels import label_reference, label_registry, parse_label_reference
from organization.models import Location, Role, Tenant

User = get_user_model()

EXPECTED_LABELLED = {
    "assets.asset",
    "inventory.accessory",
    "inventory.component",
    "inventory.consumable",
    "organization.location",
}


def _make(model, tenant):
    return baker.make(model, tenant=tenant, _fill_optional=False)


def _login(client, user, tenant):
    client.force_login(user)
    session = client.session
    session["active_tenant_id"] = tenant.pk
    session.save()


class LabelContractTests(TestCase):
    """Every labelled model gets the same label behaviour, end to end."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Label Contract", slug="label-contract")
        cls.superuser = User.objects.create_superuser("label-root", "label-root@example.com", "pw")

    def test_the_registry_holds_the_expected_models(self):
        self.assertEqual({model._meta.label_lower for model in label_registry.models()}, EXPECTED_LABELLED)

    def test_every_labelled_model_is_wired_end_to_end(self):
        _login(self.client, self.superuser, self.tenant)
        for model in label_registry.models():
            with self.subTest(model=model._meta.label_lower):
                obj = _make(model, self.tenant)
                self.assertTrue(hasattr(model.objects, "for_scope"))

                # The code scans back to exactly this row, in both encodings.
                self.assertEqual(parse_label_reference(label_reference(obj)), (model, obj.pk))
                with override_settings(ITAMBOX_BASE_URL="https://itam.example.com/itam"):
                    absolute = label_reference(obj)
                    self.assertTrue(absolute.startswith("https://itam.example.com/itam/"))
                    self.assertEqual(parse_label_reference(absolute), (model, obj.pk))

                url = reverse(
                    "label_print", kwargs={"app_label": model._meta.app_label, "model_name": model._meta.model_name}
                )
                # Detail page offers the print button for this object.
                detail = self.client.get(obj.get_absolute_url())
                self.assertContains(detail, f'href="{url}?pk={obj.pk}"')
                # The print page lists it; the built-in template prints it.
                self.assertContains(self.client.get(url, {"pk": obj.pk}), str(obj))
                response = self.client.post(url, {"pk": [obj.pk], "_print": "1", "template": "", "layout": "roll"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertEqual(len(PdfReader(io.BytesIO(response.content)).pages), 1)

    def test_list_toolbar_posts_the_selection_to_the_print_route(self):
        _login(self.client, self.superuser, self.tenant)
        for model in label_registry.models():
            with self.subTest(model=model._meta.label_lower):
                _make(model, self.tenant)
                url = reverse(
                    "label_print", kwargs={"app_label": model._meta.app_label, "model_name": model._meta.model_name}
                )
                listing = self.client.get(reverse(f"{model._meta.app_label}:{model._meta.model_name}_list"))
                self.assertContains(listing, 'action="' + url + '"')

    def test_lists_of_unlabelled_models_offer_no_print_button(self):
        _login(self.client, self.superuser, self.tenant)
        listing = self.client.get(reverse("assets:statuslabel_list"))
        self.assertNotContains(listing, "bulk-label-form")


class LabelReferenceParsingTests(TestCase):
    def test_legacy_asset_scheme_and_unlabelled_models(self):
        self.assertEqual(parse_label_reference("itambox://asset/42"), (Asset, 42))
        self.assertEqual(parse_label_reference(" ITAMBOX://ASSETS.ASSET/42 "), (Asset, 42))
        self.assertIsNone(parse_label_reference("itambox://users.user/1"))
        self.assertIsNone(parse_label_reference("itambox://assets.asset/not-a-pk"))
        self.assertIsNone(parse_label_reference("itambox:ITM-00042"))
        self.assertIsNone(parse_label_reference("https://itam.example.com/assets/assets/42/edit/"))


class GlobalScanTests(TestCase):
    """The global scanner resolves any label and never guesses across tenants."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Kunde A", slug="kunde-a")
        self.tenant_b = Tenant.objects.create(name="Kunde B", slug="kunde-b")
        status = StatusLabel.objects.create(name="Scan ok", slug="scan-ok", type="deployable")
        self.asset_a = Asset.objects.create(name="Laptop A", asset_tag="ITM-00042", status=status, tenant=self.tenant_a)
        self.asset_b = Asset.objects.create(name="Laptop B", asset_tag="ITM-00042", status=status, tenant=self.tenant_b)
        self.url = reverse("scan_resolve")

    def _member(self, username, permissions, tenants):
        user = User.objects.create_user(username=username, password="pw")
        for tenant in tenants:
            role = Role.objects.create(tenant=tenant, name=f"{username}-{tenant.pk}", permissions=permissions)
            grant(user, tenant, role)
        _login(self.client, user, tenants[0])
        return user

    def _scan(self, code):
        response = self.client.get(self.url, {"code": code})
        return response.status_code, json.loads(response.content)

    def test_a_tag_shared_by_two_customers_is_never_resolved_to_one_of_them(self):
        self._member("msp", ["assets.view_asset"], [self.tenant_a, self.tenant_b])
        status, data = self._scan("itambox:ITM-00042")
        self.assertEqual(status, 200)
        self.assertIn(reverse("assets:asset_list"), data["url"])
        self.assertNotIn(self.asset_a.get_absolute_url(), data["url"])

    def test_the_label_reference_opens_exactly_that_customers_asset(self):
        self._member("msp2", ["assets.view_asset"], [self.tenant_a, self.tenant_b])
        for asset in (self.asset_a, self.asset_b):
            with self.subTest(asset=asset.name):
                status, data = self._scan(label_reference(asset))
                self.assertEqual((status, data["url"]), (200, asset.get_absolute_url()))

    def test_legacy_pk_link_still_resolves(self):
        self._member("legacy", ["assets.view_asset"], [self.tenant_a])
        status, data = self._scan(f"itambox://asset/{self.asset_a.pk}")
        self.assertEqual((status, data["url"]), (200, self.asset_a.get_absolute_url()))

    def test_stock_labels_resolve_for_stock_only_users(self):
        accessory = _make(Accessory, self.tenant_a)
        self._member("stock", ["inventory.view_accessory"], [self.tenant_a])
        status, data = self._scan(label_reference(accessory))
        self.assertEqual((status, data["url"]), (200, accessory.get_absolute_url()))

    def test_a_reference_needs_the_view_permission_and_tenant_access(self):
        accessory = _make(Accessory, self.tenant_b)
        self._member("no-stock", ["assets.view_asset"], [self.tenant_a])
        self.assertEqual(self._scan(label_reference(accessory))[0], 404)
        self.assertEqual(self._scan(label_reference(self.asset_b))[0], 404)


class LabelTemplateObjectTypeTests(TestCase):
    def _form(self, barcode_format, *models):
        return LabelTemplateForm(
            data={
                "name": f"tpl-{barcode_format}-{len(models)}",
                "content_types": [ContentType.objects.get_for_model(model).pk for model in models],
                "page_width": 2.0,
                "page_height": 1.0,
                "barcode_format": barcode_format,
                "template_code": "",
            }
        )

    def test_object_types_are_required_and_limited_to_labelled_models(self):
        self.assertIn("content_types", self._form("qr").errors)
        form = self._form("qr", StatusLabel)
        self.assertIn("content_types", form.errors)

    def test_linear_barcodes_only_for_models_with_a_short_identifier(self):
        self.assertTrue(self._form("code128", Asset).is_valid())
        form = self._form("code128", Asset, Accessory)
        self.assertFalse(form.is_valid())
        self.assertIn("barcode_format", form.errors)
        self.assertTrue(self._form("qr", Asset, Accessory, Location).is_valid())

    def test_migration_binds_existing_templates_to_assets_and_retires_data_matrix(self):
        migration = __import__("extras.migrations.0005_label_template_object_types", fromlist=["x"])
        legacy = LabelTemplate.objects.create(name="Legacy DM", barcode_format="qr")
        LabelTemplate.objects.filter(pk=legacy.pk).update(barcode_format="datamatrix")

        migration.bind_existing_templates_to_assets(django_apps, None)
        migration.bind_existing_templates_to_assets(django_apps, None)  # idempotent

        legacy.refresh_from_db()
        self.assertEqual(legacy.barcode_format, "qr")
        self.assertEqual(list(legacy.content_types.all()), [ContentType.objects.get_for_model(Asset)])


class QueuedLabelJobTests(TestCase):
    """The queued path serves any labelled model under the persisted scope."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Queue Tenant", slug="queue-tenant")
        self.user = User.objects.create_superuser("queue-root", "queue-root@example.com", "pw")
        self.items = [_make(Accessory, self.tenant) for _ in range(3)]
        self.content_type = ContentType.objects.get_for_model(Accessory)

    def _job(self, **data):
        return Job.objects.create(
            name="labels",
            tenant=self.tenant,
            status=Job.STATUS_PENDING,
            data={"scope_tenant_ids": [self.tenant.pk], **data},
        )

    def test_built_in_template_renders_a_stock_batch(self):
        job = self._job()
        result = generate_label_pdf_batch_task(
            job.pk,
            self.content_type.pk,
            [item.pk for item in self.items],
            None,
            "a4_grid",
            self.user.pk,
            self.tenant.pk,
        )
        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.SUCCESS, "labels.pdf_completed"))
        self.assertEqual(job.status, Job.STATUS_COMPLETED)

    def test_a_template_for_another_object_type_is_terminal(self):
        assets_only = LabelTemplate.objects.create(name="Assets only")
        assets_only.content_types.add(ContentType.objects.get_for_model(Asset))
        job = self._job()
        result = generate_label_pdf_batch_task(
            job.pk, self.content_type.pk, [self.items[0].pk], assets_only.pk, "roll", self.user.pk, self.tenant.pk
        )
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.template_not_found"))

    def test_an_unlabelled_model_is_terminal(self):
        job = self._job()
        result = generate_label_pdf_batch_task(
            job.pk, ContentType.objects.get_for_model(StatusLabel).pk, [1], None, "roll", self.user.pk, self.tenant.pk
        )
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.model_not_labelled"))

    def test_shared_rows_need_the_flag_recorded_at_enqueue(self):
        shared = baker.make(Location, tenant=None, _fill_optional=False)
        location_type = ContentType.objects.get_for_model(Location)
        refused = generate_label_pdf_batch_task(
            self._job().pk, location_type.pk, [shared.pk], None, "roll", self.user.pk, self.tenant.pk
        )
        allowed = generate_label_pdf_batch_task(
            self._job(include_shared=True).pk, location_type.pk, [shared.pk], None, "roll", self.user.pk, self.tenant.pk
        )
        self.assertEqual(refused.code, "labels.objects_not_accessible")
        self.assertEqual(allowed.code, "labels.pdf_completed")
