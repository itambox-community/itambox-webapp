"""Custody RBAC, tenant-boundary, token, and consent regression tests for issue #259.

The fixture values in this module are deliberately dummy values. They are not
bearer credentials, signature payloads, or production EULA content.
"""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timedelta
from html.parser import HTMLParser
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APITestCase

from assets.models import Asset, AssetAssignment, AssetTagSequence, StatusLabel
from assets.services import checkout_asset
from compliance.models import CustodyHandoffDelivery, CustodyReceipt, CustodySigningSession, CustodyTemplate
from compliance.services import _custody_handoff_email_content
from compliance.tests.signatures import signature_png
from compliance.views import CustodyReceiptPrepareView
from core.events import DeliveryDisposition, DeliveryResult
from core.management.commands._seed.access import SeedAccessMixin
from core.managers import Scope
from core.models import ObjectChange
from core.tests.mixins import TenantTestMixin, grant
from extras.models import JournalEntry
from organization.models import AssetHolder, Role, RoleGrant, Tenant
from users.models import UserPreference

User = get_user_model()

DUMMY_TOKEN_A = "a" * 64
DUMMY_TOKEN_B = "b" * 64
DUMMY_OUTSIDER_TOKEN = "o" * 64
DUMMY_SESSION_TOKEN = "s" * 64
DUMMY_SESSION_TOKEN_B = "t" * 64
DUMMY_SIGNATURE = signature_png("dummy-signature-payload")
DUMMY_EULA = "dummy-eula-marker"
DUMMY_PASSWORD = "dummy-test-password"
WRONG_RECIPIENT_MESSAGE = "not the intended recipient"
WRONG_RECIPIENT_CODES = ("wrong-recipient", "wrong_recipient")


class CustodyRBACFixtureMixin(TenantTestMixin):
    """Build two tenants and the principals used by the issue matrix."""

    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Custody Tenant A", slug="custody-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Custody Tenant B", slug="custody-tenant-b")

        self.superadmin = User.objects.create_superuser(
            username="custody-superadmin",
            email="custody-superadmin@example.test",
            password=DUMMY_PASSWORD,
        )
        self.tenant_admin = self._make_member(
            "custody-admin",
            self.tenant_a,
            {
                "compliance.view_custodytemplate",
                "compliance.add_custodytemplate",
                "compliance.change_custodytemplate",
                "compliance.delete_custodytemplate",
                "compliance.view_custodyreceipt",
                "compliance.prepare_custodyreceipt",
                "compliance.export_custodyreceipt",
            },
        )
        self.technician = self._make_member(
            "custody-technician",
            self.tenant_a,
            {
                "compliance.view_custodytemplate",
                "compliance.view_custodyreceipt",
                "compliance.prepare_custodyreceipt",
            },
        )
        self.recipient = self._make_member("custody-recipient", self.tenant_a, set())
        self.unrelated = self._make_member("custody-unrelated", self.tenant_a, set())
        self.cross_tenant_user = self._make_member(
            "custody-cross-tenant",
            self.tenant_b,
            {"compliance.view_custodyreceipt"},
        )
        self._add_permissions(
            self.unrelated,
            self.tenant_a,
            {
                "assets.view_asset",
                "compliance.view_custodytemplate",
                "organization.view_assetholder",
            },
        )

        self.recipient_holder = AssetHolder.objects.create(
            user=self.recipient,
            first_name="Dummy",
            last_name="Recipient",
            upn="dummy-recipient@example.test",
            email="dummy-recipient@example.test",
            tenant=self.tenant_a,
        )
        self.unrelated_holder = AssetHolder.objects.create(
            user=self.unrelated,
            first_name="Dummy",
            last_name="Unrelated",
            upn="dummy-unrelated@example.test",
            email="dummy-unrelated@example.test",
            tenant=self.tenant_a,
        )
        self.cross_holder = AssetHolder.objects.create(
            user=self.cross_tenant_user,
            first_name="Dummy",
            last_name="CrossTenant",
            upn="dummy-cross-tenant@example.test",
            email="dummy-cross-tenant@example.test",
            tenant=self.tenant_b,
        )

        deployable = self._custody_status_label("Custody Deployable", "deployable")
        deployed = self._custody_status_label("Custody Deployed", "deployed")
        self.asset_a = baker.make(
            Asset,
            name="Dummy Custody Asset A",
            asset_tag="DUMMY-CUSTODY-ASSET-A",
            tenant=self.tenant_a,
            status=deployable,
        )
        self.asset_b = baker.make(
            Asset,
            name="Dummy Custody Asset B",
            asset_tag="DUMMY-CUSTODY-ASSET-B",
            tenant=self.tenant_b,
            status=deployable,
        )
        self.template_a = CustodyTemplate.objects.create(
            name="Dummy Custody Template A",
            tenant=self.tenant_a,
            eula_text=DUMMY_EULA,
        )
        self.template_b = CustodyTemplate.objects.create(
            name="Dummy Custody Template B",
            tenant=self.tenant_b,
            eula_text=DUMMY_EULA,
        )
        checkout_asset(self.asset_a, holder=self.recipient_holder, status=deployed, _suppress_custody_receipt=True)
        checkout_asset(self.asset_b, holder=self.cross_holder, status=deployed, _suppress_custody_receipt=True)
        self.assignment_a = AssetAssignment.objects.get(asset=self.asset_a, is_active=True)
        self.assignment_b = AssetAssignment.objects.get(asset=self.asset_b, is_active=True)
        self.receipt_a = CustodyReceipt.objects.create(
            asset=self.asset_a,
            assignment=self.assignment_a,
            holder=self.recipient_holder,
            custody_template=self.template_a,
            token=DUMMY_TOKEN_A,
            eula_text=DUMMY_EULA,
        )
        self.receipt_b = CustodyReceipt.objects.create(
            asset=self.asset_b,
            assignment=self.assignment_b,
            holder=self.cross_holder,
            custody_template=self.template_b,
            token=DUMMY_TOKEN_B,
            eula_text=DUMMY_EULA,
        )

    @staticmethod
    def _custody_status_label(name, label_type):
        """Provision a custody status label with a currently free primary key.

        ``CustodyConcurrentConsentTests`` resets the database sequences, and other
        suites commit their own reference rows, so a sequence-assigned key can
        collide with a row that is still present in this sparse table (#607).
        """
        from django.db.models import Max

        highest = StatusLabel._base_manager.aggregate(highest=Max("id"))["highest"] or 0
        return baker.make(StatusLabel, pk=highest + 1, type=label_type, name=name)

    @staticmethod
    def _make_member(username, tenant, permissions):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.test",
            password=DUMMY_PASSWORD,
        )
        role = Role.objects.create(
            tenant=tenant,
            name=f"{username} role",
            permissions=sorted(permissions),
        )
        grant(user, tenant, role)
        return user

    @staticmethod
    def _add_permissions(user, tenant, permissions):
        role = Role.objects.create(
            tenant=tenant,
            name=f"{user.username} surface role",
            permissions=sorted(permissions),
        )
        grant(user, tenant, role)

    def _login_to_tenant(self, user, tenant):
        self.client_login_to_tenant(user, tenant)

    def _sign_url(self, token):
        return reverse("compliance:custody_eula_sign", kwargs={"token": token})

    def _assert_no_receipt_payload(self, response, receipt):
        body = response.content.decode("utf-8", errors="replace")
        self.assertNotIn(receipt.asset.asset_tag, body)
        self.assertNotIn(str(receipt.holder), body)
        self.assertNotIn(DUMMY_EULA, body)

    def _assert_body_contains(self, response, text):
        self.assertIn(text, response.content.decode("utf-8", errors="replace"))

    def _assert_body_not_contains(self, response, text):
        self.assertNotIn(text, response.content.decode("utf-8", errors="replace"))

    def _assert_wrong_recipient(self, response):
        body = response.content.decode("utf-8", errors="replace")
        self.assertIn(WRONG_RECIPIENT_MESSAGE, body)
        self.assertTrue(any(code in body for code in WRONG_RECIPIENT_CODES))


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class CustodyRecipientConsentTests(CustodyRBACFixtureMixin, TestCase):
    """Recipient-only consent and the public token error contract."""

    def test_intended_recipient_can_accept(self):
        # AC §6: Rollen und Tenant-Grenze — intended Recipient can consent.
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 200)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertTrue(self.receipt_a.accepted)

    def test_authenticated_consent_audit_attributes_recipient_and_excludes_secrets(self):
        # AC §6: Audit — authenticated consent records the user/tenant without token or signature payload.
        self._login_to_tenant(self.recipient, self.tenant_a)
        self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        content_type = ContentType.objects.get_for_model(CustodyReceipt)
        change = (
            ObjectChange._base_manager.filter(
                changed_object_type=content_type,
                changed_object_id=self.receipt_a.pk,
            )
            .order_by("-time")
            .first()
        )

        self.assertIsNotNone(change)
        self.assertEqual(change.user_id, self.recipient.pk)
        self.assertEqual(change.tenant_id, self.tenant_a.pk)
        audit_data = json.dumps({"pre": change.prechange_data, "post": change.postchange_data})
        self.assertNotIn(DUMMY_TOKEN_A, audit_data)
        self.assertNotIn(DUMMY_SIGNATURE, audit_data)

    def test_superadmin_cannot_override_recipient_binding(self):
        # AC §6: Rollen und Tenant-Grenze — superadmin has global internal power but no Recipient override.
        self._login_to_tenant(self.superadmin, self.tenant_a)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self._assert_wrong_recipient(response)
        self._assert_no_receipt_payload(response, self.receipt_a)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_unrelated_same_tenant_is_wrong_recipient_without_mutation(self):
        # AC §6: Rollen und Tenant-Grenze — unrelated same-tenant user gets wrong-recipient.
        self._login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self._assert_wrong_recipient(response)
        self._assert_body_not_contains(response, "internal custody permission")
        self._assert_no_receipt_payload(response, self.receipt_a)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_invalid_token_is_neutral_404_without_payload(self):
        # AC §6: Token, Ablauf und Fehler — invalid token → 404.
        self._login_to_tenant(self.recipient, self.tenant_a)

        for token in ("invalid-dummy-token", "x" * 64):
            with self.subTest(token_length=len(token)):
                response = self.client.get(self._sign_url(token))

                self.assertEqual(response.status_code, 404)
                self._assert_no_receipt_payload(response, self.receipt_a)

    def test_expired_token_is_410_without_payload(self):
        # AC §6: Token, Ablauf und Fehler — expired token → 410 without payload.
        CustodyReceipt.objects.filter(pk=self.receipt_a.pk).update(
            created_date=self.receipt_a.created_date - timedelta(days=8),
        )
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.get(self._sign_url(DUMMY_TOKEN_A))

        self.assertEqual(response.status_code, 410)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_empty_signature_does_not_accept(self):
        # AC §6: Prepare- und Consent-Semantik — empty signature is not acceptance.
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": "empty"},
        )

        self.assertEqual(response.status_code, 200)
        self._assert_body_contains(response, "valid signature")
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)
        self.assertFalse(self.receipt_a.accepted)

    def test_decline_is_distinct_from_empty_and_accept(self):
        # AC §6: Prepare- und Consent-Semantik — decline remains a separate terminal state.
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(self._sign_url(DUMMY_TOKEN_A), {"action": "decline"})

        self.assertEqual(response.status_code, 200)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_DECLINED)
        self.assertFalse(self.receipt_a.accepted)

    def test_completed_receipt_cannot_be_accepted_again(self):
        # AC §6: Prepare- und Consent-Semantik — already accepted remains completed.
        self.receipt_a.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt_a.save(update_fields=["acceptance_status", "updated_at"])
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 200)
        self._assert_body_not_contains(response, "valid signature")
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)

    def test_completed_declined_receipt_cannot_be_accepted_again(self):
        # AC §6: Prepare- und Consent-Semantik — already declined remains declined.
        self.receipt_a.acceptance_status = CustodyReceipt.STATUS_DECLINED
        self.receipt_a.save(update_fields=["acceptance_status", "updated_at"])
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 200)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_DECLINED)
        self.assertFalse(self.receipt_a.accepted)

    def test_authenticated_cross_tenant_recipient_cannot_sign_foreign_receipt(self):
        # AC §6: Rollen und Tenant-Grenze — cross-tenant recipient cannot consent through UI.
        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self._assert_wrong_recipient(response)
        self._assert_no_receipt_payload(response, self.receipt_a)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)


