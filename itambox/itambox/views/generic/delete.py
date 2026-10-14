import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db import models
from django.db.models import ProtectedError
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView

from core.archive_handlers import AggregateArchiveBlocked, archive_object
from core.forms import ConfirmationForm
from itambox.utils import get_help_url, get_model_viewname
from itambox.views.generic.authorization import PermissionResolver
from itambox.views.generic.mixins import (
    CachedObjectMixin,
    TenantScopingViewMixin,
    is_managed_definition,
    lock_unmanaged_definition,
    user_can_mutate_model,
)
from itambox.views.generic.utils import resolve_view_model, safe_return_url
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)

#: Names listed per blocking model before the rest is only counted.
BLOCKER_NAME_LIMIT = 5


def summarize_blockers(pks_by_model):
    """Turn ``{model: [pk, ...]}`` of blocking rows into display lines.

    A PROTECT check sees every row, including archived (soft-deleted) ones and
    rows outside the operator's tenant scope, so names are resolved through the
    model's scoped default manager: only rows the operator may see are named, and
    the remainder is reported as a count, never as a name.
    """
    lines = []
    for model, pks in pks_by_model.items():
        visible = list(model._default_manager.filter(pk__in=pks)[: BLOCKER_NAME_LIMIT + 1])
        named = ", ".join(str(row) for row in visible[:BLOCKER_NAME_LIMIT])
        hidden = len(pks) - len(visible)
        line = _("%(count)s %(model)s") % {"count": len(pks), "model": model._meta.verbose_name_plural}
        if named:
            line = _("%(line)s: %(names)s") % {"line": line, "names": named}
        if len(visible) > BLOCKER_NAME_LIMIT:
            line = _("%(line)s and more") % {"line": line}
        elif hidden > 0:
            line = _("%(line)s (%(hidden)s archived or outside your scope)") % {"line": line, "hidden": hidden}
        lines.append(line)
    return lines


def protected_blockers(obj):
    """Blocking rows of a direct PROTECT relation to ``obj``, as display lines.

    Uses the base manager on purpose: that is what Django's delete collector
    checks, so archived rows block the same way here as at delete time.
    """
    pks_by_model = {}
    for rel in obj._meta.related_objects:
        if rel.on_delete is not models.PROTECT or not (rel.one_to_many or rel.one_to_one):
            continue
        # unscoped: the PROTECT check itself sees archived and out-of-scope rows, so the count must too; only
        # primary keys leave this query and names are resolved through the scoped manager.
        pks = list(rel.related_model._base_manager.filter(**{rel.field.name: obj}).values_list("pk", flat=True))
        if pks:
            pks_by_model.setdefault(rel.related_model, []).extend(pks)
    return summarize_blockers(pks_by_model)


class ObjectDeleteView(
    TenantScopingViewMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, CachedObjectMixin, DeleteView
):
    template_name = "generic/object_confirm_delete.html"
    form_class = ConfirmationForm

    def has_permission(self):
        if not user_can_mutate_model(self.request.user, resolve_view_model(self)):
            return False
        if self.kwargs.get("pk") or self.kwargs.get("slug"):
            try:
                if is_managed_definition(self.get_object()):
                    return False
            except Http404:
                raise
        perms = self.get_permission_required()
        return self.request.user.has_perms(perms, obj=PermissionResolver.object_under_check(self))

    def get_permission_required(self):
        return PermissionResolver.model_permissions(resolve_view_model(self), "delete")

    def get_success_url(self):
        if hasattr(self, "default_return_url") and self.default_return_url:
            fallback = reverse(self.default_return_url)
        elif hasattr(self, "success_url") and self.success_url:
            fallback = str(self.success_url)
        else:
            try:
                list_view_name = get_model_viewname(self.object.__class__, "list")
                fallback = reverse(list_view_name)
            except NoReverseMatch:
                fallback = reverse("dashboard")
        return safe_return_url(self.request, self.request.POST.get("return_url"), fallback)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.object
        return kwargs

    def form_valid(self, form):
        obj_repr = self.get_object_display()
        model = self.object.__class__
        try:
            with lock_unmanaged_definition(self.object) as locked_object:
                self.object = locked_object
                archive_object(self.object, actor=self.request.user, request=self.request)
            messages.success(
                self.request,
                _("Deleted %(model)s %(object)s.") % {"model": model._meta.verbose_name, "object": obj_repr},
            )
            return HttpResponseRedirect(self.get_success_url())
        except AggregateArchiveBlocked as exc:
            # A typed refusal from the object's own aggregate service (the
            # reason it cannot be archived belongs to that domain): report it
            # and leave the object untouched.
            messages.error(self.request, exc.user_message)
            if hasattr(self.object, "get_absolute_url"):
                return redirect(self.object.get_absolute_url())
            return redirect(self.get_success_url())
        except ProtectedError as e:
            # Re-render the confirmation page with the blockers named inline:
            # a redirect plus an auto-hiding toast is easy to miss entirely.
            pks_by_model = {}
            for row in e.protected_objects:
                pks_by_model.setdefault(row.__class__, []).append(row.pk)
            context = self.get_context_data(form=form, blockers=summarize_blockers(pks_by_model))
            return self.render_to_response(context, status=409)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        model = self.object.__class__ if self.object else (self.model or None)
        if model is None:
            raise ValueError("Cannot determine model for delete view.")
        context["model"] = self.model or model
        if "blockers" not in context:
            context["blockers"] = protected_blockers(self.object)
        context["verbose_name"] = model._meta.verbose_name
        object_display = self.get_object_display()
        context["object_display"] = object_display
        context["title"] = _("Delete {verbose_name}: {object}").format(
            verbose_name=model._meta.verbose_name, object=object_display
        )

        if hasattr(self.object, "get_absolute_url"):
            context["cancel_url"] = self.object.get_absolute_url()
        else:
            try:
                list_view_name = get_model_viewname(model, "list")
                context["cancel_url"] = reverse(list_view_name)
            except NoReverseMatch:
                context["cancel_url"] = reverse("dashboard")
        context["return_url"] = safe_return_url(self.request, self.request.GET.get("return_url"), context["cancel_url"])

        base_breadcrumbs = [
            (reverse("dashboard"), _("Dashboard")),
            (context["cancel_url"], model._meta.verbose_name_plural),
            (None, _("Delete {object}").format(object=object_display)),
        ]
        context["breadcrumbs"] = getattr(self, "get_breadcrumbs", lambda: base_breadcrumbs)()
        context["help_url"] = get_help_url(self, model._meta.app_label, model._meta.model_name)
        return context

    def get_object_display(self):
        """Return the presentation label without changing the model string contract."""
        return str(self.object)
