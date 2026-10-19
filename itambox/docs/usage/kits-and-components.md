# Kits & Components

**Kits** group items for checkout. **Components** track pooled modular parts
(such as RAM or SSDs) allocated to assets, holders, or locations.

---

## Kits

A **Kit** is a pre-assembled template of items that are regularly issued
together. Think of it as a "shopping list" that can be checked out in a single
operation — the kit itself doesn't hold inventory; its items pull from their
respective stock pools.

### When to Use Kits

| Use Case | Example Kit |
|---|---|
| New hire onboarding | "Standard Developer Kit" — laptop, monitor, keyboard, mouse, headset |
| Role-based provisioning | "Remote Sales Kit" — laptop, 4G dongle, headset, webcam |
| Conference room staging | "Meeting Room Kit" — display adapter, clicker, cables, HDMI switch |
| Field technician dispatch | "Field Repair Kit" — tools, spare cables, thermal paste, cable ties |
| Temporary event setup | "Trade Show Booth Kit" — tablets, stands, chargers, signage mounts |

### Kit Structure

A Kit contains **Kit Items** — slots that each reference one of five possible
target types:

| Target Type | Example | Quantity Applicable |
|---|---|---|
| **Asset Type** | `MacBook Pro 16"` | No (one asset per slot) |
| **Accessory** | `Dell Wired Keyboard KB216` | Yes |
| **Consumable** | `Thermal Paste MX-4` | Yes |
| **Component** | `16 GB DDR4 SODIMM` | Yes |
| **License** | `Microsoft 365 Business Premium` | No (one seat per distinct license for the recipient) |

Each kit item must declare **exactly one** target — a database constraint
(`chk_kit_item_single_target`) prevents a single slot from referencing both
an accessory and a consumable, for example.

### Creating a Kit

Navigate to **Inventory → Kits → Add**:

1. **Name**: Give the kit a unique, descriptive name (e.g. "Standard Developer Onboarding Kit").
2. **Description**: Document who it's for and what it contains.
3. **Tenant**: Leave blank for a global kit visible across all tenants, or
   assign to a specific tenant for private use.
4. **Tags**: Optional labels for filtering and search.

Save the kit, then add items on the kit detail page.

### Adding Items to a Kit

On the kit detail page, click **Add Kit Item** and fill in:

| Field | Required | Description |
|---|---|---|
| **Target** | Yes | Select ONE: Asset Type, Accessory, License, Consumable, or Component |
| **Quantity** | Yes | How many units (default 1). Applies to Accessories, Consumables, and Components. Ignored for Asset Types and Licenses (one per slot) |

!!! warning "One target per item"
    A kit item can only target one thing. If your "Developer Kit" needs BOTH a
    keyboard and a mouse, create two separate kit items — one for the keyboard
    accessory and one for the mouse accessory.

### Checking Out a Kit

When you check out a kit to an AssetHolder or Location, ITAMbox performs an
**atomic multi-item checkout** in a single database transaction:

```mermaid
sequenceDiagram
    actor Admin
    participant UI as ITAMbox UI
    participant Service as Checkout Service
    participant DB as Database

    Admin->>UI: Check out "Dev Kit" to Jane Doe
    UI->>Service: checkout_kit(kit, holder, selected_assets, ...)
    Service->>DB: BEGIN TRANSACTION
    Service->>DB: Validate the selected device per hardware slot
    alt Selection or stock insufficient
        DB-->>Service: ValidationError
        Service->>DB: ROLLBACK
        Service-->>UI: "Insufficient stock: 2x USB-C Cable"
        UI-->>Admin: Show error with details
    else All in stock
        Service->>DB: Check out selected device → AssetAssignment + custody receipt
        Service->>DB: Assign Accessory → deduct stock
        Service->>DB: Consume Consumable → deduct stock
        Service->>DB: Assign License seat
        Service->>DB: COMMIT
        Service-->>UI: Checkout successful
        UI-->>Admin: Confirmation with item breakdown
    end
```

In a tenant-group or **All accessible tenants** scope, first select the required
**Target tenant**. The existing checkout form refreshes its holder, source-location
and device choices for that tenant. No allocation occurs during this refresh.
Only accessible tenants within the active group and, for a tenant-owned kit, its
own tenant are eligible. In a concrete tenant scope, that tenant is authoritative
and no additional tenant selector is shown.

Select each hardware device explicitly, then optionally set a shared checkout
status and dates. Selecting **Loan** makes **Due Date** mandatory. These hardware
options do not change the quantities of stock items.

Key properties of kit checkout:

