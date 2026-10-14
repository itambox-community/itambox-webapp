"""Bounded follow-up to #495/#523: a kit is always measured against its owner.

Maintainer decisions under test:

* A tenant-owned kit is a tenant's kit. Opening its checkout modal from an
  aggregate scope (all-accessible or tenant group) preselects the kit's owning
  tenant and populates *that* tenant's dependent choices — no needless tenant
  change, no aggregate-scope leakage, and no permission shortcut.
* Kit availability (hardware, accessories, consumables, components, licenses)
  counts the owning tenant's devices and stock pools only. Never the aggregate
  total across the tenants a scope happens to contain, and never another
  tenant's pool just because the operator can see it.
* A tenantless legacy (global) template has no owner to measure: availability
  fails closed to an explicit unknown/select-target presentation and the
  explicit target-tenant selection stays the only way to check it out.

The tests drive the real ORM and the real HTTP routes; nothing here is a mocked
scope helper. Choice-list expectations are asserted against the rendered
response, because the tenant-scoped choice fields re-apply the tenant scope on
every access (``core.forms.scoping``) and would otherwise be re-scoped
to the test's ambient scope instead of the scope the modal was rendered under.
"""

import re

from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetAssignment, AssetType, Category, Manufacturer, StatusLabel
from compliance.models import CustodyReceipt, CustodyTemplate
from core.tables.constants import TABLE_EMPTY_VALUE
from core.tests.mixins import TenantTestMixin
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
    Kit,
    KitItem,
)
from inventory.tests.factories import create_assignment_fixture
from licenses.models import License, LicenseSeatAssignment
from organization.models import AssetHolder, Location, Role, Site, Tenant, TenantGroup
from software.models import Software

UNKNOWN_BADGE_COPY = "Select a target tenant first."
REQUIRED_FIELD_COPY = "This field is required."
ERROR_ALERT_COPY = "Please correct the errors below."
OWNER_HOLDER_UPN = "owner.holder@ko"
FOREIGN_HOLDER_UPN = "foreign.holder@ko"


