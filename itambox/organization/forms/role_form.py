"""Role form (tenant-owned permission set) with permission-matrix UI.

Post-collapse there is exactly one kind of role: a permission set owned by a
tenant. The owner is never picked on the form — it comes from context (the
``?tenant=`` deep-link or the active tenant) on create and is immutable on edit.
Roles owned by a managing (``is_provider``) tenant can additionally be shared
with its managed tenants via the ``shared_with_managed`` checkbox.
"""

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.forms import FilterForm
from core.forms.base import TenantScopedFormMixin
from core.managers import get_current_tenant
from core.mfa import role_is_privileged
from organization.services.role_grant_validation import validate_permission_grant, validate_role_grant

from ..models import Role, RoleGrantScope
from .helpers import add_standard_buttons
from .role_matrix import MATRIX_MODELS


# Custom (non-CRUD) permissions exposed as named checkboxes alongside the matrix.
# A standard CRUD permission is the action plus its own content type's model
# name; custom codenames may also start with ``view_``/``add_`` and still need a
# dedicated checkbox (for example ``core.view_plugin_diagnostics``).
def get_custom_permissions():
    """All non-CRUD permissions as ``(field_key, label, full_codename)`` tuples.

    ``field_key`` combines app label and codename so the generated ``perm_*``
    form fields stay unique even when two apps declare the same codename.
    """
    permissions = Permission.objects.select_related("content_type").order_by("content_type__app_label", "codename")
    crud_actions = ("view", "add", "change", "delete")
    return [
        (
            f"{permission.content_type.app_label}_{permission.codename}",
            permission.name,
            f"{permission.content_type.app_label}.{permission.codename}",
        )
        for permission in permissions
        if permission.codename not in {f"{action}_{permission.content_type.model}" for action in crud_actions}
    ]