class CustodyPermissionMetadataTests(TestCase):
    def test_prepare_and_export_permissions_are_published_model_permissions(self):
        # AC §6: Rollen und Tenant-Grenze — prepare/export are stable model codenames.
        content_type = ContentType.objects.get(app_label="compliance", model="custodyreceipt")

        codenames = set(Permission.objects.filter(content_type=content_type).values_list("codename", flat=True))

        self.assertIn("prepare_custodyreceipt", codenames)
        self.assertIn("export_custodyreceipt", codenames)

    def test_seed_technician_role_is_narrow_and_has_no_export(self):
        # AC §6: Rollen und Tenant-Grenze — seeded Technician has view+prepare, not export or template policy writes.
        provider = Tenant.objects.create(name="Seed Provider", slug="seed-provider", is_provider=True)
        customer = Tenant.objects.create(
            name="Seed Customer",
            slug="seed-customer",
            managed_by=provider,
        )
        customer_b = Tenant.objects.create(
            name="Seed Customer B",
            slug="seed-customer-b",
            managed_by=provider,
        )
        customer_c = Tenant.objects.create(
            name="Seed Customer C",
            slug="seed-customer-c",
            managed_by=provider,
        )

        command = SeedAccessMixin()
        command.stdout = StringIO()
        command._tenants = {
            "seed-provider": provider,
            "seed-customer": customer,
            "seed-customer-b": customer_b,
            "seed-customer-c": customer_c,
        }
        command._tenant_meta = {
            "seed-provider": {"kind": "msp", "group_slug": "seed"},
            "seed-customer": {"kind": "customer", "group_slug": "helix-biopharma"},
            "seed-customer-b": {"kind": "customer", "group_slug": "sterling-am"},
            "seed-customer-c": {"kind": "customer", "group_slug": "meridian-bank"},
        }
        command._tenant_holders = {
            "seed-provider": [],
            "seed-customer": [],
            "seed-customer-b": [],
            "seed-customer-c": [],
        }
        command._provider_tenant = provider
        command._orgs = []

        command._seed_access()

        technician = Role.objects.get(tenant=customer, name="Technician")
        permissions = set(technician.permissions)
        self.assertIn("compliance.view_custodyreceipt", permissions)
        self.assertIn("compliance.prepare_custodyreceipt", permissions)
        self.assertNotIn("compliance.export_custodyreceipt", permissions)
        self.assertNotIn("compliance.add_custodytemplate", permissions)
        self.assertNotIn("compliance.change_custodytemplate", permissions)
        self.assertNotIn("compliance.delete_custodytemplate", permissions)


@override_settings(REQUIRE_CUSTODY_SIGNIN=False)
class CustodyBearerPolicyTests(CustodyRBACFixtureMixin, TestCase):
    """Explicit non-login mode remains covered without creating an operator path."""

    def test_anonymous_consent_is_rejected_when_signin_setting_is_optional(self):
        # AC §6: Token, Ablauf und Fehler — REQUIRE_CUSTODY_SIGNIN=False is explicit and tested.
        self.client.logout()

        response = self.client.post(
            self._sign_url(DUMMY_TOKEN_A),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self._assert_no_receipt_payload(response, self.receipt_a)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)
        self.assertIsNone(self.receipt_a.signed_at)


