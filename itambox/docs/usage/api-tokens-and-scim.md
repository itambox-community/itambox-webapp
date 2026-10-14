# API Tokens, SCIM Provisioning & RBAC

This guide covers programmatic access to ITAMbox: API tokens for REST API
authentication, SCIM 2.0 for automated user provisioning, and the basics of
role-based access control (RBAC).

---

## API Tokens

API tokens authenticate REST API requests. Each token is owned by a user,
scoped to a tenant, and carries a set of capabilities (read/write, expiry,
IP restrictions) that determine what the bearer can do.

### Creating a Token

Tokens are created in the user profile page (**User Menu → Profile → API
Tokens**). You must have an active user account and at least one tenant
membership.

1. Navigate to your user profile and click **Add Token**.
2. Fill in the token attributes (see table below).
3. Click **Create**. The full plaintext token is displayed **once** —
   copy it immediately. After the page is refreshed, only the `key_preview`
   (first 8 characters) is shown.

| Attribute | Description |
|---|---|
| **Description** | Human-readable label (e.g. `CI/CD Pipeline`, `SCIM Provisioning`). |
| **Write Enabled** | When checked, the token authorises `POST`, `PUT`, `PATCH`, and `DELETE` requests. Uncheck for read-only access. |
| **Expires** | Optional expiry date. Tokens without an expiry never expire (not recommended for long-lived automation). |
| **Allowed IPs** | Optional CIDR list restricting which IP addresses can use this token (e.g. `["10.0.0.0/8", "192.168.1.5"]`). Leave blank to allow any source. |
| **Tenant** | The tenant this token is scoped to. API requests authenticated with this token operate within this tenant's data boundary. |

> [!IMPORTANT]
> The plaintext token secret is generated with `secrets.token_hex(20)` (40 hex
> characters). It is **never stored in plaintext** — only an HMAC-SHA256 digest
> and a short key preview are persisted. If you lose the plaintext, you must
> create a new token.

### Token Scope: Tenant vs Provider

A token is always scoped to one tenant:

- **Customer tenant token** — operates within that single tenant's data
  boundary. Used for tenant-local automation (asset imports, custom scripts,
  CI/CD pipelines).
- **Provider tenant token** — still bound to exactly the provider tenant. It
  can authenticate to that provider's provider-SCIM mount; it is not a
  multi-customer credential and its tenant binding is not widened by a
  managed-tenant RoleGrant. Customer tenant SCIM authentication requires a
  token bound to the exact tenant named in `/api/tenants/<slug>/scim/v2/`.
  Provider RoleGrant reach and token tenant binding are separate checks.

### Write-Enabled vs Read-Only

