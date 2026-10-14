# Event Rules

An **Event Rule** matches supported lifecycle events for a selected model and triggers a webhook or in-app notification. This is a model/API reference; see [Webhooks & Automation](../../usage/webhooks-and-automation.md) for the supported workflow and fixed payload contracts.

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Action Config** | Fallback webhook target settings (`url`, `method`, `headers`, `secret`) used only when no Webhook Endpoint is linked. It does not template or override the outbound payload. | JSON | No |
| **Action Type** | Type of action to trigger (e.g., Webhook). | Choice | Yes |
| **Conditions** | Preserved authored conditions; withdrawn and read-only for 1.0. Authored values fail closed and do not dispatch the rule. | JSON | No |
| **Enabled** | Flag indicating if this rule is actively monitored. | Boolean | Yes |
| **Events** | List of event types triggering this rule (e.g., `create`, `update`, `delete`). | JSON | Yes |
| **Model** | The target database model being monitored. | Foreign Key | Yes |
| **Name** | Descriptive name for the event rule. | String | Yes |
| **Tenant** | Tenant context that owns this rule. Null implies a system-wide rule. | Foreign Key | No |
| **Webhook** | Target Webhook Endpoint to invoke when the rule conditions match. | Foreign Key | No |

## Features & Validation

* **Condition Preservation**: Existing condition JSON remains readable, but authored conditions are not evaluated in 1.0 and fail closed.
* **Webhook Mapping**: A linked Webhook Endpoint takes precedence over `action_config` target settings. Its URL, method, headers, secret and retry policy are snapshotted for the delivery.
