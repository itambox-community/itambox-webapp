"""Domain-blind notification delivery contracts and transports.

This module owns the reusable delivery primitives only: the typed
``DeliveryResult`` contract, the sanitized correlation-log helpers, the
SSRF-pinned Slack/Teams transports, the explicit-recipient e-mail transport and
the structural notification-channel boundary. Event/EventRule persistence,
rule evaluation and webhook orchestration belong to their owning domain and
live in ``extras.services.events`` / ``extras.tasks.webhooks``.
"""

import logging
import smtplib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

import requests
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import BadHeaderError, EmailMessage, get_connection
from django.utils.translation import gettext_lazy as _

from core.context import get_current_request_id, get_current_tenant, get_current_user
from core.models import EmailSettings, Notification

logger = logging.getLogger(__name__)

# Closed structural vocabulary of serialized notification-channel types. The
# owning domain model (``extras.NotificationChannel``) keeps its own enum; a
# parity test in extras proves the two never drift. Importing the model here
# would make a platform service depend on a domain model.
ChannelType = Literal["email", "in_app", "slack", "teams"]
NOTIFICATION_CHANNEL_TYPES: Final[frozenset[str]] = frozenset({"email", "in_app", "slack", "teams"})

CHANNEL_TYPE_EMAIL: Final[ChannelType] = "email"
CHANNEL_TYPE_IN_APP: Final[ChannelType] = "in_app"
CHANNEL_TYPE_SLACK: Final[ChannelType] = "slack"
CHANNEL_TYPE_TEAMS: Final[ChannelType] = "teams"


class NotificationChannelRef(Protocol):
    """Structural shape a delivery target must provide (no domain import)."""

    channel_type: str
    config: Mapping[str, object]
    tenant_id: int | None
    name: str


class DeliveryDisposition(StrEnum):
    SUCCESS = "success"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    operation: str
    disposition: DeliveryDisposition
    user_visible: bool = False
    user_message: str = ""
    error_class: str | None = None

    def __bool__(self):
        return self.disposition == DeliveryDisposition.SUCCESS


def delivery_log_context(operation, *, tenant_id=None, actor_id=None, request_id=None, endpoint=None):
    """Build non-sensitive correlation fields for delivery-boundary logs."""
    user = get_current_user()
    tenant = get_current_tenant()
    try:
        parsed = urlsplit(endpoint) if endpoint else None
        if parsed is not None and parsed.hostname:
            host = parsed.hostname
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            endpoint_log = f"{parsed.scheme}://{host}"
        else:
            endpoint_log = ""
    except ValueError:
        endpoint_log = ""
    return {
        "operation": operation,
        "actor_id": actor_id if actor_id is not None else getattr(user, "pk", None),
        "tenant_id": tenant_id if tenant_id is not None else getattr(tenant, "pk", None),
        "request_id": request_id if request_id is not None else get_current_request_id(),
        "endpoint": endpoint_log,
    }


def delivery_log_message(context):
    return (
        "operation=%(operation)s actor_id=%(actor_id)s tenant_id=%(tenant_id)s "
        "request_id=%(request_id)s endpoint=%(endpoint)s"
    ) % context


def _post_pinned(webhook_url, payload):
    """SSRF-hardened POST shared by the synchronous notification senders.

    Routes through core.http.request_pinned: send-time validation (fail closed,
    incl. unresolvable hosts) + the connection pinned to the validated address,
    so a DNS-rebinding answer between check and use cannot re-route the request.
    Returns the response, or None when the URL is blocked.
    """

    # inline import: heavy-import: keep event-dispatch import-light; core.http pulls requests.
    from core.http import request_pinned

    try:
        return request_pinned("POST", webhook_url, json=payload, timeout=10)
    except ValidationError:
        return None


