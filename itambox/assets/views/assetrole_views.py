from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

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
from ..models import Asset, AssetRole


class AssetRoleListView(ObjectListView):
    queryset = AssetRole.objects.prefetch_related("tags").annotate(asset_count=Count("assets"))
    filterset = filters.AssetRoleFilterSet
    filterset_form = forms.AssetRoleFilterForm
    table = tables.AssetRoleTable
    action_buttons = ("add",)


class AssetRoleDetailView(ObjectDetailView):
    queryset = AssetRole.objects.prefetch_related("tags")

    layout = (((Panel("info", _("Asset Role Details")),),),)

    def get_context_data(self, **kwargs):
        asset_scope = Scope.current()
        context = super().get_context_data(**kwargs)
        assetrole = self.get_object()

        can_view_assets = self.request.user.has_perm("assets.view_asset")
        assets_table = None
        related_objects_list = []
        if can_view_assets:
            assets_qs = (
                Asset.objects.for_scope(asset_scope)
                .filter(asset_role=assetrole)
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
                        "url": f"{reverse('assets:asset_list')}?asset_role={assetrole.slug}",
                    }
                )

        context["can_view_assets"] = can_view_assets
        context["assets_table"] = assets_table
        context["related_objects_list"] = related_objects_list
        return context


class AssetRoleEditView(QuickAddMixin, ObjectEditView):
    queryset = AssetRole.objects.all()
    model = AssetRole
    model_form = forms.AssetRoleForm
    template_name = "generic/object_edit.html"
    quick_add_target = "id_asset_role"


class AssetRoleCloneView(ObjectCloneView):
    model = AssetRole
    model_form = forms.AssetRoleForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:assetrole_list"


class AssetRoleDeleteView(ObjectDeleteView):
    queryset = AssetRole.objects.all()
    model = AssetRole
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:assetrole_list")

    def post(self, request, *args, **kwargs):
        assetrole = self.get_object()
        asset_count = assetrole.assets.count()

        if asset_count > 0:
            messages.error(
                request,
                _("Cannot delete asset role '%(name)s': It is associated with %(count)s asset%(suffix)s.")
                % {
                    "name": assetrole.name,
                    "count": asset_count,
                    "suffix": "s" if asset_count != 1 else "",
                },
            )
            return redirect(assetrole.get_absolute_url())

        return super().post(request, *args, **kwargs)
