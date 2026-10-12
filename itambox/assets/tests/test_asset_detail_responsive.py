"""Issue #260 — responsive asset-detail and scan-shell regressions.

Regression coverage for removing the assignment-status and software-inventory
stat banners while preserving the underlying asset data and tab navigation.

The detail-view regressions use the real view + template path; the theme
contract renders the affected include directly.
"""

from html.parser import HTMLParser
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from assets.services import checkout_asset
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Role, Tenant, TenantGroup

User = get_user_model()


class _ScanInputGroupParser(HTMLParser):
    """Extract semantic facts from the real scan input group without bs4."""

    def __init__(self):
        super().__init__()
        self.group_depth = 0
        self.button_depth = 0
        self.hidden_depth = 0
        self.input_attrs = {}
        self.button_attrs = {}
        self.visible_button_text = []
        self.barcode_segment_count = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        if self.group_depth == 0 and tag == "div" and "input-group-lg" in classes:
            self.group_depth = 1
            return
        if not self.group_depth:
            return
        if tag == "div":
            self.group_depth += 1
        if tag == "span" and "input-group-text" in classes:
            self.barcode_segment_count += 1
        if tag == "input" and attributes.get("id") == "scan-basket-input":
            self.input_attrs = attributes
        if tag == "button" and attributes.get("id") == "basket-open-scanner-btn":
            self.button_attrs = attributes
            self.button_depth = 1
            return
        if self.button_depth:
            self.button_depth += 1
            if tag == "span" and "visually-hidden" in classes:
                self.hidden_depth = 1

    def handle_endtag(self, tag):
        if self.hidden_depth:
            self.hidden_depth -= 1
        if self.button_depth:
            self.button_depth -= 1
        elif self.group_depth and tag == "div":
            self.group_depth -= 1

    def handle_data(self, data):
        if self.button_depth and not self.hidden_depth:
            self.visible_button_text.append(data)


def _scan_input_group(response):
    parser = _ScanInputGroupParser()
    parser.feed(response.content.decode())
    return parser


def _asset_fixtures(tenant, suffix=""):
    manufacturer = Manufacturer.objects.create(name=f"Mfr{suffix}", slug=f"mfr{suffix}")
    role = AssetRole.objects.create(name=f"Role{suffix}", slug=f"role{suffix}")
    asset_type = AssetType.objects.create(manufacturer=manufacturer, model=f"Model{suffix}", slug=f"type{suffix}")
    status = StatusLabel.objects.create(name=f"Deployable{suffix}", slug=f"deployable{suffix}", type="deployable")
    return Asset.objects.create(
        name=f"Detail Asset{suffix}",
        asset_tag=f"DET-{suffix or '001'}",
        serial_number=f"SN-DET-{suffix or '001'}",
        asset_type=asset_type,
        asset_role=role,
        status=status,
        tenant=tenant,
    )


class _RelatedAllocations(list):
    def all(self):
        return self


