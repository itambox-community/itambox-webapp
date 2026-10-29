import django_filters
from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.models import Supplier
from core.filters import BaseFilterSet
from core.managers import Scope
from organization.models import CostCenter, Tenant

from .models import Subscription, SubscriptionAssignment, SubscriptionStatusChoices, SubscriptionTypeChoices


class SubscriptionFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": _("Name, description, or contract reference")}),
    )
    type = django_filters.ChoiceFilter(
        field_name="type",
        choices=SubscriptionTypeChoices.choices,
        label=_("Type"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = django_filters.ChoiceFilter(
        field_name="status",
        choices=SubscriptionStatusChoices.choices,
        label=_("Status"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    vendor_contract_auto_renews = django_filters.BooleanFilter(
        field_name="vendor_contract_auto_renews",
        label=_("Vendor Contract Auto-Renews"),
    )
    auto_renewal = django_filters.BooleanFilter(
        field_name="vendor_contract_auto_renews",
        label=_("Auto-Renewal (deprecated alias)"),
    )
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(), widget=forms.Select(attrs={"class": "form-select"}), label=_("Tenant")
    )
    supplier = django_filters.ModelChoiceFilter(
        queryset=Supplier.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Supplier"),
    )
    cost_center = django_filters.ModelChoiceFilter(
        queryset=CostCenter.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Cost Center"),
    )
    renewal_within = django_filters.NumberFilter(
        method="filter_renewal_within",
        label=_("Renews Within (Days)"),
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "e.g. 30"}),
    )

    class Meta:
        model = Subscription
        fields = [
            "type",
            "status",
            "vendor_contract_auto_renews",
            "auto_renewal",
            "tenant",
            "supplier",
            "cost_center",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filters["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.filters["cost_center"].field.label_from_instance = lambda cost_center: (
            f"{cost_center.code}: {cost_center.name}" if cost_center.code else cost_center.name
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(description__icontains=value)
            | Q(notes__icontains=value)
            | Q(contract_reference__icontains=value)
            | Q(supplier__name__icontains=value)
        ).distinct()

    def filter_renewal_within(self, queryset, name, value):
        if value:
            cutoff = timezone.now().date() + timezone.timedelta(days=int(value))
            return queryset.filter(renewal_date__lte=cutoff, renewal_date__gte=timezone.now().date())
        return queryset


class SubscriptionAssignmentFilterSet(BaseFilterSet):
    class Meta:
        model = SubscriptionAssignment
        fields = ["subscription", "content_type", "object_id"]