def _send_slack_notification(webhook_url, message_text, title=None):
    operation = "slack.deliver"
    context = delivery_log_context(operation, endpoint=webhook_url)
    payload = {
        "text": message_text,
    }
    if title:
        payload["blocks"] = [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            {"type": "section", "text": {"type": "mrkdwn", "text": message_text}},
        ]
    try:
        response = _post_pinned(webhook_url, payload)
        if response is None:
            logger.error("%s disposition=terminal reason=invalid_target", delivery_log_message(context))
            return DeliveryResult(
                operation, DeliveryDisposition.TERMINAL, True, str(_("Invalid webhook configuration."))
            )
        if 400 <= response.status_code < 500:
            logger.warning("%s disposition=terminal reason=http_4xx", delivery_log_message(context))
            return DeliveryResult(
                operation, DeliveryDisposition.TERMINAL, True, str(_("Notification delivery was rejected."))
            )
        response.raise_for_status()
        logger.info("%s disposition=success", delivery_log_message(context))
        return DeliveryResult(operation, DeliveryDisposition.SUCCESS)
    except requests.RequestException:
        logger.warning("%s disposition=retryable reason=transport_or_5xx", delivery_log_message(context))
        return DeliveryResult(operation, DeliveryDisposition.RETRYABLE)


def _send_teams_notification(webhook_url, message_text, title=None):
    operation = "teams.deliver"
    context = delivery_log_context(operation, endpoint=webhook_url)
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": title or message_text[:80],
        "themeColor": "0076D7",
        "title": title or str(_("ITAMbox Notification")),
        "text": message_text,
    }
    try:
        response = _post_pinned(webhook_url, payload)
        if response is None:
            logger.error("%s disposition=terminal reason=invalid_target", delivery_log_message(context))
            return DeliveryResult(
                operation, DeliveryDisposition.TERMINAL, True, str(_("Invalid webhook configuration."))
            )
        if 400 <= response.status_code < 500:
            logger.warning("%s disposition=terminal reason=http_4xx", delivery_log_message(context))
            return DeliveryResult(
                operation, DeliveryDisposition.TERMINAL, True, str(_("Notification delivery was rejected."))
            )
        response.raise_for_status()
        logger.info("%s disposition=success", delivery_log_message(context))
        return DeliveryResult(operation, DeliveryDisposition.SUCCESS)
    except requests.RequestException:
        logger.warning("%s disposition=retryable reason=transport_or_5xx", delivery_log_message(context))
        return DeliveryResult(operation, DeliveryDisposition.RETRYABLE)


def send_email_notification(recipients, subject, body, *, tenant_id):
    """Deliver a plain-text e-mail to an explicit recipient list.

    The transport is deliberately independent from notification-channel audience
    configuration so recipient-bound custody links cannot be broadcast to a
    channel's administrators or test address.
    """
    operation = "email.deliver"
    context = delivery_log_context(operation, tenant_id=tenant_id)
    email_config = EmailSettings.load()
    if not email_config or not email_config.enabled:
        logger.warning("%s disposition=terminal reason=disabled", delivery_log_message(context))
        return DeliveryResult(
            operation,
            DeliveryDisposition.TERMINAL,
            True,
            str(_("Email is not configured.")),
            "configuration",
        )

    if not recipients:
        logger.warning("%s disposition=terminal reason=no_recipients", delivery_log_message(context))
        return DeliveryResult(
            operation,
            DeliveryDisposition.TERMINAL,
            True,
            str(_("No email recipients are configured.")),
            "missing_recipient",
        )

    try:
        connection = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=email_config.smtp_host,
            port=email_config.smtp_port,
            username=email_config.smtp_username or "",
            password=email_config.smtp_password_decrypted or "",
            use_tls=email_config.smtp_use_tls,
            fail_silently=False,
        )
        msg = EmailMessage(
            subject=subject,
            body=body,
            from_email=f"{email_config.from_name} <{email_config.from_address}>",
            to=recipients,
            connection=connection,
        )
        msg.send()
        return DeliveryResult(operation, DeliveryDisposition.SUCCESS)
    except smtplib.SMTPAuthenticationError:
        logger.error("%s disposition=terminal reason=authentication", delivery_log_message(context))
        return DeliveryResult(
            operation,
            DeliveryDisposition.TERMINAL,
            True,
            str(_("Email authentication failed.")),
            "SMTPAuthenticationError",
        )
    except (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
        logger.warning("%s disposition=retryable reason=transport", delivery_log_message(context))
        return DeliveryResult(operation, DeliveryDisposition.RETRYABLE, error_class="timeout")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPException):
        logger.error("%s disposition=terminal reason=smtp_rejected", delivery_log_message(context))
        return DeliveryResult(
            operation,
            DeliveryDisposition.TERMINAL,
            True,
            str(_("Email delivery was rejected.")),
            "SMTPException",
        )
    except (BadHeaderError, ValueError):
        logger.error("%s disposition=terminal reason=invalid_message", delivery_log_message(context))
        return DeliveryResult(
            operation,
            DeliveryDisposition.TERMINAL,
            True,
            str(_("Email delivery was rejected.")),
            "invalid_message",
        )


