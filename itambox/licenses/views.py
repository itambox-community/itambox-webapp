from django.db.models import Prefetch
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models import AssetAssignment
from core.managers import Scope
from inventory.models import Kit
from inventory.tables import KitTable
from itambox.panels import Panel
from itambox.quick_add import QuickAddMixin
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from itambox.views.generic.service_views import GenericTransactionView, SimplePostView

from . import filters, forms, tables
from .models import License, LicenseSeatAssignment
from .services import checkin_license_seat, checkout_license

# =============================================================================
# License Entitlement Views
# =============================================================================


class LicenseListView(ObjectListView):
    queryset = (
        License.objects.for_scope(Scope.current())
        .with_counts()
        .select_related("software", "software__manufacturer", "tenant", "supplier")
        .prefetch_related("tags")
    )
    filterset = filters.LicenseFilterSet
    filterset_form = forms.LicenseFilterForm
    table = tables.LicenseTable
    template_name = "generic/object_list.html"
    action_buttons = ("add", "import", "export")


class LicenseDetailView(ObjectDetailView):
    queryset = License.objects.select_related("software", "software__manufacturer", "tenant").prefetch_related("tags")
    template_name = "licenses/license_detail.html"

    layout = (
        ((Panel("metrics", _("License Overview")),),),
        ((Panel("info", _("License Details")),),),
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        license_obj = self.get_object()

        # Query all seat assignments (to either assets or holders). Prefetch each
        # asset's active assignment + its holder so the "Asset Holder" column can
        # resolve an asset-targeted seat's effective holder without N+1 queries.

        active_asset_assignments = (
            AssetAssignment.objects.for_scope(Scope.current()).filter(is_active=True).select_related("assigned_user")
        )
        assignments_qs = license_obj.assignments.select_related("asset", "assigned_holder", "license").prefetch_related(
            Prefetch("asset__assignments", queryset=active_asset_assignments, to_attr="prefetched_active_assignments")
        )
        assignments_table = tables.LicenseSeatAssignmentTable(assignments_qs, request=self.request)
        assignments_table.configure(self.request)
        context["assignments_table"] = assignments_table

        # Kits

        kits_qs = Kit.objects.for_scope(Scope.current()).filter(items__license=license_obj).distinct()
        kits_table = KitTable(kits_qs, request=self.request)
        kits_table.configure(self.request)
        context["kits_table"] = kits_table

        related_objects_list = []
        assignment_count = assignments_qs.count()
        if assignment_count:
            related_objects_list.append(
                {
                    "label": _("Seat Assignments"),
                    "count": assignment_count,
                    "url": f"{reverse('licenses:license_detail', kwargs={'pk': license_obj.pk})}#seats",
                }
            )
        kit_count = kits_qs.count()
        if kit_count:
            related_objects_list.append(
                {
                    "label": _("Kits"),
                    "count": kit_count,
                    "url": f"{reverse('inventory:kit_list')}?license={license_obj.pk}",
                }
            )
        context["related_objects_list"] = related_objects_list

        return context


class LicenseEditView(ObjectEditView):
    queryset = License.objects.all()
    model_form = forms.LicenseForm
    template_name = "generic/object_edit.html"
    default_return_url = "licenses:license_list"


class LicenseSeatAssignmentEditView(QuickAddMixin, ObjectEditView):
    """Assign/edit a license seat on an asset (quick-add modal from the asset
    detail Licenses tab; mirrors WarrantyEditView)."""

    queryset = LicenseSeatAssignment.objects.all()
    model = LicenseSeatAssignment
    model_form = forms.LicenseSeatAssignmentForm
    template_name = "generic/object_edit.html"
    default_return_url = "licenses:license_list"
    quick_add_reload = True

    def get_initial(self):
        initial = super().get_initial()
        asset_id = self.request.GET.get("asset")
        if asset_id:
            initial["asset"] = asset_id
        return initial


class LicenseDeleteView(ObjectDeleteView):
    queryset = License.objects.all()
    default_return_url = "licenses:license_list"


class LicenseCloneView(ObjectCloneView):
    model = License
    model_form = forms.LicenseForm
    template_name = "generic/object_edit.html"
    default_return_url = "licenses:license_list"

    def pre_save_clone(self, original, cloned):
        cloned.product_key = ""


class LicenseBulkEditView(ObjectBulkEditView):
    queryset = License.objects.all()


class LicenseBulkDeleteView(ObjectBulkDeleteView):
    queryset = License.objects.all()


class LicenseCheckoutView(GenericTransactionView):
    permission_required = ("licenses.change_license",)
    queryset = License.objects.all()
    model_form = forms.LicenseCheckOutForm
    service_callable = checkout_license
    context_object_name = "license"
    template_name = "licenses/includes/license_checkout_modal.html"
    error_partial = "licenses/includes/license_checkout_modal.html#checkout-modal-form"
    success_message = _("License checked out successfully.")
    hx_trigger = "licenseUpdated"
    form_field_map = {}
    form_exclude_fields = ("target_type",)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        del kwargs["instance"]
        kwargs["license"] = self.get_object()
        return kwargs

    def get_success_message(self, result=None):
        target = result.asset or result.assigned_holder
        return _("License seat for '%(name)s' checked out to %(target)s.") % {
            "name": self.get_object().name,
            "target": target,
        }


class LicenseCheckinView(SimplePostView):
    permission_required = ("licenses.delete_licenseseatassignment",)
    queryset = LicenseSeatAssignment.objects.select_related("license", "asset", "assigned_holder").all()
    hx_trigger = "licenseUpdated"

    def perform_action(self, assignment, request):
        return checkin_license_seat(assignment, user=request.user)
