import datetime
import json
import re
from decimal import Decimal
from html.parser import HTMLParser

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext, ngettext
from model_bakery import baker

from assets.forms.asset_form import AssetForm
from assets.models import (
    Asset,
    AssetAssignment,
    AssetMaintenance,
    AssetRequest,
    AssetRole,
    AssetType,
    AssetTypeFieldset,
    Category,
    Depreciation,
    Manufacturer,
    StatusLabel,
    Supplier,
)
from assets.services import checkout_asset
from compliance.models import CustodyReceipt
from compliance.tests.signatures import signature_png
from core.managers import set_current_tenant
from core.models import ObjectChange
from core.serialization import serialize_object
from core.tests.mixins import TenantTestMixin, grant
from extras.models import CustomField, CustomFieldset, CustomFieldsetField
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    Component,
    ComponentAllocation,
    Consumable,
    Kit,
    KitItem,
)
from inventory.models_assignment_write import authorized_assignment_write
from inventory.services import checkin_component, purge_inventory_assignment
from inventory.tests.factories import create_assignment_fixture
from organization.models import Contact, ContactAssignment, ContactRole, Location, Role, Site, Tenant

User = get_user_model()

EXISTING_TEST_SIGNATURE = signature_png("drawingdata123")


class _SeededStatusLabelsMixin:
    """Restore the seeded StatusLabel defaults for this class.

    A TransactionTestCase (here or in an earlier, possibly crashed run against
    the reused test DB) flushes every table, and flush's post_migrate does NOT
    re-run data migrations — so the seeded defaults can be missing. The
    migration that originally carried them was removed with the migration
    normalization, so each class re-seeds from the seed command's canonical
    definitions inside its own class-level transaction (idempotent
    get_or_create; rolls back with the class, so it never pollutes anything
    else).
    """

    @classmethod
    def setUpTestData(cls):
        ComponentTrackingTestCase._restore_seeded_status_labels()
        super().setUpTestData()


