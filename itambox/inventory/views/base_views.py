import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.generic import View

from assets.models import Asset
from core.managers import Scope
from inventory.services import checkout_inventory_item
from itambox.views.generic.utils import safe_return_url
from organization.models import AssetHolder, Location

from ..models import Accessory, AccessoryStock, Component, ComponentStock, Consumable, ConsumableStock


class InventoryListView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):

        target_type = request.GET.get("type")
        if target_type == "accessories" and request.user.has_perm("inventory.view_accessory"):
            return redirect("inventory:accessory_list")
        elif target_type == "consumables" and request.user.has_perm("inventory.view_consumable"):
            return redirect("inventory:consumable_list")
        elif target_type == "components" and request.user.has_perm("inventory.view_component"):
            return redirect("inventory:component_list")

        accessible_url = None
        if request.user.has_perm("inventory.view_component"):
            accessible_url = reverse("inventory:component_list")
        elif request.user.has_perm("inventory.view_accessory"):
            accessible_url = reverse("inventory:accessory_list")
        elif request.user.has_perm("inventory.view_consumable"):
            accessible_url = reverse("inventory:consumable_list")

        if not accessible_url:
            raise PermissionDenied(_("You do not have permission to view inventory."))

        return redirect(accessible_url)


@login_required
def bulk_checkout_inventory(request):
    logger = logging.getLogger(__name__)

    model_name_str = request.POST.get("model_name")
    # Resolve correct permission code
    if model_name_str in ("inventory.accessory", "inventory.accessorystock"):
        perm = "inventory.change_accessory"
    elif model_name_str in ("inventory.consumable", "inventory.consumablestock"):
        perm = "inventory.change_consumable"
    elif model_name_str in ("inventory.component", "inventory.componentstock"):
        perm = "inventory.change_component"
    else:
        return HttpResponseBadRequest(_("Invalid model specified."))

    if not request.user.has_perm(perm):
        return HttpResponseForbidden(_("Permission denied."))
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    object_pks = request.POST.getlist("pk")
    qty_str = request.POST.get("qty", "1")
    notes = request.POST.get("notes", "")

    try:
        qty = int(qty_str)
        if qty <= 0:
            raise ValueError()
    except ValueError:
        messages.error(request, _("Invalid checkout quantity specified."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))

    if not object_pks:
        messages.error(request, _("No items selected."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))

    holder_id = request.POST.get("assigned_holder")
    location_id = request.POST.get("assigned_location")
    asset_id = request.POST.get("assigned_asset")

    filled = [t for t in [holder_id, location_id, asset_id] if t]
    if len(filled) == 0:
        messages.error(request, _("You must select either an Asset Holder, a Location, or an Asset."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))
    if len(filled) > 1:
        messages.error(request, _("Please select only one target (Asset Holder, Location, or Asset)."))
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))

    holder = None
    location = None
    asset = None

    if holder_id:
        holder = get_object_or_404(AssetHolder, pk=holder_id)
    elif location_id:
        location = get_object_or_404(Location, pk=location_id)
    elif asset_id:
        asset = get_object_or_404(Asset, pk=asset_id)

    success_count = 0
    failure_count = 0

    if model_name_str in ("inventory.accessory", "inventory.accessorystock"):
        item_model = Accessory
        stock_model = AccessoryStock
    elif model_name_str in ("inventory.consumable", "inventory.consumablestock"):
        item_model = Consumable
        stock_model = ConsumableStock
    else:
        item_model = Component
        stock_model = ComponentStock

    with transaction.atomic():
        if model_name_str in ("inventory.accessory", "inventory.consumable", "inventory.component"):
            # Catalog page checkouts: requires from_location
            from_location_id = request.POST.get("from_location")
            if not from_location_id:
                messages.error(request, _("No source location specified."))
                return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))
            from_location = get_object_or_404(Location, pk=from_location_id)

            for pk in object_pks:
                try:
                    item = item_model.objects.for_scope(Scope.current()).get(pk=pk)
                    checkout_inventory_item(
                        item=item,
                        qty=qty,
                        holder=holder,
                        location=location,
                        asset=asset,
                        user=request.user,
                        notes=notes,
                        source_location=from_location,
                    )
                    success_count += 1
                # broad except: task-isolation: one failed item must not abort the reviewed batch
                except Exception as ex:
                    failure_count += 1
                    logger.error(
                        "Bulk checkout failed for model=%s pk=%s tenant_id=%s actor_id=%s exception_type=%s",
                        item_model.__name__,
                        pk,
                        getattr(holder, "tenant_id", None),
                        request.user.pk,
                        type(ex).__name__,
                    )
                    messages.error(request, _("Failed to check out item %(pk)s.") % {"pk": pk})

        elif model_name_str in ("inventory.accessorystock", "inventory.consumablestock", "inventory.componentstock"):
            # Stocks page checkouts: from_location determined per stock record
            for pk in object_pks:
                try:
                    stock = stock_model.objects.for_scope(Scope.current()).get(pk=pk)
                    item = getattr(stock, item_model.__name__.lower())
                    checkout_inventory_item(
                        item=item,
                        qty=qty,
                        holder=holder,
                        location=location,
                        asset=asset,
                        user=request.user,
                        notes=notes,
                        source_location=stock.location,
                    )
                    success_count += 1
                # broad except: task-isolation: one failed stock row must not abort the reviewed batch
                except Exception as ex:
                    failure_count += 1
                    logger.error(
                        "Bulk checkout failed for model=%s pk=%s tenant_id=%s actor_id=%s exception_type=%s",
                        stock_model.__name__,
                        pk,
                        getattr(holder, "tenant_id", None),
                        request.user.pk,
                        type(ex).__name__,
                    )
                    messages.error(request, _("Failed to check out stock item."))

    if success_count > 0:
        messages.success(
            request,
            ngettext(
                "Successfully checked out %(count)s item.",
                "Successfully checked out %(count)s items.",
                success_count,
            )
            % {"count": success_count},
        )

    redirect_url = reverse("inventory:inventory_list")
    if model_name_str in ("inventory.accessory", "inventory.accessorystock"):
        redirect_url = reverse("inventory:accessory_list")
    elif model_name_str in ("inventory.consumable", "inventory.consumablestock"):
        redirect_url = reverse("inventory:consumable_list")
    elif model_name_str in ("inventory.component", "inventory.componentstock"):
        redirect_url = reverse("inventory:component_list")

    return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), redirect_url))
