import base64
import io
from types import SimpleNamespace

import segno
from django.test import SimpleTestCase, TestCase, override_settings
from model_bakery import baker
from pypdf import PdfReader

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from extras.models import LabelTemplate
from extras.services.labels import (
    LabelContent,
    LabelNotApplicable,
    barcode_data_uri,
    build_labels_document,
    default_label_card,
    label_content,
    label_print_css,
    render_label_card,
    render_labels_pdf,
    render_object_card,
)
from inventory.models import Accessory
from itambox.labels import label_reference
from organization.models import Tenant


def _content(**values):
    """A LabelContent shaped like an asset's (alias ``asset``)."""
    values = {"asset_tag": "IT-1", "name": "Laptop", "serial_number": "SN-1", "location": "", "status": "", **values}
    return LabelContent(
        title=values["name"],
        values=values,
        field_labels={key: key.replace("_", " ").title() for key in values},
        reference="itambox://assets.asset/1",
        linear_value=values["asset_tag"],
        template_alias="asset",
        model_name="asset",
    )


class LabelRendererHTMLTests(SimpleTestCase):
    def setUp(self):
        self.asset = _content()
        self.barcode_uri = "data:image/png;base64,AAAA"

    def test_default_label_card_uses_classes_instead_of_inline_styles(self):
        rendered = default_label_card(self.asset, self.barcode_uri)

        self.assertNotIn("style=", rendered.lower())
        self.assertIn('class="label-card-table"', rendered)
        self.assertIn('class="label-card-barcode"', rendered)

    def test_custom_label_is_sanitized_after_jinja_rendering(self):
        label_template = SimpleNamespace(
            name="Custom",
            template_code='<div onclick="alert(1)"><span style="color: #123456">{{ asset.name }}</span><script>x</script></div>',
            barcode_format="qr",
        )

        rendered = render_label_card(self.asset, label_template, self.barcode_uri)

        self.assertNotIn("style=", rendered.lower())
        self.assertNotIn("onclick", rendered.lower())
        self.assertNotIn("<script", rendered.lower())
        self.assertIn("Laptop", rendered)
        self.assertIn("#123456", rendered)

    def test_label_jinja_exposes_scalars_and_escapes_untrusted_values(self):
        self.asset = _content(name="<b>owned</b>")
        label_template = SimpleNamespace(
            name="Unsafe context",
            template_code="<span>{{ asset.name }}</span>{{ asset.__class__ }}",
            barcode_format="qr",
        )

        rendered = render_label_card(self.asset, label_template, self.barcode_uri)

        self.assertIn("&lt;b&gt;owned&lt;/b&gt;", rendered)
        self.assertNotIn("<b>owned</b>", rendered)
        self.assertNotIn("__class__", rendered)

    def test_custom_label_only_keeps_the_generated_barcode_data_uri(self):
        label_template = SimpleNamespace(
            name="Unsafe resource",
            template_code=('<img src="data:image/png;base64,BBBB"><img src="{{ barcode_data_uri }}">'),
            barcode_format="qr",
        )

        rendered = render_label_card(self.asset, label_template, self.barcode_uri)

        self.assertNotIn("BBBB", rendered)
        self.assertIn(self.barcode_uri, rendered)

    def test_pdf_document_uses_safe_measurements_and_class_page_breaks(self):
        label_template = SimpleNamespace(page_width="2.25in; color: red", page_height=float("inf"))
        document = build_labels_document(["<div>Label</div>"], label_template, "roll")

        self.assertNotIn("style=", document.lower())
        self.assertIn("size: 2.25in 1.25in", document)
        self.assertIn('class="label-card page-break-avoid"', document)
        self.assertNotIn("color: red", document)

    def test_custom_label_size_limits_fall_back_to_the_default_card(self):
        oversized_source = SimpleNamespace(
            name="Oversized source",
            template_code="x" * (64 * 1024 + 1),
            barcode_format="qr",
        )
        oversized_output = render_label_card(self.asset, oversized_source, self.barcode_uri)
        self.assertIn('class="label-card-table"', oversized_output)

        oversized_rendered = SimpleNamespace(
            name="Oversized output",
            template_code="{{ 'x' * 262145 }}",
            barcode_format="qr",
        )
        rendered_output = render_label_card(self.asset, oversized_rendered, self.barcode_uri)
        self.assertIn('class="label-card-table"', rendered_output)

    def test_custom_template_fallback_redacts_template_and_exception_details(self):
        label_template = SimpleNamespace(
            pk=17,
            name="customer-secret-template",
            template_code="{{ invalid",
            barcode_format="qr",
        )

        with self.assertLogs("extras.services.labels", level="WARNING") as captured:
            rendered = render_label_card(self.asset, label_template, self.barcode_uri)

        log_output = " ".join(captured.output)
        self.assertIn('class="label-card-table"', rendered)
        self.assertNotIn("customer-secret-template", log_output)
        self.assertNotIn("invalid", log_output)

    def test_grid_document_pads_cards_and_marks_page_boundaries(self):
        label_template = SimpleNamespace()
        document = build_labels_document(["<div>Label</div>"] * 25, label_template, "a4_grid")

        self.assertIn('class="grid-table page-break-always"', document)
        self.assertIn('class="grid-table page-break-avoid"', document)
        self.assertIn("&nbsp;", document)


