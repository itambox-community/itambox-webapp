"""Tests for assets/scanning.py and the /scan/resolve/ endpoint."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from assets.scanning import resolve_scanned_asset, resolve_scanned_code
from core.tests.mixins import TenantTestMixin
from organization.models import Role, Tenant

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_asset_fixtures():
    mfr = Manufacturer.objects.create(name="TestMfr", slug="testmfr")
    role = AssetRole.objects.create(name="TestRole", slug="testrole")
    status = StatusLabel.objects.create(name="Active", slug="active-scan-test", type=StatusLabel.TYPE_DEPLOYABLE)
    atype = AssetType.objects.create(manufacturer=mfr, model="TestModel", slug="test-model", requestable=False)
    return role, status, atype


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for resolve_scanned_code
# ─────────────────────────────────────────────────────────────────────────────


class ResolveScannedCodeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context()
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.status, self.atype = _make_asset_fixtures()
        self.asset = Asset.objects.create(
            name="Scan Test Laptop",
            asset_tag="ITM-00001",
            serial_number="SN-LAPTOP-99",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )

    def test_bare_asset_tag(self):
        result = resolve_scanned_code("ITM-00001")
        self.assertEqual(result, self.asset)

    def test_bare_serial_number(self):
        result = resolve_scanned_code("SN-LAPTOP-99")
        self.assertEqual(result, self.asset)

    def test_itambox_scheme_tag(self):
        result = resolve_scanned_code("itambox:ITM-00001")
        self.assertEqual(result, self.asset)

    def test_itambox_asset_pk_url(self):
        result = resolve_scanned_code(f"itambox://asset/{self.asset.pk}")
        self.assertEqual(result, self.asset)

    def test_full_http_url(self):
        # Simulate a label that encoded a full URL (legacy or external QR)
        url = f"https://itam.example.com/assets/{self.asset.pk}/"
        result = resolve_scanned_code(url)
        self.assertEqual(result, self.asset)

    def test_url_with_tag_segment(self):
        # URL whose last segment is an asset tag (not a numeric pk)
        url = "http://localhost:8000/assets/ITM-00001/"
        result = resolve_scanned_code(url)
        self.assertEqual(result, self.asset)

    def test_unknown_code_returns_none(self):
        result = resolve_scanned_code("NO-SUCH-TAG")
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = resolve_scanned_code("")
        self.assertIsNone(result)

    def test_whitespace_stripped(self):
        result = resolve_scanned_code("  ITM-00001  ")
        self.assertEqual(result, self.asset)

    def test_case_insensitive_asset_tag(self):
        result = resolve_scanned_code("itm-00001")
        self.assertEqual(result, self.asset)

    def test_case_insensitive_serial_number(self):
        result = resolve_scanned_code("sn-laptop-99")
        self.assertEqual(result, self.asset)

    def test_case_insensitive_itambox_scheme_tag(self):
        result = resolve_scanned_code("itambox:itm-00001")
        self.assertEqual(result, self.asset)

    def test_itambox_double_slash_tag(self):
        result = resolve_scanned_code("itambox://itm-00001")
        self.assertEqual(result, self.asset)

    def test_itambox_nested_url(self):
        result = resolve_scanned_code(f"itambox:https://itam.example.com/assets/{self.asset.pk}/")
        self.assertEqual(result, self.asset)

    def test_whitespace_and_slashes_stripped(self):
        result = resolve_scanned_code("  itambox://itm-00001/  ")
        self.assertEqual(result, self.asset)

    def test_enclosed_in_quotes(self):
        result = resolve_scanned_code('"itambox:ITM-00001"')
        self.assertEqual(result, self.asset)

    def test_full_width_colon(self):
        result = resolve_scanned_code("itambox：ITM-00001")
        self.assertEqual(result, self.asset)

    def test_bom_and_zero_width_space(self):
        result = resolve_scanned_code("\ufeffitambox:\u200bITM-00001")
        self.assertEqual(result, self.asset)

    # ─────────────────────────────────────────────────────────────────────────
    # EAN / GTIN resolution (through the AssetType catalogue)
    # ─────────────────────────────────────────────────────────────────────────

    def test_ean_unique_type_resolves_asset(self):
        mfr = Manufacturer.objects.get(slug="testmfr")
        atype = AssetType.objects.create(
            manufacturer=mfr, model="EAN Scan Model", slug="ean-scan-model", ean="4012345678902"
        )
        asset = Asset.objects.create(
            name="EAN Scan Asset",
            asset_tag="EAN-SCAN-1",
            serial_number="SN-EAN-SCAN-1",
            asset_type=atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )
        self.assertEqual(resolve_scanned_code("4012345678902"), asset)
        resolved, ambiguous = resolve_scanned_asset("4012345678902")
        self.assertEqual(resolved, asset)
        self.assertFalse(ambiguous)

    def test_ean_whitespace_stripped(self):
        mfr = Manufacturer.objects.get(slug="testmfr")
        atype = AssetType.objects.create(
            manufacturer=mfr, model="EAN Case Model", slug="ean-case-model", ean="4012345678905"
        )
        Asset.objects.create(
            name="EAN Case Asset",
            asset_tag="EAN-CASE-1",
            asset_type=atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )
        self.assertIsNotNone(resolve_scanned_code("4012345678905"))
        self.assertIsNotNone(resolve_scanned_code("  4012345678905  "))

    def test_ean_ambiguous_type_is_flagged_not_silently_picked(self):
        mfr = Manufacturer.objects.get(slug="testmfr")
        atype = AssetType.objects.create(
            manufacturer=mfr, model="EAN Ambiguous Model", slug="ean-ambiguous-model", ean="4012345678903"
        )
        for i in range(2):
            Asset.objects.create(
                name=f"EAN Amb Asset {i}",
                asset_tag=f"EAN-AMB-{i}",
                serial_number=f"SN-EAN-AMB-{i}",
                asset_type=atype,
                asset_role=self.role,
                status=self.status,
                tenant=self.tenant,
            )
        asset, ambiguous = resolve_scanned_asset("4012345678903")
        self.assertIsNone(asset)
        self.assertTrue(ambiguous)
        # The plain resolver must never pick a wrong asset for an ambiguous EAN.
        self.assertIsNone(resolve_scanned_code("4012345678903"))

    def test_ean_asset_in_other_tenant_not_resolved_in_scope(self):
        """Audit/bulk semantics (no user): an EAN whose only asset lives in
        another tenant resolves to nothing while the current tenant is active."""
        mfr = Manufacturer.objects.get(slug="testmfr")
        atype = AssetType.objects.create(
            manufacturer=mfr, model="EAN Other Model", slug="ean-other-model", ean="4012345678904"
        )
        other = Tenant.objects.create(name="EANOtherTenant", slug="ean-other-tenant")
        Asset.objects.create(
            name="Other EAN Asset",
            asset_tag="EAN-OTH-1",
            asset_type=atype,
            asset_role=self.role,
            status=self.status,
            tenant=other,
        )
        self.assertIsNone(resolve_scanned_code("4012345678904"))

    # ─────────────────────────────────────────────────────────────────────────
    # Edge cases: failed lookups and defensive fallbacks
    # ─────────────────────────────────────────────────────────────────────────

    def test_itambox_pk_link_unknown_pk_returns_none(self):
        self.assertIsNone(resolve_scanned_code("itambox://asset/999999999"))
        self.assertIsNone(resolve_scanned_code(f"itambox://asset/{self.asset.pk + 100000}"))

    def test_itambox_pk_link_non_numeric_returns_none(self):
        asset, ambiguous = resolve_scanned_asset("itambox://asset/not-a-pk")
        self.assertIsNone(asset)
        self.assertFalse(ambiguous)

    def test_url_numeric_segment_without_match_returns_none(self):
        result = resolve_scanned_code("https://itam.example.com/assets/999999999/")
        self.assertIsNone(result)

    def test_url_without_path_segments_returns_none(self):
        result = resolve_scanned_code("https://itam.example.com")
        self.assertIsNone(result)

    def test_resolve_with_user_without_accessible_tenants(self):
        user = User.objects.create_user(username="noaccessscan", email="noaccessscan@example.com", password="password")
        asset, ambiguous = resolve_scanned_asset("ITM-00001", user=user)
        self.assertIsNone(asset)
        self.assertFalse(ambiguous)
        self.assertIsNone(resolve_scanned_code("ITM-00001", user=user))

    def test_accessible_queryset_falls_back_on_missing_softdelete_field(self):
        """Models without deleted_at (e.g. ObjectChange) take the FieldError fallback."""
        from assets.scanning import _accessible_model_queryset
        from core.models import ObjectChange

        qs = _accessible_model_queryset(ObjectChange, self.tenant_user)
        self.assertIsNotNone(qs)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-tenant resolution (global scan-to-find flow)
# ─────────────────────────────────────────────────────────────────────────────


class CrossTenantScanTests(TenantTestMixin, TestCase):
    """The global scan flow searches every tenant the user can access."""

    def setUp(self):
        self.setup_tenant_context(name="TenantA", slug="tenant-a", permissions=["assets.view_asset"])
        self.tenant_a = self.tenant
        self.member = self.tenant_user
        self.role, self.status, self.atype = _make_asset_fixtures()
        self.asset_a = Asset.objects.create(
            name="Cross Tenant A",
            asset_tag="CT-0001",
            serial_number="SN-CT-A",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant_a,
        )
        self.tenant_b = Tenant.objects.create(name="TenantB", slug="tenant-b")
        role_b = Role.objects.create(tenant=self.tenant_b, name="Role B", permissions=["assets.view_asset"])
        self.grant(self.member, self.tenant_b, role_b)
        self.asset_b = Asset.objects.create(
            name="Cross Tenant B",
            asset_tag="CT-0002",
            serial_number="SN-CT-B",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant_b,
        )
        self.url = reverse("scan_resolve")

    def test_found_in_other_tenant_than_active(self):
        """Active tenant A, asset lives in tenant B: the global scan still finds it."""
        self.client_login_to_tenant(self.member, self.tenant_a)
        resp = self.client.get(self.url, {"code": "CT-0002"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])
        self.assertIn("/assets/", data["url"])

    def test_found_by_serial_in_other_tenant(self):
        self.client_login_to_tenant(self.member, self.tenant_a)
        resp = self.client.get(self.url, {"code": "SN-CT-B"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_found_by_pk_link_in_other_tenant(self):
        self.client_login_to_tenant(self.member, self.tenant_a)
        resp = self.client.get(self.url, {"code": f"itambox://asset/{self.asset_b.pk}"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_no_active_tenant_still_resolves_accessible(self):
        """Member with accessible tenants but no tenant selected: resolve, not 404."""
        self.client.force_login(self.member)
        resp = self.client.get(self.url, {"code": "CT-0002"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_inaccessible_tenant_still_hidden(self):
        """A tenant holding the tag exists, but the user cannot access it."""
        tenant_c = Tenant.objects.create(name="TenantC", slug="tenant-c")
        Asset.objects.create(
            name="Hidden Asset",
            asset_tag="CT-0003",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=tenant_c,
        )
        self.client_login_to_tenant(self.member, self.tenant_a)
        resp = self.client.get(self.url, {"code": "CT-0003"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data.get("found"))

    def test_audit_style_scan_stays_scope_bound(self):
        """resolve_scanned_code without a user (audit/bulk flows) stays within the
        active tenant scope: the tenant-B asset is not found while tenant A is active."""
        self.set_active_tenant(self.tenant_a, None)
        self.assertIsNone(resolve_scanned_code("CT-0002"))
        self.assertIsNotNone(resolve_scanned_code("CT-0001"))


class ScanTargetNoAccessTests(TenantTestMixin, TestCase):
    """Global target resolution must skip inventory EANs when the user has no
    accessible tenants (defensive fallback of the cross-tenant queryset)."""

    def setUp(self):
        self.setup_tenant_context(
            slug="target-no-access", permissions=["assets.view_asset", "inventory.view_component"]
        )
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.member = self.tenant_user
        mfr = Manufacturer.objects.create(name="TargetMfr", slug="targetmfr")
        from inventory.models import Component

        self.component = Component.objects.create(
            name="Target RAM", slug="target-ram", manufacturer=mfr, ean="5555555555555", tenant=self.tenant
        )

    @patch("organization.access.accessible_tenant_ids", return_value=set())
    def test_inventory_ean_skipped_without_accessible_tenants(self, _mocked_ids):
        from assets.scanning import resolve_scanned_target

        target = resolve_scanned_target("5555555555555", self.member)
        self.assertIsNone(target)


class ScanTargetPermissionTests(TenantTestMixin, TestCase):
    """resolve_scanned_target honors per-object-type permissions."""

    def setUp(self):
        self.setup_tenant_context(slug="target-perm", permissions=["inventory.view_component"])
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.member = self.tenant_user
        mfr = Manufacturer.objects.create(name="PermMfr", slug="permmfr")
        from inventory.models import Component

        self.component = Component.objects.create(
            name="Perm RAM", slug="perm-ram", manufacturer=mfr, ean="4444444444444", tenant=self.tenant
        )

    def test_component_ean_resolves_with_view_component_only(self):
        """A member without assets.view_asset still resolves inventory EANs."""
        from assets.scanning import resolve_scanned_target

        target = resolve_scanned_target("4444444444444", self.member)
        self.assertIsNotNone(target)
        self.assertEqual(target["url"], self.component.get_absolute_url())

    def test_bare_scheme_without_view_asset_returns_none(self):
        from assets.scanning import resolve_scanned_target

        self.assertIsNone(resolve_scanned_target("itambox://", self.member))

    def test_candidate_from_url_without_segments(self):
        from assets.models import Asset
        from assets.scanning import _candidate_from_url

        candidate, asset = _candidate_from_url("", Asset.objects.all())
        self.assertEqual(candidate, "")
        self.assertIsNone(asset)


# ─────────────────────────────────────────────────────────────────────────────
# ScanResolveView endpoint tests
# ─────────────────────────────────────────────────────────────────────────────


class ScanResolveViewTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="TenantA", slug="tenant-a")
        role, status, atype = _make_asset_fixtures()
        self.asset = Asset.objects.create(
            name="Scan Endpoint Asset",
            asset_tag="SCAN-001",
            serial_number="SN-SCAN-001",
            asset_type=atype,
            asset_role=role,
            status=status,
            tenant=self.tenant,
        )
        self.url = reverse("scan_resolve")

    def _login(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)

    def test_found_by_tag(self):
        self._login()
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])
        self.assertIn("/assets/", data["url"])

    def test_found_by_serial(self):
        self._login()
        resp = self.client.get(self.url, {"code": "SN-SCAN-001"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_found_by_itambox_scheme(self):
        self._login()
        resp = self.client.get(self.url, {"code": "itambox:SCAN-001"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_found_by_url_wrapped(self):
        self._login()
        resp = self.client.get(self.url, {"code": f"https://itam.example.com/assets/{self.asset.pk}/"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_not_found(self):
        self._login()
        resp = self.client.get(self.url, {"code": "NOPE-9999"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data["found"])

    def test_missing_code_returns_400(self):
        self._login()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_redirects(self):
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertIn(resp.status_code, (302, 403))

    def test_cross_tenant_isolation(self):
        """A member in TenantB must not see TenantA's assets."""
        self.setup_tenant_context(name="TenantB", slug="tenant-b", permissions=["assets.view_asset"])
        # tenant_user is now TenantB's member with view_asset; the global scan
        # searches every tenant the user can access — which is TenantB only —
        # so TenantA's asset must stay invisible.
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data.get("found"))

    def test_no_active_tenant_returns_404_no_leak(self):
        """User authenticated but no active tenant → fail closed (404), no cross-tenant data."""
        # Log in without setting active_tenant_id in session — TenantMiddleware leaves tenant None.
        no_tenant_user = User.objects.create_user(
            username="notenant", email="notenant@example.com", password="password"
        )
        self.client.force_login(no_tenant_user)
        # Deliberately omit session['active_tenant_id'] so TenantMiddleware finds no tenant.
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data.get("found"))

    def test_superuser_no_active_tenant_resolves_global(self):
        """Superuser without an active tenant set in session can still resolve scanned assets."""
        self.client.force_login(self.tenant_admin)
        # Deliberately omit session['active_tenant_id'] so TenantMiddleware finds no tenant.
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

    def test_member_without_view_asset_resolves_nothing(self):
        """Without assets.view_asset no asset is ever resolved.

        The endpoint has no blanket asset gate (stock and location labels are
        resolved under their own view permissions); every asset step checks
        assets.view_asset, so an asset code answers a plain miss.
        """
        # tenant_user has empty permissions (setup_tenant_context default).
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data.get("found"))

    def test_member_with_view_asset_sees_own_tenant_not_other(self):
        """Member with view_asset resolves own-tenant asset and gets 404 for other tenant's tag."""
        from assets.models import AssetRole, AssetType, StatusLabel

        # Give tenant_user the view_asset permission.
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()

        # Reuse the type / role / status already created in setUp (avoid unique-constraint clash).
        existing_atype = AssetType.objects.filter(slug="test-model").first()
        existing_role = AssetRole.objects.filter(slug="testrole").first()
        existing_status = StatusLabel.objects.filter(slug="active-scan-test").first()

        # Create an asset in a second tenant.
        other_tenant = Tenant.objects.create(name="OtherTenant", slug="other-tenant-scan")
        Asset.objects.create(
            name="Other Tenant Asset",
            asset_tag="OTHER-999",
            serial_number="SN-OTHER-999",
            asset_type=existing_atype,
            asset_role=existing_role,
            status=existing_status,
            tenant=other_tenant,
        )

        self.client_login_to_tenant(self.tenant_user, self.tenant)

        # Own-tenant asset — should be found.
        resp = self.client.get(self.url, {"code": "SCAN-001"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])

        # Other tenant's asset — must NOT be visible.
        resp2 = self.client.get(self.url, {"code": "OTHER-999"})
        self.assertIn(resp2.status_code, (404, 403))
        data2 = json.loads(resp2.content)
        self.assertFalse(data2.get("found"))
