from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from assets.models import Asset
from core.managers import Scope, get_current_tenant
from inventory.services import (
    checkout_inventory_item,
    recipient_assignment_union,
    shared_stock_union,
)
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
from itambox.views.generic.service_views import GenericTransactionView
from organization.access import resolved_shared_stock_ids
from organization.models import AssetHolder, Location, TenantResourceGrant

from .. import filters, forms, tables
from ..models import Consumable, ConsumableAssignment, ConsumableStock, Kit
from .stock_actions import StockAdjustView, StockCreateModalView


class ConsumableListView(ObjectListView):
    queryset = (
        Consumable.objects.for_scope(Scope.current())
        .with_counts()
        .select_related("tenant", "manufacturer", "category")
        .prefetch_related("tags")
    )
    filterset = filters.ConsumableFilterSet
    filterset_form = forms.ConsumableFilterForm
    table = tables.ConsumableTable
    action_buttons = ("add",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Consumables")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (None, _("Inventory & Stock")),
            (None, _("Consumables")),
        ]
        if not (self.is_htmx_partial() and self.content_partial_name):
            context["asset_holders"] = AssetHolder.objects.all().order_by("last_name", "first_name")
            context["locations"] = Location.objects.for_scope(Scope.current()).order_by("name")
            context["assets"] = Asset.objects.for_scope(Scope.current()).all().order_by("asset_tag")
        return context


