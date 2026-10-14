from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils.html import strip_tags
from django.utils.translation import override

from assets.models import Asset, AssetType
from assets.tables import (
    AssetDisposalTable,
    AssetMaintenanceTable,
    AssetRequestTable,
    AssetTable,
    CategoryTable,
    StatusLabelTable,
    WarrantyTable,
)
from compliance.views_audit import AuditSessionTable
from core.tables import AssigneeColumn
from core.templatetags.utility_tags import absolute, localize_journal_comment
from extras.tables import AlertRuleTable, JournalEntryTable, SavedFilterTable, TagTable, WebhookDeliveryTable
from inventory.tables import (
    AccessoryAssignmentTable,
    AccessoryTable,
    ComponentAllocationTable,
    ConsumableAssignmentTable,
    ConsumableTable,
)
from licenses.tables import LicenseSeatAssignmentTable
from organization.models import AssetHolder
from organization.tables import AssetAssignmentTable, CostCenterTable, LocationTable, RoleTable, SiteTable
from subscriptions.tables import SubscriptionAssignmentTable, SubscriptionTable


class ChangedTableRendererTests(SimpleTestCase):
    def assert_empty_placeholder(self, rendered):
        self.assertEqual(strip_tags(str(rendered)).strip(), "-")

    def test_asset_renderers_cover_empty_status_and_quantity_copy(self):
        table = AssetTable([])
        status = SimpleNamespace(color="#00ff00", name="Ready")
        status_record = SimpleNamespace(type="deployable", get_type_display=lambda: "Einsatzbereit")
        overdue = SimpleNamespace(audit_due_date=date(2026, 8, 30), audit_overdue=True)
        current = SimpleNamespace(audit_due_date=date(2026, 8, 30), audit_overdue=False)
        inherited = SimpleNamespace(is_requestable=True, requestable=None)
        explicit = SimpleNamespace(is_requestable=False, requestable=False)

        with override("de"):
            self.assert_empty_placeholder(table.render_serial_number(None))
            self.assertIn("badge-status", table.render_status(status))
            status_table = StatusLabelTable([])
            self.assertEqual(status_table.render_type("deployable", status_record), "Einsatzbereit")
            self.assert_empty_placeholder(status_table.render_type(None, None))
            self.assert_empty_placeholder(table.render_status(None))
            self.assertIn('title="Überfällig"', table.render_audit_due_date(overdue))
            self.assertEqual(table.render_audit_due_date(current), "2026-08-30")
            self.assert_empty_placeholder(table.render_audit_due_date(SimpleNamespace(audit_due_date=None)))
            self.assert_empty_placeholder(table.render_salvage_value(None))
            self.assertIn("12.50", table.render_salvage_value(12.5))
            self.assertIn("Vom Asset-Typ übernommen", table.render_requestable(inherited))
            self.assertIn("Auf diesem Asset gesetzt", table.render_requestable(explicit))
        self.assertEqual(table.value_purchase_date(None), "")
        self.assertEqual(table.value_purchase_date(date(2026, 1, 2)), "2026-01-02")

    def test_inventory_available_and_assignment_renderers_cover_all_target_types(self):
        with override("de"), patch("inventory.tables.reverse", return_value="/target/"):
            accessory = AccessoryTable([])
            self.assertIn("Leer", accessory.render_available(0, SimpleNamespace(min_qty=1)))
            self.assertIn("Niedrig", accessory.render_available(1, SimpleNamespace(min_qty=2)))
            self.assertEqual(accessory.render_available(5, SimpleNamespace(min_qty=2)), 5)

            consumable = ConsumableTable([])
            self.assertIn("Nicht vorrätig", consumable.render_available(0, SimpleNamespace(min_qty=1)))
            self.assertIn("Niedriger Bestand", consumable.render_available(1, SimpleNamespace(min_qty=2)))
            self.assertEqual(consumable.render_available(5, SimpleNamespace(min_qty=2)), 5)

            location = SimpleNamespace(pk=2)
            asset = SimpleNamespace(pk=3)
            for table_class in (AccessoryAssignmentTable, ConsumableAssignmentTable):
                table = table_class([])
                self.assertIn(
                    "Lagerort",
                    table.render_assigned_to(
                        SimpleNamespace(assigned_holder=None, assigned_location=location, assigned_asset=None)
                    ),
                )
                self.assertIn(
                    "Asset",
                    table.render_assigned_to(
                        SimpleNamespace(assigned_holder=None, assigned_location=None, assigned_asset=asset)
                    ),
                )
                self.assert_empty_placeholder(
                    table.render_assigned_to(
                        SimpleNamespace(assigned_holder=None, assigned_location=None, assigned_asset=None)
                    )
                )

            component = ComponentAllocationTable([])
            self.assertIn(
                "Lagerort",
                component.render_assigned_to(
                    SimpleNamespace(assigned_holder=None, assigned_location=location, assigned_asset=None)
                ),
            )
            self.assertIn(
                "Asset",
                component.render_assigned_to(
                    SimpleNamespace(assigned_holder=None, assigned_location=None, assigned_asset=asset)
                ),
            )
            self.assert_empty_placeholder(
                component.render_assigned_to(
                    SimpleNamespace(assigned_holder=None, assigned_location=None, assigned_asset=None)
                )
            )

    def test_subscription_and_license_renderers_use_localized_quantities(self):
        table = SubscriptionTable([])
        record = SimpleNamespace(status="active", get_status_display=lambda: "Active")
        with override("de"):
            self.assert_empty_placeholder(table.render_status(None, None))
            self.assertIn("badge-status", table.render_status("active", record))
            self.assert_empty_placeholder(table.render_renewal_cost(None, SimpleNamespace(currency="EUR")))
            self.assertIn("12.50 EUR", table.render_renewal_cost(12.5, SimpleNamespace(currency="EUR")))
            self.assert_empty_placeholder(table.render_days_until_renewal(None))
            self.assertIn("1 Tag überfällig", table.render_days_until_renewal(-1))
            self.assertIn("2 Tage überfällig", table.render_days_until_renewal(-2))
            self.assertIn("Heute", table.render_days_until_renewal(0))
            self.assertIn("1 Tag", table.render_days_until_renewal(1))
            self.assertIn("31 Tage", table.render_days_until_renewal(31))
            assignment_table = SubscriptionAssignmentTable([])
            self.assert_empty_placeholder(
                assignment_table.render_assigned_object(None, SimpleNamespace(tenant_safe_assigned_object=None))
            )
            self.assertEqual(
                assignment_table.render_assigned_object(None, SimpleNamespace(tenant_safe_assigned_object="Asset")),
                "Asset",
            )

        license_table = LicenseSeatAssignmentTable([])
        no_holder = SimpleNamespace(assigned_holder=None, asset_id=None)
        direct_holder = AssetHolder(pk=1, upn="holder@example.test", first_name="Test", last_name="Holder")
        via_asset = SimpleNamespace(assigned_holder=None, asset_id=1, asset=SimpleNamespace(assigned_to=direct_holder))
        with patch("licenses.tables.reverse", return_value="/holder/"):
            with override("de"):
                self.assert_empty_placeholder(license_table.render_assigned_holder(no_holder))
                self.assertIn("über Asset", license_table.render_assigned_holder(via_asset))
                self.assertIn(
                    "holder@example.test",
                    license_table.render_assigned_holder(SimpleNamespace(assigned_holder=direct_holder, asset_id=None)),
                )

    def test_shared_tables_cover_localized_empty_and_action_labels(self):
        with override("de"), patch("extras.tables.reverse", return_value="/target/"):
            self.assert_empty_placeholder(TagTable([]).render_color(None))
            self.assertIn("Global", SavedFilterTable([]).render_tenant(None))
            journal = JournalEntryTable([])
            self.assert_empty_placeholder(journal.render_content_object(None))
            webhook = WebhookDeliveryTable([])
            self.assert_empty_placeholder(webhook.render_delivery_id(None))
            self.assertIn("Test-Webhook", str(webhook.render_event(None, SimpleNamespace(test_send=True))))
            self.assert_empty_placeholder(webhook.render_event(None, SimpleNamespace(test_send=False)))
            self.assert_empty_placeholder(webhook.render_response_code(None))
            self.assert_empty_placeholder(webhook.render_error_message(None))
            self.assertIn("Nein", webhook.render_test_send(False))
            self.assertIn("Test", webhook.render_test_send(True))
            self.assertIn(
                "Success", webhook.render_status("success", SimpleNamespace(get_status_display=lambda: "Success"))
            )
            self.assertIn(
                "Info", AlertRuleTable([]).render_severity("info", SimpleNamespace(get_severity_display=lambda: "Info"))
            )

    def test_organization_and_audit_empty_renderers_are_localized(self):
        with override("de"), patch("organization.tables.reverse", return_value="/target/"):
            self.assert_empty_placeholder(SiteTable([]).render_status(None, None))
            self.assert_empty_placeholder(LocationTable([]).render_status(None, None))
            self.assertIn("Keine Rolle", str(AssetAssignmentTable([]).render_asset_role(None)))
            self.assertIn(
                "Nicht verfügbar", AssetAssignmentTable([]).render_checkin_btn(SimpleNamespace(asset=SimpleNamespace()))
            )
            self.assert_empty_placeholder(RoleTable([]).render_tenant(None, SimpleNamespace(tenant=None)))
            self.assertIn("Nein", str(RoleTable([]).render_shared(None, SimpleNamespace())))
            self.assert_empty_placeholder(CostCenterTable([]).render_parent(None))
            self.assertIn(
                "FIN: Finance",
                CostCenterTable([]).render_parent(
                    SimpleNamespace(code="FIN", name="Finance", get_absolute_url=lambda: "/cc/")
                ),
            )

        with override("de"):
            audit = AuditSessionTable([])
            self.assertIn("Geplant", audit.render_status(None, SimpleNamespace()))
            self.assertIn("Global", str(audit.render_location(None)))
            self.assertIn("Nicht abgeschlossen", str(audit.render_completed_at(None)))

    def test_asset_request_item_variants_and_empty_fallback_are_rendered(self):
        table = AssetRequestTable([])
        with patch("assets.tables.reverse", return_value="/target/"), override("de"):
            cases = [
                (
                    SimpleNamespace(
                        asset=SimpleNamespace(),
                        asset_id=1,
                        asset_type=None,
                        component=None,
                        accessory=None,
                        consumable=None,
                        qty=1,
                    ),
                    "Asset",
                ),
                (
                    SimpleNamespace(
                        asset=None,
                        asset_id=None,
                        asset_type=SimpleNamespace(),
                        asset_type_id=2,
                        component=None,
                        accessory=None,
                        consumable=None,
                        qty=1,
                    ),
                    "Asset-Typ",
                ),
                (
                    SimpleNamespace(
                        asset=None,
                        asset_id=None,
                        asset_type=SimpleNamespace(),
                        asset_type_id=2,
                        component=None,
                        accessory=None,
                        consumable=None,
                        qty=2,
                    ),
                    "Asset-Typ",
                ),
                (
                    SimpleNamespace(
                        asset=None,
                        asset_id=None,
                        asset_type=None,
                        component=SimpleNamespace(),
                        component_id=3,
                        accessory=None,
                        consumable=None,
                        qty=1,
                    ),
                    "Komponente",
                ),
                (
                    SimpleNamespace(
                        asset=None,
                        asset_id=None,
                        asset_type=None,
                        component=None,
                        accessory=SimpleNamespace(),
                        accessory_id=4,
                        consumable=None,
                        qty=1,
                    ),
                    "Zubehör",
                ),
                (
                    SimpleNamespace(
                        asset=None,
                        asset_id=None,
                        asset_type=None,
                        component=None,
                        accessory=None,
                        consumable=SimpleNamespace(),
                        consumable_id=5,
                        qty=1,
                    ),
                    "Verbrauchsmaterial",
                ),
                (
                    SimpleNamespace(
                        asset=None,
                        asset_id=None,
                        asset_type=None,
                        component=None,
                        accessory=None,
                        consumable=None,
                        qty=1,
                    ),
                    "-",
                ),
            ]
            for record, expected in cases:
                with self.subTest(expected=expected):
                    rendered = str(table.render_item(record))
                    self.assertIn(expected, rendered)
                    if expected == "-":
                        self.assertEqual(rendered, "-")

    def test_asset_and_warranty_table_fallbacks_are_rendered(self):
        with override("de"):
            asset_type = AssetType(eol_months=13)
            self.assertEqual(Asset(status=None).get_status_display(), "Nicht festgelegt")
            self.assertIn(
                "Abgelaufen",
                str(Asset(purchase_date=date(2020, 1, 1), asset_type=AssetType(eol_months=1)).time_to_eol),
            )
            self.assertIn("1 Jahr", str(Asset(purchase_date=date.today(), asset_type=asset_type).time_to_eol))
            self.assertIn("Nicht festgelegt", str(Asset(purchase_date=None, asset_type=None).time_to_eol))
            with patch.object(Asset, "eol_date", new=property(lambda self: date.today() + timedelta(days=1))):
                self.assertIn("Weniger als ein Monat", str(Asset().time_to_eol))
            self.assert_empty_placeholder(AssetMaintenanceTable([]).render_cost(None))
            self.assertIn("Am selben Tag", str(AssetMaintenanceTable([]).render_downtime_days(0)))
            self.assertIn("2 Tage", str(AssetMaintenanceTable([]).render_downtime_days(2)))
            self.assert_empty_placeholder(AssetMaintenanceTable([]).render_downtime_days(None))
            self.assert_empty_placeholder(AssetDisposalTable([]).render_recipient(None))
            self.assert_empty_placeholder(AssetDisposalTable([]).render_proceeds(None, SimpleNamespace()))
            self.assert_empty_placeholder(CategoryTable([]).render_color(None))
            self.assert_empty_placeholder(WarrantyTable([]).render_cost(None, SimpleNamespace()))

    def test_assignee_and_utility_fallbacks_are_rendered(self):
        table = SimpleNamespace(data=[])
        column = AssigneeColumn(location_field="location")
        cache_attr = f"_assignee_cache_{id(column)}"
        setattr(table, cache_attr, {})
        record = SimpleNamespace(
            pk=1,
            active_assignment=SimpleNamespace(),
            location=SimpleNamespace(get_absolute_url=Mock(side_effect=RuntimeError)),
        )
        bound = SimpleNamespace(_table=table)
        with override("de"):
            self.assertIn("Lagerort", str(column.render(record.pk, record, bound, table)))
        self.assertEqual(absolute("not a number"), "not a number")
        self.assertIsNone(localize_journal_comment(None))
        malformed = "Renewed subscription. Next renewal date: 2026-09-01. Cost: ."
        self.assertEqual(localize_journal_comment(malformed), malformed)
