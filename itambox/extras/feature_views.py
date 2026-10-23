"""Extras-owned generic-presentation provider."""

from django.db.models import Q
from django.http import QueryDict

from core.forms import JournalEntryForm
from core.managers import Scope
from extras.customfields import apply_custom_field_filters
from extras.models import (
    Bookmark,
    CustomField,
    ExportTemplate,
    FileAttachment,
    ImageAttachment,
    JournalEntry,
    ObjectWatch,
    SavedFilter,
)
from itambox.registry import (
    DetailContextInput,
    ListContextInput,
    ListFilterInput,
    ListParamsInput,
    ListParamsResult,
    registry,
)


def _custom_field_context(input: DetailContextInput) -> dict[str, object]:
    data = getattr(input.obj, "custom_field_data", None) or {}
    if not data:
        return {"custom_fields_display": []}
    labels = dict(CustomField.objects.filter(object_types=input.content_type).values_list("name", "label"))
    return {"custom_fields_display": [(labels.get(name, name), value) for name, value in sorted(data.items())]}


def _bookmark_context(input: DetailContextInput) -> dict[str, object]:
    is_bookmarked = False
    if input.request.user.is_authenticated:
        is_bookmarked = Bookmark.objects.filter(
            user=input.request.user,
            model=input.content_type,
            object_id=input.obj.pk,
        ).exists()
    return {
        "is_bookmarkable": True,
        "bookmark_content_type_id": input.content_type.pk,
        "is_bookmarked": is_bookmarked,
    }


def _watch_context(input: DetailContextInput) -> dict[str, object]:
    is_watched = False
    if input.request.user.is_authenticated:
        is_watched = ObjectWatch.objects.filter(
            user=input.request.user,
            model=input.content_type,
            object_id=input.obj.pk,
        ).exists()
    return {
        "is_watchable": True,
        "watch_content_type_id": input.content_type.pk,
        "is_watched": is_watched,
    }


def _visible_saved_filters(input: ListParamsInput) -> list[SavedFilter]:
    queryset = (
        SavedFilter.objects.for_scope(Scope.current())
        .filter(content_type=input.content_type, enabled=True)
        .filter(Q(tenant__isnull=True) | Q(shared=True) | Q(created_by=input.request.user))
    )
    return list(queryset)


def _saved_filter_params(saved_filter: SavedFilter) -> QueryDict:
    params = QueryDict(mutable=True)
    for key, value in (saved_filter.parameters or {}).items():
        if isinstance(value, (list, tuple)):
            params.setlist(key, list(value))
        else:
            params[key] = value
    params._mutable = False
    return params


class _ExtrasGenericPresentationProvider:
    def resolve_list_params(self, input: ListParamsInput) -> ListParamsResult:
        raw_filter_pk = input.params.get("filter")
        try:
            filter_pk = int(raw_filter_pk) if raw_filter_pk else None
        except (TypeError, ValueError):
            filter_pk = None
        try:
            saved_filters = _visible_saved_filters(input)
        except Exception:  # broad except: render-degrade: preserve the optional catalogue without an activation lookup
            if filter_pk is not None:
                raise
            saved_filters = []

        selected_id = None
        params = input.params
        if filter_pk is not None:
            selected = next(
                (
                    saved_filter
                    for saved_filter in saved_filters
                    if saved_filter.pk == filter_pk and saved_filter.content_type_id == input.content_type.pk
                ),
                None,
            )
            if selected is not None:
                params = _saved_filter_params(selected)
                selected_id = selected.pk
        return ListParamsResult(
            params=params,
            state={
                "saved_filters": saved_filters,
                "active_saved_filter_id": selected_id,
            },
        )

    def filter_list_queryset(self, input: ListFilterInput):
        if registry.model_has_feature(input.model, "custom_field_data"):
            return apply_custom_field_filters(input.queryset, input.model, input.params)
        return input.queryset

    def build_list_context(self, input: ListContextInput) -> dict[str, object]:
        context = {
            "saved_filters": input.state["saved_filters"],
            "active_saved_filter_id": input.state["active_saved_filter_id"],
        }
        if input.partial:
            context["export_templates"] = []
            return context

        try:
            context["export_templates"] = list(ExportTemplate.objects.filter(content_type=input.content_type))
        except Exception:  # broad except: render-degrade: keep list pages usable when export catalogues fail
            context["export_templates"] = []
        return context

    def build_detail_context(self, input: DetailContextInput) -> dict[str, object]:
        context: dict[str, object] = {
            "attachment_app_label": input.obj._meta.app_label,
            "attachment_model_name": input.obj._meta.model_name,
        }
        active_features = input.active_features

        if "journaling" in active_features:
            journal_entries = JournalEntry.objects.for_scope(Scope.current()).filter(
                model=input.content_type,
                object_id=input.obj.pk,
            )
            context.update(
                {
                    "has_journaling": True,
                    "journal_app_label": input.obj._meta.app_label,
                    "journal_model_name": input.obj._meta.model_name,
                    "journal_entries": journal_entries.select_related("user").order_by("-created")[:50],
                    "journal_entries_count": journal_entries.count(),
                    "journal_form": JournalEntryForm(),
                }
            )

        if "custom_field_data" in active_features:
            context.update(_custom_field_context(input))

        if "image_attachments" in active_features:
            context["image_attachments"] = ImageAttachment.objects.filter(
                model=input.content_type,
                object_id=input.obj.pk,
            ).order_by("-created")[:20]
            context["has_image_attachments"] = True

        if "file_attachments" in active_features:
            context["file_attachments"] = FileAttachment.objects.filter(
                model=input.content_type,
                object_id=input.obj.pk,
            ).order_by("-created")[:20]
            context["has_file_attachments"] = True

        if "job_file_attachments" in active_features:
            context["attachments"] = FileAttachment.objects.filter(
                model=input.content_type,
                object_id=input.obj.pk,
            )

        if "bookmarkable" in active_features:
            context.update(_bookmark_context(input))

        if "watchable" in active_features:
            context.update(_watch_context(input))

        return context


EXTRAS_GENERIC_PRESENTATION_PROVIDER = _ExtrasGenericPresentationProvider()
