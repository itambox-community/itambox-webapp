"""Event and watcher receivers owned by the extras domain.

``ExtrasConfig.ready()`` imports this module once so the receivers below are
connected after the app registry is populated. Importing it must never query the
ORM: every table access happens inside a receiver, behind the preflight below.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from core.mixins import SoftDeleteMixin
from core.models import ChangeLoggingMixin, Notification
from core.schedules import SCHEDULED_REPORT_TASK_PATH, remove_schedule
from extras.models import EventRule, ObjectWatch, ScheduledReport
from extras.services.events import dispatch_event, invalidate_rule_index

logger = logging.getLogger(__name__)

# Stable dispatch identifiers: a repeated ``ready()`` (tests swapping
# INSTALLED_APPS, a module reload) must reconnect the same receiver rather than
# add a second copy that would double every event and notification.
_PRE_SAVE_UID = "extras.signals.capture_prior_soft_delete_state.v1"
_POST_SAVE_UID = "extras.signals.event_on_save.v1"
_POST_DELETE_UID = "extras.signals.event_on_delete.v1"
_RULE_SAVE_UID = "extras.signals.invalidate_rule_index_on_save.v1"
_RULE_DELETE_UID = "extras.signals.invalidate_rule_index_on_delete.v1"

# Models excluded from event dispatch + watcher-notify. Recursion guards
# (ObjectChange/Event/Notification) plus high-frequency operational/archive rows
# (Job lifecycle, scheduled-report archives) that would otherwise flood
# EventRules/webhooks on every status flip. (Bookmark/ObjectWatch/Event no longer
# inherit ChangeLoggingMixin, so the issubclass guard already skips them; their
# entries are kept here as defensive belt-and-suspenders.)
_SIGNAL_SKIP_MODELS = frozenset(
    (
        "Event",
        "ObjectChange",
        "JournalEntry",
        "Notification",
        "Bookmark",
        "ObjectWatch",
        "Job",
        "ReportGenerationArchive",
    )
)

# Sentinel distinguishing "no prior row was looked up" (create, or a non-soft-delete
# model) from a prior row whose deleted_at was genuinely None.
_NO_PRIOR = object()

# Tables confirmed to exist, cached for the lifetime of the process.  Positive only:
# a table that exists will always exist; a missing table is re-checked each time.
_TABLES_CONFIRMED: set = set()


def _table_exists(name: str) -> bool:
    if name in _TABLES_CONFIRMED:
        return True

    if name in connection.introspection.table_names():
        _TABLES_CONFIRMED.add(name)
        return True
    return False


@receiver(pre_save, dispatch_uid=_PRE_SAVE_UID)
def capture_prior_soft_delete_state(sender, instance, **kwargs):
    """Stash the DB's prior ``deleted_at`` so ``event_on_save`` can detect a transition.

    Read through ``_base_manager`` (unscoped): the prior row may be hidden from the
    default manager by the active tenant context or the soft-delete filter, but the
    transition delete -> set / set -> None still has to be observed precisely.
    """
    if kwargs.get("raw"):
        return
    if not isinstance(instance, SoftDeleteMixin):
        return
    if instance._state.adding or instance.pk is None:
        instance._presave_deleted_at = _NO_PRIOR
        return
    instance._presave_deleted_at = (
        sender._base_manager.filter(pk=instance.pk).values_list("deleted_at", flat=True).first()
    )


def _resolve_save_action(instance, created):
    """Map a save to ('create' | 'update' | 'delete' | 'restore'), watch-verb pair.

    A soft-delete is precise: it fires 'delete' only on the None -> set transition,
    a distinct 'restore' on the set -> None transition (so a restore does not
    masquerade as an 'update'; 'restore' is a declared Event.ACTION_CHOICES value so
    EventRules can subscribe to it), and a plain 'update' when editing an
    already-archived row (no deleted_at transition) so 'delete' is not re-emitted.
    """
    if created:
        return "create", "created"

    if not isinstance(instance, SoftDeleteMixin):
        return "update", "updated"

    prior = getattr(instance, "_presave_deleted_at", _NO_PRIOR)
    now_deleted = instance.deleted_at is not None

    # No prior row was resolved (e.g. an out-of-band save); fall back to the current
    # state so an archived row still reports 'delete' rather than masquerading as an edit.
    if prior is _NO_PRIOR:
        return ("delete", "deleted") if now_deleted else ("update", "updated")

    was_deleted = prior is not None
    if now_deleted and not was_deleted:
        return "delete", "deleted"
    if was_deleted and not now_deleted:
        return "restore", "restored"
    # set -> set (archived-row edit) or None -> None: an ordinary update.
    return "update", "updated"


@receiver(post_save, dispatch_uid=_POST_SAVE_UID)
def event_on_save(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    if not issubclass(sender, ChangeLoggingMixin):
        return
    if sender.__name__ in _SIGNAL_SKIP_MODELS:
        return

    action, watch_action = _resolve_save_action(instance, created)
    transaction.on_commit(lambda: _safe_dispatch(sender, instance, action, created))

    # Notify watchers (ObjectWatch subscribers only — bookmarks no longer notify).
    # Deferred to on_commit so a rolled-back save spends no has_perm work and writes
    # no Notification rows.
    _defer_notify_watchers(sender, instance, watch_action)


@receiver(post_save, sender=EventRule, dispatch_uid=_RULE_SAVE_UID)
@receiver(post_delete, sender=EventRule, dispatch_uid=_RULE_DELETE_UID)
def invalidate_event_rule_index(sender, **kwargs):
    """Rule writes change which events are worth recording: drop the cached index now and at commit."""
    invalidate_rule_index()
    transaction.on_commit(invalidate_rule_index)


@receiver(post_delete, dispatch_uid=_POST_DELETE_UID)
def event_on_delete(sender, instance, **kwargs):
    if not issubclass(sender, ChangeLoggingMixin):
        return
    if sender.__name__ in _SIGNAL_SKIP_MODELS:
        return
    object_id = instance.pk
    transaction.on_commit(lambda: _safe_dispatch(sender, instance, "delete", object_id=object_id))

    # Notify watchers (deferred to on_commit — see event_on_save).
    _defer_notify_watchers(sender, instance, "deleted")


def _defer_notify_watchers(sender, instance, action):
    """Schedule watcher notification for after the triggering transaction commits."""
    try:
        transaction.on_commit(lambda: _notify_watchers(sender, instance, action))
    except Exception:
        # No active transaction (autocommit): run inline so the notification still fires.
        logger.debug("Failed to defer watcher notification; notifying inline", exc_info=True)
        try:
            _notify_watchers(sender, instance, action)
        except Exception as e:
            logger.debug("Failed to notify watchers: %s", e)


def _safe_dispatch(sender, instance, action, created=None, *, object_id=None):
    event_object_id = object_id if object_id is not None else getattr(instance, "pk", None)
    try:
        if not _table_exists("extras_event"):
            return
        dispatch_event(sender, instance, action=action, created=created, object_id=object_id)
    # broad except: boundary-isolation: a failed event dispatch must never invalidate the audited write that triggered it
    except Exception as error:
        logger.error(
            "Event dispatch failed model=%s object_id=%s action=%s error_class=%s",
            sender.__name__,
            event_object_id,
            action,
            type(error).__name__,
        )


def _notify_watchers(sender, instance, action):
    # Pre-flight table check: avoids aborting the Postgres transaction (unlike catching
    # DatabaseError after the fact, which leaves the connection in "transaction aborted" state).
    if not _table_exists("extras_objectwatch"):
        return

    ct = ContentType.objects.get_for_model(sender)
    watches = ObjectWatch.objects.filter(model=ct, object_id=instance.pk).select_related("user")

    if not watches.exists():
        return

    target_url = ""
    if action != "deleted" and hasattr(instance, "get_absolute_url"):
        try:
            target_url = instance.get_absolute_url()
        except Exception:
            pass

    subject = _("Watched item %(action)s: %(instance)s") % {"action": action, "instance": instance}
    message = _("The item '%(instance)s' you are watching has been %(action)s.") % {
        "instance": instance,
        "action": action,
    }
    level = "warning" if action == "deleted" else "info"

    app_label = ct.app_label
    model_name = ct.model
    perm = f"{app_label}.view_{model_name}"

    notifications = []
    for watch in watches:
        # Re-verify the watcher still has view access before sending — watches outlive
        # tenant-membership removal, so skip silently if access was revoked.
        if not watch.user.has_perm(perm, instance):
            continue
        notifications.append(
            Notification(
                user=watch.user,
                subject=subject,
                message=message,
                level=level,
                target_url=target_url,
            )
        )

    if notifications:
        # One INSERT for the surviving watchers instead of O(watchers) round-trips.
        Notification.objects.bulk_create(notifications)


# -- Scheduled-report registration cleanup ------------------------------------
# The domain model cannot import the platform scheduler directly (R-X3), and a
# queryset delete never runs ``ScheduledReport.delete()``, so the name-keyed
# registration removal lives here: it fires for every deletion path (instance,
# queryset, admin bulk) under the same advisory lock registration uses.
_SCHEDULED_REPORT_CLEANUP_UID = "extras.signals.remove_scheduled_report_registration.v1"


@receiver(post_delete, sender=ScheduledReport, dispatch_uid=_SCHEDULED_REPORT_CLEANUP_UID)
def remove_scheduled_report_registration(sender, instance, **kwargs):
    remove_schedule(SCHEDULED_REPORT_TASK_PATH, name=f"scheduled_report_{instance.pk}")
