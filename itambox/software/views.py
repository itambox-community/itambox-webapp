from django.db.models import Count, Q
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.managers import Scope
from itambox.panels import Panel
from itambox.quick_add import QuickAddMixin
from itambox.views.generic import (
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from licenses.models import License
from licenses.tables import LicenseTable

from . import filters, forms, tables
from .models import InstalledSoftware, Software
from .tables import InstalledSoftwareTable

# =============================================================================
# Software Views
# =============================================================================


class SoftwareListView(ObjectListView):
    queryset = (
        Software.objects.for_scope(Scope.current())
        .select_related("manufacturer")
        .prefetch_related("tags")
        .annotate(
            _installed_count=Count("installed_instances", distinct=True),
            _license_count=Count("licenses", filter=Q(licenses__deleted_at__isnull=True), distinct=True),
        )
    )
    filterset = filters.SoftwareFilterSet
    filterset_form = forms.SoftwareFilterForm
    table = tables.SoftwareTable
    template_name = "generic/object_list.html"  # Use a specific template or generic one
    action_buttons = ("add", "import", "export")  # Standard list view actions


class SoftwareDetailView(ObjectDetailView):
    queryset = Software.objects.select_related("manufacturer").prefetch_related("tags")
    template_name = "software/software_detail.html"  # Use a specific template or generic one

    layout = (((Panel("info", _("Software Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        software = self.get_object()

        instances_qs = InstalledSoftware.objects.for_scope(Scope.current()).filter(software=software)
        instances_table = InstalledSoftwareTable(instances_qs)
        instances_table.configure(self.request)
        context["instances_table"] = instances_table

        # Software Licenses & Entitlements

        license_qs = License.objects.for_scope(Scope.current()).filter(software=software)
        licenses_table = LicenseTable(license_qs, request=self.request)
        licenses_table.configure(self.request)
        context["licenses_table"] = licenses_table

        related_objects_list = []
        instance_count = instances_qs.count()
        if instance_count:
            related_objects_list.append(
                {
                    "label": _("Installed Instances"),
                    "count": instance_count,
                    "url": f"{reverse('software:software_detail', kwargs={'pk': software.pk})}#installed-instances",
                }
            )
        license_count = license_qs.count()
        if license_count:
            related_objects_list.append(
                {
                    "label": _("Licenses"),
                    "count": license_count,
                    "url": f"{reverse('licenses:license_list')}?software={software.pk}",
                }
            )
        context["related_objects_list"] = related_objects_list

        return context


class SoftwareEditView(ObjectEditView):
    queryset = Software.objects.all()
    model_form = forms.SoftwareForm
    template_name = "generic/object_edit.html"
    default_return_url = "software:software_list"


class InstalledSoftwareEditView(QuickAddMixin, ObjectEditView):
    """Record/edit a software installation on an asset (quick-add modal from the
    asset detail Software tab; mirrors WarrantyEditView)."""

    queryset = InstalledSoftware.objects.all()
    model = InstalledSoftware
    model_form = forms.InstalledSoftwareForm
    template_name = "generic/object_edit.html"
    default_return_url = "software:software_list"
    quick_add_reload = True

    def get_initial(self):
        initial = super().get_initial()
        asset_id = self.request.GET.get("asset")
        if asset_id:
            initial["asset"] = asset_id
        return initial


class SoftwareDeleteView(ObjectDeleteView):
    queryset = Software.objects.all()
    # Set default return URL
    default_return_url = "software:software_list"


class SoftwareCloneView(SoftwareEditView, ObjectCloneView):
    model = Software
