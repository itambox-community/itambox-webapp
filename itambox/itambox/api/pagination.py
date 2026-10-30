from collections import OrderedDict

from django.conf import settings
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param, replace_query_param


class ITAMBoxPagination(LimitOffsetPagination):
    """Offset pagination with two scale safeguards:

    1. **Capped count** — the ``count`` field is counted only up to
       ``ITAMBOX_PAGINATOR_COUNT_CAP`` (default 100k) rather than a full
       ``SELECT COUNT(*)`` over the whole filtered table on every page, which is
       slow once a tenant has hundreds of thousands of rows. When the real total
       exceeds the cap, ``count`` reports the cap and ``count_capped`` is True.
       Set the cap to 0 to disable (stock unbounded count). Mirrors the UI's
       ``core.paginator.EnhancedPaginator``.
    2. **Keyset cursor** — pass ``?start=<pk>`` to iterate by primary key
       (``pk >= start``, ordered by pk). This skips the COUNT entirely (``count``
       is null) and is the recommended mode for bulk export / iterating large
       collections, where offset pagination degrades.
    """

    start_query_param = "start"

    def __init__(self):
        self.default_limit = getattr(settings, "PAGINATE_COUNT", 50)
        self.start = None
        self._page_length = 0
        self._last_pk = None
        self.count_capped = False

    def paginate_queryset(self, queryset, request, view=None):
        if isinstance(queryset, QuerySet) and not queryset.ordered:
            queryset = queryset.order_by("pk")

        self.start = self.get_start(request)
        self.limit = self.get_limit(request)
        self.request = request

        if self.start is not None:
            if self.offset_query_param in request.query_params:
                raise ValidationError(
                    _("'{start_param}' and '{offset_param}' are mutually exclusive.").format(
                        start_param=self.start_query_param,
                        offset_param=self.offset_query_param,
                    )
                )

            self.count = None
            self.offset = 0

            queryset = queryset.filter(pk__gte=self.start).order_by("pk")
            results = list(queryset[: self.limit]) if self.limit is not None else list(queryset)

            self._page_length = len(results)
            if results:
                self._last_pk = results[-1].pk if hasattr(results[-1], "pk") else results[-1]["pk"]

            return results

        if isinstance(queryset, QuerySet):
            self.count = self.get_queryset_count(queryset)
        else:
            self.count = len(queryset)

        self.offset = self.get_offset(request)

        if self.limit is not None and self.count > self.limit and self.template is not None:
            self.display_page_controls = True

        if self.count == 0 or self.offset > self.count:
            return list()

        if self.limit is not None:
            return list(queryset[self.offset : self.offset + self.limit])
        return list(queryset[self.offset :])

    def get_start(self, request):
        try:
            value = int(request.query_params[self.start_query_param])
            if value < 0:
                raise ValidationError(
                    _("Invalid '{param}' parameter: must be a non-negative integer.").format(
                        param=self.start_query_param,
                    )
                )
            return value
        except KeyError:
            return None
        except (ValueError, TypeError):
            raise ValidationError(
                _("Invalid '{param}' parameter: must be a non-negative integer.").format(
                    param=self.start_query_param,
                )
            ) from None

    def get_limit(self, request):
        max_limit = self.default_limit
        MAX_PAGE_SIZE = getattr(settings, "MAX_PAGE_SIZE", None)

        if MAX_PAGE_SIZE:
            max_limit = min(max_limit, MAX_PAGE_SIZE)

        if self.limit_query_param:
            try:
                limit = int(request.query_params[self.limit_query_param])
            except (KeyError, ValueError):
                return max_limit

            # A negative limit is invalid and 0 means "the maximum"; both keep the cap.
            if limit <= 0:
                return max_limit

            if MAX_PAGE_SIZE:
                max_limit = min(MAX_PAGE_SIZE, limit)
            else:
                max_limit = limit

        return max_limit

    def get_queryset_count(self, queryset):
        cap = getattr(settings, "ITAMBOX_PAGINATOR_COUNT_CAP", 0)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = 0
        if cap <= 0:
            # Capping disabled — stock unbounded count.
            return queryset.count()
        # Count over a bounded slice so the DB stops scanning at cap + 1 rows
        # instead of a full COUNT(*) on every list page (qs[:n].count() emits
        # COUNT(*) over a LIMIT-ed subquery). Mirrors EnhancedPaginator.
        raw = queryset[: cap + 1].count()
        if raw > cap:
            self.count_capped = True
            return cap
        return raw

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("count", self.count),
                    # True when `count` was capped (the real total is larger). Clients
                    # that must enumerate everything should switch to ?start= cursoring.
                    ("count_capped", self.count_capped),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )

    def get_paginated_response_schema(self, schema):
        response_schema = super().get_paginated_response_schema(schema)
        # `count` is null in keyset/cursor mode (?start=), where no COUNT is run.
        if "count" in response_schema.get("properties", {}):
            response_schema["properties"]["count"]["nullable"] = True
        response_schema["properties"]["count_capped"] = {
            "type": "boolean",
            "description": (
                "True when `count` was capped at ITAMBOX_PAGINATOR_COUNT_CAP and "
                "the real total is larger. Use `start` (keyset cursor) pagination "
                "to iterate the full result set."
            ),
        }
        return response_schema

    def get_next_link(self):
        if self.limit is None:
            return None

        if self.start is not None:
            if self._page_length < self.limit:
                return None
            url = self.request.build_absolute_uri()
            url = replace_query_param(url, self.start_query_param, self._last_pk + 1)
            url = replace_query_param(url, self.limit_query_param, self.limit)
            url = remove_query_param(url, self.offset_query_param)
            return url

        return super().get_next_link()

    def get_previous_link(self):
        if self.limit is None:
            return None

        if self.start is not None:
            return None

        return super().get_previous_link()

    def get_schema_operation_parameters(self, view):
        parameters = super().get_schema_operation_parameters(view)
        parameters.append(
            {
                "name": self.start_query_param,
                "required": False,
                "in": "query",
                "description": (
                    "Keyset/cursor pagination: return results with pk >= start, ordered by pk. "
                    "Skips the (capped) row count and stays O(page) regardless of table size — "
                    "use this instead of offset/limit for bulk export or iterating large "
                    "collections. Follow the `next` link to walk subsequent pages."
                ),
                "schema": {
                    "type": "integer",
                },
            }
        )
        return parameters
