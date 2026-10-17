"""Category importer parity (#851): description, colour, applies-to targets and audit cadence."""

import csv
import io

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from assets.forms.import_forms import CategoryBulkImportForm
from assets.models import Category
from core.csv_utils import csv_safe
from core.importers.bulk_forms import get_registered_import_form
from core.models import Job
from core.tests.mixins import TenantTestMixin
from organization.models import Role

ALL_FALSE = {"asset": False, "accessory": False, "consumable": False, "component": False}


class CategoryRowMappingTests(SimpleTestCase):
    def setUp(self):
        self.form = CategoryBulkImportForm()

    def test_declared_columns_drive_the_field_options_table(self):
        self.assertIs(get_registered_import_form(Category), CategoryBulkImportForm)
        self.assertEqual(
            self.form.field_names,
            ["name", "slug", "description", "color", "applies_to", "audit_interval_months"],
        )

    def test_values_map_onto_model_fields(self):
        mapped = self.form.map_row(
            {
                "name": "Laptops",
                "color": "#00FF00",
                "applies_to": "Asset, component",
                "audit_interval_months": "12",
                "description": "Mobile",
            }
        )
        self.assertEqual(mapped["color"], "00FF00")
        self.assertEqual(mapped["applies_to"], {**ALL_FALSE, "asset": True, "component": True})
        self.assertEqual(mapped["audit_interval_months"], 12)
        self.assertEqual(mapped["description"], "Mobile")

    def test_empty_audit_interval_means_no_cadence(self):
        self.assertIsNone(self.form.map_row({"name": "x", "audit_interval_months": ""})["audit_interval_months"])

    def test_absent_optional_columns_are_not_mapped(self):
        self.assertEqual(self.form.map_row({"name": "x"}), {"name": "x"})

    def test_export_notation_of_applies_to_reads_back(self):
        mapped = self.form.map_row({"name": "x", "applies_to": str({**ALL_FALSE, "accessory": True})})
        self.assertEqual(mapped["applies_to"], {**ALL_FALSE, "accessory": True})

    def test_bad_values_fail_with_readable_messages(self):
        cases = (
            ({"color": "green"}, "color", "6-digit hex"),
            ({"color": "12345"}, "color", "6-digit hex"),
            ({"applies_to": "asset,laptop"}, "applies_to", "unknown target 'laptop'"),
            ({"applies_to": "{broken"}, "applies_to", "comma-separated"),
            ({"audit_interval_months": "0"}, "audit_interval_months", "greater than zero"),
            ({"audit_interval_months": "-3"}, "audit_interval_months", "greater than zero"),
            ({"audit_interval_months": "monthly"}, "audit_interval_months", "greater than zero"),
        )
        for extra, field, fragment in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(ValidationError) as ctx:
                    self.form.map_row({"name": "x", **extra})
                self.assertIn(fragment, " ".join(ctx.exception.message_dict[field]))


class CategoryImportRouteTests(TenantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.clear_tenant_context()
        self.setup_tenant_context(name="Category Parity Tenant", slug="category-parity-tenant")
        user = get_user_model().objects.create_user(username="cat-editor", email="cat@example.com", password="pw")
        role = Role.objects.create(
            tenant=self.tenant,
            name="cat role",
            permissions=["assets.add_category", "assets.change_category", "assets.view_category"],
        )
        self.grant(user, self.tenant, role)
        self.user = user
        self.url = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "category"})

    def tearDown(self):
        self.clear_tenant_context()
        super().tearDown()

    def _import(self, csv_text):
        self.client_login_to_tenant(self.user, self.tenant)
        data = {"import_text": csv_text, "active_tab": "editor", "import_format": "csv", "delimiter": ","}
        preview = self.client.post(self.url, {**data, "_preview": "1"})
        if preview.context["form"].errors:
            return preview, None
        return preview, self.client.post(self.url, {"active_tab": "editor", "_confirm": "1"})

    def _csv(self, **extra):
        values = {
            "name": "Parity Cat",
            "slug": "parity-cat",
            "description": "Described",
            "color": "ff8800",
            "applies_to": "asset,accessory",
            "audit_interval_months": "6",
            **extra,
        }
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(list(values))
        writer.writerow(list(values.values()))
        return out.getvalue()

    def _category(self):
        return Category._base_manager.get(name="Parity Cat")

    def test_create_then_unchanged_reimport_is_skipped(self):
        _preview, confirmed = self._import(self._csv())
        self.assertEqual(confirmed.status_code, 302)
        category = self._category()
        self.assertEqual(category.description, "Described")
        self.assertEqual(category.color, "ff8800")
        self.assertEqual(category.audit_interval_months, 6)
        self.assertEqual(category.applies_to, {**ALL_FALSE, "asset": True, "accessory": True})
        self.assertEqual(Job.objects.latest("created").result["imported"], 1)

        preview, confirmed = self._import(self._csv())
        self.assertEqual(confirmed.status_code, 302)
        self.assertTrue(preview.context["preview_notes"])
        self.assertEqual(Job.objects.latest("created").result["skipped"], 1)

    def test_update_changes_each_new_field(self):
        self._import(self._csv())
        _preview, confirmed = self._import(
            self._csv(description="Changed", color="#112233", applies_to="component", audit_interval_months="")
        )
        self.assertEqual(confirmed.status_code, 302)
        category = self._category()
        self.assertEqual(category.description, "Changed")
        self.assertEqual(category.color, "112233")
        self.assertIsNone(category.audit_interval_months)
        self.assertEqual(category.applies_to, {**ALL_FALSE, "component": True})

    def test_invalid_rows_fail_the_preview_with_row_messages(self):
        for extra, fragment in (
            ({"color": "zzzzzz"}, "6-digit hex"),
            ({"applies_to": "bogus"}, "unknown target"),
            ({"audit_interval_months": "0"}, "greater than zero"),
        ):
            with self.subTest(extra=extra):
                preview, confirmed = self._import(self._csv(**extra))
                self.assertIsNone(confirmed)
                errors = " ".join(str(e) for e in preview.context["form"].non_field_errors())
                self.assertIn("Row 2", errors)
                self.assertIn(fragment, errors)
        self.assertFalse(Category._base_manager.filter(name="Parity Cat").exists())

    def test_exported_category_imports_back_unchanged(self):
        self._import(self._csv())
        category = self._category()
        fields = [f for f in Category._meta.fields if not f.many_to_many]
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([f.name for f in fields])
        writer.writerow([csv_safe(getattr(category, f.name)) for f in fields])
        before = Job.objects.count()
        _preview, confirmed = self._import(out.getvalue())
        self.assertEqual(confirmed.status_code, 302)
        self.assertEqual(Job.objects.count(), before + 1)
        self.assertEqual(Job.objects.latest("created").result["skipped"], 1)
        self.assertEqual(self._category().applies_to, category.applies_to)

    def test_tenant_column_is_rejected(self):
        preview, confirmed = self._import(self._csv(tenant="x"))
        self.assertIsNone(confirmed)
        self.assertTrue(preview.context["form"].errors)
        self.assertIn("enant", " ".join(str(e) for e in preview.context["form"].non_field_errors()))
