# Alerts & Notifications — how they work

ITAMbox provides an automated alerting system that evaluates threshold conditions
daily and dispatches notifications through your configured channels. The system
helps you stay ahead of low stock, expiring warranties and licenses, upcoming
end-of-life dates, and overdue audits — without having to check dashboards manually.

> **Disclaimer:** Alerts are evaluated once per day by background workers
> (`django-q2`). They are not real-time; the worst-case delay between a condition
> being met and a notification being sent is the next daily evaluation run.
> Evaluation is state-based, not event-based: a delayed or missed run is not
> replayed, and the next successful run evaluates the full current state.

---

## Architecture overview

```mermaid
graph TD
    A[Daily django-q2 Scheduler] --> B[AlertRule Engine]
    B --> C{Condition met?}
    C -->|Yes| D[Create / Update AlertLog]
    C -->|No| E[Auto-resolve stale AlertLogs]
    D --> F{Rule muted?}
    F -->|No| G[NotificationChannel dispatch]
    F -->|Yes| H[Log only — skip dispatch]
    G --> I[Email / Slack / Teams]
    D --> J[Alert Center UI]
    H --> J
    E --> J

    style A fill:#4a5568,stroke:#718096,color:#fff
    style B fill:#2d3748,stroke:#4a5568,color:#fff
    style C fill:#2d3748,stroke:#4a5568,color:#fff
    style D fill:#c53030,stroke:#fc8181,color:#fff
    style E fill:#2f855a,stroke:#68d391,color:#fff
    style F fill:#2d3748,stroke:#4a5568,color:#fff
    style G fill:#2b6cb0,stroke:#63b3ed,color:#fff
    style H fill:#744210,stroke:#f6e05e,color:#fff
    style I fill:#2b6cb0,stroke:#63b3ed,color:#fff
    style J fill:#553c9a,stroke:#b794f4,color:#fff
```

The whole pipeline lives in `extras/models.py` (models `AlertRule`,
`NotificationChannel`, and `AlertLog`), `extras/tasks/alerts.py` (evaluation,
dispatch scheduling, and pending-dispatch recovery) and `core/events.py` (the
channel boundaries and typed delivery results). Evaluation is triggered by a
`django-q2` schedule, not by user actions.

---

## Alert Rules

An **Alert Rule** defines a threshold or time-horizon condition. When the
condition is met, the rule fires — creating an entry in the Alert Log and
optionally dispatching notifications to its attached channels.

### Anatomy of an alert rule

| Field | Type | Description |
|---|---|---|
| **Name** | String (required) | Display name, e.g. _"Low Stock — Patch Cables"_ |
| **Description** | Text | Optional notes explaining the rule's intent |
| **Alert Type** | Choice (required) | What the rule monitors (see table below) |
| **Threshold Value** | Positive integer | Meaning depends on alert type — a count or a day horizon |
| **Severity** | Choice | `info`, `warning`, `critical` — controls badge colour and sorting |
| **Is Active** | Boolean | Inactive rules are skipped entirely during evaluation |
| **Is Muted** | Boolean | Muted rules still create AlertLog entries but send **no** channel notifications |
| **Renotify Interval Days** | Positive integer | How often to re-dispatch while an alert remains unresolved (`0` = notify once) |
| **Channels** | M2M → `NotificationChannel` | Where to send notifications when the rule fires — only enabled channels in the rule's own scope receive a dispatch |

### Alert types

| Alert Type | Choice value | Threshold meaning | Example |
|---|---|---|---|
| Low Stock Alert | `low_stock` | Minimum unit count | Notify when patch cables drop below 20 units |
| Upcoming EOL Planning | `upcoming_eol` | Days before EOL date | Warn 90 days before a hardware model reaches end-of-life |
| License Expiry Alert | `license_expiry` | Days before expiry | Alert 30 days before a software license expires |
| Renewal Due Alert | `renewal_due` | Days before renewal date | Remind 14 days before a SaaS contract renews |
| Warranty Expiry Alert | `warranty_expiry` | Days before expiry | Flag devices whose warranty lapses in 60 days |
| Audit Overdue | `audit_overdue` | Fallback number of days since an asset's last audit | Flag assets overdue for a physical audit |

> [!IMPORTANT]
> For date-horizon alert types (`upcoming_eol`, `license_expiry`, `renewal_due`,
> `warranty_expiry`), the threshold is **days before the event**. For
> `audit_overdue`, the threshold is a fallback audit interval in days for assets
> whose category has no audit cadence. A category's `audit_interval_months`
> takes precedence; assets never audited are included when overdue (using their
> creation date for a category cadence).
> `low_stock` is an absolute quantity threshold.

