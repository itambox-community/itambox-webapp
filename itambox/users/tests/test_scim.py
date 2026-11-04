import base64

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.tests.mixins import grant
from organization.models import AssetHolder, Membership, Role, Tenant
from users.models import GroupMembership, Token, UserGroup

User = get_user_model()


class SCIMProvisioningTests(TestCase):
    def setUp(self):
        # Create Tenants
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")
        self.other_tenant = Tenant.objects.create(name="Other Corp", slug="other")

        # Create Users
        self.admin_user = User.objects.create_user(username="admin", email="admin@acme.com", password="adminpassword")
        self.inactive_user = User.objects.create_user(
            username="inactive", email="inactive@acme.com", password="password123", is_active=False
        )
        self.no_membership_user = User.objects.create_user(
            username="nomember", email="nomember@acme.com", password="password123"
        )

        # Create Tenant Roles
        self.role_member = Role.objects.create(
            tenant=self.tenant, name="Member", permissions=["assets.view_asset", "extras.view_dashboard"]
        )
        self.role_admin = Role.objects.create(
            tenant=self.tenant,
            name="Admin",
            permissions=[
                "assets.view_asset",
                "assets.add_asset",
                "extras.view_dashboard",
                # SCIM auth authorizes on real permissions, not the "Admin" name —
                # this role must actually grant membership-management to pass it.
                "organization.change_membership",
                # WP-20: tenant SCIM Group reads require the same permission as provider reads.
                "users.view_usergroup",
            ],
        )

        # Create Tenant Memberships — grant() creates the membership + role grant together.
        self.admin_membership = grant(
            self.admin_user,
            self.tenant,
            self.role_admin,
        ).membership

        # Setup tokens — tenant is explicit (not left to the model's current-tenant-context
        # fallback) so each token is unambiguously scoped to self.tenant, matching the URLs
        # these tests hit.
        self.valid_token = Token.objects.create(
            user=self.admin_user, tenant=self.tenant, expires=timezone.now() + timezone.timedelta(days=1)
        )
        self.expired_token = Token.objects.create(
            user=self.admin_user, tenant=self.tenant, expires=timezone.now() - timezone.timedelta(hours=1)
        )
        self.inactive_token = Token.objects.create(
            user=self.inactive_user, tenant=self.tenant, expires=timezone.now() + timezone.timedelta(days=1)
        )
        self.no_membership_token = Token.objects.create(
            user=self.no_membership_user, tenant=self.tenant, expires=timezone.now() + timezone.timedelta(days=1)
        )

        # Headers helpers
        self.auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {self.valid_token.key}"}

    def test_authentication_scenarios(self):
        url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug})

        # 1. No authentication
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 2. Valid token authentication
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 3. Invalid token key
        response = self.client.get(url, HTTP_AUTHORIZATION="Bearer invalidkey")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 4. Expired token key
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.expired_token.key}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 5. Inactive user token
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.inactive_token.key}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 6. User without tenant membership
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.no_membership_token.key}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 7. HTTP Basic auth is no longer accepted on SCIM endpoints (removed to
        #    avoid transmitting credentials on every request) — rejected even
        #    with otherwise-valid credentials. Bearer token is now required.
        basic_credentials = base64.b64encode(b"admin:adminpassword").decode("utf-8")
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Basic {basic_credentials}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # 8. Basic Authentication fallback failure
        basic_credentials_fail = base64.b64encode(b"admin:wrongpassword").decode("utf-8")
        response = self.client.get(url, HTTP_AUTHORIZATION=f"Basic {basic_credentials_fail}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_service_provider_config(self):
        url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug})
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig", data["schemas"])
        self.assertTrue(data["patch"]["supported"])

    def test_user_list_and_filtering(self):
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})

        # List should include admin_user
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["totalResults"], 1)
        self.assertEqual(data["Resources"][0]["userName"], self.admin_user.username)

        # Create another user in tenant to test filters.
        user2 = User.objects.create_user(username="user2", email="user2@acme.com")
        grant(user2, self.tenant, self.role_member)

        # Total count is 2
        response = self.client.get(url, **self.auth_headers)
        self.assertEqual(response.json()["totalResults"], 2)

        # Test eq filter
        response = self.client.get(f'{url}?filter=userName eq "user2"', **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["totalResults"], 1)
        self.assertEqual(data["Resources"][0]["userName"], "user2")

        # Test filters co
        response = self.client.get(f'{url}?filter=userName co "user"', **self.auth_headers)
        self.assertEqual(response.json()["totalResults"], 1)

    def test_user_creation_and_assetholder_linking(self):
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "newuser@example.com",
            "name": {"familyName": "Doe", "givenName": "John"},
            "emails": [{"value": "newuser@example.com", "primary": True}],
            "active": True,
        }

        # Success path
        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        res_data = response.json()
        self.assertEqual(res_data["userName"], "newuser@example.com")
        self.assertEqual(res_data["name"]["givenName"], "John")

        # Verify User and AssetHolder and Membership.
        # SCIM /Users provisioning creates the membership with NO role assigned —
        # roles are granted in-app, not at provisioning time.
        user = User.objects.get(username="newuser@example.com")
        self.assertTrue(user.is_active)

        membership = Membership.objects.get(user=user, tenant=self.tenant)
        self.assertFalse(membership.role_grants.exists())

        holder = AssetHolder.objects.get(user=user, tenant=self.tenant)
        self.assertEqual(holder.email, "newuser@example.com")
        self.assertEqual(holder.first_name, "John")
        self.assertEqual(holder.last_name, "Doe")

        # Conflict check in same tenant
        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_creation_linking_existing_assetholder(self):
        # Create unlinked AssetHolder
        unlinked_holder = AssetHolder.objects.create(
            first_name="Existing",
            last_name="Holder",
            upn="jane@acme.com",
            email="contact@acme.com",
            tenant=self.tenant,
        )

        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "jane@acme.com",
            "name": {"familyName": "Smith", "givenName": "Jane"},
            "emails": [{"value": "different-address@acme.com", "primary": True}],
            "active": True,
        }

        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = User.objects.get(username="jane@acme.com")
        unlinked_holder.refresh_from_db()
        self.assertEqual(unlinked_holder.user, user)
        self.assertEqual(unlinked_holder.upn, "jane@acme.com")
        self.assertEqual(unlinked_holder.first_name, "Existing")
        self.assertEqual(unlinked_holder.last_name, "Holder")
        self.assertEqual(unlinked_holder.email, "contact@acme.com")

    def test_scim_does_not_link_existing_global_user_by_username(self):
        existing = User.objects.create_user(username="global-existing", email="global-existing@example.com")
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": existing.username,
            "emails": [{"value": existing.email, "primary": True}],
            "active": True,
        }

        response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json()["scimType"], "uniqueness")
        self.assertFalse(Membership.objects.filter(user=existing, tenant=self.tenant).exists())

    def test_scim_does_not_link_existing_foreign_user_by_username_or_external_id(self):
        foreign_role = Role.objects.create(tenant=self.other_tenant, name="Foreign Member", permissions=[])
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        for external_id in (None, "attacker-chosen-external-id"):
            with self.subTest(external_id=external_id):
                existing = User.objects.create_user(username=f"foreign-existing-{external_id or 'none'}")
                grant(existing, self.other_tenant, foreign_role)
                payload = {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": existing.username,
                    "emails": [{"value": f"{existing.username}@example.com", "primary": True}],
                    "active": True,
                }
                if external_id:
                    payload["externalId"] = external_id

                response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)

                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertEqual(response.json()["scimType"], "uniqueness")
                self.assertFalse(Membership.objects.filter(user=existing, tenant=self.tenant).exists())
                self.assertTrue(Membership.objects.filter(user=existing, tenant=self.other_tenant).exists())

    def test_user_detail_put_patch_delete(self):
        # Create user with a role via the standard grant helper.
        user = User.objects.create_user(username="testuser", email="test@acme.com")
        grant(user, self.tenant, self.role_member)
        AssetHolder.objects.create(
            user=user,
            first_name="Test",
            last_name="User",
            upn="test@acme.com",
            email="test@acme.com",
            tenant=self.tenant,
        )

        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": user.id})

        # 1. GET Details
        response = self.client.get(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["userName"], "testuser")

        # 2. PUT Update
        put_payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "testuser_updated",
            "name": {"familyName": "UpdatedLastName", "givenName": "UpdatedFirstName"},
            "emails": [{"value": "updated@acme.com", "primary": True}],
            "active": False,
        }
        response = self.client.put(detail_url, data=put_payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user.refresh_from_db()
        self.assertEqual(user.username, "testuser_updated")
        self.assertEqual(user.email, "updated@acme.com")
        self.assertFalse(user.is_active)

        holder = AssetHolder.objects.get(user=user, tenant=self.tenant)
        self.assertEqual(holder.first_name, "Test")
        self.assertEqual(holder.last_name, "User")
        self.assertEqual(holder.upn, "test@acme.com")
        self.assertEqual(holder.email, "test@acme.com")

        # 3. PATCH Update
        patch_payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": True}],
        }
        response = self.client.patch(
            detail_url, data=patch_payload, content_type="application/json", **self.auth_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        # 4. DELETE details
        response = self.client.delete(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(User.objects.filter(id=user.id).exists())
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(Membership.objects.filter(user=user, tenant=self.tenant).exists())

    def test_group_endpoint_is_read_only(self):
        """SCIM /Groups is read-only and exposes only tenant-owned groups."""
        list_url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})

        # 1. GET list — this tenant owns no groups yet.
        response = self.client.get(list_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["totalResults"], 0)

        # 2. POST is rejected — groups cannot be created via tenant SCIM.
        post_payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Software Managers",
        }
        response = self.client.post(list_url, data=post_payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(UserGroup.objects.filter(name="Software Managers").exists())

        # 3. A group owned by THIS tenant is visible; another tenant's group is not.
        group = UserGroup.objects.create(tenant=self.tenant, name="Software Managers")
        GroupMembership.objects.create(
            user_group=group,
            membership=self.admin_membership,
            source=GroupMembership.SOURCE_SCIM,
            external_id=str(self.admin_user.id),
        )
        other_group = UserGroup.objects.create(
            tenant=self.other_tenant,
            name="Other Tenant Group",
        )
        other_membership = Membership.objects.create(
            user=self.admin_user,
            tenant=self.other_tenant,
        )
        GroupMembership.objects.create(
            user_group=other_group,
            membership=other_membership,
            source=GroupMembership.SOURCE_SCIM,
            external_id=str(self.admin_user.id),
        )
        response = self.client.get(list_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["totalResults"], 1)
        self.assertEqual(response.json()["Resources"][0]["displayName"], "Software Managers")

        user_detail_url = reverse(
            "api:scim:user-detail",
            kwargs={"tenant_slug": self.tenant.slug, "pk": self.admin_user.id},
        )
        user_response = self.client.get(user_detail_url, **self.auth_headers)
        self.assertEqual(user_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [entry["display"] for entry in user_response.json()["groups"]],
            ["Software Managers"],
        )

        # 4. GET detail works.
        detail_url = reverse("api:scim:group-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": group.id})
        response = self.client.get(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["displayName"], "Software Managers")
        self.assertEqual(response.json()["members"][0]["value"], str(self.admin_user.scim_id))

        # 5. PUT / PATCH / DELETE are all rejected; the group is unchanged.
        put_payload = dict(post_payload, displayName="Renamed")
        self.assertEqual(
            self.client.put(
                detail_url, data=put_payload, content_type="application/json", **self.auth_headers
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        patch_payload = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": []}
        self.assertEqual(
            self.client.patch(
                detail_url, data=patch_payload, content_type="application/json", **self.auth_headers
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.delete(detail_url, **self.auth_headers).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        group.refresh_from_db()
        self.assertEqual(group.name, "Software Managers")
        self.assertTrue(UserGroup.objects.filter(id=group.id).exists())

    def test_filter_parsing_bracketed_emails(self):
        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        user2 = User.objects.create_user(username="user2", email="user2@acme.com")
        grant(user2, self.tenant, self.role_member)

        response = self.client.get(
            f'{url}?filter=emails[type eq "work"].value eq "user2@acme.com"', **self.auth_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["totalResults"], 1)
        self.assertEqual(data["Resources"][0]["userName"], "user2")

    def test_auth_no_tenant_resolved(self):
        response = self.client.get("/api/tenants//scim/v2/Users", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_graceful_assetholder_creation_on_integrity_error(self):
        from unittest.mock import patch

        from django.db import IntegrityError

        url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "mockeduser@example.com",
            "name": {"familyName": "Mock", "givenName": "User"},
            "emails": [{"value": "mockeduser@example.com", "primary": True}],
            "active": True,
        }

        with patch(
            "organization.models.AssetHolder._base_manager.create",
            side_effect=IntegrityError("Mocked constraint violation"),
        ):
            response = self.client.post(url, data=payload, content_type="application/json", **self.auth_headers)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertTrue(User.objects.filter(username="mockeduser@example.com").exists())

    def test_scim_patch_does_not_mutate_shared_global_user(self):
        """WS1-3: a tenant-A SCIM token must NOT globally deactivate or rename a user who is
        also a member of tenant B (cross-tenant write on a shared principal)."""
        shared = User.objects.create_user(username="shared", email="shared@x.com", is_active=True)
        Membership.objects.create(user=shared, tenant=self.tenant)
        other_role = Role.objects.create(tenant=self.other_tenant, name="Member", permissions=[])
        grant(shared, self.other_tenant, other_role)

        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": shared.id})
        patch_payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "replace", "path": "active", "value": False},
                {"op": "replace", "path": "userName", "value": "hijacked"},
            ],
        }
        response = self.client.patch(
            detail_url, data=patch_payload, content_type="application/json", **self.auth_headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        shared.refresh_from_db()
        # Global identity/active are untouched — the user stays active & authenticatable to B.
        self.assertTrue(shared.is_active)
        self.assertEqual(shared.username, "shared")
        self.assertTrue(Membership.objects.filter(user=shared, tenant=self.other_tenant).exists())
        # active=false is now applied PER-TENANT: this tenant's membership is suspended,
        # the other tenant's stays active (no cross-tenant write, but a real local revoke).
        self.assertFalse(Membership.objects.get(user=shared, tenant=self.tenant).is_active)
        self.assertTrue(Membership.objects.get(user=shared, tenant=self.other_tenant).is_active)

    def test_scim_active_false_deprovisions_this_tenant_only(self):
        """active=false on a multi-tenant user suspends THIS tenant's membership (revoking
        access here) while leaving the global account and other tenants untouched; the SCIM
        response reflects the per-tenant state, and active=true restores access."""
        from core.auth import MembershipBackend

        backend = MembershipBackend()

        shared = User.objects.create_user(username="shared2", email="shared2@x.com", is_active=True)
        grant(shared, self.tenant, self.role_member)
        other_role = Role.objects.create(tenant=self.other_tenant, name="Member", permissions=["assets.view_asset"])
        grant(shared, self.other_tenant, other_role)

        # Baseline: the membership grants access in this tenant.
        self.assertTrue(backend.has_perm(User.objects.get(pk=shared.pk), "assets.view_asset", obj=self.tenant))

        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": shared.id})

        # Deprovision this tenant.
        resp = self.client.patch(
            detail_url,
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # SCIM response reports active=false FOR THIS TENANT (not the global flag).
        self.assertFalse(resp.json()["active"])

        shared.refresh_from_db()
        self.assertTrue(shared.is_active)  # global account stays enabled (other tenant active)
        self.assertFalse(Membership.objects.get(user=shared, tenant=self.tenant).is_active)
        self.assertTrue(Membership.objects.get(user=shared, tenant=self.other_tenant).is_active)
        # Access is revoked HERE but unaffected in the other tenant.
        self.assertFalse(backend.has_perm(User.objects.get(pk=shared.pk), "assets.view_asset", obj=self.tenant))
        self.assertTrue(backend.has_perm(User.objects.get(pk=shared.pk), "assets.view_asset", obj=self.other_tenant))

        # Reactivate this tenant.
        resp = self.client.patch(
            detail_url,
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": True}],
            },
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.json()["active"])
        self.assertTrue(Membership.objects.get(user=shared, tenant=self.tenant).is_active)
        self.assertTrue(backend.has_perm(User.objects.get(pk=shared.pk), "assets.view_asset", obj=self.tenant))

    def test_scim_put_updates_sole_tenant_user(self):
        """Control for WS1-3: a user whose ONLY membership is this tenant is still fully
        updatable (the guard must not over-block single-tenant users)."""
        solo = User.objects.create_user(username="solo", email="solo@acme.com", is_active=True)
        grant(solo, self.tenant, self.role_member)
        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": solo.id})
        put_payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "solo_renamed",
            "name": {"familyName": "S", "givenName": "Solo"},
            "emails": [{"value": "solo2@acme.com", "primary": True}],
            "active": False,
        }
        response = self.client.put(detail_url, data=put_payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        solo.refresh_from_db()
        self.assertEqual(solo.username, "solo_renamed")
        self.assertFalse(solo.is_active)

    def test_scim_sole_membership_lifecycle_deprovision_and_reprovision(self):
        """The frozen lifecycle for a solely-provisioned identity: deactivate suspends
        the membership AND the global account (login blocked), the resource stays
        addressable while inactive, reactivation restores both, full de-provision
        removes the membership, and a re-provision POST restores login without any
        manual is_active step."""
        from core.auth import MembershipBackend

        backend = MembershipBackend()
        create_url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "externalId": "entra-object-lifecycle-1",
            "userName": "lifecycle@acme.com",
            "name": {"givenName": "Life", "familyName": "Cycle"},
            "emails": [{"value": "lifecycle@acme.com", "type": "work"}],
            "active": True,
        }
        created = self.client.post(create_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        scim_id = created.json()["id"]
        user = User.objects.get(scim_id=scim_id)
        grant(user, self.tenant, self.role_member)

        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": scim_id})

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

        self.assertTrue(User.objects.get(pk=user.pk).is_active)

        # Deactivate: the sole membership is suspended, so the global account mirrors it.
        response = patch_active(False)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["active"])
        self.assertFalse(Membership.objects.get(user=user, tenant=self.tenant).is_active)
        self.assertFalse(User.objects.get(pk=user.pk).is_active)
        self.assertFalse(backend.has_perm(User.objects.get(pk=user.pk), "extras.view_dashboard", obj=self.tenant))

        # An inactive resource stays addressable: GET returns it with active=false.
        response = self.client.get(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["active"])

        # Reactivate: membership, login and access come back.
        response = patch_active(True)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Membership.objects.get(user=user, tenant=self.tenant).is_active)
        self.assertTrue(User.objects.get(pk=user.pk).is_active)
        self.assertTrue(backend.has_perm(User.objects.get(pk=user.pk), "extras.view_dashboard", obj=self.tenant))

        # Full de-provision: the membership is removed; the User row survives, deactivated.
        response = self.client.delete(detail_url, **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Membership.objects.filter(user=user, tenant=self.tenant).exists())
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(User.objects.get(pk=user.pk).is_active)
        self.assertEqual(self.client.get(detail_url, **self.auth_headers).status_code, status.HTTP_404_NOT_FOUND)

        # Re-provision: the POST correlation path restores the membership and login.
        # (SCIM provisioning is identity-only: it re-creates no in-app role grants,
        # but it must not leave the account locked behind a manual is_active flip.)
        again = self.client.post(create_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertIn(again.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED))
        membership = Membership.objects.get(user=user, tenant=self.tenant)
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.external_id, "entra-object-lifecycle-1")
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

    def test_tenant_patch_is_strict_and_shares_the_provider_subset(self):
        """The tenant mount accepts exactly the provider mount's PATCH subset:
        unsupported operations and paths are explicit 400s (previously silent
        no-ops), bracketed work-email paths now apply, and the unmanaged
        displayName/userType attributes stay explicit skips."""
        user = User.objects.create_user(username="strict", email="strict@acme.com")
        grant(user, self.tenant, self.role_member)
        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": user.scim_id})

        def patch(operations):
            return self.client.patch(
                detail_url,
                data={
                    "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                    "Operations": operations,
                },
                content_type="application/json",
                **self.auth_headers,
            )

        response = patch([{"op": "bogus", "path": "active", "value": True}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unsupported SCIM PATCH operation", response.json()["detail"])
        self.assertEqual(response.json()["status"], "400")
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        response = patch([{"op": "remove", "path": "active"}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unsupported SCIM PATCH path", response.json()["detail"])

        response = patch([{"op": "replace", "path": "userType", "value": "Employee"}])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = patch([{"op": "replace", "path": "displayName", "value": "Strict User"}])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = patch([{"op": "replace", "path": 'emails[type eq "work"].value', "value": "work@acme.com"}])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.email, "work@acme.com")

    def test_tenant_rename_onto_an_existing_username_is_a_uniqueness_conflict(self):
        """A SCIM rename fails closed when the target username is taken: 409 with
        scimType=uniqueness, and the losing user keeps its old identity."""
        first = User.objects.create_user(username="collide-one", email="one@acme.com")
        grant(first, self.tenant, self.role_member)
        second = User.objects.create_user(username="collide-two", email="two@acme.com")
        grant(second, self.tenant, self.role_member)

        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": second.scim_id})
        response = self.client.patch(
            detail_url,
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "userName", "value": "collide-one"}],
            },
            content_type="application/json",
            **self.auth_headers,
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json()["scimType"], "uniqueness")
        second.refresh_from_db()
        self.assertEqual(second.username, "collide-two")
        self.assertEqual(second.email, "two@acme.com")

    def test_scim_writes_require_a_write_enabled_token(self):
        """A read-only token can still read SCIM but must not provision, and no
        identity may be created through it."""
        read_only = Token.objects.create(
            user=self.admin_user,
            tenant=self.tenant,
            write_enabled=False,
            expires=timezone.now() + timezone.timedelta(days=1),
        )
        headers = {"HTTP_AUTHORIZATION": f"Bearer {read_only.key}"}
        list_url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})
        self.assertEqual(self.client.get(list_url, **headers).status_code, status.HTTP_200_OK)

        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "readonly-write@acme.com",
            "emails": [{"value": "readonly-write@acme.com", "type": "work"}],
        }
        response = self.client.post(list_url, data=payload, content_type="application/json", **headers)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(User.objects.filter(username="readonly-write@acme.com").exists())

    def test_scim_group_post_is_rejected(self):
        """Read-only tenant SCIM cannot create groups or provision foreign users."""
        foreign_role = Role.objects.create(tenant=self.other_tenant, name="Member", permissions=[])
        foreign_user = User.objects.create_user(username="foreignuser", email="foreign@other.com")
        grant(foreign_user, self.other_tenant, foreign_role)

        list_url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})
        payload = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "Injected Group",
            "members": [{"value": str(foreign_user.id)}],
        }
        response = self.client.post(list_url, data=payload, content_type="application/json", **self.auth_headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Nothing was created or provisioned.
        self.assertFalse(UserGroup.objects.filter(name="Injected Group").exists())
        self.assertFalse(Membership.objects.filter(user=foreign_user, tenant=self.tenant).exists())
        self.assertFalse(AssetHolder.objects.filter(user=foreign_user, tenant=self.tenant).exists())


class SCIMServiceProviderConfigAccuracyTests(SCIMProvisioningTests):
    """WP-18: ServiceProviderConfig must truthfully advertise only implemented capabilities."""

    def setUp(self):
        super().setUp()
        self.config_url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug})

    def test_bearer_is_advertised_and_httpbasic_is_not(self):
        """Only Bearer auth is implemented → only Bearer is advertised."""
        response = self.client.get(self.config_url, **self.auth_headers)
        self.assertEqual(response.status_code, 200)
        schemes = response.json()["authenticationSchemes"]
        types = {s["type"] for s in schemes}
        self.assertIn("oauthbearertoken", types, "Bearer must be advertised")
        self.assertNotIn("httpbasic", types, "HTTP Basic is not implemented and must not be advertised")

    def test_bearer_metadata_is_truthful(self):
        """Bearer entry carries accurate description and primary flag."""
        response = self.client.get(self.config_url, **self.auth_headers)
        bearer = next(s for s in response.json()["authenticationSchemes"] if s["type"] == "oauthbearertoken")
        self.assertTrue(bearer["primary"], "Bearer must be the primary scheme")
        self.assertEqual(bearer["name"], "OAuth Bearer Token")

    def test_user_lastmodified_is_not_creation_time(self):
        """After a SCIM PATCH, meta.lastModified must advance beyond created."""
        user = self.admin_user
        user_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": user.id})

        # GET before mutation — capture created and lastModified
        resp_before = self.client.get(user_url, **self.auth_headers)
        meta_before = resp_before.json()["meta"]
        # Note: current behaviour returns created==lastModified (the bug).
        # After the fix, PATCH should advance lastModified.

        # Mutate the user via SCIM PATCH
        patch_payload = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "name.givenName", "value": "Updated Name"}],
        }
        self.client.patch(user_url, data=patch_payload, content_type="application/json", **self.auth_headers)

        # GET after mutation
        resp_after = self.client.get(user_url, **self.auth_headers)
        meta_after = resp_after.json()["meta"]

        self.assertEqual(meta_after["created"], meta_before["created"], "created must never change")
        self.assertNotEqual(
            meta_after["lastModified"], meta_after["created"], "lastModified must differ from created after mutation"
        )

    def test_frozen_capability_matrix_is_advertised(self):
        """The advertised document is exactly the frozen subset: PATCH and filter are
        supported (filter maxResults 200); Bulk, ETag, sorting and password changes are
        not implemented and must stay advertised as unsupported. Exactly one
        authentication scheme (Bearer) is advertised."""
        response = self.client.get(self.config_url, **self.auth_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data["patch"]["supported"])
        self.assertTrue(data["filter"]["supported"])
        self.assertEqual(data["filter"]["maxResults"], 200)
        self.assertFalse(data["bulk"]["supported"])
        self.assertFalse(data["changePassword"]["supported"])
        self.assertFalse(data["sort"]["supported"])
        self.assertFalse(data["etag"]["supported"])
        self.assertEqual([scheme["type"] for scheme in data["authenticationSchemes"]], ["oauthbearertoken"])
