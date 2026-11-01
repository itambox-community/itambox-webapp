"""Event and EventRule orchestration owned by the extras domain.

Everything that constructs or queries ``Event``, ``EventRule``,
``WebhookDelivery``, ``WebhookEndpoint`` or the condition helpers lives here.
The reusable delivery contracts and transports stay in ``core.events``.
"""

import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Q
from django.urls import NoReverseMatch
from django.utils.translation import gettext_lazy as _
from django_q.tasks import async_task

from core.context import get_current_request_id, get_current_user
from core.crypto import encrypt_string
from core.models import ChangeLoggingMixin, Notification
from extras.models import Event, EventRule, WebhookDelivery, WebhookEndpoint, has_authored_conditions
from extras.tasks.webhooks import WEBHOOK_TASK_PATH, WebhookDeliveryAssertions

logger = logging.getLogger(__name__)


def _resolve_instance_tenant_id(instance):
    """Resolve the tenant that owns ``instance`` so event rules are matched against the
    object's OWN tenant rather than the ambient tenant contextvar.

    The contextvar is unset in system contexts (management commands, the django-q worker
    after a ``TaskContext`` exits, the shell). There the tenant-scoping manager fails *open*
    (``filter_by_tenant`` returns the unscoped queryset), so matching rules by the contextvar
    would fire EVERY tenant's rules for the object's ContentType — a cross-tenant dispatch
    (foreign webhooks/notifications about another tenant's object). Resolving the tenant from
    the instance itself closes that regardless of context. Returns the tenant pk, or ``None``
    for a tenant-less/global object (in which case only global ``tenant=None`` rules fire).
    """
    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is not None:
        return tenant_id
    # Models that derive their tenant through a relation (assignments/stock) declare a
    # ``tenant_lookup`` ORM path (e.g. 'asset__tenant'); walk it to the owning tenant.
    # Fall back to ``changelog_tenant_lookup`` (used by models such as AssetAudit that
    # only declare the changelog attribute) so their events match tenant EventRules.
    lookup = getattr(type(instance), "tenant_lookup", None) or getattr(type(instance), "changelog_tenant_lookup", None)
    if lookup:
        obj = instance
        for part in lookup.split("__"):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return getattr(obj, "pk", None)
    return None


_RULE_INDEX_CACHE_KEY = "extras:event_rule_index:v1"
_RULE_INDEX_TTL = 300


def _actionable(rule):
    """Whether a rule can ever act: the same gates ``_eligible_rules`` applies per event."""
    return rule.action_type in (EventRule.ACTION_WEBHOOK, EventRule.ACTION_NOTIFICATION) and _check_conditions(
        rule.conditions, None
    )


def _build_rule_index():
    """Return ``{(content_type_id, tenant_id | None, action)}`` for every actionable live rule."""
    index = set()
    # unscoped: the index spans every tenant by design; it is keyed by tenant and never read ambiently
    rules = EventRule._base_manager.filter(enabled=True, deleted_at__isnull=True).only(
        "model_id", "tenant_id", "events", "action_type", "conditions"
    )
    for rule in rules:
        if not _actionable(rule):
            continue
        for action in rule.events or []:
            index.add((rule.model_id, rule.tenant_id, action))
    return frozenset(index)


def _rule_index():
    try:
        index = cache.get(_RULE_INDEX_CACHE_KEY)
    # broad except: availability-tradeoff: a cache outage falls back to reading the rules from the database
    except Exception:
        index = None
    if index is not None:
        return index
    index = _build_rule_index()
    # Inside an open transaction the rows read may still roll back; cache only committed state.
    if not connection.in_atomic_block:
        try:
            cache.set(_RULE_INDEX_CACHE_KEY, index, timeout=_RULE_INDEX_TTL)
        # broad except: availability-tradeoff: an unwritable cache only costs one query per dispatch
        except Exception:
            logger.warning("Could not cache the event rule index")
    return index


def invalidate_rule_index():
    """Drop the cached rule index; called on every EventRule write (and again at commit)."""
    try:
        cache.delete(_RULE_INDEX_CACHE_KEY)
    # broad except: availability-tradeoff: the short TTL bounds staleness if the cache is unreachable
    except Exception:
        logger.warning("Could not invalidate the event rule index")


