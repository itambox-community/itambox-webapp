"""#1052 regression: a TaskContext setup failure must not leave the import Job pending."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import OperationalError, close_old_connections, transaction
from django.utils import timezone

from core.models import Job, Notification
from core.tasks.csv_import import IMPORT_ABORT_MESSAGE, _fail_job_after_setup_error, import_csv_task
from core.tests.mixins import TenantTestMixin


@pytest.mark.django_db
class TestImportSetupFailure(TenantTestMixin):
    def _run(self):
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)
        # The setup failure still propagates so the worker records it.
        with pytest.raises((ObjectDoesNotExist, PermissionDenied)):
            import_csv_task(
                job_id=job.pk,
                rows_data=[{"name": "Acme", "slug": "acme"}],
                app_label="assets",
                model_name="manufacturer",
                user_id=self.tenant_user.pk,
                tenant_id=self.tenant.pk,
            )
        job.refresh_from_db()
        return job

    def _assert_failed_and_notified(self, job):
        assert job.status == Job.STATUS_FAILED
        assert IMPORT_ABORT_MESSAGE in job.logs
        assert Notification.objects.filter(user=self.tenant_user, level=Notification.LEVEL_DANGER).count() == 1

    def test_transient_setup_error_leaves_the_job_pending_for_redelivery(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        job = Job.objects.create(name="Bulk Import transient", tenant=self.tenant, status=Job.STATUS_PENDING)

        with patch(
            "core.tasks.context.TaskContext._resolve_principal_and_tenant",
            side_effect=OperationalError("database temporarily unavailable"),
        ):
            with pytest.raises(OperationalError):
                import_csv_task(
                    job_id=job.pk,
                    rows_data=[{"name": "Acme", "slug": "acme"}],
                    app_label="assets",
                    model_name="manufacturer",
                    user_id=self.tenant_user.pk,
                    tenant_id=self.tenant.pk,
                )

        job.refresh_from_db()
        assert job.status == Job.STATUS_PENDING
        assert IMPORT_ABORT_MESSAGE not in job.logs
        assert not Notification.objects.filter(user=self.tenant_user).exists()

    def test_deleted_tenant_fails_job_and_notifies(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        self.tenant.deleted_at = timezone.now()
        self.tenant.save(update_fields=["deleted_at"])
        self._assert_failed_and_notified(self._run())

    def test_inactive_actor_fails_job_without_notification(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        self.tenant_user.is_active = False
        self.tenant_user.save(update_fields=["is_active"])
        job = self._run()
        assert job.status == Job.STATUS_FAILED
        assert IMPORT_ABORT_MESSAGE in job.logs
        assert not Notification.objects.filter(user=self.tenant_user).exists()

    def test_revoked_access_fails_job_and_notifies(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        self.tenant_grant.delete()
        self._assert_failed_and_notified(self._run())

    def _setup_failing(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        self.tenant.deleted_at = timezone.now()
        self.tenant.save(update_fields=["deleted_at"])

    def _call(self, job_id, user_id):
        with pytest.raises(ObjectDoesNotExist):
            import_csv_task(
                job_id=job_id,
                rows_data=[],
                app_label="assets",
                model_name="manufacturer",
                user_id=user_id,
                tenant_id=self.tenant.pk,
            )

    def test_missing_job_is_ignored(self):
        self._setup_failing()
        self._call(0, self.tenant_user.pk)
        assert not Notification.objects.exists()

    def test_job_no_longer_pending_is_left_untouched(self):
        self._setup_failing()
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_COMPLETED)
        self._call(job.pk, self.tenant_user.pk)
        job.refresh_from_db()
        assert job.status == Job.STATUS_COMPLETED
        assert not Notification.objects.exists()

    def test_actorless_task_fails_job_without_notification(self):
        self._setup_failing()
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)
        self._call(job.pk, None)
        job.refresh_from_db()
        assert job.status == Job.STATUS_FAILED
        assert not Notification.objects.exists()


@pytest.mark.django_db(transaction=True)
class TestCsvSetupFailureRace(TenantTestMixin):
    """A setup-failure handler must not overwrite a job another worker completed."""

    def test_a_completed_job_keeps_its_result_instead_of_being_marked_failed(self):
        self.setup_tenant_context(permissions=["assets.add_manufacturer"])
        job = Job.objects.create(name="Bulk Import race", tenant=self.tenant, status=Job.STATUS_PENDING)
        locked = Event()

        def complete_job():
            close_old_connections()
            try:
                with transaction.atomic():
                    Job._base_manager.select_for_update().get(pk=job.pk)
                    locked.set()
                    time.sleep(1)
                    Job._base_manager.filter(pk=job.pk).update(
                        status=Job.STATUS_COMPLETED,
                        result={"imported": 1},
                        completed=timezone.now(),
                    )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            completion = pool.submit(complete_job)
            assert locked.wait(timeout=5)
            _fail_job_after_setup_error(
                job.pk,
                self.tenant_user.pk,
                self.tenant.pk,
                PermissionDenied("access revoked during setup"),
            )
            completion.result(timeout=10)

        job.refresh_from_db()
        assert job.status == Job.STATUS_COMPLETED
        assert job.result == {"imported": 1}
        assert not Notification.objects.filter(user=self.tenant_user).exists()
