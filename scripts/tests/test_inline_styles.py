import unittest
from pathlib import Path

from scripts.check_inline_styles import scan_repository, scan_source, tracked_source_files


class InlineStylePolicyTests(unittest.TestCase):
    def test_current_repository_has_no_unapproved_findings(self):
        self.assertEqual(scan_repository(), [])

    def test_detects_html_style_attributes_and_missing_nonces(self):
        findings = scan_source("itambox/templates/example.html", '<div style="display:none"><style>.x{}</style>')

        self.assertEqual([finding.rule for finding in findings], ["CSP-STYLE2", "CSP-STYLE3"])

    def test_accepts_nonce_authorized_browser_style_elements(self):
        self.assertEqual(
            scan_source("itambox/templates/example.html", '<style nonce="{{ request.csp_nonce }}">.x{}</style>'),
            [],
        )

    def test_detects_python_html_and_frontend_dom_style_writes(self):
        python_findings = scan_source("itambox/core/example.py", "return '<span style=\"color:red\">x</span>'")
        frontend_findings = scan_source("itambox/static/src/example.ts", "element.style.display = 'none'")

        self.assertEqual([finding.rule for finding in python_findings], ["CSP-STYLE4"])
        self.assertEqual([finding.rule for finding in frontend_findings], ["CSP-STYLE6"])

    def test_does_not_misclassify_graphviz_style_attributes(self):
        self.assertEqual(scan_source("itambox/core/export.py", 'node [style="rounded,filled"];'), [])

    def test_pdf_and_standalone_style_exceptions_are_centralized(self):
        self.assertEqual(
            scan_source("itambox/core/html_sanitizer.py", "return f'<style>{rules}</style>'"),
            [],
        )
        self.assertEqual(
            scan_source("itambox/extras/services/labels.py", "return '<style>.label{}</style>'"),
            [],
        )
        self.assertEqual(
            scan_source("itambox/core/reports/charts.py", 'return f"<style{nonce}>{css}</style>"'),
            [],
        )

    def test_unsafe_inline_is_a_production_finding_outside_attribute_exception(self):
        findings = scan_source("itambox/itambox/middleware.py", "style-src 'unsafe-inline'")

        self.assertEqual([finding.rule for finding in findings], ["CSP-STYLE1"])

    def test_runtime_style_attribute_exception_is_narrow(self):
        self.assertEqual(
            scan_source("itambox/itambox/middleware.py", "style-src-attr 'unsafe-inline'"),
            [],
        )
        source = "style-src 'unsafe-inline'; style-src-attr 'unsafe-inline'; style-src-elem 'unsafe-inline'"
        findings = scan_source("itambox/itambox/middleware.py", source)

        self.assertEqual([finding.rule for finding in findings], ["CSP-STYLE1", "CSP-STYLE1"])

    def test_inventory_is_gettracked_and_nonempty(self):
        self.assertTrue(tracked_source_files(Path.cwd()))


if __name__ == "__main__":
    unittest.main()
