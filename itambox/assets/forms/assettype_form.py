import json

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Fieldset, Layout, Row, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from assets.customfields import resolve_effective_custom_fields
from core.forms import SlugModelForm, TenantScopedFormMixin, cancel_button
from extras.customfields import (
    CustomFieldModelFormMixin,
    build_custom_field_form_field,
    clean_custom_field_form_values,
    validate_custom_field_value,
)
from extras.models import CustomField, CustomFieldset, CustomFieldsetField, Tag
from extras.services.vocabulary_translations import BASE_LABELS, VocabularyTranslator

from ..models import AssetRole, AssetType, Category, Manufacturer
from ..services.specifications.commands import (
    apply_category_defaults,
    create_asset_type,
    preview_apply_category_defaults,
    preview_asset_type_create,
    set_asset_type_composition,
    update_asset_type_specifications,
)
from ..services.specifications.contracts import ExplicitFieldsetSelectionDTO
from ..services.specifications.messages import specification_message
from ..specification_adapters import (
    actor_context_for_user,
    create_fieldset_selection,
    current_specification_plan,
    discard_staged_image,
    native_asset_type_create_input,
    native_persistence_fields,
    owner_id_from_result,
    prospective_specification_plan,
    require_command_success,
    specification_patch,
    stage_uploaded_image,
)

_T15_UNSET = object()
_DRAFT_PREFIX = "specification_draft__cf_"


def _choice_rows(definition):
    choice_set = getattr(definition, "choice_set", None)
    if choice_set is None:
        return ()
    relation = getattr(choice_set, "choices", ())
    rows = relation.all() if callable(getattr(relation, "all", None)) else relation
    return tuple(sorted(rows, key=lambda row: (getattr(row, "position", 0), getattr(row, "key", ""))))


def _choice_options(definition, initial_value, translator=BASE_LABELS):
    stored = set(initial_value if isinstance(initial_value, (list, tuple)) else [initial_value])
    options = []
    for choice in _choice_rows(definition):
        if choice.lifecycle == CustomField.LIFECYCLE_ACTIVE or choice.key in stored:
            label = translator.choice_label(choice)
            if choice.lifecycle == CustomField.LIFECYCLE_DEPRECATED:
                label = f"{label} ({str(_('No longer offered'))})"
            options.append((choice.key, label))
    return options


def _coerce_t15_boolean(value):
    if value == "":
        return _T15_UNSET
    if value == "__null__":
        return None
    return str(value).casefold() == "true"


def _build_t15_custom_field(
    definition, initial_value=None, *, has_stored_value=False, read_only=False, translator=BASE_LABELS
):
    label = translator.field_label(definition)
    help_text = translator.field_help_text(definition)
    if definition.field_type == CustomField.FIELD_TYPE_BOOLEAN and (definition.required or definition.nullable):
        # Required booleans use an explicit two-choice control: Django's
        # BooleanField treats false as missing when required=True.  Keep that
        # distinction so required false is a valid persisted value while
        # omission remains invalid. Nullable booleans also need the explicit
        # null option, which a checkbox cannot represent.
        # The empty first choice makes the HTML control required as well;
        # without it browsers silently select true for an unset required value.
        choices = (("", _("Unset")), ("true", _("Yes")), ("false", _("No")))
        if definition.nullable:
            choices = (*choices, ("__null__", _("Explicit null")))
        if has_stored_value:
            if initial_value is None:
                initial = "__null__" if definition.nullable else ""
            else:
                initial = "true" if initial_value is True else "false"
        else:
            initial = ""
        return forms.TypedChoiceField(
            choices=choices,
            coerce=_coerce_t15_boolean,
            label=label,
            help_text=help_text,
            required=definition.required,
            initial=initial,
            disabled=read_only,
            widget=forms.Select(attrs={"class": "form-select", "data-specification-input": "1"}),
        )

    if definition.field_type == CustomField.FIELD_TYPE_SINGLE_SELECT:
        return forms.ChoiceField(
            choices=[("", "---------"), *_choice_options(definition, initial_value, translator)],
            label=label,
            help_text=help_text,
            required=definition.required,
            initial=initial_value,
            disabled=read_only,
            widget=forms.Select(attrs={"class": "form-select", "data-specification-input": "1"}),
        )

    if definition.field_type == CustomField.FIELD_TYPE_MULTI_SELECT:
        return forms.MultipleChoiceField(
            choices=_choice_options(definition, initial_value, translator),
            label=label,
            help_text=help_text,
            required=definition.required,
            initial=initial_value,
            disabled=read_only,
            widget=forms.SelectMultiple(attrs={"class": "form-select", "data-specification-input": "1"}),
        )

    field = build_custom_field_form_field(definition, initial_value, read_only=read_only)
    if field is not None:
        field.label = label
        field.help_text = help_text
        field.widget.attrs.update({"data-specification-input": "1"})
    return field


