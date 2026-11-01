"""Tests for the scanner-driven bulk check-in / disposal feature.

Covers:
- AssetScanActionResolveView (payload, eligibility, permissions, tenant scope)
- bulk_checkin_assets / bulk_dispose_assets submit views (Job + enqueue args)
- bulk_checkin_task / bulk_dispose_task (state changes, skips, partial results)
- the basket page views (render + list-view seeding)
"""

import json
import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.models import (
    Asset,
    AssetDisposal,
    AssetRole,
    AssetType,
    Manufacturer,
    StatusLabel,
)
from assets.services import checkout_asset, dispose_asset
from assets.tasks.checkin import bulk_checkin_task
from assets.tasks.checkout import bulk_checkout_task
from assets.tasks.disposal import bulk_dispose_task
from core.models import Job
from core.tasks.utils import TaskStatus
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Location, Site, Tenant

User = get_user_model()


def _fixtures(suffix=""):
    mfr = Manufacturer.objects.create(name=f"Mfr{suffix}", slug=f"mfr{suffix}")
    role = AssetRole.objects.create(name=f"Role{suffix}", slug=f"role{suffix}")
    atype = AssetType.objects.create(manufacturer=mfr, model=f"Model{suffix}", slug=f"type{suffix}")
    deployable = StatusLabel.objects.create(name=f"Deployable{suffix}", slug=f"deployable{suffix}", type="deployable")
    deployed = StatusLabel.objects.create(name=f"Deployed{suffix}", slug=f"deployed{suffix}", type="deployed")
    archived = StatusLabel.objects.create(name=f"Archived{suffix}", slug=f"archived{suffix}", type="archived")
    return role, atype, deployable, deployed, archived


# ─────────────────────────────────────────────────────────────────────────────
# Resolve-for-action endpoint
# ─────────────────────────────────────────────────────────────────────────────


class ScanActionResolveTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="resolve")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-r")
        self.asset = Asset.objects.create(
            name="Resolve Asset",
            asset_tag="RES-001",
            serial_number="SN-RES-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        self.url = reverse("assets:asset_scan_resolve_action")

    def test_checkin_payload(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "checkin"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])
        self.assertEqual(data["pk"], self.asset.pk)
        self.assertEqual(data["asset_tag"], "RES-001")
        # Not checked out → eligible False with a warning (still resolvable).
        self.assertFalse(data["eligible"])
        self.assertTrue(data["warning"])

    def test_payload_carries_tenant_identity(self):
        """Every resolved row names its own tenant so the basket can key state by it."""
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "checkin"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["tenant_id"], self.tenant.pk)

    def test_no_accessible_tenants_fails_closed(self):
        """A member with no accessible tenants gets the fail-closed 404."""
        user = User.objects.create_user(
            username="resolve-noaccess", email="resolve-noaccess@example.com", password="password"
        )
        self.client.force_login(user)
        resp = self.client.get(self.url, {"code": "RES-001"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(json.loads(resp.content)["found"])

    def test_ambiguous_ean_reports_ambiguity(self):
        """An EAN matching several assets is reported distinctly, never resolved silently."""
        mfr = Manufacturer.objects.get(slug="mfr-r")
        ean_type = AssetType.objects.create(
            manufacturer=mfr, model="EAN Bulk Model", slug="type-ean-bulk", ean="4012345678912"
        )
        for i in range(2):
            Asset.objects.create(
                name=f"EAN Bulk {i}",
                asset_tag=f"EANB-{i}",
                asset_type=ean_type,
                asset_role=self.role,
                status=self.deployable,
                tenant=self.tenant,
            )
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "4012345678912"})
        self.assertEqual(resp.status_code, 404)
        data = json.loads(resp.content)
        self.assertFalse(data["found"])
        self.assertTrue(data["ambiguous"])

    def test_checkin_eligible_when_checked_out(self):
        holder = AssetHolder.objects.create(
            first_name="A", last_name="B", upn="ab@x.io", email="ab@x.io", tenant=self.tenant
        )
        checkout_asset(self.asset, holder=holder, user=self.tenant_admin)
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "checkin"})
        data = json.loads(resp.content)
        self.assertTrue(data["eligible"])
        self.assertIn("A", data["assigned_to"])

    def test_dispose_payload_has_book_value_field(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "dispose"})
        data = json.loads(resp.content)
        self.assertTrue(data["found"])
        self.assertTrue(data["eligible"])
        self.assertIn("book_value", data)

    def test_dispose_already_disposed_is_ineligible(self):
        dispose_asset(self.asset, disposal_method="recycle", disposal_date="2026-06-19", user=self.tenant_admin)
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "dispose"})
        data = json.loads(resp.content)
        self.assertTrue(data["found"])
        self.assertFalse(data["eligible"])
        self.assertTrue(data["warning"])

    def test_not_found(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "NOPE", "mode": "checkin"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(json.loads(resp.content)["found"])

    def test_checkin_requires_change_asset(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "checkin"})
        self.assertEqual(resp.status_code, 403)

    def test_dispose_requires_add_assetdisposal(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "dispose"})
        self.assertEqual(resp.status_code, 403)

    def test_member_with_perms_resolves_own_tenant(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(self.url, {"code": "RES-001", "mode": "checkin"})
        self.assertEqual(resp.status_code, 200)

    def test_cross_tenant_isolation(self):
        other = Tenant.objects.create(name="Other", slug="other-resolve")
        Asset.objects.create(
            name="Other",
            asset_tag="OTH-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=other,
        )
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.url, {"code": "OTH-001", "mode": "checkin"})
        self.assertEqual(resp.status_code, 404)

        # A regular scoped member cannot see it either.
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp2 = self.client.get(self.url, {"code": "OTH-001", "mode": "checkin"})
        self.assertEqual(resp2.status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Submit views — Job creation + enqueue arguments
# ─────────────────────────────────────────────────────────────────────────────


class BulkScanSubmitViewTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="submit")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-s")
        self.a1 = Asset.objects.create(
            name="A1",
            asset_tag="S-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        self.a2 = Asset.objects.create(
            name="A2",
            asset_tag="S-002",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )

    @patch("assets.views.bulk_scan_views.async_task")
    def test_bulk_checkin_enqueues(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {
                "pk": [self.a1.pk, self.a2.pk],
                "notes": "returned to stock",
            },
        )
        self.assertEqual(resp.status_code, 302)
        job = Job.objects.filter(name__contains="Bulk Check-in").first()
        self.assertIsNotNone(job)
        self.assertEqual(job.status, Job.STATUS_PENDING)
        mock_async.assert_called_once()
        args = mock_async.call_args[0]
        self.assertEqual(args[0], "assets.tasks.checkin.bulk_checkin_task")
        self.assertEqual(args[1], job.pk)
        self.assertEqual(args[2], [str(self.a1.pk), str(self.a2.pk)])
        self.assertEqual(args[3], self.tenant_admin.pk)
        self.assertEqual(args[4], self.tenant.pk)

    @override_settings(Q_CLUSTER={"sync": False})
    @patch("assets.views.bulk_scan_views.async_task")
    def test_bulk_checkin_async_enqueue_waits_for_commit(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(
                reverse("assets:asset_bulk_checkin"),
                {"pk": [self.a1.pk], "notes": "queued after commit"},
            )
            self.assertEqual(response.status_code, 302)
            mock_async.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args.args[0], "assets.tasks.checkin.bulk_checkin_task")

    @patch("assets.views.bulk_scan_views.async_task")
    def test_bulk_dispose_enqueues_with_proceeds_map(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("assets:asset_bulk_dispose"),
            {
                "pk": [self.a1.pk, self.a2.pk],
                "disposal_method": "recycle",
                "disposal_date": "2026-06-19",
                "data_sanitization_method": "nist_purge",
                "currency": "EUR",
                "weee_compliant": "on",
                f"proceeds_{self.a1.pk}": "50.00",
            },
        )
        self.assertEqual(resp.status_code, 302)
        job = Job.objects.filter(name__contains="Bulk Disposal").first()
        self.assertIsNotNone(job)
        mock_async.assert_called_once()
        args = mock_async.call_args[0]
        self.assertEqual(args[0], "assets.tasks.disposal.bulk_dispose_task")
        disposal_kwargs = args[5]
        proceeds_map = args[6]
        self.assertEqual(disposal_kwargs["disposal_method"], "recycle")
        self.assertTrue(disposal_kwargs["weee_compliant"])
        self.assertEqual(proceeds_map[str(self.a1.pk)], "50.00")
        self.assertIsNone(proceeds_map[str(self.a2.pk)])

    @patch("assets.views.bulk_scan_views.async_task")
    def test_dispose_requires_date(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("assets:asset_bulk_dispose"),
            {
                "pk": [self.a1.pk],
                "disposal_method": "recycle",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_async.assert_not_called()
        self.assertFalse(Job.objects.exists())

    @patch("assets.views.bulk_scan_views.async_task")
    def test_no_selection_redirects(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(reverse("assets:asset_bulk_checkin"), {})
        self.assertEqual(resp.status_code, 302)
        mock_async.assert_not_called()

    def test_checkin_permission_denied(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.post(reverse("assets:asset_bulk_checkin"), {"pk": [self.a1.pk]})
        self.assertEqual(resp.status_code, 403)


# ─────────────────────────────────────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────────────────────────────────────


class BulkCheckinTaskTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="ci-task")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-ci")
        self.holder = AssetHolder.objects.create(
            first_name="H", last_name="X", upn="hx@x.io", email="hx@x.io", tenant=self.tenant
        )
        self.checked_out = Asset.objects.create(
            name="CO",
            asset_tag="CI-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        checkout_asset(self.checked_out, holder=self.holder, user=self.tenant_admin)
        self.idle = Asset.objects.create(
            name="Idle",
            asset_tag="CI-002",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )

    def _job(self):
        from django.contrib.contenttypes.models import ContentType

        return Job.objects.create(
            name="t", tenant=self.tenant, model=ContentType.objects.get_for_model(Asset), status=Job.STATUS_PENDING
        )

    def test_checks_in_and_skips(self):
        job = self._job()
        bulk_checkin_task(job.pk, [str(self.checked_out.pk), str(self.idle.pk)], self.tenant_admin.pk, self.tenant.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertEqual(job.result["checked_in"], 1)
        self.assertEqual(job.result["skipped"], 1)
        self.checked_out.refresh_from_db()
        self.assertIsNone(self.checked_out.active_assignment)

    def test_status_override(self):
        job = self._job()
        bulk_checkin_task(
            job.pk, [str(self.checked_out.pk)], self.tenant_admin.pk, self.tenant.pk, status_id=self.archived.pk
        )
        self.checked_out.refresh_from_db()
        self.assertEqual(self.checked_out.status, self.archived)

    def test_blank_location_preserves_current_location(self):
        site = Site.objects.create(name="HQ", slug="hq-ci")
        loc = Location.objects.create(name="Shelf 1", slug="shelf-1-ci", site=site, tenant=self.tenant)
        asset = Asset.objects.create(
            name="Loc",
            asset_tag="CI-LOC",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        checkout_asset(asset, location=loc, user=self.tenant_admin)
        asset.refresh_from_db()
        self.assertEqual(asset.location, loc)
        job = self._job()
        bulk_checkin_task(job.pk, [str(asset.pk)], self.tenant_admin.pk, self.tenant.pk)  # no return location
        asset.refresh_from_db()
        self.assertEqual(asset.location, loc)  # preserved, not wiped to NULL

    def test_provided_location_is_applied(self):
        site = Site.objects.create(name="HQ2", slug="hq2-ci")
        loc = Location.objects.create(name="Shelf A", slug="shelf-a-ci", site=site, tenant=self.tenant)
        dest = Location.objects.create(name="Shelf B", slug="shelf-b-ci", site=site, tenant=self.tenant)
        asset = Asset.objects.create(
            name="Loc2",
            asset_tag="CI-LOC2",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        checkout_asset(asset, location=loc, user=self.tenant_admin)
        job = self._job()
        bulk_checkin_task(job.pk, [str(asset.pk)], self.tenant_admin.pk, self.tenant.pk, location_id=dest.pk)
        asset.refresh_from_db()
        self.assertEqual(asset.location, dest)

    def test_missing_and_cancelled_jobs_return_typed_outcomes(self):
        missing = bulk_checkin_task(999999, [], self.tenant_admin.pk, self.tenant.pk)
        cancelled_job = self._job()
        cancelled_job.status = Job.STATUS_FAILED
        cancelled_job.save(update_fields=["status"])

        skipped = bulk_checkin_task(cancelled_job.pk, [], self.tenant_admin.pk, self.tenant.pk)

        self.assertEqual((missing.status, missing.code), (TaskStatus.TERMINAL, "checkin.job_not_found"))
        self.assertEqual((skipped.status, skipped.code), (TaskStatus.SKIPPED, "checkin.job_not_pending"))

    @patch("assets.services.checkin_asset", side_effect=RuntimeError("secret-person secret-asset"))
    def test_item_failures_are_terminal_and_redacted(self, _checkin):
        job = self._job()

        with self.assertLogs("assets.tasks.checkin", level="WARNING") as captured:
            result = bulk_checkin_task(job.pk, [str(self.checked_out.pk)], self.tenant_admin.pk, self.tenant.pk)

        job.refresh_from_db()
        rendered = " ".join(captured.output) + " " + job.logs
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "checkin.all_failed"))
        self.assertEqual(dict(result.counts), {"failed": 1, "total": 1})
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertNotIn("secret-person", rendered)
        self.assertNotIn("secret-asset", rendered)

    def test_item_failure_after_success_returns_partial(self):
        job = self._job()

        result = bulk_checkin_task(
            job.pk,
            [str(self.checked_out.pk), "999999"],
            self.tenant_admin.pk,
            self.tenant.pk,
        )

        self.assertEqual((result.status, result.code), (TaskStatus.PARTIAL, "checkin.partial"))
        self.assertEqual(dict(result.counts), {"checked_in": 1, "skipped": 0, "failed": 1})

    @patch("assets.models.StatusLabel.objects.filter", side_effect=ValueError("secret-boundary"))
    def test_boundary_failure_is_terminal_and_redacted(self, _filter):
        job = self._job()

        result = bulk_checkin_task(job.pk, [], self.tenant_admin.pk, self.tenant.pk, status_id=self.archived.pk)

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "checkin.boundary_failed"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertNotIn("secret-boundary", job.logs)

    @patch("assets.tasks.checkin.Job.objects.get", side_effect=OperationalError("secret-database"))
    def test_entry_database_failure_is_retryable_and_redacted(self, _get):
        with self.assertLogs("assets.tasks.checkin", level="ERROR") as captured:
            result = bulk_checkin_task(17, [], self.tenant_admin.pk, self.tenant.pk)

        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "checkin.entry_failed"))
        self.assertNotIn("secret-database", " ".join(captured.output))


class BulkDisposeTaskTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="dz-task")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-dz")
        self.a1 = Asset.objects.create(
            name="D1",
            asset_tag="DZ-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        self.a2 = Asset.objects.create(
            name="D2",
            asset_tag="DZ-002",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )

    def _job(self):
        from django.contrib.contenttypes.models import ContentType

        return Job.objects.create(
            name="t", tenant=self.tenant, model=ContentType.objects.get_for_model(Asset), status=Job.STATUS_PENDING
        )

    def _kwargs(self):
        return {
            "disposal_method": "recycle",
            "disposal_date": "2026-06-19",
            "data_sanitization_method": "nist_purge",
            "sanitization_certificate": "C1",
            "sanitized_by": "Acme",
            "recipient": "Recycler",
            "currency": "EUR",
            "weee_compliant": True,
            "notes": "bulk",
        }

    def test_disposes_with_per_asset_proceeds(self):
        job = self._job()
        bulk_dispose_task(
            job.pk,
            [str(self.a1.pk), str(self.a2.pk)],
            self.tenant_admin.pk,
            self.tenant.pk,
            disposal_kwargs=self._kwargs(),
            proceeds_map={str(self.a1.pk): "50.00"},
        )
        job.refresh_from_db()
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertEqual(job.result["disposed"], 2)
        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertIsNotNone(self.a1.disposed_at)
        self.assertEqual(self.a1.disposal_value, Decimal("50.00"))
        self.assertEqual(AssetDisposal.objects.get(asset=self.a1).proceeds, Decimal("50.00"))
        # a2 had no proceeds → disposal record proceeds is None
        self.assertIsNone(AssetDisposal.objects.get(asset=self.a2).proceeds)

    def test_skips_already_disposed(self):
        dispose_asset(self.a1, disposal_method="donation", disposal_date="2026-01-01", user=self.tenant_admin)
        original = AssetDisposal.objects.get(asset=self.a1)
        job = self._job()
        bulk_dispose_task(
            job.pk,
            [str(self.a1.pk), str(self.a2.pk)],
            self.tenant_admin.pk,
            self.tenant.pk,
            disposal_kwargs=self._kwargs(),
            proceeds_map={},
        )
        job.refresh_from_db()
        self.assertEqual(job.result["disposed"], 1)
        self.assertEqual(job.result["skipped"], 1)
        # The original disposal must be untouched (method/date preserved).
        refreshed = AssetDisposal.objects.get(asset=self.a1)
        self.assertEqual(refreshed.disposal_method, "donation")
        self.assertEqual(refreshed.pk, original.pk)

    def test_missing_and_cancelled_jobs_return_typed_outcomes(self):
        missing = bulk_dispose_task(999999, [], self.tenant_admin.pk, self.tenant.pk)
        cancelled_job = self._job()
        cancelled_job.status = Job.STATUS_FAILED
        cancelled_job.save(update_fields=["status"])

        skipped = bulk_dispose_task(cancelled_job.pk, [], self.tenant_admin.pk, self.tenant.pk)

        self.assertEqual((missing.status, missing.code), (TaskStatus.TERMINAL, "disposal.job_not_found"))
        self.assertEqual((skipped.status, skipped.code), (TaskStatus.SKIPPED, "disposal.job_not_pending"))

    @patch("assets.services.dispose_asset", side_effect=RuntimeError("secret-disposal-payload"))
    def test_item_failures_are_terminal_and_redacted(self, _dispose):
        job = self._job()

        with self.assertLogs("assets.tasks.disposal", level="WARNING") as captured:
            result = bulk_dispose_task(
                job.pk,
                [str(self.a1.pk)],
                self.tenant_admin.pk,
                self.tenant.pk,
                disposal_kwargs=self._kwargs(),
            )

        job.refresh_from_db()
        rendered = " ".join(captured.output) + " " + job.logs
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "disposal.all_failed"))
        self.assertEqual(dict(result.counts), {"failed": 1})
        self.assertNotIn("secret-disposal-payload", rendered)

    def test_item_failure_after_success_returns_partial(self):
        job = self._job()

        result = bulk_dispose_task(
            job.pk,
            [str(self.a1.pk), "999999"],
            self.tenant_admin.pk,
            self.tenant.pk,
            disposal_kwargs=self._kwargs(),
        )

        self.assertEqual((result.status, result.code), (TaskStatus.PARTIAL, "disposal.partial"))
        self.assertEqual(dict(result.counts), {"disposed": 1, "skipped": 0, "failed": 1})

    @patch("assets.tasks.disposal._parse_date", side_effect=ValueError("secret-date"))
    def test_boundary_failure_returns_terminal_result(self, _parse):
        job = self._job()

        result = bulk_dispose_task(job.pk, [], self.tenant_admin.pk, self.tenant.pk, disposal_kwargs=self._kwargs())

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "disposal.boundary_failed"))
        self.assertNotIn("secret-date", job.logs)

    @patch("assets.tasks.disposal.Notification.objects.create", side_effect=ValueError("secret-notification"))
    def test_boundary_notification_failure_is_redacted(self, _create):
        job = self._job()

        with self.assertLogs("assets.tasks.disposal", level="ERROR") as captured:
            result = bulk_dispose_task(
                job.pk,
                [str(self.a1.pk)],
                self.tenant_admin.pk,
                self.tenant.pk,
                disposal_kwargs=self._kwargs(),
            )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "disposal.entry_failed"))
        self.assertNotIn("secret-notification", " ".join(captured.output) + " " + job.logs)

    @patch("assets.tasks.disposal.Job.objects.get", side_effect=OperationalError("secret-database"))
    def test_entry_database_failure_is_retryable(self, _get):
        result = bulk_dispose_task(17, [], self.tenant_admin.pk, self.tenant.pk)

        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "disposal.entry_failed"))


