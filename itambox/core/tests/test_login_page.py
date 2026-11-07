"""Login page presentation: credential errors and SSO entry points (issue #162).

The page must show exactly one generic, non-enumerating authentication error,
associated with the form and announced accessibly, and must offer a distinct
login action for every genuinely configured SAML/OIDC provider (and none for
unconfigured, disabled or malformed ones).
"""

import html
import re
import sys
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import caches
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, resolve, reverse
from django.utils.module_loading import import_string
from djangosaml2 import views as djangosaml2_views

from core.auth import providers as providers_module
from core.auth import saml as saml_module
from core.auth.oidc import TenantOIDCAuthorizeView, TenantOIDCCallbackView, TenantOIDCSettingsMixin
from core.managers import get_current_tenant, set_current_tenant
from core.views import auth as auth_views
from itambox.middleware import CSPMiddleware
from organization.models import Tenant

User = get_user_model()

SAML_CONFIG = {
    "entityid": "https://acme.example.com/saml2/metadata/",
    "base_url": "https://acme.example.com",
    "metadata": {"remote": [{"url": "https://idp.acme.example.com/metadata"}]},
}

# Metadata-free variants: ``load_saml_config`` compiles a real ``SPConfig``, and
# a remote metadata source would reach out over the network.
LOADER_SAML_CONFIGS = {
    "acme": {"entityid": "https://acme.example.com/saml2/metadata/", "base_url": "https://acme.example.com"},
    "default": {"entityid": "https://sso.example.com/saml2/metadata/", "base_url": "https://sso.example.com"},
}

OIDC_CONFIG = {
    "OIDC_RP_CLIENT_ID": "itambox",
    "OIDC_RP_CLIENT_SECRET": "s3cret",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://idp.acme.example.com/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": "https://idp.acme.example.com/token",
    "OIDC_OP_USER_ENDPOINT": "https://idp.acme.example.com/userinfo",
    "OIDC_OP_ISSUER": "https://idp.acme.example.com/",
    "OIDC_OP_JWKS_ENDPOINT": "https://idp.acme.example.com/jwks",
}

GENERIC_ERROR = "Your username and password didn't match. Please try again."
DJANGO_DEFAULT_ERROR = "Please enter a correct username and password"


def page_text(response):
    """Decoded, HTML-unescaped response body (so apostrophes compare equal)."""
    return html.unescape(response.content.decode())


