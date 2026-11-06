# Single Sign-On & Multi-Factor Authentication

ITAMbox supports per-tenant identity providers so that each tenant in a
multi-tenant deployment can authenticate its users against its own LDAP
directory, SAML identity provider, or OpenID Connect (OIDC) provider — all
within a single ITAMbox instance.

---

## Multi-Tenant SSO Overview

In a conventional Django application, authentication backends are configured
globally in `settings.py`. ITAMbox extends this model by making every SSO
backend **tenant-aware**: when a user authenticates, the backend first resolves
which tenant the user is signing into, then loads that tenant's specific
configuration from a JSON settings dictionary keyed by tenant slug.

Each tenant can use a **different identity provider type**:

| Tenant | SSO Type | Configuration Key |
|---|---|---|
| `alpha-corp` | LDAP | `ITAMBOX_TENANT_LDAP_CONFIGS` |
| `beta-labs` | SAML | `ITAMBOX_TENANT_SAML_CONFIGS` |
| `gamma-inc` | OIDC | `ITAMBOX_TENANT_OIDC_CONFIGS` |
| `delta-co` | Local only | (no SSO config — falls back to password login) |

> [!IMPORTANT]
> SSO backends are **additive**: they participate in the authentication chain
> alongside the password backend. A tenant with no SSO configuration simply
> ignores those backends — users authenticate with username/password as normal.
> When an SSO backend is configured but cannot resolve the tenant, it returns
> `None` (skips) rather than raising an error, letting the next backend in the
> chain handle the request.

### How Tenant Resolution Works

The SSO backends resolve the tenant from several sources, tried in order:

1. **Session** — the `oidc_tenant_slug` session key (set during OIDC flow).
2. **URL path** — `/accounts/ldap/<tenant_slug>/login` or
   `/oidc/authenticate/<tenant_slug>/`.
