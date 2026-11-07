import hashlib
import importlib
import json
import os
import re
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core import tenant_scope
from core.auth import checks, providers
from core.context import set_current_tenant, set_current_user
from core.settings import base
from organization.models import Membership, Tenant

SAML_CONFIG = {
    "entityid": "https://acme.example.com/saml2/metadata/",
    "base_url": "https://acme.example.com",
    "metadata": {"remote": [{"url": "https://idp.example.com/metadata"}]},
}
OIDC_CONFIG = {
    "OIDC_RP_CLIENT_ID": "itambox",
    "OIDC_RP_CLIENT_SECRET": "test",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://idp.example.com/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": "https://idp.example.com/token",
    "OIDC_OP_USER_ENDPOINT": "https://idp.example.com/userinfo",
    "OIDC_OP_ISSUER": "https://idp.example.com/",
    "OIDC_OP_JWKS_ENDPOINT": "https://idp.example.com/jwks",
}

LEGACY_LOGIN_FINGERPRINT = "01e60be0a0566f68c34cf24146ae84a1ddc9d40ac2821818356538eb80c64832"


def _record_query(queries):
    def record(execute, sql, params, many, context):
        queries.append(sql)
        return execute(sql, params, many, context)

    return record


def _assert_no_user_or_membership_queries(test_case, queries):
    sql = [query["sql"] if isinstance(query, dict) else query for query in queries]
    query_text = " ".join(sql).lower()
    for table_name in (get_user_model()._meta.db_table, Membership._meta.db_table):
        test_case.assertNotIn(table_name.lower(), query_text)


class ProviderDiscoveryArchitectureTests(TestCase):
    def test_provider_module_does_not_resolve_or_capture_a_model_at_import(self):
        with patch.object(tenant_scope, "tenant_model", side_effect=AssertionError("import-time model lookup")):
            importlib.reload(providers)

    def test_tenant_model_is_resolved_by_the_owner_at_each_discovery_call(self):
        Tenant.objects.create(name="Acme Corp", slug="acme")
        with (
            override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG}, ITAMBOX_TENANT_OIDC_CONFIGS={}),
            patch.object(tenant_scope, "tenant_model", wraps=tenant_scope.tenant_model) as resolver,
        ):
            first = providers.get_login_providers()
            second = providers.get_login_providers()

        expected = [("SAML", "acme", reverse("saml2_login_tenant", kwargs={"tenant_slug": "acme"}))]
        self.assertEqual([(p["protocol"], p["key"], p["url"]) for p in first], expected)
        self.assertEqual([(p["protocol"], p["key"], p["url"]) for p in second], expected)
        self.assertEqual(resolver.call_count, 2)


class ProviderDiscoveryQueryAndBehaviorTests(TestCase):
    def _configured_saml(self, count, prefix):
        tenants = [
            Tenant(name=f"{prefix} Tenant {index:02d}", slug=f"{prefix}-tenant-{index:02d}") for index in range(count)
        ]
        Tenant.objects.bulk_create(tenants)
        return {tenant.slug: SAML_CONFIG for tenant in tenants}

    def test_live_tenant_query_count_does_not_scale_with_button_count(self):
        one = self._configured_saml(1, "one")
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS=one, ITAMBOX_TENANT_OIDC_CONFIGS={}):
            with CaptureQueriesContext(connection) as one_queries:
                one_result = providers.get_login_providers()

        many = self._configured_saml(8, "many")
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS=many, ITAMBOX_TENANT_OIDC_CONFIGS={}):
            with CaptureQueriesContext(connection) as many_queries:
                many_result = providers.get_login_providers()

        self.assertEqual(len(one_result), 1)
        self.assertEqual(len(many_result), 8)
        self.assertEqual(len(one_queries), 1)
        self.assertEqual(len(many_queries), 1)

    def test_behavior_matrix_preserves_suppression_defaults_and_global_oidc(self):
        acme = Tenant.objects.create(name="Acme Corp", slug="acme")
        cases = (
            ({}, {}, []),
            ({"ghost": SAML_CONFIG}, {}, []),
            ({"acme": {**SAML_CONFIG, "enabled": False}}, {}, []),
            ({"acme": SAML_CONFIG}, {}, [("SAML", "acme")]),
            ({"default": SAML_CONFIG}, {}, [("SAML", "acme")]),
            ({}, {"default": OIDC_CONFIG}, []),
            ({}, {}, []),
        )
        for saml, oidc, expected in cases:
            with self.subTest(saml=tuple(saml), oidc=tuple(oidc)):
                with override_settings(
                    ITAMBOX_TENANT_SAML_CONFIGS=saml,
                    ITAMBOX_TENANT_OIDC_CONFIGS=oidc,
                ):
                    result = providers.get_login_providers()
                self.assertEqual([(p["protocol"], p["key"]) for p in result], expected)

        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={},
            **OIDC_CONFIG,
        ):
            result = providers.get_login_providers()
        self.assertEqual([(p["protocol"], p["key"]) for p in result], [("OIDC", "default")])

        Tenant.objects.create(name="Other Corp", slug="other")
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"default": SAML_CONFIG}, ITAMBOX_TENANT_OIDC_CONFIGS={}):
            self.assertEqual(providers.get_login_providers(), [])
        acme.delete()
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG}, ITAMBOX_TENANT_OIDC_CONFIGS={}):
            self.assertEqual(providers.get_login_providers(), [])


