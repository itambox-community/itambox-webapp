"""Queued label printing for large selections of any labelled model.

The web request authorizes the selection and persists its tenant scope on the
Job; the task re-resolves every object inside that persisted scope and re-checks
the per-object ``view`` permission, so access revoked while the job waited is
honoured. Rendering is the shared engine in :mod:`extras.services.labels`.
"""

import logging
from collections.abc import Sequence

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from core.models import Job, Notification
from core.pdf_renderer import html_to_pdf_bytes
from core.tasks.context import TaskContext
from core.tasks.utils import TaskResult, TaskStatus, classify_task_error, reverse_job_detail
from extras.models import FileAttachment, LabelTemplate
from extras.services.labels import (
    LAYOUT_ROLL,
    LAYOUTS,
    build_labels_document,
    default_label_template,
    render_object_card,
    template_applies,
)
from itambox.labels import label_registry

logger = logging.getLogger(__name__)

TASK_PATH = "extras.tasks.labels.generate_label_pdf_batch_task"


def _try_create_notification(user, *, subject, message, level, target_url=None, log_extra=None, job=None):
    """Deliver a task notification best-effort (failure isolation).

    Notification delivery must never reverse an already-durable task outcome
    (completed job + persisted attachment) nor mask the original task error.
    """
    try:
        Notification.objects.create(user=user, subject=subject, message=message, level=level, target_url=target_url)
    # broad except: availability-tradeoff: notification delivery is best-effort and must
    # never reverse a durable task outcome (completed job + persisted attachment) nor mask
    # the original task error, so creation failures are logged with safe metadata only.
    except Exception as exc:
        logger.error(
            "Task notification delivery failed",
            extra={**(log_extra or {}), "phase": "notification", "exception_type": type(exc).__name__},
        )
        if job is not None:
            try:
                job.append_log("Notification delivery failed (phase=notification).")
            # broad except: boundary-isolation: a failing job-log append must not cascade
            # while handling a notification failure
            except Exception as log_exc:
                logger.error(
                    "Job log append failed while handling notification failure",
                    extra={**(log_extra or {}), "phase": "notification", "exception_type": type(log_exc).__name__},
                )


def _cleanup_partial_attachment(attachment, log_extra):
    """Remove a partial attachment (row and stored file) after a failed persistence step."""
    if attachment is None:
        return
    try:
        attachment.file.delete(save=False)
        attachment.delete()
    # broad except: availability-tradeoff: attachment cleanup is best-effort; a cleanup
    # failure must not cascade into the already-recorded task boundary outcome
    except Exception as exc:
        logger.error(
            "Attachment cleanup failed",
            extra={**log_extra, "phase": "attachment_cleanup", "exception_type": type(exc).__name__},
        )


def _scope_tenant_ids(job, tenant_id):
    """The persisted authorized scope (fail-closed when absent)."""
    scope = job.data.get("scope_tenant_ids") or ([tenant_id] if tenant_id is not None else [])
    return [int(scope_id) for scope_id in scope]


def _labelled_model(content_type_id):
    try:
        model = ContentType.objects.get_for_id(int(content_type_id)).model_class()
    except (ContentType.DoesNotExist, TypeError, ValueError):
        return None
    return model if model is not None and label_registry.is_labelled(model) else None


