from types import SimpleNamespace
from unittest.mock import call, patch

from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from assets.models import Asset, AssetType, Manufacturer, StatusLabel, Supplier
from core.forms import JournalEntryForm
from core.managers import Scope
from core.models import Job, ObjectChange
from core.tests.mixins import TenantTestMixin
from extras.feature_views import EXTRAS_GENERIC_PRESENTATION_PROVIDER
from extras.models import Bookmark, CustomField, FileAttachment, ImageAttachment, JournalEntry, ObjectWatch
from itambox.registry import DetailContextInput
from itambox.views.generic.detail import ObjectDetailView
from itambox.views.generic.extensions import build_detail_provider_context
from subscriptions.feature_views import SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER
from subscriptions.models import Subscription, SubscriptionAssignment
from subscriptions.tables import SubscriptionAssignmentTable


class GenericDetailProviderContextTests(TenantTestMixin, TestCase):
    maxDiff = None

    def setUp(self):
        self.setup_tenant_context(
            name="Detail Provider Tenant",
            slug="detail-provider-tenant",
            permissions=["assets.view_asset", "core.view_job"],
        )
        manufacturer = Manufacturer.objects.create(name="Detail Provider Manufacturer", slug="detail-provider-mfr")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Detail Provider Type",
            slug="detail-provider-type",
        )
        status = StatusLabel.objects.create(
            name="Detail Provider Ready",
            slug="detail-provider-ready",
            type=StatusLabel.TYPE_DEPLOYABLE,
        )
        self.asset = Asset.objects.create(
            name="Detail Provider Asset",
            asset_tag="DETAIL-PROVIDER-1",
            asset_type=asset_type,
            status=status,
            tenant=self.tenant,
            custom_field_data={"rack_code": "R1"},
        )
        self.content_type = ContentType.objects.get_for_model(self.asset)
        custom_field = CustomField.objects.create(
            name="rack_code",
            label="Rack Code",
            activation=CustomField.ACTIVATION_GLOBAL,
        )
        custom_field.object_types.add(self.content_type)

        self.journal_entry = JournalEntry.objects.create(
            model=self.content_type,
            object_id=self.asset.pk,
            user=self.tenant_user,
            comment="Provider journal entry",
        )
        self.image_attachment = ImageAttachment.objects.create(
            model=self.content_type,
            object_id=self.asset.pk,
            image=SimpleUploadedFile("provider.png", b"\x89PNG\r\n"),
            name="provider.png",
        )
        self.file_attachment = FileAttachment.objects.create(
            model=self.content_type,
            object_id=self.asset.pk,
            file=SimpleUploadedFile("provider.txt", b"provider file"),
            name="provider.txt",
        )
        Bookmark.objects.create(user=self.tenant_user, model=self.content_type, object_id=self.asset.pk)
        ObjectWatch.objects.create(user=self.tenant_user, model=self.content_type, object_id=self.asset.pk)

        supplier = Supplier.objects.create(name="Detail Supplier", tenant=self.tenant)
        subscription = Subscription.objects.create(name="Detail Subscription", supplier=supplier, tenant=self.tenant)
        self.assignment = SubscriptionAssignment.objects.create(
            subscription=subscription,
            content_type=self.content_type,
            object_id=self.asset.pk,
            assigned_by=self.tenant_user,
        )
        self.request = RequestFactory().get(f"/assets/{self.asset.pk}/")
        self.request.user = self.tenant_user

    def extras_input(self, *features):
        return DetailContextInput(
            request=self.request,
            obj=self.asset,
            content_type=self.content_type,
            active_features=frozenset(features),
        )

    def test_each_extras_detail_feature_preserves_its_context_contract(self):
        expectations = {
            "journaling": {
                "has_journaling",
                "journal_app_label",
                "journal_model_name",
                "journal_entries",
                "journal_entries_count",
                "journal_form",
            },
            "custom_field_data": {"custom_fields_display"},
            "image_attachments": {"image_attachments", "has_image_attachments"},
            "file_attachments": {"file_attachments", "has_file_attachments"},
            "bookmarkable": {"is_bookmarkable", "bookmark_content_type_id", "is_bookmarked"},
            "watchable": {"is_watchable", "watch_content_type_id", "is_watched"},
        }

        for feature, expected_keys in expectations.items():
            with self.subTest(feature=feature):
                context = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input(feature))
                self.assertTrue(expected_keys.issubset(context))

        journal = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input("journaling"))
        self.assertEqual(journal["journal_entries_count"], 1)
        self.assertEqual(list(journal["journal_entries"]), [self.journal_entry])
        self.assertIsInstance(journal["journal_form"], JournalEntryForm)

        custom = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input("custom_field_data"))
        self.assertEqual(custom["custom_fields_display"], [("Rack Code", "R1")])

        images = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input("image_attachments"))
        self.assertEqual(list(images["image_attachments"]), [self.image_attachment])
        files = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input("file_attachments"))
        self.assertEqual(list(files["file_attachments"]), [self.file_attachment])

        bookmark = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input("bookmarkable"))
        self.assertTrue(bookmark["is_bookmarked"])
        watch = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(self.extras_input("watchable"))
        self.assertTrue(watch["is_watched"])

    def test_combined_extras_and_subscriptions_context_invokes_each_provider_once(self):
        with (
            patch.object(
                EXTRAS_GENERIC_PRESENTATION_PROVIDER,
                "build_detail_context",
                wraps=EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context,
            ) as extras_detail,
            patch.object(
                SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER,
                "build_detail_context",
                wraps=SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER.build_detail_context,
            ) as subscriptions_detail,
        ):
            context = build_detail_provider_context(self.request, self.asset, self.content_type)

        extras_detail.assert_called_once()
        subscriptions_detail.assert_called_once()
        self.assertEqual(
            extras_detail.call_args.args[0].active_features,
            frozenset(
                {
                    "bookmarkable",
                    "custom_field_data",
                    "file_attachments",
                    "image_attachments",
                    "journaling",
                    "watchable",
                }
            ),
        )
        self.assertEqual(subscriptions_detail.call_args.args[0].active_features, frozenset({"subscribable"}))
        self.assertEqual(context["subscription_assignments_count"], 1)
        self.assertIsInstance(context["subscription_assignments_table"], SubscriptionAssignmentTable)
        self.assertEqual(
            context["subscription_assignments_table"].exclude,
            ("content_type", "object_id", "assigned_object"),
        )
        assignment_queryset = context["subscription_assignments_table"].data.data
        self.assertEqual(
            assignment_queryset.query.select_related,
            {"subscription": {"supplier": {}}, "assigned_by": {}},
        )
        self.assertEqual(list(assignment_queryset), [self.assignment])

    def test_combined_provider_query_budget_is_ten_queries(self):
        with CaptureQueriesContext(connection) as queries:
            context = build_detail_provider_context(self.request, self.asset, self.content_type)
            list(context["journal_entries"])
            list(context["image_attachments"])
            list(context["file_attachments"])
            list(context["subscription_assignments_table"].data.data)

        self.assertEqual(len(queries), 10)

    def test_extras_provider_query_budget_is_seven_queries(self):
        with CaptureQueriesContext(connection) as queries:
            context = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(
                self.extras_input(
                    "bookmarkable",
                    "custom_field_data",
                    "file_attachments",
                    "image_attachments",
                    "journaling",
                    "watchable",
                )
            )
            list(context["journal_entries"])
            list(context["image_attachments"])
            list(context["file_attachments"])

        self.assertEqual(len(queries), 7)

    def test_providers_reuse_the_supplied_content_type_without_resolving_another(self):
        with patch.object(
            ContentType.objects,
            "get_for_model",
            side_effect=AssertionError("detail providers must reuse the supplied ContentType"),
        ):
            extras_context = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(
                self.extras_input(
                    "bookmarkable",
                    "custom_field_data",
                    "file_attachments",
                    "image_attachments",
                    "journaling",
                    "watchable",
                )
            )
            subscriptions_context = SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(
                DetailContextInput(
                    request=self.request,
                    obj=self.asset,
                    content_type=self.content_type,
                    active_features=frozenset({"subscribable"}),
                )
            )

        self.assertEqual(extras_context["bookmark_content_type_id"], self.content_type.pk)
        self.assertEqual(subscriptions_context["subscribable_content_type_id"], self.content_type.pk)

    def test_generic_detail_resolves_one_content_type_shared_with_changelog_and_providers(self):
        class MinimalAssetDetailView(ObjectDetailView):
            queryset = Asset.objects.all()
            disable_related_objects_list = True

        view = MinimalAssetDetailView()
        view.setup(self.request, pk=self.asset.pk)
        view.object = self.asset
        view._cached_object = self.asset

        scoped_changelog_cls = type(ObjectChange.objects.for_scope(Scope(Scope.SYSTEM)))

        with (
            patch.object(ContentType.objects, "get_for_model", return_value=self.content_type) as get_for_model,
            patch.object(
                scoped_changelog_cls, "filter", autospec=True, side_effect=scoped_changelog_cls.filter
            ) as changelog_filter,
            patch.object(
                EXTRAS_GENERIC_PRESENTATION_PROVIDER,
                "build_detail_context",
                wraps=EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context,
            ) as extras_detail,
            patch.object(
                SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER,
                "build_detail_context",
                wraps=SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER.build_detail_context,
            ) as subscriptions_detail,
        ):
            context = view.get_context_data(object=self.asset)

        get_for_model.assert_has_calls(
            [
                call(self.asset),
                call(Asset),
            ]
        )
        # Exactly two lookups: the view's shared ContentType for the object, and
        # the model hook's own internal lookup inside
        # ``ChangeLoggingMixin.get_changelog_url`` (the restored object hook
        # precedence). No provider re-resolves the ContentType.
        self.assertEqual(get_for_model.call_count, 2)
        object_calls = [c for c in changelog_filter.call_args_list if "changed_object_type" in c.kwargs]
        self.assertEqual(len(object_calls), 1)
        self.assertIs(object_calls[0].kwargs["changed_object_type"], self.content_type)
        self.assertIs(extras_detail.call_args.args[0].content_type, self.content_type)
        self.assertIs(subscriptions_detail.call_args.args[0].content_type, self.content_type)
        self.assertEqual(context["journal_entries_count"], 1)

    def test_job_synthetic_attachment_context_uses_the_supplied_content_type(self):
        job = Job.objects.create(name="Detail attachment job", tenant=self.tenant)
        job_content_type = ContentType.objects.get_for_model(job)
        attachment = FileAttachment.objects.create(
            model=job_content_type,
            object_id=job.pk,
            file=SimpleUploadedFile("job-output.txt", b"job output"),
            name="job-output.txt",
        )

        with CaptureQueriesContext(connection) as queries:
            context = EXTRAS_GENERIC_PRESENTATION_PROVIDER.build_detail_context(
                DetailContextInput(
                    request=self.request,
                    obj=job,
                    content_type=job_content_type,
                    active_features=frozenset({"job_file_attachments"}),
                )
            )
            self.assertEqual(list(context["attachments"]), [attachment])

        self.assertEqual(len(queries), 1)
        self.assertEqual(context["attachment_app_label"], "core")
        self.assertEqual(context["attachment_model_name"], "job")

    def test_feature_disabled_model_receives_no_provider_context(self):
        group = Group.objects.create(name="Feature-disabled detail group")
        group_content_type = ContentType.objects.get_for_model(group)
        request = RequestFactory().get(f"/groups/{group.pk}/")
        request.user = self.tenant_user

        with (
            CaptureQueriesContext(connection) as queries,
            patch.object(EXTRAS_GENERIC_PRESENTATION_PROVIDER, "build_detail_context") as extras_detail,
        ):
            context = build_detail_provider_context(request, group, group_content_type, core_context={"title": "Group"})

        extras_detail.assert_not_called()
        self.assertEqual(len(queries), 0)
        self.assertEqual(context, {"title": "Group"})


