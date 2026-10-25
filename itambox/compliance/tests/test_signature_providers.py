"""External signature providers: start, browser return, authenticated callback, per-tenant config."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework.test import APITestCase

from assets.models import AssetAssignment
from assets.services import checkin_asset, checkout_asset
from compliance.models import CustodyReceipt, CustodyTemplate, SignatureProviderConfig
from compliance.providers import (
    OUTCOME_ACCEPTED,
    BaseSignatureProvider,
    CallbackEvent,
    CallbackRejected,
    SignatureOutcome,
    SignatureProviderError,
    StartResult,
)
from compliance.registry import signature_providers
from compliance.services import EVIDENCE_VERIFIED, verify_custody_evidence
from compliance.tests.signatures import signature_png
from compliance.tests.test_custody_hardening import _ReceiptFixtureMixin
from core.management.commands.rotate_encryption_keys import encrypted_field_specs
from core.models import ObjectChange
from core.tests.mixins import TenantTestMixin
from organization.models import Tenant

User = get_user_model()

WEBHOOK_SECRET = "fake-webhook-secret"


class FakeESign(BaseSignatureProvider):
    name = "fake_esign"
    verbose_name = "Fake e-sign"
    required_options = ("account",)
    required_secrets = ("webhook_secret",)
    starts = []

    def start(self, receipt, *, request, settings, return_url):
        FakeESign.starts.append(
            {"receipt": receipt.pk, "resumed": receipt.provider_reference, "settings": settings, "return": return_url}
        )
        reference = receipt.provider_reference or f"env-{receipt.pk}"
        return StartResult(redirect_url=f"https://esign.example.test/sign/{reference}", provider_reference=reference)

    def complete_return(self, receipt, *, request, settings):
        if request.GET.get("result") == "signed":
            return SignatureOutcome(status=OUTCOME_ACCEPTED, provider_evidence={"via": "return"})
        return None

    def parse_callback(self, request, *, settings):
        expected = hmac.new(settings.secrets["webhook_secret"].encode(), request.body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Fake-Signature", ""), expected):
            raise CallbackRejected("bad signature")
        payload = json.loads(request.body)
        outcome = None
        if payload["status"] in ("accepted", "declined"):
            outcome = SignatureOutcome(
                status=payload["status"],
                signature_image=payload.get("image"),
                provider_evidence={"signer_ip": payload.get("ip", "")},
            )
        return CallbackEvent(provider_reference=payload["reference"], outcome=outcome)


class BrokenESign(FakeESign):
    name = "broken_esign"

    def start(self, receipt, *, request, settings, return_url):
        raise SignatureProviderError("upstream down")


class _ProviderRegistrationMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        signature_providers.register(FakeESign)
        signature_providers.register(BrokenESign)

    @classmethod
    def tearDownClass(cls):
        signature_providers.unregister(FakeESign.name)
        signature_providers.unregister(BrokenESign.name)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        FakeESign.starts.clear()

    def make_config(self, tenant=None, provider=FakeESign.name, **values):
        config = SignatureProviderConfig(
            tenant=tenant, name=f"{provider} {tenant or 'global'}", provider=provider, options={"account": "acct-1"}
        )
        for key, value in values.items():
            setattr(config, key, value)
        config.set_secrets({"webhook_secret": WEBHOOK_SECRET})
        config.save()
        return config

    def use_provider(self, receipt, provider=FakeESign.name):
        CustodyReceipt.objects.filter(pk=receipt.pk).update(signature_provider=provider)
        receipt.refresh_from_db()

    def callback(self, config, payload, *, secret=WEBHOOK_SECRET):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse("compliance:signature_provider_callback", kwargs={"config_pk": config.pk}),
            data=body,
            content_type="application/json",
            HTTP_X_FAKE_SIGNATURE=signature,
        )


@override_settings(REQUIRE_CUSTODY_SIGNIN=True, ITAMBOX_BASE_URL="https://itambox.example.test")
class ProviderSigningFlowTests(_ProviderRegistrationMixin, _ReceiptFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tenant = baker.make(Tenant, name="Provider Tenant", slug="provider-tenant")
        self.make_receipt_fixture(self.tenant)
        self.use_provider(self.receipt)
        self.client.force_login(self.recipient)

    def _start(self):
        self.make_config(self.tenant)
        response = self.client.get(self.sign_url)
        self.receipt.refresh_from_db()
        return response

    def test_opening_the_link_starts_signing_at_the_provider_once(self):
        first = self._start()
        second = self.client.get(self.sign_url)

        reference = f"env-{self.receipt.pk}"
        self.assertRedirects(first, f"https://esign.example.test/sign/{reference}", fetch_redirect_response=False)
        self.assertRedirects(second, f"https://esign.example.test/sign/{reference}", fetch_redirect_response=False)
        self.assertEqual(self.receipt.provider_reference, reference)
        self.assertEqual(self.receipt.provider_config.tenant, self.tenant)
        self.assertEqual([start["resumed"] for start in FakeESign.starts], ["", reference])
        settings = FakeESign.starts[0]["settings"]
        self.assertEqual(settings.secrets, {"webhook_secret": WEBHOOK_SECRET})
        self.assertEqual(settings.options, {"account": "acct-1"})
        self.assertEqual(
            settings.callback_url,
            f"https://itambox.example.test/compliance/custody/providers/{settings.config_id}/callback/",
        )
        self.assertEqual(
            FakeESign.starts[0]["return"],
            f"https://itambox.example.test/compliance/custody/sign/{self.receipt.token}/return/",
        )

    def test_the_tenant_config_wins_over_the_install_wide_one(self):
        global_config = self.make_config(None)
        tenant_config = self.make_config(self.tenant)

        self.client.get(self.sign_url)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.provider_config, tenant_config)
        self.assertNotEqual(tenant_config, global_config)

    def test_the_install_wide_config_is_the_fallback(self):
        global_config = self.make_config(None)

        self.client.get(self.sign_url)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.provider_config, global_config)

    def test_without_an_active_config_the_page_fails_closed(self):
        self.make_config(self.tenant, is_active=False)

        with self.assertLogs("compliance.views", level="ERROR"):
            response = self.client.get(self.sign_url)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.context["error_code"], "custody_provider_unavailable")
        self.assertEqual(FakeESign.starts, [])

    def test_a_failing_provider_start_is_reported(self):
        self.use_provider(self.receipt, BrokenESign.name)
        self.make_config(self.tenant, provider=BrokenESign.name)

        with self.assertLogs("compliance.views", level="ERROR"):
            response = self.client.get(self.sign_url)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.context["error_code"], "custody_provider_failed")

    def test_wrong_recipient_never_reaches_the_provider(self):
        self.make_config(self.tenant)
        self.client.force_login(baker.make(User, username="someone-else@example.test"))

        response = self.client.get(self.sign_url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(FakeESign.starts, [])

    def test_accepted_callback_records_and_seals_the_decision(self):
        self._start()
        config = self.receipt.provider_config

        response = self.callback(
            config,
            {
                "reference": self.receipt.provider_reference,
                "status": "accepted",
                "image": signature_png("provider"),
                "ip": "203.0.113.20",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "accepted"})
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(self.receipt.acceptance_method, "provider:fake_esign")
        self.assertEqual(self.receipt.signature_canvas, signature_png("provider"))
        self.assertEqual(self.receipt.provider_evidence, {"signer_ip": "203.0.113.20"})
        self.assertEqual(verify_custody_evidence(self.receipt).status, EVIDENCE_VERIFIED)
        self.assertTrue(
            ObjectChange.objects.filter(
                changed_object_id=self.receipt.pk, changed_object_type__model="custodyreceipt"
            ).exists()
        )

    def test_declined_and_acknowledged_callbacks(self):
        self._start()
        config = self.receipt.provider_config
        reference = self.receipt.provider_reference

        acknowledged = self.callback(config, {"reference": reference, "status": "sent"})
        self.receipt.refresh_from_db()
        self.assertEqual(acknowledged.json(), {"status": "acknowledged"})
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)

        declined = self.callback(config, {"reference": reference, "status": "declined"})
        self.receipt.refresh_from_db()
        self.assertEqual(declined.json(), {"status": "declined"})
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_DECLINED)

        replay = self.callback(config, {"reference": reference, "status": "accepted"})
        self.receipt.refresh_from_db()
        self.assertEqual(replay.json(), {"status": "not_pending"})
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_DECLINED)

    def test_callbacks_that_must_not_change_the_receipt(self):
        self._start()
        config = self.receipt.provider_config
        reference = self.receipt.provider_reference
        other_tenant = baker.make(Tenant, name="Other Provider Tenant", slug="other-provider-tenant")
        other_config = self.make_config(other_tenant)

        forged = self.callback(config, {"reference": reference, "status": "accepted"}, secret="wrong")
        unknown = self.callback(config, {"reference": "env-unknown", "status": "accepted"})
        cross_config = self.callback(other_config, {"reference": reference, "status": "accepted"})
        bad_image = self.callback(config, {"reference": reference, "status": "accepted", "image": "nope"})

        self.assertEqual(forged.status_code, 403)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(cross_config.status_code, 404)
        self.assertEqual(bad_image.json(), {"status": "invalid_signature"})
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_callback_for_an_inactive_or_unknown_config_is_404(self):
        self._start()
        config = self.receipt.provider_config
        SignatureProviderConfig.objects.filter(pk=config.pk).update(is_active=False)

        response = self.callback(config, {"reference": self.receipt.provider_reference, "status": "accepted"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"status": "unknown_config"})

    def test_callback_after_the_custody_period_ended_is_refused(self):
        self._start()
        config = self.receipt.provider_config
        AssetAssignment._base_manager.filter(pk=self.receipt.assignment_id).update(
            is_active=False, checked_in_at=timezone.now()
        )

        response = self.callback(config, {"reference": self.receipt.provider_reference, "status": "accepted"})

        self.assertEqual(response.json(), {"status": "superseded"})
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_PENDING)

    def test_callback_after_checkin_finds_the_receipt_superseded(self):
        self._start()
        config = self.receipt.provider_config
        checkin_asset(self.asset)

        response = self.callback(config, {"reference": self.receipt.provider_reference, "status": "accepted"})

        self.assertEqual(response.json(), {"status": "not_pending"})
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_SUPERSEDED)

    def test_browser_return_with_a_final_outcome_records_it(self):
        self._start()
        return_url = reverse("compliance:custody_provider_return", kwargs={"token": self.receipt.token})

        response = self.client.get(
            f"{return_url}?result=signed", REMOTE_ADDR="198.51.100.30", HTTP_USER_AGENT="ReturnBrowser"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "compliance/custody/receipt_success.html")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.acceptance_status, CustodyReceipt.STATUS_ACCEPTED)
        self.assertEqual(self.receipt.ip_address, "198.51.100.30")
        self.assertEqual(self.receipt.provider_evidence, {"via": "return"})

        again = self.client.get(return_url)
        self.assertTemplateUsed(again, "compliance/custody/receipt_success.html")

    def test_browser_return_without_an_outcome_waits_for_the_callback(self):
        self._start()
        return_url = reverse("compliance:custody_provider_return", kwargs={"token": self.receipt.token})

        response = self.client.get(return_url)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.context["error_code"], "custody_provider_pending")

    def test_browser_return_checks_the_signer_and_the_provider(self):
        return_url = reverse("compliance:custody_provider_return", kwargs={"token": self.receipt.token})

        self.client.logout()
        self.assertEqual(self.client.get(return_url).status_code, 403)
        self.client.force_login(baker.make(User, username="return-stranger@example.test"))
        self.assertEqual(self.client.get(return_url).status_code, 403)
        self.client.force_login(self.recipient)
        self.assertEqual(self.client.get(return_url).status_code, 503)
        self.use_provider(self.receipt, "local")
        self.assertEqual(self.client.get(return_url).status_code, 503)
        bad_token = reverse("compliance:custody_provider_return", kwargs={"token": "x" * 64})
        self.assertEqual(self.client.get(bad_token).status_code, 404)


class CheckoutLinksToTheSigningPageTests(_ProviderRegistrationMixin, TestCase):
    @override_settings(ITAMBOX_BASE_URL="https://itambox.example.test")
    def test_checkout_mail_never_calls_the_provider_and_links_to_itambox(self):
        tenant = baker.make(Tenant)
        category = baker.make("assets.Category")
        deployable = baker.make("assets.StatusLabel", type="deployable", name="Link Deployable")
        baker.make("assets.StatusLabel", type="deployed", name="Link Deployed")
        asset = baker.make(
            "assets.Asset",
            tenant=tenant,
            asset_type=baker.make("assets.AssetType", category=category),
            status=deployable,
        )
        holder = baker.make("organization.AssetHolder", tenant=tenant, email="link@example.test")
        baker.make(
            CustodyTemplate,
            tenant=tenant,
            category=category,
            require_acceptance=True,
            email_signature_request=True,
            signature_provider=FakeESign.name,
        )
        user = baker.make(User)
        email_config = SimpleNamespace(enabled=True, from_address="itambox@example.test", test_recipient="")

        with (
            patch("core.models.EmailSettings.load", return_value=email_config),
            patch("assets.services.send_mail") as send_mail,
            self.captureOnCommitCallbacks(execute=True),
        ):
            checkout_asset(asset=asset, holder=holder, user=user, request=SimpleNamespace(user=user))

        receipt = CustodyReceipt.objects.get(asset=asset)
        self.assertEqual(FakeESign.starts, [])
        message = send_mail.call_args.kwargs["message"]
        self.assertIn(f"https://itambox.example.test/compliance/custody/sign/{receipt.token}/", message)


class ProviderConfigStorageTests(_ProviderRegistrationMixin, TenantTestMixin, TestCase):
    def test_secrets_are_encrypted_at_rest_and_kept_out_of_the_change_log(self):
        self.setup_tenant_context(slug="storage-tenant")
        with self.tenant_context(self.tenant):
            from core.tasks.context import TaskContext

            with TaskContext(tenant_id=self.tenant.pk):
                config = self.make_config(self.tenant)

        stored = SignatureProviderConfig.objects.values_list("secrets_encrypted", flat=True).get(pk=config.pk)
        self.assertTrue(stored.startswith("enc$"))
        self.assertNotIn(WEBHOOK_SECRET, stored)
        self.assertEqual(config.get_secrets(), {"webhook_secret": WEBHOOK_SECRET})
        self.assertEqual(config.secret_keys, ["webhook_secret"])
        change = ObjectChange.objects.filter(changed_object_id=config.pk).latest("time")
        self.assertNotIn("secrets_encrypted", json.dumps(change.postchange_data))
        self.assertIn((SignatureProviderConfig, "secrets_encrypted"), encrypted_field_specs())

        config.set_secrets({})
        self.assertEqual(config.secrets_encrypted, "")
        self.assertEqual(config.get_secrets(), {})


class ProviderConfigUiTests(_ProviderRegistrationMixin, TenantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.setup_tenant_context(
            name="Config UI Tenant",
            slug="config-ui",
            permissions=[
                "compliance.view_signatureproviderconfig",
                "compliance.add_signatureproviderconfig",
                "compliance.change_signatureproviderconfig",
                "compliance.delete_signatureproviderconfig",
            ],
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _form(self, **values):
        data = {
            "tenant": self.tenant.pk,
            "name": "Tenant e-sign",
            "provider": FakeESign.name,
            "is_active": "on",
            "options": json.dumps({"account": "acct-9"}),
            "secrets": json.dumps({"webhook_secret": "s3cret-value"}),
        }
        data.update(values)
        return data

    def test_tenant_admin_creates_edits_and_never_sees_secrets_again(self):
        create = self.client.post(reverse("compliance:signatureproviderconfig_create"), self._form())
        self.assertEqual(create.status_code, 302, getattr(create, "context", None) and create.context["form"].errors)
        config = SignatureProviderConfig.objects.get(name="Tenant e-sign")
        self.assertEqual(config.get_secrets(), {"webhook_secret": "s3cret-value"})

        edit_url = reverse("compliance:signatureproviderconfig_update", kwargs={"pk": config.pk})
        edit_page = self.client.get(edit_url)
        detail = self.client.get(config.get_absolute_url())
        self.assertNotContains(edit_page, "s3cret-value")
        self.assertNotContains(detail, "s3cret-value")
        self.assertContains(detail, "webhook_secret")
        self.assertContains(detail, f"/compliance/custody/providers/{config.pk}/callback/")

        keep = self.client.post(edit_url, self._form(name="Renamed", secrets=""))
        self.assertEqual(keep.status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.name, "Renamed")
        self.assertEqual(config.get_secrets(), {"webhook_secret": "s3cret-value"})

    def test_missing_required_settings_and_install_wide_configs_are_refused(self):
        missing = self.client.post(
            reverse("compliance:signatureproviderconfig_create"), self._form(secrets=json.dumps({}))
        )
        install_wide = self.client.post(reverse("compliance:signatureproviderconfig_create"), self._form(tenant=""))

        self.assertEqual(missing.status_code, 200)
        self.assertIn("Missing secret: webhook_secret", str(missing.context["form"].errors))
        self.assertEqual(install_wide.status_code, 200)
        self.assertIn("install-wide", str(install_wide.context["form"].errors))
        self.assertFalse(SignatureProviderConfig.objects.exists())

    def test_install_wide_config_is_visible_but_not_writable_for_tenant_admins(self):
        install_wide = self.make_config(None)

        listing = self.client.get(reverse("compliance:signatureproviderconfig_list"))
        detail = self.client.get(install_wide.get_absolute_url())
        edit = self.client.get(reverse("compliance:signatureproviderconfig_update", kwargs={"pk": install_wide.pk}))
        delete = self.client.post(reverse("compliance:signatureproviderconfig_delete", kwargs={"pk": install_wide.pk}))

        self.assertContains(listing, install_wide.name)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(edit.status_code, 404)
        self.assertEqual(delete.status_code, 404)
        self.assertTrue(SignatureProviderConfig.objects.filter(pk=install_wide.pk).exists())

    def test_platform_admin_manages_install_wide_configs(self):
        admin = User.objects.create_superuser(username="platform-admin", email="pa@example.test", password="x")
        self.client.force_login(admin)

        response = self.client.post(reverse("compliance:signatureproviderconfig_create"), self._form(tenant=""))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SignatureProviderConfig.objects.filter(tenant__isnull=True).exists())


class ProviderConfigApiTests(_ProviderRegistrationMixin, TenantTestMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.setup_tenant_context(
            name="Config API Tenant",
            slug="config-api",
            permissions=[
                "compliance.view_signatureproviderconfig",
                "compliance.add_signatureproviderconfig",
                "compliance.change_signatureproviderconfig",
            ],
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        self.url = reverse("api:compliance_api:signatureproviderconfig-list")

    def _payload(self, **values):
        payload = {
            "name": "API e-sign",
            "tenant_id": self.tenant.pk,
            "provider": FakeESign.name,
            "options": {"account": "acct-api"},
            "secrets": {"webhook_secret": "api-secret"},
        }
        payload.update(values)
        return payload

    def test_create_returns_secret_names_only(self):
        response = self.client.post(self.url, self._payload(), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("secrets", response.data)
        self.assertEqual(response.data["secret_keys"], ["webhook_secret"])
        self.assertNotIn("api-secret", json.dumps(response.data))
        config = SignatureProviderConfig.objects.get(pk=response.data["id"])
        self.assertEqual(config.get_secrets(), {"webhook_secret": "api-secret"})

        detail = reverse("api:compliance_api:signatureproviderconfig-detail", kwargs={"pk": config.pk})
        etag = self.client.get(detail)["ETag"]
        patched = self.client.patch(detail, {"name": "Renamed"}, format="json", HTTP_IF_MATCH=etag)
        self.assertEqual(patched.status_code, 200, patched.data)
        config.refresh_from_db()
        self.assertEqual(config.get_secrets(), {"webhook_secret": "api-secret"})

    def test_invalid_configs_are_rejected(self):
        cases = {
            "unknown provider": self._payload(provider="nope"),
            "local needs no config": self._payload(provider="local"),
            "missing secret": self._payload(secrets={}),
            "install-wide": self._payload(tenant_id=None),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.assertEqual(self.client.post(self.url, payload, format="json").status_code, 400)
        self.assertFalse(SignatureProviderConfig.objects.exists())

    def test_install_wide_config_cannot_be_changed_by_a_tenant_admin(self):
        install_wide = self.make_config(None)

        response = self.client.patch(
            reverse("api:compliance_api:signatureproviderconfig-detail", kwargs={"pk": install_wide.pk}),
            {"name": "Hijacked"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        install_wide.refresh_from_db()
        self.assertNotEqual(install_wide.name, "Hijacked")


class RegistryTests(TestCase):
    def test_register_validates_provider_classes(self):
        with self.assertRaises(TypeError):
            signature_providers.register(object)

        class Nameless(BaseSignatureProvider):
            pass

        with self.assertRaises(ValueError):
            signature_providers.register(Nameless)

        class Impostor(BaseSignatureProvider):
            name = "local"

        with self.assertRaises(ValueError):
            signature_providers.register(Impostor)

    def test_plugin_declared_providers_are_loaded(self):
        from itambox.plugins.assembly import _compose_signature_providers
        from itambox.registry import registry as platform_registry

        class PluginESign(FakeESign):
            name = "plugin_esign"

        config = SimpleNamespace(name="plugin_pkg", signature_providers=[PluginESign])
        before = list(platform_registry._plugin_signature_providers)
        try:
            _compose_signature_providers(config)
            self.assertIsInstance(signature_providers.get("plugin_esign"), PluginESign)
            self.assertIn(("plugin_esign", "Fake e-sign"), signature_providers.choices())
            with self.assertRaises(TypeError):
                _compose_signature_providers(SimpleNamespace(name="bad_pkg", signature_providers=[object()]))
            with self.assertRaises(TypeError):
                _compose_signature_providers(SimpleNamespace(name="bad_pkg", signature_providers="not-a-list"))
        finally:
            platform_registry._plugin_signature_providers[:] = before
            signature_providers.unregister("plugin_esign")
            signature_providers._plugin_entries_loaded = len(before)

    def test_base_provider_defaults(self):
        provider = BaseSignatureProvider()
        self.assertEqual(provider.validate_config({}, {}), [])
        with self.assertRaises(NotImplementedError):
            provider.start(None, request=None, settings=None, return_url="")
        self.assertIsNone(provider.complete_return(None, request=None, settings=None))
        with self.assertRaises(CallbackRejected):
            provider.parse_callback(None, settings=None)
        with self.assertRaises(ValueError):
            SignatureOutcome(status="maybe")
