from datetime import timedelta

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Fieldset, Layout, Submit
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models as db_models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, Supplier
from core.context import get_current_tenant
from core.forms import BulkEditForm, CrispyFormMixin, FilterForm, TenantScopedFormMixin, cancel_button
from core.importers.bulk_forms import BulkImportForm, register_import_form
from core.managers import Scope
from extras.customfields import CustomFieldModelFormMixin
from organization.models import AssetHolder, CostCenter, Location, Tenant

from .filters import SubscriptionFilterSet
from .models import Subscription, SubscriptionAssignment

# Contract and Subscription forms share one exclusive ownership mapping: the same
# text ships on the Contract type field, so one agreement is recorded in one
# module only. Kept as one constant so both shipped surfaces cannot drift.
AGREEMENT_OWNERSHIP_HELP = _(
    "Record one agreement in one module only. SaaS and cloud entitlement: record as Subscription. "
    "Support, maintenance, lease, warranty, SLA, or asset-covered service: record as Contract. "
    "Other recurring entitlement without asset/SLA coverage: record as Subscription. "
    "Other legal/commercial agreement: record as Contract."
)


@register_import_form
class SubscriptionBulkImportForm(BulkImportForm):
    model = Subscription
    required_fields = ["name", "supplier"]
    optional_fields = [
        "slug",
        "type",
        "start_date",
        "renewal_date",
        "renewal_cost",
        "currency",
        "billing_cycle",
        "term_months",
        "vendor_contract_auto_renews",
        "auto_renewal",
        "licensed_quantity",
        "contract_reference",
        "linked_contract",
        "cost_center",
        "owner",
        "description",
        "notes",
    ]

    def _create_instance(self, mapped_data):
        tenant = get_current_tenant()
        if tenant is not None:
            mapped_data = {**mapped_data, "tenant_id": tenant.pk}
        return super()._create_instance(mapped_data)

    @staticmethod
    def _boolean(value):
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValidationError("Renewal terms must be a boolean value.")

    def map_row(self, row):
        mapped = super().map_row(row)
        canonical = row.get("vendor_contract_auto_renews")
        legacy = row.get("auto_renewal")
        canonical_value = self._boolean(canonical) if canonical not in (None, "") else None
        legacy_value = self._boolean(legacy) if legacy not in (None, "") else None
        if canonical_value is not None and legacy_value is not None and canonical_value != legacy_value:
            raise ValidationError("auto_renewal conflicts with vendor_contract_auto_renews.")
        value = canonical_value if canonical_value is not None else legacy_value
        if value is not None:
            mapped["vendor_contract_auto_renews"] = value
        return mapped


