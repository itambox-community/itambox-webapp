"""Prune aged changelog / operational-data rows.

Release blocker #3 (1.0 readiness review): ``ObjectChange``, ``AlertLog``, and
``Notification`` had no retention policy -- they grow forever -- and
django-q2 never prunes its own ``Failure`` table (see the ``save_limit``
comment on ``Q_CLUSTER`` in core/settings/base.py). This command closes all
four gaps in one place, each against its own ``ITAMBOX_*_RETENTION_DAYS``
setting (0 = unlimited/never pruned), with a per-tenant override for the
changelog only (``Tenant.changelog_retention_days`` -- null = use the global
setting, 0 = unlimited/legal hold).

Scheduled to run daily via ``core.tasks.prune_changelog_task`` (registered as
a django-q2 ``Schedule`` by ``CoreConfig._register_prune_schedule`` in
core/apps.py); also safe to run ad hoc or from cron/CI.
"""

import json
import os
from datetime import timedelta

from django.conf import settings
from django.core.management.base import CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from django_q.models import Failure

from core.models import Notification, ObjectChange
from core.tasks.management import SystemTaskCommand
from extras.models import AlertLog, Event
from organization.models import (
    Tenant,
    TenantResourceGrantExpiryRevocation,
    TenantResourceGrantExpiryRun,
)

CLASS_CHOICES = ("changelog", "alertlog", "notification", "event", "qtask")


class _ArchiveWriter:
    """Streams a data class's pruned rows to a JSONL file before each batch delete.

    One file per class per command run (not per tenant, not per batch), named
    ``<label>_<run timestamp>.jsonl`` inside ``--archive-dir``. Rows are
    written as plain field dicts (``QuerySet.values()``), JSON-encoded with
    ``DjangoJSONEncoder`` so dates/decimals/UUIDs serialize cleanly.

    NOT transactional with the delete that follows it: a batch is written and
    flushed to disk, then deleted in a separate query. A crash between the two
    can leave that batch archived-but-not-yet-deleted (harmless -- the next
    run deletes it, at worst duplicating the row across two archive files) or,
    in the rare case the process dies mid-write, a batch deleted with an
    incomplete archive entry. Treat ``--archive-dir`` as a best-effort export,
    not a guaranteed atomic backup.
    """

    def __init__(self, directory, label, run_stamp):
        self.path = os.path.join(directory, f"{label}_{run_stamp}.jsonl")
        self._fh = open(self.path, "a", encoding="utf-8")

    def write_batch(self, values_iterable):
        for row in values_iterable:
            self._fh.write(json.dumps(row, cls=DjangoJSONEncoder))
            self._fh.write("\n")
        self._fh.flush()

    def close(self):
        self._fh.close()


