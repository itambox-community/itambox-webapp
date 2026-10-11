import logging
from uuid import uuid4

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from assets.models import Asset, Warranty
from assets.models.audit_cadence import add_calendar_months, audit_due_on_expression, is_audit_overdue
from core import context
from core.events import DeliveryDisposition, delivery_log_context, delivery_log_message, send_notification_to_channel
from core.managers import Scope
from core.tasks.context import TaskContext
from extras.models import AlertLog, AlertRule
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
)
from licenses.models import License
from subscriptions.models import Subscription

logger = logging.getLogger(__name__)

# A claimed dispatch owns its alert until the claim lease expires: longer than
# any plausible channel send, short enough that a crashed run is still recovered
# by a later evaluation without ever racing an active attempt.
_DISPATCH_CLAIM_LEASE = timezone.timedelta(minutes=15)


def evaluate_alert_rules_task() -> int:
    """
    Scheduled daily task: evaluate all active AlertRules, create AlertLogs for
    new matches, auto-resolve logs whose conditions have cleared, re-notify
    unresolved alerts on the configured cadence, and dispatch channel
    notifications (unless the rule is muted).
    """
    with TaskContext(tenant_id=None, user_id=None):
        active_rules = AlertRule.objects.filter(is_active=True).select_related("tenant")
        logger.info("Evaluating %d active alert rules...", active_rules.count())

        today = timezone.localdate()
        existing_logs = _prefetch_open_logs()
        alerts_triggered_count = 0

        for rule in active_rules:
            logger.info(
                "Evaluating rule: %s (type=%s, threshold=%s, muted=%s, renotify=%s)",
                rule.name,
                rule.alert_type,
                rule.threshold_value,
                rule.is_muted,
                rule.renotify_interval_days,
            )
            # Use a system-level TaskContext; no specific user_id for scheduled tasks.
            with TaskContext(tenant_id=rule.tenant_id):
                alerts_triggered_count += _evaluate_rule(rule, today, existing_logs)

        logger.info(
            "Alert evaluation complete. Triggered %d fresh alert(s).",
            alerts_triggered_count,
        )
        return alerts_triggered_count


def run_alert_rule_now(rule_id: int) -> int:
    """Evaluate a single AlertRule immediately (used by the 'Run now' UI action).

    Runs in a restoring system context so rule selection and open-log dedup are
    not constrained by an ambient (possibly non-superuser) principal.

    Returns the number of fresh alerts triggered.
    """
    with TaskContext(tenant_id=None, user_id=None):
        rule = AlertRule.objects.filter(pk=rule_id, is_active=True).select_related("tenant").first()
        if not rule:
            logger.warning("run_alert_rule_now: rule %s not found or inactive.", rule_id)
            return 0

        today = timezone.localdate()
        existing_logs = _prefetch_open_logs(rule_id=rule.pk)

        with TaskContext(tenant_id=rule.tenant_id):
            return _evaluate_rule(rule, today, existing_logs)


def _prefetch_open_logs(rule_id=None):
    """Map of (rule_id, content_type_id, object_id) -> AlertLog for open alerts.

    Uses ``unscoped`` (the cross-tenant manager) so the dedup map always spans
    every tenant's open logs regardless of the ambient tenant/user context. The
    tenant-scoping default manager would fail closed to an empty queryset when a
    non-superuser principal is bound with no active tenant, causing a fresh log
    to be created on every evaluation (the duplicate-AlertLog bug).
    """
    qs = AlertLog.unscoped.filter(status__in=[AlertLog.STATUS_ACTIVE, AlertLog.STATUS_ACKNOWLEDGED])
    if rule_id is not None:
        qs = qs.filter(rule_id=rule_id)
    return {(log.rule_id, log.content_type_id, log.object_id): log for log in qs}


