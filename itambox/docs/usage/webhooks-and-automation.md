# Webhooks & Automation — how they work

ITAMbox can push real-time event notifications to external systems through
**Webhook Endpoints** and **Event Rules**. Together they let you trigger
automations in Slack, Teams, CI/CD pipelines, custom integrations, or any
HTTP-speaking service whenever an asset, license, contract, or other tracked
object is created, updated, deleted, restored, checked out, or checked in.

> **Disclaimer:** Webhook delivery is asynchronous — events are enqueued via
> `django-q2` and dispatched by background workers. The worst-case delay is
> the time it takes the next worker cycle to pick up the task. For instant
> delivery keep your worker pool responsive and monitor the task queue.

> **Stability:** Webhooks and Event Rules are a **Stable** capability; the
> envelope contract below is graded Stable. Delivery itself stays opt-in at the
> object level — nothing is ever sent until you deliberately create and enable a
> Webhook Endpoint and an Event Rule.

---

## Architecture overview

```mermaid
graph TD
    A[Model save / delete] --> B[extras.signals post_save / post_delete]
    B --> C[dispatch_event]
    C --> D[Event row created]
    D --> E[process_event_rules]
    E --> F{Enabled EventRules?}
    F -->|No| G[Event marked processed]
    F -->|Yes| H{Rule conditions met?}
    H -->|No| G
    H -->|Yes| I{Action type?}
    I -->|Webhook| J[_send_webhook]
    I -->|Notification| K[_send_notification]
    J --> L[async_task → django-q2 queue]
    L --> M[send_webhook_task]
    M --> N{Response?}
    N -->|2xx| O[Done ✓]
    N -->|4xx (not 429)| P[Final failure — no retry]
    N -->|429 / 5xx / error| Q{Attempts < retry_count?}
    Q -->|Yes| R[Retry scheduled: backoff, or Retry-After for 429]
    Q -->|No| S[All attempts exhausted]

    style A fill:#4a5568,stroke:#718096,color:#fff
    style C fill:#2d3748,stroke:#4a5568,color:#fff
    style D fill:#2d3748,stroke:#4a5568,color:#fff
    style E fill:#2d3748,stroke:#4a5568,color:#fff
    style J fill:#2b6cb0,stroke:#63b3ed,color:#fff
    style L fill:#553c9a,stroke:#b794f4,color:#fff
    style M fill:#553c9a,stroke:#b794f4,color:#fff
    style O fill:#2f855a,stroke:#68d391,color:#fff
    style P fill:#c53030,stroke:#fc8181,color:#fff
    style S fill:#c53030,stroke:#fc8181,color:#fff
    style R fill:#744210,stroke:#f6e05e,color:#fff
```

The domain pipeline lives in `extras/signals.py` (model receivers),
`extras/services/events.py` (event dispatch and rule processing),
`extras/tasks/webhooks.py` (durable outbound delivery and retries), and
`extras/models.py` (the `WebhookEndpoint`, `EventRule`, and delivery records).
Reusable domain-blind transport contracts remain in `core/events.py`.

---

## Webhook Endpoints

A **Webhook Endpoint** defines *where* and *how* ITAMbox sends HTTP payloads.
It is the reusable, connection-level configuration — URL, authentication
headers, HTTP method, signing secret, and retry policy.

### Creating a webhook endpoint

1. Navigate to **Extras → Webhook Endpoints** and click **Add**.
2. Fill in the form:

