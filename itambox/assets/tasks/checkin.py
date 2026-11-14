"""Background task: scanner-driven bulk check-in of hardware assets.

Mirrors ``bulk_checkout_task`` — wraps the run in ``TaskContext`` so change-log
entries are attributed, locks each asset row with ``select_for_update``, and
delegates the per-asset state change to the canonical ``checkin_asset`` service
so single and bulk check-in stay behaviourally identical.
"""

import datetime
import logging
from collections.abc import Sequence

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from assets import services
from assets.models import Asset, StatusLabel
from core.models import Job, Notification
from core.tasks.context import TaskContext
from core.tasks.utils import TaskResult, TaskStatus, classify_task_error, reverse_job_detail
from organization.models import Location

logger = logging.getLogger(__name__)


def _parse_date(value: str | datetime.date | None) -> datetime.date | None:
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _checkin_item(
    job,
    pk,
    log_extra,
    ctx,
    status,
    location,
    resolved_date,
    notes,
) -> str:
    """Run one asset check-in under a per-item transaction.

    Returns "success", "skipped", or "failed"; one item's failure never
    aborts the batch.
    """
    try:
        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(pk=pk)
            # location=None → checkin_asset() preserves the asset's current location.
            result = services.checkin_asset(
                asset,
                user=ctx.user,
                status=status,
                location=location,
                checkin_date=resolved_date,
                notes=notes,
            )
        if result is None:
            return "skipped"
        return "success"
    # broad except: boundary-isolation: one asset failure must not abort the requested batch
    except Exception as ex:
        logger.warning(
            "Bulk check-in item failed",
            extra={**log_extra, "object_id": pk, "exception_type": type(ex).__name__},
        )
        return "failed"


def _finish_checkin(
    job,
    ctx,
    success_count,
    skipped_count,
    failure_count,
    total,
) -> TaskResult:
    """Finalize a bulk check-in: all-failed vs completed result and notification."""
    if success_count == 0 and skipped_count == 0:
        job.mark_failed("All asset check-ins failed.")
        Notification.objects.create(
            user=ctx.user,
            kind=Notification.KIND_PERSONAL,
            subject=_("Bulk Check-in Failed"),
            message=_("All hardware check-ins failed. View logs for details."),
            level=Notification.LEVEL_DANGER,
            target_url=reverse_job_detail(job.pk),
        )
        return TaskResult(
            TaskStatus.TERMINAL,
            "checkin.all_failed",
            {"failed": failure_count, "total": total},
            "All asset check-ins failed.",
            True,
        )
    job.mark_completed(
        result={
            "checked_in": success_count,
            "skipped": skipped_count,
            "failed": failure_count,
            "total": total,
        }
    )
    Notification.objects.create(
        user=ctx.user,
        kind=Notification.KIND_PERSONAL,
        subject=_("Bulk Check-in Complete"),
        message=ngettext("Checked in %(count)s asset.", "Checked in %(count)s assets.", success_count)
        % {"count": success_count},
        level=Notification.LEVEL_SUCCESS,
        target_url=reverse_job_detail(job.pk),
    )
    return TaskResult(
        TaskStatus.PARTIAL if failure_count else TaskStatus.SUCCESS,
        "checkin.partial" if failure_count else "checkin.completed",
        {"checked_in": success_count, "skipped": skipped_count, "failed": failure_count},
        user_visible=True,
    )


def _run_checkin(
    job,
    ctx,
    job_id,
    log_extra,
    asset_pks,
    status_id,
    location_id,
    checkin_date,
    notes,
) -> TaskResult:
    """Resolve check-in overrides, process every asset and finalize the job."""
    status = StatusLabel.objects.filter(pk=status_id).first() if status_id else None
    location = Location.objects.filter(pk=location_id).first() if location_id else None
    resolved_date = _parse_date(checkin_date)

    success_count = 0
    skipped_count = 0
    failure_count = 0

    for pk in asset_pks:
        outcome = _checkin_item(
            job,
            pk,
            log_extra,
            ctx,
            status,
            location,
            resolved_date,
            notes,
        )
        if outcome == "success":
            success_count += 1
            job.append_log(f" - Asset PK {pk} checked in successfully.")
        elif outcome == "skipped":
            skipped_count += 1
            job.append_log(f" - Asset PK {pk} skipped (not checked out).")
        else:
            failure_count += 1
            job.append_log(f" - Asset PK {pk} failed [checkin.item_failed].")

    job.append_log(
        f"Bulk check-in finished. Checked in: {success_count} | Skipped: {skipped_count} | Failures: {failure_count}"
    )

    return _finish_checkin(
        job,
        ctx,
        success_count,
        skipped_count,
        failure_count,
        len(asset_pks),
    )


