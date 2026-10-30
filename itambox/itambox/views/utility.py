import logging

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import connection
from django.http import JsonResponse
from django.urls import NoReverseMatch, reverse
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.views.generic import View
from django.views.generic.base import TemplateResponseMixin

from core.forms import SearchForm
from itambox.scanning import strip_itambox_prefix
from itambox.utils import get_model_viewname, get_table_for_model

from .generic import BaseHTMXView

logger = logging.getLogger(__name__)


class SearchView(LoginRequiredMixin, BaseHTMXView, TemplateResponseMixin, View):
    template_name = "core/search.html"

    def get(self, request):
        query = request.GET.get("q", "").strip()
        if query:
            query = strip_itambox_prefix(query)
        obj_types = request.GET.getlist("obj_type")

        lookup = request.GET.get("lookup", "icontains")

        allowed_lookups = {"icontains", "iexact", "istartswith", "iendswith"}
        if lookup not in allowed_lookups:
            lookup = "icontains"

        form = SearchForm(request.GET)
        results_data = {}
        results_count = 0

        if query:
            search_backend_cls = import_string(settings.SEARCH_BACKEND)
            search_backend = search_backend_cls()
            results_data = search_backend.search(query, user=request.user, obj_types=obj_types, lookup=lookup)

            for model, data in results_data.items():
                results_count += data["count"]
                table_class = get_table_for_model(model)
                if table_class:
                    data["table"] = table_class(list(data["queryset"][:10]), request=request)
                    try:
                        data["list_url"] = reverse(get_model_viewname(model, "list"))
                    except NoReverseMatch:
                        data["list_url"] = f"/{model._meta.app_label}/{model._meta.model_name}s/"
                else:
                    data["table"] = None

        context = {
            "form": form,
            "query": query,
            "obj_types": obj_types,
            "lookup": lookup,
            "results": results_data,
            "results_count": results_count,
            "title": _("Search Results"),
            "breadcrumbs": [(reverse("dashboard"), _("Dashboard")), (None, _("Search"))],
        }
        context["content_template_name"] = self.template_name
        return self.render_to_response(context)


def health(request):
    """Readiness/health check.

    Verifies database connectivity so orchestrators (k8s, compose, load
    balancers) don't route traffic to an instance that can't serve requests.
    Returns 200 when healthy, 503 otherwise.
    """
    checks = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    # broad except: boundary-isolation: any driver or connection failure must read as "not ready" (503), never as a 500
    except Exception:
        logger.exception("Health check: database connectivity failed")
        checks["database"] = "error"
        healthy = False

    # Informational only: a broken plugin aborts startup, so a process answering
    # here has a consistent plugin set. "skipped" means a version-incompatible plugin.
    checks["plugins"] = "skipped" if getattr(settings, "PLUGINS_DISABLED", None) else "ok"

    return JsonResponse(
        {"status": "ok" if healthy else "error", "checks": checks},
        status=200 if healthy else 503,
    )