class AssetScanMarkupTests(TenantTestMixin, TestCase):
    """AC9/AC10 — preserve scan behavior while simplifying the controls."""

    def setUp(self):
        self.setup_tenant_context(slug="i260-scan")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        self.url = reverse("assets:asset_bulk_checkin_scan")

    def test_scan_group_keeps_input_and_has_icon_only_camera_action(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        scan_group = _scan_input_group(response)
        self.assertEqual(scan_group.barcode_segment_count, 0)
        self.assertEqual(scan_group.input_attrs.get("id"), "scan-basket-input")
        self.assertEqual(scan_group.button_attrs.get("id"), "basket-open-scanner-btn")
        self.assertEqual(scan_group.button_attrs.get("aria-label"), "Camera")
        self.assertEqual(scan_group.button_attrs.get("title"), "Camera")
        self.assertEqual("".join(scan_group.visible_button_text).strip(), "")
        self.assertIn("btn-primary", scan_group.button_attrs.get("class", ""))
        self.assertIn("scan-basket-camera", scan_group.button_attrs.get("class", ""))


class AssetDetailBannerRemovalTests(TenantTestMixin, TestCase):
    """AC1/AC2/AC3 — the two informational banners are gone, the data is not."""

    def setUp(self):
        self.setup_tenant_context(slug="i260-detail")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.asset = _asset_fixtures(self.tenant, "-d")
        self.deployed_status = StatusLabel.objects.create(name="I260 Deployed", slug="i260-deployed", type="deployed")
        self.url = reverse("assets:asset_detail", kwargs={"pk": self.asset.pk})
        self.client_login_to_tenant(self.tenant_admin, self.tenant)

    def _body(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_assignment_status_banner_not_rendered(self):
        body = self._body()
        self.assertNotIn("Assignment Status", body)
        self.assertNotIn("Available in Inventory", body)

    def test_assignment_status_banner_not_rendered_for_assigned_asset(self):
        holder = AssetHolder.objects.create(
            first_name="Assigned",
            last_name="Holder",
            upn="assigned-holder-i260@example.test",
            tenant=self.tenant,
        )
        checkout_asset(self.asset, holder=holder, user=self.tenant_admin, status=self.deployed_status)
        self.assertIsNotNone(self.asset.active_assignment)
        self.assertEqual(self.asset.active_assignment.assigned_user, holder)

        body = self._body()
        self.assertIn(str(holder), body)
        self.assertNotIn("Assignment Status", body)
        self.assertNotIn("Deployed / Checked Out", body)

    def test_software_inventory_banner_not_rendered_in_zero_state(self):
        """The empty/zero state ("0 Items Logged") must not render either."""
        self.assertEqual(self.asset.installed_software.count(), 0)
        body = self._body()
        # "Installed Software Inventory" (the Software tab heading) is legitimate
        # and must stay; only the removed banner's bare "0 Items Logged" stat is checked.
        self.assertNotIn("Items Logged", body)
        self.assertNotIn("Item Logged", body)

    def test_metrics_include_is_no_longer_used_by_the_detail_template(self):
        response = self.client.get(self.url)
        used = {template.name for template in response.templates if template.name}
        self.assertIn("assets/assets/asset_detail.html", used)
        self.assertNotIn("assets/includes/detail/asset_metrics.html", used)

    def test_detail_data_and_navigation_are_preserved(self):
        body = self._body()
        # Underlying asset data still rendered
        self.assertIn(self.asset.asset_tag, body)
        self.assertIn(self.asset.serial_number, body)
        # Deployment & Custody card (the real assignment data) still present
        self.assertIn("Deployment &amp; Custody", body)
        # Details / Maintenances / Software navigation intact
        self.assertIn('data-bs-target="#details"', body)
        self.assertIn('data-bs-target="#maintenances"', body)
        self.assertIn('data-bs-target="#software"', body)
        self.assertIn("?tab=software", body)
        self.assertEqual(body.count('href="/graphql/"'), 1)
        for menu_path in (
            "/user/profile/",
            "/user/notifications/",
            "/user/subscriptions/",
            "/user/preferences/",
            "/user/api-tokens/",
        ):
            self.assertGreaterEqual(body.count('href="' + menu_path + '"'), 2)

    def test_edit_action_keeps_accessible_label_for_mobile_icon_mode(self):
        body = self._body()
        self.assertIn("detail-edit-action", body)
        self.assertIn('aria-label="Edit"', body)
        self.assertIn('title="Edit"', body)
        self.assertIn("detail-edit-action__label", body)


class AssetHardwareModificationsThemeRenderTests(SimpleTestCase):
    def test_card_uses_theme_aware_colors(self):
        component = SimpleNamespace(manufacturer=SimpleNamespace(name="Dell"), name="16GB DDR5 Module")
        allocation = SimpleNamespace(component=component, qty=1)
        asset = SimpleNamespace(component_allocations=_RelatedAllocations([allocation]))

        html = render_to_string(
            "assets/includes/detail/asset_specs.html",
            {"object": asset, "asset_type_specification_fields": []},
        )

        self.assertIn("Active Hardware Modifications &amp; Upgrades", html)
        self.assertIn("Dell 16GB DDR5 Module", html)
        self.assertIn("rounded-2 bg-surface-secondary border border-dashed border-teal-subtle", html)
        self.assertIn("text-truncate text-body small", html)
        self.assertNotIn("bg-light", html)
        self.assertNotIn("text-dark", html)


class AssetListMobileSelectionTests(TenantTestMixin, TestCase):
    """AC8 — the selectable asset list exposes a mobile card-mode control."""

    def setUp(self):
        self.setup_tenant_context(slug="i260-list")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        _asset_fixtures(self.tenant, "-l")
        self.client_login_to_tenant(self.tenant_admin, self.tenant)

    def test_select_all_control_is_rendered_for_card_tables(self):
        response = self.client.get(reverse("assets:asset_list"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("mobile-select-all", body)
        self.assertIn('data-select-all="true"', body)
        self.assertIn("card-table", body)


class CatalogueDetailAssetScopeTests(TenantTestMixin, TestCase):
    """Catalogue detail asset tables must respect the viewer's request scope."""

    asset_permissions = (
        "assets.view_asset",
        "assets.view_assetrole",
        "assets.view_assettype",
        "assets.view_statuslabel",
    )
    catalogue_permissions = (
        "assets.view_assetrole",
        "assets.view_assettype",
        "assets.view_statuslabel",
    )

    def setUp(self):
        self.setup_tenant_context(name="Catalogue Scope Tenant A", slug="catalogue-scope-a")
        self.group = TenantGroup.objects.create(name="Catalogue Scope Group", slug="catalogue-scope-group")
        self.tenant.group = self.group
        self.tenant.save(update_fields=["group"])
        self.tenant_b = Tenant.objects.create(
            name="Catalogue Scope Tenant B",
            slug="catalogue-scope-b",
            group=self.group,
        )

        view_role = Role.objects.create(
            tenant=self.tenant,
            name="Catalogue detail asset viewer",
            permissions=self.asset_permissions,
        )
        self.grant(self.tenant_user, self.tenant, view_role)
        self.catalogue_only_user = User.objects.create_user(username="catalogue-only", password="password")
        catalogue_role = Role.objects.create(
            tenant=self.tenant,
            name="Catalogue detail viewer",
            permissions=self.catalogue_permissions,
        )
        self.grant(self.catalogue_only_user, self.tenant, catalogue_role)

        manufacturer = Manufacturer.objects.create(name="Catalogue Scope Mfr", slug="catalogue-scope-mfr")
        self.asset_role = AssetRole.objects.create(name="Catalogue Scope Role", slug="catalogue-scope-role")
        self.asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Catalogue Scope Device",
            slug="catalogue-scope-device",
            asset_role=self.asset_role,
        )
        self.status = StatusLabel.objects.create(
            name="Catalogue Scope Status",
            slug="catalogue-scope-status",
            type=StatusLabel.TYPE_DEPLOYABLE,
        )
        self.asset_a = self._make_asset(self.tenant, "A")
        self.asset_b = self._make_asset(self.tenant_b, "B")
        self.deleted_asset = self._make_asset(self.tenant, "DELETED")
        self.deleted_asset.deleted_at = timezone.now()
        self.deleted_asset.save(update_fields=["deleted_at"])

        self.detail_pages = (
            ("assets:assetrole_detail", self.asset_role.pk, "assets-tab"),
            ("assets:assettype_detail", self.asset_type.pk, "related-assets"),
            ("assets:statuslabel_detail", self.status.pk, "assets"),
        )

    def _make_asset(self, tenant, suffix):
        return Asset.objects.create(
            name="Catalogue Scope Asset " + suffix,
            asset_tag="CATALOGUE-SCOPE-" + suffix,
            serial_number="CATALOGUE-SCOPE-SN-" + suffix,
            asset_type=self.asset_type,
            asset_role=self.asset_role,
            status=self.status,
            tenant=tenant,
        )

    def _login_for_scope(self, user, scope):
        self.client.force_login(user)
        session = self.client.session
        for key in ("active_tenant_id", "active_tenant_group_id", "active_all_accessible"):
            session.pop(key, None)
        if scope == "tenant":
            session["active_tenant_id"] = self.tenant.pk
        elif scope == "group":
            session["active_tenant_group_id"] = self.group.pk
        else:
            session["active_all_accessible"] = True
        session.save()

    def test_detail_asset_tables_and_counts_use_tenant_group_and_all_accessible_scope(self):
        for scope in ("tenant", "group", "all_accessible"):
            self._login_for_scope(self.tenant_user, scope)
            for route, object_id, _tab in self.detail_pages:
                with self.subTest(scope=scope, route=route):
                    response = self.client.get(reverse(route, kwargs={"pk": object_id}))
                    self.assertEqual(response.status_code, 200)
                    table = response.context["assets_table"]
                    self.assertEqual([row.record.pk for row in table.rows], [self.asset_a.pk])
                    self.assertEqual(response.context["related_objects_list"][0]["count"], 1)

                    body = response.content.decode()
                    self.assertIn(self.asset_a.asset_tag, body)
                    self.assertNotIn(self.asset_b.name, body)
                    self.assertNotIn(self.asset_b.asset_tag, body)
                    self.assertNotIn(self.asset_b.serial_number, body)
                    self.assertNotIn(self.deleted_asset.asset_tag, body)

    def test_catalogue_only_role_does_not_receive_asset_panels_or_records(self):
        self._login_for_scope(self.catalogue_only_user, "tenant")
        for route, object_id, tab in self.detail_pages:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, kwargs={"pk": object_id}))
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context.get("assets_table"))
                self.assertEqual(response.context["related_objects_list"], [])

                body = response.content.decode()
                self.assertNotIn('data-bs-target="#' + tab + '"', body)
                self.assertNotIn(self.asset_a.name, body)
                self.assertNotIn(self.asset_a.asset_tag, body)
                self.assertNotIn(self.asset_b.name, body)
                self.assertNotIn(self.asset_b.asset_tag, body)
