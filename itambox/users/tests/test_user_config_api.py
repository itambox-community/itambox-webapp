from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from users.models import UserPreference

User = get_user_model()


class UserConfigAPITests(APITestCase):
    def setUp(self):
        self.superuser = User.objects.create_user(
            username="user-config-superuser",
            email="user-config@example.com",
            password="password123",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.superuser)
        self.url = reverse("api:users_api:user-config")

    def _create_preference(self, data):
        return UserPreference.objects.create(user=self.superuser, data=data)

    def test_put_rejects_unknown_only_payload_and_does_not_create_preference(self):
        response = self.client.put(self.url, data={"zeta": 1, "alpha": 2}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.data), ["alpha", "zeta"])
        self.assertEqual(response.data["alpha"], ["Unknown field."])
        self.assertEqual(response.data["zeta"], ["Unknown field."])
        self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_put_rejects_mixed_known_and_unknown_payload_without_applying_it(self):
        original = {"theme": {"theme": "dark"}, "language": "en"}
        self._create_preference(original)

        response = self.client.put(
            self.url,
            data={"theme": {"theme": "light"}, "unknown": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["unknown"], ["Unknown field."])
        preference = UserPreference.objects.get(user=self.superuser)
        self.assertEqual(preference.data, original)

    def test_patch_rejects_unknown_only_payload_without_applying_it(self):
        original = {"pagination": {"per_page": 25}}
        self._create_preference(original)

        response = self.client.patch(self.url, data={"unknown": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["unknown"], ["Unknown field."])
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, original)

    def test_patch_rejects_mixed_known_and_unknown_payload_without_applying_it(self):
        original = {"language": "en", "pagination": {"per_page": 25}}
        self._create_preference(original)

        response = self.client.patch(
            self.url,
            data={"language": "de", "unknown": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["unknown"], ["Unknown field."])
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, original)

    def test_put_rejects_empty_payload_without_creating_preference(self):
        response = self.client.put(self.url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["non_field_errors"], ["At least one writable field is required."])
        self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_patch_rejects_empty_payload_without_creating_preference(self):
        response = self.client.patch(self.url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["non_field_errors"], ["At least one writable field is required."])
        self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_put_replaces_full_configuration_and_get_reads_it_back(self):
        self._create_preference({"old": "value"})
        payload = {
            "tables": {"assets": {"AssetTable": {"columns": ["name", "asset_tag"]}}},
            "theme": {"theme": "auto"},
            "pagination": {"per_page": 50},
            "language": "de",
        }

        response = self.client.put(self.url, data=payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"data": payload})
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, payload)
        self.assertEqual(self.client.get(self.url).data, {"data": payload})

    def test_put_with_partial_key_set_drops_previously_stored_keys(self):
        self._create_preference(
            {
                "theme": {"theme": "dark"},
                "pagination": {"per_page": 25},
                "language": "en",
                "tables": {"assets": {"AssetTable": {"columns": ["old"]}}},
            }
        )
        payload = {"theme": {"theme": "light"}}

        response = self.client.put(self.url, data=payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"data": payload})
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, payload)

    def test_get_returns_empty_default_configuration(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"data": {}})

    def test_patch_tables_deep_merges_apps_and_replaces_mentioned_table(self):
        self._create_preference(
            {
                "tables": {
                    "assets": {
                        "AssetTable": {"columns": ["old"], "ordering": "old"},
                        "OtherTable": {"columns": ["keep"]},
                    },
                    "inventory": {"StockTable": {"columns": ["stock"]}},
                },
                "language": "en",
            }
        )
        payload = {"tables": {"assets": {"AssetTable": {"columns": ["new"]}}}}
        expected = {
            "tables": {
                "assets": {
                    "AssetTable": {"columns": ["new"]},
                    "OtherTable": {"columns": ["keep"]},
                },
                "inventory": {"StockTable": {"columns": ["stock"]}},
            },
            "language": "en",
        }

        response = self.client.patch(self.url, data=payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"data": expected})
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, expected)

    def test_patch_theme_pagination_and_language_replace_values(self):
        self._create_preference(
            {
                "theme": {"theme": "dark"},
                "pagination": {"per_page": 25},
                "language": "en",
            }
        )

        for field, value in (
            ("theme", {"theme": "light"}),
            ("pagination", {"per_page": 50}),
            ("language", "de"),
        ):
            with self.subTest(field=field):
                response = self.client.patch(self.url, data={field: value}, format="json")
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["data"][field], value)

        self.assertEqual(
            UserPreference.objects.get(user=self.superuser).data,
            {"theme": {"theme": "light"}, "pagination": {"per_page": 50}, "language": "de"},
        )

    def test_patch_legacy_frontend_table_payload_is_persisted_exactly(self):
        payload = {"tables": {"assets": {"AssetTable": {"columns": ["a", "b"]}}}}

        response = self.client.patch(self.url, data=payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"data": payload})
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, payload)

    def test_patch_accepts_every_documented_theme_value(self):
        for value in ("light", "dark", "auto"):
            with self.subTest(value=value):
                response = self.client.patch(self.url, data={"theme": {"theme": value}}, format="json")
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["data"]["theme"], {"theme": value})
                self.assertEqual(
                    UserPreference.objects.get(user=self.superuser).data["theme"]["theme"],
                    value,
                )

    def test_patch_rejects_an_undocumented_theme_value_without_persisting(self):
        original = {"theme": {"theme": "dark"}}
        self._create_preference(original)

        response = self.client.patch(self.url, data={"theme": {"theme": "purple"}}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("theme", response.data)
        self.assertEqual(UserPreference.objects.get(user=self.superuser).data, original)

    def test_put_rejects_an_undocumented_theme_value_without_persisting(self):
        response = self.client.put(self.url, data={"theme": {"theme": "system"}}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("theme", response.data)
        self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_type_validation_rejects_invalid_configuration_shapes(self):
        cases = (
            ({"tables": []}, "tables"),
            ({"tables": {"assets": []}}, "tables"),
            ({"tables": {"assets": {"AssetTable": []}}}, "tables"),
            ({"theme": []}, "theme"),
            ({"pagination": []}, "pagination"),
            ({"language": 42}, "language"),
        )

        for payload, field in cases:
            with self.subTest(payload=payload):
                response = self.client.put(self.url, data=payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)
                self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_combined_invalid_type_and_unknown_field_is_rejected_without_persisting(self):
        response = self.client.put(self.url, data={"language": 42, "unknown": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_blank_language_is_rejected(self):
        response = self.client.patch(self.url, data={"language": ""}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("language", response.data)
        self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_non_object_payloads_are_rejected_with_400(self):
        for payload in ([], ["tables"], "config", 42):
            for method in ("put", "patch"):
                with self.subTest(method=method, payload=payload):
                    response = getattr(self.client, method)(self.url, data=payload, format="json")
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertFalse(UserPreference.objects.filter(user=self.superuser).exists())

    def test_unauthenticated_request_is_rejected(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