| Flag | Permitted Methods | Use Case |
|---|---|---|
| **Write Enabled** (checked) | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` | Full automation, SCIM provisioning, data imports |
| **Read-Only** (unchecked) | `GET`, `HEAD`, `OPTIONS` | Dashboards, monitoring, read-only integrations |

> [!WARNING]
> Even with a write-enabled token, the token owner must hold the corresponding
> RBAC permissions. For example, creating an asset via the API requires both a
> write-enabled token **and** the `assets.add_asset` permission in the token's
> tenant scope. The token does not bypass the permission system.

### Token Expiry

Set the `expires` field to automatically revoke the token at a future date.
Tokens with no expiry (`expires=None`) are valid indefinitely. Best practice:

- **Service accounts** — set a reasonable expiry (90–365 days) and rotate
  before expiration.
- **Temporary access** — always set an expiry.
- **CI/CD pipelines** — use shorter expiries (30–90 days) with automated
  rotation in your pipeline.

### Key Hashing and `ITAMBOX_API_TOKEN_PEPPERS`

API tokens are stored as HMAC-SHA256 digests, keyed by **server-side peppers**.
This means that even if the database is compromised, the token digests cannot
be reversed without the pepper secrets.

Configure peppers in `.env`:

```bash
ITAMBOX_API_TOKEN_PEPPERS='{"1":"a-50-plus-character-random-secret-here-xxxxxxxxxxxxxxxx","2":"another-random-secret-for-rotation-yyyyyyyyyyyyyyyyyy"}'
```

| Property | Description |
|---|---|
| **Format** | JSON object: `{"<int-id>": "<secret-string>"}` |
| **Highest ID** | Peppers new tokens. When you add a new pepper with a higher ID, newly created tokens use it. |
| **All IDs** | Decrypt existing tokens. Never remove old pepper IDs while tokens hashed with them still exist — doing so makes those tokens unusable. |
| **Secret length** | At least 50 characters, random. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. |
| **Fallback** | If unset/blank, falls back to `SECRET_KEY` as a single pepper (acceptable for development; production starts with a loud warning). An explicitly supplied **non-blank** value that is not a valid mapping **aborts production startup** — malformed configuration never silently falls back. Tokens hashed under the fallback stop validating once a dedicated mapping is configured; re-issue them after the cutover. |

### Token Rotation Best Practices

1. **Create the replacement token first** — generate a new token while the
   old one is still valid.
2. **Update all consumers** — scripts, CI/CD configs, SCIM provisioning
   configs — to use the new token.
3. **Verify** — confirm all integrations work with the new token.
4. **Delete the old token** — removes it from the database. Revoked tokens
   cannot be re-activated.
5. **Rotate peppers periodically** — append a new pepper ID to
   `ITAMBOX_API_TOKEN_PEPPERS` (new tokens use it automatically). Remove old
   peppers only after migrating or revoking all tokens hashed under them.

> [!IMPORTANT]
> **Migrating from the fallback to a dedicated mapping is not seamless.**
> Tokens created while `ITAMBOX_API_TOKEN_PEPPERS` was unset/blank are hashed
> under the `SECRET_KEY`-derived fallback pepper. The moment a dedicated mapping
> is configured, those tokens stop validating (validation only tries configured
> peppers). There is no automatic re-hash — plaintext tokens are not stored.
> Plan the migration around a deliberate cutover:
>
> 1. **Configure the dedicated mapping first** (in the same change that
>    rotates a short legacy `SECRET_KEY`, if applicable — see the installation
>    upgrade note). From this moment, fallback-hashed tokens are invalid;
>    accept that.
> 2. **After** the cutover, create replacement tokens — they are hashed under
>    the dedicated mapping and survive it.
> 3. Update consumers and delete the old-scheme tokens.
>
> Rotation *within* an existing dedicated mapping (adding ID 2 while ID 1
> remains) keeps existing tokens valid.
>
> **Short legacy pepper values block startup and must be removed first.** The
> documented minimum secret length of 50 characters is enforced at production
> startup, so a mapping containing a short value (e.g. a 43-character
> `token_urlsafe(32)` secret) refuses to boot — replacement tokens cannot be
> created in that state. Replace the short pepper (accepting the invalidation
> of every token hashed under it, since the digest changes), boot, then issue
> replacements as part of the same planned re-issuance.

### Using a Token

Include the token in the `Authorization` header of API requests:

```bash
curl -H "Authorization: Token abc123def456..." \
     https://itam.example.com/api/assets/assets/
```

The token's tenant scope is applied automatically — you do not need to pass a
tenant header. IP restrictions are checked on every request; requests from
non-allowed IPs receive HTTP 403.

### Managing Tokens via the REST API

Tokens are also a first-class REST resource at `/api/users/tokens/`, so
automation can create, inspect, and revoke its own tokens without the web UI.

**One-time plaintext key.** `POST /api/users/tokens/` returns `HTTP 201` with
the full plaintext `key` in the response body — this is the *only* time the
plaintext is ever returned. Capture it immediately:

```bash
curl -X POST https://itam.example.com/api/users/tokens/ \
     -H "Authorization: Token <existing-token>" \
     -H "Content-Type: application/json" \
     -d '{"description": "CI/CD Pipeline"}'
