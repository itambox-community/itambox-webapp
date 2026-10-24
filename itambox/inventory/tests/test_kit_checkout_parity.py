"""Issue #495: kit checkout parity with individual asset checkout.

Every kit checkout must compose the same per-asset operations an individual
``checkout_asset`` performs: explicit hardware selection by the operator,
custody receipt creation, reservation/lifecycle guards, loan fields, and
commit-safe notifications (no mail for rolled-back kits).
"""

import datetime
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from assets.models import (
    Asset,
    AssetAssignment,
    AssetReservation,
    AssetType,
    Category,
    Manufacturer,
    ReservationStatusChoices,
    StatusLabel,
)
from assets.services import checkout_asset, checkout_kit
from compliance.models import CustodyReceipt, CustodyTemplate
from core.context import (
    get_current_all_accessible,
    get_current_membership,
    get_current_tenant,
    override_current_tenant_scope,
)
from core.tests.mixins import TenantTestMixin
from inventory.forms import KitCheckoutForm
from inventory.models import Consumable, ConsumableAssignment, ConsumableStock, Kit, KitItem
from inventory.views import kit_views
from licenses.models import License, LicenseSeatAssignment
from organization.models import AssetHolder, Location, Role, Site, Tenant, TenantGroup
from software.models import Software


class KitCheckoutParityBase(TenantTestMixin, TestCase):
    """Shared world: one tenant, one kit with hardware rows, custody template."""

    def setUp(self):
        self.setup_tenant_context(name="Kit Parity", slug="kit-parity")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.deployable = StatusLabel.objects.create(name="KP In Stock", slug="kp-in-stock", type="deployable")
        self.deployed = StatusLabel.objects.create(name="KP In Use", slug="kp-in-use", type="deployed")
        self.manufacturer = Manufacturer.objects.create(name="Parity Vendor", slug="parity-vendor")
        self.category = Category.objects.create(name="Parity Laptops", slug="parity-laptops")
        self.consumable_category = Category.objects.create(
            name="Parity Consumables", slug="parity-consumables", applies_to={"consumable": True}
        )
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="Parity 14", slug="parity-14", category=self.category
        )
        self.template = CustodyTemplate.objects.create(
            tenant=self.tenant,
            category=self.category,
            is_active=True,
            require_acceptance=True,
            email_signature_request=True,
            signature_provider="local",
            name="Parity EULA",
            eula_text="Parity EULA terms.",
            disclaimer="Sign here.",
            qms_reference="QMS-PARITY-1",
        )
        self.site = Site.objects.create(name="Parity Site", slug="parity-site", tenant=self.tenant)
        self.location = Location.objects.create(
            name="Parity Warehouse", slug="parity-warehouse", site=self.site, tenant=self.tenant
        )
        self.holder = AssetHolder.objects.create(
            first_name="Kit",
            last_name="Recipient",
            upn="kit.recipient@parity",
            email="kit.recipient@parity",
            tenant=self.tenant,
        )
        self.kit = Kit.objects.create(name="Parity Hardware Kit", tenant=self.tenant)

    def tearDown(self):
        self.clear_tenant_context()
        super().tearDown()

    def make_asset(self, tag, status=None, asset_type=None, tenant=None):
        return Asset.objects.create(
            name=f"Device {tag}",
            asset_tag=tag,
            serial_number=f"SN-{tag}",
            asset_type=asset_type or self.asset_type,
            status=status or self.deployable,
            tenant=tenant or self.tenant,
        )

    def add_hardware_item(self, qty=1):
        return KitItem.objects.create(kit=self.kit, asset_type=self.asset_type, qty=qty)

    def make_consumable(self, name):
        return Consumable.objects.create(name=name, manufacturer=self.manufacturer, category=self.consumable_category)

    def default_deployed_status(self):
        """The tenant-wide default deployed label the services resolve to."""
        return StatusLabel.objects.filter(type="deployed").order_by("pk").first()

    def make_license(self, seats):
        software = Software.objects.create(name=f"KP Suite {seats}", manufacturer=self.manufacturer)
        return License.objects.create(name="KP Seat", software=software, seats=seats, tenant=self.tenant)

    def grant_role_permissions(self, *permissions):
        self.tenant_role.permissions = list(permissions)
        self.tenant_role.save(update_fields=["permissions"])

    @contextmanager
    def patched_notifications(self):
        """Email boundary double: no real SMTP is involved."""
        email_config = SimpleNamespace(enabled=True, from_address="itambox@example.test", test_recipient="")
        request = SimpleNamespace(user=self.tenant_user)
        with (
            patch("core.models.EmailSettings.load", return_value=email_config),
            patch("assets.services.send_mail") as send_mail,
        ):
            yield request, send_mail


