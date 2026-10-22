"""The bulk scan entries are offered exactly where the scan views would accept them (#907).

The mobile action bar, the docked bulk-action toolbar and the sidebar menu
group share one capability decision, so all three surfaces are asserted from the
same rendered asset list.
"""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse

from assets.views.context_processors import bulk_scan_capabilities
from core.tests.mixins import TenantTestMixin
from organization.models import Role, Tenant

User = get_user_model()


class BulkScanCapabilityTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="caps")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.list_url = reverse("assets:asset_list")
        self.scan_urls = {
            "checkin": reverse("assets:asset_bulk_checkin_scan"),
            "checkout": reverse("assets:asset_bulk_checkout_scan"),
            "dispose": reverse("assets:asset_bulk_dispose_scan"),
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _grant_second_tenant(self, permissions):
        other = Tenant.objects.create(name="Second", slug="caps-second")
        role = Role.objects.create(tenant=other, name="Second Role", permissions=permissions)
        self.grant(self.tenant_user, other, role)
        return other

    def _login_all_accessible(self):
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session["active_all_accessible"] = True
        session.save()

    def _assert_surfaces(self, response, *, checkin, checkout, dispose):
        """Assert every surface offers exactly the expected entries.

        Three surfaces render the same scan links: the sidebar menu group, the
        mobile action bar and the docked bulk-action toolbar.
        """
        body = response.content.decode()
        # The mobile bar is one block of the global layout; slice it out so the
        # sidebar's own scan links (a separate surface) cannot answer for it.
        start = body.index('class="mobile-action-bar')
        end = body.index('id="global-scanner-modal"')
        mobile_bar = body[start:end]
        # The sidebar menu group is a third surface with the same promise.
        side_start = body.index('<aside class="navbar navbar-vertical')
        sidebar = body[side_start : body.index("</aside>", side_start)]
        expected = {"checkin": checkin, "checkout": checkout, "dispose": dispose}
        for action, url in self.scan_urls.items():
            # Mobile action bar and sidebar: a plain anchor to the scan page.
            anchor = 'href="' + url + '"'
            # Docked bulk-action toolbar: a seed button carrying the scan URL.
            toolbar = 'data-scan-url="' + url + '"'
            self.assertEqual(
                mobile_bar.count(anchor),
                1 if expected[action] else 0,
                f"mobile action bar {'should' if expected[action] else 'should not'} offer {action}",
            )
            self.assertEqual(
                sidebar.count(anchor),
                1 if expected[action] else 0,
                f"sidebar {'should' if expected[action] else 'should not'} offer {action}",
            )
            self.assertEqual(
                body.count(toolbar),
                1 if expected[action] else 0,
                f"bulk-action toolbar {'should' if expected[action] else 'should not'} offer {action}",
            )

    # ── aggregate scope ──────────────────────────────────────────────────────

    def test_aggregate_scope_offers_actions_permitted_in_one_accessible_tenant(self):
        """Read-only at home, action rights in a second tenant: both surfaces offer the entries."""
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self._grant_second_tenant(["assets.view_asset", "assets.change_asset", "assets.add_assetdisposal"])
        self._login_all_accessible()

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["bulk_scan_capabilities"],
            {"checkin": True, "checkout": True, "dispose": True},
        )
        self._assert_surfaces(response, checkin=True, checkout=True, dispose=True)

    def test_aggregate_scope_offers_nothing_without_permission_in_any_accessible_tenant(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self._grant_second_tenant(["assets.view_asset"])
        self._login_all_accessible()

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["bulk_scan_capabilities"],
            {"checkin": False, "checkout": False, "dispose": False},
        )
        self._assert_surfaces(response, checkin=False, checkout=False, dispose=False)

    def test_aggregate_scope_keeps_disposal_separate_from_checkin(self):
        """Check-in/check-out rights alone must not offer the disposal entry."""
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self._login_all_accessible()

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["bulk_scan_capabilities"],
            {"checkin": True, "checkout": True, "dispose": False},
        )
        self._assert_surfaces(response, checkin=True, checkout=True, dispose=False)

    def test_aggregate_scope_offers_the_sidebar_entries_too(self):
        """The sidebar group followed the objectless permission check (#923).

        Under "all accessible tenants" the objectless ``assets.change_asset``
        check is refused by design, so the menu group only ever showed the
        disposal entry even though the scan pages accept check-in/check-out
        with a target tenant the actor holds the permission in.
        """
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self._login_all_accessible()

        response = self.client.get(self.list_url)

        body = response.content.decode()
        side_start = body.index('<aside class="navbar navbar-vertical')
        sidebar = body[side_start : body.index("</aside>", side_start)]
        self.assertIn('href="' + self.scan_urls["checkin"] + '"', sidebar)
        self.assertIn('href="' + self.scan_urls["checkout"] + '"', sidebar)
        self.assertNotIn('href="' + self.scan_urls["dispose"] + '"', sidebar)

    # ── concrete tenant scope ────────────────────────────────────────────────

    def test_concrete_scope_is_driven_by_the_active_tenant_permission(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self._assert_surfaces(response, checkin=True, checkout=True, dispose=False)

    def test_concrete_scope_hides_entries_without_the_permission(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self._assert_surfaces(response, checkin=False, checkout=False, dispose=False)

    def test_concrete_scope_ignores_a_permission_held_only_elsewhere(self):
        """A grant in another tenant must not light up the active tenant's entries."""
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self._grant_second_tenant(["assets.view_asset", "assets.change_asset", "assets.add_assetdisposal"])
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self._assert_surfaces(response, checkin=False, checkout=False, dispose=False)

    # ── request without a usable principal ───────────────────────────────────

    def test_anonymous_request_offers_nothing(self):
        self.assertEqual(
            bulk_scan_capabilities(SimpleNamespace(user=AnonymousUser())),
            {"bulk_scan_capabilities": {"checkin": False, "checkout": False, "dispose": False}},
        )

    def test_request_without_a_user_attribute_offers_nothing(self):
        self.assertEqual(
            bulk_scan_capabilities(SimpleNamespace()),
            {"bulk_scan_capabilities": {"checkin": False, "checkout": False, "dispose": False}},
        )

    # ── toolbar surface ──────────────────────────────────────────────────────

    def test_toolbar_renders_no_scan_buttons_for_an_unauthorised_user(self):
        """The toolbar used to render the three buttons unconditionally."""
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="bulk-action-toolbar"')
        self.assertNotContains(response, "btn-bulk-scan-seed")
        self.assertContains(response, "btn-bulk-label")

    def test_toolbar_renders_scan_buttons_for_a_permitted_user(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset", "assets.add_assetdisposal"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count("btn-bulk-scan-seed"), 3)
