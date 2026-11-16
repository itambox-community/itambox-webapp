from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from types import TracebackType
from typing import Protocol, Self

from django.apps import apps
from django.core.exceptions import PermissionDenied

from core import tenant_scope
from core.context import (
    SystemAuthorizationContext,
    _current_user,
    _issue_system_authorization,
    _issued_system_authorizations,
    _request_id,
    _system_authorization_scope,
    get_current_all_accessible,
    get_current_membership,
    get_current_request_id,
    get_current_tenant,
    get_current_tenant_group,
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)

logger = logging.getLogger(__name__)


@contextmanager
def task_failure_audit_context():
    """Audit a task-failure mutation without attributing it to the caller.

    A failed ``TaskContext`` restores the surrounding request context before the
    task can record its failure. Running the failure write under that restored
    context would either skip the audit entry or attribute it to the enclosing
    request. This context installs a fresh request id and clears the actor and
    tenant scope for the duration of the failure write, then restores every
    captured value exactly.
    """
    previous = (
        _request_id.get(),
        _current_user.get(),
        get_current_tenant(),
        get_current_membership(),
        get_current_tenant_group(),
        get_current_all_accessible(),
        _system_authorization_scope.get(),
        _issued_system_authorizations.get(),
    )
    _request_id.set(uuid.uuid4())
    _current_user.set(None)
    set_current_tenant(None)
    set_current_membership(None)
    set_current_tenant_group(None)
    set_current_all_accessible(False)
    _system_authorization_scope.set(None)
    _issued_system_authorizations.set(())
    try:
        yield
    finally:
        _request_id.set(previous[0])
        _current_user.set(previous[1])
        set_current_tenant(previous[2])
        set_current_membership(previous[3])
        set_current_tenant_group(previous[4])
        set_current_all_accessible(previous[5])
        _system_authorization_scope.set(previous[6])
        _issued_system_authorizations.set(previous[7])


