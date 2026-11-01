# core/search.py
from collections import defaultdict

from django.db.models import Q

# Registry for search indexes
SEARCH_INDEXES = defaultdict(list)
_SEARCH_FILTERS_UNSET = object()


def get_search_indexes(model):
    """Return the registered indexes for ``model`` without exposing registry state."""
    return tuple(SEARCH_INDEXES.get(model, ()))


def search(
    model,
    term="",
    queryset=None,
    *,
    specification_filters=_SEARCH_FILTERS_UNSET,
    tenant_ids=None,
    definitions=None,
):
    """Dispatch a public search request through the model's registered index.

    Specification search is an optional index capability.  Core owns only the
    dispatch contract; the domain index owns source-qualified query semantics.
    Passing an empty specification sequence is intentional and still reaches
    the domain method so its explicit tenant scope cannot be skipped.
    """
    indexes = get_search_indexes(model)
    if not indexes:
        raise ValueError(f"no search index registered for {model!r}")
    if specification_filters is not _SEARCH_FILTERS_UNSET:
        for index in indexes:
            handler = getattr(index, "search_specifications", None)
            if handler is not None:
                return handler(
                    specification_filters,
                    queryset=queryset,
                    tenant_ids=tenant_ids,
                    definitions=definitions,
                )
        raise NotImplementedError(f"no specification search handler registered for {model!r}")
    return indexes[0].get_results(term, queryset)


def get_table_queryset(model, queryset):
    """Prepare ``queryset`` for rendering the model's search result table.

    Each registered index may load the relations its table renders in bulk.
    The hook receives the already scoped queryset and may only add loading
    hints; it must never widen or replace the rows the caller selected.
    """
    for index in get_search_indexes(model):
        queryset = index.get_table_queryset(queryset)
    return queryset


def register_search():
    """
    Decorator to register a SearchIndex class.
    """

    def _wrapper(cls):
        model = cls.model
        # Ensure the model attribute exists before accessing it
        if not model:
            raise TypeError(f"SearchIndex subclass {cls.__name__} must define a 'model' attribute.")
        SEARCH_INDEXES[model].append(cls())
        return cls

    return _wrapper


class SearchIndex:
    # Subclasses must define the model
    model = None
    # Subclasses must define fields to search
    fields = ()
    # Optional: Define fields for ordering results
    order_by = ()

    def __init__(self):
        if not self.model or not self.fields:
            raise NotImplementedError(
                f"SearchIndex subclass {self.__class__.__name__} must define 'model' and 'fields'."
            )

    def search(self, term, queryset=None):
        """
        Search the index's model fields for the given term.
        Returns a filtered queryset.
        """
        if queryset is None:
            queryset = self.model.objects.all()

        query = Q()
        for field_name in self.fields:
            # Simple case: direct field lookup
            lookup = f"{field_name}__icontains"
            query |= Q(**{lookup: term})
            # Add more sophisticated lookup logic here if needed (e.g., exact matches, related fields)

        # Apply ordering if defined
        if self.order_by:
            return queryset.filter(query).order_by(*self.order_by)
        else:
            return queryset.filter(query)

    def search_specifications(self, filters, queryset=None, *, tenant_ids=None, definitions=None):
        """Optional domain hook for source-qualified specification search."""
        raise NotImplementedError

    def get_table_queryset(self, queryset):
        """Optional hook: add ``select_related``/``prefetch_related`` for the result table.

        The global search renders matches through the model's list table from a
        materialized list, so the table cannot add its own loading hints. The
        default leaves the queryset unchanged.
        """
        return queryset

    def get_results(
        self, term, queryset=None, *, specification_filters=_SEARCH_FILTERS_UNSET, tenant_ids=None, definitions=None
    ):
        """
        Wrapper around search with an explicit specification-search branch.
        """
        if specification_filters is not _SEARCH_FILTERS_UNSET:
            return self.search_specifications(
                specification_filters,
                queryset=queryset,
                tenant_ids=tenant_ids,
                definitions=definitions,
            )
        return self.search(term, queryset)
