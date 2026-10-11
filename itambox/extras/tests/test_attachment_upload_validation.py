"""Request-level regression tests for attachment upload validation.

Both upload endpoints used to persist the uploaded bytes through
``objects.create()`` before any validator ran, so the size limits, the
extension blocklist and the image signature check declared on the model fields
were bypassable. These tests drive the real endpoints and assert that an
invalid upload is rejected before anything reaches storage, while a valid
upload keeps the existing authorization, redirect and download behaviour.
"""

import struct
import zlib
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, StatusLabel
from core.tests.mixins import grant
from extras.models import FileAttachment, ImageAttachment
from organization.models import Role, Tenant

User = get_user_model()

SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.domain)</script></svg>'
NOT_AN_IMAGE_BYTES = b"This is definitely not an image payload."


def png_bytes(width=1, height=1):
    """Build a real (validated) PNG without depending on an image library."""

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    scanlines = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b"")
    )


class AttachmentUploadValidationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Attach Upload", slug="attach-upload")
        self.status = StatusLabel.objects.create(name="Attach Ready", slug="attach-ready", type="deployable")
        self.asset = Asset.objects.create(
            name="Attachment Host",
            asset_tag="ATT-UP-1",
            status=self.status,
            tenant=self.tenant,
        )
        self.user = User.objects.create_user(username="attach_uploader", password="pw")
        grant(
            self.user,
            self.tenant,
            Role.objects.create(
                tenant=self.tenant,
                name="Attachment Uploader",
                permissions=["assets.change_asset", "assets.view_asset"],
            ),
        )
        self.content_type = ContentType.objects.get_for_model(Asset)

        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    def _upload_url(self, kind):
        return reverse(
            f"{kind}_attachment_upload",
            kwargs={"app_label": "assets", "model_name": "asset", "object_id": self.asset.pk},
        )

    def _stored_files(self):
        root = Path(settings.MEDIA_ROOT)
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())

    def test_file_upload_rejects_oversized_upload_and_persists_nothing(self):
        before = self._stored_files()
        oversized = SimpleUploadedFile("oversize.pdf", b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024))

        response = self.client.post(self._upload_url("file"), {"file": oversized}, follow=True)

        self.assertEqual(FileAttachment.objects.count(), 0)
        self.assertEqual(self._stored_files(), before)
        self.assertContains(response, "File size must not exceed 10 MB.")

    def test_file_upload_rejects_blocked_extension_and_persists_nothing(self):
        before = self._stored_files()

        response = self.client.post(
            self._upload_url("file"),
            {"file": SimpleUploadedFile("blocked.exe", b"MZ\x90\x00payload")},
            follow=True,
        )

        self.assertEqual(FileAttachment.objects.count(), 0)
        self.assertEqual(self._stored_files(), before)
        self.assertContains(response, "are not allowed for security reasons")

    def test_image_upload_rejects_svg_and_forged_content(self):
        before = self._stored_files()
        payloads = {
            "svg": SimpleUploadedFile("frame.svg", SVG_BYTES, content_type="image/svg+xml"),
            "forged": SimpleUploadedFile("frame.png", NOT_AN_IMAGE_BYTES, content_type="image/png"),
        }

        for label, payload in payloads.items():
            with self.subTest(label=label):
                response = self.client.post(self._upload_url("image"), {"image": payload}, follow=True)

                self.assertEqual(ImageAttachment.objects.count(), 0)
                self.assertEqual(self._stored_files(), before)
                self.assertEqual(response.status_code, 200)

    def test_valid_file_upload_persists_and_downloads_as_attachment(self):
        response = self.client.post(
            self._upload_url("file"),
            {"file": SimpleUploadedFile("notes.pdf", b"%PDF-1.4\nvalid payload")},
        )

        self.assertEqual(response.status_code, 302)
        attachment = FileAttachment.objects.get()
        self.assertEqual(attachment.name, "notes.pdf")
        self.assertEqual(attachment.mime_type, "application/pdf")

        download = self.client.get(reverse("file_attachment_download", kwargs={"pk": attachment.pk}))
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")

    def test_valid_image_upload_persists_and_serves_verified_raster_inline(self):
        response = self.client.post(
            self._upload_url("image"),
            {"image": SimpleUploadedFile("photo.png", png_bytes(), content_type="image/png")},
        )

        self.assertEqual(response.status_code, 302)
        attachment = ImageAttachment.objects.get()
        self.assertEqual(attachment.name, "photo.png")

        served = self.client.get(reverse("image_attachment_serve", kwargs={"pk": attachment.pk}))
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served["Content-Type"], "image/png")
        self.assertNotIn("attachment", served["Content-Disposition"])
        self.assertEqual(served["X-Content-Type-Options"], "nosniff")

    def test_image_serve_forces_download_for_unverified_stored_content(self):
        # Pre-existing rows written before upload validation existed.
        attachment = ImageAttachment.objects.create(
            model=self.content_type,
            object_id=self.asset.pk,
            image=SimpleUploadedFile("frame.svg", SVG_BYTES, content_type="image/svg+xml"),
            name="frame.svg",
        )

        response = self.client.get(reverse("image_attachment_serve", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertNotIn("image/svg+xml", response["Content-Type"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_image_serve_serves_verified_stored_raster_inline(self):
        attachment = ImageAttachment.objects.create(
            model=self.content_type,
            object_id=self.asset.pk,
            image=SimpleUploadedFile("legacy.png", png_bytes(), content_type="image/png"),
            name="legacy.png",
        )

        response = self.client.get(reverse("image_attachment_serve", kwargs={"pk": attachment.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertNotIn("attachment", response["Content-Disposition"])