class ProviderTenantBindingTests(SimpleTestCase):
    def test_tenant_slug_returns_only_tenant_bound_actions(self):
        tenant = SimpleNamespace(name="Acme Corp", slug="acme")
        with (
            override_settings(
                ITAMBOX_TENANT_SAML_CONFIGS={"acme": SAML_CONFIG},
                ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
                **OIDC_CONFIG,
            ),
            patch.object(providers, "_tenants_by_slug", return_value={"acme": tenant}),
        ):
            actions = providers.get_login_providers(tenant_slug="acme")

        self.assertEqual({action["protocol"] for action in actions}, {"SAML", "OIDC"})
        self.assertTrue(all(action["tenant_bound"] for action in actions))
        self.assertNotIn(reverse("oidc_authentication_init"), {action["url"] for action in actions})

    def test_legacy_call_keeps_global_oidc_and_marks_it_unbound(self):
        with override_settings(
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={},
            **OIDC_CONFIG,
        ):
            actions = providers.get_login_providers()

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["url"], reverse("oidc_authentication_init"))
        self.assertFalse(actions[0]["tenant_bound"])

    def test_tenant_oidc_action_does_not_complete_from_global_settings(self):
        tenant = SimpleNamespace(name="Acme Corp", slug="acme")
        with (
            override_settings(
                ITAMBOX_TENANT_SAML_CONFIGS={},
                ITAMBOX_TENANT_OIDC_CONFIGS={"acme": {"OIDC_RP_CLIENT_ID": "tenant-client"}},
                **OIDC_CONFIG,
            ),
            patch.object(providers, "_tenants_by_slug", return_value={"acme": tenant}),
        ):
            actions = providers.get_login_providers(tenant_slug="acme")

        self.assertEqual(actions, [])

    def test_saml_provider_for_a_different_tenant_is_filtered(self):
        other_tenant = SimpleNamespace(name="Other Corp", slug="other")
        with (
            override_settings(ITAMBOX_TENANT_SAML_CONFIGS={"other": SAML_CONFIG}, ITAMBOX_TENANT_OIDC_CONFIGS={}),
            patch.object(providers, "_tenants_by_slug", return_value={"other": other_tenant}),
        ):
            actions = providers.get_login_providers(tenant_slug="acme")

        self.assertEqual(actions, [])

    def test_oidc_provider_for_missing_tenant_is_filtered(self):
        with (
            override_settings(ITAMBOX_TENANT_SAML_CONFIGS={}, ITAMBOX_TENANT_OIDC_CONFIGS={"missing": OIDC_CONFIG}),
            patch.object(providers, "_tenants_by_slug", return_value={}),
        ):
            actions = providers.get_login_providers(tenant_slug="missing")

        self.assertEqual(actions, [])

    def test_unbound_provider_scan_uses_global_oidc_validation_for_tenant_entries(self):
        tenant = SimpleNamespace(name="Acme Corp", slug="acme")
        with (
            override_settings(
                ITAMBOX_TENANT_SAML_CONFIGS={},
                ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
                **OIDC_CONFIG,
            ),
            patch.object(providers, "_tenants_by_slug", return_value={"acme": tenant}),
        ):
            actions = providers.get_login_providers()

        self.assertEqual([action["key"] for action in actions], ["acme"])
        self.assertTrue(actions[0]["tenant_bound"])

    def test_tenant_oidc_accepts_non_rsa_algorithm_and_ignores_non_mapping_settings(self):
        config = {**OIDC_CONFIG, "OIDC_RP_SIGN_ALGO": "HS256"}

        self.assertTrue(providers.is_usable_tenant_oidc_config(config))
        self.assertFalse(providers.is_usable_tenant_oidc_config(None))
        self.assertIsNone(providers._tenant_oidc_setting(None, "OIDC_RP_CLIENT_ID"))


