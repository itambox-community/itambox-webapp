"""Core object-change presentation."""

import difflib
import json

from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView

from core.filters import ObjectChangeFilterSet
from core.forms import FilterForm
from core.models import ObjectChange
from core.tables import ObjectChangeTable
from itambox.views.generic import BaseHTMXView, ObjectListView


class ObjectChangeFilterForm(FilterForm):
    filterset_class = ObjectChangeFilterSet


@method_decorator(login_required, name="dispatch")
class ObjectChangeListView(ObjectListView):
    # Scoped per request: TenantScopingViewMixin applies filter_by_tenant().
    queryset = ObjectChange.objects.prefetch_related("user", "changed_object_type", "related_object_type")
    filterset = ObjectChangeFilterSet
    filterset_form = ObjectChangeFilterForm
    table = ObjectChangeTable
    template_name = "core/objectchange/objectchange_list.html"
    action_buttons = ()

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Changelog"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Changelog")
        return context


# What looking up a row by a logged primary key can raise: the row is gone (or outside
# the active tenant), or the stored value does not fit the key's type.
_LOOKUP_ERRORS = (ObjectDoesNotExist, ValueError, TypeError, ValidationError)


def _related_label(related_model, pk):
    try:
        return str(related_model.objects.get(pk=pk))
    except _LOOKUP_ERRORS:
        return f"{related_model._meta.model_name} #{pk} (deleted)"


def resolve_serialized_data(model_class, data):
    if not model_class or not data:
        return data

    resolved_data = {}
    for k, v in data.items():
        if v is None:
            resolved_data[k] = v
            continue

        try:
            field = model_class._meta.get_field(k)
        except FieldDoesNotExist:
            resolved_data[k] = v
            continue

        if field.is_relation and field.related_model:
            related_model = field.related_model
            if isinstance(v, list):
                resolved_data[k] = [_related_label(related_model, item_id) for item_id in v]
            else:
                resolved_data[k] = _related_label(related_model, v)
        else:
            resolved_data[k] = v

    # Resolve generic foreign keys if present; an unresolvable target keeps its raw id.
    for gfk in [f for f in model_class._meta.private_fields if isinstance(f, GenericForeignKey)]:
        ct_val = data.get(gfk.ct_field)
        fk_val = data.get(gfk.fk_field)
        if gfk.ct_field not in resolved_data or gfk.fk_field not in resolved_data or not (ct_val and fk_val):
            continue
        try:
            related_model = ContentType.objects.get(pk=ct_val).model_class()
            if related_model is not None:
                resolved_data[gfk.fk_field] = str(related_model.objects.get(pk=fk_val))
        except _LOOKUP_ERRORS:
            continue

    return resolved_data


@method_decorator(login_required, name="dispatch")
class ObjectChangeView(BaseHTMXView, DetailView):
    model = ObjectChange
    template_name = "core/objectchange/objectchange.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()

        model_class = obj.changed_object_type.model_class()
        prechange_data = resolve_serialized_data(model_class, obj.prechange_data or {})
        postchange_data = resolve_serialized_data(model_class, obj.postchange_data or {})

        prechange_string = json.dumps(prechange_data, cls=DjangoJSONEncoder, indent=2, sort_keys=True)
        postchange_string = json.dumps(postchange_data, cls=DjangoJSONEncoder, indent=2, sort_keys=True)
        prechange_lines = prechange_string.splitlines(keepends=True)
        postchange_lines = postchange_string.splitlines(keepends=True)
        context["diff_lines"] = list(difflib.Differ().compare(prechange_lines, postchange_lines))
        context["prechange_data_json"] = prechange_string
        context["postchange_data_json"] = postchange_string

        diff_added_keys = {
            key for key, value in postchange_data.items() if key not in prechange_data or prechange_data[key] != value
        }
        diff_removed_keys = {
            key for key, value in prechange_data.items() if key not in postchange_data or postchange_data[key] != value
        }
        diff_added = {key: value for key, value in postchange_data.items() if key in diff_added_keys}
        diff_removed = {key: value for key, value in prechange_data.items() if key in diff_removed_keys}
        context["diff_added_json"] = json.dumps(diff_added, cls=DjangoJSONEncoder, indent=2)
        context["diff_removed_json"] = json.dumps(diff_removed, cls=DjangoJSONEncoder, indent=2)

        context["title"] = _("Change #%(pk)s") % {"pk": obj.pk}
        base_breadcrumbs = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse("objectchange_list"), _("Changelog")),
            (None, context["title"]),
        ]
        context["breadcrumbs"] = getattr(self, "get_breadcrumbs", lambda: base_breadcrumbs)()
        context["content_template_name"] = self.template_name
        return context
