from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from assets.models import Asset
from assets.models.catalog import scoped_related_count
from assets.tables import AssetTable
from core.managers import Scope
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

from ..filters import SiteFilterSet
from ..forms import SiteFilterForm, SiteForm
from ..models import Location, Site
from ..tables import LocationTable, SiteTable


class SiteListView(ObjectListView):
    queryset = Site.objects.select_related("region", "group", "tenant").prefetch_related("tags")
    filterset = SiteFilterSet
    filterset_form = SiteFilterForm
    table = SiteTable
    action_buttons = ("add",)

    def get_queryset(self):
        scope = Scope.current()
        return (
            super()
            .get_queryset()
            .annotate(
                location_count=scoped_related_count(Location.objects.for_scope(scope), "site_id"),
                asset_count=scoped_related_count(Asset.objects.for_scope(scope), "location__site_id"),
            )
        )


class SiteDetailView(ObjectDetailView):
    queryset = Site.objects.select_related("region", "group", "tenant").prefetch_related("tags")

    layout = (((Panel("info", _("Site Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        site = self.get_object()

        locations_qs = Location.objects.for_scope(Scope.current()).filter(site=site)
        locations_table = LocationTable(locations_qs, request=self.request)
        locations_table.configure(self.request)

        site_assets = Asset.objects.for_scope(Scope.current()).filter(location__site=site)
        assets_table = AssetTable(site_assets, request=self.request)
        assets_table.configure(self.request)

        related_objects_list = []
        location_count = locations_qs.count()
        if location_count:
            related_objects_list.append(
                {
                    "label": _("Locations"),
                    "count": location_count,
                    "url": f"{reverse('organization:location_list')}?site={site.slug}",
                }
            )
        asset_count = site_assets.count()
        if asset_count:
            related_objects_list.append(
                {"label": _("Assets"), "count": asset_count, "url": f"{reverse('assets:asset_list')}?site={site.slug}"}
            )

        context["locations_table"] = locations_table
        context["assets_table"] = assets_table
        context["related_objects_list"] = related_objects_list
        return context


class SiteEditView(ObjectEditView):
    queryset = Site.objects.all()
    model = Site
    model_form = SiteForm
    template_name = "generic/object_edit.html"


class SiteCloneView(ObjectCloneView):
    model = Site
    model_form = SiteForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:site_list"


class SiteDeleteView(ObjectDeleteView):
    queryset = Site.objects.all()
    model = Site
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:site_list")

    def post(self, request, *args, **kwargs):
        site = self.get_object()
        # Every location blocks deletion; only visible ones may be named or
        # counted in the message.  The same rule applies to assets, which are
        # already resolved through the active scope.
        total_location_count = site.locations.count()
        visible_locations = Location.objects.for_scope(Scope.current()).filter(site=site)
        location_count = visible_locations.count()
        asset_count = Asset.objects.for_scope(Scope.current()).filter(location__site=site).count()

        if total_location_count > 0 or asset_count > 0:
            related_object_details = []
            if location_count > 0:
                related_object_details.append(
                    _("%(count)d location%(plural)s")
                    % {
                        "count": location_count,
                        "plural": "s" if location_count != 1 else "",
                    }
                )
            if total_location_count > location_count:
                related_object_details.append(_("additional locations outside your scope"))
            if asset_count > 0:
                related_object_details.append(
                    _("%(count)d asset%(plural)s")
                    % {
                        "count": asset_count,
                        "plural": "s" if asset_count != 1 else "",
                    }
                )

            messages.error(
                request,
                _("Cannot delete site '%(name)s': It is associated with %(details)s.")
                % {
                    "name": site.name,
                    "details": ", ".join(str(d) for d in related_object_details),
                },
            )
            return redirect(site.get_absolute_url())

        return super().post(request, *args, **kwargs)


class SiteBulkEditView(ObjectBulkEditView):
    queryset = Site.objects.all()


class SiteBulkDeleteView(ObjectBulkDeleteView):
    queryset = Site.objects.all()
