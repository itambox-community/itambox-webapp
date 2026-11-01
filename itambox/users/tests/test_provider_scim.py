from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.tests.mixins import grant
from organization.models import AssetHolder, Membership, Role, RoleGrant, RoleGrantScope, Tenant
from users.api.scim.provider_patch import GroupMemberOperation, GroupPatch, SCIMPatchError, UserPatch
from users.api.scim.provider_services import (
    _apply_group_member_operations,
    _save_group_or_raise,
    apply_provider_group_patch,
    apply_provider_user_patch,
    create_provider_group,
    sync_provider_group_members,
)
from users.models import GroupMembership, Token, UserGroup

User = get_user_model()


class ProviderSCIMProvisioningTests(TestCase):
    """Provider-level SCIM: an MSP is a plain ``Tenant(is_provider=True)`` — there is no
    ``Provider`` model anymore. Authorization is the same standard permission the tenant
    SCIM path uses (``organization.change_membership``, checked via ``user.has_perm``
    against role *content*, never ``Role.name``) held on the provider tenant itself; a
    token is "provider-scoped" simply by its ``tenant`` FK pointing at that tenant.
    Provisioning creates a bare ``Membership`` at the provider tenant with NO
    ``RoleGrant`` — permissions/reach are granted in-app afterwards, never implied
    by SCIM.
    """

    def setUp(self):
        # An ordinary (non-provider) tenant, used both because Token.save() requires a
        # tenant (auto-assigns if unset) and to prove a token scoped to a plain tenant is
        # not a "provider token".
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")

        self.provider = Tenant.objects.create(name="MSP One", slug="msp-one", is_provider=True)
        self.other_provider = Tenant.objects.create(name="MSP Two", slug="msp-two", is_provider=True)

        # Provider-tenant-owned role granting the standard permission that gates this
        # surface. Named deliberately unrelated to "admin"/"staff" to prove authorization
        # is resolved from permission CONTENT, not the role's name (D2-2 regression).
        self.role_staff = Role.objects.create(
            tenant=self.provider,
            name="Tier 2 Grant",
            permissions=[
                "organization.change_membership",
                "users.view_usergroup",
                "users.add_usergroup",
                "users.change_usergroup",
                "users.delete_usergroup",
            ],
        )
        # A role WITHOUT that permission.
        self.role_readonly = Role.objects.create(
            tenant=self.provider,
            name="Read Only",
            permissions=[],
        )

        # Authorised provider-staff user: active Membership at the provider tenant +
        # own-scope RoleGrant carrying the gating permission.
        self.admin_user = User.objects.create_user(
            username="provadmin", email="provadmin@msp.com", password="adminpassword"
        )
        grant(self.admin_user, self.provider, self.role_staff)

        # A user with a membership but lacking the permission.
        self.weak_user = User.objects.create_user(username="weak", email="weak@msp.com", password="password123")
        grant(self.weak_user, self.provider, self.role_readonly)

        # Tokens. Token.key plaintext is available right after create().
        self.valid_token = Token.objects.create(
            user=self.admin_user,
            tenant=self.provider,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        # Token scoped to an ordinary (non-provider) tenant — NOT a provider token, even
        # though its user also has an active membership at the provider tenant.
        self.unscoped_token = Token.objects.create(
            user=self.admin_user,
            tenant=self.tenant,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        # Token scoped to the provider tenant, but its user lacks the gating permission.
        self.weak_token = Token.objects.create(
            user=self.weak_user,
            tenant=self.provider,
            expires=timezone.now() + timezone.timedelta(days=1),
        )

        self.auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.valid_token.key}"}

    # ---- Authentication / authorization -------------------------------------------------

    def test_no_auth_is_unauthorized(self):
        url = reverse("api:provider_scim:service-provider-config", kwargs={"provider_slug": self.provider.slug})
        self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_valid_token_passes(self):
        url = reverse("api:provider_scim:service-provider-config", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig",
            response.json()["schemas"],
        )

    def test_real_provider_actor_permissions_are_tenant_scoped(self):
        self.assertFalse(self.admin_user.is_superuser)
        self.assertTrue(self.admin_user.has_perm("organization.change_membership", self.provider))
        self.assertTrue(self.admin_user.has_perm("users.change_usergroup", self.provider))
        self.assertFalse(self.admin_user.has_perm("organization.change_membership", self.tenant))

    def test_token_not_scoped_to_provider_rejected(self):
        """A token whose ``tenant`` is an ordinary tenant (not this provider) is rejected
        even though its user holds an active, sufficiently-permissioned membership at the
        provider tenant — the token's own scope is checked, not just the user's access."""
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.unscoped_token.key}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_token_without_permission_rejected(self):
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.weak_token.key}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_read_only_and_expired_tokens_fail_closed_on_provider_mount(self):
        """Provider SCIM enforces token semantics the same way: a read-only token may
        read but not write; an expired token may do neither, and nothing is created."""
        list_url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        read_only = Token.objects.create(
            user=self.admin_user,
            tenant=self.provider,
            write_enabled=False,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        read_only_headers = {"HTTP_AUTHORIZATION": f"Bearer {read_only.key}"}
        self.assertEqual(self.client.get(list_url, **read_only_headers).status_code, status.HTTP_200_OK)
        response = self.client.post(
            list_url,
            data={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "readonly-prov@msp.com",
            },
            content_type="application/json",
            **read_only_headers,
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(User.objects.filter(username="readonly-prov@msp.com").exists())

        expired = Token.objects.create(
            user=self.admin_user,
            tenant=self.provider,
            expires=timezone.now() - timezone.timedelta(hours=1),
        )
        expired_headers = {"HTTP_AUTHORIZATION": f"Bearer {expired.key}"}
        self.assertEqual(self.client.get(list_url, **expired_headers).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_group_endpoints_require_method_specific_group_permissions(self):
        role = Role.objects.create(
            tenant=self.provider,
            name="SCIM identity only",
            permissions=["organization.change_membership"],
        )
        user = User.objects.create_user(username="scim-identity-only")
        grant(user, self.provider, role)
        token = Token.objects.create(
            user=user,
            tenant=self.provider,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token.key}"}
        group = UserGroup.objects.create(tenant=self.provider, name="Protected group")
        list_url = reverse(
            "api:provider_scim:group-list",
            kwargs={"provider_slug": self.provider.slug},
        )
        detail_url = reverse(
            "api:provider_scim:group-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": group.pk},
        )
        cases = [
            ("get", list_url, None),
            ("post", list_url, {"displayName": "Denied create"}),
            ("get", detail_url, None),
            ("put", detail_url, {"displayName": "Denied update"}),
            ("patch", detail_url, {"Operations": []}),
            ("delete", detail_url, None),
        ]

        for method, url, payload in cases:
            with self.subTest(method=method, url=url):
                client_method = getattr(self.client, method)
                if payload is None:
                    response = client_method(url, **headers)
                else:
                    response = client_method(
                        url,
                        data=payload,
                        content_type="application/json",
                        **headers,
                    )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_token_scoped_to_other_provider_rejected(self):
        """D2-1 regression (cross-tenant token isolation): valid_token is scoped
        (token.tenant) to self.provider; presenting it against other_provider's mount
        must fail on the token-scope check alone, regardless of the user's permissions
        anywhere else."""
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.other_provider.slug})
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authorization_is_permission_content_not_role_name(self):
        """D2-2 regression: authorization must never match on ``Role.name`` — only on
        resolved permission content via ``user.has_perm``. A role literally named
        "Administrator" grants nothing if its permissions list is empty; a role with an
        unrelated name but the actual permission passes.
        """
        decoy_role = Role.objects.create(tenant=self.provider, name="Administrator", permissions=[])
        decoy_user = User.objects.create_user(username="decoy", email="decoy@msp.com")
        grant(decoy_user, self.provider, decoy_role)
        decoy_token = Token.objects.create(
            user=decoy_user,
            tenant=self.provider,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {decoy_token.key}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        legit_role = Role.objects.create(
            tenant=self.provider,
            name="Zzz Custom Grant 42",
            permissions=["organization.change_membership"],
        )
        legit_user = User.objects.create_user(username="legit", email="legit@msp.com")
        grant(legit_user, self.provider, legit_role)
        legit_token = Token.objects.create(
            user=legit_user,
            tenant=self.provider,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {legit_token.key}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_superuser_token_bypasses_permission_check(self):
        """Superusers pass regardless of role/permission content (but the token must
        still be scoped to this provider tenant)."""
        super_user = User.objects.create_superuser(username="root", email="root@msp.com", password="x")
        super_token = Token.objects.create(
            user=super_user,
            tenant=self.provider,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {super_token.key}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ---- Users --------------------------------------------------------------------------

    def test_user_list_returns_provider_staff(self):
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        usernames = {r["userName"] for r in data["Resources"]}
        # Both staff users (admin + weak) are active members of this provider tenant —
        # list membership is not gated by role/permission.
        self.assertEqual(data["totalResults"], 2)
        self.assertIn("provadmin", usernames)
        self.assertIn("weak", usernames)

    def test_provider_patch_preserves_identity_when_other_membership_is_inactive(self):
        shared_user = User.objects.create_user(username="shared-provider-user", email="shared@example.com")
        Membership.objects.create(user=shared_user, tenant=self.provider, is_active=True)
        Membership.objects.create(user=shared_user, tenant=self.tenant, is_active=False)
        detail_url = reverse(
            "api:provider_scim:user-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": shared_user.pk},
        )
        response = self.client.patch(
            detail_url,
            data={"Operations": [{"op": "replace", "path": "userName", "value": "rewritten"}]},
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        shared_user.refresh_from_db()
        self.assertEqual(shared_user.username, "shared-provider-user")

    def test_user_post_creates_user_and_membership(self):
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "newstaff@msp.com",
            "name": {"familyName": "Doe", "givenName": "John"},
            "emails": [{"value": "newstaff@msp.com", "primary": True}],
            "active": True,
        }
        holder = AssetHolder.objects.create(
            first_name="Existing",
            last_name="Provider Holder",
            upn="different-login@msp.com",
            email="newstaff@msp.com",
            tenant=self.provider,
        )
        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["userName"], "newstaff@msp.com")

        user = User.objects.get(username="newstaff@msp.com")
        self.assertTrue(user.is_active)
        self.assertTrue(Membership.objects.filter(user=user, tenant=self.provider, is_active=True).exists())
        holder.refresh_from_db()
        self.assertIsNone(holder.user_id)
        self.assertFalse(AssetHolder.objects.filter(user=user, tenant=self.provider).exists())
        # SCIM provisions identity only: no RoleGrant is auto-created — permissions
        # and reach are granted in-app afterwards, never implied by provisioning.
        self.assertFalse(RoleGrant.objects.filter(membership__user=user).exists())

        # Conflict on duplicate membership.
        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_detail_get_put_patch_delete(self):
        url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "lifecycle@msp.com",
            "name": {"familyName": "Cycle", "givenName": "Life"},
            "emails": [{"value": "lifecycle@msp.com", "primary": True}],
            "active": True,
        }
        created = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers).json()
        pk = created["id"]
        detail_url = reverse("api:provider_scim:user-detail", kwargs={"provider_slug": self.provider.slug, "pk": pk})

        # GET
        self.assertEqual(self.client.get(detail_url, **self.auth_headers).status_code, status.HTTP_200_OK)

        # PUT: this user's only membership is this provider, so global identity is editable.
        put_payload = dict(payload, userName="lifecycle_renamed", active=True)
        response = self.client.put(detail_url, data=put_payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(scim_id=pk)
        self.assertEqual(user.username, "lifecycle_renamed")
        self.assertTrue(Membership.objects.get(user=user, tenant=self.provider).is_active)
        holder = AssetHolder.objects.create(
            user=user,
            tenant=self.tenant,
            first_name="Provider",
            last_name="Holder",
            upn="lifecycle@acme.com",
            email="lifecycle@acme.com",
        )

        collision_user = User.objects.create_user(username="already-used", email="already-used@msp.com")
        Membership.objects.create(user=collision_user, tenant=self.provider, is_active=True)
        conflict_response = self.client.patch(
            detail_url,
            data={"Operations": [{"op": "replace", "path": "userName", "value": "already-used"}]},
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(conflict_response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict_response.json()["scimType"], "uniqueness")
        self.assertEqual(User.objects.get(scim_id=pk).username, "lifecycle_renamed")

        Membership.objects.create(user=user, tenant=self.tenant, is_active=True)
        deactivate_payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        }
        response = self.client.patch(
            detail_url, data=deactivate_payload, content_type="application/json", **self.auth_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Membership.objects.get(user=user, tenant=self.provider).is_active)
        self.assertTrue(User.objects.get(scim_id=pk).is_active)
        list_response = self.client.get(url, **self.auth_headers)
        listed = {resource["id"]: resource for resource in list_response.json()["Resources"]}
        self.assertIn(pk, listed)
        self.assertFalse(listed[pk]["active"])

        # PATCH active back to true. Inactive provider memberships remain addressable
        # so an IdP can re-enable a previously deprovisioned assignment.
        patch_payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": True}],
        }
        response = self.client.patch(
            detail_url, data=patch_payload, content_type="application/json", **self.auth_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Membership.objects.get(user=user, tenant=self.provider).is_active)
        self.assertTrue(User.objects.get(scim_id=pk).is_active)

        # DELETE removes the membership.
        response = self.client.delete(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Membership.objects.filter(user=user, tenant=self.provider).exists())
        # The User row survives.
        self.assertTrue(User.objects.filter(scim_id=pk).exists())
        holder.refresh_from_db()
        self.assertEqual(holder.user_id, user.pk)
        self.assertIsNone(holder.deleted_at)

    def test_provision_deprovision_reprovision_restores_login(self):
        """The lifecycle round trip for a solely-provisioned provider identity: create,
        deactivate, reactivate, full de-provision (DELETE) and re-provision restore the
        membership and login without a manual is_active flip on the surviving User row."""
        list_url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "externalId": "entra-object-lifecycle-prov",
            "userName": "prov.lifecycle@msp.com",
            "name": {"givenName": "Prov", "familyName": "Life"},
            "emails": [{"value": "prov.lifecycle@msp.com", "type": "work"}],
            "active": True,
        }
        created = self.client.post(list_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        scim_id = created.json()["id"]
        user = User.objects.get(scim_id=scim_id)

        detail_url = reverse(
            "api:provider_scim:user-detail", kwargs={"provider_slug": self.provider.slug, "pk": scim_id}
        )

        def patch_active(active):
            return self.client.patch(
                detail_url,
                data={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": [{"op": "replace", "path": "active", "value": active}],
                },
                content_type="application/json",
                **self.auth_headers,
            )

        # Deactivate: sole membership suspended, so the global account mirrors it;
        # the resource stays addressable while inactive.
        response = patch_active(False)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["active"])
        self.assertFalse(User.objects.get(pk=user.pk).is_active)
        self.assertEqual(self.client.get(detail_url, **self.auth_headers).status_code, status.HTTP_200_OK)

        response = patch_active(True)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

        # Full de-provision: the membership is removed; the account is left deactivated.
        response = self.client.delete(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Membership.objects.filter(user=user, tenant=self.provider).exists())
        self.assertFalse(User.objects.get(pk=user.pk).is_active)

        # Re-provision: same userName + externalId correlate to the surviving row; the
        # membership returns active and login works again without manual intervention.
        again = self.client.post(list_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertIn(again.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED))
        membership = Membership.objects.get(user=user, tenant=self.provider)
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.external_id, "entra-object-lifecycle-prov")
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

    # ---- Groups -------------------------------------------------------------------------

    def test_group_list_and_create(self):
        list_url = reverse("api:provider_scim:group-list", kwargs={"provider_slug": self.provider.slug})

        # Empty to start.
        response = self.client.get(list_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["totalResults"], 0)

        # POST creates a provider-tenant-owned group with a provider-staff member.
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Senior Technicians",
            "members": [{"value": str(self.admin_user.id)}],
        }
        response = self.client.post(list_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["displayName"], "Senior Technicians")

        group = UserGroup.objects.get(name="Senior Technicians")
        self.assertEqual(group.tenant, self.provider)
        self.assertTrue(
            GroupMembership.objects.filter(
                user_group=group,
                membership__user=self.admin_user,
            ).exists()
        )
        group_membership = GroupMembership.objects.get(user_group=group)
        self.assertEqual(group_membership.source, GroupMembership.SOURCE_SCIM)
        self.assertEqual(group_membership.external_id, str(self.admin_user.scim_id))
        self.assertEqual(group_membership.added_by, self.admin_user)

        # Now the list shows it.
        response = self.client.get(list_url, **self.auth_headers)
        self.assertEqual(response.json()["totalResults"], 1)

        # Duplicate name -> 409.
        response = self.client.post(list_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_group_member_guard_skips_non_staff(self):
        """A user who is NOT an active member of this provider tenant is silently
        skipped on group sync."""
        outsider = User.objects.create_user(username="outsider", email="out@x.com")
        list_url = reverse("api:provider_scim:group-list", kwargs={"provider_slug": self.provider.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Guarded Group",
            "members": [{"value": str(outsider.id)}, {"value": str(self.admin_user.id)}],
        }
        response = self.client.post(list_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        group = UserGroup.objects.get(name="Guarded Group")
        self.assertTrue(
            GroupMembership.objects.filter(
                user_group=group,
                membership__user=self.admin_user,
            ).exists()
        )
        self.assertFalse(
            GroupMembership.objects.filter(
                user_group=group,
                membership__user=outsider,
            ).exists()
        )

    def test_group_sync_rejects_self_and_other_escalation(self):
        dangerous_role = Role.objects.create(
            tenant=self.provider,
            name="Dangerous inherited role",
            permissions=["assets.delete_asset"],
        )
        group = UserGroup.objects.create(
            tenant=self.provider,
            name="Privileged SCIM group",
        )
        role_grant = RoleGrant.objects.create(user_group=group, role=dangerous_role)
        RoleGrantScope.objects.create(
            role_grant=role_grant,
            scope_type=RoleGrantScope.SCOPE_OWN,
        )
        detail_url = reverse(
            "api:provider_scim:group-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": group.pk},
        )

        for user in (self.admin_user, self.weak_user):
            with self.subTest(user=user.username):
                payload = {
                    "Operations": [
                        {"op": "replace", "path": "displayName", "value": "Must roll back"},
                        {
                            "op": "add",
                            "path": "members",
                            "value": [{"value": str(user.pk)}],
                        },
                    ]
                }
                response = self.client.patch(
                    detail_url,
                    data=payload,
                    content_type="application/json",
                    **self.auth_headers,
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                group.refresh_from_db()
                self.assertEqual(group.name, "Privileged SCIM group")
                self.assertIn("Privilege escalation", str(response.json()))
                self.assertFalse(
                    GroupMembership.objects.filter(
                        user_group=group,
                        membership__user=user,
                    ).exists()
                )

    def test_put_and_patch_reconcile_only_scim_memberships(self):
        group = UserGroup.objects.create(tenant=self.provider, name="Mixed provenance")
        admin_membership = Membership.objects.get(
            tenant=self.provider,
            user=self.admin_user,
        )
        weak_membership = Membership.objects.get(
            tenant=self.provider,
            user=self.weak_user,
        )
        scim_user = User.objects.create_user(username="scim-owned-member")
        scim_membership = Membership.objects.create(
            tenant=self.provider,
            user=scim_user,
        )
        manual_row = GroupMembership.objects.create(
            user_group=group,
            membership=admin_membership,
            source=GroupMembership.SOURCE_MANUAL,
            external_id="manual-record",
            added_by=self.weak_user,
        )
        ldap_row = GroupMembership.objects.create(
            user_group=group,
            membership=weak_membership,
            source=GroupMembership.SOURCE_LDAP,
            external_id="ldap-record",
        )
        scim_row = GroupMembership.objects.create(
            user_group=group,
            membership=scim_membership,
            source=GroupMembership.SOURCE_SCIM,
            external_id=str(scim_user.pk),
            added_by=self.admin_user,
        )
        detail_url = reverse(
            "api:provider_scim:group-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": group.pk},
        )
        scim_row.external_id = "stale-external-id"
        scim_row.save(update_fields=["external_id"])
        reconcile_response = self.client.patch(
            detail_url,
            data={"Operations": [{"op": "replace", "path": "members", "value": [{"value": str(scim_user.pk)}]}]},
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(reconcile_response.status_code, status.HTTP_200_OK)
        scim_row.refresh_from_db()
        self.assertEqual(scim_row.external_id, str(scim_user.scim_id))

        put_payload = {
            "displayName": "Mixed provenance renamed",
            "members": [{"value": str(self.admin_user.pk)}],
        }
        response = self.client.put(
            detail_url,
            data=put_payload,
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(GroupMembership.objects.filter(pk=scim_row.pk).exists())

        for row, expected_source, expected_external_id, expected_actor in (
            (manual_row, GroupMembership.SOURCE_MANUAL, "manual-record", self.weak_user),
            (ldap_row, GroupMembership.SOURCE_LDAP, "ldap-record", None),
        ):
            row.refresh_from_db()
            self.assertEqual(row.source, expected_source)
            self.assertEqual(row.external_id, expected_external_id)
            self.assertEqual(row.added_by, expected_actor)

        patch_payload = {
            "Operations": [{"op": "replace", "path": "members", "value": []}],
        }
        response = self.client.patch(
            detail_url,
            data=patch_payload,
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(GroupMembership.objects.filter(pk=manual_row.pk).exists())
        self.assertTrue(GroupMembership.objects.filter(pk=ldap_row.pk).exists())

    def test_group_detail_put_patch_delete(self):
        group = UserGroup.objects.create(tenant=self.provider, name="Editable Group")
        detail_url = reverse(
            "api:provider_scim:group-detail", kwargs={"provider_slug": self.provider.slug, "pk": group.id}
        )

        # PUT renames + sets members.
        put_payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Renamed Group",
            "members": [{"value": str(self.admin_user.id)}],
        }
        response = self.client.put(detail_url, data=put_payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        group.refresh_from_db()
        self.assertEqual(group.name, "Renamed Group")
        self.assertTrue(
            GroupMembership.objects.filter(
                user_group=group,
                membership__user=self.admin_user,
            ).exists()
        )

        # A tenant-local displayName collision is a SCIM 409, not a database 500.
        UserGroup.objects.create(tenant=self.provider, name="Conflict Group")
        conflict_response = self.client.patch(
            detail_url,
            data={"Operations": [{"op": "replace", "path": "displayName", "value": "Conflict Group"}]},
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(conflict_response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict_response.json()["status"], "409")
        self.assertEqual(conflict_response.json()["scimType"], "uniqueness")
        group.refresh_from_db()
        self.assertEqual(group.name, "Renamed Group")

        # PATCH removes the member.
        patch_payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "path": 'members[value eq "%s"]' % self.admin_user.id}],
        }
        response = self.client.patch(
            detail_url, data=patch_payload, content_type="application/json", **self.auth_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        group.refresh_from_db()
        self.assertFalse(
            GroupMembership.objects.filter(
                user_group=group,
                membership__user=self.admin_user,
            ).exists()
        )
        self.assertFalse(GroupMembership.objects.filter(user_group=group).exists())

        # DELETE soft-deletes.
        response = self.client.delete(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(UserGroup.objects.filter(id=group.id).exists())

    def test_group_patch_validation_errors_use_scim_contract(self):
        group = UserGroup.objects.create(tenant=self.provider, name="Validation Group")
        detail_url = reverse(
            "api:provider_scim:group-detail", kwargs={"provider_slug": self.provider.slug, "pk": group.id}
        )
        cases = [
            (
                {"op": "add", "path": "members", "value": [{"value": "not-an-id"}]},
                None,
                "Invalid member ID",
            ),
            ({"op": "remove"}, "noTarget", "remove operation requires a path"),
        ]

        for operation, expected_scim_type, expected_detail in cases:
            with self.subTest(operation=operation):
                response = self.client.patch(
                    detail_url,
                    data={"Operations": [operation]},
                    content_type="application/json",
                    **self.auth_headers,
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(
                    response.json()["schemas"],
                    ["urn:ietf:params:scim:api:messages:2.0:Error"],
                )
                self.assertEqual(response.json()["status"], "400")
                self.assertEqual(response.json()["detail"], expected_detail)
                if expected_scim_type:
                    self.assertEqual(response.json()["scimType"], expected_scim_type)

    def test_provider_group_isolation(self):
        """Groups owned by another provider tenant are not visible/editable through this
        provider's SCIM mount."""
        other_group = UserGroup.objects.create(tenant=self.other_provider, name="Other Provider Group")
        list_url = reverse("api:provider_scim:group-list", kwargs={"provider_slug": self.provider.slug})
        response = self.client.get(list_url, **self.auth_headers)
        self.assertEqual(response.json()["totalResults"], 0)

        detail_url = reverse(
            "api:provider_scim:group-detail", kwargs={"provider_slug": self.provider.slug, "pk": other_group.id}
        )
        self.assertEqual(self.client.get(detail_url, **self.auth_headers).status_code, status.HTTP_404_NOT_FOUND)

    def test_group_post_and_put_reject_malformed_members_without_mutation(self):
        list_url = reverse("api:provider_scim:group-list", kwargs={"provider_slug": self.provider.slug})
        group = UserGroup.objects.create(tenant=self.provider, name="Stable group")
        membership = Membership.objects.get(tenant=self.provider, user=self.admin_user)
        GroupMembership.objects.create(
            user_group=group,
            membership=membership,
            source=GroupMembership.SOURCE_SCIM,
            external_id=str(self.admin_user.pk),
            added_by=self.admin_user,
        )
        detail_url = reverse(
            "api:provider_scim:group-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": group.pk},
        )
        malformed_members = (
            [{"value": True}],
            [{"value": 1.5}],
            [{}],
            [{"value": "0"}],
            "not-a-list",
            None,
        )
        for index, members in enumerate(malformed_members):
            with self.subTest(members=members):
                payload = {"displayName": f"Malformed {index}", "members": members}
                post_response = self.client.post(
                    list_url,
                    data=payload,
                    content_type="application/json",
                    **self.auth_headers,
                )
                self.assertEqual(post_response.status_code, status.HTTP_400_BAD_REQUEST)
                put_response = self.client.put(
                    detail_url,
                    data={"displayName": "Should not apply", "members": members},
                    content_type="application/json",
                    **self.auth_headers,
                )
                self.assertEqual(put_response.status_code, status.HTTP_400_BAD_REQUEST)

        group.refresh_from_db()
        self.assertEqual(group.name, "Stable group")
        self.assertEqual(
            GroupMembership.objects.filter(user_group=group).values_list("membership__user_id", flat=True).count(),
            1,
        )

    def test_provider_resource_locations_and_refs_use_provider_mount(self):
        group = UserGroup.objects.create(tenant=self.provider, name="Provider links")
        membership = Membership.objects.get(tenant=self.provider, user=self.admin_user)
        GroupMembership.objects.create(
            user_group=group,
            membership=membership,
            source=GroupMembership.SOURCE_SCIM,
            external_id=str(self.admin_user.pk),
            added_by=self.admin_user,
        )
        user_url = reverse(
            "api:provider_scim:user-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": self.admin_user.pk},
        )
        group_url = reverse(
            "api:provider_scim:group-detail",
            kwargs={"provider_slug": self.provider.slug, "pk": group.pk},
        )
        user_data = self.client.get(user_url, **self.auth_headers).json()
        group_data = self.client.get(group_url, **self.auth_headers).json()
        user_list_url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        self.assertTrue(user_list_url.endswith("/Users"))
        base = user_list_url[: -len("/Users")]
        self.assertEqual(user_data["meta"]["location"], f"{base}/Users/{self.admin_user.scim_id}")
        self.assertEqual(user_data["groups"][0]["$ref"], f"{base}/Groups/{group.scim_id}")
        self.assertEqual(group_data["meta"]["location"], f"{base}/Groups/{group.scim_id}")
        self.assertEqual(group_data["members"][0]["$ref"], f"{base}/Users/{self.admin_user.scim_id}")

    def test_extracted_services_fail_closed_and_update_global_state(self):
        foreign_group = UserGroup.objects.create(tenant=self.other_provider, name="Foreign")
        with self.assertRaisesRegex(SCIMPatchError, "provider tenant"):
            sync_provider_group_members(self.provider, foreign_group, (), actor=self.admin_user)

        orphan = User.objects.create_user(username="orphan-service-user")
        with self.assertRaisesRegex(SCIMPatchError, "not staff"):
            apply_provider_user_patch(orphan, self.provider, UserPatch(), actor=self.admin_user)

        local_user = User.objects.create_user(username="local-service-user")
        Membership.objects.create(user=local_user, tenant=self.provider, is_active=True)
        apply_provider_user_patch(local_user, self.provider, UserPatch(active=False), actor=self.admin_user)
        local_user.refresh_from_db()
        self.assertFalse(local_user.is_active)

        create_provider_group(self.provider, "Service duplicate", (), actor=self.admin_user)
        with self.assertRaisesRegex(SCIMPatchError, "Group already exists"):
            create_provider_group(self.provider, "Service duplicate", (), actor=self.admin_user)

    def test_group_service_branch_and_raw_integrity_error_paths(self):
        group = UserGroup.objects.create(tenant=self.provider, name="Branch coverage")
        result = _apply_group_member_operations(
            self.provider,
            group,
            [
                GroupMemberOperation(op="remove", filter_member_id=101),
                GroupMemberOperation(op="remove", member_ids=(102,)),
                GroupMemberOperation(op="remove", clear_members=True),
                GroupMemberOperation(op="replace", member_ids=(103,)),
            ],
        )
        self.assertEqual(result, set())

        UserGroup.objects.create(tenant=self.provider, name="Integrity branch")
        conflicting_group = UserGroup(tenant=self.provider, name="Integrity branch")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _save_group_or_raise(conflicting_group, translate_integrity=False)

        deleted_group = UserGroup.objects.create(tenant=self.provider, name="Deleted race")
        deleted_group.delete()
        with self.assertRaisesRegex(SCIMPatchError, "group was deleted"):
            apply_provider_group_patch(self.provider, deleted_group, GroupPatch(), actor=self.admin_user)
