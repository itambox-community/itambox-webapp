"""Request-scoped runtime context: the contextvars every layer reads.

This module is the designated *leaf* of the request-context dependency graph.
It owns the ``ContextVar`` objects (and their accessors) that carry the active
tenant / tenant group / membership / scope flag, the acting user, and the
request id. Nothing here imports another first-party module, so any layer —
managers, middleware, auth backends, MFA policy, background tasks — can import
it at module scope without closing an import loop.

Why it exists (issue #87, phase D): the tenant scoping managers, the middleware
that populates the context, and the auth backends that read it all needed the
*same* contextvars, and each half used to import the other half's module. That
is a genuine cycle, and it was only being hidden by function-body imports. A
deferred import hides a cycle; it does not remove one. Owning the shared
constructs here removes it.

``core.managers`` and ``itambox.middleware`` re-export these names, so the
long-standing import sites (``from core.managers import set_current_tenant``,
``from itambox.middleware import get_current_user``) keep working unchanged.

Only pure-Python state belongs here. Anything that touches models, settings,
the cache, or the app registry belongs in the module that owns that concern —
adding such a dependency would re-create the cycle this module exists to break.
"""

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

_current_tenant = contextvars.ContextVar("current_tenant", default=None)
_current_tenant_group = contextvars.ContextVar("current_tenant_group", default=None)
_current_membership = contextvars.ContextVar("current_membership", default=None)
# "All accessible tenants" scope for a non-superuser: no single tenant/group is
# active, yet the request is NOT global — it is scoped to exactly the tenants the
# canonical resolver authorizes. Distinct from the superuser global
# scope (all three None + is_superuser) so it can never widen into it.
_current_all_accessible = contextvars.ContextVar("current_all_accessible", default=False)
_descendant_group_ids_cache = contextvars.ContextVar("descendant_group_ids_cache", default=None)
_current_user = contextvars.ContextVar("current_user", default=None)
_request_id = contextvars.ContextVar("request_id", default=None)
_csp_nonce = contextvars.ContextVar("csp_nonce", default=None)
_system_authorization_scope = contextvars.ContextVar("system_authorization_scope", default=None)
_issued_system_authorizations = contextvars.ContextVar("issued_system_authorizations", default=())


@dataclass(frozen=True, init=False)
class SystemAuthorizationContext:
    """Explicit authorization for actorless work inside one task scope.

    Instances are issued by ``TaskContext.authorize_system`` and bind one
    tenant, permission, operation, reason, and synthetic request ID. Consumers
    must compare every bound value with the active context; possession alone is
    never a cross-context capability.
    """

    tenant_id: int
    permission: str
    operation: str
    reason: str
    request_id: Optional[uuid.UUID]
    _issuer: object

    def is_valid_for(
        self,
        *,
        tenant_id: int,
        permission: str,
        operation: str,
        request_id: Optional[uuid.UUID],
    ) -> bool:
        active_scope = _system_authorization_scope.get()
        return (
            active_scope is not None
            and self._issuer is active_scope
            and any(self is issued for issued in _issued_system_authorizations.get())
            and self.tenant_id == tenant_id
            and self.permission == permission
            and self.operation == operation
            and bool(self.reason.strip())
            and self.request_id is not None
            and self.request_id == request_id
        )


def has_valid_system_authorization(*, tenant_id: int, permission: str, operation: str) -> bool:
    """Whether the active task scope has issued this exact authorization tuple."""
    current_request_id = _request_id.get()
    if _system_authorization_scope.get() is None or current_request_id is None:
        return False
    return any(
        isinstance(authorization, SystemAuthorizationContext)
        and authorization.is_valid_for(
            tenant_id=tenant_id,
            permission=permission,
            operation=operation,
            request_id=current_request_id,
        )
        for authorization in _issued_system_authorizations.get()
    )


