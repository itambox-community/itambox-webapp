"""Background task: scanner-driven bulk checkout of hardware assets.

Wraps the run in ``TaskContext`` so change-log entries are attributed, locks
each asset row with ``select_for_update``, and delegates the per-asset state
change to the canonical ``checkout_asset`` service so single and bulk checkout
stay behaviourally identical.
"""

import datetime
import logging
from collections.abc import Sequence

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from assets import services
from assets.models import Asset, StatusLabel
from core.models import Job, Notification
from core.tasks.context import TaskContext
from core.tasks.utils import TaskResult, TaskStatus, classify_task_error, reverse_job_detail

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


def _checkout_item(
    job,
    pk,
    log_extra,
    ctx,
    target,
    target_kwarg,
    status,
    resolved_checkin,
    resolved_checkout,
    notes,
) -> str:
    """Run one asset checkout under a per-item transaction.

    Returns "success" or "failed"; one item's failure never aborts the batch.
    """
    try:
        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(pk=pk)
            services.checkout_asset(
                asset=asset,
                **{target_kwarg: target},
                user=ctx.user,
                notes=notes,
                status=status,
                expected_checkin=resolved_checkin,
                checkout_date=resolved_checkout,
            )
        return "success"
    # broad except: boundary-isolation: one asset failure must not abort the requested batch
    except Exception as ex:
        logger.warning(
            "Bulk checkout item failed",
            extra={**log_extra, "object_id": pk, "exception_type": type(ex).__name__},
        )
        return "failed"


def _finish_checkout(
    job,
    ctx,
    success_count,
    failure_count,
    total,
) -> TaskResult:
    """Finalize a bulk checkout: all-failed vs completed result and notification."""
    if success_count == 0:
        job.mark_failed("All asset checkouts failed.")
        Notification.objects.create(
            user=ctx.user,
            kind=Notification.KIND_PERSONAL,
            subject=_("Bulk Checkout Failed"),
            message=_("All hardware checkouts failed. View logs for error tracebacks."),
            level=Notification.LEVEL_DANGER,
            target_url=reverse_job_detail(job.pk),
        )
        return TaskResult(TaskStatus.TERMINAL, "checkout.all_failed", {"failed": failure_count}, user_visible=True)
    job.mark_completed(result={"checked_out": success_count, "failed": failure_count, "total": total})
    Notification.objects.create(
        user=ctx.user,
        kind=Notification.KIND_PERSONAL,
        subject=_("Bulk Checkout Complete"),
        message=ngettext(
            "Successfully checked out %(count)s asset.",
            "Successfully checked out %(count)s assets.",
            success_count,
        )
        % {"count": success_count},
        level=Notification.LEVEL_SUCCESS,
        target_url=reverse_job_detail(job.pk),
    )
    return TaskResult(
        TaskStatus.PARTIAL if failure_count else TaskStatus.SUCCESS,
        "checkout.partial" if failure_count else "checkout.completed",
        {"checked_out": success_count, "failed": failure_count},
        user_visible=True,
    )


def _run_checkout(
    job,
    ctx,
    log_extra,
    asset_pks,
    target_kwarg,
    target,
    status_id,
    expected_checkin_date,
    checkout_date,
    notes,
) -> TaskResult:
    """Resolve overrides, process every asset, finalize the job."""
    status = StatusLabel.objects.filter(pk=status_id).first() if status_id else None
    resolved_checkin = _parse_date(expected_checkin_date)
    resolved_checkout = _parse_date(checkout_date)

    success_count = 0
    failure_count = 0

    for pk in asset_pks:
        outcome = _checkout_item(
            job,
            pk,
            log_extra,
            ctx,
            target,
            target_kwarg,
            status,
            resolved_checkin,
            resolved_checkout,
            notes,
        )
        if outcome == "success":
            success_count += 1
            job.append_log(f" - Asset PK {pk} checked out successfully.")
        else:
            failure_count += 1
            job.append_log(f" - Asset PK {pk} failed [checkout.item_failed].")

    job.append_log(f"Bulk checkout execution finished. Successes: {success_count} | Failures: {failure_count}")

    return _finish_checkout(
        job,
        ctx,
        success_count,
        failure_count,
        len(asset_pks),
    )


def _resolve_checkout_target(target_type_str, target_pk):
    """Resolve the checkout target model instance from the type string."""
    _CT_MAP = {
        "assetholder": ("organization", "assetholder"),
        "asset": ("assets", "asset"),
        "location": ("organization", "location"),
    }
    app_label, model_name = _CT_MAP.get(target_type_str, ("organization", target_type_str))
    target_model = ContentType.objects.get(
        app_label=app_label,
        model=model_name,
    ).model_class()
    return target_model.objects.get(pk=target_pk)


