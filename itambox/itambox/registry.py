# This file is adapted from NetBox (https://github.com/netbox-community/netbox).
# Copyright (c) DigitalOcean, LLC.
# Licensed under the Apache License, Version 2.0.

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Protocol

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Model, QuerySet
    from django.http import HttpRequest, QueryDict


@dataclass(frozen=True, slots=True)
class ListParamsInput:
    request: HttpRequest
    model: type[Model]
    params: QueryDict
    content_type: ContentType
    partial: bool


@dataclass(frozen=True, slots=True)
class ListParamsResult:
    params: QueryDict
    state: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ListFilterInput:
    request: HttpRequest
    model: type[Model]
    params: QueryDict
    queryset: QuerySet[Model]
    content_type: ContentType
    partial: bool
    state: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ListContextInput:
    request: HttpRequest
    model: type[Model]
    params: QueryDict
    content_type: ContentType
    partial: bool
    state: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DetailContextInput:
    request: HttpRequest
    obj: Model
    content_type: ContentType
    active_features: frozenset[str]


class GenericPresentationProvider(Protocol):
    def resolve_list_params(self, input: ListParamsInput) -> ListParamsResult:
        pass

    def filter_list_queryset(self, input: ListFilterInput) -> QuerySet[Model]:
        pass

    def build_list_context(self, input: ListContextInput) -> Mapping[str, object]:
        pass

    def build_detail_context(self, input: DetailContextInput) -> Mapping[str, object]:
        pass


@dataclass(frozen=True, slots=True)
class GenericPresentationRegistration:
    name: str
    provider: GenericPresentationProvider
    detail_features: frozenset[str]
    list_params: bool
    list_filter: bool
    list_context: bool
    priority: int


GENERIC_PRESENTATION_DETAIL_FEATURES = frozenset(
    {
        "bookmarkable",
        "custom_field_data",
        "file_attachments",
        "image_attachments",
        "job_file_attachments",
        "journaling",
        "subscribable",
        "watchable",
    }
)

_GENERIC_PRESENTATION_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.-]*")


def validate_list_filter_result(
    provider_name: str,
    input_queryset: QuerySet[Model],
    result: object,
) -> QuerySet[Model]:
    """Validate the runtime properties that generic orchestration can prove.

    Matching model and database identities cannot prove clone lineage. Concrete
    providers must still derive from the supplied queryset and carry scope
    canaries for exclusions that a fresh same-model manager could reintroduce.
    """
    if not isinstance(result, type(input_queryset)):
        raise ImproperlyConfigured(
            f"Generic presentation provider {provider_name!r} must return a QuerySet derived from its input"
        )
    if result.model is not input_queryset.model:
        raise ImproperlyConfigured(
            f"Generic presentation provider {provider_name!r} returned a QuerySet for a different model"
        )
    if result.db != input_queryset.db:
        raise ImproperlyConfigured(
            f"Generic presentation provider {provider_name!r} returned a QuerySet for a different database"
        )
    return result