class KitOwnerScopeBase(TenantTestMixin, TestCase):
    """One owned kit in tenant A, plus a second tenant B the user can also see."""

    def setUp(self):
        self.setup_tenant_context(name="Owner Scope", slug="owner-scope")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.deployable = StatusLabel.objects.create(name="KO In Stock", slug="ko-in-stock", type="deployable")
        self.deployed = StatusLabel.objects.create(name="KO In Use", slug="ko-in-use", type="deployed")
        self.manufacturer = Manufacturer.objects.create(name="KO Vendor", slug="ko-vendor")
        self.category = Category.objects.create(name="KO Laptops", slug="ko-laptops")
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="KO 14", slug="ko-14", category=self.category
        )
        self.template = CustodyTemplate.objects.create(
            tenant=self.tenant,
            category=self.category,
            is_active=True,
            require_acceptance=True,
            email_signature_request=True,
            signature_provider="local",
            name="KO EULA",
            eula_text="KO EULA terms.",
            disclaimer="Sign here.",
            qms_reference="QMS-KO-1",
        )
        self.site = Site.objects.create(name="KO Site", slug="ko-site", tenant=self.tenant)
        self.location = Location.objects.create(
            name="KO Warehouse", slug="ko-warehouse", site=self.site, tenant=self.tenant
        )
        self.holder = AssetHolder.objects.create(
            first_name="Owner",
            last_name="Recipient",
            upn=OWNER_HOLDER_UPN,
            email=OWNER_HOLDER_UPN,
            tenant=self.tenant,
        )
        self.kit = Kit.objects.create(name="KO Owned Kit", tenant=self.tenant)

        # Tenant B: reachable by the same user, but never the owner of A's kit.
        self.other = Tenant.objects.create(name="KO Other", slug="ko-other")
        self.other_role = Role.objects.create(
            tenant=self.other,
            name="KO Other Role",
            permissions=["inventory.view_kit", "inventory.change_kit"],
        )
        self.grant(self.tenant_user, self.other, self.other_role)
        self.other_site = Site.objects.create(name="KO Other Site", slug="ko-other-site", tenant=self.other)
        self.other_location = Location.objects.create(
            name="KO Other Warehouse", slug="ko-other-warehouse", site=self.other_site, tenant=self.other
        )
        self.other_holder = AssetHolder.objects.create(
            first_name="Foreign",
            last_name="Recipient",
            upn=FOREIGN_HOLDER_UPN,
            email=FOREIGN_HOLDER_UPN,
            tenant=self.other,
        )

    def tearDown(self):
        self.clear_tenant_context()
        super().tearDown()

    # ---------------------------------------------------------------- fixtures
    def make_asset(self, tag, tenant=None, status=None, asset_type=None):
        return Asset.objects.create(
            name=f"Device {tag}",
            asset_tag=tag,
            serial_number=f"SN-{tag}",
            asset_type=asset_type or self.asset_type,
            status=status or self.deployable,
            tenant=tenant or self.tenant,
        )

    def make_accessory(self, name="KO Dock", tenant=None):
        return Accessory.objects.create(name=name, manufacturer=self.manufacturer, tenant=tenant or self.tenant)

    def make_consumable(self, name="KO Cable", tenant=None):
        return Consumable.objects.create(name=name, manufacturer=self.manufacturer, tenant=tenant or self.tenant)

    def make_global_accessory(self, name="KO Shared Dock"):
        """A legitimate global (tenantless) catalogue item, stocked per tenant."""
        return Accessory.objects.create(name=name, manufacturer=self.manufacturer, tenant=None)

    def make_component(self, name="KO SSD", tenant=None):
        return Component.objects.create(name=name, manufacturer=self.manufacturer, tenant=tenant or self.tenant)

    def make_license(self, seats, tenant=None, name="KO Seat"):
        software = Software.objects.create(name=f"KO Suite {name}", manufacturer=self.manufacturer)
        return License.objects.create(
            name=name, software=software, seats=seats, tenant=self.tenant if tenant is None else tenant
        )

    def grant_role_permissions(self, *permissions):
        self.tenant_role.permissions = list(permissions)
        self.tenant_role.save(update_fields=["permissions"])

    # ------------------------------------------------------------------- scopes
    def _login(self, *permissions):
        self.grant_role_permissions(*(permissions or ("inventory.view_kit", "inventory.change_kit")))
        self.client_login_to_tenant(self.tenant_user, self.tenant)

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

    def _group_with_both_tenants(self, name="KO Scope Group", slug="ko-scope-group"):
        group = TenantGroup.objects.create(name=name, slug=slug)
        self.tenant.group = group
        self.tenant.save(update_fields=["group"])
        self.other.group = group
        self.other.save(update_fields=["group"])
        return group

    # ----------------------------------------------------------------- surfaces
    def _detail_rows(self, kit=None):
        kit = kit or self.kit
        response = self.client.get(reverse("inventory:kit_detail", kwargs={"pk": kit.pk}))
        self.assertEqual(response.status_code, 200)
        rows = {row["item"].pk: row for row in response.context["items_with_availability"]}
        return response, rows

    def _modal(self, kit=None):
        kit = kit or self.kit
        return self.client.get(reverse("inventory:kit_checkout_modal", kwargs={"pk": kit.pk}), HTTP_HX_REQUEST="true")

    def _post_data(self, **extra):
        data = {
            "source_location": self.location.pk,
            "assigned_holder": self.holder.pk,
            "assigned_location": "",
            "assigned_asset": "",
            "notes": "Owner scope checkout",
        }
        data.update(extra)
        return data


