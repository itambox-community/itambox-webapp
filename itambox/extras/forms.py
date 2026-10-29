import json as _json
import re

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Div, Fieldset, Layout, Row, Submit
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core.data_transfer import policy_for
from core.forms import ColorFieldFormMixin, FilterForm, TenantScopedFormMixin
from core.managers import Scope, get_current_tenant
from core.models import ChangeLoggingMixin
from core.validators import validate_external_url
from itambox.labels import label_registry
from itambox.middleware import get_current_user

from .definition_contract import custom_field_definition_contract_errors
from .filters import TagFilter
from .models import (
    AlertRule,
    CustomField,
    CustomFieldChoiceSet,
    CustomFieldset,
    CustomFieldsetField,
    Event,
    EventRule,
    ExportTemplate,
    LabelTemplate,
    NotificationChannel,
    ReportTemplate,
    SavedFilter,
    ScheduledReport,
    Tag,
    WebhookEndpoint,
    alert_rule_channel_scope_errors,
)
from .signals import _SIGNAL_SKIP_MODELS


class TagForm(TenantScopedFormMixin, ColorFieldFormMixin, forms.ModelForm):
    # color is handled by ColorFieldFormMixin (prepends '#' on init, strips on clean)
    color = forms.CharField(
        max_length=7,
        required=False,
        widget=forms.TextInput(attrs={"type": "color", "class": "form-control form-control-color"}),
    )

    class Meta:
        model = Tag
        fields = ["name", "slug", "color", "description"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.TextInput(attrs={"class": "form-control"}),
        }
        help_texts = {
            "slug": _("URL-friendly identifier."),
            "color": _("Hexadecimal color code (e.g., 00ff00 for green)."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("extras:tag_list")
        self.helper.layout = Layout(
            "name",
            "slug",
            "color",
            "description",
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2">{_("Cancel")}</a>'),
            HTML("</div>"),
        )


# --- Tag Filter Form ---
class TagFilterForm(FilterForm):
    filterset_class = TagFilter


def _add_immutable_tamper_errors(form, model):
    if not form.instance.pk:
        return
    for model_field in model.immutable_fields:
        form_field = "choice_set" if model_field == "choice_set_id" else model_field
        if form_field not in form.fields or form_field not in form.data:
            continue
        current = getattr(form.instance, model_field)
        submitted = form.data.get(form_field)
        if form_field == "choice_set":
            try:
                submitted = None if submitted in (None, "") else int(submitted)
            except (TypeError, ValueError):
                continue
            current = form.instance.choice_set_id
        if submitted != current:
            form.add_error(form_field, _("This value is immutable after creation."))


class CustomFieldForm(TenantScopedFormMixin, forms.ModelForm):
    choice_set = forms.ModelChoiceField(
        queryset=CustomFieldChoiceSet.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    mappings = forms.JSONField(
        required=False, initial=list, widget=forms.Textarea(attrs={"class": "form-control", "rows": 3})
    )

    class Meta:
        model = CustomField
        fields = [
            "name",
            "namespace",
            "label",
            "help_text",
            "field_type",
            "activation",
            "quantity_kind",
            "canonical_unit",
            "minimum_value",
            "maximum_value",
            "regex",
            "decimal_scale",
            "max_values",
            "text_max_length",
            "validation_rule",
            "required",
            "nullable",
            "mappings",
            "choice_set",
            "object_types",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "namespace": forms.TextInput(attrs={"class": "form-control"}),
            "label": forms.TextInput(attrs={"class": "form-control"}),
            "help_text": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "field_type": forms.Select(attrs={"class": "form-select"}),
            "activation": forms.Select(attrs={"class": "form-select"}),
            "quantity_kind": forms.TextInput(attrs={"class": "form-control"}),
            "canonical_unit": forms.TextInput(attrs={"class": "form-control"}),
            "minimum_value": forms.NumberInput(attrs={"class": "form-control"}),
            "maximum_value": forms.NumberInput(attrs={"class": "form-control"}),
            "regex": forms.TextInput(attrs={"class": "form-control"}),
            "decimal_scale": forms.NumberInput(attrs={"class": "form-control", "min": 0, "max": 6}),
            "max_values": forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 64}),
            "text_max_length": forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 4096}),
            "validation_rule": forms.TextInput(attrs={"class": "form-control"}),
            "required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "nullable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "object_types": forms.SelectMultiple(attrs={"class": "form-select", "size": 8}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.fields["name"].widget.attrs["slugify"] = "label"
        self.fields["namespace"].initial = self.instance.namespace or "local"

        # Only models that actually store custom field data are selectable.
        from itambox.registry import registry

        supported = [
            ContentType.objects.get_for_model(model).pk
            for model, features in registry.model_features.items()
            if "custom_field_data" in features and not model._meta.abstract
        ]
        self.fields["object_types"].queryset = ContentType.objects.filter(pk__in=supported).order_by(
            "app_label", "model"
        )
        self.fields["choice_set"].queryset = CustomFieldChoiceSet.objects.all().order_by("namespace", "slug")
        self.fields["object_types"].help_text = _(
            "Select every model this field applies to. Applicability is stored only in this relation."
        )
        self._managed_read_only = bool(
            self.instance.pk
            and self.instance.management_kind in {CustomField.MANAGEMENT_CORE, CustomField.MANAGEMENT_LIBRARY}
        )
        if self._managed_read_only:
            for field in self.fields.values():
                field.disabled = True
        elif self.instance.pk:
            for field_name in CustomField.immutable_fields:
                form_name = "choice_set" if field_name == "choice_set_id" else field_name
                if form_name in self.fields:
                    self.fields[form_name].disabled = True

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("extras:customfield_list")
        self.helper.layout = Layout(
            Fieldset(
                _("Identity"),
                Row(Column("name", css_class="col-md-6"), Column("namespace", css_class="col-md-6")),
                Row(Column("label", css_class="col-md-6"), Column("field_type", css_class="col-md-6")),
                "help_text",
            ),
            Fieldset(
                _("Activation and validation"),
                Row(
                    Column("activation", css_class="col-md-4"),
                    Column("quantity_kind", css_class="col-md-4"),
                    Column("canonical_unit", css_class="col-md-4"),
                ),
                Row(
                    Column("minimum_value", css_class="col-md-3"),
                    Column("maximum_value", css_class="col-md-3"),
                    Column("decimal_scale", css_class="col-md-3"),
                    Column("text_max_length", css_class="col-md-3"),
                ),
                Row(
                    Column("max_values", css_class="col-md-4"),
                    Column("validation_rule", css_class="col-md-4"),
                    Column("regex", css_class="col-md-4"),
                ),
                "choice_set",
                "mappings",
            ),
            Fieldset(
                _("Applicability"),
                Div("required", css_class="mb-3 form-check"),
                Div("nullable", css_class="mb-3 form-check"),
                "object_types",
            ),
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(format_html('<a href="{}" class="btn btn-outline-secondary ms-2">{}</a>', cancel_url, _("Cancel"))),
            HTML("</div>"),
        )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("mappings") is None:
            cleaned_data["mappings"] = []
        if not self._managed_read_only:
            _add_immutable_tamper_errors(self, CustomField)
        if self._managed_read_only:
            return cleaned_data
        errors = custom_field_definition_contract_errors(
            field_type=cleaned_data.get("field_type"),
            activation=cleaned_data.get("activation"),
            quantity_kind=cleaned_data.get("quantity_kind"),
            canonical_unit=cleaned_data.get("canonical_unit"),
            minimum_value=cleaned_data.get("minimum_value"),
            maximum_value=cleaned_data.get("maximum_value"),
            regex=cleaned_data.get("regex"),
            decimal_scale=cleaned_data.get("decimal_scale"),
            max_values=cleaned_data.get("max_values"),
            text_max_length=cleaned_data.get("text_max_length"),
            validation_rule=cleaned_data.get("validation_rule"),
            mappings=cleaned_data.get("mappings"),
            choice_set=cleaned_data.get("choice_set"),
            object_types=cleaned_data.get("object_types"),
            management_kind=cleaned_data.get("management_kind", self.instance.management_kind or "local"),
            lifecycle=cleaned_data.get("lifecycle", self.instance.lifecycle or "active"),
            name=cleaned_data.get("name"),
            namespace=cleaned_data.get("namespace"),
        )
        for field_name, message in errors.items():
            form_name = "choice_set" if field_name == "choice_set_id" else field_name
            if form_name in self.fields:
                self.add_error(form_name, message)
            else:
                self.add_error(None, message)
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self._managed_read_only:
            return instance
        if commit:
            with transaction.atomic():
                instance.save()
                self.save_m2m()
        return instance


class CustomFieldsetForm(TenantScopedFormMixin, forms.ModelForm):
    fields = forms.ModelMultipleChoiceField(
        queryset=CustomField.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 12}),
    )
    field_positions = forms.JSONField(
        required=False,
        initial=dict,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": '{"field_key": 10}'}),
        help_text=_("Optional JSON object mapping field keys to positive positions. Omitted keys use selection order."),
    )

    class Meta:
        model = CustomFieldset
        fields = ["namespace", "slug", "label", "description"]
        widgets = {
            "namespace": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "label": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.fields["namespace"].initial = self.instance.namespace or "local"
        self.fields["fields"].queryset = CustomField.objects.order_by("name")
        if self.instance.pk:
            memberships = list(self.instance.field_memberships.select_related("custom_field").order_by("position"))
            self.fields["fields"].initial = [membership.custom_field_id for membership in memberships]
            self.fields["field_positions"].initial = {
                membership.custom_field.name: membership.position for membership in memberships
            }
        self._managed_read_only = bool(
            self.instance.pk
            and self.instance.management_kind in {CustomFieldset.MANAGEMENT_CORE, CustomFieldset.MANAGEMENT_LIBRARY}
        )
        if self._managed_read_only:
            for field in self.fields.values():
                field.disabled = True
        elif self.instance.pk:
            for field_name in CustomFieldset.immutable_fields:
                if field_name in self.fields:
                    self.fields[field_name].disabled = True

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("extras:customfieldset_list")
        self.helper.layout = Layout(
            Fieldset(
                _("Identity"),
                Row(Column("namespace", css_class="col-md-6"), Column("slug", css_class="col-md-6")),
                "label",
                "description",
            ),
            Fieldset(_("Ordered fields"), "fields", "field_positions"),
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(format_html('<a href="{}" class="btn btn-outline-secondary ms-2">{}</a>', cancel_url, _("Cancel"))),
            HTML("</div>"),
        )

    def clean_field_positions(self):
        positions = self.cleaned_data.get("field_positions") or {}
        if not isinstance(positions, dict):
            raise forms.ValidationError(_("Positions must be a JSON object."))
        normalized = {}
        for key, value in positions.items():
            if not str(key).strip() or type(value) is not int or not 1 <= value <= 1000000:
                raise forms.ValidationError(_("Every field position must be a positive integer."))
            normalized[str(key)] = value
        if len(normalized) != len(set(normalized.values())):
            raise forms.ValidationError(_("Field positions must be unique."))
        return normalized

    def clean(self):
        cleaned_data = super().clean()
        if not self._managed_read_only:
            _add_immutable_tamper_errors(self, CustomFieldset)
        if self._managed_read_only:
            return cleaned_data
        positions = cleaned_data.get("field_positions") or {}
        selected = {field.pk: field for field in cleaned_data.get("fields") or ()}
        raw_ids = self.data.getlist("fields") if hasattr(self.data, "getlist") else []
        ordered_ids = [int(value) for value in raw_ids if str(value).isdigit() and int(value) in selected]
        effective_positions = {
            field_id: positions.get(selected[field_id].name, positions.get(str(field_id), index * 10))
            for index, field_id in enumerate(ordered_ids, start=1)
        }
        if len(effective_positions) != len(set(effective_positions.values())):
            raise forms.ValidationError(_("Field positions must be unique."))
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self._managed_read_only:
            return instance
        if not commit:
            return instance

        raw_ids = self.data.getlist("fields") if hasattr(self.data, "getlist") else []
        ordered_ids = [int(value) for value in raw_ids if str(value).isdigit()]
        selected = {field.pk: field for field in self.cleaned_data["fields"]}
        ordered_ids = [field_id for field_id in ordered_ids if field_id in selected]
        positions = self.cleaned_data.get("field_positions") or {}
        position_by_id = {}
        for index, field_id in enumerate(ordered_ids, start=1):
            field = selected[field_id]
            position_by_id[field_id] = positions.get(field.name, positions.get(str(field_id), index * 10))
        if len(position_by_id) != len(set(position_by_id.values())):
            raise forms.ValidationError(_("Field positions must be unique."))
        with transaction.atomic():
            instance.save()
            instance.field_memberships.all().delete()
            CustomFieldsetField.objects.bulk_create(
                [
                    CustomFieldsetField(fieldset=instance, custom_field_id=field_id, position=position)
                    for field_id, position in sorted(position_by_id.items(), key=lambda item: item[1])
                ]
            )
        return instance


class CustomFieldFilterForm(FilterForm):
    from .filters import CustomFieldFilterSet

    filterset_class = CustomFieldFilterSet


class CustomFieldsetFilterForm(FilterForm):
    from .filters import CustomFieldsetFilterSet

    filterset_class = CustomFieldsetFilterSet


class SavedFilterForm(TenantScopedFormMixin, forms.ModelForm):
    # ``parameters`` is captured from the list view (the quick-save endpoint), not
    # edited here. ``tenant``/``created_by`` are set by the view, not the user.
    class Meta:
        model = SavedFilter
        fields = ["name", "description", "content_type", "shared", "enabled"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "content_type": forms.Select(attrs={"class": "form-select"}),
            "shared": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        self.fields["content_type"].queryset = ContentType.objects.order_by("app_label", "model")

        button_text = _("Update") if self.instance.pk else _("Create")
        cancel_url = reverse("extras:savedfilter_list")

        self.helper.layout = Layout(
            "name",
            "description",
            "content_type",
            Div("shared", css_class="mb-3 form-check"),
            Div("enabled", css_class="mb-3 form-check"),
            HTML('<div class="mt-3">'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(f'<a href="{cancel_url}" class="btn btn-outline-secondary ms-2">{_("Cancel")}</a>'),
            HTML("</div>"),
        )


class SavedFilterFilterForm(FilterForm):
    from .filters import SavedFilterFilterSet

    filterset_class = SavedFilterFilterSet


# =============================================================================
# Domain forms — moved from core/forms/__init__.py (audit B2).
# These depend on extras.models and therefore live here rather than in the
# framework layer.  The old import path (core.forms.XxxForm) has been
# repointed to extras.forms in every consumer.
# =============================================================================


def logged_content_types():
    """ContentTypes whose model actually emits Events (ChangeLoggingMixin, not skipped).

    An EventRule pointed at any other model would never fire — constraining the dropdown
    to these keeps users out of that silent dead-end.
    """

    ids = []
    for ct in ContentType.objects.all():
        model = ct.model_class()
        if model is None:
            continue
        if issubclass(model, ChangeLoggingMixin) and model.__name__ not in _SIGNAL_SKIP_MODELS:
            ids.append(ct.id)
    return ContentType.objects.filter(id__in=ids).order_by("app_label", "model")


def exportable_content_types():
    """ContentTypes whose model is declared exportable (``core.data_transfer``).

    An ExportTemplate pointed at a generated-log / UI-only model could never be
    reached from a list view's export menu, so constraining the dropdown to real
    domain models keeps authors out of that dead-end — and keeps the picker free
    of the hundreds of internal ContentTypes (sessions, permissions, tokens, …)
    that a bare ``content_type`` field would otherwise list.
    """
    ids = []
    for ct in ContentType.objects.all():
        model = ct.model_class()
        if model is None:
            continue
        if policy_for(model).export:
            ids.append(ct.id)
    return ContentType.objects.filter(id__in=ids).order_by("app_label", "model")


SLACK_PAYLOAD_PRESET = _json.dumps(
    {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "ITAMbox Event: {{ event }}"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": "*Model:*\n{{ model }}"},
                    {"type": "mrkdwn", "text": "*Object ID:*\n{{ object_id }}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": "```{{ data | tojson }}```"}},
        ]
    },
    indent=2,
)

TEAMS_PAYLOAD_PRESET = _json.dumps(
    {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "ITAMbox Event: {{ event }}",
                            "weight": "Bolder",
                            "size": "Medium",
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Model", "value": "{{ model }}"},
                                {"title": "Object ID", "value": "{{ object_id }}"},
                            ],
                        },
                        {"type": "TextBlock", "text": "```{{ data | tojson }}```", "wrap": True},
                    ],
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.2",
                },
            }
        ],
    },
    indent=2,
)