def _deny_permission(job, *, tenant_id, user_id) -> TaskResult:
    logger.warning(
        "Bulk checkout denied [checkout.permission_revoked] tenant_id=%s actor_id=%s job_id=%s",
        tenant_id,
        user_id,
        job.pk,
        extra={
            "tenant_id": tenant_id,
            "actor_id": user_id,
            "job_id": job.pk,
            "code": "checkout.permission_revoked",
        },
    )
    job.mark_failed("[terminal] checkout.permission_revoked")
    return TaskResult(TaskStatus.TERMINAL, "checkout.permission_revoked")


def _claim_job(job_id, tenant_id, user_id, asset_count) -> tuple[Job | None, TaskResult | None]:
    log_extra = {"tenant_id": tenant_id, "actor_id": user_id, "job_id": job_id}
    try:
        job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
    except Job.DoesNotExist:
        logger.error("Bulk checkout job not found", extra=log_extra)
        return None, TaskResult(TaskStatus.TERMINAL, "checkout.job_not_found")
    # broad except: task-isolation: no Job can be persisted when the claim lookup itself fails
    except Exception as exc:
        logger.error("Bulk checkout entry failed", extra={**log_extra, "exception_type": type(exc).__name__})
        return None, TaskResult(classify_task_error(exc), "checkout.entry_failed")
    if not job.mark_running():
        logger.info("Job %s is no longer pending (cancelled?); skipping checkout.", job_id)
        return None, TaskResult(TaskStatus.SKIPPED, "checkout.job_not_pending")
    job.append_log("Initializing asynchronous bulk checkout pipeline...")
    job.append_log(f"Assets to process: {asset_count}")
    return job, None


def bulk_checkout_task(
    job_id: int,
    asset_pks: Sequence[int | str],
    target_type_str: str,
    target_pk: int | str,
    user_id: int | None,
    notes: str,
    expected_checkin_date: str | datetime.date | None = None,
    tenant_id: int | None = None,
    status_id: int | str | None = None,
    checkout_date: str | datetime.date | None = None,
) -> TaskResult:
    """
    Asynchronously executes bulk checkout operations on selected hardware Assets
    utilizing select_for_update row-level locking to prevent race anomalies.
    """
    job, claim_result = _claim_job(job_id, tenant_id, user_id, len(asset_pks))
    if claim_result is not None:
        return claim_result
    assert job is not None
    entry_log_extra = {"tenant_id": tenant_id, "actor_id": user_id, "job_id": job_id}

    try:
        with TaskContext(tenant_id=tenant_id, user_id=user_id, operation="assets.bulk_checkout") as ctx:
            log_extra = {**ctx.log_context, "job_id": job_id}
            # Execution-time RBAC recheck: enqueue-time authorization
            # is not enough — a permission revoked between submission and worker
            # execution must fail closed before any asset state is resolved
            # or mutated. No Notification is created on denial.
            if ctx.user is None or ctx.tenant is None or not ctx.user.has_perm("assets.change_asset", obj=ctx.tenant):
                return _deny_permission(job, tenant_id=tenant_id, user_id=user_id)

            try:
                target = _resolve_checkout_target(target_type_str, target_pk)
                job.append_log("Checkout target resolved.")

                # Map target_type_str to the correct checkout_asset keyword argument
                _TARGET_KWARG = {
                    "assetholder": "holder",
                    "asset": "asset_target",
                    "location": "location",
                }
                target_kwarg = _TARGET_KWARG.get(target_type_str, "location")

                return _run_checkout(
                    job,
                    ctx,
                    log_extra,
                    asset_pks,
                    target_kwarg,
                    target,
                    status_id,
                    expected_checkin_date,
                    checkout_date,
                    notes,
                )

            # broad except: task-isolation: record a safe typed task-boundary failure
            except Exception as e:
                status = classify_task_error(e)
                logger.error("Bulk checkout task failed", extra={**log_extra, "exception_type": type(e).__name__})
                job.mark_failed(f"[{status.value}] checkout.boundary_failed")
                Notification.objects.create(
                    user=ctx.user,
                    kind=Notification.KIND_PERSONAL,
                    subject=_("Bulk Checkout Error"),
                    message=_("The bulk checkout could not be completed. Check the job details for more information."),
                    level=Notification.LEVEL_DANGER,
                    target_url=reverse_job_detail(job.pk),
                )
                return TaskResult(status, "checkout.boundary_failed", user_visible=True)
    except PermissionDenied:
        return _deny_permission(job, tenant_id=tenant_id, user_id=user_id)
    # broad except: task-isolation: persist failures after the Job claim
    except Exception as exc:
        status = classify_task_error(exc)
        logger.error("Bulk checkout entry failed", extra={**entry_log_extra, "exception_type": type(exc).__name__})
        job.mark_failed(f"[{status.value}] checkout.entry_failed")
        return TaskResult(status, "checkout.entry_failed")
