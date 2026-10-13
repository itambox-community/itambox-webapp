import logging
from collections.abc import Mapping, Sequence

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from core.context import get_current_request_id
from core.importers.bulk_forms import get_import_form_class
from core.models import Job, Notification

from .context import TaskContext
from .utils import reverse_job_detail

logger = logging.getLogger(__name__)
IMPORT_ABORT_MESSAGE = "The import could not be completed due to an unexpected error."


def _task_log_extra(*, operation, tenant_id, actor_id, exception_type=None):
    request_id = get_current_request_id()
    context = {
        "operation": operation,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "request_id": str(request_id) if request_id else None,
    }
    if exception_type is not None:
        context["exception_type"] = exception_type
    return {"import_context": context}


def _log_skipped(job, form):
    """Report rows the form deliberately left untouched (existing records) in the job log."""
    skipped = list(getattr(form, "skipped_list", ()))
    if skipped:
        job.append_log(
            ngettext("Skipped %(count)s existing record:", "Skipped %(count)s existing records:", len(skipped))
            % {"count": len(skipped)}
        )
        for note in skipped:
            job.append_log(f" - {note}")
    return skipped


def import_csv_task(
    job_id: int,
    rows_data: Sequence[Mapping[str, object]],
    app_label: str,
    model_name: str,
    user_id: int | None,
    tenant_id: int | None = None,
) -> None:
    """
    Asynchronously imports parsed CSV/YAML rows into a target model
    using the dynamic BulkImportForm schema inside database transactions.
    """
    with TaskContext(tenant_id=tenant_id, user_id=user_id) as ctx:
        try:
            try:
                job = Job.objects.get(pk=job_id)
            except Job.DoesNotExist:
                logger.error(f"Job {job_id} not found during async import.")
                return

            if not job.mark_running():
                logger.info("Job %s is no longer pending (cancelled?); skipping import.", job_id)
                return
            job.append_log("Initializing asynchronous import pipeline...")
            job.append_log(f"Target model: {app_label}.{model_name} | Row Count: {len(rows_data)}")

            try:
                model = ContentType.objects.get(app_label=app_label, model=model_name).model_class()
                if not model:
                    raise ValidationError(
                        _("Target model %(app_label)s.%(model_name)s could not be resolved.")
                        % {"app_label": app_label, "model_name": model_name}
                    )

                ImportFormClass = get_import_form_class(model)

                form = ImportFormClass()
                form._rows_data = rows_data
                # Updates are authorized per object against the importing actor.
                form.actor = ctx.user

                job.append_log("Validating and importing records inside transaction...")

                with transaction.atomic():
                    imported_count, errors = form.import_data()

                job.append_log(
                    ngettext(
                        "Import finished. Successfully imported %(count)s record.",
                        "Import finished. Successfully imported %(count)s records.",
                        imported_count,
                    )
                    % {"count": imported_count}
                )

                skipped = _log_skipped(job, form)

                if errors:
                    job.append_log(
                        ngettext(
                            "Encountered %(count)s error during processing:",
                            "Encountered %(count)s errors during processing:",
                            len(errors),
                        )
                        % {"count": len(errors)}
                    )
                    for err in errors:
                        job.append_log(f" - {err}")

                    if imported_count == 0:
                        job.mark_failed("All records failed to import due to validation errors.")
                        Notification.objects.create(
                            user=ctx.user,
                            subject=_("Bulk Import Failed"),
                            message=_("Failed to import CSV/YAML data to %(model)s. View job logs for details.")
                            % {"model": model._meta.verbose_name_plural},
                            level=Notification.LEVEL_DANGER,
                            target_url=reverse_job_detail(job.pk),
                        )
                        return

                result = {"imported": imported_count, "failed": len(errors), "total": len(rows_data)}
                result.update({"skipped": len(skipped)} if skipped else {})
                job.mark_completed(result=result)

                Notification.objects.create(
                    user=ctx.user,
                    subject=_("Bulk Import Complete"),
                    message=ngettext(
                        "Successfully imported %(count)s record to %(model)s.",
                        "Successfully imported %(count)s records to %(model)s.",
                        imported_count,
                    )
                    % {"count": imported_count, "model": model._meta.verbose_name_plural},
                    level=Notification.LEVEL_SUCCESS,
                    target_url=reverse_job_detail(job.pk),
                )

            except Exception as exc:
                # broad except: task-isolation: task aborts must leave a safe job result and notification
                extra = _task_log_extra(
                    operation="task.run",
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    exception_type=type(exc).__name__,
                )
                logger.error("Asynchronous import aborted import_context=%s", extra["import_context"], extra=extra)
                job.mark_failed(IMPORT_ABORT_MESSAGE)
                Notification.objects.create(
                    user=ctx.user,
                    subject=_("Bulk Import Error"),
                    message=_("A system error occurred during the import. View job logs for details."),
                    level=Notification.LEVEL_DANGER,
                    target_url=reverse_job_detail(job.pk),
                )
        except Exception as exc:
            # broad except: cleanup-reraise: record safe task context before worker-level propagation
            extra = _task_log_extra(
                operation="task.cleanup",
                tenant_id=tenant_id,
                actor_id=user_id,
                exception_type=type(exc).__name__,
            )
            logger.error("Asynchronous import cleanup failed import_context=%s", extra["import_context"], extra=extra)
            raise
