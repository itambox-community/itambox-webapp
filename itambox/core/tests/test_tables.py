import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import django_tables2 as tables
from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import translation
from django.utils.html import strip_tags
from model_bakery import baker

from assets.models import (
    Asset,
    AssetRole,
    StatusLabel,
    Warranty,
    WarrantyTypeChoices,
)
from assets.services import checkout_asset
from assets.tables import AssetMaintenanceTable, AssetTable, AssetTypeTable, WarrantyTable
from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from core.tables import AssigneeColumn, BaseTable, BooleanColumn, ObjectChangeTable
from core.templatetags.helpers import get_action_color
from core.templatetags.utility_tags import localize_journal_comment
from extras.models import NotificationChannel
from extras.tables import JournalEntryTable
from itambox.middleware import _request_id
from organization.models import Location, Site

User = get_user_model()


class BaseTableEmptyValueTest(SimpleTestCase):
    def test_empty_cells_render_a_hyphen_default(self):
        class EmptyValueTable(BaseTable):
            value = tables.Column()

            class Meta:
                fields = ("value",)

        table = EmptyValueTable([{"value": None}])
        request = RequestFactory().get("/")
        rendered = table.as_html(request)

        self.assertIn(">-</td>", rendered)
        self.assertNotIn("Not set", rendered)
        self.assertNotIn("—", rendered)


class TableLocalizationTests(SimpleTestCase):
    def test_boolean_empty_value_is_language_neutral(self):
        column = BooleanColumn()

        with translation.override("de"):
            self.assertEqual(strip_tags(str(column.render(None))), "-")
        with translation.override("en"):
            self.assertEqual(strip_tags(str(column.render(None))), "-")

    def test_action_color_uses_choice_metadata(self):
        action = ObjectChangeActionChoices.ACTION_CREATE
        expected = next(choice[2] for choice in ObjectChangeActionChoices.CHOICES if choice[0] == action)

        self.assertEqual(get_action_color(action), expected)

    def test_subscription_renewal_journal_is_localized_only_when_rendered(self):
        comment = "Renewed subscription. Next renewal date: 2026-09-01. Cost: Not set EUR."

        with translation.override("de"):
            self.assertEqual(
                localize_journal_comment(comment),
                "Abonnement verlängert. Nächster Verlängerungstermin: 2026-09-01. Kosten: Nicht festgelegt EUR.",
            )
            self.assertIn(
                "Kosten: Nicht festgelegt EUR.",
                localize_journal_comment("Renewed subscription. Next renewal date: 2026-09-01. Cost: — EUR."),
            )
        with translation.override("en"):
            self.assertEqual(localize_journal_comment(comment), comment)
        self.assertEqual(localize_journal_comment("A user-written journal entry."), "A user-written journal entry.")

    def test_journal_table_uses_the_render_time_localization_helper(self):
        with translation.override("de"):
            rendered = JournalEntryTable([]).render_comment(
                "Renewed subscription. Next renewal date: 2026-09-01. Cost: 12.50 EUR."
            )

        self.assertIn("Abonnement verlängert", rendered)
        self.assertIn("Kosten: 12.50 EUR", rendered)


class AssetTableLocalizationTest(SimpleTestCase):
    def test_duration_renderers_use_german_plural_forms(self):
        with translation.override("de"):
            self.assertEqual(AssetTypeTable([]).render_eol_months(1), "1 Monat")
            self.assertEqual(AssetTypeTable([]).render_eol_months(2), "2 Monate")
            self.assertEqual(AssetMaintenanceTable([]).render_downtime_days(0), "Am selben Tag")
            self.assertEqual(AssetMaintenanceTable([]).render_downtime_days(1), "1 Tag")
            self.assertEqual(AssetMaintenanceTable([]).render_downtime_days(2), "2 Tage")

    def test_overdue_tooltip_is_translated(self):
        record = SimpleNamespace(audit_due_date=datetime.date(2026, 8, 30), audit_overdue=True)
        with translation.override("de"):
            rendered = AssetTable([]).render_audit_due_date(record)
        self.assertIn('title="Überfällig"', rendered)


