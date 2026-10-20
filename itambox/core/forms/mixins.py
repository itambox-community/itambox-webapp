# itambox/core/forms/mixins.py
#
# Framework-level form mixins and base classes.
# No dependency on extras (or any other domain app).
import re

import django_filters
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Submit
from django import forms
from django.apps import apps
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core import search as core_search
from core.forms.base import TenantScopedFormMixin, apply_tom_select
from core.forms.choices import apply_empty_choice_first, empty_choice_first
from itambox.utils import get_model_viewname


def object_type_choices():
    """Choices for the search form's ``obj_type`` field.

    Evaluated per call, never at import time: the per-app ``search.py`` modules
    fill ``SEARCH_INDEXES`` through ``@register_search`` while the app registry
    becomes ready, which happens after this module is imported. A module-level
    constant therefore captured the still-empty registry for the lifetime of
    the process (#919).
    """
    return [
        (
            f"{model._meta.app_label}.{model._meta.model_name}",
            f"{model._meta.app_label.capitalize()} | {model._meta.verbose_name.capitalize()}",
        )
        for model in sorted(core_search.SEARCH_INDEXES.keys(), key=lambda m: (m._meta.app_label, m._meta.verbose_name))
    ]


def cancel_button(cancel_url, *, dismiss=False, no_dirty_track=False, label=None):
    """Build a normal navigation link or a modal-only dismiss button."""
    label = label or _("Cancel")
    css_class = "btn btn-outline-secondary ms-2"
    if dismiss:
        return HTML(
            format_html(
                '<button type="button" class="{}" data-bs-dismiss="modal">{}</button>',
                css_class,
                label,
            )
        )
    if no_dirty_track:
        return HTML(
            format_html('<a href="{}" class="{}" data-no-dirty-track="true">{}</a>', cancel_url, css_class, label)
        )
    return HTML(format_html('<a href="{}" class="{}">{}</a>', cancel_url, css_class, label))


class SearchForm(TenantScopedFormMixin, forms.Form):
    q = forms.CharField(
        label=_("Search"), widget=forms.TextInput(attrs={"placeholder": _("Search ITAMbox"), "class": "form-control"})
    )
    obj_type = forms.MultipleChoiceField(
        label=_("Object types"),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "id": "id_obj_type_select", "data-tom-select": ""}),
    )
    lookup_choices = (
        ("icontains", _("Partial match")),
        ("iexact", _("Exact match")),
        ("istartswith", _("Starts with")),
        ("iendswith", _("Ends with")),
        # No 'iregex' — the search view whitelists lookups to the four above (ReDoS guard),
        # so advertising a regex choice here was dead/misleading config.
    )
    lookup = forms.ChoiceField(
        label=_("Lookup"),
        choices=lookup_choices,
        required=False,
        initial="icontains",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The registry is only complete once the app registry is ready, so the
        # object-type choices are resolved per construction, not at import time.
        self.fields["obj_type"].choices = object_type_choices()


class JournalEntryForm(TenantScopedFormMixin, forms.Form):
    comment = forms.CharField(widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))


class ConfirmationForm(TenantScopedFormMixin, forms.Form):
    return_url = forms.CharField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if kwargs.get("initial") and "return_url" in kwargs["initial"]:
            self.fields["return_url"].initial = kwargs["initial"]["return_url"]
        elif instance:
            try:
                list_view_name = get_model_viewname(instance.__class__, "list")
                self.fields["return_url"].initial = reverse(list_view_name)
            except NoReverseMatch:
                if hasattr(instance, "get_absolute_url"):
                    self.fields["return_url"].initial = instance.get_absolute_url()


BULK_EDIT_FIELD_BLACKLIST = {
    "id",
    "pk",
    "created_at",
    "updated_at",
    "deleted_at",
    "last_audited",
    "last_audited_by",
    "signed_at",
    "accepted_date",
    "verification_hash",
    "slug",
}

BULK_EDIT_FIELD_TYPE_MAP = {
    "CharField": forms.CharField,
    "TextField": lambda **kw: forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), **kw),
    "IntegerField": forms.IntegerField,
    "PositiveIntegerField": forms.IntegerField,
    "BigIntegerField": forms.IntegerField,
    "PositiveBigIntegerField": forms.IntegerField,
    "SmallIntegerField": forms.IntegerField,
    "PositiveSmallIntegerField": forms.IntegerField,
    "FloatField": forms.FloatField,
    "DecimalField": forms.DecimalField,
    "BooleanField": forms.BooleanField,
    "NullBooleanField": forms.NullBooleanField,
    "DateField": forms.DateField,
    "DateTimeField": forms.DateTimeField,
    "EmailField": forms.EmailField,
    "URLField": forms.URLField,
    "ForeignKey": forms.ChoiceField,
}


