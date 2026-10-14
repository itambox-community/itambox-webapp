import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from core.managers import Scope
from extras.models import JournalEntry
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from itambox.views.generic.authorization import SecuredObjectActionMixin
from itambox.views.generic.utils import safe_return_url

from . import filters, forms, tables
from .models import Subscription, SubscriptionAssignment
from .seat_services import count_assigned_seats


def _lifecycle_error_response(request, error):
    message = " ".join(error.messages)
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"showMessage": {"message": message, "level": "danger"}})
        return response
    return HttpResponseBadRequest(message)


# =============================================================================
# Subscription Views
# =============================================================================


class SubscriptionListView(ObjectListView):
    queryset = Subscription.objects.select_related("supplier", "linked_contract", "tenant", "owner").prefetch_related(
        "tags"
    )
    filterset = filters.SubscriptionFilterSet
    filterset_form = forms.SubscriptionFilterForm
    table = tables.SubscriptionTable
    action_buttons = ("add",)


class SubscriptionDetailView(ObjectDetailView):
    queryset = Subscription.objects.select_related("supplier", "linked_contract", "tenant", "owner").prefetch_related(
        "tags", "assignments"
    )
    template_name = "subscriptions/subscription_detail.html"

    layout = (((Panel("info", _("Subscription Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        subscription = self.get_object()

        # Resolve seat usage once: the template shows it as both assigned and available values.
        assigned_seats = count_assigned_seats(subscription)
        total_seats = subscription.total_seats
        context["assigned_seats"] = assigned_seats
        context["total_seats"] = total_seats
        context["available_seats"] = max(0, total_seats - assigned_seats)

        assignments_qs = subscription.assignments.select_related("assigned_by", "content_type")

        # 1. Map content types to primary keys to batch load related models in 1 query per type
        gfk_mapping = {}
        for assignment in assignments_qs:
            gfk_mapping.setdefault(assignment.content_type, []).append(assignment.object_id)

        # Batch execute query scans per unique model class
        prefetch_cache = {}
        for content_type, object_ids in gfk_mapping.items():
            model_class = content_type.model_class()
            if model_class:
                prefetch_cache[content_type] = {obj.pk: obj for obj in model_class.objects.filter(pk__in=object_ids)}

        # 2. Re-assign resolved target objects onto assignments in-memory
        for assignment in assignments_qs:
            assignment.assigned_object = prefetch_cache.get(assignment.content_type, {}).get(assignment.object_id)

        context["assignments_table"] = tables.SubscriptionAssignmentTable(assignments_qs, request=self.request)
        context["assignments_table"].configure(self.request)
        # assignments_qs is already fully materialized by the loops above, so
        # len() reuses the result cache instead of issuing a second COUNT query.
        context["assignment_count"] = len(assignments_qs)
        return context


class SubscriptionEditView(ObjectEditView):
    queryset = Subscription.objects.all()
    model_form = forms.SubscriptionForm
    template_name = "generic/object_edit.html"
    default_return_url = "subscriptions:subscription_list"


class SubscriptionDeleteView(ObjectDeleteView):
    queryset = Subscription.objects.all()
    default_return_url = "subscriptions:subscription_list"


class SubscriptionCloneView(SubscriptionEditView, ObjectCloneView):
    model = Subscription


class SubscriptionBulkEditView(ObjectBulkEditView):
    queryset = Subscription.objects.all()
    form_class = forms.SubscriptionBulkEditForm


class SubscriptionBulkDeleteView(ObjectBulkDeleteView):
    queryset = Subscription.objects.all()


class SubscriptionRenewView(SecuredObjectActionMixin, LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "subscriptions.change_subscription"
    queryset = Subscription.objects.all()
    template_name = "subscriptions/includes/subscription_renew_modal.html"

    def get(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        form = forms.SubscriptionRenewForm(subscription=subscription)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "subscription": subscription,
            },
        )

    def post(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        form = forms.SubscriptionRenewForm(request.POST, subscription=subscription)
        if form.is_valid():
            renewal_date = form.cleaned_data["renewal_date"]
            renewal_cost = form.cleaned_data["renewal_cost"]
            try:
                changed = subscription.renew(renewal_date, renewal_cost)
            except ValidationError as error:
                form.add_error(None, error)
                return render(
                    request,
                    self.template_name,
                    {"form": form, "subscription": subscription},
                )

            if changed:
                obj_type = ContentType.objects.get_for_model(Subscription)
                JournalEntry.objects.create(
                    model=obj_type,
                    object_id=subscription.pk,
                    user=request.user,
                    comment=(
                        "Renewed subscription. Next renewal date: %(renewal_date)s. "
                        "Cost: %(renewal_cost)s %(currency)s."
                    )
                    % {
                        "renewal_date": renewal_date,
                        "renewal_cost": renewal_cost if renewal_cost is not None else "Not set",
                        "currency": subscription.currency,
                    },
                )

            messages.success(request, _("Subscription '%(name)s' renewed successfully.") % {"name": subscription.name})
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps(
                    {
                        "tableRefreshRequired": None,
                        "showMessage": {
                            "message": str(
                                _("Subscription '%(name)s' renewed successfully.") % {"name": subscription.name}
                            ),
                            "level": "success",
                        },
                    }
                )
                return response
            return redirect(subscription.get_absolute_url())

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "subscription": subscription,
            },
        )


class SubscriptionCancelView(SecuredObjectActionMixin, LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "subscriptions.change_subscription"
    queryset = Subscription.objects.all()
    template_name = "subscriptions/includes/subscription_cancel_modal.html"

    def get(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        form = forms.SubscriptionCancelForm()
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "subscription": subscription,
            },
        )

    def post(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        form = forms.SubscriptionCancelForm(request.POST)
        if form.is_valid():
            cancellation_date = form.cleaned_data["cancellation_date"]
            reason = form.cleaned_data["reason"]
            try:
                changed = subscription.cancel(cancellation_date, reason)
            except ValidationError as error:
                form.add_error(None, error)
                return render(
                    request,
                    self.template_name,
                    {"form": form, "subscription": subscription},
                )

            if changed:
                obj_type = ContentType.objects.get_for_model(Subscription)
                comment_text = f"Cancelled subscription. Cancellation Date: {cancellation_date}."
                if reason:
                    comment_text += f" Reason: {reason}"
                JournalEntry.objects.create(
                    model=obj_type, object_id=subscription.pk, user=request.user, comment=comment_text
                )

            messages.success(
                request, _("Subscription '%(name)s' cancelled successfully.") % {"name": subscription.name}
            )
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps(
                    {
                        "tableRefreshRequired": None,
                        "showMessage": {
                            "message": str(
                                _("Subscription '%(name)s' cancelled successfully.") % {"name": subscription.name}
                            ),
                            "level": "success",
                        },
                    }
                )
                return response
            return redirect(subscription.get_absolute_url())

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "subscription": subscription,
            },
        )


class SubscriptionSuspendView(SecuredObjectActionMixin, LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "subscriptions.change_subscription"
    queryset = Subscription.objects.all()

    def post(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        try:
            changed = subscription.suspend()
        except ValidationError as error:
            return _lifecycle_error_response(request, error)

        if changed:
            obj_type = ContentType.objects.get_for_model(Subscription)
            JournalEntry.objects.create(
                model=obj_type, object_id=subscription.pk, user=request.user, comment="Suspended subscription."
            )

        messages.success(request, _("Subscription '%(name)s' suspended successfully.") % {"name": subscription.name})
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "tableRefreshRequired": None,
                    "showMessage": {
                        "message": str(
                            _("Subscription '%(name)s' suspended successfully.") % {"name": subscription.name}
                        ),
                        "level": "success",
                    },
                }
            )
            return response
        return redirect(subscription.get_absolute_url())


class SubscriptionResumeView(SecuredObjectActionMixin, LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "subscriptions.change_subscription"
    queryset = Subscription.objects.all()

    def post(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        try:
            changed = subscription.resume()
        except ValidationError as error:
            return _lifecycle_error_response(request, error)

        if changed:
            obj_type = ContentType.objects.get_for_model(Subscription)
            JournalEntry.objects.create(
                model=obj_type, object_id=subscription.pk, user=request.user, comment="Resumed subscription."
            )

        message = _("Subscription '%(name)s' resumed successfully.") % {"name": subscription.name}
        messages.success(request, message)
        if request.htmx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "tableRefreshRequired": None,
                    "showMessage": {"message": str(message), "level": "success"},
                }
            )
            return response
        return redirect(subscription.get_absolute_url())


class SubscriptionCheckoutView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "subscriptions.change_subscription"
    template_name = "subscriptions/includes/subscription_checkout_modal.html"

    def get(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        form = forms.SubscriptionCheckoutForm(subscription=subscription)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "subscription": subscription,
            },
        )

    def post(self, request, pk, *args, **kwargs):
        subscription = get_object_or_404(Subscription.objects.for_scope(Scope.current()).all(), pk=pk)
        form = forms.SubscriptionCheckoutForm(request.POST, subscription=subscription)
        if form.is_valid():
            target_type = form.cleaned_data["target_type"]
            notes = form.cleaned_data["notes"]

            if target_type == "holder":
                target_obj = form.cleaned_data["assigned_holder"]
            elif target_type == "asset":
                target_obj = form.cleaned_data["asset"]
            else:
                target_obj = form.cleaned_data["location"]

            content_type = ContentType.objects.get_for_model(target_obj)

            # Create SubscriptionAssignment
            SubscriptionAssignment.objects.create(
                subscription=subscription,
                content_type=content_type,
                object_id=target_obj.pk,
                assigned_by=request.user,
                notes=notes,
            )

            messages.success(
                request,
                _("Assigned subscription '%(name)s' successfully to %(target)s.")
                % {"name": subscription.name, "target": target_obj},
            )
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps(
                    {
                        "tableRefreshRequired": None,
                        "showMessage": {
                            "message": str(
                                _("Assigned subscription '%(name)s' successfully to %(target)s.")
                                % {"name": subscription.name, "target": target_obj}
                            ),
                            "level": "success",
                        },
                    }
                )
                return response
            return redirect(subscription.get_absolute_url())

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "subscription": subscription,
            },
        )


