from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_product_language.py"
spec = importlib.util.spec_from_file_location("check_product_language", SCRIPT)
assert spec and spec.loader
MODULE = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = MODULE
spec.loader.exec_module(MODULE)


class ProductLanguageGateTests(unittest.TestCase):
    @staticmethod
    def write(tmp_path: Path, name: str, source: str) -> Path:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_forbidden_tokens_cover_unicode_entities_and_escape_notation(self):
        samples = (
            "plain – dash",
            "plain — dash",
            "&NDASH;",
            "&#8212;",
            "&#x02013;",
            r"\u2014",
            r"\u{2013}",
            r"\U00002014",
            r"\N{EN DASH}",
        )
        self.assertTrue(all(MODULE.forbidden_tokens(sample) for sample in samples))

    def test_python_scan_rejects_concatenation_chr_join_format_and_f_string(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.py",
                "\n".join(
                    (
                        "_('left ' + chr(0x2013) + ' right')",
                        "gettext(''.join(['left &n', 'dash; right']))",
                        "gettext('left &#x{:x}; right'.format(8212))",
                        "format_html(f'left {chr(8211)} right')",
                    )
                ),
            )
            findings = MODULE.scan_python(path, "itambox/example/copy.py")
            self.assertEqual([finding.line for finding in findings], [1, 2, 3, 4])

    def test_python_scan_checks_crispy_html_and_submit_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.py",
                "HTML('<span>left — right</span>')\nSubmit('submit', 'left – right')",
            )
            findings = MODULE.scan_python(path, "itambox/forms/copy.py")
            self.assertEqual([finding.line for finding in findings], [1, 2])

    def test_frozen_string_allowlist_only_allows_the_exact_compatibility_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "model.py",
                "class Warranty:\n"
                "    def __str__(self):\n"
                "        return f'{self.name} – {self.name}'\n\n"
                "class WarrantyChanged:\n"
                "    def __str__(self):\n"
                "        return f'new – {self.name}'\n",
            )
            findings = MODULE.scan_python(path, "itambox/assets/models/lifecycle.py")
            self.assertEqual([(finding.line, finding.tokens) for finding in findings], [(7, ("en dash",))])

    def test_dynamic_presentation_f_string_literals_are_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.py",
                "from django.utils.html import format_html\n"
                "def render(value):\n"
                "    return format_html(f'left – {value}')\n",
            )
            findings = MODULE.scan_python(path, "itambox/forms/copy.py")
            self.assertEqual([(finding.line, finding.field) for finding in findings], [(3, "format_html")])

    def test_python_scan_checks_direct_form_message_and_model_string_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.py",
                "\n".join(
                    (
                        "from django import forms",
                        "field = forms.CharField(label='left – right', "
                        "widget=forms.TextInput(attrs={'placeholder': 'left — right'}))",
                        "def __str__(self):",
                        "    return f'left — {self.name}'",
                        "messages.success(request, 'left – right')",
                    )
                ),
            )
            findings = MODULE.scan_python(path, "itambox/example/copy.py")
            self.assertEqual([finding.line for finding in findings], [2, 2, 4, 5])

    def test_javascript_scan_rejects_split_entities_and_codepoint_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.ts",
                "gettext('left &n' + 'dash; right');\ngettext('left ' + String.fromCodePoint(0x2014) + ' right');",
            )
            findings = MODULE.scan_javascript(path, "itambox/static/src/copy.ts")
            self.assertEqual([finding.line for finding in findings], [1, 2])

    def test_javascript_scan_checks_direct_dom_text_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.ts",
                "document.getElementById('status').textContent = 'left — right';",
            )
            findings = MODULE.scan_javascript(path, "itambox/static/src/copy.ts")
            self.assertEqual([finding.line for finding in findings], [1])

    def test_javascript_dom_scan_checks_all_rhs_literals_and_codepoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.ts",
                "node.textContent = 'left ' + '– right';\n"
                "node.setAttribute('title', 'left ' + String.fromCharCode(0x2014) + ' right');",
            )
            findings = MODULE.scan_javascript(path, "itambox/static/src/copy.ts")
            self.assertEqual([finding.line for finding in findings], [1, 2])

    def test_template_scan_ignores_comments_but_checks_rendered_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                Path(directory),
                "copy.html",
                "{# hidden — copy #}\n<!-- hidden &mdash; copy -->\n<span>visible &#x2013; copy</span>",
            )
            findings = MODULE.scan_template(path, "itambox/templates/copy.html")
            self.assertEqual(
                [(finding.line, finding.field) for finding in findings],
                [(3, "rendered template")],
            )

    def test_po_scan_checks_translation_even_when_exact_contract_msgid_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            relative, msgid = next(iter(MODULE.FROZEN_CONTRACT_COPY))
            path = self.write(
                Path(directory),
                "django.po",
                '#: %s:1\nmsgid %s\nmsgstr "Deutsch — nicht erlaubt"\n'
                % (relative, json.dumps(msgid, ensure_ascii=False)),
            )
            findings = MODULE.scan_po(path, "itambox/locale/de/LC_MESSAGES/django.po")
            self.assertEqual(
                [(finding.field, finding.tokens) for finding in findings],
                [("msgstr", ("em dash",))],
            )

    def test_po_allowlist_requires_a_matching_source_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            relative, msgid = next(iter(MODULE.FROZEN_CONTRACT_COPY))
            path = self.write(
                Path(directory),
                "django.po",
                '#: itambox/other/models.py:1\nmsgid %s\nmsgstr "Deutsch"\n' % json.dumps(msgid, ensure_ascii=False),
            )
            findings = MODULE.scan_po(path, "itambox/locale/de/LC_MESSAGES/django.po")
            self.assertEqual([(finding.field, finding.tokens) for finding in findings], [("msgid", ("em dash",))])

    def test_frozen_metadata_allowlist_is_path_and_message_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            relative, msgid = next(iter(MODULE.FROZEN_CONTRACT_COPY))
            path = self.write(Path(directory), "model.py", f"_({msgid!r})\n")
            self.assertEqual(MODULE.scan_python(path, relative), [])
            self.assertTrue(MODULE.scan_python(path, "itambox/other/models.py"))

    def test_python_scan_checks_the_table_empty_value_constant(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "constants.py"
            for expression, token in (("'–'", "en dash"), ("'—'", "em dash"), ("chr(0x2013)", "en dash")):
                with self.subTest(expression=expression):
                    path.write_text(f"TABLE_EMPTY_VALUE = {expression}\n", encoding="utf-8")
                    findings = MODULE.scan_python(path, "itambox/core/tables/constants.py")
                    self.assertEqual(
                        [(finding.line, finding.field, finding.tokens) for finding in findings],
                        [(1, "TABLE_EMPTY_VALUE", (token,))],
                    )

    def test_current_repository_passes_product_language_gate(self):
        self.assertEqual(MODULE.main(), 0)


if __name__ == "__main__":
    unittest.main()
