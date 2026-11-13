import inspect

from django.core.paginator import Page, Paginator
from django.utils.functional import cached_property
from django.utils.inspect import method_has_no_args

from itambox.constants import DEFAULT_PAGINATE_COUNT, PAGINATE_COUNT_CHOICES

__all__ = (
    "EnhancedPage",
    "EnhancedPaginator",
)


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
    def count(self):
        """
        Exact number of objects, in one query for a queryset.

        The list must report the same total it navigates over. The former
        capped count ran two queries above the cap and made
        ``Page.end_index()`` clamp the last page to the cap (for example
        "Showing 101-100 of 100"). A genuinely bounded count needs a
        navigation model that does not depend on an exact last page; until
        then this paginator counts exactly.
        """
        object_list = self.object_list
        # Mirror Django's own detection of a real ``.count()`` method (a
        # QuerySet), excluding builtins like ``list.count`` that require an arg.
        c = getattr(object_list, "count", None)
        if callable(c) and not inspect.isbuiltin(c) and method_has_no_args(c):
            return c()
        return len(object_list)


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
