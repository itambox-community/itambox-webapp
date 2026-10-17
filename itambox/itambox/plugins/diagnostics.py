"""Read-only plugin inventory: manifest, load state, pending migrations and orphaned data."""

import importlib

from django.apps import apps
from django.conf import settings
from django.db import Error as DatabaseError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from itambox.plugins import PluginConfig

DATABASE_UNAVAILABLE = "database unavailable"


def plugin_app_labels(plugin_names):
    """App labels owned by the given plugins: the plugin app plus its declared ``django_apps``."""
    names = set(plugin_names)
    for config in apps.get_app_configs():
        if isinstance(config, PluginConfig) and config.name in names:
            names.update(getattr(config, "django_apps", ()))
    return sorted(config.label for config in apps.get_app_configs() if config.name in names)


def pending_migrations(app_labels):
    """Unapplied migrations of the given apps as ``app.name`` strings, or ``None`` when the database is down."""
    labels = set(app_labels)
    if not labels:
        return []
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except DatabaseError:
        return None
    return [
        f"{migration.app_label}.{migration.name}" for migration, _backwards in plan if migration.app_label in labels
    ]


def find_orphans():
    """Tables, content types and migration records that no installed app owns (read-only)."""
    labels = {config.label for config in apps.get_app_configs()}
    try:
        # inline import: app-registry: ContentType needs the populated app registry
        from django.contrib.contenttypes.models import ContentType

        existing = set(connection.introspection.table_names())
        known = set(connection.introspection.django_table_names(only_existing=False, include_views=False))
        known.add(MigrationRecorder.Migration._meta.db_table)
        content_types = ContentType.objects.exclude(app_label__in=labels).order_by("app_label", "model")
        migrations = MigrationRecorder(connection).migration_qs.exclude(app__in=labels).order_by("app", "name")
        return {
            "tables": sorted(existing - known),
            "content_types": [f"{ct.app_label}.{ct.model}" for ct in content_types],
            "migrations": [f"{row.app}.{row.name}" for row in migrations],
        }
    except DatabaseError:
        return {"error": DATABASE_UNAVAILABLE}


def _manifest(plugin_name):
    module = importlib.import_module(plugin_name)
    config = getattr(module, "config", None)
    return {
        "version": str(getattr(config, "version", "") or ""),
        "itambox_range": getattr(config, "itambox_range", None),
    }


def plugin_inventory():
    """One row per configured plugin that resolved: manifest, load state and pending migrations."""
    rows = []
    active = list(getattr(settings, "PLUGINS_ACTIVE", ()))
    labels = {}
    for name in active:
        labels[name] = plugin_app_labels([name])
    for name in active:
        rows.append(
            {
                "plugin": name,
                "state": "active",
                **_manifest(name),
                "pending_migrations": pending_migrations(labels[name]),
            }
        )
    for row in getattr(settings, "PLUGINS_DIAGNOSTICS", ()):
        rows.append(
            {
                "plugin": row["plugin"],
                "state": "skipped",
                **_manifest(row["plugin"]),
                "pending_migrations": [],
            }
        )
    return rows
