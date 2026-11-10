"""Permission helpers for GraphQL specification resolvers.

A dependency-free leaf so that both the type definitions and the integration
layer can gate on a permission without importing each other.
"""

from __future__ import annotations

from collections.abc import Mapping

from graphql import GraphQLError


def authenticated_user(info: object) -> object:
    context = getattr(info, "context", None)
    if context is None:
        raise GraphQLError(
            "Authentication credentials were not provided.",
            extensions={"code": "UNAUTHENTICATED"},
        )
    if isinstance(context, Mapping):
        user = context.get("user")
    else:
        user = getattr(context, "user", None)
        if user is None:
            user = getattr(getattr(context, "request", None), "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        raise GraphQLError(
            "Authentication credentials were not provided.",
            extensions={"code": "UNAUTHENTICATED"},
        )
    return user


def has_global_permission(info: object, permission: str) -> bool:
    """Return whether the caller holds ``permission``, memoized per request context.

    Field resolvers call this once per parent row; without the memo a list of N
    rows repeats the membership lookup N times (the page-size query budget).
    """
    user = authenticated_user(info)
    state = getattr(getattr(info, "context", None), "__dict__", None)
    if state is None:
        return bool(user.has_perm(permission))
    memo = state.setdefault("_global_permission_memo", {})
    key = (id(user), permission)
    if key not in memo:
        memo[key] = bool(user.has_perm(permission))
    return memo[key]
