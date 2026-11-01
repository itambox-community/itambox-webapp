import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _
from django.views.generic import View

from core import restore_authority
from core.archive_handlers import AggregateArchiveBlocked, restore_object
from core.purge_handlers import TombstonePurgeBlocked, purge_object
from itambox.utils import get_model_viewname
from itambox.views.generic.htmx_responses import is_htmx_request, success_response
from itambox.views.generic.mixins import (
    filter_permitted_rows,
    user_can_mutate_model,
)
from itambox.views.generic.utils import safe_return_url

logger = logging.getLogger(__name__)


def _recycle_bin_list_url(model):
    try:
        return reverse(get_model_viewname(model, "list")) + "?deleted=true"
    except NoReverseMatch:
        return "/"


class HtmxActionMixin:
    """Emit a 204 + HX-Trigger response for HTMX callers; fall back to a
    redirect for plain-HTTP callers.

    Subclasses implement ``perform_action(request)`` which returns a success
    message string and, optionally, sets ``self.list_url`` for the redirect.
    """

    def _htmx_or_redirect(self, request, success_msg, list_url):
        if is_htmx_request(request):
            # No closeModalEvent: restore/purge are page-level actions, not modal
            # submissions.
            return success_response(success_msg, close_modal=False)

        messages.success(request, success_msg)
        return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), list_url))


