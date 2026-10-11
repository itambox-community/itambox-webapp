"""Repair maintenance anchor (#644): replace RepairEpisode with linked records.

Hand-written for the RepairEpisode retirement. RepairEpisode shipped in
v1.0.0-beta.2 and is removed before the 1.0 contract freeze: the repair
maintenance is the anchor of the story, its loan and its disposal link to it, and
the separate grouping object, its CRUD surface and its navigation entry are gone.

The data migration maps every beta-era episode onto that model and never drops a
fact silently:

* a disposal linked to the episode moves onto the episode's single repair
  maintenance;
* the episode's ``substitute_asset`` is translated into the loan assignment of
  that stand-in unit which overlaps the repair window, when exactly one does;
* everything that cannot be mapped unambiguously (no or several repair
  maintenances, a reservation link, the episode's notes, a substitute without a
  matching loan) is appended to the maintenance notes, or - when there is no
  single maintenance to carry it - recorded as a journal entry on the asset.

Rows are read through ``_base_manager`` (a tenant-scoped default manager would
silently return nothing during a migration). ``SET CONSTRAINTS ALL IMMEDIATE``
flushes the deferred foreign-key events the updates queue, because the following
``ALTER TABLE`` steps refuse to run with pending trigger events.

Already-applied databases (remediation policy): an earlier revision of this migration
could link an ordinary (``is_loan=False``) assignment to a repair maintenance. The
episode rows are gone after the migration, so the original evidence cannot be rebuilt
and the links are deliberately not rewritten automatically. Such a link is inert: the
runtime lookup only recognizes ``is_loan=True`` and ignores it. Operators can list the
affected rows with ``AssetAssignment.objects.filter(maintenance__isnull=False,
is_loan=False)`` and clear ``maintenance`` by hand after review.

The data mapping is not reversible: the episode links it translated were derived
facts, and converting the successor links back into a grouping row would invent
history.
"""

import django.db.models.deletion
from django.db import migrations, models


def _append_note(existing: str, additions: list) -> str:
    block = "Migrated from repair episode (#644):\n" + "\n".join(f"- {line}" for line in additions)
    return f"{existing.rstrip()}\n\n{block}" if existing and existing.strip() else block


def _describe_disposal(disposal) -> str:
    return f"Disposal {disposal.pk} on {disposal.disposal_date} (method {disposal.disposal_method})"


def _describe_reservation(reservation) -> str:
    return (
        f"Reservation {reservation.pk} from {reservation.start_date} to {reservation.end_date} "
        "(a reservation books a unit, it is not a repair handover)"
    )


def _overlaps(assignment, start, end) -> bool:
    checked_out = assignment.checked_out_at.date()
    if checked_out > end:
        return False
    if assignment.checked_in_at is None:
        return True
    return assignment.checked_in_at.date() >= start


