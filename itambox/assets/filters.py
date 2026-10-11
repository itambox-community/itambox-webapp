from collections.abc import Mapping

import django_filters
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from assets.choices import RequestStatusChoices
from assets.models.audit_cadence import audit_due_on_expression
from core.filters import BaseFilterSet
from extras.models import Tag
from itambox.scanning import strip_itambox_prefix
from organization.models import AssetHolder, Location, Site, Tenant

from .models import (
    Asset,
    AssetDisposal,
    AssetRequest,
    AssetReservation,
    AssetRole,
    AssetTagSequence,
    AssetType,
    Category,
    Depreciation,
    Manufacturer,
    StatusLabel,
    Supplier,
    Warranty,
)
from .models.choices import (
    DataSanitizationMethodChoices,
    DisposalMethodChoices,
    ReservationStatusChoices,
    WarrantyTypeChoices,
)
from .services.specification_consumers.contracts import (
    FieldReference,
    identify_saved_references,
    parse_filter_document,
)
from .services.specification_consumers.query import apply_specification_filters

User = get_user_model()


def saved_specification_reference_impacts(
    parameters,
    *,
    known_references: Mapping[FieldReference, str] | None = None,
):
    """Inventory canonical and legacy references before a saved filter is used."""

    return identify_saved_references(parameters, known_references=known_references)


class SpecificationFilterMixin:
    """Shared explicit JSON filter entry point for Asset and AssetType lists."""

    def filter_specification(self, queryset, name, value):
        if not value:
            return queryset
        try:
            filters = parse_filter_document(value)
        except (TypeError, ValueError):
            return queryset.none()
        definitions = getattr(self, "specification_definitions", None)
        return apply_specification_filters(queryset, filters, definitions=definitions).distinct()


class AssetFilterSet(SpecificationFilterMixin, BaseFilterSet):
    specification = django_filters.CharFilter(
        method="filter_specification",
        label=_("Specification filters"),
        widget=forms.HiddenInput(),
    )
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name, Tag, Serial..."})
    )

    status = django_filters.ModelChoiceFilter(
        queryset=StatusLabel.objects.all(), label=_("Status"), widget=forms.Select(attrs={"class": "form-select"})
    )
    asset_role = django_filters.ModelChoiceFilter(
        queryset=AssetRole.objects.all(), label=_("Asset Role"), widget=forms.Select(attrs={"class": "form-select"})
    )
    asset_type = django_filters.ModelChoiceFilter(
        queryset=AssetType.objects.all().select_related("manufacturer"),
        label=_("Asset Type"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    ean = django_filters.CharFilter(
        field_name="asset_type__ean",
        lookup_expr="iexact",
        label=_("EAN"),
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "EAN / barcode"}),
    )
    manufacturer = django_filters.ModelChoiceFilter(
        field_name="asset_type__manufacturer",
        queryset=Manufacturer.objects.all(),
        label=_("Manufacturer"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    category = django_filters.ModelChoiceFilter(
        field_name="asset_type__category",
        queryset=Category.objects.all(),
        label=_("Category"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    location = django_filters.ModelChoiceFilter(
        queryset=Location.objects.all().select_related("site"), widget=forms.Select(attrs={"class": "form-select"})
    )
    site = django_filters.ModelChoiceFilter(
        field_name="location__site",
        queryset=Site.objects.all(),
        label=_("Site"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(), widget=forms.Select(attrs={"class": "form-select"}), label=_("Tenant")
    )
    assigned_to = django_filters.ModelChoiceFilter(
        field_name="assignments__assigned_user",
        queryset=AssetHolder.objects.all(),
        label=_("Assigned To"),
        widget=forms.Select(attrs={"class": "form-select"}),
        method="filter_assigned_to",
    )
    supplier = django_filters.ModelChoiceFilter(
        queryset=Supplier.objects.all(), label=_("Supplier"), widget=forms.Select(attrs={"class": "form-select"})
    )
    tags = django_filters.ModelMultipleChoiceFilter(
        field_name="tags__slug",
        queryset=Tag.objects.all(),
        to_field_name="slug",
        label=_("Tags"),
        conjoined=True,
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )
    purchase_date_after = django_filters.DateFilter(
        field_name="purchase_date",
        lookup_expr="gte",
        label=_("Purchased After"),
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    purchase_date_before = django_filters.DateFilter(
        field_name="purchase_date",
        lookup_expr="lte",
        label=_("Purchased Before"),
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    requestable = django_filters.BooleanFilter(
        method="filter_requestable",
        label=_("Requestable"),
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))], attrs={"class": "form-select"}
        ),
    )
    audit_due = django_filters.BooleanFilter(
        method="filter_audit_due",
        label=_("Audit Due"),
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Overdue")), ("false", _("Up to date"))], attrs={"class": "form-select"}
        ),
    )

    class Meta:
        model = Asset
        # All filters defined explicitly above — no auto-generated fields needed
        fields = []

    def filter_requestable(self, queryset, name, value):
        if value is None:
            return queryset
        if value:
            return queryset.filter(Q(requestable=True) | Q(requestable__isnull=True, asset_type__requestable=True))
        else:
            return queryset.filter(
                Q(requestable=False)
                | Q(requestable__isnull=True, asset_type__isnull=True)
                | Q(requestable__isnull=True, asset_type__requestable=False)
            )

    def search(self, queryset, name, value):
        """Perform basic search across designated fields."""
        if not value.strip():
            return queryset
        value = strip_itambox_prefix(value)
        # Basic search across name, asset_tag, serial_number (can be expanded)
        # Consider adding asset_holder.name if performance allows
        return queryset.filter(
            Q(name__icontains=value) | Q(asset_tag__icontains=value) | Q(serial_number__icontains=value)
        ).distinct()

    def filter_assigned_to(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(assignments__assigned_user=value, assignments__is_active=True)

    def filter_audit_due(self, queryset, name, value):
        if value is None:
            return queryset

        from django.utils import timezone

        today = timezone.localdate()
        has_cadence_q = Q(asset_type__category__audit_interval_months__isnull=False)
        queryset = queryset.annotate(_audit_due_on=audit_due_on_expression())
        overdue_q = has_cadence_q & Q(_audit_due_on__lt=today)
        if value:
            return queryset.filter(overdue_q)
        return queryset.filter(~has_cadence_q | Q(_audit_due_on__gte=today))


# --- AssetRole Filter ---
class AssetRoleFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name, Description..."})
    )

    class Meta:
        model = AssetRole
        fields = ["name"]  # Add other specific fields if needed later

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


# --- Manufacturer Filter ---
class ManufacturerFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name, Description..."})
    )

    class Meta:
        model = Manufacturer
        fields = ["name"]  # Add other specific fields if needed later

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


class AssetTypeFilterSet(SpecificationFilterMixin, BaseFilterSet):
    specification = django_filters.CharFilter(
        method="filter_specification",
        label=_("Specification filters"),
        widget=forms.HiddenInput(),
    )
    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
    )
    manufacturer = django_filters.ModelChoiceFilter(
        queryset=Manufacturer.objects.all(), field_name="manufacturer", label=_("Manufacturer")
    )
    category = django_filters.ModelChoiceFilter(
        queryset=Category.objects.all(), label=_("Category"), widget=forms.Select(attrs={"class": "form-select"})
    )
    requestable = django_filters.BooleanFilter(
        label=_("Requestable"),
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))], attrs={"class": "form-select"}
        ),
    )

    class Meta:
        model = AssetType
        fields = ["manufacturer", "model", "part_number", "category", "requestable"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(model__icontains=value)
            | Q(part_number__icontains=value)
            | Q(description__icontains=value)
            | Q(manufacturer__name__icontains=value)
        ).distinct()


class StatusLabelFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name, Description..."})
    )
    type = django_filters.MultipleChoiceFilter(
        choices=StatusLabel.TYPE_CHOICES, widget=forms.SelectMultiple(attrs={"class": "form-select"})
    )

    class Meta:
        model = StatusLabel
        fields = ["name", "type"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


class DepreciationFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    method = django_filters.ChoiceFilter(choices=Depreciation.Method.choices)
    convention = django_filters.ChoiceFilter(choices=Depreciation.Convention.choices)

    class Meta:
        model = Depreciation
        fields = ["name", "months", "method", "convention"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


class SupplierFilterSet(BaseFilterSet):
    is_active = django_filters.BooleanFilter(label=_("Active"))
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name, Account ID..."})
    )

    class Meta:
        model = Supplier
        fields = ["name", "is_active"]

    def search(self, queryset, name, value):
        # Search parity with the retired provider filter: account_id and notes
        # were transplanted from Provider and must stay findable via ?q=.
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(website__icontains=value)
            | Q(account_id__icontains=value)
            | Q(notes__icontains=value)
        ).distinct()


class CategoryFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Name..."})
    )

    class Meta:
        model = Category
        fields = ["name"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


class AssetRequestFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Search..."})
    )
    status = django_filters.ChoiceFilter(
        choices=RequestStatusChoices.choices, widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = AssetRequest
        fields = ["status"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(asset__name__icontains=value)
            | Q(asset_type__model__icontains=value)
            | Q(requester__username__icontains=value)
            | Q(notes__icontains=value)
        ).distinct()


class AssetTagSequenceFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": "Prefix..."})
    )
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(), widget=forms.Select(attrs={"class": "form-select"}), label=_("Tenant")
    )
    category = django_filters.ModelChoiceFilter(
        queryset=Category.objects.all(), widget=forms.Select(attrs={"class": "form-select"}), label=_("Category")
    )
    is_active = django_filters.BooleanFilter(
        widget=forms.Select(
            choices=[("", _("All")), ("True", _("Active")), ("False", _("Inactive"))], attrs={"class": "form-select"}
        ),
        label=_("Active"),
    )

    class Meta:
        model = AssetTagSequence
        fields = ["prefix", "tenant", "category", "is_active"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(prefix__icontains=value)).distinct()