def provider_links(response):
    """The rendered SSO actions as ``(href, label)`` pairs."""
    pattern = r'<a href="([^"]*)"[^>]*class="btn btn-outline-primary[^"]*"[^>]*>(.*?)</a>'
    return [
        (match.group(1), " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(2))).split()))
        for match in re.finditer(pattern, response.content.decode(), re.DOTALL)
    ]


def saml_session_store(session_key=None):
    """Return the same backend store used by ``SamlSessionMiddleware``."""
    return import_module(settings.SESSION_ENGINE).SessionStore(session_key)


class ProviderEntryPointHelperTests(SimpleTestCase):
    def tearDown(self):
        set_current_tenant(None)
        super().tearDown()

    def test_missing_route_returns_no_entry_point(self):
        with patch.object(providers_module, "reverse", side_effect=NoReverseMatch):
            url = providers_module._entry_point("missing_provider_route", "", tenant_slug="acme")

        self.assertEqual(url, "")

    @override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={"acme": {**OIDC_CONFIG, "enabled": False}})
    def test_disabled_oidc_provider_cannot_be_started_by_direct_url(self):
        request = SimpleNamespace(GET={}, session={})
        tenant = SimpleNamespace(slug="acme")

        with (
            patch("organization.models.Tenant._base_manager.get", return_value=tenant),
            patch(
                "mozilla_django_oidc.views.OIDCAuthenticationRequestView.dispatch",
                return_value=HttpResponse(status=200),
            ) as parent_dispatch,
        ):
            with self.assertRaises(Http404):
                TenantOIDCAuthorizeView().dispatch(request, tenant_slug="acme")

        self.assertFalse(parent_dispatch.called)
        self.assertNotIn("oidc_tenant_slug", request.session)

    @override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={"acme": {**OIDC_CONFIG, "enabled": False}})
    def test_disabled_oidc_provider_cannot_complete_a_pinned_callback(self):
        request = SimpleNamespace(session={"oidc_tenant_slug": "acme"})
        tenant = SimpleNamespace(slug="acme")

        with (
            patch("organization.models.Tenant._base_manager.get", return_value=tenant),
            patch(
                "mozilla_django_oidc.views.OIDCAuthenticationCallbackView.dispatch",
                return_value=HttpResponse(status=200),
            ) as parent_dispatch,
        ):
            with self.assertRaises(Http404):
                TenantOIDCCallbackView().dispatch(request)

        self.assertFalse(parent_dispatch.called)
        self.assertNotIn("oidc_tenant_slug", request.session)

    def test_deleted_session_tenant_cannot_fall_back_to_global_oidc_initiation(self):
        request = SimpleNamespace(GET={}, session={"oidc_tenant_slug": "deleted"})

        with (
            patch("organization.models.Tenant._base_manager.get", side_effect=Tenant.DoesNotExist),
            patch(
                "mozilla_django_oidc.views.OIDCAuthenticationRequestView.dispatch",
                return_value=HttpResponse(status=200),
            ) as parent_dispatch,
        ):
            with self.assertRaises(Http404):
                TenantOIDCAuthorizeView().dispatch(request)

        self.assertFalse(parent_dispatch.called)
        self.assertNotIn("oidc_tenant_slug", request.session)

    def test_deleted_pinned_tenant_cannot_fall_back_to_global_oidc_callback(self):
        request = SimpleNamespace(session={"oidc_tenant_slug": "deleted"})

        with (
            patch("organization.models.Tenant._base_manager.get", side_effect=Tenant.DoesNotExist),
            patch(
                "mozilla_django_oidc.views.OIDCAuthenticationCallbackView.dispatch",
                return_value=HttpResponse(status=200),
            ) as parent_dispatch,
        ):
            with self.assertRaises(Http404):
                TenantOIDCCallbackView().dispatch(request)

        self.assertFalse(parent_dispatch.called)
        self.assertNotIn("oidc_tenant_slug", request.session)

    def test_deleted_pinned_tenant_cannot_complete_oidc_login_success(self):
        request = SimpleNamespace(session={"oidc_tenant_slug": "deleted"})
        view = TenantOIDCCallbackView()
        view.request = request

        with (
            patch("organization.models.Tenant._base_manager.get", side_effect=Tenant.DoesNotExist),
            patch(
                "mozilla_django_oidc.views.OIDCAuthenticationCallbackView.login_success",
                return_value=HttpResponse(status=200),
            ) as parent_login_success,
        ):
            with self.assertRaises(Http404):
                view.login_success()

        self.assertFalse(parent_login_success.called)
        self.assertNotIn("oidc_tenant_slug", request.session)

    @override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={}, OIDC_RP_SIGN_ALGO="HS256")
    def test_provider_discovery_and_runtime_share_global_signing_algorithm(self):
        discovered = providers_module._resolve_oidc_setting({}, "OIDC_RP_SIGN_ALGO")
        runtime = TenantOIDCSettingsMixin.get_settings("OIDC_RP_SIGN_ALGO")

        self.assertEqual(runtime, discovered)
        self.assertEqual(runtime, "HS256")

    def test_saml_post_binding_response_allows_https_form_action(self):
        self.assertEqual(
            auth_views.TenantSamlLoginView.post_binding_form_template,
            settings.SAML_POST_BINDING_FORM_TEMPLATE,
        )
        request = RequestFactory().get("/saml2/login/")
        request.csp_nonce = "test-nonce"
        request.user = AnonymousUser()
        response = render(
            request,
            settings.SAML_POST_BINDING_FORM_TEMPLATE,
            {
                "target_url": "https://idp.example.test/sso",
                "params": {"SAMLRequest": "encoded-request", "RelayState": "/dashboard/"},
            },
        )
        response = import_string(settings.SAML_CSP_HANDLER)(response)

        response = CSPMiddleware().process_response(request, response)

        content = response.content.decode()
        csp = response["Content-Security-Policy"]
        self.assertIn('<script nonce="test-nonce">', content)
        self.assertIn("document.SSO_Login.submit()", content)
        self.assertIn('action="https://idp.example.test/sso"', content)
        self.assertIn('type="submit"', content)
        self.assertNotIn("onload=", content)
        self.assertIn("script-src 'self' 'nonce-test-nonce'", csp)
        self.assertIn("form-action 'self' https:", csp)

        ordinary = CSPMiddleware().process_response(request, HttpResponse("ordinary"))
        ordinary_csp = ordinary["Content-Security-Policy"]
        self.assertIn("form-action 'self';", ordinary_csp)
        self.assertNotIn("form-action 'self' https:", ordinary_csp)