class Registry:
    """
    In-memory registry that centralizes metadata about models and features.

    Follows NetBox's extras.registry pattern — a single source of truth
    for which models support which features, search indexes, filter sets,
    table classes, event rules, webhooks, and export templates.
    """

    def __init__(self):
        self._model_features = defaultdict(set)
        self._custom_field_data_validators = {}
        self._custom_field_data_definition_providers = {}
        self._search_indexes = defaultdict(list)
        self._filter_sets = {}
        self._table_classes = {}
        self._event_rules = []
        self._webhooks = []
        self._export_templates = defaultdict(list)
        # Plugin registries
        self._plugin_template_contents = defaultdict(list)
        self._plugin_template_content_sources = defaultdict(list)
        self._plugin_menus = []
        self._plugin_menu_sources = []
        self._plugin_menu_items = []
        self._plugin_menu_item_sources = []
        self._plugin_viewsets = defaultdict(list)
        self._plugin_permission_targets = defaultdict(list)
        self._plugin_graphql = []
        self._plugins_frozen = False
        self._generic_presentation_lock = RLock()
        self._generic_presentation_registrations = {}
        self._generic_presentation_feature_owners = {}
        self._generic_presentation_priorities = {}
        self._generic_presentation_provider_names = {}
        self._generic_presentation_ordered = ()

    @property
    def model_features(self):
        return dict(self._model_features)

    @property
    def search_indexes(self):
        return dict(self._search_indexes)

    @property
    def filter_sets(self):
        return self._filter_sets

    @property
    def table_classes(self):
        return self._table_classes

    @property
    def event_rules(self):
        return list(self._event_rules)

    @property
    def webhooks(self):
        return list(self._webhooks)

    @property
    def export_templates(self):
        return dict(self._export_templates)

    def register_feature(self, model, feature_name):
        """Register that a model supports a named feature (e.g., 'bookmarkable', 'taggable')."""
        self._model_features[model].add(feature_name)

    def unregister_feature(self, model, feature_name):
        self._model_features[model].discard(feature_name)

    def model_has_feature(self, model, feature_name):
        return feature_name in self._model_features.get(model, set())

    def get_models_with_feature(self, feature_name):
        return [m for m, features in self._model_features.items() if feature_name in features]

    def register_custom_field_data_validator(self, model, validator):
        existing = self._custom_field_data_validators.get(model)
        if existing is not None and existing is not validator:
            raise RuntimeError(f"A custom-field data validator is already registered for {model}.")
        self._custom_field_data_validators[model] = validator

    def get_custom_field_data_validator(self, model):
        return self._custom_field_data_validators.get(model)

    def register_custom_field_data_definition_provider(self, model, provider):
        existing = self._custom_field_data_definition_providers.get(model)
        if existing is not None and existing is not provider:
            raise RuntimeError(f"A custom-field data definition provider is already registered for {model}.")
        self._custom_field_data_definition_providers[model] = provider

    def get_custom_field_data_definition_provider(self, model):
        return self._custom_field_data_definition_providers.get(model)

    def register_search_index(self, model, index_instance):
        self._search_indexes[model].append(index_instance)

    def register_filter_set(self, model, filter_set_class):
        self._filter_sets[model] = filter_set_class

    def get_filter_set(self, model):
        return self._filter_sets.get(model)

    def register_table_class(self, model, table_class):
        self._table_classes[model] = table_class

    def get_table_class(self, model):
        return self._table_classes.get(model)

    def register_event_rule(self, rule):
        self._event_rules.append(rule)

    def get_event_rules(self, model=None, action=None):
        rules = self._event_rules
        if model is not None:
            rules = [r for r in rules if r["model"] == model]
        if action is not None:
            rules = [r for r in rules if action in r.get("events", [])]
        return rules

    def register_webhook(self, webhook_config):
        self._webhooks.append(webhook_config)

    def get_webhooks(self):
        return list(self._webhooks)

    def register_export_template(self, model, template):
        self._export_templates[model].append(template)

    def get_export_templates(self, model):
        return self._export_templates.get(model, [])

    def register_generic_presentation(
        self,
        name: str,
        provider: GenericPresentationProvider,
        *,
        detail_features: tuple[str, ...],
        list_params: bool,
        list_filter: bool,
        list_context: bool,
        priority: int,
    ) -> None:
        """Atomically register one deterministic generic-presentation provider."""
        with self._generic_presentation_lock:
            self._validate_generic_presentation_shape(
                name,
                detail_features,
                list_params,
                list_filter,
                list_context,
                priority,
            )
            normalized_features = self._normalize_generic_presentation_features(name, detail_features)
            self._validate_generic_presentation_methods(
                name,
                provider,
                normalized_features,
                list_params,
                list_filter,
                list_context,
            )
            registration = GenericPresentationRegistration(
                name=name,
                provider=provider,
                detail_features=frozenset(normalized_features),
                list_params=list_params,
                list_filter=list_filter,
                list_context=list_context,
                priority=priority,
            )
            if self._generic_presentation_is_idempotent(registration):
                return
            self._validate_generic_presentation_conflicts(registration)
            self._commit_generic_presentation(registration)

    @staticmethod
    def _validate_generic_presentation_shape(
        name,
        detail_features,
        list_params,
        list_filter,
        list_context,
        priority,
    ):
        if not isinstance(name, str) or _GENERIC_PRESENTATION_NAME_PATTERN.fullmatch(name) is None:
            raise ImproperlyConfigured("Generic presentation registration name must already match [a-z][a-z0-9_.-]*")
        for flag_name, flag_value in (
            ("list_params", list_params),
            ("list_filter", list_filter),
            ("list_context", list_context),
        ):
            if not isinstance(flag_value, bool):
                raise ImproperlyConfigured(
                    f"Generic presentation registration {name!r} requires {flag_name} to be a boolean"
                )
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ImproperlyConfigured(
                f"Generic presentation registration {name!r} requires priority to be a non-boolean integer"
            )
        if not isinstance(detail_features, tuple):
            raise ImproperlyConfigured(
                f"Generic presentation registration {name!r} requires detail_features to be a tuple"
            )

    @staticmethod
    def _normalize_generic_presentation_features(name, detail_features):
        normalized_features = set()
        for feature in detail_features:
            if not isinstance(feature, str) or not feature:
                raise ImproperlyConfigured(
                    f"Generic presentation registration {name!r} requires non-empty string detail features"
                )
            if feature not in GENERIC_PRESENTATION_DETAIL_FEATURES:
                raise ImproperlyConfigured(
                    f"Generic presentation registration {name!r} declares unknown detail feature {feature!r}"
                )
            if feature in normalized_features:
                raise ImproperlyConfigured(
                    f"Generic presentation registration {name!r} declares duplicate detail feature {feature!r}"
                )
            normalized_features.add(feature)
        return normalized_features

    @staticmethod
    def _validate_generic_presentation_methods(
        name,
        provider,
        normalized_features,
        list_params,
        list_filter,
        list_context,
    ):
        if not normalized_features and not (list_params or list_filter or list_context):
            raise ImproperlyConfigured(f"Generic presentation registration {name!r} is inert")
        required_methods = []
        if normalized_features:
            required_methods.append("build_detail_context")
        if list_params:
            required_methods.append("resolve_list_params")
        if list_filter:
            required_methods.append("filter_list_queryset")
        if list_context:
            required_methods.append("build_list_context")
        for method_name in required_methods:
            if not callable(getattr(provider, method_name, None)):
                raise ImproperlyConfigured(
                    f"Generic presentation registration {name!r} requires callable {method_name}()"
                )

    def _generic_presentation_is_idempotent(self, registration):
        existing = self._generic_presentation_registrations.get(registration.name)
        if existing is None:
            return False
        if existing.provider is not registration.provider:
            raise ImproperlyConfigured(
                f"Generic presentation registration {registration.name!r} already uses a different object"
            )
        if self._generic_presentation_metadata(existing) != self._generic_presentation_metadata(registration):
            raise ImproperlyConfigured(
                f"Generic presentation registration {registration.name!r} changed metadata for the same provider object"
            )
        return True

    def _validate_generic_presentation_conflicts(self, registration):
        provider_identity = id(registration.provider)
        existing_provider_name = self._generic_presentation_provider_names.get(provider_identity)
        if existing_provider_name is not None:
            raise ImproperlyConfigured(
                f"Generic presentation provider object registered as {existing_provider_name!r} cannot be renamed "
                f"to {registration.name!r}"
            )
        existing_priority_name = self._generic_presentation_priorities.get(registration.priority)
        if existing_priority_name is not None:
            raise ImproperlyConfigured(
                f"Generic presentation registrations {existing_priority_name!r} and {registration.name!r} conflict on "
                f"priority {registration.priority}"
            )
        for feature in registration.detail_features:
            existing_feature_name = self._generic_presentation_feature_owners.get(feature)
            if existing_feature_name is not None:
                raise ImproperlyConfigured(
                    f"Generic presentation registrations {existing_feature_name!r} and {registration.name!r} conflict "
                    f"on detail feature {feature!r}"
                )

    def _commit_generic_presentation(self, registration):
        registrations = dict(self._generic_presentation_registrations)
        feature_owners = dict(self._generic_presentation_feature_owners)
        priorities = dict(self._generic_presentation_priorities)
        provider_names = dict(self._generic_presentation_provider_names)
        registrations[registration.name] = registration
        feature_owners.update({feature: registration.name for feature in registration.detail_features})
        priorities[registration.priority] = registration.name
        provider_names[id(registration.provider)] = registration.name

        self._generic_presentation_registrations = registrations
        self._generic_presentation_feature_owners = feature_owners
        self._generic_presentation_priorities = priorities
        self._generic_presentation_provider_names = provider_names
        self._generic_presentation_ordered = tuple(sorted(registrations.values(), key=lambda item: item.priority))

    @staticmethod
    def _generic_presentation_metadata(registration):
        return (
            registration.detail_features,
            registration.list_params,
            registration.list_filter,
            registration.list_context,
            registration.priority,
        )

    def generic_presentation_registrations(self) -> tuple[GenericPresentationRegistration, ...]:
        """Return an immutable priority-ordered startup snapshot."""
        with self._generic_presentation_lock:
            return self._generic_presentation_ordered

    def generic_presentation_owner_for(self, feature: str) -> str:
        """Return the sole owner of an active closed-set detail feature."""
        with self._generic_presentation_lock:
            owner = self._generic_presentation_feature_owners.get(feature)
        if owner is None:
            raise ImproperlyConfigured(f"Generic presentation detail feature {feature!r} has no owner")
        return owner

    @contextmanager
    def isolated_generic_presentation_for_tests(self):
        """Temporarily isolate only generic-presentation state for tests."""
        with self._generic_presentation_lock:
            snapshot = (
                dict(self._generic_presentation_registrations),
                dict(self._generic_presentation_feature_owners),
                dict(self._generic_presentation_priorities),
                dict(self._generic_presentation_provider_names),
                self._generic_presentation_ordered,
            )
            self._clear_generic_presentation_locked()
        try:
            yield
        finally:
            with self._generic_presentation_lock:
                (
                    self._generic_presentation_registrations,
                    self._generic_presentation_feature_owners,
                    self._generic_presentation_priorities,
                    self._generic_presentation_provider_names,
                    self._generic_presentation_ordered,
                ) = snapshot

    def _clear_generic_presentation_locked(self):
        self._generic_presentation_registrations = {}
        self._generic_presentation_feature_owners = {}
        self._generic_presentation_priorities = {}
        self._generic_presentation_provider_names = {}
        self._generic_presentation_ordered = ()

    # Plugin registrations are host-owned: plugins declare resources on their
    # PluginConfig and the host registers them during the compose phase. These
    # methods are private to that phase; they are not plugin API.

    def _guard_plugin_registration(self):
        if self._plugins_frozen:
            raise RuntimeError("plugin registrations are frozen after assembly")

    def _register_plugin_template_content(self, plugin_name, model, content_class):
        self._guard_plugin_registration()
        self._plugin_template_contents[model].append(content_class)
        self._plugin_template_content_sources[model].append(plugin_name)

    def get_plugin_template_contents(self, model):
        return self._plugin_template_contents.get(model, [])

    def _register_plugin_menu(self, plugin_name, menu_cls):
        self._guard_plugin_registration()
        self._plugin_menus.append(menu_cls)
        self._plugin_menu_sources.append(plugin_name)

    def get_plugin_menus(self):
        return self._plugin_menus

    def _register_plugin_menu_item(self, plugin_name, item_cls):
        self._guard_plugin_registration()
        self._plugin_menu_items.append(item_cls)
        self._plugin_menu_item_sources.append(plugin_name)

    def get_plugin_menu_items(self):
        return self._plugin_menu_items

    def _register_plugin_viewset(self, plugin_name, prefix, viewset, basename=None):
        self._guard_plugin_registration()
        if any(existing_prefix == prefix for existing_prefix, _, _ in self._plugin_viewsets[plugin_name]):
            raise ValueError(f"plugin {plugin_name!r} registers the viewset prefix {prefix!r} twice")
        self._plugin_viewsets[plugin_name].append((prefix, viewset, basename))

    def get_plugin_viewsets(self):
        return self._plugin_viewsets

    def _register_plugin_permission_target(self, plugin_name, model_label):
        self._guard_plugin_registration()
        if any(model_label in labels for labels in self._plugin_permission_targets.values()):
            raise ValueError(f"permission target {model_label!r} is already registered")
        self._plugin_permission_targets[plugin_name].append(model_label)

    def get_plugin_permission_targets(self):
        return {name: tuple(labels) for name, labels in self._plugin_permission_targets.items()}

    def _register_plugin_graphql(self, plugin_name, query_type):
        self._guard_plugin_registration()
        self._plugin_graphql.append((plugin_name, query_type))

    def get_plugin_graphql(self):
        return tuple(self._plugin_graphql)

    def _freeze_plugins(self):
        """Make plugin registrations read-only once assembly has completed."""
        self._plugins_frozen = True

    def clear(self):
        """Reset all registrations. Use only in tests."""
        with self._generic_presentation_lock:
            self._clear_generic_presentation_locked()
        self._model_features.clear()
        self._search_indexes.clear()
        self._filter_sets.clear()
        self._table_classes.clear()
        self._event_rules.clear()
        self._webhooks.clear()
        self._export_templates.clear()
        self._plugin_template_contents.clear()
        self._plugin_template_content_sources.clear()
        self._plugin_menus.clear()
        self._plugin_menu_sources.clear()
        self._plugin_menu_items.clear()
        self._plugin_menu_item_sources.clear()
        self._plugin_viewsets.clear()
        self._plugin_permission_targets.clear()
        self._plugin_graphql.clear()
        self._plugins_frozen = False


registry = Registry()