# AuditSessionFilterSet / AssetAuditFilterSet moved to compliance.filters


class AssetDisposalFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": "Recipient, Sanitized By, Certificate, Notes, Asset Name..."}),
    )
    asset = django_filters.ModelChoiceFilter(
        queryset=Asset.objects.all(), label=_("Asset"), widget=forms.Select(attrs={"class": "form-select"})
    )
    disposal_method = django_filters.ChoiceFilter(
        choices=DisposalMethodChoices.choices,
        label=_("Disposal Method"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    data_sanitization_method = django_filters.ChoiceFilter(
        choices=DataSanitizationMethodChoices.choices,
        label=_("Data Sanitization Method"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    disposal_date = django_filters.DateFromToRangeFilter(
        label=_("Disposal Date"),
        widget=django_filters.widgets.RangeWidget(attrs={"class": "form-control", "type": "date"}),
    )
    weee_compliant = django_filters.BooleanFilter(
        label=_("WEEE Compliant"),
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))], attrs={"class": "form-select"}
        ),
    )

    class Meta:
        model = AssetDisposal
        fields = ["asset", "disposal_method", "data_sanitization_method", "disposal_date", "weee_compliant"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(recipient__icontains=value)
            | Q(sanitized_by__icontains=value)
            | Q(sanitization_certificate__icontains=value)
            | Q(notes__icontains=value)
            | Q(asset__name__icontains=value)
        ).distinct()


class WarrantyFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": "Supplier, Reference, Terms, Notes, Asset Name..."}),
    )
    asset = django_filters.ModelChoiceFilter(
        queryset=Asset.objects.all(), label=_("Asset"), widget=forms.Select(attrs={"class": "form-select"})
    )
    warranty_type = django_filters.ChoiceFilter(
        choices=WarrantyTypeChoices.choices,
        label=_("Warranty Type"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    supplier = django_filters.ModelChoiceFilter(
        queryset=Supplier.objects.all(), label=_("Supplier"), widget=forms.Select(attrs={"class": "form-select"})
    )
    start_date = django_filters.DateFromToRangeFilter(
        label=_("Start Date"),
        widget=django_filters.widgets.RangeWidget(attrs={"class": "form-control", "type": "date"}),
    )
    end_date = django_filters.DateFromToRangeFilter(
        label=_("End Date"), widget=django_filters.widgets.RangeWidget(attrs={"class": "form-control", "type": "date"})
    )

    class Meta:
        model = Warranty
        fields = ["asset", "warranty_type", "supplier", "start_date", "end_date"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(supplier__name__icontains=value)
            | Q(reference__icontains=value)
            | Q(terms__icontains=value)
            | Q(notes__icontains=value)
            | Q(asset__name__icontains=value)
        ).distinct()


class AssetReservationFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search",
        label=_("Search"),
        widget=forms.TextInput(attrs={"placeholder": "Purpose, Notes, Asset Name, Reserved For..."}),
    )
    asset = django_filters.ModelChoiceFilter(
        queryset=Asset.objects.all(), label=_("Asset"), widget=forms.Select(attrs={"class": "form-select"})
    )
    reserved_for = django_filters.ModelChoiceFilter(
        queryset=AssetHolder.objects.all(),
        label=_("Reserved For"),
        null_label=_("(no holder)"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = django_filters.ChoiceFilter(
        choices=ReservationStatusChoices.choices, label=_("Status"), widget=forms.Select(attrs={"class": "form-select"})
    )
    start_date = django_filters.DateFromToRangeFilter(
        label=_("Start Date"),
        widget=django_filters.widgets.RangeWidget(attrs={"class": "form-control", "type": "date"}),
    )
    end_date = django_filters.DateFromToRangeFilter(
        label=_("End Date"), widget=django_filters.widgets.RangeWidget(attrs={"class": "form-control", "type": "date"})
    )
    created_by = django_filters.ModelChoiceFilter(
        queryset=User.objects.order_by("username"),
        label=_("Created By"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = AssetReservation
        fields = ["asset", "reserved_for", "status", "start_date", "end_date", "created_by"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(purpose__icontains=value)
            | Q(notes__icontains=value)
            | Q(asset__name__icontains=value)
            | Q(reserved_for__first_name__icontains=value)
            | Q(reserved_for__last_name__icontains=value)
        ).distinct()
