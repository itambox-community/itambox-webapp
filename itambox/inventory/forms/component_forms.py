from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Layout, Row, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, Category, Manufacturer, Supplier
from core.forms import CrispyFormMixin, FilterForm, SlugModelForm, TenantScopedFormMixin, cancel_button
from core.managers import Scope
from extras.customfields import CustomFieldModelFormMixin
from extras.models import Tag
from organization.models import AssetHolder, Location

from ..filters import ComponentAllocationFilterSet, ComponentFilterSet, ComponentStockFilterSet
from ..models import Component, ComponentAllocation, ComponentStock
from ..models_assignment_write import authorized_assignment_validation
from .base_forms import BaseCheckoutForm


class ComponentForm(TenantScopedFormMixin, CustomFieldModelFormMixin, SlugModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    manufacturer = forms.ModelChoiceField(
        queryset=Manufacturer.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.filter(applies_to__component=True),
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
        model = Component
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
        cancel_url = (
            self.instance.get_absolute_url()
            if self.instance.pk
            else reverse("inventory:inventory_list") + "?type=components"
        )

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


class ComponentStockForm(TenantScopedFormMixin, forms.ModelForm):
    component = forms.ModelChoiceField(
        queryset=Component.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.all().select_related("site"), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = ComponentStock
        fields = ["component", "location", "qty"]
        widgets = {
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
        cancel_url = reverse("inventory:inventory_list") + "?type=components"

        self.helper.layout = Layout(
            Row(Column("component", css_class="col-md-6"), Column("location", css_class="col-md-6")),
            "qty",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(format_html('<a href="{}" class="btn btn-outline-secondary ms-2">{}</a>', cancel_url, _("Cancel"))),
            HTML("</div>"),
        )


class ComponentAllocationForm(TenantScopedFormMixin, CrispyFormMixin, forms.ModelForm):
    # from_location is rebuilt below from the including-deleted manager (the
    # recorded source of a historical allocation), so it stays non-scoped.
    tenant_scoped_choice_exclusions = ("from_location",)

    component = forms.ModelChoiceField(
        queryset=Component.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    assigned_holder = forms.ModelChoiceField(
        queryset=AssetHolder.objects.all().order_by("last_name", "first_name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Assign to Asset Holder"),
    )
    assigned_location = forms.ModelChoiceField(
        queryset=Location.objects.all().select_related("site").order_by("site__name", "name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Assign to Location"),
    )
    assigned_asset = forms.ModelChoiceField(
        queryset=Asset.objects.all().order_by("asset_tag"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Assign to Asset"),
    )
    from_location = forms.ModelChoiceField(
        queryset=Location.objects.all().select_related("site").order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("From Location"),
    )
    qty = forms.IntegerField(
        initial=1, min_value=1, widget=forms.NumberInput(attrs={"class": "form-control"}), label=_("Quantity")
    )
    notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}), label=_("Notes")
    )

    class Meta:
        model = ComponentAllocation
        fields = [
            "component",
            "assigned_holder",
            "assigned_location",
            "assigned_asset",
            "from_location",
            "qty",
            "notes",
        ]

    def _post_clean(self):
        # Validation exercises model invariants before the view delegates the
        # actual write to create_component_allocation(). The permit ends here,
        # so direct form.save() remains fail-closed.
        with authorized_assignment_validation():
            super()._post_clean()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        is_update = self.instance.pk is not None
        source_key = self.add_prefix("from_location")
        source_values = self.data.getlist(source_key) if hasattr(self.data, "getlist") else [self.data.get(source_key)]
        self._submitted_create_source = bool(
            not is_update and self.is_bound and any(value not in (None, "") for value in source_values)
        )
        if not is_update:
            self.fields.pop("from_location")
        else:
            for field_name in (
                "component",
                "assigned_holder",
                "assigned_location",
                "assigned_asset",
                "from_location",
            ):
                self.fields[field_name].disabled = True

        # Tenant-owned FK choices are scoped by TenantScopedFormMixin; only the
        # ordering / select_related shaping stays here.
        self.fields["assigned_holder"].queryset = AssetHolder.objects.all().order_by("last_name", "first_name")
        self.fields["assigned_location"].queryset = (
            Location.objects.all().select_related("site").order_by("site__name", "name")
        )
        self.fields["assigned_asset"].queryset = Asset.objects.for_scope(Scope.current()).all().order_by("asset_tag")
        if is_update:
            # unscoped: an existing allocation must still render its source location after the location was
            # soft-deleted
            self.fields["from_location"].queryset = (
                Location.all_objects.filter(pk=self.instance.from_location_id)
                if self.instance.from_location_id is not None
                else Location.objects.none()
            )
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("inventory:inventory_list") + "?type=components"

        self.helper.layout = Layout(
            Row(Column("component", css_class="col-md-6"), Column("from_location", css_class="col-md-6")),
            Row(
                Column("assigned_holder", css_class="col-md-4"),
                Column("assigned_location", css_class="col-md-4"),
                Column("assigned_asset", css_class="col-md-4"),
            ),
            Row(
                Column("qty", css_class="col-md-6"),
            ),
            "notes",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            cancel_button(cancel_url, dismiss=self.quick_add_dismiss),
            HTML("</div>"),
        )
        if not is_update:
            self.helper.layout.fields[0] = Row(Column("component", css_class="col-md-12"))

    def _has_immutable_update_error(self):
        if self.instance.pk is None or not self.is_bound:
            return False
        immutable_values = {
            "component": self.instance.component_id,
            "assigned_holder": self.instance.assigned_holder_id,
            "assigned_location": self.instance.assigned_location_id,
            "assigned_asset": self.instance.assigned_asset_id,
            "from_location": self.instance.from_location_id,
        }
        immutable_error = False
        for field_name, persisted_id in immutable_values.items():
            field_key = self.add_prefix(field_name)
            raw_values = self.data.getlist(field_key) if hasattr(self.data, "getlist") else [self.data.get(field_key)]
            raw_values = [value for value in raw_values if value is not None]
            if not raw_values:
                continue
            expected_value = "" if persisted_id is None else str(persisted_id)
            if any(str(value) != expected_value for value in raw_values):
                self.add_error(field_name, _("Component allocation item, source, and destination are immutable."))
                immutable_error = True
        return immutable_error

    def clean(self):
        cleaned_data = super().clean()
        if self._submitted_create_source:
            raise ValidationError(_("Source location is only available through component checkout."))
        if self._has_immutable_update_error():
            return cleaned_data

        holder = cleaned_data.get("assigned_holder")
        location = cleaned_data.get("assigned_location")
        asset = cleaned_data.get("assigned_asset")
        qty = cleaned_data.get("qty")
        component = cleaned_data.get("component")

        filled = [t for t in [holder, location, asset] if t is not None]
        if len(filled) == 0:
            raise ValidationError(_("You must select either an Asset Holder, a Location, or an Asset."))
        if len(filled) > 1:
            raise ValidationError(_("Please select exactly one target (either Asset Holder, Location, OR Asset)."))

        if component and qty:
            remaining = component.available_stock
            if not component.allow_overallocate and qty > remaining:
                raise ValidationError(
                    _("Cannot checkout %(qty)s units. Only %(remaining)s units are currently in stock.")
                    % {"qty": qty, "remaining": remaining}
                )
        return cleaned_data


class ComponentCheckoutForm(BaseCheckoutForm):
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
        self.component = kwargs.pop("component", None)
        tenant = self.component.tenant if self.component else None
        super().__init__(*args, tenant=tenant, item=self.component, stock_model=ComponentStock, **kwargs)
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
        if self.component and qty and source_location:
            stock_qty = (
                ComponentStock.objects.filter(
                    component=self.component,
                    location=source_location,
                )
                .values_list("qty", flat=True)
                .first()
                or 0
            )
            if not self.component.allow_overallocate and qty > stock_qty:
                raise ValidationError(
                    _("Cannot checkout %(qty)s units. Only %(remaining)s units are currently in stock.")
                    % {"qty": qty, "remaining": stock_qty}
                )
        return cleaned_data


class ComponentFilterForm(FilterForm):
    filterset_class = ComponentFilterSet


class ComponentStockFilterForm(FilterForm):
    filterset_class = ComponentStockFilterSet


class ComponentAllocationFilterForm(FilterForm):
    filterset_class = ComponentAllocationFilterSet


class ComponentStockModalForm(TenantScopedFormMixin, forms.ModelForm):
    qty = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )

    class Meta:
        model = ComponentStock
        fields = ["location", "qty"]
        widgets = {
            "location": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = Location.objects.all().select_related("site")
        self.helper = FormHelper()
        self.helper.form_tag = False
