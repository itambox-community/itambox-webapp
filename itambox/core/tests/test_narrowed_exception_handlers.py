"""Behaviour of handlers that were narrowed from ``except Exception``.

Each handler now names the failure it expects. These tests pin both halves of
that contract: the expected failure still degrades exactly as before, and an
unrelated error is no longer swallowed on the way.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

from crispy_forms.utils import render_crispy_form
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import NoReverseMatch

from assets.forms import AssetDisposalForm, AssetReservationForm, WarrantyForm
from core.tests.mixins import TenantTestMixin
from extras.tables import JournalEntryTable
from extras.utils import resolve_generic_items
from licenses.forms import LicenseSeatAssignmentForm
from organization.models import Tenant
from software.forms import InstalledSoftwareForm


class TestCancelUrlFallback(TenantTestMixin, TestCase):
    """Forms fall back to ``/`` only when the list route is missing."""

    CASES = (
        ("assets.forms.disposal_form.reverse", AssetDisposalForm),
        ("assets.forms.reservation_form.reverse", AssetReservationForm),
        ("assets.forms.warranty_form.reverse", WarrantyForm),
        ("licenses.forms.reverse", LicenseSeatAssignmentForm),
        ("software.forms.reverse", InstalledSoftwareForm),
    )

    def setUp(self):
        self.setup_tenant_context()
        self.set_active_tenant(self.tenant)

    def test_missing_route_falls_back_to_root(self):
        for target, form_class in self.CASES:
            with self.subTest(form=form_class.__name__), patch(target, side_effect=NoReverseMatch("gone")):
                self.assertIn('href="/"', render_crispy_form(form_class()))

    def test_unrelated_error_is_not_swallowed(self):
        for target, form_class in self.CASES:
            with self.subTest(form=form_class.__name__), patch(target, side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    form_class()


class _LinklessObject:
    def __init__(self, error):
        self._error = error

    def get_absolute_url(self):
        raise self._error

    def __str__(self):
        return "linkless"


class TestJournalEntryTableLink(SimpleTestCase):
    def test_object_without_a_route_renders_as_plain_text(self):
        table = JournalEntryTable([])
        self.assertEqual(table.render_content_object(_LinklessObject(NoReverseMatch("x"))), "linkless")

    def test_unrelated_error_is_not_swallowed(self):
        table = JournalEntryTable([])
        with self.assertRaises(RuntimeError):
            table.render_content_object(_LinklessObject(RuntimeError("boom")))


class TestResolveGenericItemsFallbacks(TestCase):
    def _row(self, obj):
        return SimpleNamespace(
            pk=1, model=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk, created=None, id=1
        )

    def test_unroutable_object_and_toggle_url_fall_back_to_hash(self):
        tenant = Tenant.objects.create(name="Fallback Tenant", slug="fallback-tenant")
        row = self._row(tenant)
        with (
            patch.object(Tenant, "get_absolute_url", side_effect=NoReverseMatch("x"), create=True),
            patch("extras.utils.reverse", side_effect=NoReverseMatch("y")),
        ):
            (item,) = resolve_generic_items([row], toggle_url_name="extras:bookmark_toggle")
        self.assertEqual(item["url"], "#")
        self.assertEqual(item["toggle_url"], "#")

    def test_unrelated_error_is_not_swallowed(self):
        tenant = Tenant.objects.create(name="Boom Tenant", slug="boom-tenant")
        row = self._row(tenant)
        with patch.object(Tenant, "get_absolute_url", side_effect=RuntimeError("boom"), create=True):
            with self.assertRaises(RuntimeError):
                resolve_generic_items([row])


class TestAssetRequestStateTransition(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context()
        self.set_active_tenant(self.tenant)

    def _transition_errors(self, request):
        try:
            request.clean()
        except ValidationError as exc:
            return [m for m in exc.messages if "Invalid state transition" in str(m)]
        return []

    def test_request_without_a_stored_row_has_no_previous_state(self):
        from assets.models import AssetRequest

        unsaved = AssetRequest(pk=987654321, status="fulfilled", tenant=self.tenant)
        self.assertEqual(self._transition_errors(unsaved), [])

    def test_stored_request_enforces_the_state_machine(self):
        from model_bakery import baker

        from assets.models import AssetRequest

        stored = baker.make(
            AssetRequest,
            tenant=self.tenant,
            status="denied",
            asset_type=baker.make("assets.AssetType", requestable=True),
        )
        stored.status = "approved"
        self.assertEqual(len(self._transition_errors(stored)), 1)
        stored.status = "denied"
        self.assertEqual(self._transition_errors(stored), [])


class TestSearchResultListUrlFallback(TestCase):
    """A result table for a model without a list route still renders, with a guessed link."""

    def _get(self, reverse_side_effect):
        from itambox.views.utility import SearchView
        from organization.models import Tenant as ResultModel

        class Backend:
            def search(self, query, *, user, obj_types, lookup):
                return {ResultModel: {"count": 1, "queryset": ResultModel.objects.none()}}

        class Table:
            def __init__(self, rows, request=None):
                pass

        request = RequestFactory().get("/search/?q=needle")
        request.user = SimpleNamespace(is_authenticated=True)
        with (
            patch("itambox.views.utility.import_string", return_value=Backend),
            patch("itambox.views.utility.get_table_for_model", return_value=Table),
            patch("itambox.views.utility.reverse", side_effect=reverse_side_effect),
            patch.object(SearchView, "render_to_response", side_effect=lambda ctx: ctx),
        ):
            return SearchView().get(request), ResultModel

    def test_missing_list_route_falls_back_to_a_guessed_path(self):
        def fake_reverse(name, *args, **kwargs):
            if name.endswith("_list"):
                raise NoReverseMatch(name)
            return "/dashboard/"

        context, model = self._get(fake_reverse)
        self.assertEqual(context["results"][model]["list_url"], "/organization/tenants/")

    def test_unrelated_error_is_not_swallowed(self):
        def fake_reverse(name, *args, **kwargs):
            if name.endswith("_list"):
                raise RuntimeError("boom")
            return "/dashboard/"

        with self.assertRaises(RuntimeError):
            self._get(fake_reverse)


class TestSavedFilterListUrlFallback(TestCase):
    def _list_url(self, side_effect):
        from extras.views import SavedFilterSaveView

        view = SavedFilterSaveView()
        request = RequestFactory().post("/extras/saved-filters/save/", {})

        def fake_reverse(name, *args, **kwargs):
            if name == "extras:savedfilter_list":
                return "/extras/saved-filters/"
            raise side_effect

        with patch("extras.views.reverse", side_effect=fake_reverse):
            return view._list_url(request, "organization.tenant")

    def test_model_without_list_route_returns_to_the_saved_filter_list(self):
        self.assertEqual(self._list_url(NoReverseMatch("x")), "/extras/saved-filters/")

    def test_unrelated_error_is_not_swallowed(self):
        with self.assertRaises(RuntimeError):
            self._list_url(RuntimeError("boom"))


class TestJsonPatternError(SimpleTestCase):
    def test_malformed_schema_pattern_is_not_a_user_error(self):
        from core.validators import _json_pattern_error

        self.assertIsNone(_json_pattern_error("code", "abc", {"pattern": "("}))

    def test_non_matching_value_is_reported(self):
        from core.validators import _json_pattern_error

        self.assertIsInstance(_json_pattern_error("code", "xyz", {"pattern": "^a"}), ValidationError)
        self.assertIsNone(_json_pattern_error("code", "abc", {"pattern": "^a"}))


class TestNarrowQuerysetToTenant(SimpleTestCase):
    def test_model_with_a_tenant_field_is_filtered_directly(self):
        from unittest.mock import MagicMock

        from assets.models import Asset
        from extras.dashboard.widgets import _narrow_queryset_to_tenant

        qs = MagicMock()
        _narrow_queryset_to_tenant(qs, Asset, 7)
        qs.filter.assert_called_once_with(tenant_id=7)

    def test_model_without_tenant_field_uses_its_lookup_or_stays_unfiltered(self):
        from unittest.mock import MagicMock

        from extras.dashboard.widgets import _narrow_queryset_to_tenant

        qs = MagicMock()
        with patch.object(ContentType, "tenant_lookup", "asset", create=True):
            _narrow_queryset_to_tenant(qs, ContentType, 7)
        qs.filter.assert_called_once_with(asset_id=7)

        unfiltered = MagicMock()
        self.assertIs(_narrow_queryset_to_tenant(unfiltered, ContentType, 7), unfiltered)


class TestFilterSetLookupWithoutFiltersModule(SimpleTestCase):
    def test_app_without_filters_module_has_no_filterset(self):
        from extras.export_views import get_filterset_for_model

        with (
            patch("extras.export_views.registry.get_filter_set", return_value=None),
            patch("extras.export_views.importlib.import_module", side_effect=ImportError("no module")),
        ):
            self.assertIsNone(get_filterset_for_model(ContentType))


class TestChoiceFieldCoercion(SimpleTestCase):
    def _field(self):
        from itambox.api.fields import ChoiceField

        return ChoiceField(choices=[(1, "One"), ("a", "A"), (True, "Yes")])

    def test_numeric_text_is_coerced_and_other_text_is_kept(self):
        field = self._field()
        self.assertEqual(field.to_internal_value("1"), 1)
        self.assertEqual(field.to_internal_value("a"), "a")

    def test_unknown_values_are_rejected(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        field = self._field()
        for value in ("zzz", "42", {"x": 1}, ["a"]):
            with self.subTest(value=value), self.assertRaises(DRFValidationError):
                field.to_internal_value(value)

    def test_unhashable_scalar_is_rejected_not_crashed(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        with self.assertRaises(DRFValidationError):
            self._field().to_internal_value(({"nested": 1},))


class TestPaginationLimit(SimpleTestCase):
    def _limit(self, params, max_page_size=None):
        from django.test import override_settings

        from itambox.api.pagination import ITAMBoxPagination

        request = SimpleNamespace(query_params=params)
        with override_settings(MAX_PAGE_SIZE=max_page_size, PAGINATE_COUNT=50):
            return ITAMBoxPagination().get_limit(request)

    def test_missing_malformed_negative_and_zero_limits_keep_the_default(self):
        for params in ({}, {"limit": "abc"}, {"limit": "-5"}, {"limit": "0"}):
            with self.subTest(params=params):
                self.assertEqual(self._limit(params), 50)

    def test_valid_limit_is_used_and_capped(self):
        self.assertEqual(self._limit({"limit": "10"}), 10)
        self.assertEqual(self._limit({"limit": "500"}, max_page_size=100), 100)
        self.assertEqual(self._limit({"limit": "0"}, max_page_size=20), 20)


class TestPrettyJson(SimpleTestCase):
    def test_json_text_is_reformatted_and_invalid_text_is_returned_unchanged(self):
        from core.templatetags.utility_tags import pretty_json

        self.assertEqual(pretty_json('{"a":1}'), '{\n  "a": 1\n}')
        self.assertEqual(pretty_json("not json"), "not json")
        self.assertEqual(pretty_json({1, 2}), str({1, 2}))
        self.assertEqual(pretty_json(None), "")


class TestChangeTenantAttribution(TenantTestMixin, TestCase):
    """A `tenant` that cannot be resolved falls back to the active tenant; other errors surface."""

    def setUp(self):
        self.setup_tenant_context()
        self.set_active_tenant(self.tenant)

    def _rename_asset(self, error):
        from model_bakery import baker

        from assets.models import Asset, StatusLabel
        from core.tasks.context import TaskContext

        original = Asset.tenant

        def broken_tenant(instance):
            # Only the change logger sees the broken link; Django's own save path keeps working.
            if sys._getframe(1).f_code.co_name == "_log_change":
                raise error
            return original.__get__(instance, Asset)

        asset = baker.make(Asset, tenant=self.tenant, status=baker.make(StatusLabel, type="deployable"))
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.tenant_admin.pk):
            asset.snapshot()
            asset.name = "Renamed"
            with patch.object(Asset, "tenant", property(broken_tenant)):
                asset.save()
        return asset

    def test_unresolvable_tenant_falls_back_to_the_active_tenant(self):
        from core.models import ObjectChange

        asset = self._rename_asset(ObjectDoesNotExist("gone"))
        change = ObjectChange._base_manager.filter(changed_object_id=asset.pk, action="update").latest("pk")
        self.assertEqual(change.tenant, self.tenant)

    def test_unexpected_error_is_not_swallowed(self):
        with self.assertRaises(RuntimeError):
            self._rename_asset(RuntimeError("boom"))


class TestSubscriptionAssignmentFormTarget(TenantTestMixin, TestCase):
    """The subscription choices narrow to the target's tenant when the target can be found."""

    def setUp(self):
        from assets.models import Supplier
        from subscriptions.models import Subscription, SubscriptionStatusChoices

        self.setup_tenant_context()
        self.set_active_tenant(self.tenant)
        supplier = Supplier.objects.create(name="Narrowing Supplier", tenant=self.tenant)
        self.active = Subscription.objects.create(
            name="Active", supplier=supplier, tenant=self.tenant, status=SubscriptionStatusChoices.ACTIVE
        )
        self.cancelled = Subscription.objects.create(
            name="Cancelled", supplier=supplier, tenant=self.tenant, status=SubscriptionStatusChoices.CANCELLED
        )

    def _choices(self, content_type, object_id):
        from subscriptions.forms import SubscriptionAssignmentForm

        form = SubscriptionAssignmentForm(content_type=content_type, object_id=object_id)
        return set(form.fields["subscription"].queryset.values_list("pk", flat=True))

    def test_target_with_a_tenant_limits_choices_to_active_subscriptions(self):
        from model_bakery import baker

        from assets.models import Asset, StatusLabel

        asset = baker.make(Asset, tenant=self.tenant, status=baker.make(StatusLabel, type="deployable"))
        choices = self._choices(ContentType.objects.get_for_model(Asset), asset.pk)
        self.assertEqual(choices, {self.active.pk})

    def test_unresolvable_targets_leave_the_choices_unfiltered(self):
        from assets.models import Asset

        everything = {self.active.pk, self.cancelled.pk}
        missing_object = self._choices(ContentType.objects.get_for_model(Asset), 987654321)
        gone_model = self._choices(ContentType(app_label="no_such_app", model="gone"), 1)
        self.assertEqual(missing_object, everything)
        self.assertEqual(gone_model, everything)


