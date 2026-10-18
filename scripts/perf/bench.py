"""Measure ITAMbox performance over authenticated HTTP.

HTTP latency is always measured against the running server. SQL query counts are
collected separately with Django's test client and are never reported as latency.
All write-path runs require an explicitly disposable local benchmark database.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.cookiejar
import io
import ipaddress
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable

DEFAULT_BASE_URL = "https://qual.itambox.dev"
DEFAULT_OUTPUT = Path("artifacts/perf-results.json")
READ_PATHS = (
    "asset_list_first_page",
    "asset_list_filtered_page",
    "asset_detail",
    "global_search",
    "rest_list",
    "graphql_list",
    "graphql_detail",
)
WRITE_PATHS = ("import_1000_rows", "qcluster_throughput")
ALL_PATHS = READ_PATHS + WRITE_PATHS
IMPORT_ROWS = 1_000
MIN_HTTP_SAMPLES = 100
BOOTSTRAP_REPLICATES = 2_000


class BenchmarkError(RuntimeError):
    """A safe-to-report benchmark failure; messages must not contain secrets."""


class HttpResponse:
    """One HTTP response with its measured wall-clock time."""

    __slots__ = ("body", "diagnostics", "elapsed_ms", "final_url", "request_id", "status")

    def __init__(
        self,
        status: int,
        final_url: str,
        body: str,
        elapsed_ms: float,
        *,
        request_id: str,
        diagnostics: dict[str, Any] | None,
    ):
        self.status = status
        self.final_url = final_url
        self.body = body
        self.elapsed_ms = elapsed_ms
        self.request_id = request_id
        self.diagnostics = diagnostics


class CountingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Include automatic redirects in request totals, not only opener calls."""

    def __init__(self, session: HttpSession):
        super().__init__()
        self.session = session

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.session.request_count += 1
        if self.session.authentication_request_in_flight:
            self.session.login_request_count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpSession:
    """Cookie-authenticated HTTP session with request/error accounting."""

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.request_count = 0
        self.error_count = 0
        self.login_request_count = 0
        self.authentication_request_in_flight = False
        self.request_records: list[dict[str, Any]] = []
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), CountingRedirectHandler(self)
        )

    def cookie_value(self, name: str) -> str | None:
        return next((cookie.value for cookie in self.cookies if cookie.name == name), None)

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authentication_request: bool = False,
    ) -> HttpResponse:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        request_headers = {"User-Agent": "itambox-performance-benchmark/1.0"}
        request_headers.update(headers or {})
        request_id = uuid.uuid4().hex
        request_headers["X-Perf-Request-ID"] = request_id
        body: bytes | None
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        self.request_count += 1
        self.authentication_request_in_flight = authentication_request
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                result = HttpResponse(
                    response.status,
                    response.geturl(),
                    response_body,
                    (time.perf_counter() - started) * 1_000,
                    request_id=request_id,
                    diagnostics=parse_response_diagnostics(response.headers, request_id),
                )
        except urllib.error.HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            result = HttpResponse(
                error.code,
                error.geturl(),
                response_body,
                (time.perf_counter() - started) * 1_000,
                request_id=request_id,
                diagnostics=parse_response_diagnostics(error.headers, request_id),
            )
        except OSError as error:
            self.error_count += 1
            if authentication_request:
                self.login_request_count += 1
            self.request_records.append(
                {
                    "request_id": request_id,
                    "method": method,
                    "path": urllib.parse.urlsplit(url).path,
                    "status": None,
                    "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3),
                    "diagnostics": None,
                }
            )
            raise BenchmarkError(f"HTTP transport failed ({type(error).__name__})") from None
        finally:
            self.authentication_request_in_flight = False
        if authentication_request:
            self.login_request_count += 1
        if result.status >= 400:
            self.error_count += 1
        self.request_records.append(
            {
                "request_id": result.request_id,
                "method": method,
                "path": urllib.parse.urlsplit(url).path,
                "status": result.status,
                "elapsed_ms": round(result.elapsed_ms, 3),
                "diagnostics": result.diagnostics,
            }
        )
        return result

    def login(self, login_path: str, dashboard_path: str, username: str, password: str) -> None:
        page = self.request(login_path, authentication_request=True)
        if page.status != 200:
            raise BenchmarkError(f"login page returned HTTP {page.status}")
        match = re.search(r"name=[\"\']csrfmiddlewaretoken[\"\']\s+value=[\"\']([^\"\']+)", page.body)
        if match is None:
            raise BenchmarkError("login page did not expose a CSRF token")
        token = match.group(1)
        result = self.request(
            login_path,
            method="POST",
            data={
                "csrfmiddlewaretoken": token,
                "username": username,
                "password": password,
                "next": dashboard_path,
            },
            headers={"Referer": f"{self.base_url}{login_path}", "X-CSRFToken": token},
            authentication_request=True,
        )
        final_path = urllib.parse.urlsplit(result.final_url).path
        if result.status != 200 or final_path.startswith(("/accounts/login", "/accounts/mfa")):
            raise BenchmarkError(f"HTTP authentication failed (status={result.status})")


