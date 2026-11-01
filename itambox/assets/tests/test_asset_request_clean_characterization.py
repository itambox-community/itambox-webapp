"""Characterization tests for ``AssetRequest.clean``.

Each test pins one branch of the validation as it behaves today: which
combination is rejected, with which exact message, and which check wins when
several would fail. They exist so ``clean`` can be decomposed without changing
behaviour; a refactor must leave every assertion here untouched.
"""

from unittest.mock import PropertyMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from assets.choices import RequestStatusChoices
from assets.models import Asset, AssetRequest, AssetRole, AssetType, Category, Manufacturer, StatusLabel
from core.managers import set_current_tenant
from inventory.models import Accessory, Component, Consumable
from organization.models import AssetHolder, Location, Site, Tenant

User = get_user_model()

S = RequestStatusChoices


class AssetRequestCleanFixtureMixin:
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Clean Corp", slug="clean-corp")
        self.other_tenant = Tenant.objects.create(name="Other Corp", slug="other-corp")
        set_current_tenant(self.tenant)

        self.requester = User.objects.create_user(username="clean-requester", password="x")
        self.second_requester = User.objects.create_user(username="clean-second", password="x")

        self.manufacturer = Manufacturer.objects.create(name="CleanMaker", slug="cleanmaker")
        self.role = AssetRole.objects.create(name="Clean Laptop", slug="clean-laptop")
        self.deployable = StatusLabel.objects.create(
            name="Clean Deployable", slug="clean-deployable", type=StatusLabel.TYPE_DEPLOYABLE
        )
        self.in_repair = StatusLabel.objects.create(name="Clean Repair", slug="clean-repair", type="in_repair")

        self.type_requestable = AssetType.objects.create(
            manufacturer=self.manufacturer, model="Req 1", slug="clean-req-1", requestable=True, asset_role=self.role
        )
        self.type_other = AssetType.objects.create(
            manufacturer=self.manufacturer, model="Req 2", slug="clean-req-2", requestable=True, asset_role=self.role
        )
        self.type_not_requestable = AssetType.objects.create(
            manufacturer=self.manufacturer, model="NoReq", slug="clean-noreq", requestable=False, asset_role=self.role
        )

        self.asset = self._asset("Clean-001", "CLEAN-001")
        self.asset_two = self._asset("Clean-002", "CLEAN-002")
        self.asset_not_requestable = self._asset("Clean-003", "CLEAN-003", requestable=False)
        self.asset_in_repair = self._asset("Clean-004", "CLEAN-004", status=self.in_repair)

        cat = Category.objects.create(
            name="Clean Inventory",
            slug="clean-inventory",
            applies_to={"component": True, "accessory": True, "consumable": True},
        )
        self.component = Component.objects.create(name="Clean RAM", manufacturer=self.manufacturer, category=cat)
        self.accessory = Accessory.objects.create(name="Clean Dock", manufacturer=self.manufacturer, category=cat)
        self.consumable = Consumable.objects.create(name="Clean Toner", manufacturer=self.manufacturer, category=cat)

        self.holder = AssetHolder.objects.create(
            first_name="Clean", last_name="Holder", upn="clean.holder@example.com", tenant=self.tenant
        )
        site = Site.objects.create(name="Clean Site", slug="clean-site", tenant=self.tenant)
        self.location = Location.objects.create(name="Clean Room", slug="clean-room", site=site, tenant=self.tenant)

    def _asset(self, name, tag, requestable=True, status=None, asset_type=None):
        return Asset.objects.create(
            name=name,
            asset_tag=tag,
            asset_type=asset_type or self.type_requestable,
            asset_role=self.role,
            status=status or self.deployable,
            requestable=requestable,
            tenant=self.tenant,
        )

    def _new(self, **kwargs):
        kwargs.setdefault("requester", self.requester)
        kwargs.setdefault("tenant", self.tenant)
        return AssetRequest(**kwargs)

    def assertCleanRejects(self, request, message):
        with self.assertRaises(ValidationError) as cm:
            request.clean()
        self.assertEqual(cm.exception.messages, [message])

    def _persisted(self, status, **kwargs):
        """A stored request whose persisted status is forced to ``status``.

        The status is written with ``QuerySet.update`` so the fixture does not
        itself walk the state machine under test.
        """
        kwargs.setdefault("component", self.component)
        req = AssetRequest.objects.create(requester=self.requester, tenant=self.tenant, **kwargs)
        AssetRequest._base_manager.filter(pk=req.pk).update(status=status)
        req.status = status
        return req


