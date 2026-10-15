import json

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Fieldset, Layout
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from assets.customfields import resolve_asset_custom_fields, resolve_asset_type_custom_fields
from core.forms import CrispyFormMixin, TenantScopedFormMixin
from core.managers import Scope
from core.mixins import suppress_custom_field_data_validation
from extras.customfields import (
    build_custom_field_clear_form_field,
    clean_custom_field_form_values,
    custom_field_clear_key,
)
from extras.models import CustomField
from extras.services.vocabulary_translations import VocabularyTranslator
from organization.models import CostCenter, Location, Tenant
from procurement.models import PurchaseOrderLine

from ..models import Asset, AssetRole, AssetTagSequence, AssetType, StatusLabel, Supplier, Warranty
from ..models.choices import WarrantyTypeChoices
from ..services.specifications.commands import update_asset_specifications
from ..services.specifications.contracts import DestinationAssetTypeSelectionDTO
from ..specification_adapters import (
    actor_context_for_user,
    authorization_for_asset,
    current_specification_plan,
    native_persistence_fields,
    require_command_success,
    specification_patch,
)
from .assettype_form import (
    _DRAFT_PREFIX,
    _T15_UNSET,
    _build_t15_custom_field,
    _build_t15_presence_field,
    _display_t15_value,
    _history_entries,
)
from .fields import StatusModelChoiceField

HTML5_DATE_FORMAT = "%Y-%m-%d"

# Changing the asset type or the owning tenant re-derives the tag preview, the
# tenant-scoped FK choices and the dynamic custom fields, so both fields swap the
# whole form back in. The two widgets must carry the identical attribute set.
HTMX_RELOAD_ATTRS = {
    "hx-post": "",
    "hx-trigger": "change",
    "hx-target": "closest form",
    "hx-swap": "outerHTML",
    "hx-vals": '{"_reload": "1"}',
    "hx-include": "closest form",
}

# Quick-add buttons live inside the field label. The markup is a literal
# developer-controlled format string; every translated or dynamic value is
# passed to format_html() as an argument so it is escaped.
QUICK_ADD_LABEL_FORMAT = (
    '{} <button type="button" class="btn btn-link p-0 ms-1 align-baseline border-0 bg-transparent'
    ' text-primary quick-add-icon" title="{}" hx-get="{}"'
    ' hx-target="#modal-placeholder"><i class="mdi mdi-plus-circle-outline"></i></button>'
)


