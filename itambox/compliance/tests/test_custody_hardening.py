"""Custody consent hardening: signature validation, signer evidence, provider
fail-closed, read-only receipt API, and category-less template fallback."""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APITestCase

from assets.models import Asset, AssetAssignment, AssetTagSequence
from assets.services import checkout_asset
from compliance.models import CustodyReceipt, CustodyTemplate
from compliance.tests.signatures import signature_png
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Tenant, TenantGroup

User = get_user_model()

SIGNATURE = signature_png("hardening-signature")


class _ReceiptFixtureMixin:
    """One pending receipt bound to an active assignment, held by ``self.recipient``."""

    def make_receipt_fixture(self, tenant):
        self.recipient = baker.make(User, username="hardening@example.test", email="hardening@example.test")
        baker.make(AssetTagSequence, tenant=tenant, category=None, prefix="HARD-")
        deployable = baker.make("assets.StatusLabel", type="deployable", name="Hardening Deployable")
        deployed = baker.make("assets.StatusLabel", type="deployed", name="Hardening Deployed")
        self.asset = baker.make(Asset, name="HARD-LT", asset_tag="TAG-HARD-1", tenant=tenant, status=deployable)
        self.holder = baker.make(
            AssetHolder,
            first_name="Hard",
            last_name="Ening",
            email="hardening@example.test",
            upn="hardening@example.test",
            user=self.recipient,
            tenant=tenant,
        )
        checkout_asset(self.asset, holder=self.holder, status=deployed, _suppress_custody_receipt=True)
        assignment = AssetAssignment.objects.get(asset=self.asset, is_active=True)
        self.receipt = baker.make(CustodyReceipt, asset=self.asset, assignment=assignment, holder=self.holder)
        self.sign_url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class SignatureEvidenceTests(_ReceiptFixtureMixin, TestCase):
    def setUp(self):
        self.make_receipt_fixture(baker.make(Tenant, name="Hardening Tenant"))
        self.client.force_login(self.recipient)

    def test_non_png_signature_is_refused_and_receipt_stays_pending(self):
        for payload in ("not-a-png <b>", "data:image/png;base64,AAA", "data:image/png;base64,VEVTVA==", "empty"):
            with self.subTest(payload=payload):
                response = self.client.post(self.sign_url, {"action": "accept", "signature_canvas": payload})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Please provide a valid signature.")
                self.receipt.refresh_from_db()
                self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)
                self.assertEqual(self.receipt.signature_canvas, "")
                self.assertIsNone(self.receipt.signed_at)

    def test_accept_records_signature_client_address_and_user_agent(self):
        long_agent = "ProbeBrowser/1.0 " + "x" * 600

        response = self.client.post(
            self.sign_url,
            {"action": "accept", "signature_canvas": SIGNATURE},
            REMOTE_ADDR="203.0.113.7",
            HTTP_USER_AGENT=long_agent,
        )

        self.assertEqual(response.status_code, 200)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(self.receipt.signature_canvas, SIGNATURE)
        self.assertEqual(self.receipt.ip_address, "203.0.113.7")
        self.assertEqual(self.receipt.user_agent, long_agent[:512])

    def test_decline_records_client_address_and_user_agent(self):
        response = self.client.post(
            self.sign_url,
            {"action": "decline"},
            REMOTE_ADDR="198.51.100.4",
            HTTP_USER_AGENT="DeclineBrowser/2.0",
        )

        self.assertEqual(response.status_code, 200)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_DECLINED)
        self.assertEqual(self.receipt.ip_address, "198.51.100.4")
        self.assertEqual(self.receipt.user_agent, "DeclineBrowser/2.0")

    def test_unregistered_provider_fails_closed_instead_of_showing_the_local_pad(self):
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(signature_provider="retired-esign")

        with self.assertLogs("compliance.views", level="ERROR"):
            get_response = self.client.get(self.sign_url)
        with self.assertLogs("compliance.views", level="ERROR"):
            post_response = self.client.post(self.sign_url, {"action": "accept", "signature_canvas": SIGNATURE})

        for response in (get_response, post_response):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.context["error_code"], "custody_provider_unavailable")
            self.assertNotContains(response, "custody-sign-form", status_code=503)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)


