"""Entra ID lifecycle drill for the frozen SCIM contract (epic #571, WP5).

This is the qualification drill recorded for the Stable promotion: a realistic
Entra ID shaped request/response sequence against both mounts, covering create,
rename, deactivate, reactivate, group sync including member removal, full
de-provision, and re-provision, plus `ServiceProviderConfig` and the served
discovery route set.

Set ``SCIM_DRILL_TRANSCRIPT_DIR`` to also write a markdown transcript of every
drill request (the artifact attached to the issue); the assertions always run,
and the transcript captures only method, path, status, and the (non-secret)
error detail.
"""

import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.tests.mixins import grant
from organization.models import AssetHolder, Membership, Role, Tenant
from users.models import GroupMembership, Token, UserGroup

User = get_user_model()

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

ENTRA_USER = {
    "schemas": [USER_SCHEMA],
    "externalId": "6c4d3f7a-1b9e-4c2f-9e51-2c9ce8104bd7",
    "userName": "jamie.fox@contoso.onmicrosoft.com",
    "name": {"givenName": "Jamie", "familyName": "Fox"},
    "displayName": "Jamie Fox",
    "emails": [{"value": "jamie.fox@contoso.onmicrosoft.com", "type": "work", "primary": True}],
    "active": True,
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {
        "employeeNumber": "E-1042",
        "department": "Operations",
    },
}


def entra_patch(operations):
    return {"schemas": [PATCH_SCHEMA], "Operations": operations}


def rename_patch(user_name, family_name):
    """The update Entra sends when a user is renamed in the directory."""
    return entra_patch(
        [
            {"op": "replace", "path": "displayName", "value": f"{user_name.split('@')[0]} {family_name}"},
            {"op": "replace", "path": "userName", "value": user_name},
            {"op": "replace", "path": "name.givenName", "value": user_name.split(".")[0].title()},
            {"op": "replace", "path": "name.familyName", "value": family_name},
        ]
    )


def active_patch(active):
    return entra_patch([{"op": "replace", "path": "active", "value": active}])


