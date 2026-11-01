from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from compliance.models import CustodyReceipt
from compliance.services import scope_custody_receipts
from compliance.tables import CustodyReceiptTable
from core.managers import Scope
from inventory.models import AccessoryAssignment, ConsumableAssignment
from inventory.tables import AccessoryAssignmentTable, ConsumableAssignmentTable
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from licenses.models import LicenseSeatAssignment
from licenses.tables import LicenseSeatAssignmentTable
from organization.services.archive import ArchiveBlocked, archive_holder
from organization.services.offboarding import get_offboarding_report

from ..filters import AssetHolderFilterSet
from ..forms import AssetHolderFilterForm, AssetHolderForm
from ..models import AssetHolder
from ..tables import (
    AssetAssignmentTable,
    AssetHolderTable,
)


class AssetHolderListView(ObjectListView):
    queryset = (
        AssetHolder.objects.select_related("tenant")
        .prefetch_related("tags")
        .annotate(
            assignment_count=Count("asset_assignments", filter=Q(asset_assignments__is_active=True)),
        )
    )
    filterset = AssetHolderFilterSet
    filterset_form = AssetHolderFilterForm
    table = AssetHolderTable
    action_buttons = ("add",)


class AssetHolderDetailView(ObjectDetailView):
    queryset = AssetHolder.objects.select_related("tenant", "user").prefetch_related(
        "asset_assignments__asset", "asset_assignments__asset__status", "tags"
    )
    related_object_exclusions = ("compliance.custodyreceipt",)

    layout = (((Panel("info", _("Asset Holder Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        assetholder = self.get_object()

        assignments_table = AssetAssignmentTable(assetholder.checked_out_assets, request=self.request)
        assignments_table.configure(self.request)
        context["assignments_table"] = assignments_table

        # Accessory Assignments

        acc_assign_qs = AccessoryAssignment.objects.for_scope(Scope.current()).filter(assigned_holder=assetholder)
        accessory_assignments_table = AccessoryAssignmentTable(acc_assign_qs, request=self.request)
        accessory_assignments_table.configure(self.request)
        context["accessory_assignments_table"] = accessory_assignments_table

        # Consumable Dispatches

        con_assign_qs = ConsumableAssignment.objects.for_scope(Scope.current()).filter(assigned_holder=assetholder)
        consumable_dispatches_table = ConsumableAssignmentTable(con_assign_qs, request=self.request)
        consumable_dispatches_table.configure(self.request)
        context["consumable_dispatches_table"] = consumable_dispatches_table

        # Software License Seats

        lic_assign_qs = LicenseSeatAssignment.objects.for_scope(Scope.current()).filter(assigned_holder=assetholder)
        license_assignments_table = LicenseSeatAssignmentTable(lic_assign_qs, request=self.request)
        license_assignments_table.configure(self.request)
        context["license_assignments_table"] = license_assignments_table

        # Custody receipts

        can_view_receipts = self.request.user.has_perm("compliance.view_custodyreceipt", obj=assetholder)
        context["can_view_custody_receipts"] = can_view_receipts
        custody_receipts_qs = CustodyReceipt.objects.none()
        if can_view_receipts:
            custody_receipts_qs = scope_custody_receipts(
                CustodyReceipt.objects.filter(
                    holder=assetholder,
                    asset__tenant=assetholder.tenant,
                ).select_related("asset", "holder", "custody_template"),
                user=self.request.user,
                permission="compliance.view_custodyreceipt",
            )
            custody_receipts_table = CustodyReceiptTable(custody_receipts_qs, request=self.request)
            custody_receipts_table.configure(self.request)
            context["custody_receipts_table"] = custody_receipts_table

        # Offboarding readiness: a read-only roll-up of every outstanding
        # obligation for this person, composed from existing capabilities.
        # Gated by the view permission this detail view already enforces
        # (staff / a documented per-tenant permission); the report itself is
        # computed per concrete holder, so a person only ever sees their own.
        # Custody items additionally require the receipt permission checked
        # above, exactly like the custody tab on this page.
        offboarding_report = get_offboarding_report(assetholder, include_custody=can_view_receipts)
        context["offboarding_report"] = offboarding_report
        context["offboarding_obligation_count"] = len(offboarding_report.items)

        related_objects_list = []
        asset_count = assetholder.checked_out_assets.count()
        if asset_count:
            related_objects_list.append(
                {
                    "label": _("Assets"),
                    "count": asset_count,
                    "url": f"{reverse('assets:asset_list')}?holder={assetholder.pk}",
                }
            )
        accessory_count = acc_assign_qs.count()
        if accessory_count:
            related_objects_list.append(
                {
                    "label": _("Accessories"),
                    "count": accessory_count,
                    "url": f"{reverse('inventory:accessoryassignment_list')}?assigned_holder={assetholder.pk}",
                }
            )
        consumable_count = con_assign_qs.count()
        if consumable_count:
            related_objects_list.append(
                {
                    "label": _("Consumables"),
                    "count": consumable_count,
                    "url": f"{reverse('inventory:consumableassignment_list')}?assigned_holder={assetholder.pk}",
                }
            )
        license_count = lic_assign_qs.count()
        if license_count:
            related_objects_list.append(
                {
                    "label": _("Licenses"),
                    "count": license_count,
                    "url": f"{reverse('organization:assetholder_detail', kwargs={'pk': assetholder.pk})}#licenses",
                }
            )
        custody_count = custody_receipts_qs.count()
        if custody_count:
            related_objects_list.append(
                {
                    "label": _("Custody Receipts"),
                    "count": custody_count,
                    "url": f"{reverse('compliance:custodyreceipt_list')}?holder={assetholder.pk}",
                }
            )
        context["related_objects_list"] = related_objects_list

        return context


class AssetHolderEditView(ObjectEditView):
    queryset = AssetHolder.objects.all()
    model = AssetHolder
    model_form = AssetHolderForm
    template_name = "generic/object_edit.html"


class AssetHolderDeleteView(ObjectDeleteView):
    queryset = AssetHolder.objects.all()
    model = AssetHolder
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:assetholder_list")

    def form_valid(self, form):
        """Archive the holder through the per-aggregate service.

        The holder is an aggregate root: the archive refuses while any open
        obligation exists, so the refusal is reported here and the operator is
        sent back to the detail page, which lists what is still outstanding.
        This replaces the view-level guard that only inspected asset
        assignments and left the API and bulk-delete paths unprotected.
        """
        obj_repr = self.get_object_display()
        try:
            result = archive_holder(self.object, actor=self.request.user, request=self.request)
        except ArchiveBlocked as exc:
            messages.error(self.request, exc.headline)
            return redirect(self.object.get_absolute_url())

        messages.success(
            self.request,
            _("Deleted %(model)s %(object)s.") % {"model": self.object._meta.verbose_name, "object": obj_repr},
        )
        if result.detached:
            messages.info(
                self.request,
                ngettext(
                    "Detached %(count)s linked record that referenced the asset holder.",
                    "Detached %(count)s linked records that referenced the asset holder.",
                    result.detached,
                )
                % {"count": result.detached},
            )
        return HttpResponseRedirect(self.get_success_url())


class AssetHolderBulkEditView(ObjectBulkEditView):
    queryset = AssetHolder.objects.all()


class AssetHolderBulkDeleteView(ObjectBulkDeleteView):
    queryset = AssetHolder.objects.all()