- **All-or-nothing**: If any item in the kit lacks sufficient stock — or any selected
  device fails its eligibility checks — the entire checkout is rolled back. No partial
  fulfilment: assignments, stock deductions, license seats, custody receipts and audit
  rows roll back together, and scheduled custody notifications are discarded.
- **Explicit device selection**: Every hardware slot must name the concrete device
  (asset tag / serial) that is handed out. One distinct device per slot; a device that
  is reserved for another holder today, already assigned, repointed to another type or no longer deployable is
  rejected — never silently substituted.
- **Stock deduction**: Accessory and consumable quantities are deducted from
  the source location's stock pool automatically.
- **License assignment**: Each distinct license assigns one available seat to the
  holder, or to the first selected hardware device for a location checkout.
  Repeated rows of the same license do not assign duplicate seats to that same
  recipient. If no seat is available, the checkout fails.
- **Asset checkout**: Hardware slots run through the same individual-asset checkout
  operation: custody receipt and signature request (per the custody template), the
  reservation guard, the lifecycle guard, and the shared loan fields (checkout date,
  expected check-in, due date) all apply to every selected device. Reservation
  checks follow the individual checkout's current-date rule, not a future loan-window
  guarantee.
- **Signature notifications**: Signature-provider handoff and email start only after
  the complete checkout commits. A required missing holder email address rejects
  checkout before commit. A provider or mail failure after commit is logged; the
  completed assignment and its custody receipt remain recorded, but notification
  delivery must not be assumed successful.

> [!IMPORTANT]
> Kit checkout uses the `checkout_kit` service function (in
> `assets/services/__init__.py`), which wraps everything in `transaction.atomic()`.
> Concurrent kit checkouts are safe — database locks prevent double-allocation
> of the same stock, and the devices are re-validated under their row locks.

### Assigning Kits to Users / Locations

Kits themselves are templates, not inventory. Assignment happens at checkout
time:

1. **To an AssetHolder** (user): The checked-out items appear in the holder's
   assigned assets, accessories, and licenses.
2. **To a Location**: The items are checked out to a physical location (e.g.
   "Conference Room A") rather than a person.

After checkout, individual items can be managed separately — returned,
reassigned, or consumed independently of the original kit context.

### Kit Inventory Lifecycle

```mermaid
stateDiagram-v2
    state "Kit (Template)" as Kit
    state "Individual Items" as Items

    Kit --> Items : Checkout
    Items --> Items : Item returned / consumed
    Items --> Items : Item reassigned
    Items --> [*] : All items resolved

    note left of Kit : Kit is just a template.\\nNo stock lives here.
    note right of Items : After checkout, items\\nhave independent lifecycles.
```

**Entire kit** vs **individual items**:

| Operation | Entire Kit | Individual Item |
|---|---|---|
| Checkout | Atomic — all or nothing | Pick and choose from the kit |
| Return | Not supported as a unit | Return each item separately |
| Stock tracking | Items pull from their own pools | Each item type has its own stock |
| Audit trail | Kit checkout creates separate assignments | Each assignment is independently auditable |

!!! warning "No bulk return for kits"
    After a kit is checked out, items must be returned individually. There is
    no "return entire kit" operation because each item type has a different
    return workflow (asset check-in, accessory unassignment, consumable
    disposal, license release).

---

### Kit Availability and Owner Scope

A kit belongs to exactly one tenant, and its **In-Stock / Available** column is
measured against that owning tenant only, never against a total across all the
tenants your current scope can see:

- **Hardware rows** count the owning tenant's deployable, unassigned devices of
  that row's asset type. Each hardware row needs its own distinct device, so a
  kit with two rows of one type needs two free devices of that type.
- **Accessory, consumable and component rows** count the owning tenant's stock
  pools across its own stock locations, minus the open commitments that target
  the owning tenant. A global catalogue item is still stocked per tenant, so its
  pools are counted per owner as well.
- **License rows** count the owning tenant's own license pool. A license owned
  by another tenant, or a tenantless global license, is shown as **Unknown**
  rather than as a number, because that seat pool cannot be attributed to this
  kit's owner.

Opening a kit's checkout from the **All Tenants** view or a tenant-group scope
preselects the kit's owning tenant for you. Kit availability stays owner-only on
purpose here: a cross-tenant catalogue overview may show different numbers
because it answers a different question.

The header action reflects the same three states, and never claims more than is
verified:

| State | Header action |
|---|---|
| Every required resource verified available | **Deploy / Checkout Kit** (green) |
| A required resource is known to be missing | **Kit Unavailable (Out of Stock)** (no checkout control) |
| A required resource cannot be verified (Unknown) | Neutral **Deploy / Checkout Kit** with an *Unknown* note |

