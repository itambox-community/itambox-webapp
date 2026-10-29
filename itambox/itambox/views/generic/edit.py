import logging

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Layout, Submit
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _
from django.views.generic import UpdateView

from itambox.utils import get_help_url, get_model_viewname, model_view_url
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


class ObjectEditView(
    TenantScopingViewMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, CachedObjectMixin, UpdateView
):
    model_form = None
    template_name = "generic/object_edit.html"

    def has_permission(self):
        if not user_can_mutate_model(self.request.user, self._get_model()):
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
        model = self._get_model()
        if model is None:
            return PermissionResolver.model_permissions(None, "change")
        # An existing object is a change; no object (add form, or a clone that is
        # not persisted yet) is a creation. Http404 is swallowed here on purpose —
        # has_permission() re-raises it after the perms are chosen.
        try:
            obj = self.get_object()
        except Http404:
            obj = None
        return PermissionResolver.model_permissions(model, "change" if obj else "add")

    def _get_model(self):
        model = resolve_view_model(self)
        if model is not None:
            return model
        if hasattr(self, "model_form") and self.model_form:
            return self.model_form._meta.model
        if hasattr(self, "form_class") and self.form_class and hasattr(self.form_class, "_meta"):
            return self.form_class._meta.model
        return None

    def get_form_class(self):
        if self.model_form:
            return self.model_form
        return super().get_form_class()

    def get_object(self, queryset=None):
        if "pk" not in self.kwargs and "slug" not in self.kwargs:
            return None
        return super().get_object(queryset)

    def get_form(self, form_class=None):
        kwargs = self.get_form_kwargs()
        kwargs["instance"] = self.object
        if form_class is None:
            form_class = self.get_form_class()
        form = form_class(**kwargs)

        if not hasattr(form, "helper") or form.helper is None:
            helper = FormHelper(form)
            helper.form_method = "post"
            helper.form_tag = True

            is_editing = self.object is not None and self.object.pk is not None
            button_text = _("Update") if is_editing else _("Create")

            cancel_url = None
            if self.object and hasattr(self.object, "get_absolute_url"):
                try:
                    cancel_url = self.object.get_absolute_url()
                except NoReverseMatch:
                    # An unsaved object (pk None) has no detail URL yet.
                    cancel_url = None
            if cancel_url is None:
                _model = self._get_model()
                cancel_url = (model_view_url(_model, "list") if _model else None) or reverse("dashboard")

            layout_elements = list(form.fields.keys())
            layout_elements.extend(
                [
                    HTML('<div class="mt-4"></div>'),
                    Submit("submit", button_text, css_class="btn btn-primary"),
                    HTML(f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2">{_("Cancel")}</a>'),
                ]
            )
            helper.layout = Layout(*layout_elements)
            form.helper = helper

        return form

    def get_success_url(self):
        fallback = None
        if hasattr(self, "default_return_url") and self.default_return_url:
            fallback = reverse(self.default_return_url)
        elif self.object and hasattr(self.object, "get_absolute_url"):
            fallback = self.object.get_absolute_url()
        if fallback is None:
            _model = self._get_model()
            if _model:
                try:
                    list_view_name = get_model_viewname(_model, "list")
                    fallback = reverse(list_view_name)
                except NoReverseMatch:
                    logger.debug("List view URL fallback failed for model %s", _model)
        if fallback is None:
            fallback = reverse("dashboard")
        return safe_return_url(self.request, self.request.POST.get("return_url"), fallback)

    def add_success_message(self, message):
        messages.success(self.request, message)

    def form_valid(self, form):
        # Unsaved instances (new objects and clones) are creations.
        is_creating = self.object is None or self.object.pk is None
        _model = self._get_model()

        # Enforce scoping check on the selected tenant of the object
        if _model:
            app_label = _model._meta.app_label
            model_name = _model._meta.model_name
            selected_tenant = form.cleaned_data.get("tenant")
            if not selected_tenant and hasattr(form.instance, "tenant"):
                selected_tenant = getattr(form.instance, "tenant", None)

            if selected_tenant:
                is_creating_instance = self.object is None or self.object.pk is None
                perm_codename = (
                    f"{app_label}.add_{model_name}" if is_creating_instance else f"{app_label}.change_{model_name}"
                )
                if not self.request.user.has_perm(perm_codename, obj=selected_tenant):
                    form.add_error(
                        "tenant",
                        _("You do not have permission to assign objects to tenant '%(tenant)s'.")
                        % {"tenant": selected_tenant},
                    )
                    return self.form_invalid(form)

        write_target = self.object if self.object is not None else form.instance
        with lock_unmanaged_definition(write_target):
            self.object = form.save()
        msg_verb = _("Created") if is_creating else _("Modified")
        msg_link = (
            f"<a href='{self.object.get_absolute_url()}'>{self.object}</a>"
            if hasattr(self.object, "get_absolute_url")
            else str(self.object)
        )
        self.add_success_message(
            _("%(verb)s %(model)s %(link)s") % {"verb": msg_verb, "model": _model._meta.verbose_name, "link": msg_link},
        )

        next_url = None
        if self.request.POST.get("_addanother") and _model:
            next_url = model_view_url(_model, "add")
        elif self.request.POST.get("_continue") and _model:
            next_url = model_view_url(_model, "edit", pk=self.object.pk)
        if next_url:
            return redirect(next_url)

        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        _model = self._get_model()
        if not _model:
            raise ImproperlyConfigured(f"{self.__class__.__name__} needs a model attribute, or related form/queryset.")

        # A clone is an unsaved instance (pk is None): treat it as creation, not
        # an edit, so we don't reverse get_absolute_url() with a null pk.
        is_editing = self.object is not None and self.object.pk is not None
        context["model"] = _model
        context["verbose_name"] = _model._meta.verbose_name
        context["is_editing"] = is_editing
        action_verb = _("Edit") if is_editing else _("Create")
        context["title"] = f"{action_verb} {context['verbose_name']}"

        if is_editing and hasattr(self.object, "get_absolute_url"):
            context["cancel_url"] = self.object.get_absolute_url()
        else:
            try:
                list_view_name = get_model_viewname(_model, "list")
                context["cancel_url"] = reverse(list_view_name)
            except NoReverseMatch:
                context["cancel_url"] = reverse("dashboard")

        base_breadcrumbs = [
            (reverse("dashboard"), _("Dashboard")),
            (context["cancel_url"], _model._meta.verbose_name_plural),
            (None, context["title"]),
        ]
        context["breadcrumbs"] = getattr(self, "get_breadcrumbs", lambda: base_breadcrumbs)()
        context["help_url"] = get_help_url(self, _model._meta.app_label, _model._meta.model_name)
        return context


class ObjectCloneView(ObjectEditView):
    """Render a create form pre-filled from an existing object.

    The clone is NOT persisted on GET — ``get_object`` returns an *unsaved*
    instance used only to pre-fill the form's fields. The new record is created
    only when the user submits the form (handled by ``ObjectEditView.form_valid``),
    so the user can review and adjust the copied values first.
    """

    def get_object(self, queryset=None):
        source_queryset = queryset if queryset is not None else self.get_queryset()
        self.original_object = get_object_or_404(source_queryset, pk=self.kwargs["pk"])
        cloned = self.original_object.clone()

        if hasattr(cloned, "name"):
            cloned.name = f"{self.original_object.name} (Copy)"
        elif hasattr(cloned, "model"):
            cloned.model = f"{self.original_object.model} (Copy)"

        if hasattr(cloned, "slug"):
            cloned.slug = ""

        self.pre_save_clone(self.original_object, cloned)
        # Intentionally NOT saved here — the form's POST creates the record.
        return cloned

    def get_initial(self):
        # An unsaved instance can't supply its many-to-many values to the form,
        # so seed them (e.g. tags) from the source object as form initial. Only
        # fields actually present on the form are rendered/saved.
        initial = super().get_initial()
        original = getattr(self, "original_object", None)
        if original is not None and original.pk:
            for field in original._meta.many_to_many:
                initial.setdefault(
                    field.name,
                    list(getattr(original, field.name).values_list("pk", flat=True)),
                )
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_clone"] = True
        context["title"] = _("Clone %(name)s") % {"name": context["verbose_name"]}
        return context

    def pre_save_clone(self, original, cloned):
        pass
