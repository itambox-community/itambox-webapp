# Standard library
from __future__ import annotations

import contextvars
import logging
from collections import Counter
from operator import attrgetter

# Third-party / Django
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, router, transaction
from django.db.models import signals, sql
from django.db.models.deletion import Collector
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.context import get_current_request_id, get_current_user
from core.managers import (
    ExplicitScopeManager,
)
from core.mixins import (
    CloneableMixin,
    ExportableMixin,
    FileAttachmentMixin,
    ImageAttachmentMixin,
    JournalingMixin,
    SoftDeleteMixin,
    TaggableMixin,
)
from core.oidc_identity import oidc_audit_excluded_fields, oidc_audit_object_repr
from core.serialization import serialize_object
from core.tenant_scope import get_descendant_tenant_group_ids

# Local application
from itambox.registry import registry

logger = logging.getLogger(__name__)


class _AuthorizedCollector(Collector):
    """Django 5.2 Collector that writes through raw SQL queries.

    Hard delete must reach rows whose queryset ``update()``/``delete()`` are
    denied (the inventory assignment querysets), so the fast-delete and
    field-update steps use ``sql`` queries directly instead of a permit.
    """

    def delete(self):  # noqa: C901 - mirrors django.db.models.deletion.Collector.delete
        for model, instances in self.data.items():
            self.data[model] = sorted(instances, key=attrgetter("pk"))
        self.sort()
        deleted_counter = Counter()

        if len(self.data) == 1 and len(instances) == 1:
            instance = list(instances)[0]
            if self.can_fast_delete(instance):
                with transaction.mark_for_rollback_on_error(self.using):
                    count = sql.DeleteQuery(model).delete_batch([instance.pk], self.using)
                setattr(instance, model._meta.pk.attname, None)
                return count, {model._meta.label: count}

        with transaction.atomic(using=self.using, savepoint=False):
            for model, obj in self.instances_with_model():
                if not model._meta.auto_created:
                    signals.pre_delete.send(
                        sender=model,
                        instance=obj,
                        using=self.using,
                        origin=self.origin,
                    )

            for queryset in self.fast_deletes:
                count = sql.DeleteQuery(queryset.model).delete_batch(
                    list(queryset.values_list("pk", flat=True)), self.using
                )
                if count:
                    deleted_counter[queryset.model._meta.label] += count

            for (field, value), instances_list in self.field_updates.items():
                pks = set()
                model = None
                for instances in instances_list:
                    model = field.model
                    if isinstance(instances, models.QuerySet):
                        pks.update(instances.values_list("pk", flat=True))
                    else:
                        pks.update(obj.pk for obj in instances)
                if pks:
                    sql.UpdateQuery(model).update_batch(list(pks), {field.name: value}, self.using)

            for instances in self.data.values():
                instances.reverse()

            for model, instances in self.data.items():
                query = sql.DeleteQuery(model)
                pk_list = [obj.pk for obj in instances]
                count = query.delete_batch(pk_list, self.using)
                if count:
                    deleted_counter[model._meta.label] += count
                if not model._meta.auto_created:
                    for obj in instances:
                        signals.post_delete.send(
                            sender=model,
                            instance=obj,
                            using=self.using,
                            origin=self.origin,
                        )

        for model, instances in self.data.items():
            for instance in instances:
                setattr(instance, model._meta.pk.attname, None)
        return sum(deleted_counter.values()), dict(deleted_counter)


def _hard_delete_instance(instance, using):
    collector = _AuthorizedCollector(using=using, origin=instance)
    collector.collect([instance])
    return collector.delete()


class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        from core.validators import CustomValidator

        CustomValidator.validate_object(self)


