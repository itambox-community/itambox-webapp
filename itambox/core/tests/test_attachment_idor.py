import io

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image

from assets.models import Asset, StatusLabel
from core.models import Job
from core.tests.mixins import grant
from extras.models import FileAttachment, ImageAttachment
from organization.models import Role, Tenant, TenantGroup

User = get_user_model()


class AttachmentCrossTenantIDORTests(TestCase):
    """WS7-3: the attachment proxy is the only barrier against downloading another tenant's
    file/image by guessing a pk (attachments have no tenant — they inherit it from the
    parent). This boundary previously had zero regression coverage."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="A", slug="a-att")
        self.tenant_b = Tenant.objects.create(name="B", slug="b-att")
        self.status = StatusLabel.objects.create(name="Dep", slug="dep-att", type="deployable")
        self.asset_a = Asset.objects.create(name="AA", asset_tag="ATT-A", status=self.status, tenant=self.tenant_a)
        self.asset_b = Asset.objects.create(name="BB", asset_tag="ATT-B", status=self.status, tenant=self.tenant_b)
        self.user = User.objects.create_user(username="attuser", password="pw")
        grant(
            self.user,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="R", permissions=["assets.view_asset"]),
        )
        ct = ContentType.objects.get_for_model(Asset)
        self.file_b = FileAttachment.objects.create(
            model=ct,
            object_id=self.asset_b.pk,
            file=SimpleUploadedFile("b.txt", b"secret"),
            name="b.txt",
        )
        self.file_a = FileAttachment.objects.create(
            model=ct,
            object_id=self.asset_a.pk,
            file=SimpleUploadedFile("a.txt", b"mine"),
            name="a.txt",
        )
        image_buffer = io.BytesIO()
        Image.new("RGB", (1, 1)).save(image_buffer, format="PNG")
        self.image_bytes = image_buffer.getvalue()
        self.image_b = ImageAttachment.objects.create(
            model=ct,
            object_id=self.asset_b.pk,
            image=SimpleUploadedFile("b.png", b"\x89PNG\r\n"),
            name="b.png",
        )
        self.image_a = ImageAttachment.objects.create(
            model=ct,
            object_id=self.asset_a.pk,
            image=SimpleUploadedFile("a.png", self.image_bytes),
            name="a.png",
        )

    def _login_a(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

    def test_cross_tenant_file_download_returns_404(self):
        self._login_a()
        url = reverse("file_attachment_download", kwargs={"pk": self.file_b.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_cross_tenant_image_serve_returns_404(self):
        self._login_a()
        url = reverse("image_attachment_serve", kwargs={"pk": self.image_b.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_own_tenant_file_download_ok_with_headers(self):
        self._login_a()
        url = reverse("file_attachment_download", kwargs={"pk": self.file_a.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "private, no-store")
        self.assertEqual(resp["Content-Type"], "text/plain")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("a.txt", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), b"mine")

    def test_own_tenant_image_serve_ok_with_headers(self):
        self._login_a()
        url = reverse("image_attachment_serve", kwargs={"pk": self.image_a.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "private, no-store")
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(b"".join(resp.streaming_content), self.image_bytes)

    def test_view_permission_allows_read_but_not_delete_or_upload(self):
        self._login_a()

        delete_url = reverse("file_attachment_delete", kwargs={"pk": self.file_a.pk})
        self.assertEqual(self.client.post(delete_url, {"return_url": "/"}).status_code, 404)
        self.assertTrue(FileAttachment.objects.filter(pk=self.file_a.pk).exists())

        upload_url = reverse(
            "file_attachment_upload",
            kwargs={"app_label": "assets", "model_name": "asset", "object_id": self.asset_a.pk},
        )
        response = self.client.post(upload_url, {"file": SimpleUploadedFile("blocked.txt", b"blocked")})
        self.assertEqual(response.status_code, 404)


class JobAttachmentAuthorizationTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Job Attachment A", slug="job-attachment-a")
        self.tenant_b = Tenant.objects.create(name="Job Attachment B", slug="job-attachment-b")
        self.user = User.objects.create_user(username="job_attachment_user", password="pw")
        self.role = Role.objects.create(
            tenant=self.tenant_a,
            name="Job Attachment Viewer",
            permissions=["core.view_job"],
        )
        grant(self.user, self.tenant_a, self.role)
        self.job_content_type = ContentType.objects.get_for_model(Job)

    def make_attachment(self, job, name):
        return FileAttachment.objects.create(
            model=self.job_content_type,
            object_id=job.pk,
            file=SimpleUploadedFile(name, name.encode()),
            name=name,
        )

    def make_pdf_attachment(self, job, name="job-labels.pdf"):
        return FileAttachment.objects.create(
            model=self.job_content_type,
            object_id=job.pk,
            file=SimpleUploadedFile(name, b"%PDF-job-attachment"),
            name=name,
            mime_type="application/pdf",
        )

    def make_image_attachment(self, job, name="job-preview.png"):
        return ImageAttachment.objects.create(
            model=self.job_content_type,
            object_id=job.pk,
            image=SimpleUploadedFile(name, b"\x89PNG\r\n", content_type="image/png"),
            name=name,
        )

    def assert_pdf_download(self, response, filename):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(filename, response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        body = b"".join(response.streaming_content)
        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertNotIn(b"<html", body.lower())

    def client_for_user_scope(self, user, *, tenant=None, group=None, all_accessible=False):
        client = Client()
        client.force_login(User.objects.get(pk=user.pk))
        session = client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session.pop("active_all_accessible", None)
        if tenant is not None:
            session["active_tenant_id"] = tenant.pk
        elif group is not None:
            session["active_tenant_group_id"] = group.pk
        elif all_accessible:
            session["active_all_accessible"] = True
        session.save()
        return client

    def client_for_scope(self, *, tenant=None, group=None, all_accessible=False):
        return self.client_for_user_scope(
            self.user,
            tenant=tenant,
            group=group,
            all_accessible=all_accessible,
        )

    def login_aggregate(self):
        self.client = self.client_for_scope(all_accessible=True)

    def login_concrete(self, tenant):
        self.client = self.client_for_scope(tenant=tenant)

    def put_tenants_in_group(self, group, *tenants):
        for tenant in tenants:
            tenant.group = group
            tenant.save(update_fields=["group"])

    def test_concrete_single_tenant_job_downloads_with_legacy_and_explicit_scope(self):
        for index, data in enumerate(({}, {"scope_tenant_ids": [self.tenant_a.pk]})):
            with self.subTest(data=data):
                job = Job.objects.create(
                    name=f"Concrete job {index}",
                    tenant=self.tenant_a,
                    data=data,
                )
                filename = f"concrete-{index}.pdf"
                attachment = self.make_pdf_attachment(job, filename)
                self.login_concrete(self.tenant_a)

                response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

                self.assert_pdf_download(response, filename)

    def test_tenant_group_mixed_job_downloads_when_complete_scope_fits(self):
        group = TenantGroup.objects.create(name="Job Group", slug="job-group")
        self.put_tenants_in_group(group, self.tenant_a, self.tenant_b)
        grant(
            self.user,
            self.tenant_b,
            Role.objects.create(tenant=self.tenant_b, name="Group Job Viewer", permissions=["core.view_job"]),
        )
        job = Job.objects.create(
            name="Group mixed job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "group-mixed.pdf")
        self.client = self.client_for_scope(group=group)

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assert_pdf_download(response, "group-mixed.pdf")

    def test_tenant_group_mixed_job_denies_when_group_excludes_non_anchor(self):
        group_a = TenantGroup.objects.create(name="Only A Group", slug="only-a-group")
        group_b = TenantGroup.objects.create(name="Only B Group", slug="only-b-group")
        self.put_tenants_in_group(group_a, self.tenant_a)
        self.put_tenants_in_group(group_b, self.tenant_b)
        grant(
            self.user,
            self.tenant_b,
            Role.objects.create(tenant=self.tenant_b, name="Excluded Group Job Viewer", permissions=["core.view_job"]),
        )
        job = Job.objects.create(
            name="Excluded group mixed job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "excluded-group.pdf")
        self.client = self.client_for_scope(group=group_a)

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_tenant_group_mixed_job_denies_after_non_anchor_access_revocation(self):
        group = TenantGroup.objects.create(name="Revocation Group", slug="revocation-group")
        self.put_tenants_in_group(group, self.tenant_a, self.tenant_b)
        membership_b = grant(
            self.user,
            self.tenant_b,
            Role.objects.create(
                tenant=self.tenant_b, name="Revocation Group Job Viewer", permissions=["core.view_job"]
            ),
        ).membership
        job = Job.objects.create(
            name="Group revoked mixed job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "group-revoked.pdf")
        url = reverse("file_attachment_download", kwargs={"pk": attachment.pk})

        initial_client = self.client_for_scope(group=group)
        self.assert_pdf_download(initial_client.get(url), "group-revoked.pdf")

        membership_b.is_active = False
        membership_b.save(update_fields=["is_active"])
        self.assertTrue(User.objects.get(pk=self.user.pk).has_perm("core.view_job", obj=job))
        revoked_client = self.client_for_scope(group=group)

        response = revoked_client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_user_with_only_anchor_access_cannot_download_mixed_job(self):
        job = Job.objects.create(
            name="Anchor-only mixed job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "anchor-only.pdf")
        self.client = self.client_for_scope(all_accessible=True)

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_user_outside_persisted_job_scope_cannot_download_attachment(self):
        outside = Tenant.objects.create(name="Outside Job Tenant", slug="outside-job-tenant")
        outside_user = User.objects.create_user(username="outside_job_user", password="pw")
        grant(
            outside_user,
            outside,
            Role.objects.create(tenant=outside, name="Outside Job Viewer", permissions=["core.view_job"]),
        )
        job = Job.objects.create(
            name="Outside persisted scope job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "outside-scope.pdf")
        outside_client = self.client_for_user_scope(outside_user, all_accessible=True)

        response = outside_client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_tenant_access_without_view_job_permission_cannot_download_attachment(self):
        denied_user = User.objects.create_user(username="job_attachment_no_view", password="pw")
        grant(
            denied_user,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="No Job View", permissions=[]),
        )
        job = Job.objects.create(name="Permission-gated job", tenant=self.tenant_a)
        attachment = self.make_pdf_attachment(job, "permission-gated.pdf")
        denied_client = self.client_for_user_scope(denied_user, tenant=self.tenant_a)

        response = denied_client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_non_superuser_cannot_download_tenantless_system_job(self):
        job = Job.objects.create(name="Tenantless system job", tenant=None)
        attachment = self.make_pdf_attachment(job, "system-job.pdf")
        self.login_concrete(self.tenant_a)

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_superuser_can_download_tenantless_system_job(self):
        superuser = User.objects.create_superuser(username="job_attachment_superuser", password="pw")
        job = Job.objects.create(name="Superuser system job", tenant=None)
        attachment = self.make_pdf_attachment(job, "superuser-system-job.pdf")
        superuser_client = self.client_for_user_scope(superuser, all_accessible=True)

        response = superuser_client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assert_pdf_download(response, "superuser-system-job.pdf")

    def test_job_image_attachment_uses_complete_job_scope(self):
        job = Job.objects.create(
            name="Mixed image job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_image_attachment(job)
        self.login_concrete(self.tenant_a)

        response = self.client.get(reverse("image_attachment_serve", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_global_parent_without_object_view_permission_returns_404(self):
        denied_user = User.objects.create_user(username="job_attachment_denied", password="pw")
        grant(
            denied_user,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="Job Attachment Denied", permissions=[]),
        )
        job = Job.objects.create(name="Global parent", tenant=None)
        attachment = self.make_attachment(job, "global-parent.txt")
        self.client.force_login(denied_user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_authorized_aggregate_scope_job_attachment_succeeds_with_headers(self):
        grant(
            self.user,
            self.tenant_b,
            Role.objects.create(tenant=self.tenant_b, name="Aggregate Job Viewer", permissions=["core.view_job"]),
        )
        job = Job.objects.create(
            name="Aggregate parent",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "aggregate-parent.pdf")
        self.login_aggregate()

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assert_pdf_download(response, "aggregate-parent.pdf")

    def test_inaccessible_aggregate_scope_job_attachment_returns_404(self):
        job = Job.objects.create(name="Foreign aggregate parent", tenant=self.tenant_b)
        attachment = self.make_attachment(job, "foreign-aggregate-parent.txt")
        self.login_aggregate()

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_mixed_job_attachment_denies_after_non_anchor_access_revocation(self):
        role_b = Role.objects.create(
            tenant=self.tenant_b,
            name="Job Attachment Revocation Viewer",
            permissions=["core.view_job"],
        )
        membership_b = grant(self.user, self.tenant_b, role_b).membership
        job = Job.objects.create(
            name="Revoked mixed job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job)
        url = reverse("file_attachment_download", kwargs={"pk": attachment.pk})

        initial_client = self.client_for_scope(all_accessible=True)
        self.assert_pdf_download(initial_client.get(url), "job-labels.pdf")

        membership_b.is_active = False
        membership_b.save(update_fields=["is_active"])
        self.assertTrue(User.objects.get(pk=self.user.pk).has_perm("core.view_job", obj=job))
        revoked_client = self.client_for_scope(all_accessible=True)

        response = revoked_client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_mixed_job_attachment_denies_in_concrete_anchor_scope(self):
        job = Job.objects.create(
            name="Concrete mismatch job",
            tenant=self.tenant_a,
            data={"scope_tenant_ids": [self.tenant_a.pk, self.tenant_b.pk]},
        )
        attachment = self.make_pdf_attachment(job, "concrete-mismatch.pdf")
        self.login_concrete(self.tenant_a)

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)

    def test_job_attachment_follows_active_scope_not_anchor_membership_alone(self):
        """A Job attachment is denied when its anchor is outside the active scope."""
        job = Job.objects.create(name="A-side parent", tenant=self.tenant_a)
        attachment = self.make_attachment(job, "a-side-parent.txt")
        grant(
            self.user,
            self.tenant_b,
            Role.objects.create(tenant=self.tenant_b, name="Job Attachment B Member", permissions=["core.view_job"]),
        )
        self.login_concrete(self.tenant_b)

        response = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 404)
