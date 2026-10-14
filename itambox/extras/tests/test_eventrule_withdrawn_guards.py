import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from core.tests.mixins import TenantTestMixin, grant
from extras.forms import EventRuleForm
from extras.models import EventRule
from organization.models import Role, Tenant

User = get_user_model()


class EventRuleFormWithdrawnGuardTests(TestCase):
    def setUp(self):
        self.model = ContentType.objects.get_for_model(EventRule)
        self.conditions = {"field": "model_name", "op": "eq", "value": "tenant-a-condition-secret"}

    def form_data(self, **overrides):
        data = {
            "name": "Rule",
            "model": self.model.pk,
            "events": ["create"],
            "action_type": EventRule.ACTION_NOTIFICATION,
            "conditions": "",
            "action_config": "{}",
            "enabled": True,
        }
        data.update(overrides)
        return data

    def test_create_with_conditions_is_invalid(self):
        form = EventRuleForm(data=self.form_data(conditions=json.dumps(self.conditions)))

        self.assertFalse(form.is_valid())
        self.assertIn("withdrawn", str(form.errors["conditions"]))

    def test_create_without_conditions_is_valid(self):
        form = EventRuleForm(data=self.form_data())

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["conditions"], {})

    def test_update_with_unchanged_conditions_is_valid_and_preserved(self):
        rule = EventRule.objects.create(
            name="Rule",
            model=self.model,
            events=["create"],
            conditions=self.conditions,
            action_type=EventRule.ACTION_NOTIFICATION,
        )
        form = EventRuleForm(
            data=self.form_data(conditions=json.dumps(self.conditions)),
            instance=rule,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["conditions"], self.conditions)

    def test_update_without_conditions_field_is_valid_and_preserved(self):
        rule = EventRule.objects.create(
            name="Rule",
            model=self.model,
            events=["create"],
            conditions=self.conditions,
            action_type=EventRule.ACTION_NOTIFICATION,
        )
        data = self.form_data()
        data.pop("conditions")
        form = EventRuleForm(data=data, instance=rule)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["conditions"], self.conditions)

    def test_update_with_changed_conditions_is_invalid(self):
        rule = EventRule.objects.create(
            name="Rule",
            model=self.model,
            events=["create"],
            conditions=self.conditions,
            action_type=EventRule.ACTION_NOTIFICATION,
        )
        changed = {**self.conditions, "value": "changed"}
        form = EventRuleForm(data=self.form_data(conditions=json.dumps(changed)), instance=rule)

        self.assertFalse(form.is_valid())
        self.assertIn("withdrawn", str(form.errors["conditions"]))

    def test_update_with_cleared_conditions_is_invalid(self):
        rule = EventRule.objects.create(
            name="Rule",
            model=self.model,
            events=["create"],
            conditions=self.conditions,
            action_type=EventRule.ACTION_NOTIFICATION,
        )
        form = EventRuleForm(data=self.form_data(conditions=""), instance=rule)

        self.assertFalse(form.is_valid())
        self.assertIn("withdrawn", str(form.errors["conditions"]))

    def test_post_with_conditions_on_existing_empty_rule_is_invalid(self):
        rule = EventRule.objects.create(
            name="Rule",
            model=self.model,
            events=["create"],
            conditions={},
            action_type=EventRule.ACTION_NOTIFICATION,
        )
        form = EventRuleForm(data=self.form_data(conditions=json.dumps(self.conditions)), instance=rule)

        self.assertFalse(form.is_valid())
        self.assertIn("withdrawn", str(form.errors["conditions"]))


class EventRuleWithdrawnUiStatusTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.model = ContentType.objects.get_for_model(EventRule)
        self.rule_with_conditions = EventRule.objects.create(
            name="Withdrawn rule",
            model=self.model,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            conditions={"rules": [{"field": "model_name", "op": "eq", "value": "manufacturer"}]},
            enabled=True,
        )
        self.rule_without_conditions = EventRule.objects.create(
            name="Plain rule",
            model=self.model,
            events=["create"],
            action_type=EventRule.ACTION_NOTIFICATION,
            conditions={},
            enabled=True,
        )
        self.list_url = reverse("extras:eventrule_list")

    def test_list_table_shows_withdrawn_badge_and_empty_marker(self):
        admin = User.objects.create_superuser(username="admin", password="password")
        self.client.force_login(admin)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Withdrawn rule", content)
        self.assertIn("Withdrawn", content)
        self.assertIn("Plain rule", content)
        self.assertNotIn("–", content)
        self.assertIn(">-</td>", content)
        # The authored condition JSON must never leak into the list table.
        self.assertNotIn('"field": "model_name"', content)


class EventRuleSerializerWithdrawnGuardTests(TenantTestMixin, APITestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.user_b = User.objects.create_user(username="tenant-b-user", password="password")
        role_b = Role.objects.create(
            tenant=self.tenant_b,
            name="Tenant B Event Rule Role",
            permissions=[
                "extras.view_eventrule",
                "extras.add_eventrule",
                "extras.change_eventrule",
            ],
        )
        self.membership_b = grant(self.user_b, self.tenant_b, role_b).membership
        self.model = ContentType.objects.get_for_model(EventRule)
        self.conditions = {"field": "model_name", "op": "eq", "value": "tenant-a-condition-secret"}
        self.rule = EventRule.all_objects.create(
            name="Tenant A Rule",
            model=self.model,
            events=["create"],
            conditions=self.conditions,
            action_type=EventRule.ACTION_NOTIFICATION,
            tenant=self.tenant_a,
        )
        self.list_url = reverse("api:extras_api:eventrule-list")
        self.detail_url = reverse("api:extras_api:eventrule-detail", kwargs={"pk": self.rule.pk})

    def api_payload(self, conditions):
        return {
            "name": "API Rule",
            "model": f"{self.model.app_label}.{self.model.model}",
            "events": ["create"],
            "action_type": EventRule.ACTION_NOTIFICATION,
            "conditions": conditions,
            "action_config": {},
            "enabled": True,
        }

    def _etag(self, obj):
        # Mutating API requests require an If-Match precondition (optimistic
        # concurrency guard); the client builds the token from updated_at.
        return 'W/"{0}"'.format(obj.updated_at.isoformat())

    def test_post_create_with_conditions_returns_400(self):
        self.client.force_authenticate(user=User.objects.create_superuser(username="admin", password="password"))

        response = self.client.post(self.list_url, self.api_payload(self.conditions), format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("withdrawn", str(response.data))

    def test_patch_changed_conditions_returns_400(self):
        self.client.force_authenticate(user=User.objects.create_superuser(username="admin", password="password"))
        changed = {**self.conditions, "value": "changed"}

        response = self.client.patch(
            self.detail_url,
            {"conditions": changed},
            HTTP_IF_MATCH=self._etag(self.rule),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("withdrawn", str(response.data))

    def test_patch_unchanged_conditions_returns_200(self):
        self.client.force_authenticate(user=User.objects.create_superuser(username="admin", password="password"))

        response = self.client.patch(
            self.detail_url,
            {"conditions": self.conditions},
            HTTP_IF_MATCH=self._etag(self.rule),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["conditions"], self.conditions)

    def test_get_exposes_conditions_withdrawn(self):
        self.client.force_authenticate(user=User.objects.create_superuser(username="admin", password="password"))

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["conditions_withdrawn"])

    def test_tenant_isolation_hides_rule_and_conditions(self):
        self.client_login_to_tenant(self.user_b, self.tenant_b)

        list_response = self.client.get(self.list_url)
        detail_response = self.client.get(self.detail_url)
        rows = list_response.data.get("results", []) if isinstance(list_response.data, dict) else list_response.data

        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn(self.rule.pk, [row["id"] for row in rows])
        self.assertNotIn("tenant-a-condition-secret", list_response.content.decode())
