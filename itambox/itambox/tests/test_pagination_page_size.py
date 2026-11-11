"""Pagination page-size policy.

The list views accept a ``per_page`` query parameter, and the user preferences
form offers the same sizes. Rendering cost is linear in the row count, so the
largest offered size is a product decision as much as a performance one: on
slower hardware a four-digit page size turned a list view into a multi-second
wait. These tests pin the offered sizes so the option cannot quietly return.
"""

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, override_settings

from itambox import constants
from itambox.utils import get_paginate_count


class PageSizeChoicesTests(SimpleTestCase):
    def test_offered_sizes_stop_at_five_hundred(self):
        for choices in (constants.PAGINATE_COUNT_CHOICES, settings.PAGINATE_COUNT_CHOICES):
            sizes = [value for value, _label in choices]
            self.assertNotIn(1000, sizes)
            self.assertEqual(max(sizes), 500)

    def test_frontend_and_settings_offer_the_same_sizes(self):
        # The dropdown reads the constant, the preferences form reads the
        # setting. Two independent definitions only stay in step if a test says so.
        self.assertEqual(
            list(constants.PAGINATE_COUNT_CHOICES),
            list(settings.PAGINATE_COUNT_CHOICES),
        )


class GetPaginateCountTests(SimpleTestCase):
    """A ``per_page`` value that is no longer offered must not be honoured."""

    def setUp(self):
        self.factory = RequestFactory()

    def _count(self, query: str) -> int:
        request = self.factory.get(f"/?{query}")
        request.user = type("Anonymous", (), {"is_authenticated": False})()
        return get_paginate_count(request)

    def test_offered_sizes_are_honoured(self):
        for size in (25, 50, 100, 250, 500):
            with self.subTest(size=size):
                self.assertEqual(self._count(f"per_page={size}"), size)

    def test_thousand_falls_back_to_the_default(self):
        self.assertEqual(self._count("per_page=1000"), constants.DEFAULT_PAGINATE_COUNT)

    def test_arbitrary_size_falls_back_to_the_default(self):
        self.assertEqual(self._count("per_page=999"), constants.DEFAULT_PAGINATE_COUNT)

    def test_garbage_falls_back_to_the_default(self):
        self.assertEqual(self._count("per_page=lots"), constants.DEFAULT_PAGINATE_COUNT)

    @override_settings(PAGINATE_COUNT_CHOICES=((10, "10"),))
    def test_choices_are_read_at_request_time_not_import_time(self):
        # ``get_paginate_count`` imports the constant at module scope; if someone
        # later switches it to read the setting, this pins that the change is
        # deliberate rather than accidental.
        self.assertEqual(self._count("per_page=10"), constants.DEFAULT_PAGINATE_COUNT)
        self.assertEqual(self._count("per_page=25"), 25)