| Field | Required | Description |
|---|---|---|
| **Name** | Yes | Unique identifier, e.g. `Slack #it-alerts` or `Zapier Asset Sync` |
| **URL** | Yes | The destination URL. Must be a public, external URL — internal/private addresses are blocked by the SSRF guard. |
| **HTTP Method** | Yes | `POST` (default), `PUT`, `PATCH`, or `GET` |
| **Enabled** | — | Toggle to temporarily pause deliveries without deleting the endpoint |
| **Secret** | No | Shared secret for HMAC-SHA256 payload signing (see [HMAC signature verification](#hmac-signature-verification)) |
| **Payload Preset** | No | UI selector (`Custom`, `Slack Block Kit`, `Microsoft Teams (Adaptive Card)`). It does not change the delivered body: delivery uses the fixed target-kind payload described under [Payload format](#payload-format). |
| **Headers** | No | JSON object of custom HTTP headers (e.g. `{"Authorization": "Bearer xxx"}`) |
| **Retry Count** | No | Maximum retry attempts on delivery failure (default: `3`) |
| **Retry Backoff** | No | Seconds to wait between retries (default: `60`) |
| **Tenant** | Admin only | Scope the endpoint to a specific tenant, or leave blank for system-wide |

!!! warning "SSRF Protection"
    The URL is validated at **save time** and again at **send time**.
    Loopback addresses (`127.0.0.1`, `localhost`), link-local IPs, private
    ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and cloud
    metadata endpoints are **rejected outright**. This is fail-closed: a
    blocked URL is never retried.

### Enabling and disabling an endpoint

Toggle the **Enabled** checkbox on the endpoint form. When disabled:

- Existing Event Rules that reference this endpoint **will not fire** — the
  `_send_webhook` function checks `endpoint.enabled` and returns immediately.
- The endpoint stays in your list and all configuration is preserved.
- Re-enabling it resumes delivery for all linked rules immediately.

Use this to temporarily silence a broken integration without unlinking rules.

### Retry logic

When a webhook delivery fails, the system retries according to the endpoint's
retry policy:

| Scenario | Behaviour |
|---|---|
| **2xx response** | Success — done. |
| **3xx response** (redirect) | **Final failure.** Redirects are never followed (the pinned SSRF-safe transport refuses to re-target a request); the delivery is recorded as failed without retrying — point the endpoint directly at its final location. |
| **429 response** (rate limited) | **Retried** within the retry budget. A valid `Retry-After` header (delta-seconds or HTTP-date, capped at 300 seconds) sets the next attempt's delay; `Retry-After: 0` is honoured as an immediately due retry; without a header the endpoint's normal backoff applies. |
| **4xx response** (400–499, except 429) | **Final failure.** Client errors (bad request, auth failure, not found) are never retried — fix the payload or credentials and re-send manually. |
| **5xx response** (500–599) | Retried up to `retry_count` times, with capped exponential backoff and ±20% jitter. |
| **Connection error** (DNS, timeout, TLS) | Same as 5xx — retried. |
| **SSRF guard rejection** | **Final failure** — blocked URLs are never retried. |

Retries with a positive backoff use a one-shot `django-q2` `Schedule` row.
The first retry starts at `retry_backoff` seconds, then the delay doubles for
each later retry, is capped at 3600 seconds, and receives ±20% jitter (with a
minimum positive delay of one second). A 429 response with a valid `Retry-After`
header schedules the next attempt at the requested delay (capped at 300 seconds)
instead of the computed backoff, even when the endpoint's `retry_backoff` is
`0`; a zero delay retries immediately. A zero-backoff retry is immediately due;
its durable delivery row remains recoverable if broker publication fails. The
schedule means positive delays are honoured even if the worker pool is busy.

> [!IMPORTANT]
> The endpoint target is snapshotted into the durable delivery before enqueue.
> Its `secret` is stored encrypted at rest (`enc$...` ciphertext) and is **never**
> written to the django-q2 payload or retry `Schedule.kwargs` (which are stored
> plaintext). Queue packages contain delivery identity assertions only. Editing
> or rotating an endpoint changes new deliveries, not an already-enqueued
> delivery or its retries. During upgrade, reconstructable endpoint-less legacy
> retry schedules are converted to the same encrypted snapshot/assertion form;
> historical rows without a trustworthy target remain unavailable for redelivery
> and fail closed.

## Delivery tracking, retries and redelivery

Every enqueued webhook has a durable delivery record. Its lifecycle is:

- **pending** — the delivery is queued or its attempt is in progress.
- **success** — the endpoint returned a successful response.
- **failed** — the attempt failed in a retryable way and retries remain.
- **dead** — the retry budget is exhausted, or the failure is terminal.

The record keeps the stable delivery ID, the last attempt number, response code,
safe error classification, attempt timestamps, and the next retry time. The
retry budget is `retry_count`: there is one initial attempt followed by up to
that many retries. Connection failures, server-side failures, and rate limiting
(HTTP 429) are retryable under the shared integration error contract; pending
rate-limited attempts are classified `integration.rate_limited`, and a 429 with
a valid `Retry-After` header schedules the next attempt at the requested delay,
capped at 300 seconds. HTTP 4xx responses other than 429 and SSRF guard
rejections are terminal and are not retried. Retry delays use exponential
backoff beginning at `retry_backoff`, doubling per retry, capped at 3600
seconds, with ±20% jitter (and a minimum delay of one second).

Identity promises: `event_id` identifies the domain event and never changes;
`delivery_id` stays stable across the retries of one delivery; a manual
redelivery creates a new delivery with a new `delivery_id` and links to its
source through `redelivered_from`. The durable record keeps the current attempt
count and the latest outcome — not a per-attempt history.

Delivery history is deliberately **not backfilled**. Records and history begin
when this delivery tracking capability is upgraded; earlier webhook attempts
remain represented only by the information that was already available.

Manual redelivery always creates a new delivery record and leaves the original
status, attempt count, and error unchanged. It is allowed for successful
deliveries as well as failed or dead deliveries, but is refused while a
delivery is pending or has a retry scheduled for the future. A redelivered test
send remains marked as a test send.

Operators can send a test webhook from an endpoint. A test delivery has no
event record and sends the normal v1 envelope with the reserved `event` value
`test` (deliberately outside the six event actions), the endpoint model marker,
and the same minimal `data` metadata (`app_label`, `model_name`) as event
deliveries. It uses the endpoint's normal retry policy and is included in
delivery history.

System-wide endpoint deliveries require platform authorization to view or
operate. Tenant operators see only delivery records for their own tenant;
delivery history from before the upgrade is not exposed as synthetic records.

### Testing a webhook endpoint

Use the **Send test webhook** action on the endpoint to verify connectivity.
The action creates a test delivery record and applies the endpoint's normal
retry policy. For an event-driven test instead:

1. Create a temporary **Event Rule** linked to the endpoint, targeting a
   low-traffic model (e.g. `Tag`) and listening for the `create` event.
2. Create a test object — the webhook fires.
3. Check the `django-q2` worker logs for delivery status:
   ```
   Webhook sent to https://example.com/hook — status 200
   ```
   or
   ```
   Webhook https://example.com/hook returned 403 — not retrying (403 is final)
   ```
4. Delete the test rule and test object.

---

## Event Rules

An **Event Rule** defines *when* a webhook fires. It watches a Django model
(e.g. `Asset`, `License`, `Contract`) for lifecycle events (`create`, `update`,
`delete`, `restore`, `checkout`, `checkin`) and triggers an action — either a
webhook or an in-app notification.

### Creating an event rule

1. Navigate to **Extras → Event Rules** and click **Add**.
2. Fill in the form:

| Field | Required | Description |
|---|---|---|
| **Name** | Yes | Descriptive name, e.g. `Asset Created → Teams` |
| **Model** | Yes | The Django model to watch. Only models that emit events are listed (anything inheriting `ChangeLoggingMixin`). |
| **Events** | Yes | The change types that trigger the rule: any of `create`, `update`, `delete`, `restore`, `checkout`, `checkin`. |
| **Action Type** | Yes | `Webhook` (calls an endpoint) or `Notification` (in-app alert) |
| **Webhook** | Cond. | The Webhook Endpoint to call. The form requires a linked endpoint for a `Webhook` rule and offers `action_config` only as a legacy fallback target. |
| **Conditions** | No | Preserved JSON conditions; authored conditions are read-only and withdrawn for 1.0 (see below) |
| **Action Config** | No | Legacy fallback target settings (`url`, `method`, `headers`, `secret`) when no Webhook Endpoint is linked; it does not define a custom payload template |
| **Payload Preset** | No | UI selector (`Custom`, `Slack Block Kit`, `Microsoft Teams (Adaptive Card)`). It does not define or override the outbound body. |
| **Enabled** | — | Toggle to temporarily pause the rule |
| **Tenant** | Admin only | Scope the rule to events from a specific tenant, or leave blank for system-wide |

> [!IMPORTANT]
> When the action type is `Webhook`, the `Webhook` field takes precedence over
> any `url` in `action_config`. Endpoint URL, method, headers, secret and retry
> settings are snapshotted for each delivery; linked endpoint settings take
> precedence. If the linked endpoint is disabled, the rule
> silently skips firing. Always link to an endpoint — it gives you secret
> encryption, retry policy, and a single place to manage the connection.

### Conditions

Conditions are preserved for readability but are read-only for the 1.0 release.
See [Event rule conditions (withdrawn for 1.0)](#event-rule-conditions-withdrawn-for-10)
for the fail-closed behavior and the upgrade report.

### Event types

| Event | Emitted when |
|---|---|
| `create` | A new object is saved for the first time |
| `update` | An existing object is saved (any field change) |
| `delete` | An object is soft-deleted (`deleted_at` set) |
| `restore` | A soft-deleted object is restored (`deleted_at` cleared) |
| `checkout` | An asset is checked out — an `AssetAssignment` is created |
| `checkin` | An asset is checked in — the active `AssetAssignment` is closed |

These six values are the complete V1 event vocabulary, and they are what the
persisted `Event.ACTION_CHOICES`, the published `core.EventActionChoices` enum,
the rule form, and this documentation all agree on. A change that sets the
soft-delete marker dispatches `delete`, a change that clears it dispatches
`restore`; every other tracked change dispatches `update`. The vocabulary is
open: a minor release may publish an additional value, and consumers should
treat an unrecognized `event` value as opaque rather than failing.

> **Note:** Internal operational models (`Event`, `ObjectChange`,
> `JournalEntry`, `Notification`, `Job`, `ReportGenerationArchive`) are
> excluded from event dispatch to prevent infinite recursion. You cannot
> create event rules that watch these models.

### Tenant scoping

Event rules are tenant-aware. A rule with `tenant = "Acme Corp"` only fires
for events on objects belonging to *that tenant*. A rule with `tenant = None`
(global/system-wide) fires for objects from *any* tenant. This lets you:

- Create per-tenant webhooks (e.g. a Slack channel per customer).
- Create global rules that apply everywhere (e.g. a security audit webhook
  that fires on any tenant's `User` changes).

The tenant check uses the object's **own** tenant (resolved via
`_resolve_instance_tenant_id`), not the ambient tenant context — so a
management command or worker task won't accidentally cross-tenant-dispatch.

---

## HMAC signature verification

When you set a **Secret** on a Webhook Endpoint, every non-Slack/non-Teams
outbound payload includes an `X-Hub-Signature-256` header so your receiver can
verify that the payload originated from ITAMbox and was not tampered with.

### How ITAMbox signs the payload

The signature is an **HMAC-SHA256** hex digest of the JSON request body,
computed with the shared secret:

```python
import hmac, hashlib

signature = hmac.new(
    secret.encode('utf-8'),
    json_body.encode('utf-8'),
    hashlib.sha256,
).hexdigest()
```

The header is set as:

```
X-Hub-Signature-256: sha256=<hex_digest>
```

### How to verify on your receiver

```python
import hmac, hashlib

def verify_signature(request_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify an ITAMbox webhook payload. Returns True if valid."""
    if not signature_header:
        return False
    # Header format: "sha256=<hex>"
    try:
        algo, received_sig = signature_header.split('=', 1)
        if algo != 'sha256':
            return False
    except ValueError:
        return False

    expected_sig = hmac.new(
        secret.encode('utf-8'),
        request_body,  # raw bytes, not decoded
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_sig, received_sig)
```

> [!IMPORTANT]
> Always use `hmac.compare_digest()` (or equivalent constant-time comparison)
> instead of `==` — it prevents timing attacks that can leak the expected
> signature byte by byte.

### Payload format

A standard webhook payload (non-Slack, non-Teams) looks like:

```json
{
    "schema_version": 1,
    "event_id": 4242,
    "delivery_id": "550e8400-e29b-41d4-a716-446655440000",
    "attempt": 1,
    "tenant": 7,
    "event": "create",
    "model": "assets.asset",
    "object_id": 42,
    "timestamp": "2026-07-20T14:30:00+00:00",
    "data": {
        "app_label": "assets",
        "model_name": "asset"
    }
}
```

| Field | Description |
|---|---|
| `schema_version` | The independent webhook wire-contract version. v1 is the integer `1`; it is not the ITAMbox product version. |
| `event_id` | The immutable `Event.pk` for the domain event. It does not change on retry. |
| `delivery_id` | A UUID for one delivery. It remains stable across retries of that delivery. |
| `attempt` | One-based attempt number. The initial send is `1`; each retry increments it. |
| `tenant` | The owning tenant primary key, or `null` for a tenant-less object. It is derived from the object, not ambient request context. |
| `event` | The event action — `create`, `update`, `delete`, `restore`, `checkout`, or `checkin` |
| `model` | Fully-qualified Django model reference (`<app_label>.<model_name>`) |
| `object_id` | The primary key of the affected object |
| `timestamp` | ISO 8601 timestamp of when the event was created |
| `data` | The `event.data` JSON blob (includes at minimum `app_label` and `model_name`) |

Slack and Teams webhooks use fixed platform-specific payloads: Slack receives
the envelope plus a `text` summary; Teams receives a legacy MessageCard-shaped
payload. Neither is a configurable Block Kit or Adaptive Card template. These
platform-specific sends do **not** include the HMAC signature header; their
webhook URLs provide authentication. Do not assume they have the generic v1
envelope's complete field set.

### Envelope version and compatibility

This is webhook envelope **v1**, independent of the ITAMbox product release.
The generic top-level field set is frozen for v1; changing that field set
requires a new integer `schema_version`. The six-value `event` vocabulary is
published and open: a minor release may add a value, and consumers must treat
an unrecognized value as opaque rather than failing. `data` carries at minimum
`app_label` and `model_name` — that minimal metadata is the explicit V1
promise; v1 promises no other member and no object snapshot, additional members
may appear, and consumers must tolerate them. Slack and Teams use fixed,
vendor-specific bodies and do not promise the generic envelope's full field set.
Consumers must select a decoder by `schema_version` and reject versions they do
not support rather than interpreting them as v1.

The v1 additions are compatible with consumers that ignore unknown JSON
members, but strict JSON validators or schemas that reject additional properties
may fail after the upgrade and must be updated to accept the five v1 metadata
fields. Existing events and deliveries from before the upgrade are not backfilled
and have no v1 marker.

The envelope deliberately contains no full object snapshot, domain serializer
output, secret, encrypted value, or custom-field snapshot. A system-wide rule or
endpoint still receives the changed object's owning tenant; the ambient tenant
context cannot override it. Cross-tenant rule/endpoint links remain rejected.

## Event rule conditions (withdrawn for 1.0)

Event rule conditions are withdrawn for the 1.0 release. Existing
`EventRule.conditions` JSON is preserved byte-for-byte and remains readable in
the UI, but authored conditions are not evaluated and the rule does not
dispatch. This is fail-closed: a rule with authored expressions is skipped
instead of being treated as a match on incomplete event data. Empty conditions
retain their historical match behavior.

The v1 event envelope described above carries only event metadata in `data`,
including `app_label` and `model_name`; it does not carry a full object
snapshot. Conditions therefore cannot be evaluated for 1.0. A v2 snapshot
version is planned, but ITAMbox makes no 1.0 snapshot-contract promise.

To identify active rules affected by this withdrawal after an upgrade, run:

```bash
python manage.py eventrule_withdrawn_report
```

---

## Troubleshooting

**Webhooks are not firing at all**

: Check that:
  - The Webhook Endpoint is **enabled** (`enabled = true`).
  - The Event Rule is **enabled**.
  - The rule's `Events` list includes the event type you're expecting
    (`create`, `update`, `delete`, `restore`, `checkout`, `checkin`).
  - The rule's `Model` matches the object you're modifying.
  - The object actually emits events (internal models like `Event` and
    `ObjectChange` are excluded).
  - The `django-q2` cluster is running and processing tasks.

**I see events in the database but no HTTP call was attempted**

: Verify that `django-q2` workers are running (`manage.py qcluster`). The
  webhook task is enqueued via `async_task` and dispatched asynchronously.
  Check the worker logs for task pickup.

**Delivery fails with a 4xx status**

: 4xx errors other than `429` are **final** — the system does not retry them.
  Common causes:
  - `401` / `403`: The `Authorization` header or webhook URL token is invalid.
  - `404`: The destination URL is wrong or the endpoint was deleted.
  - `400`: The payload format doesn't match the receiver's expectations.

  Fix the configuration and trigger a new event to re-send.

**Delivery fails with a 429 status**

: `429` is rate limiting, not a terminal error: the delivery is retried within
  the retry budget, and a valid `Retry-After` header delays the next attempt
  (capped at 300 seconds). If attempts keep getting `429` responses, the
  delivery exhausts its budget and ends as `dead` — raise the endpoint's
  `retry_count` or lower the receiver's rate limiting.

**Delivery fails with a 5xx status or connection error**

: The system retries up to `retry_count` times with `retry_backoff` seconds
  between attempts. Check:
  - The destination server is reachable from the ITAMbox host.
  - DNS resolves correctly from the worker container/server.
  - The URL passes the SSRF guard (no private/internal addresses).
  - After `retry_count` attempts the task is abandoned — check the worker logs
    for the final error message.

**How do I check delivery status?**

: Open the endpoint's delivery history to see the durable status, attempt
  number, response code, safe error, and next retry time. The worker logs still
  provide operational detail, but never include secrets, response bodies, or
  full endpoint URLs:
  ```
  INFO  operation=webhook.deliver disposition=success
  WARN  operation=webhook.deliver disposition=retryable action=retry
  ERROR operation=webhook.deliver disposition=terminal reason=invalid_target
  ```
  Delivery records from before the upgrade do not exist; history starts at the
  delivery-tracking upgrade.

**The HMAC signature doesn't match on my receiver**

: Common pitfalls:
  - You're comparing the raw body bytes (correct) against a decoded string
    (incorrect) — always sign the **raw request body bytes**.
  - The `secret` on the endpoint doesn't match the one in your verification
    code — copy it exactly.
  - Extra whitespace or encoding changes in transit (e.g. a proxy normalising
    the JSON). Log the raw body bytes on your receiver and compare.
  - Use `hmac.compare_digest()`, not `==`, to avoid timing side-channels.

**My event rule fires on every update, not just the field I care about**

: Conditions are withdrawn for 1.0 and cannot be evaluated against the v1
  envelope. Existing authored conditions are preserved and remain readable, but
  the rule will not dispatch until a future snapshot-capable version is
  available. Run `python manage.py eventrule_withdrawn_report` to identify
  affected rules.

**Webhook URL is rejected by the SSRF guard**

: The URL targets a private, internal, or loopback address. Use a public
  URL. If you need to send webhooks to an internal service, use a reverse
  proxy or a publicly-routable DNS name that resolves to the internal host.

**Slack / Teams payload looks wrong or is missing fields**

: Slack and Teams webhooks use fixed-format payloads: Slack gets a text summary
  and Teams gets a MessageCard-shaped body. `action_config` does not customise
  the payload template. For a different body, use an integration or receiver
  that transforms the generic v1 envelope.
