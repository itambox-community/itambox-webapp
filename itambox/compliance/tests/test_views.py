from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from assets.models import Asset, AssetAssignment, AssetMaintenance, AssetTagSequence, Supplier
from assets.services import checkout_asset
from compliance.providers import BaseSignatureProvider
from compliance.registry import signature_providers
from compliance.tests.signatures import signature_png
from organization.models import AssetHolder, Tenant

from ..models import CustodyReceipt, CustodyTemplate

User = get_user_model()

VALID_SIGNATURE = signature_png("view-test-signature")


class AssetMaintenanceViewTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_staff=True, is_superuser=True)
        # Set plain text password for login
        self.user.set_password("testpassword")
        self.user.save()
        self.client.login(username=self.user.username, password="testpassword")

        self.asset = baker.make(Asset, name="SRV-01", asset_tag="TAG-SRV-01", tenant=None)
        self.supplier = baker.make(Supplier, name="Dell", slug="dell")
        self.maintenance = baker.make(
            AssetMaintenance,
            asset=self.asset,
            maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
            supplier=self.supplier,
            cost=250.00,
            start_date=date(2026, 1, 1),
            completion_date=date(2026, 1, 5),
            notes="Replaced motherboard",
        )

    def test_list_view(self):
        url = reverse("assets:assetmaintenance_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SRV-01")

    def test_detail_view(self):
        url = reverse("assets:assetmaintenance_detail", kwargs={"pk": self.maintenance.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SRV-01")
        self.assertContains(response, "250.00")

    def test_create_view_get(self):
        url = reverse("assets:assetmaintenance_create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_create_view_post(self):
        hp_supplier = baker.make(Supplier, name="HP Support", slug="hp-support")
        url = reverse("assets:assetmaintenance_create")
        response = self.client.post(
            url,
            {
                "asset": self.asset.pk,
                "title": "RAM upgrade to 64GB",
                "status": "scheduled",
                "maintenance_type": AssetMaintenance.MAINTENANCE_TYPE_UPGRADE,
                "supplier": hp_supplier.pk,
                "cost": "500.00",
                "start_date": "2026-06-01",
                "completion_date": "2026-06-03",
                "notes": "RAM upgrade to 64GB",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AssetMaintenance.objects.filter(supplier=hp_supplier).exists())

    def test_edit_view_get(self):
        url = reverse("assets:assetmaintenance_update", kwargs={"pk": self.maintenance.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_edit_view_post(self):
        dell_premium = baker.make(Supplier, name="Dell Premium", slug="dell-premium")
        url = reverse("assets:assetmaintenance_update", kwargs={"pk": self.maintenance.pk})
        response = self.client.post(
            url,
            {
                "asset": self.asset.pk,
                "title": "Replaced motherboard + CPU",
                "status": "completed",
                "maintenance_type": AssetMaintenance.MAINTENANCE_TYPE_REPAIR,
                "supplier": dell_premium.pk,
                "cost": "300.00",
                "start_date": "2026-01-01",
                "completion_date": "2026-01-05",
                "notes": "Replaced motherboard + CPU",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.maintenance.refresh_from_db()
        self.assertEqual(self.maintenance.supplier, dell_premium)
        self.assertEqual(self.maintenance.cost, 300.00)

    def test_delete_view_get(self):
        url = reverse("assets:assetmaintenance_delete", kwargs={"pk": self.maintenance.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_delete_view_post(self):
        url = reverse("assets:assetmaintenance_delete", kwargs={"pk": self.maintenance.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AssetMaintenance.objects.filter(pk=self.maintenance.pk).exists())


@override_settings(REQUIRE_CUSTODY_SIGNIN=False)
class CustodyReceiptViewTests(TestCase):
    def setUp(self):
        self.recipient = baker.make(User, username="recipient@example.test", email="recipient@example.test")
        self.tenant = baker.make(Tenant, name="Recipient Tenant")
        baker.make(AssetTagSequence, tenant=self.tenant, category=None, prefix="CUST-")
        deployable = baker.make("assets.StatusLabel", type="deployable", name="Receipt Deployable")
        deployed = baker.make("assets.StatusLabel", type="deployed", name="Receipt Deployed")
        self.asset = baker.make(Asset, name="LT-02", asset_tag="TAG-LT-02", tenant=self.tenant, status=deployable)
        self.holder = baker.make(
            AssetHolder,
            first_name="Evelyn",
            last_name="Carter",
            email="recipient@example.test",
            upn="recipient@example.test",
            user=self.recipient,
            tenant=self.tenant,
        )
        checkout_asset(self.asset, holder=self.holder, status=deployed, _suppress_custody_receipt=True)
        self.assignment = AssetAssignment.objects.get(asset=self.asset, is_active=True)
        self.receipt = baker.make(
            CustodyReceipt,
            asset=self.asset,
            assignment=self.assignment,
            holder=self.holder,
        )
        self.client.force_login(self.recipient)

    def test_sign_portal_get(self):
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LT-02")
        self.assertContains(response, "Evelyn Carter")

    def test_sign_portal_post_empty_signature(self):
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.post(url, {"action": "accept", "signature_canvas": "empty"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please provide a valid signature.")
        self.receipt.refresh_from_db()
        self.assertFalse(self.receipt.accepted)

    def test_sign_portal_post_decline(self):
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.post(url, {"action": "decline"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You have declined the custody transfer.")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_DECLINED)
        self.assertFalse(self.receipt.accepted)
        self.assertIsNone(self.receipt.signed_at)

    def test_sign_portal_post_success(self):
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        sig_data = VALID_SIGNATURE
        response = self.client.post(url, {"action": "accept", "signature_canvas": sig_data})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "receipt")  # checks success page rendered
        self.receipt.refresh_from_db()
        self.assertTrue(self.receipt.accepted)
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(self.receipt.signature_data, sig_data)
        self.assertIsNotNone(self.receipt.signature_hash)
        self.assertEqual(self.receipt.signed_at, self.receipt.accepted_date)

    def test_sign_post_locks_receipt_row(self):
        """WS6-5: the sign-off POST must hold a row lock (SELECT ... FOR UPDATE) so two
        concurrent submits cannot both recompute a fresh signature/hash."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(url, {"action": "accept", "signature_canvas": VALID_SIGNATURE})
        locked = any(
            "custodyreceipt" in q["sql"].lower() and "for update" in q["sql"].lower() for q in ctx.captured_queries
        )
        self.assertTrue(locked, "custody sign-off must lock the receipt row (FOR UPDATE)")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_sign_portal_already_accepted(self):
        from django.utils import timezone

        self.receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt.signed_at = timezone.now()
        self.receipt.save()
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "success")  # checks success page rendered

    def test_sign_portal_already_declined(self):
        self.receipt.acceptance_status = CustodyReceipt.STATUS_DECLINED
        self.receipt.save()
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "declined")

    def test_sign_portal_expired_link(self):
        from django.utils import timezone

        # set created_date to older than 7 days using update (auto_now_add is immutable on direct save)
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(created_date=timezone.now() - timedelta(days=8))
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        self.client.logout()
        with override_settings(REQUIRE_CUSTODY_SIGNIN=True):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.context["error_code"], "custody_link_expired")
        self.assertContains(response, "expired", status_code=410)
        self.assertNotContains(response, self.asset.name, status_code=410)
        self.assertNotContains(response, self.holder.first_name, status_code=410)

    def test_sign_portal_expired_link_post_without_session(self):
        # POST path: the link-TTL check must still apply on the unassisted
        # path (no ?session=) after the row/session lock — covers the
        # post-lock 410 branch (differential coverage).
        from django.utils import timezone

        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(created_date=timezone.now() - timedelta(days=8))
        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        self.client.logout()
        with override_settings(REQUIRE_CUSTODY_SIGNIN=True):
            response = self.client.post(url, {"action": "accept", "signature_canvas": "x"})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.context["error_code"], "custody_link_expired")

    def test_unlinked_receipt_signs_while_its_holder_is_the_active_holder(self):
        # A receipt without a bound period (a pre-migration row, or one created by an
        # out-of-band tool) stays signable while its holder is the asset's active
        # holder: the fallback match is by asset and holder (differential coverage).
        receipt = CustodyReceipt.objects.create(asset=self.asset, holder=self.holder)
        self.assertIsNone(receipt.assignment_id)

        url = reverse("compliance:custody_eula_sign", kwargs={"token": receipt.token})
        response = self.client.post(url, {"action": "accept", "signature_canvas": VALID_SIGNATURE})

        self.assertEqual(response.status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_pending_receipt_whose_period_ended_is_refused(self):
        # A period can end without the check-in hook (a data repair or another
        # application path). A receipt that is still pending then must be refused
        # as superseded instead of being signed (differential coverage).
        from django.utils import timezone

        AssetAssignment._base_manager.filter(pk=self.assignment.pk).update(
            is_active=False, checked_in_at=timezone.now()
        )

        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.post(url, {"action": "accept", "signature_canvas": VALID_SIGNATURE})

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.context["error_code"], "custody_superseded")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_period_ending_before_the_locked_post_is_refused(self):
        # The pre-lock check and the locked write are two different moments: a period
        # that ends in between must still be refused by the re-check under the lock.
        from unittest import mock

        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        with mock.patch("compliance.views._receipt_period_is_active", side_effect=[True, False]) as period_check:
            response = self.client.post(url, {"action": "accept", "signature_canvas": VALID_SIGNATURE})

        self.assertEqual(period_check.call_count, 2)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.context["error_code"], "custody_superseded")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_completed_receipt_requires_recipient_authentication_when_signin_is_optional(self):
        # With REQUIRE_CUSTODY_SIGNIN off the view is reached unauthenticated; a
        # receipt that is already terminal then demands authentication instead of
        # rendering the recipient's outcome (differential coverage).
        from django.utils import timezone

        self.receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt.signed_at = timezone.now()
        self.receipt.save()
        self.client.logout()

        url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.context["error_code"], "recipient_authentication_required")

    def test_sign_portal_redirect_when_signin_required_unauthenticated(self):
        from django.test import override_settings

        with override_settings(REQUIRE_CUSTODY_SIGNIN=True):
            url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
            self.client.logout()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response.url)

    def test_sign_portal_allowed_when_signin_required_authenticated(self):
        from django.test import override_settings

        with override_settings(REQUIRE_CUSTODY_SIGNIN=True):
            self.client.force_login(self.recipient)
            url = reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_invalid_and_unknown_tokens_return_neutral_404_before_login_redirect(self):
        self.client.logout()

        with override_settings(REQUIRE_CUSTODY_SIGNIN=True):
            for token in ("invalid-token", "x" * 64):
                with self.subTest(token_length=len(token)):
                    response = self.client.get(reverse("compliance:custody_eula_sign", kwargs={"token": token}))

                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.context["error_code"], "custody_link_unavailable")
                    self.assertNotContains(response, self.asset.name, status_code=404)
                    self.assertNotContains(response, self.holder.first_name, status_code=404)

    def test_authenticated_wrong_recipient_gets_403_without_receipt_payload_or_mutation(self):
        wrong_recipient = baker.make(User, username="wrong-recipient@example.test")
        self.client.force_login(wrong_recipient)

        response = self.client.post(
            reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token}),
            {"action": "accept", "signature_canvas": VALID_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.context["error_code"], "wrong_recipient")
        self.assertNotContains(response, self.asset.name, status_code=403)
        self.assertNotContains(response, self.holder.first_name, status_code=403)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)
        self.assertIsNone(self.receipt.signed_at)

    def test_anonymous_consent_is_rejected_when_signin_setting_is_optional(self):
        self.client.logout()

        response = self.client.post(
            reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token}),
            {"action": "accept", "signature_canvas": VALID_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.context["error_code"], "recipient_authentication_required")
        self.assertNotContains(response, self.asset.name, status_code=403)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)
        self.assertIsNone(self.receipt.signed_at)

    def test_completed_receipt_is_not_rendered_to_wrong_recipient(self):
        from django.utils import timezone

        self.receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt.signed_at = timezone.now()
        self.receipt.verification_hash = "completed-test-verification-hash"
        self.receipt.save()
        self.client.force_login(baker.make(User, username="other-recipient@example.test"))

        response = self.client.get(reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token}))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.context["error_code"], "wrong_recipient")
        self.assertNotContains(response, self.asset.name, status_code=403)
        self.assertNotContains(response, self.receipt.verification_hash, status_code=403)

    def test_expired_completed_receipt_returns_410_instead_of_success_payload(self):
        from django.utils import timezone

        self.receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt.signed_at = timezone.now()
        self.receipt.save()
        CustodyReceipt.objects.filter(pk=self.receipt.pk).update(created_date=timezone.now() - timedelta(days=8))

        response = self.client.get(reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token}))

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.context["error_code"], "custody_link_expired")
        self.assertNotContains(response, self.asset.name, status_code=410)

    def test_wrong_recipient_is_rejected_before_external_provider_redirect(self):
        from unittest.mock import patch

        signature_providers.register(MockSignatureProvider)
        provider = signature_providers.get(MockSignatureProvider.name)
        self.receipt.signature_provider = MockSignatureProvider.name
        self.receipt.save(update_fields=["signature_provider", "updated_at"])
        self.client.force_login(baker.make(User, username="external-wrong-recipient@example.test"))

        with patch.object(provider, "initiate_signature", wraps=provider.initiate_signature) as initiate_signature:
            response = self.client.get(reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token}))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.context["error_code"], "wrong_recipient")
        initiate_signature.assert_not_called()


class MockSignatureProvider(BaseSignatureProvider):
    name = "mock_esign"
    verbose_name = "Mock E-Sign Integration"

    def initiate_signature(self, receipt, request=None):
        return f"https://mockesign.com/sign/{receipt.token}/"

    def verify_signature(self, payload):
        return True


class CustodyTemplateViewTests(TestCase):
    def setUp(self):
        self.user = baker.make(User, is_staff=True, is_superuser=True)
        self.user.set_password("testpassword")
        self.user.save()
        self.client.login(username=self.user.username, password="testpassword")

        self.template = baker.make(
            CustodyTemplate,
            name="Laptop Custody Template",
            signature_provider="local",
            eula_text="This laptop belongs to the company.",
            disclaimer="Draw signature if you agree.",
            qms_reference="QMS-IT-001",
        )

    def test_list_view(self):
        url = reverse("compliance:custodytemplate_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Laptop Custody Template")

    def test_detail_view(self):
        url = reverse("compliance:custodytemplate_detail", kwargs={"pk": self.template.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Laptop Custody Template")
        self.assertContains(response, "This laptop belongs to the company.")
        self.assertContains(response, "QMS-IT-001")

    def test_create_view_get(self):
        url = reverse("compliance:custodytemplate_create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_create_view_post(self):
        url = reverse("compliance:custodytemplate_create")
        response = self.client.post(
            url,
            {
                "name": "New Server Template",
                "signature_provider": "local",
                "eula_text": "Server room access guidelines.",
                "disclaimer": "Do not damage servers.",
                "qms_reference": "QMS-IT-002",
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CustodyTemplate.objects.filter(name="New Server Template").exists())

    def test_edit_view_post(self):
        url = reverse("compliance:custodytemplate_update", kwargs={"pk": self.template.pk})
        response = self.client.post(
            url,
            {
                "name": "Updated Laptop Template",
                "signature_provider": "local",
                "eula_text": "Updated laptop terms.",
                "disclaimer": "Sign pad.",
                "qms_reference": "QMS-IT-001-v2",
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.template.refresh_from_db()
        self.assertEqual(self.template.name, "Updated Laptop Template")
        self.assertEqual(self.template.qms_reference, "QMS-IT-001-v2")

    def test_delete_view_post(self):
        url = reverse("compliance:custodytemplate_delete", kwargs={"pk": self.template.pk})
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CustodyTemplate.objects.filter(pk=self.template.pk).exists())


class SignatureRegistryTests(TestCase):
    def test_provider_registration(self):
        # Test default local provider
        choices = signature_providers.choices()
        self.assertIn(("local", "Local Canvas Signature Pad"), choices)

        # Test mock provider registration
        signature_providers.register(MockSignatureProvider)
        choices_after = signature_providers.choices()
        self.assertIn(("mock_esign", "Mock E-Sign Integration"), choices_after)

        # Verify get
        provider = signature_providers.get("mock_esign")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "mock_esign")


class CustodyTemplateCheckoutTests(TestCase):
    def setUp(self):
        self.tg = baker.make("organization.TenantGroup", name="TG1", slug="tg1")
        self.tenant = baker.make("organization.Tenant", name="Tenant1", slug="tenant1", group=self.tg)
        self.category = baker.make("assets.Category", name="Tenant Laptops", slug="tenant-laptops")
        self.deployable = baker.make("assets.StatusLabel", type="deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Deployed")
        self.template = baker.make(
            CustodyTemplate,
            tenant=self.tenant,
            category=self.category,
            require_acceptance=True,
            email_signature_request=True,
            name="Tenant Laptop EULA",
            signature_provider="local",
            eula_text="Tenant-specific laptop EULA rules.",
            disclaimer="Please sign the pad.",
            qms_reference="QMS-TEN-001",
        )
        self.asset_type = baker.make("assets.AssetType", model="MBP16", slug="mbp16", category=self.category)
        self.asset = baker.make(
            Asset,
            name="Laptop 01",
            asset_tag="TAG-LT-01",
            asset_type=self.asset_type,
            tenant=self.tenant,
            status=self.deployable,
        )
        self.holder = baker.make(
            AssetHolder, first_name="John", last_name="Doe", email="john@tenant.com", tenant=self.tenant
        )

    def test_checkout_copies_template_fields(self):
        from assets.services import checkout_asset

        # Perform checkout
        checkout_asset(
            asset=self.asset,
            holder=self.holder,
            checkout_date=timezone.now(),
            notes="Assigning laptop to John",
            request=None,
        )

        # Verify CustodyReceipt was created and fields copied
        receipts = CustodyReceipt.objects.filter(asset=self.asset, holder=self.holder)
        self.assertEqual(receipts.count(), 1)
        receipt = receipts.first()

        self.assertEqual(receipt.custody_template, self.template)
        self.assertEqual(receipt.signature_provider, "local")
        self.assertEqual(receipt.eula_text, "Tenant-specific laptop EULA rules.")
        self.assertEqual(receipt.disclaimer, "Please sign the pad.")
        self.assertEqual(receipt.qms_reference, "QMS-TEN-001")


class CustodyUXAndPreviewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = baker.make(get_user_model(), is_staff=True, is_superuser=True)
        self.user.set_password("testpassword")
        self.user.save()
        self.client.login(username=self.user.username, password="testpassword")

        self.tenant = baker.make("organization.Tenant", name="Custody UX Tenant", slug="custody-ux")
        self.category = baker.make("assets.Category", name="Laptops", slug="custody-laptops")
        self.deployable = baker.make("assets.StatusLabel", type="deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Deployed")
        self.template = baker.make(
            CustodyTemplate,
            tenant=self.tenant,
            category=self.category,
            require_acceptance=True,
            email_signature_request=True,
            name="Standard Laptop EULA",
            signature_provider="local",
            eula_text="Laptops must be protected.",
            disclaimer="Draw signature to accept.",
            qms_reference="QMS-LT-001",
        )
        self.asset_type = baker.make("assets.AssetType", model="ThinkPad", slug="thinkpad", category=self.category)
        self.asset = baker.make(
            Asset,
            tenant=self.tenant,
            name="Developer Laptop 01",
            asset_tag="TAG-DEV-01",
            asset_type=self.asset_type,
            status=self.deployable,
        )
        self.holder = baker.make(
            AssetHolder,
            tenant=self.tenant,
            first_name="Alex",
            last_name="Staff",
            email="alex@staff.com",
        )

    def test_asset_detail_shows_pending_eula_status(self):
        from assets.services import checkout_asset

        # Checkout asset to create the pending CustodyReceipt
        checkout_asset(
            asset=self.asset, holder=self.holder, checkout_date=timezone.now(), notes="Onboarding Alex", request=None
        )

        # Retrieve asset details page
        url = reverse("assets:asset_detail", kwargs={"pk": self.asset.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Assert correct UX elements render for pending state
        self.assertContains(response, "Pending EULA Acceptance")
        receipt = CustodyReceipt.objects.get(asset=self.asset, holder=self.holder)
        self.assertContains(response, reverse("compliance:custodyreceipt_detail", kwargs={"pk": receipt.pk}))
        self.assertNotContains(response, "Sign Custody (On-Site)...")
        self.assertNotContains(response, "Copy Link")
        self.assertNotContains(response, receipt.token)
        self.assertNotContains(response, "Custody Secured")

    def test_asset_detail_shows_secured_custody_status(self):
        from assets.services import checkout_asset

        # Checkout asset to create the pending CustodyReceipt
        checkout_asset(
            asset=self.asset, holder=self.holder, checkout_date=timezone.now(), notes="Onboarding Alex", request=None
        )

        # Sign the receipt
        receipt = CustodyReceipt.objects.filter(asset=self.asset, holder=self.holder).first()
        receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        receipt.signed_at = timezone.now()
        receipt.save()

        # Retrieve asset details page
        url = reverse("assets:asset_detail", kwargs={"pk": self.asset.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Assert correct UX elements render for secured state
        self.assertContains(response, "Custody Secured")
        self.assertContains(response, "View Signed Receipt")
        self.assertContains(response, reverse("compliance:custodyreceipt_detail", kwargs={"pk": receipt.pk}))
        self.assertNotContains(response, receipt.token)
        self.assertNotContains(response, "Pending EULA Acceptance")
        self.assertNotContains(response, "Sign Custody (On-Site)...")

    def test_custody_template_preview_rendering(self):
        # Request template preview sandbox
        url = reverse("compliance:custodytemplate_preview", kwargs={"pk": self.template.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Assert elements of preview mode are present
        self.assertContains(response, "Live Template Preview Sandbox")
        self.assertContains(response, "This is a secure simulation showing exactly how users will view this template.")
        self.assertContains(response, "Sign-off submission is disabled in preview mode.")
        self.assertContains(response, "Laptops must be protected.")
        self.assertContains(response, "QMS-LT-001")


class CustodyTemplateOverrideTests(TestCase):
    def setUp(self):
        # Create organization structure
        self.tg = baker.make("organization.TenantGroup", name="TG1", slug="tg1")
        self.tenant_a = baker.make("organization.Tenant", name="Tenant A", slug="tenant-a", group=self.tg)
        self.tenant_b = baker.make("organization.Tenant", name="Tenant B", slug="tenant-b", group=self.tg)

        # Create Category
        self.category = baker.make("assets.Category", name="Custody Override Laptops", slug="custody-override-laptops")
        self.deployable = baker.make("assets.StatusLabel", type="deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Deployed")

        # Create global default template (linked to Category via ForeignKey)
        self.global_template = baker.make(
            CustodyTemplate,
            category=self.category,
            require_acceptance=True,
            email_signature_request=True,
            name="Global Laptop EULA",
            signature_provider="local",
            eula_text="Global EULA terms.",
            disclaimer="Please sign globally.",
            qms_reference="QMS-GLOBAL-01",
        )

        # Create Tenant A custom template (overriding for category Laptops)
        self.tenant_a_template = baker.make(
            CustodyTemplate,
            tenant=self.tenant_a,
            category=self.category,
            require_acceptance=True,
            email_signature_request=True,
            name="Tenant A Laptop EULA",
            signature_provider="local",
            eula_text="Tenant A specific laptop EULA terms.",
            disclaimer="Please sign for Tenant A.",
            qms_reference="QMS-TENANT-A-01",
        )

        # Assets & Holders
        self.asset_type = baker.make("assets.AssetType", model="Latitude", slug="latitude", category=self.category)
        self.asset_a = baker.make(
            Asset,
            name="Laptop A",
            asset_tag="TAG-LT-A",
            asset_type=self.asset_type,
            tenant=self.tenant_a,
            status=self.deployable,
        )
        self.asset_b = baker.make(
            Asset,
            name="Laptop B",
            asset_tag="TAG-LT-B",
            asset_type=self.asset_type,
            tenant=self.tenant_b,
            status=self.deployable,
        )

        self.holder_a = baker.make(
            AssetHolder, first_name="John", last_name="A", email="john@a.com", tenant=self.tenant_a
        )
        self.holder_b = baker.make(
            AssetHolder, first_name="John", last_name="B", email="john@b.com", tenant=self.tenant_b
        )

    def test_checkout_resolves_tenant_specific_category_override(self):
        from assets.services import checkout_asset

        # Checkout for Tenant A holder
        checkout_asset(
            asset=self.asset_a,
            holder=self.holder_a,
            checkout_date=timezone.now(),
            notes="Assigning laptop to Tenant A user",
            request=None,
        )

        # Verify receipt has Tenant A custom template values
        receipts = CustodyReceipt.objects.filter(asset=self.asset_a, holder=self.holder_a)
        self.assertEqual(receipts.count(), 1)
        receipt = receipts.first()

        self.assertEqual(receipt.custody_template, self.tenant_a_template)
        self.assertEqual(receipt.eula_text, "Tenant A specific laptop EULA terms.")
        self.assertEqual(receipt.qms_reference, "QMS-TENANT-A-01")

    def test_checkout_falls_back_to_global_category_template(self):
        from assets.services import checkout_asset

        # Checkout for Tenant B holder (no tenant-specific override template exists)
        checkout_asset(
            asset=self.asset_b,
            holder=self.holder_b,
            checkout_date=timezone.now(),
            notes="Assigning laptop to Tenant B user",
            request=None,
        )

        # Verify receipt falls back to Global template
        receipts = CustodyReceipt.objects.filter(asset=self.asset_b, holder=self.holder_b)
        self.assertEqual(receipts.count(), 1)
        receipt = receipts.first()

        self.assertEqual(receipt.custody_template, self.global_template)
        self.assertEqual(receipt.eula_text, "Global EULA terms.")
        self.assertEqual(receipt.qms_reference, "QMS-GLOBAL-01")

    def test_checkout_resolves_tenant_group_override(self):
        from assets.services import checkout_asset

        # Create a Tenant Group template (specific to the group, overriding global)
        group_template = baker.make(
            CustodyTemplate,
            tenant_group=self.tg,
            category=self.category,
            require_acceptance=True,
            email_signature_request=True,
            name="Group TG1 Laptop EULA",
            signature_provider="local",
            eula_text="Group TG1 specific EULA.",
            disclaimer="Please sign for TG1 Group.",
            qms_reference="QMS-GROUP-TG1",
        )

        # Checkout for Tenant B (no tenant-specific template, but belongs to group TG1)
        checkout_asset(
            asset=self.asset_b,
            holder=self.holder_b,
            checkout_date=timezone.now(),
            notes="Assigning laptop to Tenant B user",
            request=None,
        )

        # Verify receipt resolves to Group template instead of Global template
        receipts = CustodyReceipt.objects.filter(asset=self.asset_b, holder=self.holder_b)
        self.assertEqual(receipts.count(), 1)
        receipt = receipts.first()

        self.assertEqual(receipt.custody_template, group_template)
        self.assertEqual(receipt.eula_text, "Group TG1 specific EULA.")
        self.assertEqual(receipt.qms_reference, "QMS-GROUP-TG1")

    def test_checkout_respects_allow_global_custody_templates_disabled(self):
        from django.test import override_settings

        from assets.services import checkout_asset

        # Disallow global templates
        with override_settings(ALLOW_GLOBAL_CUSTODY_TEMPLATES=False):
            # Checkout for Tenant B (only global template available)
            checkout_asset(
                asset=self.asset_b,
                holder=self.holder_b,
                checkout_date=timezone.now(),
                notes="Assigning laptop with global disabled",
                request=None,
            )

            # Verify no receipt is created since global fallback is disabled
            receipts = CustodyReceipt.objects.filter(asset=self.asset_b, holder=self.holder_b)
            self.assertEqual(receipts.count(), 0)

    def test_form_validation_mutual_exclusion(self):
        from compliance.forms import CustodyTemplateForm

        form = CustodyTemplateForm(
            data={
                "name": "Invalid Template",
                "tenant": self.tenant_a.pk,
                "tenant_group": self.tg.pk,
                "signature_provider": "local",
                "eula_text": "Terms",
                "is_active": True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)
        self.assertEqual(
            form.errors["__all__"][0],
            "Choose either a Tenant or a Tenant Group for this template, not both.",
        )

    def test_form_validation_global_disallowed(self):
        from django.test import override_settings

        from compliance.forms import CustodyTemplateForm

        with override_settings(ALLOW_GLOBAL_CUSTODY_TEMPLATES=False):
            form = CustodyTemplateForm(
                data={
                    "name": "Invalid Global Template",
                    "signature_provider": "local",
                    "eula_text": "Terms",
                    "is_active": True,
                }
            )
            self.assertFalse(form.is_valid())
            self.assertIn("__all__", form.errors)
            self.assertEqual(
                form.errors["__all__"][0],
                "Global custody templates are disabled. You must select either a Tenant or a Tenant Group.",
            )
