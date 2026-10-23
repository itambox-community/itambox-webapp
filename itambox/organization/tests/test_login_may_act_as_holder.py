"""The custody signer check applies the same exact-identity rule as SCIM linking."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from organization.models import AssetHolder, Tenant
from organization.services.identity_provisioning import login_may_act_as_holder

User = get_user_model()


class LoginMayActAsHolderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Identity Tenant", slug="identity-tenant")
        self.other_tenant = Tenant.objects.create(name="Other Identity Tenant", slug="other-identity-tenant")

    def _holder(self, **values):
        defaults = {"first_name": "Jane", "last_name": "Doe", "tenant": self.tenant, "upn": "", "email": ""}
        defaults.update(values)
        return AssetHolder.objects.create(**defaults)

    def _login(self, username, email=""):
        return User.objects.create_user(username=username, email=email, password="x")

    def test_linked_holder_accepts_only_its_own_login(self):
        owner = self._login("owner@example.test", "owner@example.test")
        lookalike = self._login("lookalike", "owner@example.test")
        holder = self._holder(user=owner, upn="owner@example.test", email="owner@example.test")

        self.assertTrue(login_may_act_as_holder(owner, holder))
        self.assertFalse(login_may_act_as_holder(lookalike, holder))

    def test_unlinked_holder_matches_by_upn_case_insensitively(self):
        holder = self._holder(upn="Jane.Doe@Example.test", email="jane@example.test")

        self.assertTrue(login_may_act_as_holder(self._login("jane.doe@example.test"), holder))

    def test_email_does_not_stand_in_for_a_different_upn(self):
        holder = self._holder(upn="jane.doe@example.test", email="shared@example.test")
        mailbox_owner = self._login("someone-else@example.test", "shared@example.test")

        self.assertFalse(login_may_act_as_holder(mailbox_owner, holder))

    def test_email_matches_only_when_the_holder_has_no_upn(self):
        holder = self._holder(email="Jane@Example.test")

        self.assertTrue(login_may_act_as_holder(self._login("jane-local", "jane@example.test"), holder))
        self.assertFalse(login_may_act_as_holder(self._login("jane@example.test", ""), holder))

    def test_login_linked_to_another_holder_of_the_tenant_is_refused(self):
        login = self._login("jane.doe@example.test", "jane.doe@example.test")
        self._holder(user=login, upn="jane.primary@example.test")
        unlinked = self._holder(upn="jane.doe@example.test")

        self.assertFalse(login_may_act_as_holder(login, unlinked))

    def test_a_link_in_another_tenant_does_not_block_the_match(self):
        login = self._login("jane.doe@example.test")
        self._holder(user=login, upn="jane.doe@example.test", tenant=self.other_tenant)
        unlinked = self._holder(upn="jane.doe@example.test")

        self.assertTrue(login_may_act_as_holder(login, unlinked))

    def test_anonymous_or_missing_inputs_never_match(self):
        holder = self._holder(upn="jane.doe@example.test")

        self.assertFalse(login_may_act_as_holder(AnonymousUser(), holder))
        self.assertFalse(login_may_act_as_holder(None, holder))
        self.assertFalse(login_may_act_as_holder(self._login("jane.doe@example.test"), None))
        self.assertFalse(login_may_act_as_holder(self._login("no-email-login", ""), self._holder(email="")))
