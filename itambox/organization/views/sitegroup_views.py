from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from assets.models.catalog import scoped_related_count
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
    queryset = SiteGroup.objects.prefetch_related("tags")
    filterset = SiteGroupFilterSet
    filterset_form = SiteGroupFilterForm
    table = SiteGroupTable
    action_buttons = ("add",)

    def get_queryset(self):
        scope = Scope.current()
        return (
            super().get_queryset().annotate(site_count=scoped_related_count(Site.objects.for_scope(scope), "group_id"))
        )


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
    template_name = "organization/sitegroups/sitegroup_confirm_delete.html"
    success_url = reverse_lazy("organization:sitegroup_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sitegroup = self.object
        context["sitegroup"] = sitegroup
        total = Site.all_objects.filter(
            group=sitegroup
        ).count()  # unscoped: delete guards must block on references outside the visible scope
        visible = Site.objects.for_scope(Scope.current()).filter(group=sitegroup).count()
        context["related_site_count"] = visible
        context["related_sites_outside_scope"] = total > visible
        return context

    def post(self, request, *args, **kwargs):
        sitegroup = self.get_object()
        site_count = Site.all_objects.filter(
            group=sitegroup
        ).count()  # unscoped: delete guards must block on references outside the visible scope

        if site_count > 0:
            visible_count = Site.objects.for_scope(Scope.current()).filter(group=sitegroup).count()
            if visible_count == site_count:
                message = _("Cannot delete site group '%(name)s': It is associated with %(count)d site%(plural)s.") % {
                    "name": sitegroup.name,
                    "count": site_count,
                    "plural": "s" if site_count != 1 else "",
                }
            else:
                message = _("Cannot delete %(object)s: It is referenced outside your scope.") % {"object": sitegroup}
            messages.error(request, message)
            return redirect(sitegroup.get_absolute_url())

        return super().post(request, *args, **kwargs)
