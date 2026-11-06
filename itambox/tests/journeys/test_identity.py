"""Identity journeys: SCIM deprovisioning keeps obligations visible and never guesses identity links."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from assets.models import StatusLabel
from assets.services import checkout_asset
from core.auth.saml import TenantSaml2Backend
from core.managers import set_current_tenant
from core.models import ObjectChange
from organization.models import AssetHolder, Membership
from organization.services.offboarding import get_offboarding_report
from users.models import Token

from .support import JourneyMixin

User = get_user_model()


class ScimJourneyTests(JourneyMixin, TestCase):
    def setUp(self):
        self.make_tenant("journey-scim")
        self.provisioner = self.make_member(
            "scim-provisioner",
            {"organization.change_membership", "organization.add_membership", "users.view_usergroup"},
        )
        token = Token.objects.create(
            user=self.provisioner, tenant=self.tenant, expires=timezone.now() + timezone.timedelta(days=1)
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token.key}"}

    def _user_url(self, pk=None):
        if pk is None:
            return reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        return reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": pk})

    def test_scim_delete_of_asset_holder_keeps_holder_and_offboarding_report_reachable(self):
        person = self.make_member("leaving-person", set())
        holder = self.make_holder(user=person)
        profile = (holder.first_name, holder.last_name, holder.upn, holder.email, holder.tenant_id)
        deployable = StatusLabel.objects.create(
            name="Journey SCIM Deployable",
            slug="journey-scim-deployable",
            type=StatusLabel.TYPE_DEPLOYABLE,
        )
        deployed = StatusLabel.objects.create(
            name="Journey SCIM Deployed",
            slug="journey-scim-deployed",
            type=StatusLabel.TYPE_DEPLOYED,
        )
        asset = self.make_asset(status=deployable)
        checkout_asset(asset=asset, holder=holder, request=None, status=deployed)

        response = self.client.delete(self._user_url(person.pk), **self.auth)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # The person is deprovisioned, but what they still hold must stay visible.
        self.assertTrue(AssetHolder.objects.filter(pk=holder.pk).exists(), "holder vanished from the active set")
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id, "deprovisioning must revoke the holder-to-login link")
        self.assertIsNone(holder.deleted_at, "deprovisioning must not soft-delete the holder")
        self.assertEqual((holder.first_name, holder.last_name, holder.upn, holder.email, holder.tenant_id), profile)
        report = get_offboarding_report(holder)
        self.assertTrue(report.items, "offboarding report lost the outstanding assignment")
        self.assertEqual(len(report.for_kind("asset_assignment")), 1)

        reviewer = self.make_member("offboarding-reviewer", {"organization.view_assetholder", "assets.view_asset"})
        self.client_login_to_tenant(reviewer, self.tenant)
        detail = self.client.get(reverse("organization:assetholder_detail", kwargs={"pk": holder.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, asset.asset_tag)
        self.assertEqual(len(detail.context["offboarding_report"].for_kind("asset_assignment")), 1)

    def test_scim_create_does_not_link_unlinked_holder_by_email_when_upn_differs(self):
        holder = AssetHolder.objects.create(
            first_name="Jane",
            last_name="Smith",
            upn="shared@example.test",
            email="shared@example.test",
            tenant=self.tenant,
        )
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "someone-else@example.test",
            "name": {"familyName": "Else", "givenName": "Someone"},
            "emails": [{"value": "shared@example.test", "primary": True}],
            "active": True,
        }

        response = self.client.post(self._user_url(), data=payload, content_type="application/json", **self.auth)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id, "an unlinked holder was bound to a different login by email alone")
        self.assertEqual(holder.upn, "shared@example.test")
        self.assertEqual(holder.email, "shared@example.test")
        self.assertEqual(holder.first_name, "Jane")
        self.assertEqual(holder.last_name, "Smith")

    def test_scim_update_does_not_link_unlinked_holder_by_email_when_upn_differs(self):
        person = self.make_member("scim-update-person", set())
        holder = self.make_holder(
            first_name="Chief",
            last_name="Executive",
            upn="shared@example.test",
            email="shared@example.test",
        )
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "emails.value", "value": "shared@example.test"}],
        }

        response = self.client.patch(
            self._user_url(person.pk), data=payload, content_type="application/json", **self.auth
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        person.refresh_from_db()
        self.assertEqual(person.email, "shared@example.test")
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id, "an update bound a different login to the holder by email alone")
        self.assertEqual(holder.upn, "shared@example.test")
        self.assertEqual(holder.email, "shared@example.test")
        self.assertEqual(holder.first_name, "Chief")
        self.assertEqual(holder.last_name, "Executive")


SAML_ATTRIBUTE_MAPPING = {"email": ("email",), "first_name": ("first_name",), "last_name": ("last_name",)}


class SamlNameIdBindingJourneyTests(JourneyMixin, TestCase):
    """A SAML NameID resolves an existing account only inside the asserting tenant."""

    def setUp(self):
        self.make_tenant("journey-saml-a")
        self.other = self.make_provider_tenant("journey-saml-b")
        set_current_tenant(self.tenant)
        self.addCleanup(set_current_tenant, None)

    def _assert(self, name_id, **ava):
        session_info = {
            "issuer": "https://idp.example.test/",
            "name_id": SimpleNamespace(text=name_id),
            "ava": {"email": [f"{name_id}@asserted.test"], "first_name": ["Asserted"], "last_name": ["Name"], **ava},
        }
        return TenantSaml2Backend().authenticate(
            None, session_info=session_info, attribute_mapping=SAML_ATTRIBUTE_MAPPING, create_unknown_user=True
        )

    def _snapshot(self):
        return (Membership.objects.count(), AssetHolder.objects.count(), ObjectChange.objects.count())

    def test_assertion_for_a_foreign_tenants_user_is_refused_without_side_effects(self):
        victim = self.make_member("saml-foreign-user", set(), tenant=self.other)
        before = self._snapshot()

        self.assertIsNone(self._assert(victim.username))

        victim.refresh_from_db()
        self.assertEqual(victim.email, "saml-foreign-user@example.test")
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(Membership.objects.filter(user=victim, tenant=self.tenant).exists())

    def test_assertion_for_a_superuser_is_refused_even_with_membership(self):
        root = User.objects.create_superuser(username="saml-root", email="root@example.test", password="x")
        Membership.objects.create(user=root, tenant=self.tenant)
        staff = User.objects.create_user(username="saml-staff", email="staff@example.test", is_staff=True)
        Membership.objects.create(user=staff, tenant=self.tenant)
        before = self._snapshot()

        self.assertIsNone(self._assert("saml-root"))
        self.assertIsNone(self._assert("saml-staff"))

        root.refresh_from_db()
        self.assertEqual(root.email, "root@example.test")
        self.assertEqual(self._snapshot(), before)

    def test_tenant_opt_out_of_unknown_user_provisioning_is_honoured(self):
        configs = {self.tenant.slug: {"SAML_CREATE_UNKNOWN_USER": False}}
        before = self._snapshot()
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS=configs):
            self.assertIsNone(self._assert("saml-brand-new"))

        self.assertFalse(User.objects.filter(username="saml-brand-new").exists())
        self.assertEqual(self._snapshot(), before)

    def test_unknown_user_is_still_provisioned_when_the_key_is_absent(self):
        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS={self.tenant.slug: {}}):
            user = self._assert("saml-brand-new")

        self.assertIsNotNone(user)
        self.assertTrue(Membership.objects.filter(user=user, tenant=self.tenant, is_active=True).exists())

    def test_member_of_the_asserting_tenant_still_logs_in(self):
        member = self.make_member("saml-member", set())

        self.assertEqual(self._assert("saml-member"), member)