class KitAvailabilityOwnerScopeTests(KitOwnerScopeBase):
    """Availability counts the owning tenant's devices and pools, never a total."""

    def test_owned_kit_hardware_availability_ignores_other_tenants_devices(self):
        item = KitItem.objects.create(kit=self.kit, asset_type=self.asset_type)
        self.make_asset("KO-OWN-1")
        for tag in ("KO-FOR-1", "KO-FOR-2", "KO-FOR-3"):
            self.make_asset(tag, tenant=self.other)

        self._login()
        _, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 1)
        self.assertTrue(rows[item.pk]["is_available"])

        group = self._group_with_both_tenants()
        self._activate_tenant_group(group)
        _, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 1)
        self.assertTrue(rows[item.pk]["is_available"])

        self._activate_all_accessible()
        response, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 1)
        self.assertTrue(rows[item.pk]["is_available"])
        self.assertTrue(response.context["all_available"])

    def test_owned_kit_stock_availability_ignores_other_tenants_pools(self):
        accessory = self.make_accessory()
        item = KitItem.objects.create(kit=self.kit, accessory=accessory, qty=1)
        # Only tenant B holds stock for this catalog item.
        AccessoryStock.objects.create(accessory=accessory, location=self.other_location, qty=5)

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows()

        self.assertEqual(rows[item.pk]["available_count"], 0)
        self.assertFalse(rows[item.pk]["is_available"])
        self.assertFalse(response.context["all_available"])
        self.assertContains(response, "Kit Unavailable (Out of Stock)")

    def test_owned_kit_pool_and_assignment_deductions_are_scoped_to_the_owner(self):
        accessory = self.make_accessory("KO Cradle")
        item = KitItem.objects.create(kit=self.kit, accessory=accessory, qty=1)
        AccessoryStock.objects.create(accessory=accessory, location=self.location, qty=3)
        AccessoryStock.objects.create(accessory=accessory, location=self.other_location, qty=10)
        # A target-only commitment (no source pool) deducts from the owner pool.
        create_assignment_fixture(AccessoryAssignment, accessory=accessory, assigned_holder=self.holder, qty=2)

        self._login()
        _, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 1)

        self._activate_all_accessible()
        _, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 1)
        self.assertTrue(rows[item.pk]["is_available"])

    def test_global_catalogue_item_uses_owner_pools_and_destination_ownership(self):
        """A legitimate global catalogue item is a normal, intended pattern.

        Its stock is still owned per tenant (the stock row's tenant), and a
        commitment deducts from the tenant it TARGETS -- the catalogue item
        itself carries no tenant to scope by.
        """
        accessory = self.make_global_accessory()
        item = KitItem.objects.create(kit=self.kit, accessory=accessory, qty=1)
        AccessoryStock.objects.create(accessory=accessory, location=self.location, qty=2)
        AccessoryStock.objects.create(accessory=accessory, location=self.other_location, qty=9)
        create_assignment_fixture(AccessoryAssignment, accessory=accessory, assigned_holder=self.holder, qty=1)
        create_assignment_fixture(AccessoryAssignment, accessory=accessory, assigned_holder=self.other_holder, qty=4)

        self._login()
        self._activate_all_accessible()
        _, rows = self._detail_rows()

        # 2 owner units - 1 owner-targeted commitment = 1. The foreign pool and
        # the foreign-targeted commitment stay out of the owner's number.
        self.assertEqual(rows[item.pk]["available_count"], 1)
        self.assertTrue(rows[item.pk]["is_available"])

    def test_global_catalogue_item_without_owner_stock_stays_unavailable(self):
        accessory = self.make_global_accessory("KO Shared Fan")
        item = KitItem.objects.create(kit=self.kit, accessory=accessory, qty=1)
        AccessoryStock.objects.create(accessory=accessory, location=self.other_location, qty=6)

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows()

        self.assertEqual(rows[item.pk]["available_count"], 0)
        self.assertFalse(rows[item.pk]["is_available"])
        self.assertContains(response, "Kit Unavailable (Out of Stock)")

    def test_owned_kit_consumable_availability_is_scoped_to_the_owner(self):
        consumable = self.make_consumable("KO Toner")
        item = KitItem.objects.create(kit=self.kit, consumable=consumable, qty=2)
        ConsumableStock.objects.create(consumable=consumable, location=self.location, qty=2)
        ConsumableStock.objects.create(consumable=consumable, location=self.other_location, qty=9)
        create_assignment_fixture(ConsumableAssignment, consumable=consumable, assigned_holder=self.holder, qty=1)

        self._login()
        self._activate_all_accessible()
        _, rows = self._detail_rows()

        self.assertEqual(rows[item.pk]["available_count"], 1)
        self.assertFalse(rows[item.pk]["is_available"])

    def test_owned_kit_component_availability_is_scoped_to_the_owner(self):
        component = self.make_component()
        item = KitItem.objects.create(kit=self.kit, component=component, qty=2)
        ComponentStock.objects.create(component=component, location=self.location, qty=2)
        ComponentStock.objects.create(component=component, location=self.other_location, qty=9)

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows()

        self.assertEqual(rows[item.pk]["available_count"], 2)
        self.assertTrue(rows[item.pk]["is_available"])
        self.assertTrue(response.context["all_available"])
        self.assertContains(response, "KO SSD")

    def test_component_row_without_owner_stock_but_foreign_stock_is_unavailable(self):
        component = self.make_component("KO Fan")
        item = KitItem.objects.create(kit=self.kit, component=component, qty=1)
        ComponentStock.objects.create(component=component, location=self.other_location, qty=4)

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows()

        self.assertEqual(rows[item.pk]["available_count"], 0)
        self.assertFalse(rows[item.pk]["is_available"])
        self.assertContains(response, "Kit Unavailable (Out of Stock)")

    def test_repeated_hardware_rows_require_distinct_owner_devices(self):
        first = KitItem.objects.create(kit=self.kit, asset_type=self.asset_type)
        second = KitItem.objects.create(kit=self.kit, asset_type=self.asset_type)
        self.make_asset("KO-SINGLE-1")

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows()

        # The per-type pool is truthfully reported ...
        self.assertEqual(rows[first.pk]["available_count"], 1)
        self.assertEqual(rows[second.pk]["available_count"], 1)
        # ... and the kit still cannot be satisfied: two rows need two distinct
        # devices, which is exactly what the checkout service enforces.
        self.assertFalse(rows[first.pk]["is_available"])
        self.assertFalse(rows[second.pk]["is_available"])
        self.assertFalse(response.context["all_available"])
        self.assertContains(response, "Kit Unavailable (Out of Stock)")

        self.make_asset("KO-SINGLE-2")
        _, rows = self._detail_rows()
        self.assertTrue(rows[first.pk]["is_available"])
        self.assertTrue(rows[second.pk]["is_available"])

    def test_license_seats_count_the_owner_pool_only(self):
        license_obj = self.make_license(seats=3)
        item = KitItem.objects.create(kit=self.kit, license=license_obj)
        LicenseSeatAssignment.objects.create(license=license_obj, assigned_holder=self.holder)

        self._login()
        _, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 2)
        self.assertTrue(rows[item.pk]["is_available"])

        self._activate_all_accessible()
        _, rows = self._detail_rows()
        self.assertEqual(rows[item.pk]["available_count"], 2)

    def test_license_owned_by_another_tenant_fails_closed(self):
        license_obj = self.make_license(seats=5, tenant=self.other, name="KO Foreign Seat")
        item = KitItem.objects.create(kit=self.kit, license=license_obj)

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows()

        self.assertIsNone(rows[item.pk]["available_count"])
        self.assertTrue(rows[item.pk]["unknown"])
        self.assertFalse(rows[item.pk]["needs_target"])
        self.assertContains(response, "Unknown")

    def test_tenantless_license_fails_closed(self):
        software = Software.objects.create(name="KO Global Suite", manufacturer=self.manufacturer)
        license_obj = License.objects.create(name="KO Global Seat", software=software, seats=4, tenant=None)
        item = KitItem.objects.create(kit=self.kit, license=license_obj)

        self._login()
        _, rows = self._detail_rows()

        self.assertIsNone(rows[item.pk]["available_count"])
        self.assertTrue(rows[item.pk]["unknown"])


