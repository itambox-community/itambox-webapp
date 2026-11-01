from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Layout, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.choices import StatusTypeChoices
from assets.models import Asset, StatusLabel
from core.forms import FilterForm, TenantScopedFormMixin
from core.managers import get_current_tenant
from core.tenant_scope import accessible_tenant_ids, get_descendant_tenant_group_ids
from extras.models import Tag
from organization.models import Location, Tenant

from ..filters import KitFilterSet
from ..models import Kit, KitItem
from .base_forms import BaseCheckoutForm


class KitForm(TenantScopedFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tomselect-tags": "true"}),
    )

    class Meta:
        model = Kit
        fields = ["name", "description", "tenant", "tags"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = "Update" if self.instance.pk else "Create"
        cancel_url = reverse("inventory:kit_list")

        self.helper.layout = Layout(
            "name",
            "description",
            "tenant",
            "tags",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML('<a href="{}" class="btn btn-outline-secondary ms-2">Cancel</a>'.format(cancel_url)),
            HTML("</div>"),
        )


class KitItemForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = KitItem
        fields = ["kit", "asset_type", "accessory", "license", "consumable", "qty"]
        widgets = {
            "kit": forms.Select(attrs={"class": "form-select"}),
            "asset_type": forms.Select(attrs={"class": "form-select"}),
            "accessory": forms.Select(attrs={"class": "form-select"}),
            "license": forms.Select(attrs={"class": "form-select"}),
            "consumable": forms.Select(attrs={"class": "form-select"}),
            "qty": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # kit / accessory / license / consumable are scoped by TenantScopedFormMixin
        # so a kit item can't reference another tenant's rows. asset_type is a
        # global catalogue model (its queryset has no tenant scoping).

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = "Update" if self.instance.pk else "Create"
        cancel_url = (
            self.instance.kit.get_absolute_url()
            if (self.instance.pk and self.instance.kit)
            else reverse("inventory:kit_list")
        )

        self.helper.layout = Layout(
            "kit",
            "asset_type",
            "accessory",
            "license",
            "consumable",
            "qty",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML('<a href="{}" class="btn btn-outline-secondary ms-2">Cancel</a>'.format(cancel_url)),
            HTML("</div>"),
        )

    def clean(self):
        cleaned_data = super().clean()
        asset_type = cleaned_data.get("asset_type")
        accessory = cleaned_data.get("accessory")
        license_item = cleaned_data.get("license")
        consumable = cleaned_data.get("consumable")

        targets = [asset_type, accessory, license_item, consumable]
        filled = [t for t in targets if t is not None]
        if len(filled) == 0:
            raise ValidationError(_("A kit item must select either an Asset Type, Accessory, License, or Consumable."))
        if len(filled) > 1:
            raise ValidationError(
                _(
                    "A kit item cannot select more than one target (must be either Asset Type OR Accessory OR "
                    "License OR Consumable)."
                )
            )
        return cleaned_data


def _asset_choice_label(asset):
    """Identify a selectable device by asset tag (str(asset)) and serial."""
    if asset.serial_number:
        return f"{asset}, SN {asset.serial_number}"
    return str(asset)


def tenant_group_tenant_ids(group):
    """Live tenants of a tenant group's subtree."""
    return set(
        Tenant.objects.filter(
            group_id__in=get_descendant_tenant_group_ids(group.pk, live_only=True),
            deleted_at__isnull=True,
        ).values_list("pk", flat=True)
    )


def kit_target_tenant_queryset(request, kit):
    """Live tenants the request may explicitly target for this kit checkout.

    The canonical accessible set (``core.tenant_scope``) intersected with the
    active tenant group when a group scope is active, and with the kit's own
    tenant when the kit is tenant-scoped. Never derived from a holder or asset.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return Tenant.objects.none()
    tenant_ids = accessible_tenant_ids(user)
    group = getattr(request, "active_tenant_group", None)
    if group is not None:
        tenant_ids &= tenant_group_tenant_ids(group)
    if kit is not None and kit.tenant_id:
        tenant_ids &= {kit.tenant_id}
    if not tenant_ids:
        return Tenant.objects.none()
    return Tenant.objects.filter(pk__in=tenant_ids, deleted_at__isnull=True).order_by("name")


def kit_target_tenant(request, kit, raw_value=None):
    """Resolve the tenant this kit checkout is bound to, without widening scope.

    In a concrete tenant scope the request context is authoritative and any
    submitted value is ignored. In an aggregate scope the submitted value only
    counts when it names one of the candidate tenants; anything else resolves to
    ``None`` so the dependent choices stay empty.
    """
    if request is None:
        return None
    concrete = getattr(request, "active_tenant", None)
    if concrete is not None and not getattr(request, "active_all_accessible", False):
        return concrete
    if raw_value in (None, ""):
        return None
    try:
        return kit_target_tenant_queryset(request, kit).filter(pk=raw_value).first()
    except (TypeError, ValueError):
        return None


def kit_owner_tenant(kit):
    """Owning tenant of a tenant-scoped kit; ``None`` for a global template."""
    if kit is None or not kit.tenant_id:
        return None
    return kit.tenant


class KitCheckoutForm(BaseCheckoutForm):
    source_location = forms.ModelChoiceField(
        queryset=Location.objects.all().order_by("name"),
        required=True,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Source Location (stock deduction)"),
    )
    status = forms.ModelChoiceField(
        queryset=StatusLabel.objects.filter(type=StatusTypeChoices.DEPLOYED).order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Status"),
    )
    checkout_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Checkout Date"),
    )
    expected_checkin = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Expected Checkin Date"),
    )
    is_loan = forms.BooleanField(
        required=False,
        label=_("Loan"),
        help_text=_("Mark this assignment as a temporary loan with a mandatory return date."),
    )
    due_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label=_("Due Date"),
    )

    def __init__(self, *args, **kwargs):
        self.kit = kwargs.pop("kit", None)
        self.request = kwargs.pop("request", None)
        tenant = self.kit.tenant if self.kit else None
        kwargs["initial"] = self._initial_with_owner_tenant(kwargs.get("initial"))
        super().__init__(*args, tenant=tenant, **kwargs)
        self.aggregate_scope = self._is_aggregate_scope()
        self.target_tenant = self._resolve_target_tenant()
        # Hardware rows need an explicit device pick: the previous implicit
        # first deployable asset silently substituted devices (and could pick
        # the same asset twice). The per-row fields are the operator-facing
        # half of that contract; the service re-validates the mapping under
        # row locks.
        self.hardware_items = self._hardware_items()
        self._add_target_tenant_field()
        self._add_hardware_selection_fields()
        self._rescope_choice_fields()
        self.helper = FormHelper()
        self.helper.form_tag = False
        layout_fields = []
        if "tenant" in self.fields:
            layout_fields.append("tenant")
        layout_fields += [
            "source_location",
            "assigned_holder",
            HTML('<p class="text-muted text-center my-2">OR</p>'),
            "assigned_location",
            HTML("<hr>"),
            HTML('<p class="text-muted">%s</p>' % _("Select one device per hardware item.")),
            *[f"asset_{item.pk}" for item in self.hardware_items],
            "status",
            "checkout_date",
            "expected_checkin",
            "is_loan",
            "due_date",
            "notes",
        ]
        self.helper.layout = Layout(*layout_fields)

    def _is_aggregate_scope(self):
        """Whether the request carries no concrete tenant to check out into."""
        if self.request is None:
            return False
        return getattr(self.request, "active_tenant", None) is None

    def _initial_with_owner_tenant(self, initial):
        """Open a tenant-owned kit's checkout on its OWNING tenant.

        The owner is the only admissible target of a tenant-owned kit, so the
        modal must not require a needless tenant change before its dependent
        choices appear -- and a forged or stale ``?tenant=`` must not displace
        it. A submitted candidate that resolves stays authoritative for a
        tenantless template, whose target really is an explicit choice.
        """
        resolved = dict(initial or {})
        owner = kit_owner_tenant(self.kit)
        if owner is None:
            return resolved
        if kit_target_tenant(self.request, self.kit, resolved.get("tenant")) is None:
            resolved["tenant"] = owner.pk
        return resolved

    def _resolve_target_tenant(self):
        if self.kit is None:
            return None
        if self.request is None:
            return get_current_tenant() or self.kit.tenant
        raw_value = (self.data.get("tenant") if self.is_bound else None) or self.initial.get("tenant")
        return kit_target_tenant(self.request, self.kit, raw_value)

    def _add_target_tenant_field(self):
        """Explicit target-tenant choice for aggregate scopes only.

        In a concrete scope the active tenant is authoritative and the field is
        not rendered at all, so a forged value cannot override it.
        """
        if self.kit is None or self.request is None or not self.aggregate_scope:
            return
        self.fields["tenant"] = forms.ModelChoiceField(
            queryset=kit_target_tenant_queryset(self.request, self.kit),
            required=True,
            label=_("Target tenant"),
            help_text=_("Required when the current scope contains more than one tenant."),
            widget=forms.Select(attrs=self._tenant_reload_attrs()),
        )

    def _tenant_reload_attrs(self):
        """Refresh the dependent choices in place once a tenant is picked."""
        return {
            "class": "form-select",
            "hx-post": reverse("inventory:kit_checkout_modal", kwargs={"pk": self.kit.pk}),
            "hx-trigger": "change",
            "hx-target": "#kit-checkout-modal-form-container",
            "hx-swap": "innerHTML",
            "hx-vals": '{"_reload": "1"}',
            "hx-include": "closest form",
        }

    def _hardware_items(self):
        if self.kit is None:
            return []
        return list(self.kit.items.select_related("asset_type").filter(asset_type__isnull=False).order_by("pk"))

    def _add_hardware_selection_fields(self):
        for item in self.hardware_items:
            field = forms.ModelChoiceField(
                queryset=self._eligible_devices(item, self.target_tenant),
                required=True,
                empty_label=_("Select a device"),
                label=_("Device for %(item)s") % {"item": item.asset_type},
                widget=forms.Select(attrs={"class": "form-select"}),
            )
            field.label_from_instance = _asset_choice_label
            self.fields[f"asset_{item.pk}"] = field

    @staticmethod
    def _eligible_devices(item, tenant):
        if tenant is None:
            return Asset.objects.none()
        return Asset.issuable().filter(asset_type=item.asset_type, tenant=tenant).order_by("asset_tag", "serial_number")

    def _rescope_choice_fields(self):
        """Render every target choice strictly within the resolved tenant.

        Without a resolved tenant (unselected aggregate scope) the choices stay
        empty: an aggregate target must not expose holders, locations or devices
        of arbitrary tenants.
        """
        tenant = self.target_tenant
        holder_model = self.fields["assigned_holder"].queryset.model
        location_model = self.fields["assigned_location"].queryset.model
        asset_model = self.fields["assigned_asset"].queryset.model
        holder_qs = holder_model.objects.filter(tenant=tenant) if tenant else holder_model.objects.none()
        location_qs = location_model.objects.filter(tenant=tenant) if tenant else location_model.objects.none()
        asset_qs = asset_model.objects.filter(tenant=tenant) if tenant else asset_model.objects.none()
        self.fields["assigned_holder"].queryset = holder_qs.order_by("last_name", "first_name")
        self.fields["assigned_location"].queryset = location_qs.select_related("site").order_by("site__name", "name")
        self.fields["assigned_asset"].queryset = asset_qs.order_by("asset_tag")
        if "source_location" in self.fields:
            self.fields["source_location"].queryset = location_qs.order_by("name")

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("is_loan") and not cleaned_data.get("due_date"):
            self.add_error("due_date", _("A loan assignment requires a due date."))
        cleaned_data["selected_assets"] = self._clean_selected_assets(cleaned_data)
        cleaned_data["target_tenant"] = self._clean_target_tenant(cleaned_data)
        return cleaned_data

    def _clean_selected_assets(self, cleaned_data):
        selected_assets = {}
        seen_devices = set()
        for item in self.hardware_items:
            device = cleaned_data.get(f"asset_{item.pk}")
            if device is None:
                continue
            if device.pk in seen_devices:
                self.add_error(f"asset_{item.pk}", _("Each hardware item must use a different device."))
                continue
            seen_devices.add(device.pk)
            selected_assets[item.pk] = device.pk
        return selected_assets

    def _clean_target_tenant(self, cleaned_data):
        if self.kit is None:
            return None
        if self.aggregate_scope:
            return cleaned_data.get("tenant")
        return self.target_tenant


class KitFilterForm(FilterForm):
    filterset_class = KitFilterSet