For `low_stock`, an item's own minimum quantity (`min_qty`) takes precedence
over the rule's threshold value when it is set; the rule's threshold is the
fallback for items without their own safety limit.

### Severity levels

| Severity | Badge colour | Use for |
|---|---|---|
| `info` | Blue | Low-urgency heads-ups, routine notifications |
| `warning` | Amber / yellow | Action is needed soon but there is time |
| `critical` | Red | Immediate attention required — missed SLA, stock-out risk |

Warning is the default severity for new rules.

### Creating an alert rule (example)

From the ITAMbox UI (Extras → Alert Rules), click **Add Alert Rule** and fill in:

- **Name:** `Low Stock — Cat6 Cables`
- **Alert Type:** `Low Stock Alert`
- **Threshold Value:** `10`
- **Severity:** `warning`
- **Renotify Interval Days:** `7`

This rule fires whenever the on-hand quantity of Cat6 cables (tracked as a
consumable or component in the inventory) falls to 10 or fewer units. Once
fired, it re-notifies every 7 days until the stock is replenished above the
threshold.

### Muting rules

Set `is_muted = true` when you are aware of a condition and want the alert
**visible in the Alert Center UI** but do **not** want channel notifications
(email, Slack, Teams) to be sent. This is useful during planned maintenance
windows or when you are already working on a fix.

Muted rules:

- Still create `AlertLog` entries on every evaluation where the condition holds.
- Do **not** trigger channel dispatch.
- Can be unmuted at any time to resume notifications.

### Renotification intervals

| Setting | Behaviour |
|---|---|
| `0` | Fire once — never send a repeat notification for the same alert |
| `N` (> 0) | Re-dispatch channel notifications every N days while the alert stays in `active` or `acknowledged` status |

The renotification clock starts from `AlertLog.last_notified_at`. Each
evaluation checks whether enough days have passed since the last dispatch; if
the condition still holds **and** the interval has elapsed, notifications are
re-sent and `last_notified_at` is updated.

### Daily evaluation schedule

Alert rules are evaluated by a `django-q2` scheduled task, typically configured
to run once per day (e.g. at 08:00 UTC). The evaluation engine:

1. Loads all **active** rules for all tenants (system-scoped, not user-scoped).
2. For each rule, queries the target data (inventory levels, expiry dates, audit
   statuses) scoped to the rule's tenant.
