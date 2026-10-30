from django.utils import timezone

from core.models import Job
from core.tasks.management import SystemTaskCommand


class Command(SystemTaskCommand):
    help = "Process pending background jobs from the database job queue."

    def handle(self, *args, **options):
        now = timezone.now()

        pending_jobs = Job.objects.filter(
            status=Job.STATUS_PENDING,
        ).exclude(
            scheduled_for__gt=now,
        )

        if not pending_jobs.exists():
            self.stdout.write(self.style.SUCCESS("No pending jobs to process."))
            return

        processed = 0
        failed = 0

        for job in pending_jobs:
            try:
                if not job.mark_running():
                    continue
                self._execute_job(job)
                job.mark_completed({"status": "success"})
                processed += 1
                self.stdout.write(self.style.SUCCESS(f'Job "{job.name}" completed.'))
            # broad except: task-isolation: one failing job is recorded as failed and must not stop the queue
            except Exception as e:
                job.mark_failed(str(e))
                failed += 1
                self.stderr.write(self.style.ERROR(f'Job "{job.name}" failed: {e}'))

        self.stdout.write(self.style.SUCCESS(f"Job processing complete: {processed} succeeded, {failed} failed."))

    def _execute_job(self, job):
        if hasattr(self, f"_run_{job.name.replace(':', '_').replace(' ', '_').lower()}"):
            handler = getattr(self, f"_run_{job.name.replace(':', '_').replace(' ', '_').lower()}")
            handler(job)
