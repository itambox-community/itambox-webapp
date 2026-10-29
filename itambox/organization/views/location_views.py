from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from assets.models import AssetAssignment
from assets.tables import AssetTable
from compliance.models import AuditSession
from compliance.views_audit import AuditSessionTable
from core.managers import Scope
from inventory.models import AccessoryStock, ComponentStock, ConsumableStock
from inventory.tables import AccessoryStockTable, ComponentStockTable, ConsumableStockTable
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
from organization.services.location_archive import ArchiveBlocked, archive_location
from organization.tables import AssetAssignmentTable

from ..filters import LocationFilterSet
from ..forms import LocationFilterForm, LocationForm
from ..models import Location
from ..tables import LocationTable


class LocationListView(ObjectListView):
    queryset = (
        Location.objects.select_related("site", "site__region", "tenant")
        .prefetch_related("tags")
        .annotate(
            asset_count=Count("assets", filter=Q(assets__deleted_at__isnull=True)),
        )
    )
    filterset = LocationFilterSet
    filterset_form = LocationFilterForm
    table = LocationTable
    action_buttons = ("add",)


class LocationDetailView(ObjectDetailView):
    queryset = Location.objects.select_related("site", "parent", "tenant").prefetch_related(
        "children", "tags", "assets"
    )

    layout = (((Panel("info", _("Location Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        location = self.get_object()

        assets_table = AssetTable(location.assets.all(), request=self.request)
        assets_table.configure(self.request)
        context["assets_table"] = assets_table

        # Accessory Stocks

        acc_stock_qs = AccessoryStock.objects.for_scope(Scope.current()).filter(location=location)
        accessory_stocks_table = AccessoryStockTable(acc_stock_qs, request=self.request)
        accessory_stocks_table.configure(self.request)
        context["accessory_stocks_table"] = accessory_stocks_table

        # Consumable Stocks

        con_stock_qs = ConsumableStock.objects.for_scope(Scope.current()).filter(location=location)
        consumable_stocks_table = ConsumableStockTable(con_stock_qs, request=self.request)
        consumable_stocks_table.configure(self.request)
        context["consumable_stocks_table"] = consumable_stocks_table

        # Component Stocks

        comp_stock_qs = ComponentStock.objects.for_scope(Scope.current()).filter(location=location)
        component_stocks_table = ComponentStockTable(comp_stock_qs, request=self.request)
        component_stocks_table.configure(self.request)
        context["component_stocks_table"] = component_stocks_table

        # Historical Checkout Log

        asset_assignments_qs = AssetAssignment.objects.for_scope(Scope.current()).filter(assigned_location=location)
        asset_assignments_table = AssetAssignmentTable(asset_assignments_qs, request=self.request)
        asset_assignments_table.configure(self.request)
        context["asset_assignments_table"] = asset_assignments_table

        # Audit Campaigns

        audits_qs = AuditSession.objects.for_scope(Scope.current()).filter(location=location)
        audits_table = AuditSessionTable(audits_qs, request=self.request)
        audits_table.configure(self.request)
        context["audits_table"] = audits_table

        related_objects_list = []
        asset_count = location.assets.count()
        if asset_count:
            related_objects_list.append(
                {
                    "label": _("Assets"),
                    "count": asset_count,
                    "url": f"{reverse('assets:asset_list')}?location={location.slug}",
                }
            )
        child_count = location.children.count()
        if child_count:
            related_objects_list.append(
                {
                    "label": _("Child Locations"),
                    "count": child_count,
                    "url": f"{reverse('organization:location_list')}?parent={location.slug}",
                }
            )
        accessory_count = acc_stock_qs.count()
        if accessory_count:
            related_objects_list.append(
                {
                    "label": _("Accessory Stocks"),
                    "count": accessory_count,
                    "url": f"{reverse('inventory:accessorystock_list')}?location={location.slug}",
                }
            )
        consumable_count = con_stock_qs.count()
        if consumable_count:
            related_objects_list.append(
                {
                    "label": _("Consumable Stocks"),
                    "count": consumable_count,
                    "url": f"{reverse('inventory:consumablestock_list')}?location={location.slug}",
                }
            )
        component_count = comp_stock_qs.count()
        if component_count:
            related_objects_list.append(
                {
                    "label": _("Component Stocks"),
                    "count": component_count,
                    "url": f"{reverse('inventory:componentstock_list')}?location={location.slug}",
                }
            )
        checkout_count = asset_assignments_qs.count()
        if checkout_count:
            related_objects_list.append(
                {
                    "label": _("Checkout Log"),
                    "count": checkout_count,
                    "url": f"{reverse('organization:location_detail', kwargs={'pk': location.pk})}#checkout-log",
                }
            )
        audit_count = audits_qs.count()
        if audit_count:
            related_objects_list.append(
                {
                    "label": _("Audit Campaigns"),
                    "count": audit_count,
                    "url": f"{reverse('compliance:auditsession_list')}?location={location.slug}",
                }
            )

        context["related_objects_list"] = related_objects_list
        return context


class LocationEditView(QuickAddMixin, ObjectEditView):
    queryset = Location.objects.all()
    model = Location
    model_form = LocationForm
    template_name = "generic/object_edit.html"
    quick_add_target = "id_location"


class LocationCloneView(ObjectCloneView):
    model = Location
    model_form = LocationForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:location_list"


class LocationDeleteView(ObjectDeleteView):
    queryset = Location.objects.all()
    model = Location
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:location_list")

    def form_valid(self, form):
        """Archive the location through the per-aggregate service.

        The service refuses while assets, open checkouts, stock, child
        locations or open purchase orders still depend on the location; the
        refusal replaces the former asset-only view guard and is reported here.
        """
        obj_repr = self.get_object_display()
        try:
            result = archive_location(self.object, actor=self.request.user, request=self.request)
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
                    "Detached %(count)s linked record that referenced the location.",
                    "Detached %(count)s linked records that referenced the location.",
                    result.detached,
                )
                % {"count": result.detached},
            )
        return HttpResponseRedirect(self.get_success_url())


class LocationBulkEditView(ObjectBulkEditView):
    queryset = Location.objects.all()


class LocationBulkDeleteView(ObjectBulkDeleteView):
    queryset = Location.objects.all()