class KitAvailabilityGlobalTemplateTests(KitOwnerScopeBase):
    """A tenantless template has no owner: fail closed instead of aggregating."""

    def setUp(self):
        super().setUp()
        self.global_kit = Kit.objects.create(name="KO Global Kit")
        self.global_item = KitItem.objects.create(kit=self.global_kit, asset_type=self.asset_type)

    def test_tenantless_kit_availability_is_unknown_in_an_aggregate_scope(self):
        self.make_asset("KO-GLOBAL-OWN")
        for tag in ("KO-GLOBAL-FOR-1", "KO-GLOBAL-FOR-2", "KO-GLOBAL-FOR-3"):
            self.make_asset(tag, tenant=self.other)

        self._login()
        self._activate_all_accessible()
        response, rows = self._detail_rows(self.global_kit)

        self.assertIsNone(rows[self.global_item.pk]["available_count"])
        self.assertIsNone(rows[self.global_item.pk]["is_available"])
        self.assertTrue(rows[self.global_item.pk]["unknown"])
        self.assertTrue(rows[self.global_item.pk]["needs_target"])
        content = response.content.decode()
        self.assertIn(UNKNOWN_BADGE_COPY, content)
        # The explicit target selection stays the only checkout path.
        self.assertIn("Deploy / Checkout Kit", content)

    def test_tenantless_kit_modal_does_not_infer_an_owner(self):
        self.make_asset("KO-GLOBAL-MODAL")
        self.make_asset("KO-GLOBAL-MODAL-FOR", tenant=self.other)

        self._login()
        self._activate_all_accessible()
        response = self._modal(self.global_kit)

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("tenant", form.fields)
        self.assertFalse(form.initial.get("tenant"))
        content = response.content.decode()
        self.assertNotIn('<option value="%s" selected' % self.tenant.pk, content)
        self.assertNotIn('<option value="%s" selected' % self.other.pk, content)
        # No owner inferred: no holder and no device is offered yet.
        self.assertNotIn(OWNER_HOLDER_UPN, content)
        self.assertNotIn("KO-GLOBAL-MODAL", content)

    def test_tenantless_kit_availability_uses_a_concrete_scope_tenant(self):
        self.make_asset("KO-CONCRETE-OWN")
        for tag in ("KO-CONCRETE-FOR-1", "KO-CONCRETE-FOR-2"):
            self.make_asset(tag, tenant=self.other)

        self._login()
        _, rows = self._detail_rows(self.global_kit)

        self.assertEqual(rows[self.global_item.pk]["available_count"], 1)
        self.assertTrue(rows[self.global_item.pk]["is_available"])


