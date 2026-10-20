from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse

from core.tests.mixins import TenantTestMixin
from users.forms import UserPreferencesForm
from users.models import UserPreference, get_theme_preference, normalize_theme_preference

User = get_user_model()


class ThemePreferenceResolutionTests(TestCase):
    """The stored account value is the single source of truth."""

    def setUp(self):
        self.user = User.objects.create_user(username="theme-resolution-user", password="password123")

    def test_anonymous_visitors_follow_the_operating_system(self):
        self.assertEqual(get_theme_preference(None), UserPreference.THEME_AUTO)
        self.assertEqual(get_theme_preference(AnonymousUser()), UserPreference.THEME_AUTO)

    def test_missing_preference_row_defaults_to_automatic(self):
        self.assertFalse(UserPreference.objects.filter(user=self.user).exists())
        self.assertEqual(get_theme_preference(self.user), UserPreference.THEME_AUTO)

    def test_stored_light_and_dark_values_are_preserved(self):
        for value in (UserPreference.THEME_LIGHT, UserPreference.THEME_DARK, UserPreference.THEME_AUTO):
            with self.subTest(value=value):
                UserPreference.objects.update_or_create(user=self.user, defaults={"data": {"theme": {"theme": value}}})
                self.assertEqual(get_theme_preference(self.user), value)

    def test_unusable_stored_values_fall_back_to_automatic(self):
        stored_values = (
            {},
            {"theme": {}},
            {"theme": []},
            {"theme": {"theme": "purple"}},
            {"theme": {"theme": None}},
            {"theme": {"theme": ""}},
        )
        for data in stored_values:
            with self.subTest(data=data):
                UserPreference.objects.update_or_create(user=self.user, defaults={"data": data})
                self.assertEqual(get_theme_preference(self.user), UserPreference.THEME_AUTO)

    def test_normalize_accepts_only_the_three_choices(self):
        for value in (UserPreference.THEME_LIGHT, UserPreference.THEME_DARK, UserPreference.THEME_AUTO):
            with self.subTest(value=value):
                self.assertEqual(normalize_theme_preference(value), value)
        for value in (None, "", "Dark", "system"):
            with self.subTest(value=value):
                self.assertEqual(normalize_theme_preference(value), UserPreference.THEME_AUTO)


class ThemePreferenceRenderingTests(TenantTestMixin, TestCase):
    """The preference reaches the document element on the first render."""

    def setUp(self):
        self.setup_tenant_context()
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _document(self):
        response = self.client.get(reverse("users:user_preferences"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_each_stored_value_is_rendered_on_the_document(self):
        for value in (UserPreference.THEME_LIGHT, UserPreference.THEME_DARK, UserPreference.THEME_AUTO):
            with self.subTest(value=value):
                UserPreference.objects.update_or_create(
                    user=self.tenant_user,
                    defaults={"data": {"theme": {"theme": value}}},
                )
                self.assertIn('data-theme-preference="%s"' % value, self._document())

    def test_new_accounts_render_automatic(self):
        self.assertFalse(UserPreference.objects.filter(user=self.tenant_user).exists())
        self.assertIn('data-theme-preference="auto"', self._document())

    def test_preferences_page_offers_the_three_choices_and_explains_automatic(self):
        content = self._document()
        for value in ("light", "dark", "auto"):
            self.assertIn('<option value="%s"' % value, content)
        self.assertIn("Automatic follows the light or dark setting of your operating system", content)


class PublicLayoutThemeRenderingTests(TestCase):
    """The public layout follows the operating system unless a preference applies."""

    def setUp(self):
        self.user = User.objects.create_user(username="theme-public-user", password="password123")
        self.url = reverse("login")

    def test_anonymous_visitors_render_automatic(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-preference="auto"')

    def test_authenticated_visitors_render_their_stored_preference(self):
        UserPreference.objects.create(user=self.user, data={"theme": {"theme": UserPreference.THEME_DARK}})
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-preference="dark"')


class ThemePreferenceFormTests(TestCase):
    """The preferences form accepts exactly the three documented values."""

    def setUp(self):
        self.user = User.objects.create_user(username="theme-form-user", password="password123")
        self.per_page = settings.PAGINATE_COUNT_CHOICES[0][0]

    def _form(self, theme):
        return UserPreferencesForm(
            user=self.user,
            data={"pagination_per_page": self.per_page, "theme": theme},
        )

    def test_every_documented_value_is_accepted_and_stored(self):
        for value in (UserPreference.THEME_LIGHT, UserPreference.THEME_DARK, UserPreference.THEME_AUTO):
            with self.subTest(value=value):
                form = self._form(value)
                self.assertTrue(form.is_valid(), form.errors.as_json())
                form.save()
                self.assertEqual(
                    UserPreference.objects.get(user=self.user).data["theme"]["theme"],
                    value,
                )

    def test_an_undocumented_value_is_rejected(self):
        form = self._form("purple")
        self.assertFalse(form.is_valid())
        self.assertIn("theme", form.errors)
        self.assertFalse(UserPreference.objects.filter(user=self.user).exists())

    def test_an_empty_selection_stores_the_automatic_default(self):
        form = self._form("")
        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        self.assertEqual(
            UserPreference.objects.get(user=self.user).data["theme"]["theme"],
            UserPreference.THEME_AUTO,
        )

    def test_the_initial_value_is_the_stored_preference(self):
        UserPreference.objects.create(user=self.user, data={"theme": {"theme": UserPreference.THEME_DARK}})
        self.assertEqual(UserPreferencesForm(user=self.user).fields["theme"].initial, UserPreference.THEME_DARK)
