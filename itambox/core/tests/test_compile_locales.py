"""Focused tests for the compile_locales management command.

The command must compile both supported catalogue domains (django + djangojs)
per locale, preserve locale filtering, omit fuzzy/empty entries, fail clearly
on malformed input, tolerate locales that only contain django.po, and produce
deterministic output for unchanged input.
"""

import io
import shutil
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from core.management.commands.compile_locales import _compile_po

HEADER = """#: reference.js:1
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

"""

SIMPLE_ENTRY = """#: reference.js:2
msgid "Hello"
msgstr "Hallo"
"""


def write_catalog(locale_dir: Path, domain: str, content: str) -> Path:
    messages_dir = locale_dir / "LC_MESSAGES"
    messages_dir.mkdir(parents=True, exist_ok=True)
    po_file = messages_dir / f"{domain}.po"
    po_file.write_text(content, encoding="utf-8")
    return po_file


def locale_fixture(testcase, de: bool = True, js: bool = True) -> Path:
    base = Path(tempfile.mkdtemp(prefix="compile-locales-test-"))
    testcase.addCleanup(shutil.rmtree, base, True)
    if de:
        write_catalog(base / "locale" / "de", "django", HEADER + SIMPLE_ENTRY)
        if js:
            write_catalog(base / "locale" / "de", "djangojs", HEADER + SIMPLE_ENTRY)
    return base


