from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Layout, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms import formset_factory
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, AssetRequest, AssetType
from core.forms import TenantScopedFormMixin
from core.managers import Scope


class AssetRequestForm(TenantScopedFormMixin, forms.ModelForm):
    CATEGORY_CHOICES = [
        ("asset_type", _("Asset Type (General Model)")),
        ("asset", _("Specific Asset (by Tag)")),
        ("component", _("Component")),
        ("accessory", _("Accessory")),
        ("consumable", _("Consumable")),
    ]
    request_category = forms.ChoiceField(
        choices=CATEGORY_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "id": "id_request_category"}),
        label=_("Request Category"),
        required=False,
        initial="asset_type",
    )

    TARGET_CHOICES = [
        ("", _("Myself")),
        ("assetholder", _("Asset Holder")),
        ("location", _("Location")),
        ("asset", _("Asset")),
    ]

    target_type = forms.ChoiceField(
        choices=TARGET_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Request For"),
    )

    class Meta:
        model = AssetRequest
        fields = [
            "request_category",
            "asset_type",
            "asset",
            "component",
            "accessory",
            "consumable",
            "qty",
            "target_type",
            "assigned_user",
            "assigned_location",
            "assigned_asset",
            "notes",
        ]
        widgets = {
            "asset_type": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "asset": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "component": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "accessory": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "consumable": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "qty": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "assigned_user": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "assigned_location": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "assigned_asset": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):

        from core.managers import get_current_tenant
        from inventory.models import Accessory, Component, Consumable
        from organization.models import AssetHolder, Location

        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        self.fields["qty"].required = False
        self.fields["qty"].initial = 1

        user = self.request.user if self.request else None
        can_delegate = (user.is_staff or user.has_perm("assets.add_delegated_assetrequest")) if user else False

        tenant = get_current_tenant()

        # Only allow requestable objects
        self.fields["asset_type"].queryset = AssetType.objects.filter(requestable=True)
        # Only allow requestable and deployable assets
        self.fields["asset"].queryset = Asset.issuable().filter(
            Q(requestable=True) | Q(requestable__isnull=True, asset_type__requestable=True)
        )
        self.fields["component"].queryset = Component.objects.all().order_by("name")
        self.fields["accessory"].queryset = Accessory.objects.all().order_by("name")
        self.fields["consumable"].queryset = Consumable.objects.all().order_by("name")

        if tenant:
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(tenant=tenant)
            self.fields["component"].queryset = self.fields["component"].queryset.filter(tenant=tenant)
            self.fields["accessory"].queryset = self.fields["accessory"].queryset.filter(tenant=tenant)
            self.fields["consumable"].queryset = self.fields["consumable"].queryset.filter(tenant=tenant)
            self.fields["assigned_user"].queryset = AssetHolder.objects.filter(tenant=tenant).order_by(
                "last_name", "first_name"
            )
            self.fields["assigned_location"].queryset = (
                Location.objects.filter(tenant=tenant).select_related("site").order_by("site__name", "name")
            )
            self.fields["assigned_asset"].queryset = (
                Asset.objects.for_scope(Scope.current()).filter(tenant=tenant).order_by("name")
            )
        else:
            self.fields["assigned_user"].queryset = AssetHolder.objects.all().order_by("last_name", "first_name")
            self.fields["assigned_location"].queryset = (
                Location.objects.all().select_related("site").order_by("site__name", "name")
            )
            self.fields["assigned_asset"].queryset = Asset.objects.for_scope(Scope.current()).all().order_by("name")

        # Set initial value for request_category and target_type if instance exists
        if self.instance and self.instance.pk:
            if self.instance.asset:
                self.fields["request_category"].initial = "asset"
            elif self.instance.component:
                self.fields["request_category"].initial = "component"
            elif self.instance.accessory:
                self.fields["request_category"].initial = "accessory"
            elif self.instance.consumable:
                self.fields["request_category"].initial = "consumable"
            else:
                self.fields["request_category"].initial = "asset_type"

            if self.instance.assigned_user:
                self.fields["target_type"].initial = "assetholder"
            elif self.instance.assigned_location:
                self.fields["target_type"].initial = "location"
            elif self.instance.assigned_asset:
                self.fields["target_type"].initial = "asset"
            else:
                self.fields["target_type"].initial = ""

        if not can_delegate:
            self.fields["target_type"].choices = [("", "Myself")]
            self.fields["target_type"].widget = forms.HiddenInput()
            self.fields["assigned_user"].widget = forms.HiddenInput()
            self.fields["assigned_location"].widget = forms.HiddenInput()
            self.fields["assigned_asset"].widget = forms.HiddenInput()

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        cancel_url = "#"
        if self.instance and self.instance.pk:
            cancel_url = self.instance.get_absolute_url()
        else:
            cancel_url = reverse("assets:assetrequest_list")

        button_text = _("Update") if self.instance and self.instance.pk else _("Create")

        self.helper.layout = Layout(
            "request_category",
            "asset_type",
            "asset",
            "component",
            "accessory",
            "consumable",
            "qty",
            "target_type",
            "assigned_user",
            "assigned_location",
            "assigned_asset",
            "notes",
            HTML('<div class="mt-4"></div>'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2">Cancel</a>'),
        )

    def clean(self):
        cleaned_data = super().clean()
        asset_type = cleaned_data.get("asset_type")
        asset = cleaned_data.get("asset")
        target_type = cleaned_data.get("target_type")
        assigned_user = cleaned_data.get("assigned_user")
        assigned_location = cleaned_data.get("assigned_location")
        assigned_asset = cleaned_data.get("assigned_asset")

        component = cleaned_data.get("component")
        accessory = cleaned_data.get("accessory")
        consumable = cleaned_data.get("consumable")
        qty = cleaned_data.get("qty")

        user = self.request.user if self.request else None
        can_delegate = (user.is_staff or user.has_perm("assets.add_delegated_assetrequest")) if user else False

        if user and not self.instance.requester_id:
            self.instance.requester = user

        categories_filled = []
        if asset is not None or asset_type is not None:
            categories_filled.append("asset")
        if component is not None:
            categories_filled.append("component")
        if accessory is not None:
            categories_filled.append("accessory")
        if consumable is not None:
            categories_filled.append("consumable")

        if len(categories_filled) == 0:
            raise ValidationError(
                _("Select the item you are requesting (Asset, Asset Type, Component, Accessory, or Consumable).")
            )
        if len(categories_filled) > 1:
            raise ValidationError(_("A request can contain only one item type."))

        if qty is None:
            qty = 1
            cleaned_data["qty"] = 1
        elif qty <= 0:
            raise ValidationError({"qty": _("Requested quantity must be greater than zero.")})

        if asset and asset_type and asset.asset_type != asset_type:
            raise ValidationError({"asset": _("Selected asset does not match the chosen asset type.")})

        # Process target selections based on target_type
        if target_type == "assetholder":
            if not can_delegate:
                raise ValidationError(_("You do not have permission to request assets on behalf of others."))
            if not assigned_user:
                raise ValidationError({"assigned_user": _("Select an Asset Holder.")})
            cleaned_data["assigned_location"] = None
            cleaned_data["assigned_asset"] = None
        elif target_type == "location":
            if not can_delegate:
                raise ValidationError(_("You do not have permission to request assets on behalf of others."))
            if not assigned_location:
                raise ValidationError({"assigned_location": _("Select a Location.")})
            cleaned_data["assigned_user"] = None
            cleaned_data["assigned_asset"] = None
        elif target_type == "asset":
            if not can_delegate:
                raise ValidationError(_("You do not have permission to request assets on behalf of others."))
            if not assigned_asset:
                raise ValidationError({"assigned_asset": _("Select a Parent Asset.")})
            cleaned_data["assigned_user"] = None
            cleaned_data["assigned_location"] = None
        else:  # target_type == '' (Myself)
            cleaned_data["assigned_user"] = None
            cleaned_data["assigned_location"] = None
            cleaned_data["assigned_asset"] = None

        return cleaned_data


class AssetRequestActionForm(TenantScopedFormMixin, forms.Form):
    allocated_asset = forms.ModelChoiceField(
        queryset=Asset.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Allocate Specific Asset"),
    )
    allocated_location = forms.ModelChoiceField(
        queryset=Asset.objects.none(),  # Populated dynamically in __init__
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Stock Location"),
    )
    qty = forms.IntegerField(
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        label=_("Approved Quantity"),
    )
    response_notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        required=False,
        label=_("Response/Decision Notes"),
    )

    def __init__(self, *args, **kwargs):

        from core.managers import get_current_tenant
        from organization.models import Location

        self.request_instance = kwargs.pop("request_instance", None)
        kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)

        tenant = get_current_tenant()

        # Populate allocated_location
        loc_qs = Location.objects.filter(status=Location.STATUS_ACTIVE)
        if tenant:
            loc_qs = loc_qs.filter(tenant=tenant)
        self.fields["allocated_location"].queryset = loc_qs.select_related("site").order_by("site__name", "name")

        if self.request_instance:
            target_type = self.request_instance.asset_type
            # Populate allocated_asset
            asset_qs = Asset.issuable()
            if tenant:
                asset_qs = asset_qs.filter(tenant=tenant)

            if target_type:
                self.fields["allocated_asset"].queryset = asset_qs.filter(
                    Q(requestable=True) | Q(requestable__isnull=True, asset_type__requestable=True),
                    asset_type=target_type,
                )
            else:
                self.fields["allocated_asset"].queryset = asset_qs.filter(
                    Q(requestable=True) | Q(requestable__isnull=True, asset_type__requestable=True)
                )

            # Pre-populate qty
            if self.request_instance.qty:
                self.fields["qty"].initial = self.request_instance.qty

    def clean(self):
        cleaned_data = super().clean()
        qty = cleaned_data.get("qty")
        allocated_location = cleaned_data.get("allocated_location")

        if self.request_instance:
            is_inventory = (
                self.request_instance.component_id is not None
                or self.request_instance.accessory_id is not None
                or self.request_instance.consumable_id is not None
            )

            # 1. Action form validation: requires stock location for inventory items
            if is_inventory and not allocated_location:
                raise ValidationError({"allocated_location": _("Stock location is required for inventory items.")})

            # 2. Action form validation: approved qty cannot exceed requested qty
            if qty is not None:
                if qty <= 0:
                    raise ValidationError({"qty": _("Quantity must be greater than zero.")})
                if qty > self.request_instance.qty:
                    raise ValidationError({"qty": _("Approved quantity cannot exceed requested quantity.")})

        return cleaned_data


