from itambox.plugins.utils import get_plugin_diagnostics
from itambox.release import VERSION


def settings_processor(request):
    """
    Expose a minimal, explicit settings dict to templates ({{ settings.VERSION }}).
    Deliberately NOT the full Django settings object — only what templates need.
    """
    return {"settings": {"VERSION": VERSION}}


def notifications_processor(request):
    """Context processor providing unread notification counts and items globally."""
    if request.user.is_authenticated:
        from django.utils.functional import SimpleLazyObject

        from core.models import Notification

        def get_unread_count():
            return Notification.objects.filter(user=request.user, is_read=False).count()

        def get_recent_unread():
            return Notification.objects.filter(user=request.user, is_read=False).order_by("-created_at")[:5]

        return {
            "unread_notifications_count": SimpleLazyObject(get_unread_count),
            "recent_unread_notifications": SimpleLazyObject(get_recent_unread),
        }
    return {"unread_notifications_count": 0, "recent_unread_notifications": []}


def base_template_processor(request):
    """
    Determine the base template to extend based on whether the request is a boosted HTMX request.
    This ensures that views do not double-render the main layout when loaded via HTMX boosted links,
    while still rendering the full layout for direct page loads.
    """
    if hasattr(request, "base_template"):
        return {"base_template": request.base_template}

    base_template = "layout.html"
    if getattr(request, "htmx", False):
        target = getattr(request.htmx, "target", "") or ""
        is_boosted_main_swap = (
            getattr(request.htmx, "boosted", False)
            or getattr(request.htmx, "history_restore_request", False)
            or target in ("page-content-wrapper", "#page-content-wrapper", "page-body-main", "#page-body-main")
        )
        if is_boosted_main_swap:
            base_template = "base_htmx.html"

    return {"base_template": base_template}


def plugin_diagnostics_processor(request):
    """Expose startup-failure rows for the in-product notice, to operators only.

    TODO(F9): replace the superuser check with a dedicated operator permission
    (``core.view_plugin_diagnostics``) once the plugin-foundation design lands it.
    """
    user = getattr(request, "user", None)
    if not (user is not None and user.is_authenticated and user.is_superuser):
        return {"plugin_diagnostics": []}
    return {"plugin_diagnostics": get_plugin_diagnostics()}
