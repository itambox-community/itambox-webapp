"""The one label print route for every labelled model.

``labels/<app_label>/<model_name>/`` serves a single object (``?pk=7`` from a
detail page) and a list selection (``pk`` values posted from the list
toolbar) alike. The first step shows what will be printed and lets the user
pick a template and layout; the second renders. Small selections come back as
an inline PDF; large ones become a background Job with the same engine.
"""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils.text import get_valid_filename
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.generic import View
from django_q.tasks import async_task

from core.managers import Scope
from core.models import Job
from extras.models import LabelTemplate
from extras.services.labels import (
    LAYOUT_CHOICES,
    LAYOUT_ROLL,
    LabelNotApplicable,
    default_label_template,
    render_labels_pdf,
    template_applies,
)
from extras.tasks.labels import TASK_PATH
from itambox.labels import label_registry
from itambox.views.generic.utils import safe_return_url

# Up to this many labels render inside the request; more become a Job.
LABEL_SYNC_LIMIT = 25
# Upper bound for one request; a larger print run is split by the operator.
LABEL_SELECTION_LIMIT = 1000

BUILT_IN_TEMPLATE = ""


class LabelPrintForm(forms.Form):
    template = forms.ChoiceField(label=_("Label Template"), required=False)
    layout = forms.ChoiceField(label=_("Layout"), choices=LAYOUT_CHOICES, initial=LAYOUT_ROLL)

    def __init__(self, *args, model, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model
        saved = [template for template in LabelTemplate.for_model(model) if template_applies(template, model)]
        choices = [(str(template.pk), self._describe(template)) for template in saved]
        choices.append((BUILT_IN_TEMPLATE, self._describe(default_label_template())))
        self.fields["template"].choices = choices
        self.fields["template"].initial = choices[0][0]

    @staticmethod
    def _describe(template):
        size = f"{template.page_width:g} x {template.page_height:g} in"
        return f"{template.name} ({size}, {template.get_barcode_format_display()})"

    def clean_template(self):
        value = self.cleaned_data.get("template") or BUILT_IN_TEMPLATE
        if value == BUILT_IN_TEMPLATE:
            return default_label_template()
        template = LabelTemplate.objects.filter(pk=int(value)).first() if value.isdigit() else None
        if template is None or not template_applies(template, self.model):
            raise ValidationError(_("This template does not print labels for this object type."))
        return template


def _parse_pks(raw_pks):
    try:
        pks = [int(pk) for pk in raw_pks]
    except (TypeError, ValueError):
        return None
    if any(pk <= 0 for pk in pks) or len(set(pks)) != len(pks):
        return None
    return pks


class LabelPrintView(LoginRequiredMixin, View):
    template_name = "generic/label_print.html"

    def dispatch(self, request, *args, **kwargs):
        self.model = label_registry.by_label(f"{kwargs['app_label']}.{kwargs['model_name']}")
        if self.model is None:
            raise Http404("this object type carries no label")
        meta = self.model._meta
        self.view_perm = f"{meta.app_label}.view_{meta.model_name}"
        if request.user.is_authenticated and not request.user.has_perm(self.view_perm):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    # -- selection ---------------------------------------------------------

    def _resolve(self, raw_pks):
        """The selected objects in request order, or ``(None, message)``.

        Objects resolve through the active tenant/group/aggregate scope, then
        each needs the per-object ``view`` permission: a label discloses the
        object's identity, and scope visibility is a separate boundary.
        """
        invalid = _("The selected objects are invalid or no longer accessible.")
        pks = _parse_pks(raw_pks)
        if not pks:
            return None, _("No objects selected for label printing.") if pks == [] else invalid
        if len(pks) > LABEL_SELECTION_LIMIT:
            return None, _("Print at most %(limit)s labels at once.") % {"limit": LABEL_SELECTION_LIMIT}
        rows = {obj.pk: obj for obj in self.model.objects.for_scope(Scope.current()).filter(pk__in=pks)}
        if len(rows) != len(pks):
            return None, invalid
        objects = [rows[pk] for pk in pks]
        if not all(self.request.user.has_perm(self.view_perm, obj=obj) for obj in objects):
            return None, invalid
        return objects, None

    def _return_url(self):
        meta = self.model._meta
        try:
            fallback = reverse(f"{meta.app_label}:{meta.model_name}_list")
        except NoReverseMatch:
            fallback = reverse("dashboard")
        return safe_return_url(
            self.request, self.request.POST.get("return_url") or self.request.GET.get("return_url"), fallback
        )

    def _page(self, raw_pks, form=None, *, status=200):
        objects, error = self._resolve(raw_pks)
        if error:
            messages.error(self.request, error)
        meta = self.model._meta
        context = {
            "title": _("Print Labels"),
            "model_verbose_name": meta.verbose_name,
            "model_verbose_name_plural": meta.verbose_name_plural,
            "objects": (objects or [])[:10],
            "object_count": len(objects or []),
            "object_pks": [obj.pk for obj in objects or []],
            "form": form or LabelPrintForm(model=self.model),
            "can_print": objects is not None,
            "queued": len(objects or []) > LABEL_SYNC_LIMIT,
            "return_url": self._return_url(),
            "can_manage_templates": self.request.user.is_superuser,
        }
        return render(self.request, self.template_name, context, status=status)

    def get(self, request, app_label, model_name):
        return self._page(request.GET.getlist("pk"))

    # -- printing ----------------------------------------------------------

    def post(self, request, app_label, model_name):
        raw_pks = request.POST.getlist("pk")
        if "_print" not in request.POST:
            return self._page(raw_pks)
        form = LabelPrintForm(request.POST, model=self.model)
        objects, error = self._resolve(raw_pks)
        if error or not form.is_valid():
            return self._page(raw_pks, form, status=400)
        template = form.cleaned_data["template"]
        layout = form.cleaned_data["layout"]
        if len(objects) > LABEL_SYNC_LIMIT:
            return self._enqueue(objects, template, layout)
        try:
            pdf_bytes = render_labels_pdf(objects, template, layout)
        except LabelNotApplicable:
            messages.error(request, _("This template cannot print labels for every selected object."))
            return self._page(raw_pks, form, status=400)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        stem = objects[0].pk if len(objects) == 1 else f"{len(objects)}"
        filename = get_valid_filename(f"labels_{self.model._meta.model_name}_{stem}.pdf")
        response["Content-Disposition"] = 'inline; filename="' + filename + '"'
        response["Cache-Control"] = "private, no-store"
        return response

    def _enqueue(self, objects, template, layout):
        tenant_ids = sorted({obj.tenant_id for obj in objects if obj.tenant_id is not None})
        owner_tenant_id = next((obj.tenant_id for obj in objects if obj.tenant_id is not None), None)
        if owner_tenant_id is None and not self.request.user.is_superuser:
            # A Job without a tenant is visible to superusers only; its owner could
            # never follow it. Shared objects print in batches of LABEL_SYNC_LIMIT.
            messages.error(
                self.request,
                _("Print at most %(limit)s shared objects at once.") % {"limit": LABEL_SYNC_LIMIT},
            )
            return self._page([obj.pk for obj in objects], status=400)
        content_type = ContentType.objects.get_for_model(self.model)
        job = Job.objects.create(
            name=ngettext(
                "Label printing: %(count)s %(object)s",
                "Label printing: %(count)s %(objects)s",
                len(objects),
            )
            % {
                "count": len(objects),
                "object": self.model._meta.verbose_name,
                "objects": self.model._meta.verbose_name_plural,
            },
            tenant_id=owner_tenant_id,
            model=content_type,
            status=Job.STATUS_PENDING,
            data={
                "scope_tenant_ids": tenant_ids,
                "include_shared": any(obj.tenant_id is None for obj in objects),
            },
        )
        args = (
            TASK_PATH,
            job.pk,
            content_type.pk,
            [obj.pk for obj in objects],
            template.pk,
            layout,
            self.request.user.pk,
            owner_tenant_id,
        )
        if getattr(settings, "Q_CLUSTER", {}).get("sync", False):
            async_task(*args)
        else:
            transaction.on_commit(lambda: async_task(*args))
        messages.success(
            self.request,
            _("Label printing job '%(job)s' queued. Track its progress in real time.") % {"job": job.name},
        )
        redirect_url = reverse("job_detail", kwargs={"pk": job.pk})
        if getattr(self.request, "htmx", False):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = redirect_url
            return response
        return HttpResponseRedirect(redirect_url)