PAYLOAD_PRESET_CHOICES = [
    ("", _("Custom")),
    ("slack", _("Slack Block Kit")),
    ("teams", _("Microsoft Teams (Adaptive Card)")),
]

PRESET_PAYLOADS = {
    "slack": SLACK_PAYLOAD_PRESET,
    "teams": TEAMS_PAYLOAD_PRESET,
}

_EVENT_RULE_CONDITIONS_WITHDRAWN_MESSAGE = _(
    "Event rule conditions are withdrawn for the 1.0 release. Existing conditions are preserved and remain readable; "
    "new or changed conditions cannot be submitted."
)


class WebhookEndpointForm(TenantScopedFormMixin, forms.ModelForm):
    # Admin-visible tenant stays required once a Tenant row exists.
    tenant_required = True

    payload_preset = forms.ChoiceField(
        choices=PAYLOAD_PRESET_CHOICES,
        required=False,
        label=_("Payload Preset"),
        help_text=_("Select a preset to pre-fill the payload template above"),
    )

    class Meta:
        model = WebhookEndpoint
        fields = [
            "name",
            "url",
            "http_method",
            "headers",
            "payload_preset",
            "secret",
            "enabled",
            "retry_count",
            "retry_backoff",
            "tenant",
        ]
        widgets = {
            "headers": forms.Textarea(attrs={"rows": 4}),
            "tenant": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = get_current_user()
        is_admin = bool(user and (user.is_superuser or getattr(user, "is_staff", False)))
        if not is_admin and "tenant" in self.fields:
            self.fields.pop("tenant")

        if self.instance and self.instance.headers:
            self.initial["headers"] = _json.dumps(self.instance.headers, indent=2)

        # BUG FIX: show the decrypted secret on edit so we don't re-encrypt
        # the stored "enc$..." ciphertext as if it were a plaintext value.
        if self.instance and self.instance.pk and self.instance.secret:
            self.initial["secret"] = self.instance.secret_decrypted

        self.helper = FormHelper()
        tenant_row = (
            Row(
                Column("tenant", css_class="col-md-6"),
                css_class="row g-3",
            )
            if is_admin
            else None
        )
        layout_fields = [
            Fieldset(
                _("Identity"),
                Row(
                    Column("name", css_class="col-md-6"),
                    Column("url", css_class="col-md-6"),
                    css_class="row g-3",
                ),
                Row(
                    Column("http_method", css_class="col-md-4"),
                    Column("enabled", css_class="col-md-4"),
                    Column("secret", css_class="col-md-4"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Payload"),
                "payload_preset",
                "headers",
            ),
            Fieldset(
                _("Retry"),
                Row(
                    Column("retry_count", css_class="col-md-6"),
                    Column("retry_backoff", css_class="col-md-6"),
                    css_class="row g-3",
                ),
            ),
        ]
        if tenant_row is not None:
            layout_fields.append(tenant_row)
        layout_fields += [
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Webhook Endpoint"), css_class="btn btn-primary"),
            HTML(
                '<a href="{% url \'extras:webhookendpoint_list\' %}" class="btn btn-outline-secondary ms-2" '
                'data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        ]
        self.helper.layout = Layout(*layout_fields)

    def clean_headers(self):
        data = self.cleaned_data["headers"]
        if isinstance(data, str):
            try:
                return _json.loads(data)
            except _json.JSONDecodeError as exc:
                raise forms.ValidationError(_("Headers must be valid JSON.")) from exc
        return data


class EventRuleForm(TenantScopedFormMixin, forms.ModelForm):
    # Admin-visible tenant stays required once a Tenant row exists.
    tenant_required = True

    events = forms.MultipleChoiceField(
        choices=Event.ACTION_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label=_("Trigger Events"),
        help_text=_("Fire this rule when any of the selected change types occur on the target model."),
    )
    payload_preset = forms.ChoiceField(
        choices=PAYLOAD_PRESET_CHOICES,
        required=False,
        label=_("Payload Preset"),
        help_text=_("Select a preset to pre-fill the action config"),
    )

    class Meta:
        model = EventRule
        fields = [
            "name",
            "model",
            "events",
            "action_type",
            "webhook",
            "conditions",
            "action_config",
            "enabled",
            "tenant",
        ]
        widgets = {
            "conditions": forms.Textarea(attrs={"rows": 3}),
            "action_config": forms.Textarea(attrs={"rows": 4}),
            "tenant": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = get_current_user()
        is_admin = bool(user and (user.is_superuser or getattr(user, "is_staff", False)))
        if not is_admin and "tenant" in self.fields:
            self.fields.pop("tenant")

        # Only models that emit Events are selectable — others would never trigger the rule.
        self.fields["model"].queryset = logged_content_types()
        self.fields["model"].label = _("Target Model")
        self.fields["webhook"].queryset = WebhookEndpoint.objects.for_scope(Scope.current()).filter(enabled=True)
        self.fields["webhook"].label = _("Webhook Endpoint")
        self.fields["webhook"].help_text = _(
            "Required for Webhook rules. Manage endpoints under Webhook Endpoints. "
            'Leave blank only if you supply a "url" in Action Configuration below.'
        )
        self.fields["conditions"].required = False
        self.fields["action_config"].required = False
        if self.instance and self.instance.pk:
            if self.instance.events:
                self.initial["events"] = self.instance.events
            if self.instance.conditions:
                self.initial["conditions"] = _json.dumps(self.instance.conditions, indent=2)
            if self.instance.action_config:
                self.initial["action_config"] = _json.dumps(self.instance.action_config, indent=2)

        if self.instance and self.instance.pk and self.instance.conditions:
            conditions_block = format_html(
                '<div class="alert alert-warning" role="alert">{}</div>'
                '<pre class="bg-light border rounded p-3">{}</pre>'
                '<a href="{}">{}</a>',
                _(
                    "Conditions are withdrawn for the 1.0 release. Existing conditions are preserved and remain "
                    "readable; new or changed conditions cannot be submitted."
                ),
                self.instance.conditions_json,
                static("docs/usage/webhooks-and-automation.html") + "#event-rule-conditions-withdrawn-for-10",
                _("Read the Event Rule conditions documentation."),
            )
        else:
            conditions_block = format_html(
                '<div class="alert alert-secondary" role="status">{}</div>',
                _("Conditions are not available for the 1.0 release."),
            )

        self.helper = FormHelper()
        layout_fields = [
            Fieldset(
                _("Identity"),
                Row(
                    Column("name", css_class="col-md-6"),
                    Column("enabled", css_class="col-md-6"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Trigger"),
                Row(
                    Column("model", css_class="col-md-6"),
                    Column("action_type", css_class="col-md-6"),
                    css_class="row g-3",
                ),
                "events",
            ),
            Fieldset(
                _("Action"),
                "webhook",
                "payload_preset",
                HTML(conditions_block),
                "action_config",
            ),
        ]
        if is_admin:
            layout_fields.append(Row(Column("tenant", css_class="col-md-6"), css_class="row g-3"))
        layout_fields += [
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Event Rule"), css_class="btn btn-primary"),
            HTML(
                '<a href="{% url \'extras:eventrule_list\' %}" class="btn btn-outline-secondary ms-2" '
                'data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        ]
        self.helper.layout = Layout(*layout_fields)

    def clean_conditions(self):
        if self.instance and self.instance.pk and self.add_prefix("conditions") not in self.data:
            return self.instance.conditions

        data = self.cleaned_data["conditions"]
        if isinstance(data, str):
            if not data.strip():
                data = {}
            else:
                try:
                    data = _json.loads(data)
                except _json.JSONDecodeError as exc:
                    raise forms.ValidationError(_("Conditions must be valid JSON.")) from exc
        data = data or {}

        if self.instance and self.instance.pk:
            if data != self.instance.conditions:
                raise forms.ValidationError(_EVENT_RULE_CONDITIONS_WITHDRAWN_MESSAGE)
        elif data:
            raise forms.ValidationError(_EVENT_RULE_CONDITIONS_WITHDRAWN_MESSAGE)

        return data

    def clean_action_config(self):
        data = self.cleaned_data["action_config"]
        if isinstance(data, str):
            if not data.strip():
                return {}
            try:
                return _json.loads(data)
            except _json.JSONDecodeError as exc:
                raise forms.ValidationError(_("Action config must be valid JSON.")) from exc
        return data or {}

    def clean(self):
        cleaned = super().clean()
        action_type = cleaned.get("action_type")
        if action_type == EventRule.ACTION_WEBHOOK:
            config = cleaned.get("action_config") or {}
            if not cleaned.get("webhook") and not config.get("url"):
                self.add_error(
                    "webhook",
                    'Select a Webhook Endpoint (or provide a "url" in Action Configuration) for a Webhook rule.',
                )
        return cleaned


def labelled_content_types():
    """ContentTypes of the models that carry a label (``itambox.labels``)."""
    ids = [ContentType.objects.get_for_model(model).pk for model in label_registry.models()]
    return ContentType.objects.filter(id__in=ids).order_by("app_label", "model")


class LabelTemplateForm(TenantScopedFormMixin, forms.ModelForm):
    """Author a global LabelTemplate (superuser-only; see LabelTemplateEditView).

    ``content_types`` is limited to labelled models. QR codes carry the
    object's reference and suit every labelled model; the linear formats
    encode a short identifier and are refused for models without one.
    """

    class Meta:
        model = LabelTemplate
        fields = [
            "name",
            "description",
            "content_types",
            "page_width",
            "page_height",
            "barcode_format",
            "template_code",
        ]
        widgets = {
            "template_code": forms.Textarea(attrs={"rows": 10}),
            "content_types": forms.SelectMultiple(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["content_types"].queryset = labelled_content_types()
        self.fields["content_types"].required = True
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Fieldset(
                _("Identity"),
                Row(
                    Column("name", css_class="col-md-8"),
                    Column("barcode_format", css_class="col-md-4"),
                    css_class="row g-3",
                ),
                "content_types",
                "description",
            ),
            Fieldset(
                _("Page Size"),
                Row(
                    Column("page_width", css_class="col-md-6"),
                    Column("page_height", css_class="col-md-6"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Template"),
                "template_code",
            ),
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Label Template"), css_class="btn btn-primary"),
            HTML(
                '<a href="{% url \'extras:labeltemplate_list\' %}" class="btn btn-outline-secondary ms-2" '
                'data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        )

    def clean(self):
        cleaned = super().clean()
        barcode_format = cleaned.get("barcode_format")
        if barcode_format in LabelTemplate.LINEAR_FORMATS:
            unsupported = [
                str(content_type.model_class()._meta.verbose_name_plural)
                for content_type in cleaned.get("content_types") or ()
                if not label_registry.get(content_type.model_class()).supports_linear_barcodes
            ]
            if unsupported:
                self.add_error(
                    "barcode_format",
                    _("%(types)s labels support QR codes only.") % {"types": ", ".join(unsupported)},
                )
        return cleaned


class ExportTemplateForm(TenantScopedFormMixin, forms.ModelForm):
    """Author a global ExportTemplate (superuser-only; see ExportTemplateEditView).

    Curates ``content_type`` to exportable domain models and validates that
    ``template_code`` actually compiles in the sandbox used at render time, so a
    broken template is rejected here rather than 500-ing a member's export later.
    """

    class Meta:
        model = ExportTemplate
        fields = [
            "name",
            "content_type",
            "description",
            "template_code",
            "mime_type",
            "file_extension",
            "as_attachment",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "template_code": forms.Textarea(
                attrs={
                    "rows": 16,
                    "class": "font-monospace",
                    "spellcheck": "false",
                    "data-no-dirty-track": "false",
                }
            ),
        }
        help_texts = {
            "template_code": _(
                "Jinja2 template rendered once over the whole result set. The full "
                "queryset is available as <code>queryset</code>. Loop over it with "
                "<code>{% for obj in queryset %}…{% endfor %}</code> and emit your own "
                "header row. Built-in filters such as <code>|tojson</code> are available."
            ),
            "mime_type": _("Sent as the Content-Type header, e.g. text/csv or application/json."),
            "file_extension": _("Appended to the download filename (without a leading dot)."),
            "as_attachment": _(
                "Download the result as a file. Disable to render inline in the browser (useful for previewing)."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["content_type"].queryset = exportable_content_types()
        self.fields["content_type"].label = _("Model")
        self.fields["content_type"].help_text = _("The object type this template can export.")
        self.fields["file_extension"].required = False

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Fieldset(
                _("Identity"),
                Row(
                    Column("name", css_class="col-md-7"),
                    Column("content_type", css_class="col-md-5"),
                    css_class="row g-3",
                ),
                "description",
            ),
            Fieldset(
                _("Output"),
                Row(
                    Column("mime_type", css_class="col-md-6"),
                    Column("file_extension", css_class="col-md-3"),
                    Column("as_attachment", css_class="col-md-3 d-flex align-items-end"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Template"),
                "template_code",
            ),
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Export Template"), css_class="btn btn-primary"),
            HTML(
                '<a href="{% url \'extras:exporttemplate_list\' %}" class="btn btn-outline-secondary ms-2" '
                'data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        )

    def clean_file_extension(self):
        # Normalise: drop a leading dot and lowercase so filenames stay tidy.
        ext = (self.cleaned_data.get("file_extension") or "").strip().lstrip(".").lower()
        return ext

    def clean_template_code(self):
        code = self.cleaned_data.get("template_code") or ""
        # inline import: heavy-import: jinja2 is only needed when validating template authoring,
        # not on every forms.py import.
        from jinja2 import TemplateSyntaxError

        try:
            ExportTemplate.get_jinja_environment().from_string(code)
        except TemplateSyntaxError as exc:
            raise forms.ValidationError(
                _("Template syntax error on line %(line)s: %(message)s")
                % {
                    "line": exc.lineno,
                    "message": exc.message,
                }
            ) from exc
        return code


def _resolve_nonadmin_write_tenant(form, user):
    """Tenant to bind for a non-admin write to a tenant-scoped Extras record.

    Canonical write scopes are single tenant, tenant group, and All accessible
    tenants; AssetHolder is a domain profile, never an authorization source, so
    it is never consulted here (issue #134). Behaviour:

    * Admins (superuser / staff) choose the tenant explicitly through the
      exposed field — including a deliberate global ``tenant=None`` — so this
      never overrides them (returns ``None``).
    * Editing an existing record retains its current tenant: a non-admin has no
      tenant field to retarget it with (returns ``None``).
    * Creating requires one explicit live tenant — the active single-tenant
      scope. A multi-tenant scope (tenant group / All accessible) has no
      unambiguous tenant, so it fails validation instead of silently picking the
      first (or any) AssetHolder/membership tenant.

    Returns the tenant to assign on a non-admin create, or ``None`` to leave the
    instance's tenant untouched. Raises ``ValidationError`` when a non-admin
    create has no single active tenant.
    """
    if user is None or user.is_superuser or getattr(user, "is_staff", False):
        return None
    if form.instance.pk:
        return None
    tenant = get_current_tenant()
    if tenant is None:
        raise forms.ValidationError(
            _(
                "This record must belong to one tenant. Switch to a specific tenant before saving because the "
                "active scope contains multiple tenants."
            )
        )
    return tenant


class ReportTemplateForm(TenantScopedFormMixin, forms.ModelForm):
    COLUMN_CHOICES = [
        # Asset Inventory Summary Columns
        ("asset_tag", _("Asset Tag")),
        ("name", _("Asset Name")),
        ("manufacturer", _("Manufacturer")),
        ("model", _("Model")),
        ("serial_number", _("Serial Number")),
        ("status", _("Status Label")),
        ("location", _("Location")),
        ("assigned_to", _("Asset Holder")),
        ("purchase_cost", _("Purchase Cost")),
        ("purchase_date", _("Purchase Date")),
        ("warranty_months", _("Warranty (Months)")),
        # License Utilization Columns
        ("license_name", _("License Name")),
        ("software", _("Software")),
        ("seats", _("Total Seats")),
        ("assigned_seats", _("Assigned Seats")),
        ("available_seats", _("Available Seats")),
        ("utilization_rate", _("Utilization Rate")),
        # Subscription Renewals Columns
        ("subscription_name", _("Subscription Name")),
        ("supplier", _("Supplier")),
        ("agreement_entitled_quantity", _("Agreement Entitled Quantity")),
        ("billing_cycle", _("Billing Cycle")),
        ("cost", _("Cost")),
        ("end_date", _("End Date")),
        # Asset Maintenance Columns
        ("maintenance_asset", _("Asset")),
        ("maintenance_type", _("Type")),
        ("maintenance_status", _("Status")),
        ("maintenance_cost", _("Cost")),
        ("maintenance_start_date", _("Start Date")),
        ("maintenance_completion_date", _("Completion Date")),
        ("maintenance_downtime", _("Downtime (Days)")),
        # Asset Depreciation Columns
        ("salvage_value", _("Salvage Value")),
        ("depreciation_months", _("Depreciation Lifespan (Months)")),
        ("current_value", _("Depreciated Value")),
        # Software Inventory Columns
        ("software_name", _("Software Product")),
        ("version", _("Version")),
        ("category", _("Category")),
        ("license_type", _("License Type")),
        ("installed_count", _("Installed Count")),
        # Contract Renewals & Expirations Columns
        ("contract_number", _("Contract #")),
        ("contract_name", _("Contract Name")),
        ("contract_type", _("Contract Type")),
        ("contract_status", _("Contract Status")),
        ("contract_supplier", _("Supplier")),
        ("contract_start_date", _("Start Date")),
        ("contract_end_date", _("End Date")),
        ("contract_renewal_date", _("Renewal Date")),
        ("contract_days_until_expiry", _("Days Until Expiry")),
        ("contract_cost", _("Contract Cost")),
        ("contract_billing_cycle", _("Billing Cycle")),
        ("contract_auto_renew", _("Auto-Renew")),
        ("contract_covered_assets", _("Covered Assets")),
        ("contract_sla_response_time", _("SLA Response Time")),
        ("contract_sla_resolution_time", _("SLA Resolution Time")),
        ("contract_coverage_hours", _("Coverage Hours")),
        # Warranty Expiration Columns
        ("warranty_asset", _("Asset")),
        ("warranty_type", _("Warranty Type")),
        ("warranty_supplier", _("Supplier")),
        ("warranty_start_date", _("Start Date")),
        ("warranty_end_date", _("End Date")),
        ("warranty_days_remaining", _("Days Remaining")),
        ("warranty_status", _("Status")),
        ("warranty_cost", _("Warranty Cost")),
        ("warranty_reference", _("Reference")),
        # Asset Disposal & End-of-Life Columns
        ("disposal_asset", _("Asset")),
        ("disposal_status", _("Disposal Status")),
        ("disposal_cancelled_at", _("Cancelled At")),
        ("disposal_cancelled_by", _("Cancelled By")),
        ("disposal_cancellation_reason", _("Disposal cancellation reason")),
        ("disposal_date", _("Disposal Date")),
        ("disposal_method", _("Disposal Method")),
        ("disposal_sanitization_method", _("Data Sanitization Method")),
        ("disposal_sanitization_certificate", _("Sanitization Certificate")),
        ("disposal_sanitized_by", _("Sanitized By")),
        ("disposal_recipient", _("Recipient")),
        ("disposal_proceeds", _("Proceeds")),
        ("disposal_weee_compliant", _("WEEE Compliant")),
        ("disposal_notes", _("Notes")),
        # Hardware Inventory (Accessories, Consumables, Components) Columns
        ("hw_item_type", _("Item Type")),
        ("hw_name", _("Name")),
        ("hw_manufacturer", _("Manufacturer")),
        ("hw_category", _("Category")),
        ("hw_part_number", _("Part Number")),
        ("hw_total_stock", _("Total Stock")),
        ("hw_available", _("Available")),
        ("hw_min_qty", _("Safety Threshold")),
        ("hw_status", _("Stock Status")),
        # Custody & EULA Sign-off Compliance Columns
        ("custody_asset", _("Asset")),
        ("custody_holder", _("Holder")),
        ("custody_status", _("Acceptance Status")),
        ("custody_accepted_date", _("Accepted Date")),
        ("custody_eula_version", _("EULA Version")),
        ("custody_signature_provider", _("Signature Provider")),
        ("custody_qms_reference", _("QMS Reference")),
        ("custody_ip_address", _("IP Address")),
        ("custody_created_date", _("Created Date")),
        ("license_count", _("License Count")),
    ]

    included_columns = forms.MultipleChoiceField(
        choices=COLUMN_CHOICES,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        required=False,
        label=_("Included Columns"),
        help_text=_(
            "Select the columns to include in your visual report grid. Only columns matching your report type will render."
        ),
    )

    class Meta:
        model = ReportTemplate
        fields = [
            "name",
            "description",
            "report_type",
            "included_columns",
            "include_summary_cards",
            "include_distribution_chart",
            "group_by_field",
            "style_preset",
            "template_content",
            "tenant",
            "filter_tenants",
        ]
        widgets = {
            "template_content": forms.Textarea(attrs={"rows": 15}),
            "tenant": forms.Select(attrs={"class": "form-select"}),
            "filter_tenants": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = get_current_user()
        if user and not (user.is_superuser or (hasattr(user, "is_staff") and user.is_staff)):
            if "tenant" in self.fields:
                self.fields.pop("tenant")
            if "filter_tenants" in self.fields:
                self.fields.pop("filter_tenants")
        else:
            if "tenant" in self.fields:
                self.fields["tenant"].required = False
                self.fields["tenant"].label = _("Scope / Tenant Filter")
                self.fields["tenant"].empty_label = _("--------- All Tenants ---------")
                self.fields["tenant"].help_text = _(
                    "Select a specific tenant to restrict this report's compiled data strictly to that tenant. "
                    "Choose 'All Tenants' (blank) to aggregate data globally across all tenants."
                )
            if "filter_tenants" in self.fields:
                self.fields["filter_tenants"].label = _("Filter Tenants (Scoping Constellation)")
                self.fields["filter_tenants"].help_text = _(
                    "Select one or more specific tenants to filter this report's compiled data. If none are selected, "
                    "aggregates data globally across all tenants."
                )

    def clean(self):
        cleaned_data = super().clean()
        _resolve_nonadmin_write_tenant(self, get_current_user())
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        tenant = _resolve_nonadmin_write_tenant(self, get_current_user())
        if tenant is not None:
            instance.tenant = tenant
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ScheduledReportForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = ScheduledReport
        fields = [
            "name",
            "report",
            "recipients",
            "frequency",
            "cron_expression",
            "start_time",
            "format",
            "channels",
            "save_to_archive",
            "is_active",
            "tenant",
            "filter_tenants",
        ]
        widgets = {
            "recipients": forms.Textarea(
                attrs={"rows": 2, "placeholder": "recipient1@example.com, recipient2@example.com"}
            ),
            "tenant": forms.Select(attrs={"class": "form-select"}),
            "report": forms.Select(attrs={"class": "form-select"}),
            "filter_tenants": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
            "channels": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
            "cron_expression": forms.TextInput(attrs={"placeholder": "e.g. 0 8 * * 1-5", "class": "form-control"}),
            "start_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = get_current_user()
        if user and not (user.is_superuser or (hasattr(user, "is_staff") and user.is_staff)):
            if "tenant" in self.fields:
                self.fields.pop("tenant")
            if "filter_tenants" in self.fields:
                self.fields.pop("filter_tenants")
            # Report/channel choices follow the active canonical read scope
            # (single tenant, tenant group, or All accessible) resolved by the
            # tenant-scoping managers — never an arbitrary AssetHolder profile
            # tenant, which under a multi-tenant scope would hide records the
            # actor can legitimately reach.
            self.fields["report"].queryset = ReportTemplate.objects.for_scope(Scope.current())
            self.fields["channels"].queryset = NotificationChannel.objects.for_scope(Scope.current())
        else:
            if "tenant" in self.fields:
                self.fields["tenant"].required = False
                self.fields["tenant"].label = _("Scope / Tenant Filter")
                self.fields["tenant"].empty_label = _("--------- All Tenants ---------")
                self.fields["tenant"].help_text = _(
                    "Select a specific tenant to restrict this scheduled report's compiled data strictly to that tenant. "
                    "Choose 'All Tenants' (blank) to aggregate data globally across all tenants."
                )
            if "filter_tenants" in self.fields:
                self.fields["filter_tenants"].label = _("Filter Tenants (Scoping Constellation)")
                self.fields["filter_tenants"].help_text = _(
                    "Select one or more specific tenants to filter this scheduled report's compiled data. If none are selected, "
                    "aggregates data globally across all tenants."
                )

        is_admin = bool(user and (user.is_superuser or getattr(user, "is_staff", False)))
        self.helper = FormHelper()
        admin_fieldset_fields = []
        if is_admin:
            admin_fieldset_fields = [
                Row(
                    Column("tenant", css_class="col-md-6"),
                    Column("filter_tenants", css_class="col-md-6"),
                    css_class="row g-3",
                ),
            ]
        layout_fields = [
            Fieldset(
                _("Identity"),
                Row(
                    Column("name", css_class="col-md-6"),
                    Column("report", css_class="col-md-6"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Schedule"),
                Row(
                    Column("frequency", css_class="col-md-4"),
                    Column("cron_expression", css_class="col-md-4"),
                    Column("start_time", css_class="col-md-4"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Delivery"),
                "recipients",
                "channels",
                Row(
                    Column("format", css_class="col-md-4"),
                    Column("save_to_archive", css_class="col-md-4"),
                    Column("is_active", css_class="col-md-4"),
                    css_class="row g-3",
                ),
            ),
        ]
        if admin_fieldset_fields:
            layout_fields.append(Fieldset(_("Scope"), *admin_fieldset_fields))
        layout_fields += [
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Scheduled Report"), css_class="btn btn-primary"),
            HTML(
                '<a href="{% url \'extras:scheduledreport_list\' %}" class="btn btn-outline-secondary ms-2" '
                'data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        ]
        self.helper.layout = Layout(*layout_fields)

    def clean(self):
        cleaned_data = super().clean()
        _resolve_nonadmin_write_tenant(self, get_current_user())
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        tenant = _resolve_nonadmin_write_tenant(self, get_current_user())
        if tenant is not None:
            instance.tenant = tenant
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class AlertRuleForm(TenantScopedFormMixin, forms.ModelForm):
    # Admin-visible tenant stays required once a Tenant row exists.
    tenant_required = True

    # Alert types whose threshold_value is a *days* horizon rather than a unit count.
    _DAYS_ALERT_TYPES = {
        AlertRule.ALERT_TYPE_UPCOMING_EOL,
        AlertRule.ALERT_TYPE_LICENSE_EXPIRY,
        AlertRule.ALERT_TYPE_RENEWAL_DUE,
        AlertRule.ALERT_TYPE_WARRANTY_EXPIRY,
        AlertRule.ALERT_TYPE_AUDIT_OVERDUE,
    }

    class Meta:
        model = AlertRule
        fields = [
            "name",
            "description",
            "alert_type",
            "threshold_value",
            "severity",
            "is_active",
            "is_muted",
            "renotify_interval_days",
            "channels",
            "tenant",
        ]
        widgets = {
            "channels": forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
            "tenant": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = get_current_user()
        if user and not (user.is_superuser or (hasattr(user, "is_staff") and user.is_staff)):
            if "tenant" in self.fields:
                self.fields.pop("tenant")
            # Channel choices follow the active canonical read scope resolved by
            # the tenant-scoping manager — rebound per request so the dropdown
            # never lists channels outside the actor's scope.
            self.fields["channels"].queryset = NotificationChannel.objects.for_scope(Scope.current())

        # Make the threshold label/help reflect what the number actually means
        # for the selected alert type (days horizon vs. unit count).
        alert_type = (
            self.data.get("alert_type") or self.initial.get("alert_type") or getattr(self.instance, "alert_type", None)
        )
        threshold = self.fields["threshold_value"]
        if alert_type in self._DAYS_ALERT_TYPES:
            threshold.label = _("Days horizon")
            threshold.help_text = _("Alert when the date is within this many days.")
        elif alert_type == AlertRule.ALERT_TYPE_LOW_STOCK:
            threshold.label = _("Stock threshold (units)")
            threshold.help_text = _(
                "Alert when available stock is at or below this many units "
                "(per-item minimum quantity overrides this when set)."
            )
        else:
            threshold.help_text = _("Limit count or days horizon, depending on alert type.")

        self.fields["renotify_interval_days"].label = _("Re-notify every (days)")

        is_admin = bool(user and (user.is_superuser or getattr(user, "is_staff", False)))
        self.helper = FormHelper()
        layout_fields = [
            Fieldset(
                _("Identity"),
                Row(
                    Column("name", css_class="col-md-8"),
                    Column("severity", css_class="col-md-4"),
                    css_class="row g-3",
                ),
                "description",
            ),
            Fieldset(
                _("Alert Configuration"),
                Row(
                    Column("alert_type", css_class="col-md-6"),
                    Column("threshold_value", css_class="col-md-6"),
                    css_class="row g-3",
                ),
                Row(
                    Column("renotify_interval_days", css_class="col-md-4"),
                    Column("is_active", css_class="col-md-4"),
                    Column("is_muted", css_class="col-md-4"),
                    css_class="row g-3",
                ),
            ),
            Fieldset(
                _("Notifications"),
                "channels",
            ),
        ]
        if is_admin:
            layout_fields.append(Row(Column("tenant", css_class="col-md-6"), css_class="row g-3"))
        layout_fields += [
            HTML('<div class="mt-4"></div>'),
            Submit("submit", _("Save Alert Rule"), css_class="btn btn-primary"),
            HTML(
                '<a href="{% url \'extras:alertrule_list\' %}" class="btn btn-outline-secondary ms-2" '
                'data-no-dirty-track="true">' + str(_("Cancel")) + "</a>"
            ),
        ]
        self.helper.layout = Layout(*layout_fields)

    def clean(self):
        cleaned_data = super().clean()
        _resolve_nonadmin_write_tenant(self, get_current_user())
        self._validate_channel_scope(cleaned_data)
        return cleaned_data

    def _validate_channel_scope(self, cleaned_data):
        """Reject channel attachments that can never deliver for this rule.

        A rule notifies only through channels of its own scope: a tenant rule
        through its own tenant's channels, a platform-wide rule through
        platform-wide channels. Without this guard such an attachment is
        accepted and then silently never delivers (issue #567, WP3/WP5).
        """
        channels = cleaned_data.get("channels")
        if not channels:
            return
        effective_tenant = cleaned_data.get("tenant")
        if "tenant" not in self.fields:
            # Non-admin writes carry no tenant field: an edit keeps the
            # record's tenant and a create is bound to the active tenant
            # scope (see ``_resolve_nonadmin_write_tenant``).
            effective_tenant = self.instance.tenant if self.instance.pk else get_current_tenant()
        errors = alert_rule_channel_scope_errors(channels, effective_tenant.pk if effective_tenant else None)
        if errors:
            raise forms.ValidationError({"channels": errors})

    def save(self, commit=True):
        instance = super().save(commit=False)
        tenant = _resolve_nonadmin_write_tenant(self, get_current_user())
        if tenant is not None:
            instance.tenant = tenant
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class NotificationChannelForm(TenantScopedFormMixin, forms.ModelForm):
    """Channel config via typed, per-type fields rather than a raw JSON blob.

    The model stores everything in a single ``config`` JSONField, but users
    should never have to hand-build that JSON. We expose friendly fields
    (webhook URL, recipient list, recipient users) and assemble ``config``
    on save. ``form-toggles.ts`` shows only the fields relevant to the
    selected channel type.
    """

    # Admin-visible tenant stays required once a Tenant row exists.
    tenant_required = True

    webhook_url = forms.URLField(
        required=False,
        label=_("Incoming webhook URL"),
        widget=forms.URLInput(attrs={"placeholder": "https://hooks.slack.com/services/..."}),
        help_text=_("Paste the incoming-webhook URL from Slack or Microsoft Teams."),
    )
    email_recipients = forms.CharField(
        required=False,
        label=_("Recipient email addresses"),
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "alice@example.com, bob@example.com"}),
        help_text=_("Comma- or newline-separated email addresses."),
    )
    in_app_recipient_users = forms.ModelMultipleChoiceField(
        required=False,
        queryset=None,
        label=_("Specific recipients"),
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
        help_text=_("Optional. Leave empty to notify every member of this channel's tenant."),
    )

    field_order = [
        "name",
        "channel_type",
        "webhook_url",
        "email_recipients",
        "in_app_recipient_users",
        "enabled",
        "tenant",
    ]

    class Meta:
        model = NotificationChannel
        fields = ["name", "channel_type", "enabled", "tenant"]
        widgets = {
            "channel_type": forms.Select(attrs={"class": "form-select"}),
            "tenant": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        User = get_user_model()

        user = get_current_user()
        is_admin = bool(user and (user.is_superuser or getattr(user, "is_staff", False)))

        # Scope the in-app recipient choices: admins see everyone, tenant
        # users see only their own tenant's members.
        if is_admin:
            self.fields["in_app_recipient_users"].queryset = User.objects.filter(is_active=True)
        else:
            from core.managers import get_current_tenant

            tenant = get_current_tenant()
            if tenant:
                self.fields["in_app_recipient_users"].queryset = User.objects.filter(
                    asset_holder_profiles__tenant=tenant, is_active=True
                ).distinct()
            else:
                self.fields["in_app_recipient_users"].queryset = User.objects.none()
            if "tenant" in self.fields:
                self.fields.pop("tenant")

        # Pre-fill typed fields from the stored config JSON (edit view).
        config = (self.instance.config or {}) if self.instance else {}
        if config:
            self.initial.setdefault("webhook_url", config.get("webhook_url", ""))
            recipients = config.get("recipients") or []
            if recipients:
                self.initial.setdefault("email_recipients", "\n".join(recipients))
            recipient_users = config.get("recipient_users") or []
            if recipient_users:
                self.initial.setdefault("in_app_recipient_users", recipient_users)

    def clean(self):

        cleaned = super().clean()
        channel_type = cleaned.get("channel_type")
        config = {}

        if channel_type == NotificationChannel.TYPE_EMAIL:
            raw = cleaned.get("email_recipients") or ""
            parts = [p.strip() for p in re.split(r"[,\n;]+", raw) if p.strip()]
            if not parts:
                self.add_error("email_recipients", _("Enter at least one recipient email address."))
            else:
                bad = []
                for addr in parts:
                    try:
                        validate_email(addr)
                    except DjangoValidationError:
                        bad.append(addr)
                if bad:
                    self.add_error(
                        "email_recipients",
                        _("Invalid email address(es): %(bad)s") % {"bad": ", ".join(bad)},
                    )
                else:
                    config["recipients"] = parts

        elif channel_type in (NotificationChannel.TYPE_SLACK, NotificationChannel.TYPE_TEAMS):
            url = (cleaned.get("webhook_url") or "").strip()
            if not url:
                self.add_error("webhook_url", _("This channel type requires an incoming webhook URL."))
            else:
                # SSRF guard: apply the same boundary check used by WebhookEndpoint.clean
                # and the DRF serializer so the UI form cannot persist a loopback/
                # private/metadata URL.
                try:
                    validate_external_url(url)
                except DjangoValidationError as exc:
                    self.add_error("webhook_url", exc.message)
                else:
                    config["webhook_url"] = url

        elif channel_type == NotificationChannel.TYPE_IN_APP:
            users = cleaned.get("in_app_recipient_users")
            if users:
                config["recipient_users"] = [u.pk for u in users]
            # Empty is valid — delivery falls back to all tenant members.

        self._assembled_config = config
        _resolve_nonadmin_write_tenant(self, get_current_user())
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.config = getattr(self, "_assembled_config", {}) or {}
        tenant = _resolve_nonadmin_write_tenant(self, get_current_user())
        if tenant is not None:
            instance.tenant = tenant
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ObjectChangeFilterForm(FilterForm):
    from core.filters import ObjectChangeFilterSet

    filterset_class = ObjectChangeFilterSet


class JournalEntryFilterForm(FilterForm):
    from extras.filters import JournalEntryFilterSet

    filterset_class = JournalEntryFilterSet


class AlertRuleFilterForm(FilterForm):
    from extras.filters import AlertRuleFilterSet

    filterset_class = AlertRuleFilterSet


class AlertLogFilterForm(FilterForm):
    from extras.filters import AlertLogFilterSet

    filterset_class = AlertLogFilterSet


class NotificationChannelFilterForm(FilterForm):
    from extras.filters import NotificationChannelFilterSet

    filterset_class = NotificationChannelFilterSet


class ReportTemplateFilterForm(FilterForm):
    from extras.filters import ReportTemplateFilterSet

    filterset_class = ReportTemplateFilterSet


class ScheduledReportFilterForm(FilterForm):
    from extras.filters import ScheduledReportFilterSet

    filterset_class = ScheduledReportFilterSet
