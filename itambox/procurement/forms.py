from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Layout, Row, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, Supplier
from core.forms import FilterForm, TenantScopedFormMixin
from core.managers import Scope, get_current_tenant

from .filters import ContractFilterSet, PurchaseOrderFilterSet
from .models import Contract, PurchaseOrder, PurchaseOrderLine

# Contract and Subscription forms share one exclusive ownership mapping: the same
# text ships on the Subscription type field, so one agreement is recorded in one
# module only. Kept as one constant so the Contract form cannot drift.
AGREEMENT_OWNERSHIP_HELP = _(
    "Record one agreement in one module only. SaaS and cloud entitlement: record as Subscription. "
    "Support, maintenance, lease, warranty, SLA, or asset-covered service: record as Contract. "
    "Other recurring entitlement without asset/SLA coverage: record as Subscription. "
    "Other legal/commercial agreement: record as Contract."
)


class PurchaseOrderFilterForm(FilterForm):
    filterset_class = PurchaseOrderFilterSet


class ContractFilterForm(FilterForm):
    filterset_class = ContractFilterSet


class PurchaseOrderForm(TenantScopedFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    class Meta:
        model = PurchaseOrder
        fields = [
            "order_number",
            "supplier",
            "currency",
            "order_date",
            "expected_delivery_date",
            "destination_location",
            "tenant",
            "notes",
        ]
        widgets = {
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "expected_delivery_date": forms.DateInput(attrs={"type": "date"}),
            "supplier": forms.Select(attrs={"data-tom-select": ""}),
            "destination_location": forms.Select(attrs={"data-tom-select": ""}),
            "tenant": forms.Select(attrs={"data-tom-select": ""}),
            "currency": forms.Select(attrs={"data-tom-select": ""}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column("order_number", css_class="col-md-12"),
                css_class="row g-3",
            ),
            Row(
                Column("supplier", css_class="col-md-6"), Column("currency", css_class="col-md-6"), css_class="row g-3"
            ),
            Row(Column("tenant", css_class="col-md-12"), css_class="row g-3"),
            Row(
                Column("order_date", css_class="col-md-6"),
                Column("expected_delivery_date", css_class="col-md-6"),
                css_class="row g-3",
            ),
            Row(Column("destination_location", css_class="col-md-6"), css_class="row g-3"),
            "notes",
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Purchase Order"), css_class="btn btn-primary"),
            HTML(
                "<a href=\"{% url 'procurement:purchaseorder_list' %}\" "
                'class="btn btn-outline-secondary ms-2" data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        )


class PurchaseOrderLineForm(TenantScopedFormMixin, forms.ModelForm):
    item_category = forms.ChoiceField(
        choices=[
            ("", "---------"),
            ("asset_type", _("Asset Type")),
            ("component", _("Component")),
            ("accessory", _("Accessory")),
            ("consumable", _("Consumable")),
            ("license", _("License")),
        ],
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "id": "id_request_category"}),
        label=_("Item Category"),
    )

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "item_category",
            "asset_type",
            "component",
            "accessory",
            "consumable",
            "license",
            "qty_ordered",
            "unit_price",
        ]
        widgets = {
            "asset_type": forms.Select(attrs={"data-tom-select": ""}),
            "component": forms.Select(attrs={"data-tom-select": ""}),
            "accessory": forms.Select(attrs={"data-tom-select": ""}),
            "consumable": forms.Select(attrs={"data-tom-select": ""}),
            "license": forms.Select(attrs={"data-tom-select": ""}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Populate initial value for item_category if editing
        if self.instance and self.instance.pk:
            if self.instance.asset_type:
                self.fields["item_category"].initial = "asset_type"
            elif self.instance.component:
                self.fields["item_category"].initial = "component"
            elif self.instance.accessory:
                self.fields["item_category"].initial = "accessory"
            elif self.instance.consumable:
                self.fields["item_category"].initial = "consumable"
            elif self.instance.license:
                self.fields["item_category"].initial = "license"

        # Scoping querysets by tenant

        tenant = get_current_tenant()
        if tenant:
            self.fields["component"].queryset = self.fields["component"].queryset.filter(tenant=tenant)
            self.fields["accessory"].queryset = self.fields["accessory"].queryset.filter(tenant=tenant)
            self.fields["consumable"].queryset = self.fields["consumable"].queryset.filter(tenant=tenant)
            self.fields["license"].queryset = self.fields["license"].queryset.filter(tenant=tenant)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "item_category",
            "asset_type",
            "component",
            "accessory",
            "consumable",
            "license",
            Row(
                Column("qty_ordered", css_class="col-md-6"),
                Column("unit_price", css_class="col-md-6"),
                css_class="row g-3",
            ),
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Line Item"), css_class="btn btn-primary"),
            HTML(
                '<a href="javascript:history.back()" class="btn btn-outline-secondary ms-2" data-no-dirty-track="true">'
                + str(_("Cancel"))
                + "</a>"
            ),
        )

    def clean(self):
        cleaned_data = super().clean()
        item_category = cleaned_data.get("item_category")

        if not item_category:
            raise ValidationError(_("Please select an Item Category."))

        fields_map = {
            "asset_type": "asset_type",
            "component": "component",
            "accessory": "accessory",
            "consumable": "consumable",
            "license": "license",
        }

        target_field = fields_map.get(item_category)
        if not target_field:
            raise ValidationError(_("Invalid category selected: %(category)s") % {"category": item_category})

        if not cleaned_data.get(target_field):
            field_label = self.fields[target_field].label or target_field.replace("_", " ").title()
            raise ValidationError({target_field: _("Please select a %(label)s.") % {"label": field_label}})

        # Clear all other fields to prevent multiple fields from being saved
        for _cat, field_name in fields_map.items():
            if field_name != target_field:
                cleaned_data[field_name] = None

        return cleaned_data


class ContractForm(TenantScopedFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    class Meta:
        model = Contract
        fields = [
            "name",
            "contract_number",
            "contract_type",
            "status",
            "supplier",
            "cost",
            "currency",
            "billing_cycle",
            "start_date",
            "end_date",
            "renewal_date",
            "auto_renew",
            "sla_response_time",
            "sla_resolution_time",
            "coverage_hours",
            "sla_terms",
            "assets",
            "purchase_order",
            "cost_center",
            "tenant",
            "notes",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "renewal_date": forms.DateInput(attrs={"type": "date"}),
            "supplier": forms.Select(attrs={"data-tom-select": ""}),
            "purchase_order": forms.Select(attrs={"data-tom-select": ""}),
            "cost_center": forms.Select(attrs={"data-tom-select": ""}),
            "tenant": forms.Select(attrs={"data-tom-select": ""}),
            "currency": forms.Select(attrs={"data-tom-select": ""}),
            "contract_type": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "billing_cycle": forms.Select(attrs={"class": "form-select"}),
            "assets": forms.SelectMultiple(attrs={"data-tom-select": ""}),
        }
        help_texts = {
            "contract_type": AGREEMENT_OWNERSHIP_HELP,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.fields["cost_center"].label_from_instance = lambda cost_center: (
            f"{cost_center.code}: {cost_center.name}" if cost_center.code else cost_center.name
        )

        # Scope tenant-aware querysets to the active tenant

        tenant = get_current_tenant()
        if tenant:
            self.fields["assets"].queryset = self.fields["assets"].queryset.filter(tenant=tenant)
            self.fields["cost_center"].queryset = self.fields["cost_center"].queryset.filter(tenant=tenant)
            self.fields["purchase_order"].queryset = self.fields["purchase_order"].queryset.filter(tenant=tenant)

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-md-8"),
                Column("contract_number", css_class="col-md-4"),
                css_class="row g-3",
            ),
            Row(
                Column("contract_type", css_class="col-md-4"),
                Column("status", css_class="col-md-4"),
                Column("tenant", css_class="col-md-4"),
                css_class="row g-3",
            ),
            Row(
                Column("supplier", css_class="col-md-6"),
                Column("purchase_order", css_class="col-md-6"),
                css_class="row g-3",
            ),
            Row(
                Column("cost", css_class="col-md-4"),
                Column("currency", css_class="col-md-4"),
                Column("billing_cycle", css_class="col-md-4"),
                css_class="row g-3",
            ),
            Row(Column("cost_center", css_class="col-md-6"), css_class="row g-3"),
            Row(
                Column("start_date", css_class="col-md-4"),
                Column("end_date", css_class="col-md-4"),
                Column("renewal_date", css_class="col-md-4"),
                css_class="row g-3",
            ),
            Row(Column("auto_renew", css_class="col-md-12"), css_class="row g-3"),
            Row(
                Column("sla_response_time", css_class="col-md-4"),
                Column("sla_resolution_time", css_class="col-md-4"),
                Column("coverage_hours", css_class="col-md-4"),
                css_class="row g-3",
            ),
            "sla_terms",
            "assets",
            "notes",
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Contract"), css_class="btn btn-primary"),
            HTML(
                "<a href=\"{% url 'procurement:contract_list' %}\" "
                'class="btn btn-outline-secondary ms-2" data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        )


class ReceiveLineForm(TenantScopedFormMixin, forms.Form):
    line_id = forms.IntegerField(widget=forms.HiddenInput)
    qty_to_receive = forms.IntegerField(
        min_value=0, label=_("Qty to Receive"), widget=forms.NumberInput(attrs={"class": "form-control", "min": 0})
    )


class BaseReceiveLineFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        total_qty = 0
        for form in self.forms:
            total_qty += form.cleaned_data.get("qty_to_receive", 0)
        if total_qty == 0:
            raise ValidationError(_("You must specify at least one item to receive."))


ReceiveLineFormSet = forms.formset_factory(ReceiveLineForm, formset=BaseReceiveLineFormSet, extra=0)


class AssetProvisionForm(TenantScopedFormMixin, forms.Form):
    line_id = forms.IntegerField(widget=forms.HiddenInput)
    serial_number = forms.CharField(
        max_length=100,
        required=False,
        label=_("Serial Number"),
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": _("Optional")}),
    )
    asset_tag = forms.CharField(
        max_length=50,
        required=False,
        label=_("Asset Tag"),
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": _("Generate automatically")}),
    )
    name = forms.CharField(
        max_length=255,
        required=False,
        label=_("Asset Name"),
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": _("Asset model name")}),
    )


class BaseAssetProvisionFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return

        tags = set()
        serials = set()

        for form in self.forms:
            if not form.is_valid():
                continue

            tag = form.cleaned_data.get("asset_tag")
            serial = form.cleaned_data.get("serial_number")

            if tag:
                tag = tag.strip()
                if tag in tags:
                    form.add_error("asset_tag", _("Duplicate asset tag in this batch."))
                tags.add(tag)
                # Check DB for duplicate tag
                if Asset.objects.for_scope(Scope.current()).filter(asset_tag=tag).exists():
                    form.add_error("asset_tag", _("An asset with tag '%(tag)s' already exists.") % {"tag": tag})

            if serial:
                serial = serial.strip()
                if serial in serials:
                    form.add_error("serial_number", _("Duplicate serial number in this batch."))
                serials.add(serial)
                # Check DB for duplicate serial
                if Asset.objects.for_scope(Scope.current()).filter(serial_number=serial).exists():
                    form.add_error(
                        "serial_number",
                        _("An asset with serial number '%(serial)s' already exists.") % {"serial": serial},
                    )
