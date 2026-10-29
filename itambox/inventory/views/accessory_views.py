from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from core.managers import Scope, get_current_tenant
from inventory.services import (
    checkin_accessory,
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
from itambox.views.generic.service_views import GenericTransactionView, SimplePostView
from organization.access import resolved_shared_stock_ids
from organization.models import AssetHolder, Location, TenantResourceGrant

from .. import filters, forms, tables
from ..models import Accessory, AccessoryAssignment, AccessoryStock, Kit
from .stock_actions import StockAdjustView, StockCreateModalView


class AccessoryListView(ObjectListView):
    queryset = (
        Accessory.objects.for_scope(Scope.current())
        .with_counts()
        .select_related("tenant", "manufacturer", "category")
        .prefetch_related("tags")
    )
    filterset = filters.AccessoryFilterSet
    filterset_form = forms.AccessoryFilterForm
    table = tables.AccessoryTable
    action_buttons = ("add",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Accessories")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (None, _("Inventory & Stock")),
            (None, _("Accessories")),
        ]
        if not (self.is_htmx_partial() and self.content_partial_name):
            from assets.models import Asset

            context["asset_holders"] = AssetHolder.objects.all().order_by("last_name", "first_name")
            context["locations"] = Location.objects.for_scope(Scope.current()).order_by("name")
            context["assets"] = Asset.objects.for_scope(Scope.current()).all().order_by("asset_tag")
        return context


class AccessoryDetailView(ObjectDetailView):
    queryset = Accessory.objects.select_related("manufacturer").prefetch_related(
        "tags", "assignments__assigned_holder", "assignments__assigned_location", "stocks__location"
    )
    template_name = "assets/accessories/accessory_detail.html"

    layout = (
        ((Panel("metrics", _("Metrics Overview")),),),
        ((Panel("info", _("Accessory Details")),),),
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        accessory = self.get_object()

        assignments_table = tables.AccessoryAssignmentTable(accessory.assignments.all(), request=self.request)
        assignments_table.configure(self.request)
        context["assignments_table"] = assignments_table

        stocks_table = tables.AccessoryStockTable(accessory.stocks.all(), request=self.request)
        stocks_table.configure(self.request)
        context["stocks_table"] = stocks_table

        # Kits
        kits_qs = (
            Kit.objects.for_scope(Scope.current())
            .filter(items__accessory=accessory, items__deleted_at__isnull=True)
            .distinct()
        )
        kits_table = tables.KitTable(kits_qs, request=self.request)
        kits_table.configure(self.request)
        context["kits_table"] = kits_table

        return context


class AccessoryEditView(ObjectEditView):
    queryset = Accessory.objects.all()
    model = Accessory
    model_form = forms.AccessoryForm
    template_name = "generic/object_edit.html"
    default_return_url = "inventory:accessory_list"


class AccessoryDeleteView(ObjectDeleteView):
    queryset = Accessory.objects.all()
    model = Accessory
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("inventory:accessory_list")

    def post(self, request, *args, **kwargs):
        accessory = self.get_object()
        assignment_count = accessory.assignments.count()
        if assignment_count > 0:
            messages.error(
                request,
                _("Cannot delete accessory '%(accessory)s': It has %(count)s active assignments.")
                % {"accessory": accessory, "count": assignment_count},
            )
            return redirect(accessory.get_absolute_url())
        return super().post(request, *args, **kwargs)


class AccessoryCloneView(ObjectCloneView):
    model = Accessory
    model_form = forms.AccessoryForm
    template_name = "generic/object_edit.html"
    default_return_url = "inventory:accessory_list"


class AccessoryCheckoutView(GenericTransactionView):
    permission_required = ("inventory.add_accessoryassignment",)
    queryset = Accessory.objects.all()
    model_form = forms.AccessoryCheckoutForm
    service_callable = checkout_inventory_item
    context_object_name = "accessory"
    template_name = "inventory/includes/accessory_checkout_modal.html"
    error_partial = "inventory/includes/accessory_checkout_modal.html#checkout-modal-form"
    success_message = _("Accessory checked out successfully.")
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
            AccessoryStock,
            active_tenant,
            self.request.user,
            TenantResourceGrant.ACCESS_USE,
            "inventory.add_accessoryassignment",
        )
        return (queryset | Accessory.objects.filter(stocks__pk__in=shared_stock_ids)).distinct()

    def has_permission(self):
        active_tenant = get_current_tenant()
        return active_tenant is not None and self.request.user.has_perms(
            self.get_permission_required(),
            obj=active_tenant,
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        del kwargs["instance"]
        kwargs["accessory"] = self.get_object()
        kwargs["user"] = self.request.user
        if "initial" not in kwargs:
            kwargs["initial"] = {}
        for key in self.request.GET:
            kwargs["initial"][key] = self.request.GET.get(key)
        return kwargs


class AccessoryCheckinView(SimplePostView):
    permission_required = ("inventory.change_accessory",)
    queryset = AccessoryAssignment.objects.all()

    def get_queryset(self):
        # ADR-0001 4b: the recipient tenant may run the return workflow.
        return recipient_assignment_union(super().get_queryset(), AccessoryAssignment)

    def has_permission(self):
        perms = self.get_permission_required()
        obj = self.get_object()
        if self.request.user.has_perms(perms, obj=obj):
            return True
        # Recipient side: the same permission, held in the TARGET tenant
        # (a Tenant instance is its own permission context).
        target = obj.target_tenant
        return target is not None and self.request.user.has_perms(perms, obj=target)

    def perform_action(self, assignment, request):
        accessory, qty, recipient = checkin_accessory(assignment.pk, user=request.user)
        return {
            "message": str(
                _("Checked in %(qty)sx '%(accessory)s' from %(recipient)s.")
                % {"qty": qty, "accessory": accessory, "recipient": recipient}
            ),
            "redirect": accessory.get_absolute_url(),
        }

    def get_success_redirect(self, obj, result):
        return redirect(result.get("redirect") or "/")


class AccessoryBulkEditView(ObjectBulkEditView):
    queryset = Accessory.objects.all()


class AccessoryBulkDeleteView(ObjectBulkDeleteView):
    queryset = Accessory.objects.all()


class AccessoryStockListView(ObjectListView):
    queryset = AccessoryStock.objects.select_related("accessory", "location").all()

    def get_queryset(self):
        # ADR-0001 4b: include pools shared TO the active tenant (read-only).
        return shared_stock_union(super().get_queryset(), AccessoryStock).select_related("accessory", "location")

    table = tables.AccessoryStockTable
    action_buttons = ("add",)
    filterset = filters.AccessoryStockFilterSet
    filterset_form = forms.AccessoryStockFilterForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Accessory Stocks")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse("inventory:accessory_list"), _("Accessories")),
            (None, _("Stocks")),
        ]
        if not (self.is_htmx_partial() and self.content_partial_name):
            from assets.models import Asset

            context["asset_holders"] = AssetHolder.objects.all().order_by("last_name", "first_name")
            context["locations"] = Location.objects.for_scope(Scope.current()).order_by("name")
            context["assets"] = Asset.objects.for_scope(Scope.current()).all().order_by("asset_tag")
        return context


class AccessoryStockEditView(ObjectEditView):
    queryset = AccessoryStock.objects.all()
    model = AccessoryStock
    model_form = forms.AccessoryStockForm
    template_name = "generic/object_edit.html"
    default_return_url = "inventory:accessory_list"


class AccessoryStockDeleteView(ObjectDeleteView):
    queryset = AccessoryStock.objects.all()
    model = AccessoryStock
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("inventory:accessory_list")


class AccessoryAssignmentListView(ObjectListView):
    queryset = AccessoryAssignment.objects.select_related(
        "accessory", "assigned_holder", "assigned_location", "assigned_asset"
    ).all()

    def get_queryset(self):
        # ADR-0001 4b: recipients see assignments targeting their tenant.
        return recipient_assignment_union(
            super().get_queryset(),
            AccessoryAssignment,
        ).select_related("accessory", "assigned_holder", "assigned_location", "assigned_asset")

    table = tables.AccessoryAssignmentTable
    action_buttons = ()
    filterset = filters.AccessoryAssignmentFilterSet
    filterset_form = forms.AccessoryAssignmentFilterForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Accessory Assignments")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse("inventory:accessory_list"), _("Accessories")),
            (None, _("Assignments")),
        ]
        return context


class AccessoryStockAdjustView(StockAdjustView):
    permission_required = "inventory.change_accessorystock"
    # Adjustments are owner-only: scope authorization at the pool, never a
    # shared catalogue item or the grantee-widened stock-list queryset.
    queryset = AccessoryStock.objects.all()


class AccessoryStockCreateModalView(StockCreateModalView):
    permission_required = "inventory.add_accessorystock"
    queryset = Accessory.objects.all()
    modal_form = forms.AccessoryStockModalForm