def parse_response_diagnostics(headers: Any, request_id: str) -> dict[str, Any] | None:
    """Read the opt-in, per-request diagnostics emitted by the perf WSGI wrapper."""
    if headers.get("X-Perf-Request-ID") != request_id:
        return None
    required_headers = (
        "X-Perf-Worker-PID",
        "X-Perf-Worker-Instance",
        "X-Perf-Query-Count",
        "X-Perf-Query-Time-MS",
        "X-Perf-Slow-Query-Count",
        "X-Perf-Slow-Queries-B64",
        "X-Perf-GC-Pause-Count",
        "X-Perf-GC-Pause-Time-MS",
        "X-Perf-GC-Max-Pause-MS",
    )
    if any(headers.get(name) is None for name in required_headers):
        return {"available": False, "request_id": request_id, "error": "incomplete diagnostics headers"}
    try:
        encoded_slow_queries = headers.get("X-Perf-Slow-Queries-B64", "")
        slow_queries = (
            json.loads(base64.urlsafe_b64decode(encoded_slow_queries).decode("utf-8")) if encoded_slow_queries else []
        )
        if not isinstance(slow_queries, list):
            raise ValueError("slow-query header is not a list")
        worker_instance_id = headers.get("X-Perf-Worker-Instance", "")
        if not worker_instance_id:
            raise ValueError("missing worker instance identifier")
        return {
            "available": True,
            "request_id": request_id,
            "worker_pid": int(headers.get("X-Perf-Worker-PID", "0")),
            "worker_instance_id": worker_instance_id,
            "db_query_count": int(headers.get("X-Perf-Query-Count", "0")),
            "db_time_ms": float(headers.get("X-Perf-Query-Time-MS", "0")),
            "slow_query_count": int(headers.get("X-Perf-Slow-Query-Count", "0")),
            "slow_queries": slow_queries,
            "gc_pause_count": int(headers.get("X-Perf-GC-Pause-Count", "0")),
            "gc_pause_time_ms": float(headers.get("X-Perf-GC-Pause-Time-MS", "0")),
            "gc_max_pause_ms": float(headers.get("X-Perf-GC-Max-Pause-MS", "0")),
        }
    except ValueError:
        return {"available": False, "request_id": request_id, "error": "invalid diagnostics headers"}


def validate_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--base-url must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--base-url must not include credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("--base-url must not include a path prefix")
    hostname = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if hostname != "qual.itambox.dev" and not loopback:
        raise ValueError("only qual.itambox.dev or a loopback host is allowed")
    if hostname == "qual.itambox.dev" and parsed.scheme != "https":
        raise ValueError("qual.itambox.dev must use HTTPS")
    if hostname == "qual.itambox.dev" and parsed.port not in (None, 443):
        raise ValueError("qual.itambox.dev must use the standard HTTPS port")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="dataset manifest from the benchmark seeder")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", type=Path, help="JSON output path; a sibling .md table is also written")
    parser.add_argument(
        "--iterations", type=int, default=MIN_HTTP_SAMPLES, help="measured iterations per HTTP read path (minimum 100)"
    )
    parser.add_argument("--warmups", type=int, default=2, help="discarded warmups per path")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1, help="Gunicorn worker count used by the target server")
    parser.add_argument(
        "--qcluster-workers", type=int, help="qcluster worker count; inferred from Django settings when omitted"
    )
    parser.add_argument("--import-repeats", type=int, default=5)
    parser.add_argument("--queue-repeats", type=int, default=5)
    parser.add_argument("--queue-tasks", type=int, default=1_000)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--job-timeout-seconds", type=float, default=1_800)
    parser.add_argument("--paths", nargs="+", choices=ALL_PATHS, default=list(ALL_PATHS))
    parser.add_argument("--count-queries", action="store_true", help="report SQL counts only; this is NOT latency")
    parser.add_argument(
        "--confirm-disposable",
        action="store_true",
        help="confirm that write paths target the dedicated perf_qual_* database",
    )
    args = parser.parse_args(argv)
    try:
        args.base_url = validate_base_url(args.base_url)
    except ValueError as error:
        parser.error(str(error))
    values = [args.iterations, args.warmups, args.concurrency, args.workers, args.import_repeats, args.queue_repeats]
    if min(values) < 1 or args.queue_tasks < 1 or args.timeout_seconds <= 0 or args.job_timeout_seconds <= 0:
        parser.error("iteration, worker, batch, concurrency, and timeout values must be positive")
    if args.qcluster_workers is not None and args.qcluster_workers < 1:
        parser.error("--qcluster-workers must be positive")
    if args.count_queries:
        if args.paths != list(ALL_PATHS):
            parser.error("--paths cannot be combined with --count-queries")
    elif set(args.paths) & set(WRITE_PATHS):
        if not args.confirm_disposable:
            parser.error("import/qcluster write paths require --confirm-disposable")
        if not is_loopback_url(args.base_url):
            parser.error("import/qcluster write paths are restricted to a loopback HTTP server")
    _validate_http_sample_count(parser, args)
    return args


