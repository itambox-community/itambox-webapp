from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from core.managers import Scope
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)

from ..filters import SiteGroupFilterSet
from ..forms import SiteGroupFilterForm, SiteGroupForm
from ..models import Site, SiteGroup
from ..tables import SiteGroupTable, SiteTable


class SiteGroupListView(ObjectListView):
    queryset = SiteGroup.objects.annotate(
        site_count=Count("sites", filter=Q(sites__deleted_at__isnull=True))
    ).prefetch_related("tags")
    filterset = SiteGroupFilterSet
    filterset_form = SiteGroupFilterForm
    table = SiteGroupTable
    action_buttons = ("add",)


class SiteGroupDetailView(ObjectDetailView):
    queryset = SiteGroup.objects.prefetch_related("children", "tags")

    layout = (((Panel("info", _("Site Group Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sitegroup = self.get_object()

        sites_qs = Site.objects.for_scope(Scope.current()).filter(group=sitegroup).select_related("tenant", "region")
        sites_table = SiteTable(sites_qs, request=self.request)
        sites_table.configure(self.request)

        related_objects_list = []
        site_count = sites_qs.count()
        if site_count:
            related_objects_list.append(
                {
                    "label": _("Sites"),
                    "count": site_count,
                    "url": f"{reverse('organization:site_list')}?group={sitegroup.slug}",
                }
            )
        child_count = sitegroup.children.count()
        if child_count:
            related_objects_list.append(
                {
                    "label": _("Child Groups"),
                    "count": child_count,
                    "url": f"{reverse('organization:sitegroup_list')}?parent={sitegroup.slug}",
                }
            )

        context["sites_table"] = sites_table
        context["related_objects_list"] = related_objects_list

        children = sitegroup.children.all()
        if children.exists():
            context["children_table"] = SiteGroupTable(children, request=self.request)

        return context


class SiteGroupEditView(ObjectEditView):
    queryset = SiteGroup.objects.all()
    model = SiteGroup
    model_form = SiteGroupForm
    template_name = "generic/object_edit.html"


class SiteGroupCloneView(ObjectCloneView):
    model = SiteGroup
    model_form = SiteGroupForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:sitegroup_list"


class SiteGroupDeleteView(ObjectDeleteView):
    queryset = SiteGroup.objects.all()
    model = SiteGroup
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:sitegroup_list")

    def post(self, request, *args, **kwargs):
        sitegroup = self.get_object()
        site_count = sitegroup.sites.count()

        if site_count > 0:
            messages.error(
                request,
                _("Cannot delete site group '%(name)s': It is associated with %(count)d site%(plural)s.")
                % {
                    "name": sitegroup.name,
                    "count": site_count,
                    "plural": "s" if site_count != 1 else "",
                },
            )
            return redirect(sitegroup.get_absolute_url())

        return super().post(request, *args, **kwargs)
