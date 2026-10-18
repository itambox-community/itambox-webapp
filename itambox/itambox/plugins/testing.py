"""Test helper for plugin authors: boot ITAMbox with your plugins and verify the result.

``verify_plugins`` is the single supported entry point. It starts a fresh
interpreter with the given plugins configured, drives the real startup phases
(resolve, compose, ready, assemble) and then checks the outcome. A failure
raises :class:`PluginVerificationError`, an ``AssertionError`` whose message
names the plugin and the phase.

The helper never imports Django in the calling process: plugin loading happens
at settings time, so the plugin set of a running test process cannot be
changed. Booting a child interpreter exercises exactly the path an operator's
deployment takes, and no database connection is opened.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

RESULT_MARKER = "ITAMBOX_PLUGIN_TEST_RESULT="
DEFAULT_TIMEOUT = 180
_HOST = "(plugin set)"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_CHILD = r"""
import importlib.util, json, os, sys

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
    emit(
        {
            "status": "abort",
            "plugin": getattr(exc, "plugin_name", None),
            "phase": getattr(exc, "phase", None) or "startup",
            "message": str(exc) if hasattr(exc, "plugin_name") else "%%s: %%s" %% (type(exc).__name__, exc),
        }
    )
    sys.exit(0)

from django.apps import apps
from django.conf import settings
from django.urls import Resolver404, get_resolver, resolve

from itambox.plugins import assembly
from itambox.registry import registry

requested = json.loads(os.environ["ITAMBOX_PLUGIN_TEST_REQUESTED"])
active = list(settings.PLUGINS_ACTIVE)
skipped = {row["plugin"]: row["error"] for row in getattr(settings, "PLUGINS_DIAGNOSTICS", ())}
problems = []


def problem(plugin, phase, message):
    problems.append({"plugin": plugin, "phase": phase, "message": message})


for name in requested:
    if name not in active:
        problem(name, "resolve", "plugin was skipped: %%s" %% skipped.get(name, "it is not active"))

sources = {
    "template_extensions": set(
        n for names in registry._plugin_template_content_sources.values() for n in names
    ),
    "menu": set(registry._plugin_menu_sources),
    "menu_items": set(registry._plugin_menu_item_sources),
    "viewsets": set(registry.get_plugin_viewsets()),
    "permission_targets": set(registry.get_plugin_permission_targets()),
    "graphql_schema": set(n for n, _ in registry.get_plugin_graphql()),
}
for kind, owners in sources.items():
    for owner in sorted(owners - set(active)):
        problem(owner, "compose", "%%s registration is attributed to a plugin that is not active" %% kind)

namespaces = get_resolver().namespace_dict.get("plugins")
ui_namespaces = namespaces[1].namespace_dict if namespaces else {}
plugins = {}
for name in active:
    config = apps.get_app_config(name)
    record = {
        "version": config.version,
        "itambox_range": config.itambox_range,
        "registered": sorted(kind for kind, owners in sources.items() if name in owners),
        "ui": False,
        "api_prefixes": [prefix for prefix, _, _ in registry.get_plugin_viewsets().get(name, ())],
    }
    plugins[name] = record
    if not config.description:
        problem(name, "resolve", "the manifest has no description")
    if not config.itambox_range:
        problem(name, "resolve", "the manifest has no itambox_range")
    for attr in assembly._DEFAULTS:
        try:
            declared = assembly._resolve(config, attr)
        # broad except: boundary-isolation: plugin code is reported with its name
        except Exception as exc:
            problem(name, "compose", "resource %%s does not load: %%s: %%s" %% (attr, type(exc).__name__, exc))
            continue
        if declared in (None, [], ()):
            continue
        if name not in sources[attr]:
            problem(name, "compose", "declared resource %%s has no registration attributed to the plugin" %% attr)
    for prefix in record["api_prefixes"]:
        path = "/api/plugins/%%s/%%s/" %% (name, prefix)
        try:
            resolve(path)
        except Resolver404:
            problem(name, "assemble", "REST route %%s does not resolve" %% path)
    try:
        has_urls = importlib.util.find_spec(name + ".urls") is not None
    except ModuleNotFoundError:
        has_urls = False
    if has_urls:
        record["ui"] = name in ui_namespaces
        if not record["ui"]:
            problem(name, "assemble", "the urls.py URLconf is not mounted under /plugins/")