def _build_t15_presence_field(definition):
    choices = [("", _("Leave unchanged")), ("value", _("Set value"))]
    if definition.field_type in {CustomField.FIELD_TYPE_TEXT, CustomField.FIELD_TYPE_MULTI_SELECT}:
        choices.append(
            (
                "empty",
                _("Set explicit empty text")
                if definition.field_type == CustomField.FIELD_TYPE_TEXT
                else _("Set empty selection"),
            )
        )
    if definition.nullable:
        choices.append(("null", _("Set explicit null")))
    return forms.ChoiceField(
        choices=choices,
        required=False,
        label=_("Presence"),
        help_text=_("Choose whether this draft value is omitted, explicit, empty, or null."),
        widget=forms.Select(attrs={"class": "form-select form-select-sm", "data-specification-presence": "1"}),
        initial="",
    )


def _display_t15_value(definition, value, translator=BASE_LABELS):
    if value is None:
        return "null"
    if definition.field_type == CustomField.FIELD_TYPE_BOOLEAN:
        return str(_("Yes")) if value is True else str(_("No"))
    options = {choice.key: translator.choice_label(choice) for choice in _choice_rows(definition)}
    if isinstance(value, (list, tuple)):
        return ", ".join(options.get(item, str(item)) for item in value)
    return options.get(value, str(value))


def _history_entries(stored_values, current_definitions, translator=BASE_LABELS):
    if not stored_values:
        return []
    historical = CustomField.objects.filter(name__in=stored_values).prefetch_related("choice_set__choices")
    historical_by_name = {field.name: field for field in historical}
    translator.load(fields=list(historical_by_name.values()))
    entries = []
    for name, value in stored_values.items():
        definition = current_definitions.get(f"cf_{name}") or historical_by_name.get(name)
        if definition is None:
            entries.append(
                {
                    "key": name,
                    "label": name,
                    "display_value": str(value),
                    "state": "unknown",
                    "reasons": (_("Unknown definition"),),
                }
            )
            continue
        current = f"cf_{name}" in current_definitions
        reasons = []
        if not current:
            reasons.append(_("Inactive composition"))
        if definition.lifecycle == CustomField.LIFECYCLE_DEPRECATED:
            reasons.append(_("Deprecated field"))
        choice_keys = {
            choice.key for choice in _choice_rows(definition) if choice.lifecycle == CustomField.LIFECYCLE_DEPRECATED
        }
        values = value if isinstance(value, (list, tuple)) else (value,)
        if any(item in choice_keys for item in values):
            reasons.append(_("Deprecated choice"))
        state = "historical" if reasons else "current"
        try:
            validate_custom_field_value(definition, value)
        except ValidationError:
            state = "invalid"
            reasons.append(_("Invalid stored value"))
        entries.append(
            {
                "key": name,
                "label": translator.field_label(definition),
                "display_value": _display_t15_value(definition, value, translator),
                "state": state,
                "reasons": tuple(reasons),
            }
        )
    return entries


