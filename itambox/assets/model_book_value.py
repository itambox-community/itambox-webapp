"""
Pure depreciation math — no ORM, no side effects.

The module is deliberately a flat model-support leaf.  It can be imported by
``assets.models.asset`` without executing the importful ``assets.models``
package initializer.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")


def _to_decimal(value: Decimal | int | float | str | None) -> Decimal | None:
    """Coerce a money-ish value to Decimal without binary float artefacts."""
    if value is None or isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _round(value):
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def resolve_policy(asset: object) -> tuple[object | None, str | None]:
    """Resolve the asset override, tenant default, or asset-type schedule."""
    override = getattr(asset, "depreciation_override", None)
    if override is not None:
        return override, "override"

    tenant = getattr(asset, "tenant", None)
    if tenant is not None:
        tenant_default = getattr(tenant, "default_depreciation", None)
        if tenant_default is not None:
            return tenant_default, "tenant"

    asset_type = getattr(asset, "asset_type", None)
    if asset_type is not None:
        type_policy = getattr(asset_type, "depreciation", None)
        if type_policy is not None:
            return type_policy, "type"
    return None, None


def _clock_start(asset: object) -> datetime.date | None:
    return getattr(asset, "in_service_date", None) or asset.purchase_date


def _months_held(clock_start, on_date, convention):
    month_diff = (on_date.year - clock_start.year) * 12 + on_date.month - clock_start.month
    if convention == "include_purchase_month":
        return max(month_diff + 1, 0)
    return max(month_diff, 0)


def _threshold_value(purchase_cost, salvage, threshold, months_held):
    if threshold is None or purchase_cost > _to_decimal(threshold):
        return None
    return salvage if months_held >= 1 else purchase_cost


def _policy_value(asset, purchase_cost, salvage, policy, months_held):
    method = getattr(policy, "method", "straight_line")
    months = getattr(policy, "months", 0)
    if method == "none" or not months or months <= 0 or months_held == 0:
        return purchase_cost
    if months_held >= months:
        return salvage

    depreciable_base = purchase_cost - salvage
    monthly_depreciation = depreciable_base / Decimal(str(months))
    current = purchase_cost - (monthly_depreciation * Decimal(str(months_held)))
    return max(current, salvage)


def compute_book_value(asset: object, on_date: datetime.date | None = None) -> Decimal | None:
    """Return the straight-line depreciated book value of ``asset``.

    Disposal values are frozen, absent purchase costs return ``None``, and all
    other values are rounded to two decimal places using ``ROUND_HALF_UP``.
    """
    if getattr(asset, "disposed_at", None) is not None:
        return _to_decimal(asset.disposal_value)

    if not asset.purchase_cost:
        return None

    purchase_cost = _to_decimal(asset.purchase_cost)
    salvage = _to_decimal(asset.salvage_value) or Decimal("0.00")
    policy, _ = resolve_policy(asset)
    if policy is None:
        return _round(purchase_cost)

    clock_start = _clock_start(asset)
    if clock_start is None:
        return _round(purchase_cost)
    on_date = on_date or datetime.date.today()
    if on_date < clock_start:
        return _round(purchase_cost)
    months_held = _months_held(clock_start, on_date, getattr(policy, "convention", "exclude_purchase_month"))

    # Method ``none`` and non-positive schedules are intentionally non-depreciating.
    # This guard must precede the immediate-expense threshold: the threshold only
    # changes a depreciation schedule, not a policy that explicitly disables it.
    method = getattr(policy, "method", "straight_line")
    months = getattr(policy, "months", 0)
    if method == "none" or not months or months <= 0:
        return _round(purchase_cost)

    threshold_value = _threshold_value(
        purchase_cost,
        salvage,
        getattr(policy, "immediate_expense_threshold", None),
        months_held,
    )
    value = (
        threshold_value
        if threshold_value is not None
        else _policy_value(asset, purchase_cost, salvage, policy, months_held)
    )
    return _round(value)