def _issue_system_authorization(*, tenant_id, permission, operation, reason, request_id, issuer):
    if (
        issuer is None
        or issuer is not _system_authorization_scope.get()
        or request_id is None
        or request_id != _request_id.get()
        or tenant_id != getattr(_current_tenant.get(), "pk", None)
    ):
        raise PermissionError("System authorization must be issued by the active TaskContext")
    authorization = object.__new__(SystemAuthorizationContext)
    values = {
        "tenant_id": tenant_id,
        "permission": permission,
        "operation": operation,
        "reason": reason,
        "request_id": request_id,
        "_issuer": issuer,
    }
    for name, value in values.items():
        object.__setattr__(authorization, name, value)
    _issued_system_authorizations.set((*_issued_system_authorizations.get(), authorization))
    return authorization


def set_current_tenant(tenant: Optional[Any]) -> None:
    _current_tenant.set(tenant)
    _descendant_group_ids_cache.set(None)


def get_current_tenant() -> Optional[Any]:
    return _current_tenant.get()


def set_current_csp_nonce(nonce: Optional[str]):
    return _csp_nonce.set(nonce)


def get_current_csp_nonce() -> Optional[str]:
    return _csp_nonce.get()


def reset_current_csp_nonce(token) -> None:
    _csp_nonce.reset(token)


def set_current_tenant_group(group: Optional[Any]) -> None:
    _current_tenant_group.set(group)
    _descendant_group_ids_cache.set(None)


def get_current_tenant_group() -> Optional[Any]:
    return _current_tenant_group.get()


def set_current_membership(membership: Optional[Any]) -> None:
    _current_membership.set(membership)
    _descendant_group_ids_cache.set(None)


def get_current_membership() -> Optional[Any]:
    return _current_membership.get()


def set_current_all_accessible(flag: bool) -> None:
    _current_all_accessible.set(bool(flag))
    _descendant_group_ids_cache.set(None)


def get_current_all_accessible() -> bool:
    return _current_all_accessible.get()


@contextmanager
def override_current_tenant_scope(tenant: Optional[Any], membership: Optional[Any] = None):
    """Temporarily bind one tenant scope and restore the exact outer state."""
    tenant_token = _current_tenant.set(tenant)
    group_token = _current_tenant_group.set(None)
    membership_token = _current_membership.set(membership)
    all_accessible_token = _current_all_accessible.set(False)
    cache_token = _descendant_group_ids_cache.set(None)
    try:
        yield
    finally:
        _descendant_group_ids_cache.reset(cache_token)
        _current_all_accessible.reset(all_accessible_token)
        _current_membership.reset(membership_token)
        _current_tenant_group.reset(group_token)
        _current_tenant.reset(tenant_token)


def get_current_request_id():
    return _request_id.get()


def get_current_user():
    return _current_user.get()


def set_current_user(user):
    """Bind the current-user contextvar after the fact.

    DRF authentication runs inside a view's ``initial()`` — *after*
    ``CurrentUserMiddleware`` has already captured ``request.user`` (which is
    ``AnonymousUser`` for a token-authenticated request at that point). Token-auth
    views (e.g. SCIM) call this once authenticated so changelog rows are attributed
    to the acting principal instead of being recorded as ``user=None`` ('System').
    The middleware's response phase resets the contextvar via its entry token, so
    this set is correctly torn down at request end (no cross-request leak).
    """
    _current_user.set(user)


def get_current_scope_conflict(user: Optional[Any]) -> bool:
    """True when more than one of tenant / group / all-accessible scope is
    active for an authenticated non-superuser.

    The session/middleware resolution, token authentication, and TaskContext
    each set at most one of these by construction, so a contradiction here
    means the contextvars were poked directly (a bug, or a background task
    inheriting stale ambient state from a wrapping request). Tenant-scoping
    consumers must fail closed to nothing in that case rather than silently
    prioritize one of the contradictory states — a superuser has no such
    ambiguity (they keep their own global/explicit-scope path regardless).
    """
    if user is None or not getattr(user, "is_authenticated", False) or getattr(user, "is_superuser", False):
        return False
    active_states = (
        get_current_tenant(),
        get_current_tenant_group(),
        get_current_all_accessible(),
    )
    return sum(bool(state) for state in active_states) > 1