3. **Domain suffix** — for LDAP, the domain part of the username
   (e.g. `user@alpha-corp` resolves to tenant `alpha-corp` by matching
   the domain's first component against tenant slugs).

> [!TIP]
> When using LDAP with UPN-style usernames (`user@domain`), configure your
> tenants' slugs to match the first subdomain of your corporate email domain
> (e.g. tenant slug `alpha-corp` for `user@alpha-corp.com`).

### JIT Provisioning

All three SSO backends perform **just-in-time (JIT) provisioning** on
successful authentication:

1. **User account** — created or updated from identity provider claims
   (username, email, first/last name).
2. **AssetHolder profile** — linked or created in the target tenant, syncing
   UPN, email, and name attributes from the IdP.
3. **Membership & role** — a Membership row is created in the target tenant,
   and a role (Admin, Manager, or Member) is assigned based on group-claim
   mappings (see each backend's group mapping section below).

> [!WARNING]
> Privileged roles (Admin, Manager) that are created through JIT provisioning
> carry a **24-hour TTL** by default. This forces an operator to create the
> permanent role definition deliberately before the temporary grant expires.
> Set `ITAMBOX_SSO_AUTOCREATE_PRIVILEGED_ROLES=False` to disable auto-creation
> of privileged roles entirely — group claims will fall back to Member.

---

## LDAP Configuration

The LDAP backend (`core.auth.ldap.MultiTenantLDAPBackend`) wraps
`django-auth-ldap` and injects tenant-specific configuration at runtime.

### Configuring `ITAMBOX_TENANT_LDAP_CONFIGS`

Set this environment variable to a JSON object keyed by tenant slug. Each
tenant's value is a dictionary of `django-auth-ldap` settings (uppercase
or lowercase keys both work):

```json
{
  "alpha-corp": {
    "SERVER_URI": "ldaps://ldap.alpha-corp.com:636",
    "BIND_DN": "cn=readonly,dc=alpha-corp,dc=com",
    "BIND_PASSWORD": "your-bind-password",
    "USER_SEARCH_BASE": "ou=users,dc=alpha-corp,dc=com",
    "USER_SEARCH_FILTER": "(uid=%(user)s)",
    "REQUIRE_GROUP": "cn=itambox-users,ou=groups,dc=alpha-corp,dc=com",
    "LDAP_GROUP_ROLE_MAPPING": {
      "cn=itambox-admins,ou=groups,dc=alpha-corp,dc=com": "Admin",
      "cn=itambox-managers,ou=groups,dc=alpha-corp,dc=com": "Manager"
    }
  }
}
```

### URI Format

ITAMbox supports standard LDAP and LDAPS URIs:

- `ldap://host:389` — unencrypted (not recommended in production).
- `ldaps://host:636` — LDAP over TLS.
- `ldap://host:389` with `START_TLS: true` — upgrade to TLS after connecting.

Set `OPT_REFERRALS: 0` to disable referral chasing if your directory returns
referral responses that the server cannot follow.

### Required Parameters

| Parameter | Description |
|---|---|
| `SERVER_URI` | LDAP server URI (`ldap://` or `ldaps://`). |
| `BIND_DN` | DN of the service account used to search the directory. |
| `BIND_PASSWORD` | Password for the bind DN. |
| `USER_SEARCH_BASE` | Base DN for user search (e.g. `ou=users,dc=example,dc=com`). |
| `USER_SEARCH_FILTER` | LDAP filter template — `%(user)s` is replaced with the username. Default: `(uid=%(user)s)`. |

### Optional Parameters

| Parameter | Description |
|---|---|
| `REQUIRE_GROUP` | DN of a group the user must belong to. Users not in this group are rejected. |
| `LDAP_GROUP_ROLE_MAPPING` | Map LDAP group DNs to ITAMbox role names (`Admin`, `Manager`, `Member`). The highest-priority mapped role wins. |
| `GROUP_TYPE` | Group type class name from `django_auth_ldap.config` (e.g. `GroupOfNamesType`). |
| `GROUP_SEARCH_BASE` | Base DN for group searches. |
| `GROUP_SEARCH_FILTER` | Filter for group membership queries. |
| `START_TLS` | Set to `true` to upgrade a plain `ldap://` connection to TLS. |
| `OPT_REFERRALS` | `0` to disable, `1` to follow referrals. |
| `OPT_NETWORK_TIMEOUT` | Network timeout in seconds. |

### Group Requirements

The `REQUIRE_GROUP` parameter enforces group-based access control. When set,
the user is only allowed to authenticate if they are a member of the specified
group. Combine this with `LDAP_GROUP_ROLE_MAPPING` to map multiple LDAP groups
to ITAMbox roles:

```json
"LDAP_GROUP_ROLE_MAPPING": {
  "cn=itambox-admins,ou=groups,dc=example,dc=com": "Admin",
  "cn=itambox-managers,ou=groups,dc=example,dc=com": "Manager",
  "cn=itambox-users,ou=groups,dc=example,dc=com": "Member"
}
```

The priority order is: Admin > Manager > Member. If a user belongs to multiple
mapped groups, the highest-priority role wins.

### Testing LDAP Connections

Use the `python-ldap` library directly to verify connectivity before
configuring ITAMbox:

```python
import ldap

conn = ldap.initialize('ldaps://ldap.alpha-corp.com:636')
conn.set_option(ldap.OPT_REFERRALS, 0)
conn.simple_bind_s('cn=readonly,dc=alpha-corp,dc=com', 'password')

# Search for a test user
result = conn.search_s(
    'ou=users,dc=alpha-corp,dc=com',
    ldap.SCOPE_SUBTREE,
    '(uid=testuser)',
    ['dn', 'cn', 'mail', 'memberOf']
)
print(result)
conn.unbind_s()
```

> [!TIP]
> If the `python-ldap` package is not installed, the LDAP backend gracefully
> degrades with a dummy module — it returns `None` on every authentication
> attempt rather than crashing at import time. This makes the LDAP backend
> safe to include in `AUTHENTICATION_BACKENDS` even in environments where
> LDAP is not used.

### Membership Sync

On each successful LDAP authentication, the backend:

1. Updates the user's `AssetHolder` profile (UPN, email, first/last name from
   `userPrincipalName`, `mail`, `givenName`, `sn` attributes).