class RoleForm(TenantScopedFormMixin, forms.ModelForm):
    """ModelForm for ``organization.Role`` — owner tenant comes from context, never a picker."""

    class Meta:
        model = Role
        fields = ["name", "description", "shared_with_managed"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Inventory Manager"}),
            "description": forms.Textarea(
                attrs={"class": "form-control", "rows": 3, "placeholder": _("Describe the role…")}
            ),
            # role="switch" pairs with the .form-switch wrapper role_form.html renders it
            # in — a highlighted switch, not a plain checkbox (RBAC_STAGE3_SPEC.md §4).
            "shared_with_managed": forms.CheckboxInput(attrs={"class": "form-check-input", "role": "switch"}),
        }

    def __init__(self, *args, **kwargs):
        self.current_user = kwargs.pop("user", None)
        tenant_ctx = kwargs.pop("tenant", None)
        super().__init__(*args, **kwargs)
        self._original_permissions = frozenset(self.instance.permissions or [])
        self._was_shared_with_managed = bool(self.instance.pk and self.instance.shared_with_managed)
        self._was_privileged = bool(self.instance.pk and role_is_privileged(self.instance))

        # Owner resolution: locked to the instance's tenant on edit; on create it is the
        # context tenant (?tenant= deep-link from the view) falling back to the active
        # tenant. There is deliberately no owner picker — a role always lives in the
        # tenant you are working in.
        if self.instance.pk:
            self.owner_tenant = self.instance.tenant
        else:
            self.owner_tenant = tenant_ctx or get_current_tenant()

        # Sharing is only meaningful when the owner manages other tenants — the
        # switch never renders for a plain tenant's role, even for a superuser.
        if not (self.owner_tenant is not None and self.owner_tenant.is_provider):
            self.fields.pop("shared_with_managed", None)
        else:
            # Single source of truth for the switch's copy — role_form.html renders
            # this help_text verbatim rather than hand-copying it into the template.
            self.fields["shared_with_managed"].help_text = _(
                "Managed tenants can assign this role to their own members; only you can edit it."
            )

        # Build the CRUD matrix and pre-check from the instance's permission set.
        existing_perms = set(self.instance.permissions or [])
        for key, info in MATRIX_MODELS.items():
            for action in ("read", "create", "edit", "delete"):
                fname = f"perm_{key}_{action}"
                self.fields[fname] = forms.BooleanField(
                    required=False,
                    widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
                )
            app, model = info["app"], info["model_name"]
            self.fields[f"perm_{key}_read"].initial = f"{app}.view_{model}" in existing_perms
            self.fields[f"perm_{key}_create"].initial = f"{app}.add_{model}" in existing_perms
            self.fields[f"perm_{key}_edit"].initial = f"{app}.change_{model}" in existing_perms
            self.fields[f"perm_{key}_delete"].initial = f"{app}.delete_{model}" in existing_perms

        # Custom (non-CRUD) permissions.
        self._custom_permissions = get_custom_permissions()
        for field_key, label, full in self._custom_permissions:
            fname = f"perm_{field_key}"
            self.fields[fname] = forms.BooleanField(
                required=False,
                label=label,
                widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            )
            self.fields[fname].initial = full in existing_perms

        # Crispy layout — the matrix sections render through {{ form.matrix_grouped_items }},
        # so we only need to lay out the meta fields here.
        self.helper = FormHelper(self)
        self.helper.form_method = "post"
        self.helper.form_tag = True
        layout_fields = ["name", "description"]
        if "shared_with_managed" in self.fields:
            layout_fields.append("shared_with_managed")
        self.helper.layout = Layout(*layout_fields)
        add_standard_buttons(self.helper, self.instance, "organization:role_list")

    # ------------------------------------------------------------------ cleaning
    def clean(self):
        cleaned_data = super().clean()

        if self.owner_tenant is None:
            raise forms.ValidationError(_("Open this form from a tenant, or select an active tenant first."))

        # Build permission set from the matrix + custom checkboxes.
        assigned_perms = set()
        for key, info in MATRIX_MODELS.items():
            app, model = info["app"], info["model_name"]
            if cleaned_data.get(f"perm_{key}_read"):
                assigned_perms.add(f"{app}.view_{model}")
            if cleaned_data.get(f"perm_{key}_create"):
                assigned_perms.add(f"{app}.add_{model}")
            if cleaned_data.get(f"perm_{key}_edit"):
                assigned_perms.add(f"{app}.change_{model}")
            if cleaned_data.get(f"perm_{key}_delete"):
                assigned_perms.add(f"{app}.delete_{model}")

        for field_key, _label, full in self._custom_permissions:
            if cleaned_data.get(f"perm_{field_key}"):
                assigned_perms.add(full)

        # If any permission is granted, also auto-grant the dashboard perms needed for a
        # functioning landing page (view + create/customize own dashboards). Deliberately NOT
        # delete_dashboard: it isn't needed for a landing page, and auto-adding it would make
        # every non-empty role carry a delete_* permission — breaking role presets like
        # "Technician" whose whole point is to exclude delete_*.
        if assigned_perms:
            assigned_perms |= {
                "extras.view_dashboard",
                "extras.change_dashboard",
                "extras.add_dashboard",
            }

        # Filter against the live permission table to drop codenames that don't exist
        # (matrix rows for models lacking that action, uninstalled plugins, etc.).
        valid = set(
            f"{p.content_type.app_label}.{p.codename}" for p in Permission.objects.select_related("content_type").all()
        )
        assigned_perms = {p for p in assigned_perms if p in valid}

        self.instance.tenant = self.owner_tenant

        # Privilege-escalation guard: a non-superuser may not assign permissions they do
        # not themselves hold in the role's owning tenant.
        validate_permission_grant(self.current_user, assigned_perms, self.owner_tenant)

        self.instance.permissions = sorted(assigned_perms)
        resulting_shared = bool(
            cleaned_data.get(
                "shared_with_managed",
                self.instance.shared_with_managed,
            )
        )
        self.instance.shared_with_managed = resulting_shared

        permissions_changed = assigned_perms != self._original_permissions
        sharing_reenabled = resulting_shared and not self._was_shared_with_managed
        if self.instance.pk and (permissions_changed or sharing_reenabled):
            self._validate_retained_grant_projections()

        # A harmless role must not become a permanent direct-admin back door by
        # being edited after it was granted. Existing direct grants must already
        # carry the same reason + expiration required at grant creation before a
        # role can be elevated; permanent elevated access belongs on groups.
        self.instance.name = cleaned_data.get("name") or self.instance.name
        if self.instance.pk and not self._was_privileged and role_is_privileged(self.instance):
            now = timezone.now()
            unsafe_direct_grants = any(
                not grant.reason.strip() or grant.valid_until is None or grant.valid_until <= now
                for grant in self.instance.role_grants.filter(membership__isnull=False)
            )
            if unsafe_direct_grants:
                raise forms.ValidationError(
                    _(
                        "This role cannot be elevated while it is assigned directly without a reason "
                        "and a future expiration. Move permanent access to a group, or update or revoke "
                        "those direct assignments first."
                    )
                )
        return cleaned_data

    def _validate_retained_grant_projections(self):
        """Re-check every live projection that an edited role will continue to power.

        A provider-home permission check is insufficient once a role is already
        attached to principals or scopes elsewhere.  Editing that shared role is
        itself a grant into each retained target, so it must pass the same
        ``validate_role_grant`` checks as creating the aggregate in the first
        place.  Inert history (expired grants, inactive principals, deleted
        targets, or severed management edges) is deliberately ignored.
        """
        errors = []
        owner = self.owner_tenant

        grants = self.instance.role_grants.select_related(
            "membership__tenant",
            "user_group__tenant",
        ).prefetch_related(
            "scopes__tenant",
            "scopes__tenant_group",
        )

        for grant in grants:
            if not grant.is_active:
                continue

            if grant.membership_id:
                if not grant.membership.is_active:
                    continue
                principal_tenant = grant.membership.tenant
            elif grant.user_group_id:
                group = grant.user_group
                if not group.is_active or group.deleted_at is not None:
                    continue
                principal_tenant = group.tenant
            else:
                continue

            if principal_tenant.deleted_at is not None:
                continue

            scopes = list(grant.scopes.all())
            own_scope_is_effective = any(scope.scope_type == RoleGrantScope.SCOPE_OWN for scope in scopes) and (
                self.instance.tenant_id == principal_tenant.pk
                or (
                    grant.membership_id
                    and self.instance.shared_with_managed
                    and owner.is_provider
                    and principal_tenant.managed_by_id == owner.pk
                )
            )
            if own_scope_is_effective:
                self._collect_projection_errors(
                    errors,
                    principal_tenant,
                    RoleGrantScope.SCOPE_OWN,
                )

            managed_projection_is_effective = (
                owner.is_provider and principal_tenant.pk == owner.pk and self.instance.tenant_id == owner.pk
            )
            if not managed_projection_is_effective:
                continue

            explicit_tenant_ids = {
                scope.tenant_id
                for scope in scopes
                if (
                    scope.scope_type == RoleGrantScope.SCOPE_TENANT
                    and scope.tenant_id
                    and scope.tenant.deleted_at is None
                    and scope.tenant.managed_by_id == owner.pk
                )
            }
            if explicit_tenant_ids:
                self._collect_projection_errors(
                    errors,
                    principal_tenant,
                    RoleGrantScope.SCOPE_TENANT,
                    requested_tenant_ids=explicit_tenant_ids,
                )

            has_live_group_scope = any(
                scope.scope_type == RoleGrantScope.SCOPE_TENANT_GROUP
                and scope.tenant_group_id
                and scope.tenant_group.deleted_at is None
                for scope in scopes
            )
            if has_live_group_scope:
                self._collect_projection_errors(
                    errors,
                    principal_tenant,
                    RoleGrantScope.SCOPE_TENANT_GROUP,
                )

            if any(scope.scope_type == RoleGrantScope.SCOPE_ALL_MANAGED for scope in scopes):
                self._collect_projection_errors(
                    errors,
                    principal_tenant,
                    RoleGrantScope.SCOPE_ALL_MANAGED,
                )

        if errors:
            # Multiple retained aggregates can expose different gaps. Validate
            # them all, then de-duplicate the canonical guard messages for a
            # useful single form error rather than stopping at the first scope.
            raise forms.ValidationError(list(dict.fromkeys(errors)))

    def _collect_projection_errors(
        self,
        errors,
        principal_tenant,
        scope_type,
        *,
        requested_tenant_ids=None,
    ):
        try:
            validate_role_grant(
                self.current_user,
                self.instance,
                principal_tenant,
                scope_type=scope_type,
                requested_tenant_ids=requested_tenant_ids,
            )
        except forms.ValidationError as exc:
            errors.extend(exc.messages)

    # ---------------------------------------------------------------- template helpers
    @property
    def preset_definitions(self):
        """Built-in preset choices offered by the client-side preset picker.

        Returns a list of ``(value, label)`` pairs. ``value`` keys into
        ``preset_field_map``; ``blank`` is always first (clears the grid). Kept in
        sync with the seed's role catalog (``_seed/access.py``): Administrator = all,
        Technician = all non-delete op perms, Read-Only = all view_*.
        """
        return [
            ("blank", _("Blank (start from scratch)")),
            ("administrator", _("Administrator (full access)")),
            ("technician", _("Technician (all except delete)")),
            ("readonly", _("Read-only (no changes)")),
        ]

    @property
    def preset_field_map(self):
        """Map each preset to the matrix checkbox field names it pre-checks.

        Computed over *this form's* matrix models only (so presets stay scoped to
        the grid actually rendered — dropped plugin rows never appear). The values
        are matrix field names (``perm_<key>_<action>``), never permission
        codenames, so the client only toggles checkboxes and the server-side
        escalation guard in ``clean()`` still validates the final grant. Selecting
        a preset is a convenience only and never bypasses that guard.
        """
        administrator, technician, readonly = [], [], []
        for key in MATRIX_MODELS:
            for action in ("read", "create", "edit", "delete"):
                fname = f"perm_{key}_{action}"
                administrator.append(fname)
                if action != "delete":
                    technician.append(fname)
                if action == "read":
                    readonly.append(fname)
        return {
            "blank": [],
            "administrator": administrator,
            "technician": technician,
            "readonly": readonly,
        }

    @property
    def matrix_items(self):
        return [
            {
                "key": key,
                "label": info["label"],
                "read_field": self[f"perm_{key}_read"],
                "create_field": self[f"perm_{key}_create"],
                "edit_field": self[f"perm_{key}_edit"],
                "delete_field": self[f"perm_{key}_delete"],
            }
            for key, info in MATRIX_MODELS.items()
        ]

    @property
    def matrix_grouped_items(self):
        groups = {}
        for key, info in MATRIX_MODELS.items():
            groups.setdefault(info.get("group", "Other"), []).append(
                {
                    "key": key,
                    "label": info["label"],
                    "read_field": self[f"perm_{key}_read"],
                    "create_field": self[f"perm_{key}_create"],
                    "edit_field": self[f"perm_{key}_edit"],
                    "delete_field": self[f"perm_{key}_delete"],
                }
            )
        return groups

    @property
    def custom_permission_fields(self):
        return [(label, self[f"perm_{field_key}"]) for field_key, label, _ in self._custom_permissions]