```

```json
{
  "id": 42,
  "key": "b7e1...<40 hex characters>...",
  "key_preview": "b7e1a2c9",
  "description": "CI/CD Pipeline",
  "...": "..."
}
```

Every subsequent read of that token — `GET` on the detail or list endpoint,
including immediately after creation — returns `"key": null`. Only
`key_preview` (the first 8 characters, non-secret) is available afterward, for
identifying which token is which in a list. If the plaintext is lost, revoke
the token and create a replacement; it cannot be recovered or redisplayed.

**ETags and concurrency.** Token detail responses carry an `ETag` response
header (`W/"<updated_at ISO timestamp>"`). Mutating a token (`PATCH`, `PUT`,
`DELETE`) requires an `If-Match` header carrying that ETag:

- Missing `If-Match` → `HTTP 428 Precondition Required`.
- Stale `If-Match` (the token changed since you last read it) →
  `HTTP 412 Precondition Failed`.
- Current `If-Match` → the mutation proceeds normally (`204` for `DELETE`,
  `200` with a fresh `ETag` for `PATCH`/`PUT`).

```bash
# 1. Read the current ETag
curl -H "Authorization: Token <existing-token>" \
     https://itam.example.com/api/users/tokens/42/ -D -

# 2. Delete using that ETag
curl -X DELETE -H "Authorization: Token <existing-token>" \
     -H 'If-Match: W/"2026-08-15T18:50:00.123456+00:00"' \
     https://itam.example.com/api/users/tokens/42/
