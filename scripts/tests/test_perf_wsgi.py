import os
import time
import unittest
from contextlib import contextmanager

from scripts.perf import perf_wsgi
from scripts.perf.bench import parse_response_diagnostics


class _Connection:
    def __init__(self):
        self.collector = None

    @contextmanager
    def execute_wrapper(self, collector):
        self.collector = collector
        yield


class PerformanceWsgiDiagnosticsTests(unittest.TestCase):
    def test_profiled_request_correlates_queries_gc_and_worker_identity(self):
        connection = _Connection()
        response = {}
        writes = []
        request_id = "a" * 32

        def execute(sql, params, many, context):
            time.sleep(0.055)

        def django_application(environ, start_response):
            perf_wsgi._record_gc_pause("start", {})
            time.sleep(0.001)
            perf_wsgi._record_gc_pause("stop", {})
            assert connection.collector is not None
            connection.collector(execute, "SELECT id FROM assets_asset WHERE id = %s", (42,), False, {})
            write = start_response("200 OK", [("Content-Type", "text/plain")])
            write(b"buffered")
            return [b"body"]

        def start_response(status, headers, exc_info=None):
            response.update(status=status, headers=dict(headers), exc_info=exc_info)
            return writes.append

        body = perf_wsgi._profile_request(
            django_application,
            {"HTTP_X_PERF_REQUEST_ID": request_id},
            start_response,
            connection,
        )

        self.assertEqual(list(body), [b"body"])
        self.assertEqual(writes, [b"buffered"])
        diagnostics = parse_response_diagnostics(response["headers"], request_id)
        self.assertIsInstance(diagnostics, dict)
        self.assertTrue(diagnostics["available"])
        self.assertEqual(diagnostics["db_query_count"], 1)
        self.assertEqual(diagnostics["slow_query_count"], 1)
        self.assertEqual(len(diagnostics["slow_queries"]), 1)
        self.assertEqual(len(diagnostics["slow_queries"][0]["fingerprint"]), 16)
        self.assertNotIn("sql", diagnostics["slow_queries"][0])
        self.assertEqual(diagnostics["gc_pause_count"], 1)
        self.assertGreater(diagnostics["gc_pause_time_ms"], 0)
        self.assertEqual(diagnostics["worker_pid"], os.getpid())
        self.assertEqual(len(diagnostics["worker_instance_id"]), 32)

    def test_unidentified_requests_are_not_instrumented(self):
        response = {}
        calls = []

        def application(environ, start_response):
            calls.append(environ)
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"body"]

        def start_response(status, headers, exc_info=None):
            response.update(status=status, headers=dict(headers))
            return lambda _chunk: None

        body = perf_wsgi._profile_request(application, {}, start_response, None)
        self.assertEqual(list(body), [b"body"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("X-Perf-Request-ID", response["headers"])


if __name__ == "__main__":
    unittest.main()
