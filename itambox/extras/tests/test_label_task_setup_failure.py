"""Setup failures must resolve a pending label Job instead of leaving it stuck."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, close_old_connections, transaction
from django.utils import timezone
from model_bakery import baker

from core.context import (
    _request_id,
    get_current_request_id,
    get_current_tenant,
    get_current_user,
    set_current_tenant,
    set_current_user,
)
from core.models import Job, Notification, ObjectChange
from core.tests.mixins import TenantTestMixin
from extras.models import FileAttachment
from extras.tasks.labels import _fail_pending_label_job_after_setup_error, generate_label_pdf_batch_task
from inventory.models import Accessory
from organization.models import Tenant


@pytest.mark.django_db
class TestLabelTaskSetupFailure(TenantTestMixin):
    def setup_method(self, method):
        self.setup_tenant_context(permissions=["inventory.view_accessory"])
        self.accessory = baker.make(Accessory, tenant=self.tenant, _fill_optional=False)
        self.content_type = ContentType.objects.get_for_model(Accessory)
        self.job = Job.objects.create(
            name="Label batch",
            tenant=self.tenant,
            status=Job.STATUS_PENDING,
            data={"scope_tenant_ids": [self.tenant.pk]},
        )

    def _run(self, job_id=None):
        with pytest.raises(PermissionDenied):
            generate_label_pdf_batch_task(
                self.job.pk if job_id is None else job_id,
                self.content_type.pk,
                [self.accessory.pk],
                None,
                "roll",
                self.tenant_user.pk,
                self.tenant.pk,
            )

    def test_revoked_access_fails_the_pending_job_without_creating_a_file(self):
        self.tenant_grant.delete()

        self._run()

        self.job.refresh_from_db()
        assert self.job.status == Job.STATUS_FAILED
        assert "labels.setup_failed" in self.job.logs
        assert not FileAttachment.objects.filter(object_id=self.job.pk).exists()
        assert Notification.objects.filter(user=self.tenant_user, level=Notification.LEVEL_DANGER).count() == 1

    def test_inactive_actor_fails_the_pending_job_without_a_notification(self):
        self.tenant_user.is_active = False
        self.tenant_user.save(update_fields=["is_active"])

        self._run()

        self.job.refresh_from_db()
        assert self.job.status == Job.STATUS_FAILED
        assert "labels.setup_failed" in self.job.logs
        assert not Notification.objects.filter(user=self.tenant_user).exists()

    def test_transient_setup_error_leaves_the_job_pending_for_redelivery(self):
        with patch(
            "core.tasks.context.TaskContext._resolve_principal_and_tenant",
            side_effect=OperationalError("database temporarily unavailable"),
        ):
            with pytest.raises(OperationalError):
                generate_label_pdf_batch_task(
                    self.job.pk,
                    self.content_type.pk,
                    [self.accessory.pk],
                    None,
                    "roll",
                    self.tenant_user.pk,
                    self.tenant.pk,
                )

        self.job.refresh_from_db()
        assert self.job.status == Job.STATUS_PENDING
        assert "labels.setup_failed" not in self.job.logs
        assert not FileAttachment.objects.filter(object_id=self.job.pk).exists()
        assert not Notification.objects.filter(user=self.tenant_user).exists()

        generate_label_pdf_batch_task(
            self.job.pk,
            self.content_type.pk,
            [self.accessory.pk],
            None,
            "roll",
            self.tenant_user.pk,
            self.tenant.pk,
        )

        self.job.refresh_from_db()
        assert self.job.status == Job.STATUS_COMPLETED
        assert FileAttachment.objects.filter(object_id=self.job.pk).exists()

    @pytest.mark.parametrize("status", [Job.STATUS_COMPLETED, Job.STATUS_FAILED])
    def test_a_finished_job_is_not_overwritten(self, status):
        self.tenant_grant.delete()
        Job.objects.filter(pk=self.job.pk).update(status=status, completed=timezone.now())

        self._run()

        self.job.refresh_from_db()
        assert self.job.status == status

    def test_a_missing_job_is_ignored(self):
        self.tenant_grant.delete()

        self._run(job_id=0)

        assert not Notification.objects.exists()

    def test_failure_while_recording_the_setup_error_keeps_the_original_error(self):
        self.tenant_grant.delete()
        with patch(
            "extras.tasks.labels.Job.mark_failed", side_effect=RuntimeError("database unavailable")
        ) as mark_failed:
            with pytest.raises(PermissionDenied):
                generate_label_pdf_batch_task(
                    self.job.pk,
                    self.content_type.pk,
                    [self.accessory.pk],
                    None,
                    "roll",
                    self.tenant_user.pk,
                    self.tenant.pk,
                )
        mark_failed.assert_called_once_with("[terminal] labels.setup_failed")

    def test_failure_building_the_notification_url_keeps_the_original_error(self):
        self.tenant_grant.delete()
        with patch("extras.tasks.labels.reverse_job_detail", side_effect=RuntimeError("no route")):
            with pytest.raises(PermissionDenied):
                generate_label_pdf_batch_task(
                    self.job.pk,
                    self.content_type.pk,
                    [self.accessory.pk],
                    None,
                    "roll",
                    self.tenant_user.pk,
                    self.tenant.pk,
                )
        self.job.refresh_from_db()
        assert self.job.status == Job.STATUS_FAILED
        assert "labels.setup_failed" in self.job.logs

    def test_context_is_restored_after_a_setup_failure(self):
        self.tenant_grant.delete()
        set_current_tenant(self.tenant)

        self._run()

        assert get_current_tenant() == self.tenant

    def test_setup_failure_is_audited_with_a_fresh_request_id(self):
        self.tenant_grant.delete()
        previous_request_id = uuid.uuid4()
        _request_id.set(previous_request_id)
        set_current_user(self.tenant_user)
        set_current_tenant(self.tenant)

        self._run()

        assert get_current_request_id() == previous_request_id
        assert get_current_user() == self.tenant_user
        assert get_current_tenant() == self.tenant
        change = ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(Job),
            changed_object_id=self.job.pk,
            request_id__isnull=False,
        ).first()
        assert change is not None
        assert change.request_id != previous_request_id


@pytest.mark.django_db(transaction=True)
class TestLabelSetupFailureRace:
    """A concurrent worker that already claimed the Job wins the race."""

    def setup_method(self, method):
        self.tenant = Tenant.objects.create(name="Label Race", slug="label-race")
        self.job = Job.objects.create(
            name="Label race batch",
            tenant=self.tenant,
            status=Job.STATUS_PENDING,
            data={"scope_tenant_ids": [self.tenant.pk]},
        )

    def test_a_running_job_is_not_overwritten_after_the_lock_is_released(self):
        locked = Event()
        original_status = self.job.status

        def claim_job():
            close_old_connections()
            try:
                with transaction.atomic():
                    Job._base_manager.select_for_update().get(pk=self.job.pk)
                    locked.set()
                    time.sleep(1)
                    Job._base_manager.filter(pk=self.job.pk).update(status=Job.STATUS_RUNNING)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            claim = pool.submit(claim_job)
            assert locked.wait(timeout=5)
            _fail_pending_label_job_after_setup_error(self.job.pk, None, self.tenant.pk, RuntimeError("access revoked"))
            claim.result(timeout=10)

        self.job.refresh_from_db()
        assert original_status == Job.STATUS_PENDING
        assert self.job.status == Job.STATUS_RUNNING
