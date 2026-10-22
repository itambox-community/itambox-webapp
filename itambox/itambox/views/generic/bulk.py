import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db import transaction
from django.db.models import ProtectedError
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import View
from django.views.generic.base import TemplateResponseMixin

from core.archive_handlers import AggregateArchiveBlocked, archive_object
from core.forms import BulkEditForm
from itambox.views.generic.bulk_policy import (
    BULK_DELETE,
    BULK_EDIT,
    is_managed_definition_model,
    request_selected_bulk_refused,
)
from itambox.views.generic.mixins import (
    BulkViewMixin,
    filter_permitted_rows,
    user_can_mutate_model,
)
from itambox.views.generic.utils import safe_return_url
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)


def _has_managed_definition_rows(queryset, model):
    try:
        model._meta.get_field("management_kind")
    except FieldDoesNotExist:
        return False
    return any(getattr(row, "management_kind", None) != "local" for row in queryset)


def _ensure_unmanaged_definition_rows(queryset, model):
    if _has_managed_definition_rows(queryset, model):
        raise PermissionDenied("Managed definitions cannot be mutated through bulk actions.")


class ObjectBulkEditView(
    BulkViewMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, TemplateResponseMixin, View
):
    queryset = None
    form_class = None
    template_name = "generic/object_bulk_edit.html"
    bulk_action = BULK_EDIT

    def _uses_request_selected_model(self):
        return (
            self.queryset is None
            and getattr(self, "model", None) is None
            and not (
                self.form_class is not None
                and getattr(self.form_class, "_meta", None) is not None
                and getattr(self.form_class._meta, "model", None) is not None
            )
        )

    def has_permission(self):
        model = self._get_model()
        if is_managed_definition_model(model):
            return False
        if (
            self._uses_request_selected_model()
            and model is not None
            and request_selected_bulk_refused(model, self.bulk_action)
        ):
            return False
        return user_can_mutate_model(self.request.user, model) and super().has_permission()

    def get_permission_required(self):
        model = self._get_model()
        if model:
            return (f"{model._meta.app_label}.change_{model._meta.model_name}",)
        return ("",)

    def _get_bulk_edit_form(self, data=None, model=None):
        form_class = getattr(self, "form_class", None) or BulkEditForm
        # Every bulk-edit form derives from BulkEditForm, whose __init__ builds the
        # per-field edit widgets from `model`. Detect the subclass directly rather than
        # introspecting __init__: a subclass with a `*args, **kwargs` signature (e.g.
        # AssetBulkEditForm) hides the `model` parameter from inspect.signature, which
        # silently dropped `model=` and produced a form with no editable fields.
        if issubclass(form_class, BulkEditForm):
            return form_class(data, model=model)
        return form_class(data)

    def post(self, request, *args, **kwargs):
        pks = request.POST.getlist("pk")
        model = self._get_model()
        return_url = safe_return_url(
            request,
            request.POST.get("return_url") or request.META.get("HTTP_REFERER"),
            reverse("dashboard"),
        )
        raw_selected_fields = request.POST.getlist("_selected_fields")
        selected_fields = [f for f in raw_selected_fields if f not in ("add_tags", "remove_tags")]

        if not pks:
            messages.warning(request, _("No %(objects)s were selected.") % {"objects": model._meta.verbose_name_plural})
            return HttpResponseRedirect(return_url)

        # Per-row change-perm enforcement (see filter_permitted_rows): the
        # dispatch gate alone is too coarse inside a multi-tenant group scope.
        queryset, skipped = filter_permitted_rows(
            request.user, self._get_queryset(pks, with_tenant=True), model, "change"
        )
        if skipped:
            messages.warning(
                request,
                _("Skipped %(count)s %(objects)s you do not have permission to change.")
                % {"count": skipped, "objects": model._meta.verbose_name_plural},
            )
        _ensure_unmanaged_definition_rows(queryset, model)
        if not queryset:
            return HttpResponseRedirect(return_url)

        if "_apply" in request.POST:
            form = self._get_bulk_edit_form(request.POST, model)

            if form.is_valid() and (
                selected_fields
                or ("add_tags" in raw_selected_fields and form.cleaned_data.get("add_tags"))
                or ("remove_tags" in raw_selected_fields and form.cleaned_data.get("remove_tags"))
            ):
                # Validate tenant assignment permissions for bulk edits
                if "tenant" in selected_fields and "tenant" in form.cleaned_data:
                    selected_tenant = form.cleaned_data["tenant"]
                    if selected_tenant:
                        app_label = model._meta.app_label
                        model_name = model._meta.model_name
                        perm_codename = f"{app_label}.change_{model_name}"
                        if not request.user.has_perm(perm_codename, obj=selected_tenant):
                            messages.error(
                                request,
                                _("You do not have permission to assign objects to tenant '%(tenant)s'.")
                                % {"tenant": selected_tenant},
                            )
                            form.add_error(
                                "tenant",
                                _("You do not have permission to assign objects to tenant '%(tenant)s'.")
                                % {"tenant": selected_tenant},
                            )
                            context = {
                                "form": form,
                                "model": model,
                                "model_name": f"{model._meta.app_label}.{model._meta.model_name}",
                                "objects": queryset,
                                "object_pks": pks,
                                "show_object_tenant": getattr(request, "active_tenant", None) is None,
                                "return_url": return_url,
                                "selected_fields": selected_fields,
                                "verbose_name": model._meta.verbose_name,
                                "verbose_name_plural": model._meta.verbose_name_plural,
                                "title": _("Bulk Edit %(objects)s")
                                % {"objects": str(model._meta.verbose_name_plural).title()},
                                "breadcrumbs": [
                                    (reverse("dashboard"), _("Dashboard")),
                                    (return_url, str(model._meta.verbose_name_plural).title()),
                                    (None, _("Bulk Edit (%(count)s)") % {"count": len(pks)}),
                                ],
                            }
                            return self.render_to_response(context)

                updated_count = 0

                with transaction.atomic():
                    for obj in queryset:
                        if hasattr(obj, "snapshot") and callable(obj.snapshot):
                            obj.snapshot()

                        for field_name in selected_fields:
                            if field_name in form.cleaned_data:
                                val = form.cleaned_data[field_name]
                                field = model._meta.get_field(field_name)
                                if field.is_relation and val == "":
                                    val = None
                                setattr(obj, field.attname, val)

                        obj.full_clean()
                        obj.save()

                        if hasattr(obj, "tags"):
                            if "add_tags" in raw_selected_fields and form.cleaned_data.get("add_tags"):
                                obj.tags.add(*form.cleaned_data["add_tags"])
                            if "remove_tags" in raw_selected_fields and form.cleaned_data.get("remove_tags"):
                                obj.tags.remove(*form.cleaned_data["remove_tags"])

                        updated_count += 1

                messages.success(
                    request,
                    _("Updated %(count)s %(objects)s.")
                    % {"count": updated_count, "objects": model._meta.verbose_name_plural},
                )
                return HttpResponseRedirect(return_url)
            else:
                if not (selected_fields or form.cleaned_data.get("add_tags") or form.cleaned_data.get("remove_tags")):
                    messages.warning(request, _("No fields or tags were selected for editing."))
                else:
                    for field_name, errors in form.errors.items():
                        field_label = form[field_name].label if field_name in form else field_name
                        messages.error(
                            request,
                            _("Error in field '%(field)s': %(errors)s")
                            % {"field": field_label, "errors": ", ".join(errors)},
                        )
                form = self._get_bulk_edit_form(request.POST if "_apply" in request.POST else None, model)
        else:
            form = self._get_bulk_edit_form(model=model)

        context = {
            "form": form,
            "model": model,
            "model_name": f"{model._meta.app_label}.{model._meta.model_name}",
            "objects": queryset,
            "object_pks": pks,
            "show_object_tenant": getattr(request, "active_tenant", None) is None,
            "return_url": return_url,
            "selected_fields": selected_fields,
            "verbose_name": model._meta.verbose_name,
            "verbose_name_plural": model._meta.verbose_name_plural,
            "title": _("Bulk Edit %(objects)s") % {"objects": str(model._meta.verbose_name_plural).title()},
            "breadcrumbs": [
                (reverse("dashboard"), _("Dashboard")),
                (return_url, str(model._meta.verbose_name_plural).title()),
                (None, _("Bulk Edit (%(count)s)") % {"count": len(pks)}),
            ],
        }
        return self.render_to_response(context)


