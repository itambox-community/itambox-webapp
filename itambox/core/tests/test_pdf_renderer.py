"""Unit contracts for the shared PDF renderer leaf (issue #100)."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from core.pdf_renderer import html_to_pdf_bytes, pdf_safe_link_callback


class PdfSafeLinkCallbackTests(SimpleTestCase):
    def test_data_uris_pass_through(self):
        uri = "data:image/png;base64,AAAA"
        self.assertEqual(pdf_safe_link_callback(uri, "image"), uri)

    def test_remote_urls_are_refused(self):
        self.assertEqual(pdf_safe_link_callback("https://example.test/a.png", "image"), "")
        self.assertEqual(pdf_safe_link_callback("http://internal.local/x", "image"), "")
        self.assertEqual(pdf_safe_link_callback("//example.test/x", "image"), "")

    @override_settings(STATIC_URL="/static/", STATIC_ROOT=None, MEDIA_URL=None, MEDIA_ROOT=None)
    def test_static_prefix_without_root_is_refused(self):
        self.assertEqual(pdf_safe_link_callback("/static/a.png", "image"), "")

    def test_static_file_below_root_resolves_and_traversal_is_refused(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            candidate = os.path.join(root, "a.png")
            with open(candidate, "w", encoding="utf-8") as handle:
                handle.write("png")
            with override_settings(STATIC_URL="/static/", STATIC_ROOT=root, MEDIA_URL=None, MEDIA_ROOT=None):
                self.assertEqual(pdf_safe_link_callback("/static/a.png", "image"), os.path.abspath(candidate))
                self.assertEqual(pdf_safe_link_callback("/static/../secret.png", "image"), "")


class HtmlToPdfBytesTests(SimpleTestCase):
    def _fake_pisa(self, *, err, content=b"%PDF-1.4"):
        def create_pdf(html, dest=None, link_callback=None):
            if dest is not None:
                dest.write(content)
            return SimpleNamespace(err=err)

        return create_pdf

    def test_render_success_returns_buffer_bytes(self):
        with mock.patch("xhtml2pdf.pisa.CreatePDF", side_effect=self._fake_pisa(err=0, content=b"%PDF-1.4")):
            self.assertEqual(html_to_pdf_bytes("<html></html>"), b"%PDF-1.4")

    def test_render_failure_raises(self):
        with mock.patch("xhtml2pdf.pisa.CreatePDF", side_effect=self._fake_pisa(err=1)):
            with self.assertRaisesRegex(RuntimeError, "xhtml2pdf rendering failed"):
                html_to_pdf_bytes("<html></html>")
