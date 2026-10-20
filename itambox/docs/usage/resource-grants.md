# Resource Grants

A **Resource Grant** lets one tenant share a single stock pool with another
principal without merging the two tenancies. The owning tenant keeps the pool,
its quantities and its catalogue item; the grantee just gets exactly the access
the grant names, for as long as the grant is active.

Use grants when a customer tenant has to draw on stock that another tenant
holds: an MSP spare pool, a shared warehouse, a group-wide consumables shelf.
Tenant-group membership alone never shares anything. It only makes a grant
*possible*, because only tenants inside a common group tree are offered as
grantees.

---

## What can be shared

A grant points at exactly one **stock pool**: one stock row at one location.
The three stock models are shareable:

| Shared object | Where it is managed |
|---|---|
| Component stock | **Inventory → Components → Stocks** |
| Accessory stock | **Inventory → Accessories → Stocks** |
| Consumable stock | **Inventory → Consumables → Stocks** |

Nothing else can be granted. Assets, licences, kit templates and catalogue
items (the component, accessory or consumable *model* rather than a stock row)
are not grantable, and a grant can never cover several pools at once. Sharing
three stock rows takes three grants.

The **owning tenant** of a pool is derived from the tenant of the pool's
location. It is never chosen in the form, so a grant can only ever share stock
the granting tenant really owns.

---

## Who may grant and revoke

Resource grants use the standard model permissions, but always in the **owner
tenant**:

| Action | Permission | Checked against |
|---|---|---|
| Grant a pool | `add_tenantresourcegrant` | The owner tenant of the pool |
| Revoke a grant | `delete_tenantresourcegrant` | The owner tenant of the grant |
| Read the grant list and the audit API | `view_tenantresourcegrant` | The active tenant scope |

Assign these through a role: in the role editor they live under
**Organization & Structure → Resource Grants**. Object-level scoping is applied
on top of the permission, so holding `add_tenantresourcegrant` in your own
tenant does not let you share another tenant's stock.

The **grantee side cannot revoke** a grant. Only the owner tenant ends it,
either by revoking it directly or by letting its deadline pass. A grantee that
no longer needs the pool asks the owner to revoke.

---

## Creating a grant

Grants are created **from the pool**, not from an empty form:

1. Switch to the owning tenant.
2. Open the stock list of the pool you want to share
   (**Inventory → Components/Accessories/Consumables → Stocks**).
3. Click **Share** on the stock row. The button only appears when you hold
   `add_tenantresourcegrant` at that pool.
4. Choose the grantee, the access level, an optional reason and an optional
   deadline.

| Field | Required | Description |
|---|---|---|
| **Grantee tenant** | One of the two | A single tenant that receives the access. |
| **Grantee tenant group** | One of the two | A tenant group; the grant also covers every descendant group of it. |
| **Access level** | Yes | `View` or `View + allocate/consume`. Defaults to `View`. |
| **Reason** | No | Free-text note for the audit trail, for example the contract or ticket the sharing belongs to. |
| **Valid until** | No | Deadline for the grant. Must be strictly in the future. |

Exactly one of **Grantee tenant** and **Grantee tenant group** must be set. The
grantee list offered in the form is limited to tenants the owner manages,
tenants that share the owner's tenant-group tree, and tenants you can access
yourself; the owning tenant never appears in it.

!!! warning "One grant per pool and grantee"
    Only one active grant may exist for the same pool and the same grantee.
    To change the access level or the deadline, revoke the grant and create a
    new one; the change log keeps both records.

---

## What a grantee receives

The grant carries **resource access only**. It never conveys user permissions:
an operator in the grantee tenant still needs the ordinary permission for the
operation they want to perform, checked in their own tenant.

With an active grant the shared pool becomes selectable in the grantee tenant
where the stock pool is offered:

- The pool appears in the grantee's component, accessory or consumable stock
  views and in the checkout flows that pick a source pool.
- A check-out that uses a cross-tenant pool records the pool as its source and
  keeps the grant as provenance on the resulting allocation or assignment, so
  the history still explains where the stock came from after the grant ends.

The pool itself stays the owner's: quantities, availability, low-stock
thresholds and reordering remain owner-side facts. A grantee sees the shared
pool, not a copy of it. Grants are also **not transitive**: a grantee cannot
re-share the pool with a third tenant. Only the owner tenant can create grants
for its own stock.

### Access levels

| Level | Allows |
|---|---|
| **View** | See the pool and its stock figures in the grantee tenant. No stock is moved. |
| **View + allocate/consume** | Everything View allows, plus check-out and allocation from the pool, up to the pool's available quantity. |

A `View + allocate/consume` grant satisfies a request that only needs View; a
`View` grant does not satisfy an allocation. If both a direct grant and a group
grant cover the same pool, the **direct grant is the one that applies**, even
when the group grant is the more permissive of the two.

---

## Expiry and revocation

### Deadlines

A grant with a **Valid until** value ends automatically. Once per UTC hour the
expiry sweep collects, per owner tenant, every active grant whose deadline has
already passed at the sweep's cutoff and revokes it. Revocation through expiry
is a normal revocation: the grant becomes inactive, the audit trail records that
the scheduled sweep performed it (not a human user), and the deadline that
triggered it is stored with the evidence.

Grants without a deadline stay active until somebody revokes them. The
expiry run's operational details, stable error codes and the restoration path
for a wrongly revoked grant are covered in
[Resource Grant Expiry](../operations/resource-grant-expiry.md).

### Revoking a grant

1. Switch to the owning tenant.
2. Open **Organization → Tenancy → Resource Grants**.
3. Click **Revoke** on the row and confirm.

Revocation takes effect immediately and ends the grantee's access to the pool.
It does **not** delete history: assignments and allocations that were created
under the grant keep their provenance pointer.