class EmailDomainNormalizationTests(SimpleTestCase):
    def test_domain_is_normalized_to_a_lowercase_idna_alabel(self):
        domains = importlib.import_module("core.email_domains")

        self.assertEqual(domains.normalize_domain("  Exämple.DE. "), "xn--exmple-cua.de")
        self.assertEqual(domains.email_domain("person@sub@Exämple.DE."), "xn--exmple-cua.de")

    def test_malformed_public_and_ip_domains_are_not_claimable(self):
        domains = importlib.import_module("core.email_domains")

        self.assertIsNone(domains.email_domain("bare-user"))
        self.assertEqual(domains.email_domain("person@gmail.com"), "gmail.com")
        self.assertIsNone(domains.email_domain("person@127.0.0.1"))
        self.assertIsNone(domains.email_domain("person@bad..example"))
        self.assertIn("outlook.com", domains.PUBLIC_EMAIL_DOMAINS)

    def test_domain_validation_rejects_non_strings_and_invalid_dns_forms(self):
        domains = importlib.import_module("core.email_domains")
        oversized_domain = ".".join(["a" * 63] * 4) + ".com"

        for value in (
            None,
            123,
            "",
            ".",
            "example.com..",
            "bad domain.example",
            "https://bad.example",
            "bad_.example",
            oversized_domain,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    domains.normalize_domain(value)

    def test_email_domain_rejects_non_string_identifiers_and_empty_local_parts(self):
        domains = importlib.import_module("core.email_domains")

        for identifier in (None, 123, "@acme.example", "   @acme.example"):
            with self.subTest(identifier=identifier):
                self.assertIsNone(domains.email_domain(identifier))


class TenantLoginDomainSettingsTests(SimpleTestCase):
    setting_name = "ITAMBOX_TENANT_LOGIN_DOMAINS"

    def test_loader_normalizes_domains_and_preserves_optional_method(self):
        loader = getattr(base, "_load_tenant_login_domains", None)
        self.assertTrue(callable(loader), "tenant login-domain settings loader is missing")
        raw = json.dumps({"acme": {"domains": [" Acme.COM. ", "Exämple.DE"], "method": "oidc"}})

        with patch.dict(os.environ, {self.setting_name: raw}, clear=True):
            value = loader()

        self.assertEqual(
            value,
            {"acme": {"domains": ["acme.com", "xn--exmple-cua.de"], "method": "oidc"}},
        )

    def test_loader_rejects_duplicate_domains_after_normalization(self):
        loader = getattr(base, "_load_tenant_login_domains", None)
        self.assertTrue(callable(loader), "tenant login-domain settings loader is missing")
        raw = json.dumps({"acme": {"domains": ["acme.example", "ACME.EXAMPLE."]}})

        with patch.dict(os.environ, {self.setting_name: raw}, clear=True):
            with self.assertRaises(ImproperlyConfigured) as raised:
                loader()

        self.assertIn("acme", str(raised.exception))
        self.assertIn("duplicate", str(raised.exception).lower())

    def test_loader_rejects_public_and_malformed_domains_with_slug_context(self):
        loader = getattr(base, "_load_tenant_login_domains", None)
        self.assertTrue(callable(loader), "tenant login-domain settings loader is missing")
        invalid = (
            ({"acme": {"domains": ["gmail.com"]}}, "public"),
            ({"broken": {"domains": "acme.example"}}, "domains"),
            ({"bad slug": {"domains": ["acme.example"]}}, "slug"),
            ({"acme": {"domains": ["bad..example"]}}, "domain"),
            ({"acme": {"domains": ["acme.example"], "method": "ldap"}}, "method"),
        )
        for value, reason in invalid:
            with self.subTest(value=value):
                with patch.dict(os.environ, {self.setting_name: json.dumps(value)}, clear=True):
                    with self.assertRaises(ImproperlyConfigured) as raised:
                        loader()
                self.assertIn(next(iter(value)), str(raised.exception))
                self.assertIn(reason, str(raised.exception).lower())

    def test_absent_setting_returns_empty_mapping(self):
        loader = getattr(base, "_load_tenant_login_domains", None)
        self.assertTrue(callable(loader), "tenant login-domain settings loader is missing")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(loader(), {})

    def test_loader_rejects_invalid_json_and_non_object_values(self):
        loader = getattr(base, "_load_tenant_login_domains", None)
        self.assertTrue(callable(loader), "tenant login-domain settings loader is missing")

        for raw, expected in (("{invalid", "invalid json"), ("[]", "json object")):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {self.setting_name: raw}, clear=True):
                    with self.assertRaises(ImproperlyConfigured) as raised:
                        loader()
                self.assertIn(expected, str(raised.exception).lower())

    def test_loader_rejects_invalid_entry_shapes_and_allows_omitted_method(self):
        loader = getattr(base, "_load_tenant_login_domains", None)
        self.assertTrue(callable(loader), "tenant login-domain settings loader is missing")
        invalid = (
            ({"": {"domains": ["acme.example"]}}, "invalid slug"),
            ({"acme": "not-an-object"}, "object with a domains list"),
            ({"acme": {"domains": ["acme.example"], "unexpected": True}}, "unsupported keys"),
        )
        for value, reason in invalid:
            with self.subTest(value=value):
                with patch.dict(os.environ, {self.setting_name: json.dumps(value)}, clear=True):
                    with self.assertRaises(ImproperlyConfigured) as raised:
                        loader()
                self.assertIn(reason, str(raised.exception).lower())

        with patch.dict(
            os.environ,
            {self.setting_name: json.dumps({"acme": {"domains": ["acme.example"]}})},
            clear=True,
        ):
            self.assertEqual(loader(), {"acme": {"domains": ["acme.example"]}})


