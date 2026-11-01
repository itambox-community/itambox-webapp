"""Kernel-owned model serialization helpers."""

import datetime
from decimal import Decimal
from uuid import UUID

from django.db.models import Model
from django.db.models.fields.files import FieldFile


def serialize_object(obj: Model, extra_fields=None, exclude_fields=None) -> dict:
    if not obj:
        return None
    extra_fields = set(extra_fields or ())
    exclude_fields = set(exclude_fields or ())
    data = {}
    m2m_fields = {field.name for field in obj._meta.many_to_many}

    for field in obj._meta.get_fields():
        field_name = field.name
        if field_name in exclude_fields:
            continue
        if not field.concrete or field_name == obj._meta.pk.name:
            if field_name not in extra_fields:
                continue
        try:
            field_value = getattr(obj, field_name)
        except AttributeError:
            continue
        if field_name in m2m_fields:
            if hasattr(field_value, "all"):
                try:
                    data[field_name] = sorted(field_value.values_list("pk", flat=True))
                # broad except: boundary-isolation: an unreadable M2M relation is recorded as empty
                # rather than aborting the audited save (pinned by test_kernel_serialization)
                except Exception:
                    data[field_name] = []
            else:
                data[field_name] = []
        elif field.is_relation:
            data[field_name] = field_value.pk if field_value is not None else None
        elif isinstance(field_value, (datetime.date, datetime.datetime, datetime.time)):
            data[field_name] = field_value.isoformat()
        elif isinstance(field_value, (Decimal, UUID)):
            data[field_name] = str(field_value)
        else:
            if isinstance(field_value, FieldFile):
                data[field_name] = field_value.name if field_value else None
            else:
                data[field_name] = field_value
    return data
