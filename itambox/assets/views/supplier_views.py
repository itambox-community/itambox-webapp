from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django_tables2 import RequestConfig

from assets.models import AssetMaintenance
from assets.tables import AssetMaintenanceTable
from core.managers import Scope
from inventory.models import Accessory
from inventory.tables import AccessoryTable
from itambox.panels import Panel
from itambox.utils import get_paginate_count
from itambox.views.generic import (
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from licenses.models import License
from licenses.tables import LicenseTable

from .. import filters, forms, tables
from ..models import Asset, Supplier


class SupplierListView(ObjectListView):
    queryset = Supplier.objects.prefetch_related("tags")
    filterset = filters.SupplierFilterSet
    filterset_form = forms.SupplierFilterForm
    table = tables.SupplierTable
    action_buttons = ("add",)

    def get_queryset(self):
        queryset = super().get_queryset()
        # Mirror the detail tab's scoped Subscription.objects query — the scoped
        # manager resolves the active single-tenant/group/all-accessible scope and
        # soft-deletes — so the count can neither disagree with the tab nor leak
        # subscription counts from tenants outside the active scope.
        # inline imports: heavy-import: subscriptions.models for the supplier subscription counts
        from subscriptions.models import Subscription

        live_subscriptions = (
            Subscription.objects.for_scope(Scope.current())
            .filter(supplier=OuterRef("pk"))
            .order_by()
            .values("supplier")
            .annotate(total=Count("pk"))
        )
        return queryset.annotate(
            subscription_count=Coalesce(
                Subquery(live_subscriptions.values("total"), output_field=IntegerField()),
                0,
            )
        )


class SupplierDetailView(ObjectDetailView):
    queryset = Supplier.objects.all()

    layout = (((Panel("info", _("Supplier Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        supplier = self.get_object()

        supplier_assets = (
            Asset.objects.for_scope(Scope.current())
            .filter(supplier=supplier)
            .select_related("asset_role", "asset_type", "location")
        )
        assets_table = tables.AssetTable(supplier_assets, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(assets_table)
        context["assets_table"] = assets_table

        # Accessories Supplied

        accessory_qs = (
            Accessory.objects.for_scope(Scope.current())
            .filter(supplier=supplier)
            .select_related("manufacturer", "category", "tenant")
            .prefetch_related("tags")
        )
        accessories_table = AccessoryTable(accessory_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            accessories_table
        )
        context["accessories_table"] = accessories_table

        # Maintenance Expenditures

        maintenance_qs = (
            AssetMaintenance.objects.for_scope(Scope.current())
            .filter(supplier=supplier)
            .select_related("asset", "supplier")
        )
        maintenances_table = AssetMaintenanceTable(maintenance_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            maintenances_table
        )
        context["maintenances_table"] = maintenances_table

        # Licenses Purchased

        license_qs = (
            License.objects.for_scope(Scope.current())
            .filter(supplier=supplier)
            .select_related("software", "tenant")
            .prefetch_related("tags")
        )
        licenses_table = LicenseTable(license_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(licenses_table)
        context["licenses_table"] = licenses_table

        # inline imports: heavy-import: subscriptions.models and subscriptions.tables for the supplier detail tab only
        from subscriptions.models import Subscription
        from subscriptions.tables import SupplierSubscriptionTable

        subscription_qs = Subscription.objects.for_scope(Scope.current()).filter(supplier=supplier)
        subscriptions_table = SupplierSubscriptionTable(subscription_qs, request=self.request)
        RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
            subscriptions_table
        )
        context["subscriptions_table"] = subscriptions_table

        related_objects_list = []
        asset_count = supplier_assets.count()
        if asset_count:
            related_objects_list.append(
                {
                    "label": "Assets",
                    "count": asset_count,
                    "url": f"{reverse('assets:asset_list')}?supplier={supplier.slug}",
                }
            )
        accessory_count = accessory_qs.count()
        if accessory_count:
            related_objects_list.append(
                {
                    "label": "Accessories",
                    "count": accessory_count,
                    "url": f"{reverse('inventory:accessory_list')}?supplier={supplier.slug}",
                }
            )
        maintenance_count = maintenance_qs.count()
        if maintenance_count:
            related_objects_list.append(
                {
                    "label": "Maintenances",
                    "count": maintenance_count,
                    "url": f"{reverse('assets:assetmaintenance_list')}?supplier={supplier.slug}",
                }
            )
        license_count = license_qs.count()
        if license_count:
            related_objects_list.append(
                {
                    "label": "Licenses",
                    "count": license_count,
                    "url": f"{reverse('licenses:license_list')}?supplier={supplier.slug}",
                }
            )
        subscription_count = subscription_qs.count()
        if subscription_count:
            related_objects_list.append(
                {
                    "label": "Subscriptions",
                    "count": subscription_count,
                    "url": f"{reverse('subscriptions:subscription_list')}?supplier={supplier.pk}",
                }
            )
        context["related_objects_list"] = related_objects_list
        return context


class SupplierEditView(ObjectEditView):
    queryset = Supplier.objects.all()
    model = Supplier
    model_form = forms.SupplierForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:supplier_list"


class SupplierDeleteView(ObjectDeleteView):
    queryset = Supplier.objects.all()
    model = Supplier
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:supplier_list")


class SupplierCloneView(ObjectCloneView):
    model = Supplier
    model_form = forms.SupplierForm
    template_name = "generic/object_edit.html"
    default_return_url = "assets:supplier_list"
