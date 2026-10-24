"""The custody evidence seal: a keyed HMAC over a frozen snapshot of each decision."""

import hashlib
import json
import os
from io import StringIO
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset
from compliance.models import CustodyReceipt
from compliance.services import (
    EVIDENCE_KEY_PURPOSE,
    EVIDENCE_LEGACY,
    EVIDENCE_MISMATCH,
    EVIDENCE_UNKNOWN_KEY,
    EVIDENCE_UNSEALED,
    EVIDENCE_VERIFIED,
    verify_custody_evidence,
)
from compliance.tests.signatures import signature_png
from compliance.tests.test_custody_hardening import _ReceiptFixtureMixin
from compliance.views import _custody_receipt_pdf_context
from core.crypto import derive_purpose_keys
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Tenant

SIGNATURE = signature_png("seal-signature")
KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def keyring(*keys):
    return patch.dict(os.environ, {"ITAMBOX_FIELD_ENCRYPTION_KEYS": ",".join(keys)})


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class EvidenceSealTests(_ReceiptFixtureMixin, TestCase):
    def setUp(self):
        self.make_receipt_fixture(baker.make(Tenant, name="Seal Tenant", slug="seal-tenant"))
        self.client.force_login(self.recipient)

    def _accept(self):
        response = self.client.post(
            self.sign_url,
            {"action": "accept", "signature_canvas": SIGNATURE},
            REMOTE_ADDR="2001:DB8::1",
            HTTP_USER_AGENT="SealBrowser/1.0",
        )
        self.assertEqual(response.status_code, 200)
        self.receipt.refresh_from_db()
        return self.receipt

    def _tamper(self, **fields):
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(**fields)
        self.receipt.refresh_from_db()
        return verify_custody_evidence(self.receipt)

    def test_accepted_receipt_is_sealed_and_verifies(self):
        with keyring(KEY_A):
            receipt = self._accept()
            check = verify_custody_evidence(receipt)

        self.assertEqual(check.status, EVIDENCE_VERIFIED)
        self.assertTrue(check.verified)
        self.assertEqual(receipt.evidence_version, 1)
        self.assertEqual(len(receipt.evidence_key_id), 16)
        self.assertEqual(len(receipt.verification_hash), 64)
        self.assertEqual(receipt.signature_hash, hashlib.sha256(SIGNATURE.encode()).hexdigest())
        snapshot = receipt.evidence_snapshot
        self.assertEqual(snapshot["asset"]["asset_tag"], self.asset.asset_tag)
        self.assertEqual(snapshot["holder"]["upn"], self.holder.upn)
        self.assertEqual(snapshot["client"], {"ip_address": "2001:db8::1", "user_agent": "SealBrowser/1.0"})
        self.assertEqual(snapshot["decision"]["status"], CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(snapshot["terms"]["eula_sha256"], hashlib.sha256(receipt.eula_text.encode()).hexdigest())

    def test_declined_receipt_is_sealed_and_verifies(self):
        response = self.client.post(self.sign_url, {"action": "decline"}, REMOTE_ADDR="198.51.100.9")

        self.assertEqual(response.status_code, 200)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.evidence_snapshot["decision"]["status"], CustodyReceipt.STATUS_DECLINED)
        self.assertIsNotNone(self.receipt.evidence_snapshot["decided_at"])
        self.assertEqual(verify_custody_evidence(self.receipt).status, EVIDENCE_VERIFIED)

    def test_rewriting_a_bound_field_is_detected(self):
        self._accept()
        other_holder = baker.make(AssetHolder, tenant=self.asset.tenant)
        cases = {
            "holder_id": {"holder": other_holder},
            "client": {"ip_address": "192.0.2.99"},
            "terms": {"eula_text": "softer terms"},
            "signature_canvas_sha256": {"signature_canvas": signature_png("forged")},
            "decision": {"acceptance_status": CustodyReceipt.STATUS_DECLINED},
        }
        original = {
            "holder": self.holder,
            "ip_address": self.receipt.ip_address,
            "eula_text": self.receipt.eula_text,
            "signature_canvas": self.receipt.signature_canvas,
            "acceptance_status": self.receipt.acceptance_status,
        }
        for differing, change in cases.items():
            with self.subTest(differing=differing):
                check = self._tamper(**change)
                self.assertEqual(check.status, EVIDENCE_MISMATCH)
                self.assertIn(differing, check.differing_fields)
                self._tamper(**{name: original[name] for name in change})
                self.assertEqual(verify_custody_evidence(self.receipt).status, EVIDENCE_VERIFIED)

    def test_rewriting_the_snapshot_without_the_key_is_detected(self):
        receipt = self._accept()
        snapshot = dict(receipt.evidence_snapshot)
        snapshot["asset"] = {**snapshot["asset"], "asset_tag": "SWAPPED-TAG"}
        unkeyed = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()

        check = self._tamper(evidence_snapshot=snapshot, verification_hash=unkeyed)

        self.assertEqual(check.status, EVIDENCE_MISMATCH)
        self.assertEqual(check.differing_fields, ("evidence_snapshot",))

    def test_editing_the_asset_or_holder_later_keeps_the_seal_valid(self):
        receipt = self._accept()
        Asset.objects.filter(pk=self.asset.pk).update(asset_tag="RENAMED-1", name="Renamed")
        AssetHolder.objects.filter(pk=self.holder.pk).update(upn="renamed@example.test")

        receipt.refresh_from_db()
        self.assertEqual(verify_custody_evidence(receipt).status, EVIDENCE_VERIFIED)
        self.assertEqual(receipt.evidence_snapshot["asset"]["asset_tag"], "TAG-HARD-1")

    def test_a_rotated_out_key_still_verifies_while_it_stays_in_the_keyring(self):
        with keyring(KEY_A):
            receipt = self._accept()
        with keyring(KEY_B, KEY_A):
            self.assertEqual(verify_custody_evidence(receipt).status, EVIDENCE_VERIFIED)
        with keyring(KEY_B):
            self.assertEqual(verify_custody_evidence(receipt).status, EVIDENCE_UNKNOWN_KEY)

    def test_without_a_keyring_the_seal_follows_secret_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ITAMBOX_FIELD_ENCRYPTION_KEYS", None)
            receipt = self._accept()
            self.assertEqual(verify_custody_evidence(receipt).status, EVIDENCE_VERIFIED)
            with override_settings(SECRET_KEY="a-different-secret-key-for-the-rotation-case-0123456789"):
                self.assertEqual(verify_custody_evidence(receipt).status, EVIDENCE_UNKNOWN_KEY)

    def test_legacy_and_pending_receipts_are_reported_as_such(self):
        self.assertEqual(verify_custody_evidence(self.receipt).status, EVIDENCE_UNSEALED)
        self._tamper(acceptance_status=CustodyReceipt.STATUS_ACCEPTED, verification_hash="a" * 64)
        self.assertEqual(verify_custody_evidence(self.receipt).status, EVIDENCE_LEGACY)


