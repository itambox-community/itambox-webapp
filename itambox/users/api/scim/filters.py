import re
from typing import Protocol, Self
from uuid import UUID

from django.db.models import Q

from users.api.scim.identifiers import MAX_LEGACY_PK


class SCIMFilterError(ValueError):
    """Raised when a SCIM filter expression cannot be parsed."""

    pass


class _SCIMQuery(Protocol):
    """What a parsed SCIM filter answers with: a combinable query expression.

    The runtime value is a Django ``Q``, and the parser bodies below build it
    exactly as they always have. The admitted signatures name this protocol
    instead so the checked contract is the query algebra a caller actually uses
    -- conjunction, disjunction, negation -- rather than the ORM class that
    implements it. That is the honest boundary here: this module maps SCIM
    filter syntax onto a query expression and never executes one.

    Django's ``Q`` satisfies this structurally; the ``Self`` bound is what makes
    the combining operators describe "another expression of my own kind" rather
    than a widening to this protocol.
    """

    def __and__(self, other: Self, /) -> Self:
        pass

    def __or__(self, other: Self, /) -> Self:
        pass

    def __invert__(self) -> Self:
        pass


# Upper bound on a SCIM filter expression before it reaches the parser regex.
# The grammar has adjacent whitespace groups (\s+ ... \s*) that backtrack
# polynomially on long crafted inputs (ReDoS); real filters are short.
MAX_SCIM_FILTER_LENGTH = 512


def _reject_oversized_filter(filter_str: str) -> None:
    """Raise SCIMFilterError if the expression exceeds the ReDoS length bound."""
    if len(filter_str) > MAX_SCIM_FILTER_LENGTH:
        raise SCIMFilterError("SCIM filter expression is too long.")


def _normalize_filter_value(val: str | None, attr_lower: str) -> str | bool | None:
    if val is None or attr_lower == "externalid":
        return val
    val_lower = val.lower()
    if val_lower == "true":
        return True
    if val_lower == "false":
        return False
    if val_lower == "null":
        return None
    return val


def _parse_id_filter(op: str, val: str | bool | None) -> _SCIMQuery:
    if op == "pr":
        return Q(scim_id__isnull=False)
    if op not in {"eq", "ne"} or not isinstance(val, str):
        raise SCIMFilterError("SCIM id filters support only eq, ne, and pr.")
    if isinstance(val, str) and val.isascii() and val.isdecimal():
        if len(val) > 19 or int(val) > MAX_LEGACY_PK:
            raise SCIMFilterError("SCIM id filter contains an out-of-range legacy integer.")
        legacy_query = Q(pk=int(val))
        return legacy_query if op == "eq" else ~legacy_query
    try:
        opaque_id = UUID(val)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SCIMFilterError("SCIM id filter must contain a legacy integer or UUID.") from exc
    opaque_query = Q(scim_id=opaque_id)
    return opaque_query if op == "eq" else ~opaque_query


def _build_filter_query(field_name: str, op: str, val: str | bool | None) -> _SCIMQuery:
    if op == "eq":
        if val is True or val is False or val is None:
            return Q(**{field_name: val})
        if isinstance(val, str):
            lookup = "exact" if field_name.endswith("external_id") else "iexact"
            return Q(**{f"{field_name}__{lookup}": val})
        return Q(**{field_name: val})
    if op == "ne":
        if isinstance(val, str):
            lookup = "exact" if field_name.endswith("external_id") else "iexact"
            return ~Q(**{f"{field_name}__{lookup}": val})
        return ~Q(**{field_name: val})
    lookups = {
        "co": "icontains",
        "sw": "istartswith",
        "ew": "iendswith",
        "gt": "gt",
        "ge": "gte",
        "lt": "lt",
        "le": "lte",
    }
    if field_name.endswith("external_id"):
        lookups.update({"co": "contains", "sw": "startswith", "ew": "endswith"})
    if op == "pr":
        return Q(**{f"{field_name}__isnull": False}) & ~Q(**{field_name: ""})
    lookup = lookups.get(op)
    return Q(**{f"{field_name}__{lookup}": val}) if lookup else Q()


