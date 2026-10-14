# Users

A **User** is an authentication account. Tenant access is represented by
`Membership` rows and permission grants, not by direct Django user/group
permissions. See [API Tokens, SCIM Provisioning & RBAC](../../usage/api-tokens-and-scim.md#rbac-basics).

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Date Joined** | The date and time when the account was registered. | Date Time | Yes |
| **Email Address** | Email contact address for notifications. | String | No |
| **First Name** | User's first name. | String | No |
| **Can Login** | Controls whether the account may perform an interactive login. This is separate from `is_active` and from API-token access; setting it false does not revoke existing API tokens. | Boolean | Yes (default true) |
| **Groups** | Django auth groups. Application RBAC uses tenant `UserGroup` and `GroupMembership` records instead. | Many-to-Many | No |
| **Active** | Flag specifying if the account is active. Accounts should be deactivated instead of deleted to preserve audit trails. | Boolean | Yes |
| **Staff Status** | Designates whether the user can access administrative portals. | Boolean | Yes |
| **Superuser Status** | Grants all system permissions without explicit assignment. | Boolean | Yes |
| **Last Login** | The last login of the user. | Date Time | No |
| **Last Name** | User's last name. | String | No |
| **Password** | Hashed credentials for user login. | String | Yes |
| **User Permissions** | Django direct permissions; not the source of effective application RBAC grants. Use `Role`/`RoleGrant`/`RoleGrantScope`. | Many-to-Many | No |
| **Username** | Unique alphanumeric login username. | String | Yes |

## Features & Validation

* **Privilege fields**: `can_login`, `is_staff` and `is_superuser` are editable only by superusers. Disabling `can_login` does not revoke API tokens.
* **Account status**: `is_active` is distinct from `can_login`; memberships are independently active/inactive.
* **Audit Trails**: Changes are recorded in the system changelog where change logging applies.