class LoginDiscoveryResolutionTests(SimpleTestCase):
    def setUp(self):
        self.config = {"acme": {"domains": ["acme.example"]}}

    def _module(self):
        return importlib.import_module("core.auth.discovery")

    def test_single_provider_redirects_only_to_its_configured_action(self):
        discovery = self._module()
        action = {"protocol": "OIDC", "url": "/oidc/authenticate/acme/", "tenant_bound": True, "name": "Acme (OIDC)"}
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery, "_tenant_slug_for_domain", return_value="acme"),
            patch.object(discovery, "get_login_providers", return_value=[action]) as get_providers,
        ):
            route = discovery.resolve_login_route("person@acme.example", "/assets/")

        self.assertEqual(route.mode, "redirect")
        self.assertEqual(route.url, action["url"])
        self.assertEqual(route.actions, (action,))
        get_providers.assert_called_once_with("/assets/", tenant_slug="acme")

    def test_success_log_records_domain_and_outcome_without_local_part(self):
        discovery = self._module()
        action = {"protocol": "OIDC", "url": "/oidc/acme/", "tenant_bound": True, "name": "Acme (OIDC)"}
        with (
            self.assertLogs("core.auth.discovery", level="INFO") as captured,
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery, "_tenant_slug_for_domain", return_value="acme"),
            patch.object(discovery, "get_login_providers", return_value=[action]),
        ):
            route = discovery.resolve_login_route("local-secret@acme.example")

        log_output = " ".join(captured.output)
        self.assertEqual(route.mode, "redirect")
        self.assertIn("acme.example", log_output)
        self.assertIn("redirecting", log_output)
        self.assertNotIn("local-secret", log_output)

    def test_multiple_actions_are_neutral_choices_without_tenant_names(self):
        discovery = self._module()
        actions = [
            {"protocol": "OIDC", "url": "/oidc/acme/", "tenant_bound": True, "name": "Customer Name (OIDC)"},
            {"protocol": "SAML", "url": "/saml/acme/", "tenant_bound": True, "name": "Customer Name (SAML)"},
        ]
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery, "_tenant_slug_for_domain", return_value="acme"),
            patch.object(discovery, "get_login_providers", return_value=actions),
        ):
            route = discovery.resolve_login_route("person@acme.example")

        self.assertEqual(route.mode, "choose")
        self.assertEqual([action["protocol"] for action in route.actions], ["OIDC", "SAML"])
        self.assertNotIn("Customer Name", str(route.actions))

    def test_unsafe_next_is_not_forwarded_to_provider_resolution(self):
        discovery = self._module()
        action = {"protocol": "OIDC", "url": "/oidc/acme/", "tenant_bound": True, "name": "Acme (OIDC)"}
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config, ALLOWED_HOSTS=["app.example"]),
            patch.object(discovery, "_tenant_slug_for_domain", return_value="acme"),
            patch.object(discovery, "get_login_providers", return_value=[action]) as get_providers,
        ):
            discovery.resolve_login_route("person@acme.example", "https://evil.example/steal")

        get_providers.assert_called_once_with("", tenant_slug="acme")

    def test_unusable_requested_method_fails_closed_and_warns(self):
        discovery = self._module()
        with self.assertLogs("core.auth.discovery", level="WARNING") as captured:
            with (
                override_settings(
                    ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"], "method": "saml"}}
                ),
                patch.object(discovery, "_tenant_slug_for_domain", return_value="acme"),
                patch.object(
                    discovery,
                    "get_login_providers",
                    return_value=[{"protocol": "OIDC", "url": "/oidc/acme/", "tenant_bound": True, "name": "Acme"}],
                ),
            ):
                route = discovery.resolve_login_route("person@acme.example")

        self.assertEqual(route.mode, "password")
        self.assertEqual(route.actions, ())
        self.assertIn("saml", " ".join(captured.output).lower())

    def test_domain_lookup_uses_unscoped_live_tenant_query(self):
        discovery = self._module()
        tenant_model = SimpleNamespace(_base_manager=SimpleNamespace(filter=None))
        queryset = SimpleNamespace(exists=lambda: True)
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery.apps, "get_model", return_value=tenant_model),
            patch.object(tenant_model._base_manager, "filter", return_value=queryset) as tenant_filter,
        ):
            slug = discovery._tenant_slug_for_domain("acme.example")

        self.assertEqual(slug, "acme")
        tenant_filter.assert_called_once_with(slug="acme", deleted_at__isnull=True)

    def test_domain_mapping_uses_exact_match_not_a_parent_domain(self):
        discovery = self._module()
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery.apps, "get_model") as get_model,
        ):
            slug = discovery._tenant_slug_for_domain("mail.acme.example")

        self.assertIsNone(slug)
        get_model.assert_not_called()

    def test_deleted_tenant_fails_closed_and_public_domain_skips_lookup(self):
        discovery = self._module()
        tenant_model = SimpleNamespace(_base_manager=SimpleNamespace(filter=None))
        queryset = SimpleNamespace(exists=lambda: False)
        with (
            self.assertLogs("core.auth.discovery", level="WARNING"),
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery.apps, "get_model", return_value=tenant_model) as get_model,
            patch.object(tenant_model._base_manager, "filter", return_value=queryset) as tenant_filter,
        ):
            self.assertIsNone(discovery._tenant_slug_for_domain("acme.example"))
            self.assertIsNone(discovery._tenant_slug_for_domain("gmail.com"))

        tenant_filter.assert_called_once_with(slug="acme", deleted_at__isnull=True)
        get_model.assert_called_once_with("organization", "Tenant")

    def test_malformed_domain_and_mapping_values_fail_closed(self):
        discovery = self._module()
        self.assertIsNone(discovery._claimable_login_domain("bad..example"))

        cases = (
            (["not-a-mapping"], "acme.example"),
            ({None: {"domains": ["acme.example"]}, "bad": "not-a-config"}, "acme.example"),
            ({"bad": {"domains": ["bad_.example"]}}, "acme.example"),
        )
        for mapping, domain in cases:
            with self.subTest(mapping=mapping):
                with override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=mapping):
                    self.assertIsNone(discovery._tenant_slug_for_domain(domain))

    def test_unavailable_tenant_model_fails_closed(self):
        discovery = self._module()
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery.apps, "get_model", side_effect=LookupError),
        ):
            self.assertIsNone(discovery._tenant_slug_for_domain("acme.example"))

    def test_route_is_independent_of_authenticated_ambient_tenant(self):
        discovery = self._module()
        action = {"protocol": "OIDC", "url": "/oidc/acme/", "tenant_bound": True, "name": "Tenant display name"}
        tenant_model = SimpleNamespace(_base_manager=SimpleNamespace(filter=None))
        queryset = SimpleNamespace(exists=lambda: True)
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=self.config),
            patch.object(discovery.apps, "get_model", return_value=tenant_model),
            patch.object(tenant_model._base_manager, "filter", return_value=queryset),
            patch.object(discovery, "get_login_providers", return_value=[action]),
        ):
            anonymous_route = discovery.resolve_login_route("person@acme.example")
            set_current_tenant(SimpleNamespace(pk=999, slug="other-tenant"))
            set_current_user(SimpleNamespace(is_authenticated=True))
            try:
                authenticated_route = discovery.resolve_login_route("person@acme.example")
            finally:
                set_current_user(None)
                set_current_tenant(None)

        self.assertEqual(anonymous_route, authenticated_route)

    def test_login_route_is_frozen(self):
        discovery = self._module()
        route = discovery.LoginRoute(mode="password", url="/accounts/login/", actions=())

        with self.assertRaises(AttributeError):
            route.mode = "redirect"


class LoginDiscoveryDatabaseTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")
        self.other_tenant = Tenant.objects.create(name="Other Corp", slug="other")
        self.discovery = importlib.import_module("core.auth.discovery")

    def test_route_ignores_authenticated_active_tenant_and_user_tables(self):
        get_user_model().objects.create_user(username="known", email="known@acme.example")
        route_settings = {
            "ITAMBOX_TENANT_LOGIN_DOMAINS": {"acme": {"domains": ["acme.example"]}},
            "ITAMBOX_TENANT_SAML_CONFIGS": {},
            "ITAMBOX_TENANT_OIDC_CONFIGS": {"acme": OIDC_CONFIG},
        }
        with override_settings(**route_settings):
            with CaptureQueriesContext(connection) as anonymous_queries:
                anonymous_route = self.discovery.resolve_login_route("known@acme.example")
            with CaptureQueriesContext(connection) as unknown_user_queries:
                unknown_user_route = self.discovery.resolve_login_route("missing@acme.example")
            set_current_tenant(self.other_tenant)
            set_current_user(SimpleNamespace(is_authenticated=True, is_superuser=False))
            try:
                with CaptureQueriesContext(connection) as authenticated_queries:
                    authenticated_route = self.discovery.resolve_login_route("missing@acme.example")
            finally:
                set_current_user(None)
                set_current_tenant(None)

        expected = reverse("oidc_authentication_init_tenant", kwargs={"tenant_slug": "acme"})
        self.assertEqual(anonymous_route, unknown_user_route)
        self.assertEqual(anonymous_route, authenticated_route)
        self.assertEqual(anonymous_route.mode, "redirect")
        self.assertEqual(anonymous_route.url, expected)
        query_counts = {len(anonymous_queries), len(unknown_user_queries), len(authenticated_queries)}
        self.assertEqual(len(query_counts), 1)
        query_text = " ".join(
            query["sql"].lower()
            for queries in (anonymous_queries, unknown_user_queries, authenticated_queries)
            for query in queries
        )
        _assert_no_user_or_membership_queries(self, [query_text])

    def test_existing_and_unknown_addresses_on_same_domain_are_indistinguishable(self):
        get_user_model().objects.create_user(username="known", email="known@acme.example")
        route_settings = {
            "ITAMBOX_TENANT_LOGIN_DOMAINS": {"acme": {"domains": ["acme.example"]}},
            "ITAMBOX_TENANT_SAML_CONFIGS": {},
            "ITAMBOX_TENANT_OIDC_CONFIGS": {"acme": OIDC_CONFIG},
        }
        answers = []
        with override_settings(**route_settings):
            for identifier in ("known@acme.example", "missing@acme.example"):
                queries = []
                with connection.execute_wrapper(_record_query(queries)):
                    response = self.client.post(reverse("login_discover"), {"identifier": identifier})
                body = response.content.decode()
                body = re.sub(
                    r'name="csrfmiddlewaretoken" value="[^"]+"', 'name="csrfmiddlewaretoken" value="<token>"', body
                )
                body = re.sub(r'nonce="[^"]+"', 'nonce="<nonce>"', body)
                answers.append((response.status_code, response.get("Location"), body, len(queries)))
                _assert_no_user_or_membership_queries(self, queries)

        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0][0], 302)

    def test_password_domain_matches_unknown_and_bare_answers_without_identity_queries(self):
        get_user_model().objects.create_user(username="known", email="known@acme.example")
        identifiers = (
            "person@unknown.example",
            "bare-user",
            "known@acme.example",
            "missing@acme.example",
        )
        answers = []
        query_counts = []
        route_settings = {
            "ITAMBOX_TENANT_LOGIN_DOMAINS": {"acme": {"domains": ["acme.example"]}},
            "ITAMBOX_TENANT_SAML_CONFIGS": {},
            "ITAMBOX_TENANT_OIDC_CONFIGS": {},
        }
        with override_settings(**route_settings):
            for identifier in identifiers:
                queries = []
                with connection.execute_wrapper(_record_query(queries)):
                    response = self.client.post(reverse("login_discover"), {"identifier": identifier})
                body = response.content.decode()
                body = re.sub(
                    r'name="csrfmiddlewaretoken" value="[^"]+"', 'name="csrfmiddlewaretoken" value="<token>"', body
                )
                body = re.sub(r'nonce="[^"]+"', 'nonce="<nonce>"', body)
                self.assertRegex(body, r"<details[^>]* open>")
                answers.append((response.status_code, response.get("Location"), body))
                query_counts.append(len(queries))
                _assert_no_user_or_membership_queries(self, queries)

        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[1], answers[2])
        self.assertEqual(answers[2], answers[3])
        self.assertEqual(query_counts[2], query_counts[3])

    def test_deleted_mapping_fails_closed_without_global_or_other_tenant_fallback(self):
        self.tenant.delete()
        with (
            self.assertLogs("core.auth.discovery", level="WARNING"),
            override_settings(
                ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
                ITAMBOX_TENANT_SAML_CONFIGS={},
                ITAMBOX_TENANT_OIDC_CONFIGS={"other": OIDC_CONFIG},
                **OIDC_CONFIG,
            ),
        ):
            route = self.discovery.resolve_login_route("person@acme.example")

        self.assertEqual(route.mode, "password")
        self.assertEqual(route.actions, ())

    def test_incomplete_tenant_oidc_provider_does_not_fall_back_to_global_oidc(self):
        with (
            self.assertLogs("core.auth.discovery", level="WARNING"),
            override_settings(
                ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
                ITAMBOX_TENANT_SAML_CONFIGS={},
                ITAMBOX_TENANT_OIDC_CONFIGS={"acme": {"OIDC_RP_CLIENT_ID": "tenant-client"}},
                **OIDC_CONFIG,
            ),
        ):
            route = self.discovery.resolve_login_route("person@acme.example")

        self.assertEqual(route.mode, "password")
        self.assertEqual(route.actions, ())

    def test_disabled_tenant_oidc_provider_fails_closed_despite_global_oidc(self):
        disabled_config = {**OIDC_CONFIG, "enabled": False}
        with (
            self.assertLogs("core.auth.discovery", level="WARNING"),
            override_settings(
                ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
                ITAMBOX_TENANT_SAML_CONFIGS={},
                ITAMBOX_TENANT_OIDC_CONFIGS={"acme": disabled_config},
                **OIDC_CONFIG,
            ),
        ):
            route = self.discovery.resolve_login_route("person@acme.example")

        self.assertEqual(route.mode, "password")
        self.assertEqual(route.actions, ())