def _schedule_alert_dispatch(rule, match, alert_log, expected_prior_notified_at=None):
    """Dispatch one persisted alert after commit and record disposition state.

    WP-13 (Path B): exactly one delivery attempt per planned dispatch. Each run
    carries a stable unique ``delivery_id``; the run is claimed (marker written)
    before any channel send so a repeated invocation of the same run cannot
    duplicate in-app notifications or lifecycle transitions. The claim is a
    single conditional UPDATE that matches only while the alert still carries
    the ``last_notified_at`` value this run planned from, so two parallel
    evaluations planning a dispatch for the same alert state cannot both
    deliver — exactly one claim wins (issue #567, WP4).
    """
    alert_id = alert_log.pk
    rule_id = rule.pk
    tenant_id = alert_log.tenant_id
    alert_match = dict(match)
    delivery_id = str(uuid4())

    def dispatch_alert():
        notified_at = timezone.now()
        # Idempotency guard: if this exact run was already claimed/executed,
        # skip it. on_commit callbacks run serially in-process, and the claim
        # update below is written before any channel is contacted.
        if AlertLog.unscoped.filter(pk=alert_id, last_delivery_id=delivery_id).exists():
            logger.debug("Alert delivery run %s already executed for AlertLog %s; skipping.", delivery_id, alert_id)
            return
        try:
            with TaskContext(tenant_id=tenant_id):
                rule_for_dispatch = AlertRule._base_manager.get(pk=rule_id)
                persisted_alert = AlertLog.unscoped.get(pk=alert_id)
                # Claim the run before sending: pending marker + attempt counter
                # + delivery id are persisted first so a replayed invocation
                # observes the marker and backs off. The single conditional
                # UPDATE is the atomic claim: it matches only while the alert
                # still carries the planned-from notification state, so a
                # parallel evaluation planning its own run for the same state
                # claims zero rows and backs off without sending.
                prior_condition = (
                    Q(last_notified_at__isnull=True)
                    if expected_prior_notified_at is None
                    else Q(last_notified_at=expected_prior_notified_at)
                )
                claimed = AlertLog.unscoped.filter(Q(pk=alert_id) & prior_condition).update(
                    delivery_status={
                        "__dispatch__": "pending",
                        "__delivery_id__": delivery_id,
                        "__claimed_at__": notified_at.isoformat(),
                    },
                    delivery_attempts=F("delivery_attempts") + 1,
                    last_delivery_id=delivery_id,
                    delivery_outcome=AlertLog.DELIVERY_OUTCOME_PENDING,
                    last_notified_at=notified_at,
                )
                if not claimed:
                    logger.info(
                        "Alert dispatch run %s for AlertLog %s skipped: a parallel evaluation already claimed this alert state.",
                        delivery_id,
                        alert_id,
                    )
                    return
                delivery = _dispatch_channels(rule_for_dispatch, alert_match, persisted_alert, delivery_id=delivery_id)
                delivery["__delivery_id__"] = delivery_id
        # broad except: boundary-isolation: delivery backend failures are non-enumerable and become terminal
        except Exception:
            logger.exception("Alert delivery failed for AlertLog %s.", alert_id)
            delivery = {"__dispatch__": "terminal", "__delivery_id__": delivery_id}
        try:
            # Fence the completion to this run's own delivery id: a run that no
            # longer owns the claim (superseded by a newer attempt) must not
            # overwrite the newer result.
            completed = AlertLog.unscoped.filter(pk=alert_id, last_delivery_id=delivery_id).update(
                delivery_status=delivery,
                delivery_outcome=_delivery_outcome(delivery),
                last_delivery_error=_delivery_error(delivery),
                last_notified_at=notified_at,
            )
            if not completed:
                logger.info(
                    "Alert delivery run %s for AlertLog %s no longer owns the alert; completion metadata not written.",
                    delivery_id,
                    alert_id,
                )
        # broad except: task-isolation: metadata write failure must not abort later callbacks
        except Exception:
            # A metadata-write failure must not escape the on_commit callback:
            # Django stops executing later callbacks when one raises.
            logger.exception("Alert delivery metadata update failed for AlertLog %s.", alert_id)

    transaction.on_commit(dispatch_alert)


def _evaluate_rule(rule, today, existing_logs):
    """Evaluate one rule: create/renotify alerts and auto-resolve cleared ones.

    Returns the count of freshly-created alerts. Mutates ``existing_logs`` in place.
    """
    now = timezone.now()
    fresh_count = 0
    matched_keys = set()  # (content_type_id, object_id) for this rule run
    scheduled_dispatch_keys = set()  # at most one callback per alert in this pass

    for match in _collect_matches_safely(rule, today):
        fresh_count += _process_rule_match(
            rule,
            match,
            now,
            existing_logs,
            matched_keys,
            scheduled_dispatch_keys,
        )

    # Auto-resolve logs whose conditions have cleared.
    _auto_resolve_cleared(rule, matched_keys)

    # Record evaluation time without tripping the change log (system bookkeeping).
    AlertRule.objects.filter(pk=rule.pk).update(last_fired_at=now)

    return fresh_count