class CustodyConcurrentConsentTests(CustodyRBACFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def _post_from_independent_client(self, signature):
        close_old_connections()
        try:
            client = self.client_class()
            client.force_login(self.recipient)
            session = client.session
            session["active_tenant_id"] = self.tenant_a.pk
            session.save()
            return client.post(
                self._sign_url(DUMMY_TOKEN_A),
                {"action": "accept", "signature_canvas": signature},
            )
        finally:
            close_old_connections()

    def _post_action_from_independent_client(self, action, signature=None):
        close_old_connections()
        try:
            client = self.client_class()
            client.force_login(self.recipient)
            session = client.session
            session["active_tenant_id"] = self.tenant_a.pk
            session.save()
            data = {"action": action}
            if signature is not None:
                data["signature_canvas"] = signature
            return client.post(self._sign_url(DUMMY_TOKEN_A), data)
        finally:
            close_old_connections()

    def test_concurrent_accept_posts_have_one_terminal_transition(self):
        # AC §6: Prepare- und Consent-Semantik — concurrent POSTs are serialized by select_for_update().
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self._post_from_independent_client, signature_png("dummy-signature-one")),
                executor.submit(self._post_from_independent_client, signature_png("dummy-signature-two")),
            ]
            responses = [future.result() for future in futures]

        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(sum(response.status_code == 200 for response in responses), 2)
        self.assertEqual(
            CustodyReceipt.objects.filter(
                pk=self.receipt_a.pk, acceptance_status=CustodyReceipt.STATUS_ACCEPTED
            ).count(),
            1,
        )

    def test_concurrent_accept_and_decline_have_one_terminal_state(self):
        # AC §6: Prepare- und Consent-Semantik — concurrent Accept/Decline posts serialize to one terminal state.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self._post_action_from_independent_client, "accept", signature_png("dummy-accept-signature")
                ),
                executor.submit(self._post_action_from_independent_client, "decline"),
            ]
            responses = [future.result() for future in futures]

        self.receipt_a.refresh_from_db()
        self.assertEqual(sum(response.status_code == 200 for response in responses), 2)
        self.assertIn(
            self.receipt_a.acceptance_status,
            (CustodyReceipt.STATUS_ACCEPTED, CustodyReceipt.STATUS_DECLINED),
        )
        if self.receipt_a.acceptance_status == CustodyReceipt.STATUS_ACCEPTED:
            self.assertTrue(self.receipt_a.accepted)
            self.assertIsNotNone(self.receipt_a.signed_at)
        else:
            self.assertFalse(self.receipt_a.accepted)
            self.assertIsNone(self.receipt_a.signed_at)

    def test_parallel_signing_by_a_holder_without_membership_is_served(self):
        """#607/#306: parallel accepts by a holder without membership must both be
        answered. The former full asset save read the global tag sequence through
        a tenant-scoped manager on the signer's behalf, so both posts crashed."""
        outsider = User.objects.create_user(username="consent-outsider", password="x")
        outsider_holder = AssetHolder.objects.create(
            user=outsider,
            first_name="Dummy",
            last_name="Outsider",
            upn="dummy-outsider@example.test",
            email="dummy-outsider@example.test",
            tenant=self.tenant_a,
        )
        asset = baker.make(
            Asset,
            name="Dummy Outsider Asset",
            asset_tag="DUMMY-OUTSIDER-ASSET",
            tenant=self.tenant_a,
            status=StatusLabel.objects.get(name="Custody Deployable"),
        )
        checkout_asset(
            asset,
            holder=outsider_holder,
            status=StatusLabel.objects.get(name="Custody Deployed"),
            _suppress_custody_receipt=True,
        )
        receipt = CustodyReceipt.objects.create(
            asset=asset,
            assignment=AssetAssignment.objects.get(asset=asset, is_active=True),
            holder=outsider_holder,
            custody_template=self.template_a,
            token=DUMMY_OUTSIDER_TOKEN,
            eula_text=DUMMY_EULA,
        )
        # The global default sequence exists, as in the reported reproduction; the
        # signer holds no scope that could see it.
        AssetTagSequence._base_manager.get_or_create(
            tenant=None, category=None, prefix="ASSET-", defaults={"next_value": 1}
        )

        def post(signature):
            close_old_connections()
            try:
                client = self.client_class()
                client.force_login(outsider)
                return client.post(
                    self._sign_url(DUMMY_OUTSIDER_TOKEN),
                    {"action": "accept", "signature_canvas": signature},
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(post, signature_png("outsider-signature-one")),
                executor.submit(post, signature_png("outsider-signature-two")),
            ]
            responses = [future.result() for future in futures]

        self.assertEqual([response.status_code for response in responses], [200, 200])
        receipt.refresh_from_db()
        self.assertEqual(receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(
            AssetTagSequence._base_manager.get(tenant__isnull=True, category__isnull=True, prefix="ASSET-").next_value,
            1,
            "signing must not allocate or consume an asset tag",
        )

    def test_sign_post_contains_row_lock(self):
        # AC §6: Prepare- und Consent-Semantik — receipt POST must use a row lock.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._login_to_tenant(self.recipient, self.tenant_a)
        with CaptureQueriesContext(connection) as queries:
            self.client.post(
                self._sign_url(DUMMY_TOKEN_A),
                {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
            )

        self.assertTrue(
            any(
                "custodyreceipt" in query["sql"].lower() and "for update" in query["sql"].lower()
                for query in queries.captured_queries
            ),
            "recipient consent must lock the receipt row",
        )


def reverse_if_available(*names, kwargs=None):
    """Resolve the first compatible route spelling in the target implementation."""
    for name in names:
        try:
            return reverse(name, kwargs=kwargs or {})
        except NoReverseMatch:
            continue
    return None


class CustodyInternalRouteTests(CustodyRBACFixtureMixin, TestCase):
    """Authenticated internal receipt views are separate from recipient consent."""

    def setUp(self):
        super().setUp()
        self.internal_list_url = reverse_if_available(
            "compliance:custodyreceipt_list",
            "compliance:custodyreceipt-list",
        )
        self.internal_detail_url = reverse_if_available(
            "compliance:custodyreceipt_detail",
            "compliance:custodyreceipt-detail",
            kwargs={"pk": self.receipt_a.pk},
        )
        self.prepare_url = reverse_if_available(
            "compliance:custodyreceipt_prepare",
            "compliance:custodyreceipt-prepare",
            kwargs={"pk": self.receipt_a.pk},
        )
        self.export_url = reverse_if_available(
            "compliance:custodyreceipt_export",
            "compliance:custodyreceipt-export",
            kwargs={"pk": self.receipt_a.pk},
        )

    def _require_url(self, url, surface):
        self.assertIsNotNone(url, f"SOL must expose the {surface} route")
        return url

    def _assert_internal_denial(self, response):
        body = response.content.decode("utf-8", errors="replace").lower()
        self.assertNotIn("wrong-recipient", body)
        self.assertNotIn("not the intended recipient", body)
        self.assertTrue("internal" in body or "custody" in body)

    def test_superadmin_sees_global_internal_receipt_list(self):
        # AC §6: Rollen und Tenant-Grenze — superadmin sees receipts globally on the internal path.
        url = self._require_url(self.internal_list_url, "internal receipt list")
        self.client.force_login(self.superadmin)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self._assert_body_contains(response, self.asset_a.asset_tag)
        self._assert_body_contains(response, self.asset_b.asset_tag)
        self._assert_body_not_contains(response, DUMMY_TOKEN_A)
        self._assert_body_not_contains(response, DUMMY_TOKEN_B)

    def test_tenant_admin_sees_own_tenant_and_foreign_detail_is_404(self):
        # AC §6: Rollen und Tenant-Grenze — Tenant Admin is own-tenant only; foreign detail → 404.
        list_url = self._require_url(self.internal_list_url, "internal receipt list")
        foreign_detail_url = self._require_url(
            reverse_if_available(
                "compliance:custodyreceipt_detail",
                "compliance:custodyreceipt-detail",
                kwargs={"pk": self.receipt_b.pk},
            ),
            "internal receipt detail",
        )
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        list_response = self.client.get(list_url)
        detail_response = self.client.get(foreign_detail_url)

        self.assertEqual(list_response.status_code, 200)
        self._assert_body_contains(list_response, self.asset_a.asset_tag)
        self._assert_body_not_contains(list_response, self.asset_b.asset_tag)
        self.assertEqual(detail_response.status_code, 404)
        self._assert_no_receipt_payload(detail_response, self.receipt_b)

    def test_tenant_admin_cannot_open_foreign_template_detail(self):
        # AC §6: Rollen und Tenant-Grenze — Tenant Admin cannot view a foreign template.
        url = reverse("compliance:custodytemplate_detail", kwargs={"pk": self.template_b.pk})
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)
        self._assert_body_not_contains(response, self.template_b.name)
        self._assert_body_not_contains(response, DUMMY_EULA)

    def test_technician_can_list_and_detail_but_raw_token_is_not_rendered(self):
        # AC §6: Rollen und Tenant-Grenze — Technician gets internal view/detail only.
        list_url = self._require_url(self.internal_list_url, "internal receipt list")
        detail_url = self._require_url(self.internal_detail_url, "internal receipt detail")
        self._login_to_tenant(self.technician, self.tenant_a)

        list_response = self.client.get(list_url)
        detail_response = self.client.get(detail_url)

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self._assert_body_contains(detail_response, self.asset_a.asset_tag)
        self._assert_body_not_contains(list_response, DUMMY_TOKEN_A)
        self._assert_body_not_contains(detail_response, DUMMY_TOKEN_A)

    def test_internal_route_without_permission_is_403_not_recipient_error(self):
        # AC §6: Token, Ablauf und Fehler — missing internal permission → internal 403.
        url = self._require_url(self.internal_list_url, "internal receipt list")
        self._login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 403)
        self._assert_internal_denial(response)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_expired_role_grant_is_denied_closed(self):
        # AC §6: Rollen und Tenant-Grenze — expired RoleGrant scope cannot read internal receipts.
        RoleGrant.objects.filter(membership__user=self.technician, role__tenant=self.tenant_a).update(
            valid_until=timezone.now() - timedelta(minutes=1)
        )
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.get(self.internal_list_url)

        self.assertEqual(response.status_code, 403)
        self._assert_body_not_contains(response, WRONG_RECIPIENT_MESSAGE)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_asset_detail_hides_custody_surface_without_receipt_permission(self):
        # AC §6: Inventar-Surfaces — asset detail must not expose bearer token or signing action.
        url = reverse("assets:asset_detail", kwargs={"pk": self.asset_a.pk})
        self._login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self._assert_body_not_contains(response, DUMMY_TOKEN_A)
        self._assert_body_not_contains(response, "custody_eula_sign")

    def test_template_detail_hides_receipts_without_receipt_permission(self):
        # AC §6: Inventar-Surfaces — Template detail must not embed receipt rows without view permission.
        url = reverse("compliance:custodytemplate_detail", kwargs={"pk": self.template_a.pk})
        self._login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self._assert_body_not_contains(response, self.asset_a.asset_tag)
        self._assert_body_not_contains(response, DUMMY_TOKEN_A)

    def test_holder_detail_does_not_expose_bearer_token(self):
        # AC §6: Inventar-Surfaces — holder detail must not expose a receipt token.
        url = reverse("organization:assetholder_detail", kwargs={"pk": self.recipient_holder.pk})
        self._login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self._assert_body_not_contains(response, DUMMY_TOKEN_A)

    def test_cross_tenant_internal_detail_is_404_without_payload(self):
        # AC §6: Rollen und Tenant-Grenze — cross-tenant UI detail → 404.
        url = self._require_url(self.internal_detail_url, "internal receipt detail")
        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_technician_cannot_change_template_policy(self):
        # AC §6: Rollen und Tenant-Grenze — Technician template policy is not mutable.
        url = reverse("compliance:custodytemplate_update", kwargs={"pk": self.template_a.pk})
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(CustodyTemplate.objects.get(pk=self.template_a.pk).eula_text, DUMMY_EULA)

    def test_technician_export_is_denied_by_default(self):
        # AC §6: API und interne Darstellung — Technician has no export capability by default.
        # The export codename is seeded as denied; the internal export route itself is a
        # documented follow-up (#259, Slice D) and is exercised here when present.
        self._login_to_tenant(self.technician, self.tenant_a)

        self.assertFalse(self.technician.has_perm("compliance.export_custodyreceipt", obj=self.tenant_a))
        if self.export_url is not None:
            response = self.client.get(self.export_url)

            self.assertEqual(response.status_code, 403)
            self._assert_no_receipt_payload(response, self.receipt_a)

    def test_prepare_capability_is_seeded_and_route_is_exposed(self):
        # AC §6: Prepare- und Consent-Semantik — the technician prepare capability is seeded
        # and the prepare-session/export routes (Slice D) are exposed for the same tenant.
        self._login_to_tenant(self.technician, self.tenant_a)

        self.assertTrue(self.technician.has_perm("compliance.prepare_custodyreceipt", obj=self.tenant_a))
        self.assertIsNotNone(self.prepare_url)
        self.assertIsNotNone(self.export_url)


