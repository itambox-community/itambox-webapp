# This file is adapted from NetBox (https://github.com/netbox-community/netbox).
# Copyright (c) DigitalOcean, LLC.
# Licensed under the Apache License, Version 2.0.

# itambox/templatetags/utility_tags.py
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django import template
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.staticfiles import finders
from django.utils.translation import gettext_lazy as _  # Import gettext_lazy

from core.html_styles import color_chip_class, length_class, percentage_class, status_tint_class
from itambox.utils import get_status_color

register = template.Library()


def asset_stamp(path, finder=None):
    """Cache-busting stamp for one built asset.

    ``settings.VERSION`` only changes per release, so redeploying the same
    release kept browsers on the previous stylesheet/bundle: the markup updated
    while ``dist/itambox.css`` stayed the old build in cache (issue #935).
    ``static/dist`` is rewritten by every build, so its mtime is the stamp.
    Falls back to the release version when the asset is absent.
    """
    resolve = finder or finders.find
    found = resolve(path)
    if not found:
        return str(settings.VERSION)
    try:
        return str(int(Path(found).stat().st_mtime))
    except OSError:
        return str(settings.VERSION)


@register.simple_tag
def asset_version(path="dist/itambox.css"):
    """Template tag wrapper around :func:`asset_stamp`."""
    return asset_stamp(path)


@register.filter
def lookup(dictionary, key):
    """Enable dictionary lookup by variable key in templates."""
    return dictionary.get(key)


@register.filter
def absolute(value):
    """Return the absolute numeric value for count-aware display copy."""
    try:
        return abs(value)
    except (TypeError, ValueError):
        return value


@register.filter
def localize_journal_comment(comment):
    """Translate the stable subscription renewal journal format at render time."""
    if not isinstance(comment, str):
        return comment
    prefix = "Renewed subscription. Next renewal date: "
    separator = ". Cost: "
    if not comment.startswith(prefix) or separator not in comment or not comment.endswith("."):
        return comment
    renewal_date, cost_and_currency = comment[len(prefix) : -1].split(separator, 1)
    if " " not in cost_and_currency:
        return comment
    renewal_cost, currency = cost_and_currency.rsplit(" ", 1)
    try:
        Decimal(renewal_cost)
    except InvalidOperation:
        renewal_cost = _("Not set")
    return _("Renewed subscription. Next renewal date: %(renewal_date)s. Cost: %(renewal_cost)s %(currency)s.") % {
        "renewal_date": renewal_date,
        "renewal_cost": renewal_cost,
        "currency": currency,
    }


@register.simple_tag(takes_context=True)
def get_active_filters(context):
    """
    Checks if there are any active filters in the request GET parameters,
    excluding pagination and search ('page', 'per_page', 'q').
    """
    request = context.get("request")
    if not request:
        return False

    ignored_params = ["page", "per_page", "q"]
    for key in request.GET:
        if key not in ignored_params:
            return True  # Found an active filter
    return False


@register.filter
def status_from_tag(tag):
    """
    Map Django message tags (strings) to Bootstrap alert classes.
    """
    if tag == "debug":
        return "secondary"
    elif tag == "info":
        return "info"
    elif tag == "success":
        return "success"
    elif tag == "warning":
        return "warning"
    elif tag == "error":  # Django's 'error' level tag maps to Bootstrap 'danger'
        return "danger"
    return "info"  # Default


@register.filter
def icon_from_status(status):
    """
    Map Bootstrap alert class/status to an MDI icon name.
    """
    if status == "secondary":  # debug
        return "mdi-bug"
    elif status == "info":
        return "mdi-information-outline"
    elif status == "success":
        return "mdi-check-circle-outline"
    elif status == "warning":
        return "mdi-alert-outline"
    elif status == "danger":  # error
        return "mdi-alert-circle-outline"
    return "mdi-information-outline"  # Default


