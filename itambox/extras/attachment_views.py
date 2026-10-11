"""Journal and attachment presentation owned by extras."""

import mimetypes

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from core.forms import JournalEntryForm
from core.job_access import visible_jobs_for_user
from core.validators import (
    SAFE_IMAGE_ATTACHMENT_MIMES,
    detect_image_mime,
    validate_file_attachment,
    validate_image_attachment,
)
from extras.filters import JournalEntryFilterSet
from extras.forms import JournalEntryFilterForm
from extras.models import FileAttachment, ImageAttachment, JournalEntry
from extras.tables import JournalEntryTable
from itambox.views.generic import ObjectListView
from itambox.views.generic.utils import safe_return_url


@method_decorator(login_required, name="dispatch")
class JournalEntryListView(ObjectListView):
    queryset = JournalEntry.objects.select_related("model", "user", "tenant").prefetch_related("content_object")
    filterset = JournalEntryFilterSet
    filterset_form = JournalEntryFilterForm
    table = JournalEntryTable
    template_name = "extras/journalentry/journalentry_list.html"
    action_buttons = ()

    def get_breadcrumbs(self):
        return [(reverse("dashboard"), _("Dashboard")), (None, _("Journal Entries"))]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Journal Entries")
        return context


def _attachment_parent_for_change(request, content_type, object_id):
    """Return the tenant-scoped parent only when the user may change it.

    Upload/delete/journal writes keep the historical object-bound
    ``change_<parent>`` authorization: the parent is resolved through its
    tenant-scoping ``objects`` manager (so a guessed cross-tenant object_id
    cannot select a row outside the active tenant), and ``has_perm(..., obj=parent)``
    requires the object-bound permission from ``TenantMembershipBackend``.
    Every failure is a 404 so attachment IDs are not enumerable.
    """
    model_class = content_type.model_class()
    if model_class is None:
        raise Http404
    parent = model_class.objects.filter(pk=object_id).first()
    if parent is None:
        raise Http404
    app_label = model_class._meta.app_label
    model_name = model_class._meta.model_name
    if not request.user.has_perm(f"{app_label}.change_{model_name}", obj=parent):
        raise Http404
    return parent


def _attachment_parent_for_view(request, content_type, object_id):
    """Return a visible parent only when object view access is held.

    Job attachments use the same complete active-scope visibility policy as Job
    list and detail views. Other parent models retain their generic default-
    manager lookup. Every failure is a 404 so attachment IDs are not enumerable.
    """
    model_class = content_type.model_class()
    if model_class is None:
        raise Http404

    if model_class._meta.label_lower == "core.job":
        queryset = visible_jobs_for_user(request.user)
    else:
        queryset = model_class._default_manager.all()

    parent = queryset.filter(pk=object_id).first()
    if parent is None:
        raise Http404
    app_label = model_class._meta.app_label
    model_name = model_class._meta.model_name
    if not request.user.has_perm(f"{app_label}.view_{model_name}", obj=parent):
        raise Http404
    return parent


class JournalEntryCreateView(LoginRequiredMixin, View):
    def post(self, request, app_label, model_name, object_id):
        try:
            model_class = apps.get_model(app_label, model_name)
        except LookupError:
            raise Http404 from None
        content_type = ContentType.objects.get_for_model(model_class)
        obj = _attachment_parent_for_change(request, content_type, object_id)
        form = JournalEntryForm(request.POST)
        if form.is_valid():
            JournalEntry.objects.create(
                model=content_type,
                object_id=obj.pk,
                user=request.user,
                comment=form.cleaned_data["comment"],
            )
            messages.success(request, _("Journal entry added."))
        else:
            messages.error(request, _("Could not add journal entry."))
        return HttpResponseRedirect(
            safe_return_url(
                request,
                request.POST.get("return_url") or request.META.get("HTTP_REFERER"),
                obj.get_absolute_url(),
            )
        )