class SCIMEntraLifecycleDrillTests(TestCase):
    """The WP5 drill: Entra-shaped lifecycle against the frozen contract (both mounts)."""

    def setUp(self):
        self.provider = Tenant.objects.create(name="Drill MSP", slug="drill-msp", is_provider=True)
        self.tenant = Tenant.objects.create(name="Drill Tenant", slug="drill-tenant")

        provider_role = Role.objects.create(
            tenant=self.provider,
            name="Drill provisioner",
            permissions=[
                "organization.change_membership",
                "users.view_usergroup",
                "users.add_usergroup",
                "users.change_usergroup",
            ],
        )
        tenant_role = Role.objects.create(
            tenant=self.tenant,
            name="Drill provisioner",
            permissions=["organization.change_membership", "users.view_usergroup"],
        )
        self.provider_actor = User.objects.create_user(username="drill-provider-actor")
        self.tenant_actor = User.objects.create_user(username="drill-tenant-actor")
        grant(self.provider_actor, self.provider, provider_role)
        grant(self.tenant_actor, self.tenant, tenant_role)

        expires = timezone.now() + timezone.timedelta(days=1)
        self.provider_token = Token.objects.create(user=self.provider_actor, tenant=self.provider, expires=expires)
        self.tenant_token = Token.objects.create(user=self.tenant_actor, tenant=self.tenant, expires=expires)

        self.user_list_url = reverse("api:provider_scim:user-list", kwargs={"provider_slug": self.provider.slug})
        self.tenant_user_list_url = reverse("api:scim:user-list", kwargs={"tenant_slug": self.tenant.slug})

    # -- helpers ---------------------------------------------------------------------------

    def _headers(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token.key}"}

    def _record(self, rows, step, method, url, response, note=""):
        if not note and response.status_code >= 400:
            try:
                note = str(response.json().get("detail", ""))
            except Exception:  # pragma: no cover - non-JSON error bodies are not expected
                note = ""
        rows.append({"step": step, "request": f"{method} {url}", "status": response.status_code, "note": note})

    def _write_transcript(self, mount, rows):
        target_dir = os.environ.get("SCIM_DRILL_TRANSCRIPT_DIR")
        if not target_dir:
            return None
        path = Path(target_dir) / f"scim-entra-drill-{mount}-mount.md"
        lines = [
            f"# SCIM Entra ID lifecycle drill transcript ({mount} mount)",
            "",
            "| # | Step | Request | Status | Detail |",
            "|---|------|---------|--------|--------|",
        ]
        for index, row in enumerate(rows, start=1):
            lines.append(f"| {index} | {row['step']} | {row['request']} | {row['status']} | {row['note']} |")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _provision(self, rows, step, base_collection_url, headers):
        response = self.client.post(base_collection_url, data=ENTRA_USER, content_type="application/json", **headers)
        self._record(rows, step, "POST", base_collection_url, response)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response

    # -- drills ----------------------------------------------------------------------------

    def test_entra_provider_mount_lifecycle_drill(self):
        """Provider mount: create, rename, deactivate, reactivate, group sync, deprovision,
        reprovision, ServiceProviderConfig, and the served discovery route set."""
        rows = []
        headers = self._headers(self.provider_token)

        # Discovery: the mount serves ServiceProviderConfig (and nothing else beyond
        # the published routes).
        config_url = reverse("api:provider_scim:service-provider-config", kwargs={"provider_slug": self.provider.slug})
        config = self.client.get(config_url, **headers)
        self._record(rows, "ServiceProviderConfig read", "GET", config_url, config)
        self.assertEqual(config.status_code, status.HTTP_200_OK)
        self.assertTrue(config.json()["patch"]["supported"])
        self.assertFalse(config.json()["bulk"]["supported"])

        # Provision.
        created = self._provision(rows, "Provision user", self.user_list_url, headers)
        scim_id = created.json()["id"]
        self.assertEqual(created.json()["userName"], ENTRA_USER["userName"])
        self.assertTrue(created.json()["active"])
        user = User.objects.get(scim_id=scim_id)
        detail_url = reverse(
            "api:provider_scim:user-detail", kwargs={"provider_slug": self.provider.slug, "pk": scim_id}
        )

        # Rename (Entra sends displayName, userName, and name parts together).
        renamed = self.client.patch(
            detail_url,
            data=rename_patch("jamie.fox-smith@contoso.onmicrosoft.com", "Fox-Smith"),
            content_type="application/json",
            **headers,
        )
        self._record(rows, "Rename user", "PATCH", detail_url, renamed)
        self.assertEqual(renamed.status_code, status.HTTP_200_OK)
        self.assertEqual(renamed.json()["userName"], "jamie.fox-smith@contoso.onmicrosoft.com")
        user.refresh_from_db()
        self.assertEqual(user.last_name, "Fox-Smith")

        # Deactivate, inspect while inactive, reactivate.
        deactivated = self.client.patch(
            detail_url, data=active_patch(False), content_type="application/json", **headers
        )
        self._record(rows, "Deactivate user", "PATCH", detail_url, deactivated)
        self.assertEqual(deactivated.status_code, status.HTTP_200_OK)
        self.assertFalse(deactivated.json()["active"])
        self.assertFalse(User.objects.get(pk=user.pk).is_active)

        while_inactive = self.client.get(detail_url, **headers)
        self._record(rows, "Read while inactive", "GET", detail_url, while_inactive)
        self.assertEqual(while_inactive.status_code, status.HTTP_200_OK)
        self.assertFalse(while_inactive.json()["active"])

        reactivated = self.client.patch(detail_url, data=active_patch(True), content_type="application/json", **headers)
        self._record(rows, "Reactivate user", "PATCH", detail_url, reactivated)
        self.assertEqual(reactivated.status_code, status.HTTP_200_OK)
        self.assertTrue(reactivated.json()["active"])
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

        # Group sync: create a group, add the member, replay the add, remove the member.
        group_url = reverse("api:provider_scim:group-list", kwargs={"provider_slug": self.provider.slug})
        group_created = self.client.post(
            group_url,
            data={
                "schemas": [GROUP_SCHEMA],
                "displayName": "Entra: Operations",
                "externalId": "53a8d20b-90f2-4fd4-b0ce-3b0d90ab3f21",
            },
            content_type="application/json",
            **headers,
        )
        self._record(rows, "Create group", "POST", group_url, group_created)
        self.assertEqual(group_created.status_code, status.HTTP_201_CREATED)
        group_id = group_created.json()["id"]
        group = UserGroup.objects.get(scim_id=group_id)
        group_detail_url = reverse(
            "api:provider_scim:group-detail", kwargs={"provider_slug": self.provider.slug, "pk": group_id}
        )
        membership = Membership.objects.get(user=user, tenant=self.provider)

        add_member = entra_patch([{"op": "add", "path": "members", "value": [{"value": scim_id}]}])
        added = self.client.patch(group_detail_url, data=add_member, content_type="application/json", **headers)
        self._record(rows, "Add group member", "PATCH", group_detail_url, added)
        self.assertEqual(added.status_code, status.HTTP_200_OK)
        self.assertEqual(GroupMembership.objects.filter(user_group=group, membership=membership).count(), 1)

        replayed = self.client.patch(group_detail_url, data=add_member, content_type="application/json", **headers)
        self._record(rows, "Replay member add", "PATCH", group_detail_url, replayed)
        self.assertEqual(replayed.status_code, status.HTTP_200_OK)
        self.assertEqual(GroupMembership.objects.filter(user_group=group, membership=membership).count(), 1)

        remove_member = entra_patch([{"op": "remove", "path": f"members[value eq {scim_id!r}]"}])
        removed = self.client.patch(group_detail_url, data=remove_member, content_type="application/json", **headers)
        self._record(rows, "Remove group member", "PATCH", group_detail_url, removed)
        self.assertEqual(removed.status_code, status.HTTP_200_OK)
        self.assertFalse(GroupMembership.objects.filter(user_group=group, membership=membership).exists())

        # Full de-provision (Entra deactivates, then deletes).
        final_deactivate = self.client.patch(
            detail_url, data=active_patch(False), content_type="application/json", **headers
        )
        self._record(rows, "Deactivate before delete", "PATCH", detail_url, final_deactivate)
        self.assertEqual(final_deactivate.status_code, status.HTTP_200_OK)
        deleted = self.client.delete(detail_url, **headers)
        self._record(rows, "Deprovision user", "DELETE", detail_url, deleted)
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(User.objects.get(pk=user.pk).is_active)
        missing = self.client.get(detail_url, **headers)
        self._record(rows, "Read after deprovision", "GET", detail_url, missing)
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

        # Re-provision: the IdP re-creates the same identity (with its current
        # directory values, so the rename above is part of the payload); login
        # must return without a manual account edit.
        reprovision_payload = {
            **ENTRA_USER,
            "userName": "jamie.fox-smith@contoso.onmicrosoft.com",
            "emails": [{"value": "jamie.fox-smith@contoso.onmicrosoft.com", "type": "work", "primary": True}],
        }
        reprovisioned = self.client.post(
            self.user_list_url, data=reprovision_payload, content_type="application/json", **headers
        )
        self._record(rows, "Reprovision user", "POST", self.user_list_url, reprovisioned)
        self.assertIn(reprovisioned.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED))
        self.assertEqual(reprovisioned.json()["id"], scim_id)
        membership = Membership.objects.get(user=user, tenant=self.provider)
        self.assertTrue(membership.is_active)
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

        self._write_transcript("provider", rows)

    def test_entra_tenant_mount_lifecycle_drill(self):
        """Tenant mount: the same Entra lifecycle with the tenant write scope; Groups are
        read-only, so the delete/readd cycle happens on Users only."""
        rows = []
        headers = self._headers(self.tenant_token)

        config_url = reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug})
        config = self.client.get(config_url, **headers)
        self._record(rows, "ServiceProviderConfig read", "GET", config_url, config)
        self.assertEqual(config.status_code, status.HTTP_200_OK)

        created = self._provision(rows, "Provision user", self.tenant_user_list_url, headers)
        scim_id = created.json()["id"]
        user = User.objects.get(scim_id=scim_id)
        detail_url = reverse("api:scim:user-detail", kwargs={"tenant_slug": self.tenant.slug, "pk": scim_id})

        renamed = self.client.patch(
            detail_url,
            data=rename_patch("jamie.fox-smith@contoso.onmicrosoft.com", "Fox-Smith"),
            content_type="application/json",
            **headers,
        )
        self._record(rows, "Rename user", "PATCH", detail_url, renamed)
        self.assertEqual(renamed.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.username, "jamie.fox-smith@contoso.onmicrosoft.com")
        deactivated = self.client.patch(
            detail_url, data=active_patch(False), content_type="application/json", **headers
        )
        self._record(rows, "Deactivate user", "PATCH", detail_url, deactivated)
        self.assertEqual(deactivated.status_code, status.HTTP_200_OK)
        self.assertFalse(User.objects.get(pk=user.pk).is_active)

        while_inactive = self.client.get(detail_url, **headers)
        self._record(rows, "Read while inactive", "GET", detail_url, while_inactive)
        self.assertEqual(while_inactive.status_code, status.HTTP_200_OK)

        reactivated = self.client.patch(detail_url, data=active_patch(True), content_type="application/json", **headers)
        self._record(rows, "Reactivate user", "PATCH", detail_url, reactivated)
        self.assertEqual(reactivated.status_code, status.HTTP_200_OK)
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

        # Tenant Groups are read-only for IdPs: reads work, writes stay forbidden.
        group_url = reverse("api:scim:group-list", kwargs={"tenant_slug": self.tenant.slug})
        groups = self.client.get(group_url, **headers)
        self._record(rows, "List groups (read-only)", "GET", group_url, groups)
        self.assertEqual(groups.status_code, status.HTTP_200_OK)
        forbidden = self.client.post(
            group_url,
            data={"schemas": [GROUP_SCHEMA], "displayName": "Entra: Should Fail"},
            content_type="application/json",
            **headers,
        )
        self._record(rows, "Group write rejected", "POST", group_url, forbidden)
        self.assertEqual(forbidden.status_code, status.HTTP_403_FORBIDDEN)

        deleted = self.client.delete(detail_url, **headers)
        self._record(rows, "Deprovision user", "DELETE", detail_url, deleted)
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Membership.objects.filter(user=user, tenant=self.tenant).exists())
        self.assertFalse(User.objects.get(pk=user.pk).is_active)
        holder = AssetHolder.objects.get(tenant=self.tenant, upn="jamie.fox-smith@contoso.onmicrosoft.com")
        self.assertIsNone(holder.user_id)

        reprovision_payload = {
            **ENTRA_USER,
            "userName": "jamie.fox-smith@contoso.onmicrosoft.com",
            "emails": [{"value": "jamie.fox-smith@contoso.onmicrosoft.com", "type": "work", "primary": True}],
        }
        reprovisioned = self.client.post(
            self.tenant_user_list_url, data=reprovision_payload, content_type="application/json", **headers
        )
        self._record(rows, "Reprovision user", "POST", self.tenant_user_list_url, reprovisioned)
        self.assertIn(reprovisioned.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED))
        self.assertEqual(reprovisioned.json()["id"], scim_id)
        self.assertTrue(Membership.objects.get(user=user, tenant=self.tenant).is_active)
        self.assertTrue(User.objects.get(pk=user.pk).is_active)

        self._write_transcript("tenant", rows)

    def test_discovery_contract_matches_both_mounts(self):
        """The advertised matrix is identical on both mounts and equals the frozen subset."""
        tenant_config = self.client.get(
            reverse("api:scim:service-provider-config", kwargs={"tenant_slug": self.tenant.slug}),
            **self._headers(self.tenant_token),
        ).json()
        provider_config = self.client.get(
            reverse("api:provider_scim:service-provider-config", kwargs={"provider_slug": self.provider.slug}),
            **self._headers(self.provider_token),
        ).json()

        for config in (tenant_config, provider_config):
            self.assertTrue(config["patch"]["supported"])
            self.assertTrue(config["filter"]["supported"])
            self.assertEqual(config["filter"]["maxResults"], 200)
            self.assertFalse(config["bulk"]["supported"])
            self.assertFalse(config["changePassword"]["supported"])
            self.assertFalse(config["sort"]["supported"])
            self.assertFalse(config["etag"]["supported"])
            self.assertEqual([scheme["type"] for scheme in config["authenticationSchemes"]], ["oauthbearertoken"])

        for key in ("patch", "bulk", "filter", "changePassword", "sort", "etag", "authenticationSchemes"):
            self.assertEqual(tenant_config[key], provider_config[key], f"both mounts must advertise {key} identically")
