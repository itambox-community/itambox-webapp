from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Fieldset, Layout, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models import Supplier, Warranty
from core.forms import CrispyFormMixin, TenantScopedFormMixin, cancel_button
from core.managers import Scope


class WarrantyForm(TenantScopedFormMixin, CrispyFormMixin, forms.ModelForm):
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        required=True,
    )
    end_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        required=True,
    )

    class Meta:
        model = Warranty
        fields = [
            "asset",
            "warranty_type",
            "supplier",
            "start_date",
            "end_date",
            "cost",
            "currency",
            "reference",
            "terms",
            "notes",
        ]
        widgets = {
            "asset": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "warranty_type": forms.Select(attrs={"class": "form-select"}),
            "supplier": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "currency": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "reference": forms.TextInput(attrs={"class": "form-control"}),
            "terms": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance and self.instance.pk else _("Add Warranty")
        try:
            cancel_url = reverse("assets:warranty_list")
        except Exception:
            cancel_url = "/"

        self.helper.layout = Layout(
            Fieldset(
                _("Warranty Details"),
                Div(
                    Div("asset", css_class="col-md-6"),
                    Div("warranty_type", css_class="col-md-6"),
                    css_class="row",
                ),
                Div(
                    Div("supplier", css_class="col-md-6"),
                    Div("reference", css_class="col-md-6"),
                    css_class="row",
                ),
                Div(
                    Div("start_date", css_class="col-md-4"),
                    Div("end_date", css_class="col-md-4"),
                    css_class="row",
                ),
                "terms",
            ),
            Fieldset(
                _("Financial"),
                Div(
                    Div("cost", css_class="col-md-4"),
                    Div("currency", css_class="col-md-4"),
                    css_class="row",
                ),
            ),
            "notes",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            cancel_button(cancel_url, dismiss=self.quick_add_dismiss),
            HTML("</div>"),
        )
