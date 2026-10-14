from crispy_forms.helper import FormHelper
from crispy_forms.layout import Div, Layout
from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import CrispyFormMixin, FilterForm, TenantScopedFormMixin
from extras.customfields import CustomFieldModelFormMixin
from extras.models import Tag

from ..filters import LocationFilterSet
from ..models import Location, Site, Tenant
from .helpers import add_standard_buttons


class LocationForm(TenantScopedFormMixin, CrispyFormMixin, CustomFieldModelFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    site = forms.ModelChoiceField(
        queryset=Site.objects.all(), required=True, widget=forms.Select(attrs={"class": "form-select"})
    )
    parent = forms.ModelChoiceField(
        queryset=Location.objects.all(), required=False, widget=forms.Select(attrs={"class": "form-select"})
    )
    tenant = forms.ModelChoiceField(
        queryset=Tenant.objects.all(), required=False, widget=forms.Select(attrs={"class": "form-select"})
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
    )

    class Meta:
        model = Location
        fields = ["site", "name", "slug", "status", "parent", "tenant", "facility", "description", "tags"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "facility": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        help_texts = {"slug": _("URL-friendly identifier."), "facility": _("Building, Floor, Room, Rack, etc.")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Rescope tenant-owned FK querysets per request (import-frozen unscoped):
        # `site` and the self-referential `parent` are both tenant-scoped.
        self.fields["site"].queryset = Site.objects.all()
        self.fields["parent"].queryset = Location.objects.all()
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(Div("site", css_class="col-md-6"), Div("name", css_class="col-md-6"), css_class="row"),
            Div(
                Div("slug", css_class="col-md-4"),
                Div("status", css_class="col-md-4"),
                Div("parent", css_class="col-md-4"),
                css_class="row",
            ),
            Div(Div("tenant", css_class="col-md-6"), Div("facility", css_class="col-md-6"), css_class="row"),
            "description",
            "tags",
        )
        self.append_custom_fields_to_layout()

        add_standard_buttons(self.helper, self.instance, "organization:location_list", dismiss=self.quick_add_dismiss)

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and self.instance and self.instance.pk:
            if parent.pk == self.instance.pk:
                raise forms.ValidationError(_("A location cannot be its own parent."))
        return parent


class LocationFilterForm(FilterForm):
    filterset_class = LocationFilterSet