class AssigneeColumnGenericForeignKeyTest(SimpleTestCase):
    def test_assignee_column_generic_foreign_key_cache(self):
        parent = SimpleNamespace(pk=101)
        target = SimpleNamespace(pk=202)
        assignment = SimpleNamespace(
            assigned_to_content_type_id=3,
            location_id=parent.pk,
            assigned_to_object_id=target.pk,
        )

        class AssignmentQuerySet:
            def filter(self, **kwargs):
                return self

            def select_related(self, *fields):
                return self

            def __iter__(self):
                return iter([assignment])

        assignment_model = type(
            "GenericAssignmentModel",
            (),
            {
                "_meta": SimpleNamespace(
                    fields=[SimpleNamespace(is_relation=True, related_model=Location, name="location")]
                ),
                "assigned_to_content_type": object(),
                "objects": AssignmentQuerySet(),
            },
        )
        target_model = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kwargs: [target]))
        content_type = SimpleNamespace(model_class=lambda: target_model)
        table = SimpleNamespace(data=[parent])
        column = AssigneeColumn(assignment_model_path="mock.GenericAssignmentModel")

        with (
            patch.object(column, "_get_assignment_model", return_value=assignment_model),
            patch("django.contrib.contenttypes.models.ContentType.objects.get_for_id", return_value=content_type),
        ):
            column._build_cache(table, Location, "_assignee_cache")

        self.assertEqual(table._assignee_cache, {parent.pk: target})


