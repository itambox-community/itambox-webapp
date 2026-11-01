from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

import pytest
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase

from core.models import ChangeLoggingMixin
from core.reports.columns import headers_for, label_for
from core.reports.rendering import (
    _custom_context,
    _safe_custom_value,
    render_report_html,
)
from extras.forms import ReportTemplateForm
from extras.models import ReportTemplate, ScheduledReport, ScheduledReportScopeAuthorization
from extras.tasks.reports import (
    _parse_authorized_scope,
    _principal_can_authorize_scope,
    _render_report_output,
    _resolve_authorized_principal,
    _resolve_report_scope,
    _resolve_scope_authorization,
    _scope_requires_authorization,
    _tenant_scope_permission_is_valid,
    generate_scheduled_report_task,
)
from extras.views import ReportTemplateDetailView, ReportTemplateDownloadView, ReportTemplatePreviewView
from organization.models import Tenant


class ReportDesignerIssue181ContractTests(SimpleTestCase):
    def test_model_and_form_keep_custom_html_but_retire_legacy_fields(self):
        fields = {field.name: field for field in ReportTemplate._meta.get_fields()}
        assert "template_content" in fields
        assert "advanced_mode" not in fields
        assert "legacy_designer_grandfathered" not in fields
        form = ReportTemplateForm()
        assert "template_content" in form.fields
        assert "advanced_mode" not in form.fields
        assert "legacy_designer_grandfathered" not in form.fields

    def test_unknown_columns_are_rejected_with_machine_key(self):
        with pytest.raises(ValidationError, match="not_published"):
            ReportTemplate(
                name="bad columns",
                report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
                included_columns=["asset_tag", "not_published"],
            ).clean()

    def test_removed_warranty_provider_key_is_rejected_with_machine_key(self):
        """The replaced warranty_provider key fails validation like any unknown key.

        The removal is deliberate and has no compatibility shim, so the machine
        key is reported back to the author instead of being silently mapped.
        """
        with pytest.raises(ValidationError, match="warranty_provider"):
            ReportTemplate(
                name="removed warranty column",
                report_type=ReportTemplate.REPORT_TYPE_WARRANTY_EXPIRATION,
                included_columns=["warranty_asset", "warranty_provider"],
            ).clean()

    def test_custom_jinja_context_does_not_expose_request_or_runtime_objects(self):
        template = SimpleNamespace(
            name="safe",
            template_content="{{ request.COOKIES|default('missing') }}|{{ report_name }}",
        )
        rendered = render_report_html(
            {"report_name": "safe", "request": SimpleNamespace(COOKIES={"sessionid": "secret"})}, template
        )
        assert "secret" not in rendered
        assert "missing" in rendered

    def test_custom_html_renders_for_scheduled_delivery(self):
        template = SimpleNamespace(
            name="custom",
            template_content="<h1>{{ report_name }}</h1>",
        )
        output = _render_report_output(
            SimpleNamespace(format=ScheduledReport.FORMAT_HTML),
            template,
            ["Asset Tag"],
            [{"Asset Tag": "A-1"}],
            {"report_name": "custom", "summary_cards": [], "grouped_data": {}},
        )
        assert output.email_body == "<h1>custom</h1>"

    def test_scheduled_delivery_renders_custom_html(self):
        template = SimpleNamespace(
            name="inactive",
            template_content="<h1>{{ report_name }}</h1>",
        )
        output = _render_report_output(
            SimpleNamespace(format=ScheduledReport.FORMAT_HTML),
            template,
            [],
            [],
            {"report_name": "inactive", "summary_cards": [], "grouped_data": {}},
        )
        assert output.email_body == "<h1>inactive</h1>"

    def test_worker_scope_authorization_checks_each_persisted_tenant(self):
        tenant_a = SimpleNamespace(pk=1)
        tenant_b = SimpleNamespace(pk=2)
        principal = Mock(is_active=True, pk=99)
        principal.has_perm.side_effect = lambda permission, obj=None: obj is tenant_a
        authorization = SimpleNamespace(scope_tenant_ids=[tenant_a.pk, tenant_b.pk], authorized_by=principal)
        authorization_manager = Mock()
        authorization_manager.filter.return_value.select_related.return_value.first.return_value = authorization
        schedule = SimpleNamespace(pk=123)

        with (
            patch("extras.tasks.reports.ScheduledReportScopeAuthorization.objects", authorization_manager),
            patch("extras.tasks.reports.TaskContext") as task_context,
            patch("extras.tasks.reports.get_current_user", return_value=principal),
        ):
            result = _resolve_scope_authorization(schedule, tenant_a, [tenant_a, tenant_b])

        assert result is None
        assert task_context.call_args_list == [
            call(tenant_id=tenant_a.pk, user_id=principal.pk, operation="reports.scope_authorization"),
            call(tenant_id=tenant_b.pk, user_id=principal.pk, operation="reports.scope_authorization"),
        ]
        assert principal.has_perm.call_args_list == [
            call("reports.view_cross_tenant_reports", obj=tenant_a),
            call("reports.view_cross_tenant_reports", obj=tenant_b),
        ]