def _deny_permission(job, *, tenant_id, user_id) -> TaskResult:
    logger.warning(
        "Bulk check-in denied [checkin.permission_revoked] tenant_id=%s actor_id=%s job_id=%s",
        tenant_id,
        user_id,
        job.pk,
        extra={
            "tenant_id": tenant_id,
            "actor_id": user_id,
            "job_id": job.pk,
            "code": "checkin.permission_revoked",
        },
    )
    job.mark_failed("[terminal] checkin.permission_revoked")
    return TaskResult(TaskStatus.TERMINAL, "checkin.permission_revoked")


def _claim_job(job_id, tenant_id, user_id, asset_count) -> tuple[Job | None, TaskResult | None]:
    log_extra = {"tenant_id": tenant_id, "actor_id": user_id, "job_id": job_id}
    try:
        job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
    except Job.DoesNotExist:
        logger.error("Bulk check-in job not found", extra=log_extra)
        return None, TaskResult(TaskStatus.TERMINAL, "checkin.job_not_found")
    # broad except: task-isolation: no Job can be persisted when the claim lookup itself fails
    except Exception as exc:
        logger.error("Bulk check-in entry failed", extra={**log_extra, "exception_type": type(exc).__name__})
        return None, TaskResult(classify_task_error(exc), "checkin.entry_failed")
    if not job.mark_running():
        logger.info("Job %s is no longer pending (cancelled?); skipping check-in.", job_id)
        return None, TaskResult(TaskStatus.SKIPPED, "checkin.job_not_pending")
    job.append_log("Initializing asynchronous bulk check-in pipeline...")
    job.append_log(f"Assets to process: {asset_count}")
    return job, None


def bulk_checkin_task(
    job_id: int,
    asset_pks: Sequence[int | str],
    user_id: int | None,
    tenant_id: int | None = None,
    status_id: int | str | None = None,
    location_id: int | str | None = None,
    checkin_date: str | datetime.date | None = None,
    notes: str = "",
) -> TaskResult:
    """Asynchronously check in selected hardware assets.

    Assets with no active assignment (and no location) are a no-op in
    ``checkin_asset`` — they are counted as *skipped* rather than failed.
    """
    job, claim_result = _claim_job(job_id, tenant_id, user_id, len(asset_pks))
    if claim_result is not None:
        return claim_result
    assert job is not None
    entry_log_extra = {"tenant_id": tenant_id, "actor_id": user_id, "job_id": job_id}

    try:
        with TaskContext(tenant_id=tenant_id, user_id=user_id, operation="assets.bulk_checkin") as ctx:
            log_extra = {**ctx.log_context, "job_id": job_id}
            # Execution-time RBAC recheck: enqueue-time authorization
            # is not enough — a permission revoked between submission and worker
            # execution must fail closed before any asset/status/location state is
            # resolved or mutated. No Notification is created on denial.
            if ctx.user is None or ctx.tenant is None or not ctx.user.has_perm("assets.change_asset", obj=ctx.tenant):
                return _deny_permission(job, tenant_id=tenant_id, user_id=user_id)

            try:
                return _run_checkin(
                    job,
                    ctx,
                    job_id,
                    log_extra,
                    asset_pks,
                    status_id,
                    location_id,
                    checkin_date,
                    notes,
                )

            # broad except: task-isolation: record a safe typed task-boundary failure
            except Exception as e:
                status = classify_task_error(e)
                logger.error("Bulk check-in task failed", extra={**log_extra, "exception_type": type(e).__name__})
                job.mark_failed(f"[{status.value}] checkin.boundary_failed")
                Notification.objects.create(
                    user=ctx.user,
                    kind=Notification.KIND_PERSONAL,
                    subject=_("Bulk Check-in Error"),
                    message=_("The bulk check-in could not be completed. Code: checkin.boundary_failed"),
                    level=Notification.LEVEL_DANGER,
                    target_url=reverse_job_detail(job.pk),
                )
                return TaskResult(status, "checkin.boundary_failed", user_visible=True)
    except PermissionDenied:
        return _deny_permission(job, tenant_id=tenant_id, user_id=user_id)
    # broad except: task-isolation: persist failures after the Job claim
    except Exception as exc:
        status = classify_task_error(exc)
        logger.error("Bulk check-in entry failed", extra={**entry_log_extra, "exception_type": type(exc).__name__})
        job.mark_failed(f"[{status.value}] checkin.entry_failed")
        return TaskResult(status, "checkin.entry_failed")
