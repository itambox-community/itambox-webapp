import unittest
from pathlib import Path

from django.conf import settings
from django.template import Engine

if not settings.configured:
    settings.configure(
        DJANGO_TABLES2_TEMPLATE="django_tables2/bootstrap5.html",
    )


class DjangoTables2TemplateCompatibilityTests(unittest.TestCase):
    def test_htmx_sort_link_compiles_with_supported_django_tables2(self):
        template_path = Path(__file__).resolve().parents[2] / "templates" / "global_includes" / "htmx_table.html"
        sort_link = next(
            line
            for line in template_path.read_text(encoding="utf-8").splitlines()
            if "table.prefixed_order_by_field=" in line
        )
        engine = Engine(
            libraries={
                "django_tables2": "django_tables2.templatetags.django_tables2",
            }
        )

        engine.from_string("{% load django_tables2 %}\n" + sort_link)

    def test_issue260_mobile_select_all_contract_is_declared(self):
        template_root = Path(__file__).resolve().parents[2]
        table_template = (template_root / "templates" / "global_includes" / "htmx_table.html").read_text(
            encoding="utf-8"
        )
        column_source = (template_root / "core" / "tables" / "columns.py").read_text(encoding="utf-8")
        batch_source = (template_root / "static" / "src" / "batch-actions.ts").read_text(encoding="utf-8")
        table_styles = (template_root / "static" / "src" / "styles" / "_tables.scss").read_text(encoding="utf-8")

        self.assertIn("bulk.card_layout", table_template)
        self.assertIn('data-select-all="true"', table_template)
        self.assertIn('data-select-all="true"', column_source)
        self.assertIn("querySelectorAll<HTMLInputElement>('[data-select-all]')", batch_source)
        self.assertIn("selectAllCb.indeterminate", batch_source)
        self.assertIn("selectAllCb.disabled", batch_source)
        self.assertIn(".mobile-select-all__label", table_styles)

    def test_page_header_puts_right_aligned_actions_above_title_on_mobile(self):
        template_root = Path(__file__).resolve().parents[2]
        for template_name in ("layout.html", "base_htmx.html"):
            template = (template_root / "templates" / template_name).read_text(encoding="utf-8")

            self.assertIn('<div class="col-12 col-lg order-2 order-lg-1">', template)
            self.assertIn(
                '<div class="col-12 col-lg-auto ms-lg-auto d-flex justify-content-end order-1 order-lg-2 d-print-none">',
                template,
            )

    def test_mobile_footer_is_hidden_and_tabler_credit_is_removed(self):
        template_root = Path(__file__).resolve().parents[2]
        layout = (template_root / "templates" / "layout.html").read_text(encoding="utf-8")
        mobile_styles = (template_root / "static" / "src" / "styles" / "_mobile.scss").read_text(encoding="utf-8")

        self.assertNotIn("Theme by", layout)
        self.assertNotIn("https://tabler.io", layout)
        self.assertIn(
            '<div class="container-fluid d-flex flex-column flex-lg-row '
            "justify-content-between align-items-stretch align-items-lg-center "
            'gap-2 gap-lg-0">',
            layout,
        )
        self.assertIn('<ul class="list-inline mb-0 fs-2 text-end">', layout)
        self.assertIn("footer.footer {", mobile_styles)
        self.assertIn("display: none !important;", mobile_styles)

    def test_issue260_responsive_shell_contract_is_declared(self):
        template_root = Path(__file__).resolve().parents[2]
        layout = (template_root / "templates" / "layout.html").read_text(encoding="utf-8")
        topbar = (template_root / "templates" / "global_includes" / "_topbar.html").read_text(encoding="utf-8")
        user_menu = (template_root / "templates" / "global_includes" / "_user_menu.html").read_text(encoding="utf-8")
        mobile_styles = (template_root / "static" / "src" / "styles" / "_mobile.scss").read_text(encoding="utf-8")

        self.assertIn('{% include "global_includes/_user_menu.html"', layout)
        self.assertIn('{% include "global_includes/_user_menu.html"', topbar)
        self.assertIn("{% url 'graphql' %}", layout)
        self.assertIn("mobile-topbar-actions", layout)
        self.assertIn("safe-area-inset-bottom", mobile_styles)
        self.assertIn("detail-edit-action__label", mobile_styles)
        self.assertIn(".mobile-topbar-actions {", mobile_styles)
        self.assertIn(".nav-link:hover", mobile_styles)
        self.assertIn("focus-visible", mobile_styles)

        menu_tokens = [
            "users:user_profile",
            "users:user_notifications",
            "users:user_subscriptions",
            "users:user_preferences",
            "users:user_api_tokens",
            "logout",
        ]
        positions = [user_menu.index(token) for token in menu_tokens]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Signed in as", user_menu)


if __name__ == "__main__":
    unittest.main()
