from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.checks import run_checks
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from assets.models import Asset, StatusLabel
from assets.services import checkout_asset
from compliance.models import CustodyReceipt, CustodyTemplate
from compliance.tests.signatures import signature_png
from core.management.commands._seed.access import _technician_permissions
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder, Tenant

User = get_user_model()

ACCEPTED_SIGNATURE = signature_png("accepted-signature")


class CustodyReceiptInternalViewTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(
            permissions=[
                "assets.view_asset",
                "compliance.view_custodyreceipt",
                "compliance.view_custodytemplate",
                "organization.view_assetholder",
            ]
        )
        self.user = self.tenant_user
        self.client_login_to_tenant(self.user, self.tenant)

        self.template = baker.make(
            CustodyTemplate,
            name="Shared custody terms",
            tenant=None,
            tenant_group=None,
            eula_text="Test custody terms",
        )
        deployable = baker.make(StatusLabel, type="deployable", name="Custody Deployable")
        deployed = baker.make(StatusLabel, type="deployed", name="Custody Deployed")
        self.asset = baker.make(Asset, tenant=self.tenant, name="Tenant A laptop", status=deployable)
        self.holder = baker.make(
            AssetHolder,
            tenant=self.tenant,
            first_name="Tenant A",
            last_name="Recipient",
        )
        checkout_asset(
            self.asset,
            holder=self.holder,
            user=self.user,
            status=deployed,
            _suppress_custody_receipt=True,
        )
        self.receipt = CustodyReceipt.objects.create(
            asset=self.asset,
            holder=self.holder,
            custody_template=self.template,
            eula_text="Test custody terms",
        )

        self.other_tenant = baker.make(Tenant, name="Tenant B", slug="tenant-b")
        self.other_asset = baker.make(Asset, tenant=self.other_tenant, name="Tenant B secret laptop")
        self.other_holder = baker.make(
            AssetHolder,
            tenant=self.other_tenant,
            first_name="Tenant B",
            last_name="Recipient",
        )
        self.other_receipt = CustodyReceipt.objects.create(
            asset=self.other_asset,
            holder=self.other_holder,
            custody_template=self.template,
            eula_text="Other tenant test terms",
        )

    def _login_with_permissions(self, *permissions):
        user = User.objects.create_user(
            username=f"limited-{User.objects.count()}",
            email=f"limited-{User.objects.count()}@example.test",
            password="test-password",
        )
        self.client_login_to_tenant(user, self.tenant, role_permissions=list(permissions))
        return user

    def test_accepted_filter_follows_acceptance_status(self):
        from compliance.filters import CustodyReceiptFilterSet

        self.receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt.save(update_fields=["acceptance_status"])
        queryset = CustodyReceipt.objects.filter(pk__in=[self.receipt.pk, self.other_receipt.pk])

        accepted = CustodyReceiptFilterSet({"accepted": "true"}, queryset=queryset).qs
        not_accepted = CustodyReceiptFilterSet({"accepted": "false"}, queryset=queryset).qs

        self.assertEqual(list(accepted.values_list("pk", flat=True)), [self.receipt.pk])
        self.assertEqual(list(not_accepted.values_list("pk", flat=True)), [self.other_receipt.pk])

    def test_pending_receipt_has_no_signed_timestamp(self):
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)
        self.assertIsNone(self.receipt.signed_at)

    def test_internal_list_is_tenant_scoped_and_never_renders_bearer_links(self):
        response = self.client.get(reverse("compliance:custodyreceipt_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.asset.name)
        self.assertNotContains(response, self.other_asset.name)
        self.assertContains(
            response,
            reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}),
        )
        self.assertNotContains(response, self.receipt.token)
        self.assertNotContains(response, reverse("compliance:custody_eula_sign", kwargs={"token": self.receipt.token}))

    def test_internal_detail_distinguishes_pending_declined_and_accepted(self):
        pending_response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))
        self.assertEqual(pending_response.status_code, 200)
        self.assertContains(pending_response, "Pending")
        self.assertContains(pending_response, "awaiting the recipient")
        self.assertNotContains(pending_response, self.receipt.token)

        self.receipt.acceptance_status = CustodyReceipt.STATUS_DECLINED
        self.receipt.save(update_fields=["acceptance_status"])
        declined_response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))
        self.assertContains(declined_response, "Declined")
        self.assertContains(declined_response, "declined this custody transfer")

        signed_at = timezone.now()
        self.receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.receipt.signed_at = signed_at
        self.receipt.acceptance_method = "checkbox"
        self.receipt.verification_hash = "obvious-test-verification-hash"
        self.receipt.signature_canvas = ACCEPTED_SIGNATURE
        self.receipt.save()
        accepted_response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))
        self.assertContains(accepted_response, "Accepted")
        self.assertContains(accepted_response, "obvious-test-verification-hash")
        self.assertContains(accepted_response, ACCEPTED_SIGNATURE)
        self.assertNotContains(accepted_response, self.receipt.token)

    def test_internal_routes_return_custody_specific_403_without_permission(self):
        self._login_with_permissions("assets.view_asset")

        list_response = self.client.get(reverse("compliance:custodyreceipt_list"))
        detail_response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))

        for response in (list_response, detail_response):
            self.assertEqual(response.status_code, 403)
            self.assertTemplateUsed(response, "compliance/custody/internal_permission_error.html")
            self.assertContains(response, "Internal custody permission required", status_code=403)
            self.assertNotContains(response, "intended recipient", status_code=403)
            self.assertNotContains(response, self.receipt.token, status_code=403)

    def test_internal_detail_returns_404_for_other_tenant(self):
        response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.other_receipt.pk}))

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, self.other_asset.name, status_code=404)

    def test_internal_routes_redirect_anonymous_users_to_login(self):
        self.client.logout()

        list_response = self.client.get(reverse("compliance:custodyreceipt_list"))
        detail_response = self.client.get(reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))

        self.assertEqual(list_response.status_code, 302)
        self.assertEqual(detail_response.status_code, 302)

    def test_template_receipts_are_scoped_through_asset_tenant(self):
        response = self.client.get(
            reverse("compliance:custodytemplate_detail", kwargs={"pk": self.template.pk}) + "?tab=receipts"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.asset.name)
        self.assertNotContains(response, self.other_asset.name)
        self.assertNotContains(response, self.receipt.token)

    def test_asset_surface_uses_tokenless_internal_receipt_summary(self):
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pending EULA Acceptance")
        self.assertContains(
            response,
            reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}),
        )
        self.assertNotContains(response, self.receipt.token)
        self.assertNotContains(response, "Copy Link")
        self.assertNotContains(response, "Sign Custody (On-Site)")
        self.assertNotIn("eula_token", response.context)
        self.assertNotIn("token", response.context["custody_receipt_summary"])

    def test_holder_surface_rejects_receipts_owned_by_another_asset_tenant(self):
        mismatched_receipt = CustodyReceipt.objects.create(
            asset=self.other_asset,
            holder=self.holder,
            custody_template=self.template,
            eula_text="Mismatched tenant test terms",
        )

        response = self.client.get(reverse("organization:assetholder_detail", kwargs={"pk": self.holder.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.asset.name)
        self.assertNotContains(response, self.other_asset.name)
        self.assertNotContains(response, mismatched_receipt.token)

    def test_embedded_receipt_surfaces_are_hidden_without_receipt_permission(self):
        self._login_with_permissions(
            "assets.view_asset",
            "compliance.view_custodytemplate",
            "organization.view_assetholder",
        )

        asset_response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        holder_response = self.client.get(reverse("organization:assetholder_detail", kwargs={"pk": self.holder.pk}))
        template_response = self.client.get(
            reverse("compliance:custodytemplate_detail", kwargs={"pk": self.template.pk})
        )

        for response in (asset_response, holder_response, template_response):
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.receipt.token)
            self.assertNotContains(
                response, reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk})
            )

    def test_holder_offboarding_tab_requires_receipt_permission_for_custody_items(self):
        holder_url = reverse("organization:assetholder_detail", kwargs={"pk": self.holder.pk})

        # The receipt permission makes the pending receipt part of the
        # offboarding readiness report on this holder's page.
        response = self.client.get(holder_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unaccepted custody receipt for:")
        self.assertContains(response, reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))

        # Without it, the offboarding report hides custody items exactly like
        # the embedded custody surfaces on this page.
        self._login_with_permissions(
            "organization.view_assetholder",
            "compliance.view_custodytemplate",
        )
        response = self.client.get(holder_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Unaccepted custody receipt for:")
        self.assertNotContains(response, reverse("compliance:custodyreceipt_detail", kwargs={"pk": self.receipt.pk}))


class CustodyPermissionPolicyTests(TestCase):
    def test_accepted_is_derived_from_acceptance_status(self):
        self.assertNotIn("accepted", {field.name for field in CustodyReceipt._meta.get_fields()})
        receipt = CustodyReceipt(acceptance_status=CustodyReceipt.STATUS_PENDING)
        self.assertFalse(receipt.accepted)
        receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        self.assertTrue(receipt.accepted)

    def test_eula_version_default_is_the_declared_constant(self):
        self.assertEqual(CustodyReceipt._meta.get_field("eula_version").default, CustodyReceipt.EULA_VERSION)

    @override_settings(ITAMBOX_BASE_URL="https://public.example.test/")
    def test_configured_handoff_base_url_rejects_trailing_slash(self):
        errors = run_checks(tags=["security"])

        self.assertIn("compliance.E001", {error.id for error in errors})

    @override_settings(ITAMBOX_BASE_URL="https://public.example.test")
    def test_configured_handoff_base_url_accepts_absolute_http_url(self):
        errors = run_checks(tags=["security"])

        self.assertNotIn("compliance.E001", {error.id for error in errors})

    def test_receipt_action_permissions_are_explicit_and_do_not_delegate_consent(self):
        codenames = {codename for codename, _label in CustodyReceipt._meta.permissions}

        self.assertEqual(codenames, {"prepare_custodyreceipt", "export_custodyreceipt"})
        self.assertFalse(any(codename.startswith("sign_") for codename in codenames))

    def test_seeded_technician_permissions_match_the_custody_role_matrix(self):
        permissions = list(Permission.objects.select_related("content_type"))
        technician_permissions = set(_technician_permissions(permissions, {"compliance"}))

        self.assertIn("compliance.view_custodytemplate", technician_permissions)
        self.assertIn("compliance.view_custodyreceipt", technician_permissions)
        self.assertIn("compliance.prepare_custodyreceipt", technician_permissions)
        self.assertNotIn("compliance.export_custodyreceipt", technician_permissions)
        self.assertNotIn("compliance.add_custodytemplate", technician_permissions)
        self.assertNotIn("compliance.change_custodytemplate", technician_permissions)
        self.assertNotIn("compliance.delete_custodytemplate", technician_permissions)
        self.assertNotIn("compliance.add_custodyreceipt", technician_permissions)
        self.assertNotIn("compliance.change_custodyreceipt", technician_permissions)
        self.assertFalse(any(permission.startswith("compliance.sign_") for permission in technician_permissions))
