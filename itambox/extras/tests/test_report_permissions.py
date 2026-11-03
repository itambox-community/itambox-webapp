from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from core.tests.mixins import TenantTestMixin, grant
from extras.models import ReportTemplate, ScheduledReport
from extras.views import ReportTemplateDownloadView, ReportTriggerImmediateView
from organization.models import Role, Tenant

User = get_user_model()


class ReportObjectPermissionTests(SimpleTestCase):
    def _assert_scoped_miss_denies(self, view_class):
        view = view_class()
        view.request = Mock()
        view.request.user = Mock()
        view.kwargs = {"pk": 42}

        with patch("extras.views.get_object_or_404", side_effect=Http404):
            self.assertFalse(view.has_permission())

        view.request.user.has_perms.assert_not_called()

    def test_scheduled_report_scoped_miss_does_not_fall_back_to_model_permission(self):
        self._assert_scoped_miss_denies(ReportTriggerImmediateView)

    def test_report_template_scoped_miss_does_not_fall_back_to_model_permission(self):
        self._assert_scoped_miss_denies(ReportTemplateDownloadView)


class ReportDomainPermissionViewTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Report Permission Tenant", slug="report-permission-tenant")
        self.template = ReportTemplate.objects.create(
            name="Asset Permission Report",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            tenant=self.tenant,
            included_columns=["asset_tag"],
        )

    def _client_with_permissions(self, username, permissions):
        user = User.objects.create_user(username=username, password="password123")
        role = Role.objects.create(tenant=self.tenant, name=f"Role {username}", permissions=permissions)
        grant(user, self.tenant, role)
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()
        return client, user

    def test_preview_domain_permission_is_required_beyond_template_permission(self):
        for username, permissions in (
            ("preview-template-only", ["extras.add_reporttemplate"]),
            (
                "preview-cross-tenant-only",
                ["extras.add_reporttemplate", "reports.view_cross_tenant_reports"],
            ),
        ):
            with self.subTest(username=username):
                client, _user = self._client_with_permissions(username, permissions)
                response = client.post(
                    reverse("extras:reporttemplate_preview"),
                    {
                        "name": "Preview",
                        "report_type": ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
                        "included_columns": ["asset_tag"],
                    },
                )
                self.assertEqual(response.status_code, 403)

    def test_download_html_and_machine_csv_require_domain_permission(self):
        client, _user = self._client_with_permissions("download-template-only", ["extras.view_reporttemplate"])
        url = reverse("extras:reporttemplate_download", kwargs={"pk": self.template.pk})
        for format_type in ("html", "machine_csv"):
            with self.subTest(format=format_type):
                response = client.get(url, {"format": format_type})
                self.assertEqual(response.status_code, 403)

    def test_pinned_download_still_requires_domain_permission_after_scope_reach(self):
        tenant_b = Tenant.objects.create(name="Report Permission Tenant B", slug="report-permission-tenant-b")
        user = User.objects.create_user(username="pinned-download-template-only", password="password123")
        for tenant in (self.tenant, tenant_b):
            role = Role.objects.create(
                tenant=tenant,
                name=f"Cross Scope {tenant.slug}",
                permissions=["extras.view_reporttemplate", "reports.view_cross_tenant_reports"],
            )
            grant(user, tenant, role)
        self.template.filter_tenants.add(self.tenant, tenant_b)
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

        response = client.get(
            reverse("extras:reporttemplate_download", kwargs={"pk": self.template.pk}),
            {"format": "html"},
        )

        self.assertEqual(response.status_code, 403)


class RunNowDomainPermissionTests(TestCase):
    def test_run_now_passes_the_invoking_principal_and_denies_without_domain_permission(self):
        tenant = Tenant.objects.create(name="Run Now Permission Tenant", slug="run-now-permission-tenant")
        user = User.objects.create_user(username="run-now-template-only", password="password123")
        role = Role.objects.create(
            tenant=tenant,
            name="Run Now Schedule Viewer",
            permissions=["extras.view_scheduledreport"],
        )
        grant(user, tenant, role)
        template = ReportTemplate.objects.create(
            name="Run Now Report",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            tenant=tenant,
            included_columns=["asset_tag"],
        )
        from extras.models import ScheduledReport

        sched = ScheduledReport.objects.create(
            name="Run Now Schedule",
            report=template,
            tenant=tenant,
            format=ScheduledReport.FORMAT_HTML,
            recipients="run-now@example.test",
            save_to_archive=True,
        )
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

        with (
            patch("extras.tasks.reports._deliver_report_email") as deliver_email,
            patch("extras.tasks.reports._deliver_report_channels") as deliver_channels,
        ):
            response = client.post(reverse("extras:scheduledreport_trigger", kwargs={"pk": sched.pk}))

        self.assertEqual(response.status_code, 302)
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "terminal: report.permission_denied")
        self.assertIsNone(sched.last_run_archive)
        deliver_email.assert_not_called()
        deliver_channels.assert_not_called()


class ReportTemplateDetailScheduleScopeTests(TenantTestMixin, TestCase):
    """F9: a global template's detail page lists only in-scope schedules."""

    def setUp(self):
        self.setup_tenant_context(name="F9 Tenant A", slug="f9-tenant-a", permissions=["extras.view_reporttemplate"])
        self.tenant_a = self.tenant
        self.tenant_b = Tenant.objects.create(name="F9 Tenant B", slug="f9-tenant-b")
        self.template = ReportTemplate.objects.create(
            name="F9 Global Template",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            included_columns=["asset_tag"],
        )
        self.url = reverse("extras:reporttemplate_detail", kwargs={"pk": self.template.pk})

    def _schedule(self, tenant, name):
        return ScheduledReport.objects.create(
            name=name, report=self.template, tenant=tenant, recipients="f9@example.com"
        )

    def test_detail_lists_only_own_schedules(self):
        self._schedule(self.tenant_a, "F9-SCHEDULE-A")
        self._schedule(self.tenant_b, "F9-SECRET-SCHEDULE-B")
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "F9-SCHEDULE-A")
        self.assertNotContains(response, "F9-SECRET-SCHEDULE-B")

    def test_foreign_only_schedule_is_not_listed(self):
        self._schedule(self.tenant_b, "F9-SECRET-SCHEDULE-B")
        self.client_login_to_tenant(self.tenant_user, self.tenant_a)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "F9-SECRET-SCHEDULE-B")
        self.assertEqual(len(response.context["schedules"]), 0)