def resolve_label_objects(model, object_pks, scope_tenant_ids, user, *, include_shared=False):
    """Re-resolve ``object_pks`` inside the persisted scope; ``(objects, failure)``.

    ``failure`` is ``"inaccessible"`` when any requested object is outside the
    scope, deleted, or no longer viewable by ``user``; ``"empty"`` when nothing
    was requested. Shared (tenantless) rows are admitted only when the request
    that queued the job selected some (``include_shared``), and they still need
    the per-object permission. Order follows ``object_pks``.
    """
    requested = []
    for raw_pk in object_pks:
        pk = int(raw_pk)
        if pk not in requested:
            requested.append(pk)
    if not requested:
        return [], "empty"
    in_scope = Q(tenant_id__in=scope_tenant_ids)
    if include_shared:
        in_scope |= Q(tenant__isnull=True)
    rows = model._base_manager.filter(in_scope, pk__in=requested)
    if any(field.name == "deleted_at" for field in model._meta.fields):
        rows = rows.filter(deleted_at__isnull=True)
    by_pk = {row.pk: row for row in rows}
    if len(by_pk) != len(requested):
        return [], "inaccessible"
    objects = [by_pk[pk] for pk in requested]
    perm = f"{model._meta.app_label}.view_{model._meta.model_name}"
    if user is not None and not user.is_superuser and not all(user.has_perm(perm, obj=obj) for obj in objects):
        return [], "inaccessible"
    return objects, None


def _finish_unresolved(job, failure):
    if failure == "inaccessible":
        job.append_log("Some selected objects are no longer accessible.")
        job.mark_failed("[terminal] labels.objects_not_accessible")
        return TaskResult(TaskStatus.TERMINAL, "labels.objects_not_accessible", user_visible=True)
    job.append_log("No objects were selected.")
    job.mark_completed(result={"status": "no_objects"})
    return TaskResult(TaskStatus.SKIPPED, "labels.no_objects", user_visible=True)


def _resolve_template(job, model, template_id, log_extra):
    if template_id in (None, ""):
        return default_label_template()
    try:
        template = LabelTemplate.objects.get(pk=int(template_id))
    except (LabelTemplate.DoesNotExist, TypeError, ValueError):
        template = None
    if template is None or not template_applies(template, model):
        logger.error("Label template not usable", extra={**log_extra, "template_id": template_id})
        job.mark_failed("[terminal] labels.template_not_found")
        return TaskResult(TaskStatus.TERMINAL, "labels.template_not_found", user_visible=True)
    return template


def _render_cards(job, objects, template, log_extra):
    """One card per object; one failure is logged and never aborts the batch."""
    cards = []
    for obj in objects:
        try:
            cards.append(render_object_card(obj, template))
            job.append_log(f" - Rendered label for object PK {obj.pk}.")
        # broad except: boundary-isolation: one label failure must not abort the requested batch
        except Exception as exc:
            job.append_log(f" - Object PK {obj.pk} failed [labels.item_failed].")
            logger.warning(
                "Label rendering failed",
                extra={**log_extra, "object_id": obj.pk, "exception_type": type(exc).__name__},
            )
    return cards


def _persist_pdf(job, pdf_bytes, log_extra):
    content_type = ContentType.objects.get_for_model(Job)
    attachment = FileAttachment(
        model=content_type, object_id=job.pk, name=f"labels_batch_{job.pk}.pdf", mime_type="application/pdf"
    )
    try:
        attachment.save()
        attachment.file.save(f"labels_batch_{job.pk}.pdf", ContentFile(pdf_bytes))
        attachment.save()
    # broad except: cleanup-reraise: persistence is all-or-nothing across the database
    # row and storage object, while the outer task boundary retains error classification
    except Exception:
        _cleanup_partial_attachment(attachment, log_extra)
        raise
    return attachment


def _complete(job, user, log_extra, attachment, total, rendered):
    job.append_log("PDF document generated and saved successfully.")
    job.mark_completed(result={"file_name": attachment.name, "download_url": attachment.get_download_url()})
    _try_create_notification(
        user,
        subject=_("Label Generation Complete"),
        message=ngettext(
            "Successfully generated a label PDF for %(count)s object. Click to download.",
            "Successfully generated a label PDF for %(count)s objects. Click to download.",
            rendered,
        )
        % {"count": rendered},
        level=Notification.LEVEL_SUCCESS,
        target_url=attachment.get_download_url(),
        log_extra=log_extra,
        job=job,
    )
    partial = rendered < total
    return TaskResult(
        TaskStatus.PARTIAL if partial else TaskStatus.SUCCESS,
        "labels.pdf_partial" if partial else "labels.pdf_completed",
        {"rendered": rendered, "failed": total - rendered},
        user_visible=True,
    )


