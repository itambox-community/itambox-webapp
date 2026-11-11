import inspect
from math import ceil

from django.conf import settings
from django.core.paginator import Page, Paginator
from django.utils.functional import cached_property
from django.utils.inspect import method_has_no_args

from itambox.constants import DEFAULT_PAGINATE_COUNT, PAGINATE_COUNT_CHOICES

__all__ = (
    "EnhancedPage",
    "EnhancedPaginator",
)

# Fallback used when ITAMBOX_PAGINATOR_COUNT_CAP is unset. Chosen high enough
# that it never engages for test data or normal installs — only enormous tables.
DEFAULT_PAGINATOR_COUNT_CAP = 100000


class EnhancedPaginator(Paginator):
    # Single source: the offered sizes come from PAGINATE_COUNT_CHOICES.
    default_page_lengths = tuple(value for value, _label in PAGINATE_COUNT_CHOICES)

    def __init__(self, object_list, per_page, orphans=None, **kwargs):
        try:
            per_page = int(per_page)
            if per_page < 1:
                per_page = DEFAULT_PAGINATE_COUNT
        except (ValueError, TypeError):
            per_page = DEFAULT_PAGINATE_COUNT

        if orphans is None and per_page <= 50:
            orphans = 5
        elif orphans is None:
            orphans = 10

        super().__init__(object_list, per_page, orphans=orphans, **kwargs)

    def _get_page(self, *args, **kwargs):
        return EnhancedPage(*args, **kwargs)

    def get_page_lengths(self):
        # A page size outside the offered list stays visible in the menu so the
        # current selection is never missing. It is only added while it does not
        # exceed the largest offered size: a direct caller passing a retired
        # size (e.g. 1000) must not bring it back into the dropdown.
        if self.per_page not in self.default_page_lengths and self.per_page <= max(self.default_page_lengths):
            return sorted([*self.default_page_lengths, self.per_page])
        return self.default_page_lengths

    @cached_property
    def _count_cap(self):
        """The configured count cap, or None to disable capping."""
        cap = getattr(settings, "ITAMBOX_PAGINATOR_COUNT_CAP", DEFAULT_PAGINATOR_COUNT_CAP)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            return DEFAULT_PAGINATOR_COUNT_CAP
        # A non-positive cap disables capping (revert to stock unbounded count).
        return cap if cap > 0 else None

    @cached_property
    def _raw_count(self):
        """
        Object total counted only up to ``cap + 1`` (so ``> cap`` is detectable),
        or the exact total when capping is disabled.

        A plain ``SELECT COUNT(*)`` scans the whole (filtered) table on every
        list page, which is slow once a tenant has hundreds of thousands of
        rows. Counting over a bounded slice lets the DB stop once it has seen
        ``cap + 1`` rows.
        """
        object_list = self.object_list
        cap = self._count_cap

        # Mirror Django's own detection of a real ``.count()`` method (a
        # QuerySet), excluding builtins like ``list.count`` that require an arg.
        c = getattr(object_list, "count", None)
        if not (callable(c) and not inspect.isbuiltin(c) and method_has_no_args(c)):
            # Lists / in-memory iterables — identical to Django's len() path.
            # len() is O(1) here, so capping buys nothing.
            return len(object_list)

        if cap is None:
            return c()

        # QuerySets / sliceable managers: count over a bounded slice so the DB
        # stops scanning past the cap. ``qs[:cap + 1].count()`` emits
        # COUNT(*) over a LIMIT-ed subquery.
        try:
            return object_list[: cap + 1].count()
        except TypeError:
            # Not sliceable in the QuerySet sense — fall back to the full count.
            return c()

    @cached_property
    def count(self):
        """
        Total number of objects, capped at ``ITAMBOX_PAGINATOR_COUNT_CAP``.

        If the real total is at or below the cap, the returned value is *exactly*
        the true count (identical to Django's default), so small tables and the
        test suite are unaffected. Only when a table genuinely exceeds the cap do
        we report the cap itself (and ``is_count_capped`` flips to True so the UI
        can render "<cap>+").
        """
        cap = self._count_cap
        raw = self._raw_count
        if cap is not None and raw > cap:
            return cap
        return raw

    @property
    def is_count_capped(self):
        """True when the real total exceeded the cap (``count`` reports the cap)."""
        cap = self._count_cap
        return cap is not None and self._raw_count > cap

    @cached_property
    def _real_count(self):
        """
        The exact, uncapped object total — used only for page *navigation*
        (``num_pages`` / ``validate_number`` / last-page slice), never for the
        "<cap>+" display.

        When the table did not exceed the cap, ``count`` is already exact, so we
        reuse it and emit no extra query. Only when capping actually engaged do
        we pay for a full ``COUNT(*)`` — necessary so that rows beyond
        ``cap * per_page`` remain reachable through pagination rather than
        raising ``EmptyPage``.
        """
        if not self.is_count_capped:
            # Cheap path: the capped count equals the true total, no extra query.
            return self.count

        object_list = self.object_list
        c = getattr(object_list, "count", None)
        if callable(c) and not inspect.isbuiltin(c) and method_has_no_args(c):
            return c()
        return len(object_list)

    @cached_property
    def num_pages(self):
        """
        Total number of pages, computed from the *real* (uncapped) count.

        Django derives ``num_pages`` from ``self.count``; because our ``count``
        is capped for display, the stock implementation would make every page
        past ``ceil(cap / per_page)`` unreachable (``validate_number`` raises
        ``EmptyPage``). Deriving the page range from ``_real_count`` instead lets
        navigation reach every real row, while ``count`` keeps reporting the cap.

        For tables at or below the cap, ``_real_count == count``, so this returns
        exactly the same value as Django's default.
        """
        if self._real_count == 0 and not self.allow_empty_first_page:
            return 0
        hits = max(1, self._real_count - self.orphans)
        return ceil(hits / self.per_page)

    def page(self, number):
        """
        Return a Page for ``number``, slicing against the *real* total.

        Identical to Django's ``Paginator.page`` except the last-page orphan
        truncation uses ``_real_count`` rather than the capped ``count`` — so the
        final reachable page beyond the cap is not clipped to ``cap`` rows.
        """
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page
        if top + self.orphans >= self._real_count:
            top = self._real_count
        return self._get_page(self.object_list[bottom:top], number, self)


class EnhancedPage(Page):
    def smart_pages(self):
        if self.paginator.num_pages <= 5:
            return self.paginator.page_range

        n = self.number
        pages_wanted = [1, n - 2, n - 1, n, n + 1, n + 2, self.paginator.num_pages]
        page_list = sorted(set(self.paginator.page_range).intersection(pages_wanted))

        skip_pages = [x[1] for x in zip(page_list[:-1], page_list[1:], strict=False) if (x[1] - x[0] != 1)]
        for i in skip_pages:
            page_list.insert(page_list.index(i), False)

        return page_list
