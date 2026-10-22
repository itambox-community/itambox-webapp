"""Request-time contract for generic-presentation orchestration (issue #444)."""

from types import MappingProxyType
from unittest.mock import patch

import django_filters
import pytest
from django import forms
from django.apps import apps
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.http import QueryDict
from django.test import RequestFactory

from core.models import Job
from core.tables import BaseTable
from extras.feature_views import EXTRAS_GENERIC_PRESENTATION_PROVIDER
from itambox.registry import ListParamsResult, registry
from itambox.views.generic.detail import ObjectDetailView
from itambox.views.generic.extensions import (
    build_detail_provider_context,
    build_list_provider_context,
    filter_list_provider_queryset,
    resolve_list_provider_params,
)
from itambox.views.generic.list_ import ObjectListView
from subscriptions.feature_views import SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER


class RecordingProvider:
    def __init__(self, *, params=None, filters=None, context=None, detail=None):
        self.params_callback = params
        self.filter_callback = filters
        self.context_callback = context
        self.detail_callback = detail
        self.calls = []

    def resolve_list_params(self, input):
        self.calls.append(("params", input))
        if self.params_callback is not None:
            return self.params_callback(input)
        return ListParamsResult(input.params, {})

    def filter_list_queryset(self, input):
        self.calls.append(("filter", input))
        if self.filter_callback is not None:
            return self.filter_callback(input)
        return input.queryset

    def build_list_context(self, input):
        self.calls.append(("context", input))
        if self.context_callback is not None:
            return self.context_callback(input)
        return {}

    def build_detail_context(self, input):
        self.calls.append(("detail", input))
        if self.detail_callback is not None:
            return self.detail_callback(input)
        return {}


def _copy_with(params, values):
    """Return a mutable copy of ``params`` with ``values`` applied (test helper)."""
    changed = params.copy()
    changed.update(values)
    return changed


def register_provider(
    name,
    provider,
    *,
    detail_features=(),
    list_params=False,
    list_filter=False,
    list_context=False,
    priority=100,
):
    registry.register_generic_presentation(
        name,
        provider,
        detail_features=detail_features,
        list_params=list_params,
        list_filter=list_filter,
        list_context=list_context,
        priority=priority,
    )


class GroupFilterSet(django_filters.FilterSet):
    class Meta:
        model = Group
        fields = ("id", "name")