2. Resolves the user's LDAP group memberships.
3. Matches groups against `LDAP_GROUP_ROLE_MAPPING` to determine the role.
4. Creates or refreshes the tenant Membership and RoleGrant.

---

## SAML Configuration

The SAML backend (`core.auth.saml.TenantSaml2Backend`) wraps `djangosaml2` /
`pysaml2` and constructs a tenant-specific `SPConfig` at runtime.

### Configuring `ITAMBOX_TENANT_SAML_CONFIGS`

```json
{
  "alpha-corp": {
    "entityid": "https://alpha.itambox.com/saml2/metadata/",
    "base_url": "https://alpha.itambox.com",
    "metadata": {
      "remote": [
        {
          "url": "https://identity.alpha-corp.com/federation/metadata"
        }
      ]
    },
    "SAML_GROUP_ROLE_MAPPING": {
      "ITAMbox-Admins": "Admin",
      "ITAMbox-Managers": "Manager",
      "ITAMbox-Users": "Member"
    }
  }
}
```

Each key normally names the tenant slug whose login flow and memberships the
provider belongs to. A deployment-wide `default` entry is accepted only when
exactly one live tenant exists; with multiple tenants it is intentionally not
shown on the login page because the callback could not be scoped safely.

### Required Parameters

| Parameter | Description |
|---|---|
| `entityid` | The SAML entity ID of this ITAMbox instance as a Service Provider. Usually the metadata URL (e.g. `https://your-instance/saml2/metadata/`). |
| `metadata` | IdP metadata source. Use `{"remote": [{"url": "..."}]}` to fetch metadata from a URL, or `{"local": ["/path/to/metadata.xml"]}` for a local file. |

### Optional Parameters

| Parameter | Description |
|---|---|
| `base_url` | Override the base URL used to construct ACS and SLO endpoint URLs. Defaults to `https://<tenant-slug>.local`. |
| `allow_unsolicited` | Accept unsolicited IdP-initiated assertions. Default: `false` (secure default). |
| `authn_requests_signed` | Sign authentication requests. Default: `false`. |
| `logout_requests_signed` | Sign logout requests. Default: `false`. |
| `want_assertions_signed` | Require signed assertions. Default: `true`. |
| `want_response_signed` | Require signed responses. Default: `true`. |
| `SAML_GROUP_ROLE_MAPPING` | Map SAML group attribute values to ITAMbox role names. |
| `SAML_CREATE_UNKNOWN_USER` | Set to `false` to stop this tenant's assertions from provisioning accounts that do not exist yet. It is combined with the global `SAML_CREATE_UNKNOWN_USER` setting (both must allow it). Default: `true`. |
| `enabled` | Set to `false` to disable the provider and hide its login action without deleting the configuration. Default: `true`. |
| `display_name` | Optional label used on the login page. The tenant name is used when omitted. |

### Attribute Mapping

The SAML backend extracts user attributes from the SAML assertion's
`ava` (attribute-value-assertion) dictionary. The following attribute keys
are tried in order:

| User Field | SAML Attribute Keys (tried in order) |
|---|---|
| Email | `email`, `mail`, `User.Email` |
| First Name | `givenName`, `first_name`, `User.FirstName` |
| Last Name | `sn`, `last_name`, `User.LastName` |
| UPN | `upn`, `userPrincipalName`, `uid`, `nameidentifier` |
| Groups | `groups`, `memberOf`, `User.Groups` |

### Account Binding

A SAML NameID is matched against the account username. To stop one tenant's
identity provider from authenticating as another tenant's user, an assertion
that names an **existing** account is accepted only when:

