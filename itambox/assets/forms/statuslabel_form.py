from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Fieldset, Layout, Row, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.forms import ColorFieldFormMixin
from core.forms.base import TenantScopedFormMixin
from extras.models import Tag

from ..models import StatusLabel


class StatusLabelForm(TenantScopedFormMixin, ColorFieldFormMixin, forms.ModelForm):
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tomselect-tags": "true"}),
    )

    class Meta:
        model = StatusLabel
        fields = ["name", "slug", "type", "description", "color", "tags"]

    color = forms.CharField(
        max_length=7,
        required=False,
        widget=forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
        help_text=_("Choose a color for this Status Label"),
    )

    type = forms.ChoiceField(choices=StatusLabel.TYPE_CHOICES, widget=forms.Select(attrs={"class": "form-select"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        cancel_url = reverse("assets:statuslabel_list")
        self.helper.layout = Layout(
            Fieldset(
                "",
                Row(
                    Column("name", css_class="col-md-6"),
                    Column("slug", css_class="col-md-6"),
                ),
                "type",
                "description",
                "color",
                "tags",
            ),
            Row(
                Column(Submit("submit", _("Save"), css_class="btn btn-primary"), css_class="col"),
                Column(
                    HTML(
                        '<a href="%s" class="btn btn-outline-secondary ms-2" '
                        'data-no-dirty-track="true">Cancel</a>' % cancel_url
                    ),
                    css_class="col text-end",
                ),
            ),
        )
        self.fields["slug"].widget.attrs["slugify"] = "name"