def _scoped_choices(model):
    """Rows of ``model`` visible in the current scope (explicit, not manager-ambient)."""
    # inline import: app-registry: core.managers imports models at call time
    from core.managers import Scope

    qs = model.objects.all()
    if hasattr(qs, "for_scope"):
        return qs.for_scope(Scope.current())
    return qs


class BulkEditForm(forms.Form):
    _selected_fields = forms.MultipleChoiceField(widget=forms.MultipleHiddenInput(), required=False)
    add_tags = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label=_("Add Tags"),
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
    )
    remove_tags = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label=_("Remove Tags"),
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tom-select": ""}),
    )

    def __init__(self, *args, model=None, **kwargs):
        Tag = apps.get_model("extras", "Tag")
        super().__init__(*args, **kwargs)
        self.fields["add_tags"].queryset = Tag.objects.all()
        self.fields["remove_tags"].queryset = Tag.objects.all()

        if model is None:
            return

        from django.db.models import ForeignKey, ManyToManyField
        from django.db.models.fields import related

        choices = [
            ("add_tags", "add_tags"),
            ("remove_tags", "remove_tags"),
        ]
        for field in model._meta.get_fields():
            if field.name in BULK_EDIT_FIELD_BLACKLIST:
                continue
            if not field.editable:
                continue
            if isinstance(field, (ManyToManyField, related.RelatedField)) and not isinstance(field, ForeignKey):
                continue
            if field.auto_created and field.name.endswith("_ptr"):
                continue
            if getattr(field, "primary_key", False):
                continue

            internal_type = field.get_internal_type()
            if getattr(field, "choices", None):
                form_field_cls = forms.ChoiceField
            else:
                form_field_cls = BULK_EDIT_FIELD_TYPE_MAP.get(internal_type)
            if form_field_cls is None:
                continue

            field_kwargs = {
                "label": getattr(field, "verbose_name", field.name).title(),
                "required": False,
            }

            if getattr(field, "choices", None):
                field_kwargs["choices"] = [("", "---------")] + list(field.choices)
            elif isinstance(field, ForeignKey):
                related_model = field.remote_field.model
                if related_model:
                    field_kwargs["choices"] = [("", "---------")] + [
                        (obj.pk, str(obj)) for obj in _scoped_choices(related_model)[:200]
                    ]

            self.fields[field.name] = form_field_cls(**field_kwargs)
            choices.append((field.name, field.name))

        self.fields["_selected_fields"].choices = choices

        # Auto-apply Bootstrap classes and TomSelect attribute to all fields in BulkEditForm
        for field_name, field in self.fields.items():
            if field_name == "_selected_fields":
                continue

            # Apply dynamic classes
            existing_classes = field.widget.attrs.get("class", "").split()
            if isinstance(field.widget, (forms.CheckboxInput, forms.RadioSelect, forms.CheckboxSelectMultiple)):
                target_class = "form-check-input"
            elif isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
                target_class = "form-select"
            else:
                target_class = "form-control"

            if target_class not in existing_classes:
                existing_classes.append(target_class)
                field.widget.attrs["class"] = " ".join(existing_classes)

        # TomSelect attribute through the shared helper (same exclusions as the former local loop)
        apply_tom_select(self.fields)


class CrispyFormMixin:
    """
    Form mixin to auto-initialize FormHelper with standard settings,
    reducing crispy FormHelper boilerplate across all forms.
    """

    def __init__(self, *args, **kwargs):
        self.quick_add_dismiss = kwargs.pop("quick_add_dismiss", False)
        super().__init__(*args, **kwargs)
        if not hasattr(self, "helper") or self.helper is None:
            self.helper = FormHelper(self)
            self.helper.form_method = "post"
            self.helper.form_tag = True

    def action_buttons(self, cancel_url, dismiss=False):
        """Return the standard Submit + Cancel button row as crispy elements.

        Splice into a form's Layout to avoid re-implementing the same block in
        every form, e.g.::

            self.helper.layout = Layout(..., *self.action_buttons(cancel_url))

        ``cancel_url`` is a resolved URL string. The submit label reflects
        create vs. update based on the bound instance.
        """
        from django.utils.translation import gettext as _

        label = _("Update") if getattr(self, "instance", None) and self.instance.pk else _("Create")
        return [
            HTML('<div class="mt-4"></div>'),
            Submit("submit", label, css_class="btn btn-primary"),
            cancel_button(cancel_url, dismiss=dismiss, no_dirty_track=True),
        ]


class SlugModelForm(CrispyFormMixin, forms.ModelForm):
    pass