class KitCheckoutSelectionTests(KitCheckoutParityBase):
    """Maintainer decision 1: explicit, server-validated hardware selection."""

    def test_hardware_row_requires_a_selection(self):
        self.add_hardware_item()
        self.make_asset("KP-1001")

        with self.assertRaisesMessage(ValidationError, "Select an asset for hardware item"):
            checkout_kit(self.kit, holder=self.holder, source_location=self.location)

        self.assertFalse(AssetAssignment._base_manager.filter(asset__asset_tag="KP-1001").exists())
        self.assertIsNone(Asset.objects.get(asset_tag="KP-1001").active_assignment)

    def test_selection_must_cover_only_hardware_rows(self):
        self.add_hardware_item()
        stock_item = KitItem.objects.create(kit=self.kit, consumable=self.make_consumable("Parity Cable"), qty=1)
        asset = self.make_asset("KP-1002")

        with self.assertRaisesMessage(ValidationError, "not hardware rows"):
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                selected_assets={stock_item.pk: asset.pk},
            )

        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())

    def test_unknown_item_key_is_rejected(self):
        self.add_hardware_item()
        asset = self.make_asset("KP-1003")

        with self.assertRaisesMessage(ValidationError, "not hardware rows"):
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                selected_assets={999999: asset.pk},
            )

    def test_duplicate_asset_across_rows_is_rejected(self):
        first = self.add_hardware_item()
        second = self.add_hardware_item()
        asset = self.make_asset("KP-1004")

        with self.assertRaisesMessage(ValidationError, "must use a different device"):
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                selected_assets={first.pk: asset.pk, second.pk: asset.pk},
            )

    def test_wrong_type_selection_is_rejected(self):
        item = self.add_hardware_item()
        other_type = AssetType.objects.create(manufacturer=self.manufacturer, model="Other", slug="kp-other")
        asset = self.make_asset("KP-1005", asset_type=other_type)

        with self.assertRaisesMessage(ValidationError, "does not match hardware item"):
            checkout_kit(
                self.kit, holder=self.holder, source_location=self.location, selected_assets={item.pk: asset.pk}
            )

        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())

    def test_foreign_tenant_selection_is_rejected(self):
        item = self.add_hardware_item()
        self.make_asset("KP-1006-LOCAL")  # a visible sibling must not mask the foreign pick
        other_tenant = Tenant.objects.create(name="Other Tenant", slug="kp-other-tenant")
        asset = self.make_asset("KP-1006", tenant=other_tenant)

        with self.assertRaises(ValidationError):
            checkout_kit(
                self.kit, holder=self.holder, source_location=self.location, selected_assets={item.pk: asset.pk}
            )

        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())
        self.assertFalse(AssetAssignment._base_manager.filter(asset__asset_tag="KP-1006-LOCAL").exists())

    def test_assigned_selection_is_rejected_without_silent_reassignment(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-1007")
        other_holder = AssetHolder.objects.create(
            first_name="Other", last_name="Holder", upn="other.holder@parity", tenant=self.tenant
        )
        checkout_asset(asset, holder=other_holder, user=self.tenant_user)
        original_assignment = AssetAssignment._base_manager.get(asset=asset, is_active=True)

        with self.assertRaisesMessage(ValidationError, "not available for issuance"):
            checkout_kit(
                self.kit, holder=self.holder, source_location=self.location, selected_assets={item.pk: asset.pk}
            )

        # The device must still belong to the original holder (no auto-checkin).
        current = AssetAssignment._base_manager.get(asset=asset, is_active=True)
        self.assertEqual(current.pk, original_assignment.pk)
        self.assertEqual(current.assigned_user, other_holder)

    def test_non_deployable_selection_is_rejected(self):
        item = self.add_hardware_item()

        for label_type in ("in_repair", "on_order", "archived"):
            blocked = StatusLabel.objects.create(
                name=f"KP {label_type}", slug=f"kp-{label_type.replace('_', '-')}", type=label_type
            )
            asset = self.make_asset(f"KP-1008-{label_type}", status=blocked)
            with self.subTest(status_type=label_type):
                with self.assertRaisesMessage(ValidationError, "not available for issuance"):
                    checkout_kit(
                        self.kit,
                        holder=self.holder,
                        source_location=self.location,
                        selected_assets={item.pk: asset.pk},
                    )
                self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())

    def test_invalid_mapping_payload_is_rejected(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-1009")
        payloads = {
            "none-key": {None: None},
            "bool-key": {True: asset.pk},
            "float-value": {item.pk: 1.5},
            "non-mapping": [(item.pk, asset.pk)],
        }

        for name, payload in payloads.items():
            with self.subTest(payload=name):
                with self.assertRaisesMessage(ValidationError, "hardware selection is invalid"):
                    checkout_kit(
                        self.kit,
                        holder=self.holder,
                        source_location=self.location,
                        selected_assets=payload,
                    )

        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())


