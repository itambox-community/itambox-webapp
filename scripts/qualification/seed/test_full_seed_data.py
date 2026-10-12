"""Full demo-seed qualification (scripts/qualification/seed/).

Run explicitly:

    PYTHONPATH=itambox pytest scripts/qualification/seed/
"""

import io

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from assets.customfields import resolve_asset_custom_fields, resolve_asset_type_custom_fields
from assets.forms.asset_form import AssetForm
from assets.forms.assettype_form import AssetTypeForm
from assets.models import Asset, AssetType
from compliance.models import CustodyReceipt
from core.management.commands._seed.access import check_seed_access_invariants
from core.management.commands._seed.inventory import check_seed_inventory_invariants
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
)
from organization.models import AssetHolder, Membership
from subscriptions.models import SubscriptionAssignment

User = get_user_model()


class FullSeedDataQualificationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def test_full_seed_data_keeps_subscription_assignments_within_tenant(self):
        with override_settings(SEED_PASSWORD="configured-seed-password"):
            call_command("seed_data", force=True, stdout=self.stdout, stderr=self.stderr)
        assignments = list(SubscriptionAssignment._base_manager.select_related("subscription"))
        self.assertGreater(len(assignments), 0)
        for assignment in assignments:
            target = assignment._resolve_assigned_object_unscoped()
            self.assertIsNotNone(target)
            self.assertEqual(target.tenant_id, assignment.subscription.tenant_id)
        lars = User.objects.get(username="lars.eklund")
        self.assertTrue(lars.check_password("configured-seed-password"))
        self.assertFalse(lars.is_superuser)
        lars_membership = Membership._base_manager.select_related("tenant").get(user=lars, is_active=True)
        self.assertTrue(lars_membership.tenant.is_provider)
        self.assertTrue(lars.has_perm("extras.view_specificationlibrary", obj=lars_membership.tenant))
        self.assertTrue(lars.has_perm("extras.manage_specification_library", obj=lars_membership.tenant))
        self.assertTrue(self.client.login(username="lars.eklund", password="configured-seed-password"))
        self._activate_seeded_tenant(lars_membership.tenant_id)
        check_seed_access_invariants()
        seeded_people = User.objects.exclude(username="admin").exclude(username__startswith="admin@")
        self.assertGreater(seeded_people.count(), 0)
        for user in seeded_people:
            self.assertTrue(user.has_usable_password())
            memberships = Membership._base_manager.filter(user=user, is_active=True)
            holders = AssetHolder._base_manager.filter(user=user, deleted_at__isnull=True)
            self.assertEqual(memberships.count(), 1, user.username)
            self.assertEqual(holders.count(), 1, user.username)
            self.assertEqual(holders.get().tenant_id, memberships.get().tenant_id, user.username)
        check_seed_inventory_invariants()
        self.assertGreater(Component._base_manager.count(), 0)
        self.assertGreater(Accessory._base_manager.count(), 0)
        self.assertGreater(Consumable._base_manager.count(), 0)
        for component in Component._base_manager.all():
            total_stock = sum(ComponentStock._base_manager.filter(component=component).values_list("qty", flat=True))
            allocated = sum(
                ComponentAllocation._base_manager.filter(component=component, deleted_at__isnull=True).values_list(
                    "qty", flat=True
                )
            )
            self.assertGreaterEqual(total_stock, allocated, component.pk)
            self.assertEqual(component.available, total_stock - allocated)
        asset_type = AssetType._base_manager.get(slug="dell-latitude-5550")
        asset = Asset._base_manager.filter(asset_type=asset_type).first()
        self.assertIsNotNone(asset)
        asset_type_form = AssetTypeForm(instance=asset_type)
        asset_form = AssetForm(instance=asset)
        self.assertIn("cf_processor_model", asset_type_form.fields)
        self.assertIn("cf_memory_capacity", asset_type_form.fields)
        self.assertIn("cf_hostname", asset_form.fields)
        self.assertIn("cf_operating_system_family", asset_form.fields)
        self.assertTrue({item.definition.name for item in resolve_asset_type_custom_fields(asset_type)})
        self.assertTrue(
            {item.definition.name for item in resolve_asset_custom_fields(asset_type, asset.custom_field_data)}
        )
        self.assertTrue(
            set(asset.custom_field_data).issubset(
                {item.definition.name for item in resolve_asset_custom_fields(asset_type, asset.custom_field_data)}
            )
        )
        for item, assignment_model, stock_model, field in (
            (Accessory, AccessoryAssignment, AccessoryStock, "accessory"),
            (Consumable, ConsumableAssignment, ConsumableStock, "consumable"),
        ):
            for inventory_item in item._base_manager.all():
                total_stock = sum(
                    stock_model._base_manager.filter(**{field: inventory_item}).values_list("qty", flat=True)
                )
                assignments = assignment_model._base_manager.filter(
                    **{field: inventory_item, "deleted_at__isnull": True}
                )
                target_only = sum(assignments.filter(from_location__isnull=True).values_list("qty", flat=True))
                self.assertGreaterEqual(total_stock, target_only, inventory_item.pk)
                self.assertEqual(inventory_item.available, max(0, total_stock - target_only), inventory_item.pk)
        admin_accounts = User.objects.filter(username="admin") | User.objects.filter(username__startswith="admin@")
        self.assertGreater(admin_accounts.count(), 0)
        for user in admin_accounts:
            self.assertTrue(Membership._base_manager.filter(user=user, is_active=True).exists(), user.username)
            self.assertFalse(
                AssetHolder._base_manager.filter(user=user, deleted_at__isnull=True).exists(), user.username
            )
        allocation = ComponentAllocation._base_manager.filter(deleted_at__isnull=True).first()
        self.assertIsNotNone(allocation)
        ComponentStock._base_manager.filter(component_id=allocation.component_id).update(qty=0)
        with self.assertRaisesRegex(CommandError, "allocates"):
            check_seed_inventory_invariants()
        ComponentStock._base_manager.filter(component_id=allocation.component_id).update(qty=-1)
        with self.assertRaisesRegex(CommandError, "negative stock"):
            check_seed_inventory_invariants()

    def test_full_seed_data_keeps_custody_receipts_coherent_with_internal_detail_views(self):
        """The seeded custody-receipt story stays coherent with the internal workflow.

        Receipt primary keys are intentionally non-stable across destructive
        reseeds: the full seed TRUNCATEs compliance.CustodyReceipt, so this
        regression discovers the current seeded receipts and derives every URL
        from the current primary key instead of relying on a fixed receipt id.
        """
        with override_settings(SEED_PASSWORD="configured-seed-password"):
            call_command("seed_data", force=True, stdout=self.stdout, stderr=self.stderr)

        receipts = list(CustodyReceipt._base_manager.select_related("asset", "asset__tenant", "holder").order_by("pk"))
        self.assertGreater(len(receipts), 0, "the full seed must create a custody-receipt sample")
        accepted = [receipt for receipt in receipts if receipt.acceptance_status == CustodyReceipt.STATUS_ACCEPTED]
        pending = [receipt for receipt in receipts if receipt.acceptance_status == CustodyReceipt.STATUS_PENDING]
        self.assertGreater(len(accepted), 0, "the sample must contain accepted receipts")
        self.assertGreater(len(pending), 0, "the sample must contain pending receipts")

        for receipt in pending:
            self.assertFalse(receipt.accepted, receipt.pk)
            self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_PENDING, receipt.pk)
            self.assertIsNone(receipt.signed_at, receipt.pk)
            # A pending receipt must not carry acceptance metadata that would
            # imply a completed signature/acceptance event.
            self.assertIsNone(receipt.accepted_date, receipt.pk)
            self.assertIsNone(receipt.verification_hash, receipt.pk)
            self.assertFalse(receipt.signature_canvas, receipt.pk)
            self.assertFalse(receipt.signature_data, receipt.pk)
            self.assertFalse(receipt.signature_hash, receipt.pk)

        for receipt in accepted:
            self.assertTrue(receipt.accepted, receipt.pk)
            self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED, receipt.pk)
            self.assertIsNotNone(receipt.signed_at, receipt.pk)
            self.assertIsNotNone(receipt.accepted_date, receipt.pk)
            self.assertEqual(receipt.accepted_date, receipt.signed_at, receipt.pk)
            self.assertTrue(receipt.verification_hash, receipt.pk)
            self.assertRegex(receipt.verification_hash, r"^[0-9a-f]{64}$", receipt.pk)
            # Accepted demo receipts are modeled without a signature image: no
            # fake PNG payload and no placeholder data anywhere.
            self.assertEqual(receipt.signature_canvas, "", receipt.pk)
            self.assertNotIn("SIGNED_", receipt.signature_canvas, receipt.pk)
            self.assertNotIn("SIGNED_", receipt.signature_data, receipt.pk)

        # The authorized seeded operator reaches the custody surfaces through
        # the real seeded authorization model: MSP staff log in with the seeded
        # password and carry a technician grant scoped to all managed tenants.
        self.assertTrue(self.client.login(username="lars.eklund", password="configured-seed-password"))

        # Every seeded receipt renders in the internal detail surface; accepted
        # receipts render their explicit no-signature-image state.
        for receipt in receipts:
            self._activate_seeded_tenant(receipt.asset.tenant_id)
            response = self.client.get(receipt.get_absolute_url())
            self.assertEqual(response.status_code, 200, receipt.pk)
            self.assertNotContains(response, "SIGNED_")
            if receipt.acceptance_status == CustodyReceipt.STATUS_ACCEPTED:
                self.assertContains(response, "No renderable signature image is stored.")

        # Discover every asset whose detail surface exposes a custody-receipt
        # link, mirroring AssetDetailView.get_context_data: the link renders for
        # an active holder assignment whose holder owns a receipt on the asset,
        # and the newest receipt (created_date, pk) is the current one.
        link_targets = {}
        for receipt in receipts:
            asset = receipt.asset
            if asset.pk in link_targets:
                continue
            active_assignment = asset.active_assignment
            if active_assignment is None or not isinstance(active_assignment.assigned_target, AssetHolder):
                continue
            current_receipt = (
                CustodyReceipt._base_manager.filter(asset=asset, holder=active_assignment.assigned_target)
                .order_by("-created_date", "-pk")
                .first()
            )
            if current_receipt is not None:
                link_targets[asset.pk] = (asset, current_receipt)
        self.assertGreater(len(link_targets), 0, "seeded assets must expose custody-receipt detail links")

        for asset, current_receipt in link_targets.values():
            self._activate_seeded_tenant(asset.tenant_id)
            response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": asset.pk}))
            self.assertEqual(response.status_code, 200, asset.asset_tag)
            summary = response.context.get("custody_receipt_summary") if response.context else None
            self.assertIsNotNone(summary, f"{asset.asset_tag} must render its custody-receipt link")
            detail_url = summary["detail_url"]
            # The URL is derived from the current receipt object and its current
            # primary key, never a fixed id.
            self.assertEqual(detail_url, current_receipt.get_absolute_url())
            self.assertContains(response, detail_url)
            resolved = self.client.get(detail_url)
            self.assertEqual(resolved.status_code, 200, f"{asset.asset_tag}: {detail_url}")

    def _activate_seeded_tenant(self, tenant_id):
        session = self.client.session
        session["active_tenant_id"] = tenant_id
        session.save()
