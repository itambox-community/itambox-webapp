"""Scheduled cleanup for abandoned import previews.

The import view cleans a user's spent and expired previews opportunistically
when that user uploads again. A user who previews once and never returns would
otherwise leave the row behind forever, so this task applies the same TTL across
every user and tenant on a schedule.

The cleanup only removes rows that are already spent (``consumed_at`` set) or
whose ``created_at`` is older than ``IMPORT_PREVIEW_TTL``. A fresh, unconsumed
preview is never touched, which keeps the atomic confirmation path intact: the
claim's conditional update and this delete serialize on the preview row, and a
row that is still fresh cannot match the predicate.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from core.models import IMPORT_PREVIEW_TTL, ImportPreview
from core.tasks.utils import RetryableTaskError, TaskResult, TaskStatus, TerminalTaskError, classify_task_error

logger = logging.getLogger(__name__)

# Bound one delete statement so a large backlog cannot hold a long transaction.
IMPORT_PREVIEW_CLEANUP_BATCH_SIZE = 500


def cleanup_import_previews(*, now=None, batch_size=IMPORT_PREVIEW_CLEANUP_BATCH_SIZE):
    """Delete spent and expired preview rows; return the number of rows removed.

    ``created_at`` is immutable, so a row cannot become expired and then fresh
    again. Deleting by primary-key batches keeps repeated runs idempotent: the
    second run finds no rows and returns zero.
    """
    cutoff = (now or timezone.now()) - IMPORT_PREVIEW_TTL
    predicate = Q(consumed_at__isnull=False) | Q(created_at__lt=cutoff)
    deleted_total = 0

    while True:
        # unscoped: import previews are global scratch rows; the task cleans every user and tenant
        preview_ids = list(ImportPreview.objects.filter(predicate).values_list("pk", flat=True)[:batch_size])
        if not preview_ids:
            break
        deleted, _ = ImportPreview.objects.filter(pk__in=preview_ids).delete()
        deleted_total += deleted
        # A concurrent cleanup or confirmation may have removed the batch first;
        # stop rather than spinning on rows this transaction did not delete.
        if deleted == 0:
            break

    return deleted_total


def cleanup_import_previews_task() -> TaskResult:
    """Scheduled hourly task: remove spent and expired import previews."""
    logger.info("Starting scheduled import-preview cleanup.")
    try:
        deleted = cleanup_import_previews()
    # broad except: cleanup-reraise: classify the queue-visible failure without swallowing it
    except Exception as exc:
        error_type = RetryableTaskError if classify_task_error(exc) is TaskStatus.RETRYABLE else TerminalTaskError
        logger.error(
            "Scheduled import-preview cleanup failed",
            extra={"operation": "import_previews.cleanup", "exception_type": type(exc).__name__},
        )
        raise error_type(
            code="import_previews.cleanup_failed",
            message="Scheduled import-preview cleanup failed.",
        ) from exc
    logger.info("Scheduled import-preview cleanup removed %d row(s).", deleted)
    return TaskResult(TaskStatus.SUCCESS, "import_previews.cleanup_completed", counts={"deleted": deleted})
