"""Realistic change-history engine for the seed command.

The seed runs in a management command with no HTTP request, so ChangeLoggingMixin
records nothing on its own. This engine produces ObjectChange rows that read like
genuine user actions:

- ``change()`` drives a real ``snapshot() -> mutate -> save()`` inside a
  ``TaskContext`` (which wires the user/tenant/request_id contextvars), so the
  ObjectChange carries a real prechange/postchange diff produced by the SAME
  serializer the live app uses. No hand-written field dicts.
- ``log_create()`` records a back-dated ``create`` entry whose ``postchange_data``
  is ``serialize_object(obj)`` — byte-identical to what the live create path would
  have logged at creation time.

Every entry is back-dated (``ObjectChange.time`` + the object's
``created_at``/``updated_at``) to a simulated moment so the history reads as
naturally grown over the MSP's lifetime rather than all-at-once.
"""

import datetime
import uuid

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.utils import timezone

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from core.serialization import serialize_object
from core.tasks.context import TaskContext


def as_aware_datetime(when):
    """Coerce a date / naive datetime / None into an aware datetime."""
    if when is None:
        dt = timezone.now()
    elif isinstance(when, datetime.datetime):
        dt = when
    else:  # datetime.date
        dt = datetime.datetime(when.year, when.month, when.day, 10, 0)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    # History must never be dated in the future — a record created for a future
    # event (e.g. a forward-dated reservation/contract) was still entered "now".
    now = timezone.now()
    if dt > now:
        dt = now
    return dt


class ChangeLogEngine:
    """Drives realistic, back-dated change history. Construct one per seed run."""

    def __init__(self, stdout=None, style=None):
        self.stdout = stdout
        self.style = style
        self.count = 0

    # ── internals ────────────────────────────────────────────────────
    @staticmethod
    def _tenant_of(obj):
        return getattr(obj, "tenant", None)

    @staticmethod
    def _reload(obj):
        """Re-read ``obj`` so DB-typed values replace the seed's plain Python ones.

        An object deleted since it was created simply keeps its in-memory state.
        """
        try:
            obj.refresh_from_db()
        except ObjectDoesNotExist:
            return False
        return True

    @staticmethod
    def _field_names(obj):
        return {f.name for f in obj._meta.fields}

    def _backdate_timestamps(self, obj, when, *, set_created=False):
        """Back-date created_at/updated_at via the base manager so auto_now /
        auto_now_add (and tenant scoping) don't override them."""
        fields = {}
        names = self._field_names(obj)
        if set_created and "created_at" in names:
            fields["created_at"] = when
        if "updated_at" in names:
            fields["updated_at"] = when
        if fields:
            type(obj)._base_manager.filter(pk=obj.pk).update(**fields)

    @staticmethod
    def _latest_change_pk(ct, obj):
        return (
            ObjectChange._base_manager.filter(changed_object_type=ct, changed_object_id=obj.pk)
            .order_by("-pk")
            .values_list("pk", flat=True)
            .first()
        ) or 0

    @staticmethod
    def _save_object_change(obj, action):
        """Permit only simulated checkout/check-in history to cross deployed."""
        operation = action if action in ("checkout", "checkin") else None
        if operation is None or obj._meta.label_lower != "assets.asset":
            obj.save()
            return

        sentinel = object()
        original = getattr(obj, "_asset_status_transition", sentinel)
        obj._asset_status_transition = operation
        try:
            obj.save()
        finally:
            if original is sentinel:
                del obj._asset_status_transition
            else:
                obj._asset_status_transition = original

    # ── public API ───────────────────────────────────────────────────
    def change(self, obj, *, when, user, action="update", **field_updates):
        """Apply ``field_updates`` through a real save so the resulting ObjectChange
        has a genuine diff, then back-date it. No-op updates (no field actually
        changed) produce no entry and are skipped. ``action`` may be a custom
        ObjectChangeActionChoices value (e.g. 'checkout', 'checkin', 'audit').
        Returns ``obj``.
        """
        when = as_aware_datetime(when)
        # Re-load DB-typed values (the seed assigns plain floats for money; the DB
        # round-trips them to Decimal). Without this, model logic like
        # compute_book_value() hits ``float / Decimal`` on the stale in-memory object.
        self._reload(obj)
        ct = ContentType.objects.get_for_model(type(obj))
        before_pk = self._latest_change_pk(ct, obj)
        tenant = self._tenant_of(obj)
        tenant_id = tenant.id if tenant is not None else None

        with TaskContext(tenant_id=tenant_id, user_id=getattr(user, "id", None)):
            if hasattr(obj, "snapshot"):
                obj.snapshot()
            for key, value in field_updates.items():
                setattr(obj, key, value)
            custom = action not in (
                ObjectChangeActionChoices.ACTION_CREATE,
                ObjectChangeActionChoices.ACTION_UPDATE,
            )
            if custom:
                obj._changelog_action = action
            saved = True
            try:
                self._save_object_change(obj, action)
            except ValidationError:
                # A model constraint (e.g. the Asset status state machine, or a
                # custom validator fired via pre_save) rejected this change.
                # Treat it as a no-op — a real user could not have taken this
                # action either — so the seed never crashes on it.
                saved = False
            finally:
                obj._changelog_action = None

        if not saved:
            self._reload(obj)
            return obj

        oc = (
            ObjectChange._base_manager.filter(changed_object_type=ct, changed_object_id=obj.pk, pk__gt=before_pk)
            .order_by("-pk")
            .first()
        )
        if oc is not None:
            ObjectChange._base_manager.filter(pk=oc.pk).update(time=when)
            self._backdate_timestamps(obj, when)
            self.count += 1
        return obj

    def log_create(self, obj, *, when, user):
        """Record an authentic, back-dated ``create`` entry for an already-created
        object, using the same serializer the live create path uses. Also back-dates
        the object's created_at/updated_at to ``when``. Returns ``obj``.
        """
        when = as_aware_datetime(when)
        self._reload(obj)  # DB-typed values for an accurate serialized snapshot
        ct = ContentType.objects.get_for_model(type(obj))
        excluded = getattr(obj, "_change_logging_excluded_fields", ["updated_at"])
        tenant = self._tenant_of(obj)
        ObjectChange._base_manager.create(
            tenant=tenant,
            user=user,
            user_name=(user.get_username() if user else "System"),
            request_id=uuid.uuid4(),
            action=ObjectChangeActionChoices.ACTION_CREATE,
            changed_object_type=ct,
            changed_object_id=obj.pk,
            object_repr=str(obj)[:200],
            object_type_repr=f"{ct.app_label} | {ct.model}",
            prechange_data=None,
            postchange_data=serialize_object(obj, exclude_fields=excluded),
            time=when,
        )
        self._backdate_timestamps(obj, when, set_created=True)
        self.count += 1
        return obj

    def touch_created(self, obj, when):
        """Back-date created_at/updated_at to ``when`` WITHOUT emitting a changelog
        entry — for aging base records that don't need a narrated history."""
        self._backdate_timestamps(obj, as_aware_datetime(when), set_created=True)
        return obj
