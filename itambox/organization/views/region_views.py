from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

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

from ..filters import RegionFilterSet
from ..forms import RegionFilterForm, RegionForm
from ..models import Region, Site
from ..tables import RegionTable, SiteTable


class RegionListView(ObjectListView):
    queryset = Region.objects.annotate(
        site_count=Count("sites", filter=Q(sites__deleted_at__isnull=True))
    ).prefetch_related("tags")
    filterset = RegionFilterSet
    filterset_form = RegionFilterForm
    table = RegionTable
    action_buttons = ("add",)


class RegionDetailView(ObjectDetailView):
    queryset = Region.objects.prefetch_related("children", "tags")

    layout = (((Panel("info", _("Region Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        region = self.get_object()

        sites_qs = Site.objects.for_scope(Scope.current()).filter(region=region).select_related("tenant", "group")
        sites_table = SiteTable(sites_qs, request=self.request)
        sites_table.configure(self.request)

        related_objects_list = []
        site_count = sites_qs.count()
        if site_count:
            related_objects_list.append(
                {
                    "label": _("Sites"),
                    "count": site_count,
                    "url": f"{reverse('organization:site_list')}?region={region.slug}",
                }
            )
        child_count = region.children.count()
        if child_count:
            related_objects_list.append(
                {
                    "label": _("Child Regions"),
                    "count": child_count,
                    "url": f"{reverse('organization:region_list')}?parent={region.slug}",
                }
            )

        context["sites_table"] = sites_table
        context["related_objects_list"] = related_objects_list

        children = region.children.all()
        if children.exists():
            context["children_table"] = RegionTable(children, request=self.request)

        return context


class RegionEditView(ObjectEditView):
    queryset = Region.objects.all()
    model = Region
    model_form = RegionForm
    template_name = "generic/object_edit.html"


class RegionCloneView(ObjectCloneView):
    model = Region
    model_form = RegionForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:region_list"


class RegionDeleteView(ObjectDeleteView):
    queryset = Region.objects.all()
    model = Region
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:region_list")

    def post(self, request, *args, **kwargs):
        region = self.get_object()
        site_count = region.sites.count()

        if site_count > 0:
            messages.error(
                request,
                _("Cannot delete region '%(name)s': It is associated with %(count)d site%(plural)s.")
                % {
                    "name": region.name,
                    "count": site_count,
                    "plural": "s" if site_count != 1 else "",
                },
            )
            return redirect(region.get_absolute_url())

        return super().post(request, *args, **kwargs)


class RegionBulkEditView(ObjectBulkEditView):
    queryset = Region.objects.all()


class RegionBulkDeleteView(ObjectBulkDeleteView):
    queryset = Region.objects.all()