class ConsumableDetailView(ObjectDetailView):
    queryset = Consumable.objects.select_related("manufacturer").prefetch_related(
        "tags", "consumptions__assigned_holder", "consumptions__assigned_location", "stocks__location"
    )
    template_name = "assets/consumables/consumable_detail.html"

    layout = (
        ((Panel("metrics", _("Metrics Overview")),),),
        ((Panel("info", _("Consumable Details")),),),
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        consumable = self.get_object()

        consumptions_table = tables.ConsumableAssignmentTable(consumable.consumptions.all(), request=self.request)
        consumptions_table.configure(self.request)
        context["consumptions_table"] = consumptions_table

        stocks_table = tables.ConsumableStockTable(consumable.stocks.all(), request=self.request)
        stocks_table.configure(self.request)
        context["stocks_table"] = stocks_table

        # Kits
        kits_qs = (
            Kit.objects.for_scope(Scope.current())
            .filter(items__consumable=consumable, items__deleted_at__isnull=True)
            .distinct()
        )
        kits_table = tables.KitTable(kits_qs, request=self.request)
        kits_table.configure(self.request)
        context["kits_table"] = kits_table

        return context


class ConsumableEditView(ObjectEditView):
    queryset = Consumable.objects.all()
    model = Consumable
    model_form = forms.ConsumableForm
    template_name = "generic/object_edit.html"
    default_return_url = "inventory:consumable_list"


class ConsumableDeleteView(ObjectDeleteView):
    queryset = Consumable.objects.all()
    model = Consumable
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("inventory:consumable_list")

    def post(self, request, *args, **kwargs):
        consumable = self.get_object()
        consumption_count = consumable.consumptions.count()
        if consumption_count > 0:
            messages.error(
                request,
                _("Cannot delete consumable '%(consumable)s': It has %(count)s historical consumption records.")
                % {"consumable": consumable, "count": consumption_count},
            )
            return redirect(consumable.get_absolute_url())
        return super().post(request, *args, **kwargs)


class ConsumableCloneView(ObjectCloneView):
    model = Consumable
    model_form = forms.ConsumableForm
    template_name = "generic/object_edit.html"
    default_return_url = "inventory:consumable_list"


class ConsumableCheckoutView(GenericTransactionView):
    permission_required = ("inventory.add_consumableassignment",)
    queryset = Consumable.objects.all()
    model_form = forms.ConsumableCheckoutForm
    service_callable = checkout_inventory_item
    context_object_name = "consumable"
    template_name = "inventory/includes/consumable_checkout_modal.html"
    error_partial = "inventory/includes/consumable_checkout_modal.html#checkout-modal-form"
    success_message = _("Consumable consumed successfully.")
    form_field_map = {
        "assigned_holder": "holder",
        "assigned_location": "location",
        "assigned_asset": "asset",
        "from_location": "source_location",
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        active_tenant = get_current_tenant()
        if active_tenant is None:
            return queryset.none()
        shared_stock_ids = resolved_shared_stock_ids(
            ConsumableStock,
            active_tenant,
            self.request.user,
            TenantResourceGrant.ACCESS_USE,
            "inventory.add_consumableassignment",
        )
        return (queryset | Consumable.objects.filter(stocks__pk__in=shared_stock_ids)).distinct()

    def has_permission(self):
        active_tenant = get_current_tenant()
        return active_tenant is not None and self.request.user.has_perms(
            self.get_permission_required(), obj=active_tenant
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        del kwargs["instance"]
        kwargs["consumable"] = self.get_object()
        kwargs["user"] = self.request.user
        if "initial" not in kwargs:
            kwargs["initial"] = {}
        for key in self.request.GET:
            kwargs["initial"][key] = self.request.GET.get(key)
        return kwargs


class ConsumableBulkEditView(ObjectBulkEditView):
    queryset = Consumable.objects.all()


class ConsumableBulkDeleteView(ObjectBulkDeleteView):
    queryset = Consumable.objects.all()


class ConsumableStockListView(ObjectListView):
    queryset = ConsumableStock.objects.select_related("consumable", "location").all()

    def get_queryset(self):
        # ADR-0001 4b: include pools shared TO the active tenant (read-only).
        return shared_stock_union(super().get_queryset(), ConsumableStock).select_related("consumable", "location")

    table = tables.ConsumableStockTable
    action_buttons = ("add",)
    filterset = filters.ConsumableStockFilterSet
    filterset_form = forms.ConsumableStockFilterForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Consumable Stocks")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse("inventory:consumable_list"), _("Consumables")),
            (None, _("Stocks")),
        ]
        if not (self.is_htmx_partial() and self.content_partial_name):
            context["asset_holders"] = AssetHolder.objects.all().order_by("last_name", "first_name")
            context["locations"] = Location.objects.for_scope(Scope.current()).order_by("name")
            context["assets"] = Asset.objects.for_scope(Scope.current()).all().order_by("asset_tag")
        return context


class ConsumableStockEditView(ObjectEditView):
    queryset = ConsumableStock.objects.all()
    model = ConsumableStock
    model_form = forms.ConsumableStockForm
    template_name = "generic/object_edit.html"
    default_return_url = "inventory:consumable_list"


class ConsumableStockDeleteView(ObjectDeleteView):
    queryset = ConsumableStock.objects.all()
    model = ConsumableStock
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("inventory:consumable_list")


class ConsumableAssignmentListView(ObjectListView):
    queryset = ConsumableAssignment.objects.select_related(
        "consumable", "assigned_holder", "assigned_location", "assigned_asset"
    ).all()

    def get_queryset(self):
        # ADR-0001 4b: recipients see consumptions targeting their tenant.
        return recipient_assignment_union(
            super().get_queryset(),
            ConsumableAssignment,
        ).select_related("consumable", "assigned_holder", "assigned_location", "assigned_asset")

    table = tables.ConsumableAssignmentTable
    action_buttons = ()
    filterset = filters.ConsumableAssignmentFilterSet
    filterset_form = forms.ConsumableAssignmentFilterForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Consumable Consumptions")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse("inventory:consumable_list"), _("Consumables")),
            (None, _("Consumptions")),
        ]
        return context


class ConsumableStockAdjustView(StockAdjustView):
    permission_required = "inventory.change_consumablestock"
    # Adjustments are owner-only; shared pools remain read/consume-only.
    queryset = ConsumableStock.objects.all()


class ConsumableStockCreateModalView(StockCreateModalView):
    permission_required = "inventory.add_consumablestock"
    queryset = Consumable.objects.all()
    modal_form = forms.ConsumableStockModalForm
