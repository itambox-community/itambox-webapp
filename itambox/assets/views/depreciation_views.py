from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

from core.managers import Scope
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)

from .. import filters, forms, tables
from ..models import Depreciation


class DepreciationListView(ObjectListView):
    queryset = Depreciation.objects.all()
    filterset = filters.DepreciationFilterSet
    filterset_form = forms.DepreciationFilterForm
    table = tables.DepreciationTable
    action_buttons = ("add",)


class DepreciationDetailView(ObjectDetailView):
    queryset = Depreciation.objects.all().prefetch_related("asset_types")

    layout = (((Panel("info", _("Depreciation Rule Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        depreciation = self.get_object()

        from itambox.utils import get_paginate_count

        from ..models import Asset, AssetType

        # Affected Asset Types
        assettype_qs = (
            AssetType.objects.filter(depreciation=depreciation).select_related("manufacturer").prefetch_related("tags")
        )
        assettypes_table = tables.AssetTypeTable(assettype_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(assettypes_table)
        context["assettypes_table"] = assettypes_table

        # Active Assets Amortization Schedule (using this depreciation rule)
        asset_qs = (
            Asset.objects.for_scope(Scope.current())
            .filter(asset_type__depreciation=depreciation)
            .select_related(
                "asset_role",
                "asset_type",
                "asset_type__manufacturer",
                "location",
                "tenant",
                "status",
                "supplier",
            )
            .prefetch_related("tags")
        )
        assets_table = tables.AssetTable(asset_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(assets_table)
        context["assets_table"] = assets_table

        return context


class DepreciationEditView(ObjectEditView):
    queryset = Depreciation.objects.all()
    model = Depreciation
    model_form = forms.DepreciationForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:depreciation_list"


class DepreciationCloneView(ObjectCloneView):
    model = Depreciation
    model_form = forms.DepreciationForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:depreciation_list"


class DepreciationDeleteView(ObjectDeleteView):
    queryset = Depreciation.objects.all()
    model = Depreciation
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:depreciation_list")