class ReportDesignerIssue181CoverageTests(SimpleTestCase):
    def test_columns_resolve_only_canonical_keys(self):
        assert label_for("asset_tag") == "Asset Tag"
        assert label_for("warranty_supplier") == "Supplier"
        assert label_for("agreement_entitled_quantity") == "Agreement Entitled Quantity"
        assert headers_for(["asset_tag", "not_published", "name"]) == ["Asset Tag", "Asset Name"]
        assert headers_for(["warranty_provider"]) == []

    def test_custom_context_sanitizes_supported_values_and_drops_runtime_objects(self):
        runtime_object = object()
        assert _safe_custom_value(None) is None
        assert _safe_custom_value("text") == "text"
        assert _safe_custom_value(4) == 4
        assert _safe_custom_value(1.5) == 1.5
        assert _safe_custom_value(True) is True
        assert _safe_custom_value(date(2026, 8, 14)) == "2026-08-14"
        assert _safe_custom_value(datetime(2026, 8, 14, 5, 6, 7)) == "2026-08-14 05:06:07"
        assert _safe_custom_value(Decimal("12.50")) == "12.50"
        assert _safe_custom_value({1: [Decimal("2.00"), runtime_object]}) == {"1": ["2.00", None]}
        assert _safe_custom_value(("a", runtime_object)) == ["a", None]
        context = _custom_context({"report_name": "safe", "request": runtime_object, "ignored": "hidden"})
        assert context == {"report_name": "safe", "request": None}

    def test_custom_html_renders_with_autoescape_and_empty_custom_html_uses_curated_fallback(self):
        template = SimpleNamespace(template_content="<p>{{ report_name }}</p>")
        rendered = render_report_html({"report_name": "<safe>"}, template)
        assert "&lt;safe&gt;" in rendered

        template.template_content = ""
        fallback = render_report_html({"report_name": "curated"}, template)
        assert "curated" in fallback

    def test_scope_helpers_fail_closed_for_invalid_approvals(self):
        tenant_a = SimpleNamespace(pk=1)
        tenant_b = SimpleNamespace(pk=2)
        assert _scope_requires_authorization(tenant_a, []) is False
        assert _scope_requires_authorization(tenant_a, [tenant_a]) is False
        assert _scope_requires_authorization(tenant_a, [tenant_b]) is True
        assert _scope_requires_authorization(None, []) is True
        assert _parse_authorized_scope(SimpleNamespace(scope_tenant_ids=["2", 1, 2])) == [1, 2]
        assert _parse_authorized_scope(SimpleNamespace(scope_tenant_ids=["not-an-id"])) is None
        assert _resolve_authorized_principal(SimpleNamespace(authorized_by=None)) is None
        assert _resolve_authorized_principal(SimpleNamespace(authorized_by=SimpleNamespace(is_active=False))) is None
        assert _resolve_authorized_principal(SimpleNamespace(authorized_by=SimpleNamespace(is_active=True))) is not None

        class MissingPrincipal:
            @property
            def authorized_by(self):
                raise ObjectDoesNotExist

        assert _resolve_authorized_principal(MissingPrincipal()) is None

    def test_scope_permission_and_principal_checks_cover_denials_and_exceptions(self):
        tenant = SimpleNamespace(pk=7)
        principal = SimpleNamespace(pk=42)
        assert _tenant_scope_permission_is_valid(principal, SimpleNamespace(pk=None)) is False
        worker = Mock()
        worker.has_perm.return_value = False
        with (
            patch("extras.tasks.reports.TaskContext"),
            patch("extras.tasks.reports.get_current_user", return_value=worker),
        ):
            assert _tenant_scope_permission_is_valid(principal, tenant) is False
            worker.has_perm.return_value = True
            assert _tenant_scope_permission_is_valid(principal, tenant) is True

        with patch("extras.tasks.reports.TaskContext", side_effect=PermissionDenied):
            assert _tenant_scope_permission_is_valid(principal, tenant) is False
        with (
            patch("extras.tasks.reports.TaskContext"),
            patch("extras.tasks.reports.get_current_user", side_effect=ObjectDoesNotExist),
        ):
            assert _tenant_scope_permission_is_valid(principal, tenant) is False

        with patch("extras.tasks.reports._tenant_scope_permission_is_valid", side_effect=[True, False]) as check:
            assert _principal_can_authorize_scope(principal, [tenant, SimpleNamespace(pk=8)]) is False
            assert check.call_count == 2
        assert _principal_can_authorize_scope(principal, []) is True

    def test_scope_authorization_rejects_missing_mismatched_and_inactive_approvals(self):
        tenant_a = SimpleNamespace(pk=1)
        tenant_b = SimpleNamespace(pk=2)
        schedule = SimpleNamespace(pk=123)
        manager = Mock()
        manager.filter.return_value.select_related.return_value.first.return_value = None
        with patch("extras.tasks.reports.ScheduledReportScopeAuthorization.objects", manager):
            assert _resolve_scope_authorization(schedule, tenant_a, [tenant_a, tenant_b]) is None

        authorization = SimpleNamespace(scope_tenant_ids=[1], authorized_by=SimpleNamespace(is_active=True, pk=8))
        manager.filter.return_value.select_related.return_value.first.return_value = authorization
        with patch("extras.tasks.reports.ScheduledReportScopeAuthorization.objects", manager):
            assert _resolve_scope_authorization(schedule, tenant_a, [tenant_a, tenant_b]) is None

        authorization.scope_tenant_ids = [1, 2]
        authorization.authorized_by = SimpleNamespace(is_active=False, pk=8)
        with patch("extras.tasks.reports.ScheduledReportScopeAuthorization.objects", manager):
            assert _resolve_scope_authorization(schedule, tenant_a, [tenant_a, tenant_b]) is None

        with patch("extras.tasks.reports._principal_can_authorize_scope", return_value=False):
            with patch("extras.tasks.reports.ScheduledReportScopeAuthorization.objects", manager):
                assert _resolve_scope_authorization(schedule, tenant_a, [tenant_a, tenant_b]) is None

        with patch("extras.tasks.reports.ScheduledReportScopeAuthorization.objects") as unused:
            assert _resolve_scope_authorization(schedule, tenant_a, [tenant_a]) is None
            unused.filter.assert_not_called()

    def test_generate_task_stops_for_unauthorized_scope(self):
        broad_schedule = SimpleNamespace(
            pk=2,
            report=SimpleNamespace(),
            is_active=True,
        )
        manager = Mock()
        manager.get.return_value = broad_schedule
        tenant_a = SimpleNamespace(pk=1, id=1)
        tenant_b = SimpleNamespace(pk=2, id=2)
        with (
            patch("extras.tasks.reports.ScheduledReport.objects", manager),
            patch("extras.tasks.reports._resolve_report_scope", return_value=(tenant_a, [tenant_a, tenant_b])),
            patch("extras.tasks.reports._resolve_scope_authorization", return_value=None),
            patch("extras.tasks.reports._scope_requires_authorization", return_value=True),
        ):
            result = generate_scheduled_report_task(2)
        assert result.status.value == "terminal"
        assert result.code == "report.scope_unauthorized"

    def test_generate_task_uses_authorized_principal_without_ambient_tenant_for_broad_scope(self):
        schedule = SimpleNamespace(
            pk=3,
            report=SimpleNamespace(),
            is_active=True,
            save=Mock(),
        )
        tenant_a = SimpleNamespace(pk=1, id=1)
        tenant_b = SimpleNamespace(pk=2, id=2)
        authorized_principal_id = 99
        expected_result = SimpleNamespace(status="success")
        # The start-marker write bypasses the tenant-scoping default manager
        # via ``_base_manager`` (a ModelBase property that cannot be patched
        # directly), so the class reference itself is mocked; the conditional
        # update chain reports one row moved.
        scheduled_report = Mock()
        scheduled_report.objects.get.return_value = schedule
        scheduled_report._base_manager.filter.return_value.filter.return_value.update.return_value = 1

        with (
            patch("extras.tasks.reports.ScheduledReport", scheduled_report),
            patch("extras.tasks.reports._resolve_report_scope", return_value=(tenant_a, [tenant_a, tenant_b])),
            patch("extras.tasks.reports._resolve_scope_authorization", return_value=authorized_principal_id),
            patch("extras.tasks.reports._scope_requires_authorization", return_value=True),
            patch("extras.tasks.reports.TaskContext") as task_context,
            patch("extras.tasks.reports._process_scheduled_report", return_value=expected_result) as process_report,
        ):
            result = generate_scheduled_report_task(schedule.pk)

        assert result is expected_result
        task_context.assert_called_once_with(
            tenant_id=None,
            user_id=authorized_principal_id,
            all_accessible=True,
            operation="reports.generate",
        )
        process_report.assert_called_once_with(
            schedule, tenant_a, [tenant_a, tenant_b], run_started_at=schedule.last_run
        )

    def test_custom_html_can_be_edited(self):
        template = ReportTemplate(
            pk=5,
            name="custom html",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            included_columns=[],
            template_content="<p>new</p>",
        )
        with patch.object(ChangeLoggingMixin, "save") as parent_save:
            assert template.clean() is None
            template.save()
        parent_save.assert_called_once()

    def test_scheduled_report_scope_helpers_and_approval_are_database_independent(self):
        tenant_a = SimpleNamespace(pk=1)
        tenant_b = SimpleNamespace(pk=2)
        report_filter_manager = SimpleNamespace(all=lambda: [tenant_b])
        schedule = SimpleNamespace(
            filter_tenants=SimpleNamespace(all=lambda: []),
            report_id=99,
            report=SimpleNamespace(filter_tenants=report_filter_manager, tenant=tenant_a),
            tenant=tenant_a,
        )
        schedule.effective_scope_tenant_ids = lambda: ScheduledReport.effective_scope_tenant_ids(schedule)
        assert ScheduledReport.persisted_scope_tenant_ids(schedule) == [2]
        assert ScheduledReport.effective_scope_tenant_ids(schedule) == [2]
        assert ScheduledReport.scope_requires_authorization(schedule) is True

        schedule.filter_tenants = SimpleNamespace(all=lambda: [tenant_a, tenant_b])
        assert ScheduledReport.persisted_scope_tenant_ids(schedule) == [1, 2]
        assert ScheduledReport.effective_scope_tenant_ids(schedule) == [1, 2]
        assert ScheduledReport.scope_requires_authorization(schedule) is True

        schedule.filter_tenants = SimpleNamespace(all=lambda: [])
        schedule.report = SimpleNamespace(filter_tenants=SimpleNamespace(all=lambda: []), tenant=None)
        schedule.tenant = tenant_a
        assert ScheduledReport.persisted_scope_tenant_ids(schedule) == []
        assert ScheduledReport.effective_scope_tenant_ids(schedule) == [1]
        assert ScheduledReport.scope_requires_authorization(schedule) is False

        actor = Mock(is_active=False)
        with pytest.raises(PermissionDenied):
            ScheduledReportScopeAuthorization.approve(schedule, actor)
        actor.is_active = True
        actor.has_perm.return_value = True
        schedule.scope_requires_authorization = lambda: False
        with pytest.raises(ValidationError, match="does not need"):
            ScheduledReportScopeAuthorization.approve(schedule, actor)

        schedule.scope_requires_authorization = lambda: True
        schedule.effective_scope_tenant_ids = lambda: [1, 2]
        authorization = SimpleNamespace(scope_tenant_ids=[1, 2])
        authorization_manager = Mock()
        authorization_manager.update_or_create.return_value = (authorization, True)
        with patch.object(ScheduledReportScopeAuthorization, "objects", authorization_manager):
            assert ScheduledReportScopeAuthorization.approve(schedule, actor) is authorization
        authorization_manager.update_or_create.assert_called_once_with(
            scheduled_report=schedule,
            defaults={
                "authorized_by": actor,
                "scope_tenant_ids": [1, 2],
                "approved_at": ANY,
                "revoked_by": None,
                "revoked_at": None,
            },
        )

    def test_report_scope_uses_persisted_scope_without_owner_fallback(self):
        tenant_a = SimpleNamespace(pk=1)
        tenant_b = SimpleNamespace(pk=2)
        schedule = SimpleNamespace(
            pk=17,
            tenant=tenant_a,
            report=SimpleNamespace(tenant=tenant_a),
            persisted_scope_tenant_ids=lambda: [2],
            effective_scope_tenant_ids=lambda: [1, 2],
        )
        query = Mock()
        query.order_by.return_value = [tenant_b]
        tenant_model = SimpleNamespace(_base_manager=SimpleNamespace(filter=Mock(return_value=query)))
        with patch("django.apps.apps.get_model", return_value=tenant_model) as get_model:
            assert _resolve_report_scope(schedule) == (tenant_a, [tenant_b])
        get_model.assert_called_once_with("organization", "Tenant")
        tenant_model._base_manager.filter.assert_called_once_with(pk__in=[2], deleted_at__isnull=True)

        owner_only = SimpleNamespace(
            pk=18,
            tenant=tenant_a,
            report=SimpleNamespace(tenant=tenant_a),
            persisted_scope_tenant_ids=lambda: [],
            effective_scope_tenant_ids=lambda: [1],
        )
        assert _resolve_report_scope(owner_only) == (tenant_a, [])

    def test_report_template_persisted_filter_scope_uses_the_unscoped_through_manager(self):
        queryset = Mock()
        queryset.values_list.return_value = [3, 2, 3]
        through_manager = Mock()
        through_manager.filter.return_value = queryset
        template = SimpleNamespace(
            pk=27,
            filter_tenants=SimpleNamespace(through=SimpleNamespace(_base_manager=through_manager)),
        )

        assert ReportTemplate.persisted_filter_tenant_ids(template) == [2, 3]
        through_manager.filter.assert_called_once_with(reporttemplate_id=27)
        queryset.values_list.assert_called_once_with("tenant_id", flat=True)

    def test_scheduled_reports_promotion_drops_the_row_probe_and_limitations(self):
        """The scheduled slice is Stable/always-on; the row probe is gone."""
        from extras import apps as extras_apps

        assert not hasattr(extras_apps, "_scheduled_reports_probe")

        from itambox.capabilities import registry

        capability = registry.get("reporting.scheduled")
        assert capability.activation_probe is None
        assert capability.limitations == ()

    def test_report_views_cover_preview_permissions_and_rendering_seams(self):
        from core.reports.contracts import ReportPermissionDenied

        preview = ReportTemplatePreviewView()
        preview.request = SimpleNamespace(user=Mock(has_perm=Mock(side_effect=[False, True])))
        assert preview.has_permission() is True
        preview.request.user.has_perm.reset_mock(side_effect=True)
        preview.request.user.has_perm.return_value = True
        assert preview.has_permission() is True

        request = RequestFactory().post(
            "/preview/",
            {
                "report_type": "asset_summary",
                "included_columns": ["asset_tag"],
                "include_summary_cards": "true",
                "template_content": "<h1>{{ report_name }}</h1>",
                "description": "preview",
            },
        )
        request.user = SimpleNamespace(is_superuser=False)
        with (
            patch("extras.views.get_current_tenant", return_value=None),
            patch(
                "core.reports.build_report_context",
                return_value=([], [], [], {}, "", {"report_name": "preview"}),
            ),
            patch("extras.views.render_report_html", return_value="<h1>preview</h1>") as render,
        ):
            response = preview.post(request)
        assert response.status_code == 200
        assert response.content == b"<h1>preview</h1>"
        render.assert_called_once()

        with patch("core.reports.build_report_context", side_effect=ReportPermissionDenied("domain permission")):
            response = preview.post(request)
        assert response.status_code == 403
        assert response.content == b"You may not view this report's data."

    def test_preview_rejects_unknown_columns_before_building_report_context(self):
        request = RequestFactory().post(
            "/preview/",
            {
                "report_type": "asset_summary",
                "included_columns": ["asset_tag", "not_published"],
            },
        )
        request.user = SimpleNamespace(is_superuser=False)
        with (
            patch("extras.views.get_current_tenant", return_value=None),
            patch(
                "core.reports.build_report_context",
                return_value=([], [], [], {}, "", {"report_name": "preview"}),
            ) as build_context,
            patch("extras.views.render_report_html", return_value="<h1>preview</h1>"),
        ):
            response = ReportTemplatePreviewView().post(request)

        assert response.status_code == 400
        assert b"Invalid report template configuration." in response.content
        assert b"Unknown report columns" in response.content
        assert b"not_published" in response.content
        build_context.assert_not_called()

    def test_report_template_detail_and_download_cover_csv_html_and_pdf(self):
        from core.reports.contracts import ReportPermissionDenied

        detail = ReportTemplateDetailView()
        detail_template = SimpleNamespace(name="Report", schedules=Mock())
        detail.get_object = Mock(return_value=detail_template)
        with patch("extras.views.ObjectDetailView.get_context_data", return_value={}):
            context = detail.get_context_data()
        assert context["schedules"] is detail_template.schedules.all.return_value

        template = SimpleNamespace(
            name="My Report",
            filter_tenants=SimpleNamespace(all=lambda: []),
            persisted_filter_tenant_ids=lambda: [],
            report_type="asset_summary",
            template_content="",
        )
        build_context = (["Asset Tag"], [{"Asset Tag": "A-1"}], [], {}, "", {"report_name": "My Report"})
        with (
            patch("extras.views.get_object_or_404", return_value=template),
            patch("extras.views.get_current_tenant", return_value=None),
            patch("core.reports.build_report_context", return_value=build_context),
            patch("extras.views.render_report_csv", return_value="Asset Tag\nA-1\n"),
        ):
            csv_response = ReportTemplateDownloadView().get(SimpleNamespace(GET={"format": "csv"}), 1)
        assert csv_response.status_code == 200
        assert 'filename="my_report_' in csv_response["Content-Disposition"]

        with (
            patch("extras.views.get_object_or_404", return_value=template),
            patch("extras.views.get_current_tenant", return_value=None),
            patch("core.reports.build_report_context", return_value=build_context),
            patch("extras.views.render_report_html", return_value="<p>html</p>"),
        ):
            html_response = ReportTemplateDownloadView().get(SimpleNamespace(GET={"format": "html"}), 1)
        assert html_response["Content-Type"].startswith("text/html")

        with (
            patch("extras.views.get_object_or_404", return_value=template),
            patch("extras.views.get_current_tenant", return_value=None),
            patch("core.reports.build_report_context", return_value=build_context),
            patch("extras.views.render_report_html", return_value="<p>html</p>"),
            patch("extras.views.report_pdf_bytes", return_value=b"pdf"),
        ):
            pdf_response = ReportTemplateDownloadView().get(SimpleNamespace(GET={"format": "pdf", "print": "true"}), 1)
        assert pdf_response["Content-Type"].startswith("application/pdf")
        assert pdf_response["Content-Disposition"].startswith("inline;")

        with (
            patch("extras.views.get_object_or_404", return_value=template),
            patch("extras.views.get_current_tenant", return_value=None),
            patch("core.reports.build_report_context", side_effect=ReportPermissionDenied("domain permission")),
        ):
            denied_response = ReportTemplateDownloadView().get(SimpleNamespace(GET={"format": "csv"}), 1)
        assert denied_response.status_code == 403


class ReportTemplateFilterTenantWriteTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Filter Tenant A", slug="filter-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Filter Tenant B", slug="filter-tenant-b")
        self.template = ReportTemplate.objects.create(
            name="Filter Template",
            report_type=ReportTemplate.REPORT_TYPE_ASSET_SUMMARY,
            tenant=self.tenant_a,
        )
        self.template.filter_tenants.add(self.tenant_a)
        self.admin = SimpleNamespace(is_superuser=True, is_staff=True)
        self.current_user = patch("extras.forms.get_current_user", return_value=self.admin)
        self.current_user.start()
        self.addCleanup(self.current_user.stop)

    def _form(self, filter_tenant):
        return ReportTemplateForm(
            data={
                "name": self.template.name,
                "description": self.template.description,
                "report_type": self.template.report_type,
                "included_columns": [],
                "include_summary_cards": "on",
                "include_distribution_chart": "",
                "group_by_field": "",
                "style_preset": self.template.style_preset,
                "template_content": "",
                "tenant": str(self.tenant_a.pk),
                "filter_tenants": [str(filter_tenant.pk)],
            },
            instance=ReportTemplate.objects.get(pk=self.template.pk),
        )

    def test_template_can_change_filter_tenant_through_form_save(self):
        form = self._form(self.tenant_b)
        assert form.is_valid()
        form.save()
        self.template.refresh_from_db()
        assert set(self.template.filter_tenants.values_list("pk", flat=True)) == {self.tenant_b.pk}

    def test_template_can_change_filter_tenant_through_deferred_save_m2m(self):
        form = self._form(self.tenant_b)
        assert form.is_valid()
        form.save(commit=False)
        form.save_m2m()
        assert set(self.template.filter_tenants.values_list("pk", flat=True)) == {self.tenant_b.pk}

    def test_template_can_save_unchanged_filter_tenants(self):
        form = self._form(self.tenant_a)
        assert form.is_valid()
        form.save(commit=False)
        form.save_m2m()
        assert set(self.template.filter_tenants.values_list("pk", flat=True)) == {self.tenant_a.pk}
