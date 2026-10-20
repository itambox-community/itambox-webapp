"""Webhooks and event rules are stable; their list pages must not announce Beta."""

from django.test import TestCase
from django.urls import reverse

from core.features import STABLE
from core.tests.mixins import TenantTestMixin
from itambox.capabilities import registry

BANNER_ID = 'id="beta-module-banner"'


class StableAutomationSurfacesTests(TenantTestMixin, TestCase):
    """The registry declares automation stable, so the deprecated app-level flag may not reintroduce the banner."""

    def setUp(self):
        self.setup_tenant_context(name="Automation Tenant", slug="automation-tenant")
        self.client_login_to_tenant(
            self.tenant_user,
            self.tenant,
            role_permissions=["extras.view_webhookendpoint", "extras.view_eventrule"],
        )

    def test_the_capability_that_owns_both_surfaces_is_stable(self):
        capability = registry.get("automation.webhooks")

        self.assertEqual(capability.maturity, STABLE)
        self.assertEqual(set(capability.owns), {"extras.EventRule", "extras.WebhookEndpoint"})

    def test_neither_list_announces_a_beta_module(self):
        for url_name in ("extras:webhookendpoint_list", "extras:eventrule_list"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))

                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["is_beta_module"])
                self.assertNotContains(response, BANNER_ID)
