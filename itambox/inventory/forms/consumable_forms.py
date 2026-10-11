from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Layout, Row, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from assets.models import Category, Manufacturer, Supplier
from core.forms import FilterForm, SlugModelForm, TenantScopedFormMixin
from core.managers import Scope
from extras.customfields import CustomFieldModelFormMixin
from extras.models import Tag
from organization.models import Location

from ..filters import ConsumableAssignmentFilterSet, ConsumableFilterSet, ConsumableStockFilterSet
from ..models import Consumable, ConsumableStock
from .base_forms import BaseCheckoutForm


class ConsumableForm(TenantScopedFormMixin, CustomFieldModelFormMixin, SlugModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    manufacturer = forms.ModelChoiceField(
        queryset=Manufacturer.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.filter(applies_to__consumable=True),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Category"),
    )
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.filter(is_active=True),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Supplier"),
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tomselect-tags": "true"}),
        label=_("Tags"),
    )

    class Meta:
        model = Consumable
        fields = [
            "manufacturer",
            "name",
            "slug",
            "category",
            "supplier",
            "part_number",
            "ean",
            "min_qty",
            "allow_overallocate",
            "notes",
            "tags",
            "tenant",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control", "slugify": "name"}),
            "part_number": forms.TextInput(attrs={"class": "form-control"}),
            "ean": forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric"}),
            "min_qty": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "allow_overallocate": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        help_texts = {
            "ean": _("Barcode (EAN, UPC, or GTIN). Scan it to open this item."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.fields["slug"].widget.attrs["slugify"] = "name"

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = self.instance.get_absolute_url() if self.instance.pk else reverse("inventory:consumable_list")

        self.helper.layout = Layout(
            Row(Column("manufacturer", css_class="col-md-6"), Column("name", css_class="col-md-6")),
            Row(
                Column("supplier", css_class="col-md-4"),
                Column("part_number", css_class="col-md-4"),
                Column("ean", css_class="col-md-4"),
            ),
            Row(Column("slug", css_class="col-md-6"), Column("category", css_class="col-md-6")),
            Row(Column("min_qty", css_class="col-md-6"), Column("tenant", css_class="col-md-6")),
            Row(Column("allow_overallocate", css_class="col-md-6")),
            Row(Column("tags", css_class="col-md-12")),
            "notes",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(format_html('<a href="{}" class="btn btn-outline-secondary ms-2">{}</a>', cancel_url, _("Cancel"))),
            HTML("</div>"),
        )
        self.append_custom_fields_to_layout()

    def clean(self):
        """Report an active-row duplicate as a form error (#767).

        The catalogue uniqueness is a conditional constraint
        (``deleted_at IS NULL``), and Django skips constraint validation when
        the condition references a field the form does not carry
        (``deleted_at`` is not editable). Without this check the INSERT fails
        with an ``IntegrityError`` and the request answers 500.
        """
        cleaned_data = super().clean()
        manufacturer = cleaned_data.get("manufacturer")
        name = cleaned_data.get("name")
        if manufacturer and name and "tenant" in cleaned_data:
            duplicates = Consumable.objects.filter(
                manufacturer=manufacturer, name=name, tenant=cleaned_data["tenant"]
            )
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise ValidationError(
                    {"name": _("A consumable with this manufacturer and name already exists for this tenant.")}
                )
        return cleaned_data


class ConsumableStockForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = ConsumableStock
        fields = ["consumable", "location", "qty"]
        widgets = {
            "consumable": forms.Select(attrs={"class": "form-select"}),
            "location": forms.Select(attrs={"class": "form-select"}),
            "qty": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Tenant-owned FK choices are scoped by TenantScopedFormMixin; only the
        # select_related optimisation stays here.
        self.fields["location"].queryset = Location.objects.all().select_related("site")
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        button_text = _("Update") if self.instance.pk else _("Create")
        self.helper.layout = Layout(
            "consumable",
            "location",
            "qty",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML("</div>"),
        )


class ConsumableCheckoutForm(BaseCheckoutForm):
    qty = forms.IntegerField(
        initial=1, min_value=1, widget=forms.NumberInput(attrs={"class": "form-control"}), label=_("Quantity")
    )
    from_location = forms.ModelChoiceField(
        queryset=Location.objects.all().order_by("name"),
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("From Location"),
    )

    def __init__(self, *args, **kwargs):
        self.consumable = kwargs.pop("consumable", None)
        tenant = self.consumable.tenant if self.consumable else None
        super().__init__(*args, tenant=tenant, item=self.consumable, stock_model=ConsumableStock, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "from_location",
            "assigned_holder",
            HTML('<p class="text-muted text-center my-2">OR</p>'),
            "assigned_location",
            HTML('<p class="text-muted text-center my-2">OR</p>'),
            "assigned_asset",
            "qty",
            "notes",
        )

    def clean(self):
        cleaned_data = super().clean()
        qty = cleaned_data.get("qty")
        source_location = cleaned_data.get("from_location")
        if self.consumable and qty and source_location:
            stock_qty = (
                ConsumableStock.objects.filter(
                    consumable=self.consumable,
                    location=source_location,
                )
                .values_list("qty", flat=True)
                .first()
                or 0
            )
            if not self.consumable.allow_overallocate and qty > stock_qty:
                raise ValidationError(
                    _("Cannot checkout %(qty)s units. Only %(remaining)s units are currently in stock.")
                    % {"qty": qty, "remaining": stock_qty}
                )
        return cleaned_data


class ConsumableFilterForm(FilterForm):
    filterset_class = ConsumableFilterSet


class ConsumableStockFilterForm(FilterForm):
    filterset_class = ConsumableStockFilterSet


class ConsumableAssignmentFilterForm(FilterForm):
    filterset_class = ConsumableAssignmentFilterSet


class ConsumableStockModalForm(TenantScopedFormMixin, forms.ModelForm):
    qty = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )

    class Meta:
        model = ConsumableStock
        fields = ["location", "qty"]
        widgets = {
            "location": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = Location.objects.all().select_related("site")
        self.helper = FormHelper()
        self.helper.form_tag = False
