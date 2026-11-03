from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from core.managers import Scope
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

from ..filters import ContactFilterSet
from ..forms import ContactFilterForm, ContactForm
from ..models import Contact
from ..tables import ContactAssignmentTable, ContactTable


class ContactListView(ObjectListView):
    queryset = Contact.objects.prefetch_related("tags")
    filterset = ContactFilterSet
    filterset_form = ContactFilterForm
    table = ContactTable
    action_buttons = ("add",)


class ContactDetailView(ObjectDetailView):
    queryset = Contact.objects.prefetch_related("tags")

    layout = (((Panel("info", _("Contact Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contact = self.get_object()

        assignments = contact.assignments.visible_in(Scope.current())
        assignments_table = ContactAssignmentTable(assignments, request=self.request)
        assignments_table.configure(self.request)

        context["assignments_table"] = assignments_table
        return context


class ContactEditView(ObjectEditView):
    queryset = Contact.objects.all()
    model = Contact
    model_form = ContactForm
    template_name = "generic/object_edit.html"


class ContactCloneView(ObjectCloneView):
    model = Contact
    model_form = ContactForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:contact_list"


class ContactDeleteView(ObjectDeleteView):
    queryset = Contact.objects.all()
    model = Contact
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:contact_list")

    def post(self, request, *args, **kwargs):
        contact = self.get_object()
        # Every assignment blocks deletion (they cascade with the contact); only
        # the ones visible in the active scope may be counted in the message.
        assignment_count = contact.assignments.count()

        if assignment_count > 0:
            visible_count = contact.assignments.visible_in(Scope.current()).count()
            if visible_count == assignment_count:
                message = _("Cannot delete contact '%(contact)s': It has %(count)d assignment%(plural)s.") % {
                    "contact": contact,
                    "count": assignment_count,
                    "plural": "s" if assignment_count != 1 else "",
                }
            else:
                message = _("Cannot delete contact '%(contact)s': It has assignments outside your scope.") % {
                    "contact": contact
                }
            messages.error(request, message)
            return redirect(contact.get_absolute_url())

        return super().post(request, *args, **kwargs)


class ContactBulkEditView(ObjectBulkEditView):
    queryset = Contact.objects.all()


class ContactBulkDeleteView(ObjectBulkDeleteView):
    queryset = Contact.objects.all()
