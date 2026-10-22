import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.db import OperationalError
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel

# The former asset-only task path: kept as a queue-compatibility entry that
# delegates to extras.tasks.labels, so these tests also cover upgrade-queued jobs.
from assets.tasks.labels import generate_label_pdf_batch_task
from core.models import Job, Notification
from core.tasks.utils import TaskStatus
from core.tests.mixins import grant
from extras.models import FileAttachment, LabelTemplate
from organization.models import Membership, Role, Tenant, TenantGroup

User = get_user_model()


class BulkActionsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

        self.tenant = Tenant.objects.create(name="Test Tenant", slug="test-tenant")
        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.role = AssetRole.objects.create(name="Laptop", slug="laptop")
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="ThinkPad T14", slug="lenovo-thinkpad-t14"
        )
        self.status, _ = StatusLabel.objects.get_or_create(
            slug="available", defaults={"name": "Available", "type": "deployable"}
        )

        self.asset1 = Asset.objects.create(
            name="Asset 1",
            asset_tag="AST-001",
            asset_type=self.asset_type,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )
        self.asset2 = Asset.objects.create(
            name="Asset 2",
            asset_tag="AST-002",
            asset_type=self.asset_type,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )

        self.label_template = LabelTemplate.objects.create(
            name="Standard QR",
            description="Standard QR label",
            barcode_format="qr",
            template_code="<div>{{ asset.name }}</div>",
        )
        self.label_template.content_types.add(ContentType.objects.get_for_model(Asset))

    @patch("extras.label_views.LABEL_SYNC_LIMIT", 0)
    @patch("extras.label_views.async_task")
    def test_bulk_print_labels(self, mock_async):
        url = reverse("label_print", kwargs={"app_label": "assets", "model_name": "asset"})
        post_data = {
            "pk": [self.asset1.pk, self.asset2.pk],
            "_print": "1",
            "template": self.label_template.pk,
            "layout": "roll",
        }

        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        # Verify Job was created
        job = Job.objects.filter(name__contains="Label printing").first()
        self.assertIsNotNone(job)
        self.assertEqual(job.status, Job.STATUS_PENDING)

        # Verify async_task was called with the generic task
        mock_async.assert_called_once()
        args = mock_async.call_args[0]
        self.assertEqual(args[0], "extras.tasks.labels.generate_label_pdf_batch_task")
        self.assertEqual(args[1], job.pk)
        self.assertEqual(args[2], ContentType.objects.get_for_model(Asset).pk)
        self.assertEqual(args[3], [self.asset1.pk, self.asset2.pk])
        self.assertEqual(args[4], self.label_template.pk)
        self.assertEqual(args[5], "roll")

    def test_small_selection_prints_inline_without_a_job(self):
        url = reverse("label_print", kwargs={"app_label": "assets", "model_name": "asset"})
        before = Job.objects.count()
        response = self.client.post(
            url,
            {
                "pk": [self.asset1.pk, self.asset2.pk],
                "_print": "1",
                "template": self.label_template.pk,
                "layout": "roll",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(len(PdfReader(io.BytesIO(response.content)).pages), 2)
        self.assertEqual(Job.objects.count(), before)

    def _job(self):
        return Job.objects.create(name="labels", tenant=self.tenant, status=Job.STATUS_PENDING)

    def test_label_tasks_classify_missing_and_non_pending_jobs(self):
        missing_pdf = generate_label_pdf_batch_task(
            999999, [], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
        )
        pdf_job = self._job()
        pdf_job.status = Job.STATUS_FAILED
        pdf_job.save(update_fields=["status"])

        skipped_pdf = generate_label_pdf_batch_task(
            pdf_job.pk, [], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
        )

        self.assertEqual((missing_pdf.status, missing_pdf.code), (TaskStatus.TERMINAL, "labels.job_not_found"))
        self.assertEqual((skipped_pdf.status, skipped_pdf.code), (TaskStatus.SKIPPED, "labels.job_not_pending"))

    def test_pdf_task_classifies_missing_template_and_assets(self):
        missing_template_job = self._job()
        missing_template = generate_label_pdf_batch_task(
            missing_template_job.pk, [], 999999, "roll", self.user.pk, self.tenant.pk
        )
        no_assets_job = self._job()
        no_assets = generate_label_pdf_batch_task(
            no_assets_job.pk, [], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
        )

        missing_template_job.refresh_from_db()
        no_assets_job.refresh_from_db()
        self.assertEqual(
            (missing_template.status, missing_template.code), (TaskStatus.TERMINAL, "labels.template_not_found")
        )
        self.assertEqual(missing_template_job.status, Job.STATUS_FAILED)
        self.assertEqual((no_assets.status, no_assets.code), (TaskStatus.SKIPPED, "labels.no_objects"))
        self.assertEqual(no_assets_job.result, {"status": "no_objects"})

    @patch("extras.tasks.labels.render_object_card", side_effect=RuntimeError("secret-asset-label"))
    def test_pdf_task_all_render_failures_are_terminal_and_redacted(self, _barcode):
        job = self._job()

        with self.assertLogs("extras.tasks.labels", level="WARNING") as captured:
            result = generate_label_pdf_batch_task(
                job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
            )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.no_labels_rendered"))
        self.assertNotIn("secret-asset-label", " ".join(captured.output) + " " + job.logs)
        self.assertNotIn(self.asset1.asset_tag, job.logs)
        self.assertNotIn(self.asset1.name, job.logs)

    @patch("extras.tasks.labels.html_to_pdf_bytes", return_value=b"%PDF-test")
    @patch("extras.services.labels.render_label_card", return_value="<div>safe</div>")
    @patch("extras.tasks.labels.render_object_card", side_effect=["data:image/png;base64,AAAA", ValueError("secret")])
    def test_pdf_task_returns_partial_without_exposing_asset_labels(self, _barcode, _render, _pdf):
        job = self._job()

        result = generate_label_pdf_batch_task(
            job.pk,
            [self.asset1.pk, self.asset2.pk],
            self.label_template.pk,
            "roll",
            self.user.pk,
            self.tenant.pk,
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.PARTIAL, "labels.pdf_partial"))
        self.assertEqual(dict(result.counts), {"rendered": 1, "failed": 1})
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertNotIn(self.asset1.asset_tag, job.logs)
        self.assertNotIn(self.asset1.name, job.logs)

    @patch("extras.tasks.labels.html_to_pdf_bytes", side_effect=OperationalError("secret-pdf-payload"))
    @patch("extras.services.labels.render_label_card", return_value="<div>safe</div>")
    @patch("extras.tasks.labels.render_object_card", return_value="data:image/png;base64,AAAA")
    def test_pdf_boundary_database_failure_is_retryable_and_redacted(self, _barcode, _render, _pdf):
        job = self._job()

        result = generate_label_pdf_batch_task(
            job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "labels.pdf_failed"))
        self.assertNotIn("secret-pdf-payload", job.logs)

    def test_pdf_task_completes_with_real_renderer_and_valid_attachment(self):
        job = self._job()

        result = generate_label_pdf_batch_task(
            job.pk, [self.asset1.pk, self.asset2.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.SUCCESS, "labels.pdf_completed"))
        self.assertEqual(dict(result.counts), {"rendered": 2, "failed": 0})
        self.assertEqual(job.status, Job.STATUS_COMPLETED)

        ct = ContentType.objects.get_for_model(Job)
        attachments = FileAttachment.objects.filter(model=ct, object_id=job.pk)
        self.assertEqual(attachments.count(), 1)
        attachment = attachments.first()
        self.assertEqual(attachment.mime_type, "application/pdf")
        data = attachment.file.read()
        self.assertTrue(data.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(data))
        self.assertEqual(len(reader.pages), 2)

        self.assertIn("download_url", job.result)
        self.assertTrue(job.result["download_url"].startswith("/"))
        self.assertTrue(
            Notification.objects.filter(
                user=self.user, level=Notification.LEVEL_SUCCESS, target_url__startswith="/"
            ).exists()
        )

    @patch("extras.tasks.labels.Notification.objects.create", side_effect=OperationalError("secret-notify"))
    def test_pdf_task_success_notification_failure_does_not_fail_completed_job(self, _notify):
        job = self._job()

        with self.assertLogs("extras.tasks.labels", level="ERROR") as captured:
            result = generate_label_pdf_batch_task(
                job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
            )

        job.refresh_from_db()
        ct = ContentType.objects.get_for_model(Job)
        attachment = FileAttachment.objects.filter(model=ct, object_id=job.pk).first()
        self.assertEqual((result.status, result.code), (TaskStatus.SUCCESS, "labels.pdf_completed"))
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertIsNotNone(attachment)
        data = attachment.file.read()
        self.assertTrue(data.startswith(b"%PDF-"))
        PdfReader(io.BytesIO(data))
        self.assertIn("(phase=notification)", job.logs)
        self.assertNotIn("secret-notify", " ".join(captured.output) + " " + job.logs)

    @patch("extras.tasks.labels.html_to_pdf_bytes", side_effect=RuntimeError("secret-render-payload"))
    def test_pdf_task_render_failure_is_terminal_phased_and_redacted(self, _render):
        job = self._job()

        with self.assertLogs("extras.tasks.labels", level="ERROR") as captured:
            result = generate_label_pdf_batch_task(
                job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
            )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.pdf_failed"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertIn("phase=pdf_render", job.logs)
        self.assertNotIn("secret-render-payload", " ".join(captured.output) + " " + job.logs)
        ct = ContentType.objects.get_for_model(Job)
        self.assertEqual(FileAttachment.objects.filter(model=ct, object_id=job.pk).count(), 0)
        notification = Notification.objects.filter(user=self.user, level=Notification.LEVEL_DANGER).first()
        self.assertIsNotNone(notification)
        self.assertNotIn("secret-render-payload", notification.message)

    @patch("extras.tasks.labels.Job.objects.filter", side_effect=OperationalError("secret-database"))
    def test_pdf_entry_database_failure_is_retryable_and_not_masked(self, _get):
        # The PDF task's single task boundary also covers Job resolution: a
        # transient failure there must be classified, with the boundary state
        # (phase/attachment) bound — never replaced by an UnboundLocalError.
        with self.assertLogs("extras.tasks.labels", level="ERROR") as captured:
            result = generate_label_pdf_batch_task(17, [], self.label_template.pk, "roll", self.user.pk, self.tenant.pk)

        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "labels.pdf_failed"))
        self.assertNotIn("secret-database", " ".join(captured.output))
        self.assertTrue(any(getattr(record, "phase", None) == "job_resolve" for record in captured.records))

    @patch("extras.tasks.labels.Notification.objects.create", side_effect=OperationalError("secret-notify"))
    @patch("extras.tasks.labels.html_to_pdf_bytes", side_effect=RuntimeError("secret-render"))
    def test_pdf_task_failure_notification_failure_does_not_mask_task_outcome(self, _render, _notify):
        # The failure notification is best-effort: even if BOTH the render and
        # the notification delivery fail, the original task outcome and job
        # state must survive, redacted.
        job = self._job()

        with self.assertLogs("extras.tasks.labels", level="ERROR") as captured:
            result = generate_label_pdf_batch_task(
                job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
            )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.pdf_failed"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertIn("phase=pdf_render", job.logs)
        self.assertIn("(phase=notification)", job.logs)
        self.assertNotIn("secret-render", " ".join(captured.output) + " " + job.logs)
        self.assertNotIn("secret-notify", " ".join(captured.output) + " " + job.logs)
        ct = ContentType.objects.get_for_model(Job)
        self.assertEqual(FileAttachment.objects.filter(model=ct, object_id=job.pk).count(), 0)

    def test_pdf_task_persist_failure_cleans_up_partial_attachment(self):
        job = self._job()

        real_save = FileAttachment.save
        calls = {"n": 0}

        def flaky_save(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # allow the create() insert; the explicit save() afterwards fails
                return real_save(self, *args, **kwargs)
            raise OperationalError("secret-storage")

        with (
            self.assertLogs("extras.tasks.labels", level="ERROR") as captured,
            patch.object(FileAttachment, "save", new=flaky_save),
        ):
            result = generate_label_pdf_batch_task(
                job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
            )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "labels.pdf_failed"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertIn("phase=attachment_persist", job.logs)
        self.assertNotIn("secret-storage", " ".join(captured.output) + " " + job.logs)
        ct = ContentType.objects.get_for_model(Job)
        self.assertEqual(FileAttachment.objects.filter(model=ct, object_id=job.pk).count(), 0)
        self.assertEqual(
            [name for name in default_storage.listdir("attachments/files")[1] if name == f"labels_batch_{job.pk}.pdf"],
            [],
        )

    @patch("extras.models.FileAttachment.delete", side_effect=OperationalError("secret-cleanup"))
    def test_pdf_task_persist_failure_reports_cleanup_failure_without_leaking(self, _delete):
        job = self._job()

        real_save = FileAttachment.save
        calls = {"n": 0}

        def flaky_save(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_save(self, *args, **kwargs)
            raise OperationalError("secret-storage")

        with (
            self.assertLogs("extras.tasks.labels", level="ERROR") as captured,
            patch.object(FileAttachment, "save", new=flaky_save),
        ):
            result = generate_label_pdf_batch_task(
                job.pk, [self.asset1.pk], self.label_template.pk, "roll", self.user.pk, self.tenant.pk
            )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "labels.pdf_failed"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        # cleanup failed, so the orphan row remains — but nothing sensitive leaked
        ct = ContentType.objects.get_for_model(Job)
        self.assertEqual(FileAttachment.objects.filter(model=ct, object_id=job.pk).count(), 1)
        self.assertNotIn("secret-cleanup", " ".join(captured.output) + " " + job.logs)
        self.assertTrue(any(record.phase == "attachment_cleanup" for record in captured.records))

    def test_bulk_delete_assets_get(self):
        url = reverse("assets:asset_bulk_delete")
        post_data = {
            "pk": [self.asset1.pk, self.asset2.pk],
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "generic/object_confirm_bulk_delete.html")

    def test_bulk_delete_assets_confirm(self):
        url = reverse("assets:asset_bulk_delete")
        post_data = {
            "pk": [self.asset1.pk, self.asset2.pk],
            "_confirm": "Confirm",
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)
        # Check that assets were deleted
        self.assertFalse(Asset.objects.filter(pk=self.asset1.pk).exists())
        self.assertFalse(Asset.objects.filter(pk=self.asset2.pk).exists())

    def test_bulk_edit_assets_get(self):
        url = reverse("assets:asset_bulk_edit")
        post_data = {
            "pk": [self.asset1.pk, self.asset2.pk],
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "generic/object_bulk_edit.html")

    def test_bulk_edit_assets_apply(self):
        status2 = StatusLabel.objects.create(name="Archived", slug="archived", type="archived")
        url = reverse("assets:asset_bulk_edit")
        post_data = {
            "pk": [self.asset1.pk, self.asset2.pk],
            "_selected_fields": ["status"],
            "status": status2.pk,
            "_apply": "Apply",
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)
        # Check that assets were updated
        self.asset1.refresh_from_db()
        self.asset2.refresh_from_db()
        self.assertEqual(self.asset1.status, status2)
        self.assertEqual(self.asset2.status, status2)

    def test_bulk_edit_assets_apply_tags(self):
        from extras.models import Tag

        tag1 = Tag.objects.create(name="Tag 1", slug="tag-1")
        tag2 = Tag.objects.create(name="Tag 2", slug="tag-2")

        # Add tag1 initially to asset1
        self.asset1.tags.add(tag1)

        url = reverse("assets:asset_bulk_edit")
        post_data = {
            "pk": [self.asset1.pk, self.asset2.pk],
            "_selected_fields": ["add_tags", "remove_tags"],
            "add_tags": [tag2.pk],
            "remove_tags": [tag1.pk],
            "_apply": "Apply",
        }

        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        # Verify tag changes
        self.asset1.refresh_from_db()
        self.asset2.refresh_from_db()

        # asset1 should have tag2 but not tag1
        self.assertIn(tag2, self.asset1.tags.all())
        self.assertNotIn(tag1, self.asset1.tags.all())

        # asset2 should have tag2 but not tag1
        self.assertIn(tag2, self.asset2.tags.all())
        self.assertNotIn(tag1, self.asset2.tags.all())


class LabelBulkScopeTests(TestCase):
    def setUp(self):
        self.group = TenantGroup.objects.create(name="Label Group", slug="label-group")
        self.tenant_a = Tenant.objects.create(name="Label Tenant A", slug="label-tenant-a", group=self.group)
        self.tenant_b = Tenant.objects.create(name="Label Tenant B", slug="label-tenant-b", group=self.group)
        self.outside_tenant = Tenant.objects.create(name="Outside Tenant", slug="outside-label-tenant")
        self.unreachable_tenant = Tenant.objects.create(name="Unreachable Tenant", slug="unreachable-label-tenant")
        self.user = User.objects.create_user(username="label-scope-user", password="pw")
        for tenant in (self.tenant_a, self.tenant_b, self.outside_tenant):
            role = Role.objects.create(
                tenant=tenant,
                name=f"Label viewer {tenant.pk}",
                permissions=["assets.view_asset", "core.view_job"],
            )
            grant(self.user, tenant, role)

        status = StatusLabel.objects.create(name="Available label", slug="available-label", type="deployable")
        self.asset_a = Asset.objects.create(name="Asset A", asset_tag="LABEL-A", status=status, tenant=self.tenant_a)
        self.asset_b = Asset.objects.create(name="Asset B", asset_tag="LABEL-B", status=status, tenant=self.tenant_b)
        self.unreachable_asset = Asset.objects.create(
            name="Unreachable asset",
            asset_tag="LABEL-X",
            status=status,
            tenant=self.unreachable_tenant,
        )
        self.outside_job = Job.objects.create(name="Outside group job", tenant=self.outside_tenant)
        self.template = LabelTemplate.objects.create(name="Scope label template")
        self.template.content_types.add(ContentType.objects.get_for_model(Asset))
        self.url = reverse("label_print", kwargs={"app_label": "assets", "model_name": "asset"})
        # Queue every selection so the job-scope contract is exercised with a few rows.
        sync_limit = patch("extras.label_views.LABEL_SYNC_LIMIT", 0)
        sync_limit.start()
        self.addCleanup(sync_limit.stop)

    def _login_with_scope(self, *, all_accessible=False, group=None):
        self.client.force_login(self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session.pop("active_all_accessible", None)
        if all_accessible:
            session["active_all_accessible"] = True
        elif group is not None:
            session["active_tenant_group_id"] = group.pk
        session.save()

    def _login_with_tenant(self, tenant):
        self.client.force_login(self.user)
        session = self.client.session
        session.pop("active_tenant_group_id", None)
        session.pop("active_all_accessible", None)
        session["active_tenant_id"] = tenant.pk
        session.save()

    def _post(self, pks):
        return self.client.post(
            self.url,
            {
                "pk": [str(pk) for pk in pks],
                "_print": "1",
                "template": self.template.pk,
                "layout": "roll",
            },
        )

    @patch("extras.label_views.async_task")
    def test_all_accessible_scope_prints_assets_from_multiple_tenants(self, mock_async):
        self._login_with_scope(all_accessible=True)

        response = self._post([self.asset_a.pk, self.asset_b.pk])

        self.assertEqual(response.status_code, 302)
        job = Job.objects.latest("created")
        self.assertEqual(job.tenant_id, self.tenant_a.pk)
        self.assertEqual(job.data["scope_tenant_ids"], [self.tenant_a.pk, self.tenant_b.pk])
        self.assertEqual(mock_async.call_args.args[6], self.user.pk)
        self.assertEqual(mock_async.call_args.args[7], self.tenant_a.pk)
        self.assertEqual(self.client.get(response.url).status_code, 200)

    @patch("extras.label_views.async_task")
    def test_group_scope_prints_assets_from_multiple_tenants(self, mock_async):
        self._login_with_scope(group=self.group)

        response = self._post([self.asset_a.pk, self.asset_b.pk])

        self.assertEqual(response.status_code, 302)
        job = Job.objects.latest("created")
        self.assertEqual(job.tenant_id, self.tenant_a.pk)
        self.assertEqual(job.data["scope_tenant_ids"], [self.tenant_a.pk, self.tenant_b.pk])
        self.assertEqual(mock_async.call_args.args[7], self.tenant_a.pk)
        self.assertEqual(self.client.get(response.url).status_code, 200)

    @patch("extras.tasks.labels.html_to_pdf_bytes", return_value=b"%PDF-scope-test")
    @patch("extras.services.labels.render_label_card", return_value="<div>safe</div>")
    @patch("extras.tasks.labels.render_object_card", return_value="data:image/png;base64,AAAA")
    def test_pdf_task_uses_persisted_multi_tenant_scope(self, _barcode, _render, _pdf):
        job = Job.objects.create(
            name="multi-tenant labels",
            tenant=self.tenant_a,
            status=Job.STATUS_PENDING,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )

        result = generate_label_pdf_batch_task(
            job.pk,
            [self.asset_a.pk, self.asset_b.pk],
            self.template.pk,
            "roll",
            self.user.pk,
            self.tenant_a.pk,
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.SUCCESS, "labels.pdf_completed"))
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertEqual(FileAttachment.objects.filter(object_id=job.pk).count(), 1)

    @patch("extras.tasks.labels.html_to_pdf_bytes", return_value=b"%PDF-scope-test")
    @patch("extras.services.labels.render_label_card", return_value="<div>safe</div>")
    @patch("extras.tasks.labels.render_object_card", return_value="data:image/png;base64,AAAA")
    def test_pdf_task_rejects_assets_outside_persisted_scope(self, _barcode, _render, _pdf):
        job = Job.objects.create(
            name="scoped labels",
            tenant=self.tenant_a,
            status=Job.STATUS_PENDING,
            data={"scope_tenant_ids": [self.tenant_a.pk]},
        )

        result = generate_label_pdf_batch_task(
            job.pk,
            [self.asset_a.pk, self.asset_b.pk],
            self.template.pk,
            "roll",
            self.user.pk,
            self.tenant_a.pk,
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.objects_not_accessible"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertEqual(FileAttachment.objects.filter(object_id=job.pk).count(), 0)

    @patch("extras.tasks.labels.html_to_pdf_bytes", return_value=b"%PDF-scope-test")
    @patch("extras.services.labels.render_label_card", return_value="<div>safe</div>")
    @patch("extras.tasks.labels.render_object_card", return_value="data:image/png;base64,AAAA")
    def test_pdf_task_rejects_asset_after_view_access_is_revoked(self, _barcode, _render, _pdf):
        Role.objects.filter(tenant=self.tenant_b).update(permissions=["core.view_job"])
        job = Job.objects.create(
            name="revoked labels",
            tenant=self.tenant_a,
            status=Job.STATUS_PENDING,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )

        result = generate_label_pdf_batch_task(
            job.pk,
            [self.asset_a.pk, self.asset_b.pk],
            self.template.pk,
            "roll",
            self.user.pk,
            self.tenant_a.pk,
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "labels.objects_not_accessible"))
        self.assertEqual(job.status, Job.STATUS_FAILED)

    @patch("extras.label_views.async_task")
    def test_bulk_print_labels_rejects_invalid_asset_ids_before_job_creation(self, mock_async):
        self._login_with_scope(all_accessible=True)
        before = Job.objects.count()

        for pks in (("not-an-id",), ("1", "1"), ("0",)):
            with self.subTest(pks=pks):
                response = self._post(pks)
                self.assertEqual(response.status_code, 400)

        self.assertEqual(Job.objects.count(), before)
        mock_async.assert_not_called()

    @patch("extras.label_views.async_task")
    def test_bulk_print_labels_rejects_empty_selection_before_job_creation(self, mock_async):
        self._login_with_scope(all_accessible=True)
        before = Job.objects.count()

        response = self.client.post(
            self.url,
            {"_print": "1", "template": self.template.pk, "layout": "roll"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Job.objects.count(), before)
        mock_async.assert_not_called()

    @patch("extras.label_views.async_task")
    def test_bulk_print_labels_rejects_invalid_template_before_job_creation(self, mock_async):
        self._login_with_scope(all_accessible=True)
        before = Job.objects.count()

        response = self.client.post(
            self.url,
            {"pk": [str(self.asset_a.pk)], "_print": "1", "template": "not-a-template", "layout": "roll"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Job.objects.count(), before)
        mock_async.assert_not_called()

    @patch("extras.label_views.async_task")
    def test_aggregate_scope_rejects_inaccessible_selection_before_job_creation(self, mock_async):
        self._login_with_scope(all_accessible=True)
        before = Job.objects.count()

        response = self._post([self.asset_a.pk, self.unreachable_asset.pk])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Job.objects.count(), before)
        mock_async.assert_not_called()

    def test_single_tenant_scope_hides_multi_tenant_job_anchored_to_that_tenant(self):
        job = Job.objects.create(
            name="multi-tenant labels",
            tenant=self.tenant_a,
            status=Job.STATUS_COMPLETED,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        self._login_with_tenant(self.tenant_a)

        response = self.client.get(reverse("job_detail", kwargs={"pk": job.pk}))

        self.assertEqual(response.status_code, 404)

    def test_all_accessible_scope_hides_multi_tenant_job_after_access_revoked(self):
        job = Job.objects.create(
            name="multi-tenant labels",
            tenant=self.tenant_a,
            status=Job.STATUS_COMPLETED,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        Membership.objects.filter(user=self.user, tenant=self.tenant_b).delete()
        self._login_with_scope(all_accessible=True)

        response = self.client.get(reverse("job_detail", kwargs={"pk": job.pk}))

        self.assertEqual(response.status_code, 404)

    def test_group_scope_hides_jobs_outside_selected_group(self):
        self._login_with_scope(group=self.group)

        response = self.client.get(reverse("job_detail", kwargs={"pk": self.outside_job.pk}))

        self.assertEqual(response.status_code, 404)


class BulkMutationTenantVisibilityTests(TestCase):
    """Issue #499: the bulk confirm surfaces name each object's tenant whenever
    the active scope does not pin a single tenant.

    The all-accessible scope refuses ambient change/delete permissions by
    design, so the member-facing aggregate case is the tenant-group scope and
    the global view case belongs to the superuser.
    """

    def setUp(self):
        self.group = TenantGroup.objects.create(name="Visibility Group", slug="visibility-group")
        self.tenant_a = Tenant.objects.create(name="Visibility Tenant A", slug="visibility-tenant-a", group=self.group)
        self.tenant_b = Tenant.objects.create(name="Visibility Tenant B", slug="visibility-tenant-b", group=self.group)
        self.superuser = User.objects.create_superuser(
            username="visibility-su",
            email="visibility-su@example.com",
            password="pw",
        )
        self.user = User.objects.create_user(username="visibility-scope-user", password="pw")
        for tenant in (self.tenant_a, self.tenant_b):
            role = Role.objects.create(
                tenant=tenant,
                name=f"Visibility editor {tenant.pk}",
                permissions=["assets.view_asset", "assets.change_asset", "assets.delete_asset"],
            )
            grant(self.user, tenant, role)
        status = StatusLabel.objects.create(name="Available visibility", slug="available-visibility", type="deployable")
        self.asset_a = Asset.objects.create(name="Visibility A", asset_tag="VIS-A", status=status, tenant=self.tenant_a)
        self.asset_b = Asset.objects.create(name="Visibility B", asset_tag="VIS-B", status=status, tenant=self.tenant_b)
        self.delete_url = reverse("assets:asset_bulk_delete")
        self.edit_url = reverse("assets:asset_bulk_edit")

    def _login(self, *, superuser=False, group=None, tenant=None):
        self.client.force_login(self.superuser if superuser else self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session.pop("active_all_accessible", None)
        if group is not None:
            session["active_tenant_group_id"] = group.pk
        elif tenant is not None:
            session["active_tenant_id"] = tenant.pk
        session.save()

    def test_bulk_delete_confirm_names_each_tenant_in_group_scope(self):
        self._login(group=self.group)

        response = self.client.post(self.delete_url, {"pk": [self.asset_a.pk, self.asset_b.pk]})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f"(Tenant: {self.tenant_a.name})", content)
        self.assertIn(f"(Tenant: {self.tenant_b.name})", content)

    def test_bulk_delete_confirm_names_each_tenant_in_global_view(self):
        self._login(superuser=True)

        response = self.client.post(self.delete_url, {"pk": [self.asset_a.pk, self.asset_b.pk]})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f"(Tenant: {self.tenant_a.name})", content)
        self.assertIn(f"(Tenant: {self.tenant_b.name})", content)

    def test_bulk_delete_confirm_hides_tenant_in_single_tenant_scope(self):
        self._login(tenant=self.tenant_a)

        response = self.client.post(self.delete_url, {"pk": [self.asset_a.pk]})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f"(Tenant: {self.tenant_a.name})", response.content.decode())

    def test_bulk_edit_names_each_tenant_in_group_scope(self):
        self._login(group=self.group)

        response = self.client.post(self.edit_url, {"pk": [self.asset_a.pk, self.asset_b.pk]})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f"(Tenant: {self.tenant_a.name})", content)
        self.assertIn(f"(Tenant: {self.tenant_b.name})", content)

    def test_bulk_edit_names_each_tenant_in_global_view(self):
        self._login(superuser=True)

        response = self.client.post(self.edit_url, {"pk": [self.asset_a.pk, self.asset_b.pk]})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(f"(Tenant: {self.tenant_a.name})", content)
        self.assertIn(f"(Tenant: {self.tenant_b.name})", content)

    def test_bulk_edit_hides_tenant_in_single_tenant_scope(self):
        self._login(tenant=self.tenant_a)

        response = self.client.post(self.edit_url, {"pk": [self.asset_a.pk]})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f"(Tenant: {self.tenant_a.name})", response.content.decode())