class FilterForm(forms.Form):
    filterset_class = None

    def __init__(self, *args, **kwargs):
        self.queryset = kwargs.pop("queryset", None)
        super(FilterForm, self).__init__(*args, **kwargs)

        if self.filterset_class is None:
            raise NotImplementedError("'filterset_class' must be defined on the FilterForm subclass.")

        filterset_data = args[0] if args else kwargs.get("data", None)
        self.filterset = self.filterset_class(filterset_data, queryset=self.queryset)

        for name, filter_field in self.filterset.filters.items():
            if hasattr(filter_field, "field"):
                self.fields[name] = filter_field.field
            else:
                field_type = forms.CharField
                if isinstance(filter_field, django_filters.BooleanFilter):
                    field_type = forms.BooleanField
                elif isinstance(filter_field, django_filters.NumberFilter):
                    field_type = forms.DecimalField
                elif isinstance(filter_field, django_filters.DateFilter):
                    field_type = forms.DateField
                elif isinstance(filter_field, django_filters.DateTimeFilter):
                    field_type = forms.DateTimeField
                elif isinstance(filter_field, django_filters.MultipleChoiceFilter):
                    self.fields[name] = forms.MultipleChoiceField(
                        label=filter_field.label if filter_field.label else name.replace("_", " ").capitalize(),
                        required=False,
                        choices=empty_choice_first(filter_field.extra.get("choices", [])),
                    )
                    continue

                self.fields[name] = field_type(
                    label=filter_field.label if filter_field.label else name.replace("_", " ").capitalize(),
                    required=False,
                )

        self.helper = FormHelper()
        self.helper.form_method = "get"
        self.helper.form_tag = False

        ajax_fields = getattr(self, "ajax_fields", None)
        if ajax_fields:
            self.setup_ajax_fields(ajax_fields, filterset_data)

        # TomSelect attribute through the shared helper (same exclusions as the former local loop)
        apply_tom_select(self.fields)

        self.normalise_choice_order()

    def normalise_choice_order(self):
        """Keep the blank entry first in every hand-assembled choice list.

        Model-backed fields are skipped: Django renders ``empty_label`` first
        already and replacing the lazy choices would freeze the queryset.
        """
        for field in self.fields.values():
            apply_empty_choice_first(field)

    def setup_ajax_fields(self, ajax_fields, filterset_data):
        for field_name, config in ajax_fields.items():
            if field_name not in self.fields:
                continue

            field = self.fields[field_name]
            url = reverse(config["url_name"])

            field.widget.attrs.update(
                {
                    "data-tom-select": "",
                    "data-tom-select-url": url,
                    "data-tom-select-value-field": config.get("value_field", "id"),
                    "data-tom-select-label-field": config.get("label_field", "name"),
                }
            )

            if hasattr(field, "queryset"):
                selected_vals = []
                if filterset_data:
                    if hasattr(filterset_data, "getlist"):
                        selected_vals = filterset_data.getlist(field_name)
                    else:
                        val = filterset_data.get(field_name)
                        if val:
                            if isinstance(val, list):
                                selected_vals = val
                            else:
                                selected_vals = [val]
                elif self.initial and field_name in self.initial:
                    val = self.initial[field_name]
                    if isinstance(val, list):
                        selected_vals = val
                    else:
                        selected_vals = [val]

                # Convert model instances to PK/to_field_name values if necessary, and filter empty values
                to_field = getattr(field, "to_field_name", None) or "pk"
                cleaned_vals = []
                for val in selected_vals:
                    if val is None or val == "":
                        continue
                    if hasattr(val, to_field):
                        cleaned_vals.append(getattr(val, to_field))
                    elif hasattr(val, "pk"):
                        cleaned_vals.append(val.pk)
                    else:
                        cleaned_vals.append(val)

                if cleaned_vals:
                    filter_kwargs = {f"{to_field}__in": cleaned_vals}
                    field.queryset = field.queryset.filter(**filter_kwargs)
                else:
                    field.queryset = field.queryset.none()

    def search(self):
        if self.is_valid():
            return self.filterset.qs
        return self.filterset.queryset

    @property
    def applied_filters(self):
        if not self.filterset or not self.filterset.data:
            return {}

        applied = {}
        ignored_params = ["page", "per_page", "q"]

        for name, _filter_field in self.filterset.filters.items():
            if name in ignored_params:
                continue

            value = (
                self.filterset.data.getlist(name)
                if hasattr(self.filterset.data, "getlist")
                else self.filterset.data.get(name)
            )

            if value:
                if isinstance(value, list):
                    value = [v for v in value if v != ""]
                    if value:
                        applied[name] = value
                elif value != "":
                    applied[name] = value

        return applied


class ColorFieldFormMixin:
    """
    Mixin for forms with a 'color' hex field. Ensures '#' is prepended for the picker
    and cleaned up to raw hex when validating/saving.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self, "instance") and self.instance and getattr(self.instance, "color", None):
            self.initial["color"] = f"#{self.instance.color}"

    def clean_color(self):
        color = self.cleaned_data.get("color") or ""
        if color.startswith("#"):
            color = color[1:]
        if not color:
            return ""
        # Enforce 6 HEX digits — not just length. A non-hex 6-char value (e.g. '"><img')
        # would otherwise be interpolated into a style= attribute by render_color.
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
            raise forms.ValidationError(_("Enter a valid 6-digit hex color (0-9, A-F)."))
        return color