# ─────────────────────────────────────────────────────────────────────────────
# Basket pages
# ─────────────────────────────────────────────────────────────────────────────


class BulkScanPageTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="page")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-pg")
        self.asset = Asset.objects.create(
            name="Seed",
            asset_tag="PG-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )

    def test_checkin_page_renders(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_checkin_scan"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "scan-basket-root")

    def test_dispose_page_renders(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_dispose_scan"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "scan-basket-root")

    def test_page_seeds_from_pk_querystring(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_checkin_scan"), {"pk": self.asset.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PG-001")

    def test_seed_payloads_carry_tenant_identity(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_checkin_scan"), {"pk": self.asset.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([p["tenant_id"] for p in resp.context["seed_payloads"]], [self.tenant.pk])

    def test_page_permission_denied(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_dispose_scan"))
        self.assertEqual(resp.status_code, 403)

    def test_tenant_field_is_hidden_for_explicit_tenant_scope(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_checkin_scan"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="tenant"')
        self.assertContains(resp, 'type="hidden"')

    def test_explicit_scope_tenant_form_initial_is_bound(self):
        from assets.forms.bulk_scan_forms import AssetBulkCheckInForm

        request = SimpleNamespace(
            user=self.tenant_user,
            active_all_accessible=False,
            active_tenant=self.tenant,
        )
        form = AssetBulkCheckInForm(request=request)

        self.assertEqual(form.fields["tenant"].initial, self.tenant.pk)

    def test_anonymous_request_has_no_bulk_tenant_choices(self):
        from assets.forms.bulk_scan_forms import bulk_tenant_queryset

        request = SimpleNamespace(user=AnonymousUser())

        self.assertFalse(bulk_tenant_queryset(request).exists())

    def test_bulk_submission_without_resolved_tenant_is_forbidden(self):
        from assets.forms.bulk_scan_forms import AssetBulkCheckInForm
        from assets.views.bulk_scan_views import _submission_tenant

        request = SimpleNamespace(
            user=self.tenant_user,
            active_all_accessible=False,
            active_tenant=None,
            POST={},
        )
        with self.tenant_context(None):
            tenant, outcome, error = _submission_tenant(request, AssetBulkCheckInForm, "assets.change_asset")

        self.assertIsNone(tenant)
        self.assertEqual(outcome, "forbidden")
        self.assertIsNone(error)

    def test_all_accessible_scope_renders_tenant_selector(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.get(reverse("assets:asset_bulk_checkin_scan"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="tenant"')
        self.assertContains(resp, "required")
        self.assertContains(resp, self.tenant.name)

    def test_all_accessible_scan_requires_target_tenant(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        missing = self.client.get(
            reverse("assets:asset_scan_resolve_action"),
            {"code": "PG-001", "mode": "checkin"},
        )
        selected = self.client.get(
            reverse("assets:asset_scan_resolve_action"),
            {"code": "PG-001", "mode": "checkin", "tenant": self.tenant.pk},
        )

        self.assertEqual(missing.status_code, 400)
        self.assertTrue(json.loads(missing.content)["tenant_required"])
        self.assertEqual(selected.status_code, 200)
        self.assertTrue(json.loads(selected.content)["found"])

    def test_all_accessible_missing_tenant_redirects_without_job(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"pk": [self.asset.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())

    def test_all_accessible_missing_tenant_redirects_disposal(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.add_assetdisposal"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_dispose"),
            {"pk": [self.asset.pk], "disposal_date": "2026-08-18"},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())

    def test_all_accessible_missing_tenant_redirects_checkout(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkout"),
            {"pk": [self.asset.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())

    def test_all_accessible_target_without_permission_is_forbidden(self):
        other = Tenant.objects.create(name="Other", slug="page-other")
        other_role = self.tenant_role.__class__.objects.create(
            tenant=other,
            name="Read-only",
            permissions=["assets.view_asset"],
        )
        self.grant(self.tenant_user, other, other_role)
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": other.pk, "pk": [self.asset.pk]},
        )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Job.objects.exists())

    def test_aggregate_bulk_page_accepts_tenant_post_before_method_rejection(self):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin_scan"),
            {"tenant": self.tenant.pk},
        )

        self.assertEqual(resp.status_code, 405)

    @patch("assets.views.bulk_scan_views.async_task")
    def test_all_accessible_submit_uses_selected_tenant(self, mock_async):
        self.tenant_role.permissions = ["assets.view_asset", "assets.change_asset"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset.pk], "notes": "selected tenant"},
        )

        self.assertEqual(resp.status_code, 302)
        job = Job.objects.filter(name__contains="Bulk Check-in").first()
        self.assertIsNotNone(job)
        self.assertEqual(job.tenant_id, self.tenant.pk)
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][4], self.tenant.pk)

    @patch("assets.views.bulk_scan_views.async_task")
    def test_all_accessible_disposal_submit_uses_selected_tenant(self, mock_async):
        """Aggregate scope: a disposal submitted for the selected tenant reaches the task."""
        self.tenant_role.permissions = ["assets.view_asset", "assets.add_assetdisposal"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_dispose"),
            {"tenant": self.tenant.pk, "pk": [self.asset.pk], "disposal_date": "2026-08-18"},
        )

        self.assertEqual(resp.status_code, 302)
        job = Job.objects.filter(name__contains="Bulk Disposal").first()
        self.assertIsNotNone(job)
        self.assertEqual(job.tenant_id, self.tenant.pk)
        mock_async.assert_called_once()

    @patch("assets.views.bulk_scan_views.async_task")
    def test_all_accessible_disposal_never_submits_a_foreign_tenant_asset(self, mock_async):
        """Aggregate scope: an asset of a tenant with read-only access is not disposed (#496)."""
        other = Tenant.objects.create(name="Other disposal", slug="dispose-other")
        other_role = self.tenant_role.__class__.objects.create(
            tenant=other,
            name="Read-only",
            permissions=["assets.view_asset"],
        )
        self.grant(self.tenant_user, other, other_role)
        foreign_asset = Asset.objects.create(
            name="Foreign disposal asset",
            asset_tag="FOREIGN-496",
            status=self.asset.status,
            asset_type=self.asset.asset_type,
            tenant=other,
        )
        self.tenant_role.permissions = ["assets.view_asset", "assets.add_assetdisposal"]
        self.tenant_role.save()
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        resp = self.client.post(
            reverse("assets:asset_bulk_dispose"),
            {
                "tenant": self.tenant.pk,
                "pk": [self.asset.pk, foreign_asset.pk],
                "disposal_date": "2026-08-18",
            },
        )

        self.assertIn(resp.status_code, (302,))
        self.assertFalse(
            Job.objects.filter(
                name__contains="Bulk Disposal",
            )
            .exclude(tenant_id=self.tenant.pk)
            .exists()
        )

        def _flatten(value):
            if isinstance(value, (list, tuple)):
                for item in value:
                    yield from _flatten(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from _flatten(item)
            else:
                yield value

        submitted = list(_flatten(mock_async.call_args[0])) if mock_async.called else []
        if submitted:
            self.assertIn(self.asset.pk, submitted)
            self.assertNotIn(foreign_asset.pk, submitted)
        foreign_asset.refresh_from_db()
        self.assertIsNone(foreign_asset.disposed_at)
        self.assertFalse(AssetDisposal.all_objects.filter(asset_id=foreign_asset.pk).exists())


# ─────────────────────────────────────────────────────────────────────────────
# Bulk check-out
# ─────────────────────────────────────────────────────────────────────────────


class BulkCheckoutTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(slug="co")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-co")
        self.holder = AssetHolder.objects.create(
            first_name="C", last_name="O", upn="co@x.io", email="co@x.io", tenant=self.tenant
        )
        self.asset = Asset.objects.create(
            name="CO Asset",
            asset_tag="CO-001",
            serial_number="SN-CO-001",
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=self.tenant,
        )
        self.resolve_url = reverse("assets:asset_scan_resolve_action")

    def _job(self):
        from django.contrib.contenttypes.models import ContentType

        return Job.objects.create(
            name="t", tenant=self.tenant, model=ContentType.objects.get_for_model(Asset), status=Job.STATUS_PENDING
        )

    # ── resolve ──
    def test_resolve_checkout_eligible(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.resolve_url, {"code": "CO-001", "mode": "checkout"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data["found"])
        self.assertTrue(data["eligible"])

    def test_resolve_checkout_ineligible_when_archived(self):
        self.asset.status = self.archived
        self.asset.save()
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(self.resolve_url, {"code": "CO-001", "mode": "checkout"})
        data = json.loads(resp.content)
        self.assertFalse(data["eligible"])
        self.assertTrue(data["warning"])

    def test_resolve_checkout_requires_change_asset(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.get(self.resolve_url, {"code": "CO-001", "mode": "checkout"})
        self.assertEqual(resp.status_code, 403)

    # ── submit view ──
    @patch("assets.views.bulk_scan_views.async_task")
    def test_bulk_checkout_enqueues(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("assets:asset_bulk_checkout"),
            {
                "pk": [self.asset.pk],
                "asset_holder": self.holder.pk,
                "notes": "deploy",
            },
        )
        self.assertEqual(resp.status_code, 302)
        job = Job.objects.filter(name__contains="Bulk Check-out").first()
        self.assertIsNotNone(job)
        mock_async.assert_called_once()
        args = mock_async.call_args[0]
        self.assertEqual(args[0], "assets.tasks.checkout.bulk_checkout_task")
        self.assertEqual(args[1], job.pk)
        self.assertEqual(args[2], [str(self.asset.pk)])
        self.assertEqual(args[3], "assetholder")
        self.assertEqual(args[4], str(self.holder.pk))

    @patch("assets.views.bulk_scan_views.async_task")
    def test_bulk_checkout_requires_one_target(self, mock_async):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(reverse("assets:asset_bulk_checkout"), {"pk": [self.asset.pk]})
        self.assertEqual(resp.status_code, 302)
        mock_async.assert_not_called()
        self.assertFalse(Job.objects.exists())

    @patch("assets.views.bulk_scan_views.async_task")
    def test_bulk_checkout_rejects_multiple_targets(self, mock_async):
        site = Site.objects.create(name="S", slug="s-co")
        loc = Location.objects.create(name="L", slug="l-co", site=site, tenant=self.tenant)
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.post(
            reverse("assets:asset_bulk_checkout"),
            {
                "pk": [self.asset.pk],
                "asset_holder": self.holder.pk,
                "location": loc.pk,
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_async.assert_not_called()

    def test_checkout_permission_denied(self):
        self.tenant_role.permissions = ["assets.view_asset"]
        self.tenant_role.save()
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        resp = self.client.post(
            reverse("assets:asset_bulk_checkout"), {"pk": [self.asset.pk], "asset_holder": self.holder.pk}
        )
        self.assertEqual(resp.status_code, 403)

    # ── task ──
    def test_task_checks_out_to_holder(self):
        job = self._job()
        bulk_checkout_task(
            job.pk,
            [str(self.asset.pk)],
            "assetholder",
            self.holder.pk,
            self.tenant_admin.pk,
            "deploy",
            None,
            self.tenant.pk,
        )
        job.refresh_from_db()
        self.assertEqual(job.status, Job.STATUS_COMPLETED)
        self.assertEqual(job.result["checked_out"], 1)
        self.asset.refresh_from_db()
        active = self.asset.active_assignment
        self.assertIsNotNone(active)
        self.assertEqual(active.assigned_target, self.holder)

    def test_task_applies_status_override(self):
        custom = StatusLabel.objects.create(name="CO Custom", slug="co-custom", type="deployed")
        job = self._job()
        bulk_checkout_task(
            job.pk,
            [str(self.asset.pk)],
            "assetholder",
            self.holder.pk,
            self.tenant_admin.pk,
            "",
            None,
            self.tenant.pk,
            custom.pk,
        )
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, custom)

    def test_task_missing_and_cancelled_jobs_return_typed_outcomes(self):
        missing = bulk_checkout_task(
            999999, [], "assetholder", self.holder.pk, self.tenant_admin.pk, "", tenant_id=self.tenant.pk
        )
        cancelled_job = self._job()
        cancelled_job.status = Job.STATUS_FAILED
        cancelled_job.save(update_fields=["status"])

        skipped = bulk_checkout_task(
            cancelled_job.pk,
            [],
            "assetholder",
            self.holder.pk,
            self.tenant_admin.pk,
            "",
            tenant_id=self.tenant.pk,
        )

        self.assertEqual((missing.status, missing.code), (TaskStatus.TERMINAL, "checkout.job_not_found"))
        self.assertEqual((skipped.status, skipped.code), (TaskStatus.SKIPPED, "checkout.job_not_pending"))

    @patch("assets.services.checkout_asset", side_effect=RuntimeError("secret-holder secret-asset"))
    def test_task_item_failures_are_terminal_and_redacted(self, _checkout):
        job = self._job()

        with self.assertLogs("assets.tasks.checkout", level="WARNING") as captured:
            result = bulk_checkout_task(
                job.pk,
                [str(self.asset.pk)],
                "assetholder",
                self.holder.pk,
                self.tenant_admin.pk,
                "",
                tenant_id=self.tenant.pk,
            )

        job.refresh_from_db()
        rendered = " ".join(captured.output) + " " + job.logs
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "checkout.all_failed"))
        self.assertEqual(dict(result.counts), {"failed": 1})
        self.assertNotIn("secret-holder", rendered)
        self.assertNotIn("secret-asset", rendered)

    def test_task_item_failure_after_success_returns_partial(self):
        job = self._job()

        result = bulk_checkout_task(
            job.pk,
            [str(self.asset.pk), "999999"],
            "assetholder",
            self.holder.pk,
            self.tenant_admin.pk,
            "",
            tenant_id=self.tenant.pk,
        )

        self.assertEqual((result.status, result.code), (TaskStatus.PARTIAL, "checkout.partial"))
        self.assertEqual(dict(result.counts), {"checked_out": 1, "failed": 1})

    @patch("assets.tasks.checkout.ContentType.objects.get", side_effect=ValueError("secret-target"))
    def test_task_boundary_failure_is_terminal_and_redacted(self, _get):
        job = self._job()

        result = bulk_checkout_task(
            job.pk,
            [],
            "assetholder",
            self.holder.pk,
            self.tenant_admin.pk,
            "",
            tenant_id=self.tenant.pk,
        )

        job.refresh_from_db()
        self.assertEqual((result.status, result.code), (TaskStatus.TERMINAL, "checkout.boundary_failed"))
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertNotIn("secret-target", job.logs)

    @patch("assets.tasks.checkout.Job.objects.get", side_effect=OperationalError("secret-database"))
    def test_task_entry_database_failure_is_retryable(self, _get):
        result = bulk_checkout_task(
            17, [], "assetholder", self.holder.pk, self.tenant_admin.pk, "", tenant_id=self.tenant.pk
        )

        self.assertEqual((result.status, result.code), (TaskStatus.RETRYABLE, "checkout.entry_failed"))

    # ── page ──
    def test_checkout_page_renders(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        resp = self.client.get(reverse("assets:asset_bulk_checkout_scan"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "scan-basket-root")


# ─────────────────────────────────────────────────────────────────────────────
# Pre-Job batch validation — every submitted PK must belong to the bound tenant
# ─────────────────────────────────────────────────────────────────────────────


class BulkSubmissionBatchTests(TenantTestMixin, TestCase):
    """A mismatched or tampered batch is rejected before a Job exists.

    The background tasks keep their own per-item tenant checks; these tests
    assert the request-time boundary, so a rejected batch leaves no Job row,
    no enqueue and no cross-tenant mutation.
    """

    def setUp(self):
        self.setup_tenant_context(slug="batch")
        self.role, self.atype, self.deployable, self.deployed, self.archived = _fixtures("-bt")
        self.tenant_role.permissions = [
            "assets.view_asset",
            "assets.change_asset",
            "assets.add_assetdisposal",
        ]
        self.tenant_role.save()

        # A second accessible tenant, plus one the user can never reach.
        self.other = Tenant.objects.create(name="Batch Other", slug="batch-other")
        other_role = self.tenant_role.__class__.objects.create(
            tenant=self.other,
            name="Batch Other Role",
            permissions=["assets.view_asset", "assets.change_asset", "assets.add_assetdisposal"],
        )
        self.grant(self.tenant_user, self.other, other_role)
        self.foreign = Tenant.objects.create(name="Batch Foreign", slug="batch-foreign")

        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.asset_a = self._asset("BT-A", self.tenant)
        self.asset_b = self._asset("BT-B", self.other)
        self.asset_foreign = self._asset("BT-F", self.foreign)
        self.holder = AssetHolder.objects.create(
            first_name="Bat", last_name="Ch", upn="bt@x.io", email="bt@x.io", tenant=self.tenant
        )

    def _asset(self, tag, tenant):
        return Asset.objects.create(
            name=tag,
            asset_tag=tag,
            asset_type=self.atype,
            asset_role=self.role,
            status=self.deployable,
            tenant=tenant,
        )

    def _login_all_accessible(self):
        self.client.force_login(self.tenant_user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

    def _assert_sanitized_rejection(self, resp, rejected_count, *hidden_assets):
        """The error carries counts only — never a foreign asset's label or PK."""
        stored = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(stored), 1)
        message = stored[0]
        for asset in hidden_assets:
            self.assertNotIn(asset.asset_tag, message)
            self.assertNotIn(asset.name, message)
        self.assertEqual(set(re.findall(r"\d+", message)), {str(rejected_count)})

    # ── seed identity ──
    def test_mixed_tenant_seed_entries_carry_their_own_tenant(self):
        self._login_all_accessible()

        resp = self.client.get(
            reverse("assets:asset_bulk_checkin_scan"),
            {"pk": [self.asset_a.pk, self.asset_b.pk]},
        )

        self.assertEqual(resp.status_code, 200)
        seeded = {p["pk"]: p["tenant_id"] for p in resp.context["seed_payloads"]}
        self.assertEqual(seeded, {self.asset_a.pk: self.tenant.pk, self.asset_b.pk: self.other.pk})

    # ── valid batches keep today's behaviour ──
    @patch("assets.views.bulk_scan_views.async_task")
    def test_same_tenant_submit_still_enqueues(self, mock_async):
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset_a.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        job = Job.objects.get(name__contains="Bulk Check-in")
        self.assertEqual(job.tenant_id, self.tenant.pk)
        mock_async.assert_called_once()

    # ── mixed-tenant batches ──
    @patch("assets.views.bulk_scan_views.async_task")
    def test_mixed_tenant_checkin_is_rejected_before_job(self, mock_async):
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset_a.pk, self.asset_b.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()
        self._assert_sanitized_rejection(resp, 1, self.asset_b)

    @patch("assets.views.bulk_scan_views.async_task")
    def test_mixed_tenant_dispose_is_rejected_before_job(self, mock_async):
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_dispose"),
            {
                "tenant": self.tenant.pk,
                "pk": [self.asset_a.pk, self.asset_b.pk],
                "disposal_method": "recycle",
                "disposal_date": "2026-08-20",
            },
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        self.assertFalse(AssetDisposal.all_objects.filter(asset=self.asset_b).exists())
        mock_async.assert_not_called()
        self._assert_sanitized_rejection(resp, 1, self.asset_b)

    @patch("assets.views.bulk_scan_views.async_task")
    def test_mixed_tenant_checkout_is_rejected_before_job(self, mock_async):
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkout"),
            {
                "tenant": self.tenant.pk,
                "pk": [self.asset_a.pk, self.asset_b.pk],
                "asset_holder": self.holder.pk,
            },
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()
        self._assert_sanitized_rejection(resp, 1, self.asset_b)

    # ── unreachable, unknown, malformed and deleted PKs ──
    @patch("assets.views.bulk_scan_views.async_task")
    def test_inaccessible_tenant_pk_is_rejected(self, mock_async):
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset_a.pk, self.asset_foreign.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()
        self._assert_sanitized_rejection(resp, 1, self.asset_foreign)

    @patch("assets.views.bulk_scan_views.async_task")
    def test_unknown_pk_is_rejected(self, mock_async):
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset_a.pk, self.asset_foreign.pk + 10_000]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()

    @patch("assets.views.bulk_scan_views.async_task")
    def test_non_numeric_pk_is_rejected(self, mock_async):
        """Garbage fails the same existence check instead of reaching the ORM."""
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset_a.pk, "not-a-pk"]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()

    @patch("assets.views.bulk_scan_views.async_task")
    def test_soft_deleted_pk_is_rejected(self, mock_async):
        self.asset_a.delete()
        self._login_all_accessible()

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.tenant.pk, "pk": [self.asset_a.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(Asset.all_objects.filter(pk=self.asset_a.pk).first())
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()

    # ── concrete tenant scope stays server-authoritative ──
    @patch("assets.views.bulk_scan_views.async_task")
    def test_concrete_scope_ignores_tampered_hidden_tenant(self, mock_async):
        """A posted foreign tenant id cannot rebind the Job away from the scope tenant."""
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.other.pk, "pk": [self.asset_a.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        job = Job.objects.get(name__contains="Bulk Check-in")
        self.assertEqual(job.tenant_id, self.tenant.pk)
        self.assertEqual(mock_async.call_args[0][4], self.tenant.pk)

    @patch("assets.views.bulk_scan_views.async_task")
    def test_concrete_scope_rejects_foreign_pk(self, mock_async):
        self.client_login_to_tenant(self.tenant_user, self.tenant)

        resp = self.client.post(
            reverse("assets:asset_bulk_checkin"),
            {"tenant": self.other.pk, "pk": [self.asset_b.pk]},
        )

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.exists())
        mock_async.assert_not_called()
        self._assert_sanitized_rejection(resp, 1, self.asset_b)
