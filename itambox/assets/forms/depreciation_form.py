from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Field, Layout, Row, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.forms.base import TenantScopedFormMixin

from ..models import Depreciation


class DepreciationForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Depreciation
        fields = ["name", "months", "method", "convention", "immediate_expense_threshold", "description"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "months": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "method": forms.Select(attrs={"class": "form-select"}),
            "convention": forms.Select(attrs={"class": "form-select"}),
            "immediate_expense_threshold": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("assets:depreciation_list")

        self.helper.layout = Layout(
            "name",
            Row(
                Column("months", css_class="col-md-6"),
                Column("method", css_class="col-md-6"),
            ),
            Row(
                Column("convention", css_class="col-md-6"),
                Column("immediate_expense_threshold", css_class="col-md-6"),
            ),
            Field("description"),
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML('<a href="%s" class="btn btn-outline-secondary ms-2">Cancel</a>' % cancel_url),
            HTML("</div>"),
        )