class ObjectChange(models.Model):
    # Tenant-scoped so one tenant cannot read another tenant's change history
    # (which includes full field-level prechange/postchange snapshots). Rows with
    # tenant=None are system/global changes (global catalogue / system config);
    # allow_global_tenant below makes those — and only those — visible to every
    # tenant, mirroring how the global objects themselves are shared.
    # Explicit scope: readers call ``ObjectChange.objects.for_scope(Scope.current())``.
    objects = ExplicitScopeManager()
    allow_global_tenant = True

    tenant = models.ForeignKey(
        to="organization.Tenant",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Tenant"),
    )
    time = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="changes",
        blank=True,
        null=True,
        verbose_name=_("User"),
    )
    user_name = models.CharField(max_length=150, editable=False)
    request_id = models.UUIDField(editable=False)
    action = models.CharField(
        max_length=50,
        choices=ObjectChangeActionChoices(),
        db_index=True,
        verbose_name=_("Action"),
    )
    changed_object_type = models.ForeignKey(to=ContentType, on_delete=models.PROTECT, related_name="+")
    changed_object_id = models.PositiveBigIntegerField(db_index=True)
    changed_object = GenericForeignKey(ct_field="changed_object_type", fk_field="changed_object_id")
    related_object_type = models.ForeignKey(
        to=ContentType, on_delete=models.PROTECT, related_name="+", blank=True, null=True
    )
    related_object_id = models.PositiveBigIntegerField(blank=True, null=True, db_index=True)
    related_object = GenericForeignKey(ct_field="related_object_type", fk_field="related_object_id")
    object_repr = models.CharField(max_length=200, editable=False)
    prechange_data = models.JSONField(editable=False, blank=True, null=True)
    postchange_data = models.JSONField(editable=False, blank=True, null=True)
    object_type_repr = models.CharField(max_length=100, editable=False, blank=True, null=True)

    class Meta:
        ordering = ["-time"]
        indexes = [
            models.Index(fields=["changed_object_type", "changed_object_id"]),
            models.Index(fields=["related_object_type", "related_object_id"]),
        ]

    def __str__(self):
        action_label = self.get_action_display()
        return (
            f"{self.changed_object_type} {self.object_repr} {action_label} by "
            f"{self.user_name} at {self.time:%Y-%m-%d %H:%M:%S}"
        )

    def get_absolute_url(self):
        return reverse("objectchange", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.object_type_repr:
            self.object_type_repr = f"{self.changed_object_type.app_label} | {self.changed_object_type.model}"
        super().save(*args, **kwargs)

    def get_changed_object_url(self):
        if hasattr(self.changed_object, "get_absolute_url"):
            return self.changed_object.get_absolute_url()
        return None


# Per-request cache of validated user pks for the changelog. Keyed on request_id
# so it auto-invalidates when a new request/task begins (no cross-request leak).
_user_validation_cache = contextvars.ContextVar("user_validation_cache", default=None)


def write_object_change(
    *,
    instance: object,
    action: str,
    user: object | None,
    request_id: object,
    change_tenant: object | None,
    prechange_data: dict[str, object] | None = None,
    postchange_data: dict[str, object] | None = None,
) -> object:
    """Create the ``ObjectChange`` audit row for a logged change.

    Single source of truth for the audit-row payload shape, shared by
    ``ChangeLoggingMixin._log_change`` and the auth-``User`` signal receivers
    (``users/signals.py`` — ``User`` cannot inherit the mixin). Each caller resolves
    ``user``/``change_tenant`` itself (their rules differ) and passes them in; this builds
    the row from the instance.
    """
    ct = ContentType.objects.get_for_model(instance.__class__)
    return ObjectChange._base_manager.create(
        tenant=change_tenant,
        user=user,
        user_name=user.username if user else "System",
        request_id=request_id,
        action=action,
        changed_object_type=ct,
        changed_object_id=instance.pk,
        object_repr=oidc_audit_object_repr(
            instance=instance,
            app_label=ct.app_label,
            model=ct.model,
            default=str(instance)[:200],
        ),
        object_type_repr=f"{ct.app_label} | {ct.model}",
        prechange_data=prechange_data,
        postchange_data=postchange_data,
    )


class ChangeLoggingMixin:
    _change_logging_excluded_fields = ["updated_at"]

    # Optional ORM path (e.g. 'asset__tenant') used to derive the changelog
    # tenant for models that carry no `tenant` field/property of their own.
    # Resolved on the instance before the ambient-request-tenant fallback so the
    # change is attributed to the object's owning tenant rather than whichever
    # tenant happens to be active on the request.
    changelog_tenant_lookup = None

    # Tenant-less global/shared models (catalogue, system config) set this True so
    # their changes are attributed to tenant=None — recorded once, system-wide —
    # instead of the ambient request tenant (which fragments the history across
    # whichever tenant happened to be active). ObjectChange.allow_global_tenant=True
    # then surfaces these tenant=None rows to every tenant (shared-catalogue pattern).
    changelog_global = False

    def __init__(self, *args, **kwargs):
        self._changelog_action = None
        self._changelog_message = ""
        self._prechange_snapshot = None
        super().__init__(*args, **kwargs)

    def _serialize_for_change(self, instance):
        return serialize_object(
            instance,
            exclude_fields=oidc_audit_excluded_fields(self._change_logging_excluded_fields),
        )

    def snapshot(self):
        self._prechange_snapshot = self._serialize_for_change(self)

    def _log_change(self, action, prechange_data=None, postchange_data=None, message="", user=None):
        # ``user`` overrides the acting user for this row (e.g. an explicit actor
        # passed to log_m2m_change); otherwise the ambient request user is used.
        if user is None:
            user = get_current_user()
        request_id = get_current_request_id()

        if not request_id:
            self._handle_missing_change_context(action)
            return

        if user:
            cache = _user_validation_cache.get()
            if cache is None or cache[0] != request_id:
                cache = (request_id, set())
                _user_validation_cache.set(cache)
            validated = cache[1]
            if user.pk not in validated:
                from django.contrib.auth import get_user_model

                User = get_user_model()
                if User.objects.filter(pk=user.pk).exists():
                    validated.add(user.pk)
                else:
                    user = None

        # Attribute the change to a tenant so the changelog can be scoped. Prefer
        # the changed object's own tenant (works for both direct `tenant` fields
        # and relation-derived `tenant` properties); next follow an explicit
        # `changelog_tenant_lookup` ORM path for objects whose owning tenant lives
        # on a relation (e.g. AssetAudit -> asset.tenant); finally fall back to the
        # active request tenant for objects that carry no tenant of their own.
        from core.managers import get_current_tenant

        try:
            change_tenant = getattr(self, "tenant", None)
        except Exception:
            change_tenant = None
        if change_tenant is None and self.changelog_tenant_lookup:
            change_tenant = self._resolve_changelog_tenant(self.changelog_tenant_lookup)
        # Shared models (changelog_global) record changes against tenant=None
        # (system-wide, surfaced to all tenants via ObjectChange.allow_global_tenant);
        # everything else — including tenant-group-scoped rows, which are shared
        # with a bounded audience rather than system-wide — falls back to the
        # ambient request tenant so snapshots never leak across that audience.
        if change_tenant is None and not self._logs_changes_globally():
            change_tenant = get_current_tenant()

        # Tenant-group-scoped rows edited with no single-tenant anchor (a group
        # workspace, a background task) must not become tenant=None "global"
        # rows either: fan the audit row out to every live tenant in the group's
        # subtree so the snapshot stays visible to exactly that bounded audience.
        self._write_audit_rows(
            action=action,
            user=user,
            request_id=request_id,
            change_tenant=change_tenant,
            prechange_data=prechange_data,
            postchange_data=postchange_data,
        )

    def _handle_missing_change_context(self, action):
        """Refuse to silently drop an audit entry when no execution context is set.

        Audited writes run inside a request or a ``TaskContext`` (state-changing
        management commands enter one via ``SystemTaskCommand``); a missing
        request id therefore means a code path forgot to establish its scope,
        not a legitimate no-op. Dev and test runs raise so the gap fails loudly;
        production keeps serving and logs at WARNING, because a broken execution
        context must not take the instance down.
        """
        if settings.DEBUG or getattr(settings, "IS_TESTING", False):
            raise RuntimeError(
                "Change logging attempted without an execution context "
                f"(model={self.__class__.__name__} object_id={self.pk} action={action}). "
                "Enter a request or TaskContext instead of skipping the audit entry."
            )
        logger.warning(
            "Change log entry skipped: no execution context model=%s object_id=%s action=%s",
            self.__class__.__name__,
            self.pk,
            action,
        )

    def _write_audit_rows(self, *, action, user, request_id, change_tenant, prechange_data, postchange_data):
        for audience_tenant in self._audit_row_tenants(change_tenant):
            write_object_change(
                instance=self,
                action=action,
                user=user,
                request_id=request_id,
                change_tenant=audience_tenant,
                prechange_data=prechange_data,
                postchange_data=postchange_data,
            )

    def _logs_changes_globally(self):
        """True when this row's changes belong system-wide (tenant=None).

        ``changelog_global`` models log without a tenant, but a tenant-group-scoped
        row (tenant=None, tenant_group set) is shared with a bounded audience,
        not system-wide: those attribute to the ambient request tenant so their
        full pre/post snapshots never surface to every tenant through
        ``ObjectChange.allow_global_tenant``.
        """
        if getattr(self, "tenant_group_id", None) is not None:
            return False
        return bool(self.changelog_global)

    def _audit_row_tenants(self, change_tenant):
        """The tenant(s) this change's audit row(s) are written against.

        ``[None]`` (one system-wide row) only for genuine global/shared changes.
        A tenant-group-scoped row that resolved to no single-tenant anchor fans
        out to every live tenant of its group's subtree (descendant groups
        included) — never a tenant=None row for a bounded-audience object.
        """
        if change_tenant is None and getattr(self, "tenant_group_id", None) is not None:
            return list(self._tenant_group_audience())
        return [change_tenant]

    def _tenant_group_audience(self):
        """Live tenants inside this row's tenant-group subtree."""
        Tenant = apps.get_model("organization", "Tenant")
        group_ids = get_descendant_tenant_group_ids(self.tenant_group_id)
        return Tenant._base_manager.filter(group_id__in=group_ids, deleted_at__isnull=True)

    def _resolve_changelog_tenant(self, lookup):
        # Follow a double-underscore ORM path (e.g. 'asset__tenant') on the
        # instance, walking already-loaded relations where possible. Returns None
        # null-safely if any hop is missing so callers fall through to the next
        # fallback rather than raising on orphaned/partial relations.
        obj = self
        for attr in lookup.split("__"):
            try:
                obj = getattr(obj, attr, None)
            except ObjectDoesNotExist:
                return None
            if obj is None:
                return None
        return obj

    def log_m2m_change(self, field_name, new_values, actor=None):
        """Apply an M2M ``.set()`` and record it in the changelog.

        ``save()``/``delete()`` cannot capture many-to-many edits: Django fires
        ``m2m_changed`` AFTER ``save()`` returns, so a plain ``field.set(...)``
        leaves the audit trail showing the pre-edit (often empty) set. Any AUDITED
        M2M mutation must go through here. It captures a full prechange snapshot
        (M2M ids included, via ``serialize_object``), applies the change, captures
        the postchange snapshot, and writes exactly ONE attributed ``ObjectChange``
        UPDATE — but only when the set actually changes, so an unchanged re-apply
        writes nothing (no duplicate/no-op rows). ``actor`` overrides the changelog
        user for this write (else the ambient request user). Returns ``True`` when a
        change was applied and logged, ``False`` on a no-op.
        """
        # Read the current + target ids through the UNSCOPED through table. Reading
        # the related manager (``field.values_list``) would apply the related
        # model's own manager — which for a tenant-scoped target (e.g. Tenant) fails
        # closed to nothing outside its tenant context, so the snapshot would show
        # an empty set even though rows exist. serialize_object() has the same blind
        # spot, so the M2M key is overridden with the true ids below.
        m2m_field = self._meta.get_field(field_name)
        through = m2m_field.remote_field.through
        source_fn = m2m_field.m2m_field_name()
        target_col = m2m_field.m2m_reverse_field_name() + "_id"
        current_ids = sorted(through._base_manager.filter(**{source_fn: self}).values_list(target_col, flat=True))
        target_ids = sorted({getattr(v, "pk", v) for v in (new_values or [])})
        if target_ids == current_ids:
            return False
        prechange = self._serialize_for_change(self)
        prechange[field_name] = current_ids
        getattr(self, field_name).set(list(new_values or []))
        postchange = self._serialize_for_change(self)
        postchange[field_name] = target_ids
        self._log_change(
            action=ObjectChangeActionChoices.ACTION_UPDATE,
            prechange_data=prechange,
            postchange_data=postchange,
            user=actor,
        )
        return True

    def save(self, *args, **kwargs):
        if not get_current_request_id():
            super().save(*args, **kwargs)
            return

        is_creation = self._state.adding
        prechange_data = None

        if not is_creation:
            if self._prechange_snapshot is not None:
                prechange_data = self._prechange_snapshot
            else:
                # Use the unfiltered base manager: the changelog must capture the
                # pre-change state even when the active tenant context or soft-delete
                # filters would hide the row from the default manager.
                original_instance = self.__class__._base_manager.filter(pk=self.pk).first()
                if original_instance is not None:
                    prechange_data = self._serialize_for_change(original_instance)

        super().save(*args, **kwargs)

        action = self._changelog_action or (
            ObjectChangeActionChoices.ACTION_CREATE if is_creation else ObjectChangeActionChoices.ACTION_UPDATE
        )
        postchange_data = self._serialize_for_change(self)

        if action == ObjectChangeActionChoices.ACTION_UPDATE and prechange_data == postchange_data:
            return

        self._log_change(
            action=action,
            prechange_data=prechange_data,
            postchange_data=postchange_data,
            message=self._changelog_message,
        )

    def delete(self, *args, **kwargs):
        # Resolve force_hard_delete from EITHER the kwarg (a direct
        # delete(force_hard_delete=True) call) OR a flag stashed on the instance
        # by SoftDeleteMixin earlier in the MRO. Re-stash it for the partner mixin
        # and POP the kwarg so it never reaches models.Model.delete() (which
        # rejects it). This makes the two mixins order-independent: a hard delete
        # is change-logged whether ChangeLoggingMixin or SoftDeleteMixin runs
        # first. (Never treat a positional arg as the flag: positionally,
        # delete()'s first arg is Django's `using`.)
        from core.mixins import SoftDeleteMixin

        force_hard = bool(kwargs.pop("force_hard_delete", False)) or getattr(self, "_force_hard_delete", False)
        self._force_hard_delete = force_hard

        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)

        if not get_current_request_id():
            if force_hard:
                _hard_delete_instance(self, using)
            else:
                super().delete(*args, **kwargs)
            return

        is_soft_delete = isinstance(self, SoftDeleteMixin) and not force_hard
        if is_soft_delete:
            super().delete(*args, **kwargs)
            return

        if self._prechange_snapshot is not None:
            prechange_data = self._prechange_snapshot
        else:
            prechange_data = self._serialize_for_change(self)
        action = self._changelog_action or ObjectChangeActionChoices.ACTION_DELETE

        self._log_change(action=action, prechange_data=prechange_data, message=self._changelog_message)

        if force_hard:
            _hard_delete_instance(self, using)
        else:
            super().delete(*args, **kwargs)

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "change_logging")

    def get_changelog_url(self):
        ct = ContentType.objects.get_for_model(self.__class__)
        return (
            reverse("objectchange_list")
            + "?"
            + urlencode(
                {
                    "changed_object_type": ct.pk,
                    "changed_object_id": self.pk,
                }
            )
        )


