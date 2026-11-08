"""#1052 regression: a TaskContext setup failure must not leave the import Job pending."""

import pytest
from django.utils import timezone

from core.models import Job, Notification
from core.tasks.csv_import import IMPORT_ABORT_MESSAGE, import_csv_task
from core.tests.mixins import TenantTestMixin


@pytest.mark.django_db
class TestImportSetupFailure(TenantTestMixin):
    def _run(self):
        job = Job.objects.create(name="Bulk Import", tenant=self.tenant, status=Job.STATUS_PENDING)
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
