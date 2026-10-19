"""WSGI wrapper with a password-authenticated qcluster probe for the perf harness.

This endpoint is enabled only when Gunicorn is explicitly launched with this
module. Bind it to loopback on a disposable qualification stack.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Callable


@lru_cache(maxsize=1)
def _get_django_application():
    # inline import: optional-dependency: the loopback denial probe must work without Django installed.
    from django.core.wsgi import get_wsgi_application

    return get_wsgi_application()


_MAX_TASKS_PER_REQUEST = 10_000
_SLOW_QUERY_THRESHOLD_MS = 50.0
_ACTIVE_DIAGNOSTICS: ContextVar[dict[str, Any] | None] = ContextVar("perf_request_diagnostics", default=None)
_WORKER_PID = os.getpid()
_WORKER_INSTANCE_ID = uuid.uuid4().hex
_WORKER_IDENTITY_LOCK = threading.Lock()


class _QueryCollector:
    """Collect sanitized query timings for one benchmark request."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = diagnostics

    def __call__(self, execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            duration_ms = (time.perf_counter() - started) * 1_000
            self.diagnostics["query_count"] += 1
            self.diagnostics["query_time_ms"] += duration_ms
            if duration_ms >= _SLOW_QUERY_THRESHOLD_MS:
                self.diagnostics["slow_query_count"] += 1
                if len(self.diagnostics["slow_queries"]) < 50:
                    self.diagnostics["slow_queries"].append(
                        {
                            "fingerprint": hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16],
                            "duration_ms": round(duration_ms, 3),
                        }
                    )


def _record_gc_pause(phase: str, _info: dict[str, Any]) -> None:
    diagnostics = _ACTIVE_DIAGNOSTICS.get()
    if diagnostics is None:
        return
    if phase == "start":
        diagnostics["gc_started"].append(time.perf_counter())
    elif phase == "stop" and diagnostics["gc_started"]:
        duration_ms = (time.perf_counter() - diagnostics["gc_started"].pop()) * 1_000
        diagnostics["gc_pause_count"] += 1
        diagnostics["gc_pause_time_ms"] += duration_ms
        diagnostics["gc_max_pause_ms"] = max(diagnostics["gc_max_pause_ms"], duration_ms)


if _record_gc_pause not in gc.callbacks:
    gc.callbacks.append(_record_gc_pause)


def _worker_identity() -> tuple[int, str]:
    global _WORKER_PID, _WORKER_INSTANCE_ID
    pid = os.getpid()
    if pid != _WORKER_PID:
        with _WORKER_IDENTITY_LOCK:
            if pid != _WORKER_PID:
                _WORKER_PID = pid
                _WORKER_INSTANCE_ID = uuid.uuid4().hex
    return _WORKER_PID, _WORKER_INSTANCE_ID


