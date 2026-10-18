"""Read-only plugin inventory: manifest, load state, pending migrations and orphaned data."""

import importlib

from django.apps import apps
from django.conf import settings
from django.db import Error as DatabaseError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from itambox.plugins import PluginConfig
from itambox.registry import registry

DATABASE_UNAVAILABLE = "database unavailable"


def plugin_app_labels(plugin_names):
    """App labels owned by the given plugins: the plugin app plus its declared ``django_apps``."""
    names = set(plugin_names)
    for config in apps.get_app_configs():
        if isinstance(config, PluginConfig) and config.name in names:
            names.update(getattr(config, "django_apps", ()))
    return sorted(config.label for config in apps.get_app_configs() if config.name in names)


def empty_plugin_resources():
    """Empty resource inventory with every supported extension point present."""
    return {
        "template_extensions": [],
        "menu": [],
        "menu_items": [],
        "viewsets": [],
        "permission_targets": [],
        "graphql_schema": [],
    }


def _qualified_name(resource):
    resource_class = resource if isinstance(resource, type) else type(resource)
    return f"{resource_class.__module__}.{resource_class.__qualname__}"


def plugin_resources(plugin_name):
    """Contributions by one plugin, grouped by the host's declared extension points."""
    resources = empty_plugin_resources()
    for model, content_classes in registry._plugin_template_contents.items():
        owners = registry._plugin_template_content_sources.get(model, ())
        for owner, content_class in zip(owners, content_classes, strict=True):
            if owner == plugin_name:
                resources["template_extensions"].append({"model": model, "class_name": _qualified_name(content_class)})
    for owner, menu in zip(registry._plugin_menu_sources, registry.get_plugin_menus(), strict=True):
        if owner == plugin_name:
            resources["menu"].append(_qualified_name(menu))
    for owner, item in zip(registry._plugin_menu_item_sources, registry.get_plugin_menu_items(), strict=True):
        if owner == plugin_name:
            resources["menu_items"].append(_qualified_name(item))
    for prefix, viewset, basename in registry.get_plugin_viewsets().get(plugin_name, ()):
        resources["viewsets"].append({"prefix": prefix, "viewset": _qualified_name(viewset), "basename": basename})
    resources["permission_targets"] = list(registry.get_plugin_permission_targets().get(plugin_name, ()))
    resources["graphql_schema"] = [
        _qualified_name(query) for owner, query in registry.get_plugin_graphql() if owner == plugin_name
    ]
    return resources


def _skipped_plugin_app_labels(plugin_name):
    """Migration recorder label for a compatible-but-skipped plugin not in Django's app registry."""
    module = importlib.import_module(plugin_name)
    config = getattr(module, "config", None)
    if config is None:
        return []
    app_name = getattr(config, "name", plugin_name)
    label = getattr(config, "label", None) or app_name.rsplit(".", 1)[-1]
    return [label]


def migration_state(app_labels, *, loaded_labels):
    """Applied and pending migration names for labels, using one migration graph."""
    labels = set(app_labels)
    if not labels:
        return {}
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except DatabaseError:
        return {label: None for label in labels}

    states = {label: {"applied_migrations": [], "pending_migrations": []} for label in labels}
    for app_label, name in executor.loader.applied_migrations:
        if app_label in states:
            states[app_label]["applied_migrations"].append(f"{app_label}.{name}")
    for migration, backwards in plan:
        if not backwards and migration.app_label in loaded_labels and migration.app_label in states:
            states[migration.app_label]["pending_migrations"].append(f"{migration.app_label}.{migration.name}")
    for status in states.values():
        for migration_names in status.values():
            migration_names.sort()
    return states


def _migration_summary(app_labels, states):
    labels = set(app_labels)
    if not labels:
        return {"applied_migrations": [], "pending_migrations": []}
    plugin_states = [states.get(label) for label in labels]
    if any(status is None for status in plugin_states):
        return {"applied_migrations": None, "pending_migrations": None}
    return {
        key: sorted(migration for status in plugin_states for migration in status[key])
        for key in ("applied_migrations", "pending_migrations")
    }


def pending_migrations(app_labels):
    """Unapplied migrations of the given apps as ``app.name`` strings, or ``None`` when the database is down."""
    labels = set(app_labels)
    if not labels:
        return []
    states = migration_state(labels, loaded_labels=labels)
    summary = _migration_summary(labels, states)
    return summary["pending_migrations"]


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


def _plugin_row(plugin_name, state, app_labels, migration_states, running_version):
    is_active = state == "active"
    return {
        "plugin": plugin_name,
        "state": state,
        **_manifest(plugin_name),
        "running_version": running_version,
        "resources": plugin_resources(plugin_name) if is_active else empty_plugin_resources(),
        **_migration_summary(app_labels, migration_states),
    }


def plugin_inventory():
    """One row per configured plugin, including public manifest and host-owned contributions."""
    active = list(getattr(settings, "PLUGINS_ACTIVE", ()))
    skipped = [row["plugin"] for row in getattr(settings, "PLUGINS_DIAGNOSTICS", ())]
    if not active and not skipped:
        return []

    labels_by_plugin = {name: plugin_app_labels([name]) for name in active}
    labels_by_plugin.update({name: _skipped_plugin_app_labels(name) for name in skipped})
    loaded_labels = {label for name in active for label in labels_by_plugin[name]}
    all_labels = {label for labels in labels_by_plugin.values() for label in labels}
    migration_states = migration_state(all_labels, loaded_labels=loaded_labels)
    running_version = str(getattr(settings, "VERSION", "") or "")

    return [
        _plugin_row(name, "active", labels_by_plugin[name], migration_states, running_version) for name in active
    ] + [_plugin_row(name, "skipped", labels_by_plugin[name], migration_states, running_version) for name in skipped]
