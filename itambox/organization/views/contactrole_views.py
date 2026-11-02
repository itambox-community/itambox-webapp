from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

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
from itambox.views.generic.utils import safe_return_url

from ..filters import ContactRoleFilterSet
from ..forms import ContactAssignmentForm, ContactRoleFilterForm, ContactRoleForm
from ..forms.contactrole_form import resolve_assignment_target
from ..models import ContactAssignment, ContactRole
from ..tables import ContactAssignmentTable, ContactRoleTable


class ContactRoleListView(ObjectListView):
    queryset = ContactRole.objects.all()
    filterset = ContactRoleFilterSet
    filterset_form = ContactRoleFilterForm
    table = ContactRoleTable
    action_buttons = ("add",)


class ContactRoleDetailView(ObjectDetailView):
    queryset = ContactRole.objects.prefetch_related("assignments")

    layout = (((Panel("info", _("Contact Role Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        role = self.get_object()

        assignments_table = ContactAssignmentTable(role.assignments.all(), request=self.request)
        assignments_table.configure(self.request)

        context["assignments_table"] = assignments_table
        return context


class ContactRoleEditView(ObjectEditView):
    queryset = ContactRole.objects.all()
    model = ContactRole
    model_form = ContactRoleForm
    template_name = "generic/object_edit.html"


class ContactRoleCloneView(ObjectCloneView):
    model = ContactRole
    model_form = ContactRoleForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:contactrole_list"


class ContactRoleDeleteView(ObjectDeleteView):
    queryset = ContactRole.objects.all()
    model = ContactRole
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:contactrole_list")

    def post(self, request, *args, **kwargs):
        role = self.get_object()
        assignment_count = role.assignments.count()

        if assignment_count > 0:
            messages.error(
                request,
                _("Cannot delete role '%(role)s': It is associated with %(count)d contact assignment%(plural)s.")
                % {
                    "role": role,
                    "count": assignment_count,
                    "plural": "s" if assignment_count != 1 else "",
                },
            )
            return redirect(role.get_absolute_url())

        return super().post(request, *args, **kwargs)


class ContactAssignmentCreateView(LoginRequiredMixin, View):
    template_name = "organization/contactassignments/contactassignment_form.html"
    permission_required = "organization.add_contactassignment"

    def dispatch(self, request, *args, **kwargs):
        # Anonymous callers are redirected to login by LoginRequiredMixin first.
        if request.user.is_authenticated and not request.user.has_perm(self.permission_required):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def _resolve_target(self, request, content_type_id, object_id):
        """Resolve the target through its scoped manager; out of scope is 404.

        The object-level permission is checked against the target (and thus its
        own tenant), not only against the ambient membership.
        """
        try:
            content_type = get_object_or_404(ContentType, id=content_type_id)
        except (TypeError, ValueError):
            raise Http404 from None
        target_obj = resolve_assignment_target(content_type, object_id)
        if target_obj is None:
            raise Http404
        if not request.user.has_perm(self.permission_required, obj=target_obj):
            raise PermissionDenied
        return content_type, target_obj

    def get(self, request, *args, **kwargs):
        content_type_id = request.GET.get("content_type")
        object_id = request.GET.get("object_id")

        if not content_type_id or not object_id:
            return HttpResponseBadRequest(_("Missing content_type or object_id"))

        content_type, target_obj = self._resolve_target(request, content_type_id, object_id)

        form = ContactAssignmentForm(content_type=content_type, object_id=object_id)
        context = {
            "form": form,
            "target_obj": target_obj,
            "content_type": content_type,
            "object_id": object_id,
        }
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        content_type_id = request.POST.get("content_type") or request.GET.get("content_type")
        object_id = request.POST.get("object_id") or request.GET.get("object_id")

        if not content_type_id or not object_id:
            return HttpResponseBadRequest(_("Missing content_type or object_id"))

        content_type, target_obj = self._resolve_target(request, content_type_id, object_id)

        form = ContactAssignmentForm(request.POST, content_type=content_type, object_id=object_id)
        if form.is_valid():
            form.save()
            messages.success(request, _("Assigned contact successfully to %(target)s.") % {"target": target_obj})
            return redirect(target_obj.get_absolute_url())

        context = {
            "form": form,
            "target_obj": target_obj,
            "content_type": content_type,
            "object_id": object_id,
        }
        return render(request, self.template_name, context)


class ContactAssignmentDeleteView(ObjectDeleteView):
    queryset = ContactAssignment.objects.all()
    model = ContactAssignment
    template_name = "generic/object_confirm_delete.html"

    def get_success_url(self):
        obj = self.object
        if obj and obj.assigned_object and hasattr(obj.assigned_object, "get_absolute_url"):
            fallback = obj.assigned_object.get_absolute_url()
        else:
            fallback = reverse("dashboard")
        # Only honor a caller-supplied return_url when it is same-host (open-redirect guard).
        return safe_return_url(
            self.request,
            self.request.GET.get("return_url") or self.request.POST.get("return_url"),
            fallback,
        )


class ContactRoleBulkEditView(ObjectBulkEditView):
    queryset = ContactRole.objects.all()


class ContactRoleBulkDeleteView(ObjectBulkDeleteView):
    queryset = ContactRole.objects.all()