Unknown is not an availability claim and never a fabricated zero. A tenantless
(global) kit in an all-tenants scope additionally asks you to **select a target
tenant first**. Whichever action the header offers, the checkout re-validates
every resource inside the transaction, so a shortage still fails closed.

---

## Components

A **Component** is a modular hardware sub-assembly tracked in the inventory
catalogue. Unlike assets (which are individually serialised and tracked),
components exist as a **pooled stock** that gets allocated into parent assets.

### Common Component Examples

| Component Type | Example Model | Typical Parent Asset |
|---|---|---|
| Memory (RAM) | `Crucial 16 GB DDR4-3200 SODIMM` | Laptop, server |
| Solid-State Drive | `Samsung 990 Pro 1 TB NVMe M.2` | Workstation, server |
| Hard Disk Drive | `Seagate Exos 4 TB 7200 RPM` | NAS, server |
| CPU | `Intel Xeon Silver 4314` | Server |
| GPU | `NVIDIA RTX A4000 16 GB` | Workstation |
| Network Card | `Intel X710-DA2 10 GbE` | Server |
| RAID Controller | `Broadcom MegaRAID 9560-8i` | Server |
| Power Supply | `Dell 750 W Hot-Plug PSU` | Server |

### Creating a Component

Navigate to **Inventory → Components → Add**:

| Field | Required | Description |
|---|---|---|
| **Name** | Yes | Unique model name (e.g. "16 GB DDR4-3200 SODIMM") |
| **Manufacturer** | Yes | Hardware vendor (e.g. "Crucial", "Samsung") |
| **Category** | Yes | Must be a category with `applies_to` set to `component` |
| **Part Number** | No | SKU or manufacturer part number |
| **EAN** | No | Barcode for scanning |
| **Specs** | No | JSON dictionary of technical properties |
| **Min Qty** | No | Alert threshold for low-stock warnings |
| **Allow Overallocate** | No | If enabled, permits allocation beyond available stock |
| **Supplier** | No | Preferred vendor for re-ordering |
| **Tenant** | No | Leave blank for global, set for tenant-private catalogue |
| **Notes** | No | Additional details |

#### Specs Field

The `specs` field stores structured technical data as JSON. Use it for
filtering and reporting:

```json
{
    "capacity_gb": 16,
    "type": "DDR4",
    "speed_mhz": 3200,
    "form_factor": "SODIMM",
    "cas_latency": 22,
    "voltage": 1.2
}
```

### Managing Stock per Location

Components are stocked at specific **Locations**. Each location can hold its
own quantity of the same component model:

Navigate to **Inventory → Component Stock → Add**:

| Field | Required | Description |
|---|---|---|
| **Component** | Yes | The catalogue component being stocked |
| **Location** | Yes | The physical location / warehouse room |
| **Quantity** | Yes | Number of units at this location |

#### Stock Summary

The component detail page shows:

| Metric | Formula | Description |
|---|---|---|
| **Total Stock** | `SUM(stocks.qty)` | All stock across all locations |
| **Allocated** | `SUM(allocations.qty WHERE deleted_at IS NULL)` | Tracking total for all active allocations; source-backed rows are already reflected in stock |
| **Available** | `total_stock - target_only_allocated` | On-hand stock minus allocations that did not already deduct a source pool |

The `available_stock` property is computed from these aggregates and updated
automatically as allocations change.

### Allocating Components to Parent Assets

When you physically install a component into an asset (e.g. putting a new SSD
into a laptop), record the allocation in ITAMbox:

Navigate to **Inventory → Component Allocations → Add**:

| Field | Required | Description |
|---|---|---|
| **Component** | Yes | The component model being installed |
| **Qty** | Yes | How many units (e.g. `2` for a dual-rank RAM kit) |
| **Assigned Asset** | No* | The parent asset receiving the component |
| **Assigned Holder** | No* | The holder if allocated directly to a person |
| **Assigned Location** | No* | The location if allocated to a room |
| **Notes** | No | Slot position, installation notes |

\* Exactly ONE of Assigned Asset, Assigned Holder, or Assigned Location must be set.

#### Automated Stock Control