class ReceiptDetailSignatureTests(TenantTestMixin, _ReceiptFixtureMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Detail Tenant", slug="detail-tenant", permissions=["compliance.view_custodyreceipt"]
        )
        self.make_receipt_fixture(self.tenant)
        self.detail_url = reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk})
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _store_canvas(self, value):
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(
            signature_canvas=value, acceptance_status=CustodyReceipt.STATUS_ACCEPTED, signed_at=timezone.now()
        )

    def test_detail_renders_a_valid_png_signature(self):
        self._store_canvas(SIGNATURE)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["signature_image"], SIGNATURE)

    def test_detail_omits_a_legacy_signature_that_is_not_a_png(self):
        self._store_canvas("data:image/png;base64,not-really-a-png")

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("signature_image", response.context)
        self.assertNotContains(response, "not-really-a-png")


class CustodyReceiptApiReadOnlyTests(TenantTestMixin, _ReceiptFixtureMixin, APITestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Api Tenant",
            slug="api-hardening",
            permissions=[
                "compliance.view_custodyreceipt",
                "compliance.add_custodyreceipt",
                "compliance.change_custodyreceipt",
                "compliance.delete_custodyreceipt",
            ],
        )
        self.make_receipt_fixture(self.tenant)
        self.other_holder = baker.make(AssetHolder, tenant=self.tenant, email="other@example.test")
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        self.list_url = reverse("api:compliance_api:custodyreceipt-list")
        self.detail_url = reverse("api:compliance_api:custodyreceipt-detail", kwargs={"pk": self.receipt.pk})

    def test_read_omits_the_signing_token_and_carries_signer_evidence(self):
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(ip_address="192.0.2.1", user_agent="EvidenceUA")

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.data)
        self.assertNotIn(self.receipt.token, str(response.data))
        self.assertEqual(response.data["ip_address"], "192.0.2.1")
        self.assertEqual(response.data["user_agent"], "EvidenceUA")
        self.assertEqual(response.data["signature_provider"], "local")

    def test_writes_are_refused_even_with_every_receipt_permission(self):
        before = CustodyReceipt.objects.count()

        create = self.client.post(
            self.list_url, {"asset_id": self.asset.pk, "holder_id": self.other_holder.pk}, format="json"
        )
        patch_response = self.client.patch(self.detail_url, {"holder_id": self.other_holder.pk}, format="json")
        put = self.client.put(
            self.detail_url, {"asset_id": self.asset.pk, "holder_id": self.other_holder.pk}, format="json"
        )
        delete = self.client.delete(self.detail_url)

        for response in (create, patch_response, put, delete):
            self.assertEqual(response.status_code, 405)
        self.assertEqual(CustodyReceipt.objects.count(), before)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.holder_id, self.holder.pk)