class AssetForm(TenantScopedFormMixin, CrispyFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    asset_type = forms.ModelChoiceField(
        queryset=AssetType.objects.select_related("manufacturer").all(),
        label=_("Asset Type"),
        required=True,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": "", **HTMX_RELOAD_ATTRS}),
    )
    asset_role = forms.ModelChoiceField(
        queryset=AssetRole.objects.all(),
        label=_("Asset Role"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    status = StatusModelChoiceField(
        queryset=StatusLabel.objects.all(),
        label=_("Status"),
        required=True,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    purchase_date = forms.DateField(
        widget=forms.DateInput(format=HTML5_DATE_FORMAT, attrs={"type": "date", "class": "form-control"}),
        required=False,
    )
    requestable = forms.ChoiceField(
        choices=[
            ("", _("Inherit from Asset Type (default)")),
            ("true", _("Yes, force requestable")),
            ("false", _("No, force not requestable")),
        ],
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Requestable Status"),
    )

    # Optional inline warranty (non-model fields). When the dates are filled in,
    # the view creates a Warranty for this asset via create_inline_warranty().
    warranty_supplier = forms.ModelChoiceField(
        label=_("Warranty Supplier"),
        required=False,
        queryset=Supplier.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    warranty_type = forms.ChoiceField(
        label=_("Warranty Type"),
        required=False,
        choices=[("", _("Select warranty type"))] + list(WarrantyTypeChoices.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    warranty_start_date = forms.DateField(
        label=_("Warranty Start Date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    warranty_end_date = forms.DateField(
        label=_("Warranty End Date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    warranty_cost = forms.DecimalField(
        label=_("Warranty Cost"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
    )

    class Meta:
        model = Asset
        fields = [
            "name",
            "asset_tag",
            "serial_number",
            "asset_type",
            "asset_role",
            "status",
            "location",
            "tenant",
            "purchase_date",
            "purchase_cost",
            "salvage_value",
            "currency",
            "order_number",
            "supplier",
            "purchase_order_line",
            "cost_center",
            "in_service_date",
            "depreciation_override",
            "notes",
            "tags",
            "requestable",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "asset_tag": forms.TextInput(
                attrs={"class": "form-control", "placeholder": _("Leave blank to generate automatically")}
            ),
            "serial_number": forms.TextInput(attrs={"class": "form-control"}),
            "purchase_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "salvage_value": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "currency": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "order_number": forms.TextInput(attrs={"class": "form-control"}),
            "in_service_date": forms.DateInput(
                format=HTML5_DATE_FORMAT,
                attrs={"class": "form-control", "type": "date"},
            ),
            "depreciation_override": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "tenant": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "tags": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
            "supplier": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "purchase_order_line": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "cost_center": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        }
        help_texts = {
            "depreciation_override": _(
                "Override the depreciation policy. Leave empty to use the tenant default or asset-type schedule."
            ),
        }

    def _post_clean(self):
        with suppress_custom_field_data_validation(self.instance):
            super()._post_clean()

    def clean_status(self):
        status = self.cleaned_data.get("status")
        if isinstance(status, str):
            from django.db.models import Q

            status_obj = StatusLabel.objects.filter(Q(slug=status) | Q(name__iexact=status)).first()
            if status_obj:
                return status_obj
            raise forms.ValidationError(_("Invalid status label: %(status)s") % {"status": status})
        return status

    def clean_requestable(self):
        val = self.cleaned_data.get("requestable")
        if val == "true":
            return True
        elif val == "false":
            return False
        return None

    def clean(self):
        cleaned_data = super().clean()
        for key in self.custom_field_keys:
            if cleaned_data.get(key) is _T15_UNSET:
                cleaned_data.pop(key, None)
        self._apply_t15_presence(cleaned_data)
        warranty_fields = (
            "warranty_supplier",
            "warranty_type",
            "warranty_start_date",
            "warranty_end_date",
            "warranty_cost",
        )
        any_filled = any(cleaned_data.get(f) for f in warranty_fields)
        if any_filled:
            start = cleaned_data.get("warranty_start_date")
            end = cleaned_data.get("warranty_end_date")
            if not start:
                self.add_error(
                    "warranty_start_date",
                    _("Start date is required when adding a warranty."),
                )
            if not end:
                self.add_error(
                    "warranty_end_date",
                    _("End date is required when adding a warranty."),
                )
            if start and end and end < start:
                self.add_error(
                    "warranty_end_date",
                    _("End date cannot be before the start date."),
                )
        return clean_custom_field_form_values(
            self,
            cleaned_data,
            self.custom_field_definitions,
            self.custom_field_clear_keys,
        )

    def create_inline_warranty(self, asset):
        cd = self.cleaned_data
        start = cd.get("warranty_start_date")
        end = cd.get("warranty_end_date")
        if not (start and end):
            return None

        kwargs = dict(
            asset=asset,
            start_date=start,
            end_date=end,
            supplier=cd.get("warranty_supplier"),
            cost=cd.get("warranty_cost"),
        )
        wt = cd.get("warranty_type")
        if wt:
            kwargs["warranty_type"] = wt
        return Warranty.objects.create(**kwargs)

    def __init__(self, *args, **kwargs):
        request = kwargs.pop("request", None)
        self.request = request
        explicit_initial = kwargs.get("initial") or {}
        super().__init__(*args, **kwargs)
        self.fields["warranty_supplier"].queryset = Supplier.objects.for_scope(Scope.current()).filter(is_active=True)
        self.fields["cost_center"].label_from_instance = lambda cost_center: (
            f"{cost_center.code}: {cost_center.name}" if cost_center.code else cost_center.name
        )
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        self._configure_requestable_initial()
        self._configure_quick_add_labels()
        self._configure_htmx_reload_widgets()

        asset_type_id = self._resolve_asset_type_id(request, explicit_initial)
        selected_asset_type = self._resolve_asset_type(asset_type_id)
        selected_tenant = self._resolve_selected_tenant()

        self._configure_tenant_scoped_querysets(selected_tenant)
        self._configure_asset_tag_sequence_help_text(selected_tenant, selected_asset_type)
        self._configure_default_asset_role(selected_asset_type)
        # Custom fields must exist before the layout pairs them into rows.
        self._configure_custom_fields(selected_asset_type)

        # Build the custom-field-aware layout after dynamic fields are known.

    def _resolve_asset_type_id(self, request, explicit_initial):
        """Return the raw asset-type ID the form should work from, or None.

        Precedence (#81): a bound ``asset_type`` key wins outright — an empty or
        malformed submitted value must *not* fall back to a lower-precedence
        source, form validation reports it instead. Otherwise the quick-add
        ``?asset_type=`` query value, then explicit ``initial``, then the
        instance. Never mutates ``data`` / ``initial`` / ``instance``.
        """
        if self.is_bound and "asset_type" in self.data:
            try:
                return int(self.data.get("asset_type"))
            except (ValueError, TypeError):
                return None

        if request and request.GET.get("asset_type"):
            try:
                return int(request.GET.get("asset_type"))
            except (ValueError, TypeError):
                return None

        if "asset_type" in explicit_initial:
            asset_type_val = explicit_initial.get("asset_type")
            if isinstance(asset_type_val, AssetType):
                return asset_type_val.pk
            return asset_type_val

        return self.instance.asset_type_id

    def _resolve_asset_type(self, asset_type_id):
        """Resolve the one AssetType driving preview, role default and custom fields."""
        if not asset_type_id:
            return None
        try:
            return AssetType.objects.get(pk=asset_type_id)
        except AssetType.DoesNotExist:
            return None

    def _resolve_selected_tenant(self):
        """Resolve the tenant the form is scoped to, or None.

        A *truthy* bound value wins; an empty one falls through to ``initial``
        (which ``scope_tenant_field()`` may have autoset) and then to the
        instance. An unresolvable value simply yields no selected tenant.
        """
        raw_tenant = None
        if self.is_bound and self.data.get("tenant"):
            raw_tenant = self.data.get("tenant")
        elif self.initial.get("tenant"):
            raw_tenant = self.initial.get("tenant")
        elif self.instance and self.instance.tenant:
            raw_tenant = self.instance.tenant

        if not raw_tenant:
            return None
        if isinstance(raw_tenant, Tenant):
            return raw_tenant
        try:
            return Tenant.objects.get(pk=raw_tenant)
        except (Tenant.DoesNotExist, ValueError, TypeError):
            return None

    def _configure_requestable_initial(self):
        """Map the saved tri-state ``requestable`` onto the select's choices."""
        if not (self.instance and self.instance.pk):
            return
        if self.instance.requestable is None:
            self.initial["requestable"] = ""
        elif self.instance.requestable is True:
            self.initial["requestable"] = "true"
        else:
            self.initial["requestable"] = "false"

    def _configure_quick_add_labels(self):
        """Put quick-add buttons inside the field labels instead of layout divs."""
        quick_add_fields = (
            ("asset_type", _("Asset Type"), _("Add new Asset Type"), "assets:assettype_create"),
            ("asset_role", _("Asset Role"), _("Add new Asset Role"), "assets:assetrole_create"),
            ("location", _("Location"), _("Add new Location"), "organization:location_create"),
        )
        for field_name, label, title, url_name in quick_add_fields:
            if field_name not in self.fields:
                continue
            self.fields[field_name].label = format_html(
                QUICK_ADD_LABEL_FORMAT,
                label,
                title,
                reverse(url_name) + "?_quickadd=1",
            )

    def _configure_htmx_reload_widgets(self):
        # The tenant picker reloads the form for the same reasons asset_type does.
        if "tenant" in self.fields:
            self.fields["tenant"].widget.attrs.update(HTMX_RELOAD_ATTRS)

    def _configure_tenant_scoped_querysets(self, selected_tenant):
        """Rescope tenant-owned FK choice fields per request.

        B2: their querysets are frozen at import time (no tenant context), so they
        would otherwise expose every tenant's Locations / CostCenters /
        PurchaseOrderLines and permit cross-tenant FK assignment. Re-evaluate
        through the scoping managers so choices are limited to the active tenant,
        narrowed to the selected tenant when one is resolvable. Asset.clean()
        validates the final selection as defence-in-depth.
        """
        tenant_scoped_fk_fields = {
            "location": Location,
            "cost_center": CostCenter,
            "purchase_order_line": PurchaseOrderLine,
        }
        for fk_field_name, fk_model in tenant_scoped_fk_fields.items():
            if fk_field_name in self.fields:
                fk_qs = fk_model.objects.all()
                if selected_tenant:
                    fk_qs = fk_qs.filter(tenant=selected_tenant)
                self.fields[fk_field_name].queryset = fk_qs

    def _configure_asset_tag_sequence_help_text(self, selected_tenant, selected_asset_type):
        """Preview the next tag for the selected scope in the asset_tag help text.

        The tag field deliberately stays optional: a blank tag is generated by
        ``Asset.save()`` from the resolved sequence, and the help text states
        that contract next to the suggestion (#503).

        Resolving does not consume a tag, but it does create the global default
        sequence when nothing else matches — a deliberately preserved side effect.
        """
        preview_asset = Asset(tenant=selected_tenant, asset_type=selected_asset_type)
        sequence = AssetTagSequence.resolve_sequence_for_asset(preview_asset)
        if sequence:
            suggested_tag = sequence.next_tag_preview
            help_text = format_html(
                '<span class="text-muted small">{} <a href="#" class="text-primary font-monospace"'
                ' data-fill-target="id_asset_tag" data-fill-value="{}">{}</a></span>',
                _("Leave blank to generate automatically. Suggested:"),
                suggested_tag,
                suggested_tag,
            )
        else:
            help_text = format_html(
                '<span class="text-muted small">{}</span>',
                _("No active tag sequence found for this scope."),
            )
        self.fields["asset_tag"].help_text = help_text

    def _configure_default_asset_role(self, selected_asset_type):
        """Seed a brand-new asset's role from its type; never overwrite a set one."""
        if self.instance.pk or selected_asset_type is None:
            return

        current_role = None
        if self.data and "asset_role" in self.data:
            current_role = self.data.get("asset_role")
        elif self.initial and "asset_role" in self.initial:
            current_role = self.initial.get("asset_role")

        if not current_role and selected_asset_type.asset_role:
            self.fields["asset_role"].initial = selected_asset_type.asset_role

    def _configure_custom_fields(self, selected_asset_type):
        """Attach target fields and retain one model/type presentation separately."""
        stored_values = self._stored_custom_values()

        self.custom_field_keys = []
        self.custom_field_definitions = {}
        self.custom_field_clear_keys = {}
        self.custom_field_presence_keys = {}
        resolved_fields = resolve_asset_custom_fields(selected_asset_type, stored_values)
        self.vocabulary_translator = VocabularyTranslator(active_locale=get_language()).load_resolved(
            resolved_fields, self._composed_fieldset_rows(selected_asset_type)
        )
        drafts = self._read_t15_draft_transport()
        for resolved in resolved_fields:
            field = resolved.definition
            field_key = f"cf_{field.name}"
            self.custom_field_keys.append(field_key)
            self.custom_field_definitions[field_key] = field
            initial_value = drafts.get(field.name, stored_values.get(field.name))
            form_field = _build_t15_custom_field(
                field,
                initial_value,
                has_stored_value=field.name in stored_values or field.name in drafts,
                read_only=resolved.read_only,
                translator=self.vocabulary_translator,
            )
            if form_field is None:
                continue
            self.fields[field_key] = form_field
            form_field.widget.attrs.update({"data-specification-key": field.name})
            if not form_field.disabled:
                clear_key = custom_field_clear_key(field.name)
                self.fields[clear_key] = build_custom_field_clear_form_field()
                self.custom_field_clear_keys[field_key] = clear_key
                if not field.required:
                    presence_key = f"{field_key}__presence"
                    self.fields[presence_key] = _build_t15_presence_field(field)
                    self.fields[presence_key].widget.attrs["data-specification-presence-for"] = field.name
                    self.custom_field_presence_keys[field_key] = presence_key

        self._inject_t15_drafts(drafts)
        self._configure_t15_draft_transport()
        self.helper.layout = self._build_t15_presentation(
            selected_asset_type,
            resolved_fields,
            reverse("assets:asset_list"),
        )

    def _stored_custom_values(self):
        # Explicit clones already carry copied values before they receive a PK.
        # Fresh Assets still start with their own empty map, never Type values.
        if not self.instance:
            return {}
        return dict(self.instance.custom_field_data or {})

    def _read_t15_draft_transport(self):
        if not self.is_bound:
            return {}
        drafts = {}
        for key in self.data.keys():
            if not key.startswith(_DRAFT_PREFIX):
                continue
            name = key[len(_DRAFT_PREFIX) :]
            try:
                drafts[name] = json.loads(self.data.get(key))
            except (TypeError, ValueError):
                continue
        return drafts

    def _posted_t15_drafts(self):
        drafts = self._read_t15_draft_transport()
        if not self.is_bound:
            return drafts
        for key in self.data.keys():
            if not key.startswith("cf_") or key.endswith(("__clear", "__presence")):
                continue
            values = self.data.getlist(key) if hasattr(self.data, "getlist") else [self.data.get(key)]
            drafts[key[3:]] = values if len(values) != 1 else values[0]
        return drafts

    def _inject_t15_drafts(self, drafts):
        if not self.is_bound or not drafts:
            return
        data = self.data.copy()
        for name, value in drafts.items():
            key = f"cf_{name}"
            if key in data or key not in self.custom_field_definitions:
                continue
            if isinstance(value, list):
                data.setlist(key, ["" if item is None else str(item) for item in value])
            elif value is None:
                data[key] = ""
            else:
                data[key] = str(value).lower() if isinstance(value, bool) else str(value)
            presence_key = f"{key}__presence"
            if value not in ("", None, []) and presence_key in self.fields and presence_key not in data:
                data[presence_key] = "value"
        self.data = data

    def _configure_t15_draft_transport(self):
        drafts = self._posted_t15_drafts()
        if not drafts:
            return
        data = self.data.copy() if self.is_bound else None
        for name, value in drafts.items():
            transport_key = f"{_DRAFT_PREFIX}{name}"
            if transport_key not in self.fields:
                self.fields[transport_key] = forms.CharField(
                    required=False,
                    widget=forms.HiddenInput(attrs={"data-specification-draft": name}),
                )
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            self.fields[transport_key].initial = encoded
            if data is not None:
                data[transport_key] = encoded
        if data is not None:
            self.data = data

    def _apply_t15_presence(self, cleaned_data):
        """Translate posted presence selectors into explicit patch values."""
        for key, presence_key in self.custom_field_presence_keys.items():
            # A missing optional selector is not an explicit omission of a
            # posted value. When both are absent, preserve the stored value;
            # Django's empty cleaned default is not a submitted patch.
            if self.is_bound and presence_key not in self.data:
                clear_key = self.custom_field_clear_keys.get(key)
                if key not in self.data and not (clear_key and cleaned_data.get(clear_key)):
                    cleaned_data.pop(key, None)
                continue
            mode = cleaned_data.get(presence_key, "")
            clear_key = self.custom_field_clear_keys.get(key)
            if clear_key and cleaned_data.get(clear_key) and mode in {"empty", "null", "value"}:
                self.add_error(presence_key, _("Choose either removal or an explicit value, not both."))
            if mode in {"", None}:
                cleaned_data.pop(key, None)
            elif mode == "empty":
                definition = self.custom_field_definitions[key]
                cleaned_data[key] = [] if definition.field_type == CustomField.FIELD_TYPE_MULTI_SELECT else ""
            elif mode == "null":
                cleaned_data[key] = None

    @staticmethod
    def _composed_fieldset_rows(selected_asset_type):
        memberships = getattr(selected_asset_type, "_prefetched_objects_cache", {}).get("fieldset_memberships", ())
        return [membership.fieldset for membership in memberships]

    def _asset_fieldset_context(self, selected_asset_type, resolved_fields):
        translator = self.vocabulary_translator
        by_identity = {}
        for fieldset in self._composed_fieldset_rows(selected_asset_type):
            identity = f"{fieldset.namespace}/{fieldset.slug}"
            by_identity[identity] = fieldset

        sections = []
        section_by_identity = {}
        used = set()
        for resolved in resolved_fields:
            key = f"cf_{resolved.definition.name}"
            if key not in self.fields or not resolved.provenance:
                continue
            identity = resolved.provenance[0]
            if identity not in section_by_identity:
                fieldset = by_identity.get(identity)
                if fieldset is not None:
                    label = translator.fieldset_label(fieldset)
                    description = translator.fieldset_description(fieldset)
                else:
                    label, description = identity.rsplit("/", 1)[-1], ""
                source = self._fieldset_source(fieldset) if fieldset is not None else identity.split("/", 1)[0]
                section_by_identity[identity] = {
                    "id": getattr(fieldset, "pk", identity),
                    "identity": identity,
                    "label": label,
                    "description": description,
                    "source": source,
                    "fields": [],
                }
                sections.append(section_by_identity[identity])
            if key in used:
                continue
            used.add(key)
            section_by_identity[identity]["fields"].append(self._t15_asset_field_context(key, resolved))

        additional = [
            self._t15_asset_field_context(f"cf_{resolved.definition.name}", resolved)
            for resolved in resolved_fields
            if f"cf_{resolved.definition.name}" in self.fields and f"cf_{resolved.definition.name}" not in used
        ]
        if additional:
            sections.append(
                {
                    "id": "additional",
                    "identity": "additional",
                    "label": _("Additional specifications"),
                    "description": _("Global specifications and values retained from an earlier composition."),
                    "source": _("Global"),
                    "fields": additional,
                }
            )
        return sections

    def _t15_asset_field_context(self, key, resolved):
        return {
            "key": key[3:],
            "bound": self[key],
            "presence": self[self.custom_field_presence_keys[key]] if key in self.custom_field_presence_keys else None,
            "clear": self[self.custom_field_clear_keys[key]] if key in self.custom_field_clear_keys else None,
            "definition": resolved.definition,
            "provenance": resolved.provenance,
        }

    def _fieldset_source(self, fieldset):
        if fieldset is None:
            return "Local"
        return str(getattr(fieldset, "management_kind", "local")).capitalize()

    def _build_t15_model_specification_fields(self, selected_asset_type):
        """Build the read-only Asset Type values shown beside the editor."""
        if selected_asset_type is None:
            return []

        model_values = dict(selected_asset_type.custom_field_data or {})
        translator = self.vocabulary_translator
        model_fields = resolve_asset_type_custom_fields(selected_asset_type)
        translator.load_resolved(model_fields)
        fields = []
        for resolved in model_fields:
            definition = resolved.definition
            value = model_values.get(definition.name)
            has_value = definition.name in model_values
            fields.append(
                {
                    "key": definition.name,
                    "label": translator.field_label(definition),
                    "definition": definition,
                    "value": value,
                    "display_value": _display_t15_value(definition, value, translator) if has_value else _("Not set"),
                    "model_value_json": json.dumps(value, ensure_ascii=False),
                    "model_value_script_id": f"asset-model-value-{definition.name}",
                    "can_copy": definition.name in self.custom_field_definitions,
                }
            )
        return fields

    def _build_t15_presentation(self, selected_asset_type, resolved_fields, cancel_url):
        stored_values = self._stored_custom_values()
        self.specification_sections = self._asset_fieldset_context(selected_asset_type, resolved_fields)
        self.specification_history = _history_entries(
            stored_values, self.custom_field_definitions, self.vocabulary_translator
        )
        self.specification_definition_revision = ""
        if selected_asset_type is not None:
            try:
                owner = self.instance if self.instance and self.instance.pk else Asset(asset_type=selected_asset_type)
                self.specification_definition_revision = current_specification_plan(
                    owner,
                    target_kind="asset",
                    asset_type_id=selected_asset_type.pk,
                ).definition_revision
            except (ValidationError, AttributeError):
                self.specification_definition_revision = ""

        self.model_specification_fields = self._build_t15_model_specification_fields(selected_asset_type)

        # Grouped, standardized section order: Identity -> Classification ->
        # Assignment -> Procurement & Financial -> Lifecycle -> Custom -> Notes.
        layout_elements = [
            Fieldset(
                _("Identity"),
                Div(Div("name", css_class="col-md-6"), Div("asset_tag", css_class="col-md-6"), css_class="row"),
                Div(Div("serial_number", css_class="col-md-6"), Div("status", css_class="col-md-6"), css_class="row"),
            ),
            Fieldset(
                _("Classification"),
                Div(Div("asset_type", css_class="col-md-6"), Div("asset_role", css_class="col-md-6"), css_class="row"),
            ),
            Fieldset(
                _("Assignment"),
                Div(Div("location", css_class="col-md-6"), Div("tenant", css_class="col-md-6"), css_class="row"),
            ),
            Fieldset(
                _("Procurement & Financial"),
                Div(
                    Div("purchase_date", css_class="col-md-4"),
                    Div("order_number", css_class="col-md-4"),
                    Div("supplier", css_class="col-md-4"),
                    css_class="row",
                ),
                Div(Div("purchase_order_line", css_class="col-md-6"), css_class="row"),
                Div(
                    Div("purchase_cost", css_class="col-md-4"),
                    Div("currency", css_class="col-md-4"),
                    Div("salvage_value", css_class="col-md-4"),
                    css_class="row",
                ),
                Div(Div("cost_center", css_class="col-md-6"), css_class="row"),
            ),
            Fieldset(
                _("Lifecycle"),
                Div(
                    Div("in_service_date", css_class="col-md-6"),
                    Div("depreciation_override", css_class="col-md-6"),
                    css_class="row",
                ),
            ),
        ]

        if self.custom_field_keys:
            cf_divs = []
            for i in range(0, len(self.custom_field_keys), 2):
                chunk = self.custom_field_keys[i : i + 2]
                row_cols = []
                for key in chunk:
                    clear_key = self.custom_field_clear_keys.get(key)
                    fields = [key]
                    if clear_key:
                        fields.append(clear_key)
                    row_cols.append(Div(*fields, css_class="col-md-6"))
                cf_divs.append(Div(*row_cols, css_class="row"))
            layout_elements.append(Fieldset(_("Custom Specifications"), *cf_divs, css_class="mb-4 border p-3 rounded"))

        layout_elements.append(
            Fieldset(
                _("Optional: Warranty"),
                HTML(
                    format_html(
                        '<p class="text-muted small">{}</p>',
                        _("Fill in to create a warranty for this asset; leave blank to skip."),
                    )
                ),
                Div(
                    Div("warranty_supplier", css_class="col-md-6"),
                    Div("warranty_type", css_class="col-md-6"),
                    css_class="row",
                ),
                Div(
                    Div("warranty_start_date", css_class="col-md-4"),
                    Div("warranty_end_date", css_class="col-md-4"),
                    Div("warranty_cost", css_class="col-md-4"),
                    css_class="row",
                ),
            )
        )

        layout_elements.append(
            Fieldset(
                _("Notes & Tags"),
                Div(Div("tags", css_class="col-md-6"), Div("requestable", css_class="col-md-6"), css_class="row"),
                "notes",
            )
        )

        layout_elements.extend(self.action_buttons(cancel_url))

        return Layout(*layout_elements)

    def save(self, commit=True):
        # ModelForm.save(commit=False) is intentionally used without the old
        # custom-field merge.  The command below is the only value/type-switch
        # writer; native fields are saved separately so a stale form instance
        # cannot overwrite command-owned state.
        # is_valid() has already copied the submitted Type onto instance.
        # Deferred native persistence must retain the stored Type, not that
        # mutated in-memory value; only save_m2m may execute the switch.
        previous_type_id = None
        if not commit and self.instance.pk:
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            previous_type_id = Asset._base_manager.values_list("asset_type_id", flat=True).get(pk=self.instance.pk)
        instance = forms.ModelForm.save(self, commit=False)
        self._native_save_m2m = getattr(self, "save_m2m", None)
        self._pending_target_asset_type_id = instance.asset_type_id
        self._pending_create = not bool(instance.pk)
        if not commit:
            # A caller following Django's commit=False lifecycle must not be
            # able to persist a type switch through instance.save().  The
            # pending command runs from save_m2m after the caller has saved
            # native fields.
            instance.asset_type_id = previous_type_id
            self.save_m2m = self._save_pending_m2m
            return instance

        actor = actor_context_for_user(getattr(self.request, "user", None))
        with transaction.atomic():
            if instance.pk:
                # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
                # ambient scope
                current = Asset._base_manager.get(pk=instance.pk)
                self._apply_specification_command(current, actor)
                current = self._persist_native_update(instance)
            else:
                current = self._persist_native_create(instance)
                self._apply_specification_command(current, actor)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            self.instance = Asset._base_manager.get(pk=current.pk)
            if self._native_save_m2m is not None:
                self._native_save_m2m()
        return self.instance

    def _specification_patch(self):
        return specification_patch(
            definitions=self.custom_field_definitions,
            cleaned_values=self.cleaned_data,
            fields=self.fields,
            clear_keys=self.custom_field_clear_keys,
        )

    def _native_field_names(self):
        concrete_names = {field.name for field in Asset._meta.concrete_fields}
        return tuple(
            name
            for name in self.changed_data
            if name in concrete_names and name not in {"asset_type", "custom_field_data"}
        )

    def _persist_native_update(self, instance):
        # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or ambient scope
        current = Asset._base_manager.get(pk=instance.pk)
        field_names = self._native_field_names()
        for field_name in field_names:
            field = Asset._meta.get_field(field_name)
            setattr(current, field.attname, getattr(instance, field.attname))
        if field_names:
            current.save(update_fields=native_persistence_fields(current, field_names))
        return current

    def _persist_native_create(self, instance):
        # The empty specification object is not a user value.  Suppress only
        # dynamic custom-field validation for this initial native row; the
        # canonical command immediately validates and writes the requested
        # patch/type destination inside the same outer transaction.
        instance.custom_field_data = {}
        with suppress_custom_field_data_validation(instance):
            instance.save()
        return instance

    def _apply_specification_command(self, current, actor):
        target_type_id = self._pending_target_asset_type_id
        patch = self._specification_patch()
        if target_type_id == current.asset_type_id and not patch.set_values and not patch.clear_keys:
            return
        if target_type_id is None:
            raise ValidationError("An Asset Type is required for specification changes.")
        authorization = authorization_for_asset(
            user=getattr(self.request, "user", None),
            tenant_id=current.tenant_id,
        )
        plan = current_specification_plan(current, target_kind="asset", asset_type_id=target_type_id)
        result = update_asset_specifications(
            authorization=authorization,
            asset_id=current.pk,
            destination=DestinationAssetTypeSelectionDTO(
                presence="replace",
                asset_type_id=int(target_type_id),
            ),
            expected_resource_revision=plan.resource_revision,
            expected_definition_revision=plan.definition_revision,
            patch=patch,
        )
        require_command_success(result)

    def _save_pending_m2m(self):
        with transaction.atomic():
            if self._native_save_m2m is not None:
                self._native_save_m2m()
            if not self.instance.pk:
                return
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            current = Asset._base_manager.get(pk=self.instance.pk)
            actor = actor_context_for_user(getattr(self.request, "user", None))
            self._apply_specification_command(current, actor)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            self.instance = Asset._base_manager.get(pk=current.pk)
