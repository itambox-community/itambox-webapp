from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset
from core.tests.mixins import TenantTestMixin
from extras.models import (
    AlertRule,
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldset,
    CustomFieldsetField,
    Dashboard,
    EventRule,
    NotificationChannel,
    Tag,
    WebhookEndpoint,
)

User = get_user_model()


class TagModelTests(TestCase):
    def test_tag_creation(self):
        tag = Tag.objects.create(name="Production", slug="production", color="00ff00")
        self.assertEqual(str(tag), "Production")
        self.assertEqual(tag.color, "00ff00")
        self.assertEqual(tag.description, "")

    def test_tag_absolute_url(self):
        tag = Tag.objects.create(name="Staging", slug="staging")
        url = tag.get_absolute_url()
        self.assertIn(str(tag.pk), url)

    def test_tag_name_unique(self):
        Tag.objects.create(name="Critical", slug="critical")
        with self.assertRaises(IntegrityError):
            Tag.objects.create(name="Critical", slug="critical-dup")

    def test_tag_slug_unique(self):
        Tag.objects.create(name="Tag A", slug="tag-a")
        with self.assertRaises(IntegrityError):
            Tag.objects.create(name="Tag B", slug="tag-a")

    def test_tag_ordering(self):
        Tag.objects.create(name="B", slug="b")
        Tag.objects.create(name="A", slug="a")
        tags = list(Tag.objects.all())
        self.assertEqual(tags[0].name, "A")

    def test_tag_with_description(self):
        tag = Tag.objects.create(name="DevOps", slug="devops", description="DevOps team assets", color="ff0000")
        self.assertEqual(tag.description, "DevOps team assets")


