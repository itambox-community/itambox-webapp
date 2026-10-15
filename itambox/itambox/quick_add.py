"""
Quick-Add mixin for FK/M2M field inline creation.

Usage on a CreateView:
    class ManufacturerQuickAddView(QuickAddMixin, ObjectEditView):
        model = Manufacturer
        model_form = ManufacturerForm
        quick_add_target = 'id_manufacturer'  # ID of the parent select to refresh
"""

import json

from django.http import HttpResponse
from django.shortcuts import render

from itambox.views.generic.htmx_responses import is_htmx_request


class QuickAddMixin:
    """
    Mixin that modifies the form response for quick-add scenarios.

    When the form is submitted in a quick-add modal, instead of redirecting,
    returns a script that closes the modal and triggers a select refresh.
    """

    quick_add_target = None
    quick_add_reload = False

    def is_quick_add(self):
        return self.request.GET.get("_quickadd") == "1"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if self.is_quick_add():
            kwargs["quick_add_dismiss"] = True
        return kwargs

    def get_quick_add_redirect_url(self):
        """Where to navigate after a reload-mode quick-add success.
        Default: the new object's parent asset detail, falling back to the object itself."""
        obj = self.object
        asset = getattr(obj, "asset", None)
        if asset is not None:
            return asset.get_absolute_url()
        return obj.get_absolute_url()

    def get_template_names(self):
        if self.is_quick_add():
            return ["generic/includes/quick_add_modal.html"]
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.is_quick_add():
            context["model_label"] = (
                self.model._meta.verbose_name.title() if hasattr(self, "model") and self.model else ""
            )
            form = context.get("form")
            if form:
                if not hasattr(form, "helper") or form.helper is None:
                    from crispy_forms.helper import FormHelper

                    form.helper = FormHelper(form)
                form.helper.form_tag = False
            context["quick_add_form"] = form
        return context

    def get_quick_add_success_response(self):
        if getattr(self, "quick_add_reload", False):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = self.get_quick_add_redirect_url()
            return response
        target = getattr(self, "quick_add_target", None) or ""
        value = str(self.object)
        pk = self.object.pk

        response = HttpResponse("Object created successfully")
        response["HX-Trigger"] = json.dumps(
            {
                "quickAddSuccess": {
                    "target_id": target,
                    "value": value,
                    "pk": str(pk),
                }
            }
        )
        return response

    def form_valid(self, form):
        if self.is_quick_add():
            self.object = form.save()
            return self.get_quick_add_success_response()

        return super().form_valid(form)

    def form_invalid(self, form):
        if self.is_quick_add() and is_htmx_request(self.request):
            context = self.get_context_data(form=form)
            response = render(self.request, "generic/includes/quick_add_form.html", context)
            response.status_code = 422
            return response
        return super().form_invalid(form)