class ObjectBulkDeleteView(
    BulkViewMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, TemplateResponseMixin, View
):
    queryset = None
    template_name = "generic/object_confirm_bulk_delete.html"
    bulk_action = BULK_DELETE

    def has_permission(self):
        model = self._get_model()
        # The request-selected endpoint refuses the denylisted models with the
        # same 403 as /bulk-edit/: their dedicated delete views carry checks the
        # generic archive path does not.
        if (
            self.queryset is None
            and getattr(self, "model", None) is None
            and model is not None
            and request_selected_bulk_refused(model, self.bulk_action)
        ):
            return False
        return user_can_mutate_model(self.request.user, model) and super().has_permission()

    def get_permission_required(self):
        model = self._get_model()
        if model:
            return (f"{model._meta.app_label}.delete_{model._meta.model_name}",)
        return ("",)

    def _confirm_locked_delete(self, request, pks, model, return_url):
        try:
            with transaction.atomic():
                objects_to_delete, skipped = filter_permitted_rows(
                    request.user,
                    self._get_queryset(pks).select_for_update(),
                    model,
                    "delete",
                )
                if skipped:
                    messages.warning(
                        request,
                        _("Skipped %(count)s %(objects)s you do not have permission to delete.")
                        % {"count": skipped, "objects": model._meta.verbose_name_plural},
                    )
                _ensure_unmanaged_definition_rows(objects_to_delete, model)
                if not objects_to_delete:
                    messages.warning(
                        request,
                        _("No valid %(objects)s selected for deletion.") % {"objects": model._meta.verbose_name_plural},
                    )
                    return HttpResponseRedirect(return_url)
                deleted_count = 0
                for obj in objects_to_delete:
                    if hasattr(obj, "snapshot") and callable(obj.snapshot):
                        obj.snapshot()
                    archive_object(obj, actor=request.user, request=request)
                    deleted_count += 1
        except AggregateArchiveBlocked as exc:
            # An aggregate service refused one of the rows: the whole batch is
            # rolled back (the reason is about the row, not the batch) and the
            # operator sees the typed message.
            messages.error(request, exc.user_message)
            return HttpResponseRedirect(return_url)
        except ProtectedError as exc:
            messages.error(
                request,
                _("Could not delete objects due to protected relationships: %(error)s") % {"error": exc},
            )
            return HttpResponseRedirect(return_url)

        messages.success(
            request,
            _("Successfully deleted %(count)s %(objects)s.")
            % {"count": deleted_count, "objects": model._meta.verbose_name_plural},
        )
        return HttpResponseRedirect(return_url)

    def post(self, request, *args, **kwargs):
        pks = request.POST.getlist("pk")
        model = self._get_model()
        return_url = safe_return_url(
            request,
            request.POST.get("return_url") or request.META.get("HTTP_REFERER"),
            reverse("dashboard"),
        )

        if not pks:
            messages.warning(request, _("No %(objects)s were selected.") % {"objects": model._meta.verbose_name_plural})
            return HttpResponseRedirect(return_url)

        if "_confirm" in request.POST:
            return self._confirm_locked_delete(request, pks, model, return_url)

        # Per-row delete-perm enforcement (see filter_permitted_rows): the
        # dispatch gate alone is too coarse inside a multi-tenant group scope.
        objects_to_delete, skipped = filter_permitted_rows(
            request.user,
            self._get_queryset(pks, with_tenant=True),
            model,
            "delete",
        )
        if skipped:
            messages.warning(
                request,
                _("Skipped %(count)s %(objects)s you do not have permission to delete.")
                % {"count": skipped, "objects": model._meta.verbose_name_plural},
            )

        _ensure_unmanaged_definition_rows(objects_to_delete, model)

        if not objects_to_delete:
            messages.warning(
                request, _("No valid %(objects)s selected for deletion.") % {"objects": model._meta.verbose_name_plural}
            )
            return HttpResponseRedirect(return_url)

        context = {
            "model": model,
            "model_name": f"{model._meta.app_label}.{model._meta.model_name}",
            "model_verbose_name": model._meta.verbose_name,
            "model_verbose_name_plural": model._meta.verbose_name_plural,
            "objects": objects_to_delete,
            "object_pks": pks,
            "show_object_tenant": getattr(request, "active_tenant", None) is None,
            "return_url": return_url,
            "title": _("Confirm Bulk Deletion"),
            "breadcrumbs": [
                (reverse("dashboard"), _("Dashboard")),
                (return_url, str(model._meta.verbose_name_plural).title()),
                (None, _("Delete (%(count)s)") % {"count": len(objects_to_delete)}),
            ],
        }
        return self.render_to_response(context)