def _collect_matches_safely(rule, today):
    """Collect this rule's matches; a collector failure yields no matches."""
    try:
        return _collect_matches(rule, today)
    # broad except: task-isolation: one rule's collector failure must not abort the batch
    except Exception:
        logger.exception("Error collecting matches for rule %s", rule.name)
        return []


def _process_rule_match(rule, match, now, existing_logs, matched_keys, scheduled_dispatch_keys):
    """Persist/renotify one matched object. Returns 1 when a fresh alert was created."""
    obj = match["obj"]
    ct = ContentType.objects.get_for_model(obj)
    key = (rule.id, ct.id, obj.pk)
    matched_keys.add((ct.id, obj.pk))

    existing = existing_logs.get(key)
    fresh_count = 0

    if existing is None:
        alert_log, created = _create_or_adopt_alert_log(rule, match, ct, obj)
        if alert_log is None:
            # The conflict was not the expected open-alert constraint (or the row
            # vanished): skip this match rather than mask the cause silently.
            return 0
        existing_logs[key] = alert_log
        if created:
            fresh_count = 1
            logger.info("Triggered AlertLog %s for '%s' on '%s'.", alert_log.pk, match["subject"], obj)
            if not rule.is_muted:
                # Mark the in-memory row as scheduled so duplicate matches in
                # one evaluation cannot immediately re-notify it before commit.
                alert_log.last_notified_at = now

                # The freshly created row carries no last_notified_at yet.
                _schedule_alert_dispatch(rule, match, alert_log, None)
                scheduled_dispatch_keys.add(key)
        else:
            # Treat the adopted row as the existing alert (re-notify below).
            existing = alert_log

    if existing is not None and not rule.is_muted:
        _renotify_when_due(rule, match, existing, now, key, scheduled_dispatch_keys)

    return fresh_count


def _create_or_adopt_alert_log(rule, match, ct, obj):
    """Create the open alert for this match, or adopt the one that already exists.

    Returns ``(alert_log, created)``; ``(None, False)`` when neither was possible.
    """
    # New alert. Guard the create with a savepoint: the partial unique
    # constraint (one open alert per rule+object) can fire if the
    # prefetch missed an open row (a concurrent evaluation, or a context
    # that scoped the prefetch out). On conflict, adopt the existing open
    # row instead of crashing the task and poisoning the transaction.
    try:
        with transaction.atomic():
            alert_log = AlertLog.objects.create(
                rule=rule,
                subject=match["subject"],
                message=match["message"],
                severity=rule.severity,
                content_type=ct,
                object_id=obj.pk,
                tenant=match.get("tenant"),
                delivery_status=({"__dispatch__": "pending"} if not rule.is_muted else {}),
                delivery_outcome=(
                    AlertLog.DELIVERY_OUTCOME_PENDING if not rule.is_muted else AlertLog.DELIVERY_OUTCOME_NONE
                ),
            )
    except IntegrityError:
        # The partial unique constraint fired (or, rarely, another
        # integrity error). Adopt the existing open row so the task does
        # not crash; log so a non-constraint conflict is never silent.
        logger.warning(
            "AlertLog create conflicted for rule '%s' on '%s'; adopting existing open row.",
            rule.name,
            obj,
        )
        adopted = (
            AlertLog.unscoped.filter(
                rule=rule,
                content_type=ct,
                object_id=obj.pk,
                status__in=[AlertLog.STATUS_ACTIVE, AlertLog.STATUS_ACKNOWLEDGED],
            )
            .order_by("created_at")
            .first()
        )
        if adopted is None:
            # No open row to adopt: the conflict was not the expected
            # open-alert constraint (or the row vanished).
            logger.error(
                "AlertLog create failed for rule '%s' on '%s' with no open row to adopt; skipping.",
                rule.name,
                obj,
            )
            return None, False
        return adopted, False
    return alert_log, True