class Notification(models.Model):
    LEVEL_INFO = "info"
    LEVEL_WARNING = "warning"
    LEVEL_SUCCESS = "success"
    LEVEL_DANGER = "danger"

    LEVEL_CHOICES = [
        (LEVEL_INFO, "Info"),
        (LEVEL_WARNING, "Warning"),
        (LEVEL_SUCCESS, "Success"),
        (LEVEL_DANGER, "Danger"),
    ]

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
        help_text=_("Target user for the notification. Null represents global broadcast alert."),
        verbose_name=_("User"),
    )
    subject = models.CharField(max_length=255, verbose_name=_("Subject"))
    message = models.TextField(verbose_name=_("Message"))
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default=LEVEL_INFO, verbose_name=_("Level"))
    is_read = models.BooleanField(default=False, verbose_name=_("Is Read"))
    target_url = models.CharField(
        max_length=500, blank=True, help_text=_("Optional destination URL when clicked."), verbose_name=_("Target URL")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
        indexes = [
            models.Index(fields=["user", "is_read"]),
        ]

    def __str__(self):
        return f"{self.subject} ({self.get_level_display()})"


class Job(ChangeLoggingMixin, BaseModel):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = JobStatusChoices()

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    # Tenant the job was enqueued for. Null means a system-level job
    # (e.g. management commands); those are only visible to superusers.
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="jobs",
        null=True,
        blank=True,
        verbose_name=_("Tenant"),
    )
    model = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    content_object = GenericForeignKey("model", "object_id")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True, verbose_name=_("Status")
    )
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Data"))
    result = models.JSONField(null=True, blank=True, verbose_name=_("Result"))
    logs = models.TextField(blank=True, verbose_name=_("Logs"))
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    started = models.DateTimeField(null=True, blank=True, verbose_name=_("Started"))
    completed = models.DateTimeField(null=True, blank=True, verbose_name=_("Completed"))
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Scheduled For"))

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Job")
        verbose_name_plural = _("Jobs")
        indexes = [
            models.Index(fields=["status", "scheduled_for"]),
        ]

    def __str__(self):
        return f"Job: {self.name} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("job_detail", kwargs={"pk": self.pk})

    def mark_running(self):
        """
        Atomically transition pending -> running. Returns False if the job is
        no longer pending (e.g. it was cancelled before the worker picked it
        up), in which case the caller must not execute the task.
        """
        started = timezone.now()
        updated = Job.objects.filter(pk=self.pk, status=self.STATUS_PENDING).update(
            status=self.STATUS_RUNNING,
            started=started,
        )
        if not updated:
            self.refresh_from_db(fields=["status", "started", "logs"])
            return False
        self.status = self.STATUS_RUNNING
        self.started = started
        return True

    def mark_completed(self, result=None):
        self.status = self.STATUS_COMPLETED
        self.completed = timezone.now()
        if result is not None:
            self.result = result
        self.save(update_fields=["status", "completed", "result"])

    def cancel(self, reason=""):
        """
        Atomically cancel a still-pending job. Returns False if a worker
        already picked it up (or it has finished) — a running task cannot
        be stopped from here.
        """
        completed = timezone.now()
        logs = f"{self.logs}\n{reason}" if self.logs else str(reason)
        updated = Job.objects.filter(pk=self.pk, status=self.STATUS_PENDING).update(
            status=self.STATUS_FAILED,
            completed=completed,
            logs=logs,
        )
        if not updated:
            self.refresh_from_db(fields=["status", "started", "completed"])
            return False
        self.status = self.STATUS_FAILED
        self.completed = completed
        self.logs = logs
        return True

    def mark_failed(self, error=None):
        self.status = self.STATUS_FAILED
        self.completed = timezone.now()
        if error:
            self.logs = f"{self.logs}\n{error}" if self.logs else str(error)
        self.save(update_fields=["status", "completed", "logs"])

    def append_log(self, message):
        timestamp = timezone.now().isoformat()
        entry = f"[{timestamp}] {message}"
        self.logs = f"{self.logs}\n{entry}" if self.logs else entry
        self.save(update_fields=["logs"])