def _validate_http_sample_count(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.count_queries or not (set(args.paths) & set(READ_PATHS)):
        return
    if args.iterations < MIN_HTTP_SAMPLES:
        parser.error(f"HTTP probes require at least {MIN_HTTP_SAMPLES} measured samples per path")


def is_loopback_url(base_url: str) -> bool:
    hostname = urllib.parse.urlsplit(base_url).hostname or ""
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def setup_django() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    app_root = repo_root / "itambox"
    for import_root in (repo_root, app_root):
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
    os.environ.setdefault("ITAMBOX_ENV", "dev")
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if settings_module not in (None, "core.settings.dev"):
        raise BenchmarkError("benchmark requires DJANGO_SETTINGS_MODULE=core.settings.dev")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")
    if os.environ.get("ITAMBOX_ENV") != "dev":
        raise BenchmarkError("benchmark tooling requires ITAMBOX_ENV=dev")
    # inline import: app-registry: django itself is imported before django.setup() configures the app registry
    import django

    django.setup()
    # inline import: app-registry: framework modules may only be imported after django.setup() ran
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db import connection
    from django.test import Client
    from django.test.utils import CaptureQueriesContext
    from django.urls import reverse
    from django_q.models import Task
    from django_q.tasks import async_task

    # inline import: app-registry: application models may only be imported after django.setup() ran
    from assets.models import Asset
    from core.models import Job

    return {
        "settings": settings,
        "get_user_model": get_user_model,
        "connection": connection,
        "Client": Client,
        "CaptureQueriesContext": CaptureQueriesContext,
        "reverse": reverse,
        "Task": Task,
        "Asset": Asset,
        "Job": Job,
        "async_task": async_task,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"could not read benchmark manifest ({type(error).__name__})") from None
    if not isinstance(manifest, dict):
        raise BenchmarkError("manifest must be a JSON object")
    benchmark = manifest.get("benchmark")
    if not isinstance(benchmark, dict):
        raise BenchmarkError("manifest must contain a benchmark object")
    required = (
        "asset_id",
        "tenant_group_id",
        "filtered_q",
        "search_term",
        "import_tenant_id",
        "asset_type_id",
        "status_id",
    )
    missing = [key for key in required if key not in benchmark]
    if missing:
        raise BenchmarkError(f"manifest is missing benchmark keys: {', '.join(missing)}")
    return manifest


def require_disposable_database(bindings: dict[str, Any], manifest: dict[str, Any], *, strict: bool = True) -> str:
    connection = bindings["connection"]
    settings = bindings["settings"]
    database = connection.settings_dict
    name = str(database.get("NAME", ""))
    if not settings.DEBUG or os.environ.get("ITAMBOX_ENV") != "dev":
        raise BenchmarkError("benchmark requires Django development settings")
    if connection.vendor != "postgresql":
        raise BenchmarkError("benchmark requires PostgreSQL")
    manifest_database = manifest.get("database")
    if manifest_database and manifest_database != name:
        raise BenchmarkError("manifest database does not match the active Django database")
    if not strict:
        return name
    if not name.startswith("perf_qual_"):
        raise BenchmarkError("benchmark database name must start with perf_qual_")
    host = str(database.get("HOST", "")).strip().lower()
    if host:
        try:
            is_local_host = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_local_host = host == "localhost"
        if not is_local_host:
            raise BenchmarkError("benchmark database must use a loopback host")
    return name


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(len(ordered) * probability)))
    return ordered[rank - 1]


