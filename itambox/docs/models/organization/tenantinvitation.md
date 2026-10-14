# Tenant Invitations — removed

> **Removed feature:** `TenantInvitation` and its email acceptance workflow have
> been removed from the application. This page remains only so that existing
> links resolve; it does not describe a supported model or onboarding process.

## Current onboarding

For technician onboarding, use the current Organization → **Onboard
Technician** route (`/organization/onboard/technician/`). For other tenant
membership changes, use Organization → **Memberships**
(`/organization/memberships/`). Check the deployed UI's labels and required
permissions; access is assigned through Role Grants, not through an
invitation's role field. SCIM provisioning is a separate, explicitly
configured integration; see [API Tokens and SCIM](../../usage/api-tokens-and-scim.md).

Do not promise that invitation emails, acceptance tokens, or automatic
AssetHolder-to-login linking are available. Identity linkage must follow the
current onboarding/provisioning flow, not an email-only invitation match.