class RoleFilterForm(FilterForm):
    # Class-body position, not a function-body import: breaks a forms <-> filters
    # cycle by running after this module's own definitions.
    from ..filters import RoleFilterSet

    filterset_class = RoleFilterSet


class RoleAssignUsersForm(TenantScopedFormMixin, forms.Form):
    """Bulk-add users to a Role (used by the "Assign Users" action).

    The view creates memberships (get_or_create) at the role's owning tenant plus
    direct ``RoleGrant`` rows with an own-tenant scope for the selected users.
    """

    users = forms.ModelMultipleChoiceField(
        queryset=None,
        required=True,
        label=_("Users"),
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        label=_("Reason"),
        help_text=_("Required when assigning an elevated role directly to a user."),
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
    )
    valid_until = forms.DateTimeField(
        required=False,
        label=_("Valid until"),
        help_text=_("Required when assigning an elevated role directly to a user."),
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M"],
    )

    def __init__(self, *args, **kwargs):
        self.role = kwargs.pop("role", None)
        super().__init__(*args, **kwargs)
        self.fields["users"].queryset = get_user_model().objects.order_by("username")

    def clean(self):
        cleaned_data = super().clean()
        if self.role is None or not role_is_privileged(self.role):
            return cleaned_data
        reason = (cleaned_data.get("reason") or "").strip()
        valid_until = cleaned_data.get("valid_until")
        if not reason:
            self.add_error("reason", _("Directly assigned elevated roles require a reason."))
        if valid_until is None:
            self.add_error("valid_until", _("Directly assigned elevated roles require an expiration."))
        elif valid_until <= timezone.now():
            self.add_error("valid_until", _("The expiration must be in the future."))
        cleaned_data["reason"] = reason
        return cleaned_data