class CustomFieldModelTests(TestCase):
    def test_custom_field_creation(self):
        cf = CustomField.objects.create(
            name="cost_center",
            label="Cost Center",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        self.assertEqual(str(cf), "Cost Center (Text)")
        self.assertFalse(cf.required)

    def test_custom_field_absolute_url(self):
        cf = CustomField.objects.create(
            name="dept",
            label="Department",
            field_type="text",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        url = cf.get_absolute_url()
        self.assertIn(str(cf.pk), url)

    def test_custom_field_types(self):
        for ft, ft_label in CustomField.FIELD_TYPE_CHOICES:
            kwargs = {"decimal_scale": 2} if ft == CustomField.FIELD_TYPE_DECIMAL else {}
            if ft == CustomField.FIELD_TYPE_SINGLE_SELECT:
                kwargs.update(
                    choice_set=CustomFieldChoiceSet.objects.create(
                        namespace="test",
                        slug=f"{ft}-set",
                        label=f"{ft_label} set",
                    ),
                    max_values=1,
                )
            elif ft == CustomField.FIELD_TYPE_MULTI_SELECT:
                kwargs["choice_set"] = CustomFieldChoiceSet.objects.create(
                    namespace="test",
                    slug=f"{ft}-set",
                    label=f"{ft_label} set",
                )
                kwargs["max_values"] = 2
            cf = CustomField.objects.create(
                name=f"test_{ft}",
                label=f"Test {ft_label}",
                field_type=ft,
                activation=CustomField.ACTIVATION_GLOBAL,
                **kwargs,
            )
            self.assertEqual(cf.field_type, ft)

    def test_custom_field_required(self):
        cf = CustomField.objects.create(
            name="mandatory_field",
            label="Mandatory",
            field_type="text",
            activation=CustomField.ACTIVATION_GLOBAL,
            required=True,
        )
        self.assertTrue(cf.required)

    def test_custom_field_choices_for_select(self):
        choice_set = CustomFieldChoiceSet.objects.create(namespace="local", slug="environment", label="Environment")
        for position, (key, label) in enumerate(
            (("production", "Production"), ("staging", "Staging"), ("development", "Development")), start=1
        ):
            CustomFieldChoice.objects.create(
                choice_set=choice_set,
                key=key,
                label=label,
                position=position * 10,
            )
        cf = CustomField.objects.create(
            name="env",
            label="Environment",
            field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
            activation=CustomField.ACTIVATION_GLOBAL,
            choice_set=choice_set,
            max_values=1,
        )
        self.assertEqual(
            list(cf.choice_set.choices.values_list("key", flat=True)), ["production", "staging", "development"]
        )

    def test_custom_field_name_is_slug(self):
        cf = CustomField.objects.create(
            name="my_custom_field",
            label="My Custom Field",
            field_type="text",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        self.assertEqual(cf.name, "my_custom_field")


class CustomFieldsetModelTests(TestCase):
    def test_custom_fieldset_creation(self):
        cf1 = CustomField.objects.create(
            name="field_a",
            label="Field A",
            field_type="text",
            activation=CustomField.ACTIVATION_COMPOSED,
        )
        cf2 = CustomField.objects.create(
            name="field_b",
            label="Field B",
            field_type="decimal",
            activation=CustomField.ACTIVATION_COMPOSED,
            decimal_scale=2,
        )
        cfs = CustomFieldset.objects.create(
            namespace="local",
            slug="asset-details",
            label="Asset Details",
        )
        CustomFieldsetField.objects.create(fieldset=cfs, custom_field=cf1, position=10)
        CustomFieldsetField.objects.create(fieldset=cfs, custom_field=cf2, position=20)
        self.assertEqual(str(cfs), "Asset Details")
        self.assertEqual(cfs.fields.count(), 2)

    def test_custom_fieldset_absolute_url(self):
        cfs = CustomFieldset.objects.create(namespace="local", slug="server-config", label="Server Config")
        url = cfs.get_absolute_url()
        self.assertIn(str(cfs.pk), url)

    def test_custom_fieldset_identity_unique(self):
        CustomFieldset.objects.create(namespace="local", slug="unique-set", label="Unique Set")
        with self.assertRaises(IntegrityError):
            CustomFieldset.objects.create(namespace="local", slug="unique-set", label="Different label")

    def test_custom_fieldset_empty_fields(self):
        cfs = CustomFieldset.objects.create(namespace="local", slug="empty-set", label="Empty Set")
        self.assertEqual(cfs.fields.count(), 0)


class DashboardModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dashuser", password="testpass")

    def test_dashboard_creation(self):
        dash = Dashboard.objects.create(user=self.user)
        self.assertIn("Dashboard for dashuser", str(dash))
        self.assertEqual(dash.layout, [])

    def test_dashboard_add_widget(self):
        dash = Dashboard.objects.create(user=self.user)
        dash.add_widget("asset_count", title="Total Assets", w=4, h=2)
        self.assertEqual(len(dash.layout), 1)
        self.assertEqual(dash.layout[0]["widget"], "asset_count")
        self.assertEqual(dash.layout[0]["title"], "Total Assets")

    def test_dashboard_remove_widget(self):
        dash = Dashboard.objects.create(user=self.user)
        dash.add_widget("widget_a", title="Widget A")
        dash.add_widget("widget_b", title="Widget B")
        self.assertEqual(len(dash.layout), 2)
        dash.remove_widget(0)
        self.assertEqual(len(dash.layout), 1)
        self.assertEqual(dash.layout[0]["widget"], "widget_b")

    def test_dashboard_update_widget(self):
        dash = Dashboard.objects.create(user=self.user)
        dash.add_widget("configurable", title="Original Title")
        dash.update_widget(0, title="Updated Title", visible=False)
        self.assertEqual(dash.layout[0]["title"], "Updated Title")
        self.assertFalse(dash.layout[0]["visible"])

    def test_dashboard_move_widget(self):
        dash = Dashboard.objects.create(user=self.user)
        dash.add_widget("first", title="First")
        dash.add_widget("second", title="Second")
        dash.add_widget("third", title="Third")
        dash.move_widget(0, 2)
        self.assertEqual(dash.layout[0]["widget"], "second")
        self.assertEqual(dash.layout[1]["widget"], "third")
        self.assertEqual(dash.layout[2]["widget"], "first")

    def test_dashboard_multiple_allowed(self):
        Dashboard.objects.create(user=self.user, name="Dashboard 1")
        Dashboard.objects.create(user=self.user, name="Dashboard 2")
        self.assertEqual(Dashboard.objects.filter(user=self.user).count(), 2)


class TagViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")
        self.tag = Tag.objects.create(name="Production", slug="production", color="00ff00")

    def test_list_view(self):
        url = reverse("extras:tag_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production")

    def test_detail_view(self):
        url = reverse("extras:tag_detail", kwargs={"pk": self.tag.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production")

    def test_create_view_get(self):
        url = reverse("extras:tag_create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_create_view_post(self):
        url = reverse("extras:tag_create")
        response = self.client.post(
            url,
            {
                "name": "Development",
                "slug": "development",
                "color": "0000ff",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Tag.objects.filter(name="Development").exists())

    def test_edit_view_get(self):
        url = reverse("extras:tag_update", kwargs={"pk": self.tag.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_edit_view_post(self):
        url = reverse("extras:tag_update", kwargs={"pk": self.tag.pk})
        response = self.client.post(
            url,
            {
                "name": "Staging",
                "slug": "staging",
                "color": "ffff00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "Staging")

    def test_delete_view_get(self):
        url = reverse("extras:tag_delete", kwargs={"pk": self.tag.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_delete_view_post(self):
        url = reverse("extras:tag_delete", kwargs={"pk": self.tag.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Tag.objects.filter(pk=self.tag.pk).exists())


class CustomFieldViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")
        self.cf = CustomField.objects.create(
            name="dept_code",
            label="Department Code",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )

    def test_list_view(self):
        url = reverse("extras:customfield_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_view(self):
        url = reverse("extras:customfield_detail", kwargs={"pk": self.cf.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Department Code")

    def test_create_view_post(self):
        url = reverse("extras:customfield_create")
        response = self.client.post(
            url,
            {
                "name": "building_floor",
                "namespace": "local",
                "label": "Building Floor",
                "field_type": CustomField.FIELD_TYPE_INTEGER,
                "activation": CustomField.ACTIVATION_GLOBAL,
                "object_types": [ContentType.objects.get_for_model(Asset).pk],
                "required": "on",
            },
        )
        if response.status_code != 302:
            form = response.context.get("form")
            if form:
                self.fail(f"Form invalid. Errors: {form.errors}")
        self.assertTrue(CustomField.objects.filter(name="building_floor").exists())

    def test_delete_view_post(self):
        url = reverse("extras:customfield_delete", kwargs={"pk": self.cf.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CustomField.objects.filter(pk=self.cf.pk).exists())
        self.cf.refresh_from_db()
        self.assertEqual(self.cf.lifecycle, CustomField.LIFECYCLE_ACTIVE)


class CustomFieldsetViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")
        self.cfs = CustomFieldset.objects.create(
            namespace="local",
            slug="network-config",
            label="Network Config",
        )

    def test_list_view(self):
        url = reverse("extras:customfieldset_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_view(self):
        url = reverse("extras:customfieldset_detail", kwargs={"pk": self.cfs.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Network Config")

    def test_create_view_post(self):
        url = reverse("extras:customfieldset_create")
        response = self.client.post(
            url,
            {
                "namespace": "local",
                "slug": "server-specs",
                "label": "Server Specs",
                "description": "Server hardware specifications.",
            },
        )
        if response.status_code != 302:
            form = response.context.get("form")
            if form:
                self.fail(f"Form invalid. Errors: {form.errors}")
        self.assertTrue(
            CustomFieldset.objects.filter(namespace="local", slug="server-specs", label="Server Specs").exists()
        )

    def test_delete_view_post(self):
        url = reverse("extras:customfieldset_delete", kwargs={"pk": self.cfs.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CustomFieldset.objects.filter(pk=self.cfs.pk).exists())
        self.cfs.refresh_from_db()
        self.assertEqual(self.cfs.lifecycle, CustomFieldset.LIFECYCLE_ACTIVE)


class TagColumnTests(TestCase):
    def test_tag_column_rendering(self):
        from extras.tables import TagColumn

        Tag.objects.create(name="Tag1", slug="tag1", color="ffffff")  # white bg -> dark text
        Tag.objects.create(name="Tag2", slug="tag2", color="000000")  # black bg -> white text
        Tag.objects.create(name="Tag3", slug="tag3", color="20c997")
        Tag.objects.create(name="Tag4", slug="tag4", color="111111")

        column = TagColumn(url_name="extras:tag_list")

        # Test empty tags
        self.assertEqual(column.render(Tag.objects.none()), "")

        # Test up to 3 tags
        qs_3 = Tag.objects.filter(name__in=["Tag1", "Tag2", "Tag3"])
        from core.context import reset_current_csp_nonce, set_current_csp_nonce

        token = set_current_csp_nonce("test-nonce")
        try:
            html_3 = column.render(qs_3)
            self.assertIn("background-color:#ffffff", html_3)
            self.assertIn("color-chip-text-dark", html_3)  # contrast text for white bg
            self.assertIn("background-color:#000000", html_3)
            self.assertIn("color-chip-text-light", html_3)  # contrast text for black bg
            self.assertNotIn("+1", html_3)

            # Test 4 tags (limit exceeded)
            qs_4 = Tag.objects.all()
            html_4 = column.render(qs_4)
            self.assertIn("+1", html_4)
        finally:
            reset_current_csp_nonce(token)


class BulkEditFormTests(TestCase):
    def test_bulk_edit_form_choices_and_styling(self):
        from django import forms

        from core.forms import BulkEditForm
        from organization.models import Location

        # Construct form with Location model
        form = BulkEditForm(model=Location)

        # Check 'status' field (it has choices on the model)
        self.assertIn("status", form.fields)
        self.assertIsInstance(form.fields["status"], forms.ChoiceField)

        # Check that it includes the STATUS_CHOICES plus '---------'
        status_choices = dict(form.fields["status"].choices)
        self.assertIn("planned", status_choices)
        self.assertIn("active", status_choices)
        self.assertEqual(status_choices[""], "---------")

        # Check that appropriate styling classes are added to widgets
        # 'status' (Select widget) should have 'form-select' class
        status_class = form.fields["status"].widget.attrs.get("class", "")
        self.assertIn("form-select", status_class)
        self.assertIn("data-tom-select", form.fields["status"].widget.attrs)

        # 'name' (TextInput widget) should have 'form-control' class
        name_class = form.fields["name"].widget.attrs.get("class", "")
        self.assertIn("form-control", name_class)

        # 'description' (Textarea widget) should have 'form-control' class
        desc_class = form.fields["description"].widget.attrs.get("class", "")
        self.assertIn("form-control", desc_class)


class TagDetailTenantScopeTests(TenantTestMixin, TestCase):
    """F7: a global tag must list only assets visible in the active scope."""

    def setUp(self):
        from assets.models import AssetType, Manufacturer, StatusLabel
        from organization.models import Tenant

        self.setup_tenant_context(
            name="Tag Scope A", slug="tag-scope-a", permissions=["extras.view_tag", "assets.view_asset"]
        )
        self.tenant_a = self.tenant
        self.tenant_b = Tenant.objects.create(name="Tag Scope B", slug="tag-scope-b")
        manufacturer = Manufacturer.objects.create(name="Tag Scope Mfr", slug="tag-scope-mfr")
        asset_type = AssetType.objects.create(model="Tag Scope Type", slug="tag-scope-type", manufacturer=manufacturer)
        status, _ = StatusLabel.objects.get_or_create(
            slug="available", defaults={"name": "Available", "type": "deployable", "color": "28a745"}
        )
        self.tag = Tag.objects.create(name="Scope Global Tag", slug="scope-global-tag", color="00ff00")
        self.asset_a = Asset.objects.create(
            name="SCOPE-A-ASSET", asset_tag="SCOPE-A-TAG", tenant=self.tenant_a, status=status, asset_type=asset_type
        )
        self.asset_b = Asset.objects.create(
            name="SECRET-B-ASSET", asset_tag="SECRET-B-TAG", tenant=self.tenant_b, status=status, asset_type=asset_type
        )
        self.asset_a.tags.add(self.tag)
        self.asset_b.tags.add(self.tag)
        self.url = reverse("extras:tag_detail", kwargs={"pk": self.tag.pk})

    def test_tenant_sees_only_own_tagged_assets(self):
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SCOPE-A-ASSET")
        self.assertNotContains(response, "SECRET-B-ASSET")
        self.assertNotContains(response, "SECRET-B-TAG")
        self.assertEqual(len(response.context["assets_table"].rows), 1)

    def test_foreign_only_tag_renders_empty_table(self):
        self.asset_a.tags.remove(self.tag)
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "SECRET-B-ASSET")
        self.assertEqual(len(response.context["assets_table"].rows), 0)


class _GlobalParentReverseScopeBase(TenantTestMixin, TestCase):
    permissions = []

    def setUp(self):
        from organization.models import Tenant

        self.setup_tenant_context(name="Rev Scope A", slug="rev-scope-a", permissions=self.permissions)
        self.tenant_a = self.tenant
        self.tenant_b = Tenant.objects.create(name="Rev Scope B", slug="rev-scope-b")


class WebhookEndpointEventRuleScopeTests(_GlobalParentReverseScopeBase):
    """R-2: a global endpoint must list only event rules visible in the active scope."""

    permissions = ["extras.view_webhookendpoint", "extras.view_eventrule"]

    def setUp(self):
        super().setUp()
        self.endpoint = WebhookEndpoint.objects.create(name="Rev Global Hook", url="https://example.com/hook")
        self.content_type = ContentType.objects.get_for_model(Asset)
        self.url = reverse("extras:webhookendpoint_detail", kwargs={"pk": self.endpoint.pk})

    def _rule(self, name, tenant):
        return EventRule.objects.create(
            name=name,
            model=self.content_type,
            events=["create"],
            action_type=EventRule.ACTION_WEBHOOK,
            webhook=self.endpoint,
            tenant=tenant,
        )

    def test_own_rule_visible_foreign_rule_hidden(self):
        own = self._rule("A-RULE-VISIBLE", self.tenant_a)
        self._rule("B-RULE-SECRET", self.tenant_b)
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r.pk for r in response.context["event_rules"]], [own.pk])
        self.assertContains(response, "A-RULE-VISIBLE")
        self.assertNotContains(response, "B-RULE-SECRET")

    def test_foreign_only_rule_renders_empty_state(self):
        self._rule("B-RULE-SECRET", self.tenant_b)
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["event_rules"]), [])
        self.assertNotContains(response, "B-RULE-SECRET")
        self.assertContains(response, "No event rules target this endpoint yet.")


class AlertRuleChannelScopeTests(_GlobalParentReverseScopeBase):
    """R-3: a global alert rule must list only notification channels visible in the active scope."""

    permissions = ["extras.view_alertrule", "extras.view_notificationchannel"]

    def setUp(self):
        super().setUp()
        self.rule = AlertRule.objects.create(
            name="Rev Global Rule", alert_type=AlertRule.ALERT_TYPE_LOW_STOCK, threshold_value=5, tenant=None
        )
        self.url = reverse("extras:alertrule_detail", kwargs={"pk": self.rule.pk})

    def _channel(self, name, tenant):
        channel = NotificationChannel.objects.create(
            name=name, channel_type=NotificationChannel.TYPE_EMAIL, tenant=tenant
        )
        self.rule.channels.add(channel)
        return channel

    def test_own_channel_visible_foreign_channel_hidden(self):
        own = self._channel("A-CHAN-VISIBLE", self.tenant_a)
        self._channel("B-CHAN-SECRET", self.tenant_b)
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c.pk for c in response.context["channels"]], [own.pk])
        self.assertContains(response, "A-CHAN-VISIBLE")
        self.assertNotContains(response, "B-CHAN-SECRET")

    def test_foreign_only_channel_renders_empty_state(self):
        self._channel("B-CHAN-SECRET", self.tenant_b)
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["channels"]), [])
        self.assertNotContains(response, "B-CHAN-SECRET")
        self.assertContains(response, "No custom channels configured")