_SCOPED_FILTER_RE = re.compile(
    r"^\s*(externalId|active)\s+(eq|co|sw|ew|gt|ge|lt|le|ne|pr)\s*+"
    r'(?:"([^"]*)"|\'([^\']*)\'|([^\s"\']+))?\s*+$',
    re.IGNORECASE,
)


def parse_scim_membership_filter(filter_str: str | None) -> _SCIMQuery | None:
    """Return a Membership-local Q for tenant-scoped User filters, if applicable."""
    if not filter_str:
        return None
    match = _SCOPED_FILTER_RE.match(filter_str.strip())
    if not match:
        return None
    attr, op, val_double, val_single, val_unquoted = match.groups()
    val = next((candidate for candidate in (val_double, val_single, val_unquoted) if candidate is not None), None)
    op = op.lower()
    attr_lower = attr.lower()
    if op != "pr" and val is None:
        return None
    val = _normalize_filter_value(val, attr_lower)
    field_name = "external_id" if attr_lower == "externalid" else "is_active"
    return _build_filter_query(field_name, op, val)


def parse_scim_filter(filter_str: str | None, model_type: str = "user") -> _SCIMQuery:
    if not filter_str:
        return Q()

    # Normalize whitespace
    filter_str = filter_str.strip()
    if not filter_str:
        return Q()

    # Bound the input before any regex work (see MAX_SCIM_FILTER_LENGTH).
    _reject_oversized_filter(filter_str)

    # Normalize common bracketed filter paths (e.g. emails[type eq "work"].value -> email)
    filter_str = re.sub(
        r'emails\[type\s+eq\s+["\']?[a-zA-Z0-9_-]+["\']?\]\.value', "email", filter_str, flags=re.IGNORECASE
    )

    # Parse simple expressions like: attribute operator "value" or attribute operator value
    # E.g. userName eq "test@example.com"
    # E.g. active eq true
    # E.g. displayName eq "Admins"
    # E.g. userName sw "test"
    # E.g. id eq 123

    # We match: (attribute) (operator) (value)
    # The value can be double-quoted, single-quoted, or unquoted (like true/false/numbers).
    # Whitespace quantifiers are possessive (\s*+ / \s++, Python 3.11+): the tokens
    # around each whitespace run are non-whitespace, so nothing is ever given back —
    # this removes the polynomial backtracking (ReDoS) on crafted space runs while
    # matching exactly the same expressions.
    pattern = re.compile(
        r'^\s*+([a-zA-Z0-9_\.]+)\s++(eq|ne|co|sw|ew|gt|ge|lt|le|pr)\s*+(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'\)]+))?\s*+$',
        re.IGNORECASE,
    )

    match = pattern.match(filter_str)
    if not match:
        raise SCIMFilterError(f"Invalid SCIM filter expression: {filter_str}")

    attr, op, val_double, val_single, val_unquoted = match.groups()
    val = next((candidate for candidate in (val_double, val_single, val_unquoted) if candidate is not None), None)
    op = op.lower()

    if op != "pr" and val is None:
        raise SCIMFilterError("Operator '%s' requires a value." % op)

    # Never expose Django's double-underscore relation grammar through SCIM filters.
    if "__" in attr:
        raise SCIMFilterError("SCIM filter attribute is invalid.")

    # Normalize attribute name
    attr_lower = attr.lower()

    # Map SCIM attributes to Django model fields
    field_name: str | None = None
    if model_type == "user":
        if attr_lower == "username":
            field_name = "username"
        elif attr_lower in ("email", "emails", "emails.value"):
            field_name = "email"
        elif attr_lower == "externalid":
            field_name = "memberships__external_id"
        elif attr_lower == "active":
            field_name = "memberships__is_active"
        elif attr_lower == "id":
            field_name = "scim_id"
        elif attr_lower == "displayname":
            field_name = "username"
    elif model_type == "group":
        if attr_lower in ("displayname", "name"):
            field_name = "name"
        elif attr_lower == "externalid":
            field_name = "external_id"
        elif attr_lower == "id":
            field_name = "scim_id"

    if not field_name:
        raise SCIMFilterError(f"SCIM attribute is not filterable: {attr}")

    val = _normalize_filter_value(val, attr_lower)

    if attr_lower == "id":
        return _parse_id_filter(op, val)
    return _build_filter_query(field_name, op, val)