3. Compares the current state against the threshold.
4. Creates or updates `AlertLog` entries (see [Alert Log](#alert-log) below).
5. If the rule is not muted and has at least one attached, enabled channel in
   the rule's own scope, dispatches notifications through those channels.

The `Daily Alert Rule Evaluation` schedule row is registered by the `extras` app
from `post_migrate` (idempotently — repeated `migrate` runs or restarts never
create duplicates), so a standard `qcluster` deployment needs no manual cron
entry. Failed evaluation runs surface in the queue's failed-task list; a failed
run is never silently swallowed. Runs that are delayed or missed (worker down,
deployment window) are **not backfilled**: the next successful run evaluates the
full current state.

> [!IMPORTANT]
> Because evaluation runs in a background worker context (no request user), the
> engine uses an **unscoped queryset manager** to query across all tenants. Normal
> UI views remain tenant-scoped. This is an implementation detail — it means
> alert evaluation works correctly even when no user is logged in.

### Delivery semantics (single-attempt policy)

Channel delivery follows an explicitly **single-attempt, best-effort policy**
with a terminal failed state — there is **no automatic retry**:

- Each planned dispatch runs **exactly one attempt** per attached channel.
- The dispatch claim is atomic: when two parallel evaluations plan a dispatch
  for the same alert state, exactly one claim wins and the other backs off
  without sending — a repeated or overlapping evaluation cannot produce a
  duplicate delivery for the same alert. A claimed dispatch owns its alert for
  a claim lease (15 minutes): a parallel evaluation never re-dispatches an
  in-flight attempt, while a crashed run's claim is recovered by a later
  evaluation once the lease has expired. Completion metadata is fenced to the
  owning delivery id, so a superseded run cannot overwrite a newer result.
- The per-channel outcome is recorded as a typed result:
  `success`, `retryable` (transient-class failure, e.g. SMTP timeout or HTTP 5xx),
  or `terminal` (permanent failure, e.g. rejected credentials or 4xx response),
  together with the delivery identifier, attempt timestamp, the typed error
  class when the boundary supplied one, and a safe user-visible message when
  the boundary declared one.
- A dispatch whose callback crashed before completing is recorded as
  `__dispatch__: terminal` with outcome `failed` — it is **not** retried.
- A delivery is never reported as successful unless a channel boundary returned
  an explicit success: unknown or malformed results are recorded as failures,
  and a dispatch that was scheduled but never completed stays visible as
  `pending`.
- **No manual redelivery** exists: there is no UI action and no API endpoint to
  re-send a failed delivery, and nothing in the UI advertises one.
- Re-notification is a *separate, deliberate* feature, not a retry: an alert
  whose delivery failed stays in the inbox, and if the rule's renotification
  interval is configured (or you use **Run now**), a **fresh** dispatch attempt
  with a new delivery identifier starts on the next evaluation.

#### Filtering on delivery outcome

The Alert Log list (and the read-only REST API) supports a **Delivery Outcome**
filter so operators can find alerts that fired but were **not** delivered:

| Outcome | Meaning |
|---|---|
| `none` | No delivery planned (muted rule, no channels attached, every attached channel disabled, or legacy row) |
| `pending` | Dispatch scheduled but never completed (e.g. worker stopped mid-run) |
| `delivered` | At least one channel delivered successfully |
| `failed` | Every attempted channel failed (typed failure stored per channel) |

Each log also exposes `delivery_attempts`, `last_delivery_id`, and
`last_delivery_error` (the typed error class of the most recent failure) so the
failure state is queryable in-product — logs alone are never the only source of
truth.

#### Operator upgrade note

Upgrading from an earlier version preserves all existing `AlertLog` rows and
their `delivery_status` payloads verbatim; the new `delivery_outcome` column is
derived once from the existing payloads during the migration. Historical
deliveries keep their original per-channel records; no attempt history is
fabricated for deliveries made before the upgrade.

The Beta→Stable promotion itself adds no schema migration — it changes only the
declared grade. Existing rules, channels, alert history, and delivery state are
preserved exactly as they are.

---

## Notification Channels

A **Notification Channel** is a destination for alert dispatches. Channels are
configured once and can be attached to multiple alert rules via a
many-to-many relationship.

### Channel types

| Type | Choice value | Transport | Typical config |
|---|---|---|---|
| Email | `email` | SMTP (global) | `recipients` — SMTP settings come from global `EmailSettings`, **not** per-channel config |
| Slack | `slack` | Incoming Webhook | `webhook_url` |
| Microsoft Teams | `teams` | Incoming Webhook | `webhook_url` |
| In-App | `in_app` | Built-in | No config needed — alerts appear in the Alert Center bell icon. An optional `recipient_users` list can target specific users; each recipient must be inside the channel's scope (tenant members, or staff for a platform-wide channel) |

### Configuring an email channel

1. Go to **Extras → Notification Channels** and click **Add**.
2. Set **Channel Type** to `Email`.
3. Enter the addresses in **Recipient email addresses** (comma- or
   newline-separated). The form builds the stored configuration for you.
   The following shows the channel's stored configuration shape, not a form field:

```json
{
    "recipients": ["it-team@example.com", "procurement@example.com"]
}
```

Email delivery uses the **global** SMTP configuration (host, port, credentials,
TLS) from the System Email Settings page, not per-channel SMTP. The channel
config only contributes the recipient list.

> [!IMPORTANT]
> The `config` field is excluded from the change log (`_change_logging_excluded_fields`)
> so SMTP passwords and webhook tokens are never stored in plain-text audit trails.

### Configuring a Slack channel

1. Create an **Incoming Webhook** in your Slack workspace (Slack Admin → Apps → Incoming Webhooks).
2. Copy the webhook URL.
3. In ITAMbox, create a new Notification Channel with **Channel Type** = `Slack`.
4. Paste the URL into **Incoming webhook URL**. This UI form builds the
   stored channel configuration for you; it does not expose a Config JSON field.

```json
{
    "webhook_url": "https://hooks.slack.com/services/T00/B00/xxxxxx"
}
```

### Configuring a Microsoft Teams channel

1. In your Teams channel, add a **Workflows** → **Post to a channel when a webhook request is received** connector.
2. Copy the webhook URL.
3. In ITAMbox, create a channel with **Channel Type** = `Microsoft Teams`.
4. Paste the URL into **Incoming webhook URL**. The UI builds the stored
   configuration; it does not expose a Config JSON field. This is the stored
   configuration shape, not a form field:

```json
{
    "webhook_url": "https://example.webhook.office.com/webhookb2/..."
}
```

### Attaching channels to alert rules

Once channels are configured, open an alert rule and select one or more channels
in the **Channels** multi-select widget. A single rule can notify through
multiple channels simultaneously:

```
Alert Rule: "License Expiry — Adobe CC"
  └── Channels: [Email: IT Admins], [Slack: #licensing-alerts]
```

Every **enabled** channel attached to a rule is consulted when the rule fires —
and only channels in the rule's own scope: a tenant-scoped rule delivers through
its own tenant's channels, a platform-wide rule through platform-wide channels.
The rule form and the REST API reject out-of-scope attachments up front, and a
dispatch never consults a channel outside the rule's scope, so a misconfigured
attachment cannot leak one tenant's alerts into another tenant's destination. A
disabled channel is skipped; if every attached channel is disabled, the alert
records outcome `none` with an explicit reason instead of pretending it
notified.

### Channel dispatch guarantees

| Property | Behaviour |
|---|---|
| Delivery tracking | Each `AlertLog` records the typed per-channel result in `delivery_status` (`success` / `retryable` / `terminal`, with delivery identifier, attempt timestamp, and a safe message) |
| Channel scope | A rule delivers only through channels of its own scope (tenant rule → its own tenant's channels; platform-wide rule → platform-wide channels). Out-of-scope attachments are rejected on save and never dispatched |
| Enabled gate | Disabled channels are never contacted. If every attached channel is disabled, the dispatch records an explicit reason and outcome `none` — never a silent success |
| SSRF protection | Outbound webhook URLs are validated at send time by `request_pinned` — DNS is resolved, the destination IP is pinned, redirects are not followed |
| Delivery model | Synchronous, single-attempt best-effort. Failed channel notifications are **not** retried through `django-q2` or any other queue. A transient failure loses that channel's notification until the next renotify interval (which may be never) |
| Timeout | Webhook requests time out after 10 seconds |

---

## Alert Log

The **Alert Log** is the persistent record of every rule violation. It tracks
the full lifecycle from firing through acknowledgement to resolution.

### Lifecycle states

```mermaid
stateDiagram-v2
    [*] --> active : Rule condition met
    active --> acknowledged : User acknowledges
    acknowledged --> active : Condition re-triggers on next eval
    acknowledged --> resolved : User resolves OR condition clears
    active --> resolved : User resolves OR condition clears (auto-resolve)
    resolved --> active : Same condition fires again (new log created)

    note right of active : unique constraint: one open log\nper rule + target object
    note right of resolved : Resolved logs are exempt\nfrom the uniqueness constraint\nso the condition can legitimately re-fire
```

### Log fields

| Field | Description |
|---|---|
| **Rule** | The parent AlertRule that triggered this log |
| **Subject** | Short summary (e.g. _"Low Stock: Cat6 Cables (3 units remaining)"_) |
| **Message** | Detailed explanation of the violation |
| **Severity** | Copied from the rule at fire time (`info` / `warning` / `critical`) |
| **Target object** | Generic foreign key to the affected entity (consumable, license, asset, audit campaign, etc.) |
| **Status** | `active` → `acknowledged` → `resolved` |
| **Delivery Status** | JSON dict mapping channel PKs to delivery outcomes |
| **Last Notified At** | Timestamp of most recent channel dispatch (drives renotification) |
| **Acknowledged By** / **Resolved By** | Which user performed the action |
| **Resolution Notes** | Free-text description of corrective action taken |
| **Resolved At** | When the alert was closed |

### Deduplication

The database enforces a **partial unique constraint** on `(rule, content_type, object_id)`
for rows with status `active` or `acknowledged`. This means:

- Only **one open alert** can exist for a given rule + target combination.
- If the engine detects the same condition again while an open alert exists, it
  **updates** the existing log (bumps severity, updates the message) rather than
  creating a duplicate.
- Once an alert is **resolved**, the constraint no longer applies — the
  condition can fire again later, creating a fresh `AlertLog`.

### Auto-resolution

When the daily evaluation runs and finds that a previously-violated condition
has cleared (e.g. stock was replenished, warranty was extended), the engine
automatically transitions the corresponding `AlertLog` to `resolved` status.
This happens without user intervention.

### Viewing the alert log

All alert logs are visible in the **Alert Center** page (accessible via the
bell icon in the top navigation bar). The list view supports:

- Filtering by status (`active`, `acknowledged`, `resolved`)
- Filtering by severity
- Filtering by alert rule
- Tenant-scoped viewing (you only see alerts for your current tenant)

Clicking an alert log entry opens a detail view where you can acknowledge,
resolve, or add resolution notes.

---

## Practical examples

### Example 1: Low-stock alert for consumables

**Scenario:** You want to be notified when USB-C cables drop below 15 units so
procurement can re-order before you run out.

1. Create a Notification Channel:

   | Field | Value |
   |---|---|
   | Name | `Procurement Email` |
   | Channel Type | `Email` |
   | Recipient email addresses | `buyer@example.com` |

2. Create an Alert Rule:

   | Field | Value |
   |---|---|
   | Name | `Low Stock — USB-C Cables` |
   | Alert Type | `Low Stock Alert` |
   | Threshold Value | `15` |
   | Severity | `Warning` |
   | Renotify Interval Days | `3` |
   | Channels | `Procurement Email` |

**Result:** Every morning at 08:00, if USB-C cable stock is ≤ 15 units, the
buyer receives an email. If stock stays low, they get a reminder every 3 days.

### Example 2: Warranty expiry dashboard

**Scenario:** Track all devices whose warranty expires within the next 90 days.

- **Alert Type:** `Warranty Expiry Alert`
- **Threshold Value:** `90` (days)
- **Severity:** `Info`
- **Is Muted:** `true`

The rule tracks expiring warranties in the Alert Center without sending emails.
The IT manager reviews the dashboard weekly and proactively extends warranties.

### Example 3: Critical license expiry with Slack escalation

**Scenario:** Adobe Creative Cloud licenses expire in 30 days — notify the
creative team on Slack with critical urgency.

1. Create a Slack Notification Channel with the `#licensing` webhook URL.
2. Create the rule:

   | Field | Value |
   |---|---|
   | Name | `Adobe CC Expiry — Critical` |
   | Alert Type | `License Expiry Alert` |
   | Threshold Value | `30` |
   | Severity | `Critical` |
   | Renotify Interval Days | `1` |
   | Channels | `Slack #licensing` |

**Result:** Starting 30 days before expiry, the `#licensing` channel gets a
daily critical alert until the license is renewed.

---

## Troubleshooting

**Alerts are not firing at all**

: Check that the `django-q2` cluster is running and the scheduled task for alert
  evaluation is configured and not paused. Verify the rule's `is_active` flag is
  set. Inactive rules are skipped entirely.

**I receive no email notifications**

: Verify the Notification Channel is enabled (disabled channels are never
  contacted by dispatch) and that the global SMTP settings are
  correct (host, port, credentials, TLS setting). Check the `delivery_status`
  field on the `AlertLog` — it records per-channel outcomes including error
  messages. Also confirm the rule is not muted (`is_muted = false`).

**Slack / Teams notifications never arrive**

: The webhook URL in the channel's `config` may be expired, mistyped, or
  pointing to a deleted integration. The outbound call resolves the destination
  hostname at send time and pins the resolved IP; redirects are not followed.
  Test the webhook URL with `curl` from the ITAMbox server to rule out network
  issues.

**A rule has channels attached but nothing is delivered**

: Check each attached channel's **Enabled** flag and the rule's scope: a
  disabled channel is never contacted, and only channels of the rule's own scope
  can deliver (a tenant rule delivers through its own tenant's channels, a
  platform-wide rule through platform-wide channels). The alert's delivery
  outcome shows `none` with an explicit reason, and the rule form refuses
  out-of-scope attachments when you save.

**I see duplicate alert log entries**

: The deduplication constraint should prevent open duplicates. If you see
  duplicates, confirm they don't have different `rule` or `content_object`
  values. Resolved logs can coexist with new `active` logs for the same target —
  this is intentional (a condition cleared, then re-occurred).

**An alert resolved itself without my action**

: This is the auto-resolution feature. When the evaluation engine detects that
  the condition no longer holds (stock replenished, warranty extended, etc.), it
  marks the log as resolved automatically. This is expected behaviour.

**Renotifications are not being sent**

: Check `renotify_interval_days` on the rule — a value of `0` means "notify
  once, never again." If the value is > 0, verify that enough time has passed
  since `AlertLog.last_notified_at`. The interval is calendar days, not
  business days. Also confirm the alert log is still in `active` or
  `acknowledged` status — resolved logs are never re-notified.

**The bell icon shows a stale count**

: The unread-alert counter is tied to `AlertLog` rows. Acknowledge or resolve
  open alerts to clear the badge. If the count persists across page reloads,
  check that your tenant scope matches — switching tenants changes which alerts
  are visible.

**Webhook dispatch fails with a network error**

: The outbound call resolves the destination hostname at send time and does not
  follow redirects. Verify the webhook URL is reachable from the ITAMbox server
  and that no firewall or proxy is blocking the pinned destination.
