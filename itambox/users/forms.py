# This file is adapted from NetBox (https://github.com/netbox-community/netbox).
# Copyright (c) DigitalOcean, LLC.
# Licensed under the Apache License, Version 2.0.

import ipaddress
import logging

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Fieldset, Layout, Row
from django import forms
from django.conf import settings  # Import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import translation
from django.utils.translation import gettext_lazy as _

from core.forms import BulkEditForm, FilterForm, TenantScopedFormMixin
from organization.access import accessible_tenant_ids, get_descendant_tenant_group_ids
from organization.forms.helpers import add_standard_buttons
from organization.models import (
    Membership,
    Role,
    RoleGrant,
    RoleGrantScope,
    Tenant,
    TenantGroup,
)
from organization.services.role_grant_validation import validate_role_grant

from .filters import UserFilterSet, UserGroupFilterSet

# Import UserPreference from this app's models
from .models import GroupMembership, UserGroup, UserPreference
from .services import DEFAULT_WORKSPACE_KEY, workspace_choices

logger = logging.getLogger(__name__)
User = get_user_model()


class UserProfileForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]


class UserForm(TenantScopedFormMixin, forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        required=False,
        help_text=_("Raw passwords are not stored. If editing a user, leave this blank to keep the current password."),
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active", "can_login", "is_staff", "is_superuser"]
        help_texts = {
            "can_login": _(
                "If unchecked, this user cannot sign in with a password or SSO. "
                "API tokens and the account's active status are unaffected."
            ),
        }
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "can_login": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_staff": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_superuser": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.request_user = user
        super().__init__(*args, **kwargs)
        # A password set on an EXISTING account replaces that account's credential,
        # and a Django password is global: it authenticates the person everywhere
        # they are a member, not only in the tenant whose role granted the
        # operator their permission. It is therefore the same class of global
        # account capability as is_staff / can_login below, and no tenant-scoped
        # permission can authorize it. Provisioning a NEW account has no existing
        # credential to replace and keeps the field.
        self.can_set_password = not (self.instance and self.instance.pk) or bool(
            self.request_user and self.request_user.is_superuser
        )
        if not self.can_set_password:
            del self.fields["password"]
        elif self.instance and self.instance.pk:
            self.fields["password"].required = False
            self.fields["password"].help_text = _("Leave blank to keep the current password.")
        else:
            self.fields["password"].required = True

        # Security check: only superusers can modify is_superuser, is_staff, and can_login
        # (login capability is a global account flag, not a per-tenant setting).
        if not self.request_user or not self.request_user.is_superuser:
            if "is_superuser" in self.fields:
                self.fields["is_superuser"].disabled = True
            if "is_staff" in self.fields:
                self.fields["is_staff"].disabled = True
            if "can_login" in self.fields:
                self.fields["can_login"].disabled = True

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        layout_rows = [
            Row(
                Column("username", css_class="col-md-8"),
                Column("is_active", css_class="col-md-4"),
                css_class="row g-3",
            )
        ]
        if self.can_set_password:
            layout_rows.append("password")
        layout_rows.extend(
            [
                Row(
                    Column("first_name", css_class="col-md-6"),
                    Column("last_name", css_class="col-md-6"),
                    css_class="row g-3",
                ),
                "email",
                Fieldset(
                    _("Permissions"),
                    Row(
                        Column("can_login", css_class="col-md-12"),
                        css_class="row g-3",
                    ),
                    Row(
                        Column("is_staff", css_class="col-md-6"),
                        Column("is_superuser", css_class="col-md-6"),
                        css_class="row g-3",
                    ),
                ),
            ]
        )
        self.helper.layout = Layout(*layout_rows)
        add_standard_buttons(self.helper, self.instance, "users:user_list")

    def clean_is_superuser(self):
        is_superuser = self.cleaned_data.get("is_superuser")
        if is_superuser and (not self.request_user or not self.request_user.is_superuser):
            if self.instance and self.instance.is_superuser:
                return is_superuser
            raise forms.ValidationError(_("Only superusers can grant superuser status."))
        return is_superuser

    def clean_is_staff(self):
        is_staff = self.cleaned_data.get("is_staff")
        if is_staff and (not self.request_user or not self.request_user.is_superuser):
            if self.instance and self.instance.is_staff:
                return is_staff
            raise forms.ValidationError(_("Only superusers can grant staff status."))
        return is_staff

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        if commit:
            user.save()
            self.save_m2m()
        return user


class UserPreferencesForm(TenantScopedFormMixin, forms.Form):
    # Define fields explicitly
    pagination_per_page = forms.ChoiceField(
        choices=settings.PAGINATE_COUNT_CHOICES,
        label=_("Items Per Page"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    theme = forms.ChoiceField(
        choices=UserPreference.THEME_CHOICES,  # Reference choices from model
        required=False,
        label=_("Theme"),
        help_text=_(
            "Automatic follows the light or dark setting of your operating system and updates when it changes."
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    language = forms.ChoiceField(
        choices=settings.LANGUAGES,
        required=False,
        label=_("Language"),
        help_text=_("Interface language. Applies across the whole application."),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    default_workspace = forms.ChoiceField(
        choices=(),
        required=False,
        label=_("Default workspace"),
        help_text=_("Workspace selected automatically when you sign in. You can still switch workspaces at any time."),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, user, *args, **kwargs):
        """Load initial data from UserPreference."""
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["default_workspace"].choices = workspace_choices(self.user)
        self.fields["default_workspace"].initial = ""
        try:
            # Use filter().first() to avoid DoesNotExist exception
            prefs = UserPreference.objects.filter(user=self.user).first()
            if prefs and prefs.data:
                pagination_prefs = prefs.data.get("pagination", {})
                theme_prefs = prefs.data.get("theme", {})

                initial_per_page = pagination_prefs.get("per_page", settings.DEFAULT_PAGINATE_COUNT)
                # New accounts and rows without a theme follow the operating system
                initial_theme = theme_prefs.get("theme", UserPreference.DEFAULT_THEME)

                # Ensure initial value is valid before setting
                if initial_per_page in dict(settings.PAGINATE_COUNT_CHOICES):
                    self.fields["pagination_per_page"].initial = initial_per_page
                else:
                    # Fallback if stored pref is invalid
                    self.fields["pagination_per_page"].initial = settings.DEFAULT_PAGINATE_COUNT

                if initial_theme in dict(UserPreference.THEME_CHOICES):
                    self.fields["theme"].initial = initial_theme
                else:
                    # Fallback if stored pref is invalid
                    self.fields["theme"].initial = UserPreference.DEFAULT_THEME

                initial_workspace = prefs.data.get(DEFAULT_WORKSPACE_KEY, "")
                valid_workspaces = {value for value, _label in self.fields["default_workspace"].choices}
                self.fields["default_workspace"].initial = (
                    initial_workspace if initial_workspace in valid_workspaces else ""
                )

            else:
                # Set defaults if no preferences exist
                self.fields["pagination_per_page"].initial = settings.DEFAULT_PAGINATE_COUNT
                self.fields["theme"].initial = UserPreference.DEFAULT_THEME

        except Exception:
            # broad except: render-degrade: the page must render with default preferences when the stored value cannot be read
            # Fallback to defaults on any error loading preferences
            self.fields["pagination_per_page"].initial = settings.DEFAULT_PAGINATE_COUNT
            self.fields["theme"].initial = UserPreference.DEFAULT_THEME

        # Language initial: stored preference first, else the active language

        valid_languages = dict(settings.LANGUAGES)
        stored_language = None
        try:
            prefs = UserPreference.objects.filter(user=self.user).first()
            if prefs and prefs.data:
                stored_language = prefs.data.get("language")
        # broad except: render-degrade: an unreadable stored language falls back to the active one
        except Exception:
            stored_language = None
        if stored_language not in valid_languages:
            stored_language = translation.get_language()
        if stored_language not in valid_languages:
            stored_language = settings.LANGUAGE_CODE
        self.fields["language"].initial = stored_language

    def save(self):
        """Save form data to UserPreference."""
        prefs, created_at = UserPreference.objects.get_or_create(user=self.user)

        # Ensure prefs.data is initialized as a dict if it's None or not set
        if prefs.data is None:
            prefs.data = {}

        # Update pagination preferences
        if "pagination" not in prefs.data:
            prefs.data["pagination"] = {}
        prefs.data["pagination"]["per_page"] = self.cleaned_data["pagination_per_page"]

        # Update theme preferences
        if "theme" not in prefs.data:
            prefs.data["theme"] = {}
        stored_theme = self.cleaned_data.get("theme")
        prefs.data["theme"]["theme"] = (
            stored_theme if stored_theme in dict(UserPreference.THEME_CHOICES) else UserPreference.DEFAULT_THEME
        )

        # Update language preference (the active cookie is set by the view)
        language = self.cleaned_data.get("language")
        if language and language in dict(settings.LANGUAGES):
            prefs.data["language"] = language

        default_workspace = self.cleaned_data.get("default_workspace", "")
        if default_workspace:
            prefs.data[DEFAULT_WORKSPACE_KEY] = default_workspace
        else:
            prefs.data.pop(DEFAULT_WORKSPACE_KEY, None)

        prefs.save()


class TableConfigForm(forms.Form):
    """
    Form for configuring table columns.
    Adapted from NetBox pattern.
    """

    available_columns = forms.MultipleChoiceField(
        choices=[],
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 10, "class": "form-select available-columns"}),
        label=_("Available Columns"),
    )
    columns = forms.MultipleChoiceField(
        choices=[],
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 10, "class": "form-select selected-columns"}),
        label=_("Selected Columns"),
    )

    def __init__(self, table, *args, **kwargs):
        self.table = table
        user_config = kwargs.pop("user_config", {})  # e.g., {'columns': [...], 'ordering': [...]} or {}
        super().__init__(*args, **kwargs)

        logger.debug("TableConfigForm received user_config: %s", user_config)

        # Determine initial selected columns (priority: user > table default > all)
        default_cols = getattr(table.Meta, "default_columns", None)
        initial_selected_names = user_config.get("columns", default_cols)
        # Treat empty list (from Reset) the same as None — fall back to defaults
        if not initial_selected_names:
            # Fallback if no user pref and no Meta.default_columns
            # Use Meta.fields or all non-excluded fields
            if hasattr(table.Meta, "fields"):
                initial_selected_names = list(table.Meta.fields)
            else:
                exclude = getattr(table.Meta, "exclude", ("pk", "actions"))
                initial_selected_names = [name for name in table.base_columns.keys() if name not in exclude]

        logger.debug("TableConfigForm initial selected names: %s", initial_selected_names)

        # Populate choices based on the table definition
        all_column_choices = {
            name: str(column.verbose_name)
            for name, column in table.columns.items()
            if name
            not in getattr(table.Meta, "exclude_from_config", ("pk", "actions"))  # Allow explicit exclusion from config
        }

        available_choices = []
        selected_choices = []

        # Populate selected_choices based on initial_selected_names order
        for name in initial_selected_names:
            if name in all_column_choices:
                selected_choices.append((name, all_column_choices[name]))

        # Populate available_choices with remaining columns, sorted by verbose name
        selected_names_set = set(initial_selected_names)
        available_choices = sorted(
            [(name, verbose) for name, verbose in all_column_choices.items() if name not in selected_names_set],
            key=lambda item: item[1],  # Sort by verbose name
        )

        # Assign choices to the form fields
        self.fields["available_columns"].choices = available_choices
        self.fields["columns"].choices = selected_choices

        logger.debug("TableConfigForm final available choices: %s", available_choices)
        logger.debug("TableConfigForm final selected choices: %s", selected_choices)

    @property
    def table_name(self):
        if not self.table:
            return None
        app_label = self.table.Meta.model._meta.app_label
        model_name = self.table.Meta.model._meta.model_name
        return f"{app_label}.{model_name}"


class TokenForm(TenantScopedFormMixin, forms.ModelForm):
    expires = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        help_text=_("Optional expiration date. Leave blank for a token that never expires."),
    )
    allowed_ips = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": _("e.g., 192.168.1.0/24, 10.0.0.5")}),
        help_text=_(
            "Comma-separated IPs or CIDR prefixes allowed to use this token. Leave blank to allow any address."
        ),
    )

    class Meta:
        from .models import Token

        model = Token
        fields = ["description", "write_enabled", "allowed_ips", "expires"]
        widgets = {
            "description": forms.TextInput(
                attrs={"class": "form-control", "placeholder": _("e.g., Personal Laptop API Access")}
            ),
            "write_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # form_tag=False because the surrounding template already provides <form> and the
        # CSRF token; setting it here prevents a nested <form> element being rendered.
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("write_enabled", css_class="col-md-6"),
                Column("expires", css_class="col-md-6"),
                css_class="row g-3",
            ),
            "description",
            "allowed_ips",
        )

    def clean_allowed_ips(self):
        raw = self.cleaned_data.get("allowed_ips", "")
        prefixes = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        for prefix in prefixes:
            try:
                ipaddress.ip_network(prefix, strict=False)
            except ValueError:
                raise forms.ValidationError(
                    _('"%(prefix)s" is not a valid IP address or CIDR prefix.') % {"prefix": prefix}
                ) from None
        return prefixes


