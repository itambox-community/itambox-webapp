import logging

from django import template
from django.utils.safestring import mark_safe

from extras.dashboard.widgets import get_widget

logger = logging.getLogger(__name__)
register = template.Library()


@register.simple_tag(takes_context=True)
def render_widget(context, widget_config, index=0):
    """
    Render a dashboard widget from its config dict.

    Usage: {% render_widget widget_config index %}
    """
    request = context.get("request")
    if not request:
        return ""

    widget_id = widget_config.get("widget")
    widget_cls = get_widget(widget_id)
    if not widget_cls:
        return f"<!-- Unknown widget: {widget_id} -->"

    try:
        # Safe copy to prevent in-place memory overrides on layout configurations
        cfg = dict(widget_config)
        if "config" not in cfg:
            cfg["config"] = {}
        else:
            cfg["config"] = dict(cfg["config"])

        # Intercept and override scoping boundaries if dashboard is bound to a tenant
        active_db = context.get("active_dashboard")
        if active_db and active_db.tenant:
            cfg["config"]["tenant_id"] = str(active_db.tenant.id)
            cfg["tenant_id"] = str(active_db.tenant.id)

        instance = widget_cls(config=cfg)
        instance.index = index
        return mark_safe(instance.render(request))
    # broad except: render-degrade: one failing widget must not take the whole dashboard down
    except Exception:
        # The traceback goes to the server log; the page only gets an opaque marker so
        # exception text (queries, paths, object reprs) is never rendered to the client.
        logger.exception("Dashboard widget %s failed to render", widget_id)
        return f"<!-- Widget error ({widget_id}) -->"


@register.simple_tag
def get_widget_icon(widget_config):
    """
    Return the MDI icon name (without 'mdi-' prefix) for a widget config.

    Usage: {% get_widget_icon entry.config as widget_icon %}
    """
    widget_cls = get_widget(widget_config.get("widget"))
    return getattr(widget_cls, "icon", None) or "view-grid-outline"


@register.simple_tag(takes_context=True)
def get_widget_footer_links(context, widget_config, index=0):
    """
    Get the footer links for a dashboard widget.

    Usage: {% get_widget_footer_links widget_config index %}
    """
    request = context.get("request")
    if not request:
        return []
    widget_id = widget_config.get("widget")
    widget_cls = get_widget(widget_id)
    if not widget_cls:
        return []
    try:
        # Safe copy to prevent in-place memory overrides on layout configurations
        cfg = dict(widget_config)
        if "config" not in cfg:
            cfg["config"] = {}
        else:
            cfg["config"] = dict(cfg["config"])

        # Intercept and override scoping boundaries if dashboard is bound to a tenant
        active_db = context.get("active_dashboard")
        if active_db and active_db.tenant:
            cfg["config"]["tenant_id"] = str(active_db.tenant.id)
            cfg["tenant_id"] = str(active_db.tenant.id)

        instance = widget_cls(config=cfg)
        instance.index = index
        if hasattr(instance, "get_footer_links"):
            return instance.get_footer_links(request)
    # broad except: render-degrade: a widget whose footer cannot be built renders without links
    except Exception:
        logger.exception("Dashboard widget %s failed to build its footer links", widget_id)
    return []