class Command(SystemTaskCommand):
    help = (
        "Prune aged changelog/operational-data rows: ObjectChange, AlertLog, "
        "Notification, Event, and failed django-q2 tasks -- each against its own "
        "ITAMBOX_*_RETENTION_DAYS setting (0 = unlimited/never pruned)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--changelog-days",
            type=int,
            default=None,
            help="Override ITAMBOX_CHANGELOG_RETENTION_DAYS for this run (0 = unlimited).",
        )
        parser.add_argument(
            "--alertlog-days",
            type=int,
            default=None,
            help="Override ITAMBOX_ALERTLOG_RETENTION_DAYS for this run (0 = unlimited).",
        )
        parser.add_argument(
            "--notification-days",
            type=int,
            default=None,
            help="Override ITAMBOX_NOTIFICATION_RETENTION_DAYS for this run (0 = unlimited).",
        )
        parser.add_argument(
            "--event-days",
            type=int,
            default=None,
            help="Override ITAMBOX_EVENT_RETENTION_DAYS for this run (0 = unlimited).",
        )
        parser.add_argument(
            "--qtask-days",
            type=int,
            default=None,
            help="Override ITAMBOX_QTASK_FAILED_RETENTION_DAYS for this run (0 = unlimited).",
        )
        parser.add_argument(
            "--tenant",
            default=None,
            help=(
                "Restrict CHANGELOG pruning to this tenant slug only -- its effective "
                "retention is Tenant.changelog_retention_days if set, else the global "
                "setting/--changelog-days. Global (tenant=None) changelog rows and every "
                "other tenant are left untouched. Has no effect on alertlog/notification/"
                "qtask, which carry no per-tenant scope in this command."
            ),
        )
        parser.add_argument(
            "--classes",
            default=",".join(CLASS_CHOICES),
            help=f"Comma-separated subset of {{{','.join(CLASS_CHOICES)}}} to prune (default: all).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10000,
            help="Rows deleted per batch (default: 10000).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report counts only; delete and archive nothing.",
        )
        parser.add_argument(
            "--archive-dir",
            default=None,
            help=(
                "Directory to stream pruned rows to as JSONL before deleting them (one "
                "file per class per run). Ignored under --dry-run, since nothing is "
                "deleted. Not transactional across batches -- see _ArchiveWriter."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        if batch_size <= 0:
            raise CommandError("--batch-size must be a positive integer.")

        classes = {c.strip() for c in options["classes"].split(",") if c.strip()}
        unknown = classes - set(CLASS_CHOICES)
        if unknown:
            raise CommandError(
                f"Unknown --classes value(s): {', '.join(sorted(unknown))}. Choose from: {', '.join(CLASS_CHOICES)}."
            )

        tenant_slug = options["tenant"]
        tenant = None
        if tenant_slug:
            # _base_manager: this lookup must never be narrowed by an ambient
            # tenant/request context (there shouldn't be one in a management
            # command, but stay consistent with every other lookup below).
            tenant = Tenant._base_manager.filter(slug=tenant_slug).first()
            if tenant is None:
                raise CommandError("No tenant found with slug '%s'." % tenant_slug)
            if classes - {"changelog"}:
                self.stderr.write(
                    self.style.WARNING(
                        "--tenant only restricts changelog pruning; the other selected "
                        "classes will still be pruned globally."
                    )
                )

        archive_dir = options["archive_dir"]
        if archive_dir and not dry_run:
            os.makedirs(archive_dir, exist_ok=True)

        changelog_days = self._resolve_days(
            options["changelog_days"], settings.ITAMBOX_CHANGELOG_RETENTION_DAYS, "changelog"
        )
        alertlog_days = self._resolve_days(
            options["alertlog_days"], settings.ITAMBOX_ALERTLOG_RETENTION_DAYS, "alertlog"
        )
        notification_days = self._resolve_days(
            options["notification_days"], settings.ITAMBOX_NOTIFICATION_RETENTION_DAYS, "notification"
        )
        event_days = self._resolve_days(options["event_days"], settings.ITAMBOX_EVENT_RETENTION_DAYS, "event")
        qtask_days = self._resolve_days(options["qtask_days"], settings.ITAMBOX_QTASK_FAILED_RETENTION_DAYS, "qtask")

        now = timezone.now()
        run_stamp = now.strftime("%Y%m%dT%H%M%SZ")
        grand_total = 0

        if "changelog" in classes:
            grand_total += self._run_pruner(
                "resource-grant-expiry",
                archive_dir,
                run_stamp,
                dry_run,
                prune_fn=lambda writer: self._prune_expiry_runs(
                    now=now,
                    global_days=changelog_days,
                    tenant=tenant,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    archive_writer=writer,
                ),
            )
            grand_total += self._run_pruner(
                "changelog",
                archive_dir,
                run_stamp,
                dry_run,
                prune_fn=lambda writer: self._prune_changelog(
                    now=now,
                    global_days=changelog_days,
                    tenant=tenant,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    archive_writer=writer,
                ),
            )

        if "alertlog" in classes:
            grand_total += self._run_pruner(
                "alertlog",
                archive_dir,
                run_stamp,
                dry_run,
                prune_fn=lambda writer: self._prune_simple(
                    AlertLog._base_manager,
                    "created_at",
                    now,
                    alertlog_days,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    archive_writer=writer,
                    label="alertlog",
                ),
            )

        if "notification" in classes:
            grand_total += self._run_pruner(
                "notification",
                archive_dir,
                run_stamp,
                dry_run,
                prune_fn=lambda writer: self._prune_simple(
                    Notification._base_manager,
                    "created_at",
                    now,
                    notification_days,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    archive_writer=writer,
                    label="notification",
                ),
            )

        if "event" in classes:
            grand_total += self._run_pruner(
                "event",
                archive_dir,
                run_stamp,
                dry_run,
                prune_fn=lambda writer: self._prune_simple(
                    Event._base_manager,
                    "timestamp",
                    now,
                    event_days,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    archive_writer=writer,
                    label="event",
                ),
            )

        if "qtask" in classes:
            grand_total += self._run_pruner(
                "qtask",
                archive_dir,
                run_stamp,
                dry_run,
                prune_fn=lambda writer: self._prune_simple(
                    # Deliberately Failure.objects (FailureManager, success=False),
                    # NOT Failure._base_manager: for a proxy model, _base_manager
                    # falls back to a bare Manager on the *parent* table (Task)
                    # with none of the proxy's own filtering applied -- it would
                    # match every Task row, successes included, and this command
                    # would delete non-failed task history. Task/Failure carries
                    # no tenant field, so (unlike the models above) there is no
                    # ambient-tenant-context risk in using the class's own manager.
                    Failure.objects,
                    "stopped",
                    now,
                    qtask_days,
                    batch_size=batch_size,
                    dry_run=dry_run,
                    archive_writer=writer,
                    label="qtask (failed)",
                ),
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(f"[DRY RUN] Total row(s) that would be pruned: {grand_total}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Total row(s) pruned: {grand_total}"))

    def _resolve_days(self, override, setting_value, label):
        days = override if override is not None else setting_value
        if days < 0:
            raise CommandError(f"{label} retention days must be >= 0 (0 = unlimited); got {days}.")
        return days

    def _run_pruner(self, label, archive_dir, run_stamp, dry_run, *, prune_fn):
        self.stdout.write(self.style.MIGRATE_HEADING(f"Pruning {label}..."))
        writer = _ArchiveWriter(archive_dir, label, run_stamp) if (archive_dir and not dry_run) else None
        try:
            return prune_fn(writer)
        finally:
            if writer:
                writer.close()

    def _prune_changelog(self, *, now, global_days, tenant, batch_size, dry_run, archive_writer):
        """Prune ObjectChange rows.

        TENANT-SAFE: every query below goes through ``ObjectChange._base_manager``
        (bypasses ``TenantScopingManager``) so this never depends on -- or is
        silently narrowed by -- an ambient request/task tenant context; tenant
        scoping is applied explicitly via a ``tenant_id`` filter kwarg instead.
        """
        manager = ObjectChange._base_manager
        total = 0

        if tenant is not None:
            effective_days = tenant.changelog_retention_days
            if effective_days is None:
                effective_days = global_days
            if effective_days == 0:
                self.stdout.write(f"  [changelog] tenant={tenant.slug}: retention=unlimited (legal hold) -- skipped.")
                return 0
            cutoff = now - timedelta(days=effective_days)
            qs = manager.filter(tenant_id=tenant.pk, time__lt=cutoff)
            return self._prune_queryset(
                qs, batch_size=batch_size, dry_run=dry_run, archive_writer=archive_writer, label="changelog"
            )

        # No --tenant: prune every tenant carrying an explicit override at its
        # own cutoff first, then everything else (tenants with no override +
        # global tenant=None rows) at the global cutoff in one pass.
        overridden = list(
            Tenant._base_manager.exclude(changelog_retention_days__isnull=True).only(
                "id", "slug", "changelog_retention_days"
            )
        )
        overridden_ids = [t.pk for t in overridden]

        for t in overridden:
            if t.changelog_retention_days == 0:
                self.stdout.write(f"  [changelog] tenant={t.slug}: retention=unlimited (legal hold) -- skipped.")
                continue
            cutoff = now - timedelta(days=t.changelog_retention_days)
            qs = manager.filter(tenant_id=t.pk, time__lt=cutoff)
            total += self._prune_queryset(
                qs,
                batch_size=batch_size,
                dry_run=dry_run,
                archive_writer=archive_writer,
                label=f"changelog (tenant={t.slug})",
            )

        if global_days == 0:
            self.stdout.write("  [changelog] global retention=unlimited -- remaining tenants and global rows skipped.")
            return total

        cutoff = now - timedelta(days=global_days)
        qs = manager.filter(time__lt=cutoff)
        if overridden_ids:
            qs = qs.exclude(tenant_id__in=overridden_ids)
        total += self._prune_queryset(
            qs,
            batch_size=batch_size,
            dry_run=dry_run,
            archive_writer=archive_writer,
            label="changelog (global cutoff)",
        )
        return total

    def _prune_expiry_runs(self, *, now, global_days, tenant, batch_size, dry_run, archive_writer):  # noqa: C901
        """Prune terminal expiry runs by finished time, before ObjectChange rows."""

        terminal = {
            TenantResourceGrantExpiryRun.OUTCOME_CHOICES[0][0],
            TenantResourceGrantExpiryRun.OUTCOME_CHOICES[1][0],
            TenantResourceGrantExpiryRun.OUTCOME_CHOICES[2][0],
            TenantResourceGrantExpiryRun.OUTCOME_CHOICES[4][0],
        }

        def prune_for(tenant_id, days, label, excluded_tenant_ids=()):
            if days == 0:
                self.stdout.write(f"  [resource-grant-expiry] {label}: retention=unlimited -- skipped.")
                return 0
            cutoff = now - timedelta(days=days)
            queryset = TenantResourceGrantExpiryRun._base_manager.filter(
                state=TenantResourceGrantExpiryRun.STATE_COMPLETE,
                outcome__in=terminal,
                finished_at__isnull=False,
                finished_at__lt=cutoff,
            )
            if tenant_id is not None:
                queryset = queryset.filter(tenant_id=tenant_id)
            if excluded_tenant_ids:
                queryset = queryset.exclude(tenant_id__in=excluded_tenant_ids)
            if dry_run:
                count = queryset.count()
                if count:
                    self.stdout.write(f"  [DRY RUN][resource-grant-expiry] {label}: would prune {count} row(s).")
                return count

            total = 0
            while True:
                pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:batch_size])
                if not pks:
                    break
                runs = queryset.filter(pk__in=pks)
                evidence = TenantResourceGrantExpiryRevocation._base_manager.filter(run_id__in=pks)
                if archive_writer:
                    archive_writer.write_batch(evidence.values())
                    archive_writer.write_batch(runs.values())
                evidence.delete()
                runs.delete()
                total += len(pks)
                self.stdout.write(
                    f"  [resource-grant-expiry] {label}: pruned batch of {len(pks)} (running total {total})."
                )
            self.stdout.write(f"  [resource-grant-expiry] {label}: done: {total} row(s) pruned.")
            return total

        if tenant is not None:
            effective_days = tenant.changelog_retention_days
            return prune_for(
                tenant.pk,
                global_days if effective_days is None else effective_days,
                f"tenant={tenant.slug}",
            )

        overridden = list(
            Tenant._base_manager.exclude(changelog_retention_days__isnull=True).only(
                "id", "slug", "changelog_retention_days"
            )
        )
        overridden_ids = [item.pk for item in overridden]
        total = 0
        for item in overridden:
            total += prune_for(item.pk, item.changelog_retention_days, f"tenant={item.slug}")
        if global_days == 0:
            self.stdout.write("  [resource-grant-expiry] global retention=unlimited -- remaining tenants skipped.")
            return total
        total += prune_for(None, global_days, "global cutoff", overridden_ids)
        return total

    def _prune_simple(self, manager, time_field, now, days, *, batch_size, dry_run, archive_writer, label):
        if days == 0:
            self.stdout.write(f"  [{label}] retention=unlimited -- skipped.")
            return 0
        cutoff = now - timedelta(days=days)
        qs = manager.filter(**{f"{time_field}__lt": cutoff})
        return self._prune_queryset(
            qs, batch_size=batch_size, dry_run=dry_run, archive_writer=archive_writer, label=label
        )

    def _prune_queryset(self, queryset, *, batch_size, dry_run, archive_writer, label):
        if dry_run:
            count = queryset.count()
            if count:
                self.stdout.write(f"  [DRY RUN][{label}] would prune {count} row(s).")
            return count

        total = 0
        while True:
            pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:batch_size])
            if not pks:
                break
            # Re-filter by the already-selected pks rather than reusing `queryset`
            # directly: this keeps the batch fixed even though matching rows keep
            # shrinking out of `queryset` as each batch is deleted.
            batch_qs = queryset.filter(pk__in=pks)
            if archive_writer:
                archive_writer.write_batch(batch_qs.values())
            batch_qs.delete()
            total += len(pks)
            self.stdout.write(f"  [{label}] pruned batch of {len(pks)} (running total {total}).")

        self.stdout.write(f"  [{label}] done: {total} row(s) pruned.")
        return total