class UserFilterForm(FilterForm):
    filterset_class = UserFilterSet


class UserBulkEditForm(BulkEditForm):
    def __init__(self, *args, request_user=None, **kwargs):
        self.request_user = request_user
        if "model" not in kwargs:
            kwargs["model"] = get_user_model()
        super().__init__(*args, **kwargs)

        # Remove fields that should not be bulk editable
        forbidden_fields = {"password", "last_login", "username", "first_name", "last_name", "email", "date_joined"}
        for f in forbidden_fields:
            if f in self.fields:
                del self.fields[f]
        if "_selected_fields" in self.fields:
            self.fields["_selected_fields"].choices = [
                c for c in self.fields["_selected_fields"].choices if c[0] not in forbidden_fields
            ]

        self.fields["is_active"] = forms.NullBooleanField(
            required=False,
            label=_("Active"),
            widget=forms.Select(
                choices=(
                    (None, _("No change")),
                    (True, _("Yes")),
                    (False, _("No")),
                ),
                attrs={"class": "form-select"},
            ),
        )
        self.fields["is_staff"] = forms.NullBooleanField(
            required=False,
            label=_("Staff"),
            widget=forms.Select(
                choices=(
                    (None, _("No change")),
                    (True, _("Yes")),
                    (False, _("No")),
                ),
                attrs={"class": "form-select"},
            ),
        )
        self.fields["is_superuser"] = forms.NullBooleanField(
            required=False,
            label=_("Superuser"),
            widget=forms.Select(
                choices=(
                    (None, _("No change")),
                    (True, _("Yes")),
                    (False, _("No")),
                ),
                attrs={"class": "form-select"},
            ),
        )
        self.fields["can_login"] = forms.NullBooleanField(
            required=False,
            label=_("Can log in"),
            widget=forms.Select(
                choices=(
                    (None, _("No change")),
                    (True, _("Yes")),
                    (False, _("No")),
                ),
                attrs={"class": "form-select"},
            ),
        )

        # Security check: only superusers can modify is_superuser, is_staff, and can_login
        if not self.request_user or not self.request_user.is_superuser:
            for f in ("is_superuser", "is_staff", "can_login"):
                if f in self.fields:
                    self.fields[f].disabled = True
                    self.fields[f].widget.attrs["disabled"] = "disabled"

    def clean(self):
        cleaned_data = super().clean()
        selected_fields = self.data.getlist("_selected_fields") if self.data else []

        # Only superusers can change is_superuser / is_staff / can_login
        has_privilege_field_selected = any(f in selected_fields for f in ("is_superuser", "is_staff", "can_login"))
        if has_privilege_field_selected and (not self.request_user or not self.request_user.is_superuser):
            raise forms.ValidationError(_("Only superusers can grant or modify staff, superuser, or login status."))

        # Prevent non-nullable fields from being set to None if they are selected
        for field_name in ["is_active", "is_staff", "is_superuser", "can_login"]:
            if field_name in selected_fields:
                val = cleaned_data.get(field_name)
                # Superusers can edit these fields, so validate that they are
                # not None. Other users were gated above; only validate fields
                # that remain enabled (i.e. is_active).
                if val is None and (self.request_user and self.request_user.is_superuser or field_name == "is_active"):
                    self.add_error(field_name, _("Select Yes or No for this field."))

        return cleaned_data