class ObjectRestoreView(HtmxActionMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    def has_permission(self):
        self.content_type = get_object_or_404(ContentType, pk=self.kwargs["content_type_id"])
        self.model = self.content_type.model_class()
        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name

        # unscoped: restore and purge target soft-deleted rows; tenant authorization is checked per object
        manager = getattr(self.model, "all_objects", self.model._base_manager)
        self.object = get_object_or_404(manager, pk=self.kwargs["object_id"])

        if not user_can_mutate_model(self.request.user, self.model):
            return False

        if not self.request.user.is_superuser and not self.request.user.has_perm("core.change_recyclebin"):
            return False

        return self.request.user.has_perm(f"{app_label}.change_{model_name}", self.object)

    def post(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                restore_authority.validate_restore_grant_authority(request.user, self.object)
                restore_object(self.object, actor=request.user, request=request)
        except AggregateArchiveBlocked as exc:
            # The object's own aggregate service refused the restore (for
            # example a conditional unique slot is taken again): report it and
            # leave the recycle-bin row as it is.
            messages.error(request, exc.user_message)
            return HttpResponseRedirect(
                safe_return_url(request, request.META.get("HTTP_REFERER"), _recycle_bin_list_url(self.model))
            )
        except ValidationError as exc:
            logger.warning(
                "Blocked unsafe restore of %s pk=%s by user pk=%s: %s",
                self.model._meta.label_lower,
                self.object.pk,
                request.user.pk,
                "; ".join(exc.messages),
            )
            raise PermissionDenied(_("Restoring this object would grant permissions outside your authority.")) from exc

        success_msg = _("Restored {model} {object}").format(
            model=self.model._meta.verbose_name,
            object=self.object,
        )

        return self._htmx_or_redirect(request, success_msg, _recycle_bin_list_url(self.model))


class ObjectPurgeView(HtmxActionMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    def has_permission(self):
        self.content_type = get_object_or_404(ContentType, pk=self.kwargs["content_type_id"])
        self.model = self.content_type.model_class()
        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name

        # unscoped: restore and purge target soft-deleted rows; tenant authorization is checked per object
        manager = getattr(self.model, "all_objects", self.model._base_manager)
        self.object = get_object_or_404(manager, pk=self.kwargs["object_id"])

        if not user_can_mutate_model(self.request.user, self.model):
            return False

        if not self.request.user.is_superuser and not self.request.user.has_perm("core.delete_recyclebin"):
            return False

        return self.request.user.has_perm(f"{app_label}.delete_{model_name}", self.object)

    def post(self, request, *args, **kwargs):
        obj_repr = str(self.object)
        try:
            purge_object(self.object)
        except TombstonePurgeBlocked as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect(
                safe_return_url(request, request.META.get("HTTP_REFERER"), _recycle_bin_list_url(self.model))
            )

        success_msg = _("Permanently purged {model} {object}").format(
            model=self.model._meta.verbose_name,
            object=obj_repr,
        )

        return self._htmx_or_redirect(request, success_msg, _recycle_bin_list_url(self.model))


class ObjectBulkRestoreView(HtmxActionMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    def has_permission(self):
        self.content_type = get_object_or_404(ContentType, pk=self.kwargs["content_type_id"])
        self.model = self.content_type.model_class()
        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name

        if not user_can_mutate_model(self.request.user, self.model):
            return False

        if not self.request.user.is_superuser and not self.request.user.has_perm("core.change_recyclebin"):
            return False

        return self.request.user.has_perm(f"{app_label}.change_{model_name}")

    def _restore_validated_batch(self, request, rows):
        """Validate the complete batch, then restore it as a whole.

        Returns ``(error_response, unsafe_skipped)``: an error response when an
        aggregate service refused a row — the batch is rolled back entirely —
        otherwise ``None`` plus the count of rows skipped for missing restore
        authority. The complete batch is validated before anything is restored:
        one restore could otherwise grant authority that changes a later
        decision.
        """
        safe_rows = []
        unsafe_skipped = 0
        try:
            with transaction.atomic():
                for obj in rows:
                    try:
                        restore_authority.validate_restore_grant_authority(request.user, obj)
                    except ValidationError as exc:
                        unsafe_skipped += 1
                        logger.warning(
                            "Skipped unsafe bulk restore of %s pk=%s by user pk=%s: %s",
                            self.model._meta.label_lower,
                            obj.pk,
                            request.user.pk,
                            "; ".join(exc.messages),
                        )
                    else:
                        safe_rows.append(obj)

                for obj in safe_rows:
                    restore_object(obj, actor=request.user, request=request)
        except AggregateArchiveBlocked as exc:
            # An aggregate service refused one of the rows: the reason belongs to
            # that row, so the operator sees the typed message and nothing is
            # restored.
            messages.error(request, exc.user_message)
            return (
                HttpResponseRedirect(
                    safe_return_url(request, request.META.get("HTTP_REFERER"), _recycle_bin_list_url(self.model))
                ),
                unsafe_skipped,
            )
        return None, unsafe_skipped

    def post(self, request, *args, **kwargs):
        pks = request.POST.getlist("pk")
        if not pks:
            messages.warning(request, _("No items selected."))
            return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))

        # unscoped: restore and purge target soft-deleted rows; tenant authorization is checked per object
        manager = getattr(self.model, "all_objects", self.model._base_manager)
        queryset = manager.filter(pk__in=pks, deleted_at__isnull=False)

        # Per-row change-perm enforcement (see filter_permitted_rows): the
        # dispatch gate alone is too coarse inside a multi-tenant group scope.
        rows, skipped = filter_permitted_rows(request.user, queryset, self.model, "change")
        if skipped:
            messages.warning(
                request,
                _("Skipped %(count)s %(objects)s you do not have permission to change.")
                % {"count": skipped, "objects": self.model._meta.verbose_name_plural},
            )

        error_response, unsafe_skipped = self._restore_validated_batch(request, rows)
        if error_response is not None:
            return error_response

        if unsafe_skipped:
            messages.warning(
                request,
                _(
                    "Skipped %(count)s %(objects)s because restoring them would grant "
                    "permissions outside your authority."
                )
                % {
                    "count": unsafe_skipped,
                    "objects": self.model._meta.verbose_name_plural,
                },
            )

        success_msg = _("Successfully restored {count} {model_plural}.").format(
            count=len(rows) - unsafe_skipped,
            model_plural=self.model._meta.verbose_name_plural,
        )

        return self._htmx_or_redirect(request, success_msg, _recycle_bin_list_url(self.model))


class ObjectBulkPurgeView(HtmxActionMixin, PermissionRequiredMixin, LoginRequiredMixin, View):
    def has_permission(self):
        self.content_type = get_object_or_404(ContentType, pk=self.kwargs["content_type_id"])
        self.model = self.content_type.model_class()
        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name

        if not user_can_mutate_model(self.request.user, self.model):
            return False

        if not self.request.user.is_superuser and not self.request.user.has_perm("core.delete_recyclebin"):
            return False

        return self.request.user.has_perm(f"{app_label}.delete_{model_name}")

    def post(self, request, *args, **kwargs):
        pks = request.POST.getlist("pk")
        if not pks:
            messages.warning(request, _("No items selected."))
            return HttpResponseRedirect(safe_return_url(request, request.META.get("HTTP_REFERER"), "/"))

        # unscoped: restore and purge target soft-deleted rows; tenant authorization is checked per object
        manager = getattr(self.model, "all_objects", self.model._base_manager)
        queryset = manager.filter(pk__in=pks, deleted_at__isnull=False)

        # Per-row delete-perm enforcement (see filter_permitted_rows): the
        # dispatch gate alone is too coarse inside a multi-tenant group scope.
        rows, skipped = filter_permitted_rows(request.user, queryset, self.model, "delete")
        if skipped:
            messages.warning(
                request,
                _("Skipped %(count)s %(objects)s you do not have permission to delete.")
                % {"count": skipped, "objects": self.model._meta.verbose_name_plural},
            )

        count = 0
        skipped_tombstones = 0
        with transaction.atomic():
            for obj in rows:
                try:
                    purge_object(obj)
                except TombstonePurgeBlocked:
                    skipped_tombstones += 1
                else:
                    count += 1

        if skipped_tombstones:
            messages.warning(
                request,
                _("Skipped %(count)s permanent schema tombstone(s).") % {"count": skipped_tombstones},
            )

        success_msg = _("Successfully permanently purged {count} {model_plural}.").format(
            count=count,
            model_plural=self.model._meta.verbose_name_plural,
        )

        return self._htmx_or_redirect(request, success_msg, _recycle_bin_list_url(self.model))