class ComponentTrackingTestCase(TransactionTestCase):
    # TransactionTestCase teardown TRUNCATEs every table (flush), which — unlike
    # TestCase's savepoint rollback — permanently destroys the seeded StatusLabel
    # defaults; flush's post_migrate does NOT re-run data migrations.
    # serialized_rollback restores a DB snapshot instead, but in full-suite runs
    # the deserialize collides with post_migrate-recreated rows (duplicate
    # content types/permissions), so we deterministically re-seed from the seed
    # command's idempotent definitions: before each test here (a prior flush may
    # have emptied the table) and once after the class for every later module in
    # the session. Unrelated to the RBAC collapse; found while diagnosing
    # cascading setUp failures.

    @staticmethod
    def _restore_seeded_status_labels():
        from assets.models import StatusLabel
        from core.management.commands.seed_data import Command

        for name, slug, stype, color in Command._status_label_defs():
            StatusLabel.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "type": stype, "color": color},
            )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._restore_seeded_status_labels()

    def setUp(self):
        self._restore_seeded_status_labels()
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

        self.manufacturer = Manufacturer.objects.create(name="Dell", slug="dell")
        self.category = Category.objects.create(name="Memory", slug="memory", applies_to={"component": True})

        from organization.models import Location, Site

        # NOTE: deliberately no tenant on this shared fixture — the global
        # "tenant field required" form monkey-patch (core/apps.py) activates
        # for every form in this TransactionTestCase the moment ANY Tenant
        # row exists in the DB. Tests that need stock (which, per ADR-0001
        # phase 4, requires a tenant-owned location) create their own
        # tenant-scoped location locally instead of mutating this shared one.
        self.site = Site.objects.create(name="HQ", slug="hq")
        self.location = Location.objects.create(name="Warehouse", slug="warehouse", site=self.site)
        self.acc_category = Category.objects.create(name="Keyboard", slug="keyboard", applies_to={"accessory": True})
        self.con_category = Category.objects.create(
            name="Thermal Paste", slug="thermal-paste", applies_to={"consumable": True}
        )

        self.component = Component.objects.create(
            manufacturer=self.manufacturer,
            name="16GB DDR5 RAM",
            slug="dell-16gb-ddr5-ram",
            category=self.category,
            specs={"capacity_gb": 16, "type": "DDR5", "speed_mhz": 4800},
            part_number="RAM-16G-D5",
            allow_overallocate=True,
        )

        self.role = AssetRole.objects.create(name="Server", slug="server")
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="PowerEdge R750", slug="dell-poweredge-r750"
        )

        self.status = StatusLabel.objects.get_or_create(
            slug="available", defaults={"name": "Available", "type": StatusLabel.TYPE_DEPLOYABLE, "color": "28a745"}
        )[0]

        self.asset = Asset.objects.create(
            name="Web Server 01",
            asset_tag="SRV-001",
            asset_type=self.asset_type,
            asset_role=self.role,
            status=self.status,
        )

        self.allocation = create_assignment_fixture(
            ComponentAllocation,
            component=self.component,
            assigned_asset=self.asset,
            qty=2,
            notes="Initial RAM allocation",
        )

    def tearDown(self):
        set_current_tenant(None)
        super().tearDown()

    def _activate_component_tenant(self):
        from organization.models import Tenant

        tenant = Tenant.objects.create(name="Component Mutation Co", slug="component-mutation-co")
        self.component.tenant = tenant
        self.component.save(update_fields=["tenant"])
        self.asset.tenant = tenant
        self.asset.save(update_fields=["tenant"])
        with authorized_assignment_write(self.allocation):
            self.allocation.save()
        set_current_tenant(tenant)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()
        return tenant

    def test_component_detail_view(self):
        # We replace any reference to specific request views that might be affected
        response = self.client.get(reverse("inventory:component_detail", kwargs={"pk": self.component.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "16GB DDR5 RAM")

    def test_component_list_view(self):
        response = self.client.get(reverse("components:component_list"))
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.url, reverse("inventory:inventory_list") + "?type=components")

        response = self.client.get(response.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("inventory:component_list"))

        response = self.client.get(response.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "16GB DDR5 RAM")

    def test_component_create_view(self):
        response = self.client.get(reverse("inventory:component_create"))
        self.assertEqual(response.status_code, 200)

        post_data = {
            "manufacturer": self.manufacturer.pk,
            "name": "2TB NVMe SSD",
            "slug": "dell-2tb-nvme-ssd",
            "category": self.category.pk,
            "part_number": "SSD-2TB-NVME",
            "min_qty": 0,
            "specs": "{}",
            "notes": "Samsung SSD for server storage",
            "tags": [],
        }
        response = self.client.post(reverse("inventory:component_create"), data=post_data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Component.objects.filter(name="2TB NVMe SSD").exists())

    def test_component_update_view(self):
        response = self.client.get(reverse("inventory:component_update", kwargs={"pk": self.component.pk}))
        self.assertEqual(response.status_code, 200)

        post_data = {
            "manufacturer": self.manufacturer.pk,
            "name": "16GB DDR5 RAM (Updated)",
            "slug": "dell-16gb-ddr5-ram",
            "category": self.category.pk,
            "part_number": "RAM-16G-D5-UPDATED",
            "min_qty": 0,
            "specs": "{}",
            "notes": "Updated spec RAM",
            "tags": [],
        }
        response = self.client.post(
            reverse("inventory:component_update", kwargs={"pk": self.component.pk}), data=post_data
        )
        self.assertEqual(response.status_code, 302)
        self.component.refresh_from_db()
        self.assertEqual(self.component.name, "16GB DDR5 RAM (Updated)")

    def test_component_delete_view(self):
        self._activate_component_tenant()
        checkin_component(self.allocation.pk, user=self.user)
        allocation = ComponentAllocation._base_manager.get(pk=self.allocation.pk)
        purge_inventory_assignment(allocation)
        response = self.client.post(reverse("inventory:component_delete", kwargs={"pk": self.component.pk}))
        self.assertEqual(response.status_code, 302)
        deleted_component = Component._base_manager.get(pk=self.component.pk)
        self.assertIsNotNone(deleted_component.deleted_at)

    def test_componentallocation_list_view(self):
        response = self.client.get(reverse("inventory:componentallocation_list"))
        self.assertEqual(response.status_code, 200)

    def test_componentallocation_create_view(self):
        from organization.models import Tenant

        tenant = Tenant.objects.create(name="Component Allocation Co", slug="component-allocation-co")
        self.component.tenant = tenant
        self.component.save(update_fields=["tenant"])
        self.asset.tenant = tenant
        self.asset.save(update_fields=["tenant"])
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

        response = self.client.get(reverse("inventory:componentallocation_create"))
        self.assertEqual(response.status_code, 200)

        post_data = {
            "component": self.component.pk,
            "assigned_asset": self.asset.pk,
            "qty": 1,
            "notes": "Secondary RAM stick",
        }
        response = self.client.post(reverse("inventory:componentallocation_create"), data=post_data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ComponentAllocation.objects.filter(qty=1, notes="Secondary RAM stick").exists())

    def test_componentallocation_update_view(self):
        self._activate_component_tenant()
        response = self.client.get(reverse("inventory:componentallocation_update", kwargs={"pk": self.allocation.pk}))
        self.assertEqual(response.status_code, 200)

        post_data = {
            "component": self.component.pk,
            "assigned_asset": self.asset.pk,
            "qty": 3,
            "notes": "Updated RAM allocation",
        }
        response = self.client.post(
            reverse("inventory:componentallocation_update", kwargs={"pk": self.allocation.pk}), data=post_data
        )
        self.assertEqual(response.status_code, 302)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.qty, 3)
        self.assertEqual(self.allocation.notes, "Updated RAM allocation")

    def test_componentallocation_delete_view(self):
        self._activate_component_tenant()
        response = self.client.post(reverse("inventory:componentallocation_delete", kwargs={"pk": self.allocation.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ComponentAllocation.objects.filter(pk=self.allocation.pk).exists())

    def test_checkout_checkin_button_visibility(self):
        from assets.services import checkout_asset
        from organization.models import AssetHolder

        # 1. When the asset is available (not checked out)
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        # Check Out... button is visible, but Check In button is NOT visible
        self.assertContains(response, "Check Out...")
        self.assertNotContains(response, "Check In")

        # 2. Check out the asset to a holder
        holder = AssetHolder.objects.create(first_name="John", last_name="Doe", upn="john@example.com")
        checkout_asset(self.asset, holder=holder, user=self.user)

        # 3. Reload detail page: asset is now checked out
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        # Check In button is visible, but Check Out... button is NOT visible
        self.assertContains(response, "Check In")
        self.assertNotContains(response, "Check Out...")

    def test_asset_detail_view_components_integration(self):
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Installed Physical Modules / Components")

        self.assertIn("components_table", response.context)
        comp_table = response.context["components_table"]
        self.assertEqual(len(comp_table.rows), 1)

    def test_asset_detail_shows_allocated_components_in_specs_card(self):
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned System Hardware Specifications")
        self.assertContains(response, "Active Hardware Modifications &amp; Upgrades")
        self.assertContains(response, "16GB DDR5 RAM")
        self.assertContains(response, "Qty: <strong>2</strong>")

    def test_asset_detail_without_composition_still_shows_specs_card(self):
        self.assertFalse(hasattr(self.asset_type, "custom_fieldset"))
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assigned System Hardware Specifications")

        self._activate_component_tenant()
        checkin_component(self.allocation.pk, user=self.user)
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Assigned System Hardware Specifications")

    def test_accessory_crud_and_checkout_views(self):
        from inventory.models import AccessoryStock
        from organization.models import Tenant

        # ADR-0001 phase 4: stock requires a location owned by a tenant. Use a
        # tenant-scoped location local to this test (not the shared
        # self.location) — the moment a Tenant row exists, the global "tenant
        # field required" form monkey-patch (core/apps.py) kicks in for every
        # form in this TransactionTestCase, so it must not leak into other
        # test methods via setUp.
        tenant = Tenant.objects.create(name="Accessory Crud Co", slug="accessory-crud-co")
        self.location.tenant = tenant
        self.location.save(update_fields=["tenant"])
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

        # Create Accessory
        acc = Accessory.objects.create(
            manufacturer=self.manufacturer,
            name="Wired Keyboard KB216",
            slug="dell-wired-keyboard-kb216",
            category=self.acc_category,
            min_qty=2,
            allow_overallocate=False,
        )
        AccessoryStock.objects.create(accessory=acc, location=self.location, qty=10)

        # 1. List View
        response = self.client.get(reverse("inventory:accessory_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wired Keyboard KB216")

        # 2. Detail View
        response = self.client.get(reverse("inventory:accessory_detail", kwargs={"pk": acc.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wired Keyboard KB216")

        # 3. Create View
        post_data = {
            "manufacturer": self.manufacturer.pk,
            "name": "Wireless Mouse WM126",
            "slug": "dell-wireless-mouse-wm126",
            "category": self.acc_category.pk,
            "part_number": "MS-WM126",
            "min_qty": 3,
            "allow_overallocate": True,
            "notes": "Office standard mouse",
            # A Tenant now exists in the DB (created above for the stock
            # location), which flips AccessoryForm's 'tenant' field to
            # required (core/apps.py's global "tenant required" monkey-patch).
            "tenant": tenant.pk,
        }
        response = self.client.post(reverse("inventory:accessory_create"), data=post_data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Accessory.objects.filter(name="Wireless Mouse WM126").exists())

        # 4. Strict Checkout Limit Validation (qty=11 > remaining=10, overallocate=False)
        from organization.models import AssetHolder

        holder = AssetHolder.objects.create(first_name="John", last_name="Doe", email="john@example.com", tenant=tenant)

        checkout_data = {
            "from_location": self.location.pk,
            "assigned_holder": holder.pk,
            "assigned_location": "",
            "qty": 11,
            "notes": "Over-allocate attempt",
        }
        response = self.client.post(reverse("inventory:accessory_checkout", kwargs={"pk": acc.pk}), data=checkout_data)
        self.assertEqual(AccessoryAssignment.objects.filter(accessory=acc).count(), 0)

        # 5. Successful Checkout (qty=5 <= 10)
        checkout_data["qty"] = 5
        response = self.client.post(
            reverse("inventory:accessory_checkout", kwargs={"pk": acc.pk}), data=checkout_data, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 204)  # 204 No Content for success HTMX modal
        self.assertEqual(AccessoryAssignment.objects.filter(accessory=acc).count(), 1)
        self.assertEqual(acc.available, 5)

        # 6. Check In (Checkin Assignment deletes it)
        assignment = AccessoryAssignment.objects.get(accessory=acc)
        response = self.client.post(reverse("inventory:accessory_checkin", kwargs={"pk": assignment.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AccessoryAssignment.objects.filter(accessory=acc).count(), 0)
        self.assertEqual(acc.available, 10)

    def test_asset_audit_view(self):
        # The audit service now enforces the operation's exact tenant permission;
        # make this integration fixture an explicit tenant actor instead of
        # relying on the historical superuser/global shortcut.
        tenant = Tenant.objects.create(name="Audit view tenant", slug="audit-view-tenant")
        role = Role.objects.create(
            tenant=tenant,
            name="Audit scanner",
            permissions=["compliance.add_assetaudit"],
        )
        grant(self.user, tenant, role)
        self.location.tenant = tenant
        self.location.save(update_fields=["tenant"])
        self.asset.tenant = tenant
        self.asset.save(update_fields=["tenant"])
        set_current_tenant(tenant)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

        # GET renders the modal form.
        response = self.client.get(reverse("assets:asset_audit", kwargs={"pk": self.asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "asset-audit-modal")

        # POST without required fields returns form error (422).
        response = self.client.post(
            reverse("assets:asset_audit", kwargs={"pk": self.asset.pk}),
            data={},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 422)
        self.asset.refresh_from_db()
        self.assertIsNone(self.asset.last_audited)

        # POST with location + status succeeds and records the audit.
        response = self.client.post(
            reverse("assets:asset_audit", kwargs={"pk": self.asset.pk}),
            data={"location": self.location.pk, "status": self.asset.status.pk},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)

        self.asset.refresh_from_db()
        self.assertIsNotNone(self.asset.last_audited)
        self.assertEqual(self.asset.last_audited_by, self.user)

    def test_asset_label_print_view(self):
        url = reverse("label_print", kwargs={"app_label": "assets", "model_name": "asset"})
        page = self.client.get(url, {"pk": self.asset.pk})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, str(self.asset))
        response = self.client.post(url, {"pk": [self.asset.pk], "_print": "1", "template": "", "layout": "roll"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_custody_receipt_signoff(self):
        from organization.models import AssetHolder

        # Link the holder to the logged-in user so the view's holder-identity
        # check passes without weakening the security control.
        holder = AssetHolder.objects.create(
            first_name="Alice",
            last_name="Wonder",
            upn="alice@example.com",
            user=self.user,
        )

        deployed = StatusLabel.objects.get(slug="in-use")
        checkout_asset(self.asset, holder=holder, user=self.user, status=deployed)
        assignment = AssetAssignment.objects.get(asset=self.asset, is_active=True)

        receipt = CustodyReceipt.objects.create(
            asset=self.asset,
            holder=holder,
            assignment=assignment,
        )
        token = receipt.token

        sign_url = reverse("compliance:custody_eula_sign", kwargs={"token": token})
        response = self.client.get(sign_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "custody-sign-form")

        invalid_url = reverse("compliance:custody_eula_sign", kwargs={"token": "invalid-token-value"})
        response = self.client.get(invalid_url)
        self.assertEqual(response.status_code, 404)

        post_data = {
            "signature_canvas": EXISTING_TEST_SIGNATURE,
            "action": "accept",
        }
        response = self.client.post(sign_url, data=post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secured")

        receipt.refresh_from_db()
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertIsNotNone(receipt.accepted_date)
        self.assertEqual(receipt.acceptance_method, "digital")
        self.assertEqual(receipt.signature_canvas, EXISTING_TEST_SIGNATURE)
        self.assertEqual(len(receipt.verification_hash), 64)

    def test_custody_receipt_decline(self):
        from organization.models import AssetHolder

        # Link the holder to the logged-in user so the view's holder-identity
        # check passes without weakening the security control.
        holder = AssetHolder.objects.create(
            first_name="Bob",
            last_name="Builder",
            upn="bob@example.com",
            user=self.user,
        )

        deployed = StatusLabel.objects.get(slug="in-use")
        checkout_asset(self.asset, holder=holder, user=self.user, status=deployed)
        assignment = AssetAssignment.objects.get(asset=self.asset, is_active=True)

        receipt = CustodyReceipt.objects.create(
            asset=self.asset,
            holder=holder,
            assignment=assignment,
        )

        sign_url = reverse("compliance:custody_eula_sign", kwargs={"token": receipt.token})
        post_data = {
            "action": "decline",
        }
        response = self.client.post(sign_url, data=post_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "declined")

        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_DECLINED)
        self.assertFalse(receipt.accepted)


class AssetProcurementTestCase(_SeededStatusLabelsMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

        self.manufacturer = Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        self.role = AssetRole.objects.create(name="Laptop", slug="laptop")
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="ThinkPad T14", slug="lenovo-thinkpad-t14"
        )

    def test_asset_procurement_fields_save_and_display(self):
        from assets.models import Supplier

        supplier = Supplier.objects.create(name="Lenovo Germany GmbH", slug="lenovo-germany-gmbh")
        # Create asset with procurement details
        asset = Asset.objects.create(
            name="Developer ThinkPad",
            asset_tag="LAP-001",
            asset_type=self.asset_type,
            asset_role=self.role,
            purchase_cost=Decimal("1249.99"),
            order_number="PO-998877",
            supplier=supplier,
        )

        # Verify saved correctly in DB
        asset.refresh_from_db()
        self.assertEqual(asset.purchase_cost, Decimal("1249.99"))
        self.assertEqual(asset.order_number, "PO-998877")
        self.assertEqual(asset.supplier.name, "Lenovo Germany GmbH")

        # Verify detail page displays them
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1,249.99")
        self.assertContains(response, "PO-998877")
        self.assertContains(response, "Lenovo Germany GmbH")

    def test_asset_form_procurement_fields(self):
        from assets.models import Supplier

        supplier = Supplier.objects.create(name="Bechtle AG", slug="bechtle-ag")
        # Test creating new asset via POST
        post_data = {
            "name": "Sales ThinkPad",
            "asset_tag": "LAP-002",
            "asset_type": self.asset_type.pk,
            "asset_role": self.role.pk,
            "status": "available",  # legacy free-text value, exercises the form's tolerance
            "purchase_cost": "999.50",
            "order_number": "PO-112233",
            "supplier": supplier.pk,
            "notes": "Sales laptop standard spec",
            "tags": [],
        }
        response = self.client.post(reverse("assets:asset_create"), data=post_data)
        self.assertEqual(response.status_code, 302)  # Redirects on success

        # Verify created asset
        new_asset = Asset.objects.get(asset_tag="LAP-002")
        self.assertEqual(new_asset.name, "Sales ThinkPad")
        self.assertEqual(new_asset.purchase_cost, Decimal("999.50"))
        self.assertEqual(new_asset.order_number, "PO-112233")
        self.assertEqual(new_asset.supplier.name, "Bechtle AG")

        # Test fields are optional
        post_data_optional = {
            "name": "Minimal ThinkPad",
            "asset_tag": "LAP-003",
            "asset_type": self.asset_type.pk,
            "asset_role": self.role.pk,
            "status": "available",  # legacy free-text value, exercises the form's tolerance
            "purchase_cost": "",
            "order_number": "",
            "supplier": "",
            "notes": "",
            "tags": [],
        }
        response = self.client.post(reverse("assets:asset_create"), data=post_data_optional)
        self.assertEqual(response.status_code, 302)

        minimal_asset = Asset.objects.get(asset_tag="LAP-003")
        self.assertIsNone(minimal_asset.purchase_cost)
        self.assertEqual(minimal_asset.order_number, "")
        self.assertEqual(minimal_asset.supplier, None)


class StatusLabelTestCase(_SeededStatusLabelsMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

    def test_status_label_defaults_exist(self):
        # Default labels created by migration should exist
        self.assertTrue(StatusLabel.objects.filter(slug="available").exists())
        self.assertTrue(StatusLabel.objects.filter(slug="in-use").exists())
        self.assertTrue(StatusLabel.objects.filter(slug="pending-repair").exists())
        self.assertTrue(StatusLabel.objects.filter(slug="retired").exists())

    def test_status_label_crud_views(self):
        # 1. List View
        response = self.client.get(reverse("assets:statuslabel_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Available")
        self.assertContains(response, "In Use")

        # 2. Detail View
        label = StatusLabel.objects.get(slug="available")
        response = self.client.get(reverse("assets:statuslabel_detail", kwargs={"pk": label.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Available")

        # 3. Create View
        post_data = {
            "name": "Archived (Awaiting Disposal)",
            "slug": "archived-awaiting-disposal",
            "type": StatusLabel.TYPE_ARCHIVED,
            "description": "Out of service assets waiting for disposal",
            "color": "333333",
        }
        response = self.client.post(reverse("assets:statuslabel_create"), data=post_data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(StatusLabel.objects.filter(slug="archived-awaiting-disposal").exists())

        # 4. Update View
        new_label = StatusLabel.objects.get(slug="archived-awaiting-disposal")
        update_data = {
            "name": "Archived (Awaiting Disposal) Updated",
            "slug": "archived-awaiting-disposal",
            "type": StatusLabel.TYPE_ARCHIVED,
            "description": "Updated description",
            "color": "444444",
        }
        response = self.client.post(reverse("assets:statuslabel_update", kwargs={"pk": new_label.pk}), data=update_data)
        self.assertEqual(response.status_code, 302)
        new_label.refresh_from_db()
        self.assertEqual(new_label.name, "Archived (Awaiting Disposal) Updated")
        self.assertEqual(new_label.color, "444444")

        # 5. Delete View
        response = self.client.post(reverse("assets:statuslabel_delete", kwargs={"pk": new_label.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(StatusLabel.objects.filter(slug="archived-awaiting-disposal").exists())


class AssetMaintenanceAndLifecycleTestCase(_SeededStatusLabelsMixin, TestCase):
    def setUp(self):
        # Create user
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

        # Create manufacturer and role
        self.manufacturer = Manufacturer.objects.create(name="Lenovo", slug="lenovo")
        self.role = AssetRole.objects.create(name="Laptop", slug="laptop")
        self.status = StatusLabel.objects.get(slug="available")

        # Create asset type with 24 months EOL
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="ThinkPad T14", slug="lenovo-thinkpad-t14", eol_months=24
        )

        # Create asset type with no EOL
        self.asset_type_no_eol = AssetType.objects.create(
            manufacturer=self.manufacturer, model="ThinkPad T15", slug="lenovo-thinkpad-t15"
        )

    def test_eol_date_calculations(self):
        import datetime

        # 1. Standard calculation: Purchase today + 24 months = today + 2 years
        today = datetime.date.today()
        asset = Asset.objects.create(
            name="Developer ThinkPad",
            asset_tag="LAP-101",
            asset_type=self.asset_type,
            asset_role=self.role,
            purchase_date=today,
            status=self.status,
        )
        expected_year = today.year + 2
        try:
            expected_eol = datetime.date(expected_year, today.month, today.day)
        except ValueError:
            expected_eol = datetime.date(expected_year, today.month + 1, 1) - datetime.timedelta(days=1)

        self.assertEqual(asset.eol_date, expected_eol)
        self.assertIn("2 year", asset.time_to_eol)

        # 2. Month-end overflow calculation: Purchase Aug 31 2025 + 6 months -> Feb 31 -> Feb 28 2026 (non-leap)
        self.asset_type.eol_months = 6
        self.asset_type.save()
        asset.purchase_date = datetime.date(2025, 8, 31)
        asset.save()
        self.assertEqual(asset.eol_date, datetime.date(2026, 2, 28))

        # 3. Leap year overflow: Purchase Aug 31 2023 + 6 months -> Feb 29 2024 (leap year)
        asset.purchase_date = datetime.date(2023, 8, 31)
        asset.save()
        self.assertEqual(asset.eol_date, datetime.date(2024, 2, 29))

        # 4. No EOL months defined
        asset_no_eol = Asset.objects.create(
            name="Developer ThinkPad No EOL",
            asset_tag="LAP-102",
            asset_type=self.asset_type_no_eol,
            asset_role=self.role,
            purchase_date=datetime.date(2025, 1, 15),
            status=self.status,
        )
        self.assertIsNone(asset_no_eol.eol_date)
        self.assertEqual(asset_no_eol.time_to_eol, "Not set")

    def test_total_cost_of_ownership_aggregation(self):
        import datetime

        asset = Asset.objects.create(
            name="Developer ThinkPad",
            asset_tag="LAP-201",
            asset_type=self.asset_type,
            asset_role=self.role,
            purchase_cost=Decimal("1200.00"),
            purchase_date=datetime.date(2025, 1, 15),
            status=self.status,
        )

        # Initial TCO should be purchase cost
        self.assertEqual(asset.total_cost_of_ownership, Decimal("1200.00"))

        supplier = Supplier.objects.create(name="Lenovo Support", slug="lenovo-support")

        # Record maintenance 1 costing $150.00
        AssetMaintenance.objects.create(
            asset=asset,
            status="completed",
            supplier=supplier,
            maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            cost=Decimal("150.00"),
            start_date=datetime.date(2025, 3, 1),
            completion_date=datetime.date(2025, 3, 5),
            notes="Screen replacement",
        )

        # Record maintenance 2 costing $50.00
        AssetMaintenance.objects.create(
            asset=asset,
            status="completed",
            supplier=supplier,
            maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_UPGRADE,
            cost=Decimal("50.00"),
            start_date=datetime.date(2025, 4, 1),
            completion_date=datetime.date(2025, 4, 2),
            notes="RAM upgrade",
        )

        # Recalculate TCO: 1200 + 150 + 50 = 1400.00
        self.assertEqual(asset.total_cost_of_ownership, Decimal("1400.00"))

    def test_asset_maintenance_crud_views(self):
        import datetime

        from assets.models import AssetMaintenance

        asset = Asset.objects.create(
            name="Developer ThinkPad",
            asset_tag="LAP-301",
            asset_type=self.asset_type,
            asset_role=self.role,
            purchase_cost=Decimal("1200.00"),
            purchase_date=datetime.date(2025, 1, 15),
            status=self.status,
        )

        supplier = Supplier.objects.create(name="Local Repair Shop", slug="local-repair-shop")
        supplier_premium = Supplier.objects.create(
            name="Local Repair Shop (Premium Center)", slug="local-repair-shop-premium-center"
        )

        # 1. Create maintenance via View POST
        post_data = {
            "asset": asset.pk,
            "title": "Fixed motherboard logic board issue",
            "status": "completed",
            "supplier": supplier.pk,
            "maintenance_type": AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            "cost": "250.00",
            "start_date": "2025-05-10",
            "completion_date": "2025-05-15",
            "notes": "Fixed motherboard logic board issue",
        }

        response = self.client.post(reverse("assets:assetmaintenance_create"), data=post_data)
        self.assertEqual(response.status_code, 302)

        # Verify created
        maint = AssetMaintenance.objects.get(supplier=supplier)
        self.assertEqual(maint.cost, Decimal("250.00"))
        self.assertEqual(maint.downtime_days, 5)

        # 2. List View
        response = self.client.get(reverse("assets:assetmaintenance_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Local Repair Shop")
        self.assertContains(response, "Repair")
        self.assertContains(response, "$250.00")

        # 3. Detail View
        response = self.client.get(reverse("assets:assetmaintenance_detail", kwargs={"pk": maint.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Local Repair Shop")
        self.assertContains(response, "Fixed motherboard logic board issue")
        self.assertContains(response, "5 Days")

        # 4. Update View
        update_data = {
            "asset": asset.pk,
            "title": "Fixed motherboard logic board issue and cleaned thermal paste",
            "status": "completed",
            "supplier": supplier_premium.pk,
            "maintenance_type": AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            "cost": "280.00",
            "start_date": "2025-05-10",
            "completion_date": "2025-05-16",
            "notes": "Fixed motherboard logic board issue and cleaned thermal paste",
        }
        response = self.client.post(
            reverse("assets:assetmaintenance_update", kwargs={"pk": maint.pk}), data=update_data
        )
        self.assertEqual(response.status_code, 302)
        maint.refresh_from_db()
        self.assertEqual(maint.supplier, supplier_premium)
        self.assertEqual(maint.cost, Decimal("280.00"))
        self.assertEqual(maint.downtime_days, 6)

        # 5. Delete View
        response = self.client.post(reverse("assets:assetmaintenance_delete", kwargs={"pk": maint.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AssetMaintenance.objects.filter(pk=maint.pk).exists())

    def test_manufacturer_support_contacts(self):
        # 1. Create a Manufacturer via the CRUD views
        post_data = {
            "name": "Dell Technologies",
            "slug": "dell-technologies",
            "description": "Premium enterprise servers and hardware supplier",
        }

        response = self.client.post(reverse("assets:manufacturer_create"), data=post_data)
        self.assertEqual(response.status_code, 302)

        # Verify created in DB
        dell = Manufacturer.objects.get(slug="dell-technologies")

        # Create dynamic support contact and role and assignment
        support_role, _ = ContactRole.objects.get_or_create(name="Technical Support")
        contact = Contact.objects.create(
            name="Dell Enterprise Support",
            phone="+1 (800) 456-3355",
            email="enterprise_support@dell.com",
            web_url="https://support.dell.com",
        )
        ContactAssignment.objects.create(
            contact=contact,
            role=support_role,
            content_type=ContentType.objects.get_for_model(dell),
            object_id=dell.pk,
            priority="primary",
        )

        # 2. View details in ManufacturerDetailView
        response = self.client.get(reverse("assets:manufacturer_detail", kwargs={"pk": dell.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dell-technologies")
        self.assertContains(response, "+1 (800) 456-3355")
        self.assertContains(response, "enterprise_support@dell.com")
        self.assertContains(response, "https://support.dell.com")

        # 3. Create an asset under this manufacturer and view AssetDetailView
        optiplex_type = AssetType.objects.create(manufacturer=dell, model="OptiPlex 7090", slug="dell-optiplex-7090")

        asset = Asset.objects.create(
            name="Reception Desk Desktop",
            asset_tag="TAG-DELL-99",
            asset_type=optiplex_type,
            serial_number="DELL-SN-12345",
            status=self.status,
        )

        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": asset.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Support &amp; Warranty Details")
        self.assertContains(response, "enterprise_support@dell.com")
        self.assertContains(response, "DELL-SN-12345")
        self.assertContains(response, "Dell Technologies")


class EnterpriseITAMTestCase(_SeededStatusLabelsMixin, TestCase):
    def setUp(self):
        # Create superuser to bypass permission checks in CBVs
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

        # Retrieve default status labels populated by migrations
        self.available_status = StatusLabel.objects.get(slug="available")
        self.in_use_status = StatusLabel.objects.get(slug="in-use")

        # Create basic manufacturer and roles
        self.manufacturer = Manufacturer.objects.create(name="Apple", slug="apple")
        self.role = AssetRole.objects.create(name="Mobile Phone", slug="mobile-phone")

    def test_dynamic_custom_fieldsets_and_form_saving(self):
        tenant = Tenant.objects.create(name="Enterprise form tenant", slug="enterprise-form-tenant")
        editor = User.objects.create_user(username="enterprise-asset-editor", password="testpassword")
        role = Role.objects.create(
            tenant=tenant,
            name="Enterprise asset editor",
            permissions=["assets.add_asset", "assets.change_asset", "assets.view_asset"],
        )
        grant(editor, tenant, role)
        self.client.force_login(editor)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

        # 1. Create custom fields
        sim_field = CustomField.objects.create(
            name="sim_number",
            label="SIM Number",
            field_type=CustomField.FIELD_TYPE_TEXT,
            activation=CustomField.ACTIVATION_COMPOSED,
            required=True,
        )
        screen_field = CustomField.objects.create(
            name="screen_size",
            label="Screen Size",
            field_type=CustomField.FIELD_TYPE_DECIMAL,
            activation=CustomField.ACTIVATION_COMPOSED,
            decimal_scale=1,
            required=False,
        )

        # object_types must be set so CF applies to Asset
        asset_ct = ContentType.objects.get_for_model(Asset)
        sim_field.object_types.add(asset_ct)
        screen_field.object_types.add(asset_ct)

        # 2. Create fieldset and link fields
        fieldset = CustomFieldset.objects.create(
            namespace="local",
            slug="phone-specs",
            label="Phone Specs",
        )
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=sim_field, position=10)
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=screen_field, position=20)

        # 3. Create asset type with fieldset
        asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="iPhone 15",
            slug="apple-iphone-15",
        )
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=10)

        # 4. Bind and save AssetForm with custom fields
        form_data = {
            "name": "CEO Phone",
            "asset_tag": "PHN-001",
            "asset_type": asset_type.pk,
            "asset_role": self.role.pk,
            "status": self.available_status.pk,
            "tenant": tenant.pk,
            "cf_sim_number": "8904903200001234567",
            "cf_screen_size": "6.1",
            "notes": "Dynamic specs test",
            "tags": [],
        }

        # POST via view
        response = self.client.post(reverse("assets:asset_create"), data=form_data)
        self.assertEqual(response.status_code, 302)

        # Verify custom values JSON saved correctly in DB
        asset = Asset.objects.get(asset_tag="PHN-001")
        self.assertEqual(asset.custom_field_data.get("sim_number"), "8904903200001234567")
        self.assertEqual(asset.custom_field_data.get("screen_size"), "6.1")

        # Verify values display on the detail page
        detail_url = reverse("assets:asset_detail", kwargs={"pk": asset.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SIM Number")
        self.assertContains(response, "8904903200001234567")
        self.assertContains(response, "Screen Size")
        self.assertContains(response, "6.1")

    def test_straight_line_depreciation_math(self):
        import datetime
        from decimal import Decimal

        deprec = Depreciation.objects.create(
            name="10 Months Schedule",
            months=10,
            convention="exclude_purchase_month",
        )

        asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="MacBook Air", slug="apple-macbook-air", depreciation=deprec
        )

        today = datetime.date.today()
        purchase_date_4m = today - datetime.timedelta(days=4 * 30)

        asset_mid = Asset.objects.create(
            name="Developer MacBook",
            asset_tag="MAC-001",
            asset_type=asset_type,
            purchase_cost=Decimal("1000.00"),
            salvage_value=Decimal("0.00"),
            purchase_date=purchase_date_4m,
            status=self.available_status,
        )

        months_held = (today.year - purchase_date_4m.year) * 12 + today.month - purchase_date_4m.month
        expected_val = Decimal("1000.00") - (Decimal("100.00") * Decimal(str(months_held)))
        self.assertEqual(asset_mid.current_value, expected_val)

        purchase_date_12m = today - datetime.timedelta(days=12 * 30)
        asset_expired = Asset.objects.create(
            name="Old MacBook",
            asset_tag="MAC-002",
            asset_type=asset_type,
            purchase_cost=Decimal("1000.00"),
            salvage_value=Decimal("100.00"),
            purchase_date=purchase_date_12m,
            status=self.available_status,
        )
        self.assertEqual(asset_expired.current_value, Decimal("100.00"))

        asset_free = Asset.objects.create(
            name="Free MacBook", asset_tag="MAC-003", asset_type=asset_type, status=self.available_status
        )
        self.assertIsNone(asset_free.current_value)

        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": asset_mid.pk}))
        self.assertEqual(response.status_code, 200)
        # #496: the panel now distinguishes the estimated book value from a real
        # disposal value and from an archival freeze; this asset is neither disposed
        # nor frozen, so the rendered heading is the estimate one.
        self.assertContains(response, "Estimated book value:")

    def test_atomic_kit_checkout_flow(self):
        from inventory.models import AccessoryStock
        from licenses.models import License
        from organization.models import AssetHolder, Location, Site, Tenant
        from software.models import Software

        laptop_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="MacBook Pro", slug="apple-macbook-pro"
        )

        acc_cat = Category.objects.create(name="Chargers", slug="chargers", applies_to={"accessory": True})

        # ADR-0001 phase 4: stock requires a location owned by a tenant.
        tenant = Tenant.objects.create(name="Enterprise ITAM Co", slug="enterprise-itam-co")
        site = Site.objects.create(name="HQ", slug="hq", tenant=tenant)
        location = Location.objects.create(name="Warehouse", slug="warehouse", site=site, tenant=tenant)

        charger = Accessory.objects.create(
            manufacturer=self.manufacturer, name="USB-C 96W Charger", slug="apple-usb-c-96w-charger", category=acc_cat
        )
        AccessoryStock.objects.create(accessory=charger, location=location, qty=5)

        sw = Software.objects.create(manufacturer=self.manufacturer, name="Office 365")
        license_obj = License.objects.create(
            software=sw,
            name="O365 Enterprise Seat",
            seats=2,
            tenant=tenant,
        )

        kit = Kit.objects.create(name="Developer Onboarding Kit", description="MacBook, Charger, and O365")

        KitItem.objects.create(kit=kit, asset_type=laptop_type)
        KitItem.objects.create(kit=kit, accessory=charger, qty=1)
        KitItem.objects.create(kit=kit, license=license_obj)

        holder = AssetHolder.objects.create(
            first_name="René",
            last_name="Rettig",
            upn="rene@example.com",
            tenant=tenant,
        )

        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

        checkout_data = {
            "source_location": location.pk,
            "assigned_holder": holder.pk,
            "assigned_location": "",
            "notes": "Onboarding René",
        }

        # Issue #495: hardware rows require an explicit device pick. Without a
        # selection the form fails validation and nothing is allocated.
        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": kit.pk}), data=checkout_data, HTTP_HX_REQUEST="true"
        )
        # Validation failures on HTMX form posts answer 422 with the re-rendered
        # form fragment (swapped back into the modal body by the client).
        self.assertEqual(response.status_code, 422)
        self.assertContains(response, "This field is required.", status_code=422)

        self.assertEqual(AccessoryAssignment.objects.filter(accessory=charger).count(), 0)
        self.assertEqual(license_obj.assignments.count(), 0)

        macbook = Asset.objects.create(
            name="René MacBook Pro 16",
            asset_tag="LT-PRO-001",
            asset_type=laptop_type,
            status=self.available_status,
            tenant=tenant,
        )
        hardware_item = kit.items.get(asset_type=laptop_type)
        checkout_data[f"asset_{hardware_item.pk}"] = macbook.pk

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": kit.pk}), data=checkout_data, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 204)

        asset = Asset.objects.get(asset_tag="LT-PRO-001")
        self.assertEqual(asset.status, self.in_use_status)

        self.assertEqual(AccessoryAssignment.objects.filter(accessory=charger).count(), 1)
        self.assertEqual(charger.available, 4)

        self.assertEqual(license_obj.assignments.count(), 1)
        self.assertEqual(license_obj.available_seats, 1)

    def test_itam_layouts(self):
        supplier = Supplier.objects.create(
            name="Bechtle IT-Services",
            slug="bechtle-it-services",
            website="https://www.bechtle.com",
        )
        # Create a contact via the shared Contact system
        role, _ = ContactRole.objects.get_or_create(
            slug="primary-contact",
            defaults={"name": "Primary Contact", "description": "Primary Contact"},
        )
        contact = Contact.objects.create(
            name="Markus Müller",
            email="sales@bechtle.com",
        )
        supplier_ct = ContentType.objects.get_for_model(Supplier)
        ContactAssignment.objects.create(
            contact=contact,
            role=role,
            content_type=supplier_ct,
            object_id=supplier.pk,
            priority="primary",
        )

        detail_url = reverse("assets:supplier_detail", kwargs={"pk": supplier.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bechtle IT-Services")
        self.assertContains(response, "https://www.bechtle.com")
        self.assertContains(response, "Markus Müller")
        self.assertContains(response, "Supplied Assets")
        self.assertIn("assets_table", response.context)

        category = Category.objects.create(
            name="Enterprise Laptops", slug="enterprise-laptops", color="00ff00", applies_to=["asset", "accessory"]
        )

        detail_url = reverse("assets:category_detail", kwargs={"pk": category.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enterprise Laptops")
        self.assertContains(response, "#00ff00")
        self.assertContains(response, "Asset Types")
        self.assertContains(response, "Accessories")
        self.assertIn("asset_types_table", response.context)
        self.assertIn("accessories_table", response.context)

        asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="MacBook Pro 14", slug="apple-macbook-pro-14", requestable=True
        )
        request_obj = AssetRequest.objects.create(
            requester=self.user, asset_type=asset_type, notes="Need a development machine.", status="approved"
        )

        detail_url = reverse("assets:assetrequest_detail", kwargs={"pk": request_obj.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asset Request Details")
        self.assertContains(response, "Decision &amp; Response Details")
        self.assertContains(response, "Need a development machine.")
        self.assertContains(response, "Approved")

    def test_tenant_scoped_checkout_holders(self):
        from assets.forms import AssetCheckOutForm
        from inventory.forms import AccessoryCheckoutForm, ConsumableCheckoutForm, KitCheckoutForm
        from organization.models import AssetHolder, Tenant

        # Create two tenants
        tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")

        # Create asset holders for each tenant
        holder_a = AssetHolder.objects.create(
            first_name="Alice", last_name="A", upn="alice.a@example.com", tenant=tenant_a
        )
        holder_b = AssetHolder.objects.create(first_name="Bob", last_name="B", upn="bob.b@example.com", tenant=tenant_b)

        # 1. Test Asset checkout form filtering
        asset_a = Asset.objects.create(
            name="Laptop A", asset_tag="TAG-A", status=self.available_status, tenant=tenant_a
        )
        form = AssetCheckOutForm(asset=asset_a)
        holders_qs = form.fields["asset_holder"].queryset
        self.assertIn(holder_a, holders_qs)
        self.assertNotIn(holder_b, holders_qs)

        # 2. Test Accessory checkout form filtering
        from inventory.models import Accessory

        acc = Accessory.objects.create(
            manufacturer=self.manufacturer, name="Accessory A", slug="acc-a", tenant=tenant_b
        )
        form_acc = AccessoryCheckoutForm(accessory=acc)
        holders_qs_acc = form_acc.fields["assigned_holder"].queryset
        self.assertIn(holder_b, holders_qs_acc)
        self.assertNotIn(holder_a, holders_qs_acc)

        # 3. Test Consumable checkout form filtering
        con = Consumable.objects.create(
            manufacturer=self.manufacturer, name="Consumable A", slug="con-a", tenant=tenant_a
        )
        form_con = ConsumableCheckoutForm(consumable=con)
        holders_qs_con = form_con.fields["assigned_holder"].queryset
        self.assertIn(holder_a, holders_qs_con)
        self.assertNotIn(holder_b, holders_qs_con)

        # 4. Test Kit checkout form filtering
        from inventory.models import Kit

        kit = Kit.objects.create(name="Kit A", tenant=tenant_b)
        form_kit = KitCheckoutForm(kit=kit)
        holders_qs_kit = form_kit.fields["assigned_holder"].queryset
        self.assertIn(holder_b, holders_qs_kit)
        self.assertNotIn(holder_a, holders_qs_kit)


class CategoryTestCase(_SeededStatusLabelsMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testadmin", password="testpassword", is_staff=True, is_superuser=True
        )
        self.client.login(username="testadmin", password="testpassword")

    def test_category_list_view_and_color_rendering(self):
        # Create a category with color code
        Category.objects.create(name="Laptop Category", slug="laptop-category", color="ff0000")

        response = self.client.get(reverse("assets:category_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Laptop Category")
        # Check that the color hex is displayed, and the badge span contains the background-color style
        self.assertContains(response, "background-color:#ff0000")
        self.assertContains(response, "#ff0000")


class AssetCatalogLocalizationTest(SimpleTestCase):
    def test_german_asset_labels_follow_the_glossary(self):
        bulk_receive_message = (
            "Enter serial numbers and asset details below for the approved requests (%(count)s total). "
            "New assets will be allocated to the matching requests."
        )
        translations = {
            "Add new Asset Type": "Neuen Asset-Typ hinzufügen",
            "Add new Location": "Neuen Lagerort hinzufügen",
            "Custody Declined": "Verwahrung abgelehnt",
            "View Receipt": "Verwahrungsprotokoll anzeigen",
            "Warranty Supplier": "Garantielieferant",
            "Select an Asset Holder.": "Wählen Sie einen Asset-Inhaber als Ziel aus.",
            "The holder has no e-mail address.": "Der Asset-Inhaber hat keine E-Mail-Adresse.",
            bulk_receive_message: (
                "Sie nehmen Hardware entgegen, um genehmigte Anforderungen zu erfüllen "
                "(Anzahl: %(count)s). Geben Sie unten die Seriennummern und individuellen "
                "Angaben für jedes Asset ein. Beim Anlegen werden die Assets automatisch "
                "den entsprechenden Anforderungen zugewiesen."
            ),
        }

        with translation.override("de"):
            for source, expected in translations.items():
                with self.subTest(source=source):
                    self.assertEqual(gettext(source), expected)
                    for placeholder in ("%(count)s", "%(field)s"):
                        if placeholder in source:
                            self.assertIn(placeholder, gettext(source))

    def test_german_delete_plural_keeps_both_forms(self):
        singular = "Delete %(counter)s %(model_verbose_name)s"
        plural = "Delete %(counter)s %(model_verbose_name_plural)s"

        with translation.override("de"):
            self.assertEqual(ngettext(singular, plural, 1), "%(counter)s %(model_verbose_name)s löschen")


class EnglishAssetCopyLocalizationTest(SimpleTestCase):
    def test_direct_english_asset_copy(self):
        phrases = [
            "Select the item you are requesting (Asset, Asset Type, Component, Accessory, or Consumable).",
            "A request can contain only one item type.",
            "Select an Asset Holder.",
            "Select a Location.",
            "Select a Parent Asset.",
            "Select only one assignment target.",
            "No 'Deployed' Status Label exists. Configure one first.",
            "Choose either a Tenant or a Tenant Group for this template, not both.",
            "Label generation job '%(job)s' queued. Track its progress in real time.",
            "Stock received and allocated; awaiting handover.",
            "The system archives the asset and creates a permanent disposal record for audit.",
            "The system archives each asset and freezes its book value. It skips assets already marked as disposed.",
            "Dispose of 0 assets?",
            "The system checks in each asset if needed, archives it, and records the disposal. Reversing this is difficult.",
            "No custody receipts exist for this asset.",
            'Set margins to "None" in your print options.',
        ]
        with translation.override("en"):
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(gettext(phrase), phrase)

    def test_changed_catalog_keeps_german_copy_and_placeholders(self):
        custody_message = (
            "Custody has been assigned to you:\n\n"
            "  Asset: %(name)s\n"
            "  Asset Tag: %(tag)s\n"
            "  Serial: %(serial)s\n\n"
            "Accept custody using this link:\n%(url)s\n\n"
            "This link expires in 7 days."
        )
        bulk_receive_message = (
            "Enter serial numbers and asset details below for the approved requests (%(count)s total). "
            "New assets will be allocated to the matching requests."
        )

        with translation.override("de"):
            translated_custody = gettext(custody_message)
            translated_bulk_receive = gettext(bulk_receive_message)

        self.assertEqual(
            translated_custody,
            "Ihnen wurde die Verwahrung für folgendes Asset zugewiesen:\n\n"
            "  Asset: %(name)s\n"
            "  Inventarnummer: %(tag)s\n"
            "  Seriennummer: %(serial)s\n\n"
            "Bestätigen Sie die Verwahrung über diesen Link:\n%(url)s\n\n"
            "Der Link läuft in 7 Tagen ab.",
        )
        self.assertEqual(
            translated_bulk_receive,
            "Sie nehmen Hardware entgegen, um genehmigte Anforderungen zu erfüllen (Anzahl: %(count)s). "
            "Geben Sie unten die Seriennummern und individuellen Angaben für jedes Asset ein. "
            "Beim Anlegen werden die Assets automatisch den entsprechenden Anforderungen zugewiesen.",
        )
        for placeholder in ("%(name)s", "%(tag)s", "%(serial)s", "%(url)s"):
            self.assertEqual(translated_custody.count(placeholder), 1)
        self.assertEqual(translated_bulk_receive.count("%(count)s"), 1)


class _ElementAttributesParser(HTMLParser):
    def __init__(self, element_id):
        super().__init__()
        self.element_id = element_id
        self.attributes = None
        self.match_count = 0

    def handle_starttag(self, _tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id") == self.element_id:
            self.match_count += 1
            self.attributes = attributes


class Issue419AssetDeleteTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Issue 419 Tenant",
            slug="issue-419",
            permissions=["assets.view_asset", "assets.delete_asset"],
        )
        self.other_tenant = Tenant.objects.create(name="Issue 419 Other", slug="issue-419-other")
        self.asset = Asset.objects.create(name="Issue 419 Laptop", asset_tag="ISSUE-419", tenant=self.tenant)
        self.other_asset = Asset.objects.create(
            name="Issue 419 Foreign Laptop",
            asset_tag="ISSUE-419-FOREIGN",
            tenant=self.other_tenant,
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        self.delete_url = reverse("assets:asset_delete", kwargs={"pk": self.asset.pk})

    def test_asset_list_renders_the_registered_delete_url(self):
        self.assertEqual(self.delete_url, f"/assets/assets/{self.asset.pk}/delete/")

        response = self.client.get(reverse("assets:asset_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.delete_url)

    def test_authorized_delete_confirmation_supports_normal_boosted_and_history_restore_get(self):
        response = self.client.get(self.delete_url)
        boosted_response = self.client.get(
            self.delete_url,
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
            HTTP_HX_TARGET="page-content-wrapper",
        )
        history_response = self.client.get(
            self.delete_url,
            HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true",
        )

        for candidate in (response, boosted_response, history_response):
            with self.subTest(response_type=candidate):
                self.assertEqual(candidate.status_code, 200)
                self.assertTemplateUsed(candidate, "generic/object_confirm_delete.html")
                self.assertContains(candidate, "Confirm Deletion")
        self.assertContains(history_response, 'id="page-header-block"')
        self.assertContains(history_response, 'id="page-body-main"')
        purge_call = "sessionStorage.removeItem('htmx-history-cache')"
        self.assertContains(response, purge_call)
        self.assertNotContains(boosted_response, purge_call)
        self.assertNotContains(history_response, purge_call)

    def test_confirming_asset_delete_soft_deletes_and_redirects_to_list(self):
        response = self.client.post(self.delete_url)

        self.assertRedirects(response, reverse("assets:asset_list"), fetch_redirect_response=False)
        self.assertFalse(Asset.objects.filter(pk=self.asset.pk).exists())
        self.assertIsNotNone(Asset.all_objects.get(pk=self.asset.pk).deleted_at)

    def test_unknown_and_cross_tenant_assets_remain_hidden(self):
        unknown_pk = max(self.asset.pk, self.other_asset.pk) + 1
        denied_urls = (
            reverse("assets:asset_delete", kwargs={"pk": self.other_asset.pk}),
            reverse("assets:asset_delete", kwargs={"pk": unknown_pk}),
        )

        for url in denied_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url).status_code, 404)

    def test_tenant_scoped_shell_is_not_saved_in_htmx_history_cache(self):
        response = self.client.get(reverse("assets:asset_list"))
        parser = _ElementAttributesParser("page-content-wrapper")
        parser.feed(response.content.decode())
        parser.close()

        self.assertIsNotNone(parser.attributes)
        self.assertEqual(parser.match_count, 1)
        self.assertIn("hx-history-elt", parser.attributes)
        self.assertEqual(parser.attributes.get("hx-history"), "false")


PURCHASE_DATE = datetime.date(2024, 1, 15)
IN_SERVICE_DATE = datetime.date(2024, 2, 1)
EXPECTED_EOL_DATE = datetime.date(2027, 1, 15)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


@override_settings(LANGUAGE_CODE="de")
class Issue391AssetEditTests(TenantTestMixin, TestCase):
    maxDiff = None

    def setUp(self):
        self.setup_tenant_context(name="Issue 391 Tenant", slug="issue-391")
        self.other_tenant = Tenant.objects.create(name="Other Tenant", slug="issue-391-other")
        self.set_active_tenant(self.tenant)

        site = Site.objects.create(name="Issue 391 Site", slug="issue-391-site", tenant=self.tenant)
        self.location = Location.objects.create(
            name="Room 391 A",
            slug="room-391-a",
            site=site,
            tenant=self.tenant,
        )
        self.other_location = Location.objects.create(
            name="Room 391 B",
            slug="room-391-b",
            site=site,
            tenant=self.tenant,
        )
        self.asset_type = baker.make(AssetType, eol_months=36)
        self.status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.asset = Asset.objects.create(
            name="Issue 391 Laptop",
            asset_tag="ISSUE-391",
            serial_number="SN-391",
            asset_type=self.asset_type,
            status=self.status,
            tenant=self.tenant,
            location=self.location,
            purchase_date=PURCHASE_DATE,
            purchase_cost=Decimal("1299.95"),
            salvage_value=Decimal("199.95"),
            currency="EUR",
            order_number="ORDER-391",
            in_service_date=IN_SERVICE_DATE,
            notes="Issue 391 original notes",
            requestable=True,
        )
        self.asset_content_type = ContentType.objects.get_for_model(Asset)
        self.edit_url = reverse("assets:asset_update", kwargs={"pk": self.asset.pk})
        self.detail_url = reverse("assets:asset_detail", kwargs={"pk": self.asset.pk})
        self.client.force_login(self.tenant_admin)

    def _get(self, url=None):
        with translation.override("de"):
            return self.client.get(url or self.edit_url, HTTP_ACCEPT_LANGUAGE="de")

    def _post(self, data, *, htmx=False):
        headers = {"HTTP_ACCEPT_LANGUAGE": "de"}
        if htmx:
            headers["HTTP_HX_REQUEST"] = "true"
        with translation.override("de"):
            return self.client.post(self.edit_url, data=data, **headers)

    def _browser_form_data(self, response):
        """Build a complete submission from the response's AssetForm.

        Browsers expose an empty value when a ``type=date`` input receives a
        localized, non-ISO value attribute. Reproduce that DOM behavior while
        taking every submitted field from the form's initial or bound data.
        """
        form = response.context["form"]
        self.assertIsInstance(form, AssetForm)
        data = {}
        with translation.override("de"):
            for name, field in form.fields.items():
                value = form[name].value()
                widget = field.widget
                prepared = field.prepare_value(value)

                if isinstance(widget, forms.SelectMultiple):
                    if prepared in (None, ""):
                        data[name] = []
                    elif isinstance(prepared, (list, tuple, set)):
                        data[name] = [str(item) for item in prepared]
                    else:
                        data[name] = [str(prepared)]
                    continue

                if isinstance(widget, forms.Select):
                    data[name] = "" if prepared is None else str(prepared)
                    continue

                rendered = widget.format_value(value)
                rendered = "" if rendered is None else str(rendered)
                input_type = widget.attrs.get("type", getattr(widget, "input_type", None))
                if input_type == "date" and rendered:
                    try:
                        valid_date = bool(ISO_DATE_RE.fullmatch(rendered)) and bool(
                            datetime.date.fromisoformat(rendered)
                        )
                    except ValueError:
                        valid_date = False
                    if not valid_date:
                        rendered = ""
                data[name] = rendered

        self.assertEqual(set(data), set(form.fields))
        return data

    def _assert_iso_date_inputs(self, response):
        html = response.content.decode()
        input_tags = re.findall(r"<input\b[^>]*>", html)
        for field_name, expected in (
            ("purchase_date", PURCHASE_DATE.isoformat()),
            ("in_service_date", IN_SERVICE_DATE.isoformat()),
        ):
            with self.subTest(field=field_name):
                tag = next((item for item in input_tags if 'name="' + field_name + '"' in item), None)
                self.assertIsNotNone(tag)
                self.assertIn('type="date"', tag)
                self.assertIn('value="' + expected + '"', tag)

    def _snapshot(self):
        asset = Asset._base_manager.get(pk=self.asset.pk)
        return serialize_object(asset, exclude_fields={"updated_at"})

    def _changes(self):
        return list(
            ObjectChange._base_manager.filter(
                changed_object_type=self.asset_content_type,
                changed_object_id=self.asset.pk,
            ).order_by("pk")
        )

    def _assert_one_change(self, before, *, audit_tenant, **updates):
        changes = self._changes()
        self.assertEqual(len(changes), 1)
        change = changes[0]
        expected_after = {**before, **updates}
        self.assertEqual(change.action, "update")
        self.assertEqual(change.user_id, self.tenant_admin.pk)
        self.assertEqual(change.tenant_id, audit_tenant.pk)
        self.assertEqual(change.prechange_data, before)
        self.assertEqual(change.postchange_data, expected_after)

    def _assert_lifecycle(self, asset):
        self.assertEqual(
            (asset.purchase_date, asset.in_service_date, asset.eol_date),
            (PURCHASE_DATE, IN_SERVICE_DATE, EXPECTED_EOL_DATE),
        )

    def test_german_edit_get_renders_both_date_inputs_as_iso(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self._assert_iso_date_inputs(response)

    def test_tenant_reload_is_read_only_and_final_post_preserves_lifecycle(self):
        edit_response = self._get()
        before = self._snapshot()
        reload_data = self._browser_form_data(edit_response)
        reload_data.update({"tenant": str(self.other_tenant.pk), "_reload": "1"})

        reload_response = self._post(reload_data, htmx=True)

        self.assertEqual(reload_response.status_code, 200)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._changes(), [])
        self._assert_iso_date_inputs(reload_response)

        final_data = self._browser_form_data(reload_response)
        final_data["location"] = ""
        final_response = self._post(final_data)
        edited = Asset._base_manager.get(pk=self.asset.pk)

        self.assertEqual(
            final_response.status_code,
            302,
            final_response.context["form"].errors.as_json() if final_response.context else final_response.content,
        )
        with self.subTest("lifecycle"):
            self._assert_lifecycle(edited)
        self._assert_one_change(
            before,
            audit_tenant=self.other_tenant,
            tenant=self.other_tenant.pk,
            location=None,
        )

    def test_location_only_edit_preserves_lifecycle_detail_readback_and_audit(self):
        edit_response = self._get()
        before = self._snapshot()
        post_data = self._browser_form_data(edit_response)
        post_data["location"] = str(self.other_location.pk)

        post_response = self._post(post_data)
        edited = Asset._base_manager.get(pk=self.asset.pk)
        detail_response = self._get(self.detail_url)

        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(edited.location_id, self.other_location.pk)
        with self.subTest("saved lifecycle"):
            self._assert_lifecycle(edited)
        with self.subTest("detail lifecycle"):
            self._assert_lifecycle(detail_response.context["object"])
        self._assert_one_change(before, audit_tenant=self.tenant, location=self.other_location.pk)

    def test_one_optional_field_edit_preserves_every_other_value(self):
        edit_response = self._get()
        before = self._snapshot()
        post_data = self._browser_form_data(edit_response)
        post_data["notes"] = "Only this optional field changed"

        post_response = self._post(post_data)
        expected_after = {**before, "notes": "Only this optional field changed"}

        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(self._snapshot(), expected_after)
        self._assert_one_change(
            before,
            audit_tenant=self.tenant,
            notes="Only this optional field changed",
        )

    def test_explicit_blank_dates_still_clear_them(self):
        edit_response = self._get()
        before = self._snapshot()
        post_data = self._browser_form_data(edit_response)
        post_data.update({"purchase_date": "", "in_service_date": ""})

        post_response = self._post(post_data)
        edited = Asset._base_manager.get(pk=self.asset.pk)

        self.assertEqual(post_response.status_code, 302)
        self.assertEqual((edited.purchase_date, edited.in_service_date, edited.eol_date), (None, None, None))
        self._assert_one_change(
            before,
            audit_tenant=self.tenant,
            purchase_date=None,
            in_service_date=None,
        )


class JavaScriptCatalogLocalizationTest(SimpleTestCase):
    # Bulk-basket tenant strings introduced by #413/#424 and translated under
    # #437. Kept in a separate mapping so the English parity test can iterate
    # the same identities.
    BULK_BASKET_GERMAN = {
        "Select a target tenant before scanning.": "Wählen Sie vor dem Scannen einen Ziel-Mandanten aus.",
        "%(count)s asset from another tenant is kept aside. Switch the target tenant to see it.": [
            "%(count)s Asset aus einem anderen Mandanten wird zurückgehalten. "
            "Wechseln Sie den Ziel-Mandanten, um es anzuzeigen.",
            "%(count)s Assets aus einem anderen Mandanten werden zurückgehalten. "
            "Wechseln Sie den Ziel-Mandanten, um sie anzuzeigen.",
        ],
        "Not set": "Nicht festgelegt",
    }

    EXPECTED_GERMAN = {
        "Supplier": "Lieferant",
        "Start Date": "Startdatum",
        "Renewal Date": "Verlängerungsdatum",
        "Covered Assets": "Abgedeckte Assets",
        "Warranty Type": "Garantieart",
        "Warranty Cost": "Garantiekosten",
        "Reference": "Referenz",
        "Sanitization Certificate": "Zertifikat zur Datenlöschung",
        "Recipient": "Empfänger",
        "Notes": "Notizen",
        "Item Type": "Artikeltyp",
        "Name": "Name",
        "Category": "Kategorie",
        "Part Number": "Teilenummer",
        "Total Stock": "Gesamtbestand",
        "Available": "Verfügbar",
        "Safety Threshold": "Sicherheitsbestand",
        "Stock Status": "Bestandsstatus",
        "Holder": "Asset-Inhaber",
        "Signature Provider": "Signaturanbieter",
        "Created Date": "Erstellungsdatum",
        "IP Address": "IP-Adresse",
        "Matching": "Übereinstimmung",
        "Mismatch": "Abweichung",
        "Surprise": "Unerwartet",
        "This asset cannot be audited.": "Dieses Asset kann nicht geprüft werden.",
        "loading": "Wird geladen...",
        "no_results": "Keine Ergebnisse gefunden",
        "not_loading": "Keine weiteren Ergebnisse",
        "option_create": "Option erstellen",
        **BULK_BASKET_GERMAN,
    }

    def _catalog_for(self, language):
        with translation.override(language):
            response = self.client.get(
                reverse("javascript-catalog"),
                HTTP_ACCEPT_LANGUAGE=language,
                HTTP_HOST="localhost",
            )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        marker = "const newcatalog = "
        if marker not in body:
            # Django omits the assignment for empty catalogues (e.g. English,
            # which has no locale directory): the served JS falls back to the
            # gettext identity function and renders source strings unchanged.
            return {}
        start = body.index(marker) + len(marker)
        end = body.index(";\n", start)
        return json.loads(body[start:end])

    def test_german_javascript_catalog_translates_reviewed_entries(self):
        catalog = self._catalog_for("de")

        for source, expected in self.EXPECTED_GERMAN.items():
            with self.subTest(source=source):
                self.assertEqual(catalog[source], expected)

    def test_english_javascript_catalog_keeps_bulk_basket_sources_untranslated(self):
        # The English sessions have no catalogue of their own: the JS gettext
        # identity fallback renders the English source strings unchanged. The
        # served catalog must therefore either omit the bulk-basket messages
        # or map them to their English source — never to a German value.
        catalog = self._catalog_for("en")

        for source in self.BULK_BASKET_GERMAN:
            with self.subTest(source=source):
                self.assertIn(catalog.get(source), (None, source))
