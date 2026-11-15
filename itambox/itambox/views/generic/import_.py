import logging
import uuid
from typing import cast

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured, PermissionDenied
from django.db import models, transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
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
from core.models import IMPORT_PREVIEW_TTL, ImportPreview, Job
from itambox.utils import model_view_url
from itambox.views.generic.mixins import user_can_mutate_model
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)


MAX_IMPORT_PREVIEWS = 3


class _PreviewAlreadyConsumed(Exception):
    """A confirmation lost the atomic claim: another request consumed the preview first."""


# Test-only seam: called immediately before the conditional claim update so a
# concurrency test can force both confirmations past validation before either
# claims. Production leaves this ``None``.
_BEFORE_PREVIEW_CLAIM = None


def _store_import_preview(request, *, rows, model):
    """Persist one preview row bound to user, tenant and target model; return its id.

    The preview is stored server-side (not only in the session) so confirmation
    can claim it atomically. Storing is also when cleanup happens: this user's
    consumed previews and anything past the TTL are dropped, then the per-user,
    per-tenant set is bounded to the newest ``MAX_IMPORT_PREVIEWS``.
    """
    user = request.user
    current_tenant = get_current_tenant()
    tenant_id = current_tenant.pk if current_tenant else None

    # Opportunistic, bounded cleanup for this user: consumed rows are spent and
    # anything older than the TTL is an abandoned tab.
    cutoff = timezone.now() - IMPORT_PREVIEW_TTL
    ImportPreview.objects.filter(user=user).filter(
        models.Q(consumed_at__isnull=False) | models.Q(created_at__lt=cutoff)
    ).delete()

    preview = ImportPreview.objects.create(
        user=user,
        tenant_id=tenant_id,
        app_label=model._meta.app_label,
        model_name=model._meta.model_name,
        payload=rows,
    )

    # Keep the newest previews only (two tabs plus slack) for this scope; an
    # abandoned tab must not grow the table without bound.
    scoped = ImportPreview.objects.filter(user=user, tenant_id=tenant_id, consumed_at__isnull=True)
    keep = list(scoped.order_by("-created_at").values_list("pk", flat=True)[:MAX_IMPORT_PREVIEWS])
    scoped.exclude(pk__in=keep).delete()

    return preview.pk.hex


