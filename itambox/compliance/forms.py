from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Fieldset, Layout, Row, Submit
from django import forms
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.models import Asset, AssetDisposal, AssetMaintenance, Category, Supplier
from assets.services import (
    REPAIR_ACTION_CHOICES,
    REPAIR_ACTION_NONE,
    active_repair_loan,
    complete_repair,
    issue_repair_loaner,
)
from compliance.registry import signature_providers
from core.forms import (
    CrispyFormMixin,
    FilterForm,
    TenantScopedFormMixin,
    cancel_button,
    scope_tenant_field,
    scope_tenant_group_field,
)
from core.managers import Scope
from extras.models import Tag
from organization.models import Tenant, TenantGroup

from .models import CustodyTemplate


class AssetMaintenanceFilterForm(FilterForm):
    from .filters import AssetMaintenanceFilterSet

    filterset_class = AssetMaintenanceFilterSet


class AssetMaintenanceForm(TenantScopedFormMixin, CrispyFormMixin, forms.ModelForm):
    """Record a maintenance and, for a repair, its loaner story in one step (#644).

    A repair maintenance is the anchor: the optional **Issue loaner** section
    checks a stand-in unit out to the asset's current holder as a loan bound to
    this maintenance, and the **Complete repair** section closes that story
    through the existing services (returned, replaced permanently, or left alone).
    Both sections are only rendered when the actor may perform the underlying
    operation, and both run in the same transaction as the maintenance write.
    """

    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
        required=False,
        label=_("Supplier"),
    )
    loaner_asset = forms.ModelChoiceField(
        queryset=Asset.objects.none(),
        required=False,
        label=_("Loaner"),
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    loaner_due_date = forms.DateField(
        required=False,
        label=_("Loaner due date"),
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    repair_action = forms.ChoiceField(
        choices=REPAIR_ACTION_CHOICES,
        required=False,
        initial=REPAIR_ACTION_NONE,
        label=_("Completion action"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    dispose_original = forms.BooleanField(
        required=False,
        label=_("Start the disposal of the unit that was under repair"),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    disposal_method = forms.ChoiceField(
        choices=(),
        required=False,
        label=_("Disposal Method"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    disposal_date = forms.DateField(
        required=False,
        label=_("Disposal Date"),
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )

    class Meta:
        model = AssetMaintenance
        fields = [
            "asset",
            "supplier",
            "maintenance_type",
            "status",
            "cost",
            "currency",
            "start_date",
            "completion_date",
            "performed_by",
            "description",
            "notes",
            "tags",
        ]
        widgets = {
            "performed_by": forms.TextInput(attrs={"class": "form-control"}),
            "cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "currency": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "tags": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
        }

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.actor = getattr(request, "user", None) if request is not None else None
        # Rescope the tenant-owned `asset` FK per request — its queryset is frozen
        # unscoped at import, so a maintenance record could otherwise reference (and
        # expose in the dropdown) another tenant's asset.
        self.fields["asset"].queryset = Asset.objects.for_scope(Scope.current()).all()
        self.fields["loaner_asset"].queryset = self._selectable_loaners()
        self.fields["disposal_method"].choices = [
            ("", _("Unset")),
            *AssetDisposal._meta.get_field("disposal_method").choices,
        ]
        self.can_hand_over = self._has_permission("assets.change_asset")
        self.can_dispose = self._has_permission("assets.dispose_asset")
        self.existing_loan = active_repair_loan(self.instance) if self.instance.pk else None
        self.offer_loaner = self._is_repair() and self.can_hand_over and self.existing_loan is None
        self.offer_completion = self._is_repair() and self.can_hand_over and self.existing_loan is not None
        self._build_helper()

    # ── repair-flow decisions ────────────────────────────────────────────────
    def _has_permission(self, codename: str) -> bool:
        """Permissions are resolved through the membership backend, never superuser flags (#644)."""
        return bool(self.actor is not None and self.actor.is_authenticated and self.actor.has_perm(codename))

    def _is_repair(self) -> bool:
        """The repair sections follow the SUBMITTED type, so a failed POST keeps them open."""
        if self.is_bound:
            submitted = self.data.get(self.add_prefix("maintenance_type"))
            if submitted:
                return submitted == AssetMaintenance.MAINTENANCE_TYPE_REPAIR
        if self.instance.pk:
            return self.instance.maintenance_type == AssetMaintenance.MAINTENANCE_TYPE_REPAIR
        return True

    def _selectable_loaners(self):
        """Tenant-scoped stand-ins: never the unit that is under repair."""
        queryset = Asset.objects.all()
        if self.instance.pk and self.instance.asset_id:
            queryset = queryset.exclude(pk=self.instance.asset_id)
        return queryset

    # ── rendering ────────────────────────────────────────────────────────────
    def _build_helper(self):
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance and self.instance.pk else _("Create")
        cancel_url = reverse("assets:assetmaintenance_list")

        layout = [
            Row(Column("asset", css_class="col-md-12")),
            Row(Column("supplier", css_class="col-md-6"), Column("performed_by", css_class="col-md-6")),
            Row(Column("maintenance_type", css_class="col-md-6"), Column("status", css_class="col-md-6")),
            Row(
                Column("cost", css_class="col-md-6"),
                Column("currency", css_class="col-md-6"),
            ),
            Row(Column("start_date", css_class="col-md-6"), Column("completion_date", css_class="col-md-6")),
            "description",
            "notes",
            "tags",
        ]
        if self.offer_loaner:
            layout.extend(self._loaner_section())
        if self.offer_completion:
            layout.extend(self._completion_section())
        layout.extend(
            [
                HTML('<div class="mt-3">'),
                Submit("submit", button_text, css_class="btn btn-primary"),
                cancel_button(cancel_url, dismiss=self.quick_add_dismiss),
                HTML("</div>"),
            ]
        )
        self.helper.layout = Layout(*layout)

    def _section_header(self, title, open_section=False) -> str:
        """A collapsed section: the repair extras never crowd the record form."""
        attribute = " open" if open_section else ""
        return f'<details class="border rounded p-3 mt-3"{attribute}><summary class="fw-bold">{title}</summary>'

    def _loaner_section(self) -> list:
        return [
            HTML(self._section_header(_("Issue loaner"))),
            Row(Column("loaner_asset", css_class="col-md-6"), Column("loaner_due_date", css_class="col-md-6")),
            HTML("</details>"),
        ]

    def _completion_section(self) -> list:
        section = [
            HTML(self._section_header(_("Complete repair"), open_section=True)),
            Row(Column("repair_action", css_class="col-md-12")),
        ]
        if self.can_dispose:
            section.append(Row(Column("dispose_original", css_class="col-md-12 mt-2")))
            section.append(
                Row(Column("disposal_method", css_class="col-md-6"), Column("disposal_date", css_class="col-md-6"))
            )
        section.append(HTML("</details>"))
        return section

    # ── validation and write ─────────────────────────────────────────────────
    def clean(self):
        data = super().clean()
        if not self.offer_loaner and data.get("loaner_asset"):
            raise forms.ValidationError(_("A loaner can only be issued for a repair."))
        action = data.get("repair_action") or REPAIR_ACTION_NONE
        if action != REPAIR_ACTION_NONE and not self.offer_completion:
            raise forms.ValidationError(_("There is no open loaner to close."))
        if not self.can_dispose and (data.get("dispose_original") or data.get("disposal_method")):
            raise forms.ValidationError(_("You are not allowed to dispose of an asset."))
        if self.can_dispose and data.get("dispose_original") and not data.get("disposal_method"):
            self.add_error("disposal_method", _("Choose a disposal method to start the disposal."))
        return data

    def save(self, commit=True):
        with transaction.atomic():
            maintenance = super().save(commit=commit)
            if commit:
                self._apply_repair_workflow(maintenance)
        return maintenance

    def _apply_repair_workflow(self, maintenance):
        """Issue the loaner and/or close the repair through the lifecycle services (#644).

        Both services write inside their own transaction; this method wraps them
        together with the maintenance write, so a refusal (unavailable or
        foreign-tenant loaner, no holder, no open loaner) rolls the whole form back
        and the view re-renders it with the reason.
        """
        if maintenance.maintenance_type != AssetMaintenance.MAINTENANCE_TYPE_REPAIR:
            return
        if self.offer_loaner and self.cleaned_data.get("loaner_asset") is not None:
            issue_repair_loaner(
                maintenance,
                self.cleaned_data["loaner_asset"],
                self.actor,
                due_date=self.cleaned_data.get("loaner_due_date") or maintenance.completion_date,
                request=self.request,
            )
        action = self.cleaned_data.get("repair_action") or REPAIR_ACTION_NONE
        if self.offer_completion and action != REPAIR_ACTION_NONE:
            complete_repair(
                maintenance,
                action,
                self.actor,
                request=self.request,
                disposal_method=self._submitted_disposal_method(),
                disposal_date=self.cleaned_data.get("disposal_date"),
            )

    def _submitted_disposal_method(self) -> str:
        """The disposal starts only when the operator asked for it and may dispose."""
        if not self.can_dispose or not self.cleaned_data.get("dispose_original"):
            return ""
        return self.cleaned_data.get("disposal_method") or ""


class CustodyTemplateForm(TenantScopedFormMixin, forms.ModelForm):
    # A template scopes to a tenant OR a tenant group (or is global): ``tenant``
    # stays optional and visible, so no requiredness and no single-tenant autoset.
    tenant_required = False
    tenant_autoset_when_single = False

    tenant = forms.ModelChoiceField(
        queryset=Tenant.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Tenant"),
    )
    tenant_group = forms.ModelChoiceField(
        queryset=TenantGroup.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Tenant Group"),
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Target Category"),
        help_text=_("Leave blank to use this template for every category without a more specific template."),
    )
    signature_provider = forms.ChoiceField(
        choices=[], widget=forms.Select(attrs={"class": "form-select"}), label=_("Signature Provider")
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Tags"),
    )

    class Meta:
        model = CustodyTemplate
        fields = [
            "tenant",
            "tenant_group",
            "name",
            "category",
            "signature_provider",
            "logo",
            "eula_text",
            "disclaimer",
            "qms_reference",
            "require_acceptance",
            "email_signature_request",
            "is_active",
            "tags",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "logo": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "eula_text": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
            "disclaimer": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "qms_reference": forms.TextInput(attrs={"class": "form-control"}),
            "require_acceptance": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "email_signature_request": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # autoset_when_single=False: this form scopes to a tenant OR a tenant
        # group (or global), so the tenant field must stay optional/visible —
        # auto-setting it would make the tenant-XOR-group clean() unsatisfiable.
        scope_tenant_field(self, autoset_when_single=False)
        scope_tenant_group_field(self)
        # Keep `tenant` optional: a template scopes to a tenant OR a group, or is
        # global, so forcing it required would make the tenant-XOR-group clean()
        # unsatisfiable. The global BaseForm patch (core/apps.py) already skips
        # forms that also declare `tenant_group`; this is the load-bearing guard.
        self.fields["tenant"].required = False
        self.fields["signature_provider"].choices = signature_providers.choices()

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        button_text = _("Update") if self.instance and self.instance.pk else _("Create")
        cancel_url = reverse("compliance:custodytemplate_list")

        self.helper.layout = Layout(
            Row(
                Column("tenant", css_class="col-md-4"),
                Column("tenant_group", css_class="col-md-4"),
                Column("name", css_class="col-md-4"),
                css_class="row g-3",
            ),
            Row(
                Column("category", css_class="col-md-6"),
                Column("signature_provider", css_class="col-md-6"),
                css_class="row g-3",
            ),
            Row(
                Column("qms_reference", css_class="col-md-6"),
                Column("is_active", css_class="col-md-6 mt-2"),
                css_class="row g-3",
            ),
            "logo",
            Fieldset(
                _("Content"),
                "eula_text",
                "disclaimer",
            ),
            Row(
                Column("require_acceptance", css_class="col-md-6 mt-2"),
                Column("email_signature_request", css_class="col-md-6 mt-2"),
                css_class="row g-3",
            ),
            "tags",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2">{_("Cancel")}</a>'),
            HTML("</div>"),
        )

    def clean(self):
        cleaned_data = super().clean()
        tenant = cleaned_data.get("tenant")
        tenant_group = cleaned_data.get("tenant_group")

        if tenant and tenant_group:
            raise forms.ValidationError(_("Choose either a Tenant or a Tenant Group for this template, not both."))

        from django.conf import settings

        if not getattr(settings, "ALLOW_GLOBAL_CUSTODY_TEMPLATES", True):
            if not tenant and not tenant_group:
                raise forms.ValidationError(
                    _("Global custody templates are disabled. You must select either a Tenant or a Tenant Group.")
                )

        return cleaned_data