```

A deleted token is revoked immediately and cannot be reactivated — create a
new token instead.

**Tenant context required.** `POST /api/users/tokens/` fails with
`HTTP 403` when no single tenant anchors the request (for example, a
tenant-group scope). A token is always tenant-bound, so the API never
silently assigns a tenant — select a tenant before creating a token.

**Ownership.** A token belongs to the authenticated user. Non-superusers are
pinned to their own account — a `user_id` naming another user is ignored on
create and update. A superuser may re-assign a token by `PATCH`ing a new
`user_id` (with a current `If-Match`); the transfer is committed and returns
`HTTP 200` with the transferred token. The token then belongs to the new
owner and is no longer visible in the previous owner's queries.

---

## SCIM 2.0 Provisioning

!!! info "Status: Stable"
    The SCIM subset documented here and in the [integration
    guide](../integration/scim.md) is the frozen Stable contract. Nothing
    provisions automatically: provisioning starts only when an administrator
    deliberately mints a scope-bound, write-enabled token and an identity
    provider calls the mounts.

ITAMbox implements [SCIM 2.0](https://datatracker.ietf.org/doc/html/rfc7644)
Users and Groups resources for automated identity lifecycle management. SCIM
allows identity providers like Microsoft Entra ID, Okta, or OneLogin to
provision, update, and deprovision user accounts and group memberships
automatically.

### Overview

SCIM provisioning in ITAMbox operates at two scopes:

| Scope | Base URL | Purpose |
|---|---|---|
| **Tenant** | `/api/tenants/<tenant_slug>/scim/v2/` | Provision users into a specific tenant. Read tenant-owned groups (read-only). |
| **Provider** | `/api/providers/<provider_slug>/scim/v2/` | Provision provider-staff users and manage provider-owned groups. |

Both scopes serve exactly these routes: `ServiceProviderConfig`, `Users`,
`Users/<id>`, `Groups`, and `Groups/<id>`. The `ResourceTypes` and `Schemas`
discovery endpoints are not served.

### Authentication

SCIM endpoints use **HTTP Bearer token** authentication. Create a dedicated
API token for a least-privilege service account — do not reuse personal tokens.

For a non-superuser service account, ensure:

1. The token is scoped to the target tenant or provider tenant.
2. The token owner has `organization.change_membership` permission in that
   scope.
3. The token is **write-enabled** for `POST`, `PUT`, `PATCH`, or `DELETE`
   operations.
4. For provider Group operations, the owner also needs the corresponding
   `users.*_usergroup` permissions.

### Supported Operations

| Scope & Resource | Create | Read | Update | Delete |
|---|---|---|---|---|
| Tenant `Users` | Yes | Yes | Yes | Yes (membership removal; user deactivates when no memberships remain) |
| Tenant `Groups` | No | Yes | No | No |
| Provider `Users` | Yes | Yes (also while inactive) | Yes (also while inactive) | Yes (membership removal; user deactivates when no memberships remain) |
| Provider `Groups` | Yes | Yes | Yes | Yes |

> [!IMPORTANT]
> Tenant Group write requests return `403` — authorization changes for tenant
> users remain explicit in-app operations. Provider Groups require the
> corresponding `users` Group permissions and may include only active staff
> of that provider tenant.

Both scopes share one strict request parser: the same document shapes are
accepted, and the same shapes are rejected with SCIM error envelopes.
Unmanaged attributes (such as `displayName`) are explicit no-ops in `PATCH`,
and unsupported operations or paths answer `400` errors instead of silently
doing nothing. The [integration guide](../integration/scim.md) publishes the
exact operation and path matrix.

### Connecting Microsoft Entra ID

1. In Entra ID, create a **non-gallery Enterprise Application**.
2. Under **Provisioning**, select **Automatic**.
3. Set the **Tenant URL** to the appropriate base URL:
   - `/api/tenants/<tenant_slug>/scim/v2/` for tenant user provisioning
     and read-only group discovery.
   - `/api/providers/<provider_slug>/scim/v2/` when the identity
     provider needs to create or update provider-owned Groups.
4. Set **Secret Token** to the dedicated ITAMbox API token (as a Bearer token).
5. Test the connection.
6. Review attribute mappings and provisioning scope before enabling the job.

### User Attribute Mapping (Entra ID)

| Entra ID Attribute | SCIM Attribute | Notes |
|---|---|---|
| `userPrincipalName` | `userName` | Login username |
| `mail` | `emails[type eq "work"].value` | Primary email |
| `displayName` | `displayName` | Accepted and skipped as an unmanaged attribute |
| `givenName` | `name.givenName` | Given name |
| `surname` | `name.familyName` | Family name |
| `accountEnabled` | `active` | Suspends or restores this scope's membership; see reactivation below |

### Reactivation and deprovisioning

Setting a User to `active=false` suspends only this scope's membership;
memberships in other tenants or providers are untouched. The resource stays
addressable while inactive: `GET` and `PATCH` keep working on both scopes, so
an identity provider can inspect and re-enable a user with `active=true`
directly through SCIM. When the last active membership is suspended the
account can no longer authenticate anywhere; reactivating restores login.

`DELETE` removes this scope's membership and the user is deactivated globally
only when no membership remains anywhere. A later `POST` with the same
`userName` and `externalId` re-provisions the membership and restores login
without a manual account edit.

### Current Limitations

- **Bulk operations** — not supported (`POST /Bulk`).
- **Password changes** — not supported via SCIM. Use the IdP's native password
  management.
- **Sorting** — not supported.
- **List pagination** — capped at 200 resources per request.
- **Filtering** — list endpoints apply a single-expression `filter` subset
  (`attribute operator value` with `eq`, `ne`, `co`, `sw`, `ew`, `gt`, `ge`,
  `lt`, `le`, `pr`); logical operators and multi-expression filters are
  rejected. See the [integration guide](../integration/scim.md) for the
  filterable attributes.
- **ETags** — SCIM resources carry no `ETag`/`If-Match` handling.
- **Provider Group membership sync** — skips users who are not active staff of
  that provider. Provision Users first.

> [!NOTE]
> `ServiceProviderConfig` truthfully advertises the implemented subset:
> `patch` and `filter` (200-resource maximum) supported, `bulk`,
> `changePassword`, `sort`, and `etag` unsupported, and OAuth Bearer as the
> only authentication scheme.

For the full SCIM specification details including endpoint schemas, operation
semantics, and provider-specific configuration, see the dedicated SCIM
integration guide:
[docs/integration/scim.md](../integration/scim.md).

---

## RBAC Basics

ITAMbox uses a unified role-based access control (RBAC) system with one
container type (Tenant), one permission vocabulary, and additive grants.

### Core Concepts

| Concept | Description |
|---|---|
| **Tenant** | The data-isolation boundary. Every object lives in one tenant. |
| **Membership** | A user's active/inactive binding to one tenant. It has no role field; access is granted separately. |
| **Role** | A tenant-owned named permission set. Permission values use `app_label.codename`, for example `assets.view_asset`. Provider roles can be shared with managed tenants when configured. |
| **RoleGrant** | Assigns one role to exactly one Membership or one tenant-owned UserGroup. It records the grantor, reason, and optional `valid_until`. |
| **RoleGrantScope** | Child row(s) that define grant reach: `own`, one specific `tenant`, one `tenant_group` (including descendants), or `all_managed`. Multiple scopes add reach; there is no deny scope. |
| **UserGroup** | A flat application group owned by one tenant. Memberships are added via `GroupMembership` rows. |
| **GroupMembership** | Links a tenant membership to a UserGroup, making the user inherit role grants on that group. |

### Permission Resolution

When checking `user.has_perm('assets.view_asset')`, the system:

1. **Superuser** — always returns `True`.
2. **Inactive user** (`is_active=False`) — always returns `False`.
3. **Resolves the tenant context** — from the object's `tenant` attribute,
   the ambient current tenant, or the group-scoped tenant set.
4. **Collects effective permissions** — the frozen union of:
   - Direct `RoleGrant` rows on the user's Membership in the target tenant.
   - `RoleGrant` rows on any UserGroup the user belongs to in that tenant.
   - Each grant's permissions are additive; deny is unsupported.
5. **Checks scope coverage** — at least one live `RoleGrantScope` row on a
   grant must cover the target tenant. A managed scope only applies when the
   principal and role belong to the managing provider and that provider's
   live management relationship covers the target.

Permissions are cached per-tenant on the user object for the duration of the
request (`_perms_tenant_<pk>`), so repeated `has_perm` calls in a single
request cost at most three database queries.

### Role Scopes

| Scope Type | Meaning |
|---|---|
| `own` | The grant applies in the principal's own tenant (no tenant/group target on the scope row). |
| `tenant` | The grant applies in one specific tenant managed by the provider (scope row names that tenant). |
| `tenant_group` | The grant applies to managed tenants in the selected TenantGroup and descendant groups. |
| `all_managed` | The grant applies in every tenant currently managed by the role-owning provider (no explicit target on the scope row). |

> [!WARNING]
> Managed scopes (`tenant`, `tenant_group`, `all_managed`) require that the
> grant principal tenant **is** the role's owning provider, the role owner has
> `is_provider=True`, and the target remains managed by that provider. For
> `tenant` scope, the named tenant must be managed by the provider; group scope
> reaches its managed descendants. Cross-tenant access is explicit, never
> implied by a provider token.

### Provider (MSP) Mode

A tenant with `is_provider=True` is a **managing tenant**. Other tenants can
set their `managed_by` field to point to a provider (depth-1 only — chains are
not supported). Provider-staff users hold a Membership in the provider tenant
and are granted roles with managed scopes to reach into managed tenants.

### User Groups

User Groups (`users.UserGroup`) are flat, tenant-owned containers. They do not
nest — there is deliberately no group-in-group relation. External directory
nesting (LDAP OU trees, Entra ID group hierarchies) is **flattened** into
`GroupMembership` rows during SSO/SCIM sync.

A User Group:
- Is owned by exactly one tenant (cannot be reassigned after creation).
- May carry `RoleGrant` rows that apply to all its members.
- May have its memberships sourced from manual assignment, SCIM, LDAP, OIDC,
  or SAML.

### Role Best Practices

1. **Least privilege** — create narrow roles with only the permissions needed
   for a specific job function, then compose access by granting multiple roles.
2. **Use groups for shared access** — grant roles to User Groups rather than
   individual memberships for teams. Add/remove people from the group instead
   of managing individual grants.
3. **Expire privileged grants** — direct membership grants of elevated roles require
   a `valid_until` date and a `reason`. The system enforces this at the model
   level.
4. **Audit regularly** — review role grants (Organization → Memberships →
   Grants tab) and remove stale assignments.

### Permission Vocabulary

Permissions follow Django's `app_label.codename` convention:

```
assets.view_asset       assets.add_asset        assets.change_asset      assets.delete_asset
licenses.view_license   licenses.add_license    licenses.change_license  licenses.delete_license
organization.view_tenant organization.add_tenant ...
users.view_user         users.add_user          users.change_user        users.delete_user
extras.view_dashboard   extras.change_dashboard  ...
```

The full list of available permissions is determined by the installed Django
apps and their registered models.

---

## Related Documentation

- [SCIM Integration Guide](../integration/scim.md) — full SCIM spec details
- [SSO & MFA](sso-and-mfa.md) — identity provider configuration
- [Tenants](../models/organization/tenant.md) — tenant model and MSP mode
- [Users](../models/users/user.md) — user account model
- [Webhooks & Automation](webhooks-and-automation.md) — event-driven integrations
