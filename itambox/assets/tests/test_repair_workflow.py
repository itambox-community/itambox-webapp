"""Repair with a loaner and its completion, in one step each (#644).

The repair maintenance anchors the story. Issuing the loaner and closing the
repair run through the existing lifecycle services (``checkout_asset``,
``checkin_asset``, ``dispose_asset``) in the same transaction as the maintenance
write, and both fail closed for an unavailable or foreign-tenant unit, for an
asset with no holder to lend to, and for an actor who may not perform the
operation.
"""

import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset, AssetAssignment, AssetMaintenance, AssetType, StatusLabel
from assets.models.lifecycle import AssetDisposal
from assets.services import active_repair_loan, checkout_asset, complete_repair, issue_repair_loaner
from compliance.forms import AssetMaintenanceForm
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Role, RoleGrantScope, Tenant, TenantGroup

User = get_user_model()


def _asset(name, tenant=None, status=None, **kwargs):
    return baker.make(
        Asset,
        name=name,
        status=status or baker.make(StatusLabel, type="deployable"),
        tenant=tenant,
        asset_type=baker.make(AssetType),
        **kwargs,
    )


def _maintenance(asset, **kwargs):
    defaults = dict(
        maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
        status="scheduled",
        start_date=datetime.date(2026, 1, 10),
        completion_date=datetime.date(2026, 1, 20),
    )
    defaults.update(kwargs)
    return AssetMaintenance.objects.create(asset=asset, **defaults)


class RepairServiceTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        baker.make(StatusLabel, type="deployed", name="Deployed")
        self.archived = baker.make(StatusLabel, type="archived", name="Archived")
        self.holder = baker.make(AssetHolder)
        self.asset = _asset("Failed Laptop")
        self.loaner = _asset("Loaner Laptop")
        # The failed unit is still with its holder when the repair is recorded,
        # which is how the flow knows who the loaner is issued to.
        checkout_asset(self.asset, holder=self.holder, user=self.user)
        self.maintenance = _maintenance(self.asset)

    def _loan(self):
        return issue_repair_loaner(self.maintenance, self.loaner, self.user)

    def test_loaner_is_checked_out_to_the_holder_and_linked(self):
        loan = self._loan()

        self.assertIsNotNone(loan)
        self.assertEqual(loan.asset_id, self.loaner.pk)
        self.assertEqual(loan.assigned_user_id, self.holder.pk)
        self.assertTrue(loan.is_loan)
        self.assertTrue(loan.is_active)
        self.assertEqual(loan.maintenance_id, self.maintenance.pk)
        self.assertEqual(loan.due_date, self.maintenance.completion_date)
        self.loaner.refresh_from_db()
        self.assertEqual(self.loaner.active_assignment.pk, loan.pk)

    def test_loaner_due_date_can_be_set_explicitly(self):
        due = datetime.date(2026, 2, 1)
        loan = issue_repair_loaner(self.maintenance, self.loaner, self.user, due_date=due)
        self.assertEqual(loan.due_date, due)

    def test_loaner_is_refused_without_a_holder(self):
        AssetAssignment.objects.filter(asset=self.asset, is_active=True).update(is_active=False)
        with self.assertRaisesMessage(ValidationError, "no holder"):
            self._loan()
        self.assertFalse(AssetAssignment.objects.filter(asset=self.loaner).exists())

    def test_loaner_of_another_tenant_is_refused(self):
        other_tenant = Tenant.objects.create(name="Other", slug="other")
        foreign = _asset("Foreign Loaner", tenant=other_tenant)
        with self.assertRaisesMessage(ValidationError, "same tenant"):
            issue_repair_loaner(self.maintenance, foreign, self.user)
        self.assertFalse(AssetAssignment.objects.filter(asset=foreign).exists())

    def test_unavailable_loaner_is_refused(self):
        checkout_asset(self.loaner, holder=baker.make(AssetHolder), user=self.user)
        with self.assertRaisesMessage(ValidationError, "Cannot check out an asset"):
            self._loan()
        self.assertFalse(AssetAssignment.objects.filter(asset=self.loaner, maintenance=self.maintenance).exists())

    def test_the_unit_under_repair_is_not_a_loaner(self):
        with self.assertRaisesMessage(ValidationError, "cannot be issued to itself"):
            issue_repair_loaner(self.maintenance, self.asset, self.user)

    def test_only_a_repair_can_issue_a_loaner(self):
        upgrade = _maintenance(self.asset, maintenance_type="upgrade")
        with self.assertRaisesMessage(ValidationError, "only be issued for a repair"):
            issue_repair_loaner(upgrade, self.loaner, self.user)

    def test_second_loaner_for_the_same_repair_is_refused(self):
        first = self._loan()
        second_loaner = _asset("Second Loaner")
        with self.assertRaisesMessage(ValidationError, "already has an open loaner"):
            issue_repair_loaner(self.maintenance, second_loaner, self.user)
        self.assertFalse(AssetAssignment.objects.filter(asset=second_loaner).exists())
        self.assertEqual(
            list(AssetAssignment.objects.filter(maintenance=self.maintenance, is_loan=True, is_active=True)),
            [first],
        )

    def test_database_allows_one_open_loan_per_maintenance(self):
        first = self._loan()
        second_loaner = _asset("Second Loaner")
        with self.assertRaises(IntegrityError), transaction.atomic():
            # bulk_create skips model validation, so only the database constraint answers
            AssetAssignment.objects.bulk_create(
                [
                    AssetAssignment(
                        asset=second_loaner,
                        assigned_user=self.holder,
                        is_loan=True,
                        is_active=True,
                        maintenance=self.maintenance,
                    )
                ]
            )
        first.refresh_from_db()
        self.assertTrue(first.is_active)

    def test_a_new_loaner_is_possible_after_the_first_loan_was_returned(self):
        self._loan()
        complete_repair(self.maintenance, "return", self.user)
        second = issue_repair_loaner(self.maintenance, _asset("Second Loaner"), self.user)
        self.assertTrue(second.is_active)

    def test_stale_second_completion_cannot_repeat_the_hand_back(self):
        self._loan()
        complete_repair(self.maintenance, "return", self.user)
        count = AssetAssignment.objects.filter(asset=self.asset).count()
        with self.assertRaisesMessage(ValidationError, "no open loaner"):
            complete_repair(self.maintenance, "return", self.user)
        self.assertEqual(AssetAssignment.objects.filter(asset=self.asset).count(), count)

    def test_returned_closes_the_loaner_and_hands_the_unit_back(self):
        loan = self._loan()
        previous_assignment = AssetAssignment.objects.get(asset=self.asset, is_active=True)

        complete_repair(self.maintenance, "return", self.user)

        loan.refresh_from_db()
        self.assertFalse(loan.is_active)
        self.assertFalse(AssetAssignment.objects.filter(asset=self.loaner, is_active=True).exists())
        current = AssetAssignment.objects.get(asset=self.asset, is_active=True)
        self.assertNotEqual(current.pk, previous_assignment.pk)
        self.assertEqual(current.assigned_user_id, self.holder.pk)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status.type, "deployed")

    def test_replace_keeps_the_unit_and_can_dispose_the_original(self):
        loan = self._loan()

        complete_repair(
            self.maintenance,
            "replace",
            self.user,
            disposal_method="recycle",
            disposal_date=datetime.date(2026, 1, 21),
        )

        loan.refresh_from_db()
        self.assertTrue(loan.is_active)
        self.assertFalse(loan.is_loan)
        self.assertIsNone(loan.due_date)
        disposal = AssetDisposal.objects.get(asset=self.asset)
        self.assertEqual(disposal.maintenance_id, self.maintenance.pk)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status_id, self.archived.pk)

    def test_do_nothing_leaves_both_units_alone(self):
        loan = self._loan()
        self.archived = complete_repair(self.maintenance, "none", self.user)
        self.assertIsNone(self.archived)
        loan.refresh_from_db()
        self.assertTrue(loan.is_active)
        self.assertTrue(loan.is_loan)
        self.assertFalse(AssetDisposal.objects.filter(asset=self.asset).exists())
        self.assertEqual(active_repair_loan(self.maintenance).pk, loan.pk)

    def test_completion_without_a_loaner_fails_closed(self):
        with self.assertRaisesMessage(ValidationError, "no open loaner"):
            complete_repair(self.maintenance, "return", self.user)

    def test_unknown_completion_action_is_refused(self):
        self._loan()
        with self.assertRaisesMessage(ValidationError, "Unknown repair completion action"):
            complete_repair(self.maintenance, "teleport", self.user)


class AssetMaintenanceRepairFormTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.request = RequestFactory().post("/")
        self.request.user = self.user
        baker.make(StatusLabel, type="deployed", name="Deployed")
        baker.make(StatusLabel, type="archived", name="Archived")
        self.holder = baker.make(AssetHolder)
        self.asset = _asset("Failed Laptop")
        self.loaner = _asset("Loaner Laptop")
        checkout_asset(self.asset, holder=self.holder, user=self.user)

    def _data(self, **overrides):
        data = {
            "asset": self.asset.pk,
            "maintenance_type": AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            "status": "scheduled",
            "start_date": "2026-01-10",
            "completion_date": "2026-01-20",
            "description": "",
            "notes": "",
            "supplier": "",
            "performed_by": "",
        }
        data.update(overrides)
        return data

    def _form(self, instance=None, **overrides):
        return AssetMaintenanceForm(data=self._data(**overrides), instance=instance, request=self.request)

    def test_one_submission_creates_the_repair_and_issues_the_loaner(self):
        form = self._form(loaner_asset=self.loaner.pk)
        self.assertTrue(form.is_valid(), form.errors)

        maintenance = form.save()

        self.assertEqual(maintenance.maintenance_type, AssetMaintenance.MAINTENANCE_TYPE_REPAIR)
        loan = active_repair_loan(maintenance)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.asset_id, self.loaner.pk)
        self.assertEqual(loan.assigned_user_id, self.holder.pk)
        self.assertTrue(loan.is_loan)
        self.assertEqual(loan.due_date, maintenance.completion_date)

    def test_loaner_due_date_field_overrides_the_completion_date(self):
        form = self._form(loaner_asset=self.loaner.pk, loaner_due_date="2026-02-05")
        self.assertTrue(form.is_valid(), form.errors)

        maintenance = form.save()

        self.assertEqual(active_repair_loan(maintenance).due_date, datetime.date(2026, 2, 5))

    def test_a_repair_without_a_loaner_creates_no_assignment(self):
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)

        maintenance = form.save()

        self.assertIsNone(active_repair_loan(maintenance))
        self.assertFalse(AssetAssignment.objects.filter(asset=self.loaner).exists())

    def test_a_refused_loaner_rolls_the_maintenance_back(self):
        """The unavailable unit refuses the whole submission: no half-written repair."""
        checkout_asset(self.loaner, holder=baker.make(AssetHolder), user=self.user)
        form = self._form(loaner_asset=self.loaner.pk)
        self.assertTrue(form.is_valid(), form.errors)

        with self.assertRaisesMessage(ValidationError, "Cannot check out an asset"):
            form.save()

        self.assertFalse(AssetMaintenance.objects.filter(asset=self.asset).exists())

    def test_the_loaner_section_is_not_offered_without_the_checkout_permission(self):
        """An action the actor may not perform is not offered, and a forged post is refused."""
        self.request.user = baker.make(User)
        unoffered = AssetMaintenanceForm(request=self.request)
        self.assertFalse(unoffered.offer_loaner)
        self.assertFalse(unoffered.offer_completion)

        forged = self._form(loaner_asset=self.loaner.pk)
        self.assertFalse(forged.is_valid())
        self.assertIn("A loaner can only be issued for a repair.", forged.errors["__all__"])

    def test_returned_completes_the_story_from_the_form(self):
        loan = issue_repair_loaner(_maintenance(self.asset), self.loaner, self.user)
        maintenance = loan.maintenance
        form = self._form(instance=maintenance, repair_action="return")
        self.assertTrue(form.offer_completion)
        self.assertTrue(form.is_valid(), form.errors)

        form.save()

        loan.refresh_from_db()
        self.assertFalse(loan.is_active)
        self.assertEqual(AssetAssignment.objects.get(asset=self.asset, is_active=True).assigned_user_id, self.holder.pk)

    def test_replace_with_disposal_from_the_form(self):
        loan = issue_repair_loaner(_maintenance(self.asset), self.loaner, self.user)
        maintenance = loan.maintenance
        form = self._form(
            instance=maintenance,
            repair_action="replace",
            dispose_original="on",
            disposal_method="recycle",
            disposal_date="2026-01-21",
        )
        self.assertTrue(form.is_valid(), form.errors)

        form.save()

        loan.refresh_from_db()
        self.assertFalse(loan.is_loan)
        self.assertEqual(AssetDisposal.objects.get(asset=self.asset).maintenance_id, maintenance.pk)

    def test_a_forged_disposal_without_the_permission_is_refused(self):
        """The disposal input is refused on its own, even with no completion action."""
        loan = issue_repair_loaner(_maintenance(self.asset), self.loaner, self.user)
        self.request.user = baker.make(User)
        form = AssetMaintenanceForm(
            data=self._data(dispose_original="on", disposal_method="recycle"),
            instance=loan.maintenance,
            request=self.request,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("You are not allowed to dispose of an asset.", form.errors["__all__"])


class RepairWorkflowViewTests(TenantTestMixin, TestCase):
    """The one-step flows through the real maintenance form endpoints."""

    def setUp(self):
        self.setup_tenant_context()
        self.user = baker.make(User, is_superuser=True, is_staff=True)
        self.client.force_login(self.user)
        self.deployed = baker.make(StatusLabel, type="deployed", name="Deployed")
        baker.make(StatusLabel, type="archived", name="Archived")
        self.holder = baker.make(AssetHolder, tenant=self.tenant)
        self.asset = _asset("View Failed", tenant=self.tenant)
        self.loaner = _asset("View Loaner", tenant=self.tenant)
        checkout_asset(self.asset, holder=self.holder, user=self.user)

    def _post_data(self, **overrides):
        data = {
            "asset": self.asset.pk,
            "maintenance_type": AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            "status": "scheduled",
            "start_date": "2026-01-10",
            "completion_date": "2026-01-20",
            "supplier": "",
            "performed_by": "",
            "description": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    def test_create_view_records_the_repair_with_its_loaner(self):
        response = self.client.post(
            reverse("assets:assetmaintenance_create"), data=self._post_data(loaner_asset=self.loaner.pk)
        )
        self.assertEqual(response.status_code, 302)
        maintenance = AssetMaintenance.objects.get(asset=self.asset)
        loan = active_repair_loan(maintenance)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.asset_id, self.loaner.pk)

    def test_create_view_rerenders_when_the_service_refuses(self):
        checkout_asset(self.loaner, holder=baker.make(AssetHolder, tenant=self.tenant), user=self.user)
        response = self.client.post(
            reverse("assets:assetmaintenance_create"), data=self._post_data(loaner_asset=self.loaner.pk)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssetMaintenance.objects.filter(asset=self.asset).exists())

    def test_log_repair_button_opens_the_prefilled_quick_add(self):
        response = self.client.get(
            reverse("assets:assetmaintenance_create") + f"?asset={self.asset.pk}&maintenance_type=repair"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["asset"], str(self.asset.pk))
        self.assertEqual(response.context["form"].initial["maintenance_type"], "repair")

    def test_edit_view_completes_the_repair(self):
        loan = issue_repair_loaner(_maintenance(self.asset), self.loaner, self.user)
        response = self.client.post(
            reverse("assets:assetmaintenance_update", kwargs={"pk": loan.maintenance.pk}),
            data=self._post_data(repair_action="return"),
        )
        self.assertEqual(response.status_code, 302)
        loan.refresh_from_db()
        self.assertFalse(loan.is_active)


class RepairLoanerScopeTests(TenantTestMixin, TestCase):
    """A foreign-tenant unit stays refused whatever scope the actor carries."""

    def setUp(self):
        self.setup_tenant_context()
        # The checkout service resolves the deployed label; a fresh database has none.
        baker.make(StatusLabel, type="deployed", name="Deployed")
        self.group = TenantGroup.objects.create(name="Group", slug="group")
        self.tenant.group = self.group
        self.tenant.save()
        self.other_tenant = Tenant.objects.create(name="Other", slug="other", group=self.group)
        self.user = baker.make(User, is_staff=True)
        self.holder = baker.make(AssetHolder, tenant=self.tenant)
        self.asset = _asset("Scoped Failed", tenant=self.tenant)
        self.foreign = _asset("Foreign Loaner", tenant=self.other_tenant)
        checkout_asset(self.asset, holder=self.holder, user=self.user)
        self.maintenance = _maintenance(self.asset)

    def _scoped_member(self, managed_scope=None, scope_group=None):
        role = Role.objects.create(
            tenant=self.tenant,
            name=f"Repair scope {managed_scope or 'own'}",
            permissions=["assets.change_asset", "assets.dispose_asset"],
        )
        grant_result = self.grant(
            self.user, self.tenant, role, reach="own", managed_scope=managed_scope, scope_group=scope_group
        )
        return grant_result.membership

    def test_single_tenant_scope_refuses_a_foreign_loaner(self):
        membership = self._scoped_member()
        with self.tenant_context(self.tenant, membership):
            with self.assertRaisesMessage(ValidationError, "same tenant"):
                issue_repair_loaner(self.maintenance, self.foreign, self.user)

    def test_all_accessible_scope_still_refuses_a_foreign_loaner(self):
        membership = self._scoped_member(managed_scope="all")
        with self.tenant_context(self.tenant, membership):
            with self.assertRaisesMessage(ValidationError, "same tenant"):
                issue_repair_loaner(self.maintenance, self.foreign, self.user)

    def test_tenant_group_scope_still_refuses_a_foreign_loaner(self):
        membership = self._scoped_member(managed_scope=RoleGrantScope.SCOPE_TENANT_GROUP, scope_group=self.group)
        with self.tenant_context(self.tenant, membership):
            with self.assertRaisesMessage(ValidationError, "same tenant"):
                issue_repair_loaner(self.maintenance, self.foreign, self.user)