class ImageAttachmentUploadView(LoginRequiredMixin, View):
    def post(self, request, app_label, model_name, object_id):
        try:
            model_class = apps.get_model(app_label, model_name)
        except LookupError:
            raise Http404 from None
        content_type = ContentType.objects.get_for_model(model_class)
        obj = _attachment_parent_for_change(request, content_type, object_id)
        uploaded_file = request.FILES.get("image")
        if uploaded_file:
            try:
                validate_image_attachment(uploaded_file)
            except ValidationError as error:
                # Reject before anything reaches storage: an invalid upload must
                # leave neither an attachment row nor a permanent blob behind.
                messages.error(request, " ".join(error.messages))
            else:
                ImageAttachment.objects.create(
                    model=content_type,
                    object_id=obj.pk,
                    image=uploaded_file,
                    name=uploaded_file.name,
                )
                messages.success(request, _("Image '%(name)s' uploaded.") % {"name": uploaded_file.name})
        return redirect(safe_return_url(request, request.POST.get("return_url"), obj.get_absolute_url()))


class FileAttachmentUploadView(LoginRequiredMixin, View):
    def post(self, request, app_label, model_name, object_id):
        try:
            model_class = apps.get_model(app_label, model_name)
        except LookupError:
            raise Http404 from None
        content_type = ContentType.objects.get_for_model(model_class)
        obj = _attachment_parent_for_change(request, content_type, object_id)
        uploaded_file = request.FILES.get("file")
        if uploaded_file:
            try:
                validate_file_attachment(uploaded_file)
            except ValidationError as error:
                # Reject before anything reaches storage: an invalid upload must
                # leave neither an attachment row nor a permanent blob behind.
                messages.error(request, " ".join(error.messages))
            else:
                mime_type, _encoding = mimetypes.guess_type(uploaded_file.name)
                FileAttachment.objects.create(
                    model=content_type,
                    object_id=obj.pk,
                    file=uploaded_file,
                    name=uploaded_file.name,
                    mime_type=mime_type or "",
                )
                messages.success(request, _("File '%(name)s' uploaded.") % {"name": uploaded_file.name})
        return redirect(safe_return_url(request, request.POST.get("return_url"), obj.get_absolute_url()))


class ImageAttachmentDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        attachment = get_object_or_404(ImageAttachment, pk=pk)
        _attachment_parent_for_change(request, attachment.model, attachment.object_id)
        obj_url = safe_return_url(request, request.POST.get("return_url"), "/")
        attachment.delete()
        messages.success(request, _("Image '%(name)s' deleted.") % {"name": attachment.name})
        return redirect(obj_url)


class FileAttachmentDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        attachment = get_object_or_404(FileAttachment, pk=pk)
        _attachment_parent_for_change(request, attachment.model, attachment.object_id)
        obj_url = safe_return_url(request, request.POST.get("return_url"), "/")
        attachment.delete()
        messages.success(request, _("File '%(name)s' deleted.") % {"name": attachment.name})
        return redirect(obj_url)


class FileAttachmentDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        attachment = get_object_or_404(FileAttachment, pk=pk)
        _attachment_parent_for_view(request, attachment.model, attachment.object_id)
        filename = attachment.name or attachment.file.name.rsplit("/", 1)[-1]
        response = FileResponse(attachment.file.open("rb"), as_attachment=True, filename=filename)
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ImageAttachmentServeView(LoginRequiredMixin, View):
    def get(self, request, pk):
        attachment = get_object_or_404(ImageAttachment, pk=pk)
        _attachment_parent_for_view(request, attachment.model, attachment.object_id)
        with attachment.image.open("rb") as handle:
            verified_type = detect_image_mime(handle)
        if verified_type in SAFE_IMAGE_ATTACHMENT_MIMES:
            response = FileResponse(attachment.image.open("rb"))
            response["Content-Type"] = verified_type
        else:
            # Content that is not a verified raster image (an SVG stored before
            # uploads were validated, say) must never be served inline: force a
            # download with a neutral type instead of guessing from the name.
            filename = attachment.name or attachment.image.name.rsplit("/", 1)[-1]
            response = FileResponse(attachment.image.open("rb"), as_attachment=True, filename=filename)
            response["Content-Type"] = "application/octet-stream"
        response["X-Content-Type-Options"] = "nosniff"
        return response