# =============================================================================
# Subscription Assignment Views
# =============================================================================


class SubscriptionAssignmentCreateView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "subscriptions.add_subscriptionassignment"
    template_name = "subscriptions/subscriptionassignments/subscriptionassignment_form.html"
    quick_add_template = "generic/includes/quick_add_modal.html"

    def _is_quickadd(self):
        return self.request.GET.get("_quickadd") == "1"

    def _render_quickadd(self, request, form):
        # Render the shared quick-add modal. The modal template owns the <form>
        # wrapper, so suppress the crispy helper's own form tag.
        if not hasattr(form, "helper") or form.helper is None:
            from crispy_forms.helper import FormHelper

            form.helper = FormHelper(form)
        form.helper.form_tag = False
        return render(
            request,
            self.quick_add_template,
            {
                "quick_add_form": form,
                "model_label": _("Subscription"),
            },
        )

    def get(self, request, *args, **kwargs):
        content_type_id = request.GET.get("content_type")
        object_id = request.GET.get("object_id")

        if not content_type_id or not object_id:
            return HttpResponseBadRequest(_("Missing content_type or object_id"))

        content_type = get_object_or_404(ContentType, id=content_type_id)
        target_obj = get_object_or_404(content_type.model_class().objects.all(), id=object_id)

        form = forms.SubscriptionAssignmentForm(
            content_type=content_type,
            object_id=object_id,
            cancel_url=target_obj.get_absolute_url(),
            quick_add_dismiss=self._is_quickadd(),
        )
        if self._is_quickadd():
            return self._render_quickadd(request, form)
        context = {
            "form": form,
            "target_obj": target_obj,
            "content_type": content_type,
            "object_id": object_id,
            "title": _("Assign Subscription to %(target)s") % {"target": target_obj},
        }
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        content_type_id = request.POST.get("content_type") or request.GET.get("content_type")
        object_id = request.POST.get("object_id") or request.GET.get("object_id")

        if not content_type_id or not object_id:
            return HttpResponseBadRequest(_("Missing content_type or object_id"))

        content_type = get_object_or_404(ContentType, id=content_type_id)
        target_obj = get_object_or_404(content_type.model_class().objects.all(), id=object_id)

        form = forms.SubscriptionAssignmentForm(
            request.POST,
            content_type=content_type,
            object_id=object_id,
            cancel_url=target_obj.get_absolute_url(),
            quick_add_dismiss=self._is_quickadd(),
        )
        if form.is_valid():
            assignment = form.save(commit=False)
            assignment.assigned_by = request.user
            assignment.save()
            if self._is_quickadd():
                response = HttpResponse(status=204)
                response["HX-Redirect"] = target_obj.get_absolute_url()
                return response
            messages.success(request, _("Assigned subscription successfully to %(target)s.") % {"target": target_obj})
            return redirect(target_obj.get_absolute_url())

        if self._is_quickadd():
            return self._render_quickadd(request, form)
        context = {
            "form": form,
            "target_obj": target_obj,
            "content_type": content_type,
            "object_id": object_id,
            "title": _("Assign Subscription to %(target)s") % {"target": target_obj},
        }
        return render(request, self.template_name, context)


class SubscriptionAssignmentDeleteView(ObjectDeleteView):
    queryset = SubscriptionAssignment.objects.all()
    template_name = "generic/object_confirm_delete.html"

    def get_success_url(self):
        obj = self.object
        target = obj.tenant_safe_assigned_object if obj else None
        if target and hasattr(target, "get_absolute_url"):
            fallback = target.get_absolute_url()
        else:
            fallback = reverse("dashboard")
        # Only honor a caller-supplied return_url when it is same-host (open-redirect guard).
        return safe_return_url(
            self.request,
            self.request.GET.get("return_url") or self.request.POST.get("return_url"),
            fallback,
        )