class CompileLocalesTests(SimpleTestCase):
    def test_both_domains_compile_and_are_reported(self):
        base = locale_fixture(self, de=True, js=True)
        with override_settings(BASE_DIR=base):
            out = io.StringIO()
            call_command("compile_locales", stdout=out)

        messages_dir = base / "locale" / "de" / "LC_MESSAGES"
        self.assertTrue((messages_dir / "django.mo").exists())
        self.assertTrue((messages_dir / "djangojs.mo").exists())
        self.assertIn("django.po", out.getvalue())
        self.assertIn("djangojs.po", out.getvalue())
        self.assertIn("2 catalog(s) compiled", out.getvalue())

    def test_locale_filtering_compiles_only_requested_locale(self):
        base = locale_fixture(self, de=True, js=True)
        write_catalog(base / "locale" / "fr", "django", HEADER + SIMPLE_ENTRY)

        with override_settings(BASE_DIR=base):
            out = io.StringIO()
            call_command("compile_locales", "de", stdout=out)

        self.assertTrue((base / "locale" / "de" / "LC_MESSAGES" / "django.mo").exists())
        self.assertFalse((base / "locale" / "fr" / "LC_MESSAGES" / "django.mo").exists())
        # Filtering keeps the "de" locale with both of its domains.
        self.assertIn("2 catalog(s) compiled", out.getvalue())
        self.assertNotIn("fr", out.getvalue())

    def test_fuzzy_and_empty_entries_are_omitted(self):
        po = (
            HEADER
            + """#: reference.js:3
#, fuzzy
msgid "Fuzzy entry"
msgstr "Unscharf"

#: reference.js:4
msgid "Empty entry"
msgstr ""

#: reference.js:5
msgid "Real entry"
msgstr "Echt"
"""
        )
        base = locale_fixture(self, de=True, js=True)
        po_file = write_catalog(base / "locale" / "de", "django", po)

        mo_file = po_file.with_suffix(".mo")
        _compile_po(str(po_file), str(mo_file))
        mo_bytes = mo_file.read_bytes()

        self.assertIn(b"Real entry", mo_bytes)
        self.assertIn(b"Echt", mo_bytes)
        self.assertNotIn(b"Fuzzy entry", mo_bytes)
        self.assertNotIn(b"Unscharf", mo_bytes)
        self.assertNotIn(b"Empty entry", mo_bytes)

    def test_contextual_plural_entries_compile(self):
        po = (
            HEADER
            + """msgctxt "context"
msgid "Item"
msgid_plural "Items"
msgstr[0] "Eintrag"
msgstr[1] "Einträge"
"""
        )
        base = locale_fixture(self, de=True, js=False)
        po_file = write_catalog(base / "locale" / "de", "django", po)
        mo_file = po_file.with_suffix(".mo")

        _compile_po(str(po_file), str(mo_file))

        import gettext

        with open(mo_file, "rb") as fp:
            translations = gettext.GNUTranslations(fp)
        self.assertEqual(translations.npgettext("context", "Item", "Items", 1), "Eintrag")
        self.assertEqual(translations.npgettext("context", "Item", "Items", 2), "Einträge")

    def test_unexpected_literal_without_section_fails_clearly(self):
        base = locale_fixture(self, de=True, js=False)
        po_file = write_catalog(base / "locale" / "de", "django", '"orphan"\n')
        mo_file = po_file.with_suffix(".mo")

        with self.assertRaises(CommandError) as context:
            _compile_po(str(po_file), str(mo_file))

        self.assertIn("Syntax error", str(context.exception))
        self.assertIn(f"{po_file}:1", str(context.exception))

    def test_malformed_input_fails_clearly(self):
        base = locale_fixture(self, de=True, js=False)
        write_catalog(base / "locale" / "de", "django", HEADER + 'msgid "unterminated\n')

        with override_settings(BASE_DIR=base):
            with self.assertRaises(CommandError) as context:
                call_command("compile_locales", stdout=io.StringIO())
        self.assertIn("Failed to compile", str(context.exception))
        self.assertIn("django.po", str(context.exception))

    def test_compile_errors_that_already_name_the_file_are_not_wrapped_again(self):
        base = locale_fixture(self, de=True, js=False)
        po_file = write_catalog(base / "locale" / "de", "django", '"orphan"\n')

        with override_settings(BASE_DIR=base):
            with self.assertRaises(CommandError) as context:
                call_command("compile_locales", stdout=io.StringIO())

        self.assertIn(f"Syntax error on {po_file}:1", str(context.exception))
        self.assertNotIn("Failed to compile", str(context.exception))

    def test_io_failure_is_reported_as_a_compile_failure(self):
        base = locale_fixture(self, de=True, js=False)
        po_file = base / "locale" / "de" / "LC_MESSAGES" / "django.po"

        with override_settings(BASE_DIR=base):
            with patch("core.management.commands.compile_locales._compile_po", side_effect=OSError("disk full")):
                with self.assertRaises(CommandError) as context:
                    call_command("compile_locales", stdout=io.StringIO())

        self.assertIn(f"Failed to compile {po_file}: disk full", str(context.exception))

    def test_unexpected_failure_is_not_converted_into_a_command_error(self):
        base = locale_fixture(self, de=True, js=False)

        with override_settings(BASE_DIR=base):
            with patch("core.management.commands.compile_locales._compile_po", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    call_command("compile_locales", stdout=io.StringIO())

    def test_missing_djangojs_does_not_break_django_only_locale(self):
        base = locale_fixture(self, de=True, js=False)

        with override_settings(BASE_DIR=base):
            out = io.StringIO()
            call_command("compile_locales", stdout=out)

        messages_dir = base / "locale" / "de" / "LC_MESSAGES"
        self.assertTrue((messages_dir / "django.mo").exists())
        self.assertFalse((messages_dir / "djangojs.mo").exists())
        self.assertIn("1 catalog(s) compiled", out.getvalue())
        self.assertNotIn("djangojs.po", out.getvalue())

    def test_output_is_deterministic_for_unchanged_input(self):
        po = HEADER + SIMPLE_ENTRY
        base = locale_fixture(self, de=True, js=False)
        po_file = write_catalog(base / "locale" / "de", "django", po)
        first = po_file.with_suffix(".mo")
        second = base / "locale" / "de" / "LC_MESSAGES" / "django.second.mo"

        _compile_po(str(po_file), str(first))
        _compile_po(str(po_file), str(second))

        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_fuzzy_header_is_still_emitted_as_metadata_entry(self):
        # The metadata entry (empty msgid) carries the charset header. It must
        # be present even when the .po marks the header fuzzy — without it,
        # Django's JavaScriptCatalog and the stdlib gettext fall back to an
        # ASCII charset and crash on non-ASCII translations.
        po = """#, fuzzy
msgid ""
msgstr ""
"Project-Id-Version: PACKAGE VERSION\\n"
"Content-Type: text/plain; charset=UTF-8\\n"

#: reference.js:2
msgid "Hello"
msgstr "Hällo"
"""
        base = locale_fixture(self, de=True, js=False)
        po_file = write_catalog(base / "locale" / "de", "djangojs", po)
        mo_file = po_file.with_suffix(".mo")

        _compile_po(str(po_file), str(mo_file))
        mo_bytes = mo_file.read_bytes()

        magic, _version, count = struct.unpack("Iii", mo_bytes[:12])
        self.assertEqual(magic, 0x950412DE)
        offset_start = 7 * 4
        key_len, key_off = struct.unpack("ii", mo_bytes[offset_start : offset_start + 8])
        value_len, value_off = struct.unpack("ii", mo_bytes[offset_start + count * 8 : offset_start + count * 8 + 8])
        # The compiler stores the field length excluding the terminating NUL,
        # so the field itself occupies exactly key_len/value_len bytes.
        first_key = mo_bytes[key_off : key_off + key_len]
        first_value = mo_bytes[value_off : value_off + value_len]
        self.assertEqual(first_key, b"")
        self.assertIn(b"charset=UTF-8", first_value)

    def test_compiled_catalog_is_readable_by_stdlib_gettext(self):
        # End-to-end: a .mo built by the pure-Python compiler must be readable
        # by the stdlib gettext parser used at runtime (Django's
        # JavaScriptCatalog / DjangoTranslation).
        po = """#, fuzzy
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

#: reference.js:2
msgid "Hello"
msgstr "Hällo"
"""
        base = locale_fixture(self, de=True, js=False)
        po_file = write_catalog(base / "locale" / "de", "djangojs", po)
        mo_file = po_file.with_suffix(".mo")

        _compile_po(str(po_file), str(mo_file))

        import gettext

        with open(mo_file, "rb") as fp:
            translations = gettext.GNUTranslations(fp)
        self.assertEqual(translations.gettext("Hello"), "Hällo")
