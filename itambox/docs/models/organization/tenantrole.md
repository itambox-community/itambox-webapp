# Tenant Roles

A **Tenant Role** (model name `Role`) is a named permission set owned by a Tenant. A `RoleGrant` assigns it to exactly one Membership or UserGroup, and child `RoleGrantScope` rows define where that grant applies. See [API Tokens, SCIM Provisioning & RBAC](../../usage/api-tokens-and-scim.md#rbac-basics).

---

## Attributes & Fields

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Description** | Optional notes detailing the purpose of the role. | Text | No |
| **Name** | The name of the role (e.g., `Auditor`). | String | Yes |
| **Permissions** | Permission tokens in `app_label.codename` form, e.g. `assets.view_asset`. | JSON list | No |
| **Shared with managed** | If enabled on a provider-owned role, managed tenants may assign the live role to their own members; only the provider owner edits its definition. | Boolean | Yes (default false) |
| **Slug** | URL-friendly role identifier. | String | Yes |
| **Tenant** | The tenant that owns the role. | Foreign Key | Yes |

## Business Logic
Role permission sets do not themselves grant access. `RoleGrant` links one role to either a Membership or a UserGroup (exactly one principal), and `RoleGrantScope` children define additive reach: `own`, a specific managed `tenant`, a `tenant_group` and its descendants, or `all_managed`. Managed scopes are valid only for grants whose principal and role belong to that managing provider and whose target tenant remains managed by it. There is no deny scope. Direct elevated membership grants require a reason and future `valid_until`; permission checks use `app_label.codename` tokens through the membership authorization backend. Core GraphQL remains read-only.
