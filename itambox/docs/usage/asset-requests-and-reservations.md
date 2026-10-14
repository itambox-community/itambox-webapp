# Asset Requests & Reservations

ITAMbox lets users request hardware and reserve a specific asset for a date
window. Application validation and a PostgreSQL exclusion constraint reject
overlapping pending or active reservations for the same asset.

---

## Asset Requests

An **Asset Request** is a self-service request. Users browse the
catalogue and submit requests for specific assets, asset types, components,
accessories, or consumables. Managers approve or deny them. Receiving stock and
handing it over are separate operations: receipt allocates stock to a request;
claim or checkout records the assignment or inventory issue. A manual completion
is an explicit exception and creates neither an assignment nor a stock booking.

An asset is eligible for issuance only when it has Deployable status, no active
assignment, and no active disposal record. Assets in service, under repair, lost,
or disposed cannot be issued. Deployed status is lifecycle-managed: checkout
creates an active assignment and sets the asset to Deployed; check-in closes the
assignment and restores the prior eligible status. Do not set or clear Deployed
through a manual status edit.

### Creating a Request

Navigate to **Operations → Procurement → Requests** and click **Create Asset Request**. Fill in:

| Field | Required | Description |
|---|---|---|
| **Request Category** | No | Which item field the form shows: **Asset Type (General Model)** (default), **Specific Asset (by Tag)**, **Component**, **Accessory**, or **Consumable** |
| **Asset Type**, **Asset**, **Component**, **Accessory**, or **Consumable** | Yes | The item you are requesting — exactly one per request, entered in the field selected by **Request Category** |
| **Quantity** | No | How many units (default 1). Must be > 0; not shown for a **Specific Asset (by Tag)** request |
| **Request For** | No | Who the request is for: **Myself** (default), **Asset Holder**, **Location**, or **Asset**. This field and the **Assigned ...** fields below are shown only for staff or users with the `add_delegated_assetrequest` permission |
| **Assigned User** | No | The Asset Holder the item is requested for — shown and required when **Request For** is **Asset Holder** |
| **Assigned Location** | No | The Location the item is requested for — shown and required when **Request For** is **Location** |
| **Assigned Asset** | No | The parent Asset the item is requested for — shown and required when **Request For** is **Asset** |
| **Notes** | No | Justification, urgency, or other context for the approver |