def _dispatch_claim_is_recoverable(delivery_status, last_notified_at, now):
    """Whether a pending dispatch marker may be recovered by a new attempt.

    An unclaimed marker (no claim time and no notification timestamp) is
    recoverable immediately; a claimed marker stays owned by its run until the
    claim lease expires, so a parallel evaluation never re-dispatches an
    active attempt while a crashed run is still recovered by a later
    evaluation (issue #567, WP4).
    """
    raw = (delivery_status or {}).get("__claimed_at__")
    claimed_at = parse_datetime(raw) if isinstance(raw, str) else None
    if claimed_at is None:
        claimed_at = last_notified_at
    if claimed_at is None:
        return True
    return now - claimed_at >= _DISPATCH_CLAIM_LEASE


def _renotify_when_due(rule, match, existing, now, key, scheduled_dispatch_keys):
    """Retry a committed-but-undelivered alert, or re-notify on cadence."""
    status = existing.delivery_status or {}
    pending = status.get("__dispatch__") == "pending"
    if pending and not _dispatch_claim_is_recoverable(status, existing.last_notified_at, now):
        logger.info(
            "AlertLog %s has a claimed dispatch in flight; a parallel evaluation will not re-dispatch it.",
            existing.pk,
        )
        return
    ref = existing.last_notified_at or existing.created_at
    due = ref and (now - ref) >= timezone.timedelta(days=rule.renotify_interval_days)
    if (pending or (rule.renotify_interval_days > 0 and due)) and key not in scheduled_dispatch_keys:
        # Mark the planned dispatch atomically against the notification state
        # this evaluation read: a parallel evaluation that already advanced the
        # alert wins the planning write and this run backs off without
        # scheduling.
        prior_notified_at = existing.last_notified_at
        prior_condition = (
            Q(last_notified_at__isnull=True) if prior_notified_at is None else Q(last_notified_at=prior_notified_at)
        )
        planned = AlertLog.unscoped.filter(Q(pk=existing.pk) & prior_condition).update(
            delivery_status={"__dispatch__": "pending"},
            delivery_outcome=AlertLog.DELIVERY_OUTCOME_PENDING,
        )
        if not planned:
            return
        existing.delivery_status = {"__dispatch__": "pending"}
        existing.delivery_outcome = AlertLog.DELIVERY_OUTCOME_PENDING
        existing.last_notified_at = now
        _schedule_alert_dispatch(rule, match, existing, prior_notified_at)
        scheduled_dispatch_keys.add(key)
        logger.info("Re-notified AlertLog %s for '%s'.", existing.pk, existing.subject)


def _dispatch_channels(rule, match, alert_log, delivery_id=None):
    """Send notifications to all explicitly attached channels; return typed per-channel delivery dict.

    WP-13 (Path B): exactly one attempt per planned dispatch. Each channel entry
    carries the typed disposition (success|retryable|terminal), the stable
    delivery identifier, the attempt timestamp, and — for failures — the typed
    error class and a user-visible message when the boundary declared one.
    """
    # Scope guard: a rule delivers only through channels of its
    # own scope — a tenant rule through its own tenant's channels, a
    # platform-wide rule through platform-wide channels. The tenant-scoping
    # manager already excludes foreign rows in a tenant context; the explicit
    # filter keeps the guarantee even for rows attached outside that boundary.
    channels = rule.channels.all()
    if rule.tenant_id is None:
        channels = channels.filter(tenant_id__isnull=True)
    else:
        channels = channels.filter(tenant_id=rule.tenant_id)
    if not channels.exists():
        return {"__no_channels__": "no channels attached to this rule"}
    # A disabled channel is never contacted; an alert whose attached channels
    # are all disabled records an explicit reason instead of silently
    # reporting success.
    channels = channels.filter(enabled=True)
    if not channels.exists():
        return {"__no_enabled_channels__": "every attached channel is disabled"}

    delivery = {}
    for channel in channels:
        try:
            result = send_notification_to_channel(channel, match["subject"], match["message"])
            delivery[str(channel.pk)] = _delivery_entry(result, delivery_id)
            if not result:
                log_context = delivery_log_context(
                    result.operation,
                    tenant_id=rule.tenant_id,
                    request_id=context.get_current_request_id(),
                )
                logger.warning(
                    "%s disposition=%s user_visible=%s",
                    delivery_log_message(log_context),
                    result.disposition.value,
                    result.user_visible,
                )
        # broad except: boundary-isolation: third-party notification backends expose non-enumerable exceptions
        except Exception:
            delivery[str(channel.pk)] = {
                "disposition": DeliveryDisposition.TERMINAL.value,
                "operation": "alert.channel.dispatch",
                "delivery_id": delivery_id,
                "attempted_at": timezone.now().isoformat(),
                "error_class": "unexpected_channel_error",
            }
            log_context = delivery_log_context(
                "alert.channel.dispatch",
                tenant_id=rule.tenant_id,
                request_id=context.get_current_request_id(),
            )
            logger.error(
                "%s disposition=terminal reason=unexpected_channel_error channel_id=%s",
                delivery_log_message(log_context),
                channel.pk,
            )
    return delivery


