# Tenant Memberships

A **Membership** binds one User to one Tenant. It is the tenant-membership and active-status anchor; it has no role field. Access is granted separately through `RoleGrant` rows on the Membership or on a `UserGroup` the member belongs to. See [API Tokens, SCIM Provisioning & RBAC](../../usage/api-tokens-and-scim.md#rbac-basics) for permission and scope resolution.

---

## Attributes & Fields

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Active** | Whether this membership's grants are currently effective. Deactivation retains the membership and grant history. | Boolean | Yes |
| **External ID** | SCIM identifier scoped to the provisioning domain. | String | No |
| **Joined At** | The timestamp when the membership was created. | DateTime | Yes (Auto) |
| **Tenant** | The tenant this membership is scoped to; the pair (user, tenant) is unique per active membership. | Foreign Key | Yes |
| **User** | The account that receives this tenant's grants. A user may hold memberships in several tenants. | Foreign Key | Yes |

## Multi-Tenancy Scope
A user can hold memberships in multiple tenants. A membership has no direct permissions: effective permissions are the additive union of direct grants and eligible UserGroup grants whose `RoleGrantScope` covers the active tenant. Managed-tenant reach must be granted explicitly. API tokens remain bound to one tenant; GraphQL's built-in schema is read-only. See the linked RBAC guide for the full contract.
