"""WSGI wrapper with a password-authenticated qcluster probe for the perf harness.

This endpoint is enabled only when Gunicorn is explicitly launched with this
module. Bind it to loopback on a disposable qualification stack.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Callable

from django.contrib.auth import authenticate
from django.core.wsgi import get_wsgi_application
from django_q.tasks import async_task

_django_application = get_wsgi_application()
_MAX_TASKS_PER_REQUEST = 10_000


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


def _authenticated_superuser(environ: dict):
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return None
    try:
        username, password = base64.b64decode(header[6:], validate=True).decode("utf-8").split(":", 1)
    except ValueError:
        return None
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

    batch_id = uuid.uuid4().hex[:12]
    task_ids = [
        async_task("operator.add", index, 1, task_name=f"perf-882-{batch_id}-{index:05d}") for index in range(count)
    ]
    return _respond(start_response, "202 Accepted", {"batch_id": batch_id, "task_ids": task_ids})


def application(environ: dict, start_response: Callable):
    if environ.get("PATH_INFO") == "/__perf__/enqueue":
        return _enqueue(environ, start_response)
    return _django_application(environ, start_response)