def _delivery_entry(result, delivery_id):
    """Persistable structured outcome for one channel delivery (WP-13)."""
    entry = {
        "disposition": result.disposition.value,
        "operation": result.operation,
        "delivery_id": delivery_id,
        "attempted_at": timezone.now().isoformat(),
    }
    if not result:
        # Typed failure detail; the safe user-visible message is persisted only
        # when the boundary declared it user-visible (never raw exception text).
        if result.error_class:
            entry["error_class"] = result.error_class
        if result.user_visible and result.user_message:
            entry["message"] = result.user_message
    return entry


def _disposition_from_value(value):
    """Normalize a per-channel payload value (structured dict or legacy string) to a disposition."""
    if isinstance(value, dict):
        return value.get("disposition")
    if isinstance(value, str):
        if value == "ok":
            return "success"
        if value.startswith("error"):
            return "terminal"
        return value
    return None


def _delivery_outcome(payload):
    """Derive the filterable single-attempt outcome from a delivery payload (WP-13)."""
    if not payload:
        return AlertLog.DELIVERY_OUTCOME_NONE
    if payload.get("__dispatch__") == "pending":
        return AlertLog.DELIVERY_OUTCOME_PENDING
    if payload.get("__dispatch__") == "terminal":
        return AlertLog.DELIVERY_OUTCOME_FAILED
    if "__no_channels__" in payload:
        return AlertLog.DELIVERY_OUTCOME_NONE
    if "__no_enabled_channels__" in payload:
        return AlertLog.DELIVERY_OUTCOME_NONE
    channel_values = [value for key, value in payload.items() if not key.startswith("__")]
    if not channel_values:
        return AlertLog.DELIVERY_OUTCOME_NONE
    dispositions = [
        disposition
        for disposition in (_disposition_from_value(value) for value in channel_values)
        if disposition is not None
    ]
    if any(disposition == "success" for disposition in dispositions):
        return AlertLog.DELIVERY_OUTCOME_DELIVERED
    return AlertLog.DELIVERY_OUTCOME_FAILED


def _delivery_error(payload):
    """Typed error of the first failed channel in the payload, or ``None``.

    Deterministic (payload key order = dispatch order). Returns the typed error
    class when the boundary supplied one, otherwise the disposition itself.
    """
    if not payload or payload.get("__dispatch__") == "pending":
        return None
    if payload.get("__dispatch__") == "terminal":
        return "dispatch_crash"
    for key, value in payload.items():
        if key.startswith("__"):
            continue
        if isinstance(value, dict):
            disposition = value.get("disposition")
            if disposition in (DeliveryDisposition.RETRYABLE.value, DeliveryDisposition.TERMINAL.value):
                return value.get("error_class") or disposition
        else:
            disposition = _disposition_from_value(value)
            if disposition in (DeliveryDisposition.RETRYABLE.value, DeliveryDisposition.TERMINAL.value):
                return disposition
    return None


def _auto_resolve_cleared(rule, matched_keys):
    """
    Flip active/acknowledged logs to resolved when their condition no longer
    matches, so the rule can re-fire if the object dips below threshold again.
    """
    stale_qs = AlertLog.unscoped.filter(
        rule=rule,
        status__in=[AlertLog.STATUS_ACTIVE, AlertLog.STATUS_ACKNOWLEDGED],
    )
    resolved_count = 0
    for log in stale_qs:
        if (log.content_type_id, log.object_id) not in matched_keys:
            log.status = AlertLog.STATUS_RESOLVED
            log.resolved_at = timezone.now()
            log.save(update_fields=["status", "resolved_at"])
            resolved_count += 1

    if resolved_count:
        logger.info("Auto-resolved %d cleared alert(s) for rule '%s'.", resolved_count, rule.name)