class CustodyAPIContractTests(CustodyRBACFixtureMixin, APITestCase):
    """REST API tenant scope and read-only consent state."""

    def _api_login_to_tenant(self, user, tenant):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

    def _api_url(self, name, pk=None):
        kwargs = {"pk": pk} if pk is not None else {}
        return reverse(f"api:compliance_api:custodyreceipt-{name}", kwargs=kwargs)

    def test_technician_api_list_is_tenant_scoped(self):
        # AC §6: API und interne Darstellung — receipt API list is tenant-scoped.
        self._api_login_to_tenant(self.technician, self.tenant_a)

        response = self.client.get(self._api_url("list"))

        self.assertEqual(response.status_code, 200)
        payload = response.data.get("results", response.data)
        rendered = str(payload)
        self.assertIn(self.asset_a.asset_tag, rendered)
        self.assertNotIn(self.asset_b.asset_tag, rendered)

    def test_unrelated_api_principal_is_denied_without_recipient_error(self):
        # AC §6: API und interne Darstellung — same-tenant user without internal permission → 403.
        self._api_login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.get(self._api_url("list"))

        self.assertEqual(response.status_code, 403)
        rendered = str(response.data).lower()
        self.assertIn("permission", rendered)
        self.assertNotIn(WRONG_RECIPIENT_MESSAGE, rendered)
        self.assertNotIn(DUMMY_TOKEN_A, rendered)

    def test_superadmin_api_list_is_global_without_active_tenant(self):
        # AC §6: Rollen und Tenant-Grenze — superadmin API list is global when no tenant is selected.
        self.client.force_login(self.superadmin)

        response = self.client.get(self._api_url("list"))

        self.assertEqual(response.status_code, 200)
        rendered = str(response.data)
        self.assertIn(self.asset_a.asset_tag, rendered)
        self.assertIn(self.asset_b.asset_tag, rendered)

    def test_cross_tenant_api_detail_is_404(self):
        # AC §6: Rollen und Tenant-Grenze — cross-tenant API detail → 404.
        self._api_login_to_tenant(self.cross_tenant_user, self.tenant_b)

        response = self.client.get(self._api_url("detail", self.receipt_a.pk))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.asset_a.asset_tag, str(response.data))
        self.assertNotIn(DUMMY_EULA, str(response.data))

    def test_api_patch_cannot_forge_acceptance(self):
        # AC §6: Prepare- und Consent-Semantik — API PATCH cannot forge acceptance.
        self._api_login_to_tenant(self.tenant_admin, self.tenant_a)

        response = self.client.patch(
            self._api_url("detail", self.receipt_a.pk),
            {
                "accepted": True,
                "acceptance_status": CustodyReceipt.STATUS_ACCEPTED,
                "signature_data": DUMMY_SIGNATURE,
                "signature_hash": "dummy-signature-hash",
            },
            format="json",
        )

        # The receipt API is read-only: 403 when the role lacks change, 405 otherwise.
        self.assertIn(response.status_code, (403, 405))
        self.receipt_a.refresh_from_db()
        self.assertFalse(self.receipt_a.accepted)
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)


