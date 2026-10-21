"""U2/U7: the surfaces the registry drives — markers, diagnostics, and OpenAPI.

The registry is only worth having if the maturity a domain declares is the
maturity a user, an operator, and an API client all see. These tests pin those
three readers to the same source.
"""

import os
from collections import namedtuple
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.management import call_command
from django.http import Http404
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings

from core.templatetags.utility_tags import asset_stamp
from itambox.api.openapi import CapabilityAwareAutoSchema
from itambox.capabilities import ALWAYS_ON, BETA, EXPERIMENTAL, SOURCE_ALWAYS, STABLE, registry
from itambox.tests.capability_harness import deactivatable_keys, deactivated, probe_failing
from itambox.views.generic.capability_notices import NOTICE_FIELDS, capability_notice

#: Stand-ins for a DRF viewset and its queryset: the schema class only ever
#: reads ``view.queryset.model``, so there is nothing else to imitate.
_StubQuerySet = namedtuple("_StubQuerySet", "model")
_StubView = namedtuple("_StubView", "queryset")
_APP_ROOT = Path(__file__).resolve().parents[2]


def _model_for_reference(reference):
    """The model class a registry ``owns`` reference names, or ``None``.

    Capabilities also own non-model references (API surfaces, doc namespaces),
    so the ownership walk has to ask the app registry instead of assuming.
    """
    from django.apps import apps as app_registry

    app_label, _, object_name = reference.partition(".")
    try:
        return app_registry.get_model(app_label, object_name)
    except LookupError:
        return None


class TestSurfaceMarker:
    """U2: a non-Stable model carries its owning capability's marker."""

    def test_owned_model_notices_follow_the_declared_maturity(self):
        """U2: a model's marker is its owning capability's declared grade.

        Subject and expectation come from the registry: which capability is
        still settling changes with every promotion, so a pinned example goes
        stale the moment the next slice stabilizes. Every owned model is
        walked; non-Stable owners must mark their models, Stable owners must
        not, and the published notice never carries the probe.
        """
        owned_models = 0
        for capability in registry.all():
            for reference in capability.owns:
                model = _model_for_reference(reference)
                if model is None:
                    continue
                owned_models += 1
                notice = capability_notice(model)
                if capability.maturity == STABLE:
                    assert notice is None, reference
                    continue
                assert notice is not None, reference
                assert notice["key"] == capability.key
                assert notice["maturity"] == capability.maturity
                assert notice["title"]
                assert notice["docs_url"].endswith(".md")
                assert notice["limitations"]
                assert "activation_probe" not in notice
        assert owned_models > 0, "the ownership walk resolved no model references"

    def test_stable_scheduled_models_yield_no_notice(self):
        from extras.models import ReportGenerationArchive, ScheduledReport

        assert capability_notice(ScheduledReport) is None
        assert capability_notice(ReportGenerationArchive) is None

    def test_a_stable_owned_model_yields_no_notice(self):
        from procurement.models import PurchaseOrder

        assert capability_notice(PurchaseOrder) is None

    def test_stable_webhook_models_yield_no_notice(self):
        from extras.models import EventRule, WebhookEndpoint

        assert capability_notice(WebhookEndpoint) is None
        assert capability_notice(EventRule) is None

    def test_stable_alert_models_yield_no_notice(self):
        from extras.models import AlertRule, NotificationChannel

        assert capability_notice(AlertRule) is None
        assert capability_notice(NotificationChannel) is None

    def test_an_unowned_model_yields_no_notice(self):
        from assets.models import Asset

        assert capability_notice(Asset) is None

    def test_an_experimental_marker_names_its_own_grade(self):
        notice = _notice_for_key("platform.plugins")
        assert notice["maturity"] == EXPERIMENTAL

    def test_a_notice_never_carries_the_probe_or_a_value(self):
        # The production field set is closed: the probe, its observed value,
        # the owning area, and the owned references never reach a template.
        assert "activation_probe" not in NOTICE_FIELDS
        assert "value_present" not in NOTICE_FIELDS
        notice = _notice_for_key("users.scim_provisioning")
        assert set(notice) == set(NOTICE_FIELDS)

    def test_a_deactivated_capability_still_marks_its_surface(self):
        """Inactive is not invisible: the grade is a property of the contract.

        The subjects come from the harness: every capability that can be
        observed inactive is proven, so the latest promotion cannot leave a
        pinned example behind that no longer deactivates.
        """
        keys = deactivatable_keys()
        assert keys
        for key in keys:
            with deactivated(key):
                assert _notice_for_key(key)["maturity"] == registry.get(key).maturity

    def test_the_notice_survives_a_failing_probe(self):
        keys = deactivatable_keys()
        assert keys
        for key in keys:
            with probe_failing(key):
                assert _notice_for_key(key)["key"] == key


