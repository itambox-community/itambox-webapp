"""
management command: sync_intune

Usage:
  python manage.py sync_intune --tenant <slug> [--dry-run] [--now]

  --now   Run the sync inline (blocking) instead of enqueuing via django-q2.
          Useful for ad-hoc runs and cron-via-shell patterns.

Schedule example (nightly at 03:00) using django-q2:
  from django_q.models import Schedule
  Schedule.objects.create(
      func='assets.tasks.intune_sync.sync_tenant_intune',
      # kwargs are serialised into the task call
      kwargs={'tenant_id': <tenant.pk>, 'user_id': <admin_user.pk>,
              'job_id': ...},  # create a Job first, then pass its pk
      schedule_type=Schedule.CRON,
      cron='0 3 * * *',
      name=f'intune-sync-{tenant.slug}',
  )
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import transaction
from django_q.tasks import async_task

from assets.tasks.intune_sync import sync_tenant_intune
from core.models import Job
from core.tasks.management import SystemTaskCommand
from organization.models import Tenant

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(SystemTaskCommand):
    help = "Sync managed devices from Microsoft Intune for a specific tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant slug to sync.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simulate the sync without writing any data.",
        )
        parser.add_argument(
            "--now",
            action="store_true",
            default=False,
            help="Run synchronously instead of enqueuing via django-q2.",
        )

    def handle(self, *args, **options):
        tenant_slug = options["tenant"]
        dry_run = options["dry_run"]
        run_now = options["now"]

        try:
            tenant = Tenant.objects.get(slug=tenant_slug)
        except Tenant.DoesNotExist:
            raise CommandError(f"No tenant with slug {tenant_slug!r}.") from None

        intune_configs = getattr(settings, "ITAMBOX_TENANT_INTUNE_CONFIGS", {})
        if tenant_slug not in intune_configs:
            raise CommandError(
                f"No ITAMBOX_TENANT_INTUNE_CONFIGS entry for tenant '{tenant_slug}'. "
                "Set the environment variable and restart."
            )

        # Resolve a system/admin user for change-log attribution.
        admin_user = User.objects.filter(is_superuser=True).order_by("pk").first()
        if not admin_user:
            raise CommandError("No superuser found. Create one before running the sync.")

        job = Job.objects.create(
            name=f"intune-sync:{tenant_slug}",
            tenant=tenant,
            data={"tenant_slug": tenant_slug, "dry_run": dry_run},
        )
        self.stdout.write(f"Created Job #{job.pk}: {job.name}")

        if run_now:
            self.stdout.write("Running synchronously…")
            sync_tenant_intune(
                tenant_id=tenant.pk,
                user_id=admin_user.pk,
                job_id=job.pk,
                dry_run=dry_run,
            )
            job.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(f"Job finished with status: {job.status}"))
            if job.result:
                self.stdout.write(str(job.result))
        else:

            def _enqueue():
                async_task(
                    "assets.tasks.intune_sync.sync_tenant_intune",
                    tenant_id=tenant.pk,
                    user_id=admin_user.pk,
                    job_id=job.pk,
                    dry_run=dry_run,
                )

            transaction.on_commit(_enqueue)
            self.stdout.write(self.style.SUCCESS(f"Intune sync for tenant '{tenant_slug}' enqueued (Job #{job.pk})."))
