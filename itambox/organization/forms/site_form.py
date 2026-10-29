from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import FilterForm, TenantScopedFormMixin
from extras.models import Tag

from ..filters import SiteFilterSet
from ..models import Region, Site, SiteGroup, Tenant
from .helpers import add_standard_buttons


class SiteForm(TenantScopedFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    region = forms.ModelChoiceField(
        queryset=Region.objects.all(), required=False, widget=forms.Select(attrs={"class": "form-select"})
    )
    group = forms.ModelChoiceField(
        queryset=SiteGroup.objects.all(),
        required=False,
        label=_("Site Group"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    tenant = forms.ModelChoiceField(
        queryset=Site.objects.none(), required=False, widget=forms.Select(attrs={"class": "form-select"})
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    class Meta:
        model = Site
        fields = [
            "name",
            "slug",
            "status",
            "region",
            "group",
            "tenant",
            "facility",
            "time_zone",
            "description",
            "physical_address",
            "shipping_address",
            "latitude",
            "longitude",
            "comments",
            "tags",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "facility": forms.TextInput(attrs={"class": "form-control"}),
            "time_zone": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.TextInput(attrs={"class": "form-control"}),
            "physical_address": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "shipping_address": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "latitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001"}),
            "longitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001"}),
            "comments": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
        }
        help_texts = {
            "slug": _("URL-friendly identifier."),
            "latitude": _("GPS coordinate (decimal format xx.yyyyyy)"),
            "longitude": _("GPS coordinate (decimal format xx.yyyyyy)"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        self.fields["tenant"].queryset = Tenant.objects.all()

        self.helper.layout = Layout(
            "name",
            "slug",
            "status",
            Row(
                Column("region", css_class="form-group col-md-4 mb-0"),
                Column("group", css_class="form-group col-md-4 mb-0"),
                Column("tenant", css_class="form-group col-md-4 mb-0"),
                css_class="mb-3",
            ),
            Row(
                Column("facility", css_class="form-group col-md-6 mb-0"),
                Column("time_zone", css_class="form-group col-md-6 mb-0"),
                css_class="mb-3",
            ),
            "description",
            "physical_address",
            "shipping_address",
            Row(
                Column("latitude", css_class="form-group col-md-6 mb-0"),
                Column("longitude", css_class="form-group col-md-6 mb-0"),
                css_class="mb-3",
            ),
            "comments",
            "tags",
        )

        add_standard_buttons(self.helper, self.instance, "organization:site_list")


class SiteFilterForm(FilterForm):
    filterset_class = SiteFilterSet
