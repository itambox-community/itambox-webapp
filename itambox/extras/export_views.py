"""Export-template and label presentation owned by extras."""

import csv
import importlib
import logging

import yaml
from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from core.csv_utils import csv_safe
from core.data_transfer import SCOPE_CONTAINER, SCOPE_OWNER, policy_for, required_export_permissions
from core.managers import Scope
from extras.forms import ExportTemplateForm, LabelTemplateForm
from extras.models import ExportTemplate, LabelTemplate
from extras.tables import ExportTemplateTable, LabelTemplateTable
from itambox.panels import Panel
from itambox.registry import registry
from itambox.views.generic import ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView
from itambox.views.generic.utils import safe_return_url
from organization.services import visible_to_containers

logger = logging.getLogger(__name__)


def get_filterset_for_model(model):
    filterset = registry.get_filter_set(model)
    if filterset:
        return filterset

    app_label = model._meta.app_label
    model_name = model._meta.model_name
    try:
        filters_module = importlib.import_module(f"{app_label}.filters")
        for attr_name in dir(filters_module):
            if attr_name.lower() == f"{model_name}filterset":
                return getattr(filters_module, attr_name)
    except ImportError:
        # The app ships no filters module.
        return None
    return None


_REDACTED_EXPORT_FIELD_SUBSTRINGS = ("secret", "password", "token")


def _is_export_value_redacted(field_name, value):
    name = field_name.lower()
    if any(substring in name for substring in _REDACTED_EXPORT_FIELD_SUBSTRINGS):
        return True
    return isinstance(value, str) and value.startswith("enc$")


def _scoped_export_base(request, model, policy):
    """Rows the requester may see under the model's declared export scope."""
    meta = model._meta
    queryset = model.objects.all()
    if hasattr(queryset, "for_scope"):
        queryset = queryset.for_scope(Scope.current())
    if policy.scope == SCOPE_CONTAINER:
        return visible_to_containers(request.user, queryset, f"{meta.app_label}.view_{meta.model_name}")
    if policy.scope == SCOPE_OWNER:
        return queryset.filter(**{policy.owner_field: request.user})
    return queryset


def _get_export_queryset(request, model, policy, export_scope):
    queryset = _scoped_export_base(request, model, policy)
    pks = request.GET.get("pk", "")
    if pks:
        valid_pks = [int(pk) for pk in pks.split(",") if pk.strip().isdigit()]
        if not valid_pks:
            return None
        return queryset.filter(pk__in=valid_pks)
    if export_scope == "filtered":
        filterset_class = get_filterset_for_model(model)
        if filterset_class:
            filterset = filterset_class(request.GET, queryset=queryset)
            if filterset.is_valid():
                queryset = filterset.qs
    return queryset


def _render_yaml_export(model, model_name, queryset):
    fields = [field for field in model._meta.fields if not field.many_to_many]
    aliases = getattr(model, "export_aliases", {})
    export_data = []
    for obj in queryset:
        row_dict = {}
        for field in fields:
            value = getattr(obj, field.name)
            if _is_export_value_redacted(field.name, value):
                row_dict[field.name] = "***"
                continue
            if value is None:
                value = ""
            elif isinstance(value, (int, float, bool)):
                row_dict[field.name] = value
            else:
                row_dict[field.name] = str(value)
        for alias, source in aliases.items():
            row_dict[alias] = getattr(obj, source)
        export_data.append(row_dict)

    yaml_content = yaml.safe_dump(export_data, default_flow_style=False, sort_keys=False)
    response = HttpResponse(yaml_content, content_type="text/yaml")
    response["Content-Disposition"] = f'attachment; filename="{model_name}_export.yaml"'
    return response


def _render_csv_export(model, model_name, queryset):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{model_name}_export.csv"'
    writer = csv.writer(response)
    fields = [field for field in model._meta.fields if not field.many_to_many]
    aliases = getattr(model, "export_aliases", {})
    writer.writerow([field.name for field in fields] + list(aliases))
    for obj in queryset:
        row = []
        for field in fields:
            value = getattr(obj, field.name)
            if _is_export_value_redacted(field.name, value):
                row.append("***")
                continue
            row.append(csv_safe(value))
        row.extend(csv_safe(getattr(obj, source)) for source in aliases.values())
        writer.writerow(row)
    return response