def has_eligible_rules(content_type_id, tenant_id, action):
    """Whether any rule could act on this (model, tenant, action): tenant-owned or global."""
    index = _rule_index()
    return (content_type_id, tenant_id, action) in index or (content_type_id, None, action) in index


def dispatch_event(sender, instance, action, created=None, *, object_id=None):
    """Dispatch an event when a ChangeLoggingMixin model is created, updated, or deleted.

    No ``Event`` row is written (and no rule transaction opened) unless at least one
    eligible rule exists for the model, the object's own tenant and the action.
    """

    if not issubclass(sender, ChangeLoggingMixin):
        return

    ct = ContentType.objects.get_for_model(sender)
    event_object_id = instance.pk if object_id is None else object_id
    if event_object_id is None:
        logger.error("Skipping event with missing object id for %s:%s", sender.__name__, action)
        return

    instance_tenant_id = _resolve_instance_tenant_id(instance)
    if not has_eligible_rules(ct.pk, instance_tenant_id, action):
        return

    event = Event.objects.create(
        model=ct,
        object_id=event_object_id,
        action=action,
        data={"app_label": ct.app_label, "model_name": ct.model},
    )

    process_event_rules(event, instance_tenant_id)


def _eligible_rules(event, instance_tenant_id):
    """Rules that may act on this event: enabled, live, tenant-or-global, subscribed.

    Rules are scoped to the triggering object's OWN tenant (plus global ``tenant=None``
    rules), read through the unscoped ``_base_manager`` so the result NEVER depends on the
    ambient tenant contextvar (which fails open in system contexts). See
    ``_resolve_instance_tenant_id``.
    """
    # unscoped: rule matching must not depend on the emitting request's ambient tenant; tenant filtered explicitly
    rules = (
        EventRule._base_manager.filter(
            model=event.model,
            enabled=True,
            deleted_at__isnull=True,
        )
        .filter(Q(tenant_id=instance_tenant_id) | Q(tenant__isnull=True))
        .select_related("webhook")
    )

    for rule in rules:
        if event.action not in (rule.events or []):
            continue

        if rule.conditions_withdrawn:
            logger.info(
                "Skipping event rule pk=%s tenant=%s: conditions withdrawn for 1.0",
                rule.pk,
                rule.tenant_id,
            )
            continue

        if not _check_conditions(rule.conditions, event):
            continue

        yield rule


def process_event_rules(event, instance_tenant_id=None):
    """Serialize one terminal evaluation of every eligible rule for an event.

    The event row is locked for the complete rule-attempt transaction. Concurrent
    dispatchers therefore serialize before reading ``processed``; all database
    side effects and the terminal flag commit together. Queue dispatches registered
    by rule actions remain ``transaction.on_commit`` callbacks. Durable pending
    webhook rows are recovered by the periodic coordinator if publication fails.

    Each eligible rule gets one database attempt; a rule action that raises is
    caught, reported with identifiers only, and terminal for that rule on this
    event. Once every eligible rule has reached a terminal attempt — including
    the case where none was eligible — the event is marked processed. External
    webhook delivery remains at-least-once: consumers deduplicate by the stable
    event and delivery identities.
    """
    if event.pk is None:
        return

    with transaction.atomic():
        # unscoped: event row lock by pk in the worker, which has no ambient scope
        locked_event = Event._base_manager.select_for_update(of=("self",)).get(pk=event.pk)
        if locked_event.processed:
            return

        for rule in _eligible_rules(locked_event, instance_tenant_id):
            # Per-rule isolation: one rule's action raising must not abort the remaining
            # rules for this event. Only identifiers and the exception class may be logged
            # on this boundary — action config, event data, endpoints and secrets must not.
            try:
                # A per-rule savepoint keeps database errors terminal for this
                # rule without poisoning the outer event-lock transaction or
                # preventing later eligible rules from being attempted.
                with transaction.atomic():
                    _execute_event_action(rule, locked_event, instance_tenant_id)
            # broad except: task-isolation: one failing rule must not prevent other eligible rules
            except Exception as error:
                logger.error(
                    "operation=events.rule_action disposition=terminal event_id=%s rule_id=%s error_class=%s",
                    locked_event.pk,
                    rule.pk,
                    type(error).__name__,
                )

        locked_event.processed = True
        locked_event.save(update_fields=["processed"])