class EmailSettings(ChangeLoggingMixin, BaseModel):
    # Keep the encrypted SMTP password ciphertext out of the changelog JSON
    # (serialize_object would otherwise dump it into every ObjectChange row).
    _change_logging_excluded_fields = ["updated_at", "smtp_password"]
    # System-wide singleton (no tenant) — attribute changes globally, not to the
    # ambient request tenant.
    changelog_global = True

    smtp_host = models.CharField(max_length=255, default="localhost", verbose_name=_("SMTP Host"))
    smtp_port = models.PositiveIntegerField(default=25, verbose_name=_("SMTP Port"))
    smtp_use_tls = models.BooleanField(default=True, verbose_name=_("SMTP Use TLS"))
    smtp_username = models.CharField(max_length=255, blank=True, verbose_name=_("SMTP Username"))
    smtp_password = models.CharField(max_length=1000, blank=True, verbose_name=_("SMTP Password"))
    from_address = models.EmailField(max_length=255, default="itambox@localhost", verbose_name=_("From Address"))
    from_name = models.CharField(max_length=255, default="ITAMbox Notifications", verbose_name=_("From Name"))
    enabled = models.BooleanField(default=False, verbose_name=_("Enabled"))
    test_recipient = models.EmailField(
        max_length=255,
        blank=True,
        help_text=_("Email address for test notifications"),
        verbose_name=_("Test Recipient"),
    )

    class Meta:
        verbose_name = "Email Settings"
        verbose_name_plural = "Email Settings"

    def __str__(self):
        return f"Email Settings ({'Enabled' if self.enabled else 'Disabled'})"

    def save(self, *args, **kwargs):
        # System-wide singleton: one outbound SMTP config for the whole install.
        # Per-tenant *destinations* are configured on NotificationChannel.config['recipients'].
        self.pk = 1
        if self.smtp_password and not self.smtp_password.startswith("enc$"):
            from core.crypto import encrypt_string

            self.smtp_password = encrypt_string(self.smtp_password)
        super().save(*args, **kwargs)

    @property
    def smtp_password_decrypted(self) -> str:
        if not self.smtp_password:
            return ""
        if self.smtp_password.startswith("enc$"):
            from core.crypto import decrypt_string

            return decrypt_string(self.smtp_password)
        return self.smtp_password

    @classmethod
    def load(cls):
        return cls.objects.first()


class StandardModel(JournalingMixin, TaggableMixin, ExportableMixin, CloneableMixin, ChangeLoggingMixin, BaseModel):
    class Meta:
        abstract = True


class VaultModel(StandardModel, ImageAttachmentMixin, FileAttachmentMixin):
    class Meta:
        abstract = True


class DeletableVaultModel(VaultModel, SoftDeleteMixin):
    class Meta:
        abstract = True


class RecycleBin(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("view_recyclebin", _("Can view Recycle Bin")),
            ("change_recyclebin", _("Can restore from Recycle Bin")),
            ("delete_recyclebin", _("Can purge from Recycle Bin")),
        ]