def _unique_window_loan(AssetAssignment, substitute_id, start, end):
    """The one genuine loan (``is_loan``) of the stand-in unit overlapping the repair window, if any.

    Ordinary assignments are never loan evidence: the runtime lookup
    (``active_repair_loan``) only recognizes ``is_loan=True``, so linking anything else
    would report unrelated history as migrated loan evidence.
    """
    candidates = [
        assignment
        for assignment in AssetAssignment._base_manager.filter(asset_id=substitute_id, is_loan=True).order_by("pk")
        if _overlaps(assignment, start, end)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _migrate_episodes(apps, schema_editor):
    """Translate every beta-era RepairEpisode and report the outcome per case."""
    AssetAssignment = apps.get_model("assets", "AssetAssignment")
    AssetDisposal = apps.get_model("assets", "AssetDisposal")
    AssetMaintenance = apps.get_model("assets", "AssetMaintenance")
    AssetReservation = apps.get_model("assets", "AssetReservation")
    RepairEpisode = apps.get_model("assets", "RepairEpisode")
    ContentType = apps.get_model("contenttypes", "ContentType")
    JournalEntry = apps.get_model("extras", "JournalEntry")

    asset_ct = ContentType._base_manager.filter(app_label="assets", model="asset").first()
    counts = {"disposals": 0, "loans": 0, "note_appends": 0, "journal_entries": 0, "episodes": 0}

    for episode in RepairEpisode._base_manager.order_by("pk"):
        counts["episodes"] += 1
        repairs = list(AssetMaintenance._base_manager.filter(episode_id=episode.pk, maintenance_type="repair"))
        anchor = repairs[0] if len(repairs) == 1 else None
        unmapped = []
        if anchor is None:
            unmapped.append(f"The episode linked {len(repairs)} repair maintenance record(s), so nothing anchored it")

        for disposal in AssetDisposal._base_manager.filter(episode_id=episode.pk).order_by("pk"):
            if anchor is not None and disposal.maintenance_id is None:
                AssetDisposal._base_manager.filter(pk=disposal.pk).update(maintenance_id=anchor.pk)
                counts["disposals"] += 1
            else:
                unmapped.append(_describe_disposal(disposal))

        for reservation in AssetReservation._base_manager.filter(episode_id=episode.pk).order_by("pk"):
            unmapped.append(_describe_reservation(reservation))

        if episode.substitute_asset_id:
            start = anchor.start_date if anchor is not None else episode.created_at.date()
            end = (anchor.completion_date or anchor.start_date) if anchor is not None else episode.created_at.date()
            loan = _unique_window_loan(AssetAssignment, episode.substitute_asset_id, start, end)
            if anchor is not None and loan is not None and loan.maintenance_id is None:
                AssetAssignment._base_manager.filter(pk=loan.pk).update(maintenance_id=anchor.pk)
                counts["loans"] += 1
            else:
                unmapped.append(
                    f"Stand-in asset {episode.substitute_asset_id} could not be translated into exactly one loan "
                    "(only genuine loan assignments overlapping the repair window count)"
                )

        if episode.notes and episode.notes.strip():
            unmapped.append(f"Episode notes: {episode.notes.strip()}")

        if not unmapped:
            continue
        if anchor is not None:
            AssetMaintenance._base_manager.filter(pk=anchor.pk).update(notes=_append_note(anchor.notes, unmapped))
            counts["note_appends"] += 1
        elif asset_ct is not None:
            JournalEntry._base_manager.create(
                model_id=asset_ct.pk,
                object_id=episode.asset_id,
                comment=_append_note("", unmapped),
                tenant_id=None,
                tenant_group_id=None,
            )
            counts["journal_entries"] += 1

    with schema_editor.connection.cursor() as cursor:
        # The updates above queue DEFERRABLE foreign-key trigger events; every
        # following ALTER TABLE (the field removals) refuses to run with pending
        # trigger events unless they are flushed first.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    print(
        "repair episode migration (#644): "
        f"{counts['episodes']} episodes, {counts['disposals']} disposals mapped, "
        f"{counts['loans']} loans mapped, {counts['note_appends']} maintenance notes extended, "
        f"{counts['journal_entries']} asset journal entries written"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0121_supplier_scoping_and_commercial_fields"),
        ("extras", "0127_retire_report_designer_legacy"),
        ("users", "0100_issue88_shard_62_users_relations"),
    ]

    operations = [
        migrations.AddField(
            model_name="assetassignment",
            name="maintenance",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: the repair maintenance this loan or handover belongs to.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assignments",
                to="assets.assetmaintenance",
                verbose_name="Repair Maintenance",
            ),
        ),
        migrations.AddField(
            model_name="assetdisposal",
            name="maintenance",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional: the repair maintenance this disposal closes out.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="disposals",
                to="assets.assetmaintenance",
                verbose_name="Repair Maintenance",
            ),
        ),
        migrations.RunPython(_migrate_episodes, migrations.RunPython.noop),
        migrations.RemoveField(model_name="assetdisposal", name="episode"),
        migrations.RemoveField(model_name="assetreservation", name="episode"),
        migrations.RemoveField(model_name="assetmaintenance", name="episode"),
        migrations.DeleteModel(name="RepairEpisode"),
    ]
