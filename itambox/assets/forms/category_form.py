from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Layout, Row, Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.forms import ColorFieldFormMixin, SlugModelForm

from ..models import Category


class CategoryForm(ColorFieldFormMixin, SlugModelForm):
    APPLIES_TO_CHOICES = [
        ("asset", "Assets (Hardware)"),
        ("accessory", "Accessories"),
        ("consumable", "Consumables"),
        ("component", "Modular Components"),
    ]

    applies_to_flags = forms.MultipleChoiceField(
        choices=APPLIES_TO_CHOICES,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        required=False,
        label=_("Applies to"),
    )

    color = forms.CharField(
        max_length=7,
        required=False,
        widget=forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
        label=_("Category Color"),
        help_text=_("Choose a color for this Category"),
    )

    class Meta:
        model = Category
        fields = ["name", "slug", "color", "description", "applies_to_flags", "audit_interval_months", "tags"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control", "slugify": "name"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "audit_interval_months": forms.NumberInput(
                attrs={"class": "form-control", "min": "1", "placeholder": "e.g. 12"}
            ),
            "tags": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            applies_to = self.instance.applies_to or {}
            self.initial["applies_to_flags"] = [k for k, v in applies_to.items() if v]

        # Initialize premium FormHelper layout

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance and self.instance.pk else _("Create")
        cancel_url = reverse("assets:category_list")

        self.helper.layout = Layout(
            Row(Column("name", css_class="col-md-6"), Column("slug", css_class="col-md-6")),
            Row(Column("color", css_class="col-md-6"), Column("tags", css_class="col-md-6")),
            "description",
            "applies_to_flags",
            "audit_interval_months",
            HTML('<div class="mt-4"></div>'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2">Cancel</a>'),
        )

    def clean(self):
        cleaned_data = super().clean()
        flags = cleaned_data.get("applies_to_flags", [])
        cleaned_data["applies_to"] = {choice: (choice in flags) for choice, _label in self.APPLIES_TO_CHOICES}
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.applies_to = self.cleaned_data["applies_to"]
        if commit:
            instance.save()
            self.save_m2m()
        return instance