def _check_conditions(conditions, event):
    """Fail-closed evaluation of optional JSON conditions on the event.

    WP-15 (D4): the condition feature is withdrawn for 1.0. Any authored
    condition expression (or an unexpected, non-dict payload) fails closed so
    the rule never matches on the near-empty v1 event envelope. Truly empty
    condition payloads (``None``, ``{}``, or a dict whose ``rules`` list is
    empty) preserve the historical unconditional-match behavior.
    """

    if conditions is None or conditions == {}:
        return True
    if not isinstance(conditions, dict):
        return False
    return not has_authored_conditions(conditions)


def _execute_event_action(rule, event, instance_tenant_id=None):
    """Execute the action specified by an event rule."""

    if rule.action_type == EventRule.ACTION_WEBHOOK:
        _send_webhook(rule, event, instance_tenant_id)
    elif rule.action_type == EventRule.ACTION_NOTIFICATION:
        _send_notification(rule, event)
    # 'script' action_type was removed; existing rows are silently skipped.
    # Scripts may return as a proper plugin hook post-1.0.


def _send_webhook(rule, event, instance_tenant_id=None):
    """Create the durable delivery row for the rule and enqueue its dispatch.

    The exact rule provenance and target configuration are snapshotted before
    enqueue. The task receives identity assertions only, so later rule or
    endpoint mutation cannot redirect the delivery and sensitive target state
    never enters a django-q package or retry schedule.
    """

    target = _event_rule_target_snapshot(rule, instance_tenant_id)
    if target is None:
        return

    # unscoped: delivery rows are created by the worker, which has no ambient scope
    delivery = WebhookDelivery._base_manager.create(
        tenant_id=instance_tenant_id,
        endpoint=rule.webhook,
        event=event,
        payload_timestamp=event.timestamp,
        delivery_id=str(uuid4()),
        status=WebhookDelivery.STATUS_PENDING,
        **target,
    )
    _enqueue_delivery(delivery)


def _encrypted_target_secret(secret):
    """Return an encrypted target snapshot without putting plaintext in a task."""
    if not isinstance(secret, str):
        raise ValueError("Webhook secrets must be strings.")
    if not secret or secret.startswith("enc$"):
        return secret
    return encrypt_string(secret)


def _legacy_retry_policy(url, tenant_id):
    """Resolve the historical same-URL retry policy once, at row creation."""
    # unscoped: endpoint lookup by URL in the worker, which has no ambient scope
    matches = WebhookEndpoint._base_manager.filter(url=url, enabled=True, deleted_at__isnull=True)
    if tenant_id is None:
        match = matches.filter(tenant__isnull=True).order_by("pk").first()
    else:
        match = matches.filter(tenant_id=tenant_id).order_by("pk").first()
        if match is None:
            match = matches.filter(tenant__isnull=True).order_by("pk").first()
    if match is None:
        return 3, 60
    return match.retry_count, match.retry_backoff


def _event_rule_target_snapshot(rule, instance_tenant_id):
    """Return immutable target fields for the delivery created by ``rule``."""
    endpoint = rule.webhook
    if endpoint is not None:
        if (
            not endpoint.enabled
            or endpoint.deleted_at is not None
            or (endpoint.tenant_id is not None and endpoint.tenant_id != instance_tenant_id)
        ):
            return None
        url = endpoint.url
        method = endpoint.http_method or WebhookEndpoint.HTTP_POST
        headers = endpoint.headers or {}
        secret = endpoint.secret
        target_enabled = endpoint.enabled
        target_tenant_id = endpoint.tenant_id
        retry_count = endpoint.retry_count
        retry_backoff = endpoint.retry_backoff
    else:
        config = rule.action_config or {}
        url = config.get("url")
        method = config.get("method") or WebhookEndpoint.HTTP_POST
        headers = config.get("headers") or {}
        secret = config.get("secret", "")
        target_enabled = True
        target_tenant_id = None
        if not url:
            return None
        retry_count, retry_backoff = _legacy_retry_policy(url, instance_tenant_id)

    if not isinstance(url, str) or not isinstance(method, str) or not isinstance(headers, dict):
        return None
    return {
        "event_rule_id": rule.pk,
        "target_url": url,
        "target_http_method": method.upper(),
        "target_headers": dict(headers),
        "target_secret": _encrypted_target_secret(secret),
        "target_enabled": target_enabled,
        "target_tenant_id": target_tenant_id,
        "target_retry_count": retry_count,
        "target_retry_backoff": retry_backoff,
    }


