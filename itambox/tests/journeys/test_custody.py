"""Custody journeys: a receipt is accepted only by the person currently holding the asset."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from assets.models import AssetTagSequence
from assets.services import checkin_asset, checkout_asset
from compliance.models import CustodyReceipt, CustodyTemplate
from compliance.tests.signatures import signature_png

from .support import JourneyMixin

User = get_user_model()

SIGNATURE = signature_png("journey-signature-payload")


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class CustodyReceiptJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.make_tenant("journey-custody")
        category = baker.make("assets.Category", name="Journey Laptops", slug="journey-laptops")
        CustodyTemplate.objects.create(
            name="Journey EULA",
            tenant=self.tenant,
            category=category,
            require_acceptance=True,
            eula_text="journey eula",
        )
        asset_type = baker.make("assets.AssetType", model="JourneyBook", slug="journey-book", category=category)
        deployable = baker.make("assets.StatusLabel", type="deployable", name="Journey Deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Journey Deployed")
        self.asset = self.make_asset(asset_type=asset_type, asset_tag="JOURNEY-CUSTODY-1", status=deployable)

    def _sign(self, receipt, **client_kwargs):
        url = reverse("compliance:custody_eula_sign", kwargs={"token": receipt.token})
        return self.client.post(url, {"action": "accept", "signature_canvas": SIGNATURE}, **client_kwargs)

    def _checkout(self, holder):
        checkout_asset(asset=self.asset, holder=holder, checkout_date=timezone.now(), request=None)
        return CustodyReceipt.objects.get(asset=self.asset, holder=holder)

    def test_former_holder_cannot_accept_after_checkin(self):
        user = self.make_member("former-holder", set())
        holder = self.make_holder(user=user)
        receipt = self._checkout(holder)
        checkin_asset(self.asset)

        self.client_login_to_tenant(user, self.tenant)
        self._sign(receipt)

        receipt.refresh_from_db()
        self.assertNotEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_former_holder_cannot_accept_after_reassignment(self):
        first_user = self.make_member("first-holder", set())
        first = self.make_holder(user=first_user)
        second = self.make_holder(user=self.make_member("second-holder", set()))
        receipt = self._checkout(first)
        checkin_asset(self.asset)
        self._checkout(second)

        self.client_login_to_tenant(first_user, self.tenant)
        self._sign(receipt)

        receipt.refresh_from_db()
        self.assertNotEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_holder_without_membership_can_sign_their_receipt(self):
        outsider = User.objects.create_user(username="no-membership", password="x")
        holder = self.make_holder(user=outsider)
        receipt = self._checkout(holder)
        # The reported reproduction has the global default sequence present: the
        # former full asset save read it through a tenant-scoped manager on the
        # non-member's behalf and crashed (#607, same class as #306).
        AssetTagSequence._base_manager.get_or_create(
            tenant=None, category=None, prefix="ASSET-", defaults={"next_value": 1}
        )

        self.client.force_login(outsider)
        response = self._sign(receipt, raise_request_exception=False)

        self.assertEqual(response.status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(
            AssetTagSequence._base_manager.get(tenant__isnull=True, category__isnull=True, prefix="ASSET-").next_value,
            1,
            "signing must not allocate or consume an asset tag",
        )

    def test_checkin_supersedes_pending_receipt_and_blocks_signing(self):
        user = self.make_member("superseded-holder", set())
        holder = self.make_holder(user=user)
        receipt = self._checkout(holder)
        self.assertIsNotNone(receipt.assignment_id)
        checkin_asset(self.asset)

        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_SUPERSEDED)

        self.client_login_to_tenant(user, self.tenant)
        response = self._sign(receipt)
        self.assertEqual(response.status_code, 410)
        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_SUPERSEDED)

    def test_accepted_receipt_is_kept_on_checkin(self):
        user = self.make_member("accepted-holder", set())
        holder = self.make_holder(user=user)
        receipt = self._checkout(holder)
        self.client_login_to_tenant(user, self.tenant)
        self._sign(receipt)
        checkin_asset(self.asset)

        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_same_holder_recheckout_gets_fresh_receipt_bound_to_new_period(self):
        user = self.make_member("repeat-holder", set())
        holder = self.make_holder(user=user)
        first = self._checkout(holder)
        checkin_asset(self.asset)
        checkout_asset(asset=self.asset, holder=holder, checkout_date=timezone.now(), request=None)
        second = CustodyReceipt.objects.filter(asset=self.asset, holder=holder).exclude(pk=first.pk).get()

        self.assertNotEqual(first.assignment_id, second.assignment_id)
        self.client_login_to_tenant(user, self.tenant)
        self.assertEqual(self._sign(first).status_code, 410)
        self.assertEqual(self._sign(second).status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_signed_receipt_cannot_be_rewritten_or_minted_through_the_api(self):
        signer = self.make_member("api-signed-holder", set())
        holder = self.make_holder(user=signer)
        other = self.make_holder()
        receipt = self._checkout(holder)
        self.client_login_to_tenant(signer, self.tenant)
        self._sign(receipt)
        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

        operator = self.make_member(
            "api-receipt-operator",
            {
                "compliance.view_custodyreceipt",
                "compliance.add_custodyreceipt",
                "compliance.change_custodyreceipt",
                "compliance.delete_custodyreceipt",
            },
        )
        self.client_login_to_tenant(operator, self.tenant)
        detail = reverse("api:compliance_api:custodyreceipt-detail", kwargs={"pk": receipt.pk})
        rewrite = self.client.patch(detail, {"holder_id": other.pk}, content_type="application/json")
        mint = self.client.post(
            reverse("api:compliance_api:custodyreceipt-list"),
            {"asset_id": self.asset.pk, "holder_id": other.pk},
            content_type="application/json",
        )

        self.assertEqual(rewrite.status_code, 405)
        self.assertEqual(mint.status_code, 405)
        receipt.refresh_from_db()
        self.assertEqual(receipt.holder_id, holder.pk)
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertFalse(CustodyReceipt.objects.filter(holder=other).exists())