class IDColumnTestCase(TestCase):
    """The universal `id` detail-link column (NetBox-style): hidden by default,
    available via the column selector, and — on tables that have no natural
    identity column — shown as the first data column linking to the detail."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="idcoluser", password="password123", is_superuser=True)

    def _req(self, query=""):
        request = self.factory.get("/" + query)
        request.user = self.user
        return request

    def _asset(self, tag):
        role = AssetRole.objects.create(name="Role " + tag, slug="role-" + tag.lower())
        return Asset.objects.create(name="Asset " + tag, asset_tag=tag, asset_role=role)

    def test_id_hidden_by_default(self):
        """A table with a natural identity column (AssetTable.name) keeps id
        hidden by default, but exposes it in the column selector."""
        table = AssetTable(Asset.objects.none())
        table.configure(self._req())
        self.assertNotIn("id", [c.name for c in table.columns])  # not in visible set
        self.assertIn("id", [name for name, _verbose in table.available_columns])

    def test_id_enable_via_include_columns_is_first_data_column(self):
        asset = self._asset("IDC1")
        table = AssetTable(Asset.objects.filter(pk=asset.pk))
        table.configure(self._req("?include_columns=id"))
        visible = [c.name for c in table.columns]
        self.assertIn("id", visible)
        self.assertEqual(visible[0], "pk")  # checkbox stays leftmost
        self.assertEqual(visible[1], "id")  # id is the first *data* column

    def test_id_links_to_detail(self):
        asset = self._asset("IDC2")
        table = AssetTable(Asset.objects.filter(pk=asset.pk))
        table.configure(self._req("?include_columns=id"))
        cell = table.rows[0].get_cell("id")
        self.assertIn(asset.get_absolute_url(), cell)
        self.assertIn(str(asset.pk), cell)


class LifecycleTableIDColumnTest(TestCase):
    """Warranty/Reservation/Disposal lack a name column, so the id column is
    shown by default (first data column) and carries the detail link — the
    descriptive columns (type/method/reserved_for) are no longer linkified."""

    def setUp(self):
        status = baker.make(StatusLabel, type="deployable")
        self.asset = baker.make(Asset, status=status, tenant=None)
        today = datetime.date.today()
        self.warranty = baker.make(
            Warranty,
            asset=self.asset,
            warranty_type=WarrantyTypeChoices.HARDWARE,
            start_date=today,
            end_date=today + datetime.timedelta(days=30),
        )

    def test_id_visible_first_and_linked_without_configure(self):
        # Detail-view tab path uses RequestConfig only (no custom configure()).
        table = WarrantyTable(Warranty.objects.filter(pk=self.warranty.pk))
        visible = [c.name for c in table.columns]
        self.assertEqual(visible[0], "pk")
        self.assertEqual(visible[1], "id")
        cell = table.rows[0].get_cell("id")
        self.assertIn(self.warranty.get_absolute_url(), cell)
        self.assertIn(str(self.warranty.pk), cell)

    def test_descriptive_column_not_linkified(self):
        table = WarrantyTable(Warranty.objects.filter(pk=self.warranty.pk))
        cell = table.rows[0].get_cell("warranty_type")
        self.assertNotIn("<a ", cell)  # the link lives on the id column now
        self.assertIn("Hardware", cell)


class CoreTablesTestCase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="testuser", password="password123", is_superuser=True)

    def test_assignee_column_cache_build(self):
        """Test that AssigneeColumn build cache resolves without crashing for ForeignKey relationships."""
        site = Site.objects.create(name="Test Site 2", slug="test-site-2")
        location = Location.objects.create(name="Test Location 2", slug="test-location-2", site=site)

        role = AssetRole.objects.create(name="Desktop", slug="desktop-ac")
        deployable = StatusLabel.objects.create(name="Table Deployable 2", slug="table-deployable-2", type="deployable")
        deployed = StatusLabel.objects.create(name="Table In Use 2", slug="table-in-use-2", type="deployed")
        asset = Asset.objects.create(name="Test Asset 2", asset_tag="TAG-AC", asset_role=role, status=deployable)

        checkout_asset(asset, location=location, user=self.user, status=deployed)

        column = AssigneeColumn(assignment_model_path="assets.AssetAssignment")
        table = AssetTable(Asset.objects.filter(pk=asset.pk))

        cache_attr = "_assignee_cache_test"
        column._build_cache(table, Asset, cache_attr)
        cache = getattr(table, cache_attr)

        self.assertIn(asset.pk, cache)
        self.assertEqual(cache[asset.pk], location)

    def test_assignee_column_render(self):
        """Test that AssigneeColumn renders correctly for checked-out location assets and available assets."""
        site = Site.objects.create(name="Test Site 3", slug="test-site-3")
        location = Location.objects.create(name="Test Location 3", slug="test-location-3", site=site)

        role = AssetRole.objects.create(name="Desktop", slug="desktop-ar")

        # Asset 1: checked out to location
        deployable = StatusLabel.objects.create(
            name="Table Deployable Render", slug="table-deployable-render", type="deployable"
        )
        deployed = StatusLabel.objects.create(name="Table In Use Render", slug="table-in-use-render", type="deployed")
        asset_checked_out = Asset.objects.create(
            name="Asset Checked Out", asset_tag="TAG-AR1", asset_role=role, location=location, status=deployable
        )
        checkout_asset(asset_checked_out, location=location, user=self.user, status=deployed)

        # Asset 2: available, physical location is location
        asset_available = Asset.objects.create(
            name="Asset Available", asset_tag="TAG-AR2", asset_role=role, location=location
        )

        column = AssigneeColumn(location_field="location", assignment_model_path="assets.AssetAssignment")
        table = AssetTable(Asset.objects.filter(pk__in=[asset_checked_out.pk, asset_available.pk]))

        # Render check for Asset 1 (checked out to location)
        col_bound = table.columns["assignee"]
        rendered_1 = column.render(asset_checked_out.pk, asset_checked_out, col_bound, table)
        self.assertIn(f'Location: <a href="{location.get_absolute_url()}">{location.name}</a>', rendered_1)

        with translation.override("de"):
            rendered_2 = column.render(asset_available.pk, asset_available, col_bound, table)
        self.assertIn(">-</span>", rendered_2)
        self.assertNotIn("Nicht festgelegt", rendered_2)

    def test_table_optimizations(self):
        class DummyTable(BaseTable):
            assignee = AssigneeColumn(location_field="name")

            class Meta:
                model = Location
                fields = ("name", "assignee")

        site = Site.objects.create(name="Test Site", slug="test-site")
        Location.objects.create(name="Test Location", slug="test-location", site=site)

        # Initialize table and call internal methods
        table = DummyTable(Location.objects.all())
        table._apply_column_width_classes()
        table._apply_prefetching()

        request = self.factory.get("/")
        request.user = self.user
        table.configure(request)

        # Ensure assigning mechanism and cache builder are run
        for _row in table.data:
            pass

        # ObjectChangeTable render coverage
        _request_id.set(uuid.uuid4())
        NotificationChannel.objects.create(name="Another Channel", channel_type="webhook")
        _request_id.set(None)

        oc_table = ObjectChangeTable(ObjectChange.objects.all())
        self.assertGreater(len(oc_table.rows), 0)
        for row in oc_table.rows:
            html = oc_table.render_action(row.record.action, row.record)
            self.assertIn("badge bg-", html)

        # Edge case empty ObjectChange
        oc = ObjectChange()
        self.assertIsNone(oc.get_changed_object_url())


def test_search_result_table_stays_exported_from_core_tables():
    """Issue #444 review (P2): the established ``core.tables`` package export
    for the domain-neutral search table must survive the extras-table move."""
    from core.tables import SearchResultTable as Exported
    from core.tables.templates import SearchResultTable as Defined

    assert Exported is Defined