- the account holds an **active membership** in the tenant the SAML login was
  started for, and
- the account is **neither a superuser nor staff**. These accounts can never be
  signed in through a SAML NameID; use local password login (with MFA) for them.

A refused assertion fails the login without changing anything: no membership,
asset holder, profile update or audit entry is written. Unknown identities are
still provisioned just in time, unless `SAML_CREATE_UNKNOWN_USER` is `false`
for the tenant (see above).

### Security Defaults

> [!IMPORTANT]
> The SAML backend enforces secure defaults: `want_assertions_signed` and
> `want_response_signed` are `true` by default. Unsolicited assertions are
> rejected (`allow_unsolicited: false`). Only relax these if your IdP does
> not support signed assertions — and only after a security review.

---

## OIDC Configuration

The OIDC backend (`core.auth.oidc.TenantOIDCBackend`) wraps
`mozilla-django-oidc` with tenant-aware settings resolution and
additional token validation.

### Configuring `ITAMBOX_TENANT_OIDC_CONFIGS`

```json
{
  "alpha-corp": {
    "OIDC_RP_CLIENT_ID": "your-client-id",
    "OIDC_RP_CLIENT_SECRET": "your-client-secret",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token",
    "OIDC_OP_USER_ENDPOINT": "https://graph.microsoft.com/oidc/userinfo",
    "OIDC_OP_ISSUER": "https://login.microsoftonline.com/<tenant-id>/v2.0",
    "OIDC_OP_JWKS_ENDPOINT": "https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys",
    "OIDC_RP_SIGN_ALGO": "RS256",
    "OIDC_RP_SCOPES": "openid email profile",
    "OIDC_GROUP_ROLE_MAPPING": {
      "ITAMbox-Admins": "Admin",
      "ITAMbox-Managers": "Manager",
      "ITAMbox-Users": "Member"
    }
  }
}
```

### Required Parameters

| Parameter | Description |
|---|---|
| `OIDC_RP_CLIENT_ID` | The client ID registered with the IdP. |
| `OIDC_RP_CLIENT_SECRET` | The client secret for the registered application. |
| `OIDC_OP_AUTHORIZATION_ENDPOINT` | IdP's authorization endpoint URL. |
| `OIDC_OP_TOKEN_ENDPOINT` | IdP's token endpoint URL. |
| `OIDC_OP_USER_ENDPOINT` | IdP's userinfo endpoint URL. |
| `OIDC_OP_ISSUER` | The expected `iss` claim value. **Mandatory** — authentication is rejected if this is not configured or does not match the token's issuer. |
| `OIDC_OP_JWKS_ENDPOINT` | JWKS endpoint used to verify RS/ES-signed ID tokens. Instead of this endpoint, `OIDC_RP_IDP_SIGN_KEY` may provide the IdP signing key directly. |

### Optional Parameters

| Parameter | Description |
|---|---|
| `OIDC_RP_SIGN_ALGO` | JWT signature algorithm. Default: `RS256`. |
| `OIDC_RP_SCOPES` | Space-separated OIDC scopes. Default: `openid email profile`. |
| `OIDC_GROUP_ROLE_MAPPING` | Map OIDC `groups` claim values to ITAMbox role names. |
| `OIDC_GROUP_PROVIDER_ROLE_MAPPING` | For managed tenants: map OIDC groups to provider-staff roles (see below). |
| `enabled` | Set to `false` to disable the provider and hide its login action without deleting the configuration. Default: `true`. |
| `display_name` | Optional label used on the login page. The tenant name is used when omitted. |

### Callback URLs

All tenant-specific OIDC entry points use the shared callback URL:

```
https://<your-instance>/oidc/callback/
```

ITAMbox stores the selected tenant in the login session before redirecting to
the identity provider. The shared callback restores that tenant context before
validating the response and provisioning memberships.

Register this exact URL as the **Redirect URI** in your IdP application
configuration. The authorization URL is:

```
https://<your-instance>/oidc/authenticate/<tenant_slug>/
```

### Token Validation

The backend enforces additional checks beyond what `mozilla-django-oidc`
performs by default:

| Check | Behaviour |
|---|---|
| **Audience** | The token's `aud` claim must include the configured `OIDC_RP_CLIENT_ID`. |
| **Authorized Party** | If `azp` is present, it must match the client ID. |
| **Issuer** | The `iss` claim must match `OIDC_OP_ISSUER` exactly. If `OIDC_OP_ISSUER` is not configured, authentication is **rejected** (fail-closed by design). |
| **Algorithm** | The backend verifies the signature algorithm and rejects `none` / `HS256` downgrades. |

### Testing with a local mock OIDC provider

The following recipe is for development and test environments only. It uses NAV's
`mock-oauth2-server` with a pinned image and binds the provider to loopback:

```text
ghcr.io/navikt/mock-oauth2-server:6.0.0@sha256:b9fa251aefee22a97c32534d23a1c400f01dbd483ab263b013d89f6d60d96691
```

Set `JSON_CONFIG` to the provider configuration with interactive login, `NettyWrapper`, RS256, and
one exact subject request mapping, then run the image with a loopback-only port binding:

```bash
docker run --rm --name itambox-oidc-mock \
  --publish 127.0.0.1:8081:8080 \
  --env JSON_CONFIG="$JSON_CONFIG" \
  ghcr.io/navikt/mock-oauth2-server:6.0.0@sha256:b9fa251aefee22a97c32534d23a1c400f01dbd483ab263b013d89f6d60d96691
```

Configure the provider's issuer as `http://127.0.0.1:8081/itambox-e2e` and use the interactive
subject `itambox-e2e-oidc-user`. The provider mapping should match that subject exactly and return
the claims needed by the test identity; arbitrary typed subjects must not receive the mapped claim
set. Never replace `127.0.0.1:8081:8080` with a public host binding.

For ITAMbox, use a redacted tenant configuration like this and replace the client values with
local-test values:

```json
{
  "helix-rnd": {
    "enabled": true,
    "display_name": "Local OIDC mock",
    "OIDC_RP_CLIENT_ID": "local-test-client",
    "OIDC_RP_CLIENT_SECRET": "<redacted-local-test-secret>",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": "http://127.0.0.1:8081/itambox-e2e/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": "http://127.0.0.1:8081/itambox-e2e/token",
    "OIDC_OP_USER_ENDPOINT": "http://127.0.0.1:8081/itambox-e2e/userinfo",
    "OIDC_OP_ISSUER": "http://127.0.0.1:8081/itambox-e2e",
    "OIDC_OP_JWKS_ENDPOINT": "http://127.0.0.1:8081/itambox-e2e/jwks",
    "OIDC_RP_SIGN_ALGO": "RS256",
    "OIDC_RP_SCOPES": "openid email profile groups",
    "OIDC_USE_NONCE": true,
    "OIDC_CREATE_USER": true,
    "OIDC_GROUP_ROLE_MAPPING": {"local-oidc-admins": "Admin"}
  }
}
```

Use the provider's JWKS endpoint rather than a static signing key; omit `OIDC_RP_IDP_SIGN_KEY`.
The shared callback is always the exact URL `http://localhost:8000/oidc/callback/`, while the
tenant-specific authorization entry point is `http://localhost:8000/oidc/authenticate/helix-rnd/`.
Register the shared callback URL exactly in the relying-party configuration; do not substitute the
tenant-specific initiation URL.

Never expose the mock provider publicly: it is intentionally not a production identity provider.
Never reuse a fixed client secret from a local test outside local tests, and do not place it in
production configuration or documentation.

### Provider-Staff Mapping

For tenants that are **managed** by a provider (`managed_by` is set), the
backend checks the managing provider's OIDC config for
`OIDC_GROUP_PROVIDER_ROLE_MAPPING`. If the user's groups match a provider-staff
mapping, they are provisioned as provider staff instead of as a customer-local
user. The customer Membership is removed, and AssetHolder profiles are unlinked
(preserving history).