class LoginIdentifierFormTests(SimpleTestCase):
    def test_identifier_form_accepts_email_and_bare_username(self):
        auth_forms = importlib.import_module("core.forms.auth")

        for value in ("person@acme.example", "bare-user"):
            with self.subTest(value=value):
                form = auth_forms.LoginIdentifierForm(data={"identifier": value})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["identifier"], value)


class LoginDiscoveryViewTests(SimpleTestCase):
    databases = {"default"}

    def setUp(self):
        self.url = reverse("login_discover")
        self.login_url = reverse("login")

    def test_get_redirects_to_login_and_other_methods_are_not_allowed(self):
        response = self.client.get(self.url)
        put_response = self.client.put(self.url, {"identifier": "person@acme.example"})

        self.assertRedirects(response, self.login_url, fetch_redirect_response=False)
        self.assertEqual(put_response.status_code, 405)

    def test_legacy_login_page_keeps_password_form_when_discovery_is_disabled(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={},
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={},
        ):
            response = self.client.get(self.login_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')

    def test_post_redirects_to_password_login_when_discovery_is_disabled(self):
        with override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={}):
            response = self.client.post(self.url, {"identifier": "person@acme.example"})

        self.assertRedirects(response, self.login_url, fetch_redirect_response=False)

    def _autofocus_names(self, response):
        return re.findall(
            r'<input[^>]*name="(\w+)"[^>]*autofocus|<input[^>]*autofocus[^>]*name="(\w+)"', response.content.decode()
        )

    def test_exactly_one_autofocus_target_per_discovery_state(self):
        domains = {"acme": {"domains": ["acme.example"]}}
        with override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=domains):
            initial = self.client.get(self.login_url)
            with patch("django.contrib.auth.forms.authenticate", return_value=None):
                failed = self.client.post(self.login_url, {"username": "x", "password": "y"})
            password_mode = self.client.post(self.url, {"identifier": "bare-user"})
        for response, expected in ((initial, "identifier"), (failed, "username"), (password_mode, "username")):
            with self.subTest(expected=expected):
                found = [a or b for a, b in self._autofocus_names(response)]
                self.assertEqual(found, [expected])

    def test_invalid_identifier_form_renders_password_login_without_resolving(self):
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}}),
            patch("core.views.auth.resolve_login_route") as resolver,
        ):
            response = self.client.post(self.url, {"identifier": ""})

        self.assertEqual(response.status_code, 200)
        resolver.assert_not_called()

    def test_post_ignores_tenant_and_url_fields_and_is_never_cached(self):
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}}),
            patch(
                "core.views.auth.resolve_login_route",
                return_value=SimpleNamespace(mode="redirect", url="/oidc/authenticate/acme/", actions=()),
            ) as resolver,
        ):
            response = self.client.post(
                self.url,
                {
                    "identifier": "person@acme.example",
                    "next": "/assets/",
                    "tenant": "another-tenant",
                    "url": "https://evil.example/",
                },
            )

        self.assertRedirects(response, "/oidc/authenticate/acme/", fetch_redirect_response=False)
        resolver.assert_called_once_with("person@acme.example", "/assets/")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertFalse(response.wsgi_request.session.modified)

    def test_unsafe_next_is_discarded_and_post_requires_csrf(self):
        with (
            override_settings(
                ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
                ALLOWED_HOSTS=["testserver"],
            ),
            patch(
                "core.views.auth.resolve_login_route",
                return_value=SimpleNamespace(mode="password", url=self.login_url, actions=()),
            ) as resolver,
        ):
            response = self.client.post(
                self.url,
                {"identifier": "person@acme.example", "next": "//evil.example/", "url": "/oidc/"},
            )

        self.assertEqual(response.status_code, 200)
        resolver.assert_called_once_with("person@acme.example", "")
        html = response.content.decode()
        self.assertIn('name="identifier"', html)
        self.assertIn("<details", html)
        self.assertLess(html.index("<details"), html.index('name="username"'))
        self.assertNotRegex(html, r"(?i)style\s*=")
        strict_client = Client(enforce_csrf_checks=True)
        rejected = strict_client.post(self.url, {"identifier": "person@acme.example"})
        self.assertEqual(rejected.status_code, 403)

    def test_choice_mode_uses_protocol_only_not_tenant_names(self):
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}}),
            patch(
                "core.views.auth.resolve_login_route",
                return_value=SimpleNamespace(
                    mode="choose",
                    url=self.login_url,
                    actions=({"protocol": "OIDC", "url": "/oidc/"}, {"protocol": "SAML", "url": "/saml/"}),
                ),
            ),
        ):
            response = self.client.post(self.url, {"identifier": "person@acme.example"})

        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sign in with OIDC", html)
        self.assertIn("Sign in with SAML", html)
        self.assertNotIn("Acme", html)

    def test_choose_mode_focuses_first_provider_and_announces_politely(self):
        with (
            override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}}),
            patch(
                "core.views.auth.resolve_login_route",
                return_value=SimpleNamespace(
                    mode="choose",
                    url=self.login_url,
                    actions=({"protocol": "OIDC", "url": "/oidc/"}, {"protocol": "SAML", "url": "/saml/"}),
                ),
            ),
        ):
            response = self.client.post(self.url, {"identifier": "person@acme.example"})

        html = response.content.decode()
        self.assertEqual(self._autofocus_names(response), [])
        self.assertEqual(html.count("autofocus"), 1)
        self.assertRegex(html, r'<a href="/oidc/"[^>]*autofocus')
        self.assertNotRegex(html, r'<a href="/saml/"[^>]*autofocus')
        self.assertRegex(html, r'role="status"\s+aria-live="polite"')
        self.assertIn("Several sign-in methods are available", html)

    def test_live_region_is_empty_outside_choose_mode(self):
        with override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}}):
            response = self.client.post(self.url, {"identifier": "bare-user"})

        self.assertNotIn("Several sign-in methods are available", response.content.decode())

    def test_choice_mode_through_real_resolver_exposes_protocol_only(self):
        tenant = Tenant.objects.create(name="Acme Corp", slug="acme")
        domains = {tenant.slug: {"domains": ["acme.example"]}}
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS=domains,
            ITAMBOX_TENANT_SAML_CONFIGS={tenant.slug: SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={tenant.slug: OIDC_CONFIG},
        ):
            response = self.client.post(self.url, {"identifier": "person@acme.example"})

        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sign in with OIDC", html)
        self.assertIn("Sign in with SAML", html)
        self.assertNotIn("Acme", html)
        self.assertIn(reverse("saml2_login_tenant", kwargs={"tenant_slug": tenant.slug}), html)

    def test_legacy_login_page_is_pinned_when_discovery_is_disabled(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={}, ITAMBOX_TENANT_SAML_CONFIGS={}, ITAMBOX_TENANT_OIDC_CONFIGS={}
        ):
            html = self.client.get(self.login_url).content.decode()

        self.assertNotIn('name="identifier"', html)
        self.assertNotIn("login_discover", html)
        self.assertNotIn(reverse("login_discover"), html)
        self.assertNotIn("<details", html)
        self.assertNotIn('role="status"', html)
        self.assertEqual(html.count('action="%s"' % self.login_url), 1)
        self.assertEqual([a or b for a, b in self._autofocus_pairs(html)], ["username"])
        self.assertEqual(self._legacy_fingerprint(html), LEGACY_LOGIN_FINGERPRINT)

    def _autofocus_pairs(self, html):
        return re.findall(
            r'<input[^>]*name="(\w+)"[^>]*autofocus|<input[^>]*autofocus[^>]*name="(\w+)"', html
        )

    def _legacy_fingerprint(self, html):
        card = re.search(r"<form.*</form>", html, re.DOTALL)
        self.assertIsNotNone(card)
        body = re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', 'name="csrfmiddlewaretoken" value="<t>"', card.group(0))
        body = re.sub(r'nonce="[^"]+"', 'nonce="<n>"', body)
        return hashlib.sha256(re.sub(r"\s+", " ", body).encode()).hexdigest()

    def test_unknown_bare_and_public_domain_answers_are_indistinguishable(self):
        answers = []
        mapping = {"acme": {"domains": ["acme.example"]}}
        for identifier in ("person@unknown.example", "bare-user", "person@gmail.com"):
            with override_settings(ITAMBOX_TENANT_LOGIN_DOMAINS=mapping):
                queries = []
                with connection.execute_wrapper(_record_query(queries)):
                    response = Client().post(self.url, {"identifier": identifier})
            body = response.content.decode()
            body = re.sub(
                r'name="csrfmiddlewaretoken" value="[^"]+"', 'name="csrfmiddlewaretoken" value="<token>"', body
            )
            body = re.sub(r'nonce="[^"]+"', 'nonce="<nonce>"', body)
            answers.append((response.status_code, response.get("Location"), body, len(queries)))
            _assert_no_user_or_membership_queries(self, queries)
            self.assertNotIn(identifier, body)
            self.assertRegex(body, r"<details[^>]* open>")

        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[1], answers[2])


