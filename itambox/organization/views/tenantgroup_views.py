from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from compliance.models import CustodyTemplate
from compliance.tables import CustodyTemplateTable
from core.managers import Scope
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)

from ..filters import TenantGroupFilterSet
from ..forms import TenantGroupFilterForm, TenantGroupForm
from ..models import TenantGroup
from ..tables import TenantGroupTable, TenantTable


class TenantGroupListView(ObjectListView):
    queryset = TenantGroup.objects.annotate(
        tenant_count=Count("tenants", filter=Q(tenants__deleted_at__isnull=True))
    ).prefetch_related("tags")
    filterset = TenantGroupFilterSet
    filterset_form = TenantGroupFilterForm
    table = TenantGroupTable
    action_buttons = ("add",)


class TenantGroupDetailView(ObjectDetailView):
    queryset = TenantGroup.objects.prefetch_related("children", "tags", "tenants")

    layout = (((Panel("info", _("Tenant Group Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenantgroup = self.get_object()

        tenants_table = TenantTable(tenantgroup.tenants.all(), request=self.request)
        tenants_table.configure(self.request)

        # Custody Templates

        custody_templates_qs = CustodyTemplate.objects.for_scope(Scope.current()).filter(tenant_group=tenantgroup)
        custody_templates_table = CustodyTemplateTable(custody_templates_qs, request=self.request)
        custody_templates_table.configure(self.request)
        context["custody_templates_table"] = custody_templates_table

        related_objects_list = []
        tenant_count = tenantgroup.tenants.count()
        if tenant_count:
            related_objects_list.append(
                {
                    "label": _("Tenants"),
                    "count": tenant_count,
                    "url": f"{reverse('organization:tenant_list')}?group={tenantgroup.slug}",
                }
            )
        child_count = tenantgroup.children.count()
        if child_count:
            related_objects_list.append(
                {
                    "label": _("Child Groups"),
                    "count": child_count,
                    "url": f"{reverse('organization:tenantgroup_list')}?parent={tenantgroup.slug}",
                }
            )
        custody_count = custody_templates_qs.count()
        if custody_count:
            related_objects_list.append(
                {
                    "label": _("Custody EULAs"),
                    "count": custody_count,
                    "url": f"{reverse('compliance:custodytemplate_list')}?tenant_group={tenantgroup.slug}",
                }
            )

        context["tenants_table"] = tenants_table
        context["related_objects_list"] = related_objects_list

        children = tenantgroup.children.all()
        if children.exists():
            context["children_table"] = TenantGroupTable(children, request=self.request)

        return context


class TenantGroupEditView(ObjectEditView):
    queryset = TenantGroup.objects.all()
    model = TenantGroup
    model_form = TenantGroupForm
    template_name = "generic/object_edit.html"


class TenantGroupDeleteView(ObjectDeleteView):
    queryset = TenantGroup.objects.all()
    model = TenantGroup
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:tenantgroup_list")

    def post(self, request, *args, **kwargs):
        tenantgroup = self.get_object()
        tenant_count = tenantgroup.tenants.count()

        if tenant_count > 0:
            messages.error(
                request,
                _("Cannot delete tenant group '%(name)s': It is associated with %(count)d tenant%(plural)s.")
                % {
                    "name": tenantgroup.name,
                    "count": tenant_count,
                    "plural": "s" if tenant_count != 1 else "",
                },
            )
            return redirect(tenantgroup.get_absolute_url())

        return super().post(request, *args, **kwargs)
