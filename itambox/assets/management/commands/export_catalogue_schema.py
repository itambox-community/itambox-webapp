from pathlib import Path

from django.core.management.base import BaseCommand

from assets.services.specifications.catalogue_schema import SCHEMA_PATH, render_catalogue_schema


class Command(BaseCommand):
    help = "Write the catalogue JSON Schema (generated_schema.json) derived from the core vocabulary."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(SCHEMA_PATH), help="Target file (default: the committed copy).")

    def handle(self, *args, **options):
        target = Path(options["output"])
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_catalogue_schema())
        self.stdout.write(f"Wrote {target}")