class MovedFeatureRouteOwnershipTests(SimpleTestCase):
    def test_every_moved_route_resolves_to_its_concrete_extras_owner(self):
        cases = {
            reverse("journalentry_list"): "extras.attachment_views",
            reverse(
                "journal_entry_add", kwargs={"app_label": "assets", "model_name": "asset", "object_id": 1}
            ): "extras.attachment_views",
            reverse(
                "object_export",
                kwargs={"app_label": "assets", "model_name": "asset", "template_id": 0},
            ): "extras.export_views",
            reverse(
                "image_attachment_upload",
                kwargs={"app_label": "assets", "model_name": "asset", "object_id": 1},
            ): "extras.attachment_views",
            reverse("image_attachment_delete", kwargs={"pk": 1}): "extras.attachment_views",
            reverse("image_attachment_serve", kwargs={"pk": 1}): "extras.attachment_views",
            reverse(
                "file_attachment_upload",
                kwargs={"app_label": "assets", "model_name": "asset", "object_id": 1},
            ): "extras.attachment_views",
            reverse("file_attachment_delete", kwargs={"pk": 1}): "extras.attachment_views",
            reverse("file_attachment_download", kwargs={"pk": 1}): "extras.attachment_views",
            reverse("label_print", kwargs={"app_label": "assets", "model_name": "asset"}): "extras.label_views",
            reverse("extras:exporttemplate_list"): "extras.export_views",
            reverse("extras:webhookendpoint_list"): "extras.webhook_views",
            reverse("extras:eventrule_list"): "extras.event_rule_views",
            reverse("extras:labeltemplate_list"): "extras.export_views",
        }

        for url, expected_module in cases.items():
            with self.subTest(url=url):
                self.assertEqual(resolve(url).func.view_class.__module__, expected_module)

    def test_itambox_views_does_not_reexport_moved_domain_symbols(self):
        import itambox.views

        for name in (
            "ObjectExportView",
            "JournalEntryCreateView",
            "ImageAttachmentUploadView",
            "WebhookEndpointListView",
            "EventRuleListView",
            "LabelTemplateListView",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(itambox.views, name))


class SubscriptionProviderShapeTests(SimpleTestCase):
    def test_subscription_provider_is_a_data_only_presentation_contributor(self):
        provider_type = type(SUBSCRIPTIONS_GENERIC_PRESENTATION_PROVIDER)
        self.assertEqual(provider_type.__module__, "subscriptions.feature_views")
        self.assertFalse(issubclass(provider_type, SimpleNamespace))