def _diagnostic_headers(request_id: str, diagnostics: dict[str, Any]) -> list[tuple[str, str]]:
    pid, instance_id = _worker_identity()
    slow_queries = sorted(diagnostics["slow_queries"], key=lambda item: item["duration_ms"], reverse=True)[:5]
    encoded_slow_queries = base64.urlsafe_b64encode(
        json.dumps(slow_queries, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return [
        ("X-Perf-Request-ID", request_id),
        ("X-Perf-Worker-PID", str(pid)),
        ("X-Perf-Worker-Instance", instance_id),
        ("X-Perf-Query-Count", str(diagnostics["query_count"])),
        ("X-Perf-Query-Time-MS", f"{diagnostics['query_time_ms']:.3f}"),
        ("X-Perf-Slow-Query-Count", str(diagnostics["slow_query_count"])),
        ("X-Perf-Slow-Queries-B64", encoded_slow_queries),
        ("X-Perf-GC-Pause-Count", str(diagnostics["gc_pause_count"])),
        ("X-Perf-GC-Pause-Time-MS", f"{diagnostics['gc_pause_time_ms']:.3f}"),
        ("X-Perf-GC-Max-Pause-MS", f"{diagnostics['gc_max_pause_ms']:.3f}"),
    ]


def _profile_request(application: Callable, environ: dict, start_response: Callable, connection: Any):
    request_id = str(environ.get("HTTP_X_PERF_REQUEST_ID", ""))
    if re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
        return application(environ, start_response)

    diagnostics: dict[str, Any] = {
        "query_count": 0,
        "query_time_ms": 0.0,
        "slow_query_count": 0,
        "slow_queries": [],
        "gc_started": [],
        "gc_pause_count": 0,
        "gc_pause_time_ms": 0.0,
        "gc_max_pause_ms": 0.0,
    }
    response: dict[str, Any] = {}
    write_chunks: list[bytes] = []

    def capture_start_response(status, headers, exc_info=None):
        response.update(status=status, headers=list(headers), exc_info=exc_info)
        return write_chunks.append

    token = _ACTIVE_DIAGNOSTICS.set(diagnostics)
    try:
        with connection.execute_wrapper(_QueryCollector(diagnostics)):
            body = application(environ, capture_start_response)
    finally:
        _ACTIVE_DIAGNOSTICS.reset(token)
    if "status" not in response:
        raise RuntimeError("WSGI application returned without start_response")
    headers = [(name, value) for name, value in response["headers"] if not name.lower().startswith("x-perf-")]
    headers.extend(_diagnostic_headers(request_id, diagnostics))
    write = start_response(response["status"], headers, response["exc_info"])
    for chunk in write_chunks:
        write(chunk)
    return body


def _respond(start_response: Callable, status: str, payload: dict) -> list[bytes]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def _is_loopback_client(remote_addr: str) -> bool:
    try:
        return ipaddress.ip_address(remote_addr).is_loopback
    except ValueError:
        return False


def _authenticated_superuser(environ: dict):
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return None
    try:
        username, password = base64.b64decode(header[6:], validate=True).decode("utf-8").split(":", 1)
    except ValueError:
        return None
    _get_django_application()
    # inline import: app-registry: import only after get_wsgi_application initializes Django.
    from django.contrib.auth import authenticate

    user = authenticate(None, username=username, password=password)
    return user if user and user.is_active and user.is_superuser else None


def _enqueue(environ: dict, start_response: Callable) -> list[bytes]:
    if environ.get("REQUEST_METHOD") != "POST":
        return _respond(start_response, "405 Method Not Allowed", {"error": "POST required"})
    if _authenticated_superuser(environ) is None:
        return _respond(start_response, "401 Unauthorized", {"error": "superuser authentication required"})
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
        if size < 1 or size > 1_024:
            raise ValueError("request body size out of bounds")
        payload = json.loads(environ["wsgi.input"].read(size))
        count = int(payload["count"])
        if not 1 <= count <= _MAX_TASKS_PER_REQUEST:
            raise ValueError("task count out of bounds")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _respond(start_response, "400 Bad Request", {"error": "invalid task batch"})

    # inline import: app-registry: import only after get_wsgi_application initializes Django.
    from django_q.tasks import async_task

    batch_id = uuid.uuid4().hex[:12]
    task_ids = [
        async_task("operator.add", index, 1, task_name=f"perf-882-{batch_id}-{index:05d}") for index in range(count)
    ]
    return _respond(start_response, "202 Accepted", {"batch_id": batch_id, "task_ids": task_ids})


def application(environ: dict, start_response: Callable):
    if not _is_loopback_client(str(environ.get("REMOTE_ADDR", ""))):
        return _respond(start_response, "403 Forbidden", {"error": "loopback access required"})
    if environ.get("PATH_INFO") == "/__perf__/enqueue":
        return _enqueue(environ, start_response)
    django_application = _get_django_application()
    # inline import: app-registry: the WSGI application initializes Django before its DB connection is used.
    from django.db import connection

    return _profile_request(django_application, environ, start_response, connection)