class LoginErrorPresentationTests(TestCase):
    """One generic error, accessibly associated, never enumerating credentials."""

    def setUp(self):
        # RateLimitMiddleware counts /accounts/login/ hits per IP in the shared
        # LocMem cache; clear it so a neighbouring test cannot turn a login POST
        # here into a 429.
        caches["default"].clear()
        # ... and again afterwards, so this module leaves no counters behind for the
        # next test module (the LocMem cache is shared across the serial suite).
        self.addCleanup(caches["default"].clear)
        self.url = reverse("login")
        self.user = User.objects.create_user(username="alice", password="correct-horse-battery")

    def test_invalid_password_renders_exactly_one_error(self):
        response = self.client.post(self.url, {"username": "alice", "password": "wrong"})

        self.assertEqual(response.status_code, 200)
        body = page_text(response)
        self.assertEqual(
            body.count(GENERIC_ERROR),
            1,
            msg="The failed login must render the generic credential error exactly once.",
        )
        self.assertNotIn(
            DJANGO_DEFAULT_ERROR,
            body,
            msg="Django's default invalid_login message must not be rendered alongside the generic one.",
        )

    def test_failed_local_login_with_discovery_opens_password_details(self):
        with override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}}):
            response = self.client.post(self.url, {"username": "alice", "password": "wrong"})

        html = response.content.decode()
        details = re.search(r"<details[^>]*>(.*?)</details>", html, re.DOTALL)
        self.assertIsNotNone(details)
        details_markup = details.group(0) if details else ""
        self.assertRegex(details_markup, r"<details[^>]* open>")
        self.assertRegex(details_markup, r'<input[^>]*name="username"')
        self.assertRegex(details_markup, r'<input[^>]*name="password"')

    def test_unknown_username_and_wrong_password_are_indistinguishable(self):
        wrong_password = self.client.post(self.url, {"username": "alice", "password": "wrong"})
        unknown_user = self.client.post(self.url, {"username": "nobody", "password": "wrong"})

        self.assertEqual(
            self._alert_text(wrong_password),
            self._alert_text(unknown_user),
            msg="The error must not disclose whether the username or the password was wrong.",
        )

    def test_error_is_associated_with_the_form_and_announced(self):
        response = self.client.post(self.url, {"username": "alice", "password": "wrong"})

        body = response.content.decode()
        self.assertNotIn("{#", body, msg="Template comments must not leak into the rendered login page.")
        self.assertRegex(body, r'<div[^>]*id="login-form-errors"', msg="The error block needs a stable id.")
        self.assertRegex(body, r'<div[^>]*id="login-form-errors"[^>]*role="alert"')
        self.assertRegex(body, r'<div[^>]*id="login-form-errors"[^>]*aria-live="assertive"')
        self.assertRegex(
            body,
            r'<form[^>]*aria-describedby="login-form-errors"',
            msg="The form must reference the error block so it is announced as form-level feedback.",
        )
        self.assertRegex(
            body,
            r'<input[^>]*name="password"[^>]*aria-invalid="true"',
            msg="Fields involved in the failed authentication must be marked invalid.",
        )
        self.assertRegex(body, r'<input[^>]*name="username"[^>]*aria-describedby="login-form-errors"')
        self.assertRegex(
            body,
            r'<input[^>]*name="username"[^>]*autofocus',
            msg="Keyboard focus should return to the first credential field after a failed login.",
        )

    def test_blank_submission_does_not_claim_a_credential_mismatch(self):
        response = self.client.post(self.url, {"username": "", "password": ""})

        body = page_text(response)
        self.assertNotIn(
            GENERIC_ERROR,
            body,
            msg="An empty submission is a missing-field error, not a failed authentication attempt.",
        )
        self.assertIn("This field is required", body)

    def test_form_errors_do_not_survive_a_fresh_request(self):
        self.client.post(self.url, {"username": "alice", "password": "wrong"})
        response = self.client.get(self.url)

        body = page_text(response)
        self.assertNotIn(GENERIC_ERROR, body)
        self.assertNotIn("login-form-errors", response.content.decode())
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def _alert_text(self, response):
        match = re.search(
            r'<div[^>]*id="login-form-errors".*?</div>\s*</div>\s*</div>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertIsNotNone(match, "No login error block was rendered.")
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(0))).split())


