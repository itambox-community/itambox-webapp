import json
import os
import re
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch as mock_patch

from scripts.perf.bench import (
    ALL_PATHS,
    BenchmarkError,
    HttpSession,
    main,
    parse_args,
    parse_response_diagnostics,
    percentile,
    process_observations,
    render_markdown,
    require_disposable_database,
    run_scenario,
    summarize_samples,
    validate_base_url,
    validate_response,
    write_outputs,
)

SMOKE_SCENARIOS = (
    ("asset_list_first_page", "/assets/", "PF882-000001"),
    ("asset_detail", "/assets/1/", "PF882-000001"),
    ("global_search", "/search/?q=fixture", "Perf 882 Asset 000042"),
)


class _FixtureApplication(BaseHTTPRequestHandler):
    """Minimal stand-in for the running application used by the HTTP smoke test."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path != "/accounts/login/" and "sessionid=fixture-session" not in self.headers.get("Cookie", ""):
            self.send_response(401)
            self.end_headers()
            return
        if path == "/accounts/login/":
            self.send_response(200)
            self.send_header("Set-Cookie", "csrftoken=fixture-token; Path=/")
            body = b'<input name="csrfmiddlewaretoken" value="fixture-token">'
        elif path == "/dashboard/":
            self.send_response(200)
            body = b"dashboard"
        elif path == "/assets/":
            self.send_response(200)
            body = b"PF882-000001"
        elif path == "/assets/1/":
            self.send_response(200)
            body = b"PF882-000001"
        elif path == "/search/":
            self.send_response(200)
            body = b"Perf 882 Asset 000042"
        else:
            self.send_response(404)
            body = b"not found"
        request_id = self.headers.get("X-Perf-Request-ID")
        if request_id:
            request_id = request_id.replace("\n", "").replace("\r", "")
        if request_id and re.fullmatch(r"[0-9a-f]{32}", request_id):
            self.send_header("X-Perf-Request-ID", request_id)
            self.send_header("X-Perf-Worker-PID", "1234")
            self.send_header("X-Perf-Worker-Instance", "fixture-worker")
            self.send_header("X-Perf-Query-Count", "3")
            self.send_header("X-Perf-Query-Time-MS", "12.500")
            self.send_header("X-Perf-Slow-Query-Count", "1")
            self.send_header("X-Perf-Slow-Queries-B64", "W3siZmluZ2VycHJpbnQiOiJhYmNkZWYiLCJkdXJhdGlvbl9tcyI6NjAuMH1d")
            self.send_header("X-Perf-GC-Pause-Count", "2")
            self.send_header("X-Perf-GC-Pause-Time-MS", "1.250")
            self.send_header("X-Perf-GC-Max-Pause-MS", "0.900")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(302)
        self.send_header("Location", "/dashboard/")
        self.send_header("Set-Cookie", "sessionid=fixture-session; Path=/")
        self.end_headers()

    def log_message(self, format, *args):
        return


class BenchmarkReportingTests(unittest.TestCase):
    def test_percentiles_use_nearest_rank_and_median(self):
        self.assertEqual(percentile([1.0, 2.0, 3.0, 4.0], 0.95), 4.0)
        self.assertEqual(summarize_samples([1.0, 2.0, 3.0, 4.0])["p50_ms"], 2.5)

    def test_tail_summary_has_reproducible_bootstrap_interval(self):
        samples = [float(value) for value in range(1, 101)]
        summary = summarize_samples(samples, bootstrap_seed=892)
        self.assertEqual(summary["sample_count"], 100)
        self.assertEqual(summary["p50_ms"], 50.5)
        self.assertEqual(summary["p90_ms"], 90.0)
        self.assertEqual(summary["p95_ms"], 95.0)
        self.assertEqual(summary["p99_ms"], 99.0)
        self.assertEqual(summary["max_ms"], 100.0)
        self.assertEqual(
            summary["p95_bootstrap_ci_ms"], summarize_samples(samples, bootstrap_seed=892)["p95_bootstrap_ci_ms"]
        )
        self.assertLessEqual(summary["p95_bootstrap_ci_ms"]["lower_ms"], summary["p95_bootstrap_ci_ms"]["upper_ms"])

    def test_empty_samples_have_no_latency_percentiles(self):
        summary = summarize_samples([])
        self.assertEqual(summary["sample_count"], 0)
        self.assertIsNone(summary["p90_ms"])
        self.assertIsNone(summary["p95_ms"])
        self.assertIsNone(summary["max_ms"])
        self.assertIsNone(summary["p95_bootstrap_ci_ms"])

    def test_run_scenario_counts_requests_and_failures(self):
        sessions = [HttpSession("http://127.0.0.1:8010", 1), HttpSession("http://127.0.0.1:8010", 1)]

        def action(session):
            session.request_count += 1
            if session.request_count == 2:
                session.error_count += 1
                raise BenchmarkError("synthetic failure")
            return 5.0

        result = run_scenario(
            "read_path",
            action,
            sessions,
            iterations=3,
            warmups=1,
            concurrency=2,
            uses_http=True,
        )
        self.assertEqual(result["request_count"], 4)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["warmups"], 1)
        self.assertEqual(result["iterations"], 3)
        self.assertEqual(result["warmup_sample_count"], 1)
        self.assertEqual(len(result["warmup_samples_ms"]), 1)
        self.assertEqual(len(result["sample_records"]), 4)
        self.assertEqual(result["sample_records"][0]["phase"], "warmup")
        self.assertEqual(sum(record["phase"] == "measured" for record in result["sample_records"]), 3)

    def test_markdown_query_table_is_explicitly_not_latency(self):
        text = render_markdown(
            {
                "mode": "QUERY COUNTS, NOT LATENCY",
                "query_counts": {"asset_list_first_page": 12, "asset_detail": 7},
            }
        )
        self.assertIn("QUERY COUNTS, NOT LATENCY", text)
        self.assertIn("| asset_list_first_page | 12 |", text)
        self.assertNotIn("p95", text.lower())

    def test_write_outputs_creates_json_and_markdown(self):
        result = {
            "mode": "QUERY COUNTS, NOT LATENCY",
            "query_counts": {"asset_detail": 7},
        }
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path, markdown = write_outputs(result, Path(directory) / "counts.json")
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), result)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), markdown)

    def test_local_http_smoke_writes_list_detail_and_search_table(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureApplication)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        session = HttpSession(f"http://127.0.0.1:{server.server_port}", 2)
        try:
            session.login("/accounts/login/", "/dashboard/", "benchmark-test", "test-only")
            self.assertEqual(session.request_count, 3)
            self.assertEqual(session.login_request_count, 3)
            results = self._run_smoke_scenarios(session)
            with tempfile.TemporaryDirectory() as directory:
                self._assert_smoke_report(Path(directory) / "smoke.json", results)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def _run_smoke_scenarios(self, session):
        results = []
        for name, path, expected in SMOKE_SCENARIOS:
            results.append(
                run_scenario(
                    name,
                    lambda current_session, current_path=path, expected_text=expected, scenario=name: self._http_sample(
                        current_session, scenario, current_path, expected_text
                    ),
                    [session],
                    iterations=2,
                    warmups=1,
                    concurrency=1,
                    uses_http=True,
                )
            )
        return results

    def _assert_smoke_report(self, json_target, results):
        report = {
            "runtime": {
                "concurrency": 1,
                "iterations": 2,
                "warmups": 1,
                "worker_counts": {"gunicorn": 1, "qcluster": 1},
            },
            "results": results,
            "totals": {
                "request_count": sum(row["request_count"] for row in results),
                "error_count": sum(row["error_count"] for row in results),
            },
        }
        json_path, markdown_path, markdown = write_outputs(report, json_target)
        raw_text = json_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        self.assertNotIn("benchmark-test", raw_text)
        self.assertNotIn("test-only", raw_text)
        self.assertEqual([row["path"] for row in raw["results"]], [name for name, _, _ in SMOKE_SCENARIOS])
        self.assertEqual(raw["totals"]["error_count"], 0)
        self.assertTrue(markdown_path.is_file())
        for name, _, _ in SMOKE_SCENARIOS:
            self.assertIn(name, markdown)
        measured_requests = [
            request
            for row in raw["results"]
            for sample in row["sample_records"]
            if sample["phase"] == "measured"
            for request in sample["requests"]
        ]
        self.assertEqual(len(measured_requests), 6)
        self.assertTrue(all(request["diagnostics"]["available"] for request in measured_requests))
        self.assertTrue(
            all(request["diagnostics"]["worker_instance_id"] == "fixture-worker" for request in measured_requests)
        )

    def test_response_diagnostics_are_bound_to_the_request_id(self):
        headers = {
            "X-Perf-Request-ID": "request-a",
            "X-Perf-Worker-PID": "1234",
            "X-Perf-Worker-Instance": "worker-a",
            "X-Perf-Slow-Queries-B64": "W10=",
        }
        self.assertIsNone(parse_response_diagnostics(headers, "request-b"))
        incomplete = parse_response_diagnostics({"X-Perf-Request-ID": "request-a"}, "request-a")
        self.assertEqual(
            incomplete,
            {"available": False, "request_id": "request-a", "error": "incomplete diagnostics headers"},
        )

    def test_process_observations_preserve_worker_restart_correlation(self):
        results = [
            {
                "sample_records": [
                    {
                        "requests": [
                            {"diagnostics": {"available": True, "worker_pid": 42, "worker_instance_id": "first"}}
                        ]
                    },
                    {
                        "requests": [
                            {"diagnostics": {"available": True, "worker_pid": 42, "worker_instance_id": "second"}}
                        ]
                    },
                ]
            }
        ]
        observations = process_observations(results)
        self.assertEqual(observations["instrumented_request_count"], 2)
        self.assertEqual(len(observations["pid_reuse_or_process_restart_events"]), 1)
        self.assertEqual(observations["pid_reuse_or_process_restart_events"][0]["previous_instance_id"], "first")

    @staticmethod
    def _http_sample(session, name, path, expected_text):
        response = session.request(path)
        validate_response(response, name, expected_text, None)
        return response.elapsed_ms


class BenchmarkSafetyTests(unittest.TestCase):
    def test_allows_qualification_and_loopback_urls(self):
        self.assertEqual(validate_base_url("https://qual.itambox.dev"), "https://qual.itambox.dev")
        self.assertEqual(validate_base_url("http://127.0.0.1:8010/"), "http://127.0.0.1:8010")

    def test_rejects_unapproved_or_credentialed_urls(self):
        for url in (
            "https://demo.itambox.dev",
            "https://user:placeholder@qual.itambox.dev",
            "https://qual.itambox.dev:444",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_base_url(url)

    def test_database_guard_allows_remote_context_only_for_read_only_runs(self):
        connection = types.SimpleNamespace(
            vendor="postgresql",
            settings_dict={"NAME": "itambox_qual", "HOST": "db"},
        )
        settings = types.SimpleNamespace(DEBUG=True)
        bindings = {"connection": connection, "settings": settings}
        with mock_patch.dict(os.environ, {"ITAMBOX_ENV": "dev"}):
            self.assertEqual(
                require_disposable_database(bindings, {"database": "itambox_qual"}, strict=False),
                "itambox_qual",
            )
            connection.settings_dict["NAME"] = "perf_qual_smoke"
            with self.assertRaises(BenchmarkError):
                require_disposable_database(bindings, {"database": "perf_qual_smoke"}, strict=True)
            connection.settings_dict["HOST"] = "127.0.0.1"
            self.assertEqual(
                require_disposable_database(bindings, {"database": "perf_qual_smoke"}, strict=True),
                "perf_qual_smoke",
            )

    def test_count_queries_is_a_standalone_mode(self):
        args = parse_args(["--manifest", "manifest.json", "--count-queries"])
        self.assertTrue(args.count_queries)
        self.assertEqual(args.paths, list(ALL_PATHS))

    def test_http_probe_sampling_defaults_to_100_and_rejects_less(self):
        args = parse_args(["--manifest", "manifest.json", "--paths", "rest_list"])
        self.assertEqual(args.iterations, 100)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(["--manifest", "manifest.json", "--paths", "rest_list", "--iterations", "99"])

    def test_count_queries_cli_returns_per_path_counts(self):
        connection = types.SimpleNamespace(settings_dict={"NAME": "perf_qual_fixture"})

        class Manager:
            def filter(self, **_kwargs):
                return self

            def first(self):
                return object()

        class UserModel:
            _base_manager = Manager()

        class Client:
            def force_login(self, _user):
                pass

            def get(self, _path, **_kwargs):
                return types.SimpleNamespace(
                    status_code=200,
                    content=b"PF882- Perf 882 Asset 000123 PF882-000000",
                )

        class CaptureQueriesContext:
            counts = iter((11, 13, 7))

            def __init__(self, _connection):
                self.count = next(self.counts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __len__(self):
                return self.count

        bindings = {
            "connection": connection,
            "get_user_model": lambda: UserModel,
            "Client": Client,
            "CaptureQueriesContext": CaptureQueriesContext,
            "reverse": lambda _name, args=None: "/assets/" if args is None else f"/assets/{args[0]}/",
        }
        manifest = {
            "benchmark": {
                "asset_id": 42,
                "tenant_group_id": 7,
                "filtered_q": "PF882-SN-000123",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "query-counts.json"
            with (
                mock_patch("scripts.perf.bench.setup_django", return_value=bindings),
                mock_patch("scripts.perf.bench.load_manifest", return_value=manifest),
                mock_patch("scripts.perf.bench.require_disposable_database", return_value="perf_qual_fixture"),
                mock_patch.dict(os.environ, {"ITAMBOX_BENCH_USERNAME": "benchmark-test"}),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                exit_code = main(["--manifest", "manifest.json", "--count-queries", "--output", str(output)])
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                result["query_counts"],
                {
                    "asset_list_first_page": 11,
                    "asset_list_filtered_page": 13,
                    "asset_detail": 7,
                },
            )
            self.assertIn("QUERY COUNTS, NOT LATENCY", output.with_suffix(".md").read_text(encoding="utf-8"))

    def test_write_paths_require_disposable_confirmation_and_loopback(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(["--manifest", "manifest.json"])
        args = parse_args(
            [
                "--manifest",
                "manifest.json",
                "--base-url",
                "http://localhost:8010",
                "--confirm-disposable",
            ]
        )
        self.assertIn("import_1000_rows", args.paths)


if __name__ == "__main__":
    unittest.main()