def _collect_matches(rule, today):
    """Return list of match dicts for the given rule and date."""
    matches = []

    if rule.alert_type == AlertRule.ALERT_TYPE_LOW_STOCK:
        matches.extend(_match_low_stock(rule))

    elif rule.alert_type == AlertRule.ALERT_TYPE_UPCOMING_EOL:
        matches.extend(_match_upcoming_eol(rule, today))

    elif rule.alert_type == AlertRule.ALERT_TYPE_LICENSE_EXPIRY:
        matches.extend(_match_license_expiry(rule, today))

    elif rule.alert_type == AlertRule.ALERT_TYPE_RENEWAL_DUE:
        matches.extend(_match_renewal_due(rule, today))

    elif rule.alert_type == AlertRule.ALERT_TYPE_WARRANTY_EXPIRY:
        matches.extend(_match_warranty_expiry(rule, today))

    elif rule.alert_type == AlertRule.ALERT_TYPE_AUDIT_OVERDUE:
        matches.extend(_match_audit_overdue(rule, today))

    return matches


def _low_stock_threshold(item, rule):
    """Per-item safety limit, falling back to the rule threshold."""
    return item.min_qty if (item.min_qty and item.min_qty > 0) else rule.threshold_value


def _match_low_stock(rule):
    return _match_low_stock_accessories(rule) + _match_low_stock_consumables(rule) + _match_low_stock_components(rule)


def _match_low_stock_accessories(rule):
    acc_stocks_sub = AccessoryStock.objects.filter(accessory=OuterRef("pk"))
    if rule.tenant:
        acc_stocks_sub = acc_stocks_sub.filter(location__tenant=rule.tenant)
    acc_assigns_sub = AccessoryAssignment.objects.filter(
        accessory=OuterRef("pk"),
        from_location__isnull=True,
    )
    acc_qs = Accessory.objects.filter(deleted_at__isnull=True).annotate(
        annotated_total_stock=Coalesce(
            Subquery(acc_stocks_sub.values("accessory").annotate(t=Sum("qty")).values("t")), 0
        ),
        annotated_undeducted_qty=Coalesce(
            Subquery(acc_assigns_sub.values("accessory").annotate(t=Sum("qty")).values("t")), 0
        ),
    )
    if rule.tenant:
        acc_qs = acc_qs.filter(tenant=rule.tenant)

    matches = []
    for acc in acc_qs:
        available = max(0, acc.annotated_total_stock - acc.annotated_undeducted_qty)
        threshold = _low_stock_threshold(acc, rule)
        if available <= threshold:
            matches.append(
                {
                    "obj": acc,
                    "tenant": acc.tenant,
                    "subject": _("Low Stock: %(name)s") % {"name": acc.name},
                    "message": _(
                        "Accessory '%(name)s' available stock is %(available)s, "
                        "at or below the safety limit of %(threshold)s units."
                    )
                    % {"name": acc.name, "available": available, "threshold": threshold},
                }
            )
    return matches


def _match_low_stock_consumables(rule):
    con_stocks_sub = ConsumableStock.objects.filter(consumable=OuterRef("pk"))
    if rule.tenant:
        con_stocks_sub = con_stocks_sub.filter(location__tenant=rule.tenant)
    con_assigns_sub = ConsumableAssignment.objects.filter(
        consumable=OuterRef("pk"),
        from_location__isnull=True,
    )
    con_qs = Consumable.objects.filter(deleted_at__isnull=True).annotate(
        annotated_total_stock=Coalesce(
            Subquery(con_stocks_sub.values("consumable").annotate(t=Sum("qty")).values("t")), 0
        ),
        annotated_undeducted_qty=Coalesce(
            Subquery(con_assigns_sub.values("consumable").annotate(t=Sum("qty")).values("t")), 0
        ),
    )
    if rule.tenant:
        con_qs = con_qs.filter(tenant=rule.tenant)

    matches = []
    for con in con_qs:
        available = max(0, con.annotated_total_stock - con.annotated_undeducted_qty)
        threshold = _low_stock_threshold(con, rule)
        if available <= threshold:
            matches.append(
                {
                    "obj": con,
                    "tenant": con.tenant,
                    "subject": _("Low Stock: %(name)s") % {"name": con.name},
                    "message": _(
                        "Consumable '%(name)s' available stock is %(available)s, "
                        "at or below the safety limit of %(threshold)s units."
                    )
                    % {"name": con.name, "available": available, "threshold": threshold},
                }
            )
    return matches