def bootstrap_percentile_interval(
    samples: list[float],
    probability: float,
    *,
    confidence: float = 0.95,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Return a deterministic percentile-bootstrap interval for one quantile."""
    if not samples:
        return None
    generator = random.Random(seed)
    estimates = [percentile(generator.choices(samples, k=len(samples)), probability) for _ in range(replicates)]
    tail = (1 - confidence) / 2
    return percentile(estimates, tail), percentile(estimates, 1 - tail)


def summarize_samples(samples: list[float], *, bootstrap_seed: int = 0) -> dict[str, Any]:
    if not samples:
        return {
            "sample_count": 0,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
            "p95_bootstrap_ci_ms": None,
            "samples_ms": [],
        }
    interval = bootstrap_percentile_interval(samples, 0.95, seed=bootstrap_seed)
    return {
        "sample_count": len(samples),
        "p50_ms": round(median(samples), 3),
        "p90_ms": round(percentile(samples, 0.90), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "p99_ms": round(percentile(samples, 0.99), 3),
        "max_ms": round(max(samples), 3),
        "p95_bootstrap_ci_ms": {
            "method": "percentile bootstrap",
            "confidence_level": 0.95,
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": bootstrap_seed,
            "lower_ms": round(interval[0], 3),
            "upper_ms": round(interval[1], 3),
        },
        "samples_ms": [round(sample, 3) for sample in samples],
    }


def scoped_path(path: str, **params: Any) -> str:
    query = urllib.parse.urlencode(params)
    return f"{path}?{query}" if query else path


def build_http_paths(bindings: dict[str, Any], manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reverse = bindings["reverse"]
    benchmark = manifest["benchmark"]
    tenant_group = benchmark["tenant_group_id"]
    scope = {"switch_tenant_group": tenant_group}
    asset_list = reverse("assets:asset_list")
    asset_detail = reverse("assets:asset_detail", args=[benchmark["asset_id"]])
    search_term = benchmark["search_term"]
    filtered_q = benchmark["filtered_q"]
    tag_prefix = benchmark.get("asset_tag_prefix", "PF882-")
    filtered_text = benchmark.get("filtered_result_text", "Perf 882 Asset 000123")
    detail_text = benchmark.get("detail_result_text", "PF882-000000")
    search_text = benchmark.get("search_result_text", "Perf 882 Asset 000042")
    api_path = reverse("api:assets_api:asset-list")
    graphql_path = reverse("graphql")
    graphql_scope = "{ mode: TENANT_GROUP, tenantGroupId: " + json.dumps(str(tenant_group)) + " }"
    graphql_list = (
        "query PerfAssets { assets(requestedScope: "
        + graphql_scope
        + ", limit: 25, offset: 0) { id name assetTag serialNumber } }"
    )
    graphql_detail = (
        'query PerfAsset { asset(id: "'
        + str(benchmark["asset_id"])
        + '", requestedScope: '
        + graphql_scope
        + ") { id name assetTag serialNumber status { name } location { name } } }"
    )
    return {
        "asset_list_first_page": {
            "path": scoped_path(asset_list, **scope),
            "expected_text": tag_prefix,
        },
        "asset_list_filtered_page": {
            "path": scoped_path(asset_list, **scope, q=filtered_q),
            "expected_text": filtered_text,
        },
        "asset_detail": {
            "path": scoped_path(asset_detail, **scope),
            "expected_text": detail_text,
        },
        "global_search": {
            "path": scoped_path(reverse("search"), q=search_term, **scope),
            "expected_text": search_text,
        },
        "rest_list": {
            "path": scoped_path(api_path, **scope),
            "expected_text": tag_prefix,
            "response_type": "rest",
        },
        "graphql_list": {
            "path": scoped_path(graphql_path, **scope),
            "method": "POST",
            "json": {"query": graphql_list},
            "expected_text": tag_prefix,
            "response_type": "graphql",
        },
        "graphql_detail": {
            "path": scoped_path(graphql_path, **scope),
            "method": "POST",
            "json": {"query": graphql_detail},
            "expected_text": detail_text,
            "response_type": "graphql",
        },
    }


def validate_response(response: HttpResponse, name: str, expected_text: str, response_type: str | None) -> None:
    final_path = urllib.parse.urlsplit(response.final_url).path
    if response.status < 200 or response.status >= 300:
        raise BenchmarkError(f"{name} returned HTTP {response.status}")
    if final_path.startswith(("/accounts/login", "/accounts/mfa")):
        raise BenchmarkError(f"{name} redirected to an authentication page")
    if response_type in {"rest", "graphql"}:
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError:
            raise BenchmarkError(f"{name} returned invalid JSON") from None
        if response_type == "graphql" and payload.get("errors"):
            raise BenchmarkError(f"{name} returned GraphQL errors")
        if response_type == "rest":
            rows = payload.get("results") if isinstance(payload, dict) else payload
            if not isinstance(rows, list) or not rows:
                raise BenchmarkError("REST list returned no benchmark rows")
    if expected_text and expected_text not in response.body:
        raise BenchmarkError(f"{name} did not return the expected benchmark data")


class _ScenarioRunner:
    """Run one scenario's warmup and measured phases and accumulate its totals."""

    __slots__ = (
        "action",
        "concurrency",
        "error_types",
        "samples",
        "sample_records",
        "sessions",
        "totals",
        "uses_http",
        "warmup_samples",
    )

    def __init__(
        self,
        action: Callable[[HttpSession], float],
        sessions: list[HttpSession],
        *,
        concurrency: int,
        uses_http: bool,
    ) -> None:
        self.action = action
        self.sessions = sessions
        self.concurrency = concurrency
        self.uses_http = uses_http
        self.totals = {"request_count": 0, "error_count": 0, "warmup_request_count": 0, "measured_request_count": 0}
        self.samples: list[float] = []
        self.warmup_samples: list[float] = []
        self.sample_records: list[dict[str, Any]] = []
        self.error_types: dict[str, int] = {}

    def run_phase(self, count: int, *, measured: bool) -> None:
        if count == 0:
            return
        work = [
            (self.sessions[worker], [index for index in range(count) if index % self.concurrency == worker], measured)
            for worker in range(self.concurrency)
        ]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            phase_results = list(pool.map(self.run_worker, work))
        for worker_results in phase_results:
            self.accumulate(worker_results, measured=measured)

    def run_worker(
        self, work: tuple[HttpSession, list[int], bool]
    ) -> list[tuple[float | None, int, int, str | None, dict[str, Any]]]:
        session, indices, measured = work
        return [self.measure(session, measured=measured, sample_index=index) for index in indices]

    def measure(
        self, session: HttpSession, *, measured: bool, sample_index: int
    ) -> tuple[float | None, int, int, str | None, dict[str, Any]]:
        before_requests = session.request_count
        before_errors = session.error_count
        before_records = len(session.request_records)
        try:
            elapsed_ms = self.action(session)
        # broad except: task-isolation: one failed iteration is recorded and must not abort the remaining iterations
        except Exception as error:
            error_type = type(error).__name__
            request_records = session.request_records[before_records:]
            return (
                None,
                self.request_delta(session, before_requests),
                max(1, self.error_delta(session, before_errors)),
                error_type,
                self.sample_record(None, measured, sample_index, error_type, request_records),
            )
        request_records = session.request_records[before_records:]
        return (
            float(elapsed_ms),
            self.request_delta(session, before_requests),
            self.error_delta(session, before_errors),
            None,
            self.sample_record(float(elapsed_ms), measured, sample_index, None, request_records),
        )

    @staticmethod
    def sample_record(
        elapsed_ms: float | None,
        measured: bool,
        sample_index: int,
        error_type: str | None,
        request_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "phase": "measured" if measured else "warmup",
            "iteration": sample_index + 1,
            "elapsed_ms": round(elapsed_ms, 3) if elapsed_ms is not None else None,
            "error_type": error_type,
            "requests": request_records,
        }

    def request_delta(self, session: HttpSession, before: int) -> int:
        return session.request_count - before if self.uses_http else 0

    def error_delta(self, session: HttpSession, before: int) -> int:
        return session.error_count - before if self.uses_http else 0

    def accumulate(
        self,
        worker_results: list[tuple[float | None, int, int, str | None, dict[str, Any]]],
        *,
        measured: bool,
    ) -> None:
        key = "measured_request_count" if measured else "warmup_request_count"
        for elapsed_ms, request_count, error_count, error_type, record in worker_results:
            self.totals["request_count"] += request_count
            self.totals[key] += request_count
            self.totals["error_count"] += error_count
            self.sample_records.append(record)
            if error_type:
                self.error_types[error_type] = self.error_types.get(error_type, 0) + 1
            if measured and elapsed_ms is not None:
                self.samples.append(elapsed_ms)
            elif not measured and elapsed_ms is not None:
                self.warmup_samples.append(elapsed_ms)


def run_scenario(
    name: str,
    action: Callable[[HttpSession], float],
    sessions: list[HttpSession],
    *,
    iterations: int,
    warmups: int,
    concurrency: int,
    uses_http: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runner = _ScenarioRunner(action, sessions, concurrency=concurrency, uses_http=uses_http)
    runner.run_phase(warmups, measured=False)
    runner.run_phase(iterations, measured=True)
    result = {
        "path": name,
        "iterations": iterations,
        "warmups": warmups,
        "concurrency": concurrency,
        **summarize_samples(
            runner.samples,
            bootstrap_seed=int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4], "big"),
        ),
        **runner.totals,
        "warmup_sample_count": len(runner.warmup_samples),
        "warmup_samples_ms": [round(sample, 3) for sample in runner.warmup_samples],
        "sample_records": runner.sample_records,
        "error_types": runner.error_types,
    }
    if details:
        result.update(details)
    return result


