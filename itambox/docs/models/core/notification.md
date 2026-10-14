# Notifications

A **Notification** is an in-app message addressed to one user. It has a subject, message, level, optional target URL, and read state. The UI inbox is scoped to the signed-in user; a null `user` value is not a broadcast mechanism.

---

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **User** | The recipient whose inbox contains this notification. Application fan-out creates recipient-specific rows; null does not make a row visible to everyone. | Foreign Key | No |
| **Subject** | Short title or heading for the notification. | String (255) | Yes |
| **Message** | Full body text of the notification. | Text | Yes |
| **Level** | Severity / visual style: `Info`, `Warning`, `Success`, or `Danger`. | Choice | Yes |
| **Is Read** | Whether the target user has marked the notification as read. Defaults to `False`. | Boolean | Yes |
| **Target URL** | Optional destination URL opened when the notification is clicked. | String (500) | No |
| **Created At** | Timestamp when the notification was generated (auto-set). | DateTime | Yes |

---

## Usage Patterns

- **User-scoped**: When `user` is set, the notification is delivered to that specific user's inbox.
- **Multiple recipients**: Create or dispatch a separate notification for each authorized recipient. Do not use a null-user row as a broadcast; the inbox, polling, and detail views filter by the current user.
- **Read tracking**: The `is_read` flag and the composite index on `(user, is_read)` enable efficient querying of unread notifications per user.
- **Click-through**: The optional `target_url` provides deep-linking — clicking the notification can navigate directly to the relevant object or page.
