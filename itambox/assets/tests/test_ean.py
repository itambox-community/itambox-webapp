"""Tests for the EAN field + barcode-scanner resolution.

Scanning an asset-type EAN returns the asset list filtered by that EAN; scanning
a component/accessory/consumable EAN goes to that item's detail. Resolution is
tenant-scoped and permission-gated.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.filters import AssetFilterSet
from assets.models import Asset, AssetRole, AssetType, Manufacturer, StatusLabel
from compliance.audit_services import expected_assets_queryset
from core.tests.mixins import TenantTestMixin, grant
from inventory.models import Accessory, Component, Consumable

User = get_user_model()


class EanScanTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="ean")
        self.tenant_role.permissions = ["compliance.view_auditsession", "compliance.add_assetaudit"]
        self.tenant_role.save()
        self.admin_grant = grant(self.tenant_admin, self.tenant, self.tenant_role)
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.mfr = Manufacturer.objects.create(name="EANMfr", slug="eanmfr")
        self.role = AssetRole.objects.create(name="EANRole", slug="eanrole")
        self.status = StatusLabel.objects.create(name="Avail", slug="ean-avail", type="deployable")
        self.atype = AssetType.objects.create(
            manufacturer=self.mfr,
            model="EAN Model",
            slug="ean-model",
            ean="4012345678901",
        )
        self.asset = Asset.objects.create(
            name="EAN Asset",
            asset_tag="EAN-A1",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )
        self.component = Component.objects.create(
            name="RAM", slug="ram-ean", manufacturer=self.mfr, ean="1111111111116", tenant=self.tenant
        )
        self.accessory = Accessory.objects.create(
            name="Mouse", slug="mouse-ean", manufacturer=self.mfr, ean="2222222222226", tenant=self.tenant
        )
        self.consumable = Consumable.objects.create(
            name="Toner", slug="toner-ean", manufacturer=self.mfr, ean="3333333333336", tenant=self.tenant
        )
        self.url = reverse("scan_resolve")

    def _resolve(self, code):
        resp = self.client.get(self.url, {"code": code})
        return resp, (json.loads(resp.content) if resp.content else {})

    def test_assettype_ean_returns_filtered_asset_list(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp, data = self._resolve("4012345678901")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["found"])
        self.assertIn("/assets/", data["url"])
        self.assertIn("ean=4012345678901", data["url"])

    def test_component_ean_returns_detail(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        _, data = self._resolve("1111111111116")
        self.assertEqual(data["url"], self.component.get_absolute_url())

    def test_accessory_ean_returns_detail(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        _, data = self._resolve("2222222222226")
        self.assertEqual(data["url"], self.accessory.get_absolute_url())

    def test_consumable_ean_returns_detail(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        _, data = self._resolve("3333333333336")
        self.assertEqual(data["url"], self.consumable.get_absolute_url())

    def test_unknown_ean_not_found(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp, data = self._resolve("9999999999994")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(data["found"])

    def test_asset_tag_still_resolves_first(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        _, data = self._resolve("EAN-A1")
        self.assertEqual(data["url"], self.asset.get_absolute_url())

    def test_component_ean_gated_by_permission(self):
        # view_asset but NOT inventory.view_component → component EAN must not resolve.
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp, data = self._resolve("1111111111116")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(data["found"])

    def test_cross_tenant_component_not_resolved(self):
        from organization.models import Tenant

        other = Tenant.objects.create(name="OtherEan", slug="other-ean")
        Component.objects.create(
            name="Other RAM", slug="other-ram-ean", manufacturer=self.mfr, ean="7777777777776", tenant=other
        )
        self.client_login_to_tenant(
            self.tenant_admin, self.tenant
        )  # superuser resolves global; check scoped member instead
        self.tenant_role.permissions = ["assets.view_asset", "inventory.view_component"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp, data = self._resolve("7777777777776")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(data["found"])

    def test_asset_ean_filter(self):
        qs = AssetFilterSet({"ean": "4012345678901"}, queryset=Asset.objects.all()).qs
        self.assertIn(self.asset, qs)
        qs_none = AssetFilterSet({"ean": "0000"}, queryset=Asset.objects.all()).qs
        self.assertNotIn(self.asset, qs_none)

    # ─────────────────────────────────────────────────────────────────────────
    # EAN resolution in audit/bulk flows (resolve_scanned_code without user)
    # ─────────────────────────────────────────────────────────────────────────

    def test_unique_ean_resolves_asset_in_scope(self):
        from assets.scanning import resolve_scanned_asset, resolve_scanned_code

        with self.tenant_context(self.tenant, self.tenant_membership):
            self.assertEqual(resolve_scanned_code("4012345678901"), self.asset)
            asset, ambiguous = resolve_scanned_asset("4012345678901")
            self.assertEqual(asset, self.asset)
            self.assertFalse(ambiguous)

    def test_ambiguous_ean_flagged_in_scope(self):
        from assets.scanning import resolve_scanned_asset, resolve_scanned_code

        Asset.objects.create(
            name="EAN Asset 2",
            asset_tag="EAN-A2",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )
        with self.tenant_context(self.tenant, self.tenant_membership):
            asset, ambiguous = resolve_scanned_asset("4012345678901")
            self.assertIsNone(asset)
            self.assertTrue(ambiguous)
            # Never silently picks one of the two assets.
            self.assertIsNone(resolve_scanned_code("4012345678901"))

    def test_audit_scan_http_resolves_unique_ean(self):
        """POSTing an AssetType EAN to an audit session verifies the single asset of that type."""
        from compliance.models import AssetAudit, AuditSession
        from organization.models import Location, Site

        site = Site.objects.create(name="EAN HQ", slug="ean-hq", tenant=self.tenant)
        loc = Location.objects.create(name="EAN Room", slug="ean-room", site=site, tenant=self.tenant)
        session = AuditSession.objects.create(
            name="EAN Audit", location=loc, status="active", created_by=self.tenant_admin
        )
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("compliance:auditsession_scan", kwargs={"pk": session.pk}),
            data={"barcode": "4012345678901"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(AssetAudit.objects.filter(session=session, asset=self.asset).exists())

    def test_audit_scan_http_ambiguous_ean_reports_ambiguity(self):
        """An EAN matching several assets returns a distinct error, not a false verify."""
        from compliance.models import AuditSession
        from organization.models import Location, Site

        Asset.objects.create(
            name="EAN Asset 2",
            asset_tag="EAN-A2",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            tenant=self.tenant,
        )
        site = Site.objects.create(name="EAN HQ 2", slug="ean-hq-2", tenant=self.tenant)
        loc = Location.objects.create(name="EAN Room 2", slug="ean-room-2", site=site, tenant=self.tenant)
        session = AuditSession.objects.create(
            name="EAN Audit 2", tenant=self.tenant, location=loc, status="active", created_by=self.tenant_admin
        )
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("compliance:auditsession_scan", kwargs={"pk": session.pk}),
            data={"barcode": "4012345678901"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"matches several assets", resp.content)


class AuditScanClassificationTests(TenantTestMixin, TestCase):
    """Branch coverage for the audit scan classification + validate endpoint."""

    def setUp(self):
        from compliance.models import AuditSession
        from organization.models import Location, Site

        self.setup_tenant_context(slug="audit-class")
        self.tenant_role.permissions = ["compliance.view_auditsession", "compliance.add_assetaudit"]
        self.tenant_role.save()
        self.admin_grant = grant(self.tenant_admin, self.tenant, self.tenant_role)
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.mfr = Manufacturer.objects.create(name="AuditMfr", slug="auditmfr")
        self.role = AssetRole.objects.create(name="AuditRole", slug="auditrole")
        self.status = StatusLabel.objects.create(name="Avail", slug="audit-avail", type="deployable")
        self.archived_status = StatusLabel.objects.create(name="Arch", slug="audit-arch", type="archived")
        self.atype = AssetType.objects.create(manufacturer=self.mfr, model="Audit Model", slug="audit-model")
        self.site = Site.objects.create(name="Audit Site", slug="audit-site", tenant=self.tenant)
        self.loc1 = Location.objects.create(name="Room 1", slug="audit-room-1", site=self.site, tenant=self.tenant)
        self.loc2 = Location.objects.create(name="Room 2", slug="audit-room-2", site=self.site, tenant=self.tenant)
        self.session = AuditSession.objects.create(
            name="Audit S", location=self.loc1, tenant=self.tenant, status="active", created_by=self.tenant_admin
        )
        self.asset = Asset.objects.create(
            name="Audit A",
            asset_tag="AUD-1",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            location=self.loc1,
            tenant=self.tenant,
        )
        self.expected_ids = set(
            expected_assets_queryset(self.session, user=self.tenant_admin).values_list("id", flat=True)
        )
        self.validate_url = reverse("compliance:auditsession_validate", kwargs={"pk": self.session.pk})
        self.scan_url = reverse("compliance:auditsession_scan", kwargs={"pk": self.session.pk})

    def _classify(self, asset=None, observed="__session__"):
        from compliance.views_audit import _classify_audit_scan

        if observed == "__session__":
            observed = self.session.location
        return _classify_audit_scan(self.session, asset or self.asset, self.expected_ids, observed)

    # ── _classify_audit_scan branch coverage ─────────────────────────────────

    def test_no_observed_location(self):
        eligible, warning, classification = self._classify(observed=None)
        self.assertFalse(eligible)
        self.assertEqual(classification, "unknown")
        self.assertIn("location", warning)

    def test_archived_asset(self):
        self.asset.status = self.archived_status
        self.asset.save()
        eligible, _warning, classification = self._classify()
        self.assertFalse(eligible)
        self.assertEqual(classification, "unknown")

    def test_already_verified_not_expected_surprise(self):
        from compliance.audit_services import audit_asset

        other = Asset.objects.create(
            name="Other Audit Asset",
            asset_tag="AUD-2",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.status,
            location=self.loc2,
            tenant=self.tenant,
        )
        audit_asset(
            asset=other,
            user=self.tenant_admin,
            session=self.session,
            location=self.loc2,
            status=self.status,
            verification_method="barcode",
        )
        eligible, warning, classification = self._classify(asset=other, observed=self.loc2)
        self.assertFalse(eligible)
        self.assertEqual(classification, "surprise")
        self.assertIn("already", warning)

    def test_already_verified_matched(self):
        from compliance.audit_services import audit_asset

        audit_asset(
            asset=self.asset,
            user=self.tenant_admin,
            session=self.session,
            location=self.loc1,
            status=self.status,
            verification_method="barcode",
        )
        eligible, _warning, classification = self._classify()
        self.assertFalse(eligible)
        self.assertEqual(classification, "matched")

    def test_already_verified_mismatch(self):
        from compliance.audit_services import audit_asset

        audit_asset(
            asset=self.asset,
            user=self.tenant_admin,
            session=self.session,
            location=self.loc1,
            status=self.status,
            verification_method="barcode",
        )
        # Observed location differs from the session location → mismatch branch.
        eligible, _warning, classification = self._classify(observed=self.loc2)
        self.assertFalse(eligible)
        self.assertEqual(classification, "mismatch")

    def test_expected_match(self):
        eligible, warning, classification = self._classify()
        self.assertTrue(eligible)
        self.assertIsNone(warning)
        self.assertEqual(classification, "matched")

    def test_expected_mismatch(self):
        eligible, _warning, classification = self._classify(observed=self.loc2)
        self.assertTrue(eligible)
        self.assertEqual(classification, "mismatch")

    # ── validate endpoint: ambiguity payload ─────────────────────────────────

    def test_validate_endpoint_ambiguous_ean(self):
        ean_type = AssetType.objects.create(
            manufacturer=self.mfr, model="EAN Class Model", slug="audit-ean-class", ean="4012345678911"
        )
        for i in range(2):
            Asset.objects.create(
                name=f"EAN Class {i}",
                asset_tag=f"AUD-E{i}",
                asset_type=ean_type,
                asset_role=self.role,
                status=self.status,
                location=self.loc1,
                tenant=self.tenant,
            )
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.validate_url, {"code": "4012345678911"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data["found"])
        self.assertTrue(data["ambiguous"])

    def test_scan_endpoint_unknown_code_message(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(self.scan_url, data={"barcode": "NOPE-9999"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"not found", resp.content)
