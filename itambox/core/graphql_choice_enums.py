"""Choice enums for the read-only GraphQL schema.

Strawberry derives enums from Django ``choices`` itself, but its generated
names (``<App><Object><Field>Enum``) and member names differ from the contract
this schema published before the Strawberry port (``<App><Object><Field>Choices`` with
the member names of the pre-Strawberry schema).  The helper below rebuilds that contract
from the model field, so enum names, member names and member descriptions stay
identical without duplicating the choice lists by hand.
"""

from __future__ import annotations

import enum
import re

import strawberry
from django.db.models import Model

__all__ = ["choice_enum"]


def _to_const(value: str) -> str:
    """Member-name rule of the pre-Strawberry schema: every non-word run becomes ``_``."""
    return re.sub(r"[\W|^]+", "_", value).upper()


def _to_camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def choice_enum(model: type[Model], field_name: str, *, name: str | None = None) -> type[enum.Enum]:
    """Build the ``<App><Object><Field>Choices`` enum for ``model.field_name``."""
    meta = model._meta
    field = meta.get_field(field_name)
    enum_name = name or "{app}{object_name}{field_name}Choices".format(
        app=_to_camel_case(meta.app_label.title()),
        object_name=meta.object_name,
        field_name=_to_camel_case(field.name.title()),
    )
    members: dict[str, object] = {}
    for value, label in field.flatchoices:
        if value in (None, ""):
            continue
        # The member NAME is the GraphQL enum value (the pre-Strawberry schema used
        # the raw value as the Python name and converted it with ``_to_const``).
        member_name = _to_const(str(value))
        while member_name in members:
            member_name += "_"
        members[member_name] = strawberry.enum_value(value, description=str(label))
    return strawberry.enum(enum.Enum(enum_name, members), name=enum_name, description="An enumeration.")