def read_action(session: HttpSession, name: str, config: dict[str, Any]) -> float:
    payload = config.get("json")
    headers = {"Content-Type": "application/json"} if payload is not None else None
    csrf = session.cookie_value("csrftoken")
    if headers is not None and csrf:
        headers.update({"X-CSRFToken": csrf, "Referer": f"{session.base_url}/"})
    response = session.request(
        config["path"],
        method=config.get("method", "GET"),
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers,
    )
    validate_response(response, name, config.get("expected_text", ""), config.get("response_type"))
    return response.elapsed_ms


def get_csrf_token(body: str, context: str) -> str:
    match = re.search(r"name=[\"\']csrfmiddlewaretoken[\"\']\s+value=[\"\']([^\"\']+)", body)
    if match is None:
        raise BenchmarkError(f"{context} did not expose a CSRF token")
    return match.group(1)


def import_once(
    session: HttpSession,
    manifest: dict[str, Any],
    bindings: dict[str, Any],
    timeout_seconds: float,
) -> float:
    benchmark = manifest["benchmark"]
    reverse = bindings["reverse"]
    scope_url = scoped_path(reverse("assets:asset_list"), switch_tenant=benchmark["import_tenant_id"])
    scoped_response = session.request(scope_url)
    validate_response(scoped_response, "import_tenant_scope", "", None)
    import_path = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
    form = session.request(import_path)
    validate_response(form, "import_form", "", None)
    csrf = get_csrf_token(form.body, "import form")
    prefix = f"PERF-BENCH-{uuid.uuid4().hex[:10]}"
    csv_stream = io.StringIO(newline="")
    writer = csv.writer(csv_stream)
    writer.writerow(("name", "asset_tag", "serial_number", "status", "asset_type"))
    for index in range(IMPORT_ROWS):
        writer.writerow(
            (
                f"Benchmark import {index:04d}",
                f"{prefix}-{index:04d}",
                f"{prefix}-SN-{index:04d}",
                benchmark["status_id"],
                benchmark["asset_type_id"],
            )
        )
    started = time.perf_counter()
    preview = session.request(
        import_path,
        method="POST",
        data={
            "csrfmiddlewaretoken": csrf,
            "active_tab": "editor",
            "import_format": "csv",
            "delimiter": ",",
            "import_text": csv_stream.getvalue(),
            "_preview": "1",
        },
        headers={"Referer": f"{session.base_url}{import_path}", "X-CSRFToken": csrf},
    )
    validate_response(preview, "import_preview", prefix, None)
    confirmation = session.request(
        import_path,
        method="POST",
        data={"csrfmiddlewaretoken": csrf, "_confirm": "1"},
        headers={"Referer": f"{session.base_url}{import_path}", "X-CSRFToken": csrf},
    )
    validate_response(confirmation, "import_confirmation", "", None)
    job_match = re.search(r"/jobs/(\d+)/", urllib.parse.urlsplit(confirmation.final_url).path)
    if job_match is None:
        raise BenchmarkError("import confirmation did not return a job detail URL")
    job_id = int(job_match.group(1))
    job_model = bindings["Job"]
    asset_model = bindings["Asset"]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = job_model._base_manager.filter(pk=job_id).first()
        if job is None:
            raise BenchmarkError("import job disappeared before completion")
        if job.status == job_model.STATUS_FAILED:
            raise BenchmarkError("import job failed")
        imported = asset_model._base_manager.filter(asset_tag__startswith=prefix).count()
        if job.status == job_model.STATUS_COMPLETED and imported == IMPORT_ROWS:
            return (time.perf_counter() - started) * 1_000
        time.sleep(0.2)
    raise BenchmarkError("1,000-row import did not finish before the configured timeout")