```mermaid
graph TD
    A[Component Check-out<br/>qty=2, from_location=Warehouse A] --> B[Stock Deduction]
    B --> C{Sufficient stock?}
    C -->|No| D[ValidationError<br/>Allocation rejected]
    C -->|Yes| E[Warehouse A stock<br/>decremented by 2]
    E --> F[Component appears on<br/>parent asset detail page]

    G[Delete Allocation] --> H[Stock Restoration]
    H --> I[Warehouse A stock<br/>incremented by 2]

    style D fill:#c53030,stroke:#fc8181,color:#fff
    style E fill:#2f855a,stroke:#68d391,color:#fff
    style I fill:#2f855a,stroke:#68d391,color:#fff
```

- **Target-only Allocation create / asset quick-add**: No source warehouse is
  accepted. The active allocation reduces availability without mutating stock.
- **Component Check-out**: Selecting a concrete stock row deducts its location
  atomically and records the source (plus Resource Grant provenance when cross-tenant).
- **On deletion (soft or hard)**: A source-backed check-out restores stock to
  `from_location`; target-only allocations have no stock row to restore.
- The stock adjustment is handled by `adjust_inventory_stock()` in
  `inventory/services.py`, called from both `save()` and `delete()`.

### Sharing a Stock Pool With Another Tenant

A stock pool can be shared with another tenant instead of being copied to it.
The owner opens the component, accessory or consumable **Stocks** list and uses
the row's **Share** action to create a resource grant for one tenant or tenant
group, at either **View** or **View + allocate/consume** level. The grantee then
sees the pool in its own stock surfaces, and every check-out that draws on it
keeps the grant as provenance. Access, deadlines, revocation and the audit trail
are described in [Resource Grants](resource-grants.md).

> [!IMPORTANT]
> Stock adjustments are **synchronous and transactional**. If the stock
> deduction fails (e.g. insufficient quantity), the entire allocation save is
> rolled back. There is no window where stock and allocations are out of sync.

### Stock Alerts

Configure **Min Qty** on a component to trigger low-stock alerts:

1. Set `min_qty` on the component (e.g. `5`).
2. Create an **Alert Rule** of type `Low Stock Alert` targeting consumables
   and components.
3. When total available stock drops to or below `min_qty`, the alert fires.

Combine with [Alert Rules](alerts-and-notifications.md) for automated email
or Slack notifications.

### Over-Allocation

When **Allow Overallocate** is enabled on a component, you can allocate more
units than are physically in stock. This is useful for:

- **Pre-staging**: Recording planned installations before stock arrives.
- **Backordering**: Tracking demand when supply is constrained.

Use with caution — over-allocated components show **negative available stock**.
The dashboard highlights these with a warning badge.

---

## Troubleshooting

### Kits

**"A kit item cannot select more than one target"**

: Each kit item slot can reference exactly one thing (asset type, accessory,
  license, consumable, OR component). For a kit that needs a keyboard AND a
  mouse, create two separate kit items.

**Kit checkout fails with "Insufficient stock"**

: One or more items in the kit lack sufficient quantity at the source location.
  The error message names the specific item and how many units are missing.
  Replenish stock or choose a different source location with adequate inventory.

**Checked-out kit items don't appear on the holder's profile**

: Verify the checkout completed successfully (check the kit's change log).
  Each item type appears in a different section — assets under "Assigned
  Assets", accessories under "Assigned Accessories", licenses under "License
  Seats". There is no unified "kit" view post-checkout.

**Can I return an entire kit at once?**

: No. Kit checkout creates separate assignments for each item. Return each
  item individually through its respective workflow: check in assets, unassign
  accessories, release license seats. Consumables cannot be "returned" — they
  are consumed permanently.

### Components

**"Cannot allocate — insufficient stock"**

: The requested quantity exceeds available stock at the source location.
  Either reduce the quantity, choose a different source location, or enable
  `allow_overallocate` on the component to permit negative stock.

**Stock count doesn't match physical inventory**

: Check for deleted allocations. Soft-deleted allocations restore stock to
  `from_location`, but hard-deleted allocations also trigger restoration via
  the `delete()` method. If counts are off, run a stock reconciliation:
  ```
  python manage.py reconcile_component_stock
  ```

**Component appears on wrong asset**

: Allocation targets are immutable after creation. Check in or otherwise end
  the incorrect allocation using its supported workflow, then create a new
  allocation for the correct asset. A source-backed allocation returns its
  quantity to the source stock location when ended; a target-only allocation
  does not change a stock row.

**Low-stock alert not firing**

: Verify the component's `min_qty` is set and that an active alert rule of
  type `Low Stock Alert` exists. The alert engine runs daily via `django-q2`;
  check that the scheduler is running. See the
  [Alerts & Notifications guide](alerts-and-notifications.md) for
  troubleshooting alert delivery.
