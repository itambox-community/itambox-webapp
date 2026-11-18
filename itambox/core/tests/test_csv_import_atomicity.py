"""Regression coverage for the CSV import's durable outcome boundary.

Imported rows and the Job's terminal status must become durable together.  A
failure while persisting the status or while writing the post-import log must
not leave committed rows behind a running or failed Job.
"""

from unittest.mock import patch

import pytest
from django.db import DatabaseError

from assets.models import Manufacturer
from core.models import Job
from core.tasks.csv_import import IMPORT_ABORT_MESSAGE, import_csv_task
from core.tests.mixins import TenantTestMixin


@pytest.mark.django_db
class TestCsvImportAtomicOutcome(TenantTestMixin):
    def _job(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        return Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)

    def _run(self, job, rows):
        import_csv_task(
            job_id=job.pk,
            rows_data=rows,
            app_label="assets",
            model_name="manufacturer",
            user_id=self.tenant_user.pk,
            tenant_id=self.tenant.pk,
        )

    def test_status_save_failure_rolls_back_imported_rows_and_fails_job(self):
        job = self._job()
        original_save = Job.save

        def fail_completed_save(instance, *args, **kwargs):
            update_fields = kwargs.get("update_fields") or ()
            if instance.status == Job.STATUS_COMPLETED and "status" in update_fields:
                raise DatabaseError("simulated status write failure")
            return original_save(instance, *args, **kwargs)

        with patch.object(Job, "save", fail_completed_save):
            self._run(job, [{"name": "Atomic Maker", "slug": "atomic-maker"}])

        assert not Manufacturer.objects.filter(slug="atomic-maker").exists()
        job.refresh_from_db()
        assert job.status == Job.STATUS_FAILED
        assert IMPORT_ABORT_MESSAGE in job.logs

    def test_log_append_failure_rolls_back_imported_rows_and_fails_job(self):
        job = self._job()
        original_append = Job.append_log

        def fail_final_log(instance, message):
            if "Import finished" in message:
                raise DatabaseError("simulated log write failure")
            return original_append(instance, message)

        with patch.object(Job, "append_log", fail_final_log):
            self._run(job, [{"name": "Log Maker", "slug": "log-maker"}])

        assert not Manufacturer.objects.filter(slug="log-maker").exists()
        job.refresh_from_db()
        assert job.status == Job.STATUS_FAILED
        assert IMPORT_ABORT_MESSAGE in job.logs

    def test_first_log_failure_marks_the_job_failed_instead_of_leaving_it_running(self):
        job = self._job()
        original_append = Job.append_log

        def fail_first_log(instance, message):
            if "Initializing asynchronous import pipeline" in message:
                raise DatabaseError("simulated first log write failure")
            return original_append(instance, message)

        with patch.object(Job, "append_log", fail_first_log):
            self._run(job, [{"name": "Running Maker", "slug": "running-maker"}])

        assert not Manufacturer.objects.filter(slug="running-maker").exists()
        job.refresh_from_db()
        assert job.status == Job.STATUS_FAILED
        assert IMPORT_ABORT_MESSAGE in job.logs

    def test_partial_success_commits_rows_and_completed_status_together(self):
        job = self._job()

        self._run(
            job,
            [
                {"name": "Partial Maker", "slug": "partial-maker"},
                {"name": "", "slug": "invalid-maker"},
            ],
        )

        job.refresh_from_db()
        assert Manufacturer.objects.filter(slug="partial-maker").exists()
        assert not Manufacturer.objects.filter(slug="invalid-maker").exists()
        assert job.status == Job.STATUS_COMPLETED
        assert job.result == {"imported": 1, "failed": 1, "total": 2}
