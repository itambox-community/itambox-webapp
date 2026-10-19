"""Shared footer forms for the scanner-driven bulk check-in / disposal baskets.

These collect the *batch-wide* fields applied to every scanned asset. Per-asset
values that cannot be shared (disposal proceeds) are entered on each basket row
in the UI and posted as ``proceeds_<pk>`` — they are not declared here.
"""

import datetime

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Fieldset, Layout
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, AssetDisposal, StatusLabel
from core.forms.base import TenantScopedFormMixin
from core.forms.choices import apply_empty_choice_first
from core.managers import get_current_tenant
from core.tenant_scope import accessible_tenant_ids
from organization.models import AssetHolder, Location, Tenant


def bulk_tenant_queryset(request):
    """Return active tenants the request user may explicitly target."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return Tenant.objects.none()
    return Tenant.objects.filter(
        pk__in=accessible_tenant_ids(user),
        deleted_at__isnull=True,
    ).order_by("name")


def bulk_tenant_for_request(request, raw_value=None):
    """Resolve the tenant bound to a bulk action without widening scope.

    In a concrete tenant scope, the request context is authoritative and the
    posted hidden field is ignored. In the aggregate scope, the submitted value
    must be one of the user's accessible tenants.
    """
    if not getattr(request, "active_all_accessible", False):
        return getattr(request, "active_tenant", None) or get_current_tenant()
    if raw_value is None:
        raw_value = request.POST.get("tenant")
    if not raw_value:
        return None
    return bulk_tenant_queryset(request).filter(pk=raw_value).first()


def validate_bulk_tenant(request, form_class):
    """Return ``(tenant, error_message)`` for a bulk submission."""
    if not getattr(request, "active_all_accessible", False):
        tenant = bulk_tenant_for_request(request)
        return tenant, None

    field = form_class(request=request).fields["tenant"]
    try:
        return field.clean(request.POST.get("tenant")), None
    except ValidationError:
        return None, _("Select an accessible target tenant before starting this bulk action.")


class BulkTenantSelectionMixin:
    """Add the explicit target tenant required by aggregate bulk actions."""

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
        self.fields["tenant"] = forms.ModelChoiceField(
            queryset=Tenant.objects.none(),
            required=False,
            widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            label=_("Target tenant"),
            help_text=_("Required when the current scope contains more than one tenant."),
        )

        all_accessible = bool(getattr(request, "active_all_accessible", False))
        current_tenant = getattr(request, "active_tenant", None) or get_current_tenant()
        self.fields["tenant"].queryset = (
            bulk_tenant_queryset(request)
            if all_accessible
            else (Tenant.objects.filter(pk=current_tenant.pk) if current_tenant else Tenant.objects.none())
        )
        self.fields["tenant"].required = all_accessible
        if not all_accessible:
            self.fields["tenant"].widget = forms.HiddenInput()
            if current_tenant:
                self.fields["tenant"].initial = current_tenant.pk

        # The blank "no target tenant" entry stays first. It comes from
        # Django's ``empty_label`` while this is a ModelChoiceField; the helper
        # keeps the position if the field is ever rebuilt from a manual list.
        apply_empty_choice_first(self.fields["tenant"])


def _tenant_locations():
    """Active-tenant location queryset (falls back to the scoped manager)."""
    qs = Location.objects.select_related("site").order_by("site__name", "name")
    tenant = get_current_tenant()
    if tenant:
        qs = qs.filter(tenant=tenant)
    return qs


class AssetBulkCheckInForm(TenantScopedFormMixin, BulkTenantSelectionMixin, forms.Form):
    """Batch-wide check-in options applied to every scanned asset."""

    status = forms.ModelChoiceField(
        queryset=StatusLabel.objects.exclude(type="deployed").order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Status after check-in"),
        help_text=_("Leave blank to revert each asset to its pre-checkout status."),
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Return to location"),
        help_text=_("Optional. Leave blank to keep each asset's current location."),
    )
    checkin_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Check-in date"),
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
        required=False,
        label=_("Notes"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = _tenant_locations()
        self.fields["checkin_date"].initial = datetime.date.today()

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            "tenant",
            Div(
                Div("status", css_class="col-md-6"),
                Div("location", css_class="col-md-6"),
                css_class="row",
            ),
            Div(
                Div("checkin_date", css_class="col-md-6"),
                css_class="row",
            ),
            "notes",
        )


class AssetBulkDisposeForm(TenantScopedFormMixin, BulkTenantSelectionMixin, forms.ModelForm):
    """Batch-wide disposal options. ``proceeds`` is captured per-row in the UI."""

    disposal_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        required=True,
        label=_("Disposal date"),
    )

    class Meta:
        model = AssetDisposal
        fields = [
            "disposal_method",
            "disposal_date",
            "data_sanitization_method",
            "sanitization_certificate",
            "sanitized_by",
            "recipient",
            "currency",
            "weee_compliant",
            "notes",
        ]
        widgets = {
            "disposal_method": forms.Select(attrs={"class": "form-select"}),
            "data_sanitization_method": forms.Select(attrs={"class": "form-select"}),
            "sanitization_certificate": forms.TextInput(attrs={"class": "form-control"}),
            "sanitized_by": forms.TextInput(attrs={"class": "form-control"}),
            "recipient": forms.TextInput(attrs={"class": "form-control"}),
            "currency": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "weee_compliant": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["disposal_date"].initial = datetime.date.today()

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            "tenant",
            Fieldset(
                _("Disposal Details"),
                Div(
                    Div("disposal_method", css_class="col-md-6"),
                    Div("disposal_date", css_class="col-md-6"),
                    css_class="row",
                ),
                Div(
                    Div("recipient", css_class="col-md-6"),
                    Div("currency", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Data Sanitization & Compliance"),
                Div(
                    Div("data_sanitization_method", css_class="col-md-4"),
                    Div("sanitized_by", css_class="col-md-4"),
                    Div("sanitization_certificate", css_class="col-md-4"),
                    css_class="row",
                ),
                Div(
                    Div("weee_compliant", css_class="col-md-4 d-flex align-items-end pb-2"),
                    css_class="row",
                ),
            ),
            "notes",
        )


def _tenant_holders():
    qs = AssetHolder.objects.all().order_by("last_name", "first_name")
    tenant = get_current_tenant()
    if tenant:
        qs = qs.filter(tenant=tenant)
    return qs


def _tenant_target_assets():
    qs = Asset.issuable().order_by("name")
    tenant = get_current_tenant()
    if tenant:
        qs = qs.filter(tenant=tenant)
    return qs


class AssetBulkCheckOutForm(TenantScopedFormMixin, BulkTenantSelectionMixin, forms.Form):
    """Batch-wide check-out target + options applied to every scanned asset.

    Exactly one target (holder / location / parent asset) is required; the
    submit view enforces that on the raw POST (this form renders the footer).
    """

    asset_holder = forms.ModelChoiceField(
        queryset=AssetHolder.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Asset holder"),
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Location"),
    )
    asset_target = forms.ModelChoiceField(
        queryset=Asset.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Parent asset"),
    )
    status = forms.ModelChoiceField(
        queryset=StatusLabel.objects.filter(type="deployed").order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Status after check-out"),
    )
    checkout_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Check-out date"),
    )
    expected_checkin = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Expected check-in date"),
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
        required=False,
        label=_("Notes"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset_holder"].queryset = _tenant_holders()
        self.fields["location"].queryset = _tenant_locations()
        self.fields["asset_target"].queryset = _tenant_target_assets()
        initial_status = StatusLabel.objects.filter(type="deployed").first()
        if initial_status:
            self.fields["status"].initial = initial_status

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            "tenant",
            HTML(
                '<p class="text-muted small mb-2">%s</p>'
                % _("Choose exactly one target: a holder, a location, or a parent asset.")
            ),
            "asset_holder",
            "location",
            "asset_target",
            Div(
                Div("status", css_class="col-md-6"),
                Div("checkout_date", css_class="col-md-6"),
                css_class="row",
            ),
            Div(
                Div("expected_checkin", css_class="col-md-6"),
                css_class="row",
            ),
            "notes",
        )
