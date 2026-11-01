import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, View

from itambox.views.generic.authorization import SecuredObjectActionMixin
from itambox.views.generic.htmx_responses import error_response, is_htmx_request, success_response
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)


class GenericTransactionView(
    SecuredObjectActionMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, FormView
):
    queryset = None
    model_form = None
    service_callable = None
    context_object_name = "object"
    success_message = _("Operation completed successfully.")
    hx_trigger = "tableRefreshRequired"
    form_field_map = {}
    form_exclude_fields = ()

    def get(self, request, *args, **kwargs):
        try:
            return super().get(request, *args, **kwargs)
        except PermissionDenied as exc:
            if is_htmx_request(request):
                response = self._htmx_error_response(
                    str(exc) or str(_("You do not have permission to perform this action."))
                )
                response.status_code = 403
                return response
            raise

    #: When True, successful HTMX submissions answer with HX-Redirect to the
    #: object's detail page instead of 204 + closeModal/refresh triggers.
    hx_redirect_on_success = False
    #: django-template-partials reference ("template.html#partial-name") rendered
    #: on validation errors for HTMX requests. Returns only the form fragment with
    #: a 422 status, so the modal body is re-swapped without nesting the full modal.
    error_partial = None

    # Authorization, tenant-scoped queryset and the cached object lookup all come
    # from SecuredObjectActionMixin — see itambox/views/generic/authorization.py.

    def get_form_class(self):
        if self.model_form is not None:
            return self.model_form
        return super().get_form_class()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_object()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context[self.context_object_name] = self.get_object()
        return context

    def get_service_kwargs(self, form):
        service_kwargs = {}
        for key, value in form.cleaned_data.items():
            if key in self.form_exclude_fields:
                continue
            mapped_key = self.form_field_map.get(key, key)
            service_kwargs[mapped_key] = value
        return service_kwargs

    def post_service(self, obj, form, result):
        """Hook for subclasses: runs inside the same transaction, after
        ``service_callable`` succeeded."""

    def form_valid(self, form):
        obj = self.get_object()
        try:
            with transaction.atomic():
                result = self.__class__.service_callable(
                    obj, user=self.request.user, request=self.request, **self.get_service_kwargs(form)
                )
                self.post_service(obj, form, result)

            if is_htmx_request(self.request):
                if self.hx_redirect_on_success:
                    # Full navigation follows, so queue a Django message for the
                    # next render instead of a toast trigger.
                    messages.success(self.request, self.get_success_message(result))
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = obj.get_absolute_url()
                    return response
                return self._htmx_success_response(obj, result)
            messages.success(self.request, self.get_success_message(result))
            return redirect(obj.get_absolute_url())

        except PermissionDenied as e:
            if is_htmx_request(self.request):
                response = self._htmx_error_response(str(e) or _("You do not have permission to perform this action."))
                response.status_code = 403
                return response
            raise
        except ValidationError as e:
            for msg in e.messages:
                form.add_error(None, msg)
            return self.form_invalid(form)
        # broad except: render-degrade: the atomic block has rolled back; the action form shows a generic
        # error (the traceback is logged) instead of a broken modal swap
        except Exception:
            logger.exception("Unexpected error in %s.form_valid", self.__class__.__name__)
            form.add_error(None, _("An unexpected error occurred. Please try again or contact support."))
            return self.form_invalid(form)

    def form_invalid(self, form):
        if is_htmx_request(self.request) and self.error_partial:
            response = render(self.request, self.error_partial, self.get_context_data(form=form))
            # 422 signals a validation failure; the client opts this status into
            # swapping (htmx:beforeSwap handler in static/src/state.ts).
            response.status_code = 422
            return response
        return super().form_invalid(form)

    def _htmx_success_response(self, obj, result=None):
        return success_response(self.get_success_message(result), trigger=self.hx_trigger)

    def get_success_message(self, result=None):
        return self.success_message

    def _htmx_error_response(self, message):
        return error_response(message)


class SimplePostView(SecuredObjectActionMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    queryset = None
    hx_trigger = "tableRefreshRequired"

    # A POST-only action view has no form or object fallback, so a route without
    # a pk is a wiring bug rather than a 404.
    strict_pk_required = True

    # Authorization, tenant-scoped queryset and the cached object lookup all come
    # from SecuredObjectActionMixin — see itambox/views/generic/authorization.py.

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        try:
            result = self.perform_action(obj, request)
            if is_htmx_request(request):
                return self._htmx_success_response(obj, result)
            messages.success(request, result.get("message", _("Action completed successfully.")))
            return self.get_success_redirect(obj, result)
        except PermissionDenied as e:
            # A per-object authorization failure raised inside perform_action
            # (views that opt out of declarative perms via permission_required = ()).
            # For HTMX, surface a toast instead of swapping a raw 403 page into the
            # modal; for full-page requests, let the standard 403 handler run.
            if is_htmx_request(request):
                response = self._htmx_error_response(
                    str(e) or str(_("You do not have permission to perform this action."))
                )
                response.status_code = 403
                return response
            raise
        except ValidationError as e:
            if hasattr(e, "message_dict"):
                msg = "; ".join([f"{k}: {', '.join(v)}" for k, v in e.message_dict.items()])
            elif hasattr(e, "messages"):
                msg = "; ".join(e.messages)
            else:
                msg = str(e)

            if is_htmx_request(request):
                return self._htmx_error_response(msg)
            messages.error(request, msg)
            return redirect(obj.get_absolute_url())

    def perform_action(self, obj, request):
        raise NotImplementedError

    def get_success_redirect(self, obj, result):
        return redirect(obj.get_absolute_url())

    def _htmx_success_response(self, obj, result):
        return success_response(result.get("message", _("Done.")), trigger=self.hx_trigger)

    def _htmx_error_response(self, message):
        return error_response(message)