def _match_low_stock_components(rule):
    comp_stocks_sub = ComponentStock.objects.filter(component=OuterRef("pk"))
    if rule.tenant:
        comp_stocks_sub = comp_stocks_sub.filter(location__tenant=rule.tenant)
    comp_allocs_sub = ComponentAllocation.objects.filter(
        component=OuterRef("pk"),
        deleted_at__isnull=True,
        from_location__isnull=True,
    )
    if rule.tenant:
        comp_allocs_sub = comp_allocs_sub.filter(assigned_asset__tenant=rule.tenant)
    comp_qs = Component.objects.filter(deleted_at__isnull=True).annotate(
        annotated_total_stock=Coalesce(
            Subquery(comp_stocks_sub.values("component").annotate(t=Sum("qty")).values("t")), 0
        ),
        annotated_target_only_allocated=Coalesce(
            Subquery(comp_allocs_sub.values("component").annotate(t=Sum("qty")).values("t")), 0
        ),
    )
    if rule.tenant:
        comp_qs = comp_qs.filter(tenant=rule.tenant)

    matches = []
    for comp in comp_qs:
        available = max(0, comp.annotated_total_stock - comp.annotated_target_only_allocated)
        threshold = _low_stock_threshold(comp, rule)
        if available <= threshold:
            matches.append(
                {
                    "obj": comp,
                    "tenant": comp.tenant,
                    "subject": _("Low Stock: %(name)s") % {"name": comp.name},
                    "message": _(
                        "Component '%(name)s' available stock is %(available)s, "
                        "at or below the safety limit of %(threshold)s units."
                    )
                    % {"name": comp.name, "available": available, "threshold": threshold},
                }
            )
    return matches


def _match_upcoming_eol(rule, today):
    deadline = today + timezone.timedelta(days=rule.threshold_value)
    assets = Asset.objects.filter(
        deleted_at__isnull=True,
        purchase_date__isnull=False,
        asset_type__eol_months__gt=0,
    ).select_related("asset_type")
    if rule.tenant:
        assets = assets.filter(tenant=rule.tenant)

    matches = []
    for asset in assets:
        eol = asset.eol_date
        if eol and today <= eol <= deadline:
            days_left = (eol - today).days
            matches.append(
                {
                    "obj": asset,
                    "tenant": asset.tenant,
                    "subject": _("Upcoming Hardware EOL: %(tag)s") % {"tag": asset.asset_tag},
                    "message": ngettext(
                        "Asset %(tag)s (%(name)s) reaches EOL on %(eol)s (%(days)s day remaining).",
                        "Asset %(tag)s (%(name)s) reaches EOL on %(eol)s (%(days)s days remaining).",
                        days_left,
                    )
                    % {
                        "tag": asset.asset_tag,
                        "name": asset.name,
                        "eol": f"{eol:%Y-%m-%d}",
                        "days": days_left,
                    },
                }
            )
    return matches


def _match_license_expiry(rule, today):
    deadline = today + timezone.timedelta(days=rule.threshold_value)
    qs = License.objects.for_scope(Scope.current()).filter(
        deleted_at__isnull=True,
        expiration_date__lte=deadline,
        expiration_date__gte=today,
    )
    if rule.tenant:
        qs = qs.filter(tenant=rule.tenant)

    matches = []
    for lic in qs:
        days_left = (lic.expiration_date - today).days
        matches.append(
            {
                "obj": lic,
                "tenant": lic.tenant,
                "subject": _("License Expiring: %(name)s") % {"name": lic.name},
                "message": ngettext(
                    "License '%(name)s' expires on %(date)s (%(days)s day remaining).",
                    "License '%(name)s' expires on %(date)s (%(days)s days remaining).",
                    days_left,
                )
                % {"name": lic.name, "date": lic.expiration_date, "days": days_left},
            }
        )
    return matches


def _match_renewal_due(rule, today):
    deadline = today + timezone.timedelta(days=rule.threshold_value)
    qs = Subscription.objects.for_scope(Scope.current()).filter(
        deleted_at__isnull=True,
        status="active",
        renewal_date__lte=deadline,
        renewal_date__gte=today,
    )
    if rule.tenant:
        qs = qs.filter(tenant=rule.tenant)

    matches = []
    for sub in qs:
        days_left = (sub.renewal_date - today).days
        matches.append(
            {
                "obj": sub,
                "tenant": sub.tenant,
                "subject": _("Subscription Renewal Due: %(name)s") % {"name": sub.name},
                "message": ngettext(
                    "Subscription '%(name)s' ends on %(date)s "
                    "(%(days)s day remaining) and requires renewal validation.",
                    "Subscription '%(name)s' ends on %(date)s "
                    "(%(days)s days remaining) and requires renewal validation.",
                    days_left,
                )
                % {"name": sub.name, "date": sub.renewal_date, "days": days_left},
            }
        )
    return matches