---

## MFA (TOTP)

ITAMbox enforces Time-based One-Time Password (TOTP) multi-factor
authentication for local-password logins by privileged users.

### When MFA Is Required

MFA enforcement applies only when **all** of these conditions are met:

1. The user authenticated via **local password** (not SSO — LDAP/SAML/OIDC
   sessions delegate MFA to the identity provider).
2. The user is a **superuser** or holds a **privileged role**. A role is
   considered privileged if:
   - Its name (case-insensitive) matches `Admin` or `Manager`, **or**
   - It contains any non-view permission codename (e.g. anything other than
     `view_*`).
3. The `MFA_ENFORCED` Django setting is `True` (default in production; off in
   development).

SSO-authenticated sessions use a different authentication backend
(`djangosaml2.backends.Saml2Backend`, `TenantOIDCBackend`, etc.) and are
**exempt** from MFA enforcement — the identity provider is responsible for
the second factor.

### Configuring `ITAMBOX_REQUIRE_MFA`

In `.env`:

```bash
# Enforce TOTP MFA for local-password logins by superusers/owner-admin roles
# (SSO/LDAP/SAML/OIDC always delegate MFA to the IdP). Default: True in prod.
ITAMBOX_REQUIRE_MFA=True
```

This maps to the `MFA_ENFORCED` Django setting.

### Setup Flow

When a privileged user logs in with a password for the first time (or after
their MFA session expires), they are redirected to the MFA gate:

1. **Enrollment** — if the user has no confirmed TOTP device:
   - A QR code is displayed (generated with `segno`). Scan it with any
     authenticator app (Google Authenticator, Authy, 1Password, etc.).
   - A base32 secret is also shown for manual entry.
   - The user enters a 6-digit code from their authenticator app to confirm.
   - On success, **10 single-use backup codes** are generated and displayed.
     The user should save these immediately — they are shown only once.

2. **Verification** — if the user already has a confirmed TOTP device:
   - A simple 6-digit code prompt is shown.
   - The user enters the current code from their authenticator app (or a
     backup code).
   - On success, the user is redirected to their intended destination.

3. **Backup Codes** — each backup code is a `StaticToken` that can be used
   once. After use, the code is consumed. If all backup codes are exhausted,
   generate new ones by deleting and re-enrolling the TOTP device (requires
   admin intervention).

### MFA Middleware Behaviour

The `OTPEnforcementMiddleware` intercepts requests for users who need MFA
but have not yet verified:

- **Browser requests** — redirected to the MFA gate (`/mfa/setup/`).
- **API / HTMX / JSON clients** — return HTTP 403 with
  `{"detail": "MFA verification required."}` (no redirect — these clients
  cannot render the HTML gate).
- **Allowlisted paths** — login, logout, password reset, OIDC/SAML callbacks,
  health checks, static/media files are never redirected (prevents loops).

> [!TIP]
> Users can voluntarily enroll in MFA even when enforcement is off
> (e.g. in development). Visit `/mfa/setup/` while authenticated to set up
> a TOTP device and receive backup codes.

---

## Troubleshooting SSO

### Redirect URI Mismatch

**Symptom:** IdP returns an error like "The redirect URI is not registered"
or "reply URL mismatch".

**Cause:** The callback URL registered in the IdP does not match the URL
ITAMbox constructs.

**Fix:**
- OIDC: Register the shared `https://<your-instance>/oidc/callback/`
  exactly, including the trailing slash.
- SAML: Ensure the `base_url` in the tenant config matches the ACS endpoint
  the IdP sends assertions to.
- Verify `ITAMBOX_BASE_URL` is set correctly in `.env` — it is used to
  construct absolute URLs when `base_url` is not explicitly configured.

### Certificate Expiry

**Symptom:** SAML or OIDC authentication suddenly fails with signature
validation errors.

