"""Behavior locks for tenant-aware navigation helpers and rendered menu shape."""

from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.forms.import_forms import is_model_importable
from core.navigation.menu import _msp_layer_active, _user_provider_tenants
from core.tasks.context import TaskContext
from core.templatetags.navigation import nav
from core.tests.mixins import grant
from organization.models import Role, Tenant

User = get_user_model()


def _visible_links(user):
    context = nav({"request": SimpleNamespace(user=user)})
    return [item.link for _menu, groups in context["nav_items"] for _group, items in groups for item, _buttons in items]


def _visible_import_links(user):
    context = nav({"request": SimpleNamespace(user=user)})
    return {
        button.link
        for _menu, groups in context["nav_items"]
        for _group, items in groups
        for _item, buttons in items
        for button in buttons
        if button.link.startswith("/import/")
    }


class TenantNavigationTests(TestCase):
    def test_msp_layer_active_is_live_provider_only_and_cached_per_user(self):
        deleted_provider = Tenant.objects.create(
            name="Deleted Provider",
            slug="deleted-provider",
            is_provider=True,
        )
        deleted_provider.delete()
        Tenant.objects.create(name="Ordinary Tenant", slug="ordinary-tenant")
        user = User.objects.create_user(username="provider-cache-user")

        with CaptureQueriesContext(connection) as first_queries:
            self.assertFalse(_msp_layer_active(user))
        with CaptureQueriesContext(connection) as cached_queries:
            self.assertFalse(_msp_layer_active(user))

        self.assertEqual(len(first_queries), 1)
        self.assertEqual(len(cached_queries), 0)

        live_provider = Tenant.objects.create(name="Live Provider", slug="live-provider", is_provider=True)
        fresh_user = User.objects.create_user(username="provider-cache-fresh-user")
        self.assertTrue(_msp_layer_active(fresh_user))
        live_provider.delete()
        self.assertTrue(_msp_layer_active(fresh_user))

    def test_user_provider_tenants_are_accessible_live_providers_only(self):
        provider = Tenant.objects.create(name="Provider", slug="provider", is_provider=True)
        ordinary = Tenant.objects.create(name="Ordinary", slug="ordinary")
        deleted_provider = Tenant.objects.create(
            name="Retired Provider",
            slug="retired-provider",
            is_provider=True,
        )
        user = User.objects.create_user(username="provider-list-user")
        for tenant in (provider, ordinary, deleted_provider):
            role = Role.objects.create(tenant=tenant, name=f"Role {tenant.pk}", permissions=[])
            grant(user, tenant, role)
        with TaskContext(operation="test.navigation_tenancy.soft_delete_provider"):
            deleted_provider.delete()

        self.assertEqual(_user_provider_tenants(user), [provider])

    def test_visible_menu_shape_for_anonymous_and_ordinary_users(self):
        ordinary = User.objects.create_user(username="ordinary-navigation-user")

        self.assertEqual(_visible_links(AnonymousUser()), [])
        self.assertEqual(_visible_links(ordinary), [])

    def test_superuser_menu_shape_and_navigation_query_cache(self):
        Tenant.objects.create(name="Menu Provider", slug="menu-provider", is_provider=True)
        superuser = User.objects.create_superuser(username="menu-root", email="root@example.com", password="pw")

        with CaptureQueriesContext(connection) as first_queries:
            visible_links = _visible_links(superuser)
        with CaptureQueriesContext(connection) as cached_queries:
            repeated_links = _visible_links(superuser)

        self.assertEqual(repeated_links, visible_links)
        self.assertEqual(len(first_queries), 1)
        self.assertEqual(len(cached_queries), 0)
        self.assertEqual(
            visible_links,
            [
                "organization:site_list",
                "organization:region_list",
                "organization:sitegroup_list",
                "organization:location_list",
                "organization:tenant_list",
                "organization:tenantgroup_list",
                "organization:assetholder_list",
                "organization:tenantresourcegrant_list",
                "organization:contact_list",
                "organization:contactrole_list",
                "assets:asset_list",
                "assets:asset_bulk_checkout_scan",
                "assets:asset_bulk_checkin_scan",
                "assets:asset_bulk_dispose_scan",
                "assets:assettype_list",
                "assets:manufacturer_list",
                "assets:category_list",
                "assets:assetrole_list",
                "assets:statuslabel_list",
                "assets:warranty_list",
                "assets:assetmaintenance_list",
                "assets:assetreservation_list",
                "assets:assetdisposal_list",
                "inventory:component_list",
                "inventory:accessory_list",
                "inventory:consumable_list",
                "inventory:kit_list",
                "software:software_list",
                "licenses:license_list",
                "subscriptions:subscription_list",
                "procurement:purchaseorder_list",
                "assets:request_list",
                "assets:supplier_list",
                "procurement:contract_list",
                "assets:depreciation_list",
                "organization:costcenter_list",
                "compliance:auditsession_list",
                "compliance:custodytemplate_list",
                "compliance:custodyreceipt_list",
                "extras:alertlog_list",
                "extras:alertrule_list",
                "extras:notificationchannel_list",
                "extras:scheduledreport_list",
                "extras:reporttemplate_list",
                "objectchange_list",
                "journalentry_list",
                "job_list",
                "extras:customfield_list",
                "extras:customfieldset_list",
                "extras:savedfilter_list",
                "extras:tag_list",
                "assets:assettagsequence_list",
                "extras:exporttemplate_list",
                "extras:labeltemplate_list",
                "extras:webhookendpoint_list",
                "extras:eventrule_list",
                "users:user_list",
                "organization:role_list",
                "organization:membership_list",
                "users:usergroup_list",
                "organization:technician_quick_add",
                "admin:index",
            ],
        )

    def test_visible_import_buttons_match_importability_contract(self):
        Tenant.objects.create(name="Import Menu Provider", slug="import-menu-provider", is_provider=True)
        superuser = User.objects.create_superuser(
            username="import-navigation-root",
            email="import-root@example.com",
            password="pw",
        )

        visible_imports = _visible_import_links(superuser)
        importable_models = (
            ("assets", "asset"),
            ("assets", "assettype"),
            ("assets", "manufacturer"),
            ("assets", "assetmaintenance"),
            ("inventory", "accessory"),
            ("inventory", "component"),
            ("inventory", "consumable"),
            ("licenses", "license"),
            ("organization", "assetholder"),
            ("organization", "location"),
            ("software", "software"),
            ("subscriptions", "subscription"),
        )
        non_importable_models = (
            ("procurement", "purchaseorder"),
            ("assets", "assetrequest"),
            ("compliance", "auditsession"),
            ("assets", "assettagsequence"),
        )

        for app_label, model_name in importable_models:
            with self.subTest(importable=(app_label, model_name)):
                model = apps.get_model(app_label, model_name)
                link = f"/import/{app_label}/{model_name}/"
                self.assertTrue(is_model_importable(model))
                self.assertIn(link, visible_imports)

        for app_label, model_name in non_importable_models:
            with self.subTest(non_importable=(app_label, model_name)):
                model = apps.get_model(app_label, model_name)
                link = f"/import/{app_label}/{model_name}/"
                self.assertFalse(is_model_importable(model))
                self.assertNotIn(link, visible_imports)

        for link in visible_imports:
            app_label, model_name = link.removeprefix("/import/").rstrip("/").split("/")
            with self.subTest(visible_import=link):
                self.assertTrue(is_model_importable(apps.get_model(app_label, model_name)))
