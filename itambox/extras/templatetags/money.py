"""
{% load money %}

{{ value|money:context_object }}

Formats a Decimal/float value with the currency symbol of *context_object*'s tenant.
Resolution: obj.currency (per-record, if set) → obj.tenant.currency →
obj.asset.tenant.currency → ITAMBOX_DEFAULT_CURRENCY.

Symbol placement and number formatting follow the currency:
  EUR / CHF  →  1.234,56 €    (symbol after, locale-aware separators)
  USD / GBP  →  $1,234.56     (symbol before, locale-aware separators)
"""

from django import template
from django.conf import settings

from core.reports.formatting import format_money, resolve_currency_code

register = template.Library()


@register.filter
def money(value, obj=None):
    """Format *value* as a currency string using *obj*'s tenant currency."""
    if value is None:
        return ""
    try:
        record_currency = getattr(obj, "currency", None) if obj is not None else None
        tenant_currency = None
        if not (isinstance(record_currency, str) and record_currency.strip()) and obj is not None:
            tenant = getattr(obj, "tenant", None)
            if tenant is None:
                asset = getattr(obj, "asset", None)
                if asset is not None:
                    tenant = getattr(asset, "tenant", None)
            if tenant is not None:
                tenant_currency = getattr(tenant, "currency", None)
        currency = resolve_currency_code(
            record_currency,
            tenant_currency,
            getattr(settings, "ITAMBOX_DEFAULT_CURRENCY", "EUR"),
        )
    # broad except: render-degrade: any failing currency accessor prices in the default currency
    # and its message stays private (pinned by test_currency_formatting)
    except Exception:
        currency = "EUR"
    return format_money(value, currency_code=currency)