def _render_template_export(request, model, queryset, template_id):
    content_type = ContentType.objects.get_for_model(model)
    template = get_object_or_404(ExportTemplate, pk=template_id, content_type=content_type)
    try:
        content = template.render(queryset)
    except Exception as exc:  # broad except: render-degrade: turn author template failures into a safe redirect
        logger.warning("Export template %s render failed: %s", template.pk, exc)
        messages.error(
            request,
            _('The export template "%(name)s" could not be rendered. Try again or contact an administrator.')
            % {"name": template.name},
        )
        return HttpResponseRedirect(
            safe_return_url(request, request.META.get("HTTP_REFERER"), template.get_absolute_url())
        )

    response = HttpResponse(content, content_type=template.mime_type or ExportTemplate.DEFAULT_MIME_TYPE)
    response["X-Content-Type-Options"] = "nosniff"
    if template.as_attachment:
        response["Content-Disposition"] = 'attachment; filename="{}"'.format(template.get_export_filename(model))
    return response


class ObjectExportView(LoginRequiredMixin, View):
    """The one generic CSV/YAML/template export route.

    The gate is the model's declared ``core.data_transfer`` policy, not a
    default: an undeclared or denied model answers 404 on every path (CSV, YAML,
    template render, ``export_scope=all`` / ``filtered``, ``pk=`` list). The
    required permission is ``view_<model>`` plus the model's dedicated
    ``export_<model>`` permission where it declares one, so this surface is
    never weaker than a model's dedicated export. Row selection is always the
    policy's declared scope, never a bare ``Model.objects`` for a model whose
    default manager is not authoritative.

    ``export_scope=all`` means every row the requester may see under that scope,
    never every row in the database; an authenticated non-superuser whose
    context resolves no scope holds no permissions there, so the gate fails
    closed (404) instead of exporting unfiltered rows.
    """

    def get(self, request, app_label, model_name, template_id):
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            raise Http404 from None
        policy = policy_for(model)
        if not policy.export:
            raise Http404
        if not all(request.user.has_perm(perm) for perm in required_export_permissions(model)):
            raise Http404

        export_format = request.GET.get("format", "csv").lower()
        export_scope = request.GET.get("export_scope", "all").lower()
        queryset = _get_export_queryset(request, model, policy, export_scope)
        if queryset is None:
            return HttpResponseBadRequest(_("One or more selected IDs are invalid."))

        if template_id == 0:
            if export_format == "yaml":
                return _render_yaml_export(model, model_name, queryset)
            return _render_csv_export(model, model_name, queryset)
        return _render_template_export(request, model, queryset, template_id)


@method_decorator(login_required, name="dispatch")
class ExportTemplateListView(ObjectListView):
    queryset = ExportTemplate.objects.select_related("content_type")
    table = ExportTemplateTable
    template_name = "generic/object_list.html"
    action_buttons = ("add",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Export Templates")
        return context


@method_decorator(login_required, name="dispatch")
class ExportTemplateDetailView(ObjectDetailView):
    queryset = ExportTemplate.objects.select_related("content_type")
    layout = (((Panel("info", _("Export Template Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        context["title"] = str(obj)
        target_model = obj.content_type.model_class()
        if target_model is not None:
            app_label = target_model._meta.app_label
            model_name = target_model._meta.model_name
            policy = policy_for(target_model)
            permitted = all(self.request.user.has_perm(perm) for perm in required_export_permissions(target_model))
            # A link to a denied target would be a 404 dead end on this page.
            if policy.export and permitted:
                context["target_app_label"] = app_label
                context["target_model_name"] = model_name
                context["target_model_verbose"] = target_model._meta.verbose_name_plural
        return context


@method_decorator(login_required, name="dispatch")
class ExportTemplateEditView(ObjectEditView):
    queryset = ExportTemplate.objects.all()
    model_form = ExportTemplateForm

    def has_permission(self):
        return self.request.user.is_superuser and super().has_permission()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Export Template") if self.object else _("Create Export Template")
        return context


@method_decorator(login_required, name="dispatch")
class ExportTemplateDeleteView(ObjectDeleteView):
    queryset = ExportTemplate.objects.all()

    def has_permission(self):
        return self.request.user.is_superuser and super().has_permission()


@method_decorator(login_required, name="dispatch")
class LabelTemplateListView(ObjectListView):
    queryset = LabelTemplate.objects.prefetch_related("content_types")
    table = LabelTemplateTable
    template_name = "generic/object_list.html"
    action_buttons = ("add",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Label Templates")
        return context


@method_decorator(login_required, name="dispatch")
class LabelTemplateDetailView(ObjectDetailView):
    queryset = LabelTemplate.objects.all()
    layout = (((Panel("info", _("Label Template Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        context["title"] = str(obj)
        return context


@method_decorator(login_required, name="dispatch")
class LabelTemplateEditView(ObjectEditView):
    queryset = LabelTemplate.objects.all()
    model_form = LabelTemplateForm

    def has_permission(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Label Template") if self.object else _("Create Label Template")
        return context


@method_decorator(login_required, name="dispatch")
class LabelTemplateDeleteView(ObjectDeleteView):
    queryset = LabelTemplate.objects.all()

    def has_permission(self):
        return self.request.user.is_superuser