class AssetRequestCleanStatusTransitionTests(AssetRequestCleanFixtureMixin, TestCase):
    VALID = {
        S.PENDING: {S.APPROVED, S.DENIED, S.CANCELLED, S.FULFILLED},
        S.APPROVED: {S.FULFILLED, S.CANCELLED, S.PROCUREMENT},
        S.PROCUREMENT: {S.FULFILLED, S.CANCELLED, S.APPROVED},
        S.DENIED: set(),
        S.FULFILLED: set(),
        S.CANCELLED: set(),
    }

    def test_every_status_pair_follows_the_transition_table(self):
        for old in S.values:
            for new in S.values:
                if old == new:
                    continue
                with self.subTest(old=old, new=new):
                    req = self._persisted(old)
                    req.status = new
                    if new in self.VALID[old]:
                        req.clean()
                    else:
                        self.assertCleanRejects(req, f"Invalid state transition from {old} to {new}.")
                    AssetRequest._base_manager.filter(pk=req.pk).delete()

    def test_unchanged_status_is_not_a_transition(self):
        for status in S.values:
            with self.subTest(status=status):
                req = self._persisted(status)
                req.clean()
                AssetRequest._base_manager.filter(pk=req.pk).delete()

    def test_unknown_persisted_status_allows_no_transition(self):
        req = self._persisted("bogus")
        req.status = S.APPROVED
        self.assertCleanRejects(req, "Invalid state transition from bogus to approved.")

    def test_missing_row_has_no_previous_state(self):
        req = self._new(component=self.component, status=S.FULFILLED)
        req.pk = 987654321
        req.clean()

    def test_soft_deleted_row_still_supplies_the_previous_state(self):
        req = self._persisted(S.DENIED)
        AssetRequest._base_manager.filter(pk=req.pk).update(deleted_at="2026-01-01T00:00:00Z")
        req.status = S.APPROVED
        self.assertCleanRejects(req, "Invalid state transition from denied to approved.")

    def test_transition_lookup_ignores_the_active_tenant(self):
        req = self._persisted(S.FULFILLED)
        set_current_tenant(self.other_tenant)
        req.status = S.PENDING
        self.assertCleanRejects(req, "Invalid state transition from fulfilled to pending.")

    def test_transition_error_wins_over_item_selection_errors(self):
        req = self._persisted(S.CANCELLED)
        req.component = None
        req.qty = 0
        req.status = S.PENDING
        self.assertCleanRejects(req, "Invalid state transition from cancelled to pending.")


