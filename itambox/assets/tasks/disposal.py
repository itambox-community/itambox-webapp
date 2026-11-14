"""Background task: scanner-driven bulk disposal of hardware assets.

Each asset is disposed via the canonical ``dispose_asset`` service (which
auto-checks-in, records an ``AssetDisposal``, freezes book value and archives
the asset). Already-disposed assets are skipped so a re-run never overwrites an
existing disposal record. ``proceeds`` is per-asset (``proceeds_map``); when
absent the service freezes the depreciated book value.
"""

import datetime
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from assets import services
from assets.models import Asset, AssetDisposal
from core.models import Job, Notification
from core.tasks.context import TaskContext
from core.tasks.utils import TaskResult, TaskStatus, classify_task_error, reverse_job_detail

logger = logging.getLogger(__name__)


def _parse_date(value: object) -> datetime.date | None:
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_proceeds(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Negative proceeds are invalid (would push book value negative) — drop them.
    return parsed if parsed >= 0 else None


def _dispose_item(
    job,
    pk,
    log_extra,
    ctx,
    shared,
    proceeds_map,
) -> str:
    """Dispose one asset; returns "success", "skipped", or "failed"."""
    try:
        asset = Asset.objects.get(pk=pk)

        # An ACTIVE record is what makes an asset disposed; an archived asset
        # without a record is not (its stamp is the archival freeze) and stays eligible.
        already_disposed = AssetDisposal.all_objects.filter(asset=asset, cancelled_at__isnull=True).exists()
        if already_disposed:
            return "skipped"

        proceeds = _parse_proceeds(proceeds_map.get(str(pk)))
        services.dispose_asset(asset=asset, user=ctx.user, proceeds=proceeds, **shared)
        return "success"
    # broad except: boundary-isolation: one asset failure must not abort the requested batch
    except Exception as ex:
        logger.warning(
            "Bulk disposal item failed",
            extra={**log_extra, "object_id": pk, "exception_type": type(ex).__name__},
        )
        return "failed"


def _finish_disposal(
    job,
    ctx,
    success_count,
    skipped_count,
    failure_count,
    total,
) -> TaskResult:
    """Finalize a bulk disposal: all-failed vs completed result and notification."""
    if success_count == 0 and skipped_count == 0:
        job.mark_failed("All asset disposals failed.")
        Notification.objects.create(
            user=ctx.user,
            kind=Notification.KIND_PERSONAL,
            subject=_("Bulk Disposal Failed"),
            message=_("All hardware disposals failed. View logs for details."),
            level=Notification.LEVEL_DANGER,
            target_url=reverse_job_detail(job.pk),
        )
        return TaskResult(TaskStatus.TERMINAL, "disposal.all_failed", {"failed": failure_count}, user_visible=True)
    job.mark_completed(
        result={
            "disposed": success_count,
            "skipped": skipped_count,
            "failed": failure_count,
            "total": total,
        }
    )
    Notification.objects.create(
        user=ctx.user,
        kind=Notification.KIND_PERSONAL,
        subject=_("Bulk Disposal Complete"),
        message=ngettext("Disposed %(count)s asset.", "Disposed %(count)s assets.", success_count)
        % {"count": success_count},
        level=Notification.LEVEL_SUCCESS,
        target_url=reverse_job_detail(job.pk),
    )
    return TaskResult(
        TaskStatus.PARTIAL if failure_count else TaskStatus.SUCCESS,
        "disposal.partial" if failure_count else "disposal.completed",
        {"disposed": success_count, "skipped": skipped_count, "failed": failure_count},
        user_visible=True,
    )


def _run_disposal(
    job,
    ctx,
    log_extra,
    asset_pks,
    disposal_kwargs,
    proceeds_map,
) -> TaskResult:
    """Resolve shared disposal payload, process every asset, finalize."""
    disposal_date = _parse_date(disposal_kwargs.get("disposal_date"))
    if disposal_date is None:
        disposal_date = datetime.date.today()

    shared = {
        "disposal_method": disposal_kwargs.get("disposal_method", "destruction"),
        "disposal_date": disposal_date,
        "data_sanitization_method": disposal_kwargs.get("data_sanitization_method", "none"),
        "sanitization_certificate": disposal_kwargs.get("sanitization_certificate", ""),
        "sanitized_by": disposal_kwargs.get("sanitized_by", ""),
        "recipient": disposal_kwargs.get("recipient", ""),
        "currency": disposal_kwargs.get("currency", ""),
        "weee_compliant": disposal_kwargs.get("weee_compliant", False),
        "notes": disposal_kwargs.get("notes", ""),
    }

    success_count = 0
    skipped_count = 0
    failure_count = 0

    for pk in asset_pks:
        outcome = _dispose_item(
            job,
            pk,
            log_extra,
            ctx,
            shared,
            proceeds_map,
        )
        if outcome == "success":
            success_count += 1
            job.append_log(f" - Asset PK {pk} disposed.")
        elif outcome == "skipped":
            skipped_count += 1
            job.append_log(f" - Asset PK {pk} skipped (already disposed).")
        else:
            failure_count += 1
            job.append_log(f" - Asset PK {pk} failed [disposal.item_failed].")

    job.append_log(
        f"Bulk disposal finished. Disposed: {success_count} | Skipped: {skipped_count} | Failures: {failure_count}"
    )

    return _finish_disposal(
        job,
        ctx,
        success_count,
        skipped_count,
        failure_count,
        len(asset_pks),
    )


def _deny_permission(job, *, tenant_id, user_id) -> TaskResult:
    logger.warning(
        "Bulk disposal denied [disposal.permission_revoked] tenant_id=%s actor_id=%s job_id=%s",
        tenant_id,
        user_id,
        job.pk,
        extra={
            "tenant_id": tenant_id,
            "actor_id": user_id,
            "job_id": job.pk,
            "code": "disposal.permission_revoked",
        },
    )
    job.mark_failed("[terminal] disposal.permission_revoked")
    return TaskResult(TaskStatus.TERMINAL, "disposal.permission_revoked")


def _claim_job(job_id, tenant_id, user_id, asset_count) -> tuple[Job | None, TaskResult | None]:
    log_extra = {"tenant_id": tenant_id, "actor_id": user_id, "job_id": job_id}
    try:
        job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
    except Job.DoesNotExist:
        logger.error("Bulk disposal job not found", extra=log_extra)
        return None, TaskResult(TaskStatus.TERMINAL, "disposal.job_not_found")
    # broad except: task-isolation: no Job can be persisted when the claim lookup itself fails
    except Exception as exc:
        logger.error("Bulk disposal entry failed", extra={**log_extra, "exception_type": type(exc).__name__})
        return None, TaskResult(classify_task_error(exc), "disposal.entry_failed")
    if not job.mark_running():
        logger.info("Job %s is no longer pending (cancelled?); skipping disposal.", job_id)
        return None, TaskResult(TaskStatus.SKIPPED, "disposal.job_not_pending")
    job.append_log("Initializing asynchronous bulk disposal pipeline...")
    job.append_log(f"Assets to process: {asset_count}")
    return job, None


def bulk_dispose_task(
    job_id: int,
    asset_pks: Sequence[int | str],
    user_id: int | None,
    tenant_id: int | None = None,
    disposal_kwargs: Mapping[str, object] | None = None,
    proceeds_map: Mapping[str, object] | None = None,
) -> TaskResult:
    """Asynchronously dispose selected hardware assets."""
    disposal_kwargs = disposal_kwargs or {}
    proceeds_map = proceeds_map or {}

    job, claim_result = _claim_job(job_id, tenant_id, user_id, len(asset_pks))
    if claim_result is not None:
        return claim_result
    assert job is not None
    entry_log_extra = {"tenant_id": tenant_id, "actor_id": user_id, "job_id": job_id}

    try:
        with TaskContext(tenant_id=tenant_id, user_id=user_id, operation="assets.bulk_disposal") as ctx:
            log_extra = {**ctx.log_context, "job_id": job_id}
            # Execution-time RBAC recheck: enqueue-time authorization
            # is not enough — a permission revoked between submission and worker
            # execution must fail closed before any asset/disposal state is
            # resolved or mutated. No Notification is created on denial.
            if (
                ctx.user is None
                or ctx.tenant is None
                or not ctx.user.has_perm("assets.add_assetdisposal", obj=ctx.tenant)
            ):
                return _deny_permission(job, tenant_id=tenant_id, user_id=user_id)

            try:
                return _run_disposal(
                    job,
                    ctx,
                    log_extra,
                    asset_pks,
                    disposal_kwargs,
                    proceeds_map,
                )

            # broad except: task-isolation: record a safe typed task-boundary failure
            except Exception as e:
                status = classify_task_error(e)
                logger.error("Bulk disposal task failed", extra={**log_extra, "exception_type": type(e).__name__})
                job.mark_failed(f"[{status.value}] disposal.boundary_failed")
                Notification.objects.create(
                    user=ctx.user,
                    kind=Notification.KIND_PERSONAL,
                    subject=_("Bulk Disposal Error"),
                    message=_("The bulk disposal could not be completed. Code: disposal.boundary_failed"),
                    level=Notification.LEVEL_DANGER,
                    target_url=reverse_job_detail(job.pk),
                )
                return TaskResult(status, "disposal.boundary_failed", user_visible=True)
    except PermissionDenied:
        return _deny_permission(job, tenant_id=tenant_id, user_id=user_id)
    # broad except: task-isolation: persist failures after the Job claim
    except Exception as exc:
        status = classify_task_error(exc)
        logger.error("Bulk disposal entry failed", extra={**entry_log_extra, "exception_type": type(exc).__name__})
        job.mark_failed(f"[{status.value}] disposal.entry_failed")
        return TaskResult(status, "disposal.entry_failed")
