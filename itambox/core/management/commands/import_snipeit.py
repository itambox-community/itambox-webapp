"""
management command: import_snipeit

Pulls data from a Snipe-IT instance via its REST API and imports it into ITAMbox.

    python manage.py import_snipeit \\
        --url https://snipe.example \\
        --token-env SNIPEIT_TOKEN \\
        [--tenant <slug>] \\
        [--map-companies-to-tenants] \\
        [--dry-run] \\
        [--skip assets,licenses,...] \\
        [--update]

Out of scope (v1): images/file uploads, activity history, depreciation schedules,
kits, Snipe "requestable" requests.
"""

import logging
import os
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.utils import timezone

from assets.choices import StatusTypeChoices
from assets.models.choices import WarrantyTypeChoices
from assets.services import checkout_asset
from core.errors import IntegrationContext
from core.importers.snipeit import SnipeITClient, SnipeITError, SnipeITImporter
from core.models import Job
from core.tasks.context import TaskContext
from core.tasks.management import SystemTaskCommand
from inventory.services import checkout_inventory_item, create_component_allocation
from organization.models import Tenant

User = get_user_model()
logger = logging.getLogger(__name__)


class Command(SystemTaskCommand):
    help = (
        "Import data from a Snipe-IT instance into ITAMbox.\n\n"
        "IMPORTANT: Run with --dry-run first to verify the mapping.\n\n"
        "Out of scope (v1): images, activity history, depreciation schedules, kits."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            required=True,
            metavar="URL",
            help="Snipe-IT base URL, e.g. https://snipe.example (no trailing slash).",
        )
        parser.add_argument(
            "--token-env",
            required=True,
            metavar="ENV_VAR",
            help="Name of the environment variable that holds the Snipe-IT API token.",
        )
        parser.add_argument(
            "--tenant",
            metavar="SLUG",
            default=None,
            help="Target ITAMbox tenant slug. Required unless --map-companies-to-tenants is set.",
        )
        parser.add_argument(
            "--map-companies-to-tenants",
            action="store_true",
            default=False,
            help="Create/use one ITAMbox tenant per Snipe-IT company (MSP mode).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Fetch and report the mapping without writing anything to the database.",
        )
        parser.add_argument(
            "--skip",
            metavar="ENTITIES",
            default="",
            help="Comma-separated list of entity types to skip: assets,accessories,consumables,components,licenses,maintenances",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            default=False,
            help="Re-sync fields on existing records (default: skip existing).",
        )
        parser.add_argument(
            "--admin-user",
            metavar="USERNAME",
            default=None,
            help="Username of the ITAMbox admin who owns the import job/changes (default: first superuser).",
        )

    def handle(self, *args, **options):

        # Resolve API token from environment
        token_env = options["token_env"]
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise CommandError(
                f"Environment variable '{token_env}' is empty or not set. "
                "Export it before running: export SNIPEIT_TOKEN=<your-token>"
            )

        base_url = options["url"].rstrip("/")
        map_companies = options["map_companies_to_tenants"]
        dry_run = options["dry_run"]
        update = options["update"]
        skip = {s.strip() for s in options["skip"].split(",") if s.strip()}

        # Resolve tenant
        tenant = None
        if options["tenant"]:
            try:
                tenant = Tenant.objects.get(slug=options["tenant"])
            except Tenant.DoesNotExist:
                raise CommandError(f"Tenant with slug {options['tenant']!r} not found.") from None
        elif not map_companies:
            raise CommandError("Either --tenant <slug> or --map-companies-to-tenants is required.")

        # Resolve admin user
        admin_username = options.get("admin_user")
        if admin_username:
            try:
                user = User.objects.get(username=admin_username)
            except User.DoesNotExist:
                raise CommandError(f"User {admin_username!r} not found.") from None
        else:
            user = User.objects.filter(is_superuser=True).order_by("pk").first()
            if not user:
                raise CommandError("No superuser found. Create one first or pass --admin-user.")

        client = SnipeITClient(
            base_url=base_url,
            token=token,
            context=IntegrationContext(
                provider="snipe-it",
                operation="import",
                tenant_id=tenant.pk if tenant else None,
                actor_id=user.pk,
            ),
        )
        base_url = client.base_url

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No data will be written to the database.\n"))

        # Create Job record for visibility in the Jobs UI
        job_name = f"Snipe-IT Import from {base_url}"
        if dry_run:
            job_name += " (dry-run)"
        if not dry_run:
            job = Job.objects.create(
                name=job_name,
                tenant=tenant,
                status=Job.STATUS_PENDING,
                data={
                    "source": "snipeit",
                    "url": base_url,
                    "tenant": tenant.slug if tenant else None,
                    "map_companies": map_companies,
                    "skip": list(skip),
                    "update": update,
                    "started_by": user.username,
                },
            )
            job.mark_running()
        else:
            job = None

        started = timezone.now()
        self.stdout.write(f"Importing from {base_url} …")
        if tenant:
            self.stdout.write(f"  Target tenant: {tenant.name}")
        if map_companies:
            self.stdout.write("  Companies → Tenants: ON")
        if skip:
            self.stdout.write(f"  Skipping: {', '.join(sorted(skip))}")

        # Verify connectivity
        try:
            client.get_detail("/api/v1/statuslabels?limit=1&offset=0")
        except SnipeITError as exc:
            msg = f"Cannot connect to Snipe-IT: {exc.display_message()}"
            log_extra = exc.log_extra()
            logger.error(
                "Snipe-IT connectivity check failed integration=%s",
                log_extra["integration"],
                extra=log_extra,
            )
            if job:
                job.mark_failed(msg)
            raise CommandError(msg) from None

        with TaskContext(
            tenant_id=tenant.pk if tenant else None,
            user_id=user.pk,
        ):
            importer = SnipeITImporter(
                client=client,
                tenant=tenant,
                user=user,
                dry_run=dry_run,
                update=update,
                map_companies=map_companies,
                skip=skip,
                job=job,
                stdout=self.stdout,
                # This command is the composition root: it owns the edge to the
                # inventory domain services so the importer itself does not.
                checkout_inventory_item=checkout_inventory_item,
                create_component_allocation=create_component_allocation,
                checkout_asset=checkout_asset,
                deployed_status_type=StatusTypeChoices.DEPLOYED,
                warranty_type=WarrantyTypeChoices.HARDWARE,
            )
            try:
                counts = importer.run()
            except SnipeITError as exc:
                msg = f"Import aborted: {exc.display_message()}"
                log_extra = exc.log_extra()
                logger.error(
                    "Snipe-IT import aborted integration=%s",
                    log_extra["integration"],
                    extra=log_extra,
                )
                self.stdout.write(self.style.ERROR(msg))
                if job:
                    job.mark_failed(msg)
                sys.exit(1)
            stage_results = importer.stage_results

        elapsed = (timezone.now() - started).total_seconds()

        # Summary
        self.stdout.write("\n" + self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("DRY RUN complete" if dry_run else "Import complete"))
        self.stdout.write(f"Elapsed: {elapsed:.1f}s\n")

        total_created = total_updated = total_failed = 0
        for entity, stats in counts.items():
            warning_count = stage_results[entity].warning_count
            self.stdout.write(
                f"  {entity:20s}  "
                f"created={stats['created']:4d}  "
                f"updated={stats['updated']:4d}  "
                f"skipped={stats['skipped']:4d}  "
                f"failed={stats['failed']:4d}  "
                f"warnings={warning_count:4d}"
            )
            total_created += stats["created"]
            total_updated += stats["updated"]
            total_failed += stats["failed"]

        self.stdout.write(
            self.style.SUCCESS(f"\nTotal: {total_created} created, {total_updated} updated, {total_failed} failed")
        )

        if job:
            job.mark_completed(
                result={
                    "total_created": total_created,
                    "total_updated": total_updated,
                    "total_failed": total_failed,
                    "elapsed_seconds": elapsed,
                    "counts": counts,
                    "warning_counts": {key: result.warning_count for key, result in stage_results.items()},
                    "total_warnings": sum(result.warning_count for result in stage_results.values()),
                }
            )
