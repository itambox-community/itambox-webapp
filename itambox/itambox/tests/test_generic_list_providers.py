"""Production generic-list provider contracts for issue #444."""

from types import SimpleNamespace
from unittest.mock import patch

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, connection
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from assets.forms.filter_forms import AssetFilterForm
from assets.models import Asset, AssetType, Manufacturer, StatusLabel
from assets.views.asset_views import AssetListView
from core.tests.mixins import TenantTestMixin, grant
from extras.feature_views import EXTRAS_GENERIC_PRESENTATION_PROVIDER
from extras.models import CustomField, ExportTemplate, LabelTemplate, SavedFilter
from itambox.registry import ListFilterInput
from itambox.views.generic.extensions import (
    build_list_provider_context,
    resolve_list_provider_params,
)
from organization.models import Tenant

User = get_user_model()


class ExtrasListProviderTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="List Provider Tenant",
            slug="list-provider-tenant",
            permissions=["assets.view_asset", "assets.add_asset"],
        )
        self.other_user = User.objects.create_user(
            username="list-provider-other",
            email="list-provider-other@example.com",
            password="password",
        )
        grant(self.other_user, self.tenant, self.tenant_role)
        self.other_tenant = Tenant.objects.create(name="List Provider Tenant B", slug="list-provider-tenant-b")
        manufacturer = Manufacturer.objects.create(name="List Provider Manufacturer", slug="list-provider-mfr")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="List Provider Type",
            slug="list-provider-type",
        )
        self.status = StatusLabel.objects.create(
            name="List Provider Ready",
            slug="list-provider-ready",
            type=StatusLabel.TYPE_DEPLOYABLE,
        )
        self.asset = Asset.objects.create(
            name="List Provider Match",
            asset_tag="LIST-PROVIDER-1",
            asset_type=asset_type,
            status=self.status,
            tenant=self.tenant,
            custom_field_data={"rack": "wanted"},
        )
        self.nonmatching_asset = Asset.objects.create(
            name="List Provider Nonmatch",
            asset_tag="LIST-PROVIDER-2",
            asset_type=asset_type,
            status=self.status,
            tenant=self.tenant,
            custom_field_data={"rack": "other"},
        )
        self.content_type = ContentType.objects.get_for_model(Asset)
        rack_field = CustomField.objects.create(
            name="rack",
            label="Rack",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        rack_field.object_types.add(self.content_type)
        self.saved_filter = SavedFilter.objects.create(
            name="Visible saved asset filter",
            content_type=self.content_type,
            tenant=self.tenant,
            shared=False,
            created_by=self.tenant_user,
            parameters={"q": self.asset.name, "status": [str(self.status.pk)]},
        )
        self.export_template = ExportTemplate.objects.create(
            name="List Provider Export",
            content_type=self.content_type,
            template_code="",
        )
        self.label_template = LabelTemplate.objects.create(name="List Provider Label")
        self.factory = RequestFactory()

    def _request(self, params=None, *, partial=False, boosted=False):
        request = self.factory.get("/assets/", params or {})
        request.user = self.tenant_user
        request.active_tenant = self.tenant
        request.active_all_accessible = False
        if partial or boosted:
            request.htmx = SimpleNamespace(
                boosted=boosted,
                history_restore_request=False,
                target="",
            )
        return request

    def _run_view(self, params=None, *, partial=False, boosted=False):
        with self.tenant_context(self.tenant, self.tenant_membership):
            view = AssetListView()
            view.setup(self._request(params, partial=partial, boosted=boosted))
            queryset = view.get_queryset()
            view.object_list = queryset
            context = view.get_context_data(object_list=queryset)
            rows = list(context["table"].data.data)
        return view, context, rows

    def _resolve(self, params=None, *, partial=False):
        with self.tenant_context(self.tenant, self.tenant_membership):
            return resolve_list_provider_params(self._request(params), Asset, partial=partial)

    @staticmethod
    def _table_query_count(queries, table_name):
        return sum(table_name in query["sql"].lower() for query in queries)

    def test_visible_saved_filter_activates_multivalue_params_and_private_state(self):
        resolution = self._resolve({"filter": self.saved_filter.pk, "q": "raw"})

        state = resolution.provider_state["extras"]
        self.assertEqual(resolution.params.getlist("status"), [str(self.status.pk)])
        self.assertEqual(resolution.params["q"], self.asset.name)
        self.assertNotIn("filter", resolution.params)
        self.assertEqual(state["active_saved_filter_id"], self.saved_filter.pk)
        self.assertEqual([saved.pk for saved in state["saved_filters"]], [self.saved_filter.pk])

    def test_invalid_saved_filter_variants_preserve_incoming_params(self):
        private = SavedFilter.objects.create(
            name="Other private",
            content_type=self.content_type,
            tenant=self.tenant,
            shared=False,
            created_by=self.other_user,
            parameters={"q": "private"},
        )
        disabled = SavedFilter.objects.create(
            name="Disabled",
            content_type=self.content_type,
            tenant=self.tenant,
            shared=True,
            enabled=False,
            created_by=self.tenant_user,
            parameters={"q": "disabled"},
        )
        wrong_model = SavedFilter.objects.create(
            name="Wrong model",
            content_type=ContentType.objects.get_for_model(LabelTemplate),
            tenant=self.tenant,
            shared=True,
            created_by=self.tenant_user,
            parameters={"q": "wrong-model"},
        )
        foreign = SavedFilter.objects.create(
            name="Foreign",
            content_type=self.content_type,
            tenant=self.other_tenant,
            shared=True,
            created_by=self.tenant_user,
            parameters={"q": "foreign"},
        )
        cases = (None, "not-a-pk", 999999999, private.pk, disabled.pk, wrong_model.pk, foreign.pk)

        for filter_value in cases:
            with self.subTest(filter_value=filter_value):
                params = QueryDict("q=raw&q=second", mutable=True)
                if filter_value is not None:
                    params["filter"] = filter_value
                params._mutable = False
                resolution = self._resolve(params)

                self.assertEqual(resolution.params.getlist("q"), ["raw", "second"])
                if filter_value is not None:
                    self.assertEqual(resolution.params["filter"], str(filter_value))
                self.assertIsNone(resolution.provider_state["extras"]["active_saved_filter_id"])

    def test_selected_saved_filter_catalogue_is_evaluated_once_and_reused(self):
        ContentType.objects.clear_cache()
        with CaptureQueriesContext(connection) as queries:
            resolution = self._resolve({"filter": self.saved_filter.pk}, partial=True)
            context = build_list_provider_context(resolution, {})

        state_catalogue = resolution.provider_state["extras"]["saved_filters"]
        self.assertIs(context["saved_filters"], state_catalogue)
        self.assertEqual(context["active_saved_filter_id"], self.saved_filter.pk)
        self.assertEqual(self._table_query_count(queries, "extras_savedfilter"), 1)
        self.assertEqual(self._table_query_count(queries, "extras_exporttemplate"), 0)
        self.assertEqual(self._table_query_count(queries, "extras_labeltemplate"), 0)

    def test_optional_catalogue_failure_degrades_but_activation_failure_propagates(self):
        failure = DatabaseError("saved-filter catalogue unavailable")
        with patch.object(SavedFilter.objects, "for_scope", side_effect=failure):
            resolution = self._resolve()

        self.assertEqual(resolution.provider_state["extras"]["saved_filters"], [])
        self.assertIsNone(resolution.provider_state["extras"]["active_saved_filter_id"])

        with patch.object(SavedFilter.objects, "for_scope", side_effect=failure):
            with self.assertRaisesRegex(DatabaseError, "catalogue unavailable"):
                self._resolve({"filter": self.saved_filter.pk})

    def test_full_and_boosted_catalogues_are_bounded(self):
        for boosted in (False, True):
            with self.subTest(boosted=boosted):
                ContentType.objects.clear_cache()
                with CaptureQueriesContext(connection) as queries:
                    _view, context, _rows = self._run_view(
                        {"filter": self.saved_filter.pk},
                        boosted=boosted,
                    )

                self.assertEqual(context["saved_filters"], [self.saved_filter])
                self.assertEqual(context["active_saved_filter_id"], self.saved_filter.pk)
                self.assertEqual(context["export_templates"], [self.export_template])
                # The list toolbar links to the label print route; it never
                # loads the label template catalogue.
                self.assertNotIn("label_templates", context)
                self.assertEqual(self._table_query_count(queries, "extras_savedfilter"), 1)
                self.assertEqual(self._table_query_count(queries, "extras_exporttemplate"), 1)
                self.assertEqual(self._table_query_count(queries, "extras_labeltemplate"), 0)

    def test_partial_catalogues_skip_export_and_label_queries_but_keep_saved_filters(self):
        ContentType.objects.clear_cache()
        with CaptureQueriesContext(connection) as queries:
            _view, context, _rows = self._run_view(
                {"filter": self.saved_filter.pk},
                partial=True,
            )

        self.assertEqual(context["saved_filters"], [self.saved_filter])
        self.assertEqual(context["active_saved_filter_id"], self.saved_filter.pk)
        self.assertEqual(context["export_templates"], [])
        self.assertNotIn("label_templates", context)
        self.assertEqual(self._table_query_count(queries, "extras_savedfilter"), 1)
        self.assertEqual(self._table_query_count(queries, "extras_exporttemplate"), 0)
        self.assertEqual(self._table_query_count(queries, "extras_labeltemplate"), 0)

    def test_custom_field_filter_narrows_only_the_supplied_queryset(self):
        generic_validation_excluded = Asset.objects.create(
            name="Generic validation excluded",
            asset_tag="LIST-PROVIDER-3",
            asset_type=self.asset.asset_type,
            status=self.status,
            tenant=self.tenant,
            custom_field_data={"rack": "wanted"},
        )
        soft_deleted = Asset.objects.create(
            name="Soft deleted",
            asset_tag="LIST-PROVIDER-4",
            asset_type=self.asset.asset_type,
            status=self.status,
            tenant=self.tenant,
            custom_field_data={"rack": "wanted"},
        )
        Asset.all_objects.filter(pk=soft_deleted.pk).update(deleted_at=timezone.now())
        tenant_b = Asset.objects.create(
            name="Tenant B",
            asset_tag="LIST-PROVIDER-5",
            asset_type=self.asset.asset_type,
            status=self.status,
            tenant=self.other_tenant,
            custom_field_data={"rack": "wanted"},
        )
        scoped_input = Asset._base_manager.filter(pk__in=(self.asset.pk, self.nonmatching_asset.pk))
        input = ListFilterInput(
            request=self._request({"cf_rack": "wanted"}),
            model=Asset,
            params=QueryDict("cf_rack=wanted"),
            queryset=scoped_input,
            content_type=self.content_type,
            partial=False,
            state={},
        )

        result = EXTRAS_GENERIC_PRESENTATION_PROVIDER.filter_list_queryset(input)

        self.assertEqual(list(result.values_list("pk", flat=True)), [self.asset.pk])
        self.assertFalse(result.filter(pk__in=(generic_validation_excluded.pk, soft_deleted.pk, tenant_b.pk)).exists())

    def test_custom_field_filter_cannot_reopen_an_empty_validated_queryset(self):
        input = ListFilterInput(
            request=self._request({"cf_rack": "wanted"}),
            model=Asset,
            params=QueryDict("cf_rack=wanted"),
            queryset=Asset.objects.none(),
            content_type=self.content_type,
            partial=False,
            state={},
        )

        result = EXTRAS_GENERIC_PRESENTATION_PROVIDER.filter_list_queryset(input)

        self.assertTrue(result.query.is_empty())

    def test_all_invalid_generic_boundaries_skip_the_extras_filter_provider(self):
        invalid_saved = SavedFilter.objects.create(
            name="Invalid saved status",
            content_type=self.content_type,
            tenant=self.tenant,
            shared=False,
            created_by=self.tenant_user,
            parameters={"status": "999999999", "cf_rack": "wanted"},
        )
        cases = (
            ({"status": "999999999"}, False, None),
            ({"filter": invalid_saved.pk}, False, None),
            ({"q": self.asset.name, "status": "999999999"}, False, None),
            ({"status": "999999999"}, True, None),
        )

        for params, partial, form_class in cases:
            with self.subTest(params=params, partial=partial):
                with patch.object(
                    EXTRAS_GENERIC_PRESENTATION_PROVIDER,
                    "filter_list_queryset",
                    wraps=EXTRAS_GENERIC_PRESENTATION_PROVIDER.filter_list_queryset,
                ) as filter_provider:
                    if form_class is None:
                        view, context, rows = self._run_view(params, partial=partial)
                    else:
                        with patch.object(AssetListView, "filterset_form", form_class):
                            view, context, rows = self._run_view(params, partial=partial)

                self.assertTrue(view.filter_validation_failed)
                self.assertEqual(rows, [])
                self.assertTrue(context["filter_form"].errors)
                filter_provider.assert_not_called()

        class RejectingDisplayForm(AssetFilterForm):
            def clean_q(self):
                raise forms.ValidationError("Display-form-only rejection")

        with patch.object(
            EXTRAS_GENERIC_PRESENTATION_PROVIDER,
            "filter_list_queryset",
            wraps=EXTRAS_GENERIC_PRESENTATION_PROVIDER.filter_list_queryset,
        ) as filter_provider:
            with patch.object(AssetListView, "filterset_form", RejectingDisplayForm):
                view, context, rows = self._run_view({"q": self.asset.name})

        self.assertTrue(view.filter_validation_failed)
        self.assertEqual(rows, [])
        self.assertIn("q", context["filter_form"].errors)
        filter_provider.assert_not_called()

    def test_view_builds_queryset_and_table_once(self):
        class CountingAssetListView(AssetListView):
            queryset_calls = 0
            table_calls = 0

            def get_queryset(self):
                self.queryset_calls += 1
                return super().get_queryset()

            def get_table(self):
                self.table_calls += 1
                return super().get_table()

        with self.tenant_context(self.tenant, self.tenant_membership):
            view = CountingAssetListView()
            view.setup(self._request({"filter": self.saved_filter.pk}))
            queryset = view.get_queryset()
            view.object_list = queryset
            context = view.get_context_data(object_list=queryset)
            list(context["table"].data.data)

        self.assertEqual(view.queryset_calls, 1)
        self.assertEqual(view.table_calls, 1)

    def test_representative_query_counts_do_not_exceed_the_p6_base(self):
        invalid_saved = SavedFilter.objects.create(
            name="Query budget invalid saved status",
            content_type=self.content_type,
            tenant=self.tenant,
            shared=False,
            created_by=self.tenant_user,
            parameters={"status": "999999999"},
        )
        base_counts = {
            "full_raw": 14,
            "full_selected": 14,
            "htmx_raw": 9,
            "htmx_selected": 12,
            "invalid_raw": 7,
            "invalid_saved": 8,
        }
        cases = (
            ("full_raw", {}, False),
            ("full_selected", {"filter": self.saved_filter.pk}, False),
            ("htmx_raw", {}, True),
            ("htmx_selected", {"filter": self.saved_filter.pk}, True),
            ("invalid_raw", {"status": "999999999"}, False),
            ("invalid_saved", {"filter": invalid_saved.pk}, False),
        )

        for label, params, partial in cases:
            with self.subTest(label=label):
                ContentType.objects.clear_cache()
                with CaptureQueriesContext(connection) as queries:
                    self._run_view(params, partial=partial)
                self.assertLessEqual(len(queries), base_counts[label])


class ResolveContextModelFallbackTests(TestCase):
    """``_resolve_context_model`` falls back to the resolved rows when no model is declared."""

    def test_object_list_model_is_used_and_missing_model_fails_closed(self):
        from django.core.exceptions import ImproperlyConfigured

        from itambox.views.generic.list_ import ObjectListView

        class BareView(ObjectListView):
            pass

        fallback = BareView()
        fallback.object_list = Asset.objects.none()
        self.assertIs(fallback._resolve_context_model(), Asset)

        empty = BareView()
        empty.object_list = None
        with self.assertRaises(ImproperlyConfigured):
            empty._resolve_context_model()
