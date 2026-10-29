from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Fieldset, Layout, Row, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models import StatusLabel
from compliance.models import AssetAudit, AuditSession
from core.forms import TenantScopedFormMixin, apply_tenant_scoped_choices
from organization.models import Location, Membership, Tenant


class AssetAuditForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = AssetAudit
        fields = ["location", "status", "notes", "verification_method"]
        widgets = {
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "verification_method": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        asset = kwargs.pop("asset", None)
        super().__init__(*args, **kwargs)
        self.fields["location"] = forms.ModelChoiceField(
            queryset=Location.objects.all(),
            required=True,
            widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        )
        self.fields["status"] = forms.ModelChoiceField(
            queryset=StatusLabel.objects.exclude(type=StatusLabel.TYPE_ARCHIVED),
            required=True,
            widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        )

        if asset:
            self.fields["location"].initial = asset.location
            self.fields["status"].initial = asset.status

        # The fields above are replaced after the mixin ran; scope them explicitly.
        apply_tenant_scoped_choices(self)


class AuditSessionForm(TenantScopedFormMixin, forms.ModelForm):
    # ``tenant`` is optional here (blank = global MSP-wide audit).
    tenant_required = False
    tenant_autoset_when_single = False

    start_immediately = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Start immediately"),
        help_text=_("Uncheck to save as a planned campaign and activate later."),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = AuditSession
        fields = ["name", "tenant", "location"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        # Restrict tenant choices to the current user's memberships.
        if request and request.user.is_authenticated and not request.user.is_superuser:
            member_tenant_ids = Membership.objects.filter(user=request.user, is_active=True).values_list(
                "tenant_id", flat=True
            )
            self.fields["tenant"] = forms.ModelChoiceField(
                queryset=Tenant.objects.filter(pk__in=member_tenant_ids),
                required=False,
                label=_("Tenant (Optional)"),
                help_text=_("Scope this campaign to a single tenant. Leave blank for a global MSP-wide audit."),
                widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            )
        else:
            self.fields["tenant"] = forms.ModelChoiceField(
                queryset=Tenant.objects.all(),
                required=False,
                label=_("Tenant (Optional)"),
                help_text=_("Scope this campaign to a single tenant. Leave blank for a global MSP-wide audit."),
                widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            )

        self.fields["location"] = forms.ModelChoiceField(
            queryset=Location.objects.all(),
            required=False,
            label=_("Target Location (Optional)"),
            help_text=_("Expected location to audit. Leave blank to audit globally."),
            widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        )

        # Tenant and location were redeclared above; scope the replacements.
        apply_tenant_scoped_choices(self)

        cancel_url = reverse("compliance:auditsession_list")
        button_text = _("Update") if self.instance and self.instance.pk else _("Create")

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Row(
                Column("tenant", css_class="col-md-6"),
                Column("name", css_class="col-md-6"),
                css_class="row g-3",
            ),
            "location",
            Fieldset(
                _("Campaign Options"),
                "start_immediately",
            ),
            HTML('<div class="mt-4"></div>'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(
                f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2"'
                f' data-no-dirty-track="true">{_("Cancel")}</a>'
            ),
        )

    def clean(self):
        cleaned = super().clean()
        tenant = cleaned.get("tenant")
        location = cleaned.get("location")
        if tenant and location and location.tenant_id and location.tenant_id != tenant.pk:
            self.add_error(
                "location",
                _("The selected location does not belong to the chosen tenant."),
            )
        if self.instance.pk and self.instance.status == "completed":
            immutable_changes = {
                field
                for field in ("name", "tenant", "location")
                if field in cleaned and cleaned[field] != getattr(self.instance, field)
            }
            if immutable_changes:
                self.add_error(None, _("Completed audit sessions are immutable."))
        return cleaned


class AuditBarcodeScanForm(TenantScopedFormMixin, forms.Form):
    barcode = forms.CharField(
        label=_("Scan Asset Tag or Serial Number"),
        widget=forms.TextInput(
            attrs={
                "placeholder": "Scan serial or tag...",
                "autofocus": "autofocus",
                "class": "form-control",
                "id": "barcode-scan-input",
            }
        ),
    )