class TestEventNotificationTargetUrl(TestCase):
    def _notify(self, object_id=None, absolute_url_error=None):
        from core.models import Notification
        from extras.services.events import _send_notification

        tenant = Tenant.objects.create(name="Notified", slug="notified")
        event = SimpleNamespace(
            action="update",
            model=ContentType.objects.get_for_model(Tenant),
            object_id=tenant.pk if object_id is None else object_id,
            data={},
        )
        rule = SimpleNamespace(action_config={}, tenant_id=None)
        if absolute_url_error is None:
            _send_notification(rule, event)
        else:
            with patch.object(Tenant, "get_absolute_url", side_effect=absolute_url_error, create=True):
                _send_notification(rule, event)
        return tenant, Notification.objects.latest("pk")

    def test_unparsable_object_id_yields_a_notification_without_link(self):
        _, notification = self._notify("not-a-number")
        self.assertEqual(notification.target_url, "")

    def test_unroutable_object_yields_a_notification_without_link(self):
        _, notification = self._notify(absolute_url_error=NoReverseMatch("x"))
        self.assertEqual(notification.target_url, "")

    def test_unexpected_lookup_failure_is_not_swallowed(self):
        with self.assertRaises(RuntimeError):
            self._notify(absolute_url_error=RuntimeError("boom"))