class AssetRequestCleanItemSelectionTests(AssetRequestCleanFixtureMixin, TestCase):
    MISSING = "Select the item you are requesting (Asset, Asset Type, Component, Accessory, or Consumable)."
    MULTIPLE = "A request can contain only one item type."

    def test_no_item_is_rejected(self):
        self.assertCleanRejects(self._new(), self.MISSING)

    def test_no_item_is_rejected_on_update_too(self):
        req = self._persisted(S.PENDING)
        req.component = None
        self.assertCleanRejects(req, self.MISSING)

    def test_each_single_category_is_accepted(self):
        for kwargs in (
            {"asset": self.asset},
            {"asset_type": self.type_requestable},
            {"asset": self.asset, "asset_type": self.type_requestable},
            {"component": self.component},
            {"accessory": self.accessory},
            {"consumable": self.consumable},
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                self._new(**kwargs).clean()

    def test_every_pair_of_categories_is_rejected(self):
        items = {
            "asset": {"asset": self.asset},
            "asset_type": {"asset_type": self.type_requestable},
            "component": {"component": self.component},
            "accessory": {"accessory": self.accessory},
            "consumable": {"consumable": self.consumable},
        }
        names = list(items)
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                if {first, second} == {"asset", "asset_type"}:
                    continue  # one category: asset + its type
                with self.subTest(first=first, second=second):
                    self.assertCleanRejects(self._new(**items[first], **items[second]), self.MULTIPLE)

    def test_all_categories_at_once_are_rejected(self):
        req = self._new(
            asset=self.asset,
            asset_type=self.type_requestable,
            component=self.component,
            accessory=self.accessory,
            consumable=self.consumable,
        )
        self.assertCleanRejects(req, self.MULTIPLE)

    def test_missing_item_wins_over_zero_quantity(self):
        self.assertCleanRejects(self._new(qty=0), self.MISSING)

    def test_multiple_items_win_over_zero_quantity(self):
        self.assertCleanRejects(self._new(component=self.component, accessory=self.accessory, qty=0), self.MULTIPLE)

    def test_zero_quantity_is_rejected(self):
        self.assertCleanRejects(
            self._new(component=self.component, qty=0), "Requested quantity must be greater than zero."
        )

    def test_zero_quantity_is_rejected_on_update_too(self):
        req = self._persisted(S.PENDING)
        req.qty = 0
        self.assertCleanRejects(req, "Requested quantity must be greater than zero.")

    def test_quantity_greater_than_one_is_accepted(self):
        self._new(component=self.component, qty=5).clean()


class AssetRequestCleanNewRequestItemTests(AssetRequestCleanFixtureMixin, TestCase):
    def test_non_requestable_asset_is_rejected(self):
        self.assertCleanRejects(
            self._new(asset=self.asset_not_requestable),
            "The asset 'Clean-003 (CLEAN-003)' is not requestable.",
        )

    def test_asset_inheriting_requestability_from_its_type(self):
        inherits_no = self._asset("Clean-005", "CLEAN-005", requestable=None, asset_type=self.type_not_requestable)
        self.assertCleanRejects(self._new(asset=inherits_no), "The asset 'Clean-005 (CLEAN-005)' is not requestable.")
        inherits_yes = self._asset("Clean-006", "CLEAN-006", requestable=None)
        self._new(asset=inherits_yes).clean()

    def test_disposed_asset_is_rejected(self):
        with patch.object(Asset, "is_disposed", new_callable=PropertyMock, return_value=True):
            self.assertCleanRejects(
                self._new(asset=self.asset),
                "The asset 'Clean-001 (CLEAN-001)' is disposed and cannot be requested.",
            )

    def test_non_requestable_wins_over_disposed(self):
        with patch.object(Asset, "is_disposed", new_callable=PropertyMock, return_value=True):
            self.assertCleanRejects(
                self._new(asset=self.asset_not_requestable),
                "The asset 'Clean-003 (CLEAN-003)' is not requestable.",
            )

    def test_non_requestable_asset_type_is_rejected(self):
        self.assertCleanRejects(
            self._new(asset_type=self.type_not_requestable),
            "The asset type 'CleanMaker NoReq' is not requestable.",
        )

    def test_disposed_wins_over_non_requestable_asset_type(self):
        override = self._asset("Clean-007", "CLEAN-007", asset_type=self.type_not_requestable)
        with patch.object(Asset, "is_disposed", new_callable=PropertyMock, return_value=True):
            self.assertCleanRejects(
                self._new(asset=override, asset_type=self.type_not_requestable),
                "The asset 'Clean-007 (CLEAN-007)' is disposed and cannot be requested.",
            )

    def test_requestable_asset_of_non_requestable_type_with_that_type_is_rejected_by_type(self):
        override = self._asset("Clean-008", "CLEAN-008", asset_type=self.type_not_requestable)
        self.assertCleanRejects(
            self._new(asset=override, asset_type=self.type_not_requestable),
            "The asset type 'CleanMaker NoReq' is not requestable.",
        )

    def test_requestable_asset_of_non_requestable_type_alone_is_accepted(self):
        override = self._asset("Clean-009", "CLEAN-009", asset_type=self.type_not_requestable)
        self._new(asset=override).clean()

    def test_non_issuable_asset_is_rejected_with_its_status(self):
        self.assertCleanRejects(
            self._new(asset=self.asset_in_repair),
            "The asset 'Clean-004 (CLEAN-004)' is currently not available (Status: Clean Repair).",
        )

    def test_non_requestable_type_wins_over_non_issuable_asset(self):
        override = self._asset("Clean-010", "CLEAN-010", asset_type=self.type_not_requestable, status=self.in_repair)
        self.assertCleanRejects(
            self._new(asset=override, asset_type=self.type_not_requestable),
            "The asset type 'CleanMaker NoReq' is not requestable.",
        )

    def test_non_issuable_wins_over_asset_type_mismatch(self):
        self.assertCleanRejects(
            self._new(asset=self.asset_in_repair, asset_type=self.type_other),
            "The asset 'Clean-004 (CLEAN-004)' is currently not available (Status: Clean Repair).",
        )

    def test_item_availability_checks_are_skipped_for_stored_requests(self):
        req = self._persisted(S.PENDING, component=None, asset=self.asset)
        Asset.objects.filter(pk=self.asset.pk).update(requestable=False, status=self.in_repair)
        req.asset = Asset.objects.get(pk=self.asset.pk)
        req.clean()
        AssetType.objects.filter(pk=self.type_requestable.pk).update(requestable=False)
        type_req = self._persisted(S.PENDING, component=None, asset_type=self.type_requestable)
        type_req.asset_type = AssetType.objects.get(pk=self.type_requestable.pk)
        type_req.clean()

    def test_disposal_check_is_skipped_for_stored_requests(self):
        req = self._persisted(S.PENDING, component=None, asset=self.asset)
        with patch.object(Asset, "is_disposed", new_callable=PropertyMock, return_value=True):
            req.clean()


class AssetRequestCleanAssetTypeMismatchTests(AssetRequestCleanFixtureMixin, TestCase):
    MISMATCH = "The selected asset does not match the requested asset type."

    def test_mismatched_asset_and_type_are_rejected(self):
        self.assertCleanRejects(self._new(asset=self.asset, asset_type=self.type_other), self.MISMATCH)

    def test_mismatch_is_checked_on_update_too(self):
        req = self._persisted(S.PENDING, component=None, asset=self.asset, asset_type=self.type_requestable)
        req.asset_type = self.type_other
        self.assertCleanRejects(req, self.MISMATCH)

    def test_duplicate_wins_over_mismatch(self):
        AssetRequest.objects.create(requester=self.requester, tenant=self.tenant, asset=self.asset)
        self.assertCleanRejects(
            self._new(asset=self.asset, asset_type=self.type_other),
            "You already have an open request for the asset 'Clean-001 (CLEAN-001)'.",
        )

    def test_matching_asset_and_type_are_accepted(self):
        self._new(asset=self.asset, asset_type=self.type_requestable).clean()


class AssetRequestCleanDuplicateTests(AssetRequestCleanFixtureMixin, TestCase):
    def _open(self, status=S.PENDING, requester=None, **kwargs):
        req = AssetRequest.objects.create(requester=requester or self.requester, tenant=self.tenant, **kwargs)
        if status != S.PENDING:
            AssetRequest._base_manager.filter(pk=req.pk).update(status=status)
        return req

    def _cases(self):
        return (
            ({"asset": self.asset}, "You already have an open request for the asset 'Clean-001 (CLEAN-001)'."),
            (
                {"asset_type": self.type_requestable},
                "You already have an open request for the asset type 'CleanMaker Req 1'.",
            ),
            (
                {"component": self.component},
                "You already have an open request for the component 'CleanMaker Clean RAM'.",
            ),
            (
                {"accessory": self.accessory},
                "You already have an open request for the accessory 'CleanMaker Clean Dock'.",
            ),
            (
                {"consumable": self.consumable},
                "You already have an open request for the consumable 'CleanMaker Clean Toner'.",
            ),
        )

    def test_open_duplicate_is_rejected_per_category_and_open_status(self):
        for kwargs, message in self._cases():
            for status in (S.PENDING, S.APPROVED, S.PROCUREMENT):
                with self.subTest(item=sorted(kwargs), status=status):
                    existing = self._open(status=status, **kwargs)
                    self.assertCleanRejects(self._new(**kwargs), message)
                    AssetRequest._base_manager.filter(pk=existing.pk).delete()

    def test_closed_requests_do_not_count_as_duplicates(self):
        for kwargs, _message in self._cases():
            for status in (S.DENIED, S.FULFILLED, S.CANCELLED):
                with self.subTest(item=sorted(kwargs), status=status):
                    existing = self._open(status=status, **kwargs)
                    self._new(**kwargs).clean()
                    AssetRequest._base_manager.filter(pk=existing.pk).delete()

    def test_soft_deleted_requests_do_not_count_as_duplicates(self):
        existing = self._open(component=self.component)
        existing.delete()
        self._new(component=self.component).clean()

    def test_other_requesters_do_not_count_as_duplicates(self):
        for kwargs, _message in self._cases():
            with self.subTest(item=sorted(kwargs)):
                existing = self._open(requester=self.second_requester, **kwargs)
                self._new(**kwargs).clean()
                AssetRequest._base_manager.filter(pk=existing.pk).delete()

    def test_other_items_do_not_count_as_duplicates(self):
        self._open(asset=self.asset)
        self._new(asset=self.asset_two).clean()
        self._open(asset_type=self.type_requestable)
        self._new(asset_type=self.type_other).clean()

    def test_a_different_delegated_target_is_not_a_duplicate(self):
        self._open(component=self.component)
        for target in (
            {"assigned_user": self.holder},
            {"assigned_location": self.location},
            {"assigned_asset": self.asset},
        ):
            with self.subTest(target=sorted(target)):
                self._new(component=self.component, **target).clean()

    def test_the_same_delegated_target_is_a_duplicate(self):
        message = "You already have an open request for the component 'CleanMaker Clean RAM'."
        for target in (
            {"assigned_user": self.holder},
            {"assigned_location": self.location},
            {"assigned_asset": self.asset},
        ):
            with self.subTest(target=sorted(target)):
                existing = self._open(component=self.component, **target)
                self.assertCleanRejects(self._new(component=self.component, **target), message)
                AssetRequest._base_manager.filter(pk=existing.pk).delete()

    def test_asset_type_duplicate_ignores_requests_for_a_specific_asset(self):
        self._open(asset=self.asset, asset_type=self.type_requestable)
        self._new(asset_type=self.type_requestable).clean()

    def test_specific_asset_duplicate_matches_regardless_of_the_stored_type(self):
        self._open(asset=self.asset, asset_type=self.type_requestable)
        self.assertCleanRejects(
            self._new(asset=self.asset),
            "You already have an open request for the asset 'Clean-001 (CLEAN-001)'.",
        )

    def test_specific_asset_request_ignores_an_open_type_request(self):
        self._open(asset_type=self.type_requestable)
        self._new(asset=self.asset, asset_type=self.type_requestable).clean()

    def test_skip_flag_disables_the_duplicate_check(self):
        self._open(component=self.component)
        req = self._new(component=self.component)
        req._skip_duplicate_check = True
        req.clean()

    def test_false_skip_flag_keeps_the_duplicate_check(self):
        self._open(component=self.component)
        req = self._new(component=self.component)
        req._skip_duplicate_check = False
        self.assertCleanRejects(req, "You already have an open request for the component 'CleanMaker Clean RAM'.")

    def test_request_without_requester_skips_the_duplicate_check(self):
        self._open(component=self.component)
        AssetRequest(component=self.component, tenant=self.tenant).clean()

    def test_duplicates_in_another_tenant_are_outside_the_active_scope(self):
        set_current_tenant(self.other_tenant)
        AssetRequest.objects.create(requester=self.requester, tenant=self.other_tenant, component=self.component)
        set_current_tenant(self.tenant)
        self._new(component=self.component).clean()

    def test_duplicate_check_is_skipped_for_stored_requests(self):
        self._open(component=self.component)
        second = self._open(requester=self.second_requester, component=self.component)
        second.requester = self.requester
        second.clean()

    def test_item_availability_wins_over_duplicate(self):
        existing = self._open(asset=self.asset)
        Asset.objects.filter(pk=self.asset.pk).update(requestable=False)
        self.asset.refresh_from_db()
        self.assertCleanRejects(self._new(asset=self.asset), "The asset 'Clean-001 (CLEAN-001)' is not requestable.")
        self.assertTrue(AssetRequest.objects.filter(pk=existing.pk).exists())