class PurposeKeyTests(TestCase):
    def test_purposes_and_keyring_entries_get_distinct_stable_keys(self):
        with keyring(KEY_A, KEY_B):
            evidence = derive_purpose_keys(EVIDENCE_KEY_PURPOSE)
            other = derive_purpose_keys("another-purpose")
            again = derive_purpose_keys(EVIDENCE_KEY_PURPOSE)

        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence, again)
        self.assertNotEqual(evidence[0][1], evidence[1][1])
        self.assertNotEqual(evidence[0][1], other[0][1])
        self.assertNotIn(KEY_A.encode(), evidence[0][1])

    def test_malformed_keyring_fails_closed(self):
        with keyring("not-a-fernet-key"), self.assertRaises(ImproperlyConfigured):
            derive_purpose_keys(EVIDENCE_KEY_PURPOSE)


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class EvidenceSurfacesTests(TenantTestMixin, _ReceiptFixtureMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Seal Surface Tenant",
            slug="seal-surface",
            permissions=["compliance.view_custodyreceipt", "compliance.export_custodyreceipt"],
        )
        self.make_receipt_fixture(self.tenant)
        self.client.force_login(self.recipient)
        self.client.post(self.sign_url, {"action": "accept", "signature_canvas": SIGNATURE})
        self.receipt.refresh_from_db()
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def test_detail_page_shows_the_seal_status(self):
        url = reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk})

        verified = self.client.get(url)
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(ip_address="192.0.2.50")
        mismatch = self.client.get(url)

        self.assertContains(verified, 'data-evidence-status="verified"')
        self.assertContains(mismatch, 'data-evidence-status="mismatch"')
        self.assertContains(mismatch, "client")

    def test_json_export_carries_the_evidence_block(self):
        response = self.client.get(reverse("compliance:custodyreceipt_export", kwargs={"pk": self.receipt.pk}))

        self.assertEqual(response.status_code, 200)
        evidence = json.loads(response.content)["evidence"]
        self.assertEqual(evidence["status"], EVIDENCE_VERIFIED)
        self.assertEqual(evidence["version"], 1)
        self.assertEqual(evidence["key_id"], self.receipt.evidence_key_id)
        self.assertEqual(evidence["snapshot"]["asset"]["asset_tag"], self.asset.asset_tag)

    def test_pdf_context_names_the_seal_status(self):
        self.assertEqual(str(_custody_receipt_pdf_context(self.receipt)["evidence_label"]), "Verified")


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class VerifyCustodyReceiptsCommandTests(_ReceiptFixtureMixin, TestCase):
    def setUp(self):
        self.tenant = baker.make(Tenant, name="Command Tenant", slug="command-tenant")
        self.make_receipt_fixture(self.tenant)
        self.client.force_login(self.recipient)
        self.client.post(self.sign_url, {"action": "accept", "signature_canvas": SIGNATURE})

    def _run(self, *args):
        out = StringIO()
        call_command("verify_custody_receipts", *args, stdout=out)
        return out.getvalue()

    def test_reports_counts_and_mismatches(self):
        self.assertIn("Verified: 1", self._run())

        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(user_agent="rewritten")
        output = self._run()

        self.assertIn("Verified: 0", output)
        self.assertIn(f"receipt {self.receipt.pk}: mismatch (client)", output)
        with self.assertRaises(CommandError):
            self._run("--fail-on-mismatch")

    def test_tenant_filter(self):
        self.assertIn("Verified: 1", self._run("--tenant", "command-tenant"))
        self.assertIn("Verified: 0", self._run("--tenant", "no-such-tenant"))
