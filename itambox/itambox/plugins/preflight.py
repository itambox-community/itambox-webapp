"""Plugin pre-flight (``manage.py plugins --check``).

Startup aborts when a configured plugin is broken, and an aborting start never
reaches ``manage.py`` command dispatch. The pre-flight therefore does not run
inside the Django process that would abort: it boots the real startup path
(settings import, resolve, compose, ready, assemble) in a child interpreter,
once per configured plugin and once for the complete set, and reports the
outcome of each. No web process is involved.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

RESULT_MARKER = "ITAMBOX_PREFLIGHT_RESULT="
OK = "ok"
SKIPPED = "skipped"
ABORT = "would abort startup"
HOST = "(plugin set)"

_CHILD = r"""
import json, os, sys

MARKER = %(marker)r


def emit(payload):
    sys.stdout.write("\n" + MARKER + json.dumps(payload) + "\n")
    sys.stdout.flush()


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
try:
    import django

    django.setup()
# broad except: boundary-isolation: any startup failure is reported, never raised
except BaseException as exc:
    plugin = getattr(exc, "plugin_name", None)
    emit(
        {
            "status": "abort",
            "plugin": plugin,
            "phase": getattr(exc, "phase", None),
            "cause": "%%s: %%s" %% (type(getattr(exc, "cause", exc)).__name__, getattr(exc, "cause", exc)),
        }
    )
    sys.exit(0)

from django.conf import settings

from itambox.plugins import diagnostics

active = list(getattr(settings, "PLUGINS_ACTIVE", ()))
emit(
    {
        "status": "ok",
        "active": active,
        "skipped": [
            {"plugin": row["plugin"], "reason": row["error"]} for row in getattr(settings, "PLUGINS_DIAGNOSTICS", ())
        ],
        "version": settings.VERSION,
        "pending_migrations": diagnostics.pending_migrations(diagnostics.plugin_app_labels(active)),
    }
)
"""


def configured_plugins(environ=None, base_dir=None):
    """The ``ITAMBOX_PLUGINS`` list, honouring the same ``.env`` fallback as the settings module."""
    environ = os.environ if environ is None else environ
    raw = environ.get("ITAMBOX_PLUGINS")
    if raw is None:
        base_dir = Path(base_dir or Path(__file__).resolve().parents[2])
        for candidate in (base_dir / ".env", base_dir.parent / ".env"):
            if candidate.exists():
                raw = _dotenv_value(candidate, "ITAMBOX_PLUGINS")
                break
    return [name.strip() for name in (raw or "").split(",") if name.strip()]


def _dotenv_value(path, key):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def run_startup(plugin_names, environ=None):
    """Run the real startup path for the given plugin set in a child interpreter; return its result dict."""
    env = dict(os.environ if environ is None else environ)
    env["ITAMBOX_PLUGINS"] = ",".join(plugin_names)
    base_dir = Path(__file__).resolve().parents[2]
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and argv, no shell
        [sys.executable, "-c", _CHILD % {"marker": RESULT_MARKER}],
        cwd=base_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    return {
        "status": "abort",
        "plugin": None,
        "phase": "startup",
        "cause": (completed.stderr.strip().splitlines() or ["the start produced no result"])[-1],
    }


def _plugin_outcome(name, result):
    if result["status"] == "abort":
        owner = result.get("plugin") or name
        return {
            "plugin": name,
            "outcome": ABORT,
            "phase": result.get("phase"),
            "cause": result["cause"] if owner == name else f"{owner}: {result['cause']}",
        }
    if name in result.get("active", ()):
        return {"plugin": name, "outcome": OK, "pending_migrations": result.get("pending_migrations")}
    skip = next((row for row in result.get("skipped", ()) if row["plugin"] == name), None)
    return {"plugin": name, "outcome": SKIPPED, "cause": skip["reason"] if skip else "version incompatibility"}


def run_preflight(environ=None, runner=None):
    """Return the pre-flight report; ``report["abort"]`` is true when startup would abort."""
    runner = runner or run_startup
    names = configured_plugins(environ)
    plugins = [_plugin_outcome(name, runner([name])) for name in names]
    combined = runner(names) if names else {"status": "ok", "active": [], "skipped": []}
    abort = any(row["outcome"] == ABORT for row in plugins)
    host = None
    if combined["status"] == "abort":
        abort = True
        owner = combined.get("plugin")
        # A failure the single-plugin runs did not show comes from the combination (or the host).
        if not any(row["outcome"] == ABORT and row["plugin"] == owner for row in plugins):
            host = {
                "plugin": owner or HOST,
                "outcome": ABORT,
                "phase": combined.get("phase"),
                "cause": combined["cause"],
            }
    pending = []
    for row in plugins:
        pending.extend(row.get("pending_migrations") or ())
    pending.extend(combined.get("pending_migrations") or ())
    return {
        "abort": abort,
        "plugins": plugins,
        "set": host,
        "pending_migrations": sorted(set(pending)),
        "version": combined.get("version"),
    }


def format_report(report, output_format="table"):
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=True)
    lines = ["PLUGIN  OUTCOME  DETAILS", "------  -------  -------"]
    for row in report["plugins"]:
        detail = row.get("cause") or ""
        if row.get("phase"):
            detail = f"{row['phase']}: {detail}"
        lines.append(f"{row['plugin']}  {row['outcome']}  {detail}".rstrip())
    if report["set"]:
        row = report["set"]
        lines.append(f"{row['plugin']}  {row['outcome']}  {row['phase']}: {row['cause']}")
    if not report["plugins"]:
        lines.append("(none configured)")
    pending = report["pending_migrations"]
    lines.append("Pending plugin migrations: " + (", ".join(pending) if pending else "none"))
    lines.append("Startup would ABORT." if report["abort"] else "Startup would succeed.")
    return "\n".join(lines)


def main(argv, stdout=None):
    """Entry point used by ``manage.py`` before Django loads; returns the process exit code."""
    stdout = stdout or sys.stdout
    output_format = "json" if "json" in argv else "table"
    report = run_preflight()
    stdout.write(format_report(report, output_format) + "\n")
    return 1 if report["abort"] else 0
