"""Ordering helpers for choice lists that forms assemble by hand.

Django places the blank entry of a ``ModelChoiceField`` in front of the
queryset choices itself through ``empty_label``. Lists a form builds itself --
filter forms deriving choices from a filter declaration, dynamic specification
fields, select fields declared with an explicit list -- have no such guarantee,
so a blank can end up anywhere in the list. ``empty_choice_first`` normalises
those lists and ``apply_empty_choice_first`` applies it to a field.
"""

from django import forms


def empty_choice_first(choices):
    """Return ``choices`` with the blank (``""``) choice first.

    A blank entry found anywhere is moved to the front, duplicate blank entries
    collapse into one, and a list without a blank entry is returned unchanged --
    whether a field is clearable stays the caller's decision, so this never
    invents one. The result is always a list of ``(value, label)`` pairs, so the
    function is idempotent.
    """
    ordered = []
    blank = None
    for value, label in choices:
        if value == "":
            if blank is None:
                blank = (value, label)
            continue
        ordered.append((value, label))

    if blank is None:
        return ordered
    return [blank, *ordered]


def apply_empty_choice_first(field):
    """Ensure a field with hand-assembled choices starts with the blank entry.

    Model-backed fields are returned untouched: ``ModelChoiceField`` (and its
    multiple-value subclass) already renders ``empty_label`` first, and
    replacing its lazy ``choices`` would freeze the queryset into a list.
    """
    if not isinstance(field, forms.ChoiceField) or isinstance(field, forms.ModelChoiceField):
        return field

    field.choices = empty_choice_first(field.choices)
    return field
