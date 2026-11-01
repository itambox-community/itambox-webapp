import logging

from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.choices import RequestStatusChoices
from core.models import Notification
from extras.services.events import dispatch_event
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)

from .. import filters, forms, tables
from ..models import AssetRequest

logger = logging.getLogger(__name__)


class AssetRequestListView(ObjectListView):
    queryset = AssetRequest.objects.select_related("requester", "asset", "asset_type").all()
    filterset = filters.AssetRequestFilterSet
    filterset_form = forms.AssetRequestFilterForm
    table = tables.AssetRequestTable
    action_buttons = ("add",)

    def get_queryset(self):
        qs = super().get_queryset().filter(parent__isnull=True)
        return qs


class AssetRequestDetailView(ObjectDetailView):
    queryset = AssetRequest.objects.select_related("requester", "asset", "asset_type", "responded_by").all()

    layout = (
        (
            (Panel("info", _("Asset Request Details")),),
            (Panel("response", _("Decision & Response Details")),),
        ),
    )


class AssetRequestCreateView(ObjectEditView):
    model = AssetRequest
    model_form = forms.AssetRequestForm
    template_name = "assets/requests/assetrequest_form.html"
    default_return_url = "assets:assetrequest_list"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        form.instance.requester = self.request.user
        qty = form.cleaned_data.get("qty") or 1
        asset_type = form.instance.asset_type
        if asset_type and qty > 1:
            # Create the parent group request
            form.instance.qty = qty
            form.instance.is_group = True
            response = super().form_valid(form)

            # Create the individual child requests
            for _i in range(qty):
                req = AssetRequest(
                    tenant=form.instance.tenant,
                    requester=self.request.user,
                    asset_type=asset_type,
                    qty=1,
                    parent=form.instance,
                    assigned_user=form.instance.assigned_user,
                    assigned_location=form.instance.assigned_location,
                    assigned_asset=form.instance.assigned_asset,
                    notes=form.instance.notes,
                )
                req._skip_duplicate_check = True
                req.save()
            return response
        else:
            return super().form_valid(form)


class AssetRequestEditView(ObjectEditView):
    queryset = AssetRequest.objects.all()
    model = AssetRequest
    model_form = forms.AssetRequestForm
    template_name = "generic/object_edit.html"

    def form_valid(self, form):
        if form.instance.status in ("approved", "denied", "fulfilled", "cancelled"):
            form.instance.response_date = timezone.now()
            form.instance.responded_by = self.request.user
        response = super().form_valid(form)

        # Cascade status changes to sub_requests
        if form.instance.is_group and form.instance.status in ("approved", "denied", "cancelled"):
            for child in form.instance.sub_requests.exclude(status=form.instance.status):
                if child.status not in ("fulfilled", "cancelled"):
                    child.status = form.instance.status
                    child.response_date = form.instance.response_date
                    child.responded_by = form.instance.responded_by
                    child.response_notes = form.instance.response_notes
                    child.save()

        try:
            dispatch_event(AssetRequest, self.object, action="update")
            Notification.objects.create(
                user=self.object.requester,
                subject=_("Asset Request %(status)s") % {"status": self.object.get_status_display()},
                message=_("Your request for %(item)s has been %(status)s.")
                % {"item": self.object, "status": self.object.get_status_display().lower()},
                level=Notification.LEVEL_INFO,
                target_url=self.object.get_absolute_url(),
            )
        # broad except: boundary-isolation: event dispatch and the requester notification are
        # best-effort side effects of a request that is already saved; they must not fail the edit
        except Exception:
            logger.exception("Asset request %s: event dispatch or requester notification failed", self.object.pk)
        return response

    def get_success_url(self):
        if self.object:
            return self.object.get_absolute_url()
        return reverse("assets:assetrequest_list")


class AssetRequestQueueView(ObjectListView):
    queryset = AssetRequest.objects.filter(status=RequestStatusChoices.PENDING).select_related(
        "requester", "asset", "asset_type"
    )
    filterset = filters.AssetRequestFilterSet
    filterset_form = forms.AssetRequestFilterForm
    table = tables.AssetRequestTable
    action_buttons = ()
    template_name = "generic/object_list.html"


class AssetRequestDeleteView(ObjectDeleteView):
    queryset = AssetRequest.objects.all()
    model = AssetRequest
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("assets:assetrequest_list")
