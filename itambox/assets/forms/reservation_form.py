from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Layout, Submit
from django import forms
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from assets.models import AssetReservation
from core.forms import CrispyFormMixin, TenantScopedFormMixin, cancel_button


class AssetReservationForm(TenantScopedFormMixin, CrispyFormMixin, forms.ModelForm):
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        required=True,
    )
    end_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        required=True,
    )

    class Meta:
        model = AssetReservation
        fields = [
            "asset",
            "reserved_for",
            "start_date",
            "end_date",
            "status",
            "purpose",
            "notes",
        ]
        widgets = {
            "asset": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "reserved_for": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "purpose": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance and self.instance.pk else _("Create Reservation")
        try:
            cancel_url = reverse("assets:assetreservation_list")
        except NoReverseMatch:
            cancel_url = "/"

        self.helper.layout = Layout(
            Div(
                Div("asset", css_class="col-md-6"),
                Div("reserved_for", css_class="col-md-6"),
                css_class="row",
            ),
            Div(
                Div("start_date", css_class="col-md-4"),
                Div("end_date", css_class="col-md-4"),
                Div("status", css_class="col-md-4"),
                css_class="row",
            ),
            "purpose",
            "notes",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            cancel_button(cancel_url, dismiss=self.quick_add_dismiss),
            HTML("</div>"),
        )

    def _get_validation_exclusions(self):
        """Keep soft-delete state available to the reservation exclusion constraint."""
        exclusions = super()._get_validation_exclusions()
        exclusions.discard("deleted_at")
        return exclusions