**Cause:** The IdP's signing certificate has expired, or the metadata
cache is stale.

**Fix:**
- SAML: The `pysaml2` metadata loader caches remote metadata. Restart the
  application or clear the metadata cache to force a re-fetch.
- OIDC: The `mozilla-django-oidc` library refreshes the IdP's JWKS keys
  periodically. Verify the IdP's `jwks_uri` endpoint is reachable.
- Check the IdP's certificate expiry date and renew it if needed.

### Group Filtering Not Working

**Symptom:** Users authenticate successfully but are assigned the wrong
role (or no role), or are rejected despite being in the correct group.

**Cause:** The group mapping configuration does not match the actual group
names/DNs returned by the IdP.

**Fix:**
- **LDAP:** Verify the group DNs in `LDAP_GROUP_ROLE_MAPPING` exactly match
  the values in the user's `memberOf` attribute. Check the group search base
  and filter. Test with `ldapsearch`.
- **SAML:** Enable debug logging (`ITAMBOX_LOG_LEVEL=DEBUG`) and inspect
  the `ava` dictionary in the SAML response. Verify group attribute keys
  (`groups`, `memberOf`, `User.Groups`) match the keys in your mapping.
- **OIDC:** Inspect the `groups` claim in the ID token (use jwt.io to decode
  a test token). The group values must match keys in
  `OIDC_GROUP_ROLE_MAPPING` exactly.

### "Not Configured" Errors

**Symptom:** Logs show "OIDC not configured for tenant" or the backend
silently skips.

**Cause:** The tenant slug in the configuration JSON does not match the
tenant slug in the database, or the JSON is malformed.

**Fix:**
- Verify the tenant slug in the database (Organization → Tenants → slug field)
  matches the key in the JSON exactly (case-sensitive).
- Validate your JSON: run `echo "$ITAMBOX_TENANT_OIDC_CONFIGS" | python -m json.tool`.
- Check that `ITAMBOX_TENANT_LDAP_CONFIGS`, `ITAMBOX_TENANT_SAML_CONFIGS`, or
  `ITAMBOX_TENANT_OIDC_CONFIGS` is set and readable in the application
  environment.

### LDAP Connection Failures

**Symptom:** LDAP authentication times out or returns no results.

**Fix:**
- Verify network connectivity from the ITAMbox server to the LDAP server on
  the configured port (check firewalls, security groups).
- Test with `ldapsearch` from the ITAMbox server environment.
- Check TLS certificates if using `ldaps://` — the server certificate must
  be trusted by the ITAMbox host.
- Verify the bind DN and password are correct.
- Check `OPT_REFERRALS` — if your AD server returns referrals the ITAMbox
  server cannot follow, set it to `0`.

### `can_login` Flag Blocks SSO

**Symptom:** SSO authentication appears to succeed but the user is not
logged in.

**Cause:** The user's `can_login` flag is `False` on their User record.

**Fix:** Set `can_login = True` on the user's profile. This flag is checked
by all SSO backends (LDAP, SAML, OIDC) after successful authentication and
before returning the user object. It is a separate axis from `is_active`.

### SAML Assertion Not Signed

**Symptom:** SAML login fails with signature validation errors.

**Cause:** The IdP is not signing assertions, but `want_assertions_signed`
is `true` (the default).

**Fix:** Either configure the IdP to sign assertions (recommended), or set
`want_assertions_signed: false` and `want_response_signed: false` in the
tenant's SAML config. Only do this after a security review.

---

## Related Documentation

- [Tenants](../models/organization/tenant.md) — tenant configuration and MSP mode
- [Users](../models/users/user.md) — user account model and `can_login` flag
- [API Tokens & SCIM](api-tokens-and-scim.md) — API authentication and user provisioning
- [Installation Guide](../operations/installation.md) — deployment and environment setup
- [Updating a deployment](../operations/upgrades.md) — OIDC binding migration and rollback disclosure
