"""Core object-change presentation."""

import difflib
import json

from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
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
        except Exception:
            resolved_data[k] = v
            continue

        if field.is_relation and field.related_model:
            related_model = field.related_model
            if isinstance(v, list):
                resolved_list = []
                for item_id in v:
                    try:
                        related_obj = related_model.objects.get(pk=item_id)
                        resolved_list.append(str(related_obj))
                    except Exception:
                        resolved_list.append(f"{related_model._meta.model_name} #{item_id} (deleted)")
                resolved_data[k] = resolved_list
            else:
                try:
                    related_obj = related_model.objects.get(pk=v)
                    resolved_data[k] = str(related_obj)
                except Exception:
                    resolved_data[k] = f"{related_model._meta.model_name} #{v} (deleted)"
        else:
            resolved_data[k] = v

    # Resolve generic foreign keys if present
    try:
        for gfk in [f for f in model_class._meta.private_fields if isinstance(f, GenericForeignKey)]:
            ct_field = gfk.ct_field
            fk_field = gfk.fk_field
            if ct_field in resolved_data and fk_field in resolved_data:
                ct_val = data.get(ct_field)
                fk_val = data.get(fk_field)
                if ct_val and fk_val:
                    try:
                        ct = ContentType.objects.get(pk=ct_val)
                        related_model = ct.model_class()
                        related_obj = related_model.objects.get(pk=fk_val)
                        resolved_data[fk_field] = str(related_obj)
                    except Exception:
                        pass
    except Exception:
        pass

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
