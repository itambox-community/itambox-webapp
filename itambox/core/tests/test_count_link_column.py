"""CountLinkColumn (core.tables) renders a numeric count as a link to a pre-filtered
list view — reverse(viewname)?<url_param>=<record.pk>. An empty/zero value, or a row
with no record (e.g. a table footer), renders a plain ``0`` with no link. Pure render
logic — no DB needed (reverse only needs the URLconf). Replaces the ~26 hand-written
render_<x>_count methods that built this identical link inline.
"""

from types import SimpleNamespace

from django.urls import reverse

from core.tables import CountLinkColumn


def _record(pk=7):
    return SimpleNamespace(pk=pk)


class TestCountLinkColumnRender:
    def _col(self):
        return CountLinkColumn("assets:asset_list", "status")

    def test_links_count_to_filtered_list(self):
        html = str(self._col().render(5, _record(pk=7)))
        expected_url = f"{reverse('assets:asset_list')}?status=7"
        assert 'href="%s"' % expected_url in html
        assert ">5</a>" in html

    def test_zero_value_renders_plain_no_link(self):
        # Falsy count: no link, plain 0 (matches the old `return value or 0`).
        assert self._col().render(0, _record()) == 0

    def test_missing_record_renders_plain(self):
        # No record (e.g. footer/total row): no link.
        assert self._col().render(3, None) == 3
        assert self._col().render(0, None) == 0

    def test_preserves_column_kwargs(self):
        col = CountLinkColumn("assets:asset_list", "status", verbose_name="Assets", orderable=False)
        assert col.verbose_name == "Assets"
        assert col.orderable is False