class KitAvailabilityPresentationTests(KitOwnerScopeBase):
    """Tri-state presentation: unknown is NEVER rendered as verified available.

    A known shortage is a known shortage; an unverifiable required resource
    (foreign/global license ownership) is unknown, and unknown must neither be
    dressed as available nor turned into a fabricated stock zero.
    """

    def _detail(self, kit=None):
        kit = kit or self.kit
        response = self.client.get(reverse("inventory:kit_detail", kwargs={"pk": kit.pk}))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def _checkout_action_classes(self, content, kit=None):
        """``class`` of THIS kit's checkout-modal action, or ``None``."""
        kit = kit or self.kit
        modal_url = reverse("inventory:kit_checkout_modal", kwargs={"pk": kit.pk})
        pattern = '<button[^>]*class="([^"]+)"[^>]*hx-get="' + re.escape(modal_url) + '"'
        match = re.search(pattern, content, re.S)
        return match.group(1) if match else None

    @staticmethod
    def _row_html(content, needle):
        """The table row that renders ``needle`` (the kit item's target)."""
        for row in re.findall(r"<tr>.*?</tr>", content, re.S):
            if needle in row:
                return row
        return ""

    @staticmethod
    def _has_empty_placeholder(row):
        pattern = rf">{re.escape(TABLE_EMPTY_VALUE)}\s*</(?:span|td)>"
        return re.search(pattern, row) is not None

    def test_known_available_kit_renders_the_verified_action(self):
        item = KitItem.objects.create(kit=self.kit, asset_type=self.asset_type)
        self.make_asset("KO-PRESENT-OK")

        self._login()
        response, content = self._detail()

        self.assertEqual(response.context["availability_state"], "available")
        self.assertTrue(response.context["all_available"])
        self.assertFalse(response.context["availability_needs_target"])
        self.assertIn("btn btn-success", self._checkout_action_classes(content))
        self.assertFalse(self._has_empty_placeholder(self._row_html(content, "KO 14")))
        self.assertEqual(response.context["items_with_availability"][0]["item"].pk, item.pk)

    def test_known_unavailable_kit_renders_out_of_stock(self):
        accessory = self.make_accessory("KO Missing Dock")
        KitItem.objects.create(kit=self.kit, accessory=accessory, qty=1)

        self._login()
        response, content = self._detail()

        self.assertEqual(response.context["availability_state"], "out_of_stock")
        self.assertFalse(response.context["all_available"])
        self.assertIn("Kit Unavailable (Out of Stock)", content)
        # A known shortage keeps the checkout control closed entirely.
        self.assertIsNone(self._checkout_action_classes(content))
        self.assertIn("<td>0</td>", self._row_html(content, "KO Missing Dock"))

    def test_owned_kit_with_a_foreign_license_is_unknown_not_available(self):
        license_obj = self.make_license(seats=5, tenant=self.other, name="KO Foreign Seat")
        KitItem.objects.create(kit=self.kit, license=license_obj)

        self._login()
        self._activate_all_accessible()
        response, content = self._detail()

        self.assertEqual(response.context["availability_state"], "unknown")
        self.assertFalse(response.context["all_available"])
        self.assertFalse(response.context["availability_needs_target"])
        classes = self._checkout_action_classes(content)
        self.assertIsNotNone(classes)
        self.assertNotIn("btn-success", classes)
        self.assertIn("btn-outline-secondary", classes)
        row = self._row_html(content, "KO Foreign Seat")
        self.assertIn("Unknown", row)
        # Unknown is not a fabricated zero: the count stays the neutral marker.
        self.assertTrue(self._has_empty_placeholder(row))
        self.assertNotIn("<td>0</td>", row)

    def test_tenantless_kit_unknown_state_keeps_the_target_selection_action(self):
        global_kit = Kit.objects.create(name="KO Global Presentation Kit")
        KitItem.objects.create(kit=global_kit, asset_type=self.asset_type)
        self.make_asset("KO-PRESENT-GLOBAL")
        self.make_asset("KO-PRESENT-FOREIGN", tenant=self.other)

        self._login()
        self._activate_all_accessible()
        response, content = self._detail(global_kit)

        self.assertEqual(response.context["availability_state"], "unknown")
        self.assertFalse(response.context["all_available"])
        self.assertTrue(response.context["availability_needs_target"])
        classes = self._checkout_action_classes(content, global_kit)
        self.assertIsNotNone(classes)
        self.assertNotIn("btn-success", classes)
        self.assertIn("btn-outline-secondary", classes)
        self.assertIn("Select a target tenant first.", content)
        self.assertIn("Deploy / Checkout Kit", content)

    def test_mixed_unknown_and_known_unavailable_reports_the_known_shortage(self):
        accessory = self.make_accessory("KO Missing Cradle")
        KitItem.objects.create(kit=self.kit, accessory=accessory, qty=1)
        license_obj = self.make_license(seats=4, tenant=self.other, name="KO Foreign Seat Mix")
        KitItem.objects.create(kit=self.kit, license=license_obj)

        self._login()
        self._activate_all_accessible()
        response, content = self._detail()

        self.assertEqual(response.context["availability_state"], "out_of_stock")
        self.assertFalse(response.context["all_available"])
        self.assertIn("Kit Unavailable (Out of Stock)", content)
        self.assertIsNone(self._checkout_action_classes(content))
        unknown_row = self._row_html(content, "KO Foreign Seat Mix")
        self.assertIn("Unknown", unknown_row)
        self.assertTrue(self._has_empty_placeholder(unknown_row))
        self.assertNotIn("<td>0</td>", unknown_row)
        self.assertIn("<td>0</td>", self._row_html(content, "KO Missing Cradle"))