def _match_warranty_expiry(rule, today):
    deadline = today + timezone.timedelta(days=rule.threshold_value)
    qs = Warranty.objects.filter(
        deleted_at__isnull=True,
        asset__deleted_at__isnull=True,
        end_date__lte=deadline,
        end_date__gte=today,
    ).select_related("asset", "asset__asset_type")
    if rule.tenant:
        qs = qs.filter(asset__tenant=rule.tenant)

    matches = []
    for warranty in qs:
        asset = warranty.asset
        days_left = (warranty.end_date - today).days
        matches.append(
            {
                "obj": asset,
                "tenant": asset.tenant,
                "subject": _("Warranty Expiring: %(tag)s") % {"tag": asset.asset_tag},
                "message": ngettext(
                    "Asset %(tag)s (%(name)s) %(wtype)s warranty expires on %(date)s (%(days)s day remaining).",
                    "Asset %(tag)s (%(name)s) %(wtype)s warranty expires on %(date)s (%(days)s days remaining).",
                    days_left,
                )
                % {
                    "tag": asset.asset_tag,
                    "name": asset.name,
                    "wtype": warranty.get_warranty_type_display(),
                    "date": f"{warranty.end_date:%Y-%m-%d}",
                    "days": days_left,
                },
            }
        )
    return matches


def _match_audit_overdue(rule, today):
    """Assets overdue for a physical audit.

    Per-category cadence (Category.audit_interval_months) takes priority when
    set on an asset's category; otherwise falls back to rule.threshold_value days.
    Assets that have never been audited and have a cadence are always included.
    """
    cutoff = today - timezone.timedelta(days=rule.threshold_value)

    # Also pull in assets overdue per a category-level cadence that's shorter
    # than the global rule threshold (they wouldn't match last_audited__lt=cutoff).
    category_overdue_q = Q(_audit_due_on__lt=today)

    qs = (
        Asset.objects.filter(
            deleted_at__isnull=True,
        )
        .annotate(_audit_due_on=audit_due_on_expression())
        .select_related(
            "asset_type__category",
        )
        .filter(Q(last_audited__isnull=True) | Q(last_audited__lt=cutoff) | category_overdue_q)
    )
    if rule.tenant:
        qs = qs.filter(tenant=rule.tenant)

    matches = []
    for asset in qs:
        # Prefer category-level cadence over the global rule threshold.
        category = asset.asset_type.category if asset.asset_type else None
        if category and category.audit_interval_months:
            base = asset.last_audited or asset.created_at
            due_date = add_calendar_months(base, category.audit_interval_months)
            if not is_audit_overdue(due_date, today):
                continue  # category cadence says not yet overdue
            threshold_desc = ngettext(
                "every %(months)s month per category",
                "every %(months)s months per category",
                category.audit_interval_months,
            ) % {
                "months": category.audit_interval_months,
            }
        else:
            threshold_desc = ngettext("every %(days)s day", "every %(days)s days", rule.threshold_value) % {
                "days": rule.threshold_value
            }

        if asset.last_audited:
            days_overdue = (today - asset.last_audited.date()).days
            detail = ngettext(
                "last audited %(days)s day ago (%(date)s)",
                "last audited %(days)s days ago (%(date)s)",
                days_overdue,
            ) % {
                "days": days_overdue,
                "date": f"{asset.last_audited:%Y-%m-%d}",
            }
        else:
            detail = _("never audited")
        matches.append(
            {
                "obj": asset,
                "tenant": asset.tenant,
                "subject": _("Audit Overdue: %(tag)s") % {"tag": asset.asset_tag},
                "message": _("Asset %(tag)s (%(name)s) is overdue for an audit (%(detail)s; threshold: %(threshold)s).")
                % {
                    "tag": asset.asset_tag,
                    "name": asset.name,
                    "detail": detail,
                    "threshold": threshold_desc,
                },
            }
        )
    return matches