def _run(job, ctx, content_type_id, object_pks, template_id, layout_mode, tenant_id, log_extra, state):
    state["phase"] = "model_resolve"
    model = _labelled_model(content_type_id)
    if model is None:
        job.mark_failed("[terminal] labels.model_not_labelled")
        return TaskResult(TaskStatus.TERMINAL, "labels.model_not_labelled", user_visible=True)

    state["phase"] = "template_resolve"
    template = _resolve_template(job, model, template_id, log_extra)
    if isinstance(template, TaskResult):
        return template
    layout = layout_mode if layout_mode in LAYOUTS else LAYOUT_ROLL
    job.append_log(f"Layout mode: {layout} | Objects requested: {len(object_pks)}")

    state["phase"] = "object_resolve"
    objects, failure = resolve_label_objects(
        model,
        object_pks,
        _scope_tenant_ids(job, tenant_id),
        ctx.user,
        include_shared=bool(job.data.get("include_shared")),
    )
    if failure is not None:
        return _finish_unresolved(job, failure)

    state["phase"] = "label_render"
    cards = _render_cards(job, objects, template, log_extra)
    if not cards:
        job.mark_failed("[terminal] labels.no_labels_rendered")
        return TaskResult(TaskStatus.TERMINAL, "labels.no_labels_rendered", user_visible=True)

    state["phase"] = "pdf_render"
    pdf_bytes = html_to_pdf_bytes(build_labels_document(cards, template, layout))

    state["phase"] = "attachment_persist"
    state["attachment"] = _persist_pdf(job, pdf_bytes, log_extra)
    return _complete(job, ctx.user, log_extra, state["attachment"], len(objects), len(cards))


def generate_label_pdf_batch_task(
    job_id: int,
    content_type_id: int,
    object_pks: Sequence[int | str],
    template_id: int | None,
    layout_mode: str,
    user_id: int | None,
    tenant_id: int | None = None,
) -> TaskResult:
    """Render one PDF of labels for ``object_pks`` and attach it to the Job."""
    with TaskContext(tenant_id=tenant_id, user_id=user_id, operation="labels.pdf_batch") as ctx:
        log_extra = {**ctx.log_context, "job_id": job_id}
        state: dict[str, object] = {"phase": "job_resolve", "attachment": None}
        job = None
        try:
            job = Job.objects.filter(pk=job_id).first()
            if job is None:
                logger.error("Label PDF job not found", extra=log_extra)
                return TaskResult(TaskStatus.TERMINAL, "labels.job_not_found")
            state["phase"] = "job_start"
            if not job.mark_running():
                logger.info("Job %s is no longer pending (cancelled?); skipping label generation.", job_id)
                return TaskResult(TaskStatus.SKIPPED, "labels.job_not_pending")
            job.append_log("Starting PDF label batch generation...")
            return _run(job, ctx, content_type_id, object_pks, template_id, layout_mode, tenant_id, log_extra, state)
        # broad except: task-isolation: record a safe typed task-boundary failure
        except Exception as exc:
            status = classify_task_error(exc)
            logger.error(
                "Label PDF task failed",
                extra={**log_extra, "phase": state["phase"], "exception_type": type(exc).__name__},
            )
            if job is not None:
                job.append_log(f"[{status.value}] labels.pdf_failed phase={state['phase']}")
                job.mark_failed(f"[{status.value}] labels.pdf_failed")
                _cleanup_partial_attachment(state["attachment"], log_extra)
            _try_create_notification(
                ctx.user,
                subject=_("Label Generation Failed"),
                message=_("Label generation failed. Code: labels.pdf_failed"),
                level=Notification.LEVEL_DANGER,
                target_url=reverse_job_detail(job.pk) if job is not None else None,
                log_extra=log_extra,
                job=job,
            )
            return TaskResult(status, "labels.pdf_failed", user_visible=True)
