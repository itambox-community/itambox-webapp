from django.apps import apps
from django.db.models import Count, Q
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

from assets.models.catalog import scoped_related_count
from compliance.models import CustodyTemplate
from core.managers import Scope
from inventory.models import Accessory, Component, Consumable
from inventory.tables import ComponentTable, ConsumableTable
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
from ..models import AssetType, Category


class CategoryListView(ObjectListView):
    queryset = Category.objects.prefetch_related("tags")
    filterset = filters.CategoryFilterSet
    filterset_form = forms.CategoryFilterForm
    table = tables.CategoryTable
    action_buttons = ("add",)

    def get_queryset(self):
        queryset = super().get_queryset()
        scope = Scope.current()
        return queryset.annotate(
            assettype_count=Count("asset_types", filter=Q(asset_types__deleted_at__isnull=True), distinct=True),
            accessory_count=scoped_related_count(Accessory.objects.for_scope(scope), "category_id"),
            consumable_count=scoped_related_count(
                apps.get_model("inventory", "Consumable").objects.for_scope(scope), "category_id"
            ),
            component_count=scoped_related_count(
                apps.get_model("inventory", "Component").objects.for_scope(scope), "category_id"
            ),
        )


class CategoryDetailView(ObjectDetailView):
    queryset = Category.objects.all()

    layout = (((Panel("info", _("Category Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        category = self.get_object()

        cat_asset_types = AssetType.objects.filter(category=category).select_related("manufacturer")
        asset_types_table = tables.AssetTypeTable(cat_asset_types, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            asset_types_table
        )
        context["asset_types_table"] = asset_types_table

        cat_accessories = (
            Accessory.objects.for_scope(Scope.current()).filter(category=category).select_related("manufacturer")
        )
        accessories_table = tables.AccessoryTable(cat_accessories, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            accessories_table
        )
        context["accessories_table"] = accessories_table

        # Components

        cat_components = (
            Component.objects.for_scope(Scope.current())
            .filter(category=category)
            .select_related("manufacturer", "category", "tenant")
            .prefetch_related("tags")
        )
        components_table = ComponentTable(cat_components, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(components_table)
        context["components_table"] = components_table

        # Consumables

        cat_consumables = (
            Consumable.objects.for_scope(Scope.current())
            .filter(category=category)
            .select_related("manufacturer", "category", "tenant")
            .prefetch_related("tags")
        )
        consumables_table = ConsumableTable(cat_consumables, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            consumables_table
        )
        context["consumables_table"] = consumables_table

        # Query active custody templates (Policies) linked to this category

        context["custody_templates"] = (
            CustodyTemplate.objects.for_scope(Scope.current())
            .filter(category=category, is_active=True)
            .select_related("tenant", "tenant_group")
        )

        related_objects_list = []
        assettype_count = cat_asset_types.count()
        if assettype_count:
            related_objects_list.append(
                {
                    "label": "Asset Types",
                    "count": assettype_count,
                    "url": f"{reverse('assets:assettype_list')}?category={category.slug}",
                }
            )
        accessory_count = cat_accessories.count()
        if accessory_count:
            related_objects_list.append(
                {
                    "label": "Accessories",
                    "count": accessory_count,
                    "url": f"{reverse('inventory:accessory_list')}?category={category.slug}",
                }
            )
        component_count = cat_components.count()
        if component_count:
            related_objects_list.append(
                {
                    "label": "Components",
                    "count": component_count,
                    "url": f"{reverse('inventory:component_list')}?category={category.slug}",
                }
            )
        consumable_count = cat_consumables.count()
        if consumable_count:
            related_objects_list.append(
                {
                    "label": "Consumables",
                    "count": consumable_count,
                    "url": f"{reverse('inventory:consumable_list')}?category={category.slug}",
                }
            )
        context["related_objects_list"] = related_objects_list
        return context


class CategoryEditView(ObjectEditView):
    queryset = Category.objects.all()
    model = Category
    model_form = forms.CategoryForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:category_list"


class CategoryDeleteView(ObjectDeleteView):
    queryset = Category.objects.all()
    model = Category
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:category_list")


class CategoryCloneView(ObjectCloneView):
    model = Category
    model_form = forms.CategoryForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:category_list"