def queue_once(bindings: dict[str, Any], task_count: int, timeout_seconds: float) -> float:
    settings = bindings["settings"]
    if settings.Q_CLUSTER.get("sync"):
        raise BenchmarkError("qcluster benchmark requires asynchronous task execution")
    async_task = bindings["async_task"]
    task_model = bindings["Task"]
    started = time.perf_counter()
    task_ids = [async_task("operator.add", 1, 1) for _ in range(task_count)]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        query = task_model._base_manager.filter(pk__in=task_ids)
        failed = query.filter(success=False).count()
        if failed:
            raise BenchmarkError(f"qcluster reported {failed} failed benchmark tasks")
        succeeded = query.filter(success=True).count()
        if succeeded == task_count:
            return (time.perf_counter() - started) * 1_000
        time.sleep(0.2)
    raise BenchmarkError("qcluster did not drain the task batch before the configured timeout")


def query_count_results(bindings: dict[str, Any], manifest: dict[str, Any], username: str) -> dict[str, Any]:
    connection = bindings["connection"]
    benchmark = manifest["benchmark"]
    reverse = bindings["reverse"]
    user = bindings["get_user_model"]()._base_manager.filter(username=username).first()
    if user is None:
        raise BenchmarkError("benchmark principal does not exist in the active database")
    client = bindings["Client"]()
    client.force_login(user)
    scope = {"switch_tenant_group": benchmark["tenant_group_id"]}
    list_path = scoped_path(reverse("assets:asset_list"), **scope)
    filtered_path = scoped_path(reverse("assets:asset_list"), **scope, q=benchmark["filtered_q"])
    detail_path = scoped_path(reverse("assets:asset_detail", args=[benchmark["asset_id"]]), **scope)
    paths = {
        "asset_list_first_page": (list_path, benchmark.get("asset_tag_prefix", "PF882-")),
        "asset_list_filtered_page": (
            filtered_path,
            benchmark.get("filtered_result_text", "Perf 882 Asset 000123"),
        ),
        "asset_detail": (detail_path, benchmark.get("detail_result_text", "PF882-000000")),
    }
    counts: dict[str, int] = {}
    for name, (path, expected_text) in paths.items():
        with bindings["CaptureQueriesContext"](connection) as queries:
            response = client.get(path, HTTP_HOST="127.0.0.1")
        body = response.content.decode("utf-8", errors="replace")
        if response.status_code != 200 or expected_text not in body:
            raise BenchmarkError(f"query-count probe {name} returned an invalid response")
        counts[name] = len(queries)
    return {
        "mode": "QUERY COUNTS, NOT LATENCY",
        "database": connection.settings_dict["NAME"],
        "query_counts": counts,
    }


