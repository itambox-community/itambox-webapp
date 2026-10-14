from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

from assets.customfields import composed_fieldsets, fetch_definitions_by_name, resolve_asset_type_custom_fields
from assets.spec_display import build_spec_panel
from core.managers import Scope
from itambox.panels import Panel
from itambox.quick_add import QuickAddMixin
from itambox.utils import get_paginate_count
from itambox.views.generic import (
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)

from .. import filters, forms, tables
from ..models import Asset, AssetType, AssetTypeFieldset


class AssetTypeListView(ObjectListView):
    queryset = AssetType.objects.select_related("manufacturer", "category").prefetch_related("tags")
    filterset = filters.AssetTypeFilterSet
    filterset_form = forms.AssetTypeFilterForm
    table = tables.AssetTypeTable
    action_buttons = ("add",)


class AssetTypeDetailView(ObjectDetailView):
    queryset = AssetType.objects.select_related("manufacturer").prefetch_related(
        "tags",
        Prefetch(
            "fieldset_memberships",
            queryset=AssetTypeFieldset.objects.select_related("fieldset").prefetch_related(
                "fieldset__field_memberships__custom_field__object_types"
            ),
        ),
    )

    layout = (((Panel("info", _("Asset Type Details")), Panel("specs", _("Hardware Specifications"))),),)

    def get_context_data(self, **kwargs):
        asset_scope = Scope.current()
        context = super().get_context_data(**kwargs)
        assettype = self.get_object()

        can_view_assets = self.request.user.has_perm("assets.view_asset")
        assets_table = None
        related_objects_list = []
        if can_view_assets:
            assets_qs = (
                Asset.objects.for_scope(asset_scope)
                .filter(asset_type=assettype)
                .select_related("asset_role", "asset_type", "location", "tenant")
            )
            assets_table = tables.AssetTable(assets_qs, request=self.request)
            RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(assets_table)

            asset_count = assets_qs.count()
            if asset_count:
                related_objects_list.append(
                    {
                        "label": "Assets",
                        "count": asset_count,
                        "url": f"{reverse('assets:asset_list')}?asset_type={assettype.pk}",
                    }
                )

        context["can_view_assets"] = can_view_assets
        context["assets_table"] = assets_table
        context["related_objects_list"] = related_objects_list
        spec_fields = resolve_asset_type_custom_fields(assettype)
        context["specification_fields"] = spec_fields
        context["spec_panel"] = build_spec_panel(
            spec_fields,
            assettype.custom_field_data,
            composed_fieldsets(assettype),
            definitions_loader=fetch_definitions_by_name,
        )
        # The Hardware Specifications card is the representation of the type's custom data.
        context["hide_generic_custom_fields"] = True

        # Requests
        from ..models import AssetRequest

        req_qs = (
            AssetRequest.objects.for_scope(Scope.current())
            .filter(asset_type=assettype)
            .select_related("requester", "asset", "asset_type")
        )
        requests_table = tables.AssetRequestTable(req_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(requests_table)
        context["requests_table"] = requests_table
        context["request_count"] = requests_table.page.paginator.count

        # Kits
        from inventory.models import Kit
        from inventory.tables import KitTable

        kits_qs = (
            Kit.objects.for_scope(Scope.current())
            .filter(items__asset_type=assettype, items__deleted_at__isnull=True)
            .distinct()
            .select_related("tenant")
        )
        kits_table = KitTable(kits_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(kits_table)
        context["kits_table"] = kits_table

        return context


class AssetTypeEditView(QuickAddMixin, ObjectEditView):
    queryset = AssetType.objects.all()
    model = AssetType
    model_form = forms.AssetTypeForm
    template_name = "assets/assettype_specification_form.html"
    quick_add_target = "id_asset_type"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def post(self, request, *args, **kwargs):
        if request.headers.get("HX-Request") and "_reload" in request.POST:
            self.object = self.get_object() if self.kwargs.get("pk") else None
            form = self.get_form()
            context = self.get_context_data(form=form)
            return render(request, "assets/_assettype_specification_form.html", context)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ValidationError as exc:
            form.add_error(None, exc)
            form.specification_revision_conflict = True
            return self.form_invalid(form)


class AssetTypeDeleteView(ObjectDeleteView):
    queryset = AssetType.objects.all()
    model = AssetType
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:assettype_list")

    def post(self, request, *args, **kwargs):
        assettype = self.get_object()
        asset_count = assettype.assets.count()

        if asset_count > 0:
            messages.error(
                request,
                _("Cannot delete asset type '%(type)s': It is associated with %(count)s asset%(suffix)s.")
                % {
                    "type": assettype,
                    "count": asset_count,
                    "suffix": "s" if asset_count != 1 else "",
                },
            )
            return redirect(assettype.get_absolute_url())

        return super().post(request, *args, **kwargs)


class AssetTypeCloneView(ObjectCloneView):
    model = AssetType
    model_form = forms.AssetTypeForm
    template_name = "assets/assettype_specification_form.html"
    default_return_url = "assets:assettype_list"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs
