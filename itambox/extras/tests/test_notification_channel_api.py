from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from extras.models import NotificationChannel

User = get_user_model()

SLACK_URL = "https://hooks.slack.com/services/T0/B0/SECRETTOKEN"


class NotificationChannelConfigRedactionTests(APITestCase):
    """The Slack/Teams webhook_url in NotificationChannel.config is credential-like
    and must NOT be exposed on read via the REST API (it was, before this change).
    Mirrors WebhookEndpoint.secret being write-only."""

    def setUp(self):
        self.admin = User.objects.create_user(username="nc_admin", password="pw", is_staff=True, is_superuser=True)
        self.slack = NotificationChannel.objects.create(
            name="Slack Alerts",
            channel_type=NotificationChannel.TYPE_SLACK,
            config={"webhook_url": SLACK_URL, "username": "itambox-bot"},
        )
        self.email = NotificationChannel.objects.create(
            name="Email Alerts",
            channel_type=NotificationChannel.TYPE_EMAIL,
            config={"recipients": "ops@example.com"},
        )
        self.client.force_authenticate(self.admin)

    def _detail(self, pk):
        return reverse("api:extras_api:notificationchannel-detail", kwargs={"pk": pk})

    def _etag(self, obj):
        obj.refresh_from_db()
        return 'W/"%s"' % obj.updated_at.isoformat()

    def test_read_redacts_webhook_url(self):
        resp = self.client.get(self._detail(self.slack.pk))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # The real secret must not appear anywhere in the response.
        self.assertNotIn("SECRETTOKEN", str(resp.data))
        self.assertNotEqual(resp.data["config"].get("webhook_url"), SLACK_URL)
        self.assertTrue(resp.data["config"].get("webhook_url"))  # a placeholder is shown
        # Non-secret keys are still visible.
        self.assertEqual(resp.data["config"].get("username"), "itambox-bot")

    def test_read_keeps_nonsecret_config(self):
        resp = self.client.get(self._detail(self.email.pk))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["config"].get("recipients"), "ops@example.com")

    def test_list_does_not_leak_secret(self):
        resp = self.client.get(reverse("api:extras_api:notificationchannel-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn("SECRETTOKEN", str(resp.data))

    def test_create_stores_real_url(self):
        resp = self.client.post(
            reverse("api:extras_api:notificationchannel-list"),
            {
                "name": "New Slack",
                "channel_type": "slack",
                "config": {"webhook_url": "https://hooks.slack.com/services/NEW/REAL"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        ch = NotificationChannel.objects.get(pk=resp.data["id"])
        self.assertEqual(ch.config["webhook_url"], "https://hooks.slack.com/services/NEW/REAL")
        # ...but the create response is still redacted.
        self.assertNotIn("NEW/REAL", str(resp.data))

    def test_roundtrip_patch_preserves_url(self):
        # GET returns a redacted config; echoing it back must NOT overwrite the
        # stored real webhook_url with the placeholder.
        got = self.client.get(self._detail(self.slack.pk)).data["config"]
        resp = self.client.patch(
            self._detail(self.slack.pk),
            {"config": got},
            format="json",
            HTTP_IF_MATCH=self._etag(self.slack),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.slack.refresh_from_db()
        self.assertEqual(self.slack.config["webhook_url"], SLACK_URL)
        self.assertEqual(self.slack.config["username"], "itambox-bot")

    def test_patch_new_url_updates(self):
        new_url = "https://hooks.slack.com/services/CHANGED/URL"
        resp = self.client.patch(
            self._detail(self.slack.pk),
            {"config": {"webhook_url": new_url}},
            format="json",
            HTTP_IF_MATCH=self._etag(self.slack),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.slack.refresh_from_db()
        self.assertEqual(self.slack.config["webhook_url"], new_url)