class KitCheckoutParityTests(KitCheckoutParityBase):
    """Tracer + parity: kit-selected assets behave like individual checkouts."""

    def test_selected_asset_creates_same_receipt_and_assignment_as_individual_checkout(self):
        item = self.add_hardware_item()
        first_pick = self.make_asset("KP-2001")  # would win a silent first() pick
        control_asset = self.make_asset("KP-2002")
        chosen = self.make_asset("KP-2003")

        checkout_asset(control_asset, holder=self.holder, user=self.tenant_user, notes="Individual checkout")
        checkout_kit(
            self.kit,
            holder=self.holder,
            source_location=self.location,
            user=self.tenant_user,
            notes="Onboarding",
            selected_assets={item.pk: chosen.pk},
        )

        chosen.refresh_from_db()
        self.assertEqual(chosen.status, self.default_deployed_status())
        self.assertTrue(CustodyReceipt.objects.filter(asset=chosen, holder=self.holder).exists())

        first_pick.refresh_from_db()
        self.assertEqual(first_pick.status, self.deployable)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=first_pick).exists())

        kit_assignment = AssetAssignment._base_manager.get(asset=chosen)
        control_assignment = AssetAssignment._base_manager.get(asset=control_asset)
        self.assertEqual(kit_assignment.assigned_user, self.holder)
        self.assertTrue(kit_assignment.is_active)
        self.assertEqual(kit_assignment.pre_checkout_status, self.deployable)
        self.assertEqual(kit_assignment.checked_out_by, self.tenant_user)
        self.assertIn("Checked out via Kit", kit_assignment.notes)
        self.assertEqual(control_assignment.pre_checkout_status, self.deployable)
        self.assertIsNone(kit_assignment.expected_checkin_date)
        self.assertFalse(kit_assignment.is_loan)
        self.assertIsNone(kit_assignment.due_date)

        kit_receipt = CustodyReceipt.objects.get(asset=chosen, holder=self.holder)
        control_receipt = CustodyReceipt.objects.get(asset=control_asset, holder=self.holder)
        for receipt in (kit_receipt, control_receipt):
            self.assertEqual(receipt.custody_template, self.template)
            self.assertEqual(receipt.signature_provider, "local")
            self.assertEqual(receipt.eula_text, "Parity EULA terms.")
            self.assertEqual(receipt.disclaimer, "Sign here.")
            self.assertEqual(receipt.qms_reference, "QMS-PARITY-1")

    def test_person_checkout_preserves_base_location(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-2101")
        asset.location = self.location
        asset.save(update_fields=["location"])

        checkout_kit(
            self.kit,
            holder=self.holder,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={item.pk: asset.pk},
        )

        asset.refresh_from_db()
        self.assertEqual(asset.location_id, self.location.pk)

    def test_checkout_to_location_updates_location(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-2102")
        destination = Location.objects.create(
            name="Parity Desk", slug="parity-desk", site=self.site, tenant=self.tenant
        )

        checkout_kit(
            self.kit,
            location=destination,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={item.pk: asset.pk},
        )

        asset.refresh_from_db()
        self.assertEqual(asset.location_id, destination.pk)

    def test_reservation_conflict_matches_individual_checkout_error(self):
        item = self.add_hardware_item()
        reserved_for_other = self.make_asset("KP-2201")
        control = self.make_asset("KP-2202")
        other_holder = AssetHolder.objects.create(
            first_name="Reserved", last_name="Holder", upn="reserved.holder@parity", tenant=self.tenant
        )
        today = datetime.date.today()
        for asset in (reserved_for_other, control):
            AssetReservation.objects.create(
                asset=asset,
                reserved_for=other_holder,
                start_date=today - datetime.timedelta(days=1),
                end_date=today + datetime.timedelta(days=1),
                status=ReservationStatusChoices.ACTIVE,
            )

        with self.assertRaises(ValidationError) as individual:
            checkout_asset(control, holder=self.holder, user=self.tenant_user)

        with self.assertRaises(ValidationError) as via_kit:
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                user=self.tenant_user,
                selected_assets={item.pk: reserved_for_other.pk},
            )

        self.assertEqual(str(via_kit.exception), str(individual.exception))
        for asset in (reserved_for_other, control):
            self.assertEqual(Asset.objects.get(pk=asset.pk).status, self.deployable)
            self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())

    def test_loan_and_status_fields_flow_to_every_selected_asset(self):
        item = self.add_hardware_item()
        second_item = self.add_hardware_item()
        first = self.make_asset("KP-2301")
        second = self.make_asset("KP-2302")
        custom_deployed = StatusLabel.objects.create(name="Managed", slug="kp-managed", type="deployed")
        due = datetime.date.today() + datetime.timedelta(days=14)
        expected = datetime.date.today() + datetime.timedelta(days=7)

        checkout_kit(
            self.kit,
            holder=self.holder,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={item.pk: first.pk, second_item.pk: second.pk},
            expected_checkin=expected,
            is_loan=True,
            due_date=due,
            status=custom_deployed,
        )

        for asset in (first, second):
            assignment = AssetAssignment._base_manager.get(asset=asset, is_active=True)
            self.assertTrue(assignment.is_loan)
            self.assertEqual(assignment.due_date, due)
            self.assertEqual(assignment.expected_checkin_date, expected)
            asset.refresh_from_db()
            self.assertEqual(asset.status, custom_deployed)

    def test_license_row_assigns_seat_to_the_selected_device_for_location_targets(self):
        hardware = self.add_hardware_item()
        asset = self.make_asset("KP-2701")
        license_obj = self.make_license(seats=1)
        KitItem.objects.create(kit=self.kit, license=license_obj)

        checkout_kit(
            self.kit,
            location=self.location,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={hardware.pk: asset.pk},
        )

        seat = LicenseSeatAssignment._base_manager.get(license=license_obj)
        self.assertEqual(seat.asset_id, asset.pk)
        self.assertIsNone(seat.assigned_holder_id)

    def test_license_only_kit_for_location_target_is_rejected(self):
        license_obj = self.make_license(seats=1)
        KitItem.objects.create(kit=self.kit, license=license_obj)

        with self.assertRaisesMessage(ValidationError, "must be assigned to either a Holder or an Asset"):
            checkout_kit(self.kit, location=self.location, source_location=self.location, user=self.tenant_user)

        self.assertFalse(LicenseSeatAssignment._base_manager.filter(license=license_obj).exists())

    def test_exhausted_license_pool_fails_the_whole_kit_before_any_allocation(self):
        hardware = self.add_hardware_item()
        asset = self.make_asset("KP-2702")
        license_obj = self.make_license(seats=1)
        KitItem.objects.create(kit=self.kit, license=license_obj)
        LicenseSeatAssignment.objects.create(license=license_obj, assigned_holder=self.holder)

        with self.assertRaisesMessage(ValidationError, "No available seats"):
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                user=self.tenant_user,
                selected_assets={hardware.pk: asset.pk},
            )

        asset.refresh_from_db()
        self.assertEqual(asset.status, self.deployable)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())

    def test_rollback_discards_notifications_and_custody_receipts(self):
        # The shortage must be the intended one: grant the stock permission so
        # the failure is "no stock", not an unrelated denied permission.
        self.grant_role_permissions("inventory.add_consumableassignment")
        hardware = self.add_hardware_item()
        asset = self.make_asset("KP-2401")
        license_obj = self.make_license(seats=2)
        KitItem.objects.create(kit=self.kit, license=license_obj)
        consumable = self.make_consumable("Parity Tape")
        KitItem.objects.create(kit=self.kit, consumable=consumable, qty=5)
        stock = ConsumableStock.objects.create(consumable=consumable, location=self.location, qty=1)

        with self.patched_notifications() as (request, send_mail):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaisesMessage(ValidationError, "No stock available") as raised:
                    checkout_kit(
                        self.kit,
                        holder=self.holder,
                        source_location=self.location,
                        user=self.tenant_user,
                        request=request,
                        selected_assets={hardware.pk: asset.pk},
                    )

        self.assertIn("No stock available", str(raised.exception))
        self.assertEqual(callbacks, [])
        send_mail.assert_not_called()
        # Re-query every allocation surface after the late failure: nothing may
        # survive the rollback.
        self.assertEqual(CustodyReceipt.objects.filter(asset=asset).count(), 0)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())
        asset.refresh_from_db()
        self.assertEqual(asset.status, self.deployable)
        self.assertFalse(LicenseSeatAssignment._base_manager.filter(license=license_obj).exists())
        self.assertFalse(ConsumableAssignment._base_manager.filter(consumable=consumable).exists())
        stock.refresh_from_db()
        self.assertEqual(stock.qty, 1)

    def test_commit_sends_exactly_one_notification_per_custody_receipt(self):
        first_item = self.add_hardware_item()
        second_item = self.add_hardware_item()
        first = self.make_asset("KP-2501")
        second = self.make_asset("KP-2502")

        with self.patched_notifications() as (request, send_mail):
            with self.captureOnCommitCallbacks(execute=True):
                checkout_kit(
                    self.kit,
                    holder=self.holder,
                    source_location=self.location,
                    user=self.tenant_user,
                    request=request,
                    selected_assets={first_item.pk: first.pk, second_item.pk: second.pk},
                )

        receipts = CustodyReceipt.objects.filter(holder=self.holder).order_by("asset__asset_tag")
        self.assertEqual(receipts.count(), 2)
        self.assertEqual(send_mail.call_count, 2)

        sent = {}
        for call in send_mail.call_args_list:
            sent[call.kwargs["subject"]] = call.kwargs
            self.assertEqual(call.kwargs["from_email"], "itambox@example.test")
            self.assertEqual(call.kwargs["recipient_list"], [self.holder.email])
            self.assertFalse(call.kwargs["fail_silently"])
            self.assertIn("Accept custody using this link:", call.kwargs["message"])
            self.assertIn("/compliance/custody/sign/", call.kwargs["message"])
            self.assertIn("This link expires in 7 days.", call.kwargs["message"])

        for asset in (first, second):
            subject = f"Asset Acceptance Required: {asset.name} ({asset.asset_tag})"
            self.assertIn(subject, sent)
            message = sent[subject]["message"]
            self.assertIn(f"Asset: {asset.name}", message)
            self.assertIn(f"Asset Tag: {asset.asset_tag}", message)
            self.assertIn(f"Serial: {asset.serial_number}", message)

    def test_notification_failure_after_commit_preserves_assignment_and_receipt(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-NOTIFICATION-FAILURE")
        sign_url = Mock(side_effect=ValidationError("Signing link could not be built"))
        with (
            self.patched_notifications() as (request, send_mail),
            patch("assets.services.build_custody_sign_url", sign_url),
        ):
            with self.assertLogs("assets.services", level="ERROR") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    checkout_kit(
                        self.kit,
                        holder=self.holder,
                        user=self.tenant_user,
                        request=request,
                        selected_assets={item.pk: chosen.pk},
                    )
                    sign_url.assert_not_called()
            sign_url.assert_called_once()
            send_mail.assert_not_called()
        self.assertTrue(AssetAssignment._base_manager.filter(asset=chosen, is_active=True).exists())
        self.assertTrue(CustodyReceipt.objects.filter(asset=chosen, holder=self.holder).exists())
        self.assertIn("exception_type=ValidationError", logs.output[0])

    def test_individual_checkout_keeps_its_post_commit_notification(self):
        control = self.make_asset("KP-2601")

        with self.patched_notifications() as (request, send_mail):
            with self.captureOnCommitCallbacks(execute=True):
                checkout_asset(control, holder=self.holder, user=self.tenant_user, request=request)

        send_mail.assert_called_once()
        self.assertTrue(AssetAssignment._base_manager.filter(asset=control, is_active=True).exists())
        self.assertTrue(CustodyReceipt.objects.filter(asset=control, holder=self.holder).exists())

    def test_rollback_discards_single_checkout_notification_too(self):
        control = self.make_asset("KP-2602")
        self.holder.email = ""
        self.holder.save(update_fields=["email"])

        with self.patched_notifications() as (request, send_mail):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(ValidationError):
                    # Missing holder e-mail must still reject inside the transaction.
                    checkout_asset(control, holder=self.holder, user=self.tenant_user, request=request)

        self.assertEqual(callbacks, [])
        send_mail.assert_not_called()
        self.assertFalse(AssetAssignment._base_manager.filter(asset=control).exists())
        self.assertEqual(CustodyReceipt.objects.filter(asset=control).count(), 0)

    def test_scheduled_notification_reenters_its_tenant_scope(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-2803")
        observed = []
        # Leave the service call scope and the scheduling scope behind before the
        # commit callbacks execute: they must restore the tenant themselves.
        self.clear_tenant_context()

        with self.patched_notifications() as (request, send_mail):
            send_mail.side_effect = lambda *args, **kwargs: observed.append(get_current_tenant())
            with self.captureOnCommitCallbacks(execute=True):
                with override_current_tenant_scope(self.tenant, self.tenant_membership):
                    checkout_kit(
                        self.kit,
                        holder=self.holder,
                        source_location=self.location,
                        user=self.tenant_user,
                        request=request,
                        selected_assets={item.pk: asset.pk},
                    )
                self.assertIsNone(get_current_tenant())

        self.assertEqual(observed, [self.tenant])


class KitCheckoutFormTests(KitCheckoutParityBase):
    """Per-row selection fields, scoping, and the cleaned selection mapping."""

    def test_form_exposes_one_required_field_per_hardware_row(self):
        first = self.add_hardware_item()
        second = self.add_hardware_item()
        consumable = Consumable.objects.create(name="Parity Wipes", manufacturer=self.manufacturer)
        stock_item = KitItem.objects.create(kit=self.kit, consumable=consumable, qty=2)

        form = KitCheckoutForm(kit=self.kit)

        self.assertIn(f"asset_{first.pk}", form.fields)
        self.assertIn(f"asset_{second.pk}", form.fields)
        self.assertNotIn(f"asset_{stock_item.pk}", form.fields)
        self.assertTrue(form.fields[f"asset_{first.pk}"].required)
        self.assertTrue(form.fields[f"asset_{second.pk}"].required)

    def test_form_field_queryset_is_scoped_by_type_tenant_and_availability(self):
        item = self.add_hardware_item()
        eligible = self.make_asset("KP-3001")
        custom = self.make_asset(
            "KP-3002",
            status=StatusLabel.objects.create(name="Ready", slug="kp-ready", type="deployable"),
        )
        assigned = self.make_asset("KP-3003")
        checkout_asset(assigned, holder=self.holder, user=self.tenant_user)
        self.make_asset("KP-3004", status=StatusLabel.objects.create(name="Broken", slug="kp-broken", type="in_repair"))
        other_type = AssetType.objects.create(manufacturer=self.manufacturer, model="Other", slug="kp-other-2")
        self.make_asset("KP-3005", asset_type=other_type)
        other_tenant = Tenant.objects.create(name="Hidden Tenant", slug="kp-hidden-tenant")
        self.make_asset("KP-3006", tenant=other_tenant)

        form = KitCheckoutForm(kit=self.kit)
        self.assertIn(f"asset_{item.pk}", form.fields)
        choices = set(form.fields[f"asset_{item.pk}"].queryset)

        self.assertEqual(choices, {eligible, custom})
        label = form.fields[f"asset_{item.pk}"].label_from_instance(custom)
        self.assertIn("KP-3002", label)
        self.assertIn("SN-KP-3002", label)

    def test_form_builds_selected_assets_mapping(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-3101")

        form = KitCheckoutForm(
            data={
                "source_location": self.location.pk,
                "assigned_holder": self.holder.pk,
                "assigned_location": "",
                "assigned_asset": "",
                "notes": "Form selection",
                f"asset_{item.pk}": asset.pk,
            },
            kit=self.kit,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("selected_assets", form.cleaned_data)
        self.assertEqual(form.cleaned_data["selected_assets"], {item.pk: asset.pk})

    def test_form_rejects_duplicate_device_across_rows(self):
        first = self.add_hardware_item()
        second = self.add_hardware_item()
        asset = self.make_asset("KP-3102")

        form = KitCheckoutForm(
            data={
                "source_location": self.location.pk,
                "assigned_holder": self.holder.pk,
                "assigned_location": "",
                "assigned_asset": "",
                f"asset_{first.pk}": asset.pk,
                f"asset_{second.pk}": asset.pk,
            },
            kit=self.kit,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(f"asset_{second.pk}", form.errors)
        self.assertIn("must use a different device", form.errors[f"asset_{second.pk}"][0])

    def test_form_requires_every_hardware_row(self):
        item = self.add_hardware_item()
        self.make_asset("KP-3103")

        form = KitCheckoutForm(
            data={
                "source_location": self.location.pk,
                "assigned_holder": self.holder.pk,
                "assigned_location": "",
                "assigned_asset": "",
            },
            kit=self.kit,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(f"asset_{item.pk}", form.errors)

    def test_device_without_serial_uses_the_plain_asset_label(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-3104")
        asset.serial_number = ""
        asset.save(update_fields=["serial_number"])

        form = KitCheckoutForm(kit=self.kit)
        label = form.fields[f"asset_{item.pk}"].label_from_instance(asset)

        self.assertEqual(label, "Device KP-3104 (KP-3104)")

    def test_form_without_a_kit_has_no_device_fields(self):
        form = KitCheckoutForm()

        self.assertEqual(form.hardware_items, [])
        self.assertFalse([name for name in form.fields if name.startswith("asset_")])

    def test_global_kit_form_without_a_resolved_tenant_offers_no_devices(self):
        global_kit = Kit.objects.create(name="KP Global Kit")
        item = KitItem.objects.create(kit=global_kit, asset_type=self.asset_type)
        self.make_asset("KP-3105")
        self.clear_tenant_context()

        try:
            form = KitCheckoutForm(kit=global_kit)
            choices = set(form.fields[f"asset_{item.pk}"].queryset)
            holder_choices = list(form.fields["assigned_holder"].queryset)
        finally:
            self.set_active_tenant(self.tenant, self.tenant_membership)

        self.assertEqual(choices, set())
        self.assertEqual(holder_choices, [])

    def test_global_kit_form_scopes_choices_to_the_active_tenant(self):
        global_kit = Kit.objects.create(name="KP Global Kit Scoped")
        item = KitItem.objects.create(kit=global_kit, asset_type=self.asset_type)
        local = self.make_asset("KP-3106")
        other_tenant = Tenant.objects.create(name="KP Scope Hidden", slug="kp-scope-hidden")
        self.make_asset("KP-3107", tenant=other_tenant)

        form = KitCheckoutForm(kit=global_kit)

        self.assertEqual(set(form.fields[f"asset_{item.pk}"].queryset), {local})
        self.assertEqual(list(form.fields["assigned_holder"].queryset), [self.holder])


class KitCheckoutRouteTests(KitCheckoutParityBase):
    """The existing HTMX checkout route drives the explicit selection."""

    def _login(self):
        # setup_tenant_context already created the membership, so
        # client_login_to_tenant() would not apply role_permissions again.
        self.grant_role_permissions("inventory.view_kit", "inventory.change_kit")
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _post_data(self, **extra):
        data = {
            "source_location": self.location.pk,
            "assigned_holder": self.holder.pk,
            "assigned_location": "",
            "assigned_asset": "",
            "notes": "Route checkout",
        }
        data.update(extra)
        return data

    def test_modal_lists_device_choices_with_tags(self):
        item = self.add_hardware_item()
        self.make_asset("KP-4001")
        self._login()

        response = self.client.get(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        for field in (f"asset_{item.pk}", "status", "checkout_date", "expected_checkin", "is_loan", "due_date"):
            self.assertIn(field, form.fields)
        self.assertContains(response, "KP-4001")

    def test_post_without_selection_is_rejected_and_allocates_nothing(self):
        item = self.add_hardware_item()
        asset = self.make_asset("KP-4002")
        self._login()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(f"asset_{item.pk}", response.content.decode())
        asset.refresh_from_db()
        self.assertEqual(asset.status, self.deployable)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=asset).exists())

    def test_post_with_selection_checks_out_the_chosen_device(self):
        item = self.add_hardware_item()
        ignored = self.make_asset("KP-4003")  # would win a silent first() pick
        chosen = self.make_asset("KP-4004")
        self._login()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(**{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        chosen.refresh_from_db()
        ignored.refresh_from_db()
        self.assertEqual(chosen.status, self.default_deployed_status())
        self.assertEqual(ignored.status, self.deployable)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=ignored).exists())
        self.assertTrue(
            AssetAssignment._base_manager.filter(asset=chosen, assigned_user=self.holder, is_active=True).exists()
        )
        self.assertTrue(CustodyReceipt.objects.filter(asset=chosen, holder=self.holder).exists())

    def test_all_accessible_checkout_uses_explicit_target_tenant(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-COORD-ALL")
        self._login()
        session = self.client.session
        session.pop("active_tenant_id", None)
        session["active_all_accessible"] = True
        session.save()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        self.assertTrue(
            AssetAssignment._base_manager.filter(asset=chosen, assigned_user=self.holder, is_active=True).exists()
        )

    def test_loan_without_mandatory_return_date_is_rejected(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-COORD-LOAN")
        self._login()
        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(is_loan="on", **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())

    def test_view_does_not_leak_dynamic_fields_into_the_service_call(self):
        from inventory.views import kit_views

        item = self.add_hardware_item()
        chosen = self.make_asset("KP-4005")
        self._login()
        recorded = {}
        real = kit_views.checkout_kit

        def recording_service(kit, **kwargs):
            recorded.update(kwargs)
            return real(kit, **kwargs)

        with patch.object(kit_views.KitCheckoutView, "service_callable", side_effect=recording_service):
            response = self.client.post(
                reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
                data=self._post_data(**{f"asset_{item.pk}": chosen.pk}),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(recorded.get("selected_assets"), {item.pk: chosen.pk})
        self.assertFalse([key for key in recorded if key.startswith("asset_")], recorded)
        self.assertEqual(recorded.get("holder"), self.holder)
        self.assertEqual(recorded.get("location"), None)

    def test_custom_deployable_status_counts_as_available(self):
        self.add_hardware_item()
        custom = StatusLabel.objects.create(name="Field Ready", slug="kp-field-ready", type="deployable")
        asset = self.make_asset("KP-4101", status=custom)
        self._login()

        response = self.client.get(reverse("inventory:kit_detail", kwargs={"pk": self.kit.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deploy / Checkout Kit")
        self.assertEqual(asset.status, custom)


class KitCheckoutStockOnlyRegressionTests(KitCheckoutParityBase):
    """Stock-only kits keep their public call signature and behaviour."""

    def test_stock_only_kit_restores_omitted_selection_compatibility(self):
        self.grant_role_permissions("inventory.add_consumableassignment")
        consumable = self.make_consumable("Parity Labels")
        item = KitItem.objects.create(kit=self.kit, consumable=consumable, qty=2)
        ConsumableStock.objects.create(consumable=consumable, location=self.location, qty=10)

        checkout_kit(self.kit, holder=self.holder, source_location=self.location, user=self.tenant_user)

        assignment = ConsumableAssignment._base_manager.get(consumable=consumable, assigned_holder=self.holder)
        self.assertEqual(assignment.qty, 2)
        self.assertEqual(assignment.from_location_id, self.location.pk)
        self.assertEqual(item.qty, 2)

    def test_empty_mapping_is_equivalent_to_omitted_for_stock_kits(self):
        self.grant_role_permissions("inventory.add_consumableassignment")
        consumable = self.make_consumable("Parity Seals")
        KitItem.objects.create(kit=self.kit, consumable=consumable, qty=1)
        ConsumableStock.objects.create(consumable=consumable, location=self.location, qty=5)

        checkout_kit(
            self.kit,
            holder=self.holder,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={},
        )

        self.assertTrue(
            ConsumableAssignment._base_manager.filter(consumable=consumable, assigned_holder=self.holder).exists()
        )


class KitCheckoutGermanRenderingTests(KitCheckoutParityBase):
    """The compiled German catalog must serve the new checkout copy."""

    def test_new_kit_checkout_copy_renders_in_german(self):
        item = self.add_hardware_item()
        self.make_asset("KP-5001")

        with override("de"):
            form = KitCheckoutForm(kit=self.kit)
            device_field = form.fields[f"asset_{item.pk}"]
            rendered = {
                "label": str(device_field.label),
                "empty": str(device_field.empty_label),
                "loan": str(form.fields["is_loan"].label),
            }
            with self.assertRaises(ValidationError) as raised:
                checkout_kit(self.kit, holder=self.holder, source_location=self.location)

        self.assertIn("Ger\u00e4t f\u00fcr", rendered["label"])
        self.assertIn("Ger\u00e4t ausw\u00e4hlen", rendered["empty"])
        self.assertIn("Leihgabe", rendered["loan"])
        self.assertIn("W\u00e4hlen Sie ein Asset f\u00fcr die Hardware-Position", str(raised.exception))

    def test_loan_due_date_error_and_status_error_render_in_german(self):
        item = self.add_hardware_item()
        self.make_asset("KP-5002")

        with override("de"):
            form = KitCheckoutForm(
                data={
                    "source_location": self.location.pk,
                    "assigned_holder": self.holder.pk,
                    "assigned_location": "",
                    "assigned_asset": "",
                    "is_loan": "on",
                    f"asset_{item.pk}": "",
                },
                kit=self.kit,
            )
            self.assertFalse(form.is_valid())
            loan_message = str(form.errors["due_date"][0])
            with self.assertRaises(ValidationError) as raised:
                checkout_kit(
                    self.kit,
                    holder=self.holder,
                    source_location=self.location,
                    status=self.deployable,
                )

        self.assertIn("F\u00e4lligkeitsdatum", loan_message)
        self.assertIn("Deployed", str(raised.exception))


class KitCheckoutTenantScopeTests(KitCheckoutRouteTests):
    """Canonical scope contract: aggregate scopes bind to an explicit tenant."""

    def _activate_all_accessible(self):
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session["active_all_accessible"] = True
        session.save()

    def _activate_tenant_group(self, group):
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_all_accessible", None)
        session["active_tenant_group_id"] = group.pk
        session.save()

    def _add_accessible_tenant(self, name, slug, permissions=()):
        tenant = Tenant.objects.create(name=name, slug=slug)
        role = Role.objects.create(tenant=tenant, name=f"{name} Role", permissions=list(permissions))
        self.grant(self.tenant_user, tenant, role)
        return tenant

    def test_target_candidates_do_not_bypass_canonical_scope_for_superusers(self):
        from core.tenant_scope import accessible_tenant_ids
        from inventory.forms.kit_forms import kit_target_tenant_queryset

        global_kit = Kit.objects.create(name="KP Superuser Scope")
        request = SimpleNamespace(user=self.tenant_admin, active_tenant_group=None)
        canonical_ids = accessible_tenant_ids(self.tenant_admin)
        candidates = kit_target_tenant_queryset(request, global_kit)
        self.assertEqual(set(candidates.values_list("pk", flat=True)), set(canonical_ids))

    def test_group_scope_checkout_uses_explicit_target_tenant(self):
        group = TenantGroup.objects.create(name="KP Scope Group", slug="kp-scope-group")
        self.tenant.group = group
        self.tenant.save(update_fields=["group"])
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-GROUP")
        self._login()
        self._activate_tenant_group(group)

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        self.assertTrue(AssetAssignment._base_manager.filter(asset=chosen, is_active=True).exists())
        self.assertTrue(CustodyReceipt.objects.filter(asset=chosen, holder=self.holder).exists())

    def test_group_scope_rejects_an_accessible_tenant_outside_the_group(self):
        group = TenantGroup.objects.create(name="KP Scope Group 2", slug="kp-scope-group-2")
        self.tenant.group = group
        self.tenant.save(update_fields=["group"])
        outside = self._add_accessible_tenant("KP Outside", "kp-outside", permissions=["inventory.change_kit"])
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-OUT")
        self._login()
        self._activate_tenant_group(group)

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=outside.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())
        self.assertEqual(CustodyReceipt.objects.filter(asset=chosen).count(), 0)

    def test_all_accessible_rejects_an_inaccessible_tenant(self):
        hidden = Tenant.objects.create(name="KP Not Yours", slug="kp-not-yours")
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-HIDDEN")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=hidden.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())

    def test_all_accessible_requires_an_explicit_target_tenant(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-MISSING")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(**{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("tenant", response.content.decode())
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())

    def test_forged_tenant_override_is_ignored_in_a_concrete_scope(self):
        forged = Tenant.objects.create(name="KP Forged", slug="kp-forged")
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-CONCRETE")
        self._login()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=forged.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        self.assertTrue(AssetAssignment._base_manager.filter(asset=chosen, is_active=True).exists())
        self.assertTrue(CustodyReceipt.objects.filter(asset=chosen, holder=self.holder).exists())

    def test_all_accessible_rejects_a_wrong_tenant_holder(self):
        other = self._add_accessible_tenant(
            "KP Holder Tenant", "kp-holder-tenant", permissions=["inventory.change_kit"]
        )
        foreign_holder = AssetHolder.objects.create(
            first_name="Foreign", last_name="Holder", upn="foreign.holder@kp", email="foreign.holder@kp", tenant=other
        )
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-WRONG-HOLDER")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(
                tenant=self.tenant.pk, assigned_holder=foreign_holder.pk, **{f"asset_{item.pk}": chosen.pk}
            ),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())

    def test_all_accessible_rejects_a_wrong_tenant_device(self):
        other = self._add_accessible_tenant(
            "KP Device Tenant", "kp-device-tenant", permissions=["inventory.change_kit"]
        )
        foreign_device = self.make_asset("KP-SCOPE-WRONG-DEVICE", tenant=other)
        self.add_hardware_item()
        self.make_asset("KP-SCOPE-RIGHT-DEVICE")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, **{f"asset_{self.kit.items.get().pk}": foreign_device.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 422)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=foreign_device).exists())

    def test_modal_denies_kits_of_inaccessible_tenants(self):
        hidden = Tenant.objects.create(name="KP Hidden Kit Tenant", slug="kp-hidden-kit")
        hidden_kit = Kit.objects.create(name="KP Hidden Kit", tenant=hidden)
        self._login()
        self._activate_all_accessible()

        response = self.client.get(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": hidden_kit.pk}), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.status_code, 404)

    def test_all_accessible_modal_preselects_the_owner_and_scopes_its_choices(self):
        item = self.add_hardware_item()
        device = self.make_asset("KP-SCOPE-MODAL")
        self._login()
        self._activate_all_accessible()

        response = self.client.get(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("tenant", form.fields)
        self.assertTrue(form.fields["tenant"].required)
        self.assertEqual(set(form.fields["tenant"].queryset), {self.tenant})
        # Issue #495/#523 follow-up: the modal opens ON the kit's owning tenant,
        # so the dependent choices are the OWNER's -- not empty, and never
        # unscoped. (the tenant-scoped choice fields re-apply the tenant scope on
        # access, so these reads are the ambient-scope projection of the
        # owner-scoped querysets the modal rendered.)
        self.assertEqual(list(form.fields["assigned_holder"].queryset), [self.holder])
        self.assertEqual(set(form.fields[f"asset_{item.pk}"].queryset), {device})

    def test_htmx_reload_scopes_choices_to_the_chosen_tenant(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-RELOAD")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, _reload="1", **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(list(form.fields["assigned_holder"].queryset), [self.holder])
        self.assertEqual(set(form.fields[f"asset_{item.pk}"].queryset), {chosen})
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())

    def test_checkout_restores_the_previous_scope(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-SCOPE-RESTORE")
        self._login()
        self._activate_all_accessible()
        seen = {}
        restorations = []
        real_scope = kit_views.override_current_tenant_scope

        @contextmanager
        def spy_scope(tenant, membership):
            before = (get_current_tenant(), get_current_all_accessible())
            with real_scope(tenant, membership):
                yield
            restorations.append((get_current_tenant(), get_current_all_accessible()) == before)

        def recording_service(kit, **kwargs):
            seen["tenant"] = get_current_tenant()
            seen["membership"] = get_current_membership()
            seen["kwargs"] = kwargs
            return kit

        with (
            patch.object(kit_views.KitCheckoutView, "service_callable", side_effect=recording_service),
            patch("inventory.views.kit_views.override_current_tenant_scope", spy_scope),
        ):
            response = self.client.post(
                reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
                data=self._post_data(tenant=self.tenant.pk, **{f"asset_{item.pk}": chosen.pk}),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 204, response.content.decode())
        self.assertEqual(seen["tenant"], self.tenant)
        self.assertEqual(seen["membership"], self.tenant_membership)
        self.assertEqual(seen["kwargs"].get("selected_assets"), {item.pk: chosen.pk})
        self.assertNotIn("tenant", seen["kwargs"])
        self.assertNotIn("target_tenant", seen["kwargs"])
        self.assertEqual(restorations, [True])
        self.assertEqual(get_current_tenant(), self.tenant)

    def test_global_kit_checkout_binds_to_the_selected_tenant(self):
        global_kit = Kit.objects.create(name="KP Scope Global")
        item = KitItem.objects.create(kit=global_kit, asset_type=self.asset_type)
        chosen = self.make_asset("KP-SCOPE-GLOBAL")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": global_kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        self.assertTrue(AssetAssignment._base_manager.filter(asset=chosen, is_active=True).exists())

    def test_global_kit_requires_change_kit_in_the_selected_tenant(self):
        global_kit = Kit.objects.create(name="KP Scope Global Denied")
        item = KitItem.objects.create(kit=global_kit, asset_type=self.asset_type)
        chosen = self.make_asset("KP-SCOPE-GLOBAL-DENIED")
        powerless = self._add_accessible_tenant("KP Powerless", "kp-powerless")
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": global_kit.pk}),
            data=self._post_data(tenant=powerless.pk, **{f"asset_{item.pk}": chosen.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())


class KitCheckoutLoanContractTests(KitCheckoutRouteTests):
    """Loan/status contract: form and service agree, HTTP persists the fields."""

    def test_http_loan_with_due_date_persists_shared_checkout_fields(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-LOAN-1")
        self._login()
        due = datetime.date.today() + datetime.timedelta(days=21)
        expected = datetime.date.today() + datetime.timedelta(days=10)
        checked = datetime.date.today() - datetime.timedelta(days=1)

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(
                **{
                    f"asset_{item.pk}": chosen.pk,
                    "is_loan": "on",
                    "due_date": due.isoformat(),
                    "expected_checkin": expected.isoformat(),
                    "checkout_date": checked.isoformat(),
                }
            ),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        assignment = AssetAssignment._base_manager.get(asset=chosen, is_active=True)
        self.assertTrue(assignment.is_loan)
        self.assertEqual(assignment.due_date, due)
        self.assertEqual(assignment.expected_checkin_date, expected)
        self.assertEqual(timezone.localtime(assignment.checked_out_at).date(), checked)

    def test_http_deployed_status_choice_is_applied_to_the_checkout(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-LOAN-2")
        custom = StatusLabel.objects.create(name="KP Field Ready", slug="kp-field-ready", type="deployed")
        self._login()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(**{f"asset_{item.pk}": chosen.pk, "status": custom.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        chosen.refresh_from_db()
        self.assertEqual(chosen.status, custom)

    def test_service_rejects_a_loan_without_due_date_before_side_effects(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-LOAN-3")

        with self.assertRaisesMessage(ValidationError, "requires a due date"):
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                selected_assets={item.pk: chosen.pk},
                is_loan=True,
            )

        chosen.refresh_from_db()
        self.assertEqual(chosen.status, self.deployable)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())
        self.assertEqual(CustodyReceipt.objects.filter(asset=chosen).count(), 0)

    def test_service_rejects_a_non_deployed_status_label(self):
        item = self.add_hardware_item()
        chosen = self.make_asset("KP-LOAN-4")

        with self.assertRaisesMessage(ValidationError, "not a deployed status label"):
            checkout_kit(
                self.kit,
                holder=self.holder,
                source_location=self.location,
                selected_assets={item.pk: chosen.pk},
                status=self.deployable,
            )

        self.assertFalse(AssetAssignment._base_manager.filter(asset=chosen).exists())


class KitCheckoutLicensePoolTests(KitCheckoutParityBase):
    """Repeated license rows keep unique-target semantics and single-seat pools."""

    def test_repeated_license_rows_do_not_overallocate_a_single_seat_pool(self):
        license_obj = self.make_license(seats=1)
        KitItem.objects.create(kit=self.kit, license=license_obj)
        KitItem.objects.create(kit=self.kit, license=license_obj)

        checkout_kit(self.kit, holder=self.holder, source_location=self.location, user=self.tenant_user)

        seats = LicenseSeatAssignment._base_manager.filter(license=license_obj)
        self.assertEqual(seats.count(), 1)
        self.assertEqual(seats.get().assigned_holder_id, self.holder.pk)

    def test_repeated_license_rows_are_deduplicated_for_asset_targets(self):
        hardware = self.add_hardware_item()
        asset = self.make_asset("KP-LIC-1")
        license_obj = self.make_license(seats=1)
        KitItem.objects.create(kit=self.kit, license=license_obj)
        KitItem.objects.create(kit=self.kit, license=license_obj)

        checkout_kit(
            self.kit,
            location=self.location,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={hardware.pk: asset.pk},
        )

        seats = LicenseSeatAssignment._base_manager.filter(license=license_obj)
        self.assertEqual(seats.count(), 1)
        self.assertEqual(seats.get().asset_id, asset.pk)

    def test_repeated_license_rows_still_reject_an_exhausted_pool(self):
        license_obj = self.make_license(seats=1)
        KitItem.objects.create(kit=self.kit, license=license_obj)
        KitItem.objects.create(kit=self.kit, license=license_obj)
        LicenseSeatAssignment.objects.create(license=license_obj, assigned_holder=self.holder)

        with self.assertRaisesMessage(ValidationError, "No available seats"):
            checkout_kit(self.kit, holder=self.holder, source_location=self.location, user=self.tenant_user)

        self.assertEqual(LicenseSeatAssignment._base_manager.filter(license=license_obj).count(), 1)


class KitCheckoutCustodyTemplatePriorityTests(KitCheckoutParityBase):
    """The kit path resolves custody templates exactly like the individual path."""

    def _group_and_global_templates(self):
        group = TenantGroup.objects.create(name="KP Custody Group", slug="kp-custody-group")
        self.tenant.group = group
        self.tenant.save(update_fields=["group"])
        group_template = CustodyTemplate.objects.create(
            tenant_group=group,
            category=self.category,
            is_active=True,
            require_acceptance=True,
            name="KP Group EULA",
            eula_text="Group terms.",
            disclaimer="Sign.",
            qms_reference="QMS-GROUP",
        )
        global_template = CustodyTemplate.objects.create(
            category=self.category,
            is_active=True,
            require_acceptance=True,
            name="KP Global EULA",
            eula_text="Global terms.",
            disclaimer="Sign.",
            qms_reference="QMS-GLOBAL",
        )
        return group_template, global_template

    def _receipt_for(self, tag):
        item = self.add_hardware_item()
        asset = self.make_asset(tag)
        checkout_kit(
            self.kit,
            holder=self.holder,
            source_location=self.location,
            user=self.tenant_user,
            selected_assets={item.pk: asset.pk},
        )
        return CustodyReceipt.objects.get(asset=asset)

    def test_tenant_template_wins_over_group_and_global(self):
        self._group_and_global_templates()

        receipt = self._receipt_for("KP-TMPL-1")

        self.assertEqual(receipt.custody_template_id, self.template.pk)

    def test_group_template_is_used_without_a_tenant_template(self):
        group_template, _ = self._group_and_global_templates()
        self.template.is_active = False
        self.template.save(update_fields=["is_active"])

        receipt = self._receipt_for("KP-TMPL-2")

        self.assertEqual(receipt.custody_template_id, group_template.pk)

    def test_global_template_is_used_without_a_tenant_or_group_template(self):
        global_template = CustodyTemplate.objects.create(
            category=self.category,
            is_active=True,
            require_acceptance=True,
            name="KP Global Only",
            eula_text="Global only.",
            disclaimer="Sign.",
            qms_reference="QMS-GLOBAL-2",
        )
        self.template.is_active = False
        self.template.save(update_fields=["is_active"])

        receipt = self._receipt_for("KP-TMPL-3")

        self.assertEqual(receipt.custody_template_id, global_template.pk)