class KitCheckoutOwnerPreselectionTests(KitOwnerScopeBase):
    """Opening an owned kit's modal in an aggregate scope starts on its owner."""

    def setUp(self):
        super().setUp()
        self.item = KitItem.objects.create(kit=self.kit, asset_type=self.asset_type)
        self.owner_device = self.make_asset("KO-MODAL-OWN")
        self.foreign_device = self.make_asset("KO-MODAL-FOR", tenant=self.other)

    def _assert_owner_is_preselected(self, response):
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("tenant", form.fields)
        self.assertTrue(form.fields["tenant"].required)
        content = response.content.decode()
        # Scope the option checks to the tenant select: a global substring
        # check collides whenever another select (e.g. the status options)
        # renders the same primary key value.
        tenant_select = content.split('name="tenant"', 1)[1].split("</select>", 1)[0]
        # Native + rendered preselection of the owning tenant.
        self.assertEqual(str(form["tenant"].value()), str(self.tenant.pk))
        self.assertIn('<option value="%s" selected' % self.tenant.pk, tenant_select)
        # The candidate set stays the owner: scope and permissions unchanged.
        self.assertNotIn('<option value="%s"' % self.other.pk, tenant_select)
        # The owner's dependent choices are populated without any change event.
        self.assertIn(OWNER_HOLDER_UPN, content)
        self.assertIn(self.location.name, content)
        self.assertIn(self.owner_device.asset_tag, content)
        # No other tenant's records leak into the modal.
        self.assertNotIn(FOREIGN_HOLDER_UPN, content)
        self.assertNotIn(self.other_location.name, content)
        self.assertNotIn(self.foreign_device.asset_tag, content)

    def test_all_accessible_modal_preselects_the_owner_tenant(self):
        self._login()
        self._activate_all_accessible()

        self._assert_owner_is_preselected(self._modal())

    def test_tenant_group_modal_preselects_the_owner_tenant(self):
        self._login()
        self._activate_tenant_group(self._group_with_both_tenants())

        self._assert_owner_is_preselected(self._modal())

    def test_first_open_reports_no_error_markup(self):
        self._login()
        self._activate_all_accessible()

        response = self._modal()

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(ERROR_ALERT_COPY, content)
        self.assertNotIn(REQUIRED_FIELD_COPY, content)

    def test_forged_tenant_post_cannot_retarget_an_owned_kit(self):
        self._login()
        self._activate_all_accessible()
        url = reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk})

        forged = self.client.post(
            url,
            data=self._post_data(tenant=self.other.pk, **{f"asset_{self.item.pk}": self.owner_device.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(forged.status_code, 422)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=self.owner_device).exists())
        self.assertEqual(CustodyReceipt.objects.filter(asset=self.owner_device).count(), 0)
        self.owner_device.refresh_from_db()
        self.assertEqual(self.owner_device.status, self.deployable)

        # Positive control: the same submission with the owning tenant succeeds,
        # so the denial above is the forged target and not a broken route.
        valid = self.client.post(
            url,
            data=self._post_data(tenant=self.tenant.pk, **{f"asset_{self.item.pk}": self.owner_device.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(valid.status_code, 204, valid.content.decode())
        self.assertTrue(AssetAssignment._base_manager.filter(asset=self.owner_device, is_active=True).exists())
        self.assertTrue(CustodyReceipt.objects.filter(asset=self.owner_device, holder=self.holder).exists())

    def test_requested_tenant_reload_keeps_values_without_untouched_errors(self):
        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, _reload="1", **{f"asset_{self.item.pk}": self.owner_device.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(ERROR_ALERT_COPY, content)
        self.assertNotIn(REQUIRED_FIELD_COPY, content)
        form = response.context["form"]
        self.assertEqual(str(form["tenant"].value()), str(self.tenant.pk))
        self.assertEqual(str(form["assigned_holder"].value()), str(self.holder.pk))
        self.assertEqual(str(form[f"asset_{self.item.pk}"].value()), str(self.owner_device.pk))
        self.assertIn('<option value="%s" selected' % self.holder.pk, content)
        self.assertFalse(AssetAssignment._base_manager.filter(asset=self.owner_device).exists())


class KitCheckoutGlobalTemplateReloadTests(KitOwnerScopeBase):
    """The tenantless template's refresh must not report untouched field errors."""

    def setUp(self):
        super().setUp()
        self.global_kit = Kit.objects.create(name="KO Global Reload Kit")
        self.item = KitItem.objects.create(kit=self.global_kit, asset_type=self.asset_type)
        self.device = self.make_asset("KO-RELOAD-1")

    def _reload(self, **extra):
        data = {"_reload": "1"}
        data.update(extra)
        return self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": self.global_kit.pk}),
            data=data,
            HTTP_HX_REQUEST="true",
        )

    def test_tenant_only_reload_shows_no_untouched_required_field_errors(self):
        self._login()
        self._activate_all_accessible()

        response = self._reload(tenant=self.tenant.pk)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(ERROR_ALERT_COPY, content)
        self.assertNotIn(REQUIRED_FIELD_COPY, content)
        form = response.context["form"]
        self.assertEqual(str(form["tenant"].value()), str(self.tenant.pk))
        # The now-scoped dependent choices are offered for the chosen tenant.
        self.assertIn(self.device.asset_tag, content)
        self.assertIn(OWNER_HOLDER_UPN, content)

    def test_reload_keeps_the_operators_safe_values(self):
        self._login()
        self._activate_all_accessible()

        response = self._reload(
            **self._post_data(
                tenant=self.tenant.pk,
                source_location=self.location.pk,
                assigned_holder=self.holder.pk,
                **{f"asset_{self.item.pk}": self.device.pk},
            )
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(ERROR_ALERT_COPY, content)
        self.assertNotIn(REQUIRED_FIELD_COPY, content)
        form = response.context["form"]
        self.assertEqual(str(form["source_location"].value()), str(self.location.pk))
        self.assertEqual(str(form["assigned_holder"].value()), str(self.holder.pk))
        self.assertEqual(str(form[f"asset_{self.item.pk}"].value()), str(self.device.pk))
        self.assertIn('<option value="%s" selected' % self.location.pk, content)
        self.assertIn('<option value="%s" selected' % self.device.pk, content)
        # A refresh is not a write.
        self.assertFalse(AssetAssignment._base_manager.filter(asset=self.device).exists())

    def test_reload_honours_an_explicit_accessible_target_only(self):
        self._login()
        self._activate_all_accessible()

        response = self._reload(tenant=self.other.pk)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        form = response.context["form"]
        self.assertEqual(str(form["tenant"].value()), str(self.other.pk))
        # The explicit selection is honoured (not a scope bypass) and stays
        # scoped to the chosen tenant's own records.
        self.assertIn(FOREIGN_HOLDER_UPN, content)
        self.assertNotIn(OWNER_HOLDER_UPN, content)


class KitCheckoutGlobalTemplateCheckoutTests(KitOwnerScopeBase):
    """The explicit target selection of a tenantless template keeps working."""

    def test_global_kit_checkout_with_an_explicit_target_still_persists(self):
        global_kit = Kit.objects.create(name="KO Global Checkout Kit")
        item = KitItem.objects.create(kit=global_kit, asset_type=self.asset_type)
        device = self.make_asset("KO-GLOBAL-CHECKOUT")

        self._login()
        self._activate_all_accessible()

        response = self.client.post(
            reverse("inventory:kit_checkout_modal", kwargs={"pk": global_kit.pk}),
            data=self._post_data(tenant=self.tenant.pk, **{f"asset_{item.pk}": device.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content.decode())
        assignment = AssetAssignment._base_manager.get(asset=device, is_active=True)
        self.assertEqual(assignment.assigned_user, self.holder)
        self.assertEqual(assignment.pre_checkout_status, self.deployable)
        self.assertTrue(CustodyReceipt.objects.filter(asset=device, holder=self.holder).exists())
