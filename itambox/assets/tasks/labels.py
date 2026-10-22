"""Queue compatibility for asset label jobs enqueued before the generic engine.

Label printing for every labelled model lives in :mod:`extras.tasks.labels`.
This entry point only keeps a job that a ``v1.0.0-beta.6`` web worker queued
under the former asset-only path running to completion after an upgrade; no
current code enqueues it.
"""

from collections.abc import Sequence

from django.contrib.contenttypes.models import ContentType

from assets.models import Asset
from core.models import Job
from core.tasks.utils import TaskResult, TaskStatus
from extras.tasks import labels as generic_labels


def generate_label_pdf_batch_task(
    job_id: int,
    asset_pks: Sequence[int | str],
    template_id: int,
    layout_mode: str,
    user_id: int | None,
    tenant_id: int | None = None,
) -> TaskResult:
    """Delegate a queued asset label job to the generic label task."""
    return generic_labels.generate_label_pdf_batch_task(
        job_id,
        ContentType.objects.get_for_model(Asset).pk,
        asset_pks,
        template_id,
        layout_mode,
        user_id,
        tenant_id,
    )


def generate_label_batch_task(
    job_id: int,
    asset_pks: Sequence[int | str],
    label_format: str,
    user_id: int | None,
    tenant_id: int | None = None,
) -> TaskResult:
    """Retired ZIP-of-PNGs entry point, kept so a queued pre-1.0 job resolves.

    No current code enqueues it; the PNG archive is gone. A historical queue
    entry is closed as terminal instead of failing to import on resubmission.
    """
    job = Job.objects.filter(pk=job_id).first()
    if job is not None:
        job.append_log("This label job type was retired; print labels from the object page.")
        job.mark_failed("[terminal] labels.zip_retired")
    return TaskResult(TaskStatus.TERMINAL, "labels.zip_retired", user_visible=True)