class LabelPrintCssSourceTests(SimpleTestCase):
    def test_label_print_css_checkout_source_is_present_and_nonempty(self):
        # The runtime PDF document builder reads this authored stylesheet from
        # the checkout (and from the packaged runtime image, which is verified
        # by the production-image check in the PR evidence, not by this unit
        # test). This test guards the checkout source against pruning.
        css = label_print_css()
        self.assertTrue(css.strip())
        self.assertIn(".label-card", css)
        self.assertIn("page-break-always", css)


class LabelPdfRenderIntegrationTests(TestCase):
    """Real two-label PDF compilation through the locked xhtml2pdf engine.

    No mocks: the real barcode generator, label renderer, document builder and
    the shared ``html_to_pdf_bytes()`` invocation are exercised so that a
    regression in the packaged label CSS, the document markup or the renderer
    call itself surfaces as a structural test failure (issue #453).
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="PDF Tenant", slug="pdf-tenant")
        manufacturer = Manufacturer.objects.create(name="PDF Mfr", slug="pdf-mfr")
        role = AssetRole.objects.create(name="PDF Role", slug="pdf-role")
        asset_type = AssetType.objects.create(manufacturer=manufacturer, model="PDF Model", slug="pdf-model")
        status, _ = StatusLabel.objects.get_or_create(
            slug="available", defaults={"name": "Available", "type": "deployable"}
        )
        self.asset1 = Asset.objects.create(
            name="One PDF Asset",
            asset_tag="PDF-1",
            asset_type=asset_type,
            asset_role=role,
            status=status,
            tenant=self.tenant,
        )
        self.asset2 = Asset.objects.create(
            name="Two PDF Asset",
            asset_tag="PDF-2",
            asset_type=asset_type,
            asset_role=role,
            status=status,
            tenant=self.tenant,
        )
        # seed-equivalent "Standard QR Asset Label" template code (2.0 x 1.0 in)
        self.template = LabelTemplate.objects.create(
            name="Standard QR Asset Label",
            description="2.0 x 1.0 inch QR label for laptops & desktops (seed-equivalent)",
            barcode_format="qr",
            page_width=2.0,
            page_height=1.0,
            template_code=(
                '<table class="label-card-table"><tr>'
                '<td class="label-card-metadata"><div class="label-card-title">{{ asset.name }}</div>'
                '<div class="label-card-tag">{{ asset.asset_tag }}</div></td>'
                '<td class="label-card-barcode-cell">{{ barcode_img }}</td></tr></table>'
            ),
        )

    def test_real_two_label_documents_compile_to_valid_pdf(self):
        cases = (("roll", 2), ("a4_grid", 1))
        for layout, expected_pages in cases:
            with self.subTest(layout=layout):
                cards = [render_object_card(asset, self.template) for asset in (self.asset1, self.asset2)]

                document = build_labels_document(cards, self.template, layout)
                self.assertIn(".label-card", document)

                pdf_bytes = render_labels_pdf([self.asset1, self.asset2], self.template, layout)

                self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
                reader = PdfReader(io.BytesIO(pdf_bytes))
                self.assertEqual(len(reader.pages), expected_pages)


class LabelContentTests(TestCase):
    """The engine is generic: what a label says and encodes comes from the registration."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Content Tenant", slug="content-tenant")
        status, _ = StatusLabel.objects.get_or_create(
            slug="available", defaults={"name": "Available", "type": "deployable"}
        )
        self.asset = Asset.objects.create(name="Mapped", asset_tag="MAP-1", status=status, tenant=self.tenant)
        self.accessory = baker.make(Accessory, name="USB-C Dock", tenant=self.tenant)

    def test_qr_encodes_exactly_the_object_reference(self):
        content = label_content(self.asset)
        self.assertEqual(content.reference, f"itambox://assets.asset/{self.asset.pk}")
        buffer = io.BytesIO()
        segno.make_qr(content.reference).save(buffer, kind="png", scale=6, border=4)
        expected = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        self.assertEqual(barcode_data_uri(content, "qr"), expected)

    @override_settings(ITAMBOX_BASE_URL="https://itam.example.com")
    def test_reference_is_the_absolute_detail_url_when_a_base_url_is_set(self):
        self.assertEqual(
            label_reference(self.accessory), "https://itam.example.com" + self.accessory.get_absolute_url()
        )

    def test_former_asset_templates_keep_rendering_through_the_alias(self):
        template = SimpleNamespace(
            pk=None, name="Legacy", barcode_format="qr", template_code="{{ asset.asset_tag }}|{{ obj.name }}"
        )
        rendered = render_label_card(label_content(self.asset), template, "data:image/png;base64,AAAA")
        self.assertIn("MAP-1|Mapped", rendered)

    def test_linear_barcodes_are_refused_for_qr_only_models(self):
        content = label_content(self.accessory)
        self.assertIsNone(content.linear_value)
        with self.assertRaises(LabelNotApplicable):
            barcode_data_uri(content, LabelTemplate.FORMAT_CODE128)
        self.assertTrue(
            barcode_data_uri(label_content(self.asset), LabelTemplate.FORMAT_CODE128).startswith("data:image/png")
        )

    def test_built_in_card_prints_a_stock_item(self):
        card = render_object_card(self.accessory, LabelTemplate(name="Built-in", barcode_format="qr"))
        self.assertIn("USB-C Dock", card)
        self.assertIn('class="label-card-barcode"', card)