class AssetRequestManualCompletionForm(TenantScopedFormMixin, forms.Form):
    reason = forms.CharField(
        label=_("Reason for manual completion"),
        required=True,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text=_("Explain why no assignment or stock booking will be generated."),
    )
    confirmed_no_handover = forms.BooleanField(
        label=_("I confirm that no assignment or stock booking will be generated."),
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class AssetRequestResponseForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = AssetRequest
        fields = ["status", "response_notes"]
        widgets = {
            "status": forms.Select(attrs={"class": "form-select"}),
            "response_notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class AssetReceiveForm(TenantScopedFormMixin, forms.Form):
    request_id = forms.IntegerField(widget=forms.HiddenInput())
    asset_tag = forms.CharField(max_length=50, widget=forms.TextInput(attrs={"class": "form-control"}))
    serial_number = forms.CharField(max_length=100, widget=forms.TextInput(attrs={"class": "form-control"}))
    name = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "form-control"}))
    status = forms.ModelChoiceField(
        queryset=Asset.objects.none(), widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""})
    )
    location = forms.ModelChoiceField(
        queryset=Asset.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    supplier = forms.ModelChoiceField(
        queryset=Asset.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    order_number = forms.CharField(
        max_length=100, required=False, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    purchase_cost = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.00"}),
    )
    purchase_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"class": "form-control", "type": "date"})
    )

    def __init__(self, *args, **kwargs):
        from assets.models import StatusLabel, Supplier
        from core.managers import get_current_tenant
        from organization.models import Location

        super().__init__(*args, **kwargs)

        tenant = get_current_tenant()

        # Populate Status Labels
        self.fields["status"].queryset = StatusLabel.objects.all().order_by("name")

        # Populate Locations active for tenant
        loc_qs = Location.objects.filter(status=Location.STATUS_ACTIVE)
        if tenant:
            loc_qs = loc_qs.filter(tenant=tenant)
        self.fields["location"].queryset = loc_qs.select_related("site").order_by("site__name", "name")

        # Populate Suppliers
        self.fields["supplier"].queryset = (
            Supplier.objects.for_scope(Scope.current()).filter(is_active=True).order_by("name")
        )

    def clean_asset_tag(self):
        asset_tag = self.cleaned_data.get("asset_tag", "").strip()
        if not asset_tag:
            raise ValidationError(_("Asset tag is required."))
        from assets.models import Asset

        if Asset.objects.for_scope(Scope.current()).filter(asset_tag=asset_tag).exists():
            raise ValidationError(_("Asset with this tag already exists."))
        return asset_tag

    def clean_serial_number(self):
        serial_number = self.cleaned_data.get("serial_number", "").strip()
        if not serial_number:
            raise ValidationError(_("Serial number is required."))
        from assets.models import Asset

        if Asset.objects.for_scope(Scope.current()).filter(serial_number=serial_number).exists():
            raise ValidationError(_("Asset with this serial number already exists."))
        return serial_number


class BaseAssetReceiveFormSet(forms.BaseFormSet):
    def clean(self):
        if any(self.errors):
            return

        tags = set()
        serials = set()

        for form in self.forms:
            if self.is_bound and not form.is_valid():
                continue

            tag = form.cleaned_data.get("asset_tag")
            serial = form.cleaned_data.get("serial_number")

            if tag:
                if tag in tags:
                    form.add_error("asset_tag", "Duplicate asset tag in this batch.")
                tags.add(tag)

            if serial:
                if serial in serials:
                    form.add_error("serial_number", "Duplicate serial number in this batch.")
                serials.add(serial)


AssetReceiveFormSet = formset_factory(AssetReceiveForm, formset=BaseAssetReceiveFormSet, extra=0)