@override_settings(CUSTODY_SIGNING_SESSION_TTL=timedelta(minutes=30))
class CustodySigningSessionPrepareTests(CustodyRBACFixtureMixin, TestCase):
    def _prepare_url(self, receipt=None):
        receipt = receipt or self.receipt_a
        return reverse("compliance:custodyreceipt_prepare", kwargs={"pk": receipt.pk})

    def test_prepare_creates_operator_bound_short_lived_session_and_handoff(self):
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.post(
            self._prepare_url(),
            {
                "operator": self.unrelated.pk,
                "intended_holder": self.unrelated_holder.pk,
            },
        )

        self.assertRedirects(
            response,
            reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk}),
            fetch_redirect_response=False,
        )
        signing_session = CustodySigningSession._base_manager.get(receipt=self.receipt_a)
        self.assertEqual(signing_session.operator, self.technician)
        self.assertEqual(signing_session.intended_holder, self.recipient_holder)
        self.assertAlmostEqual(
            (signing_session.expires_at - signing_session.created_at).total_seconds(),
            30 * 60,
            delta=2,
        )
        self.assertIsNone(signing_session.consumed_at)
        self.assertIsNone(signing_session.canceled_at)
        self.assertEqual(signing_session.outcome, "")

        detail_response = self.client.get(response.url)
        self.assertContains(detail_response, "Recipient handoff is ready")
        self.assertContains(detail_response, signing_session.token)
        self.assertContains(detail_response, DUMMY_TOKEN_A)
        # Handoff actions must wrap on narrow viewports: the button group is a
        # flex-wrap container so the copy / QR / e-mail actions never collapse
        # into overlapping vertical strips on mobile (demo finding 2026-08-10).
        self.assertContains(detail_response, 'class="d-flex flex-wrap gap-2 mt-2"')
        # Export must not be intercepted by htmx boost: a native download
        # (Content-Disposition attachment) survives only without hx-boost.
        self.assertContains(detail_response, 'hx-boost="false"')

    def test_different_operator_sees_session_audit_without_handoff_tokens(self):
        self._login_to_tenant(self.technician, self.tenant_a)
        self.client.post(self._prepare_url())
        signing_session = CustodySigningSession._base_manager.get(receipt=self.receipt_a)

        self._login_to_tenant(self.tenant_admin, self.tenant_a)
        response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.technician.username)
        self.assertContains(response, "Active")
        self.assertNotContains(response, signing_session.token)
        self.assertNotContains(response, DUMMY_TOKEN_A)

    def test_prepare_without_permission_is_internal_403(self):
        self._login_to_tenant(self.unrelated, self.tenant_a)

        response = self.client.post(self._prepare_url())

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "compliance/custody/internal_permission_error.html")
        self.assertContains(response, "internal_custody_permission_required", status_code=403)
        self.assertFalse(CustodySigningSession._base_manager.filter(receipt=self.receipt_a).exists())

    def test_prepare_for_foreign_tenant_is_neutral_404(self):
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.post(self._prepare_url(self.receipt_b))

        self.assertEqual(response.status_code, 404)
        self._assert_no_receipt_payload(response, self.receipt_b)
        self.assertFalse(CustodySigningSession._base_manager.filter(receipt=self.receipt_b).exists())

    def test_completed_receipt_is_rejected_without_creating_session(self):
        self.receipt_a.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt_a.save(update_fields=["acceptance_status", "updated_at"])
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.post(self._prepare_url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only pending custody receipts")
        self.assertFalse(CustodySigningSession._base_manager.filter(receipt=self.receipt_a).exists())

    def test_holderless_pending_receipt_is_rejected_without_creating_session(self):
        holderless_receipt = self.receipt_a
        holderless_receipt.holder_id = None
        self._login_to_tenant(self.technician, self.tenant_a)

        with patch.object(CustodyReceiptPrepareView, "get_object", return_value=holderless_receipt):
            response = self.client.post(self._prepare_url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "requires an intended holder")
        self.assertFalse(CustodySigningSession._base_manager.filter(receipt=self.receipt_a).exists())


class _HandoffPanelParser(HTMLParser):
    """Extract mobile-safety facts from the real handoff panel markup."""

    def __init__(self):
        super().__init__()
        self.action_row_depth = 0
        self.reason_in_row = False
        self.panel_seen = False
        self.figure_mx0 = False
        self.img = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "d-flex" in classes and "flex-wrap" in classes:
            self.action_row_depth = 1
        elif self.action_row_depth:
            self.action_row_depth += 1
        if tag == "div" and attributes.get("id", "").startswith("custody-handoff-qr-"):
            self.panel_seen = True
        if tag == "figure" and "mx-0" in classes:
            self.figure_mx0 = True
        if tag == "img":
            img_classes = set(attributes.get("class", "").split())
            self.img = {
                "img-fluid": "img-fluid" in img_classes,
                "qr-class": "custody-handoff-qr" in img_classes,
                "width": attributes.get("width"),
                "height": attributes.get("height"),
            }

    def handle_endtag(self, tag):
        if self.action_row_depth:
            self.action_row_depth -= 1

    def handle_data(self, data):
        if self.action_row_depth and "E-mail is not configured" in data:
            self.reason_in_row = True


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class CustodySigningSessionHandoffTests(CustodyRBACFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.signing_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token=DUMMY_SESSION_TOKEN,
            expires_at=timezone.now() + timedelta(minutes=30),
        )

    def _handoff_url(self, session_token=DUMMY_SESSION_TOKEN, receipt_token=DUMMY_TOKEN_A):
        return f"{self._sign_url(receipt_token)}?session={session_token}"

    def _qr_url(self, receipt=None, session=None):
        receipt = receipt or self.receipt_a
        session = session or self.signing_session
        return reverse(
            "compliance:custodyreceipt_handoff_qr",
            kwargs={"pk": receipt.pk, "session_pk": session.pk},
        )

    def _email_url(self, receipt=None, session=None):
        receipt = receipt or self.receipt_a
        session = session or self.signing_session
        return reverse(
            "compliance:custodyreceipt_handoff_email",
            kwargs={"pk": receipt.pk, "session_pk": session.pk},
        )

    def _login_operator(self):
        self._login_to_tenant(self.technician, self.tenant_a)

    def test_old_receipt_with_valid_session_skips_link_ttl(self):
        # Regression (#300): a freshly prepared signing session is the
        # operator-authorized handoff channel and must override the 7-day
        # bearer-link TTL — otherwise the assisted handoff is dead on every
        # receipt older than CUSTODY_LINK_TTL (hit live on demo.itambox.dev:
        # seed receipts from 2026-07-18 returned 410 custody_link_expired
        # despite a valid session).
        CustodyReceipt.objects.filter(pk=self.receipt_a.pk).update(
            created_date=timezone.now() - timedelta(days=8),
        )
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.get(self._handoff_url())

        self.assertEqual(response.status_code, 200)

    def test_internal_detail_renders_copy_qr_and_email_controls_without_extra_tokens(self):
        self._login_operator()
        with patch("compliance.views.custody_handoff_email_is_configured", return_value=True):
            response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk}))
        body = response.content.decode("utf-8", errors="replace")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Copy recipient handoff link", body)
        self.assertIn("Show QR code", body)
        self.assertIn(self._qr_url(), body)
        self.assertIn(self._email_url(), body)
        self.assertIn("csrfmiddlewaretoken", body)
        self.assertEqual(body.count(DUMMY_TOKEN_A), 1)
        self.assertEqual(body.count(DUMMY_SESSION_TOKEN), 1)
        self.assertIn('data-bs-toggle="collapse"', body)
        self.assertIn('hx-boost="false"', body)
        self.assertIn("30", body)

    def test_internal_detail_hides_delivery_controls_without_live_operator_session(self):
        self._login_to_tenant(self.technician, self.tenant_a)
        detail_url = reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk})

        self.signing_session.expires_at = timezone.now() - timedelta(seconds=1)
        self.signing_session.save(update_fields=["expires_at", "updated_at"])
        response = self.client.get(detail_url)
        self.assertNotContains(response, "Copy recipient handoff link")
        self.assertNotContains(response, "Show QR code")
        self.assertNotContains(response, "E-mail link to holder")

        response = self.client.get(detail_url)
        self.assertNotContains(response, "Copy recipient handoff link")

        self.signing_session.expires_at = timezone.now() + timedelta(minutes=30)
        self.signing_session.consumed_at = timezone.now()
        self.signing_session.outcome = CustodySigningSession.OUTCOME_ACCEPTED
        self.signing_session.save(update_fields=["expires_at", "consumed_at", "outcome", "updated_at"])
        response = self.client.get(detail_url)
        self.assertNotContains(response, "Copy recipient handoff link")

        self.receipt_a.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt_a.save(update_fields=["acceptance_status", "updated_at"])
        response = self.client.get(detail_url)
        self.assertNotContains(response, "Copy recipient handoff link")

        self._login_to_tenant(self.tenant_admin, self.tenant_a)
        response = self.client.get(detail_url)
        self.assertNotContains(response, "Copy recipient handoff link")

    def test_qr_endpoint_returns_safe_svg_and_headers(self):
        self._login_operator()

        response = self.client.get(self._qr_url())
        body = response.content

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(
            response["Content-Disposition"], f'inline; filename="custody-handoff-{self.signing_session.pk}.svg"'
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "default-src 'none'")
        self.assertTrue(body.startswith(b"<svg"))
        self.assertIn(b"<path", body)
        # Regression: the SVG must ship its own opaque quiet zone so the
        # symbol stays decodable under [data-bs-theme="dark"] and WebView
        # algorithmic darkening (black-on-transparent was invisible there).
        # segno normalises the CUSTODY_HANDOFF_QR_DARK/LIGHT hex constants
        # (#000000/#ffffff) to their short forms in the emitted markup.
        self.assertIn(b"#000", body)
        self.assertIn(b"#fff", body)
        self.assertNotIn(DUMMY_TOKEN_A.encode(), body)
        self.assertNotIn(DUMMY_SESSION_TOKEN.encode(), body)
        self.assertNotIn(b"<script", body.lower())
        self.assertNotIn(b"<foreignobject", body.lower())

    def test_qr_endpoint_uses_shared_configured_base_url(self):
        self._login_operator()
        with (
            override_settings(ITAMBOX_BASE_URL="https://public.example.test"),
            patch("compliance.views.render_custody_handoff_qr_svg", return_value=b"<svg />") as render_qr,
        ):
            response = self.client.get(self._qr_url())

        self.assertEqual(response.status_code, 200)
        encoded_url = render_qr.call_args.args[0]
        self.assertTrue(encoded_url.startswith("https://public.example.test/"))
        self.assertIn(DUMMY_TOKEN_A, encoded_url)
        self.assertIn(DUMMY_SESSION_TOKEN, encoded_url)

    def test_qr_endpoint_enforces_internal_scope_and_terminal_surface(self):
        self._login_to_tenant(self.unrelated, self.tenant_a)
        denied = self.client.get(self._qr_url())
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn(DUMMY_TOKEN_A, denied.content.decode())

        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)
        foreign = self.client.get(self._qr_url())
        self.assertEqual(foreign.status_code, 404)
        self._assert_no_receipt_payload(foreign, self.receipt_a)

        self._login_operator()
        self.signing_session.expires_at = timezone.now() - timedelta(seconds=1)
        self.signing_session.save(update_fields=["expires_at", "updated_at"])
        expired = self.client.get(self._qr_url())
        self.assertEqual(expired.status_code, 410)
        self.assertContains(expired, "internal_custody_handoff_unavailable", status_code=410)
        self.assertNotContains(expired, "custody_session_expired_or_used", status_code=410)

        self.signing_session.expires_at = timezone.now() + timedelta(minutes=30)
        self.signing_session.save(update_fields=["expires_at", "updated_at"])
        self.receipt_a.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt_a.save(update_fields=["acceptance_status", "updated_at"])
        completed = self.client.get(self._qr_url())
        self.assertEqual(completed.status_code, 410)
        self.assertNotContains(completed, "custody_session_expired_or_used", status_code=410)

    def test_email_post_derives_holder_recipient_and_records_success(self):
        self._login_operator()
        result = DeliveryResult("email.deliver", DeliveryDisposition.SUCCESS)
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services.send_email_notification", return_value=result) as send_email,
            patch("compliance.services._cooldown_allows_handoff", return_value=True),
        ):
            response = self.client.post(self._email_url(), {"recipient": "attacker@example.test"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(send_email.call_args.args[0], [self.recipient_holder.email])
        subject, body = send_email.call_args.args[1:3]
        self.assertIn(self.asset_a.asset_tag, subject)
        self.assertIn(self.asset_a.asset_tag, body)
        self.assertIn(DUMMY_TOKEN_A, body)
        self.assertIn(DUMMY_SESSION_TOKEN, body)
        self.assertNotIn("attacker@example.test", body)
        delivery = CustodyHandoffDelivery._base_manager.get(signing_session=self.signing_session)
        self.assertEqual(delivery.attempt, 1)
        self.assertEqual(delivery.status, CustodyHandoffDelivery.STATUS_SUCCEEDED)
        self.assertIsNotNone(delivery.delivered_at)

    def test_email_post_maps_retry_and_terminal_without_success_message(self):
        self._login_operator()
        outcomes = (
            DeliveryResult("email.deliver", DeliveryDisposition.RETRYABLE, error_class="timeout"),
            DeliveryResult(
                "email.deliver",
                DeliveryDisposition.TERMINAL,
                True,
                "Email delivery was rejected.",
                "SMTPException",
            ),
        )
        for outcome in outcomes:
            with self.subTest(disposition=outcome.disposition):
                with (
                    patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
                    patch("compliance.services.send_email_notification", return_value=outcome),
                    patch("compliance.services._cooldown_allows_handoff", return_value=True),
                ):
                    response = self.client.post(self._email_url(), follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "accepted for delivery")
                self.assertIsNone(self.signing_session.consumed_at)
                self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)

                self.signing_session = CustodySigningSession._base_manager.get(pk=self.signing_session.pk)
                if outcome.disposition == DeliveryDisposition.RETRYABLE:
                    self.assertEqual(self.signing_session.handoff_deliveries.count(), 1)
                else:
                    self.assertEqual(self.signing_session.handoff_deliveries.count(), 2)

    def test_email_without_holder_address_never_sends_or_books_delivery(self):
        self._login_operator()
        self.recipient_holder.email = ""
        self.recipient_holder.save(update_fields=["email", "updated_at"])
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services.send_email_notification") as send_email,
        ):
            response = self.client.post(self._email_url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "holder has no e-mail address")
        send_email.assert_not_called()
        self.assertFalse(CustodyHandoffDelivery._base_manager.filter(signing_session=self.signing_session).exists())

    def test_email_disabled_configuration_is_safe_and_not_success(self):
        self._login_operator()
        with (
            patch("compliance.views.custody_handoff_email_is_configured", return_value=False),
            patch("compliance.services.custody_handoff_email_is_configured", return_value=False),
            patch("compliance.services.send_email_notification") as send_email,
        ):
            detail = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk}))
            response = self.client.post(self._email_url(), follow=True)

        self.assertContains(detail, "E-mail is not configured.")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Email is not configured.")
        send_email.assert_not_called()
        self.assertFalse(CustodyHandoffDelivery._base_manager.filter(signing_session=self.signing_session).exists())

    def test_email_post_rejects_unauthorized_foreign_and_terminal_sessions(self):
        self._login_to_tenant(self.unrelated, self.tenant_a)
        with patch("compliance.services.send_email_notification") as send_email:
            denied = self.client.post(self._email_url())
        self.assertEqual(denied.status_code, 403)
        send_email.assert_not_called()

        self._login_operator()
        self.signing_session.canceled_at = timezone.now()
        self.signing_session.save(update_fields=["canceled_at", "updated_at"])
        gone = self.client.post(self._email_url())
        self.assertEqual(gone.status_code, 410)
        self.assertContains(gone, "internal_custody_handoff_unavailable", status_code=410)

    def test_delivery_bound_refusal_books_no_row_and_journals_refused_event(self):
        self._login_operator()
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services._cooldown_allows_handoff", return_value=True),
            patch(
                "compliance.services.send_email_notification",
                return_value=DeliveryResult("email.deliver", DeliveryDisposition.SUCCESS),
            ) as send_email,
        ):
            for attempt in range(3):
                response = self.client.post(self._email_url())
                self.assertEqual(response.status_code, 302, attempt)
            refused = self.client.post(self._email_url(), follow=True)

        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "attempt limit has been reached")
        self.assertEqual(send_email.call_count, 3)
        self.assertEqual(CustodyHandoffDelivery._base_manager.filter(signing_session=self.signing_session).count(), 3)
        refused_entries = JournalEntry._base_manager.filter(
            object_id=self.asset_a.pk,
            comment__contains="refused",
        )
        self.assertTrue(refused_entries.exists())
        comment = refused_entries.order_by("-created").first().comment
        for secret in (self.recipient_holder.email, DUMMY_TOKEN_A, DUMMY_SESSION_TOKEN):
            self.assertNotIn(secret, comment)

    def test_delivery_retry_books_second_attempt_and_unique_constraint_holds(self):
        self._login_operator()
        retry = DeliveryResult("email.deliver", DeliveryDisposition.RETRYABLE, error_class="timeout")
        success = DeliveryResult("email.deliver", DeliveryDisposition.SUCCESS)
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services._cooldown_allows_handoff", return_value=True),
            patch("compliance.services.send_email_notification", side_effect=[retry, success]),
        ):
            self.client.post(self._email_url())
            self.client.post(self._email_url())

        deliveries = list(
            CustodyHandoffDelivery._base_manager.filter(signing_session=self.signing_session).order_by("attempt")
        )
        self.assertEqual([delivery.attempt for delivery in deliveries], [1, 2])
        self.assertEqual(deliveries[0].status, CustodyHandoffDelivery.STATUS_REQUESTED)
        self.assertEqual(deliveries[0].error_class, "timeout")
        self.assertEqual(deliveries[1].status, CustodyHandoffDelivery.STATUS_SUCCEEDED)
        with self.assertRaises(IntegrityError):
            CustodyHandoffDelivery._base_manager.create(
                receipt=self.receipt_a,
                signing_session=self.signing_session,
                operator=self.technician,
                attempt=2,
                status=CustodyHandoffDelivery.STATUS_REQUESTED,
            )

    def test_journal_contains_only_safe_delivery_correlation(self):
        self._login_operator()
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services._cooldown_allows_handoff", return_value=True),
            patch(
                "compliance.services.send_email_notification",
                return_value=DeliveryResult("email.deliver", DeliveryDisposition.SUCCESS),
            ),
        ):
            self.client.post(self._email_url())

        entry = JournalEntry._base_manager.filter(object_id=self.asset_a.pk).order_by("-created").first()
        self.assertEqual(entry.tenant_id, self.tenant_a.pk)
        self.assertEqual(entry.user_id, self.technician.pk)
        self.assertIn("succeeded", entry.comment)
        self.assertIn("expires at", entry.comment)
        for secret in (self.recipient_holder.email, DUMMY_TOKEN_A, DUMMY_SESSION_TOKEN, "email.deliver"):
            self.assertNotIn(secret, entry.comment)
        self.assertNotIn(f"receipt_id={self.receipt_a.pk}", entry.comment)
        self.assertNotIn(f"session_id={self.signing_session.pk}", entry.comment)

    def test_email_content_uses_holder_preference_language(self):
        UserPreference.objects.create(user=self.recipient, data={"language": "de"})

        with patch("compliance.services.translation.override", return_value=nullcontext()) as override:
            _custody_handoff_email_content(self.receipt_a, self.signing_session, "https://public.example.test/handoff")

        override.assert_called_once_with("de")

    def test_cooldown_refuses_second_session_without_booking_delivery(self):
        self._login_operator()
        second_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token="q" * 64,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        result = DeliveryResult("email.deliver", DeliveryDisposition.SUCCESS)
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services.send_email_notification", return_value=result) as send_email,
        ):
            first = self.client.post(self._email_url())
            second = self.client.post(self._email_url(session=second_session), follow=True)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "Please wait before sending another handoff e-mail.")
        send_email.assert_called_once()
        self.assertFalse(CustodyHandoffDelivery._base_manager.filter(signing_session=second_session).exists())

    def test_receipt_bound_refuses_fresh_session_after_six_attempts(self):
        self._login_operator()
        sessions = [self.signing_session]
        for index in range(5):
            sessions.append(
                CustodySigningSession._base_manager.create(
                    receipt=self.receipt_a,
                    operator=self.technician,
                    intended_holder=self.recipient_holder,
                    token=(chr(97 + index) * 63) + str(index),
                    expires_at=timezone.now() + timedelta(minutes=30),
                )
            )
        for session in sessions:
            CustodyHandoffDelivery._base_manager.create(
                receipt=self.receipt_a,
                signing_session=session,
                operator=self.technician,
                attempt=1,
                status=CustodyHandoffDelivery.STATUS_SUCCEEDED,
            )
        seventh_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token="z" * 64,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services._cooldown_allows_handoff", return_value=True),
            patch("compliance.services.send_email_notification") as send_email,
        ):
            response = self.client.post(self._email_url(session=seventh_session), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "attempt limit has been reached")
        send_email.assert_not_called()
        self.assertFalse(CustodyHandoffDelivery._base_manager.filter(signing_session=seventh_session).exists())

    def test_delivery_manager_is_tenant_scoped(self):
        delivery = CustodyHandoffDelivery._base_manager.create(
            receipt=self.receipt_a,
            signing_session=self.signing_session,
            operator=self.technician,
            attempt=1,
            status=CustodyHandoffDelivery.STATUS_REQUESTED,
        )
        with self.tenant_context(self.tenant_a):
            self.assertTrue(CustodyHandoffDelivery.objects.for_scope(Scope.current()).filter(pk=delivery.pk).exists())
        with self.tenant_context(self.tenant_b):
            self.assertFalse(CustodyHandoffDelivery.objects.for_scope(Scope.current()).filter(pk=delivery.pk).exists())

    def test_intended_recipient_accept_consumes_session_with_accepted_outcome(self):
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(
            self._handoff_url(),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 200)
        self.receipt_a.refresh_from_db()
        self.signing_session.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertIsNotNone(self.signing_session.consumed_at)
        self.assertEqual(self.signing_session.outcome, CustodySigningSession.OUTCOME_ACCEPTED)

    def test_intended_recipient_decline_consumes_session_with_declined_outcome(self):
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.post(self._handoff_url(), {"action": "decline"})

        self.assertEqual(response.status_code, 200)
        self.receipt_a.refresh_from_db()
        self.signing_session.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_DECLINED)
        self.assertIsNotNone(self.signing_session.consumed_at)
        self.assertEqual(self.signing_session.outcome, CustodySigningSession.OUTCOME_DECLINED)

    def test_non_recipient_operator_cannot_consume_session(self):
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.post(
            self._handoff_url(),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 403)
        self._assert_wrong_recipient(response)
        self.signing_session.refresh_from_db()
        self.assertIsNone(self.signing_session.consumed_at)
        self.assertEqual(self.signing_session.outcome, "")

    def test_expired_consumed_and_canceled_sessions_are_neutral_410(self):
        self._login_to_tenant(self.recipient, self.tenant_a)
        now = timezone.now()
        terminal_states = (
            {"expires_at": now - timedelta(seconds=1)},
            {"consumed_at": now, "outcome": CustodySigningSession.OUTCOME_ACCEPTED},
            {"canceled_at": now},
        )

        for terminal_state in terminal_states:
            with self.subTest(terminal_state=sorted(terminal_state)):
                CustodySigningSession._base_manager.filter(pk=self.signing_session.pk).update(
                    expires_at=now + timedelta(minutes=30),
                    consumed_at=None,
                    canceled_at=None,
                    outcome="",
                )
                CustodySigningSession._base_manager.filter(pk=self.signing_session.pk).update(**terminal_state)

                response = self.client.get(self._handoff_url())

                self.assertEqual(response.status_code, 410)
                self._assert_body_contains(response, "custody_session_expired_or_used")
                self._assert_no_receipt_payload(response, self.receipt_a)

    def test_unknown_and_mismatched_sessions_are_neutral_404(self):
        mismatched_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_b,
            operator=self.cross_tenant_user,
            intended_holder=self.cross_holder,
            token=DUMMY_SESSION_TOKEN_B,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        self._login_to_tenant(self.recipient, self.tenant_a)

        for token in ("z" * 64, mismatched_session.token, "invalid-session"):
            with self.subTest(token_length=len(token)):
                response = self.client.get(self._handoff_url(session_token=token))

                self.assertEqual(response.status_code, 404)
                self._assert_body_contains(response, "custody_session_unavailable")
                self._assert_no_receipt_payload(response, self.receipt_a)

    def test_consumed_session_cannot_be_reused(self):
        self._login_to_tenant(self.recipient, self.tenant_a)
        self.client.post(
            self._handoff_url(),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        response = self.client.post(
            self._handoff_url(),
            {"action": "accept", "signature_canvas": DUMMY_SIGNATURE},
        )

        self.assertEqual(response.status_code, 410)
        self._assert_body_contains(response, "custody_session_expired_or_used")
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_expired_session_is_neutral_410_without_mutation(self):
        self.signing_session.expires_at = timezone.now() - timedelta(seconds=1)
        self.signing_session.save(update_fields=["expires_at", "updated_at"])
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.get(self._handoff_url())

        self.assertEqual(response.status_code, 410)
        self._assert_body_contains(response, "custody_session_expired_or_used")
        self._assert_no_receipt_payload(response, self.receipt_a)
        self.signing_session.refresh_from_db()
        self.assertIsNone(self.signing_session.consumed_at)
        self.receipt_a.refresh_from_db()
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_session_bound_to_another_receipt_is_neutral_404(self):
        other_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_b,
            operator=self.cross_tenant_user,
            intended_holder=self.cross_holder,
            token=DUMMY_SESSION_TOKEN_B,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        self._login_to_tenant(self.recipient, self.tenant_a)

        response = self.client.get(self._handoff_url(session_token=other_session.token))

        self.assertEqual(response.status_code, 404)
        self._assert_body_contains(response, "custody_session_unavailable")
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_cross_tenant_session_is_neutral_404(self):
        other_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_b,
            operator=self.cross_tenant_user,
            intended_holder=self.cross_holder,
            token=DUMMY_SESSION_TOKEN_B,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)

        response = self.client.get(self._handoff_url(session_token=other_session.token))

        self.assertEqual(response.status_code, 404)
        self._assert_body_contains(response, "custody_session_unavailable")
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_internal_detail_handoff_qr_panel_is_mobile_safe(self):
        """Regression (mobile WebView findings 2026-08-10/11): with e-mail
        delivery disabled the reason sentence must NOT live inside the flex
        button row (it interleaved with wrapped buttons on narrow viewports),
        the QR collapse panel must be a sibling AFTER the row, the figure must
        carry the mobile-safe margin/centering hooks, and the img must keep
        its intrinsic square size contract plus the theme-safe CSS hook."""
        with patch("compliance.views.custody_handoff_email_is_configured", return_value=False):
            self._login_operator()
            response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk}))

        parser = _HandoffPanelParser()
        parser.feed(response.content.decode())
        parser.close()

        self.assertFalse(parser.reason_in_row, "disabled-reason text must not live inside the flex button row")
        self.assertTrue(parser.panel_seen, "QR collapse panel must be present after the button row")
        self.assertTrue(parser.figure_mx0, "figure must carry mx-0 so the UA 40px side margins do not crush it")
        self.assertTrue(parser.img.get("img-fluid"), "img must stay responsive")
        self.assertTrue(parser.img.get("qr-class"), "img must keep the theme-safe .custody-handoff-qr hook")
        # Intrinsic-size contract: the module-count-derived dimensions are
        # square; the exact pixel value depends on the handoff URL length, so
        # pin the shape rather than re-deriving the URL here.
        self.assertIsNotNone(parser.img.get("width"))
        self.assertEqual(parser.img.get("width"), parser.img.get("height"))
        # Tabler's .alert is a flex row; with several children (title, copy,
        # button group, note, QR panel) every child collapses to min-content
        # width on narrow viewports (text breaks letter-by-letter). The
        # handoff box must force block layout so the children stack.
        self.assertContains(response, 'class="alert alert-info d-block"')


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
class CustodySigningSessionAuditTests(CustodyRBACFixtureMixin, TestCase):
    """A non-operator internal user sees the session audit without handoff tokens.

    The creating operator is the only principal who receives the handoff link
    (with the short-lived one-time session token); every other authorized viewer
    sees the audit row without any session secret.
    """

    def test_internal_detail_separates_operator_recipient_and_timestamps_without_tokens(self):
        self._login_to_tenant(self.technician, self.tenant_a)
        self.client.post(reverse("compliance:custodyreceipt_prepare", kwargs={"pk": self.receipt_a.pk}))
        signing_session = CustodySigningSession._base_manager.get(receipt=self.receipt_a)

        self._login_to_tenant(self.tenant_admin, self.tenant_a)
        response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk}))

        self.assertEqual(response.status_code, 200)
        self._assert_body_contains(response, self.technician.username)
        self._assert_body_contains(response, str(self.recipient_holder))
        self._assert_body_contains(response, "Active")
        response_body = response.content.decode("utf-8", errors="replace")
        self.assertFalse(
            signing_session.token in response_body,
            "internal detail must not render a custody signing session secret for non-operators",
        )
        self._assert_body_not_contains(response, DUMMY_TOKEN_A)


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
@pytest.mark.serial_only
class CustodySigningSessionRaceTests(CustodyRBACFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.signing_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token=DUMMY_SESSION_TOKEN,
            expires_at=timezone.now() + timedelta(minutes=30),
        )

    def _post_handoff_from_independent_client(self, signature):
        close_old_connections()
        try:
            client = self.client_class()
            client.force_login(self.recipient)
            session = client.session
            session["active_tenant_id"] = self.tenant_a.pk
            session.save()
            url = f"{self._sign_url(DUMMY_TOKEN_A)}?session={DUMMY_SESSION_TOKEN}"
            return client.post(url, {"action": "accept", "signature_canvas": signature})
        finally:
            close_old_connections()

    def test_concurrent_session_posts_allow_exactly_one_consumption(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self._post_handoff_from_independent_client, signature_png("dummy-session-signature-one")
                ),
                executor.submit(
                    self._post_handoff_from_independent_client, signature_png("dummy-session-signature-two")
                ),
            ]
            responses = [future.result() for future in futures]

        self.receipt_a.refresh_from_db()
        self.signing_session.refresh_from_db()
        self.assertEqual(sorted(response.status_code for response in responses), [200, 410])
        self.assertEqual(self.receipt_a.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertIsNotNone(self.signing_session.consumed_at)
        self.assertEqual(self.signing_session.outcome, CustodySigningSession.OUTCOME_ACCEPTED)


@override_settings(REQUIRE_CUSTODY_SIGNIN=True)
@pytest.mark.serial_only
class CustodyHandoffDeliveryRaceTests(CustodyRBACFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.signing_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token=DUMMY_SESSION_TOKEN,
            expires_at=timezone.now() + timedelta(minutes=30),
        )

    def _post_handoff_email(self):
        close_old_connections()
        try:
            client = self.client_class()
            client.force_login(self.technician)
            session = client.session
            session["active_tenant_id"] = self.tenant_a.pk
            session.save()
            url = reverse(
                "compliance:custodyreceipt_handoff_email",
                kwargs={"pk": self.receipt_a.pk, "session_pk": self.signing_session.pk},
            )
            return client.post(url)
        finally:
            close_old_connections()

    def test_concurrent_posts_receive_unique_attempt_numbers(self):
        result = DeliveryResult("email.deliver", DeliveryDisposition.SUCCESS)
        with (
            patch("compliance.services.custody_handoff_email_is_configured", return_value=True),
            patch("compliance.services._cooldown_allows_handoff", return_value=True),
            patch("compliance.services.send_email_notification", return_value=result),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda _: self._post_handoff_email(), range(2)))

        deliveries = list(
            CustodyHandoffDelivery._base_manager.filter(signing_session=self.signing_session).order_by("attempt")
        )
        self.assertEqual(sorted(response.status_code for response in responses), [302, 302])
        self.assertEqual([delivery.attempt for delivery in deliveries], [1, 2])
        self.assertTrue(all(delivery.status == CustodyHandoffDelivery.STATUS_SUCCEEDED for delivery in deliveries))