class SubscriptionBulkEditForm(BulkEditForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("status", "cancellation_date"):
            self.fields.pop(field_name, None)
        self.fields["_selected_fields"].choices = [
            choice for choice in self.fields["_selected_fields"].choices if choice[0] in self.fields
        ]


class SubscriptionForm(TenantScopedFormMixin, CrispyFormMixin, CustomFieldModelFormMixin, forms.ModelForm):
    # The tenant stays selectable once a tenant exists; a user with a single
    # accessible tenant gets it preset.
    tenant_required = True
    tenant_autoset_when_single = True

    tenant = forms.ModelChoiceField(
        queryset=Tenant.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Tenant"),
    )
    cost_center = forms.ModelChoiceField(
        queryset=CostCenter.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Cost Center"),
    )

    class Meta:
        model = Subscription
        fields = (
            "name",
            "slug",
            "supplier",
            "type",
            "start_date",
            "renewal_date",
            "term_months",
            "renewal_cost",
            "currency",
            "billing_cycle",
            "licensed_quantity",
            "contract_reference",
            "linked_contract",
            "cost_center",
            "owner",
            "vendor_contract_auto_renews",
            "description",
            "notes",
            "tags",
            "tenant",
        )
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "supplier": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "type": forms.Select(attrs={"class": "form-select"}),
            "start_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "renewal_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "renewal_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "currency": forms.TextInput(attrs={"class": "form-control", "maxlength": 3}),
            "billing_cycle": forms.Select(attrs={"class": "form-select"}),
            "term_months": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "licensed_quantity": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "contract_reference": forms.TextInput(attrs={"class": "form-control"}),
            "linked_contract": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "owner": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "tags": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
        }
        help_texts = {
            "slug": _("Changing the slug may break existing import references that use it as a natural key."),
            "type": AGREEMENT_OWNERSHIP_HELP,
            "contract_reference": _(
                "External vendor, PO, or agreement reference. It does not link a Procurement Contract and must "
                "not be used to justify duplicating the same agreement."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Rescope the tenant-owned `cost_center` FK per request (import-frozen
        # unscoped — would expose/permit another tenant's cost center).
        self.fields["cost_center"].queryset = CostCenter.objects.all()
        self.fields["cost_center"].label_from_instance = lambda cost_center: (
            f"{cost_center.code}: {cost_center.name}" if cost_center.code else cost_center.name
        )
        contract_model = self.fields["linked_contract"].queryset.model
        self.fields["linked_contract"].queryset = contract_model.objects.all()
        # Keep active suppliers from the current tenant or group and globals selectable.
        self.fields["supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)

        cancel_url = (
            self.instance.get_absolute_url() if self.instance.pk else reverse("subscriptions:subscription_list")
        )

        self.helper.layout = Layout(
            Fieldset(
                _("Identity"),
                Div(
                    Div("name", css_class="col-md-6"),
                    Div("slug", css_class="col-md-6"),
                    css_class="row",
                ),
                Div(
                    Div("supplier", css_class="col-md-6"),
                    Div("type", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Dates & Term"),
                Div(
                    Div("start_date", css_class="col-md-4"),
                    Div("renewal_date", css_class="col-md-4"),
                    Div("term_months", css_class="col-md-4"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Financial"),
                Div(
                    Div("renewal_cost", css_class="col-md-4"),
                    Div("currency", css_class="col-md-3"),
                    Div("billing_cycle", css_class="col-md-5"),
                    css_class="row",
                ),
                Div(
                    Div("licensed_quantity", css_class="col-md-4"),
                    Div("contract_reference", css_class="col-md-4"),
                    Div("cost_center", css_class="col-md-4"),
                    css_class="row",
                ),
                "linked_contract",
            ),
            Fieldset(
                _("Policy"),
                Div(
                    Div("vendor_contract_auto_renews", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Scope"),
                Div(
                    Div("owner", css_class="col-md-6"),
                    Div("tenant", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
            Fieldset(
                _("Notes & Tags"),
                "description",
                "notes",
                "tags",
            ),
            *self.action_buttons(cancel_url),
        )
        self.append_custom_fields_to_layout()


class SubscriptionFilterForm(FilterForm):
    filterset_class = SubscriptionFilterSet


class SubscriptionAssignmentForm(TenantScopedFormMixin, forms.ModelForm):
    subscription = forms.ModelChoiceField(
        queryset=Subscription.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Subscription"),
    )
    notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}), label=_("Notes")
    )

    class Meta:
        model = SubscriptionAssignment
        fields = ["subscription", "notes"]

    def clean(self):
        cleaned_data = super().clean()
        subscription = cleaned_data.get("subscription")
        if subscription and self.content_type and self.object_id:
            if (
                SubscriptionAssignment.objects.for_scope(Scope.current())
                .filter(subscription=subscription, content_type=self.content_type, object_id=self.object_id)
                .exists()
            ):
                raise forms.ValidationError(_("This subscription is already assigned to this object."))
        return cleaned_data

    def __init__(self, *args, **kwargs):
        content_type = kwargs.pop("content_type", None)
        object_id = kwargs.pop("object_id", None)
        cancel_url = kwargs.pop("cancel_url", reverse("subscriptions:subscription_list"))
        quick_add_dismiss = kwargs.pop("quick_add_dismiss", False)
        super().__init__(*args, **kwargs)
        self.content_type = content_type
        self.object_id = object_id

        if content_type and object_id:
            # A content type whose model is gone, or a target that no longer exists,
            # simply leaves the subscription choices unfiltered.
            target_model = content_type.model_class()
            target_obj = target_model.objects.filter(id=object_id).first() if target_model is not None else None
            if target_obj is not None and hasattr(target_obj, "tenant") and target_obj.tenant:
                # Filter subscriptions to active ones that belong to the same tenant or are global
                self.fields["subscription"].queryset = Subscription.objects.for_scope(Scope.current()).filter(
                    db_models.Q(tenant=target_obj.tenant) | db_models.Q(tenant__isnull=True), status="active"
                )

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "subscription",
            "notes",
        )

        button_text = "Assign"
        self.helper.layout.append(HTML('<div class="mt-4"></div>'))
        self.helper.layout.append(Submit("submit", button_text, css_class="btn btn-primary"))
        self.helper.layout.append(cancel_button(cancel_url, dismiss=quick_add_dismiss))

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.content_type and self.object_id:
            instance.content_type = self.content_type
            instance.object_id = self.object_id
        if commit:
            instance.save()
        return instance


class SubscriptionRenewForm(TenantScopedFormMixin, forms.Form):
    renewal_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Next Renewal Date"),
        required=True,
    )
    renewal_cost = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
        label=_("Renewal Cost"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        subscription = kwargs.pop("subscription", None)
        super().__init__(*args, **kwargs)
        if subscription:
            self.fields["renewal_cost"].initial = subscription.renewal_cost
            if subscription.renewal_date:
                cycle = subscription.billing_cycle
                current_date = subscription.renewal_date
                if cycle == "monthly":
                    self.fields["renewal_date"].initial = current_date + timedelta(days=30)
                elif cycle == "quarterly":
                    self.fields["renewal_date"].initial = current_date + timedelta(days=91)
                elif cycle == "biannual":
                    self.fields["renewal_date"].initial = current_date + timedelta(days=182)
                else:
                    self.fields["renewal_date"].initial = current_date + timedelta(days=365)
            else:
                self.fields["renewal_date"].initial = timezone.now().date()

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "renewal_date",
            "renewal_cost",
        )


class SubscriptionCancelForm(TenantScopedFormMixin, forms.Form):
    cancellation_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Cancellation Date"),
        required=True,
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        label=_("Cancellation Reason"),
        required=False,
        help_text=_("Optional reason notes to log on the subscription."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["cancellation_date"].initial = timezone.now().date()

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "cancellation_date",
            "reason",
        )


class SubscriptionCheckoutForm(TenantScopedFormMixin, forms.Form):
    TARGET_CHOICES = [
        ("holder", _("Employee / Asset Holder")),
        ("asset", _("Hardware Asset")),
        ("location", _("Location")),
    ]

    target_type = forms.ChoiceField(
        choices=TARGET_CHOICES, widget=forms.Select(attrs={"class": "form-select"}), label=_("Assign to")
    )
    assigned_holder = forms.ModelChoiceField(
        queryset=AssetHolder.objects.all().order_by("last_name", "first_name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Asset Holder"),
    )
    asset = forms.ModelChoiceField(
        queryset=Asset.issuable().order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Hardware Asset"),
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.all().order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Location"),
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}), required=False, label=_("Notes")
    )

    def clean(self):
        cleaned_data = super().clean()
        target_type = cleaned_data.get("target_type")
        holder = cleaned_data.get("assigned_holder")
        asset = cleaned_data.get("asset")
        location = cleaned_data.get("location")

        if target_type == "holder" and not holder:
            raise forms.ValidationError(_("Must select an Asset Holder."), code="holder_required")
        if target_type == "asset" and not asset:
            raise forms.ValidationError(_("Must select a Hardware Asset."), code="asset_required")
        if target_type == "location" and not location:
            raise forms.ValidationError(_("Must select a Location."), code="location_required")
        if not target_type:
            raise forms.ValidationError(_("Must select a target type."), code="target_type_required")

        # Check for duplicate assignment
        target_obj = None
        if target_type == "holder":
            target_obj = holder
        elif target_type == "asset":
            target_obj = asset
        elif target_type == "location":
            target_obj = location

        if target_obj and getattr(self, "subscription", None):
            content_type = ContentType.objects.get_for_model(target_obj)
            if (
                SubscriptionAssignment.objects.for_scope(Scope.current())
                .filter(subscription=self.subscription, content_type=content_type, object_id=target_obj.pk)
                .exists()
            ):
                raise forms.ValidationError(
                    _("This subscription is already assigned to %(target)s.") % {"target": target_obj}
                )

        return cleaned_data

    def __init__(self, *args, **kwargs):
        subscription = kwargs.pop("subscription", None)
        self.subscription = subscription
        super().__init__(*args, **kwargs)

        # If tenant is restricted, filter candidates
        if subscription and subscription.tenant:
            self.fields["assigned_holder"].queryset = AssetHolder.objects.filter(tenant=subscription.tenant).order_by(
                "last_name", "first_name"
            )
            self.fields["asset"].queryset = Asset.issuable().filter(tenant=subscription.tenant).order_by("name")
            self.fields["location"].queryset = Location.objects.filter(tenant=subscription.tenant).order_by("name")

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "target_type",
            "assigned_holder",
            "asset",
            "location",
            "notes",
        )