class TestBannerTemplate:
    def test_the_banner_names_the_capability_and_links_its_document(self):
        notice = _settling_beta_notice()
        html = render_to_string("generic/includes/beta_banner.html", {"capability_notice": notice})
        assert "Beta" in html
        assert notice["title"] in html
        assert "capability-maturity" in html

    def test_the_contract_link_is_excluded_from_boost(self):
        """The docs link points outside the app shell: it must never be a
        boosted HTMX request (global hx-boost on <body> would otherwise swap
        the standalone docs page into the app layout and break the UI).
        Defense in depth next to the central boost-guard (static/src/boost-guard.ts).
        """
        html = render_to_string(
            "generic/includes/beta_banner.html",
            {"capability_notice": _settling_beta_notice()},
        )
        assert 'hx-boost="false"' in html

    def test_the_banner_renders_the_declared_limitations(self):
        notice = _settling_beta_notice()
        html = render_to_string("generic/includes/beta_banner.html", {"capability_notice": notice})
        assert notice["limitations"][0] in html

    def test_the_beta_banner_is_a_polite_live_region_with_a_label(self):
        html = render_to_string(
            "generic/includes/beta_banner.html",
            {"capability_notice": _settling_beta_notice()},
        )
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'aria-atomic="true"' in html
        assert 'aria-labelledby="beta-module-banner-title"' in html
        assert 'id="beta-module-banner-title"' in html

    def test_the_experimental_banner_is_announced_without_a_dismiss_control(self):
        html = render_to_string(
            "generic/includes/beta_banner.html",
            {"capability_notice": _notice_for_key("platform.plugins")},
        )
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'data-maturity="experimental"' in html
        assert "btn-close" not in html

    def test_the_maturity_badge_exposes_text_and_an_accessible_name(self):
        html = render_to_string(
            "generic/includes/capability_badge.html",
            {"capability_notice": _notice_for_key("platform.plugins")},
        )
        assert "aria-label=" in html
        assert 'aria-hidden="true"' not in html
        assert "Experimental" in html

    def test_the_banner_is_silent_without_a_notice(self):
        html = render_to_string("generic/includes/beta_banner.html", {})
        assert html.strip() == ""

    def test_the_legacy_flag_still_renders_a_banner(self):
        html = render_to_string("generic/includes/beta_banner.html", {"is_beta_module": True})
        assert "Beta" in html

    def test_the_badge_names_the_grade(self):
        html = render_to_string(
            "generic/includes/capability_badge.html",
            {"capability_notice": _notice_for_key("platform.plugins")},
        )
        assert "Experimental" in html

    def test_the_badge_is_silent_without_a_notice(self):
        html = render_to_string("generic/includes/capability_badge.html", {})
        assert html.strip() == ""


def _attribute(name: str, value: str) -> str:
    """Render one HTML attribute; concatenated so no f-string quotes a value."""
    return name + '="' + value + '"'


class TestAssetCacheBusting:
    """A redeploy of the same release must not leave browsers on the old assets."""

    def test_the_shells_stamp_both_assets_with_the_build(self):
        css_stamp = "?v={{ settings.VERSION }}-{% asset_version 'dist/itambox.css' %}"
        js_stamp = "{% asset_version 'dist/itambox.js' %}"
        for name in ("base.html", "base_public.html"):
            markup = (_APP_ROOT / "templates" / name).read_text(encoding="utf-8")
            assert css_stamp in markup, f"{name} does not stamp the stylesheet"
            assert js_stamp in markup, f"{name} does not stamp the bundle"

    def test_the_stamp_follows_the_built_file_not_the_release(self, tmp_path):
        built = tmp_path / "itambox.css"
        built.write_text("body { color: red; }", encoding="utf-8")
        os.utime(built, (1_700_000_000, 1_700_000_000))

        assert asset_stamp("dist/itambox.css", finder=lambda _path: str(built)) == "1700000000"
        assert asset_stamp("dist/itambox.css", finder=lambda _path: None) == str(settings.VERSION)