class TenantLoginDiscoveryChecksTests(SimpleTestCase):
    def test_mapped_slug_without_tenant_provider_is_warned_even_with_global_oidc(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={},
            OIDC_RP_CLIENT_ID="global-client",
            OIDC_RP_CLIENT_SECRET="test",
            OIDC_OP_AUTHORIZATION_ENDPOINT="https://global.example/authorize",
            OIDC_OP_TOKEN_ENDPOINT="https://global.example/token",
            OIDC_OP_USER_ENDPOINT="https://global.example/userinfo",
            OIDC_OP_ISSUER="https://global.example/",
            OIDC_OP_JWKS_ENDPOINT="https://global.example/jwks",
        ):
            warnings = checks.check_tenant_login_discovery(None)

        self.assertIn("core.W005", {warning.id for warning in warnings})

    def test_default_saml_only_deployment_has_no_w005(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS={"default": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={},
        ):
            warnings = checks.check_tenant_login_discovery(None)

        self.assertNotIn("core.W005", {warning.id for warning in warnings})

    def test_default_saml_does_not_satisfy_an_oidc_only_mapping(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"], "method": "oidc"}},
            ITAMBOX_TENANT_SAML_CONFIGS={"default": SAML_CONFIG},
            ITAMBOX_TENANT_OIDC_CONFIGS={},
        ):
            warnings = checks.check_tenant_login_discovery(None)

        self.assertIn("core.W005", {warning.id for warning in warnings})

    def test_mapped_tenant_provider_is_accepted_and_has_no_gate_warning(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": OIDC_CONFIG},
        ):
            warnings = checks.check_tenant_login_discovery(None)

        self.assertNotIn("core.W005", {warning.id for warning in warnings})
        self.assertNotIn("core.W006", {warning.id for warning in warnings})

    def test_incomplete_tenant_oidc_config_warns_despite_global_oidc_settings(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={"acme": {"OIDC_RP_CLIENT_ID": "tenant-client"}},
            **OIDC_CONFIG,
        ):
            warnings = checks.check_tenant_login_discovery(None)

        self.assertIn("core.W005", {warning.id for warning in warnings})

    def test_unmapped_tenants_and_duplicate_oidc_identity_are_warned(self):
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS={},
            ITAMBOX_TENANT_OIDC_CONFIGS={"north": OIDC_CONFIG, "west": OIDC_CONFIG},
        ):
            warnings = checks.check_tenant_login_discovery(None)

        warning_ids = {warning.id for warning in warnings}
        self.assertIn("core.W006", warning_ids)
        self.assertIn("core.W007", warning_ids)
        missing_domain_messages = [warning.msg for warning in warnings if warning.id == "core.W006"]
        self.assertEqual(len(missing_domain_messages), 2)

    def test_duplicate_inline_saml_entity_id_is_warned(self):
        metadata = {
            "inline": [
                '<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example/entity" />'
            ]
        }
        saml_configs = {
            "north": {"entityid": "https://north.example/sp", "metadata": metadata},
            "west": {"entityid": "https://west.example/sp", "metadata": metadata},
        }
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
            ITAMBOX_TENANT_SAML_CONFIGS=saml_configs,
            ITAMBOX_TENANT_OIDC_CONFIGS={},
        ):
            warnings = checks.check_tenant_login_discovery(None)

        self.assertIn("core.W007", {warning.id for warning in warnings})

    def test_duplicate_idp_check_is_silent_when_discovery_is_not_configured(self):
        for domains in ({}, None):
            with self.subTest(domains=domains):
                with override_settings(
                    ITAMBOX_TENANT_LOGIN_DOMAINS=domains,
                    ITAMBOX_TENANT_SAML_CONFIGS={},
                    ITAMBOX_TENANT_OIDC_CONFIGS={"north": OIDC_CONFIG, "west": OIDC_CONFIG},
                ):
                    warnings = checks.check_tenant_login_discovery(None)

                self.assertNotIn("core.W007", {warning.id for warning in warnings})

    def test_saml_method_mapping_reports_unclaimed_sso_and_duplicate_identity(self):
        saml_configs = {
            "acme": SAML_CONFIG,
            "north": SAML_CONFIG,
            "broken": {"entityid": "https://broken.example/sp", "metadata": {}},
        }
        oidc_configs = {"default": OIDC_CONFIG, "disabled": {**OIDC_CONFIG, "enabled": False}}
        with override_settings(
            ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"], "method": "oidc"}},
            ITAMBOX_TENANT_SAML_CONFIGS=saml_configs,
            ITAMBOX_TENANT_OIDC_CONFIGS=oidc_configs,
        ):
            warnings = checks.check_tenant_login_discovery(None)

        warning_ids = {warning.id for warning in warnings}
        self.assertIn("core.W005", warning_ids)
        self.assertIn("core.W006", warning_ids)
        self.assertIn("core.W007", warning_ids)

    def test_saml_identity_helpers_handle_config_shapes_and_invalid_xml(self):
        explicit = checks._saml_identities({"idp_entity_id": " https://idp.example/explicit "})
        self.assertEqual(explicit, {"https://idp.example/explicit"})
        self.assertEqual(checks._saml_identities({"metadata": "invalid"}), set())
        self.assertEqual(
            checks._saml_identities({"metadata": {"remote": {"url": " https://idp.example/remote "}}}),
            {"https://idp.example/remote"},
        )
        self.assertEqual(checks._saml_identities({"metadata": {"remote": "invalid"}}), set())

        nested = (
            '<EntitiesDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata">'
            '<EntityDescriptor entityID="https://idp.example/nested" />'
            "</EntitiesDescriptor>"
        )
        self.assertEqual(checks._inline_saml_identities(nested), {"https://idp.example/nested"})
        self.assertEqual(checks._inline_saml_identities(42), set())
        self.assertEqual(checks._inline_saml_identities(("<broken", 42, nested)), {"https://idp.example/nested"})

    def test_duplicate_oidc_identity_without_resolved_issuer_is_ignored(self):
        with (
            override_settings(
                ITAMBOX_TENANT_LOGIN_DOMAINS={"acme": {"domains": ["acme.example"]}},
                ITAMBOX_TENANT_OIDC_CONFIGS={"north": OIDC_CONFIG},
                ITAMBOX_TENANT_SAML_CONFIGS={},
            ),
            patch.object(providers, "is_usable_oidc_config", return_value=True),
            patch.object(providers, "_resolve_oidc_setting", return_value=""),
        ):
            warnings = checks._duplicate_idp_warnings()

        self.assertEqual(warnings, [])