class CustodyReceiptExportTests(CustodyRBACFixtureMixin, TestCase):
    def _export_url(self, receipt=None):
        receipt = receipt or self.receipt_a
        return reverse("compliance:custodyreceipt_export", kwargs={"pk": receipt.pk})

    def _pdf_url(self, receipt=None):
        receipt = receipt or self.receipt_a
        return reverse("compliance:custodyreceipt_export_pdf", kwargs={"pk": receipt.pk})

    def _accept_receipt(self, receipt=None):
        receipt = receipt or self.receipt_a
        signed_at = timezone.now()
        receipt.accepted_date = signed_at
        receipt.acceptance_method = "digital_signature"
        receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        receipt.signature_canvas = "dummy-export-signature-canvas"
        receipt.signature_data = "dummy-export-signature-data"
        receipt.signature_hash = "dummy-export-signature-hash"
        receipt.verification_hash = "dummy-export-verification-hash"
        receipt.signed_at = signed_at
        receipt.eula_version = "1.0"
        receipt.ip_address = "192.0.2.20"
        receipt.user_agent = "dummy-export-user-agent"
        receipt.save()
        return receipt

    def test_authorized_accepted_export_is_deterministic_json_without_secrets(self):
        self._accept_receipt()
        consumed_at = timezone.now()
        signing_session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token=DUMMY_SESSION_TOKEN,
            expires_at=consumed_at + timedelta(minutes=30),
            consumed_at=consumed_at,
            outcome=CustodySigningSession.OUTCOME_ACCEPTED,
        )
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        first_response = self.client.get(self._export_url())
        second_response = self.client.get(self._export_url())

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.content, second_response.content)
        self.assertEqual(first_response["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(
            first_response["Content-Disposition"],
            f'attachment; filename="custody-receipt-{self.receipt_a.pk}.json"',
        )
        self.assertEqual(first_response["Cache-Control"], "no-store")
        self.assertEqual(first_response["X-Content-Type-Options"], "nosniff")
        payload = json.loads(first_response.content)
        self.assertEqual(payload["format"], "itambox.custody-receipt")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["receipt"]["acceptance_status"], CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(payload["receipt"]["verification_hash"], "dummy-export-verification-hash")
        self.assertEqual(payload["signing_sessions"][0]["id"], signing_session.pk)
        self.assertEqual(payload["signing_sessions"][0]["operator_id"], self.technician.pk)
        self.assertNotIn("token", payload["receipt"])
        self.assertNotIn("token", payload["signing_sessions"][0])
        self.assertNotIn(DUMMY_TOKEN_A.encode(), first_response.content)
        self.assertNotIn(DUMMY_SESSION_TOKEN.encode(), first_response.content)
        self.assertNotIn(b"dummy-export-signature-canvas", first_response.content)
        self.assertNotIn(b"dummy-export-signature-data", first_response.content)

    def test_authorized_accepted_export_returns_pdf_with_download_headers(self):
        self._accept_receipt()
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        response = self.client.get(self._pdf_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="custody-receipt-{self.receipt_a.pk}.pdf"',
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertGreater(len(response.content), 500)

    def test_pdf_renderer_receives_allowlisted_context_without_secrets(self):
        self._accept_receipt()
        session = CustodySigningSession._base_manager.create(
            receipt=self.receipt_a,
            operator=self.technician,
            intended_holder=self.recipient_holder,
            token=DUMMY_SESSION_TOKEN,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        with patch("compliance.views.report_pdf_bytes", return_value=b"%PDF-test") as render_pdf:
            response = self.client.get(self._pdf_url())

        self.assertEqual(response.status_code, 200)
        rendered_html = render_pdf.call_args.args[0]
        for expected in (
            self.asset_a.asset_tag,
            "Dummy Recipient",
            DUMMY_EULA,
            "dummy-export-verification-hash",
        ):
            self.assertIn(expected, rendered_html)
        self.assertIn(str(session.pk), rendered_html)
        for secret in (DUMMY_TOKEN_A, DUMMY_SESSION_TOKEN, "dummy-export-signature-data"):
            self.assertNotIn(secret, rendered_html)

    def test_pdf_signature_image_validation_and_fallback(self):
        valid_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        oversized_png = (
            "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024 + 1)).decode()
        )
        cases = (
            (valid_png, valid_png, True),
            ("data:image/png;base64,not-base64", "No renderable signature image is stored.", False),
            (oversized_png, "No renderable signature image is stored.", False),
        )
        self._accept_receipt()
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        for signature, expected, is_image in cases:
            with self.subTest(is_image=is_image):
                self.receipt_a.signature_canvas = signature
                self.receipt_a.save(update_fields=["signature_canvas", "updated_at"])
                with patch("compliance.views.report_pdf_bytes", return_value=b"%PDF-test") as render_pdf:
                    response = self.client.get(self._pdf_url())
                self.assertEqual(response.status_code, 200)
                rendered_html = render_pdf.call_args.args[0]
                self.assertIn(expected, rendered_html)
                if is_image:
                    self.assertIn('alt="Captured recipient signature"', rendered_html)

    def test_pdf_export_preserves_scope_and_permission_contract(self):
        self._login_to_tenant(self.tenant_admin, self.tenant_a)
        pending_response = self.client.get(self._pdf_url())
        self.assertEqual(pending_response.status_code, 404)
        self._assert_no_receipt_payload(pending_response, self.receipt_a)

        self._accept_receipt()
        self._login_to_tenant(self.technician, self.tenant_a)
        denied_response = self.client.get(self._pdf_url())
        self.assertEqual(denied_response.status_code, 403)
        self._assert_no_receipt_payload(denied_response, self.receipt_a)

        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)
        foreign_response = self.client.get(self._pdf_url())
        self.assertEqual(foreign_response.status_code, 404)
        self._assert_no_receipt_payload(foreign_response, self.receipt_a)

        self.client.force_login(self.superadmin)
        superadmin_response = self.client.get(self._pdf_url())
        self.assertGreaterEqual(superadmin_response.status_code, 200)
        self.assertLess(superadmin_response.status_code, 300)

    def test_pdf_and_json_download_controls_are_native_downloads(self):
        self._accept_receipt()
        detail_url = reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk})
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        response = self.client.get(detail_url)

        pdf_href = 'href="' + self._pdf_url() + '"'
        json_href = 'href="' + self._export_url() + '"'
        self.assertContains(response, pdf_href)
        self.assertContains(response, json_href)
        body = response.content.decode()
        for url in (self._pdf_url(), self._export_url()):
            link_start = body.index('href="' + url + '"')
            link_end = body.index("</a>", link_start)
            link = body[link_start:link_end]
            self.assertIn('hx-boost="false"', link)
            self.assertIn(" download", link)

    def test_pdf_export_handles_long_eula(self):
        self._accept_receipt()
        self.receipt_a.eula_text = "\n".join(f"Legal term {index}: {DUMMY_EULA}" for index in range(250))
        self.receipt_a.save(update_fields=["eula_text", "updated_at"])
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        response = self.client.get(self._pdf_url())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertGreater(len(response.content), 4000)

    def test_pdf_renderer_failure_returns_safe_server_error(self):
        self._accept_receipt()
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        with (
            patch("compliance.views.report_pdf_bytes", side_effect=RuntimeError("secret renderer detail")),
            self.assertLogs("compliance.views", level="ERROR") as captured,
        ):
            response = self.client.get(self._pdf_url())

        self.assertEqual(response.status_code, 500)
        body = response.content.decode("utf-8", errors="replace")
        self.assertNotIn(self.asset_a.asset_tag, body)
        self.assertNotIn("secret renderer detail", body)
        rendered = " ".join(captured.output)
        for field in (
            f"receipt_id={self.receipt_a.pk}",
            f"tenant_id={self.tenant_a.pk}",
            f"actor_id={self.tenant_admin.pk}",
        ):
            self.assertIn(field, rendered)
        self.assertIn("exception_type=RuntimeError", rendered)
        self.assertNotIn("secret renderer detail", rendered)

    def test_pending_receipt_export_is_neutral_404(self):
        self._login_to_tenant(self.tenant_admin, self.tenant_a)

        response = self.client.get(self._export_url())

        self.assertEqual(response.status_code, 404)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_technician_without_export_permission_gets_internal_403(self):
        self._accept_receipt()
        self._login_to_tenant(self.technician, self.tenant_a)

        response = self.client.get(self._export_url())

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "compliance/custody/internal_permission_error.html")
        self.assertContains(response, "internal_custody_permission_required", status_code=403)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_foreign_tenant_export_is_neutral_404(self):
        self._accept_receipt()
        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)

        response = self.client.get(self._export_url())

        self.assertEqual(response.status_code, 404)
        self._assert_no_receipt_payload(response, self.receipt_a)

    def test_export_button_requires_permission_and_accepted_state(self):
        detail_url = reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt_a.pk})
        self._login_to_tenant(self.tenant_admin, self.tenant_a)
        pending_response = self.client.get(detail_url)
        self.assertNotContains(pending_response, self._export_url())

        self._accept_receipt()
        accepted_response = self.client.get(detail_url)
        self.assertContains(accepted_response, self._export_url())

        self._login_to_tenant(self.technician, self.tenant_a)
        technician_response = self.client.get(detail_url)
        self.assertNotContains(technician_response, self._export_url())

    def test_superadmin_can_export_accepted_receipt(self):
        self._accept_receipt()
        self.client.force_login(self.superadmin)

        response = self.client.get(self._export_url())

        self.assertGreaterEqual(response.status_code, 200)
        self.assertLess(response.status_code, 300)
        self.assertContains(response, "dummy-export-verification-hash")
        self.assertNotContains(response, DUMMY_TOKEN_A)