class TestTemplateCommentContracts:
    """Django's ``{# #}`` is single-line only; a wrapped one renders as page text."""

    def test_no_template_leaks_a_multi_line_hash_comment(self):
        offenders = []
        for template in sorted((_APP_ROOT / "templates").rglob("*.html")):
            for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
                if "{#" not in line or "#}" in line.split("{#", 1)[1]:
                    continue
                offenders.append(f"{template.relative_to(_APP_ROOT).as_posix()}:{number}")

        assert offenders == [], "multi-line {# #} comments render as page text: " + ", ".join(offenders)

    def test_the_theme_switch_ships_its_segmented_control_styles(self):
        menu = (_APP_ROOT / "templates" / "global_includes" / "_user_menu.html").read_text(encoding="utf-8")
        styles = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((_APP_ROOT / "static" / "src" / "styles").glob("*.scss"))
        )

        assert 'class="theme-switch"' in menu
        assert ".theme-switch {" in styles
        assert ".theme-switch-input:checked + label {" in styles


class TestAccessibilityTemplateContracts:
    def test_theme_switch_is_a_labelled_radio_group_in_the_user_menu(self):
        menu = (_APP_ROOT / "templates" / "global_includes" / "_user_menu.html").read_text(encoding="utf-8")

        assert 'role="radiogroup"' in menu
        assert "<legend" in menu, "the group needs an accessible name"
        assert menu.count('class="theme-switch-input"') == 3
        assert menu.count('name="theme-preference-{{ menu_scope }}"') == 3, (
            "the three segments share one name per render; a bare shared name would fold both shells "
            "into a single radio group and uncheck the hidden one"
        )
        theme_js = (_APP_ROOT / "static" / "src" / "theme.ts").read_text(encoding="utf-8")
        assert 'input[name^="theme-preference"]' in theme_js, (
            "the control sync has to match the scoped group names by prefix"
        )
        for name in ("Auto", "Dark", "Light"):
            assert f'<span class="visually-hidden">{{% translate "{name}" %}}</span>' in menu, (
                "the segments are icon-only: a visible option name widens its third of the track "
                "and the sliding thumb then misses the active segment"
            )
        for value in ("auto", "dark", "light"):
            assert _attribute("value", value) in menu
            assert _attribute("for", f"theme-preference-{{{{ menu_scope }}}}-{value}") in menu, (
                "every segment needs a label"
            )
        # The menu renders in both shells, so each one has to scope the ids.
        for shell_name, scope in (("global_includes/_topbar.html", "topbar"), ("layout.html", "mobile")):
            shell = (_APP_ROOT / "templates" / shell_name).read_text(encoding="utf-8")
            marker = '_user_menu.html" with menu_scope="' + scope + '"'
            assert marker in shell, f"{shell_name} must scope the menu ids"

    def test_no_shell_renders_a_theme_toggle_button(self):
        topbar = (_APP_ROOT / "templates" / "global_includes" / "_topbar.html").read_text(encoding="utf-8")
        layout = (_APP_ROOT / "templates" / "layout.html").read_text(encoding="utf-8")

        for name, markup in (("_topbar.html", topbar), ("layout.html", layout)):
            assert "color-mode-toggle" not in markup, f"{name} still renders a theme toggle"

        # The workspace switcher is the single tenant entry point on mobile now.
        assert "variant='compact'" in layout
        assert "mobile-tenant-switcher" not in layout, "the drawer switcher must be gone"
        assert "variant='full'" in topbar

    def test_page_shell_has_language_and_landmark_contracts(self):
        base = (_APP_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        public = (_APP_ROOT / "templates" / "base_public.html").read_text(encoding="utf-8")
        breadcrumbs = (_APP_ROOT / "templates" / "global_includes" / "_breadcrumbs.html").read_text(encoding="utf-8")

        for html in (base, public):
            assert "{% get_current_language as LANGUAGE_CODE %}" in html
            assert '<html lang="{{ LANGUAGE_CODE }}"' in html
        assert '<nav class="d-flex justify-content-between' in breadcrumbs
        assert "aria-label=\"{% translate 'Breadcrumb' %}\"" in breadcrumbs

    def test_dashboard_scroll_regions_are_named_and_keyboard_reachable(self):
        html = (_APP_ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")

        assert '<h1 class="visually-hidden">{% translate "Dashboard" %}' in html
        assert '<h2 class="card-title">' in html
        assert 'class="card-body overflow-auto"' in html
        assert 'tabindex="0"' in html
        assert 'aria-label="{{ entry.config.title }}"' in html

    def test_quick_search_has_a_stable_focus_target_and_name(self):
        html = (_APP_ROOT / "templates" / "htmx" / "quick_search.html").read_text(encoding="utf-8")

        assert 'id="quick-search-input"' in html
        assert "aria-label=\"{% translate 'Search' %}\"" in html

    def test_table_selectors_and_action_menus_are_named_native_controls(self):
        from assets.models import Manufacturer
        from assets.tables import AssetTable
        from core.tables.columns import ActionsColumn, ToggleColumn

        record = Manufacturer(pk=1, name="Accessibility Probe", slug="accessibility-probe")
        actions = str(ActionsColumn().render(record, table=None))

        assert '<button class="btn btn-sm btn-action dropdown-toggle' in actions
        assert 'aria-label="Toggle Dropdown"' in actions
        assert '<a class="btn btn-sm btn-action dropdown-toggle' not in actions
        assert str(ToggleColumn().attrs["input"]["aria-label"]) == "Select row"

        asset_table = AssetTable([])
        asset_table.request = SimpleNamespace(user=SimpleNamespace(has_perm=lambda *_args: True))
        asset_record = SimpleNamespace(pk=1, active_assignment=None, deleted_at=None)
        asset_actions = str(asset_table.render_actions(asset_record))

        assert '<button class="btn btn-sm btn-soft-success check-action' in asset_actions
        assert '<button class="btn btn-sm btn-action dropdown-toggle' in asset_actions
        assert 'aria-label="More actions"' in asset_actions
        assert '<a class="btn btn-sm btn-action dropdown-toggle' not in asset_actions

        edit_only_table = AssetTable([])
        edit_only_table.request = SimpleNamespace(
            user=SimpleNamespace(
                has_perm=lambda permission, _record: permission != "assets.delete_asset",
            )
        )
        edit_only_actions = str(edit_only_table.render_actions(asset_record))
        assert '<button class="btn btn-sm btn-action dropdown-toggle' in edit_only_actions
        assert 'title="Edit"' in edit_only_actions
        assert "Change log" in edit_only_actions
        assert 'title="Delete"' not in edit_only_actions

    def test_shared_toast_and_modal_errors_are_announced(self):
        toast = (_APP_ROOT / "templates" / "global_includes" / "_toast.html").read_text(encoding="utf-8")
        modal = (_APP_ROOT / "templates" / "generic" / "includes" / "add_stock_modal.html").read_text(encoding="utf-8")
        for html in (toast, modal):
            assert 'role="alert"' in html
            assert 'aria-live="assertive"' in html
            assert 'aria-atomic="true"' in html


@pytest.mark.django_db
class TestOperatorDiagnostics:
    """U7: class, mode, current state, source, and value presence — nothing else."""

    def test_the_command_reports_every_capability(self):
        output = _run_command()
        for capability in registry.all():
            assert capability.key in output

    def test_the_command_reports_mode_source_and_state(self):
        output = _run_command()
        assert "opt-in" in output
        # Derive the declared source kinds: a kind is asserted while a capability
        # still declares it, so promoting the last capability of a kind does not
        # pin this test to a vanished example.
        for source in sorted({capability.activation_source for capability in registry.all()}):
            assert source in output

    def test_the_command_reports_value_presence_not_the_value(self):
        with override_settings(PLUGINS=["demo_plugin_secret"]):
            output = _run_command()
        assert "demo_plugin_secret" not in output

    def test_a_failing_probe_is_reported_by_type_only(self):
        # Every capability that can be observed inactive is proven; the command
        # must surface the failure as its exception type, never its text.
        keys = deactivatable_keys()
        assert keys
        for key in keys:
            with probe_failing(key):
                output = _run_command()
            assert "RuntimeError" in output
            assert "hunter2" not in output

    def test_the_command_can_emit_json_rows(self):
        rows = _run_command("--format", "json")
        assert '"value_present"' in rows
        assert '"activation_probe"' not in rows


class TestOpenAPIMaturity:
    """The schema publishes the same grade the UI shows."""

    def test_a_stable_alert_owned_operation_is_annotated_stable(self):
        from extras.models import AlertRule

        assert _operation_for(AlertRule)["x-itambox-maturity"] == STABLE

    def test_a_stable_webhook_owned_operation_is_annotated_stable(self):
        from extras.models import WebhookEndpoint

        assert _operation_for(WebhookEndpoint)["x-itambox-maturity"] == STABLE

    def test_a_stable_owned_operation_is_annotated_stable(self):
        from procurement.models import PurchaseOrder

        assert _operation_for(PurchaseOrder)["x-itambox-maturity"] == STABLE

    def test_an_unowned_operation_is_not_annotated(self):
        from assets.models import Asset

        assert "x-itambox-maturity" not in _operation_for(Asset)

    def test_a_view_without_a_queryset_is_not_annotated(self):
        schema = CapabilityAwareAutoSchema()
        schema.view = object()
        assert schema.capability_maturity() is None

    def test_the_schema_class_is_wired_into_settings(self):
        from django.conf import settings

        assert settings.REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"].endswith("CapabilityAwareAutoSchema")

    def test_every_generated_scim_operation_is_annotated_stable(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        operations = [
            operation
            for path, path_item in schema["paths"].items()
            if "/scim/v2/" in path
            for method, operation in path_item.items()
            if method.lower() in {"get", "post", "put", "patch", "delete"}
        ]

        assert len(operations) == 26
        assert {operation.get("x-itambox-maturity") for operation in operations} == {STABLE}


@pytest.mark.serial_only
class TestNavigationMaturity:
    def test_procurement_navigation_is_not_marked_beta(self):
        from core.navigation.menu import OPERATIONS_MENU

        procurement = next(group for group in OPERATIONS_MENU.groups if str(group.label) == "Procurement")
        assert procurement.beta is False

    def test_report_designer_navigation_is_visible_with_the_stable_capability(self):
        from core.navigation.menu import MONITORING_MENU

        reporting = next(group for group in MONITORING_MENU.groups if str(group.label) == "Reporting")
        designer = next(item for item in reporting.items if str(item.link_text) == "Report Templates")
        scheduled = next(item for item in reporting.items if str(item.link_text) == "Scheduled Reports")
        assert designer.condition(None) is True
        assert scheduled.condition(None) is True
        # Every reporting slice is Stable now, so neither the group header nor
        # any item carries the Beta marker.
        assert reporting.beta is False
        assert designer.beta is False
        assert scheduled.beta is False


DESIGNER_VIEWS = (
    "ReportTemplateListView",
    "ReportTemplateDetailView",
    "ReportTemplateCreateView",
    "ReportTemplateUpdateView",
    "ReportTemplateDeleteView",
    "ReportTemplateBulkDeleteView",
    "ReportTemplatePreviewView",
    "ReportTemplateDownloadView",
)

#: The pk-less designer routes. The other routes would 404 on a missing row as
#: well, so an open gate is only unambiguous on these two without URL kwargs.
UNAMBIGUOUS_DESIGNER_VIEWS = ("ReportTemplateListView", "ReportTemplateBulkDeleteView")

SCHEDULED_REPORT_VIEWS = (
    "ScheduledReportListView",
    "ScheduledReportCreateView",
    "ScheduledReportUpdateView",
    "ScheduledReportDeleteView",
    "ScheduledReportBulkDeleteView",
    "ReportTriggerImmediateView",
    "ScheduledReportRetryDeliveryView",
    "ScheduledReportScopeApprovalView",
)


@pytest.mark.django_db
class TestReportDesignerStable:
    """The designer is a Stable, always-on capability after #565."""

    def test_the_operator_setting_is_removed_and_the_capability_is_always_on(self):
        capability = registry.get("reporting.designer")

        assert not hasattr(settings, "REPORT_DESIGNER_ENABLED")
        assert (capability.maturity, capability.activation, capability.activation_source) == (
            STABLE,
            ALWAYS_ON,
            SOURCE_ALWAYS,
        )
        assert registry.is_active("reporting.designer") is True

    @pytest.mark.parametrize("view_name", DESIGNER_VIEWS)
    def test_every_designer_route_names_the_capability_that_gates_it(self, view_name):
        assert _designer_view(view_name).capability_key == "reporting.designer"

    @pytest.mark.parametrize("view_name", UNAMBIGUOUS_DESIGNER_VIEWS)
    def test_designer_routes_are_open_by_default(self, view_name):
        assert _gate_outcome(view_name) != "Http404"

    def test_the_report_template_surface_is_marked_stable(self):
        from extras.models import ReportTemplate

        assert capability_notice(ReportTemplate) is None
        assert registry.owner_of("extras.ReportTemplate").maturity == STABLE


@pytest.mark.django_db
class TestScheduledReportRoutesUseTheScheduledCapability:
    """The scheduled routes bind to the scheduled capability, not the designer."""

    @pytest.mark.parametrize("view_name", SCHEDULED_REPORT_VIEWS)
    def test_every_scheduled_report_route_names_the_scheduled_capability(self, view_name):
        assert _designer_view(view_name).capability_key == "reporting.scheduled"

    @pytest.mark.parametrize("view_name", ("ScheduledReportListView", "ScheduledReportBulkDeleteView"))
    def test_scheduled_routes_are_open_with_the_stable_capability(self, view_name):
        assert _gate_outcome(view_name) != "Http404"


def _designer_view(view_name):
    from extras import views

    return getattr(views, view_name)


def _gate_outcome(view_name):
    """What the capability gate did, named rather than rendered.

    Returns ``"Http404"`` when the gate closed the route, and the name of
    whatever happened next otherwise -- a permission error, a wrong method, a
    response. The test asserts only on the gate, so downstream behaviour is
    deliberately not modelled. No URL kwargs are supplied: the gate runs before
    anything reads them, so needing one would itself mean the gate ran late.
    """
    from django.contrib.auth import get_user_model

    request = RequestFactory().get("/")
    request.user = get_user_model()(username="capability-probe", is_active=True)
    try:
        return type(_designer_view(view_name).as_view()(request)).__name__
    except Http404:
        return "Http404"
    except Exception as exc:
        return type(exc).__name__


def _notice_for_key(key):
    capability = registry.get(key)
    return {
        "key": capability.key,
        "title": capability.title,
        "maturity": capability.maturity,
        "activation": capability.activation,
        "docs_url": capability.docs_url,
        "limitations": capability.limitations,
    }


def _settling_beta_notice():
    """A Beta-grade notice for the banner contract.

    No capability settles as Beta anymore, so no registry entry renders the
    Beta banner: its Beta-specific contract (grade text, limitations, dismiss
    control) is pinned with this explicit notice, while the registry-backed
    rendering stays covered by the experimental entry.
    """
    return {
        **_notice_for_key("platform.plugins"),
        "key": "example.settling",
        "title": "Example Settling Capability",
        "maturity": BETA,
        "docs_url": "operations/capability-maturity.md",
        "limitations": ("Interfaces may still change until the capability stabilises.",),
    }


def _operation_for(model):
    schema = CapabilityAwareAutoSchema()
    schema.view = _StubView(_StubQuerySet(model))
    operation = {"operationId": "stub"}
    schema.annotate_capability_maturity(operation)
    return operation


def _run_command(*args):
    stdout = StringIO()
    call_command("capabilities", *args, stdout=stdout)
    return stdout.getvalue()