class TestSeedHelpers(TestCase):
    def test_reload_reports_whether_the_row_still_exists(self):
        from core.management.commands._seed.engine import ChangeLogEngine
        from extras.models import Tag

        tag = Tag.objects.create(name="seed-reload", slug="seed-reload")
        self.assertTrue(ChangeLogEngine._reload(tag))
        Tag.objects.filter(pk=tag.pk).delete()
        self.assertFalse(ChangeLogEngine._reload(tag))

    def test_tenant_currency_defaults_to_eur_when_unknown(self):
        from core.management.commands._seed.lifecycle import SeedLifecycleMixin

        mixin = SeedLifecycleMixin()
        asset = SimpleNamespace(tenant_id=1, tenant=SimpleNamespace(slug="acme"))
        self.assertEqual(mixin._tenant_currency(asset), "EUR")
        mixin._tenant_meta = {"acme": {"currency": "CHF"}}
        self.assertEqual(mixin._tenant_currency(asset), "CHF")
        self.assertEqual(mixin._tenant_currency(SimpleNamespace(tenant_id=1, tenant=SimpleNamespace(slug="x"))), "EUR")
        self.assertEqual(mixin._tenant_currency(SimpleNamespace(tenant_id=None, tenant=None)), "EUR")


class TestAnnotatedBestEffortHandlers(TestCase):
    """Deliberate catch-alls keep degrading, and now leave a log trail."""

    def test_footer_links_of_a_failing_widget_are_empty_and_logged(self):
        from extras.templatetags.dashboard import get_widget_footer_links

        class Widget:
            def __init__(self, config):
                pass

            def get_footer_links(self, request):
                raise RuntimeError("SECRET-INTERNAL-DETAIL")

        context = {"request": RequestFactory().get("/")}
        with (
            patch("extras.templatetags.dashboard.get_widget", return_value=Widget),
            self.assertLogs("extras.templatetags.dashboard", level="ERROR") as logs,
        ):
            self.assertEqual(get_widget_footer_links(context, {"widget": "broken"}), [])
        self.assertIn("SECRET-INTERNAL-DETAIL", "\n".join(logs.output))

    def test_asset_request_edit_survives_a_failing_notification_and_logs_it(self):
        from django.http import HttpResponse

        from assets.views.asset_request_views import AssetRequestEditView
        from itambox.views.generic import ObjectEditView

        view = AssetRequestEditView()
        view.object = SimpleNamespace(pk=5)
        view.request = RequestFactory().post("/")
        form = SimpleNamespace(instance=SimpleNamespace(status="pending", is_group=False))
        saved = HttpResponse("saved")
        with (
            patch.object(ObjectEditView, "form_valid", return_value=saved),
            patch("assets.views.asset_request_views.dispatch_event", side_effect=RuntimeError("queue down")),
            self.assertLogs("assets.views.asset_request_views", level="ERROR") as logs,
        ):
            self.assertIs(view.form_valid(form), saved)
        self.assertIn("queue down", "\n".join(logs.output))


class TestWarrantyCostColumn(SimpleTestCase):
    def test_cost_renders_through_the_money_filter_and_empty_stays_empty(self):
        from assets.tables import WarrantyTable

        table = WarrantyTable([])
        record = SimpleNamespace(currency="CHF", tenant=None)
        self.assertIn("CHF", table.render_cost(12, record).replace("\xa0", " "))
        self.assertNotEqual(table.render_cost(None, record), table.render_cost(12, record))
