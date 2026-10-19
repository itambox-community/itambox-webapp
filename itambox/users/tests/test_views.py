from html.parser import HTMLParser

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from users.forms import UserPreferencesForm
from users.models import Token, UserPreference

User = get_user_model()


class UserViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass", is_staff=True, is_superuser=True)

    def test_profile_view_requires_login(self):
        url = reverse("users:user_profile")
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 200)

    def test_profile_view_authenticated(self):
        self.client.force_login(self.user)
        url = reverse("users:user_profile")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_password_view_authenticated(self):
        self.client.force_login(self.user)
        url = reverse("users:user_password")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_preferences_view_authenticated(self):
        self.client.force_login(self.user)
        url = reverse("users:user_preferences")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_api_tokens_view_authenticated(self):
        self.client.force_login(self.user)
        url = reverse("users:user_api_tokens")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_notifications_view_authenticated(self):
        self.client.force_login(self.user)
        url = reverse("users:user_notifications")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_notification_poll_htmx(self):
        self.client.force_login(self.user)
        url = reverse("users:notification_poll")
        headers = {"HTTP_HX_Request": "true"}
        response = self.client.get(url, **headers)
        self.assertEqual(response.status_code, 200)

    def test_notification_poll_non_htmx(self):
        self.client.force_login(self.user)
        url = reverse("users:notification_poll")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 204)

    def test_view_notification_user_owned_with_url(self):
        from core.models import Notification

        notif = Notification.objects.create(
            user=self.user, subject="Test User URL Notif", message="Test message", target_url="/assets/assets/"
        )
        self.client.force_login(self.user)
        url = reverse("users:view_notification", kwargs={"pk": notif.pk})
        response = self.client.get(url)
        self.assertRedirects(response, "/assets/assets/", fetch_redirect_response=False)
        notif.refresh_from_db()
        self.assertTrue(notif.is_read)

    def test_view_notification_user_owned_without_url(self):
        from core.models import Notification

        notif = Notification.objects.create(user=self.user, subject="Test User No URL Notif", message="Test message")
        self.client.force_login(self.user)
        url = reverse("users:view_notification", kwargs={"pk": notif.pk})
        response = self.client.get(url)
        self.assertRedirects(response, reverse("users:user_notifications"))
        notif.refresh_from_db()
        self.assertTrue(notif.is_read)

    def test_view_notification_broadcast_not_openable_by_pk(self):
        # WS5-2: a global (user=None) broadcast must NOT be openable by an arbitrary user via
        # ViewNotificationView — it previously redirected any authenticated user (any tenant)
        # to its target_url, leaking the object's URL/existence.
        from core.models import Notification

        notif = Notification.objects.create(
            user=None, subject="Test Broadcast Notif", message="Test message", target_url="/assets/assets/"
        )
        self.client.force_login(self.user)
        url = reverse("users:view_notification", kwargs={"pk": notif.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_view_notification_other_user(self):
        from core.models import Notification

        other_user = User.objects.create_user(username="otheruser", password="testpass")
        notif = Notification.objects.create(
            user=other_user, subject="Other User Notif", message="Test message", target_url="/assets/assets/"
        )
        self.client.force_login(self.user)
        url = reverse("users:view_notification", kwargs={"pk": notif.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_user_subscriptions_view(self):
        from django.contrib.contenttypes.models import ContentType

        from extras.models import ObjectWatch
        from organization.models import Tenant

        tenant = Tenant.objects.create(name="Adobe Inc.", slug="adobe-inc")
        ct = ContentType.objects.get_for_model(tenant)

        ObjectWatch.objects.create(user=self.user, model=ct, object_id=tenant.pk)

        other_user = User.objects.create_user(username="otheruser2", password="testpass")
        ObjectWatch.objects.create(user=other_user, model=ct, object_id=tenant.pk)

        self.client.force_login(self.user)
        url = reverse("users:user_subscriptions")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/watching.html")
        # Only this user's watches appear
        self.assertEqual(response.context["watched_count"], 1)

    def test_user_api_tokens_view(self):
        token1 = Token.objects.create(user=self.user, description="My CI Token", write_enabled=True)

        other_user = User.objects.create_user(username="otheruser3", password="testpass")
        token2 = Token.objects.create(user=other_user, description="Other User CI Token", write_enabled=False)

        self.client.force_login(self.user)
        url = reverse("users:user_api_tokens")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/api_tokens.html")

        tokens_in_context = list(response.context["tokens"])
        self.assertIn(token1, tokens_in_context)
        self.assertNotIn(token2, tokens_in_context)

    def test_generate_api_token(self):
        from datetime import date

        self.client.force_login(self.user)
        url = reverse("users:user_api_tokens")

        data = {
            "description": "Production Access Key",
            "write_enabled": "true",
            "expires": date.today().strftime("%Y-%m-%d"),
        }

        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)

        token = Token.objects.filter(user=self.user, description="Production Access Key").first()
        self.assertIsNotNone(token)
        self.assertTrue(token.write_enabled)
        self.assertIsNotNone(token.expires)
        # Hashed at rest: the stored token cannot reveal its plaintext.
        self.assertIsNone(token.key)
        self.assertEqual(len(token.digest), 64)

        # The plaintext is surfaced exactly once (and matches the stored digest).
        new_key = response.context.get("new_token_key")
        self.assertEqual(len(new_key), 40)
        self.assertEqual(Token.find_by_key(new_key).pk, token.pk)

        response2 = self.client.get(url)
        self.assertIsNone(response2.context.get("new_token_key"))

    def test_revoke_api_token_success(self):
        token = Token.objects.create(user=self.user, description="Ephemeral key")

        self.client.force_login(self.user)
        url = reverse("users:delete_api_token", kwargs={"pk": token.pk})

        response = self.client.post(url)
        self.assertRedirects(response, reverse("users:user_api_tokens"))

        self.assertFalse(Token.objects.filter(pk=token.pk).exists())

    def test_revoke_api_token_other_user_404(self):
        other_user = User.objects.create_user(username="otheruser4", password="testpass")
        token = Token.objects.create(user=other_user, description="Other user Ephemeral key")

        self.client.force_login(self.user)
        url = reverse("users:delete_api_token", kwargs={"pk": token.pk})

        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Token.objects.filter(pk=token.pk).exists())


class BookmarkAndNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bookmarkuser", password="testpass123", is_staff=True, is_superuser=True
        )
        from organization.models import Tenant

        self.tenant = Tenant.objects.create(name="Test Tenant", slug="test-tenant")

    def test_bookmark_toggle_view_add_and_remove(self):
        from django.contrib.contenttypes.models import ContentType

        from extras.models import Bookmark

        self.client.force_login(self.user)
        ct = ContentType.objects.get_for_model(self.tenant)
        url = reverse("users:bookmark_toggle", kwargs={"content_type_id": ct.pk, "object_id": self.tenant.pk})

        response = self.client.post(url)
        self.assertRedirects(response, self.tenant.get_absolute_url())
        self.assertTrue(Bookmark.objects.filter(user=self.user, model=ct, object_id=self.tenant.pk).exists())

        response = self.client.post(url)
        self.assertRedirects(response, self.tenant.get_absolute_url())
        self.assertFalse(Bookmark.objects.filter(user=self.user, model=ct, object_id=self.tenant.pk).exists())

    def test_bookmark_toggle_view_htmx_add_and_remove(self):
        from django.contrib.contenttypes.models import ContentType

        from extras.models import Bookmark

        self.client.force_login(self.user)
        ct = ContentType.objects.get_for_model(self.tenant)
        url = reverse("users:bookmark_toggle", kwargs={"content_type_id": ct.pk, "object_id": self.tenant.pk})
        headers = {"HTTP_HX_Request": "true"}

        response = self.client.post(url, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"btn-soft-warning", response.content)
        self.assertIn(b'hx-target="this"', response.content)
        self.assertIn(b"X-CSRFToken", response.content)
        self.assertTrue(Bookmark.objects.filter(user=self.user, model=ct, object_id=self.tenant.pk).exists())

        response = self.client.post(url, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"btn-ghost-secondary", response.content)
        self.assertIn(b'hx-target="this"', response.content)
        self.assertIn(b"X-CSRFToken", response.content)
        self.assertFalse(Bookmark.objects.filter(user=self.user, model=ct, object_id=self.tenant.pk).exists())

    def test_bookmark_toggle_view_htmx_delete_from_list_context(self):
        """?context=list returns empty (row-remove) regardless of referer."""
        from django.contrib.contenttypes.models import ContentType

        from extras.models import Bookmark

        ct = ContentType.objects.get_for_model(self.tenant)
        Bookmark.objects.create(user=self.user, model=ct, object_id=self.tenant.pk)

        self.client.force_login(self.user)
        url = (
            reverse("users:bookmark_toggle", kwargs={"content_type_id": ct.pk, "object_id": self.tenant.pk})
            + "?context=list"
        )
        response = self.client.post(url, HTTP_HX_Request="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertFalse(Bookmark.objects.filter(user=self.user, model=ct, object_id=self.tenant.pk).exists())

    def test_bookmark_toggle_view_htmx_detail_context_returns_button(self):
        """No ?context=list → returns updated button HTML (detail-page swap)."""
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(self.tenant)
        self.client.force_login(self.user)
        url = reverse("users:bookmark_toggle", kwargs={"content_type_id": ct.pk, "object_id": self.tenant.pk})
        response = self.client.post(url, HTTP_HX_Request="true")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'hx-target="this"', response.content)
        self.assertGreater(len(response.content), 10)

    def test_bookmark_toggle_non_bookmarkable_model_returns_404(self):
        """Models without BookmarkableMixin are rejected."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(Permission)
        self.client.force_login(self.user)
        url = reverse("users:bookmark_toggle", kwargs={"content_type_id": ct.pk, "object_id": 1})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_watch_toggle_view_htmx_list_context_returns_empty(self):
        """?context=list unwatch → empty response + correct message."""
        from django.contrib.contenttypes.models import ContentType

        from extras.models import ObjectWatch

        ct = ContentType.objects.get_for_model(self.tenant)
        ObjectWatch.objects.create(user=self.user, model=ct, object_id=self.tenant.pk)

        self.client.force_login(self.user)
        url = (
            reverse("users:watch_toggle", kwargs={"content_type_id": ct.pk, "object_id": self.tenant.pk})
            + "?context=list"
        )
        response = self.client.post(url, HTTP_HX_Request="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertFalse(ObjectWatch.objects.filter(user=self.user, model=ct, object_id=self.tenant.pk).exists())

    def test_watch_toggle_detail_context_message_matches_action(self):
        """Watch create → 'Now watching'; watch delete → 'Unwatched'."""
        import json

        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_model(self.tenant)
        self.client.force_login(self.user)
        url = reverse("users:watch_toggle", kwargs={"content_type_id": ct.pk, "object_id": self.tenant.pk})

        response = self.client.post(url, HTTP_HX_Request="true")
        trigger = json.loads(response["HX-Trigger"])
        self.assertIn("watching", trigger["showMessage"]["message"].lower())

        response = self.client.post(url, HTTP_HX_Request="true")
        trigger = json.loads(response["HX-Trigger"])
        self.assertIn("unwatched", trigger["showMessage"]["message"].lower())

    def test_watch_change_notification_triggers(self):
        """ObjectWatch creates a notification on watched-object update and delete."""
        from django.contrib.contenttypes.models import ContentType

        from core.models import Notification
        from extras.models import ObjectWatch

        ct = ContentType.objects.get_for_model(self.tenant)
        ObjectWatch.objects.create(user=self.user, model=ct, object_id=self.tenant.pk)

        Notification.objects.filter(user=self.user).delete()

        self.tenant.comments = "Updated tenant comment"
        # Watcher notification is deferred to transaction.on_commit, which never fires
        # under a plain TestCase's wrapping transaction — capture and run the callbacks.
        with self.captureOnCommitCallbacks(execute=True):
            self.tenant.save()

        notifications = Notification.objects.filter(user=self.user)
        self.assertEqual(notifications.count(), 1)
        notif = notifications.first()
        self.assertIn("updated", notif.subject.lower())
        self.assertIn("Test Tenant", notif.message)
        self.assertEqual(notif.target_url, self.tenant.get_absolute_url())

        notifications.delete()

        with self.captureOnCommitCallbacks(execute=True):
            self.tenant.delete()

        notifications = Notification.objects.filter(user=self.user)
        self.assertEqual(notifications.count(), 1)
        notif_del = notifications.first()
        self.assertIn("deleted", notif_del.subject.lower())
        self.assertIn("Test Tenant", notif_del.message)


class _FormInspector(HTMLParser):
    """Collects every rendered <form>: its attributes and the named fields inside it."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self._open = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            record = {"attrs": dict(attrs), "fields": set()}
            self.forms.append(record)
            self._open.append(record)
        elif self._open and tag in ("input", "select", "textarea"):
            name = dict(attrs).get("name")
            if name:
                self._open[-1]["fields"].add(name)

    def handle_endtag(self, tag):
        if tag == "form" and self._open:
            self._open.pop()


class UserPreferencesLanguageTests(TestCase):
    """#909: a language switch must reload the whole document, never swap the content pane."""

    def setUp(self):
        self.user = User.objects.create_user(username="languageuser", password="testpass")
        self.client.force_login(self.user)
        self.url = reverse("users:user_preferences")
        # Pin the active language via the cookie so the change/no-change comparison does not
        # depend on the test client's Accept-Language header.
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

    def _payload(self, language):
        form = UserPreferencesForm(user=self.user)
        return {
            "pagination_per_page": form.fields["pagination_per_page"].choices[0][0],
            "theme": "light",
            "language": language,
            "default_workspace": "",
        }

    def test_htmx_language_change_forces_full_navigation(self):
        response = self.client.post(self.url, self._payload("de"), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], self.url)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")
        self.assertEqual(UserPreference.objects.get(user=self.user).data["language"], "de")

    def test_plain_language_change_redirects_with_language_cookie(self):
        response = self.client.post(self.url, self._payload("de"))

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertNotIn("HX-Redirect", response)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")
        self.assertEqual(UserPreference.objects.get(user=self.user).data["language"], "de")

    def test_htmx_save_without_language_change_keeps_redirect(self):
        response = self.client.post(self.url, self._payload("en"), HTTP_HX_REQUEST="true")

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertNotIn("HX-Redirect", response)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")

    def test_plain_save_without_language_change_keeps_redirect(self):
        response = self.client.post(self.url, self._payload("en"))

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertNotIn("HX-Redirect", response)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")

    def test_preferences_form_opts_out_of_boosting(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        inspector = _FormInspector()
        inspector.feed(response.content.decode())
        language_forms = [form for form in inspector.forms if "language" in form["fields"]]
        self.assertEqual(len(language_forms), 1, inspector.forms)
        self.assertEqual(language_forms[0]["attrs"].get("hx-boost"), "false")