> [!IMPORTANT]
> You must select exactly one item per request (asset, asset type, component,
> accessory, or consumable). A database-level check constraint
> (`exactly_one_requested_category`) enforces this — combining a laptop AND a
> monitor in one request is not possible. Submit a separate request for each item
> you need; to request several units of one asset type as one tracked batch, see
> [Bulk Requests (Group Requests)](#bulk-requests-group-requests).

### Validation Gating

When creating a request, ITAMbox enforces several rules:

- The requested item must be **marked as requestable** (assets must have
  `is_requestable=True`; asset types must have `requestable=True`).
- The requested asset must satisfy the issuance rule above: **Deployable** status,
  no active assignment, and no active disposal record.
- **Duplicate detection**: a pending, approved, or procurement request for the
  same item and assignee prevents another open request.
- Quantity must be greater than zero.
- If both a specific asset and an asset type are selected, the asset must
  belong to that type.

### Approval Workflow

Requests follow a state machine validated by the application. The supported workflow paths are:

```mermaid
stateDiagram-v2
    [*] --> Pending : User submits request
    Pending --> Approved : Approver approves
    Pending --> Denied : Approver denies
    Pending --> Cancelled : Requester cancels
    Pending --> Fulfilled : Direct fulfilment (auto)
    Approved --> Fulfilled : Claim / checkout, or explicit manual completion
    Approved --> Approved : Bulk receipt allocates stock; handover pending
    Approved --> Cancelled : Requester or approver cancels
    Approved --> Procurement : Item needs purchasing
    Procurement --> Cancelled : Cancelled
    Procurement --> Approved : PO receipt allocates stock; handover pending
    Denied --> [*]
    Fulfilled --> [*]
    Cancelled --> [*]

    note right of Procurement : Optional state for items\\nnot in stock — routes to\\nPurchase Order creation
```

When approving a request, the approver picks the **Stock Location** the item
will be pulled from (required for components, accessories, and consumables).
ITAMbox records it as the request's **Source Stock Location** and uses it as
the preferred source location for stock issued during fulfilment.

#### Status Reference

| Status | Meaning | Next Steps |
|---|---|---|
| **Pending** | Submitted, awaiting review | Approve, deny, or cancel |
| **Approved** | Cleared for fulfilment; an allocated item may still await handover | Allocate stock, claim/check out, or move an unallocated request to Procurement |
| **Procurement** | Needs purchasing (not in stock) | Receive against a Purchase Order, then hand over the allocated items |
| **Denied** | Rejected by approver | Terminal — no further action |
| **Fulfilled** | Request closed; the completion label distinguishes a recorded handover, a manual exception, or unverified historical evidence | Terminal — inspect the completion evidence, not the status alone |
| **Cancelled** | Withdrawn by requester or approver | Terminal |

#### Completion paths and evidence

| Path | Resulting request status | Assignment / stock effects | What the operator sees |
|---|---|---|---|
| Claim | Fulfilled | Books an asset checkout or an inventory issue for the requested units | Handover recorded |
| Asset checkout with automatic reconciliation | Fulfilled for one matching open unit | The new assignment is the recorded handover; a group remains open while units remain open | Handover recorded for that unit |
| Complete manually | Fulfilled | **No assignment and no stock booking** | Manually completed; no handover booked, with the required reason |
| Bulk stock receipt | Approved | Creates and allocates an asset; does not check it out | Allocated, awaiting handover |
| Purchase Order receipt | Approved for received/allocated request units | Creates assets or adds inventory stock; does not hand it over | Approved, awaiting handover; linked Purchase Order remains visible |

To use the manual exception, open an approved request and choose **Complete
manually...**. Supply a reason and explicitly confirm that no assignment or stock
booking will be generated. Use this only for a deliberate exception, such as work
completed outside ITAMbox. A request closure is not a substitute for an inventory
transaction or a custody record. The reason, acting user and completion time are
auditable; existing decision notes are retained.

List and detail labels distinguish the completion paths. Older records without
verifiable completion metadata show **Handover evidence not verified**. This does
not prove that a delivery never happened; it means ITAMbox cannot establish that
request's completion path from the available evidence. Editing a note or finding
an asset currently assigned to someone does not establish the original handover.
A later return does not undo evidence of a recorded historical handover.

For groups, inspect the unit-level outcomes: some units can have recorded
handovers while others were manually completed, cancelled, denied, or remain open.
A group summary does not imply that every originally requested unit was delivered.
These meanings do not change between single-tenant, tenant-group and
All-accessible views.

#### Auto-Approval for Low-Risk Items

Automatic approval is disabled by default. When explicitly configured,
accessory and consumable requests at or below their quantity thresholds are
**automatically approved** on creation if sufficient stock exists:

| Item Type | Example Threshold | Behaviour |
|---|---|---|
| Accessory | ≤ 3 units | Auto-approved if `available >= qty` |
| Consumable | ≤ 5 units | Auto-approved if `available >= qty` |

Set `ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS` to a JSON object such as
`{"accessory": 3, "consumable": 5}`. Omit a category or set its threshold to
`0` to prevent positive-quantity requests in that category from being approved
automatically. When the setting itself is unset (the default), **no request is
auto-approved and all requests remain pending** for manual approval. The legacy
`REQUISITION_AUTO_APPROVAL_THRESHOLDS` name is accepted through 1.x with a
startup deprecation warning.

> [!IMPORTANT]
> Configuration errors do not fail soft. Invalid JSON, unsupported keys, or
> boolean/negative thresholds abort startup — validate the JSON syntax before
> deploying a change. Auto-approval is **advisory only** — it reserves no
> stock. Capacity is enforced at fulfilment time, so a generous threshold can
> only over-approve, never over-allocate.

### Bulk Requests (Group Requests)

To request several units of one asset type as a single tracked batch, use a
**group request**. The request form has no "Is Group" field — ITAMbox creates
the group automatically from the quantity:

1. Navigate to **Operations → Procurement → Requests** and click **Create
   Asset Request**.
2. Select an **Asset Type** and set **Quantity** to a value greater than 1.
3. Submit the request. ITAMbox creates the **parent request** (marked as a
   group; it keeps the asset type and the total quantity) and one **child
   request per unit**, each with quantity 1 and the same **Request For**
   target and notes.
4. Open the parent request to inspect its **Sub-Requests** card: every unit is
   listed with its own **Req #**, **Status**, **Assigned To**, and **Allocated
   Asset**. Units that still need a concrete item show **Not set** in the
   **Allocated Asset** column.

The automatic split applies to **Asset Type** requests only. Component,
accessory, and consumable requests stay a single request with the requested
quantity, even when the quantity is greater than 1.

For a multi-unit Asset Type sent to procurement, ITAMbox creates one Purchase
Order Line and reserves each unit for its child request. Each delivered unit is
allocated to one child; a partial receipt approves only the children that
received an asset, and the parent stays in **Procurement** until every child
has an asset.

Component, accessory, and consumable requests keep their requested quantity on
a single request. A receipt attributes the delivered quantity to the oldest
open request on the line first and approves that request only once its full
quantity has arrived; units received beyond the reserved demand stay as free
stock. Once a request has been sent to procurement, the request detail page
shows the received and outstanding quantity for its purchase order, and
cancelling the request releases its still-unreceived reservation while the
delivered quantity stays on record.

Group requests are useful for:

- **Department refresh**: request 20 laptops of a specific asset type, tracked
  as a batch.
- **Onboarding wave**: request the standard laptop model for several new
  starters as one batch.
- **Event staging**: temporary equipment pools for conferences or training rooms.

### Notifications on Status Changes

ITAMbox fires **Event Rules** when a request changes status. Configure these
under **Extras → Event Rules** to send notifications via email, Slack, Teams,
or webhooks. Common event hooks:

| Trigger | Typical Action |
|---|---|
| Request created (status = pending) | Notify the approval queue |
| Request approved | Notify the requester — "Your request is approved" |
| Request denied | Notify the requester with `response_notes` |
| Request fulfilled | Notify that the request was closed; distinguish recorded handover from manual completion before claiming delivery |
| Request moved to procurement | Notify the procurement team |

The `responded_by` and `response_notes` fields are populated when an
administrator acts on a request, providing an audit trail of who approved or
denied it and why.

### Permissions

| Permission | Who Needs It |
|---|---|
| `assets.add_assetrequest` | All users (self-service) |
| `assets.add_delegated_assetrequest` | Managers requesting on behalf of others |
| `assets.approve_assetrequest` | Approvers |
| `assets.fulfill_assetrequest` | Fulfilment / IT operations staff |
| `assets.view_assetrequest` | Anyone who should see the request queue |

Request actions follow the effective permission for each request's tenant. Non-staff users with `assets.approve_assetrequest` can see and use approval, denial, and cancellation actions for covered requests; users with `assets.fulfill_assetrequest` can see and use fulfilment actions, including recording the handover of an allocated asset or request unit from the request detail page. The claim wording stays self-service for the requester or the assigned user and reads as a handover for everyone else. Requesters can still cancel their own requests, and requesters or assigned users can still claim them. Bulk receipt is offered when the page holds a receivable request, including an approved group whose request units are approved; malformed or repeated selections report a message on the request list instead of an access-denied page. Either action permission also allows access to the request list and detail pages for the tenants it covers.

---

## Asset Reservations

An **Asset Reservation** books a specific physical asset for a holder within a
defined date window. Use it for:

- **Project assignments**: reserve a high-end workstation for a 3-month
  rendering project.
- **Training rooms**: book laptops for a week-long class.
- **Conference & event equipment**: reserve AV gear, projectors, and demo
  machines.
- **Seasonal staff**: pre-book equipment for temporary workers before their
  start date.

### Creating a Reservation

Navigate to the asset's detail page and click **Reserve**, or go to
**Assets → Reservations → Add**. Fill in:

| Field | Required | Description |
|---|---|---|
| **Asset** | Yes | The specific asset to reserve |
| **Reserved For** | No | The AssetHolder who will use the asset |
| **Start Date** | Yes | First day of the reservation (inclusive) |
| **End Date** | Yes | Last day of the reservation (inclusive) |
| **Status** | Yes | Editable reservation status; date changes and checkout/check-in do not update it automatically |
| **Purpose** | No | Brief reason (e.g. "Q3 video editing project") |
| **Notes** | No | Additional terms or conditions |

### Reservation Lifecycle

Reservation status is an editable record field, not an automated lifecycle.
Changing dates, checking out an asset, or checking it in does not transition the
reservation status. Update it separately to match the operational outcome.

#### Status Reference

| Status | Meaning |
|---|---|
| **Pending** | Counts as occupied for overlap checks. Status is not automatically changed when dates arrive. |
| **Active** | Counts as occupied for overlap checks. Checkout does not automatically set this status. |
| **Fulfilled** | Does not participate in the overlap constraint. Check-in does not automatically set this status. |
| **Cancelled** | Does not participate in the overlap constraint. Set by an operator. |

### Double-Booking Prevention (btree_gist)

ITAMbox prevents overlapping reservations at **two layers**:

#### 1. Database-Level Exclusion Constraint

A PostgreSQL `ExclusionConstraint` using the `btree_gist` extension makes
double-booking **impossible at the row level**:

```sql
-- Simplified representation of the constraint
EXCLUDE USING gist (
    asset WITH =,
    daterange(start_date, end_date, '[]') WITH &&
)
WHERE status IN ('pending', 'active') AND deleted_at IS NULL
```

Key design decisions:

- The date range is **inclusive on both ends** (`[]`): `end_date` is the last
  day the asset is held. Two reservations that share a boundary day **conflict**
  — you must leave at least one clear day between bookings.
- A **one-day reservation** has `start_date == end_date`.
- Only `pending` and `active` reservations participate in the constraint.
  Fulfilled and cancelled reservations do not block the calendar.
- Soft-deleted reservations are excluded — deleting a reservation releases its
  window immediately.

#### 2. Python-Level Validation (Defence-in-Depth)

The `AssetReservation.clean()` method performs the same overlap check in
Python so the UI can show a user-friendly error message before the database
rejects the write.

> [!IMPORTANT]
> The `btree_gist` extension must be enabled in your PostgreSQL database:
> ```sql
> CREATE EXTENSION IF NOT EXISTS btree_gist;
> ```
> ITAMbox migrations handle this automatically, but if you're restoring to a
> fresh database, ensure the extension is available.

### Checking for Conflicts

Use the reservation form and its validation to check for conflicts. The REST
API exposes the reservation collection and standard filtering; it does not
provide a separate `check_overlap` action or endpoint. Both form validation and
the database constraint check overlaps when a reservation is saved.

### Checkout Integration

Reservations integrate with ITAMbox's checkout workflow:

1. Create the reservation with the intended asset, holder and dates.
2. If your workflow uses statuses such as `active`, update the reservation
   status explicitly; arrival of the start date and checkout do not change it.
3. When the device is returned, check it in through the assignment workflow.
   Update the reservation status separately; check-in does not fulfill it.

The checkout guard checks for a conflicting pending or active reservation
against the current checkout target. It does not guarantee that a future loan
window is reserved by an assignment.

### Reservation Calendar View

The asset detail page includes a reservation timeline showing:

- All active and pending reservations for the asset.
- Colour-coded status indicators (green = active, amber = pending).
- The reserved holder's name and purpose for each booking.

### Soft Deletion

Deleting a reservation (soft delete) immediately releases its date window.
The reservation record is preserved in the database with `deleted_at` set,
but it no longer participates in overlap checks. This means:

- You can delete a stale reservation and immediately create a new one for
  the same dates.
- Deleted reservations are excluded from the calendar view by default but
  remain queryable by administrators.

---

## Troubleshooting

### Requests

**"You already have a pending or approved request for this item"**

: The duplicate detection check found an existing open request for the same
  item with the same assignee. Either wait for the existing request to be
  resolved, cancel it, or change the assignee.

**"The asset is not requestable"**

: The asset's `is_requestable` flag is `false`, or its status type is not
  `deployable`. An administrator can enable requestability on the asset's
  edit page, or you can request by asset type instead.

**"Invalid state transition"**

: The status change you attempted is not allowed by the state machine. For
  example, a denied request cannot be re-approved — create a new request
  instead. A fulfilled request cannot be cancelled — unassign the item first.

**Auto-approval is not triggering**

: Check that `ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS` is configured with
  positive thresholds. Auto-approval only applies to accessories and
  consumables, and only when sufficient stock is available. Asset and
  component requests always require manual approval. The setting governs
  automatic approval only: requests can always be sent to procurement
  regardless of it.

### Reservations

**"An active or pending reservation already exists"**

: The date window overlaps with an existing reservation for the same asset.
  Check the reservation calendar on the asset detail page to see conflicting
  bookings. Either choose different dates, a different asset, or cancel the
  conflicting reservation first.

**Reservation was rejected by the database but passed the form check**

: This is a race condition — another reservation was created between the
  form validation and the database write. This is extremely rare but possible
  under high concurrency. Refresh the page and try again; the calendar view
  will now show the conflicting reservation.

**"btree_gist extension is not available"**

: The PostgreSQL extension is missing. Run:
  ```sql
  CREATE EXTENSION IF NOT EXISTS btree_gist;
  ```
  as a superuser. This is a one-time setup per database cluster.

**A cancelled reservation still blocks my dates**

: Cancelled and fulfilled reservations are excluded from the overlap
  constraint. If you're seeing a conflict, the blocking reservation is
  likely still `pending` or `active`. Double-check its status. If a
  reservation appears to be cancelled but still blocks, it may only be
  soft-deleted — soft-deleted rows are also excluded, so verify the status
  field directly in the database.
