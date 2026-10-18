"""Measure beta.4 asset workflows over authenticated HTTP on the target dataset.

The harness requires the disposable settings overlay and a loopback-only app
server. Query-count diagnostics use Django's in-process client; reported latency
samples are exclusively real HTTP requests to the running application.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.cookiejar
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from statistics import median
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ITAMBOX_ROOT = REPO_ROOT / "itambox"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(ITAMBOX_ROOT))
os.environ.setdefault("ITAMBOX_ENV", "dev")
os.environ["DJANGO_SETTINGS_MODULE"] = "scripts.perf.queue_settings"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.models import Count, Q  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import reverse  # noqa: E402
from django_q.models import Task  # noqa: E402

from assets.models import Asset, AssetType, Manufacturer  # noqa: E402
from core.models import Job  # noqa: E402
from extras.models import CustomField, JournalEntry  # noqa: E402
from organization.models import AssetHolder, Location, Site, Tenant, TenantGroup  # noqa: E402


class HttpSession:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        cookie_handler = urllib.request.HTTPCookieProcessor(self.cookies)
        self.opener = urllib.request.build_opener(cookie_handler)

    def cookie_value(self, name: str) -> str | None:
        return next((cookie.value for cookie in self.cookies if cookie.name == name), None)

    def request(
        self,
        path: str,
        method: str = "GET",
        data: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, str, float]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        body: bytes | None = None
        request_headers = {"User-Agent": "itambox-perf-qualification/1.0"}
        request_headers.update(headers or {})
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, bytes):
            body = data
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read().decode("utf-8", errors="replace")
                return response.status, response.geturl(), content, time.perf_counter() - started
        except urllib.error.HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            return error.code, error.geturl(), content, time.perf_counter() - started

    def login(self, username: str, password: str) -> None:
        login_path = reverse("login")
        status, _, body, _ = self.request(login_path)
        if status != 200:
            raise RuntimeError(f"login page returned HTTP {status}")
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
        if not match:
            raise RuntimeError("login page did not include a CSRF form token")
        token = match.group(1)
        status, final_url, _, _ = self.request(
            login_path,
            method="POST",
            data={
                "csrfmiddlewaretoken": token,
                "username": username,
                "password": password,
                "next": reverse("dashboard"),
            },
            headers={"Referer": f"{self.base_url}{login_path}", "X-CSRFToken": token},
        )
        if status != 200 or "/accounts/login/" in final_url or "/mfa/" in final_url:
            raise RuntimeError(f"HTTP authentication did not complete successfully (status={status})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="perf-882-admin")
    parser.add_argument("--source-sha", help="exact application source commit for an archived, git-less snapshot")
    parser.add_argument("--source-tree", help="exact application source tree for an archived, git-less snapshot")
    parser.add_argument("--baseline-results", type=Path, help="beta.4 result JSON used for query-count ceilings")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--import-repeats", type=int, default=5)
    parser.add_argument("--import-rows", type=int, default=1_000)
    parser.add_argument("--queue-repeats", type=int, default=5)
    parser.add_argument("--queue-tasks", type=int, default=1_000)
    parser.add_argument("--timeout-seconds", type=float, default=1_800)
    args = parser.parse_args()
    if min(args.repeats, args.import_repeats, args.queue_repeats, args.import_rows, args.queue_tasks) < 1:
        parser.error("repeat and batch values must be positive")
    return args


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("dataset") != "itambox-beta4-target-volume":
        raise RuntimeError("manifest is not an ITAMbox beta.4 target-volume dataset")
    if manifest.get("database") != connection.settings_dict.get("NAME"):
        raise RuntimeError("manifest database name does not match the active Django database")
    if connection.vendor != "postgresql":
        raise RuntimeError("HTTP benchmark requires PostgreSQL")
    return manifest


def source_identity(args: argparse.Namespace) -> tuple[str, str]:
    actual_sha = None
    actual_tree = None
    if (REPO_ROOT / ".git").exists():
        actual_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        actual_tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=REPO_ROOT, text=True).strip()
    source_sha = args.source_sha or actual_sha
    source_tree = args.source_tree or actual_tree
    if not source_sha or not source_tree:
        raise RuntimeError("source identity is unavailable; pass --source-sha and --source-tree")
    if actual_sha and args.source_sha and actual_sha != args.source_sha:
        raise RuntimeError("--source-sha does not match the checked-out commit")
    if actual_tree and args.source_tree and actual_tree != args.source_tree:
        raise RuntimeError("--source-tree does not match the checked-out source tree")
    return source_sha, source_tree


def verify_dataset(manifest: dict[str, Any], username: str) -> None:
    counts = manifest["counts"]
    custom_field_presence = Asset._base_manager.aggregate(
        department=Count("pk", filter=Q(custom_field_data__has_key="perf882_department")),
        service_band=Count("pk", filter=Q(custom_field_data__has_key="perf882_service_band")),
        ticket_group=Count("pk", filter=Q(custom_field_data__has_key="perf882_ticket_group")),
    )
    actual = {
        "assets": Asset._base_manager.count(),
        "asset_holders": AssetHolder._base_manager.count(),
        "tenants": Tenant._base_manager.count(),
        "tenant_groups": TenantGroup._base_manager.count(),
        "journal_entries": JournalEntry._base_manager.count(),
        "sites": Site._base_manager.count(),
        "locations": Location._base_manager.count(),
        "manufacturers": Manufacturer._base_manager.count(),
        "asset_types": AssetType._base_manager.count(),
        "custom_field_values": sum(custom_field_presence.values()),
        "custom_field_definitions": CustomField._base_manager.filter(name__startswith="perf882_").count(),
    }
    mismatches = {
        name: {"expected": counts[name], "actual": value} for name, value in actual.items() if counts[name] != value
    }
    if mismatches:
        raise RuntimeError(f"target dataset count verification failed: {mismatches}")
    user = get_user_model()._base_manager.filter(username=username, is_superuser=True).exists()
    if not user:
        raise RuntimeError(f"benchmark superuser {username!r} is missing")


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * probability + 0.999999)) - 1))
    return ordered[index]


def summarize_samples(samples: list[float], unit: str) -> dict[str, Any]:
    return {
        "sample_count": len(samples),
        "unit": unit,
        "p50": round(median(samples), 3),
        "p95": round(percentile(samples, 0.95), 3),
        "min": round(min(samples), 3),
        "max": round(max(samples), 3),
        "samples": [round(sample, 3) for sample in samples],
    }


def measure_http_path(
    session: HttpSession, name: str, path: str, repeats: int, warmups: int, json_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    headers = None
    if json_body is not None:
        headers = {"Content-Type": "application/json"}
        csrf_token = session.cookie_value("csrftoken")
        if csrf_token:
            headers.update({"X-CSRFToken": csrf_token, "Referer": session.base_url + "/"})
    for _ in range(warmups):
        status, final_url, body, _ = session.request(
            path,
            method="POST" if json_body is not None else "GET",
            data=json.dumps(json_body).encode("utf-8") if json_body is not None else None,
            headers=headers,
        )
        validate_http_response(status, final_url, body, name, json_body is not None)
    samples = []
    for _ in range(repeats):
        status, final_url, body, elapsed = session.request(
            path,
            method="POST" if json_body is not None else "GET",
            data=json.dumps(json_body).encode("utf-8") if json_body is not None else None,
            headers=headers,
        )
        validate_http_response(status, final_url, body, name, json_body is not None)
        samples.append(elapsed * 1_000)
    return {"path": name, **summarize_samples(samples, "ms")}


def validate_http_response(status: int, final_url: str, body: str, name: str, is_json: bool) -> None:
    if status != 200 or "/accounts/login/" in final_url or "/mfa/" in final_url:
        raise RuntimeError(f"{name} failed over HTTP (status={status}, final_url={final_url})")
    if is_json:
        payload = json.loads(body)
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL query {name} returned errors: {payload['errors']}")


def collect_query_counts(manifest: dict[str, Any], username: str) -> dict[str, int]:
    user = get_user_model()._base_manager.get(username=username)
    client = Client()
    client.force_login(user)
    benchmark = manifest["benchmark"]
    scope = f"?switch_tenant_group={benchmark['tenant_group_id']}"
    paths = {
        "asset_list_first_page": reverse("assets:asset_list") + scope,
        "asset_list_filtered_page": reverse("assets:asset_list") + scope + "&q=" + benchmark["filtered_q"],
        "asset_detail": reverse("assets:asset_detail", args=[benchmark["asset_id"]]) + scope,
    }
    result = {}
    for name, path in paths.items():
        with CaptureQueriesContext(connection) as queries:
            response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"query-count probe {name} returned HTTP {response.status_code}")
        result[name] = len(queries)
    return result


def compare_query_ceilings(current: dict[str, int], baseline_path: Path | None) -> dict[str, Any]:
    if baseline_path is None:
        return {"passed": None, "reason": "baseline run establishes the query-count ceilings"}
    with baseline_path.open(encoding="utf-8") as handle:
        baseline = json.load(handle)
    ceilings = baseline["query_count_diagnostics_test_client_not_timed"]
    overages = {
        name: {"ceiling": ceilings[name], "measured": value}
        for name, value in current.items()
        if name not in ceilings or value > ceilings[name]
    }
    return {
        "passed": not overages,
        "baseline_git_sha": baseline["candidate"]["git_sha"],
        "ceilings": {name: ceilings[name] for name in current},
        "measured": current,
        "overages": overages,
    }


def build_import_csv(prefix: str, row_count: int, benchmark: dict[str, Any]) -> str:
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    writer.writerow(("name", "asset_tag", "serial_number", "status", "asset_type"))
    for index in range(row_count):
        writer.writerow(
            (
                f"Perf Import {prefix} {index:04d}",
                f"{prefix}-{index:04d}",
                f"{prefix}-SN-{index:04d}",
                benchmark["status_id"],
                benchmark["asset_type_id"],
            )
        )
    return csv_buffer.getvalue()


def import_csrf_token(session: HttpSession, import_path: str) -> str:
    status, _, import_form, _ = session.request(import_path)
    csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', import_form)
    if status != 200 or not csrf_match:
        raise RuntimeError(f"import form unavailable for CSRF-authenticated HTTP submission (status={status})")
    return csrf_match.group(1)


def confirm_import(session: HttpSession, import_path: str, csrf_token: str) -> int:
    status, final_url, _, _ = session.request(
        import_path,
        method="POST",
        data={"csrfmiddlewaretoken": csrf_token, "_confirm": "1"},
        headers={"Referer": f"{session.base_url}{import_path}", "X-CSRFToken": csrf_token},
    )
    if status != 200:
        raise RuntimeError(f"import confirmation failed (status={status})")
    job_match = re.search(r"/jobs/(\d+)/", urllib.parse.urlparse(final_url).path)
    if not job_match:
        raise RuntimeError(f"import confirmation did not return a job detail URL: {final_url}")
    return int(job_match.group(1))


def wait_for_import(job_id: int, prefix: str, row_count: int, timeout_seconds: float, started: float) -> float:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = Job._base_manager.filter(pk=job_id).first()
        if job is None:
            raise RuntimeError(f"import job {job_id} disappeared before completion")
        if job.status == Job.STATUS_FAILED:
            raise RuntimeError(f"import job {job_id} failed")
        imported_count = Asset._base_manager.filter(asset_tag__startswith=prefix).count()
        if job.status == Job.STATUS_COMPLETED and imported_count == row_count:
            return (time.perf_counter() - started) * 1_000
        time.sleep(0.2)
    raise TimeoutError(f"import job {job_id} did not complete with {row_count} rows before timeout")


def import_batch(
    session: HttpSession, manifest: dict[str, Any], batch_index: int, row_count: int, timeout_seconds: float
) -> float:
    benchmark = manifest["benchmark"]
    scope_url = reverse("assets:asset_list") + f"?switch_tenant={benchmark['import_tenant_id']}"
    status, final_url, _, _ = session.request(scope_url)
    if status != 200 or "/accounts/login/" in final_url:
        raise RuntimeError("could not establish the import tenant's authenticated session")
    prefix = f"PF882-I-{batch_index:03d}-{uuid.uuid4().hex[:6]}"
    import_path = reverse("generic_import", kwargs={"app_label": "assets", "model_name": "asset"})
    csrf_token = import_csrf_token(session, import_path)
    import_text = build_import_csv(prefix, row_count, benchmark)
    started = time.perf_counter()
    status, _, body, _ = session.request(
        import_path,
        method="POST",
        data={
            "csrfmiddlewaretoken": csrf_token,
            "active_tab": "editor",
            "import_format": "csv",
            "delimiter": ",",
            "import_text": import_text,
            "_preview": "1",
        },
        headers={"Referer": f"{session.base_url}{import_path}", "X-CSRFToken": csrf_token},
    )
    if status != 200 or prefix not in body:
        raise RuntimeError(f"import preview failed or lost rows (status={status})")
    job_id = confirm_import(session, import_path, csrf_token)
    return wait_for_import(job_id, prefix, row_count, timeout_seconds, started)


def measure_imports(
    session: HttpSession, manifest: dict[str, Any], repeats: int, row_count: int, timeout_seconds: float
) -> dict[str, Any]:
    samples = [import_batch(session, manifest, index, row_count, timeout_seconds) for index in range(repeats)]
    total_rows = repeats * row_count
    elapsed_seconds = sum(samples) / 1_000
    return {
        "operation": "authenticated HTTP CSV preview + confirmation + qcluster import completion",
        "rows_per_batch": row_count,
        "completed_rows": total_rows,
        "batches": repeats,
        "latency": summarize_samples(samples, "ms"),
        "aggregate_rows_per_second": round(total_rows / elapsed_seconds, 2),
    }


def queue_batch(
    session: HttpSession, username: str, password: str, task_count: int, timeout_seconds: float, batch_index: int
) -> float:
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    payload = json.dumps({"count": task_count}).encode("utf-8")
    started = time.perf_counter()
    status, final_url, body, _ = session.request(
        "/__perf__/enqueue",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    if status != 202:
        raise RuntimeError(f"qcluster enqueue HTTP endpoint returned {status}: {body[:300]}")
    task_ids = json.loads(body)["task_ids"]
    if len(task_ids) != task_count:
        raise RuntimeError(f"qcluster accepted {len(task_ids)} of {task_count} requested tasks")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        query = Task._base_manager.filter(pk__in=task_ids)
        succeeded = query.filter(success=True).count()
        failed = query.filter(success=False).count()
        if failed:
            raise RuntimeError(f"qcluster batch {batch_index} recorded {failed} failed tasks")
        if succeeded == task_count:
            return (time.perf_counter() - started) * 1_000
        time.sleep(0.2)
    raise TimeoutError(f"qcluster batch {batch_index} completed {succeeded}/{task_count} tasks before timeout")


def measure_qcluster(
    session: HttpSession, username: str, password: str, repeats: int, task_count: int, timeout_seconds: float
) -> dict[str, Any]:
    samples = [queue_batch(session, username, password, task_count, timeout_seconds, index) for index in range(repeats)]
    total_tasks = repeats * task_count
    elapsed_seconds = sum(samples) / 1_000
    return {
        "operation": "authenticated HTTP enqueue to harness-only endpoint; wait for persisted qcluster Task successes",
        "worker_count": settings.Q_CLUSTER["workers"],
        "completed_tasks": total_tasks,
        "tasks_per_batch": task_count,
        "batches": repeats,
        "batch_completion_latency": summarize_samples(samples, "ms"),
        "aggregate_tasks_per_second": round(total_tasks / elapsed_seconds, 2),
    }


def main() -> int:
    args = parse_args()
    password = os.environ.get("PERF_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("PERF_ADMIN_PASSWORD must be set; its value is never printed")
    manifest = load_manifest(args.manifest)
    source_sha, source_tree = source_identity(args)
    verify_dataset(manifest, args.username)
    session = HttpSession(args.base_url, args.timeout_seconds)
    session.login(args.username, password)
    benchmark = manifest["benchmark"]
    scope = f"?switch_tenant_group={benchmark['tenant_group_id']}"
    api_path = reverse("api:assets_api:asset-list") + scope
    graph_path = reverse("graphql") + scope
    list_query = "query PerfAssets { assets(limit: 25, offset: 0) { id name assetTag serialNumber } }"
    detail_query = (
        'query PerfAsset { asset(id: "'
        + str(benchmark["asset_id"])
        + '") { id name assetTag serialNumber status { name } location { name } } }'
    )
    http_paths = [
        ("asset_list_first_page", reverse("assets:asset_list") + scope, None),
        ("asset_list_filtered_page", reverse("assets:asset_list") + scope + "&q=" + benchmark["filtered_q"], None),
        ("asset_detail", reverse("assets:asset_detail", args=[benchmark["asset_id"]]) + scope, None),
        (
            "global_search",
            reverse("search")
            + "?q="
            + benchmark["search_term"]
            + f"&switch_tenant_group={benchmark['tenant_group_id']}",
            None,
        ),
        ("rest_asset_list", api_path, None),
        ("graphql_asset_list", graph_path, {"query": list_query}),
        ("graphql_asset_detail", graph_path, {"query": detail_query}),
    ]
    latencies = [
        measure_http_path(session, name, path, args.repeats, args.warmups, payload)
        for name, path, payload in http_paths
    ]
    query_counts = collect_query_counts(manifest, args.username)
    query_ceiling = compare_query_ceilings(query_counts, args.baseline_results)
    imports = measure_imports(session, manifest, args.import_repeats, args.import_rows, args.timeout_seconds)
    qcluster = measure_qcluster(
        session, args.username, password, args.queue_repeats, args.queue_tasks, args.timeout_seconds
    )
    result = {
        "benchmark": "itambox-beta4-target-volume",
        "candidate": {
            "git_sha": source_sha,
            "tree_sha": source_tree,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "database": connection.settings_dict["NAME"],
            "dataset_seed": manifest["seed"],
            "target_deviation": manifest.get("target_deviation"),
            "database_size_bytes_before_benchmark": manifest["database_size_bytes"],
        },
        "runtime": {
            "base_url": args.base_url,
            "http_server": "running application over HTTP; not Django test-client latency",
            "gunicorn_workers": os.environ.get("PERF_WEB_WORKERS", "4"),
            "qcluster_workers": settings.Q_CLUSTER["workers"],
            "qcluster_task_success_retention": settings.Q_CLUSTER["save_limit"],
            "repeats": args.repeats,
            "warmups": args.warmups,
        },
        "dataset_counts": manifest["counts"],
        "http_latency": latencies,
        "query_count_diagnostics_test_client_not_timed": query_counts,
        "query_ceiling": query_ceiling,
        "import_rows_per_batch": args.import_rows,
        "import_1000_rows": imports if args.import_rows == 1_000 else None,
        "import_batch": imports,
        "qcluster_throughput": qcluster,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    output = json.dumps(result, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 1 if query_ceiling.get("passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