@register.simple_tag()
def update_querystring(request, **kwargs):
    """
    Renders the current page's query string with updated parameter values.
    Matches NetBox's implementation signature.
    Example: {% update_querystring request page=paginator.next_page_number %}
    """
    if not request:
        return ""

    query_params = request.GET.copy()  # Get a mutable copy

    # Update parameters from kwargs
    for key, value in kwargs.items():
        if value is not None:
            query_params[key] = str(value)
        elif key in query_params:
            del query_params[key]

    # Explicitly remove any empty string values or keys
    keys_to_delete = [k for k, v in query_params.items() if v == ""]
    for key in keys_to_delete:
        del query_params[key]

    # Don't include page=1 in the query string (it's the default)
    if "page" in query_params and query_params["page"] == "1":
        del query_params["page"]

    # If the dictionary is empty after modifications, return empty string
    if not query_params:
        return ""

    # Encode the parameters
    encoded_params = query_params.urlencode(safe="/")

    # Return with leading '?' only if there are encoded params
    if encoded_params and encoded_params.strip():
        return "?" + encoded_params
    else:
        return ""  # Should ideally not be reached due to the dict check


class _QuerystringReplaceNode(template.Node):
    def __init__(self, kwargs):
        self.kwargs = kwargs

    def render(self, context):
        request = context.get("request")
        if request is None:
            return ""
        params = request.GET.copy()
        for key_expr, value_expr in self.kwargs:
            key = key_expr.resolve(context)
            value = value_expr.resolve(context)
            if key in (None, ""):
                continue
            key = str(key)
            if value in (None, ""):
                params.pop(key, None)
            else:
                params[key] = str(value)
        # Parity with update_querystring: drop empties and the default page=1.
        for k in [k for k, v in params.items() if v == ""]:
            del params[k]
        if params.get("page") == "1":
            del params["page"]
        encoded = params.urlencode(safe="/")
        return "?" + encoded if encoded.strip() else ""


@register.tag(name="querystring_replace")
def querystring_replace(parser, token):
    """
    Like update_querystring, but resolves parameter KEYS from the context too
    (not only values) — needed for django-tables2 sort links where the sort
    parameter name is dynamic, e.g.:
        {% querystring_replace table.prefixed_order_by_field=column.order_by_alias.next %}
    Reads `request` from the template context (request context processor).
    """
    kwargs = []
    for bit in token.split_contents()[1:]:
        if "=" not in bit:
            raise template.TemplateSyntaxError("'querystring_replace' arguments must be key=value pairs")
        key, value = bit.split("=", 1)
        kwargs.append((parser.compile_filter(key), parser.compile_filter(value)))
    return _QuerystringReplaceNode(kwargs)


@register.filter
def humanize_key(value):
    """Replace underscores with spaces and capitalize words, capitalizing specific acronyms."""
    if not value:
        return ""
    cleaned = str(value).replace("_", " ").title()
    words = []
    for word in cleaned.split():
        if word.lower() in ("sim", "cpu", "gpu", "ram", "vin", "upn", "sku", "eol", "tco"):
            words.append(word.upper())
        else:
            words.append(word)
    return " ".join(words)


@register.filter
def content_type_id(obj):
    """Return ContentType ID for the given model instance."""
    if not obj:
        return None

    return ContentType.objects.get_for_model(obj).id


@register.simple_tag
def has_object_perm(user, perm, obj):
    """
    Checks if a user has a specific permission on a specific object.
    """
    if not user or not user.is_authenticated:
        return False
    return user.has_perm(perm, obj)


@register.filter
def pretty_json(value):
    """Render value as a pretty-printed JSON string."""
    if value is None:
        return ""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        return json.dumps(value, indent=2)
    except (TypeError, ValueError):
        return str(value)


@register.filter
def status_color(status):
    """Template filter to get color code for a status."""

    return get_status_color(status)


@register.simple_tag(takes_context=True)
def csp_color_style(context, color, variant="chip"):
    """Return a class/style pair for a CSP-safe dynamic color."""
    if variant == "status":
        class_name, style_block = status_tint_class(color)
    else:
        class_name, style_block = color_chip_class(color)
    return {"class_name": class_name, "style_block": style_block}


@register.simple_tag(takes_context=True)
def csp_percentage_style(context, value, prefix="percentage-width"):
    """Return a bounded class/style pair for a dynamic percentage width."""
    class_name, style_block = percentage_class(value, prefix=prefix)
    return {"class_name": class_name, "style_block": style_block}


@register.simple_tag(takes_context=True)
def csp_length_style(context, value, prefix="safe-length", property_name="height"):
    """Return a bounded class/style pair for a fixed CSS length."""
    class_name, style_block = length_class(value, prefix=prefix, property_name=property_name)
    return {"class_name": class_name, "style_block": style_block}
