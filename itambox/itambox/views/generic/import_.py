import logging
from typing import cast

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models, transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _
from django.views.generic import TemplateView
from django_q.tasks import async_task

from core.importers.bulk_forms import (
    IMPORT_EXCLUDED_FIELDS,
    _model_has_concrete_field,
    get_import_form_class,
    is_model_importable,
)
from core.managers import get_current_tenant
from core.models import Job
from itambox.utils import get_model_viewname
from itambox.views.generic.mixins import user_can_mutate_model
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)


class ObjectImportView(PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, TemplateView):
    model_form = None
    template_name = "generic/object_import.html"

    def has_permission(self):
        model = self._get_model()
        form_class = self.get_form_class() if model else None
        can_import = getattr(form_class, "can_import", None)
        if can_import is not None and not can_import(self.request.user):
            return False
        # Imports currently bind the background job and task to active_tenant;
        # never expose that write path from an aggregate or tenant-group scope.
        if not self.request.user.is_superuser and getattr(self.request, "active_tenant", None) is None:
            return False
        return user_can_mutate_model(self.request.user, model) and super().has_permission()

    def get_permission_required(self):
        model = self._get_model()
        if model:
            return (f"{model._meta.app_label}.add_{model._meta.model_name}",)
        return ("",)

    def _get_model(self):
        form_cls = getattr(self, "model_form", None)
        if form_cls and hasattr(form_cls, "model") and form_cls.model:
            return form_cls.model
        if hasattr(self, "model") and self.model:
            return self.model
        return None

    def get_form_class(self):
        if self.model_form:
            return self.model_form

        model = self._get_model()
        if not model:
            raise ImproperlyConfigured(f"{self.__class__.__name__} needs a model attribute or model_form.")

        return get_import_form_class(model)

    def get(self, request, *args, **kwargs):
        form = self.get_form_class()()
        return self._render_response(request, form)

    def post(self, request, *args, **kwargs):
        form = self.get_form_class()(request.POST, request.FILES)
        # Preview-time checks (e.g. permission to create related objects) run as the importing user.
        form.actor = request.user

        if "_preview" in request.POST:
            if form.is_valid():
                rows = form._rows_data
                request.session["import_rows"] = rows
                request.session["import_delimiter"] = form.cleaned_data.get("delimiter", ",")
                context = {
                    "form": form,
                    "preview_rows": rows,
                    "preview_headers": form.field_names if rows else [],
                    "preview_notes": form.row_notes,
                    "title": self._get_title(),
                    "cancel_url": self._get_cancel_url(),
                }
                return self.render_to_response(context)
            return self._render_response(request, form, errors=[str(e) for e in form.errors.values()])

        if "_confirm" in request.POST:
            rows = request.session.get("import_rows", [])
            if rows:
                model = cast(type[models.Model], self._get_model())
                ct = ContentType.objects.get_for_model(model)

                current_tenant = get_current_tenant()
                tenant_id = current_tenant.pk if current_tenant else None

                # Create background Job tracker instance
                job = Job.objects.create(
                    name=f"Bulk Import: {str(model._meta.verbose_name_plural).title()}",
                    tenant=current_tenant,
                    model=ct,
                    status=Job.STATUS_PENDING,
                )

                if getattr(settings, "Q_CLUSTER", {}).get("sync", False):
                    async_task(
                        "core.tasks.import_csv_task",
                        job.pk,
                        rows,
                        model._meta.app_label,
                        model._meta.model_name,
                        request.user.pk,
                        tenant_id=tenant_id,
                    )
                else:
                    task_args = (
                        "core.tasks.import_csv_task",
                        job.pk,
                        rows,
                        model._meta.app_label,
                        model._meta.model_name,
                        request.user.pk,
                    )
                    task_kwargs = {"tenant_id": tenant_id}
                    transaction.on_commit(lambda args=task_args, kwargs=task_kwargs: async_task(*args, **kwargs))

                messages.success(
                    request,
                    _("Asynchronous import job '%(name)s' enqueued successfully! Tracking progress in real-time.")
                    % {"name": job.name},
                )

                request.session.pop("import_rows", None)
                request.session.pop("import_delimiter", None)

                try:
                    return redirect("job_detail", pk=job.pk)
                except NoReverseMatch:
                    return redirect(f"/jobs/{job.pk}/")
            return self._render_response(request, form, errors=[_("No import data found. Please upload a file first.")])

        return self._render_response(request, form)

    def _get_fields_info(self):
        """Field Options help table, derived from the resolved form's actual
        required_fields/optional_fields so it always matches what is imported
        (not every model field). FK columns advertise their match accessor."""
        model = self._get_model()
        fields_info = []
        if not model:
            return fields_info

        form_cls = self.get_form_class()
        field_descriptions = getattr(form_cls, "field_descriptions", {}) or {}
        field_choices = getattr(form_cls, "field_choices", {}) or {}
        required = set(getattr(form_cls, "required_fields", []) or [])
        field_names = list(getattr(form_cls, "required_fields", []) or []) + list(
            getattr(form_cls, "optional_fields", []) or []
        )

        for name in field_names:
            if name in IMPORT_EXCLUDED_FIELDS:
                continue
            try:
                field = model._meta.get_field(name)
            except FieldDoesNotExist:
                fields_info.append(
                    {
                        "name": name,
                        "required": name in required,
                        "accessor": "",
                        "description": str(field_descriptions.get(name) or ""),
                        "choices": [],
                    }
                )
                continue

            accessor, description, choices = self._get_field_details(field, name, field_descriptions, field_choices)

            fields_info.append(
                {
                    "name": name,
                    "required": name in required,
                    "accessor": accessor,
                    "description": description,
                    "choices": choices,
                }
            )
        return fields_info

    @staticmethod
    def _get_field_details(field, name, field_descriptions, field_choices=None):
        description = field.help_text or field.verbose_name or ""
        description = str(description) if description else ""
        accessor = ""
        choices = []
        if field.is_relation and field.many_to_one:
            related_model = field.related_model
            accessor = "slug" if _model_has_concrete_field(related_model, "slug") else "name"
            description = _("Relation to {model_name} (resolves by ID, Slug or Name).").format(
                model_name=str(related_model._meta.verbose_name)
            )
        field_choices = field_choices or {}
        if name in field_choices:
            choices = field_choices[name]
        elif field.choices:
            choices = [(val, label) for val, label in field.choices]
        if isinstance(field, models.DateField):
            description = f"{description} Format: YYYY-MM-DD"
        elif isinstance(field, models.BooleanField):
            description = f"{description} Specify true or false"
        if name in field_descriptions:
            description = str(field_descriptions[name])
        return accessor, description, choices

    def _render_response(self, request, form, **extra_context):
        context = {
            "form": form,
            "title": self._get_title(),
            "cancel_url": self._get_cancel_url(),
            "fields": self._get_fields_info(),
            "show_generic_reference_help": not getattr(self.get_form_class(), "custom_reference_resolution", False),
            **extra_context,
        }
        return self.render_to_response(context)

    def _get_title(self):
        model = self._get_model()
        if model:
            return _("Import {verbose_name_plural}").format(verbose_name_plural=model._meta.verbose_name_plural)
        return _("Import Objects")

    def _get_cancel_url(self):
        model = self._get_model()
        if model:
            try:
                list_view_name = get_model_viewname(model, "list")
                return reverse(list_view_name)
            except NoReverseMatch:
                pass
        return reverse("dashboard")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs) if hasattr(super(), "get_context_data") else {}
        context["title"] = kwargs.get("title", self._get_title())
        context["cancel_url"] = kwargs.get("cancel_url", self._get_cancel_url())
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (context["cancel_url"], self._get_model()._meta.verbose_name_plural if self._get_model() else _("List")),
            (None, context["title"]),
        ]
        return context

    def render_to_response(self, context, **response_kwargs):
        context["breadcrumbs"] = context.get("breadcrumbs", [])
        return BaseHTMXView.render_to_response(self, context, **response_kwargs)


class GenericObjectImportView(ObjectImportView):
    def _get_model(self):
        app_label = self.kwargs.get("app_label")
        model_name = self.kwargs.get("model_name")
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            raise Http404 from None
        # Only models with a declared, curated import form are importable, even by direct URL.
        if not is_model_importable(model):
            raise Http404
        return model