def _relation_description(form_cls, name, related_model):
    if name in getattr(form_cls, "natural_key_only_fields", ()):
        return _("Enter the parent record's Slug or Name.")
    description = _("Relation to {model_name} (resolves by ID, Slug or Name).")
    return description.format(model_name=str(related_model._meta.verbose_name))


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

    def _no_data_response(self, request, form):
        return self._render_response(request, form, errors=[_("No import data found. Please upload a file first.")])

    def _consume_preview(self, request, form, model):
        """Load and validate the posted preview; return (preview, tenant, error).

        This only *reads* the row — the single-use claim happens atomically in
        ``_claim_and_create_job`` so a confirmation that fails validation leaves
        the preview confirmable. Binding is checked here, before any claim:
        a foreign user or model raises ``PermissionDenied`` (403); a foreign
        tenant or missing/spent rows return the existing rendered error.
        """
        raw_id = request.POST.get("preview_id", "")
        try:
            preview_id = uuid.UUID(raw_id)
        except (ValueError, TypeError, AttributeError):
            # Malformed id: treat like an unknown preview rather than a crash.
            return None, None, self._no_data_response(request, form)

        preview = ImportPreview.objects.filter(pk=preview_id, consumed_at__isnull=True).first()
        if preview is None:
            # Unknown, already confirmed or evicted preview: the rows are gone,
            # so the only safe answer is to ask for a fresh upload.
            return None, None, self._no_data_response(request, form)
        if preview.user_id != request.user.pk:
            raise PermissionDenied("This import preview belongs to another user.")
        current_tenant = get_current_tenant()
        tenant_id = current_tenant.pk if current_tenant else None
        if preview.tenant_id != tenant_id:
            return (
                None,
                None,
                self._render_response(request, form, errors=[_("Referenced object belongs to another tenant.")]),
            )
        if (preview.app_label, preview.model_name) != (model._meta.app_label, model._meta.model_name):
            raise PermissionDenied("This import preview belongs to another model.")
        if not preview.payload:
            return None, None, self._no_data_response(request, form)
        return preview, current_tenant, None

    def _claim_and_create_job(self, request, preview, model, ct, current_tenant, tenant_id, rows):
        """Atomically claim the preview and create its job in one transaction.

        The conditional update claims the preview only while it is still
        unconsumed and owned by the requester; a racing confirmation matches
        zero rows and raises ``_PreviewAlreadyConsumed``. Because the claim and
        ``Job.objects.create`` share the transaction, a failed job creation
        rolls the claim back so the preview can be retried. Scheduling is
        registered on commit, after the claim is durable.
        """
        with transaction.atomic():
            if _BEFORE_PREVIEW_CLAIM is not None:
                _BEFORE_PREVIEW_CLAIM(preview.pk)
            claimed = ImportPreview.objects.filter(pk=preview.pk, user=request.user, consumed_at__isnull=True).update(
                consumed_at=timezone.now()
            )
            if not claimed:
                raise _PreviewAlreadyConsumed

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
        return job

    def post(self, request, *args, **kwargs):
        form = self.get_form_class()(request.POST, request.FILES)
        # Preview-time checks (e.g. permission to create related objects) run as the importing user.
        form.actor = request.user

        if "_preview" in request.POST:
            if form.is_valid():
                rows = form._rows_data
                model = cast(type[models.Model], self._get_model())
                preview_id = _store_import_preview(request, rows=rows, model=model)
                context = {
                    "form": form,
                    "preview_rows": rows,
                    "preview_id": preview_id,
                    "preview_headers": form.field_names if rows else [],
                    "preview_notes": form.row_notes,
                    "title": self._get_title(),
                    "cancel_url": self._get_cancel_url(),
                }
                return self.render_to_response(context)
            return self._render_response(request, form, errors=[str(e) for e in form.errors.values()])

        if "_confirm" in request.POST:
            model = cast(type[models.Model], self._get_model())
            preview, current_tenant, error = self._consume_preview(request, form, model)
            if error is not None:
                return error
            tenant_id = current_tenant.pk if current_tenant else None
            rows = preview.payload

            ct = ContentType.objects.get_for_model(model)

            try:
                job = self._claim_and_create_job(request, preview, model, ct, current_tenant, tenant_id, rows)
            except _PreviewAlreadyConsumed:
                # Lost the race (or confirmed twice): no job, object or event was
                # created; ask for a fresh upload exactly as for a spent preview.
                return self._no_data_response(request, form)

            messages.success(
                request,
                _("Asynchronous import job '%(name)s' enqueued successfully! Tracking progress in real-time.")
                % {"name": job.name},
            )

            try:
                return redirect("job_detail", pk=job.pk)
            except NoReverseMatch:
                return redirect(f"/jobs/{job.pk}/")

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
        field_accessors = getattr(form_cls, "field_accessors", {}) or {}
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

            accessor, description, choices = self._get_field_details(
                field, name, field_descriptions, field_choices, form_cls
            )
            accessor = field_accessors.get(name, accessor)

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
    def _get_field_details(field, name, field_descriptions, field_choices, form_cls):
        description = field.help_text or field.verbose_name or ""
        description = str(description) if description else ""
        accessor = ""
        choices = []
        if field.is_relation and field.many_to_one:
            related_model = field.related_model
            accessor = "slug" if _model_has_concrete_field(related_model, "slug") else "name"
            description = _relation_description(form_cls, name, related_model)
        field_choices = field_choices or {}
        if name in field_choices:
            choices = field_choices[name]
        elif field.choices:
            choices = [(val, label) for val, label in field.choices]
        if isinstance(field, models.DateField):
            description = f"{description} Format: YYYY-MM-DD"
        elif isinstance(field, models.BooleanField):
            description = _("{description}. Enter true or false.").format(description=description.rstrip(" .!?"))
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
        return (model_view_url(model, "list") if model else None) or reverse("dashboard")

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
