"""Operator diagnostics and start-up pre-flight for configured plugins."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from itambox.plugins import diagnostics, preflight
from itambox.plugins.utils import get_plugin_diagnostics


class Command(BaseCommand):
    help = (
        "Report configured plugins (manifest, load state, pending migrations, orphaned data). "
        "With --check, run the start-up phases in a child process and exit non-zero when startup would abort."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=("table", "json"),
            default="table",
            help="Output format (default: table).",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Pre-flight: resolve, compose and assemble every configured plugin without serving requests.",
        )

    def handle(self, *args, **options):
        if options["check"]:
            self._check(options["format"])
            return
        active = tuple(getattr(settings, "PLUGINS_ACTIVE", ()))
        skipped = get_plugin_diagnostics()
        inventory = diagnostics.plugin_inventory()
        orphans = diagnostics.find_orphans()
        if options["format"] == "json":
            payload = {
                "active": active,
                "diagnostics": list(skipped),
                "plugins": inventory,
                "orphans": orphans,
            }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return
        self._table(inventory, skipped, orphans)

    def _check(self, output_format):
        report = preflight.run_preflight()
        self.stdout.write(preflight.format_report(report, output_format))
        if report["abort"]:
            raise CommandError("Startup would abort.", returncode=1)

    def _table(self, inventory, skipped, orphans):
        self.stdout.write("PLUGIN  STATE   VERSION  RANGE  PENDING MIGRATIONS  DETAILS")
        self.stdout.write("------  ------  -------  -----  ------------------  -------")
        details = {row["plugin"]: row for row in skipped}
        for row in inventory:
            pending = row["pending_migrations"]
            pending_text = "unknown" if pending is None else str(len(pending))
            extra = ""
            if row["state"] == "skipped":
                found = details[row["plugin"]]
                extra = f"{found['failure_class']} at {found['stage']} ({found['compatibility']})"
            self.stdout.write(
                f"{row['plugin']}  {row['state']}  {row['version'] or '-'}  {row['itambox_range'] or '-'}  "
                f"{pending_text}  {extra}".rstrip()
            )
        if not inventory:
            self.stdout.write("(none configured)")
        if "error" in orphans:
            self.stdout.write(f"Orphaned data: {orphans['error']}")
            return
        self.stdout.write(
            "Orphaned data (read-only): "
            f"{len(orphans['tables'])} tables, {len(orphans['content_types'])} content types, "
            f"{len(orphans['migrations'])} migration records"
        )
        for table in orphans["tables"]:
            self.stdout.write(f"  table {table}")