# --------------------------------------------------------------------------- UserGroup
# UserGroup is an identity-layer construct (relocated here from organization/):
# provider-owned groups may be projected into managed tenants through RoleGrant scopes.


class GroupManagedRoleGrantForm(TenantScopedFormMixin, forms.Form):
    """One additive managed-scope segment of a group RoleGrant."""

    # Deliberate base-manager pickers, narrowed to the owner's managed graph.
    tenant_scoped_choice_exclusions = ("role", "scope_group", "assigned_tenants")

    SCOPE_EXPLICIT = "explicit"
    id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    role = forms.ModelChoiceField(
        queryset=Role.objects.none(),
        required=False,
        label=_("Role"),
        widget=forms.Select(attrs={"class": "form-select managed-role"}),
    )
    managed_scope = forms.ChoiceField(
        choices=(
            (SCOPE_EXPLICIT, _("Specific tenants")),
            (RoleGrantScope.SCOPE_TENANT_GROUP, _("A tenant group and its subgroups")),
            (RoleGrantScope.SCOPE_ALL_MANAGED, _("All managed tenants")),
        ),
        initial=SCOPE_EXPLICIT,
        required=False,
        label=_("Coverage"),
        widget=forms.Select(attrs={"class": "form-select managed-scope"}),
    )
    scope_group = forms.ModelChoiceField(
        queryset=TenantGroup.objects.none(),
        required=False,
        label=_("Tenant group"),
        widget=forms.Select(attrs={"class": "form-select managed-scope-group"}),
    )
    assigned_tenants = forms.ModelMultipleChoiceField(
        queryset=Tenant.objects.none(),
        required=False,
        label=_("Specific tenants"),
        widget=forms.SelectMultiple(attrs={"class": "form-select managed-assigned-tenants"}),
    )

    def __init__(self, *args, owner=None, requesting_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._owner = owner
        self._requesting_user = requesting_user
        if owner is None:
            return
        self.fields["role"].queryset = Role.objects.filter(
            tenant=owner,
            deleted_at__isnull=True,
        ).order_by("name")
        self.fields["scope_group"].queryset = TenantGroup.objects.filter(
            deleted_at__isnull=True,
        ).order_by("name")
        self.fields["assigned_tenants"].queryset = Tenant.objects.filter(
            managed_by=owner,
            deleted_at__isnull=True,
        ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE") or not cleaned.get("role"):
            return cleaned

        owner = self._owner
        role = cleaned["role"]
        if owner is None or not owner.is_provider:
            raise forms.ValidationError(_("Only a tenant that manages other tenants can assign group roles there."))
        if role.tenant_id != owner.pk:
            self.add_error("role", _("Choose a role from this group's tenant."))
            return cleaned

        scope = cleaned.get("managed_scope") or self.SCOPE_EXPLICIT
        cleaned["managed_scope"] = scope
        requested_tenant_ids = None
        if scope == RoleGrantScope.SCOPE_TENANT_GROUP:
            cleaned["assigned_tenants"] = []
            scope_group = cleaned.get("scope_group")
            if scope_group is None:
                self.add_error("scope_group", _("Pick a tenant group."))
                return cleaned
            requested_tenant_ids = set(
                Tenant.objects.filter(
                    managed_by=owner,
                    group_id__in=get_descendant_tenant_group_ids(scope_group.pk),
                    deleted_at__isnull=True,
                ).values_list("pk", flat=True)
            )
        elif scope == self.SCOPE_EXPLICIT:
            cleaned["scope_group"] = None
            assigned = list(cleaned.get("assigned_tenants") or [])
            if not assigned:
                self.add_error("assigned_tenants", _("Pick at least one managed tenant."))
                return cleaned
            if any(tenant.managed_by_id != owner.pk for tenant in assigned):
                self.add_error(
                    "assigned_tenants",
                    _("Choose only tenants managed by this group's tenant."),
                )
                return cleaned
            requested_tenant_ids = {tenant.pk for tenant in assigned}
        else:
            cleaned["scope_group"] = None
            cleaned["assigned_tenants"] = []

        try:
            validate_role_grant(
                self._requesting_user,
                role,
                owner,
                scope_type=scope,
                requested_tenant_ids=requested_tenant_ids,
            )
        except forms.ValidationError as exc:
            raise forms.ValidationError(exc.messages) from None
        return cleaned


class BaseGroupManagedRoleGrantFormSet(forms.BaseFormSet):
    """Reject duplicate scope targets while allowing additive rows per role."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        signatures = set()
        explicit_tenants_by_role = {}
        row_count_by_role = {}
        all_managed_roles = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            cleaned = form.cleaned_data
            if cleaned.get("DELETE") or not cleaned.get("role"):
                continue
            role_id = cleaned["role"].pk
            scope = cleaned.get("managed_scope") or GroupManagedRoleGrantForm.SCOPE_EXPLICIT
            row_count_by_role[role_id] = row_count_by_role.get(role_id, 0) + 1
            if scope == RoleGrantScope.SCOPE_TENANT_GROUP:
                signature = (role_id, scope, cleaned["scope_group"].pk)
            elif scope == RoleGrantScope.SCOPE_ALL_MANAGED:
                all_managed_roles.add(role_id)
                signature = (role_id, scope, None)
            else:
                tenant_ids = {tenant.pk for tenant in cleaned.get("assigned_tenants") or []}
                overlap = explicit_tenants_by_role.setdefault(role_id, set()) & tenant_ids
                if overlap:
                    raise forms.ValidationError(_("The same role includes a managed tenant more than once."))
                explicit_tenants_by_role[role_id].update(tenant_ids)
                signature = None
            if signature is not None and signature in signatures:
                raise forms.ValidationError(_("The same role and coverage are listed more than once."))
            if signature is not None:
                signatures.add(signature)
        if any(row_count_by_role[role_id] > 1 for role_id in all_managed_roles):
            raise forms.ValidationError(_("'All managed tenants' already covers every other selection for that role."))


GroupManagedRoleGrantFormSet = forms.formset_factory(
    GroupManagedRoleGrantForm,
    formset=BaseGroupManagedRoleGrantFormSet,
    extra=1,
    can_delete=True,
)

GROUP_MANAGED_FORMSET_PREFIX = "managed"


class UserGroupForm(TenantScopedFormMixin, forms.ModelForm):
    """Edit a tenant-owned group through canonical RBAC aggregates.

    The role picker authors own-tenant scopes; the managed formset adds explicit,
    tenant-group, or all-managed scopes to the same per-role grant. Members are
    tenant Membership rows, never global users. Manual group memberships are
    reconciled here while directory-managed rows remain owned by their source.
    """

    # Owner, role and tenant pickers are deliberate base-manager querysets
    # narrowed by the requesting user's authority; ``tenant`` stays required.
    tenant_scoped_choice_exclusions = ("tenant", "roles")

    name = forms.CharField(
        max_length=100,
        label=_("Name"),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    description = forms.CharField(
        required=False,
        label=_("Description"),
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    roles = forms.ModelMultipleChoiceField(
        queryset=Role.objects.none(),
        required=False,
        label=_("Roles in this tenant"),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
        help_text=_("Roles that group members receive in this tenant."),
    )
    members = forms.ModelMultipleChoiceField(
        queryset=Membership.objects.none(),
        required=False,
        label=_("Members"),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
        help_text=_(
            "Only active members of this tenant can be added. Existing inactive members remain visible. "
            "To remove a directory-managed member, update the connected directory."
        ),
    )
    tenant = forms.ModelChoiceField(
        queryset=Tenant.objects.none(),
        required=True,
        label=_("Owning tenant"),
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text=_("The tenant that owns this group and its members."),
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Active"),
    )

    class Meta:
        model = UserGroup
        fields = ["name", "description", "roles", "members", "tenant", "is_active"]

    @staticmethod
    def _role_label(role):
        return role.name

    @staticmethod
    def _membership_label(membership):
        user = membership.user
        label = user.get_full_name().strip() or user.username
        if user.email:
            label = f"{label} ({user.email})"
        if not membership.is_active:
            label = _("%(member)s [inactive]") % {"member": label}
        return label

    def __init__(self, *args, user=None, tenant=None, **kwargs):
        self._requesting_user = user
        super().__init__(*args, **kwargs)
        # ModelForm applies cleaned values to ``instance`` after ``clean()``.
        # Keep the persisted activation state explicitly so reactivation can be
        # treated as restoring every retained grant, not as a metadata-only edit.
        self._initial_is_active = self.instance.is_active if self.instance.pk else None
        is_superuser = bool(user is None or user.is_superuser)
        self._submitted_owner_changed = False

        tenant_qs = Tenant.objects.filter(deleted_at__isnull=True).order_by("name")
        if not is_superuser:
            manageable_ids = [
                candidate.pk
                for candidate in tenant_qs.filter(pk__in=accessible_tenant_ids(user))
                if user.has_perm("users.add_usergroup", obj=candidate)
            ]
            tenant_qs = tenant_qs.filter(pk__in=manageable_ids)

        owner = None
        if self.instance.pk:
            owner = self.instance.tenant
            if self.is_bound:
                raw_owner = self.data.get("tenant")
                self._submitted_owner_changed = raw_owner not in (None, "") and str(raw_owner) != str(owner.pk)
            # unscoped: an existing group keeps rendering its owner even if that tenant was soft-deleted
            self.fields["tenant"].queryset = Tenant.all_objects.filter(pk=owner.pk)
            self.fields["tenant"].initial = owner.pk
            self.fields["tenant"].disabled = True
        else:
            self.fields["tenant"].queryset = tenant_qs
            if self.is_bound:
                try:
                    owner = tenant_qs.filter(pk=self.data.get("tenant")).first()
                except (TypeError, ValueError):
                    owner = None
            elif tenant is not None and tenant_qs.filter(pk=tenant.pk).exists():
                owner = tenant
                self.fields["tenant"].initial = tenant.pk

        if owner is not None:
            self.fields["roles"].queryset = (
                Role.objects.filter(
                    tenant=owner,
                    deleted_at__isnull=True,
                )
                .select_related("tenant")
                .order_by("name")
            )
            membership_qs = Membership.objects.filter(tenant=owner)
            if self.instance.pk:
                membership_qs = membership_qs.filter(
                    Q(is_active=True) | Q(group_memberships__user_group=self.instance)
                ).distinct()
            else:
                membership_qs = membership_qs.filter(is_active=True)
            self.fields["members"].queryset = membership_qs.select_related(
                "user",
                "tenant",
            ).order_by(
                "user__last_name",
                "user__first_name",
                "user__username",
            )
        self.fields["roles"].label_from_instance = self._role_label
        self.fields["members"].label_from_instance = self._membership_label

        managed_initial = []
        if self.instance.pk:
            self.fields["roles"].initial = list(
                self.instance.role_grants.filter(
                    scopes__scope_type=RoleGrantScope.SCOPE_OWN,
                ).values_list("role_id", flat=True)
            )
            self.fields["members"].initial = list(
                self.instance.group_memberships.values_list("membership_id", flat=True)
            )
            managed_initial = self._managed_initial()

        self.managed_formset = self._build_managed_formset(owner, managed_initial)

        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            Fieldset(
                str(_("Group details")),
                "tenant",
                "name",
                "description",
                "is_active",
            ),
            Fieldset(str(_("Roles in this tenant")), "roles"),
            Fieldset(str(_("Members")), "members"),
        )

    def _managed_initial(self):
        rows = []
        grants = self.instance.role_grants.select_related("role").prefetch_related("scopes")
        for grant in grants:
            scopes = [scope for scope in grant.scopes.all() if scope.scope_type != RoleGrantScope.SCOPE_OWN]
            explicit_tenant_ids = [
                scope.tenant_id
                for scope in scopes
                if scope.scope_type == RoleGrantScope.SCOPE_TENANT and scope.tenant_id
            ]
            if explicit_tenant_ids:
                rows.append(
                    {
                        "id": grant.pk,
                        "role": grant.role_id,
                        "managed_scope": GroupManagedRoleGrantForm.SCOPE_EXPLICIT,
                        "assigned_tenants": explicit_tenant_ids,
                    }
                )
            for scope in scopes:
                if scope.scope_type == RoleGrantScope.SCOPE_TENANT_GROUP:
                    rows.append(
                        {
                            "id": grant.pk,
                            "role": grant.role_id,
                            "managed_scope": RoleGrantScope.SCOPE_TENANT_GROUP,
                            "scope_group": scope.tenant_group_id,
                        }
                    )
                elif scope.scope_type == RoleGrantScope.SCOPE_ALL_MANAGED:
                    rows.append(
                        {
                            "id": grant.pk,
                            "role": grant.role_id,
                            "managed_scope": RoleGrantScope.SCOPE_ALL_MANAGED,
                        }
                    )
        return rows

    def _build_managed_formset(self, owner, initial):
        if owner is None or not owner.is_provider:
            return None
        form_kwargs = {
            "owner": owner,
            "requesting_user": self._requesting_user,
        }
        if self.is_bound:
            return GroupManagedRoleGrantFormSet(
                self.data,
                self.files,
                prefix=GROUP_MANAGED_FORMSET_PREFIX,
                form_kwargs=form_kwargs,
            )
        return GroupManagedRoleGrantFormSet(
            initial=initial,
            prefix=GROUP_MANAGED_FORMSET_PREFIX,
            form_kwargs=form_kwargs,
        )

    def is_valid(self):
        form_valid = super().is_valid()
        formset_valid = True
        if self.managed_formset is not None:
            formset_valid = self.managed_formset.is_valid()
        return form_valid and formset_valid

    def clean(self):
        cleaned_data = super().clean()
        owner = cleaned_data.get("tenant")

        if owner is None:
            self.add_error("tenant", _("Choose the tenant that owns this user group."))
            return cleaned_data

        if self.instance.pk and (owner.pk != self.instance.tenant_id or self._submitted_owner_changed):
            self.add_error("tenant", _("The tenant that owns a user group cannot be changed."))

        roles = list(cleaned_data.get("roles") or [])
        if any(role.tenant_id != owner.pk for role in roles):
            self.add_error(
                "roles",
                _("Choose only roles owned by the group's tenant."),
            )

        memberships = list(cleaned_data.get("members") or [])
        existing_member_ids = set(
            self.instance.group_memberships.values_list("membership_id", flat=True) if self.instance.pk else []
        )
        if any(
            membership.tenant_id != owner.pk or (not membership.is_active and membership.pk not in existing_member_ids)
            for membership in memberships
        ):
            self.add_error(
                "members",
                _("New group members must be active members of the group's tenant."),
            )

        existing_own_role_ids = set(
            self.instance.role_grants.filter(
                scopes__scope_type=RoleGrantScope.SCOPE_OWN,
            ).values_list("role_id", flat=True)
            if self.instance.pk
            else []
        )
        selected_role_ids = {role.pk for role in roles}
        selected_member_ids = {membership.pk for membership in memberships}

        # A new role affects every existing member; a new member inherits every
        # selected grant. Reactivating the group restores every retained role to
        # every existing member, so all selected own-scope roles must pass the
        # same escalation guard even though their grant rows already existed.
        role_ids_to_validate = selected_role_ids - existing_own_role_ids
        reactivating = bool(self.instance.pk and self._initial_is_active is False and cleaned_data.get("is_active"))
        if reactivating or selected_member_ids - existing_member_ids:
            role_ids_to_validate = selected_role_ids

        role_by_id = {role.pk: role for role in roles}
        errors = []
        for role_id in role_ids_to_validate:
            try:
                validate_role_grant(
                    self._requesting_user,
                    role_by_id[role_id],
                    owner,
                    scope_type=RoleGrantScope.SCOPE_OWN,
                )
            except forms.ValidationError as exc:
                errors.extend(exc.messages)
        if errors:
            self.add_error(None, forms.ValidationError(list(dict.fromkeys(errors))))

        return cleaned_data

    def save(self, commit=True):
        with transaction.atomic():
            group = super().save(commit=commit)
            if commit:
                self._sync_group_aggregate(group)
            else:
                django_save_m2m = self.save_m2m

                def save_m2m():
                    django_save_m2m()
                    self._sync_group_aggregate(self.instance)

                self.save_m2m = save_m2m
        return group

    def _sync_group_aggregate(self, group):
        with transaction.atomic():
            self._sync_role_grants(group)
            self._sync_manual_memberships(group)

    def _sync_role_grants(self, group):
        own_roles = list(self.cleaned_data.get("roles") or [])
        roles_by_id = {role.pk: role for role in own_roles}
        desired_scopes = {role.pk: {(RoleGrantScope.SCOPE_OWN, None, None)} for role in own_roles}

        if self.managed_formset is not None:
            for form in self.managed_formset.forms:
                if not hasattr(form, "cleaned_data"):
                    continue
                cleaned = form.cleaned_data
                if cleaned.get("DELETE") or not cleaned.get("role"):
                    continue
                role = cleaned["role"]
                roles_by_id[role.pk] = role
                role_scopes = desired_scopes.setdefault(role.pk, set())
                scope = cleaned.get("managed_scope") or GroupManagedRoleGrantForm.SCOPE_EXPLICIT
                if scope == RoleGrantScope.SCOPE_ALL_MANAGED:
                    role_scopes.add((RoleGrantScope.SCOPE_ALL_MANAGED, None, None))
                elif scope == RoleGrantScope.SCOPE_TENANT_GROUP:
                    role_scopes.add(
                        (
                            RoleGrantScope.SCOPE_TENANT_GROUP,
                            None,
                            cleaned["scope_group"].pk,
                        )
                    )
                else:
                    role_scopes.update(
                        (RoleGrantScope.SCOPE_TENANT, tenant.pk, None)
                        for tenant in cleaned.get("assigned_tenants") or []
                    )

        existing = {
            grant.role_id: grant for grant in group.role_grants.select_related("role").prefetch_related("scopes")
        }
        for role_id, grant in existing.items():
            if role_id not in desired_scopes:
                grant.delete()

        for role_id, scope_keys in desired_scopes.items():
            grant = existing.get(role_id)
            if grant is None:
                grant = RoleGrant(
                    user_group=group,
                    role=roles_by_id[role_id],
                    granted_by=self._requesting_user,
                )
                grant.save()

            existing_scopes = {
                (scope.scope_type, scope.tenant_id, scope.tenant_group_id): scope for scope in grant.scopes.all()
            }
            for key, scope in existing_scopes.items():
                if key not in scope_keys:
                    scope.delete()
            for scope_type, tenant_id, tenant_group_id in scope_keys:
                if (scope_type, tenant_id, tenant_group_id) in existing_scopes:
                    continue
                RoleGrantScope.objects.create(
                    role_grant=grant,
                    scope_type=scope_type,
                    tenant_id=tenant_id,
                    tenant_group_id=tenant_group_id,
                )

    def _sync_manual_memberships(self, group):
        memberships = list(self.cleaned_data.get("members") or [])
        selected_ids = {membership.pk for membership in memberships}
        existing = {row.membership_id: row for row in group.group_memberships.select_related("membership")}
        for membership_id, row in existing.items():
            if membership_id not in selected_ids and row.source == GroupMembership.SOURCE_MANUAL:
                row.delete()
        for membership in memberships:
            if membership.pk in existing:
                continue
            GroupMembership.objects.create(
                user_group=group,
                membership=membership,
                source=GroupMembership.SOURCE_MANUAL,
                added_by=self._requesting_user,
            )


class UserGroupFilterForm(FilterForm):
    filterset_class = UserGroupFilterSet


class UserGroupAssignUsersForm(TenantScopedFormMixin, forms.Form):
    """Pick active members of the group's owning tenant."""

    memberships = forms.ModelMultipleChoiceField(
        queryset=Membership.objects.none(),
        required=True,
        label=_("Tenant memberships"),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["memberships"].queryset = (
            Membership.objects.filter(
                tenant_id=group.tenant_id,
                is_active=True,
            )
            .select_related("user", "tenant")
            .order_by(
                "user__last_name",
                "user__first_name",
                "user__username",
            )
        )
        self.fields["memberships"].label_from_instance = UserGroupForm._membership_label