class TaskContext:
    """
    Context manager for background/async tasks.

    Sets the tenant, membership, current user, and a synthetic request_id so
    that ChangeLoggingMixin records ObjectChange entries for all saves that
    happen inside the task — the same way middleware does for web requests.

    On exit it restores whatever context was active on entry rather than
    clearing to ``None``. This keeps nested ``TaskContext`` blocks and inline
    (``Q_CLUSTER['sync'] = True``) execution inside a web request from tearing
    down the surrounding request's user/tenant scoping — which would silently
    drop change-log entries (ChangeLoggingMixin skips logging when
    ``_request_id`` is ``None``) and disable tenant filtering for the rest of
    the request.
    """

    # The explicit scope the caller asked for, and what it resolved to. Both
    # identifiers are primary keys of ``BigAutoField`` models, so ``int``.
    class _TaskTenant(Protocol):
        pk: int

    class _TaskUser(Protocol):
        is_active: bool
        is_superuser: bool
        pk: int

    tenant_id: int | None
    user_id: int | None
    all_accessible: bool
    tenant: _TaskTenant | None
    user: _TaskUser | None
    _entered: bool

    # The context captured on entry, restored verbatim on exit. Declared here
    # rather than initialised in ``__init__`` because they are bound by
    # ``__enter__`` only -- an un-entered TaskContext deliberately has nothing
    # to restore.
    _prev_request_id: uuid.UUID | None
    _prev_user: object | None
    _prev_tenant: _TaskTenant | None
    _prev_membership: object | None
    _prev_tenant_group: object | None
    _prev_all_accessible: bool
    _prev_system_authorization_scope: object | None
    _prev_issued_system_authorizations: tuple[SystemAuthorizationContext, ...]

    # The identity that makes an authorization this scope's own: an opaque
    # object compared by identity, never by value.
    _system_authorization_issuer: object

    def __init__(
        self,
        tenant_id: int | None = None,
        user_id: int | None = None,
        operation: str = "background_task",
        *,
        all_accessible: bool = False,
    ) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.operation = operation
        self.all_accessible = all_accessible
        self.tenant = None
        self.user = None
        self._entered = False

    def __enter__(self) -> Self:
        # Capture the context active on entry so __exit__ can restore it.
        self._prev_request_id = _request_id.get()
        self._prev_user = _current_user.get()
        self._prev_tenant = get_current_tenant()
        self._prev_membership = get_current_membership()
        self._prev_tenant_group = get_current_tenant_group()
        self._prev_all_accessible = get_current_all_accessible()
        self._prev_system_authorization_scope = _system_authorization_scope.get()
        self._prev_issued_system_authorizations = _issued_system_authorizations.get()

        # Every task starts from an explicit, isolated scope. In particular,
        # TaskContext(None, None) is a system/global task and must not inherit a
        # caller's tenant, group, all-accessible flag, or membership. A scoped
        # task binds only an ACTIVE membership for exactly its user and tenant.
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)
        _current_user.set(None)
        self._system_authorization_issuer = object()
        _system_authorization_scope.set(self._system_authorization_issuer)
        _issued_system_authorizations.set(())
        # Install the task identity before resolving any explicit tenant or
        # principal. A setup failure inside a synchronous task must never log
        # or attribute work to the surrounding web request's request ID.
        _request_id.set(uuid.uuid4())

        try:
            self._resolve_principal_and_tenant()
            if self.tenant is None:
                set_current_all_accessible(self.all_accessible)
            _current_user.set(self.user)
            if self.tenant:
                set_current_tenant(self.tenant)
                if self.user:
                    membership_model = apps.get_model("organization", "Membership")
                    membership = membership_model._base_manager.filter(
                        user=self.user,
                        tenant=self.tenant,
                        is_active=True,
                    ).first()
                    if membership:
                        set_current_membership(membership)

            self._entered = True
            logger.info(
                "Task context entered",
                extra=self.log_context,
            )
        # broad except: cleanup-reraise: restore every captured context variable before propagating setup failure
        except Exception as exc:
            logger.error(
                "Task context setup failed",
                extra={**self.log_context, "exception_type": type(exc).__name__},
            )
            self._entered = False
            self._restore_context()
            raise

        return self

    @property
    def log_context(self) -> dict[str, object]:
        request_id = get_current_request_id()
        return {
            "operation": self.operation,
            "tenant_id": self.tenant_id,
            "actor_id": self.user_id,
            "request_id": str(request_id) if request_id else None,
        }

    def authorize_system(self, *, permission: str, operation: str, reason: str) -> SystemAuthorizationContext:
        """Issue an explicit authorization bound to this actorless task.

        Actor-bound tasks must use the actor's normal RBAC path. Tenantless or
        inactive contexts cannot authorize domain work, and blank audit fields
        fail closed rather than producing an unattributed system capability.
        """
        request_id = get_current_request_id()
        current_tenant = get_current_tenant()
        if not self._entered or request_id is None:
            raise PermissionDenied("System authorization requires an entered TaskContext")
        if self.user is not None:
            raise PermissionDenied("Actor-bound tasks must use normal RBAC")
        if self.tenant is None or current_tenant is None or current_tenant.pk != self.tenant.pk:
            raise PermissionDenied("System authorization requires the task's live tenant scope")
        if not all(isinstance(value, str) and value.strip() for value in (permission, operation, reason)):
            raise PermissionDenied("System authorization requires permission, operation, and reason")
        return _issue_system_authorization(
            tenant_id=self.tenant.pk,
            permission=permission,
            operation=operation,
            reason=reason,
            request_id=request_id,
            issuer=self._system_authorization_issuer,
        )

    def _resolve_principal_and_tenant(self) -> None:
        """Load and authorize the task's explicit scope via unscoped managers."""
        # Base managers are intentional bootstrap paths: an inline task may
        # target a tenant outside the wrapping request's scope. Explicit bad
        # identifiers are fatal; silently continuing would turn a scoped job
        # into a tenantless/global one.
        if self.tenant_id is not None:
            tenant_model = apps.get_model("organization", "Tenant")
            self.tenant = tenant_model._base_manager.get(
                pk=self.tenant_id,
                deleted_at__isnull=True,
            )
        if self.user_id is not None:
            user_model = apps.get_model("users", "User")
            self.user = user_model._base_manager.get(pk=self.user_id)
            if not self.user.is_active:
                raise PermissionDenied("Inactive task principal")

        # A user-bound tenant task must prove canonical access to the target.
        # System tasks (no user) and superusers retain their explicit paths.
        if self.tenant is not None and self.user is not None and not self.user.is_superuser:
            if self.tenant.pk not in tenant_scope.accessible_tenant_ids(
                self.user
            ) or not tenant_scope.role_grant_covers_tenant(self.user, self.tenant):
                raise PermissionDenied("Task principal cannot access target tenant")

    def _restore_context(self) -> None:
        _issued_system_authorizations.set(self._prev_issued_system_authorizations)
        _system_authorization_scope.set(self._prev_system_authorization_scope)
        _request_id.set(self._prev_request_id)
        _current_user.set(self._prev_user)
        set_current_tenant(self._prev_tenant)
        set_current_tenant_group(self._prev_tenant_group)
        set_current_membership(self._prev_membership)
        set_current_all_accessible(self._prev_all_accessible)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._entered = False
        self._restore_context()
