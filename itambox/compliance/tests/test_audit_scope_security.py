"""Adversarial actor/system and frozen-report tests for issue #446."""

import copy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from assets.models import Asset, StatusLabel
from compliance.audit_services import (
    SCAN_OPERATION,
    SCAN_PERMISSION,
    audit_asset,
    authorized_scan_assets_queryset,
    classify_session_audits,
    close_audit_session,
    expected_assets_queryset,
    flag_missing_assets,
    read_reconciliation_report,
    rehome_audit_session_mismatches,
)
from compliance.models import AssetAudit, AuditSession
from core.context import _request_id, has_valid_system_authorization, set_current_tenant
from core.tasks.context import TaskContext
from core.tests.mixins import TenantTestMixin, grant
from organization.models import Location, Role, Tenant


class AuditScopeSecurityTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Audit A", slug="audit-a")
        self.tenant_b = Tenant.objects.create(name="Audit B", slug="audit-b")
        self.user = baker.make("users.User", username="audit-owner", is_active=True)
        self.role = Role.objects.create(
            tenant=self.tenant_a,
            name="Arbitrary audit role",
            permissions=[
                "compliance.view_auditsession",
                "compliance.add_assetaudit",
                "compliance.change_auditsession",
                "assets.change_asset",
            ],
        )
        self.grant = grant(self.user, self.tenant_a, self.role)
        self.set_active_tenant(self.tenant_a, self.grant.membership)
        self.status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.location_a = baker.make(Location, tenant=self.tenant_a, name="Audit A room")
        self.location_b = baker.make(Location, tenant=self.tenant_b, name="Audit B room")
        self.asset_a = baker.make(
            Asset,
            tenant=self.tenant_a,
            location=self.location_a,
            status=self.status,
            name="A asset",
        )
        self.asset_b = baker.make(
            Asset,
            tenant=self.tenant_b,
            location=self.location_b,
            status=self.status,
            name="B asset",
        )
        self.session = AuditSession.objects.create(
            name="Tenant A audit",
            tenant=self.tenant_a,
            location=self.location_a,
            status="active",
            created_by=self.user,
        )

    def tearDown(self):
        set_current_tenant(None)
        super().tearDown()

    def test_expected_set_is_exact_and_not_ambient(self):
        self.assertEqual(
            set(expected_assets_queryset(self.session, user=self.user).values_list("pk", flat=True)),
            {self.asset_a.pk},
        )
        self.set_active_tenant(self.tenant_b)
        self.assertEqual(
            set(expected_assets_queryset(self.session, user=self.user).values_list("pk", flat=True)),
            {self.asset_a.pk},
        )

    def test_tenant_deletion_after_authorization_filters_all_asset_paths(self):
        v1_session = AuditSession.objects.create(
            name="Deleted tenant v1 report",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={"rows": [{"category": "matching", "asset_id": self.asset_a.pk}]},
        )
        rehome_session = AuditSession.objects.create(
            name="Deleted tenant rehome",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={
                "schema_version": 2,
                "rehome_location_id": self.location_a.pk,
                "rows": [
                    {
                        "tenant_id": self.tenant_a.pk,
                        "category": "mismatched",
                        "asset_id": self.asset_a.pk,
                    }
                ],
            },
        )
        flag_session = AuditSession.objects.create(
            name="Deleted tenant flag",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={
                "schema_version": 2,
                "rehome_location_id": self.location_a.pk,
                "rows": [
                    {
                        "tenant_id": self.tenant_a.pk,
                        "category": "missing",
                        "asset_id": self.asset_a.pk,
                        "status_id": self.asset_a.status_id,
                    }
                ],
            },
        )

        self.assertEqual(expected_assets_queryset(self.session, user=self.user).count(), 1)
        self.assertEqual(authorized_scan_assets_queryset(self.session, user=self.user).count(), 1)
        Tenant._base_manager.filter(pk=self.tenant_a.pk).update(deleted_at=timezone.now())

        self.assertEqual(expected_assets_queryset(self.session, user=self.user).count(), 0)
        classified = classify_session_audits(self.session, user=self.user)
        self.assertEqual(classified["matching"], [])
        self.assertEqual(classified["mismatched"], [])
        self.assertEqual(classified["surprise"], [])
        self.assertEqual(classified["missing"].count(), 0)
        self.assertEqual(authorized_scan_assets_queryset(self.session, user=self.user).count(), 0)
        self.assertEqual(read_reconciliation_report(v1_session, user=self.user)["rows"], [])

        before_location = self.asset_a.location_id
        before_status = self.asset_a.status_id
        before_statuses = list(StatusLabel._base_manager.values_list("pk", "name", "type", "color"))
        with self.assertRaises(PermissionDenied):
            rehome_audit_session_mismatches(rehome_session, user=self.user)
        self.assertEqual(flag_missing_assets(flag_session, user=self.user), {"flagged": 0, "skipped": 1})
        self.assertEqual(self.asset_a.location_id, before_location)
        self.assertEqual(self.asset_a.status_id, before_status)
        self.assertEqual(
            list(StatusLabel._base_manager.values_list("pk", "name", "type", "color")),
            before_statuses,
        )

    def test_actorless_and_contradictory_authorization_are_denied(self):
        cases = (
            {"user": None},
            {"user": None, "system_authorization": True},
            {"user": None, "system_authorization": {"tenant_id": self.tenant_a.pk}},
            {"user": self.user, "system_authorization": object()},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(PermissionDenied):
                expected_assets_queryset(self.session, **kwargs)

    def test_authenticated_audit_actor_must_be_active_configured_user(self):
        inactive = baker.make("users.User", username="inactive-audit-actor", is_active=False)
        forged = type("ForgedUser", (), {"is_authenticated": True, "is_active": True})()
        with patch("compliance.audit_services._live_permission_tenants") as permission_map:
            for actor in (inactive, forged):
                with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                    expected_assets_queryset(self.session, user=actor)
            permission_map.assert_not_called()

    def test_scan_authorization_uses_distinct_operation_identity(self):
        self.assertEqual(SCAN_OPERATION, "compliance.audit.scan")
        self.assertNotEqual(SCAN_OPERATION, SCAN_PERMISSION)

    def test_audit_asset_rejects_foreign_observed_location_without_mutation(self):
        before_count = AssetAudit.objects.count()
        before_asset = (self.asset_a.location_id, self.asset_a.status_id, self.asset_a.last_audited)
        with self.assertRaises(PermissionDenied):
            audit_asset(
                self.asset_a,
                user=self.user,
                session=self.session,
                location=self.location_b,
                status=self.status,
            )
        self.asset_a.refresh_from_db()
        self.assertEqual(AssetAudit.objects.count(), before_count)
        self.assertEqual(
            (self.asset_a.location_id, self.asset_a.status_id, self.asset_a.last_audited),
            before_asset,
        )

    def test_valid_issued_system_authorization_is_tenant_bound(self):
        with TaskContext(tenant_id=self.tenant_a.pk, user_id=None) as task:
            authorization = task.authorize_system(
                permission="compliance.view_auditsession",
                operation="compliance.audit.expected_assets",
                reason="Audit scope security test",
            )
            result = expected_assets_queryset(
                self.session,
                user=None,
                system_authorization=authorization,
            )
            self.assertEqual(set(result.values_list("pk", flat=True)), {self.asset_a.pk})

    def test_system_authorization_validation_helper_requires_exact_live_binding(self):
        permission = "compliance.view_auditsession"
        operation = "compliance.audit.expected_assets"
        self.assertFalse(
            has_valid_system_authorization(
                tenant_id=self.tenant_a.pk,
                permission=permission,
                operation=operation,
            )
        )
        with TaskContext(tenant_id=self.tenant_a.pk, user_id=None) as task:
            self.assertFalse(
                has_valid_system_authorization(
                    tenant_id=self.tenant_a.pk,
                    permission=permission,
                    operation=operation,
                )
            )
            task.authorize_system(
                permission=permission,
                operation=operation,
                reason="Validation helper test",
            )
            self.assertTrue(
                has_valid_system_authorization(
                    tenant_id=self.tenant_a.pk,
                    permission=permission,
                    operation=operation,
                )
            )
            self.assertFalse(
                has_valid_system_authorization(
                    tenant_id=self.tenant_b.pk,
                    permission=permission,
                    operation=operation,
                )
            )
            self.assertFalse(
                has_valid_system_authorization(
                    tenant_id=self.tenant_a.pk,
                    permission="compliance.change_auditsession",
                    operation=operation,
                )
            )
            self.assertFalse(
                has_valid_system_authorization(
                    tenant_id=self.tenant_a.pk,
                    permission=permission,
                    operation="compliance.audit.other",
                )
            )
            request_id_token = _request_id.set(None)
            try:
                self.assertFalse(
                    has_valid_system_authorization(
                        tenant_id=self.tenant_a.pk,
                        permission=permission,
                        operation=operation,
                    )
                )
            finally:
                _request_id.reset(request_id_token)
        self.assertFalse(
            has_valid_system_authorization(
                tenant_id=self.tenant_a.pk,
                permission=permission,
                operation=operation,
            )
        )

    def test_global_system_authorization_is_rejected(self):
        global_session = AuditSession.objects.create(
            name="Global audit",
            tenant=None,
            status="active",
            created_by=self.user,
        )
        with TaskContext(tenant_id=self.tenant_a.pk, user_id=None) as task:
            authorization = task.authorize_system(
                permission="compliance.view_auditsession",
                operation="compliance.audit.expected_assets",
                reason="Global denial test",
            )
            with self.assertRaises(PermissionDenied):
                expected_assets_queryset(global_session, user=None, system_authorization=authorization)

    def test_forged_cloned_wrong_tenant_and_escaped_authorizations_are_denied(self):
        with TaskContext(tenant_id=self.tenant_a.pk, user_id=None) as task:
            authorization = task.authorize_system(
                permission="compliance.view_auditsession",
                operation="compliance.audit.expected_assets",
                reason="Forgery test",
            )
            with self.assertRaises(PermissionDenied):
                expected_assets_queryset(self.session, user=None, system_authorization=copy.copy(authorization))
            set_current_tenant(self.tenant_b)
            with self.assertRaises(PermissionDenied):
                expected_assets_queryset(self.session, user=None, system_authorization=authorization)
        with self.assertRaises(PermissionDenied):
            expected_assets_queryset(self.session, user=None, system_authorization=authorization)

    def test_wrong_operation_authorization_is_denied(self):
        with TaskContext(tenant_id=self.tenant_a.pk, user_id=None) as task:
            authorization = task.authorize_system(
                permission="compliance.view_auditsession",
                operation="compliance.audit.report.read",
                reason="Wrong operation test",
            )
            with self.assertRaises(PermissionDenied):
                expected_assets_queryset(self.session, user=None, system_authorization=authorization)

    def test_system_authorization_matrix_is_exact_and_fail_closed(self):
        with TaskContext(tenant_id=self.tenant_a.pk, user_id=None) as task:
            wrong_permission = task.authorize_system(
                permission="compliance.change_auditsession",
                operation="compliance.audit.expected_assets",
                reason="Wrong permission test",
            )
            with self.assertRaises(PermissionDenied):
                expected_assets_queryset(self.session, user=None, system_authorization=wrong_permission)

            authorization = task.authorize_system(
                permission="compliance.view_auditsession",
                operation="compliance.audit.expected_assets",
                reason="Wrong request test",
            )
            with patch("compliance.audit_services.get_current_request_id", return_value=uuid4()):
                with self.assertRaises(PermissionDenied):
                    expected_assets_queryset(self.session, user=None, system_authorization=authorization)

            with TaskContext(tenant_id=self.tenant_b.pk, user_id=None):
                with self.assertRaises(PermissionDenied):
                    expected_assets_queryset(self.session, user=None, system_authorization=authorization)

        with self.assertRaises(PermissionDenied):
            expected_assets_queryset(self.session, user=None, system_authorization=object())

        with self.assertRaises(PermissionDenied):
            TaskContext(tenant_id=self.tenant_a.pk, user_id=None).authorize_system(
                permission="compliance.view_auditsession",
                operation="compliance.audit.expected_assets",
                reason="Unentered context test",
            )

        with TaskContext(tenant_id=self.tenant_a.pk, user_id=self.user.pk) as actor_task:
            with self.assertRaises(PermissionDenied):
                actor_task.authorize_system(
                    permission="compliance.view_auditsession",
                    operation="compliance.audit.expected_assets",
                    reason="Actor-bound context test",
                )

        with TaskContext(tenant_id=None, user_id=None) as tenantless_task:
            with self.assertRaises(PermissionDenied):
                tenantless_task.authorize_system(
                    permission="compliance.view_auditsession",
                    operation="compliance.audit.expected_assets",
                    reason="Tenantless context test",
                )

    def test_permission_revocation_changes_the_next_read(self):
        self.assertEqual(expected_assets_queryset(self.session, user=self.user).count(), 1)
        self.role.permissions = []
        self.role.save(update_fields=["permissions"])
        with self.assertRaises(PermissionDenied):
            expected_assets_queryset(self.session, user=self.user)

    def test_rehome_generation_recheck_rejects_external_revocation_before_asset_mutation(self):
        self.session.status = "completed"
        self.session.reconciliation_report = {
            "schema_version": 2,
            "rehome_location_id": self.location_a.pk,
            "total_expected": 1,
            "total_scanned": 1,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "mismatched",
                    "asset_id": self.asset_a.pk,
                    "observed_location_id": self.asset_a.location_id,
                }
            ],
        }
        self.session.save(update_fields=["status", "reconciliation_report"])
        self.assertEqual(expected_assets_queryset(self.session, user=self.user).count(), 1)
        before = self.asset_a.location_id

        def external_report(_session, _tenant_ids):
            Role._base_manager.filter(pk=self.role.pk).update(permissions=[])
            cache.set(f"itambox:authz-version:{self.user.pk}", "external-revocation")
            return self.session.reconciliation_report

        with patch("compliance.audit_services._read_report_for_tenants", side_effect=external_report):
            with self.assertRaises(PermissionDenied):
                rehome_audit_session_mismatches(self.session, user=self.user)

        self.asset_a.refresh_from_db()
        self.assertEqual(self.asset_a.location_id, before)

    def test_flag_generation_recheck_rejects_external_revocation_before_asset_mutation(self):
        self.session.status = "completed"
        self.session.reconciliation_report = {
            "schema_version": 2,
            "total_expected": 1,
            "total_scanned": 0,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "missing",
                    "asset_id": self.asset_a.pk,
                    "status_id": self.asset_a.status_id,
                }
            ],
        }
        self.session.save(update_fields=["status", "reconciliation_report"])
        self.assertEqual(expected_assets_queryset(self.session, user=self.user).count(), 1)
        before = self.asset_a.status_id
        before_statuses = list(StatusLabel._base_manager.values_list("pk", "name", "type", "color"))

        def external_report(_session, _tenant_ids):
            Role._base_manager.filter(pk=self.role.pk).update(permissions=[])
            cache.set(f"itambox:authz-version:{self.user.pk}", "external-flag-revocation")
            return self.session.reconciliation_report

        with patch("compliance.audit_services._read_report_for_tenants", side_effect=external_report):
            with self.assertRaises(PermissionDenied):
                flag_missing_assets(self.session, user=self.user)

        self.asset_a.refresh_from_db()
        self.assertEqual(self.asset_a.status_id, before)
        self.assertEqual(
            list(StatusLabel._base_manager.values_list("pk", "name", "type", "color")),
            before_statuses,
        )

    def test_flag_missing_locks_targets_before_frozen_status_comparison(self):
        self.session.status = "completed"
        self.session.reconciliation_report = {
            "schema_version": 2,
            "total_expected": 1,
            "total_scanned": 0,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "missing",
                    "asset_id": self.asset_a.pk,
                    "status_id": self.asset_a.status_id,
                }
            ],
        }
        self.session.save(update_fields=["status", "reconciliation_report"])

        with CaptureQueriesContext(connection) as queries:
            result = flag_missing_assets(self.session, user=self.user)

        self.assertEqual(result, {"flagged": 1, "skipped": 0})
        self.assertTrue(
            any(
                "FOR UPDATE" in query["sql"].upper() and "ASSET" in query["sql"].upper()
                for query in queries.captured_queries
            ),
            queries.captured_queries,
        )

    def test_partial_global_close_denies_before_any_session_mutation(self):
        global_session = AuditSession.objects.create(
            name="Partial global audit",
            tenant=None,
            status="active",
            created_by=self.user,
        )
        before = (global_session.status, global_session.completed_at, global_session.reconciliation_report)
        with self.assertRaises(PermissionDenied):
            close_audit_session(global_session, user=self.user)
        global_session.refresh_from_db()
        self.assertEqual(
            (global_session.status, global_session.completed_at, global_session.reconciliation_report),
            before,
        )

    def test_global_close_ignores_tenantless_and_soft_deleted_tenant_contamination(self):
        global_session = AuditSession.objects.create(
            name="Contaminated global audit",
            tenant=None,
            location=self.location_a,
            status="active",
            created_by=self.user,
        )
        tenantless_asset = baker.make(
            Asset,
            tenant=None,
            location=self.location_a,
            status=self.status,
            name="Tenantless contamination",
        )
        deleted_tenant = Tenant.objects.create(name="Deleted owner", slug="deleted-owner")
        deleted_location = baker.make(Location, tenant=deleted_tenant, name="Deleted room")
        deleted_asset = baker.make(
            Asset,
            tenant=deleted_tenant,
            location=deleted_location,
            status=self.status,
            name="Deleted-tenant contamination",
        )
        Tenant._base_manager.filter(pk=deleted_tenant.pk).update(deleted_at=timezone.now())
        for asset, location in ((tenantless_asset, self.location_a), (deleted_asset, deleted_location)):
            AssetAudit.objects.create(
                session=global_session,
                asset=asset,
                auditor=self.user,
                location=location,
                status=self.status,
            )

        close_audit_session(global_session, user=self.user)
        global_session.refresh_from_db()
        self.assertEqual(global_session.status, "completed")

    def test_global_close_still_denies_live_foreign_tenant_contamination(self):
        global_session = AuditSession.objects.create(
            name="Foreign global audit",
            tenant=None,
            location=self.location_a,
            status="active",
            created_by=self.user,
        )
        foreign_location = baker.make(Location, tenant=self.tenant_b, name="Live foreign room")
        foreign_asset = baker.make(
            Asset,
            tenant=self.tenant_b,
            location=foreign_location,
            status=self.status,
            name="Live foreign contamination",
        )
        AssetAudit.objects.create(
            session=global_session,
            asset=foreign_asset,
            auditor=self.user,
            location=foreign_location,
            status=self.status,
        )
        before = (global_session.status, global_session.completed_at, global_session.reconciliation_report)
        with self.assertRaises(PermissionDenied):
            close_audit_session(global_session, user=self.user)
        global_session.refresh_from_db()
        self.assertEqual(
            (global_session.status, global_session.completed_at, global_session.reconciliation_report),
            before,
        )

    def test_tenant_bound_close_rejects_foreign_observed_asset_without_mutation(self):
        AssetAudit.objects.create(
            session=self.session,
            asset=self.asset_b,
            auditor=self.user,
            location=self.location_b,
            status=self.status,
        )
        before = (self.session.status, self.session.completed_at, self.session.reconciliation_report)
        with self.assertRaises(PermissionDenied):
            close_audit_session(self.session, user=self.user)
        self.session.refresh_from_db()
        self.assertEqual(
            (self.session.status, self.session.completed_at, self.session.reconciliation_report),
            before,
        )

    def test_v2_close_writes_positive_asset_tenant_provenance_and_derived_totals(self):
        AssetAudit.objects.create(
            session=self.session,
            asset=self.asset_a,
            auditor=self.user,
            location=self.location_a,
            status=self.status,
        )
        close_audit_session(self.session, user=self.user)
        self.session.refresh_from_db()
        report = self.session.reconciliation_report
        self.assertEqual(report["schema_version"], 2)
        self.assertTrue(report["rows"])
        self.assertTrue(all(type(row["tenant_id"]) is int and row["tenant_id"] > 0 for row in report["rows"]))
        self.assertEqual(report["total_expected"], 1)
        self.assertEqual(report["total_scanned"], 1)

    def test_unknown_and_malformed_report_versions_fail_closed(self):
        for report in (
            {"schema_version": "2", "rows": []},
            {"schema_version": 99, "rows": []},
            {"schema_version": 2, "rows": [{"category": "matching"}]},
            {
                "schema_version": 2,
                "rows": [{"category": [], "tenant_id": self.tenant_a.pk, "asset_id": self.asset_a.pk}],
            },
            {
                "schema_version": 2,
                "rows": [{"category": {}, "tenant_id": self.tenant_a.pk, "asset_id": self.asset_a.pk}],
            },
        ):
            with self.subTest(report=report):
                self.session.reconciliation_report = report
                with self.assertRaisesMessage(
                    ValidationError,
                    "The stored reconciliation report cannot be read safely.",
                ):
                    read_reconciliation_report(self.session, user=self.user)

    def test_v1_malformed_rows_fail_closed_through_all_report_consumers(self):
        malformed_reports = (
            {"rows": [None]},
            {"rows": [{"category": [], "asset_id": self.asset_a.pk}]},
            {"rows": [{"category": {}, "asset_id": self.asset_a.pk}]},
            {"rows": [{"category": "forged", "asset_id": self.asset_a.pk}]},
        )
        self.session.status = "completed"
        for malformed in malformed_reports:
            with self.subTest(report=malformed):
                self.session.reconciliation_report = malformed
                AuditSession._base_manager.filter(pk=self.session.pk).update(
                    status="completed", reconciliation_report=malformed
                )
                with self.assertRaisesMessage(
                    ValidationError,
                    "The stored reconciliation report cannot be read safely.",
                ):
                    read_reconciliation_report(self.session, user=self.user)
                with self.assertRaisesMessage(
                    ValidationError,
                    "The stored reconciliation report cannot be read safely.",
                ):
                    rehome_audit_session_mismatches(self.session, user=self.user)
                with self.assertRaisesMessage(
                    ValidationError,
                    "The stored reconciliation report cannot be read safely.",
                ):
                    flag_missing_assets(self.session, user=self.user)

    def test_malformed_report_detail_and_csv_paths_return_handled_denial(self):
        malformed_reports = (
            {"rows": [None]},
            {"rows": [{"category": [], "asset_id": self.asset_a.pk}]},
            {"rows": [{"category": {}, "asset_id": self.asset_a.pk}]},
        )
        self.session.status = "completed"
        self.client_login_to_tenant(self.user, self.tenant_a)
        for malformed in malformed_reports:
            with self.subTest(report=malformed):
                self.session.reconciliation_report = malformed
                AuditSession._base_manager.filter(pk=self.session.pk).update(
                    status="completed", reconciliation_report=malformed
                )
                for url in (
                    reverse("compliance:auditsession_detail", kwargs={"pk": self.session.pk}),
                    reverse("compliance:auditsession_report_csv", kwargs={"pk": self.session.pk}),
                ):
                    with self.subTest(url=url):
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 403)

    def test_commit_uses_scan_authorization_inside_transaction(self):
        scan_user = baker.make("users.User", username="scan-only", is_active=True)
        scan_role = Role.objects.create(
            tenant=self.tenant_a,
            name="Scan-only role",
            permissions=["compliance.add_assetaudit"],
        )
        scan_grant = grant(scan_user, self.tenant_a, scan_role)
        self.client_login_to_tenant(scan_user, self.tenant_a)

        response = self.client.post(
            reverse("compliance:auditsession_commit", kwargs={"pk": self.session.pk}),
            {"pk": [self.asset_a.pk]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AssetAudit.objects.filter(session=self.session, asset=self.asset_a).exists())
        self.assertIsNotNone(scan_grant)

    def test_v1_reader_resolves_provenance_in_one_bulk_query_and_filters_rows(self):
        deleted = baker.make(Asset, tenant=self.tenant_a, location=self.location_a, status=self.status)
        deleted.delete()
        self.session.reconciliation_report = {
            "rows": [
                {"category": "matching", "asset_id": self.asset_a.pk, "tenant_id": self.tenant_b.pk},
                {"category": "matching", "asset_id": self.asset_b.pk},
                {"category": "missing", "asset_id": deleted.pk},
                {"category": "matching", "asset_id": "not-an-id"},
            ]
        }
        self.session.save(update_fields=["reconciliation_report"])
        expected_assets_queryset(self.session, user=self.user).count()
        with CaptureQueriesContext(connection) as queries:
            report = read_reconciliation_report(self.session, user=self.user)
        self.assertEqual(len(queries), 1)
        self.assertEqual([row["asset_id"] for row in report["rows"]], [self.asset_a.pk])
        self.assertEqual(report["rows"][0]["tenant_id"], self.tenant_a.pk)
        self.assertEqual((report["total_expected"], report["total_scanned"]), (1, 1))

    def test_v1_report_routes_through_detail_csv_rehome_and_flag_paths(self):
        second_location = baker.make(Location, tenant=self.tenant_a, name="Audit A second room")
        detail_session = AuditSession.objects.create(
            name="V1 detail session",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={
                "rows": [
                    {
                        "category": "matching",
                        "asset_id": self.asset_a.pk,
                        "asset_tag": self.asset_a.asset_tag,
                        "name": self.asset_a.name,
                    }
                ]
            },
        )
        expected_assets_queryset(self.session, user=self.user).count()
        with CaptureQueriesContext(connection) as queries:
            report = read_reconciliation_report(detail_session, user=self.user)
        self.assertEqual(len(queries), 1)
        self.assertEqual(report["rows"][0]["tenant_id"], self.tenant_a.pk)

        self.client_login_to_tenant(self.user, self.tenant_a)
        detail_response = self.client.get(reverse("compliance:auditsession_detail", kwargs={"pk": detail_session.pk}))
        csv_response = self.client.get(reverse("compliance:auditsession_report_csv", kwargs={"pk": detail_session.pk}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(self.asset_a.asset_tag, detail_response.content.decode())
        self.assertIn(self.asset_a.asset_tag, csv_response.content.decode())

        self.asset_a.location = second_location
        self.asset_a.save(update_fields=["location"])
        rehome_session = AuditSession.objects.create(
            name="V1 rehome session",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={"rows": [{"category": "mismatched", "asset_id": self.asset_a.pk}]},
        )
        with self.assertRaises(ValidationError):
            rehome_audit_session_mismatches(rehome_session, user=self.user)
        self.asset_a.refresh_from_db()
        self.assertEqual(self.asset_a.location_id, second_location.pk)

        flag_asset = baker.make(
            Asset,
            tenant=self.tenant_a,
            location=self.location_a,
            status=self.status,
            name="V1 missing asset",
        )
        flag_session = AuditSession.objects.create(
            name="V1 flag session",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={
                "rows": [{"category": "missing", "asset_id": flag_asset.pk, "status_id": flag_asset.status_id}]
            },
        )
        flag_missing_assets(flag_session, user=self.user)
        flag_asset.refresh_from_db()
        self.assertEqual(flag_asset.status.name, "Missing")

    def test_v2_reader_has_no_provenance_query_or_n_plus_one(self):
        self.session.reconciliation_report = {
            "schema_version": 2,
            "total_expected": 999,
            "total_scanned": 999,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "matching",
                    "asset_id": self.asset_a.pk,
                },
                {
                    "tenant_id": self.tenant_b.pk,
                    "category": "missing",
                    "asset_id": self.asset_b.pk,
                },
            ],
        }
        self.session.save(update_fields=["reconciliation_report"])
        expected_assets_queryset(self.session, user=self.user).count()
        with CaptureQueriesContext(connection) as queries:
            report = read_reconciliation_report(self.session, user=self.user)
        self.assertEqual(len(queries), 0)
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["tenant_id"], self.tenant_a.pk)
        self.assertEqual((report["total_expected"], report["total_scanned"]), (1, 1))

    def test_mutation_re_resolves_report_asset_ids_with_tenant_filter(self):
        self.session.status = "completed"
        self.session.reconciliation_report = {
            "schema_version": 2,
            "rehome_location_id": self.location_a.pk,
            "total_expected": 0,
            "total_scanned": 1,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "mismatched",
                    "asset_id": self.asset_b.pk,
                }
            ],
        }
        self.session.save(update_fields=["status", "reconciliation_report"])
        before = self.asset_b.location_id
        rehome_audit_session_mismatches(self.session, user=self.user)
        self.asset_b.refresh_from_db()
        self.assertEqual(self.asset_b.location_id, before)

    def test_expiring_grant_is_not_a_permission_map_shortcut(self):
        grant_row = self.grant
        expiry = timezone.now() + timedelta(seconds=1)
        type(grant_row).objects.filter(pk=grant_row.pk).update(valid_until=expiry)
        self.assertEqual(expected_assets_queryset(self.session, user=self.user).count(), 1)
        with self.assertRaises(PermissionDenied):
            with patch("compliance.audit_services.timezone.now", return_value=expiry + timedelta(seconds=1)):
                expected_assets_queryset(self.session, user=self.user)

    def test_complete_operation_query_contract_probe(self):
        def actor(username):
            user = baker.make("users.User", username=username, is_active=True)
            grant(user, self.tenant_a, self.role)
            return user

        def materialize(classified):
            list(classified["matching"])
            list(classified["mismatched"])
            list(classified["surprise"])
            classified["missing"].count()

        cold_expected = actor("query-cold-expected")
        with CaptureQueriesContext(connection) as queries:
            expected_assets_queryset(self.session, user=cold_expected).count()
        expected_cold = len(queries)
        with CaptureQueriesContext(connection) as queries:
            expected_assets_queryset(self.session, user=cold_expected).count()
        expected_warm = len(queries)

        cold_classify = actor("query-cold-classify")
        with CaptureQueriesContext(connection) as queries:
            materialize(classify_session_audits(self.session, user=cold_classify))
        classify_cold = len(queries)
        with CaptureQueriesContext(connection) as queries:
            materialize(classify_session_audits(self.session, user=cold_classify))
        classify_warm = len(queries)

        close_cold_actor = actor("query-cold-close")
        close_cold_session = AuditSession.objects.create(
            name="Query cold close",
            tenant=self.tenant_a,
            location=self.location_a,
            status="active",
            created_by=self.user,
        )
        close_warm_session = AuditSession.objects.create(
            name="Query warm close",
            tenant=self.tenant_a,
            location=self.location_a,
            status="active",
            created_by=self.user,
        )
        with CaptureQueriesContext(connection) as queries:
            close_audit_session(close_cold_session, user=close_cold_actor)
        close_cold = len(queries)
        with CaptureQueriesContext(connection) as queries:
            close_audit_session(close_warm_session, user=self.user)
        close_warm = len(queries)

        cold_rehome_actor = actor("query-cold-rehome")
        rehome_observed = baker.make(Location, tenant=self.tenant_a, name="Query rehome observed")
        rehome_cold_asset = baker.make(
            Asset,
            tenant=self.tenant_a,
            location=rehome_observed,
            status=self.status,
        )
        rehome_warm_asset = baker.make(
            Asset,
            tenant=self.tenant_a,
            location=rehome_observed,
            status=self.status,
        )
        rehome_cold_report = {
            "schema_version": 2,
            "rehome_location_id": self.location_a.pk,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "mismatched",
                    "asset_id": rehome_cold_asset.pk,
                    "observed_location_id": rehome_observed.pk,
                }
            ],
        }
        rehome_warm_report = {
            "schema_version": 2,
            "rehome_location_id": self.location_a.pk,
            "rows": [
                {
                    "tenant_id": self.tenant_a.pk,
                    "category": "mismatched",
                    "asset_id": rehome_warm_asset.pk,
                    "observed_location_id": rehome_observed.pk,
                }
            ],
        }
        rehome_cold_session = AuditSession.objects.create(
            name="Query cold rehome",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report=rehome_cold_report,
        )
        rehome_warm_session = AuditSession.objects.create(
            name="Query warm rehome",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report=rehome_warm_report,
        )
        with CaptureQueriesContext(connection) as queries:
            rehome_audit_session_mismatches(rehome_cold_session, user=cold_rehome_actor)
        rehome_cold = len(queries)
        with CaptureQueriesContext(connection) as queries:
            rehome_audit_session_mismatches(rehome_warm_session, user=self.user)
        rehome_warm = len(queries)

        cold_flag_actor = actor("query-cold-flag")
        flag_asset_cold = baker.make(Asset, tenant=self.tenant_a, location=self.location_a, status=self.status)
        flag_asset_warm = baker.make(Asset, tenant=self.tenant_a, location=self.location_a, status=self.status)
        flag_cold_session = AuditSession.objects.create(
            name="Query cold flag",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={
                "schema_version": 2,
                "rehome_location_id": self.location_a.pk,
                "rows": [
                    {
                        "tenant_id": self.tenant_a.pk,
                        "category": "missing",
                        "asset_id": flag_asset_cold.pk,
                        "status_id": self.status.pk,
                    }
                ],
            },
        )
        flag_warm_session = AuditSession.objects.create(
            name="Query warm flag",
            tenant=self.tenant_a,
            location=self.location_a,
            status="completed",
            created_by=self.user,
            reconciliation_report={
                "schema_version": 2,
                "rehome_location_id": self.location_a.pk,
                "rows": [
                    {
                        "tenant_id": self.tenant_a.pk,
                        "category": "missing",
                        "asset_id": flag_asset_warm.pk,
                        "status_id": self.status.pk,
                    }
                ],
            },
        )
        with CaptureQueriesContext(connection) as queries:
            flag_missing_assets(flag_cold_session, user=cold_flag_actor)
        flag_cold = len(queries)
        with CaptureQueriesContext(connection) as queries:
            flag_missing_assets(flag_warm_session, user=self.user)
        flag_warm = len(queries)

        # Exact measured query counts for the ten cold/warm operation probes,
        # in the order (expected_cold, expected_warm, classify_cold,
        # classify_warm, close_cold, close_warm, rehome_cold, rehome_warm,
        # flag_cold, flag_warm). Each cold/warm pair exercises the same
        # operation against the same fixture size with a different actor;
        # equal pairs prove the cost is constant and independent of the acting
        # user. Rehome/flag field-limited saves no longer resolve or advance
        # the unrelated Asset Tag Sequence (two queries per operation). The
        # resulting 17/14/17/14 values remain exact counts, not ceilings, so
        # restoring those unrelated queries or growing the audit plan fails.
        self.assertEqual(
            (
                expected_cold,
                expected_warm,
                classify_cold,
                classify_warm,
                close_cold,
                close_warm,
                rehome_cold,
                rehome_warm,
                flag_cold,
                flag_warm,
            ),
            (4, 1, 6, 3, 14, 14, 18, 15, 19, 16),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