try:
    from core.schema import build_schema

    build_schema(extensions=[])
# broad except: boundary-isolation: schema failures are reported, never raised
except Exception as exc:
    owner = next(iter(sources["graphql_schema"]), None) or %(host)r
    problem(owner, "assemble", "GraphQL schema build failed: %%s: %%s" %% (type(exc).__name__, exc))

emit({"status": "ok", "active": active, "skipped": skipped, "plugins": plugins, "problems": problems})
"""


class PluginVerificationError(AssertionError):
    """A plugin did not start or failed a check; names the plugin and the phase."""

    def __init__(self, plugin, phase, detail):
        self.plugin = plugin
        self.phase = phase
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class PluginReport:
    """The verified outcome of a successful ``verify_plugins`` call."""

    active: tuple = ()
    skipped: dict = field(default_factory=dict)
    plugins: dict = field(default_factory=dict)


def _environment(plugins, pythonpath, environ):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ITAMBOX_PLUGINS")}
    env.update(environ or {})
    env.update(
        ITAMBOX_ENV=env.get("ITAMBOX_ENV", "dev"),
        ITAMBOX_PLUGINS=",".join(plugins),
        ITAMBOX_PLUGIN_TEST_REQUESTED=json.dumps(list(plugins)),
        PYTHONIOENCODING="utf-8",
    )
    paths = [str(path) for path in pythonpath] + [str(_PROJECT_ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _run_child(plugins, pythonpath, environ, timeout):
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and argv, no shell
        [sys.executable, "-c", _CHILD % {"marker": RESULT_MARKER, "host": _HOST}],
        cwd=_PROJECT_ROOT,
        env=_environment(plugins, pythonpath, environ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    last = (completed.stderr.strip().splitlines() or ["the start produced no result"])[-1]
    return {"status": "abort", "plugin": None, "phase": "startup", "message": last}


def _raise_for(result):
    if result["status"] == "abort":
        plugin = result.get("plugin") or _HOST
        raise PluginVerificationError(plugin, result.get("phase") or "startup", result["message"])
    problems = result.get("problems") or []
    if problems:
        lines = ['Plugin "%(plugin)s" failed during %(phase)s: %(message)s' % row for row in problems]
        first = problems[0]
        raise PluginVerificationError(first["plugin"], first["phase"], "\n".join(lines))


def verify_plugins(*plugins, pythonpath=(), environ=None, timeout=DEFAULT_TIMEOUT):
    """Start ITAMbox with ``plugins`` configured and verify every one of them.

    ``plugins`` are importable package names, exactly as an operator lists them in
    ``ITAMBOX_PLUGINS``. ``pythonpath`` adds the directories that contain them;
    ``environ`` adds environment variables for the start, for example operator
    configuration overrides. Run it from a plain Django ``SimpleTestCase``.

    The check passes only when startup completes, no requested plugin was
    skipped, the manifest is complete, every declared resource loads and is
    registered under the plugin that declared it, UI and REST routes resolve and
    the composed GraphQL schema builds. Anything else raises
    :class:`PluginVerificationError` naming the plugin and the phase. On success
    the returned :class:`PluginReport` describes what was registered.
    """
    if not plugins:
        raise ValueError("verify_plugins needs at least one plugin package name")
    result = _run_child(plugins, pythonpath, environ, timeout)
    _raise_for(result)
    return PluginReport(
        active=tuple(result["active"]),
        skipped=dict(result["skipped"]),
        plugins=dict(result["plugins"]),
    )
