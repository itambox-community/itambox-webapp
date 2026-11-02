from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Layout, Submit
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from core.forms import FilterForm, TenantScopedFormMixin

from ..filters import ContactRoleFilterSet
from ..models import Contact, ContactAssignment, ContactRole
from .helpers import add_standard_buttons


class ContactRoleForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = ContactRole
        fields = ["name", "slug", "description"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control", "slugify": "name"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.helper.layout = Layout("name", "slug", "description")

        add_standard_buttons(self.helper, self.instance, "organization:contactrole_list")


def resolve_assignment_target(content_type, object_id):
    """Return the assignment target visible in the active scope, or ``None``.

    The target is looked up through the target model's default manager narrowed
    with ``filter_by_tenant()``, so an object outside the active tenant scope
    resolves exactly like a missing one and its existence is never disclosed.
    """
    model_class = content_type.model_class() if content_type is not None else None
    if model_class is None:
        return None
    queryset = model_class._default_manager.all()
    if hasattr(queryset, "filter_by_tenant"):
        queryset = queryset.filter_by_tenant()
    try:
        return queryset.filter(pk=object_id).first()
    except (TypeError, ValueError):
        return None


class ContactAssignmentForm(TenantScopedFormMixin, forms.ModelForm):
    contact = forms.ModelChoiceField(
        queryset=Contact.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    role = forms.ModelChoiceField(
        queryset=ContactRole.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    priority = forms.ChoiceField(
        choices=[
            ("", "---------"),
            ("primary", _("Primary")),
            ("secondary", _("Secondary")),
            ("tertiary", _("Tertiary")),
            ("inactive", _("Inactive")),
        ],
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = ContactAssignment
        fields = ["contact", "role", "priority"]

    def __init__(self, *args, **kwargs):
        content_type = kwargs.pop("content_type", None)
        object_id = kwargs.pop("object_id", None)
        super().__init__(*args, **kwargs)
        self.content_type = content_type
        self.object_id = object_id

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "contact",
            "role",
            "priority",
        )
        button_text = "Assign"
        self.helper.layout.append(HTML('<div class="mt-4"></div>'))
        self.helper.layout.append(Submit("submit", button_text, css_class="btn btn-primary"))
        self.helper.layout.append(
            HTML('<button type="button" class="btn btn-outline-secondary ms-2" data-bs-dismiss="modal">Cancel</button>')
        )

    def _validate_target(self):
        if resolve_assignment_target(self.content_type, self.object_id) is None:
            raise ValidationError(_("Referenced object was not found in the current tenant."))

    def clean(self):
        cleaned_data = super().clean()
        self._validate_target()
        return cleaned_data

    def save(self, commit=True):
        # Re-validate at save time so a form bound to a tampered pair can never
        # persist a target outside the active scope.
        self._validate_target()
        instance = super().save(commit=False)
        if self.content_type and self.object_id:
            instance.content_type = self.content_type
            instance.object_id = self.object_id
        if commit:
            instance.save()
        return instance


class ContactRoleFilterForm(FilterForm):
    filterset_class = ContactRoleFilterSet
