from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Fieldset, Layout, Row, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.forms import ColorFieldFormMixin, CrispyFormMixin, cancel_button
from core.forms.base import TenantScopedFormMixin

from ..models import AssetRole


class AssetRoleForm(TenantScopedFormMixin, ColorFieldFormMixin, CrispyFormMixin, forms.ModelForm):
    class Meta:
        model = AssetRole
        fields = ["name", "slug", "description", "color", "allows_components", "tags"]

    color = forms.CharField(
        max_length=7,
        required=False,
        widget=forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
        help_text=_("Choose a color for this Asset Role"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        cancel_url = reverse("assets:assetrole_list")
        self.helper.layout = Layout(
            Fieldset(
                "",
                Row(
                    Column("name", css_class="col-md-6"),
                    Column("slug", css_class="col-md-6"),
                ),
                "description",
                Row(
                    Column("color", css_class="col-md-4"),
                    Column("allows_components", css_class="col-md-4 mt-4"),
                ),
                "tags",
            ),
            Row(
                Column(Submit("submit", _("Save"), css_class="btn btn-primary"), css_class="col"),
                Column(
                    cancel_button(cancel_url, dismiss=self.quick_add_dismiss, no_dirty_track=True),
                    css_class="col text-end",
                ),
            ),
        )
