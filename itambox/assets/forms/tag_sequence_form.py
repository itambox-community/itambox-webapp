from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Layout, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.forms import TenantScopedFormMixin

from ..models import AssetTagSequence


class AssetTagSequenceForm(TenantScopedFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    class Meta:
        model = AssetTagSequence
        fields = ["prefix", "next_value", "zero_padding", "tenant", "category", "is_active"]
        widgets = {
            "prefix": forms.TextInput(attrs={"class": "form-control"}),
            "next_value": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "zero_padding": forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 20}),
            "tenant": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "category": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("assets:assettagsequence_list")

        self.helper.layout = Layout(
            Div(
                Div("prefix", css_class="col-md-6"),
                Div("next_value", css_class="col-md-3"),
                Div("zero_padding", css_class="col-md-3"),
                css_class="row",
            ),
            Div(
                Div("tenant", css_class="col-md-6"),
                Div("category", css_class="col-md-6"),
                css_class="row",
            ),
            "is_active",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML('<a href="%s" class="btn btn-outline-secondary ms-2">Cancel</a>' % cancel_url),
            HTML("</div>"),
        )
