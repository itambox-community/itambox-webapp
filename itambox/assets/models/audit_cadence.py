"""Shared calendar-month semantics for category audit cadences."""

from datetime import date, datetime

from dateutil.relativedelta import relativedelta
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, Func
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone


def add_calendar_months(value: datetime, months: int) -> datetime:
    """Add calendar months, clamping month-end dates to the destination month's last day."""
    return value + relativedelta(months=months)


def audit_due_on_expression() -> TruncDate:
    """Return the database date expression matching ``add_calendar_months`` semantics."""
    interval = Func(
        F("asset_type__category__audit_interval_months"),
        function="make_interval",
        template="%(function)s(months => %(expressions)s)",
        output_field=DurationField(),
    )
    due_at = ExpressionWrapper(
        Coalesce(F("last_audited"), F("created_at")) + interval,
        output_field=DateTimeField(),
    )
    return TruncDate(due_at, tzinfo=timezone.get_current_timezone())


def is_audit_overdue(due_at: datetime, today: date) -> bool:
    """An audit deadline remains current on its local calendar date."""
    return timezone.localtime(due_at).date() < today
