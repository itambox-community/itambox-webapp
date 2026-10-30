from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Fieldset, Layout, Submit
from django import forms
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, Supplier
from core.forms import CrispyFormMixin, FilterForm, TenantScopedFormMixin, cancel_button
from core.managers import Scope
from extras.customfields import CustomFieldModelFormMixin
from extras.models import Tag
from organization.models import AssetHolder
from software.models import Software

from .filters import LicenseFilterSet
from .models import License, LicenseSeatAssignment


class LicenseForm(TenantScopedFormMixin, CrispyFormMixin, CustomFieldModelFormMixin, forms.ModelForm):
    """Form for creating and updating License entitlements."""

    tenant_required = True
    tenant_autoset_when_single = True

    software = forms.ModelChoiceField(
        queryset=Software.objects.all(),
        label=_("Software Catalog Item"),
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.filter(is_active=True),
        required=False,
        label=_("Supplier"),
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
    )

    class Meta:
        model = License
        fields = (
            "name",
            "license_type",
            "software",
            "version",
            "seats",
            "product_key",
            "order_number",
            "supplier",
            "purchase_date",
            "purchase_cost",
            "currency",
            "subscription",
            "cost_center",
            "expiration_date",
            "tenant",
            "notes",
            "tags",
        )
        widgets = {
            "product_key": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "purchase_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "expiration_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "subscription": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "cost_center": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "tenant": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "license_type": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "version": forms.TextInput(attrs={"class": "form-control"}),
            "seats": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "order_number": forms.TextInput(attrs={"class": "form-control"}),
            "purchase_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "currency": forms.TextInput(attrs={"class": "form-control", "maxlength": 3}),
        }
        help_texts = {
            "name": _("Unique renewal or purchase name (e.g., Office 365 E5 Enterprise Renewal FY26)"),
            "seats": _("Total number of activation seats purchased"),
            "version": _(
                "Optional version constraint for this license entitlement (e.g. '2021', '16.x'). "
                "Reconciliation ignores the version and runs at the Software level."
            ),
            "product_key": _("License activation key or volume credential"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        # `cost_center` / `subscription` (and the other model choices) are scoped by
        # TenantScopedFormMixin. `software` is validated same-tenant in
        # License.clean(); `supplier` is a global catalogue model.
        self.fields["cost_center"].label_from_instance = lambda cost_center: (
            f"{cost_center.code}: {cost_center.name}" if cost_center.code else cost_center.name
        )
        if self.instance and self.instance.pk:
            self.fields["product_key"].initial = self.instance.decrypted_product_key

        if self.instance and self.instance.pk:
            cancel_url = self.instance.get_absolute_url()
        else:
            cancel_url = reverse("licenses:license_list")

        self.helper.layout = Layout(
            Fieldset(
                _("Identity"),
                Div(
                    Div("name", css_class="col-md-6"),
                    Div("license_type", css_class="col-md-6"),
                    css_class="row",
                ),
                Div(
                    Div("software", css_class="col-md-6"),
                    Div("version", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Entitlement"),
                Div(
                    Div("seats", css_class="col-md-6"),
                    Div("product_key", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Procurement & Financial"),
                Div(
                    Div("order_number", css_class="col-md-4"),
                    Div("supplier", css_class="col-md-4"),
                    Div("purchase_date", css_class="col-md-4"),
                    css_class="row",
                ),
                Div(
                    Div("purchase_cost", css_class="col-md-4"),
                    Div("currency", css_class="col-md-4"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Funding"),
                Div(
                    Div("subscription", css_class="col-md-6"),
                    Div("cost_center", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Lifecycle"),
                Div(
                    Div("expiration_date", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Scope"),
                Div(
                    Div("tenant", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Notes & Tags"),
                "notes",
                "tags",
            ),
            *self.action_buttons(cancel_url),
        )
        self.append_custom_fields_to_layout()


class LicenseFilterForm(FilterForm):
    filterset_class = LicenseFilterSet


class LicenseSeatAssignmentForm(TenantScopedFormMixin, CrispyFormMixin, forms.ModelForm):
    """Assign a license seat to an asset (asset-scoped quick-add)."""

    class Meta:
        model = LicenseSeatAssignment
        fields = ["license", "asset", "notes"]
        widgets = {
            "license": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "asset": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Assign License")
        try:
            cancel_url = reverse("licenses:license_list")
        except NoReverseMatch:
            cancel_url = "/"

        self.helper.layout = Layout(
            "asset",
            "license",
            "notes",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            cancel_button(cancel_url, dismiss=self.quick_add_dismiss, no_dirty_track=True),
            HTML("</div>"),
        )

    def clean(self):
        cleaned = super().clean()
        lic = cleaned.get("license")
        asset = cleaned.get("asset")
        if lic and asset and not self.instance.pk:
            # Friendly pre-checks; the model's DB constraints enforce these too.
            if LicenseSeatAssignment.objects.for_scope(Scope.current()).filter(license=lic, asset=asset).exists():
                raise forms.ValidationError(_("This asset already holds a seat on this license."))
            if lic.available_seats <= 0:
                raise forms.ValidationError(_("No seats available on this license."))
        return cleaned


class LicenseCheckOutForm(TenantScopedFormMixin, forms.Form):
    TARGET_CHOICES = [
        ("holder", _("Asset Holder")),
        ("asset", _("Hardware Asset")),
    ]

    target_type = forms.ChoiceField(
        choices=TARGET_CHOICES, widget=forms.Select(attrs={"class": "form-select"}), label=_("Assign to")
    )
    assigned_holder = forms.ModelChoiceField(
        queryset=AssetHolder.objects.all().order_by("last_name", "first_name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Asset Holder"),
    )
    asset = forms.ModelChoiceField(
        queryset=Asset.issuable().order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Hardware Asset"),
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}), required=False, label=_("Notes")
    )

    def clean(self):
        cleaned_data = super().clean()
        target_type = cleaned_data.get("target_type")
        holder = cleaned_data.get("assigned_holder")
        asset = cleaned_data.get("asset")

        if target_type == "holder" and not holder:
            raise forms.ValidationError(_("Must select an Asset Holder."), code="holder_required")
        if target_type == "asset" and not asset:
            raise forms.ValidationError(_("Must select a Hardware Asset."), code="asset_required")
        if not target_type:
            raise forms.ValidationError(_("Must select a target type."), code="target_type_required")
        return cleaned_data

    def __init__(self, *args, **kwargs):
        license_obj = kwargs.pop("license", None)
        super().__init__(*args, **kwargs)

        # If tenant is restricted, filter candidates
        if license_obj and license_obj.tenant:
            self.fields["assigned_holder"].queryset = AssetHolder.objects.filter(tenant=license_obj.tenant).order_by(
                "last_name", "first_name"
            )
            self.fields["asset"].queryset = Asset.issuable().filter(tenant=license_obj.tenant).order_by("name")

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "target_type",
            "assigned_holder",
            "asset",
            "notes",
        )
