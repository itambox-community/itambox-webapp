import django_filters
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.forms.scoping import TenantScopedFilterSetMixin
from core.managers import get_current_tenant
from core.models import ObjectChange

User = get_user_model()


class BaseFilterSet(TenantScopedFilterSetMixin, django_filters.FilterSet):
    """Product-wide ``FilterSet`` base: read-time tenant scoping for choice filters.

    ``TenantScopedFilterSetMixin`` re-applies ``filter_by_tenant()`` on every read
    of a model-choice filter's queryset, so both the rendered choices and the
    bound filter's validation follow the ambient scope: single tenant, tenant
    group and all-accessible alike. That is exactly the behaviour the removed
    global ``ModelChoiceField.queryset`` patch gave the filter forms (#584 WP5);
    querysets that cannot be scoped (``User``, ``ContentType``, ``_base_manager``
    pickers, callable request-time resolvers) are left untouched, as before.

    The single-active-tenant narrowing in ``__init__`` is kept on purpose. For
    the global catalogue models (``Manufacturer``, ``AssetType``, ``Category``,
    ``StatusLabel``, ``AssetRole``) it is the only narrowing there is -- those
    managers expose no ``filter_by_tenant()`` -- while for the tenant-bearing
    models it intersects harmlessly with the mixin's read-time scope. Replacing
    its name branches with explicit declarations is a separate change: the
    read-time mixin must not introduce name-based rules.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        current_tenant = get_current_tenant()
        if not current_tenant:
            return

        for _field_name, filter_obj in self.filters.items():
            if isinstance(filter_obj, (django_filters.ModelChoiceFilter, django_filters.ModelMultipleChoiceFilter)):
                queryset = filter_obj.extra.get("queryset")
                # A callable queryset (a request-time resolver, e.g. a user picker) has
                # no `.model`; django-filter resolves it per request, so it cannot be
                # pre-scoped here — skip it instead of crashing on `.model`.
                if queryset is not None and not callable(queryset):
                    model = queryset.model

                    # 1. If model is Tenant, limit choices to the active tenant
                    if model.__name__ == "Tenant":
                        filtered_qs = queryset.filter(pk=current_tenant.pk)

                    # 2. Supplier is tenant-scoped with allow_global_tenant: the scoping
                    #    manager already limits choices to the active scope plus global
                    #    suppliers, so the generic tenant-field narrowing below must not
                    # apply.
                    elif model.__name__ == "Supplier":
                        filtered_qs = queryset

                    # 3. If model has tenant field, filter by active tenant
                    elif hasattr(model, "tenant") or any(f.name == "tenant" for f in model._meta.fields):
                        filtered_qs = queryset.filter(tenant=current_tenant)

                    # 4. If global models, filter by relation to tenant-owned objects
                    elif model.__name__ == "Manufacturer":
                        filtered_qs = queryset.filter(
                            Q(asset_types__assets__tenant=current_tenant)
                            | Q(accessories__tenant=current_tenant)
                            | Q(consumables__tenant=current_tenant)
                        ).distinct()
                    elif model.__name__ == "AssetType":
                        filtered_qs = queryset.filter(assets__tenant=current_tenant).distinct()
                    elif model.__name__ == "Category":
                        filtered_qs = queryset.filter(
                            Q(asset_types__assets__tenant=current_tenant)
                            | Q(accessories__tenant=current_tenant)
                            | Q(consumables__tenant=current_tenant)
                        ).distinct()
                    elif model.__name__ == "StatusLabel":
                        filtered_qs = queryset.filter(assets__tenant=current_tenant).distinct()
                    elif model.__name__ == "AssetRole":
                        filtered_qs = queryset.filter(assets__tenant=current_tenant).distinct()
                    else:
                        continue

                    filter_obj.queryset = filtered_qs
                    filter_obj.extra["queryset"] = filtered_qs


class ObjectChangeFilterSet(BaseFilterSet):
    # Auto-generated, high-volume core bookkeeping models whose change records are
    # noise in the audit trail. Hidden by default; surfaced via "show_system_events".
    NOISE_CONTENT_TYPES = (
        ("core", "job"),
        ("extras", "bookmark"),
        ("extras", "event"),
        ("extras", "reportgenerationarchive"),
    )

    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": _("Search Username, Object, type...")}),
    )

    show_system_events = django_filters.BooleanFilter(
        method="_noop", label=_("Show system events"), widget=forms.CheckboxInput(attrs={"class": "form-check-input"})
    )

    action = django_filters.MultipleChoiceFilter(
        choices=[(c[0], c[1]) for c in ObjectChangeActionChoices().CHOICES],
        label=_("Action"),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    changed_object_type = django_filters.ModelMultipleChoiceFilter(
        queryset=ContentType.objects.order_by("app_label", "model"),
        label=_("Object Type"),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    user = django_filters.ModelChoiceFilter(
        queryset=User.objects.order_by("username"), label=_("User"), widget=forms.Select(attrs={"class": "form-select"})
    )

    time_after = django_filters.DateTimeFilter(
        field_name="time",
        lookup_expr="gte",
        label=_("After"),
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
    )

    time_before = django_filters.DateTimeFilter(
        field_name="time",
        lookup_expr="lte",
        label=_("Before"),
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
    )

    class Meta:
        model = ObjectChange
        fields = []

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(user_name__icontains=value) | Q(object_repr__icontains=value) | Q(object_type_repr__icontains=value)
        ).distinct()

    def _noop(self, queryset, name, value):
        # The actual show/hide logic lives in filter_queryset so it can run when
        # the (unchecked) checkbox submits no value. This keeps the field for the form.
        return queryset

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        raw = (self.data or {}).get("show_system_events")
        show_system = str(raw).lower() in ("1", "true", "on", "yes")
        if not show_system:
            noise_q = None
            for app_label, model in self.NOISE_CONTENT_TYPES:
                clause = Q(
                    changed_object_type__app_label=app_label,
                    changed_object_type__model=model,
                )
                noise_q = clause if noise_q is None else (noise_q | clause)
            if noise_q is not None:
                queryset = queryset.exclude(noise_q)
        return queryset
