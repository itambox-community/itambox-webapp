"""Template access to the effective theme preference.

Kept as a template tag rather than a context processor so HTMX fragments and
API responses never pay for the preference lookup.
"""

from django import template

from users.models import get_theme_preference

register = template.Library()


@register.simple_tag(takes_context=True)
def theme_preference(context):
    """Return the effective theme preference for the current request."""
    request = context.get("request")
    return get_theme_preference(getattr(request, "user", None))
