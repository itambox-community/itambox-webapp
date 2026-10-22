import logging

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _
from django.utils.translation import override
from django.views.generic import ListView

from core.data_transfer import policy_for
from core.features import STABLE
from core.forms.import_forms import is_model_importable
from itambox.registry import registry
from itambox.utils import get_help_url, get_model_viewname
from itambox.views.generic.authorization import PermissionResolver
from itambox.views.generic.bulk_policy import bulk_action_url
from itambox.views.generic.capability_notices import capability_notice
from itambox.views.generic.extensions import (
    build_list_provider_context,
    filter_list_provider_queryset,
    resolve_list_provider_params,
    validate_generic_display_form,
)
from itambox.views.generic.mixins import (
    TenantScopingViewMixin,
    user_can_mutate_model,
)
from itambox.views.generic.table_context import TableContextBuilder
from itambox.views.generic.utils import resolve_view_model
from itambox.views.htmx import BaseHTMXView

logger = logging.getLogger(__name__)


class ObjectListView(TenantScopingViewMixin, PermissionRequiredMixin, LoginRequiredMixin, BaseHTMXView, ListView):
    filterset = None
    filterset_form = None
    table = None
    template_name = "generic/object_list.html"
    content_partial_name = "htmx/list_page_wrapper.html"
    action_buttons = ()

    def get_permission_required(self):
        return PermissionResolver.model_permissions(resolve_view_model(self), "view")

    def get_template_names(self):
        if self.template_name and self.template_name != "generic/object_list.html":
            return [self.template_name]

        model = resolve_view_model(self)

        if model:
            app_label = model._meta.app_label
            model_name = model._meta.model_name
            with override("en"):
                plural_name = str(model._meta.verbose_name_plural).lower().replace(" ", "")

            templates_to_try = [
                f"{app_label}/{plural_name}/{model_name}_list.html",
                f"{app_label}/{model_name}_list.html",
                "generic/object_list.html",
            ]

            for template_name in templates_to_try:
                try:
                    get_template(template_name)
                    return [template_name]
                except TemplateDoesNotExist:
                    continue

        return ["generic/object_list.html"]

    def _get_list_presentation(self, model):
        resolution = getattr(self, "_list_presentation", None)
        if resolution is None:
            resolution = resolve_list_provider_params(
                self.request,
                model,
                partial=self.is_htmx_partial(),
            )
            self._list_presentation = resolution
        return resolution

    def _get_recycle_bin_queryset(self, model):
        """The soft-deleted rows for ``?deleted=true``, still tenant-scoped."""
        if not self.request.user.is_superuser and not self.request.user.has_perm("core.view_recyclebin"):
            raise PermissionDenied(_("You do not have permission to view the Recycle Bin."))
        # unscoped: the recycle bin lists soft-deleted rows; filter_by_tenant narrows them right below, and a
        # tenant-bearing model without it raises
        manager = getattr(model, "all_objects", model._base_manager)
        queryset = manager.all()
        if hasattr(queryset, "filter_by_tenant"):
            queryset = queryset.filter_by_tenant()
        elif any(f.name == "tenant" for f in model._meta.fields):
            # Fail loud: a tenant-bearing model whose all_objects manager cannot
            # scope by tenant would expose other tenants' deleted objects here.
            raise ImproperlyConfigured(
                f"{model.__name__}.all_objects is not tenant-scoped but the model "
                f"has a tenant field. Use TenantScopingAllObjectsManager."
            )
        return queryset.filter(deleted_at__isnull=False)

    def _resolve_filter_params(self, model):
        """Return the final provider-resolved parameters for generic validation."""
        if not model:
            return self.request.GET
        return self._get_list_presentation(model).params

    def get_queryset(self):
        model = resolve_view_model(self)

        show_deleted = self.request.GET.get("deleted") == "true"
        if show_deleted and model and registry.model_has_feature(model, "soft_delete"):
            queryset = self._get_recycle_bin_queryset(model)
        else:
            queryset = super().get_queryset()

        filter_params = self._resolve_filter_params(model)
        self._resolved_filter_params = filter_params
        self.filter_form = None
        self.filter_validation_failed = False

        if self.filterset_form:
            self.filter_form = self.filterset_form(filter_params, queryset=queryset)
            configured_filterset = getattr(self.filter_form, "filterset_class", None)
            if configured_filterset is not self.filterset or not hasattr(self.filter_form, "filterset"):
                filterset_name = self.filterset.__name__ if self.filterset else "None"
                raise ImproperlyConfigured(
                    f"{self.__class__.__name__}.filterset_form must use {filterset_name} as its filterset_class."
                )

        if self.filterset:
            self.filter = self.filter_form.filterset if self.filter_form else self.filterset(filter_params, queryset)
            if not validate_generic_display_form(self.filter_form, self.filter):
                self.filter_validation_failed = True
                logger.warning("Invalid filter params for %s: %s", self.__class__.__name__, self.filter.errors)
                queryset = queryset.none()
            else:
                queryset = self.filter.qs

        if model and not self.filter_validation_failed:
            queryset = filter_list_provider_queryset(
                self._get_list_presentation(model),
                queryset,
            )

        return queryset

    def get_paginate_by(self, queryset):
        return None

    def get_table(self):
        # A5: reuse self.object_list (already filtered + resolved by get_queryset
        # via ListView.get()) instead of calling get_queryset() a second time,
        # which would re-run the full filterset and custom-field filters.
        return TableContextBuilder(self.model, self.table).build(self.object_list, self.request)

    def _resolve_context_model(self):
        model = resolve_view_model(self)
        if not model:
            # Last resort: a view that only overrides get_queryset() still leaves
            # the resolved rows on self.object_list.
            object_list = getattr(self, "object_list", None)
            if object_list is not None:
                model = object_list.model

        if not model:
            raise ImproperlyConfigured(
                f"{self.__class__.__name__} is missing a QuerySet. Define "
                f"{self.__class__.__name__}.model, {self.__class__.__name__}.queryset, or override "
                f"{self.__class__.__name__}.get_queryset()."
            )
        return model

    def _configure_base_context(self, context, model):
        self.model = model
        table = self.get_table()
        table.configure(self.request)
        context["table"] = table
        context["filter_form"] = getattr(self, "filter_form", None)
        context["model"] = model
        context["verbose_name_plural"] = model._meta.verbose_name_plural
        context["model_name_str"] = f"{model._meta.app_label}.{model._meta.model_name}"
        context["table_config_key"] = TableContextBuilder.config_key(model, table)
        context["app_label"] = model._meta.app_label
        context["model_name"] = model._meta.model_name
        context["object_type"] = model._meta.verbose_name

        # The registry answers per model; `is_beta_module` stays for the views
        # that still set it directly and is derived from the notice otherwise.
        notice = capability_notice(model)
        context.setdefault("capability_notice", notice)
        context.setdefault("is_beta_module", notice is not None and notice["maturity"] != STABLE)
        context.setdefault("title", model._meta.verbose_name_plural)
        return user_can_mutate_model(self.request.user, model)

    @staticmethod
    def _bulk_action_url(model, action):
        """The URL this list posts ``action`` to, or ``None`` for no button."""
        return bulk_action_url(model, action)

    def _configure_action_context(self, context, model, mutation_allowed):
        try:
            create_url_name = get_model_viewname(model, "create")
            reverse(create_url_name)
            context["create_url_name"] = create_url_name
        except NoReverseMatch:
            context["create_url_name"] = None

        # Import/export are offered only for importable models (not generated
        # logs or UI-only config). Importable models import via the single
        # centralized route /import/<app>/<model>/.
        importable = is_model_importable(model)
        context["can_export"] = policy_for(model).export
        context["import_url"] = None
        # The generic importer binds its background job to ``active_tenant`` and
        # has no tenant picker. Keep it concrete-scope-only until it can carry
        # the same explicit tenant-selection contract as create forms.
        has_concrete_tenant_scope = self.request.user.is_superuser or bool(getattr(self.request, "active_tenant", None))
        if importable and mutation_allowed and has_concrete_tenant_scope:
            try:
                context["import_url"] = reverse(
                    "generic_import",
                    kwargs={
                        "app_label": model._meta.app_label,
                        "model_name": model._meta.model_name,
                    },
                )
            except NoReverseMatch:
                context["import_url"] = None

        context["bulk_delete_url"] = self._bulk_action_url(model, "bulk_delete")
        context["bulk_edit_url"] = self._bulk_action_url(model, "bulk_edit")
        if not getattr(context.get("table"), "bulk_edit_enabled", True):
            context["bulk_edit_url"] = None
        if not mutation_allowed:
            context["bulk_delete_url"] = None
            context["bulk_edit_url"] = None

        context["can_add"] = mutation_allowed and self.request.user.has_perm(
            f"{model._meta.app_label}.add_{model._meta.model_name}"
        )
        context["action_buttons"] = self.action_buttons
        if "add" in self.action_buttons and not context["create_url_name"]:
            logger.debug("'add' action button enabled but create URL not resolvable for %s", self.model)

    def get_context_data(self, **kwargs):
        _model = self._resolve_context_model()
        context = super().get_context_data(**kwargs)
        mutation_allowed = self._configure_base_context(context, _model)
        self._configure_action_context(context, _model, mutation_allowed)

        has_soft_delete = registry.model_has_feature(_model, "soft_delete")
        show_deleted = self.request.GET.get("deleted") == "true"

        if show_deleted and has_soft_delete:
            context["title"] = _("Recycle Bin: {verbose_name_plural}").format(
                verbose_name_plural=_model._meta.verbose_name_plural,
            )
            context["pretitle"] = _("Trash")
            context["is_deleted_view"] = True

            try:
                ct = self._get_list_presentation(_model).content_type
                context["bulk_restore_url"] = reverse("object_bulk_restore", kwargs={"content_type_id": ct.pk})
                context["bulk_purge_url"] = reverse("object_bulk_purge", kwargs={"content_type_id": ct.pk})
            except Exception:
                context["bulk_restore_url"] = None
                context["bulk_purge_url"] = None

            if not mutation_allowed:
                context["bulk_restore_url"] = None
                context["bulk_purge_url"] = None

            base_breadcrumbs = [
                (reverse("dashboard"), _("Dashboard")),
                (reverse(get_model_viewname(_model, "list")), _model._meta.verbose_name_plural),
                (None, _("Recycle Bin")),
            ]
        else:
            base_breadcrumbs = [
                (reverse("dashboard"), _("Dashboard")),
                (None, context["title"]),
            ]

        context["has_soft_delete"] = has_soft_delete
        context["is_deleted_view"] = show_deleted
        context["breadcrumbs"] = getattr(self, "get_breadcrumbs", lambda: base_breadcrumbs)()
        context["help_url"] = get_help_url(self, _model._meta.app_label, _model._meta.model_name)
        return build_list_provider_context(
            self._get_list_presentation(_model),
            context,
        )