@override_settings(ITAMBOX_TENANT_SAML_CONFIGS={}, ITAMBOX_TENANT_OIDC_CONFIGS={})
class LoginProviderEntryPointTests(TestCase):
    """Only genuinely configured providers get a distinct, working action."""

    def setUp(self):
        caches["default"].clear()
        # ... and again afterwards, so this module leaves no counters behind for the
        # next test module (the LocMem cache is shared across the serial suite).
        self.addCleanup(caches["default"].clear)
        self.url = reverse("login")
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")

    def test_discovery_login_get_hides_tenant_provider_and_keeps_native_forms(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("Acme Corp", html)
        self.assertNotIn("Sign in with Acme Corp", html)
        self.assertEqual(response.context["sso_providers"], [])

        details = re.search(r"<details[^>]*>(.*?)</details>", html, re.DOTALL)
        self.assertIsNotNone(details)
        details_markup = details.group(1) if details else ""
        self.assertIn("Sign in with a password", details_markup)
        self.assertRegex(details_markup, r'<input[^>]*name="username"')
        self.assertRegex(details_markup, r'<input[^>]*name="password"')

        discovery_action = reverse("login_discover")
        self.assertIn('<form method="post" action="' + discovery_action + '">', html)

    def test_no_actions_when_no_provider_is_configured(self):
        response = self.client.get(self.url)

        self.assertEqual(provider_links(response), [])
        self.assertNotIn("Sign in with", page_text(response))

    def test_local_credential_login_remains_available_alongside_sso(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
        ):
            response = self.client.get(self.url)

        body = response.content.decode()
        self.assertRegex(body, r'<input[^>]*name="username"')
        self.assertRegex(body, r'<input[^>]*name="password"')
        self.assertRegex(body, r'<button[^>]*type="submit"')
        self.assertEqual(len(provider_links(response)), 2)

    def test_configured_saml_provider_gets_tenant_aware_action(self):
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG}):
            response = self.client.get(self.url)

        expected = reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"})
        self.assertEqual(provider_links(response), [(expected, "Sign in with Acme Corp (SAML)")])

    def test_configured_oidc_provider_gets_tenant_aware_action(self):
        with override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG}):
            response = self.client.get(self.url)

        expected = reverse("oidc_authentication_init_tenant", kwargs={"tenant_slug": "acme"})
        self.assertEqual(provider_links(response), [(expected, "Sign in with Acme Corp (OIDC)")])

    def test_saml_and_oidc_actions_are_distinctly_labelled(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
        ):
            links = provider_links(self.client.get(self.url))

        labels = [label for _href, label in links]
        hrefs = [href for href, _label in links]
        self.assertEqual(len(set(labels)), 2, msg=f"Provider labels are not distinct: {labels}")
        self.assertEqual(len(set(hrefs)), 2, msg=f"Provider actions are not distinct: {hrefs}")
        self.assertTrue(any("SAML" in label for label in labels))
        self.assertTrue(any("OIDC" in label for label in labels))

    def test_disabled_providers_are_not_advertised(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={"acme": {**SAML_CONFIG, "enabled": False}},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": {**OIDC_CONFIG, "enabled": False}},
        ):
            response = self.client.get(self.url)

        self.assertEqual(provider_links(response), [])

    def test_malformed_or_incomplete_configs_are_not_advertised(self):
        broken_saml = {
            "not a mapping": "https://idp.example.com/",
            "no metadata at all": {"entityid": "https://acme.example.com/saml2/metadata/"},
            "empty metadata": {**SAML_CONFIG, "metadata": {}},
            "empty metadata source": {**SAML_CONFIG, "metadata": {"remote": []}},
            "metadata is not a mapping": {**SAML_CONFIG, "metadata": "remote"},
            "no sp identity": {"metadata": SAML_CONFIG["metadata"]},
        }
        for label, config in broken_saml.items():
            with self.subTest(protocol="SAML", config=label):
                with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"acme": config}):
                    self.assertEqual(provider_links(self.client.get(self.url)), [])

        broken_oidc = {"not a mapping": "https://idp.example.com/", "empty": {}}
        for missing in OIDC_CONFIG:
            broken_oidc[f"missing {missing}"] = {key: value for key, value in OIDC_CONFIG.items() if key != missing}
        for label, config in broken_oidc.items():
            with self.subTest(protocol="OIDC", config=label):
                with override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={"acme": config}):
                    self.assertEqual(provider_links(self.client.get(self.url)), [])

    def test_provider_without_a_matching_tenant_is_not_advertised(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={"ghost": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={"ghost": OIDC_CONFIG},
        ):
            response = self.client.get(self.url)

        self.assertEqual(provider_links(response), [])

    def test_soft_deleted_tenant_provider_is_not_advertised(self):
        self.tenant.delete()

        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG}):
            response = self.client.get(self.url)

        self.assertEqual(provider_links(response), [])

    def test_default_saml_key_binds_the_only_live_tenant(self):
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"default": SAML_CONFIG}):
            response = self.client.get(self.url)

        expected = reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"})
        self.assertEqual(provider_links(response), [(expected, "Sign in with Acme Corp (SAML)")])

    def test_default_saml_key_is_hidden_when_the_tenant_is_ambiguous(self):
        Tenant.objects.create(name="Other Corp", slug="other")

        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"default": SAML_CONFIG}):
            response = self.client.get(self.url)

        self.assertEqual(provider_links(response), [])

    def test_default_saml_key_does_not_duplicate_an_explicit_tenant_provider(self):
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"default": SAML_CONFIG, "acme": SAML_CONFIG}):
            response = self.client.get(self.url)

        expected = reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"})
        self.assertEqual(provider_links(response), [(expected, "Sign in with Acme Corp (SAML)")])

    def test_global_oidc_settings_yield_an_untenanted_action(self):
        with override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={}, **OIDC_CONFIG):
            response = self.client.get(self.url)

        self.assertEqual(provider_links(response), [(reverse("oidc_authentication_init"), "Sign in with OIDC")])

    def test_supported_next_is_preserved_in_provider_actions(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
        ):
            response = self.client.get(self.url, {"next": "/assets/?status=deployed"})

        links = provider_links(response)
        self.assertEqual(len(links), 2)
        for href, label in links:
            self.assertIn("?next=%2Fassets%2F%3Fstatus%3Ddeployed", href, msg=f"{label} dropped the next destination.")
        self.assertRegex(
            response.content.decode(),
            r'<input type="hidden" name="next" value="/assets/\?status=deployed">',
        )

    def test_unsafe_next_is_dropped_from_provider_actions(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
        ):
            response = self.client.get(self.url, {"next": "https://evil.example.com/steal"})

        links = provider_links(response)
        self.assertEqual(len(links), 2)
        for href, label in links:
            self.assertNotIn("next=", href, msg=f"{label} carried an off-site next destination.")
        self.assertNotIn("evil.example.com", response.content.decode())

    def test_oidc_action_starts_the_tenant_flow_and_records_next(self):
        with override_settings(ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG}):
            response = self.client.get(
                reverse("oidc_authentication_init_tenant", kwargs={"tenant_slug": "acme"}),
                {"next": "/assets/"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(OIDC_CONFIG["OIDC_OP_AUTHORIZATION_ENDPOINT"]))
        self.assertEqual(self.client.session["oidc_tenant_slug"], "acme")
        self.assertEqual(self.client.session["oidc_login_next"], "/assets/")


@override_settings(ITAMBOX_TENANT_SAML_CONFIGS=LOADER_SAML_CONFIGS)
class TenantSamlEntryPointTests(TestCase):
    """The SAML action must start — and finish — the flow for the right tenant."""

    def setUp(self):
        caches["default"].clear()
        # ... and again afterwards, so this module leaves no counters behind for the
        # next test module (the LocMem cache is shared across the serial suite).
        self.addCleanup(caches["default"].clear)
        set_current_tenant(None)
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")
        # pysaml2 looks up the xmlsec binary while compiling an SPConfig.
        patcher = patch("saml2.sigver.get_xmlsec_binary", return_value=sys.executable)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(set_current_tenant, None)

    def test_entry_point_binds_the_tenant_and_forwards_next(self):
        seen = {}

        def fake_get(request, *args, **kwargs):
            seen["tenant"] = get_current_tenant()
            seen["next"] = request.GET.get("next")
            seen["pin"] = request.saml_session.get(saml_module.SAML_TENANT_SESSION_KEY)
            return HttpResponse("authn-request")

        url = reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"})
        with patch.object(djangosaml2_views.LoginView, "get", side_effect=fake_get) as parent_get:
            response = self.client.get(url, {"next": "/assets/"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(parent_get.called, "The djangosaml2 login flow was never started.")
        self.assertEqual(seen["tenant"], self.tenant, "The SP config would be compiled for the wrong tenant.")
        self.assertEqual(seen["next"], "/assets/", "The return destination was not handed to the SAML flow.")
        self.assertEqual(seen["pin"], "acme")
        self.assertNotIn(saml_module.SAML_TENANT_SESSION_KEY, self.client.session)

    @override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"default": LOADER_SAML_CONFIGS["default"]})
    def test_default_config_can_bind_the_only_live_tenant(self):
        seen = {}

        def fake_get(request, *args, **kwargs):
            seen["tenant"] = get_current_tenant()
            seen["pin"] = request.saml_session.get(saml_module.SAML_TENANT_SESSION_KEY)
            seen["entityid"] = saml_module.load_saml_config(request).entityid
            return HttpResponse("authn-request")

        url = reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"})
        with patch.object(djangosaml2_views.LoginView, "get", side_effect=fake_get):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["tenant"], self.tenant)
        self.assertEqual(seen["pin"], "acme")
        self.assertEqual(seen["entityid"], "https://sso.example.com/saml2/metadata/")

    def test_entry_point_rejects_an_unknown_tenant(self):
        with patch.object(djangosaml2_views.LoginView, "get", return_value=HttpResponse()) as parent_get:
            response = self.client.get(reverse("saml2_login_tenant", kwargs={"tenant_slug": "ghost"}))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(parent_get.called)
        self.assertNotIn(saml_module.SAML_TENANT_SESSION_KEY, self.client.session)

    def test_entry_point_rejects_a_soft_deleted_tenant(self):
        self.tenant.delete()

        with patch.object(djangosaml2_views.LoginView, "get", return_value=HttpResponse()):
            response = self.client.get(reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"}))

        self.assertEqual(response.status_code, 404)

    def test_entry_point_rejects_a_tenant_without_saml_configuration(self):
        other = Tenant.objects.create(name="Other Corp", slug="other")

        with patch.object(djangosaml2_views.LoginView, "get", return_value=HttpResponse()) as parent_get:
            response = self.client.get(reverse("saml2_login_tenant", kwargs={"tenant_slug": other.slug}))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(parent_get.called)

    def test_entry_point_rejects_a_disabled_saml_configuration(self):
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"acme": {**SAML_CONFIG, "enabled": False}}):
            with patch.object(djangosaml2_views.LoginView, "get", return_value=HttpResponse()) as parent_get:
                response = self.client.get(reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"}))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(parent_get.called)

    def test_authenticated_user_does_not_receive_a_stale_saml_pin(self):
        user = get_user_model().objects.create_user(username="already-in", password="irrelevant")
        self.client.force_login(user)

        response = self.client.get(reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"}))

        self.assertEqual(response.status_code, 302)
        cookie_name = getattr(settings, "SAML_SESSION_COOKIE_NAME", "saml_session")
        self.assertNotIn(cookie_name, self.client.cookies)

    def test_config_loader_compiles_the_pinned_tenant_config(self):
        request = RequestFactory().post("/saml2/acs/")
        request.saml_session = {saml_module.SAML_TENANT_SESSION_KEY: "acme"}

        config = saml_module.load_saml_config(request)

        self.assertEqual(config.entityid, "https://acme.example.com/saml2/metadata/")

    def test_config_loader_prefers_the_active_tenant_context(self):
        set_current_tenant(self.tenant)

        config = saml_module.load_saml_config()

        self.assertEqual(config.entityid, "https://acme.example.com/saml2/metadata/")

    def test_config_loader_without_a_request_keeps_the_default(self):
        config = saml_module.load_saml_config()

        self.assertEqual(config.entityid, "https://sso.example.com/saml2/metadata/")

    def test_acs_restores_the_pinned_tenant_before_processing_the_assertion(self):
        self._pin_saml_tenant("acme")
        seen = {}

        def fake_post(request, *args, **kwargs):
            seen["tenant"] = get_current_tenant()
            return HttpResponse("acs")

        with patch.object(djangosaml2_views.AssertionConsumerServiceView, "post", side_effect=fake_post):
            response = self.client.post("/saml2/acs/", {"SAMLResponse": "ignored"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            seen["tenant"],
            self.tenant,
            "SAML provisioning would run without the tenant the flow was started for.",
        )

    def test_acs_fails_closed_when_the_pinned_tenant_was_deleted(self):
        self._pin_saml_tenant("acme")
        self.tenant.delete()

        with patch.object(
            djangosaml2_views.AssertionConsumerServiceView, "post", return_value=HttpResponse()
        ) as parent_post:
            response = self.client.post("/saml2/acs/", {"SAMLResponse": "ignored"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(parent_post.called)

    def test_acs_activates_the_pinned_tenant_for_the_new_session(self):
        request = RequestFactory().post("/saml2/acs/")
        request.session = self.client.session
        request.saml_session = {saml_module.SAML_TENANT_SESSION_KEY: "acme"}
        user = get_user_model().objects.create_user(username="saml-user", password="irrelevant")
        view = auth_views.TenantSamlAcsView()
        view.request = request
        set_current_tenant(self.tenant)

        view.customize_session(user, {})

        self.assertEqual(request.session["active_tenant_id"], self.tenant.pk)
        self.assertNotIn(saml_module.SAML_TENANT_SESSION_KEY, request.saml_session)

    def test_acs_rejects_a_missing_cross_site_tenant_pin(self):
        with patch.object(
            djangosaml2_views.AssertionConsumerServiceView, "post", return_value=HttpResponse()
        ) as parent_post:
            response = self.client.post("/saml2/acs/", {"SAMLResponse": "ignored"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(parent_post.called)

    def test_acs_stays_csrf_exempt(self):
        # The IdP posts the assertion cross-site; overriding dispatch instead of
        # post would silently drop djangosaml2's csrf_exempt decorator.
        self.assertTrue(getattr(resolve("/saml2/acs/").func, "csrf_exempt", False))

    def test_saml_session_middleware_is_installed(self):
        # Every djangosaml2 view reads request.saml_session; without the
        # middleware the entry point raises AttributeError instead of starting.
        self.assertIn("djangosaml2.middleware.SamlSessionMiddleware", settings.MIDDLEWARE)

    def _pin_saml_tenant(self, slug):
        session = saml_session_store()
        session[saml_module.SAML_TENANT_SESSION_KEY] = slug
        session.save()
        cookie_name = getattr(settings, "SAML_SESSION_COOKIE_NAME", "saml_session")
        self.client.cookies[cookie_name] = session.session_key