class CustodyTemplateApiProviderTests(TenantTestMixin, APITestCase):
    def setUp(self):
        self.setup_tenant_context(
            name="Template Api Tenant",
            slug="template-api-hardening",
            permissions=["compliance.add_custodytemplate", "compliance.view_custodytemplate"],
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        self.url = reverse("api:compliance_api:custodytemplate-list")

    def _create(self, provider):
        return self.client.post(
            self.url,
            {"name": f"T {provider}", "tenant_id": self.tenant.pk, "eula_text": "x", "signature_provider": provider},
            format="json",
        )

    def test_unknown_provider_is_rejected(self):
        response = self._create("docusgn")

        self.assertEqual(response.status_code, 400)
        self.assertIn("signature_provider", response.data)
        self.assertIn("local", str(response.data["signature_provider"]))
        self.assertFalse(CustodyTemplate.objects.filter(name="T docusgn").exists())

    def test_registered_provider_is_accepted(self):
        response = self._create("local")

        self.assertEqual(response.status_code, 201, response.data)


class CustodyTemplateFallbackTests(TestCase):
    def setUp(self):
        self.group = baker.make(TenantGroup, name="Fallback Group", slug="fallback-group")
        self.tenant = baker.make(Tenant, name="Fallback Tenant", slug="fallback-tenant", group=self.group)
        self.category = baker.make("assets.Category", name="Fallback Laptops", slug="fallback-laptops")
        self.deployable = baker.make("assets.StatusLabel", type="deployable", name="Fallback Deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Fallback Deployed")
        self.holder = baker.make(AssetHolder, tenant=self.tenant, email="fallback@example.test")

    def _template(self, name, **scope):
        return baker.make(
            CustodyTemplate,
            name=name,
            require_acceptance=True,
            email_signature_request=False,
            signature_provider="local",
            eula_text=name,
            **scope,
        )

    def _checkout(self, *, category="default"):
        category = self.category if category == "default" else category
        asset_type = baker.make("assets.AssetType", category=category) if category is not None else None
        asset = baker.make(Asset, tenant=self.tenant, asset_type=asset_type, status=self.deployable)
        checkout_asset(asset=asset, holder=self.holder, checkout_date=timezone.now(), request=None)
        return CustodyReceipt.objects.filter(asset=asset).first()

    def test_category_less_tenant_template_applies_when_no_category_template_exists(self):
        generic = self._template("Tenant default", tenant=self.tenant)

        receipt = self._checkout()

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.custody_template, generic)

    def test_category_less_template_applies_to_an_asset_without_a_category(self):
        generic = self._template("Group default", tenant_group=self.group)

        receipt = self._checkout(category=None)

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.custody_template, generic)

    def test_a_category_template_in_any_scope_beats_a_category_less_one(self):
        self._template("Tenant default", tenant=self.tenant)
        global_laptops = self._template("Global laptops", category=self.category)

        receipt = self._checkout()

        self.assertEqual(receipt.custody_template, global_laptops)

    def test_tenant_beats_group_beats_global_among_category_less_templates(self):
        self._template("Global default")
        group_default = self._template("Group default", tenant_group=self.group)

        self.assertEqual(self._checkout().custody_template, group_default)

        tenant_default = self._template("Tenant default", tenant=self.tenant)

        self.assertEqual(self._checkout().custody_template, tenant_default)

    @override_settings(ALLOW_GLOBAL_CUSTODY_TEMPLATES=False)
    def test_global_category_less_template_is_ignored_when_globals_are_disabled(self):
        self._template("Global default")

        self.assertIsNone(self._checkout())

    def test_inactive_category_less_template_is_ignored(self):
        self._template("Inactive default", tenant=self.tenant, is_active=False)

        self.assertIsNone(self._checkout())


class CheckoutUnregisteredProviderTests(TestCase):
    def test_checkout_succeeds_and_logs_when_the_template_provider_is_unregistered(self):
        tenant = baker.make(Tenant)
        category = baker.make("assets.Category")
        deployable = baker.make("assets.StatusLabel", type="deployable", name="Unreg Deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Unreg Deployed")
        asset = baker.make(
            Asset, tenant=tenant, asset_type=baker.make("assets.AssetType", category=category), status=deployable
        )
        holder = baker.make(AssetHolder, tenant=tenant, email="unreg@example.test")
        baker.make(
            CustodyTemplate,
            tenant=tenant,
            category=category,
            require_acceptance=True,
            email_signature_request=True,
            signature_provider="retired-esign",
        )
        user = baker.make(User)
        email_config = SimpleNamespace(enabled=True, from_address="itambox@example.test", test_recipient="")

        with (
            patch("core.models.EmailSettings.load", return_value=email_config),
            patch("assets.services.send_mail") as send_mail,
            self.assertLogs("assets.services", level="ERROR") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            checkout_asset(asset=asset, holder=holder, user=user, request=SimpleNamespace(user=user))

        send_mail.assert_not_called()
        receipt = CustodyReceipt.objects.get(asset=asset, holder=holder)
        self.assertEqual(receipt.signature_provider, "retired-esign")
        self.assertIn(f"receipt_id={receipt.pk}", "\n".join(logs.output))