def render_markdown(result: dict[str, Any]) -> str:
    if result.get("mode") == "QUERY COUNTS, NOT LATENCY":
        lines = [
            "QUERY COUNTS, NOT LATENCY",
            "",
            "| Path | SQL queries |",
            "|---|---:|",
        ]
        lines.extend(f"| {name} | {count} |" for name, count in result["query_counts"].items())
        return "\n".join(lines) + "\n"
    runtime = result["runtime"]
    workers = runtime["worker_counts"]
    lines = [
        "| Path | p50 (ms) | p90 (ms) | p95 (ms) | p95 bootstrap 95% CI (ms) | p99 (ms) | Max (ms) | Requests | Errors | Samples |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["results"]:
        interval = row.get("p95_bootstrap_ci_ms")
        interval_text = f"{interval['lower_ms']} - {interval['upper_ms']}" if interval is not None else "-"
        lines.append(
            "| {path} | {p50} | {p90} | {p95} | {ci} | {p99} | {maximum} | {requests} | {errors} | {samples} |".format(
                path=row["path"],
                p50=row.get("p50_ms") if row.get("p50_ms") is not None else "-",
                p90=row.get("p90_ms") if row.get("p90_ms") is not None else "-",
                p95=row.get("p95_ms") if row.get("p95_ms") is not None else "-",
                ci=interval_text,
                p99=row.get("p99_ms") if row.get("p99_ms") is not None else "-",
                maximum=row.get("max_ms") if row.get("max_ms") is not None else "-",
                requests=row["request_count"],
                errors=row["error_count"],
                samples=row["sample_count"],
            )
        )
    lines.extend(
        [
            "",
            (
                f"Concurrency: {runtime['concurrency']}; Gunicorn workers: {workers['gunicorn']}; "
                f"qcluster workers: {workers['qcluster']}; warmups: {runtime['warmups']}; "
                f"iterations: {runtime['iterations']}."
            ),
            f"HTTP requests: {result['totals']['request_count']}; errors: {result['totals']['error_count']}.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(result: dict[str, Any], output: Path | None) -> tuple[Path, Path, str]:
    if output is None:
        output = Path("artifacts/perf-query-counts.json" if result.get("mode") else DEFAULT_OUTPUT)
    markdown_path = output.with_suffix(".md")
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return output, markdown_path, markdown


def _resolve_qcluster_workers(args: argparse.Namespace, settings: Any) -> int | None:
    """Resolve the qcluster worker count and validate an explicit override."""
    configured = settings.Q_CLUSTER.get("workers")
    q_workers = args.qcluster_workers if args.qcluster_workers is not None else configured
    if "qcluster_throughput" not in args.paths:
        return q_workers
    if q_workers is None:
        raise BenchmarkError("qcluster worker count is unknown; pass --qcluster-workers")
    if args.qcluster_workers is not None and configured is not None and args.qcluster_workers != configured:
        raise BenchmarkError("--qcluster-workers does not match Django's configured qcluster worker count")
    return q_workers


def _open_sessions(
    args: argparse.Namespace,
    bindings: dict[str, Any],
    username: str | None,
    password: str | None,
    needs_http: bool,
) -> tuple[list[HttpSession], int]:
    """Create the session pool and, for HTTP paths, authenticate every session."""
    sessions = [HttpSession(args.base_url, args.timeout_seconds) for _ in range(args.concurrency)]
    if not needs_http:
        return sessions, 0
    if username is None or password is None:
        raise BenchmarkError("HTTP paths require the benchmark principal environment variables")
    reverse = bindings["reverse"]
    login_path = reverse("login")
    dashboard_path = reverse("dashboard")
    for session in sessions:
        session.login(login_path, dashboard_path, username, password)
    login_requests = sum(session.login_request_count for session in sessions)
    for session in sessions:
        session.request_count = 0
        session.error_count = 0
        session.request_records.clear()
    return sessions, login_requests


def _read_scenario(
    name: str, config: dict[str, Any], args: argparse.Namespace, sessions: list[HttpSession]
) -> dict[str, Any]:
    return run_scenario(
        name,
        lambda session, current=name, item=config: read_action(session, current, item),
        sessions,
        iterations=args.iterations,
        warmups=args.warmups,
        concurrency=args.concurrency,
        uses_http=True,
    )


def _import_scenario(
    args: argparse.Namespace, manifest: dict[str, Any], bindings: dict[str, Any], sessions: list[HttpSession]
) -> dict[str, Any]:
    row = run_scenario(
        "import_1000_rows",
        lambda session: import_once(session, manifest, bindings, args.job_timeout_seconds),
        sessions,
        iterations=args.import_repeats,
        warmups=args.warmups,
        concurrency=args.concurrency,
        uses_http=True,
        details={"rows_per_batch": IMPORT_ROWS},
    )
    if row["sample_count"]:
        completed_rows = row["sample_count"] * IMPORT_ROWS
        total_seconds = sum(row["samples_ms"]) / 1_000
        row["completed_rows"] = completed_rows
        row["rows_per_second"] = round(completed_rows / total_seconds, 3) if total_seconds else None
    return row


def _queue_scenario(
    args: argparse.Namespace, bindings: dict[str, Any], sessions: list[HttpSession], q_workers: int | None
) -> dict[str, Any]:
    row = run_scenario(
        "qcluster_throughput",
        lambda _session: queue_once(bindings, args.queue_tasks, args.job_timeout_seconds),
        sessions,
        iterations=args.queue_repeats,
        warmups=args.warmups,
        concurrency=args.concurrency,
        uses_http=False,
        details={"tasks_per_batch": args.queue_tasks, "qcluster_workers": q_workers},
    )
    if row["sample_count"]:
        total_tasks = row["sample_count"] * args.queue_tasks
        total_seconds = sum(row["samples_ms"]) / 1_000
        row["completed_tasks"] = total_tasks
        row["tasks_per_second"] = round(total_tasks / total_seconds, 3) if total_seconds else None
    return row


def process_observations(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize per-request worker identities without mistaking normal routing for restarts."""
    requests = [
        request for row in results for sample in row.get("sample_records", []) for request in sample.get("requests", [])
    ]
    diagnostics = [
        request["diagnostics"]
        for request in requests
        if isinstance(request.get("diagnostics"), dict) and request["diagnostics"].get("available")
    ]
    identities: dict[int, str] = {}
    observed: set[tuple[int, str]] = set()
    pid_reuse_events: list[dict[str, Any]] = []
    for item in diagnostics:
        pid = item["worker_pid"]
        instance_id = item["worker_instance_id"]
        previous_instance = identities.setdefault(pid, instance_id)
        observed.add((pid, instance_id))
        if previous_instance != instance_id:
            pid_reuse_events.append({"pid": pid, "previous_instance_id": previous_instance, "instance_id": instance_id})
            identities[pid] = instance_id
    return {
        "request_count": len(requests),
        "instrumented_request_count": len(diagnostics),
        "uninstrumented_request_count": len(requests) - len(diagnostics),
        "worker_processes": [{"pid": pid, "instance_id": instance_id} for pid, instance_id in sorted(observed)],
        "pid_reuse_or_process_restart_events": pid_reuse_events,
        "restart_detection_limit": (
            "Per-request PID and process-instance IDs are retained. A PID observed with a new instance ID is flagged; "
            "a restart that occurs between probes and is not sampled cannot be inferred."
        ),
    }


def _run_selected_paths(
    args: argparse.Namespace,
    bindings: dict[str, Any],
    manifest: dict[str, Any],
    paths: dict[str, dict[str, Any]],
    sessions: list[HttpSession],
    q_workers: int | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in args.paths:
        if name in READ_PATHS:
            results.append(_read_scenario(name, paths[name], args, sessions))
        elif name == "import_1000_rows":
            results.append(_import_scenario(args, manifest, bindings, sessions))
        elif name == "qcluster_throughput":
            results.append(_queue_scenario(args, bindings, sessions, q_workers))
    return results


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    bindings = setup_django()
    manifest = load_manifest(args.manifest)
    needs_disposable_db = args.count_queries or bool(set(args.paths) & set(WRITE_PATHS))
    database = require_disposable_database(bindings, manifest, strict=needs_disposable_db)
    username = os.environ.get("ITAMBOX_BENCH_USERNAME")
    password = os.environ.get("ITAMBOX_BENCH_PASSWORD")
    if args.count_queries:
        if not username:
            raise BenchmarkError("set ITAMBOX_BENCH_USERNAME in the environment; no credentials are logged")
        return query_count_results(bindings, manifest, username)

    needs_http = any(name in READ_PATHS or name == "import_1000_rows" for name in args.paths)
    if needs_http and (not username or not password):
        raise BenchmarkError(
            "set ITAMBOX_BENCH_USERNAME and ITAMBOX_BENCH_PASSWORD in the environment; secret values are never logged"
        )
    q_workers = _resolve_qcluster_workers(args, bindings["settings"])
    sessions, login_requests = _open_sessions(args, bindings, username, password, needs_http)
    results = _run_selected_paths(args, bindings, manifest, build_http_paths(bindings, manifest), sessions, q_workers)
    totals = {
        "request_count": sum(row["request_count"] for row in results),
        "error_count": sum(row["error_count"] for row in results),
    }
    return {
        "benchmark": "itambox-http-performance",
        "created_at": datetime.now(UTC).isoformat(),
        "database": database,
        "runtime": {
            "base_url": args.base_url,
            "http_transport": "urllib against the running application; not Django test-client latency",
            "authentication": "benchmark principal via environment; credentials omitted",
            "authentication_setup_requests": login_requests,
            "concurrency": args.concurrency,
            "iterations": args.iterations,
            "warmups": args.warmups,
            "worker_counts": {"gunicorn": args.workers, "qcluster": q_workers},
            "worker_count_source": {
                "gunicorn": "--workers operator-reported target configuration",
                "qcluster": "Django Q_CLUSTER settings, checked against --qcluster-workers when supplied",
            },
        },
        "measurement": {
            "minimum_http_samples_per_probe": MIN_HTTP_SAMPLES,
            "warmups_excluded_from_measured_samples": True,
            "percentile_method": "nearest-rank for p90/p95/p99; p50 is the median",
            "p95_confidence_interval": {
                "method": "percentile bootstrap",
                "confidence_level": 0.95,
                "replicates": BOOTSTRAP_REPLICATES,
                "seed_method": "first 32 bits of SHA-256 of the scenario name",
            },
            "slow_query_threshold_ms": 50,
            "diagnostics_source": "per-request Django connection.execute_wrapper and gc.callbacks in perf_wsgi",
        },
        "dataset": {
            "seed": manifest.get("seed"),
            "counts": manifest.get("counts", {}),
            "target_deviation": manifest.get("target_deviation"),
        },
        "results": results,
        "process_observations": process_observations(results),
        "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_benchmark(args)
        output_path, markdown_path, markdown = write_outputs(result, args.output)
    except BenchmarkError as error:
        print(f"benchmark failed: {error}", file=sys.stderr)
        return 2
    # broad except: boundary-isolation: unexpected framework and transport failures are redacted at the CLI boundary
    except Exception as error:
        print(f"benchmark failed ({type(error).__name__})", file=sys.stderr)
        return 2
    print(markdown, end="")
    print(f"JSON: {output_path}", file=sys.stderr)
    print(f"Markdown: {markdown_path}", file=sys.stderr)
    if result.get("mode") == "QUERY COUNTS, NOT LATENCY":
        return 0
    return 1 if result["totals"]["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
