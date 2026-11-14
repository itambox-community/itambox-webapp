from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from extras.signals import _emit_event


class EventEmissionBoundaryTests(SimpleTestCase):
    @patch("extras.signals._table_exists", return_value=True)
    @patch("extras.signals.dispatch_event", side_effect=RuntimeError("event backend offline"))
    def test_emission_failure_propagates_to_the_business_transaction(self, dispatch, _table_exists):
        # E7: a missing event row is a business error, never a silently dropped event.
        with self.assertRaises(RuntimeError):
            _emit_event(SimpleNamespace, SimpleNamespace(pk=31), "update")
        self.assertEqual(dispatch.call_count, 1)