class CustodyReceiptGenericExportContainmentTests(CustodyRBACFixtureMixin, TestCase):
    """The generic CSV/YAML/template route stays closed for custody receipts.

    ``CustodyReceipt`` keeps its deliberately unscoped default manager because the
    public token sign flow must resolve a receipt regardless of the requester's
    tenant context; its dedicated JSON/PDF exports (behind
    ``compliance.export_custodyreceipt`` and asset-tenant scoping) are
    authoritative. The generic route used to export every tenant's signature
    evidence to any principal holding only ``compliance.view_custodyreceipt``.
    """

    def _generic_export_urls(self):
        base = reverse(
            "object_export",
            kwargs={"app_label": "compliance", "model_name": "custodyreceipt", "template_id": 0},
        )
        return (
            base + "?format=csv&export_scope=all",
            base + "?format=yaml&export_scope=all",
            base + "?format=csv&export_scope=filtered",
            base + f"?format=csv&pk={self.receipt_a.pk},{self.receipt_b.pk}",
        )

    def _assert_generic_paths_fail_closed(self):
        for url in self._generic_export_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                content = response.content.decode("utf-8", "replace")
                self.assertNotIn(DUMMY_TOKEN_A, content)
                self.assertNotIn(DUMMY_TOKEN_B, content)

    def test_view_only_holder_is_denied(self):
        """The seeded technician persona holds view + prepare, never export."""
        self._login_to_tenant(self.technician, self.tenant_a)
        self._assert_generic_paths_fail_closed()

    def test_cross_tenant_view_holder_is_denied(self):
        self._login_to_tenant(self.cross_tenant_user, self.tenant_b)
        self._assert_generic_paths_fail_closed()

    def test_holder_of_the_dedicated_export_permission_is_denied(self):
        self._login_to_tenant(self.tenant_admin, self.tenant_a)
        self._assert_generic_paths_fail_closed()

    def test_superuser_is_denied(self):
        self.client.force_login(self.superadmin)
        self._assert_generic_paths_fail_closed()