def _delivery_assertions(delivery):
    """Package the durable identity of ``delivery`` for the worker."""
    return WebhookDeliveryAssertions(
        delivery_pk=delivery.pk,
        delivery_id=UUID(str(delivery.delivery_id)),
        webhook_endpoint_id=delivery.endpoint_id,
        event_id=delivery.event_id,
        tenant_id=delivery.tenant_id,
        test_send=delivery.test_send,
    )


def _enqueue_delivery(delivery, *, actor_id=None, request_id=None):
    """Enqueue the webhook worker for an already-persisted delivery row."""
    if actor_id is None:
        actor_id = getattr(get_current_user(), "pk", None)
    if request_id is None:
        request_id = str(get_current_request_id()) if get_current_request_id() is not None else None
    assertions = _delivery_assertions(delivery)
    task_kwargs = {"actor_id": actor_id, "request_id": request_id}
    if getattr(settings, "Q_CLUSTER", {}).get("sync", False):
        async_task(WEBHOOK_TASK_PATH, assertions, **task_kwargs)
    else:
        transaction.on_commit(lambda: async_task(WEBHOOK_TASK_PATH, assertions, **task_kwargs))


def _render_template(template, event):
    """Render an admin-supplied notification template.

    Preserves the historical ``{event.action}`` / ``{event.model.model}`` /
    ``{data[...]}`` placeholder syntax, but binds ``event`` to a sanitized
    namespace of plain scalars instead of the live ORM instance. ``str.format``
    permits attribute/index traversal of its arguments (e.g.
    ``{event.save.__func__.__globals__[...]}``), so handing it the ORM object is
    an information-disclosure vector for anyone who can edit a rule's
    ``action_config``. A nested SimpleNamespace of strings has no such gadget.
    """
    if not template:
        return template

    safe_event = SimpleNamespace(
        action=str(event.action),
        object_id=str(event.object_id),
        model=SimpleNamespace(
            model=str(event.model.model),
            app_label=str(event.model.app_label),
        ),
        data=event.data,
    )
    try:
        return template.format(event=safe_event, data=event.data)
    except (KeyError, ValueError, IndexError, AttributeError):
        return template


def _send_notification(rule, event):
    """Create an in-app notification based on the rule's action_config."""

    config = rule.action_config or {}
    level = config.get("level", "info")
    subject = config.get(
        "subject",
        _("Event: %(action)s on %(model)s")
        % {
            "action": event.action,
            "model": event.model.model,
        },
    )
    body = config.get("body", str(event.data))

    # Render against a sanitized namespace (see _render_template) so an
    # attacker-editable action_config can't traverse a live ORM object.
    subject = _render_template(subject, event)
    body = _render_template(body, event)

    target_url = ""
    try:
        model_class = event.model.model_class()
        if model_class and hasattr(model_class, "get_absolute_url"):
            instance = model_class.objects.filter(pk=event.object_id).first()
            if instance:
                target_url = instance.get_absolute_url()
    except (NoReverseMatch, ValueError):
        # An object id the model cannot parse, or a model without a routable detail
        # view, leaves the notification without a link rather than failing the dispatch.
        target_url = ""

    if rule.tenant_id:
        # A tenant-scoped rule must fan out to the rule's tenant members, NOT create a global
        # user=None row that any authenticated user could open by pk (cross-tenant leak of the
        # rule's subject/body + the target object's URL). Mirrors the IN_APP channel branch.
        User = get_user_model()
        users = User.objects.filter(memberships__tenant_id=rule.tenant_id, is_active=True).distinct()
        Notification.objects.bulk_create(
            [Notification(user=u, subject=subject, message=body, level=level, target_url=target_url) for u in users]
        )
    else:
        # Truly system-wide (tenant=None) rule may broadcast.
        Notification.objects.create(
            user=None,
            subject=subject,
            message=body,
            level=level,
            target_url=target_url,
        )