Two consequences are worth knowing before you revoke:

- **Grants are not in the recycle bin.** Revocation is a lifecycle state, not a
  deletion to be restored from the trash. The row stays readable on the audit
  surface.
- **A revoked grant cannot be re-armed from the UI.** Restoring a grant is an
  operator action with an explicit deadline correction; see the
  [expiry runbook](../operations/resource-grant-expiry.md).

Deleting or archiving a tenant revokes the grants it owns and the grants it
receives. The records and their evidence stay readable, and restoring the
tenant does not re-grant anything, so a restored tenant needs fresh grants.

---

## Where to see grants

**Organization → Tenancy → Resource Grants** lists every grant that involves
the active tenant, both given and received. The list follows the active tenant,
so switch the tenant selector to see the other side's view of the same share. A
platform superuser without an active tenant sees all grants.

| Column | Meaning |
|---|---|
| **Owner** | The tenant whose stock is shared. |
| **Grantee** | The receiving tenant, or the tenant group with the note *Group (incl. descendants)*. |
| **Shared pool** | The stock pool, or the model and identifier when the pool no longer exists. |
| **Access** | `View` or `View + allocate/consume`. |
| **Granted by** | The user who created the grant. |
| **Granted at** | Creation timestamp. |
| **Revoke** | Owner-side action. Shown only when you hold `delete_tenantresourcegrant` there. |

The page lists active grants only. Revoked grants, their deadlines and the
exact actor of every revocation remain available through the audit API and the
change log.

---

## Audit trail

Every grant is change-logged. Creating a grant writes a create entry, revoking
writes a delete entry; both record the acting user, the request identifier and
the changed fields. An expiry-driven revocation adds a dedicated evidence row
to the expiry run that performed it, including the deadline that fired and the
linked change entry.

The read-only audit API is the stable way to read the full history, including
revoked grants:

```
GET /api/organization/resource-grant-audit/
GET /api/organization/resource-grant-audit/<id>/
```

Each record reports the lifecycle state (`active` or `revoked`), owner, grantee
type and grantee, resource type and identifier, access level, reason, granting
user, creation time, deadline (`valid_until`) and revocation time
(`revoked_at`). Revoked records additionally carry a `revocation` object
describing how the grant ended, including the expiry run when it expired.

Useful filters:

| Parameter | Filters on |
|---|---|
| `state=active` / `state=revoked` | Lifecycle state |
| `owner_tenant_id`, `grantee_tenant_id`, `grantee_tenant_group_id` | Participating tenant or group |
| `resource_type_id`, `resource_id` | The shared pool's content type and identifier |
| `access_level` | `view` or `use` |
| `valid_until_before`, `valid_until_after` | Deadlines |
| `revoked_before`, `revoked_after` | Revocation timestamps |

Visibility follows the request's tenant container. The owner tenant, a direct
grantee and any tenant inside a covered live group subtree can read a grant.
Only the owner tenant sees the expiry-run aggregation. An API token is pinned
to its own tenant; an unbound platform superuser reads globally, while a
tenant- or group-bound superuser stays limited to that container.

## API and automation

| Capability | Surface |
|---|---|
| Read grant history | REST: `GET /api/organization/resource-grant-audit/` (filterable, read-only) |
| Create a grant | UI only (**Share** on a stock pool) |
| Revoke a grant | UI only (**Revoke** on the grant list) |
| Restore a revoked grant | Management command, see the [expiry runbook](../operations/resource-grant-expiry.md) |

The audit endpoint is deliberately read-only: `POST`, `PUT`, `PATCH` and
`DELETE` are rejected. Grants are not exposed through GraphQL and are excluded
from the data-transfer (CSV import/export) surfaces, because a grant is
authorization metadata rather than inventory data. Automating grant creation by
driving the UI is unsupported; use the UI for the lifecycle and the audit API
for reporting.

---

## Troubleshooting

**The Share button is missing on a stock row**

: You do not hold `add_tenantresourcegrant` for that pool's owner tenant.
  Check the active tenant and the role assignments there, then reload the stock
  list.

**The intended grantee is not in the grantee list**

: The grantee must be a tenant the owner manages, a tenant in the owner's
  tenant-group tree, or a tenant you can access yourself. Move the tenant into
  the right group or grant the owner tenant management of it first.

**A grantee cannot see or use the shared pool**

: Check, in this order: the grant is still active, the owner tenant and the
  pool still exist, the pool's location still belongs to the owner tenant, the
  grant's access level covers the operation (allocation needs
  `View + allocate/consume`), and the operating user holds the ordinary
  permission for the action in their own tenant. A grant never substitutes for
  that permission.

**A grant ended before its deadline**

: A direct grant and a group grant may both cover a pool, and the direct grant
  is the one that applies. Check the grant list in the owner tenant for a
  second, shorter-lived grant to the same grantee, and check the audit API for
  `revoked_at` and the revocation evidence.

**The grant list is empty although a share exists**

: The list shows grants involving the active tenant only. Switch to the owner
  tenant or to the grantee tenant, or use the audit API with
  `owner_tenant_id` or `grantee_tenant_id`.

---

## Related Documentation

- [Kits & Components](kits-and-components.md) — stock pools, allocations and
  cross-tenant check-out
- [Resource Grant Expiry](../operations/resource-grant-expiry.md) — the hourly
  expiry run, its error codes and grant restoration
- [Tenants](../models/organization/tenant.md) — tenant configuration and MSP mode
- [Tenant Groups](../models/organization/tenantgroup.md) — the group tree that
  makes a grant eligible
- [API Tokens & SCIM](api-tokens-and-scim.md) — token scoping for the audit API