class GroupFilterForm(forms.Form):
    id = forms.IntegerField(required=False)
    name = forms.CharField(required=False)
    filterset_class = GroupFilterSet

    def __init__(self, *args, queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.filterset = self.filterset_class(self.data, queryset=queryset)


class GroupTable(BaseTable):
    class Meta:
        model = Group
        fields = ("id", "name")


class GroupListView(ObjectListView):
    model = Group
    queryset = Group.objects.all()
    filterset = GroupFilterSet
    filterset_form = GroupFilterForm
    table = GroupTable


class GroupDetailView(ObjectDetailView):
    model = Group
    queryset = Group.objects.all()
    disable_related_objects_list = True


def make_request(path="/groups/", params=None):
    request = RequestFactory().get(path, params or {})
    request.user = AnonymousUser()
    return request


def make_list_view(params=None):
    view = GroupListView()
    view.setup(make_request(params=params))
    return view


@pytest.mark.django_db
class TestListPipeline:
    def test_view_reuses_one_content_type_across_every_provider_phase(self):
        Group.objects.create(name="Pipeline group")
        identities = []

        def params(input):
            identities.append(input.content_type)
            assert input.params._mutable is False
            changed = input.params.copy()
            changed["name"] = "Pipeline group"
            return ListParamsResult(changed, {"catalogue": ["visible"]})

        def filters(input):
            identities.append(input.content_type)
            assert input.state == {"catalogue": ["visible"]}
            return input.queryset.filter(name="Pipeline group")

        def context(input):
            identities.append(input.content_type)
            assert input.state == {"catalogue": ["visible"]}
            return {"provider_marker": "present"}

        provider = RecordingProvider(params=params, filters=filters, context=context)
        with registry.isolated_generic_presentation_for_tests():
            register_provider(
                "pipeline",
                provider,
                list_params=True,
                list_filter=True,
                list_context=True,
                priority=100,
            )
            view = make_list_view()
            with patch.object(
                ContentType.objects,
                "get_for_model",
                wraps=ContentType.objects.get_for_model,
            ) as get_for_model:
                queryset = view.get_queryset()
                view.object_list = queryset
                context_data = view.get_context_data(object_list=queryset)

        assert get_for_model.call_count == 1
        assert len(identities) == 3
        assert all(content_type is identities[0] for content_type in identities)
        assert context_data["provider_marker"] == "present"
        assert [phase for phase, _input in provider.calls] == ["params", "filter", "context"]

    def test_invalid_generic_validation_sets_the_flag_and_calls_no_filter_provider(self):
        provider = RecordingProvider(filters=lambda input: input.queryset)
        with registry.isolated_generic_presentation_for_tests():
            register_provider("filter", provider, list_filter=True, priority=100)
            view = make_list_view({"id": "not-an-integer"})

            queryset = view.get_queryset()

        assert view.filter_validation_failed is True
        assert queryset.query.is_empty()
        assert "id" in view.filter_form.errors
        assert provider.calls == []

    def test_two_parameter_providers_chain_and_keep_state_private(self):
        seen = []

        def first(input):
            seen.append(("first", input.params.getlist("raw")))
            changed = input.params.copy()
            changed.setlist("alpha", ["one", "two"])
            return ListParamsResult(changed, {"owner": "first"})

        def second(input):
            seen.append(("second", input.params.getlist("alpha")))
            changed = input.params.copy()
            changed["beta"] = "three"
            return ListParamsResult(changed, {"owner": "second"})

        first_provider = RecordingProvider(params=first)
        second_provider = RecordingProvider(params=second)
        with registry.isolated_generic_presentation_for_tests():
            register_provider("second", second_provider, list_params=True, priority=200)
            register_provider("first", first_provider, list_params=True, priority=100)

            resolution = resolve_list_provider_params(
                make_request(params=QueryDict("raw=a&raw=b")),
                Group,
                partial=False,
            )

        assert seen == [("first", ["a", "b"]), ("second", ["one", "two"])]
        assert resolution.params.getlist("alpha") == ["one", "two"]
        assert resolution.params["beta"] == "three"
        assert resolution.params._mutable is False
        assert resolution.provider_state == {
            "first": {"owner": "first"},
            "second": {"owner": "second"},
        }
        assert isinstance(resolution.provider_state, MappingProxyType)
        assert resolution.provider_state["first"] is not resolution.provider_state["second"]

    def test_two_fake_parameter_providers_compose_around_production_extras(self, django_user_model):
        call_order = []

        def before(input):
            call_order.append("before")
            changed = input.params.copy()
            changed["before"] = "one"
            return ListParamsResult(changed, {})

        def after(input):
            call_order.append("after")
            assert input.params["before"] == "one"
            changed = input.params.copy()
            changed["after"] = "two"
            return ListParamsResult(changed, {})

        request = make_request()
        request.user = django_user_model.objects.create_user(username="plural-production-list")
        with registry.isolated_generic_presentation_for_tests():
            register_provider("before", RecordingProvider(params=before), list_params=True, priority=50)
            register_provider(
                "extras",
                EXTRAS_GENERIC_PRESENTATION_PROVIDER,
                list_params=True,
                list_filter=True,
                list_context=True,
                priority=100,
            )
            register_provider("after", RecordingProvider(params=after), list_params=True, priority=150)

            resolution = resolve_list_provider_params(request, Group, partial=True)
            context = build_list_provider_context(resolution, {})

        assert call_order == ["before", "after"]
        assert resolution.params["before"] == "one"
        assert resolution.params["after"] == "two"
        assert context["saved_filters"] == []
        assert context["active_saved_filter_id"] is None
        assert context["export_templates"] == []
        assert "label_templates" not in context

    @pytest.mark.parametrize("second_action", ("replace", "delete"))
    def test_parameter_collision_names_both_providers_and_key(self, second_action):
        def first(input):
            changed = input.params.copy()
            changed.setlist("shared", ["first"])
            return ListParamsResult(changed, {})

        def second(input):
            changed = input.params.copy()
            if second_action == "replace":
                changed.setlist("shared", ["second"])
            else:
                changed.pop("shared", None)
            return ListParamsResult(changed, {})

        with registry.isolated_generic_presentation_for_tests():
            register_provider("first", RecordingProvider(params=first), list_params=True, priority=100)
            register_provider("second", RecordingProvider(params=second), list_params=True, priority=200)

            with pytest.raises(ImproperlyConfigured, match="first.*second.*shared"):
                resolve_list_provider_params(make_request(), Group, partial=False)

    @pytest.mark.parametrize(
        ("result", "message"),
        (
            ([], "QuerySet"),
            (Permission.objects.all(), "model"),
            (Group.objects.all().using("replica"), "database"),
        ),
    )
    def test_filter_phase_rejects_invalid_results(self, result, message):
        provider = RecordingProvider(filters=lambda _input: result)
        with registry.isolated_generic_presentation_for_tests():
            register_provider("invalid", provider, list_filter=True, priority=100)
            resolution = resolve_list_provider_params(make_request(), Group, partial=False)

            with pytest.raises(ImproperlyConfigured, match=f"invalid.*{message}"):
                filter_list_provider_queryset(resolution, Group.objects.all())


@pytest.mark.django_db
class TestContextCollisionsAndFailures:
    def test_list_context_rejects_a_core_collision(self):
        provider = RecordingProvider(context=lambda _input: {"can_change": False})
        with registry.isolated_generic_presentation_for_tests():
            register_provider("extras", provider, list_context=True, priority=100)
            resolution = resolve_list_provider_params(make_request(), Group, partial=False)

            with pytest.raises(ImproperlyConfigured, match="can_change.*core.*extras"):
                build_list_provider_context(resolution, {"can_change": True})

    def test_list_context_rejects_a_provider_collision(self):
        with registry.isolated_generic_presentation_for_tests():
            register_provider(
                "first",
                RecordingProvider(context=lambda _input: {"shared": 1}),
                list_context=True,
                priority=100,
            )
            register_provider(
                "second",
                RecordingProvider(context=lambda _input: {"shared": 2}),
                list_context=True,
                priority=200,
            )
            resolution = resolve_list_provider_params(make_request(), Group, partial=False)

            with pytest.raises(ImproperlyConfigured, match="shared.*first.*second"):
                build_list_provider_context(resolution, {})

    def test_provider_exception_propagates_unchanged(self):
        failure = RuntimeError("provider failed")

        def fail(_input):
            raise failure

        with registry.isolated_generic_presentation_for_tests():
            register_provider("failing", RecordingProvider(params=fail), list_params=True, priority=100)
            with pytest.raises(RuntimeError, match="provider failed") as captured:
                resolve_list_provider_params(make_request(), Group, partial=False)

        assert captured.value is failure


@pytest.mark.django_db
class TestImmutableParameterIsolation:
    """Providers never see the collision-detection baseline and cannot defeat
    last-writer-wins protection by re-enabling QueryDict mutability."""

    def mutation_provider(self, params):
        def mutate(input):
            input.params._mutable = True
            input.params.update(params)
            input.params._mutable = False
            return ListParamsResult(input.params, {})

        return mutate

    def test_received_params_are_private_frozen_copies_that_never_alias_resolution(self):
        def params(input):
            changed = input.params.copy()
            changed["name"] = "Immutable group"
            return ListParamsResult(changed, {})

        def filters(input):
            assert input.params.getlist("name") == ["Immutable group"]
            assert input.params._mutable is False
            assert input.params is not resolution.params
            return input.queryset

        def context(input):
            assert input.params.getlist("name") == ["Immutable group"]
            assert input.params._mutable is False
            assert input.params is not resolution.params
            return {}

        provider = RecordingProvider(params=params, filters=filters, context=context)
        with registry.isolated_generic_presentation_for_tests():
            register_provider(
                "isolation", provider, list_params=True, list_filter=True, list_context=True, priority=100
            )
            resolution = resolve_list_provider_params(make_request(), Group, partial=False)
            assert "name" in resolution.params
            filter_list_provider_queryset(resolution, Group.objects.all())
            build_list_provider_context(resolution, {})

        assert [phase for phase, _ in provider.calls] == ["params", "filter", "context"]

    def test_in_place_mutation_of_received_params_raises_and_names_provider_and_key(self):
        # Original attack shape: provider A legitimately owns ``shared``,
        # provider B re-enables mutability and overwrites it in place to
        # defeat last-writer-wins detection.
        with registry.isolated_generic_presentation_for_tests():
            register_provider(
                "first",
                RecordingProvider(
                    params=lambda input: ListParamsResult(_copy_with(input.params, {"shared": ["first"]}), {})
                ),
                list_params=True,
                priority=100,
            )
            register_provider(
                "second",
                RecordingProvider(params=self.mutation_provider({"shared": "second"})),
                list_params=True,
                priority=200,
            )

            with pytest.raises(ImproperlyConfigured, match="second.*mutated parameters.*shared"):
                resolve_list_provider_params(make_request(), Group, partial=False)

    def test_in_place_mutation_left_mutable_also_raises(self):
        # Only the ``_mutable`` operand triggers: no key drift at all.
        def mutate(input):
            input.params._mutable = True
            return ListParamsResult(input.params.copy(), {})

        with registry.isolated_generic_presentation_for_tests():
            register_provider("second", RecordingProvider(params=mutate), list_params=True, priority=100)

            with pytest.raises(ImproperlyConfigured, match=r"second.*in place \(keys: \[\]\)"):
                resolve_list_provider_params(make_request(), Group, partial=False)

    def test_filter_phase_params_are_private_copies(self):
        seen_by_later_context = []

        def sabotage(input):
            assert input.params._mutable is False
            input.params._mutable = True
            input.params["sabotage"] = "yes"
            input.params._mutable = False
            return input.queryset

        def observing_context(input):
            seen_by_later_context.append(input.params.getlist("q"))
            return {}

        with registry.isolated_generic_presentation_for_tests():
            register_provider("saboteur", RecordingProvider(filters=sabotage), list_filter=True, priority=100)
            register_provider("observer", RecordingProvider(context=observing_context), list_context=True, priority=200)
            resolution = resolve_list_provider_params(make_request(params={"q": "needle"}), Group, partial=False)

            filter_list_provider_queryset(resolution, Group.objects.all())
            build_list_provider_context(resolution, {})

        assert resolution.params.getlist("q") == ["needle"]
        assert "sabotage" not in resolution.params
        assert seen_by_later_context == [["needle"]]

    def test_context_phase_params_are_private_copies(self):
        seen_by_later_context = []

        def sabotage(input):
            assert input.params._mutable is False
            input.params._mutable = True
            input.params["sabotage"] = "yes"
            input.params._mutable = False
            return {}

        def observing_context(input):
            seen_by_later_context.append(input.params.getlist("q"))
            return {}

        with registry.isolated_generic_presentation_for_tests():
            register_provider("saboteur", RecordingProvider(context=sabotage), list_context=True, priority=100)
            register_provider("observer", RecordingProvider(context=observing_context), list_context=True, priority=200)
            resolution = resolve_list_provider_params(make_request(params={"q": "needle"}), Group, partial=False)

            build_list_provider_context(resolution, {})

        assert resolution.params.getlist("q") == ["needle"]
        assert "sabotage" not in resolution.params
        assert seen_by_later_context == [["needle"]]


@pytest.mark.django_db
class TestValidatedParamsResultRejections:
    """Every rejection branch of ``_validated_params_result`` is pinned."""

    @pytest.mark.parametrize(
        ("result", "message"),
        (
            (object(), "ListParamsResult"),
            (ListParamsResult(params={"a": "b"}, state={}), "QueryDict"),
            (ListParamsResult(params=QueryDict("a=1"), state=[]), "state"),
            (ListParamsResult(params=QueryDict("a=1"), state={1: "x"}), "state key"),
        ),
    )
    def test_rejects_invalid_provider_results(self, result, message):
        provider = RecordingProvider(params=lambda _input: result)
        with registry.isolated_generic_presentation_for_tests():
            register_provider("invalid", provider, list_params=True, priority=100)

            with pytest.raises(ImproperlyConfigured, match=f"invalid.*{message}"):
                resolve_list_provider_params(make_request(), Group, partial=False)


@pytest.mark.django_db
class TestDetailPipeline:
    def test_detail_view_resolves_once_and_calls_one_provider_for_many_features(self):
        group = Group.objects.create(name="Detail pipeline group")
        expected_content_type = ContentType.objects.get_for_model(group)
        provider = RecordingProvider(detail=lambda _input: {"detail_marker": "present"})
        registry.register_feature(Group, "job_file_attachments")
        registry.register_feature(Group, "watchable")
        try:
            with registry.isolated_generic_presentation_for_tests():
                register_provider(
                    "detail",
                    provider,
                    detail_features=("job_file_attachments", "watchable"),
                    priority=100,
                )
                view = GroupDetailView()
                view.setup(make_request(f"/groups/{group.pk}/"), pk=group.pk)
                view.object = group
                view._cached_object = group
                with (
                    patch("itambox.views.generic.detail.reverse", return_value="/test/"),
                    patch.object(
                        ContentType.objects,
                        "get_for_model",
                        return_value=expected_content_type,
                    ) as get_for_model,
                ):
                    context_data = view.get_context_data(object=group)
        finally:
            registry.unregister_feature(Group, "job_file_attachments")
            registry.unregister_feature(Group, "watchable")

        assert get_for_model.call_count == 1
        detail_calls = [input for phase, input in provider.calls if phase == "detail"]
        assert len(detail_calls) == 1
        assert detail_calls[0].content_type is expected_content_type
        assert detail_calls[0].active_features == frozenset({"job_file_attachments", "watchable"})
        assert context_data["detail_marker"] == "present"

    def test_detail_context_rejects_a_core_collision(self):
        group = Group(name="Unsaved detail group")
        content_type = object()
        provider = RecordingProvider(detail=lambda _input: {"can_delete": False})
        registry.register_feature(Group, "job_file_attachments")
        try:
            with registry.isolated_generic_presentation_for_tests():
                register_provider(
                    "detail",
                    provider,
                    detail_features=("job_file_attachments",),
                    priority=100,
                )
                with pytest.raises(ImproperlyConfigured, match="can_delete.*core.*detail"):
                    build_detail_provider_context(
                        make_request(),
                        group,
                        content_type,
                        core_context={"can_delete": True},
                    )
        finally:
            registry.unregister_feature(Group, "job_file_attachments")


@pytest.mark.django_db
class TestProductionReadiness:
    def test_production_registrations_and_synthetic_job_feature_are_exact(self):
        registrations = {item.name: item for item in registry.generic_presentation_registrations()}

        assert tuple(item.name for item in registry.generic_presentation_registrations()) == (
            "extras",
            "subscriptions",
        )
        assert registrations["extras"].provider is EXTRAS_GENERIC_PRESENTATION_PROVIDER
        assert registrations["extras"].detail_features == frozenset(
            {
                "bookmarkable",
                "custom_field_data",
                "file_attachments",
                "image_attachments",
                "job_file_attachments",
                "journaling",
                "watchable",
            }
        )
        assert (
            registrations["extras"].list_params,
            registrations["extras"].list_filter,
            registrations["extras"].list_context,
            registrations["extras"].priority,
        ) == (True, True, True, 100)
        assert registrations["subscriptions"].provider is SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER
        assert registrations["subscriptions"].detail_features == frozenset({"subscribable"})
        assert (
            registrations["subscriptions"].list_params,
            registrations["subscriptions"].list_filter,
            registrations["subscriptions"].list_context,
            registrations["subscriptions"].priority,
        ) == (False, False, False, 200)
        assert registry.model_has_feature(Job, "job_file_attachments")

    def test_repeated_ready_is_zero_query_idempotent_and_invokes_no_provider(self, django_assert_num_queries):
        extras_config = apps.get_app_config("extras")
        subscriptions_config = apps.get_app_config("subscriptions")
        before = registry.generic_presentation_registrations()

        with (
            patch.object(EXTRAS_GENERIC_PRESENTATION_PROVIDER, "resolve_list_params", wraps=None) as extras_params,
            patch.object(
                SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER,
                "build_detail_context",
                wraps=None,
            ) as subscriptions_detail,
            django_assert_num_queries(0),
        ):
            extras_config.ready()
            subscriptions_config.ready()

        assert registry.generic_presentation_registrations() == before
        extras_params.assert_not_called()
        subscriptions_detail.assert_not_called()

    def test_production_list_provider_preserves_queryset_and_supplies_context(self, django_user_model):
        request = make_request()
        request.user = django_user_model.objects.create_user(username="production-list-provider")
        resolution = resolve_list_provider_params(request, Group, partial=False)
        queryset = Group.objects.all()
        filtered = filter_list_provider_queryset(resolution, queryset)
        core_context = {"can_add": False, "table": object()}

        merged = build_list_provider_context(resolution, core_context)

        assert resolution.params is not request.GET
        assert resolution.params == request.GET
        assert filtered is queryset
        assert set(("saved_filters", "active_saved_filter_id", "export_templates")) <= set(merged)
        assert merged["active_saved_filter_id"] is None
        assert merged is not core_context