class AssetTypeForm(TenantScopedFormMixin, CustomFieldModelFormMixin, SlugModelForm):
    manufacturer = forms.ModelChoiceField(
        queryset=Manufacturer.objects.all(),
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    asset_role = forms.ModelChoiceField(
        queryset=AssetRole.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        label=_("Asset Role"),
    )
    custom_fieldsets = forms.ModelMultipleChoiceField(
        queryset=CustomFieldset.objects.all(),
        required=False,
        widget=forms.MultipleHiddenInput(attrs={"data-specification-fieldsets": "1"}),
        label=_("Specification fieldsets"),
    )
    specification_fieldsets_presence = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-specification-fieldsets-presence": "1"}),
    )
    expected_resource_revision = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-specification-resource-revision": "1"}),
    )
    expected_definition_revision = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-specification-definition-revision": "1"}),
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(
            attrs={"class": "form-select", "data-tomselect-tags": "true", "data-tom-select": ""}
        ),
        label=_("Tags"),
    )

    class Meta:
        model = AssetType
        fields = [
            "manufacturer",
            "part_number",
            "ean",
            "model",
            "slug",
            "eol_months",
            "category",
            "asset_role",
            "custom_fieldsets",
            "depreciation",
            "image",
            "description",
            "comments",
            "tags",
            "requestable",
        ]
        widgets = {
            "model": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control", "slugify": "model"}),
            "part_number": forms.TextInput(attrs={"class": "form-control"}),
            "ean": forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric"}),
            "eol_months": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "category": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "depreciation": forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
            "image": forms.FileInput(attrs={"class": "form-control", "style": "max-width: 400px;"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "comments": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
        }
        help_texts = {
            "slug": _("URL-friendly identifier. Leave blank to auto-generate."),
            "ean": _("Barcode (EAN, UPC, or GTIN). Scan this barcode to view assets of this type."),
        }

    @staticmethod
    def _category_default_fieldset_ids(category_id):
        if not category_id:
            return []
        return list(
            Category.objects.filter(pk=category_id, default_fieldset_memberships__isnull=False)
            .values_list("default_fieldset_memberships__fieldset_id", flat=True)
            .order_by("default_fieldset_memberships__position")
        )

    def _normalize_omitted_fieldset_data(self, kwargs):
        data = kwargs.get("data")
        if data is None or data.get("specification_fieldsets_presence") != "omitted":
            return
        normalized = data.copy()
        category_id = normalized.get("category")
        normalized.setlist(
            "custom_fieldsets",
            [str(fieldset_id) for fieldset_id in self._category_default_fieldset_ids(category_id)],
        )
        kwargs["data"] = normalized

    def _raw_selected_fieldset_ids(self):
        cached = getattr(self, "_raw_selected_fieldset_ids_cache", None)
        if cached is not None:
            return list(cached)
        ids = self._calculate_raw_selected_fieldset_ids()
        self._raw_selected_fieldset_ids_cache = tuple(ids)
        return list(ids)

    def _calculate_raw_selected_fieldset_ids(self):
        if self.is_bound:
            if self.data.get("specification_fieldsets_presence") == "omitted" and "custom_fieldsets" not in self.data:
                return self._category_default_fieldset_ids(self.data.get("category"))
            if hasattr(self.data, "getlist"):
                values = self.data.getlist("custom_fieldsets")
            else:
                values = self.data.get("custom_fieldsets", [])
                if not isinstance(values, (list, tuple)):
                    values = [values]
            return [int(value) for value in values if str(value).isdigit()]
        if self.instance and self.instance.pk:
            return list(self.instance.fieldset_memberships.order_by("position").values_list("fieldset_id", flat=True))
        if not self._custom_fieldsets_explicit:
            category_id = getattr(self._draft_category, "pk", self._draft_category)
            return self._category_default_fieldset_ids(category_id)
        initial = self.initial.get("custom_fieldsets", [])
        if hasattr(initial, "values_list"):
            return list(initial.values_list("pk", flat=True))
        return [getattr(value, "pk", value) for value in initial]

    def _selected_fieldsets(self):
        cached = getattr(self, "_selected_fieldsets_cache", None)
        if cached is not None:
            return cached
        ids = self._raw_selected_fieldset_ids()
        by_id = (
            CustomFieldset.objects.filter(pk__in=ids)
            .prefetch_related(
                "field_memberships__custom_field__object_types",
                "field_memberships__custom_field__choice_set__choices",
            )
            .in_bulk(ids)
        )
        selected = [by_id[fieldset_id] for fieldset_id in ids if fieldset_id in by_id]
        self._selected_fieldsets_cache = selected
        return selected

    def clean_custom_fieldsets(self):
        fieldsets = self.cleaned_data["custom_fieldsets"]
        raw_ids = self._raw_selected_fieldset_ids()
        if len(raw_ids) != len(set(raw_ids)):
            raise forms.ValidationError(_("Each specification fieldset may only be selected once."))
        return fieldsets

    def get_custom_field_definitions(self):
        stored = dict(self.instance.custom_field_data or {}) if self.instance and self.instance.pk else {}
        return resolve_effective_custom_fields(
            self._selected_fieldsets(),
            "assettype",
            stored,
        )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        supplied_initial = kwargs.get("initial") or {}
        self._custom_fieldsets_explicit = "custom_fieldsets" in supplied_initial
        self._draft_category = supplied_initial.get("category")
        self._normalize_omitted_fieldset_data(kwargs)
        model_instance = kwargs.get("instance")
        instance_prefetch_cache = None
        instance_prefetch_previous = _T15_UNSET
        instance_prefetch_created = False
        if model_instance is not None and model_instance.pk:
            instance_prefetch_cache = getattr(model_instance, "_prefetched_objects_cache", None)
            if instance_prefetch_cache is None:
                instance_prefetch_cache = {}
                model_instance._prefetched_objects_cache = instance_prefetch_cache
                instance_prefetch_created = True
            instance_prefetch_previous = instance_prefetch_cache.get("custom_fieldsets", _T15_UNSET)
            instance_prefetch_cache["custom_fieldsets"] = CustomFieldset.objects.none()
        try:
            super().__init__(*args, **kwargs)
        finally:
            if instance_prefetch_cache is not None:
                if instance_prefetch_previous is _T15_UNSET:
                    instance_prefetch_cache.pop("custom_fieldsets", None)
                else:
                    instance_prefetch_cache["custom_fieldsets"] = instance_prefetch_previous
                if instance_prefetch_created and not instance_prefetch_cache:
                    delattr(model_instance, "_prefetched_objects_cache")
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.fields["slug"].widget.attrs["slugify"] = "model"
        self.fields["category"].widget.attrs.update(
            {
                "data-specification-category": "1",
                "hx-post": "",
                "hx-trigger": "change",
                "hx-target": "closest form",
                "hx-swap": "outerHTML",
                "hx-include": "closest form",
                "hx-vals": '{"_reload": "1"}',
            }
        )
        self.fields["custom_fieldsets"].initial = self._raw_selected_fieldset_ids()
        if self.is_bound:
            self.fields["specification_fieldsets_presence"].initial = self.data.get(
                "specification_fieldsets_presence", "omitted"
            )
        else:
            self.fields["specification_fieldsets_presence"].initial = (
                "explicit" if self._custom_fieldsets_explicit or self.instance.pk else "omitted"
            )
        self.vocabulary_translator = VocabularyTranslator(active_locale=get_language())
        self.vocabulary_translator.load(
            fields=list(self.custom_field_definitions.values()), fieldsets=self._selected_fieldsets()
        )
        self._configure_t15_custom_fields()
        self._configure_t15_draft_transport()
        self._build_t15_presentation()

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = self.instance.get_absolute_url() if self.instance.pk else reverse("assets:assettype_list")
        layout_elements = [
            Fieldset(
                _("General Information"),
                Row(Column("manufacturer", css_class="col-md-6"), Column("model", css_class="col-md-6")),
                Row(
                    Column("part_number", css_class="col-md-3"),
                    Column("ean", css_class="col-md-3"),
                    Column("slug", css_class="col-md-3"),
                    Column("eol_months", css_class="col-md-3"),
                ),
                Row(Column("image", css_class="col-md-6"), Column("description", css_class="col-md-6")),
            ),
            Fieldset(
                _("Classification & Financial"),
                Row(
                    Column("category", css_class="col-md-3"),
                    Column("asset_role", css_class="col-md-3"),
                    Column("custom_fieldsets", css_class="col-md-3"),
                    Column("depreciation", css_class="col-md-3"),
                ),
                Row(Column("requestable", css_class="col-md-4 mt-4")),
            ),
        ]
        if self.custom_field_keys:
            rows = []
            for index in range(0, len(self.custom_field_keys), 2):
                columns = []
                for key in self.custom_field_keys[index : index + 2]:
                    fields = [key]
                    clear_key = self.custom_field_clear_keys.get(key)
                    if clear_key:
                        fields.append(clear_key)
                    columns.append(Column(*fields, css_class="col-md-6"))
                rows.append(Row(*columns))
            layout_elements.append(Fieldset(_("Specifications"), *rows))
        else:
            layout_elements.append(
                Fieldset(
                    _("Specifications"),
                    HTML(
                        '<div class="alert alert-info d-flex align-items-center mb-0" role="alert">'
                        '  <i class="mdi mdi-information-outline me-2"></i>'
                        "  <div>Select specification fieldsets to add specifications.</div>"
                        "</div>"
                    ),
                )
            )
        layout_elements.extend(
            [
                Fieldset(_("Additional Information"), "comments", Row(Column("tags", css_class="col-md-8"))),
                HTML('<div class="mt-3">'),
                Submit("submit", button_text, css_class="btn btn-primary"),
                cancel_button(cancel_url, dismiss=self.quick_add_dismiss),
                HTML("</div>"),
            ]
        )
        self.helper.layout = Layout(*layout_elements)

    def _stored_custom_values(self):
        if not self.instance or not self.instance.pk:
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

    def _configure_t15_custom_fields(self):
        stored = self._stored_custom_values()
        drafts = self._read_t15_draft_transport()
        self.custom_field_presence_keys = {}
        for key, definition in self.custom_field_definitions.items():
            if key not in self.fields:
                continue
            name = getattr(definition, "name", key.removeprefix("cf_"))
            value = drafts.get(name, stored.get(name))
            field = _build_t15_custom_field(
                definition,
                value,
                has_stored_value=name in stored or name in drafts,
                read_only=self.fields[key].disabled,
                translator=self.vocabulary_translator,
            )
            self.fields[key] = field
            field.widget.attrs.update({"data-specification-key": key[3:]})
            if not field.disabled and not definition.required:
                presence_key = f"{key}__presence"
                self.fields[presence_key] = _build_t15_presence_field(definition)
                self.fields[presence_key].widget.attrs["data-specification-presence-for"] = key[3:]
                self.custom_field_presence_keys[key] = presence_key
            clear_key = self.custom_field_clear_keys.get(key)
            if clear_key and clear_key in self.fields:
                self.fields[clear_key].widget.attrs["data-specification-clear-for"] = key[3:]
        self._inject_t15_drafts(drafts)

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
        for key, presence_key in self.custom_field_presence_keys.items():
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
                cleaned_data[key] = (
                    [] if self.custom_field_definitions[key].field_type == CustomField.FIELD_TYPE_MULTI_SELECT else ""
                )
            elif mode == "null":
                cleaned_data[key] = None

    def clean(self):
        cleaned_data = forms.ModelForm.clean(self)
        for key in self.custom_field_keys:
            if cleaned_data.get(key) is _T15_UNSET:
                cleaned_data.pop(key, None)
        self._apply_t15_presence(cleaned_data)
        return clean_custom_field_form_values(
            self,
            cleaned_data,
            self.custom_field_definitions,
            self.custom_field_clear_keys,
        )

    def _fieldset_source(self, fieldset):
        return str(getattr(fieldset, "management_kind", "local")).capitalize()

    def _build_t15_presentation(self):
        selected = self._selected_fieldsets()
        current = set(self.custom_field_definitions)
        used = set()
        sections = []
        for fieldset in selected:
            fields = []
            memberships = fieldset.field_memberships.all()
            for membership in memberships:
                key = f"cf_{membership.custom_field.name}"
                if key not in current or key in used:
                    continue
                used.add(key)
                fields.append(self._t15_field_context(key))
            if fields:
                sections.append(
                    {
                        "id": fieldset.pk,
                        "identity": f"{fieldset.namespace}/{fieldset.slug}",
                        "label": self.vocabulary_translator.fieldset_label(fieldset),
                        "description": self.vocabulary_translator.fieldset_description(fieldset),
                        "source": self._fieldset_source(fieldset),
                        "fields": fields,
                    }
                )
        remaining = [self._t15_field_context(key) for key in self.custom_field_keys if key not in used]
        if remaining:
            sections.append(
                {
                    "id": "additional",
                    "identity": "additional",
                    "label": _("Additional specifications"),
                    "description": _("Global specifications and values retained from an earlier composition."),
                    "source": _("Global"),
                    "fields": remaining,
                }
            )
        self.specification_sections = sections
        self.specification_history = _history_entries(
            self._stored_custom_values(), self.custom_field_definitions, self.vocabulary_translator
        )
        self.specification_fieldset_options = self._fieldset_options(selected)
        self._set_render_preconditions(selected)

    def _is_t15_preview_request(self):
        return not self.is_bound or bool(
            self.request and self.request.headers.get("HX-Request") and self.data.get("_reload") == "1"
        )

    def _set_render_preconditions(self, selected):
        if not self.instance or not self.instance.pk:
            self.specification_definition_revision = ""
            return
        if self._is_t15_preview_request():
            plan = prospective_specification_plan(
                self.instance,
                target_kind="asset_type",
                fieldset_identities=tuple(f"{fieldset.namespace}/{fieldset.slug}" for fieldset in selected),
            )
            resource_revision = str(plan.resource_revision)
            definition_revision = str(plan.definition_revision)
            self.fields["expected_resource_revision"].initial = resource_revision
            self.fields["expected_definition_revision"].initial = definition_revision
            if self.is_bound:
                data = self.data.copy()
                data["expected_resource_revision"] = resource_revision
                data["expected_definition_revision"] = definition_revision
                self.data = data
            self.specification_definition_revision = definition_revision
            return
        self.specification_definition_revision = self.data.get("expected_definition_revision", "")

    def _submitted_preconditions(self):
        resource_revision = self.cleaned_data.get("expected_resource_revision", "")
        definition_revision = self.cleaned_data.get("expected_definition_revision", "")
        if not resource_revision or not definition_revision:
            raise ValidationError(
                _("Refresh the specification preview before submitting this form."),
                code="missing_specification_precondition",
            )
        return resource_revision, definition_revision

    def _t15_field_context(self, key):
        return {
            "key": key[3:],
            "bound": self[key],
            "presence": self[self.custom_field_presence_keys[key]] if key in self.custom_field_presence_keys else None,
            "clear": self[self.custom_field_clear_keys[key]] if key in self.custom_field_clear_keys else None,
            "definition": self.custom_field_definitions[key],
        }

    def _fieldset_options(self, selected):
        selected_ids = [fieldset.pk for fieldset in selected]
        fieldsets = (
            CustomFieldset.objects.filter(Q(lifecycle=CustomFieldset.LIFECYCLE_ACTIVE) | Q(pk__in=selected_ids))
            .prefetch_related(
                Prefetch(
                    "field_memberships",
                    queryset=CustomFieldsetField.objects.select_related("custom_field"),
                )
            )
            .order_by("namespace", "slug")
        )
        stored = self._stored_custom_values()
        fieldsets = list(fieldsets)
        translator = self.vocabulary_translator.load(fieldsets=fieldsets)
        options = []
        for fieldset in fieldsets:
            field_names = {membership.custom_field.name for membership in fieldset.field_memberships.all()}
            options.append(
                {
                    "id": fieldset.pk,
                    "identity": f"{fieldset.namespace}/{fieldset.slug}",
                    "label": translator.fieldset_label(fieldset),
                    "description": translator.fieldset_description(fieldset),
                    "source": self._fieldset_source(fieldset),
                    "selected": fieldset.pk in selected_ids,
                    "deprecated": fieldset.lifecycle == CustomFieldset.LIFECYCLE_DEPRECATED,
                    "has_stored_values": bool(field_names & stored.keys()),
                }
            )
        selected_positions = {fieldset_id: position for position, fieldset_id in enumerate(selected_ids)}
        return sorted(
            options,
            key=lambda option: (
                not option["selected"],
                selected_positions.get(option["id"], len(selected_ids)),
                option["identity"],
            ),
        )

    def _actor(self):
        user = getattr(self.request, "user", None)
        return actor_context_for_user(user)

    def _ordered_selected_fieldsets(self):
        selected = self.cleaned_data.get("custom_fieldsets")
        if selected is None:
            return ()
        selected_by_id = {fieldset.pk: fieldset for fieldset in selected}
        return tuple(
            selected_by_id[fieldset_id]
            for fieldset_id in self._raw_selected_fieldset_ids()
            if fieldset_id in selected_by_id
        )

    def _create_selection(self):
        # The hidden presence marker keeps category defaults as an omission
        # while still letting the browser render and submit those fields.
        omitted = self.is_bound and self.data.get("specification_fieldsets_presence") == "omitted"
        if not self.is_bound:
            omitted = not self._custom_fieldsets_explicit and not self.instance.pk
        return create_fieldset_selection(self._ordered_selected_fieldsets(), omitted=omitted)

    def _patch(self):
        return specification_patch(
            definitions=self.custom_field_definitions,
            cleaned_values=self.cleaned_data,
            fields=self.fields,
            clear_keys=self.custom_field_clear_keys,
        )

    def _native_values(self, instance):
        return {
            "manufacturer": instance.manufacturer,
            "model": instance.model,
            "slug": instance.slug,
            "part_number": instance.part_number,
            "ean": instance.ean,
            "region": instance.region,
            "configuration": instance.configuration,
            "eol_months": instance.eol_months,
            "category": instance.category,
            "asset_role": instance.asset_role,
            "depreciation": instance.depreciation,
            "description": instance.description,
            "comments": instance.comments,
            "requestable": instance.requestable,
            "tags": self.cleaned_data.get("tags", ()),
        }

    def _native_field_names(self):
        concrete_names = {field.name for field in AssetType._meta.concrete_fields}
        return tuple(name for name in self.changed_data if name in concrete_names)

    def _persist_native_update(self, instance):
        # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or ambient scope
        current = AssetType.all_objects.get(pk=instance.pk)
        field_names = self._native_field_names()
        for field_name in field_names:
            field = AssetType._meta.get_field(field_name)
            setattr(current, field.attname, getattr(instance, field.attname))
        if field_names:
            current.save(update_fields=native_persistence_fields(current, field_names))
        return current

    def _command_update(self, instance, actor):
        selection = self._create_selection()
        expected_resource_revision, expected_definition_revision = self._submitted_preconditions()
        if selection.presence == "omitted":
            result = update_asset_type_specifications(
                actor=actor,
                asset_type_id=instance.pk,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                patch=self._patch(),
            )
        else:
            result = set_asset_type_composition(
                actor=actor,
                asset_type_id=instance.pk,
                fieldsets=ExplicitFieldsetSelectionDTO(identities=selection.identities),
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                patch=self._patch(),
            )
        require_command_success(result)

    def _command_create(self, instance, actor):
        stage_id = None
        uploaded = instance.image
        if uploaded and getattr(uploaded, "_file", None) is not None:
            stage_id = stage_uploaded_image(actor=actor, uploaded=uploaded)
        try:
            native = native_asset_type_create_input(self._native_values(instance), staged_image_id=stage_id)
            selection = self._create_selection()
            patch = self._patch()
            preview = require_command_success(
                preview_asset_type_create(
                    actor=actor,
                    native=native,
                    fieldsets=selection,
                    patch=patch,
                )
            )
            if getattr(preview, "issues", ()):
                raise ValidationError("; ".join(specification_message(issue.message_key) for issue in preview.issues))
            result = create_asset_type(
                actor=actor,
                native=native,
                fieldsets=selection,
                patch=patch,
                preview_token=preview.preview_token,
                expected_definition_revision=preview.expected_definition_revision,
                expected_category_default_snapshot_revision=preview.expected_category_default_snapshot_revision,
            )
            owner_id = owner_id_from_result(result)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            created = AssetType.all_objects.get(pk=owner_id)
            stage_id = None  # Successful create owns the staged image lifecycle.
            return created
        finally:
            if stage_id is not None:
                discard_staged_image(stage_id=stage_id, actor=actor)

    def _save_m2m(self):
        # The ordered through-model belongs exclusively to the composition
        # command. Keep Django's native M2M handling (including tags) while
        # withholding this one command-owned field from its direct .set().
        cleaned_data = self.cleaned_data
        self.cleaned_data = {key: value for key, value in cleaned_data.items() if key != "custom_fieldsets"}
        try:
            super()._save_m2m()
        finally:
            self.cleaned_data = cleaned_data

    def _save_pending_m2m(self):
        native_save_m2m = self._native_save_m2m
        with transaction.atomic():
            if native_save_m2m is not None:
                native_save_m2m()
            if not self.instance.pk:
                return
            actor = self._actor()
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            current = AssetType.all_objects.get(pk=self.instance.pk)
            if self._pending_create and self._create_selection().presence == "omitted" and current.category_id:
                plan = current_specification_plan(current, target_kind="asset_type")
                preview = require_command_success(
                    preview_apply_category_defaults(
                        actor=actor,
                        asset_type_id=current.pk,
                        expected_resource_revision=plan.resource_revision,
                        patch=self._patch(),
                    )
                )
                result = apply_category_defaults(
                    actor=actor,
                    asset_type_id=current.pk,
                    preview_token=preview.preview_token,
                    expected_resource_revision=preview.expected_resource_revision,
                    expected_definition_revision=preview.expected_definition_revision,
                    expected_category_default_snapshot_revision=preview.expected_category_default_snapshot_revision,
                    patch=self._patch(),
                )
                require_command_success(result)
            else:
                self._command_update(current, actor)
            # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
            # ambient scope
            self.instance = AssetType.all_objects.get(pk=self.instance.pk)

    def save(self, commit=True):
        # Bypass CustomFieldModelFormMixin.save: that mixin is a legacy second
        # authority which merges and writes custom_field_data directly.
        instance = forms.ModelForm.save(self, commit=False)
        self._native_save_m2m = getattr(self, "save_m2m", None)
        self._pending_create = not bool(instance.pk)
        if not commit:
            self.save_m2m = self._save_pending_m2m
            return instance

        actor = self._actor()
        with transaction.atomic():
            if instance.pk:
                # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
                # ambient scope
                current = AssetType.all_objects.get(pk=instance.pk)
                self._command_update(current, actor)
                current = self._persist_native_update(instance)
                # unscoped: post-save re-read of the row being persisted must not depend on soft-delete state or
                # ambient scope
                self.instance = AssetType.all_objects.get(pk=current.pk)
                if self._native_save_m2m is not None:
                    self._native_save_m2m()
            else:
                self.instance = self._command_create(instance, actor)
        return self.instance
