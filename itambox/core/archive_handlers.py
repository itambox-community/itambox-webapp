"""App-registered archive/restore handlers for the model-agnostic callers.

Soft delete is not always a single-row flag flip. An *aggregate root* (an asset
holder, an asset, a kit) owns child rows whose lifecycle has to move with it:
some must refuse the archive while they are still active, others must be
detached so that no live row keeps pointing at an archived parent, and the rows
touched need their own audit entries. Those decisions belong to the aggregate's
own domain service, never to a generic view.

The single delete view, the bulk delete view, the REST API delete path and the
recycle-bin restore views all resolve their model from the URL and therefore
cannot import a domain service. An app registers its aggregate service here
instead, exactly like ``core.purge_handlers`` does for hard purges. Every model
WITHOUT a registration keeps the plain leaf semantics (``obj.delete()`` /
``obj.restore()``), so a registration is opt-in and changes nothing else.

A registered handler is called as ``handler(obj, actor=..., request=...)`` and
returns whatever its own result type is (aggregate archive services return an
:class:`ArchiveResult`); the generic callers only distinguish success from a
refused operation.

This module is also the shared plumbing every aggregate service builds on, so a
new aggregate plugs in instead of copying the pilot (#619):

* :class:`ArchiveResult` and :class:`ArchiveBlocked` - the one result and
  refusal type of every aggregate service;
* :class:`ArchiveOperation` - the correlation handle of one archive operation;
* :func:`lock_aggregate_root` - the scoped, fail-closed row lock a service takes
  first;
* :class:`ArchiveRelation` / :class:`ArchiveBehaviour` - the per-aggregate
  behaviour table (ARCHIVE, REFUSE, DETACH, KEEP) that a registration must
  carry and that :func:`archive_table_problems` (system check ``core.E003``)
  verifies against the live model graph, so a relation added later cannot
  silently escape its aggregate's table.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.contrib.contenttypes.fields import GenericRelation
from django.core.exceptions import PermissionDenied, ValidationError

from core.context import get_current_request_id
from core.managers import Scope

#: ``handler(obj, *, actor, request)`` - ``actor``/``request`` are ``None`` for a
#: programmatic caller (a management command, a background task).
ArchiveHandler = Callable[..., Any]
RestoreHandler = Callable[..., Any]


class ArchiveBehaviour(enum.Enum):
    """What an aggregate archive does with one child relation (the #619 vocabulary)."""

    ARCHIVE = "archive"  # archived with the root through the child's own save()/service
    REFUSE = "refuse"  # the root archive is refused while the child is active
    DETACH = "detach"  # the link is nulled or the child closed; restore does not re-attach
    KEEP = "keep"  # evidence row stays untouched and keeps referencing the root


@dataclass(frozen=True)
class ArchiveRelation:
    """One row of an aggregate's behaviour table.

    :param relation: the reverse relation key, see :func:`reverse_relation_keys`.
    :param behaviour: the chosen :class:`ArchiveBehaviour`.
    :param note: the condition the behaviour applies under, in the wording of
        the reviewed design table (e.g. ``"while active; closed rows KEEP"``).
    """

    relation: str
    behaviour: ArchiveBehaviour
    note: str = ""


@dataclass(frozen=True)
class ArchiveResult:
    """What one archive operation moved.

    :param archived: rows archived: the aggregate root (0 or 1) plus the child
        rows archived with it.
    :param detached: live rows detached from the root.
    :param kept: evidence rows deliberately left referencing the root.
    :param operation_id: id of the :class:`ArchiveOperation`; ``None`` for a no-op.
    """

    archived: int = 0
    detached: int = 0
    kept: int = 0
    operation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ArchiveOperation:
    """Correlation handle of one archive operation.

    Every row an operation touches is written through its own ``save()`` or
    service under the acting request's ``request_id``. The operation adds the
    root identity and an id of its own, so the rows of one archive stay
    attributable and a later step can persist the id as a restore marker.
    """

    id: uuid.UUID
    root_label: str
    root_pk: Any
    request_id: uuid.UUID | None

    @classmethod
    def begin(cls, root) -> ArchiveOperation:
        return cls(
            id=uuid.uuid4(),
            root_label=root._meta.label,
            root_pk=root.pk,
            request_id=get_current_request_id(),
        )

    def detach_message(self) -> str:
        """Change-log message of a row detached from the root."""
        return f"Detached from {self.root_label} {self.root_pk}"

    def archive_message(self) -> str:
        """Change-log message of a child row archived with the root."""
        return f"Archived with {self.root_label} {self.root_pk}"


class AggregateArchiveBlocked(ValidationError):
    """A typed, user-facing refusal from an aggregate archive/restore service.

    Subclasses Django's ``ValidationError`` on purpose: the REST API maps that to
    HTTP 400 with the message list through
    ``itambox.api.exceptions.itambox_exception_handler`` (never a 500), and a view
    can render ``messages`` without parsing strings. It is also the base every
    domain refusal inherits, so the model-agnostic callers catch one class
    without importing a domain service.
    """

    @property
    def user_message(self) -> str:
        """The one message to show a human.

        ``str()`` on a Django ``ValidationError`` renders the repr of the message
        list; a view that relays a refusal must use this instead.
        """
        messages = self.messages
        return str(messages[0]) if messages else ""


class ArchiveBlocked(AggregateArchiveBlocked):
    """The root cannot be archived, or cannot be restored, yet.

    Carries the blocking obligations so a caller can link them instead of
    parsing the message; ``messages`` is what a view or the REST API renders.
    """

    def __init__(self, headline: str, *, blockers: Sequence[Any] = ()) -> None:
        self.headline = str(headline)
        self.blockers: tuple[Any, ...] = tuple(blockers)
        super().__init__(self.headline)


def lock_aggregate_root(root, *, noun: str):
    """Lock ``root`` under the ambient scope, or fail closed.

    Re-resolving instead of trusting the caller's instance is the tenant
    boundary: a handler can be reached from any caller, and a row outside the
    acting scope must not be archivable through it. The row may already be
    archived, so it comes from the including-deleted manager with the ambient
    scope re-applied explicitly. ``noun`` is the object name used in the refusal.
    """
    locked = type(root).all_objects.for_scope(Scope.current()).select_for_update().filter(pk=root.pk).first()
    if locked is None:
        raise PermissionDenied(f"The {noun} is not available in the active scope.")
    return locked


_ARCHIVE_HANDLERS: dict[str, ArchiveHandler] = {}
_RESTORE_HANDLERS: dict[str, RestoreHandler] = {}
_ARCHIVE_TABLES: dict[str, tuple[ArchiveRelation, ...]] = {}


def reverse_relation_keys(model) -> set[str]:
    """Every child relation of ``model`` an archive has to account for.

    A reverse relation is ``app_label.model.field`` of the referencing model; a
    ``GenericRelation`` declared on the model is its attribute name. Forward
    relations (including the model's own many-to-many fields) are not children.
    """
    keys = {f"{rel.related_model._meta.label_lower}.{rel.field.name}" for rel in model._meta.related_objects}
    keys.update(f.name for f in model._meta.private_fields if isinstance(f, GenericRelation))
    return keys


def archive_table(model_label: str) -> tuple[ArchiveRelation, ...]:
    """The behaviour table registered for ``model_label`` (empty when unregistered)."""
    return _ARCHIVE_TABLES.get(model_label, ())


def archive_table_problems() -> list[str]:
    """Differences between every registered table and the live model graph."""
    problems = []
    for label, table in sorted(_ARCHIVE_TABLES.items()):
        declared = [row.relation for row in table]
        actual = reverse_relation_keys(apps.get_model(label))
        duplicated = sorted({key for key in declared if declared.count(key) > 1})
        problems.extend(f"{label}: relation {key!r} is declared twice." for key in duplicated)
        problems.extend(
            f"{label}: relation {key!r} has no archive behaviour." for key in sorted(actual - set(declared))
        )
        problems.extend(f"{label}: declared relation {key!r} does not exist." for key in sorted(set(declared) - actual))
    return problems


# Soft-deletable models that carry reverse relations but are not aggregate roots.
# ``SoftDeleteMixin`` is a leaf-only flag (#619): ``delete()`` sets ``deleted_at``
# and never cascades, so a model with children is either a registered root
# (its archive service owns the children) or is declared here on purpose.
ARCHIVE_LEAVES: frozenset[str] = frozenset(
    {
        "assets.assetassignment",
        "assets.assetdisposal",
        "assets.assetmaintenance",
        "assets.assetrequest",
        "assets.assetreservation",
        "assets.assetrole",
        "assets.assettype",
        "assets.category",
        "assets.depreciation",
        "assets.manufacturer",
        "assets.statuslabel",
        "assets.supplier",
        "assets.warranty",
        "compliance.auditsession",
        "compliance.custodytemplate",
        "extras.alertrule",
        "extras.notificationchannel",
        "extras.reporttemplate",
        "extras.tag",
        "extras.webhookendpoint",
        "inventory.accessoryassignment",
        "inventory.componentallocation",
        "inventory.consumableassignment",
        "organization.contact",
        "organization.contactrole",
        "organization.costcenter",
        "organization.region",
        "organization.role",
        "organization.site",
        "organization.sitegroup",
        "organization.tenantgroup",
        "organization.tenantresourcegrant",
        "procurement.contract",
        "procurement.purchaseorderline",
        "software.installedsoftware",
        "software.software",
        "subscriptions.subscriptionassignment",
        "users.usergroup",
    }
)


def archive_coverage_problems() -> list[str]:
    """Soft-deletable models with children that are neither a root nor a declared leaf."""
    # inline import: app-registry: the model layer is only importable once apps are loaded.
    from core.mixins import SoftDeleteMixin

    problems = []
    for model in apps.get_models():
        if not issubclass(model, SoftDeleteMixin):
            continue
        label = model._meta.label_lower
        if not reverse_relation_keys(model):
            continue
        if label in _ARCHIVE_HANDLERS or label in ARCHIVE_LEAVES:
            continue
        problems.append(
            f"{model._meta.label}: has child relations but is neither a registered archive root nor a declared leaf."
        )
    for label in sorted(ARCHIVE_LEAVES & set(_ARCHIVE_HANDLERS)):
        problems.append(f"{label}: is declared a leaf and a registered archive root.")
    return problems


def register_archive_handler(
    model_label: str,
    *,
    archive: ArchiveHandler,
    relations: Sequence[ArchiveRelation],
    restore: RestoreHandler | None = None,
) -> None:
    """Register the aggregate service of ``model_label`` (``app.Model``).

    ``relations`` is the aggregate's behaviour table, one row per child relation;
    the ``core.E003`` system check verifies it against the model graph.

    Re-registering the same callable is a no-op (app ``ready()`` may run more than
    once in a process); a different callable for the same label is a wiring bug
    and fails loudly rather than silently changing which service runs.
    """
    registered = _ARCHIVE_HANDLERS.get(model_label)
    if registered is not None and registered is not archive:
        raise RuntimeError(f"An archive handler is already registered for {model_label}.")
    _ARCHIVE_HANDLERS[model_label] = archive
    _ARCHIVE_TABLES[model_label] = tuple(relations)

    if restore is None:
        return
    registered_restore = _RESTORE_HANDLERS.get(model_label)
    if registered_restore is not None and registered_restore is not restore:
        raise RuntimeError(f"A restore handler is already registered for {model_label}.")
    _RESTORE_HANDLERS[model_label] = restore


def archive_object(obj, *, actor=None, request=None):
    """Archive ``obj`` through its aggregate service, else soft-delete it."""
    handler = _ARCHIVE_HANDLERS.get(obj._meta.label_lower)
    if handler is None:
        return obj.delete()
    return handler(obj, actor=actor, request=request)


def restore_object(obj, *, actor=None, request=None):
    """Restore ``obj`` through its aggregate service, else flip the leaf flag."""
    handler = _RESTORE_HANDLERS.get(obj._meta.label_lower)
    if handler is None:
        return obj.restore()
    return handler(obj, actor=actor, request=request)
