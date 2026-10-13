"""DB-free contract and batching checks for the specification graph loader."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from assets.services.specifications import loader
from assets.services.specifications.contracts import (
    AssetTypeId,
    FieldKey,
    QualifiedIdentity,
    SpecificationGraphLoadRequest,
)


class _QueryTracker:
    def __init__(self):
        self.evaluations = Counter()
        self.prefetches = Counter()
        self.filters = []


class _FakeQuerySet:
    def __init__(self, rows, tracker: _QueryTracker, label: str):
        self._rows = tuple(rows)
        self._tracker = tracker
        self._label = label

    def filter(self, **lookups):
        self._tracker.filters.append((self._label, lookups))
        return type(self)(
            [row for row in self._rows if _matches(row, lookups)],
            self._tracker,
            self._label,
        )

    def select_related(self, *lookups):
        return self

    def prefetch_related(self, *lookups):
        self._tracker.prefetches[self._label] += len(lookups)
        return self

    def distinct(self):
        return self

    def order_by(self, *fields):
        return self

    def __iter__(self):
        self._tracker.evaluations[self._label] += 1
        return iter(self._rows)


class _FakeManager:
    def __init__(self, rows, tracker: _QueryTracker, label: str):
        self._rows = tuple(rows)
        self._tracker = tracker
        self._label = label

    def filter(self, **lookups):
        return _FakeQuerySet(self._rows, self._tracker, self._label).filter(**lookups)

    def order_by(self, *fields):
        return _FakeQuerySet(self._rows, self._tracker, self._label).order_by(*fields)


def _relation_has(row, relation_name, attribute, expected):
    return any(getattr(item, attribute, None) == expected for item in getattr(row, relation_name))


def _relation_has_in(row, relation_name, attribute, expected):
    return any(getattr(item, attribute, None) in expected for item in getattr(row, relation_name))


def _lookup_handlers():
    return {
        "activation": lambda row, expected: getattr(row, "activation", None) == expected,
        "asset_type_id__in": lambda row, expected: getattr(row, "asset_type_id", None) in expected,
        "fieldset_id__in": lambda row, expected: getattr(row, "fieldset_id", None) in expected,
        "name__in": lambda row, expected: getattr(row, "name", None) in expected,
        "object_types__app_label": lambda row, expected: _relation_has(row, "object_types", "app_label", expected),
        "object_types__model__in": lambda row, expected: _relation_has_in(row, "object_types", "model", expected),
    }


_LOOKUP_HANDLERS = _lookup_handlers()


def _matches(row, lookups):
    try:
        return all(_LOOKUP_HANDLERS[lookup](row, expected) for lookup, expected in lookups.items())
    except KeyError as exc:
        raise AssertionError(f"unhandled fake ORM lookup: {exc.args[0]}") from exc


def _install_fake_orm(monkeypatch, *, type_rows, fieldset_rows, fields, choices, tracker):
    fake_type_model = SimpleNamespace(objects=_FakeManager(type_rows, tracker, "asset_type_fieldsets"))
    fake_fieldset_model = SimpleNamespace(objects=_FakeManager(fieldset_rows, tracker, "fieldset_fields"))
    fake_field_model = type(
        "FakeCustomField",
        (),
        {
            "objects": _FakeManager(fields, tracker, "custom_fields"),
            "ACTIVATION_GLOBAL": "global",
        },
    )
    fake_choice_model = SimpleNamespace(objects=_FakeManager(choices, tracker, "choices"))
    monkeypatch.setattr(loader, "AssetTypeFieldset", fake_type_model)
    monkeypatch.setattr(loader, "CustomFieldsetField", fake_fieldset_model)
    monkeypatch.setattr(loader, "CustomField", fake_field_model)
    monkeypatch.setattr(loader, "CustomFieldChoice", fake_choice_model)


def _content_type(model):
    return SimpleNamespace(app_label="assets", model=model)


def _choice_set(*choices, slug="device-state"):
    return SimpleNamespace(
        pk=10,
        namespace="local",
        slug=slug,
        label="Device state",
        lifecycle="active",
        version=1,
        management_kind="local",
        connector_identity=None,
        replaced_by=None,
        choices=list(choices),
    )


def _choice(key, label, position, lifecycle="active"):
    return SimpleNamespace(
        pk=position,
        key=key,
        label=label,
        position=position,
        lifecycle=lifecycle,
        version=1,
        replaced_by=None,
    )


def _field(
    name,
    *,
    pk,
    object_types=("assettype",),
    activation="composed",
    lifecycle="active",
    field_type="text",
    choice_set=None,
    label=None,
):
    return SimpleNamespace(
        pk=pk,
        name=name,
        namespace="local",
        label=label or name.replace("_", " ").title(),
        help_text="help",
        field_type=field_type,
        activation=activation,
        quantity_kind=None,
        canonical_unit=None,
        minimum_value=None,
        maximum_value=None,
        decimal_scale=None,
        text_max_length=None,
        max_values=None,
        regex=None,
        validation_rule=None,
        required=False,
        nullable=False,
        lifecycle=lifecycle,
        version=1,
        management_kind="local",
        connector_identity=None,
        replaced_by=None,
        choice_set=choice_set,
        object_types=[_content_type(model) for model in object_types],
    )


def _fieldset(slug, *, pk, lifecycle="active"):
    return SimpleNamespace(
        pk=pk,
        namespace="local",
        slug=slug,
        label=slug.replace("-", " ").title(),
        description=f"Description for {slug}",
        lifecycle=lifecycle,
        version=1,
        management_kind="local",
        connector_identity=None,
        replaced_by=None,
    )


def _membership(fieldset, field, position):
    return SimpleNamespace(
        pk=fieldset.pk * 100 + position,
        fieldset_id=fieldset.pk,
        fieldset=fieldset,
        custom_field_id=field.pk,
        custom_field=field,
        position=position,
    )


def _type_membership(asset_type_id, fieldset, position):
    return SimpleNamespace(
        pk=asset_type_id * 100 + fieldset.pk,
        asset_type_id=asset_type_id,
        fieldset_id=fieldset.pk,
        fieldset=fieldset,
        position=position,
    )


def _request(type_ids=(1,), *, target_kinds=("asset_type",), field_keys=()):
    return SpecificationGraphLoadRequest(
        asset_type_ids=tuple(AssetTypeId(value) for value in type_ids),
        requested_target_kinds=frozenset(target_kinds),
        requested_field_keys=frozenset(FieldKey(value) for value in field_keys),
    )


def test_loader_rejects_unknown_target_kind_before_orm_work():
    request = _request(field_keys=("serial_number",), target_kinds=("unknown",))

    with pytest.raises(ValueError, match="requested_target_kinds"):
        loader.load_specification_graph(request)


def test_loader_assembles_stable_graph_and_history_without_owner_queries(monkeypatch):
    tracker = _QueryTracker()
    choices = [_choice("beta", "Beta", 2), _choice("alpha", "Alpha", 1, lifecycle="deprecated")]
    state_choices = _choice_set(*choices)
    current = _field("state", pk=1, field_type="single-select", choice_set=state_choices)
    deprecated = _field("retired", pk=2, lifecycle="deprecated")
    removed = _field("removed", pk=3, object_types=("asset",))
    global_type = _field("asset_type_global", pk=4, activation="global")
    global_asset = _field("asset_global", pk=5, object_types=("asset",), activation="global")
    generic_global = _field("generic_global", pk=6, object_types=("computer",), activation="global")

    first = _fieldset("first", pk=11)
    retired = _fieldset("retired", pk=12, lifecycle="deprecated")
    fieldset_rows = [
        _membership(first, current, 2),
        _membership(first, deprecated, 1),
        _membership(retired, current, 1),
    ]
    type_rows = [_type_membership(2, retired, 1), _type_membership(1, first, 2)]
    _install_fake_orm(
        monkeypatch,
        type_rows=type_rows,
        fieldset_rows=fieldset_rows,
        fields=[current, deprecated, removed, global_type, global_asset, generic_global],
        choices=[],
        tracker=tracker,
    )

    graph = loader.load_specification_graph(
        _request(
            type_ids=(2, 1, 1),
            target_kinds=("asset", "asset_type"),
            field_keys=("state", "retired", "removed", "unknown"),
        )
    )

    assert list(graph.type_memberships) == [1, 2]
    assert graph.type_memberships[1][0].fieldset_identity == "local/first"
    assert graph.type_memberships[1][0].ordinal == 2
    assert graph.fields_by_key["state"].field_type == "single_select"
    assert graph.fields_by_key["state"].targets == frozenset({"asset_type"})
    assert [choice.key for choice in graph.fields_by_key["state"].choice_set.choices] == ["alpha", "beta"]
    assert graph.global_field_keys_by_target["asset_type"] == ("asset_type_global",)
    assert graph.global_field_keys_by_target["asset"] == ("asset_global",)
    assert set(graph.historical_definitions_by_key) == {"removed", "retired", "state"}
    assert "unknown" not in graph.historical_definitions_by_key

    with pytest.raises(TypeError):
        graph.fields_by_key[FieldKey("new")] = graph.fields_by_key["state"]
    assert set(tracker.evaluations) == {"asset_type_fieldsets", "fieldset_fields", "custom_fields"}
    assert "asset_types" not in tracker.evaluations
    assert "assets" not in tracker.evaluations
    assert "tenants" not in tracker.evaluations


def test_resource_revisions_change_for_metadata_membership_and_choices_but_not_values(monkeypatch):
    tracker = _QueryTracker()
    choice = _choice("ready", "Ready", 1)
    choice_set = _choice_set(choice)
    field = _field("state", pk=1, field_type="single-select", choice_set=choice_set)
    fieldset = _fieldset("spec", pk=11)
    membership = _membership(fieldset, field, 1)
    type_row = _type_membership(1, fieldset, 1)
    _install_fake_orm(
        monkeypatch,
        type_rows=[type_row],
        fieldset_rows=[membership],
        fields=[field],
        choices=[],
        tracker=tracker,
    )
    request = _request(field_keys=("state",))

    first = loader.load_specification_graph(request)
    first_field_revision = first.fields_by_key["state"].resource_revision
    first_fieldset_revision = first.fieldsets_by_identity["local/spec"].resource_revision
    first_choice_revision = first.fields_by_key["state"].choice_set.resource_revision

    field.label = "Changed label"
    metadata_changed = loader.load_specification_graph(request)
    assert metadata_changed.fields_by_key["state"].resource_revision != first_field_revision

    field.label = "State"
    membership.position = 2
    membership_changed = loader.load_specification_graph(request)
    assert membership_changed.fieldsets_by_identity["local/spec"].resource_revision != first_fieldset_revision
    assert membership_changed.fields_by_key["state"].resource_revision == first_field_revision

    membership.position = 1
    choice.label = "Changed choice"
    choices_changed = loader.load_specification_graph(request)
    assert choices_changed.fields_by_key["state"].choice_set.resource_revision != first_choice_revision
    assert choices_changed.fields_by_key["state"].resource_revision != first_field_revision

    # The public request has no owner-value input; identical definition requests
    # therefore cannot collide with or depend on owner JSON.
    same_again = loader.load_specification_graph(request)
    assert same_again == choices_changed


def test_loader_query_evaluations_are_chunked_and_repeated_ids_are_deduplicated(monkeypatch):
    tracker = _QueryTracker()
    type_rows = []
    fieldset_rows = []
    fields = []
    for index in range(1, 6):
        fieldset = _fieldset(f"set-{index}", pk=index)
        field = _field(f"field_{index}", pk=index)
        type_rows.append(_type_membership(index, fieldset, 1))
        fieldset_rows.append(_membership(fieldset, field, 1))
        fields.append(field)
    historical_fields = [_field(f"history_{index}", pk=100 + index) for index in range(1, 6)]
    _install_fake_orm(
        monkeypatch,
        type_rows=type_rows,
        fieldset_rows=fieldset_rows,
        fields=[*fields, *historical_fields],
        choices=[],
        tracker=tracker,
    )
    monkeypatch.setattr(loader, "_BATCH_SIZE", 2)

    graph = loader.load_specification_graph(
        _request(
            type_ids=(5, 4, 3, 2, 1, 1, 5),
            target_kinds=("asset_type",),
            field_keys=tuple(f"history_{index}" for index in range(1, 6)),
        )
    )

    assert list(graph.type_memberships) == [1, 2, 3, 4, 5]
    assert set(graph.historical_definitions_by_key) == {f"history_{index}" for index in range(1, 6)}
    assert tracker.evaluations["asset_type_fieldsets"] == 3
    assert tracker.evaluations["fieldset_fields"] == 3
    assert tracker.prefetches["fieldset_fields"] == 6
    assert tracker.prefetches["custom_fields"] == 8
    # One global phase plus three exact-key history chunks; no query is made
    # per historical Field or per repeated Type ID.
    assert tracker.evaluations["custom_fields"] == 4


# ---------------------------------------------------------------------------
# Batch E: transport-independent request shape, batching bounds and the
# ordinal/enum/identity helpers used while mapping rows into DTOs.
# ---------------------------------------------------------------------------


def test_type_ids_must_be_a_tuple_of_positive_integers():
    assert loader._validate_type_ids((3, 1, 3)) == (1, 3)

    with pytest.raises(TypeError):
        loader._validate_type_ids([1, 2])

    for bad in (0, -1, "1", True, None):
        with pytest.raises(ValueError):
            loader._validate_type_ids((bad,))


def test_target_kinds_must_be_a_frozenset_of_supported_strings():
    assert loader._validate_target_kinds(frozenset({"asset", "asset_type"})) == ("asset", "asset_type")

    with pytest.raises(TypeError):
        loader._validate_target_kinds(("asset",))

    with pytest.raises(ValueError):
        loader._validate_target_kinds(frozenset({1}))

    with pytest.raises(ValueError):
        loader._validate_target_kinds(frozenset({"inventory"}))


def test_field_keys_must_be_a_frozenset_of_non_empty_strings():
    assert loader._validate_field_keys(frozenset({"memory_capacity"})) == ("memory_capacity",)

    with pytest.raises(TypeError):
        loader._validate_field_keys(["memory_capacity"])

    with pytest.raises(ValueError):
        loader._validate_field_keys(frozenset({""}))


def test_fieldset_identities_must_be_qualified_and_unique():
    assert loader._validate_fieldset_identities(("itambox/storage",)) == ("itambox/storage",)

    with pytest.raises(TypeError):
        loader._validate_fieldset_identities(["itambox/storage"])

    with pytest.raises(ValueError):
        loader._validate_fieldset_identities(("itambox",))

    with pytest.raises(ValueError):
        loader._validate_fieldset_identities(("itambox/",))

    with pytest.raises(ValueError):
        loader._validate_fieldset_identities(("itambox/storage", "itambox/storage"))


def test_validate_request_rejects_foreign_requests_and_normalises_fields():
    request = SpecificationGraphLoadRequest(
        asset_type_ids=(2, 1),
        requested_target_kinds=frozenset({"asset_type"}),
        requested_field_keys=frozenset({"memory_capacity"}),
    )

    assert loader._validate_request(request) == ((1, 2), ("asset_type",), ("memory_capacity",))

    with pytest.raises(TypeError):
        loader._validate_request(SimpleNamespace(asset_type_ids=(1,)))


def test_chunks_reject_non_positive_sizes_and_partition_evenly():
    assert list(loader._chunks([1, 2, 3], 2)) == [(1, 2), (3,)]
    assert list(loader._chunks((), 2)) == []
    assert list(loader._chunks(range(2))) == [(0, 1)]

    with pytest.raises(ValueError):
        list(loader._chunks([1], 0))


def test_safe_attr_and_relation_rows_are_null_tolerant():
    class _Raising:
        @property
        def broken(self):
            raise AttributeError("missing")

    class _Manager:
        def all(self):
            return [1, 2]

    assert loader._safe_attr(SimpleNamespace(value=3), "value") == 3
    assert loader._safe_attr(SimpleNamespace(), "value", "fallback") == "fallback"
    assert loader._safe_attr(_Raising(), "broken") is None
    assert loader._relation_rows(SimpleNamespace(rows=None), "rows") == ()
    assert loader._relation_rows(SimpleNamespace(rows=_Manager()), "rows") == (1, 2)
    assert loader._relation_rows(SimpleNamespace(rows=[3]), "rows") == (3,)


def test_ordinal_lifecycle_activation_and_field_type_helpers():
    assert loader._positive_ordinal(2, "Position") == 2
    assert loader._lifecycle("active", "Field") == "active"
    assert loader._activation("global") == "global"
    assert loader._field_type("single-select") == "single_select"
    assert loader._field_type("multi_select") == "multi_select"

    with pytest.raises(ValueError):
        loader._positive_ordinal(0, "Position")

    with pytest.raises(ValueError):
        loader._positive_ordinal(True, "Position")

    with pytest.raises(ValueError):
        loader._lifecycle("retired", "Field")

    with pytest.raises(ValueError):
        loader._activation("composed_extra")

    with pytest.raises(ValueError):
        loader._field_type("unsupported")


def test_qualified_identity_requires_both_components():
    assert loader._qualified_identity("itambox", "memory", "Field") == QualifiedIdentity("itambox/memory")

    with pytest.raises(ValueError):
        loader._qualified_identity("", "memory", "Field")

    with pytest.raises(ValueError):
        loader._qualified_identity("itambox", "", "Field")

    with pytest.raises(ValueError):
        loader._qualified_identity(None, "memory", "Field")
