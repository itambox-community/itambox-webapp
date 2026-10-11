"""Tests for the scheduled-report cross-tenant scope approval view (WP-9b)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.tasks.context import TaskContext
from core.tasks.utils import TaskStatus
from core.tests.mixins import grant
from extras.models import ReportGenerationArchive, ReportTemplate, ScheduledReport, ScheduledReportScopeAuthorization
from organization.models import Role, Tenant
from organization.services.tenant_archive import archive_tenant

User = get_user_model()


class ScheduledReportScopeApprovalViewTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Approval Tenant A", slug="approval-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Approval Tenant B", slug="approval-tenant-b")
        self.tenant_c = Tenant.objects.create(name="Approval Tenant C", slug="approval-tenant-c")
        self.admin = User.objects.create_superuser(username="scope-admin", password="password123", email="a@b.com")
        self.operator = User.objects.create_user(username="scope-operator", password="password123")
        self.partial = User.objects.create_user(username="scope-partial", password="password123")
        cross_tenant = ["reports.view_cross_tenant_reports"]
        grant(
            self.operator,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="CT A", permissions=cross_tenant),
        )
        grant(
            self.operator,
            self.tenant_b,
            Role.objects.create(tenant=self.tenant_b, name="CT B", permissions=cross_tenant),
        )
        grant(
            self.partial,
            self.tenant_a,
            Role.objects.create(tenant=self.tenant_a, name="CT A partial", permissions=cross_tenant),
        )
        self.template = ReportTemplate.objects.create(
            name="Approval Template",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            tenant=self.tenant_a,
        )
        self.sched = ScheduledReport.objects.create(
            name="Cross-Tenant Schedule",
            report=self.template,
            tenant=self.tenant_a,
            format=ScheduledReport.FORMAT_HTML,
            save_to_archive=False,
            is_active=True,
        )
        self.sched.filter_tenants.add(self.tenant_a, self.tenant_b)
        self.url = reverse("extras:scheduledreport_scope_approval", kwargs={"pk": self.sched.pk})

    def _client_for(self, user):
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_tenant_id"] = self.tenant_a.pk
        session.save()
        return client

    def test_get_requires_cross_tenant_permission(self):
        plain = User.objects.create_user(username="scope-plain", password="password123")
        response = self._client_for(plain).get(self.url)
        self.assertNotEqual(response.status_code, 200)

    def test_get_shows_scope_tenants_and_actions(self):
        response = self._client_for(self.admin).get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approval Tenant A")
        self.assertContains(response, "Approval Tenant B")
        self.assertContains(response, "Approve Scope")
        self.assertNotContains(response, "Revoke Approval")

    def test_approve_post_creates_durable_authorization(self):
        response = self._client_for(self.admin).post(self.url, {"action": "approve"})
        self.assertEqual(response.status_code, 302)
        authorization = ScheduledReportScopeAuthorization.objects.get(scheduled_report=self.sched)
        self.assertEqual(authorization.authorized_by, self.admin)
        self.assertEqual(authorization.scope_tenant_ids, [self.tenant_a.pk, self.tenant_b.pk])
        self.assertIsNone(authorization.revoked_at)
        self.assertIsNone(authorization.revoked_by)

    def test_approved_broad_run_denies_when_approver_lacks_report_domain_permission(self):
        from extras.tasks.reports import generate_scheduled_report_task

        self.sched.recipients = "broad-run@example.test"
        self.sched.save_to_archive = True
        self.sched.save(update_fields=["recipients", "save_to_archive"])
        ScheduledReportScopeAuthorization.approve(self.sched, self.operator)
        with (
            patch("extras.tasks.reports._deliver_report_email") as deliver_email,
            patch("extras.tasks.reports._deliver_report_channels") as deliver_channels,
        ):
            result = generate_scheduled_report_task(self.sched.pk)

        self.assertEqual(result.status, TaskStatus.TERMINAL)
        self.assertEqual(result.code, "report.permission_denied")
        self.assertTrue(result.user_visible)
        self.sched.refresh_from_db()
        self.assertEqual(self.sched.last_status, "terminal: report.permission_denied")
        self.assertIsNone(self.sched.last_run_archive)
        self.assertEqual(ReportGenerationArchive.objects.filter(scheduled_report=self.sched).count(), 0)
        deliver_email.assert_not_called()
        deliver_channels.assert_not_called()

    def test_run_now_broad_denies_invoker_without_scope_reach(self):
        """A broad Run now is evaluated as the invoker, not as the stored approver."""
        from extras.tasks.reports import generate_scheduled_report_task

        self.sched.recipients = "broad-run-now@example.test"
        self.sched.save_to_archive = True
        self.sched.save(update_fields=["recipients", "save_to_archive"])
        capable = User.objects.create_user(username="scope-capable-approver", password="password123")
        for tenant in (self.tenant_a, self.tenant_b):
            grant(
                capable,
                tenant,
                Role.objects.create(
                    tenant=tenant,
                    name=f"Capable Approver {tenant.slug}",
                    permissions=["reports.view_cross_tenant_reports", "assets.view_asset"],
                ),
            )
        ScheduledReportScopeAuthorization.approve(self.sched, capable)

        invoker = User.objects.create_user(username="scope-run-now-invoker", password="password123")
        grant(
            invoker,
            self.tenant_a,
            Role.objects.create(
                tenant=self.tenant_a,
                name="Run Now Schedule Viewer",
                permissions=["extras.view_scheduledreport"],
            ),
        )
        with (
            patch("extras.tasks.reports._deliver_report_email") as deliver_email,
            patch("extras.tasks.reports._deliver_report_channels") as deliver_channels,
        ):
            result = generate_scheduled_report_task(self.sched.pk, invoked_by_user_id=invoker.pk)

        self.assertEqual(result.status, TaskStatus.TERMINAL)
        self.assertEqual(result.code, "report.permission_denied")
        self.assertTrue(result.user_visible)
        self.sched.refresh_from_db()
        self.assertEqual(self.sched.last_status, "terminal: report.permission_denied")
        self.assertIsNone(self.sched.last_run_archive)
        self.assertEqual(ReportGenerationArchive.objects.filter(scheduled_report=self.sched).count(), 0)
        deliver_email.assert_not_called()
        deliver_channels.assert_not_called()

    def test_run_now_broad_domain_gate_evaluates_the_invoking_user(self):
        """The invoker's domain permission decides, even when the approver holds it."""
        from extras.tasks.reports import generate_scheduled_report_task

        self.sched.recipients = "broad-run-now-domain@example.test"
        self.sched.save_to_archive = True
        self.sched.save(update_fields=["recipients", "save_to_archive"])
        capable = User.objects.create_user(username="scope-capable-approver-domain", password="password123")
        for tenant in (self.tenant_a, self.tenant_b):
            grant(
                capable,
                tenant,
                Role.objects.create(
                    tenant=tenant,
                    name=f"Capable Approver {tenant.slug}",
                    permissions=["reports.view_cross_tenant_reports", "assets.view_asset"],
                ),
            )
        ScheduledReportScopeAuthorization.approve(self.sched, capable)

        invoker = User.objects.create_user(username="scope-run-now-cross-tenant", password="password123")
        for tenant in (self.tenant_a, self.tenant_b):
            grant(
                invoker,
                tenant,
                Role.objects.create(
                    tenant=tenant,
                    name=f"Invoker Cross Scope {tenant.slug}",
                    permissions=["reports.view_cross_tenant_reports"],
                ),
            )
        with (
            patch("extras.tasks.reports._deliver_report_email") as deliver_email,
            patch("extras.tasks.reports._deliver_report_channels") as deliver_channels,
        ):
            result = generate_scheduled_report_task(self.sched.pk, invoked_by_user_id=invoker.pk)

        self.assertEqual(result.status, TaskStatus.TERMINAL)
        self.assertEqual(result.code, "report.permission_denied")
        self.assertTrue(result.user_visible)
        self.sched.refresh_from_db()
        self.assertEqual(self.sched.last_status, "terminal: report.permission_denied")
        self.assertIsNone(self.sched.last_run_archive)
        self.assertEqual(ReportGenerationArchive.objects.filter(scheduled_report=self.sched).count(), 0)
        deliver_email.assert_not_called()
        deliver_channels.assert_not_called()

    def test_invalid_run_now_invoker_is_denied_before_fire_claim(self):
        from extras.tasks.reports import generate_scheduled_report_task

        inactive = User.objects.create_user(username="inactive-run-now", password="password123", is_active=False)
        for principal_id in (inactive.pk, 999999):
            with self.subTest(principal_id=principal_id), patch("extras.tasks.reports._claim_fire") as claim_fire:
                result = generate_scheduled_report_task(
                    self.sched.pk,
                    intended_fire_at="2026-01-01T12:00:00+00:00",
                    invoked_by_user_id=principal_id,
                )
            self.assertEqual(result.status, TaskStatus.TERMINAL)
            self.assertEqual(result.code, "report.permission_denied")
            self.assertTrue(result.user_visible)
            claim_fire.assert_not_called()

    def test_approve_post_refreshes_a_stale_scope(self):
        self._client_for(self.admin).post(self.url, {"action": "approve"})
        self.sched.filter_tenants.add(self.tenant_c)
        response = self._client_for(self.admin).post(self.url, {"action": "approve"})
        self.assertEqual(response.status_code, 302)
        authorization = ScheduledReportScopeAuthorization.objects.get(scheduled_report=self.sched)
        self.assertEqual(
            authorization.scope_tenant_ids,
            sorted([self.tenant_a.pk, self.tenant_b.pk, self.tenant_c.pk]),
        )

    def test_approve_post_refuses_when_reach_does_not_cover_scope(self):
        response = self._client_for(self.partial).post(self.url, {"action": "approve"}, follow=True)
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)
        self.assertContains(response, "Your permission does not cover these tenants")

    def test_approve_of_single_tenant_schedule_is_rejected(self):
        self.sched.filter_tenants.clear()
        response = self._client_for(self.admin).post(self.url, {"action": "approve"}, follow=True)
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)
        self.assertContains(response, "does not need cross-tenant scope approval")

    def test_revoke_post_marks_approval_revoked(self):
        self._client_for(self.admin).post(self.url, {"action": "approve"})
        response = self._client_for(self.admin).post(self.url, {"action": "revoke"})
        self.assertEqual(response.status_code, 302)
        authorization = ScheduledReportScopeAuthorization.objects.get(scheduled_report=self.sched)
        self.assertTrue(authorization.is_revoked())
        self.assertEqual(authorization.revoked_by, self.admin)
        self.assertIsNotNone(authorization.revoked_at)

    def test_revoke_post_refuses_when_reach_does_not_cover_the_stored_scope(self):
        self._client_for(self.admin).post(self.url, {"action": "approve"})
        response = self._client_for(self.partial).post(self.url, {"action": "revoke"}, follow=True)
        authorization = ScheduledReportScopeAuthorization.objects.get(scheduled_report=self.sched)
        self.assertFalse(authorization.is_revoked())
        self.assertContains(response, "The revocation would not take effect")

    def test_approve_after_revoke_clears_the_revocation(self):
        client = self._client_for(self.admin)
        client.post(self.url, {"action": "approve"})
        client.post(self.url, {"action": "revoke"})
        client.post(self.url, {"action": "approve"})
        authorization = ScheduledReportScopeAuthorization.objects.get(scheduled_report=self.sched)
        self.assertFalse(authorization.is_revoked())
        self.assertIsNone(authorization.revoked_at)
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 1)

    def test_revoke_without_approval_reports_an_error(self):
        response = self._client_for(self.admin).post(self.url, {"action": "revoke"}, follow=True)
        self.assertContains(response, "no cross-tenant scope approval to revoke")
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)

    def test_unknown_action_reports_an_error_without_writing(self):
        response = self._client_for(self.admin).post(self.url, {"action": "nope"}, follow=True)
        self.assertContains(response, "Unknown scope approval action")
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)

    def test_get_refuses_storing_an_ineffective_approval_state(self):
        response = self._client_for(self.partial).get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "would not take effect")

    def test_approve_refused_when_a_scope_tenant_does_not_resolve(self):
        from unittest.mock import patch

        # Simulate a stale scope id (e.g. a hard-deleted tenant still present in
        # the through table) without violating the tenant FK at flush time.
        with patch.object(
            ScheduledReport,
            "effective_scope_tenant_ids",
            return_value=[self.tenant_a.pk, self.tenant_b.pk, 999999],
        ):
            response = self._client_for(self.admin).post(self.url, {"action": "approve"}, follow=True)
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)
        self.assertContains(response, "Some tenants in this scope are no longer available")

    def test_approve_refused_when_a_scope_tenant_is_soft_deleted(self):
        with TaskContext(operation="test.scope_approval.soft_delete_tenant"):
            archive_tenant(self.tenant_b)
        response = self._client_for(self.admin).post(self.url, {"action": "approve"}, follow=True)
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)
        self.assertContains(response, "Some tenants in this scope are no longer available")

    def test_wound_back_schedule_still_offers_revoke(self):
        self._client_for(self.admin).post(self.url, {"action": "approve"})
        self.sched.filter_tenants.clear()
        response = self._client_for(self.admin).get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revoke Approval")

    def test_approve_rolls_back_when_the_snapshot_diverges(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        with patch.object(
            ScheduledReportScopeAuthorization,
            "approve",
            return_value=SimpleNamespace(scope_tenant_ids=[self.tenant_a.pk]),
        ):
            response = self._client_for(self.admin).post(self.url, {"action": "approve"}, follow=True)
        self.assertEqual(ScheduledReportScopeAuthorization.objects.filter(scheduled_report=self.sched).count(), 0)
        self.assertContains(response, "scope changed while approving")

    def test_error_redirect_preserves_the_return_url(self):
        from urllib.parse import unquote

        self.sched.filter_tenants.clear()
        response = self._client_for(self.admin).post(
            self.url, {"action": "approve", "return_url": "/extras/reports/schedules/?page=2"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("return_url=", response.url)
        self.assertIn("page=2", unquote(response.url))

    def test_stored_authorizer_reach_loss_is_surfaced_on_the_page(self):
        from organization.models import RoleGrant

        self._client_for(self.operator).post(self.url, {"action": "approve"})
        RoleGrant.objects.filter(membership__user=self.operator, role__tenant=self.tenant_b).delete()
        response = self._client_for(self.admin).get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The approval is no longer valid")

    def test_off_host_return_url_is_sanitized(self):
        response = self._client_for(self.admin).post(
            self.url, {"action": "approve", "return_url": "https://evil.example/phish"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("extras:scheduledreport_list"))


class ScheduledReportScopeApprovalCapabilityGateTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Gate Tenant A", slug="gate-tenant-a")
        self.admin = User.objects.create_superuser(username="scope-gate-admin", password="password123", email="a@b.com")
        self.template = ReportTemplate.objects.create(
            name="Gate Template",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            tenant=self.tenant_a,
        )
        self.sched = ScheduledReport.objects.create(
            name="Gate Schedule",
            report=self.template,
            tenant=self.tenant_a,
            format=ScheduledReport.FORMAT_HTML,
            save_to_archive=False,
            is_active=True,
        )
        self.url = reverse("extras:scheduledreport_scope_approval", kwargs={"pk": self.sched.pk})

    def test_route_is_open_with_the_stable_scheduled_capability(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get(self.url)
        self.assertEqual(response.status_code, 200)


class ScheduledReportScopeBadgeTests(TestCase):
    """Pin the list-column badge states to the generation semantics."""

    def setUp(self):
        from extras.tables import ScheduledReportTable

        self.tenant_a = Tenant.objects.create(name="Badge Tenant A", slug="badge-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Badge Tenant B", slug="badge-tenant-b")
        self.tenant_c = Tenant.objects.create(name="Badge Tenant C", slug="badge-tenant-c")
        self.admin = User.objects.create_superuser(username="badge-admin", password="password123", email="a@b.com")
        self.template = ReportTemplate.objects.create(
            name="Badge Template",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            tenant=self.tenant_a,
        )
        self.sched = ScheduledReport.objects.create(
            name="Badge Schedule",
            report=self.template,
            tenant=self.tenant_a,
            format=ScheduledReport.FORMAT_HTML,
            save_to_archive=False,
            is_active=True,
        )
        self.table = ScheduledReportTable(ScheduledReport.objects.all())

    def _badge(self):
        # The table renders rows fresh from the queryset; mirror that instead of
        # reusing the setUp instance, whose reverse-O2O cache is not invalidated
        # by approve()/revoke() (they write through the queryset path).
        fresh = ScheduledReport.objects.get(pk=self.sched.pk)
        return str(self.table.render_scope(fresh, fresh.pk))

    def test_single_tenant_schedule_shows_no_approval_badge(self):
        self.assertEqual(self._badge(), '<span class="badge bg-secondary">Single tenant</span>')

    def test_cross_tenant_schedule_without_approval_shows_approval_required(self):
        self.sched.filter_tenants.add(self.tenant_a, self.tenant_b)
        badge = self._badge()
        self.assertIn("Approval required", badge)
        self.assertIn("/scope-approval/", badge)

    def test_current_approval_shows_approved(self):
        self.sched.filter_tenants.add(self.tenant_a, self.tenant_b)
        ScheduledReportScopeAuthorization.approve(self.sched, self.admin)
        self.assertIn("Approved", self._badge())

    def test_revoked_approval_shows_revoked(self):
        self.sched.filter_tenants.add(self.tenant_a, self.tenant_b)
        ScheduledReportScopeAuthorization.approve(self.sched, self.admin)
        ScheduledReportScopeAuthorization.revoke(self.sched, self.admin)
        self.assertIn("Revoked", self._badge())

    def test_stale_approval_shows_scope_changed(self):
        self.sched.filter_tenants.add(self.tenant_a, self.tenant_b)
        ScheduledReportScopeAuthorization.approve(self.sched, self.admin)
        self.sched.filter_tenants.add(self.tenant_c)
        self.assertIn("Scope changed", self._badge())

    def test_soft_deleted_scope_tenant_shows_scope_changed(self):
        self.sched.filter_tenants.add(self.tenant_a, self.tenant_b)
        ScheduledReportScopeAuthorization.approve(self.sched, self.admin)
        self.tenant_b.delete()
        self.assertIn("Scope changed", self._badge())
