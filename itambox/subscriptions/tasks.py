import json
import logging

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import Notification
from core.tasks.context import TaskContext

from .models import Subscription, SubscriptionStatusChoices

logger = logging.getLogger(__name__)
User = get_user_model()
RENEWAL_REMINDER_THRESHOLDS = (30, 14, 7)


def _notify_once(*, user, subject, message, target_url):
    identity = json.dumps(
        [user.pk, str(subject), str(message), Notification.LEVEL_WARNING, target_url],
        separators=(",", ":"),
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [identity])
        Notification.objects.get_or_create(
            user=user,
            subject=subject,
            message=message,
            level=Notification.LEVEL_WARNING,
            target_url=target_url,
        )


def _tenant_recipients(subscription):
    recipients = set(
        User.objects.filter(
            is_staff=True,
            is_active=True,
            memberships__tenant_id=subscription.tenant_id,
        ).distinct()
    )
    if (
        subscription.owner
        and subscription.owner.is_active
        and subscription.owner.memberships.filter(tenant_id=subscription.tenant_id).exists()
    ):
        recipients.add(subscription.owner)
    return recipients


def _notify_expired(subscription):
    for user in _tenant_recipients(subscription):
        _notify_once(
            user=user,
            subject=_("Subscription Expired: %(name)s") % {"name": subscription.name},
            message=_("The subscription '%(name)s' from supplier '%(supplier)s' has expired as of %(date)s.")
            % {
                "name": subscription.name,
                "supplier": subscription.supplier,
                "date": subscription.renewal_date,
            },
            target_url=subscription.get_absolute_url(),
        )


def _notify_renewal_warning(subscription, days):
    for user in _tenant_recipients(subscription):
        _notify_once(
            user=user,
            subject=_("Subscription Renewal Warning: %(name)s in %(days)s Days")
            % {"name": subscription.name, "days": days},
            message=_(
                "The subscription '%(name)s' from supplier '%(supplier)s' "
                "is due for renewal on %(date)s (%(days)s days remaining). "
                "Cost: %(cost)s %(currency)s."
            )
            % {
                "name": subscription.name,
                "supplier": subscription.supplier,
                "date": subscription.renewal_date,
                "days": days,
                "cost": subscription.renewal_cost,
                "currency": subscription.currency,
            },
            target_url=subscription.get_absolute_url(),
        )


def _renewal_reminder_progress(renewal_date, today):
    days_remaining = (renewal_date - today).days
    # When several tiers were crossed during downtime, only the closest upcoming
    # threshold remains actionable; report the actual days remaining to recipients.
    threshold = min(days for days in RENEWAL_REMINDER_THRESHOLDS if days_remaining <= days)
    return days_remaining, threshold


def _notify_pending_renewal_warning(subscription, today):
    days_remaining, threshold = _renewal_reminder_progress(subscription.renewal_date, today)
    last_threshold = subscription.renewal_reminder_days or max(RENEWAL_REMINDER_THRESHOLDS) + 1
    if subscription.renewal_reminder_cycle_date == subscription.renewal_date and threshold >= last_threshold:
        return

    _notify_renewal_warning(subscription, days_remaining)
    Subscription.objects.filter(pk=subscription.pk).update(
        renewal_reminder_cycle_date=subscription.renewal_date,
        renewal_reminder_days=threshold,
    )


def check_subscription_expiries_and_reminders():
    """
    Daily background task to:
    1. Mark subscriptions that have passed their renewal date as 'expired'.
    2. Send renewal warnings (30, 14, and 7 days prior).

    This task iterates subscriptions across all tenants, so each subscription's
    work is wrapped in its own TaskContext bound to that subscription's tenant.
    That ensures every save and Notification is recorded as an ObjectChange and
    attributed to the correct tenant rather than the global (None) context.

    Both enumerations deliberately read ``Subscription.objects`` WITHOUT
    ``for_scope``: the default manager only filters soft-deleted rows, so every
    tenant's subscriptions are found before the per-tenant scope is entered.
    Soft-deleted subscriptions remain excluded, and every row found is still
    processed inside its own tenant's TaskContext below.
    """
    # Renewal dates are calendar dates in the configured application timezone,
    # not UTC dates. Around local midnight ``timezone.now().date()`` can still
    # be yesterday and leave already-expired subscriptions active for a day.
    today = timezone.localdate()

    # 1. Handle auto-expiries
    expired_count = 0
    expired_subs = Subscription.objects.filter(status=SubscriptionStatusChoices.ACTIVE, renewal_date__lt=today)
    for candidate in expired_subs:
        with transaction.atomic():
            sub = (
                Subscription.objects.select_for_update()
                .filter(
                    pk=candidate.pk,
                    status=SubscriptionStatusChoices.ACTIVE,
                    renewal_date__lt=today,
                )
                .first()
            )
            if sub is None:
                continue
            with TaskContext(tenant_id=sub.tenant_id, user_id=None):
                sub.expire()
                expired_count += 1
                _notify_expired(sub)

    if expired_count:
        logger.info(f"Marked {expired_count} subscriptions as expired.")

    # 2. Handle renewal reminders (30, 14, 7 days warning)
    reminder_cutoff = today + timezone.timedelta(days=max(RENEWAL_REMINDER_THRESHOLDS))
    subs_to_remind = Subscription.objects.filter(
        status=SubscriptionStatusChoices.ACTIVE,
        renewal_date__gt=today,
        renewal_date__lte=reminder_cutoff,
    )
    for candidate in subs_to_remind:
        with transaction.atomic():
            sub = (
                Subscription.objects.select_for_update()
                .filter(
                    pk=candidate.pk,
                    status=SubscriptionStatusChoices.ACTIVE,
                    renewal_date__gt=today,
                    renewal_date__lte=reminder_cutoff,
                )
                .first()
            )
            if sub is None:
                continue
            with TaskContext(tenant_id=sub.tenant_id, user_id=None):
                _notify_pending_renewal_warning(sub, today)
