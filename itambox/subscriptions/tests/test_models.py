import datetime
import threading
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.deletion import PROTECT, SET_NULL, ProtectedError
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from assets.models import Asset, Supplier
from core.managers import set_current_all_accessible, set_current_tenant, set_current_tenant_group
from core.models import Notification
from core.tasks.context import TaskContext
from licenses.models import License, LicenseSeatAssignment
from organization.models import AssetHolder, CostCenter, Location, Site, Tenant, TenantGroup
from procurement.models import Contract
from software.models import Software
from subscriptions.models import (
    BillingCycleChoices,
    Subscription,
    SubscriptionAssignment,
    SubscriptionStatusChoices,
    SubscriptionTypeChoices,
)
from subscriptions.seat_services import count_assigned_seats

User = get_user_model()


class SubscriptionSeatRollupTests(TestCase):
    """Seats are tracked on Licenses; a Subscription rolls them up across the
    licenses it funds (Subscription -> License -> Software)."""

    def test_seats_roll_up_from_funded_licenses(self):
        sub = baker.make(Subscription, tenant=None)
        software = baker.make(Software, manufacturer__name="Acme", manufacturer__slug="acme", tenant=None)
        baker.make(License, software=software, subscription=sub, seats=10, tenant=None)
        l2 = baker.make(License, software=software, subscription=sub, seats=5, tenant=None)
        # A license NOT funded by this subscription must not be counted.
        baker.make(License, software=software, subscription=None, seats=99, tenant=None)

        self.assertEqual(sub.total_seats, 15)
        self.assertEqual(count_assigned_seats(sub), 0)
        self.assertEqual(max(0, sub.total_seats - count_assigned_seats(sub)), 15)

        holder = baker.make(AssetHolder, tenant=None)
        baker.make(LicenseSeatAssignment, license=l2, assigned_holder=holder, asset=None)
        self.assertEqual(count_assigned_seats(sub), 1)
        self.assertEqual(max(0, sub.total_seats - count_assigned_seats(sub)), 14)

    def test_assigned_seats_is_exactly_one_count_query(self):
        sub = baker.make(Subscription, tenant=None)
        software = baker.make(Software, manufacturer__name="Query Acme", manufacturer__slug="query-acme", tenant=None)
        license_obj = baker.make(License, software=software, subscription=sub, seats=2, tenant=None)
        holder = baker.make(AssetHolder, tenant=None)
        baker.make(LicenseSeatAssignment, license=license_obj, assigned_holder=holder, asset=None)

        with CaptureQueriesContext(connection) as queries:
            assigned = count_assigned_seats(sub)

        self.assertEqual(assigned, 1)
        self.assertEqual(len(queries), 1, queries.captured_queries)
        self.assertIn("COUNT", queries[0]["sql"].upper())

    def test_license_rejects_cross_tenant_subscription(self):
        t_a = baker.make(Tenant, name="A", slug="a")
        t_b = baker.make(Tenant, name="B", slug="b")
        sub_b = baker.make(Subscription, tenant=t_b)
        software = baker.make(Software, manufacturer__name="Acme2", manufacturer__slug="acme2", tenant=None)
        lic = baker.prepare(License, software=software, subscription=sub_b, seats=1, tenant=t_a)
        with self.assertRaises(ValidationError):
            lic.clean()

    def test_seat_accounting_ignores_stale_targets_and_is_stable_while_suspended(self):
        tenant = baker.make(Tenant, name="Seat tenant", slug="seat-tenant")
        other_tenant = baker.make(Tenant, name="Former tenant", slug="former-tenant")
        sub = baker.make(Subscription, tenant=tenant)
        software = baker.make(Software, manufacturer__name="Seat Co", manufacturer__slug="seat-co", tenant=None)
        license_obj = baker.make(License, software=software, subscription=sub, seats=4, tenant=tenant)
        holder = baker.make(AssetHolder, tenant=tenant)
        asset = baker.make(Asset, tenant=tenant)
        baker.make(LicenseSeatAssignment, license=license_obj, assigned_holder=holder, asset=None)
        baker.make(LicenseSeatAssignment, license=license_obj, assigned_holder=None, asset=asset)

        self.assertEqual(count_assigned_seats(sub), 2)
        set_current_tenant(other_tenant)
        try:
            self.assertEqual(
                (sub.total_seats, count_assigned_seats(sub), max(0, sub.total_seats - count_assigned_seats(sub))),
                (4, 2, 2),
            )
        finally:
            set_current_tenant(None)

        sub.suspend()
        self.assertEqual(
            (sub.total_seats, count_assigned_seats(sub), max(0, sub.total_seats - count_assigned_seats(sub))), (4, 2, 2)
        )

        with TaskContext(operation="test.subscription_seat_rollup.soft_delete_asset"):
            asset.delete()
        self.assertEqual(count_assigned_seats(sub), 1)

        holder.tenant = other_tenant
        holder.save(update_fields=["tenant"])
        self.assertEqual(count_assigned_seats(sub), 0)


class SupplierModelTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(
            name="AWS",
            account_id="aws-12345",
            portal_url="https://aws.amazon.com/console",
            is_active=True,
        )

    def test_supplier_creation(self):
        self.assertEqual(str(self.supplier), "AWS")
        self.assertTrue(self.supplier.is_active)

    def test_supplier_contact_resolution(self):
        from organization.models import Contact, ContactAssignment, ContactRole

        role, _ = ContactRole.objects.get_or_create(
            slug="primary-contact", defaults={"name": "Primary Contact", "description": "Primary Contact"}
        )
        contact = Contact.objects.create(
            name="AWS Account Manager", email="manager@aws.example.com", phone="+1-800-555-0199"
        )
        ContactAssignment.objects.create(
            contact=contact,
            role=role,
            content_type=ContentType.objects.get_for_model(Supplier),
            object_id=self.supplier.pk,
            priority="primary",
        )
        self.assertEqual(self.supplier.primary_contact, contact)

    def test_supplier_absolute_url(self):
        url = self.supplier.get_absolute_url()
        self.assertIn(str(self.supplier.pk), url)

    def test_supplier_slug_auto_generation(self):
        supplier = Supplier.objects.create(name="Google Cloud Platform")
        self.assertEqual(supplier.slug, "google-cloud-platform")

    def test_supplier_inactive_does_not_filter_out(self):
        supplier = Supplier.objects.create(name="Old Vendor", is_active=False)
        self.assertFalse(Supplier.objects.filter(is_active=True).filter(pk=supplier.pk).exists())


class SubscriptionModelTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name="Adobe Inc.", account_id="adobe-001")
        self.today = timezone.now().date()

    def _make_contract(self, tenant, contract_number):
        return Contract.objects.create(
            name=contract_number,
            contract_number=contract_number,
            start_date=self.today,
            end_date=self.today + datetime.timedelta(days=365),
            supplier=self.supplier,
            tenant=tenant,
        )

    def test_supplier_is_required_and_protected(self):
        supplier_field = Subscription._meta.get_field("supplier")
        self.assertFalse(supplier_field.null)
        self.assertFalse(supplier_field.blank)
        self.assertIs(supplier_field.remote_field.on_delete, PROTECT)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Subscription.objects.create(name="Missing Supplier", supplier=None)

        Subscription.objects.create(name="Protected Supplier", supplier=self.supplier)
        with self.assertRaises(ProtectedError):
            self.supplier.delete(force_hard_delete=True)

    def test_linked_contract_is_optional_and_set_null_on_delete(self):
        contract_field = Subscription._meta.get_field("linked_contract")
        self.assertTrue(contract_field.null)
        self.assertTrue(contract_field.blank)
        self.assertIs(contract_field.remote_field.on_delete, SET_NULL)

        subscription = Subscription.objects.create(name="No Linked Contract", supplier=self.supplier)
        self.assertIsNone(subscription.linked_contract_id)

    def test_clean_rejects_a_supplier_owned_by_another_tenant(self):
        tenant = baker.make(Tenant, name="Subscription Tenant", slug="subscription-tenant")
        other_tenant = baker.make(Tenant, name="Supplier Tenant", slug="supplier-tenant")
        supplier = Supplier.objects.create(name="Tenant Supplier", slug="tenant-supplier", tenant=other_tenant)
        subscription = Subscription(name="Cross Tenant Supplier", supplier=supplier, tenant=tenant)

        with self.assertRaises(ValidationError) as error:
            subscription.clean()
        self.assertIn("supplier", error.exception.message_dict)

    def test_clean_accepts_same_tenant_and_global_contracts(self):
        tenant = baker.make(Tenant, name="Linked Contract Tenant", slug="linked-contract-tenant")
        local_contract = self._make_contract(tenant, "local-linked-contract")
        global_contract = self._make_contract(None, "global-linked-contract")

        for contract in (local_contract, global_contract):
            with self.subTest(contract_tenant=contract.tenant_id):
                subscription = Subscription(
                    name=f"Linked Contract {contract.pk}",
                    supplier=self.supplier,
                    tenant=tenant,
                    linked_contract=contract,
                )
                subscription.clean()

    def test_clean_rejects_a_linked_contract_from_another_tenant(self):
        tenant = baker.make(Tenant, name="Contract Owner Tenant", slug="contract-owner-tenant")
        other_tenant = baker.make(Tenant, name="Other Contract Tenant", slug="other-contract-tenant")
        contract = self._make_contract(other_tenant, "other-linked-contract")
        subscription = Subscription(
            name="Cross Tenant Contract", supplier=self.supplier, tenant=tenant, linked_contract=contract
        )

        with self.assertRaises(ValidationError) as error:
            subscription.clean()
        self.assertIn("linked_contract", error.exception.message_dict)

    def test_subscription_creation(self):
        sub = Subscription.objects.create(
            name="Adobe Creative Cloud",
            supplier=self.supplier,
            type=SubscriptionTypeChoices.SAAS,
            status=SubscriptionStatusChoices.ACTIVE,
            start_date=self.today - datetime.timedelta(days=90),
            renewal_date=self.today + datetime.timedelta(days=275),
            renewal_cost=599.99,
            currency="USD",
            billing_cycle=BillingCycleChoices.ANNUAL,
            term_months=12,
            auto_renewal=True,
            licensed_quantity=25,
            contract_reference="PO-2026-0042",
            cost_center=None,
        )
        self.assertEqual(str(sub), "Adobe Inc. - Adobe Creative Cloud")
        self.assertEqual(sub.days_until_renewal, 275)
        self.assertFalse(sub.is_expired)
        self.assertEqual(sub.annual_cost, 599.99)

    def test_subscription_cost_center_fk(self):
        """cost_center is a FK to organization.CostCenter; null is the default."""
        sub_no_cc = Subscription.objects.create(
            name="No Cost Center Sub",
            supplier=self.supplier,
        )
        self.assertIsNone(sub_no_cc.cost_center)

        # Use baker so TenantScopingSoftDeleteManager's slug/unique constraints
        # are handled automatically for the CostCenter.
        cc = baker.make(CostCenter, name="Engineering", code="ENG-001", tenant=None)
        sub_with_cc = Subscription.objects.create(
            name="Engineering Tools",
            supplier=self.supplier,
            cost_center=cc,
        )
        self.assertEqual(sub_with_cc.cost_center, cc)
        self.assertEqual(sub_with_cc.cost_center.code, "ENG-001")
        # Reverse relation: the subscription should appear in cc.subscriptions
        self.assertIn(sub_with_cc, CostCenter.all_objects.get(pk=cc.pk).subscriptions.all())

    def test_subscription_expired(self):
        sub = Subscription.objects.create(
            name="Expired SaaS",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=self.today - datetime.timedelta(days=1),
            renewal_cost=100,
        )
        self.assertTrue(sub.is_expired)
        self.assertEqual(sub.days_until_renewal, -1)

    def test_subscription_renewing_today(self):
        sub = Subscription.objects.create(
            name="Renewing Today",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=self.today,
        )
        self.assertEqual(sub.days_until_renewal, 0)

    def test_subscription_annual_cost_monthly(self):
        sub = Subscription.objects.create(
            name="Monthly Plan",
            supplier=self.supplier,
            renewal_cost=49.99,
            billing_cycle=BillingCycleChoices.MONTHLY,
        )
        self.assertEqual(sub.annual_cost, 49.99 * 12)

    def test_subscription_annual_cost_quarterly(self):
        sub = Subscription.objects.create(
            name="Quarterly Plan",
            supplier=self.supplier,
            renewal_cost=299.99,
            billing_cycle=BillingCycleChoices.QUARTERLY,
        )
        self.assertEqual(sub.annual_cost, 299.99 * 4)

    def test_subscription_annual_cost_biannual(self):
        sub = Subscription.objects.create(
            name="Biannual Plan",
            supplier=self.supplier,
            renewal_cost=1199.99,
            billing_cycle=BillingCycleChoices.BIANNUAL,
        )
        self.assertEqual(sub.annual_cost, 1199.99 * 2)

    def test_subscription_annual_cost_none_when_no_cost(self):
        sub = Subscription.objects.create(
            name="Free Plan",
            supplier=self.supplier,
        )
        self.assertIsNone(sub.annual_cost)

    def test_subscription_annual_cost_multi_year_two_year_term(self):
        sub = Subscription.objects.create(
            name="Two-Year Contract",
            supplier=self.supplier,
            renewal_cost=Decimal("2400.00"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=24,
        )
        self.assertEqual(sub.annual_cost, Decimal("1200.00"))

    def test_subscription_annual_cost_multi_year_three_year_term(self):
        # Issue #501 scenario: 3600 over 36 months is 1200 per year, not 3600.
        sub = Subscription.objects.create(
            name="Three-Year Contract",
            supplier=self.supplier,
            renewal_cost=Decimal("3600.00"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=36,
        )
        sub.refresh_from_db()
        self.assertEqual(sub.annual_cost, Decimal("1200.00"))

    def test_subscription_annual_cost_multi_year_five_year_term(self):
        sub = Subscription.objects.create(
            name="Five-Year Contract",
            supplier=self.supplier,
            renewal_cost=Decimal("3000.00"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=60,
        )
        self.assertEqual(sub.annual_cost, Decimal("600.00"))

    def test_subscription_annual_cost_multi_year_rounds_half_up(self):
        sub = Subscription.objects.create(
            name="Rounded Multi-Year",
            supplier=self.supplier,
            renewal_cost=Decimal("1000.55"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=36,
        )
        # 1000.55 * 12 / 36 = 333.5166... -> 333.52 at two decimal places.
        self.assertEqual(sub.annual_cost, Decimal("333.52"))

    def test_subscription_annual_cost_multi_year_without_term_is_none(self):
        """Defined fallback: no yearly figure when the term is unknown."""
        sub = Subscription.objects.create(
            name="Termless Multi-Year",
            supplier=self.supplier,
            renewal_cost=Decimal("3600.00"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
        )
        self.assertIsNone(sub.annual_cost)

    def test_subscription_annual_cost_one_time_is_not_annualized(self):
        sub = Subscription.objects.create(
            name="Perpetual Software",
            supplier=self.supplier,
            renewal_cost=Decimal("500.00"),
            billing_cycle=BillingCycleChoices.ONETIME,
        )
        self.assertIsNone(sub.annual_cost)

    def test_subscription_annual_cost_one_time_with_term_stays_out_of_annual_slot(self):
        sub = Subscription.objects.create(
            name="One-Time With Term",
            supplier=self.supplier,
            renewal_cost=Decimal("500.00"),
            billing_cycle=BillingCycleChoices.ONETIME,
            term_months=12,
        )
        self.assertIsNone(sub.annual_cost)

    def test_subscription_annual_cost_zero_cost_stays_zero(self):
        """A recorded 0 is a real figure (free), not a missing value."""
        zero_monthly = Subscription.objects.create(
            name="Free Monthly Plan",
            supplier=self.supplier,
            renewal_cost=Decimal("0.00"),
            billing_cycle=BillingCycleChoices.MONTHLY,
        )
        zero_multi_year = Subscription.objects.create(
            name="Free Multi-Year Plan",
            supplier=self.supplier,
            renewal_cost=Decimal("0.00"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=36,
        )
        self.assertEqual(zero_monthly.annual_cost, Decimal("0.00"))
        self.assertEqual(zero_multi_year.annual_cost, Decimal("0.00"))

    def test_subscription_annual_cost_is_scope_independent(self):
        """Issue #501: the figure derives from instance fields only.

        Single-tenant, tenant-group, and All-accessible scopes must not change
        the displayed annual cost.
        """
        sub = Subscription.objects.create(
            name="Scoped Multi-Year",
            supplier=self.supplier,
            renewal_cost=Decimal("3600.00"),
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=36,
        )
        group = TenantGroup.objects.create(name="Scope Group", slug="scope-group")
        tenant = Tenant.objects.create(name="Scope Tenant", slug="scope-tenant", group=group)
        expected = Decimal("1200.00")

        for tenant_value, group_value, all_accessible in (
            (None, None, False),
            (tenant, None, False),
            (None, group, False),
            (None, None, True),
        ):
            set_current_tenant(tenant_value)
            set_current_tenant_group(group_value)
            set_current_all_accessible(all_accessible)
            try:
                self.assertEqual(sub.annual_cost, expected)
            finally:
                set_current_tenant(None)
                set_current_tenant_group(None)
                set_current_all_accessible(False)

    def test_subscription_days_until_renewal_none(self):
        sub = Subscription.objects.create(
            name="No Renewal",
            supplier=self.supplier,
        )
        self.assertIsNone(sub.days_until_renewal)

    def test_subscription_slug_auto_generation(self):
        sub = Subscription.objects.create(
            name="Adobe Creative Cloud - All Apps",
            supplier=self.supplier,
        )
        self.assertEqual(sub.slug, "adobe-creative-cloud-all-apps")

    def test_subscription_status_choices(self):
        self.assertEqual(
            list(SubscriptionStatusChoices.values),
            ["active", "suspended", "cancelled", "expired"],
        )

    def test_vendor_contract_auto_renews_is_canonical_with_legacy_model_alias(self):
        field_names = {field.name for field in Subscription._meta.fields}
        self.assertIn("vendor_contract_auto_renews", field_names)
        self.assertNotIn("auto_renewal", field_names)

        sub = Subscription.objects.create(
            name="Declarative Renewal Policy",
            supplier=self.supplier,
            vendor_contract_auto_renews=False,
        )
        self.assertIs(sub.vendor_contract_auto_renews, False)
        self.assertIs(sub.auto_renewal, False)

        sub.auto_renewal = True
        self.assertIs(sub.vendor_contract_auto_renews, True)

    def test_cancelled_subscription_cannot_be_renewed_back_to_active(self):
        sub = Subscription.objects.create(
            name="Cancelled Contract",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.CANCELLED,
        )

        with self.assertRaisesMessage(ValidationError, "cancelled to active"):
            sub.renew(timezone.now().date() + datetime.timedelta(days=365))

        sub.refresh_from_db()
        self.assertEqual(sub.status, SubscriptionStatusChoices.CANCELLED)

    def test_lifecycle_action_retries_are_idempotent(self):
        sub = Subscription.objects.create(name="Retry Contract", supplier=self.supplier)
        sub.suspend()
        suspended_at = sub.updated_at
        sub.suspend()
        sub.refresh_from_db()
        self.assertEqual(sub.updated_at, suspended_at)

        sub.resume()
        sub.cancel(reason="Retired")
        cancelled_at = sub.updated_at
        notes = sub.notes
        sub.cancel(reason="Retired")
        sub.refresh_from_db()
        self.assertEqual(sub.updated_at, cancelled_at)
        self.assertEqual(sub.notes, notes)

    def test_clean_enforces_transition_matrix_but_allows_same_state_updates(self):
        sub = Subscription.objects.create(name="Clean Contract", supplier=self.supplier)
        sub.description = "Ordinary update"
        sub.full_clean()

        sub.status = SubscriptionStatusChoices.CANCELLED
        sub.full_clean()
        sub.save(update_fields=["status"])

        sub.status = SubscriptionStatusChoices.ACTIVE
        with self.assertRaisesMessage(ValidationError, "cancelled to active"):
            sub.full_clean()

    def test_clean_covers_every_source_target_pair(self):
        allowed = {
            SubscriptionStatusChoices.ACTIVE: {
                SubscriptionStatusChoices.ACTIVE,
                SubscriptionStatusChoices.SUSPENDED,
                SubscriptionStatusChoices.CANCELLED,
                SubscriptionStatusChoices.EXPIRED,
            },
            SubscriptionStatusChoices.SUSPENDED: {
                SubscriptionStatusChoices.SUSPENDED,
                SubscriptionStatusChoices.ACTIVE,
                SubscriptionStatusChoices.CANCELLED,
                SubscriptionStatusChoices.EXPIRED,
            },
            SubscriptionStatusChoices.EXPIRED: {
                SubscriptionStatusChoices.EXPIRED,
                SubscriptionStatusChoices.ACTIVE,
                SubscriptionStatusChoices.CANCELLED,
            },
            SubscriptionStatusChoices.CANCELLED: {SubscriptionStatusChoices.CANCELLED},
        }
        for source in SubscriptionStatusChoices.values:
            for target in SubscriptionStatusChoices.values:
                with self.subTest(source=source, target=target):
                    sub = Subscription.objects.create(
                        name=f"Matrix {source} {target}", supplier=self.supplier, status=source
                    )
                    sub.status = target
                    if target in allowed[source]:
                        sub.full_clean()
                    else:
                        with self.assertRaises(ValidationError):
                            sub.full_clean()

    def test_clean_enforces_transition_under_a_foreign_ambient_tenant(self):
        tenant_a = baker.make(Tenant, name="Lifecycle A", slug="lifecycle-a")
        tenant_b = baker.make(Tenant, name="Lifecycle B", slug="lifecycle-b")
        sub = Subscription.objects.create(
            name="Ambient Contract",
            supplier=self.supplier,
            tenant=tenant_a,
            status=SubscriptionStatusChoices.CANCELLED,
        )
        set_current_tenant(tenant_b)
        try:
            sub.status = SubscriptionStatusChoices.ACTIVE
            with self.assertRaises(ValidationError):
                sub.full_clean()
        finally:
            set_current_tenant(None)

    def test_subscription_billing_cycle_choices(self):
        self.assertEqual(BillingCycleChoices.MONTHLY, "monthly")
        self.assertEqual(BillingCycleChoices.ANNUAL, "annual")
        self.assertEqual(BillingCycleChoices.BIANNUAL, "biannual")

    def test_subscription_tenant(self):
        tg = TenantGroup.objects.create(name="Group", slug="group")
        tenant = Tenant.objects.create(name="Tenant Inc.", slug="tenant-inc", group=tg)
        sub = Subscription.objects.create(
            name="Tenant Sub",
            supplier=self.supplier,
            tenant=tenant,
        )
        self.assertEqual(sub.tenant, tenant)


class SubscriptionAssignmentModelTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name="Microsoft", account_id="ms-001")
        self.subscription = Subscription.objects.create(
            name="M365 E5",
            supplier=self.supplier,
            licensed_quantity=100,
        )
        self.tg = TenantGroup.objects.create(name="G", slug="g")
        self.tenant = Tenant.objects.create(name="Tenant", slug="tenant", group=self.tg)
        self.subscription.tenant = self.tenant
        self.subscription.save(update_fields=["tenant"])
        self.site = Site.objects.create(name="Office", slug="office", tenant=self.tenant)
        self.location = Location.objects.create(name="Room 101", slug="room-101", site=self.site, tenant=self.tenant)
        self.asset = Asset.objects.create(
            name="Test Asset",
            asset_tag="TAG-001",
            serial_number="SN-001",
            purchase_cost=100,
            location=self.location,
            tenant=self.tenant,
        )
        self.user = get_user_model().objects.create_user(username="assigner", password="testpass123")

    def test_assignment_to_asset(self):
        ct = ContentType.objects.get_for_model(Asset)
        assignment = SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ct,
            object_id=self.asset.pk,
            assigned_by=self.user,
        )
        self.assertEqual(str(assignment), f"Subscription {self.subscription} -> {self.asset}")

    def test_assignment_unique_constraint(self):
        ct = ContentType.objects.get_for_model(Asset)
        SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ct,
            object_id=self.asset.pk,
        )
        with self.assertRaises(IntegrityError):
            SubscriptionAssignment.objects.create(
                subscription=self.subscription,
                content_type=ct,
                object_id=self.asset.pk,
            )

    def test_assignment_to_asset_holder(self):
        holder = AssetHolder.objects.create(
            first_name="John", last_name="Doe", upn="john.doe", email="john@test.com", tenant=self.tenant
        )
        ct = ContentType.objects.get_for_model(AssetHolder)
        assignment = SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ct,
            object_id=holder.pk,
        )
        self.assertIn("John Doe", str(assignment))

    def test_assignment_absolute_url_falls_back_to_subscription(self):
        ct = ContentType.objects.get_for_model(Asset)
        assignment = SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ct,
            object_id=self.asset.pk,
        )
        url = assignment.get_absolute_url()
        self.assertIn(str(self.subscription.pk), url)

    def test_assignment_model_rejects_cross_tenant_gfk_target(self):
        foreign_tenant = Tenant.objects.create(name="Foreign", slug="foreign", group=self.tg)
        foreign_asset = baker.make(Asset, tenant=foreign_tenant)
        assignment = SubscriptionAssignment(
            subscription=self.subscription,
            content_type=ContentType.objects.get_for_model(Asset),
            object_id=foreign_asset.pk,
        )

        with self.assertRaisesMessage(ValidationError, "subscription tenant"):
            assignment.full_clean()

    def test_assignment_hides_target_that_later_moves_to_another_tenant(self):
        holder = baker.make(AssetHolder, tenant=self.tenant)
        assignment = SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ContentType.objects.get_for_model(AssetHolder),
            object_id=holder.pk,
        )
        self.assertEqual(assignment.assigned_object, holder)
        foreign = Tenant.objects.create(name="Moved", slug="moved", group=self.tg)
        AssetHolder._base_manager.filter(pk=holder.pk).update(tenant=foreign)

        self.assertIsNone(assignment.tenant_safe_assigned_object)
        self.assertIn("unlinked", str(assignment))

    def test_assignment_rejects_and_hides_a_soft_deleted_target(self):
        assignment = SubscriptionAssignment.objects.create(
            subscription=self.subscription,
            content_type=ContentType.objects.get_for_model(Asset),
            object_id=self.asset.pk,
        )

        # Soft-deleting the target also archives the assignment that covers it
        # (the generic relation is a CASCADE child), which is a change-logged
        # write: run it inside the audit context, like every other write path.
        with TaskContext(tenant_id=self.tenant.pk, user_id=None):
            self.asset.delete()

        self.assertIsNone(assignment.tenant_safe_assigned_object)
        self.assertIn("unlinked", str(assignment))
        with self.assertRaisesMessage(ValidationError, "does not exist"):
            assignment.full_clean()


class SubscriptionExplicitExpiryTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name="Test Supplier")
        self.yesterday = timezone.now().date() - datetime.timedelta(days=1)

    def test_save_does_not_silently_expire_and_explicit_action_does(self):
        sub = Subscription.objects.create(
            name="Should Expire",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=self.yesterday,
        )
        sub.save()
        self.assertEqual(sub.status, SubscriptionStatusChoices.ACTIVE)
        sub.expire()
        self.assertEqual(sub.status, SubscriptionStatusChoices.EXPIRED)

    def test_explicit_action_is_not_required_for_future_renewal(self):
        future = timezone.now().date() + datetime.timedelta(days=30)
        sub = Subscription.objects.create(
            name="Future Renewal",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=future,
        )
        sub.save()
        self.assertEqual(sub.status, SubscriptionStatusChoices.ACTIVE)


@pytest.mark.serial_only
class SubscriptionConcurrencyTests(TransactionTestCase):
    def _post_teardown(self):
        super()._post_teardown()
        ContentType.objects.clear_cache()

    def test_stale_direct_writer_cannot_overwrite_a_terminal_cancellation(self):
        supplier = Supplier.objects.create(name="Race Supplier")
        subscription = Subscription.objects.create(
            name="Race Contract", supplier=supplier, status=SubscriptionStatusChoices.ACTIVE
        )
        stale_loaded = threading.Event()
        allow_stale_save = threading.Event()
        stale_errors = []

        def stale_writer():
            close_old_connections()
            try:
                stale = Subscription._base_manager.get(pk=subscription.pk)
                stale_loaded.set()
                allow_stale_save.wait(timeout=10)
                stale.status = SubscriptionStatusChoices.SUSPENDED
                stale.save()
            except ValidationError as exc:
                stale_errors.append(exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=stale_writer)
        thread.start()
        self.assertTrue(stale_loaded.wait(timeout=10))

        current = Subscription._base_manager.get(pk=subscription.pk)
        current.cancel(timezone.now().date(), reason="terminal winner")
        allow_stale_save.set()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(stale_errors), 1)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatusChoices.CANCELLED)

    def test_stale_retried_cancellation_cannot_replace_the_winning_effects(self):
        supplier = Supplier.objects.create(name="Cancellation Race Supplier")
        subscription = Subscription.objects.create(name="Cancellation Race", supplier=supplier)
        stale_loaded = threading.Event()
        winner_committed = threading.Event()
        errors = []

        def stale_cancellation():
            close_old_connections()
            stale = Subscription._base_manager.get(pk=subscription.pk)
            stale_loaded.set()
            winner_committed.wait(timeout=10)
            try:
                stale.cancel(datetime.date(2026, 2, 2), "loser")
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=stale_cancellation)
        thread.start()
        self.assertTrue(stale_loaded.wait(timeout=10))

        winner = Subscription._base_manager.get(pk=subscription.pk)
        winner.cancel(datetime.date(2026, 1, 1), "winner")
        winner_committed.set()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        subscription.refresh_from_db()
        self.assertEqual(subscription.cancellation_date, datetime.date(2026, 1, 1))
        self.assertIn("winner", subscription.notes)
        self.assertNotIn("loser", subscription.notes)

    def test_stale_direct_same_status_writer_cannot_replace_the_winning_effects(self):
        supplier = Supplier.objects.create(name="Direct cancellation race supplier")
        subscription = Subscription.objects.create(name="Direct cancellation race", supplier=supplier)
        loaded = threading.Event()
        allow_stale_save = threading.Event()
        errors = []

        def stale_writer():
            close_old_connections()
            stale = Subscription._base_manager.get(pk=subscription.pk)
            stale.status = SubscriptionStatusChoices.CANCELLED
            stale.cancellation_date = datetime.date(2026, 2, 2)
            stale.notes = "loser"
            loaded.set()
            allow_stale_save.wait(timeout=10)
            try:
                stale.save()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=stale_writer)
        thread.start()
        self.assertTrue(loaded.wait(timeout=10))

        winner = Subscription._base_manager.get(pk=subscription.pk)
        winner.cancel(cancellation_date=datetime.date(2026, 1, 1), reason="winner")
        allow_stale_save.set()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatusChoices.CANCELLED)
        self.assertEqual(subscription.cancellation_date, datetime.date(2026, 1, 1))
        self.assertIn("winner", subscription.notes)
        self.assertNotIn("loser", subscription.notes)

    def test_concurrent_reminder_delivery_is_idempotent(self):
        from subscriptions.tasks import _notify_once

        user = User.objects.create_user(username="notification-race")
        tenant_id = Tenant.objects.create(name="Notification race", slug="notification-race").pk
        barrier = threading.Barrier(2)
        errors = []

        def notify():
            close_old_connections()
            thread_user = User._base_manager.get(pk=user.pk)
            try:
                barrier.wait(timeout=10)
                _notify_once(
                    user=thread_user,
                    tenant_id=tenant_id,
                    subject="Renewal reminder",
                    message="Same daily effect",
                    target_url="/subscriptions/1/",
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=notify) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(Notification.objects.filter(user=user, subject="Renewal reminder").count(), 1)

    def test_concurrent_renew_prevents_expiry(self):
        today = timezone.localdate()
        subscription = Subscription.objects.create(
            name="Expiry race sub",
            supplier=Supplier.objects.create(name="Expiry race supplier"),
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=today - datetime.timedelta(days=1),
        )
        loaded = threading.Event()
        allow_expire = threading.Event()
        errors = []

        def expirer():
            close_old_connections()
            candidate = Subscription._base_manager.get(pk=subscription.pk)
            loaded.set()
            allow_expire.wait(timeout=10)
            try:
                candidate.expire()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                close_old_connections()

        t = threading.Thread(target=expirer)
        t.start()
        self.assertTrue(loaded.wait(timeout=10))

        winner = Subscription._base_manager.get(pk=subscription.pk)
        future = today + datetime.timedelta(days=365)
        winner.renew(future)
        allow_expire.set()
        t.join(timeout=10)

        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, SubscriptionStatusChoices.ACTIVE)
        self.assertEqual(subscription.renewal_date, future)
