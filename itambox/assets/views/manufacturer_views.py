from django.db.models import Count, Q
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

from assets.models.catalog import scoped_related_count
from core.managers import Scope
from itambox.panels import Panel
from itambox.utils import get_paginate_count
from itambox.views.generic import (
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)

from .. import filters, forms, tables
from ..models import Asset, Manufacturer


class ManufacturerListView(ObjectListView):
    queryset = Manufacturer.objects.prefetch_related("tags")
    filterset = filters.ManufacturerFilterSet
    filterset_form = forms.ManufacturerFilterForm
    table = tables.ManufacturerTable
    action_buttons = ("add",)

    def get_queryset(self):
        queryset = super().get_queryset()
        assets = Asset.objects.for_scope(Scope.current())
        return queryset.annotate(
            asset_count=scoped_related_count(assets, "asset_type__manufacturer_id"),
            asset_type_count=Count(
                "asset_types",
                filter=Q(asset_types__deleted_at__isnull=True),
                distinct=True,
            ),
        )


class ManufacturerDetailView(ObjectDetailView):
    queryset = Manufacturer.objects.prefetch_related("asset_types", "asset_types__assets")

    layout = (((Panel("info", _("Manufacturer Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        manufacturer = self.get_object()

        asset_types_table = tables.AssetTypeTable(manufacturer.asset_types.all(), request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            asset_types_table
        )

        manufacturer_assets = (
            Asset.objects.for_scope(Scope.current())
            .filter(asset_type__manufacturer=manufacturer)
            .select_related("asset_role", "asset_type", "location")
        )
        assets_table = tables.AssetTable(manufacturer_assets, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(assets_table)

        related_objects_list = []
        asset_count = manufacturer_assets.count()
        if asset_count:
            related_objects_list.append(
                {
                    "label": "Assets",
                    "count": asset_count,
                    "url": f"{reverse('assets:asset_list')}?manufacturer={manufacturer.slug}",
                }
            )
        assettype_count = manufacturer.asset_types.count()
        if assettype_count:
            related_objects_list.append(
                {
                    "label": "Asset Types",
                    "count": assettype_count,
                    "url": f"{reverse('assets:assettype_list')}?manufacturer={manufacturer.slug}",
                }
            )

        context["asset_types_table"] = asset_types_table
        context["assets_table"] = assets_table
        context["related_objects_list"] = related_objects_list

        # Components
        from inventory.models import Component
        from inventory.tables import ComponentTable

        comp_qs = (
            Component.objects.for_scope(Scope.current())
            .filter(manufacturer=manufacturer)
            .select_related("category", "tenant")
        )
        components_table = ComponentTable(comp_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(components_table)
        context["components_table"] = components_table

        # Accessories
        from inventory.models import Accessory
        from inventory.tables import AccessoryTable

        acc_qs = (
            Accessory.objects.for_scope(Scope.current())
            .filter(manufacturer=manufacturer)
            .select_related("category", "tenant")
        )
        accessories_table = AccessoryTable(acc_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            accessories_table
        )
        context["accessories_table"] = accessories_table

        # Consumables
        from inventory.models import Consumable
        from inventory.tables import ConsumableTable

        con_qs = (
            Consumable.objects.for_scope(Scope.current())
            .filter(manufacturer=manufacturer)
            .select_related("category", "tenant")
        )
        consumables_table = ConsumableTable(con_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            consumables_table
        )
        context["consumables_table"] = consumables_table

        # Software products
        from software.models import Software
        from software.tables import SoftwareTable

        sw_qs = (
            Software.objects.for_scope(Scope.current())
            .filter(manufacturer=manufacturer)
            .select_related("manufacturer")
            .prefetch_related("tags")
        )
        software_table = SoftwareTable(sw_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(software_table)
        context["software_table"] = software_table

        return context


class ManufacturerEditView(ObjectEditView):
    queryset = Manufacturer.objects.all()
    model = Manufacturer
    model_form = forms.ManufacturerForm
    template_name = "generic/object_edit.html"


class ManufacturerCloneView(ObjectCloneView):
    model = Manufacturer
    model_form = forms.ManufacturerForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:manufacturer_list"


class ManufacturerDeleteView(ObjectDeleteView):
    queryset = Manufacturer.objects.all()
    model = Manufacturer
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:manufacturer_list")
