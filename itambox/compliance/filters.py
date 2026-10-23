import django_filters
from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, AssetMaintenance, StatusLabel
from core.filters import BaseFilterSet
from core.managers import Scope
from organization.models import AssetHolder, Location

from .models import AssetAudit, AuditSession, CustodyReceipt, SignatureProviderConfig
from .registry import signature_providers


class CustodyReceiptFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Asset or Holder..."})
    )
    asset = django_filters.ModelChoiceFilter(
        queryset=Asset.objects.all(), label=_("Asset"), widget=forms.Select(attrs={"class": "form-select"})
    )
    holder = django_filters.ModelChoiceFilter(
        queryset=AssetHolder.objects.all(), label=_("Holder"), widget=forms.Select(attrs={"class": "form-select"})
    )
    acceptance_status = django_filters.ChoiceFilter(
        choices=CustodyReceipt.ACCEPTANCE_STATUS_CHOICES,
        label=_("Acceptance Status"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    accepted = django_filters.BooleanFilter(
        method="filter_accepted",
        label=_("Accepted"),
        widget=forms.Select(choices=[("", "Any"), ("true", "Yes"), ("false", "No")], attrs={"class": "form-select"}),
    )

    class Meta:
        model = CustodyReceipt
        fields = ["asset", "holder", "acceptance_status", "accepted"]

    def filter_accepted(self, queryset, name, value):
        accepted = Q(acceptance_status=CustodyReceipt.STATUS_ACCEPTED)
        return queryset.filter(accepted) if value else queryset.exclude(accepted)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(asset__name__icontains=value)
            | Q(holder__first_name__icontains=value)
            | Q(holder__last_name__icontains=value)
            | Q(holder__upn__icontains=value)
            | Q(holder__email__icontains=value)
        ).distinct()


class AssetMaintenanceFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": "Supplier, Notes, Asset Name..."}),
    )
    asset = django_filters.ModelChoiceFilter(
        queryset=Asset.objects.all(), label=_("Asset"), widget=forms.Select(attrs={"class": "form-select"})
    )
    maintenance_type = django_filters.ChoiceFilter(
        choices=AssetMaintenance.MAINTENANCE_TYPE_CHOICES,
        label=_("Type"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = AssetMaintenance
        fields = ["asset", "maintenance_type", "supplier"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(supplier__icontains=value) | Q(notes__icontains=value) | Q(asset__name__icontains=value)
        ).distinct()


class AuditSessionFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name..."})
    )
    status = django_filters.ChoiceFilter(
        choices=[
            ("planned", "Planned"),
            ("active", "Active"),
            ("completed", "Completed"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    location = django_filters.ModelChoiceFilter(
        queryset=Location.objects.all(), widget=forms.Select(attrs={"class": "form-select"}), label=_("Location")
    )

    class Meta:
        model = AuditSession
        fields = ["status", "location"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value)).distinct()


class AssetAuditFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Notes..."})
    )
    session = django_filters.ModelChoiceFilter(
        queryset=lambda request: AuditSession.objects.for_scope(Scope.current()),
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Audit Session"),
    )
    asset = django_filters.ModelChoiceFilter(
        queryset=Asset.objects.all(), widget=forms.Select(attrs={"class": "form-select"}), label=_("Asset")
    )
    location = django_filters.ModelChoiceFilter(
        queryset=Location.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Observed Location"),
    )
    status = django_filters.ModelChoiceFilter(
        queryset=StatusLabel.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Observed Status"),
    )

    class Meta:
        model = AssetAudit
        fields = ["session", "asset", "location", "status", "verification_method"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(notes__icontains=value) | Q(asset__name__icontains=value) | Q(auditor__username__icontains=value)
        ).distinct()


class SignatureProviderConfigFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    provider = django_filters.ChoiceFilter(
        choices=lambda: signature_providers.configurable_choices(),
        label=_("Signature Provider"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    is_active = django_filters.BooleanFilter(
        label=_("Active"),
        widget=forms.Select(choices=[("", "Any"), ("true", "Yes"), ("false", "No")], attrs={"class": "form-select"}),
    )

    class Meta:
        model = SignatureProviderConfig
        fields = ["provider", "is_active", "tenant"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(provider__icontains=value))
