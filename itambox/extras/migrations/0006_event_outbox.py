"""Event outbox: replace ``Event.processed`` with a ``status`` state machine (issue #1091).

Events younger than 24 h that were never processed become ``pending`` so the
coordinator recovers them; older unprocessed events become ``dead`` with the
``outbox.backfill_expired`` error class. The reverse refuses while any event is
pending: drain the outbox first.
"""

from datetime import timedelta

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone

BACKFILL_WINDOW = timedelta(hours=24)


def backfill_status(apps, schema_editor):
    Event = apps.get_model("extras", "Event")
    cutoff = timezone.now() - BACKFILL_WINDOW
    manager = Event._base_manager
    manager.filter(processed=True).update(status="processed")
    unprocessed = manager.filter(processed=False)
    unprocessed.filter(timestamp__gte=cutoff).update(status="pending")
    unprocessed.filter(timestamp__lt=cutoff).update(status="dead", last_error_class="outbox.backfill_expired")


def restore_processed(apps, schema_editor):
    Event = apps.get_model("extras", "Event")
    manager = Event._base_manager
    if manager.filter(status="pending").exists():
        raise RuntimeError("Cannot reverse: pending events exist. Drain the event outbox first.")
    manager.filter(status="processed").update(processed=True)
    manager.exclude(status="processed").update(processed=False)


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0005_label_template_object_types"),
    ]

    operations = [
        migrations.AddField("event", "status", models.CharField(
            choices=[("pending", "Pending"), ("processed", "Processed"), ("dead", "Dead")],
            db_index=True, default="pending", max_length=16, verbose_name="Status")),
        migrations.AddField("event", "tenant_id", models.PositiveBigIntegerField(
            blank=True, db_index=True, editable=False, null=True, verbose_name="Tenant ID")),
        migrations.AddField("event", "actor_id", models.PositiveBigIntegerField(
            blank=True, editable=False, null=True, verbose_name="Actor ID")),
        migrations.AddField("event", "attempts", models.PositiveSmallIntegerField(
            default=0, editable=False, verbose_name="Attempts")),
        migrations.AddField("event", "available_at", models.DateTimeField(
            default=timezone.now, editable=False, verbose_name="Available At",
            help_text="Earliest time the coordinator may claim this event; also the claim lease.")),
        migrations.AddField("event", "rule_errors", models.PositiveSmallIntegerField(
            default=0, editable=False, verbose_name="Rule Errors",
            help_text="Rule actions that failed terminally.")),
        migrations.AddField("event", "last_error_class", models.CharField(
            blank=True, editable=False, max_length=64, verbose_name="Last Error Class")),
        migrations.AddField("event", "processed_at", models.DateTimeField(
            blank=True, editable=False, null=True, verbose_name="Processed At")),
        migrations.RunPython(backfill_status, restore_processed),
        migrations.RemoveIndex("event", "core_event_process_17ef77_idx"),
        migrations.RemoveField("event", "processed"),
        migrations.AddIndex("event", models.Index(fields=["status", "available_at"], name="extras_event_outbox_due_idx")),
        migrations.AddConstraint("webhookdelivery", models.UniqueConstraint(
            fields=("event", "event_rule_id"),
            condition=Q(event__isnull=False, event_rule_id__isnull=False, redelivered_from__isnull=True),
            name="unique_original_delivery_per_event_rule")),
    ]