def _send_email_notification(channel, subject, body):
    return send_email_notification(
        channel.config.get("recipients", []),
        subject,
        body,
        tenant_id=channel.tenant_id,
    )


def _resolve_in_app_recipients(channel):
    """Resolve the in-app target users inside the channel's scope.

    Explicit ``recipient_users`` in the channel config are bounded by the
    channel's scope at delivery time: members of the channel's tenant, or
    staff for a platform-wide channel. Users outside the scope are skipped
    (issue #567 — enforced at delivery, not only in the form). Without an
    explicit list, the channel's tenant members are notified, or global
    staff for a platform-wide channel.
    """
    User = get_user_model()
    user_ids = channel.config.get("recipient_users", [])
    if user_ids:
        recipients = User.objects.filter(pk__in=user_ids, is_active=True)
        if channel.tenant_id:
            recipients = recipients.filter(memberships__tenant_id=channel.tenant_id).distinct()
        else:
            recipients = recipients.filter(is_staff=True)
        users = list(recipients)
        if len(users) < len(set(user_ids)):
            logger.warning(
                "In-App channel '%s': %s configured recipient(s) did not resolve inside the channel scope.",
                channel.name,
                len(set(user_ids)) - len(users),
            )
        return users
    if channel.tenant_id:
        # Members of the channel's tenant (via Membership) — covers
        # users with no AssetHolder profile, unlike the old
        # asset_holder_profiles join.
        return list(
            User.objects.filter(
                memberships__tenant_id=channel.tenant_id,
                is_active=True,
            ).distinct()
        )
    return list(User.objects.filter(is_staff=True, is_active=True))


def send_notification_to_channel(channel: NotificationChannelRef, subject, body):
    """Deliver a notification via the given structural channel.

    Supported channel types are the closed ``NOTIFICATION_CHANNEL_TYPES``
    vocabulary: email, in_app, slack, teams. Webhooks are NOT an alert-delivery
    channel; they belong to the EventRule system. Returns a typed result whose
    truth value preserves the historical success contract.
    """
    channel_type = getattr(channel, "channel_type", None)
    if channel_type not in NOTIFICATION_CHANNEL_TYPES:
        logger.warning("send_notification_to_channel: unhandled channel type '%s'.", channel_type)
        return DeliveryResult(
            "channel.deliver", DeliveryDisposition.TERMINAL, True, str(_("Unsupported notification channel."))
        )

    if channel_type == CHANNEL_TYPE_SLACK:
        return _send_slack_notification(
            webhook_url=channel.config.get("webhook_url", ""),
            message_text=body,
            title=subject,
        )

    if channel_type == CHANNEL_TYPE_TEAMS:
        return _send_teams_notification(
            webhook_url=channel.config.get("webhook_url", ""),
            message_text=body,
            title=subject,
        )

    if channel_type == CHANNEL_TYPE_EMAIL:
        return _send_email_notification(channel, subject, body)

    if channel_type == CHANNEL_TYPE_IN_APP:
        users = _resolve_in_app_recipients(channel)
        if not users:
            logger.warning("In-App channel '%s': no recipients found, so notifications were not sent.", channel.name)
            return DeliveryResult(
                "in_app.deliver", DeliveryDisposition.TERMINAL, True, str(_("No notification recipients were found."))
            )

        Notification.objects.bulk_create([Notification(user=user, subject=subject, message=body) for user in users])
        return DeliveryResult("in_app.deliver", DeliveryDisposition.SUCCESS)

    # Unreachable while the closed vocabulary above and these branches agree;
    # kept so a future vocabulary entry fails closed rather than falling off the end.
    logger.warning("send_notification_to_channel: unhandled channel type '%s'.", channel_type)
    return DeliveryResult(
        "channel.deliver", DeliveryDisposition.TERMINAL, True, str(_("Unsupported notification channel."))
    )
