import logging

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.urls import NoReverseMatch, reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import override
from django.views.generic import DetailView
from django_tables2 import RequestConfig

from core.managers import Scope
from core.models import ObjectChange
from core.tables import BaseTable, ObjectChangeTable
from itambox.labels import user_label_print_url
from itambox.registry import registry
from itambox.utils import get_help_url, get_model_viewname
from itambox.views.generic.authorization import PermissionResolver
from itambox.views.generic.capability_notices import capability_notice
from itambox.views.generic.extensions import build_detail_provider_context
from itambox.views.generic.mixins import (
    CachedObjectMixin,
    TenantScopingViewMixin,
    user_can_mutate_model,
)
from itambox.views.generic.related_objects import RelatedObjectProvider
from itambox.views.generic.utils import resolve_view_model
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)


class ObjectDetailView(
    TenantScopingViewMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, CachedObjectMixin, DetailView
):
    template_name = "generic/object_detail.html"
    layout = None
    # Opt-in escape hatch: when True, skip the per-reverse-relation .count()
    # loop entirely (10-15 COUNTs/page) and supply an empty list. Default False
    # preserves identical behavior for every existing detail view.
    disable_related_objects_list = False
    # Sensitive or specially scoped reverse relations can opt out before the
    # provider constructs its count query. Labels use Django's lower-case
    # ``app_label.model_name`` form.
    related_object_exclusions = ()

    def render_to_response(self, context, **response_kwargs):
        # Tables shown in detail-view tabs opt into the shared batch-action bar
        # (rendered by global_includes/htmx_table.html). django_tables2's
        # {% render_table %} only passes {table, request} to the table template, so
        # the flag has to ride on the table instance rather than the page context.
        for value in context.values():
            if isinstance(value, BaseTable):
                value.embed_bulk_bar = True
        return super().render_to_response(context, **response_kwargs)

    def get_permission_required(self):
        return PermissionResolver.model_permissions(resolve_view_model(self), "view")

    def has_permission(self):
        perms = self.get_permission_required()
        return self.request.user.has_perms(perms, obj=PermissionResolver.object_under_check(self))

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()

        tab = request.GET.get("tab")
        if tab and request.headers.get("HX-Request"):
            # Try replacing hyphens with underscores
            tab_clean = tab.replace("-", "_")
            tab_method_name = f"get_tab_{tab_clean}"
            if hasattr(self, tab_method_name):
                return getattr(self, tab_method_name)(request)

            # Try removing hyphens entirely (e.g., asset-holders -> assetholders)
            tab_flat = tab.replace("-", "")
            tab_method_name_flat = f"get_tab_{tab_flat}"
            if hasattr(self, tab_method_name_flat):
                return getattr(self, tab_method_name_flat)(request)

        return super().get(request, *args, **kwargs)

    def get_template_names(self):
        if self.template_name and self.template_name != "generic/object_detail.html":
            return [self.template_name]

        obj = self.get_object()
        if obj:
            app_label = obj._meta.app_label
            model_name = obj._meta.model_name
            with override("en"):
                plural_name = str(obj._meta.verbose_name_plural).lower().replace(" ", "")

            templates_to_try = [
                f"{app_label}/{plural_name}/{model_name}_detail.html",
                f"{app_label}/{model_name}_detail.html",
                "generic/object_detail.html",
            ]

            for template_name in templates_to_try:
                try:
                    get_template(template_name)
                    return [template_name]
                except TemplateDoesNotExist:
                    continue

        return ["generic/object_detail.html"]

    # Both helpers below stay on the view as thin, overridable wrappers: they are
    # part of the subclass-facing surface. The implementation lives in
    # itambox/views/generic/related_objects.py.

    @staticmethod
    def _related_count_uses_distinct(related_model):
        return RelatedObjectProvider.count_uses_distinct(related_model)

    def _build_related_objects_list(self, obj):
        """Build the "Related Objects" sidebar list (label/count/url per reverse
        relation) for ``obj``."""
        if self.related_object_exclusions:
            return RelatedObjectProvider(obj, excluded_model_labels=self.related_object_exclusions).build()
        return RelatedObjectProvider(obj).build()

    def _action_url(self, obj, model_name, action, allowed):
        if not allowed:
            return None
        try:
            return reverse(get_model_viewname(obj, action), kwargs={"pk": obj.pk})
        except NoReverseMatch:
            logger.debug("%s URL not resolvable for %s obj=%s", action.title(), model_name, obj.pk)
            return None

    def _build_mutation_context(self, obj, app_label, model_name):
        mutation_allowed = user_can_mutate_model(self.request.user, obj.__class__)
        can_change = mutation_allowed and self.request.user.has_perm(
            f"{app_label}.change_{model_name}",
            obj=obj,
        )
        can_delete = mutation_allowed and self.request.user.has_perm(
            f"{app_label}.delete_{model_name}",
            obj=obj,
        )
        can_clone = (
            mutation_allowed
            and registry.model_has_feature(obj.__class__, "cloneable")
            and self.request.user.has_perm(f"{app_label}.add_{model_name}", obj=obj)
        )
        return {
            "can_change": can_change,
            "can_delete": can_delete,
            "edit_url": self._action_url(obj, model_name, "update", can_change),
            "delete_url": self._action_url(obj, model_name, "delete", can_delete),
            "clone_url": self._action_url(obj, model_name, "clone", can_clone),
        }

    def _build_changelog_context(self, obj, content_type):
        context = {}
        object_change_type_exists = ContentType.objects.filter(app_label="core", model="objectchange").exists()
        if hasattr(obj, "get_changelog_url"):
            # An object's explicit changelog hook wins over the generic route:
            # models or integrations may override it with a specialized URL.
            context["changelog_url"] = obj.get_changelog_url()
        elif object_change_type_exists:
            context["changelog_url"] = (
                reverse("objectchange_list")
                + "?"
                + urlencode({"changed_object_type": content_type.pk, "changed_object_id": obj.pk})
            )
        if object_change_type_exists:
            queryset = (
                ObjectChange.objects.for_scope(Scope.current())
                .filter(
                    changed_object_type=content_type,
                    changed_object_id=obj.pk,
                )
                .order_by("-time")[:50]
            )
            table = ObjectChangeTable(list(queryset))
            RequestConfig(self.request, paginate={"per_page": 10}).configure(table)
            context["changelog_table"] = table
        return context

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        app_label = obj._meta.app_label
        model_name = obj._meta.model_name
        verbose_name_plural = obj._meta.verbose_name_plural

        context["model"] = obj.__class__
        context["layout"] = self.layout
        context.setdefault("capability_notice", capability_notice(obj.__class__))

        context.update(self._build_mutation_context(obj, app_label, model_name))

        object_display = self.get_object_display(obj)
        context["object_display"] = object_display
        context["title"] = object_display
        base_breadcrumbs = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse(get_model_viewname(obj, "list")), verbose_name_plural),
            (None, context["title"]),
        ]
        context["breadcrumbs"] = getattr(self, "get_breadcrumbs", lambda: base_breadcrumbs)()

        # A4: resolve ContentType once for this object — it is used by changelog,
        # journaling, image/file attachments, bookmarks, and watches below.
        shared_content_type = ContentType.objects.get_for_model(obj)
        context.update(self._build_changelog_context(obj, shared_content_type))

        context["page_actions"] = {
            "edit_url": context.get("edit_url"),
            "delete_url": context.get("delete_url"),
        }
        context["action_urls"] = {
            "edit": context.get("edit_url"),
            "delete": context.get("delete_url"),
            "clone": context.get("clone_url"),
            "label": user_label_print_url(self.request.user, type(obj), obj),
        }
        context["content_template_name"] = self.get_template_names()[0]

        if "related_objects_list" not in context and self.disable_related_objects_list:
            context["related_objects_list"] = []
        elif "related_objects_list" not in context:
            context["related_objects_list"] = self._build_related_objects_list(obj)

        context["help_url"] = get_help_url(self, app_label, model_name)
        return build_detail_provider_context(
            self.request,
            obj,
            shared_content_type,
            core_context=context,
        )

    def get_object_display(self, obj):
        """Return the presentation label without changing the model string contract."""
        return str(obj)
