from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Column, Layout, Row, Submit
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core.forms import FilterForm, TenantScopedFormMixin
from core.managers import get_current_tenant
from extras.customfields import CustomFieldModelFormMixin
from extras.models import Tag

from ..filters import AssetHolderFilterSet
from ..models import AssetHolder, Tenant


class AssetHolderForm(TenantScopedFormMixin, CustomFieldModelFormMixin, forms.ModelForm):
    tenant_required = True
    tenant_autoset_when_single = True

    tenant = forms.ModelChoiceField(
        queryset=Tenant.objects.all(), required=False, widget=forms.Select(attrs={"class": "form-select"})
    )
    user = forms.ModelChoiceField(
        queryset=get_user_model().objects.all(),
        required=False,
        label=_("Linked User account"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "data-tomselect-tags": "true"}),
    )

    class Meta:
        model = AssetHolder
        fields = [
            "first_name",
            "last_name",
            "upn",
            "email",
            "tenant",
            "user",
            "description",
            "comments",
            "tags",
        ]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "upn": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "comments": forms.Textarea(attrs={"class": "form-control", "rows": 5}),
        }
        help_texts = {
            "upn": _(
                "User Principal Name: the sign-in address in Active Directory or Entra ID "
                "(for example, jdoe@corp.com). Used to match this holder during directory synchronization."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True

        # Filter the user choices to only show unlinked users in the active tenant plus the currently linked user
        UserClass = get_user_model()

        tenant = getattr(self.instance, "tenant", None) or get_current_tenant()
        if tenant:
            unassigned_users = UserClass.objects.exclude(asset_holder_profiles__tenant=tenant)
        else:
            unassigned_users = UserClass.objects.filter(asset_holder_profiles__isnull=True)

        if self.instance and self.instance.pk and self.instance.user:
            assigned_user_pk = self.instance.user.pk
            self.fields["user"].queryset = UserClass.objects.filter(
                Q(pk__in=unassigned_users.values_list("pk", flat=True)) | Q(pk=assigned_user_pk)
            )
        else:
            self.fields["user"].queryset = unassigned_users

        button_text = _("Update") if self.instance and self.instance.pk else _("Create")
        cancel_url = reverse("organization:assetholder_list")

        self.helper.layout = Layout(
            Row(
                Column("first_name", css_class="form-group col-md-6 mb-0"),
                Column("last_name", css_class="form-group col-md-6 mb-0"),
                css_class="mb-3",
            ),
            Row(
                Column("upn", css_class="form-group col-md-6 mb-0"),
                Column("email", css_class="form-group col-md-6 mb-0"),
                css_class="mb-3",
            ),
            Row(
                Column("tenant", css_class="form-group col-md-6 mb-0"),
                Column("user", css_class="form-group col-md-6 mb-0"),
                css_class="mb-3",
            ),
            "description",
            "comments",
            "tags",
            HTML('<div class="mt-4"></div>'),
            Submit("submit", button_text, css_class="btn btn-primary"),
            HTML(format_html('<a href="{}" class="btn btn-outline-secondary ms-2">{}</a>', cancel_url, _("Cancel"))),
        )
        self.append_custom_fields_to_layout()


class AssetHolderFilterForm(FilterForm):
    filterset_class = AssetHolderFilterSet
