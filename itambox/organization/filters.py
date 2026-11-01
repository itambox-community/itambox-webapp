import django_filters
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Layout, Submit  # Import Helper, Layout, Submit
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from assets.models import AssetType, Manufacturer
from core.filters import BaseFilterSet
from extras.models import Tag  # Import Tag
from organization.models import (
    AssetHolder,
    Contact,
    ContactAssignment,
    ContactRole,
    CostCenter,
    Location,
    Membership,
    Region,
    Role,
    RoleGrant,
    RoleGrantScope,
    Site,
    SiteGroup,
    Tenant,
    TenantGroup,
)


# --- Base Search Filter ---
class BaseOrgFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(
        method="search", label=_("Search"), widget=forms.TextInput(attrs={"placeholder": _("Search")})
    )
    # Common tag filter
    tag = django_filters.ModelMultipleChoiceFilter(
        field_name="tags__slug",
        queryset=Tag.objects.all(),
        to_field_name="slug",
        label=_("Tags"),
        conjoined=True,  # Use AND logic for tags
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize FormHelper
        self.form.helper = FormHelper()
        self.form.helper.form_method = "get"  # Important for filters
        self.form.helper.form_tag = False  # Template handles <form> tag
        # Define a simple layout, adding the submit button
        # We'll add fields dynamically based on the FilterSet definition
        self.form.helper.layout = Layout(
            *self.filters.keys(),  # Render all defined filter fields
            HTML('<div class="mt-3">'),  # Add margin like the template had
            Submit("submit", _("Apply filter"), css_class="btn btn-primary"),
            # Add Clear button as HTML link within the layout
            HTML(
                format_html(
                    '<a href="{{{{ request.path }}}}" class="btn btn-secondary ms-2">{}</a>',
                    _("Clear filters"),
                )
            ),
            HTML("</div>"),
        )

    def search(self, queryset, name, value):
        # Default search: name and description (override in subclasses)
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


# --- Region Filter ---
class RegionFilterSet(BaseOrgFilterSet):
    parent = django_filters.ModelChoiceFilter(
        queryset=Region.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = Region
        fields = ["name", "parent"]


# --- SiteGroup Filter ---
class SiteGroupFilterSet(BaseOrgFilterSet):
    parent = django_filters.ModelChoiceFilter(
        queryset=SiteGroup.objects.all(), label=_("Parent Group"), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = SiteGroup
        fields = ["name", "parent"]


# --- Site Filter ---
class SiteFilterSet(BaseOrgFilterSet):
    status = django_filters.MultipleChoiceFilter(
        choices=Site.STATUS_CHOICES, widget=forms.SelectMultiple(attrs={"class": "form-select"})
    )
    region = django_filters.ModelChoiceFilter(
        queryset=Region.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    group = django_filters.ModelChoiceFilter(
        queryset=SiteGroup.objects.all(), label=_("Site Group"), widget=forms.Select(attrs={"class": "form-select"})
    )
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = Site
        fields = ["name", "status", "region", "group", "tenant"]

    # Override default search for Site
    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(description__icontains=value)
            | Q(facility__icontains=value)  # Include facility
            | Q(physical_address__icontains=value)
            | Q(comments__icontains=value)
        ).distinct()


# --- Location Filter ---
class LocationFilterSet(BaseOrgFilterSet):
    status = django_filters.MultipleChoiceFilter(
        choices=Location.STATUS_CHOICES, widget=forms.SelectMultiple(attrs={"class": "form-select"})
    )
    site = django_filters.ModelChoiceFilter(
        queryset=Site.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    parent = django_filters.ModelChoiceFilter(
        queryset=Location.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = Location
        fields = ["name", "status", "site", "parent", "tenant"]

    # Override default search for Location
    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value) | Q(facility__icontains=value)
        ).distinct()


# --- TenantGroup Filter ---
class TenantGroupFilterSet(BaseOrgFilterSet):
    # Callable queryset → re-evaluated per request through the tenant-scoping
    # TenantGroup manager, so the filter dropdown only lists accessible groups.
    parent = django_filters.ModelChoiceFilter(
        queryset=lambda request: TenantGroup.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = TenantGroup
        fields = ["name", "parent"]


# --- Tenant Filter ---
def _tenant_filter_managed_by_queryset(request):
    """Per-request queryset for the tenant list's ``managed_by`` filter dropdown.

    A static unscoped queryset would render EVERY managing organization's name into the
    filter form for any authenticated user who can reach the tenant list — a (minor)
    cross-MSP enumeration leak. Scope it: superuser sees all ``is_provider`` tenants;
    otherwise only managers relevant to the user (the manager of a tenant they can access,
    or an ``is_provider`` tenant where they hold ``organization.change_tenant``). Ordinary
    tenant users see none.
    """
    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        return Tenant._base_manager.none()
    managers = Tenant._base_manager.filter(is_provider=True, deleted_at__isnull=True)
    if user.is_superuser:
        return managers
    # inline import: app-registry: organization.access imports organization.models; a module-top import here
    # risks an import cycle during app loading.
    from organization.access import accessible_tenant_ids

    manager_ids = set(
        Tenant._base_manager.filter(
            pk__in=accessible_tenant_ids(user),
            managed_by__isnull=False,
        ).values_list("managed_by_id", flat=True)
    )
    manager_ids |= {t.pk for t in managers if user.has_perm("organization.change_tenant", obj=t)}
    return managers.filter(pk__in=manager_ids)


class TenantFilterSet(BaseOrgFilterSet):
    group = django_filters.ModelChoiceFilter(
        queryset=lambda request: TenantGroup.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )
    managed_by = django_filters.ModelChoiceFilter(
        queryset=_tenant_filter_managed_by_queryset,
        label=_("Managed by"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    is_provider = django_filters.BooleanFilter(
        label=_("Manages other tenants"),
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))],
            attrs={"class": "form-select"},
        ),
    )

    class Meta:
        model = Tenant
        fields = ["name", "group", "managed_by", "is_provider"]

    # Override default search for Tenant
    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value) | Q(comments__icontains=value)
        ).distinct()


# --- AssetHolder Filter ---
class AssetHolderFilterSet(BaseOrgFilterSet):
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = AssetHolder
        fields = ["first_name", "last_name", "upn", "email", "tenant"]

    # Override default search for AssetHolder
    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(first_name__icontains=value)
            | Q(last_name__icontains=value)
            | Q(upn__icontains=value)
            | Q(email__icontains=value)
            | Q(description__icontains=value)
            | Q(comments__icontains=value)
        ).distinct()


class AssetTypeFilterSet(BaseOrgFilterSet):
    manufacturer = django_filters.ModelChoiceFilter(
        queryset=Manufacturer.objects.all(), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = AssetType
        fields = ["manufacturer", "model", "part_number"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(model__icontains=value)
            | Q(part_number__icontains=value)
            | Q(description__icontains=value)
            | Q(manufacturer__name__icontains=value)
        ).distinct()


# --- Contact Filter ---
class ContactFilterSet(BaseOrgFilterSet):
    class Meta:
        model = Contact
        fields = ["name", "title", "phone", "email"]

    # Override default search for Contact
    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(title__icontains=value)
            | Q(phone__icontains=value)
            | Q(email__icontains=value)
            | Q(description__icontains=value)
            | Q(comments__icontains=value)
        ).distinct()


# --- ContactRole Filter ---
class ContactRoleFilterSet(BaseOrgFilterSet):
    tag = None  # ContactRole has no tags field — disable inherited filter

    class Meta:
        model = ContactRole
        fields = ["name"]

    # Override default search for ContactRole
    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(slug__icontains=value) | Q(description__icontains=value)
        ).distinct()


def _user_queryset(request):

    return get_user_model().objects.all()


class RoleFilterSet(BaseOrgFilterSet):
    tag = None  # Role has no tags field
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant._base_manager.filter(deleted_at__isnull=True),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    shared_with_managed = django_filters.BooleanFilter(
        label=_("Shared with managed tenants"),
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))],
            attrs={"class": "form-select"},
        ),
    )

    class Meta:
        model = Role
        fields = ["name", "tenant", "shared_with_managed"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value)).distinct()


class MembershipFilterSet(BaseOrgFilterSet):
    tag = None
    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant._base_manager.filter(deleted_at__isnull=True),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    # Filter through the grant relation; distinct prevents a membership with
    # several matching scopes from appearing more than once.
    role = django_filters.ModelMultipleChoiceFilter(
        field_name="role_grants__role",
        method="filter_role",
        # select_related('tenant'): the widget renders one <option> per role via
        # Role.__str__ ("name (tenant)"), which would otherwise fetch role.tenant
        # once per choice — an N+1 that grows with the tenant's role count.
        queryset=Role._base_manager.filter(deleted_at__isnull=True).select_related("tenant"),
        label=_("Roles"),
        distinct=True,
        widget=forms.SelectMultiple(attrs={"class": "form-select"}),
    )
    reach = django_filters.ChoiceFilter(
        method="filter_reach",
        choices=RoleGrant.REACH_CHOICES,
        label=_("Reach"),
        distinct=True,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    user = django_filters.ModelChoiceFilter(
        queryset=_user_queryset,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    is_active = django_filters.BooleanFilter(
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))],
            attrs={"class": "form-select"},
        ),
    )

    class Meta:
        model = Membership
        fields = ["tenant", "role", "reach", "user", "is_active"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        live_role_grant = (
            Q(role_grants__role__deleted_at__isnull=True)
            & Q(role_grants__scopes__isnull=False)
            & (Q(role_grants__valid_until__isnull=True) | Q(role_grants__valid_until__gt=timezone.now()))
        )
        return queryset.filter(
            Q(user__username__icontains=value)
            | Q(user__email__icontains=value)
            | (Q(role_grants__role__name__icontains=value) & live_role_grant)
            | Q(tenant__name__icontains=value)
        ).distinct()

    def filter_role(self, queryset, name, value):
        if not value:
            return queryset
        return (
            queryset.filter(
                role_grants__role__in=value,
                role_grants__role__deleted_at__isnull=True,
                role_grants__scopes__isnull=False,
            )
            .filter(Q(role_grants__valid_until__isnull=True) | Q(role_grants__valid_until__gt=timezone.now()))
            .distinct()
        )

    def filter_reach(self, queryset, name, value):
        if value == RoleGrant.REACH_OWN:
            return (
                queryset.filter(
                    role_grants__scopes__scope_type=RoleGrantScope.SCOPE_OWN,
                    role_grants__role__deleted_at__isnull=True,
                )
                .filter(Q(role_grants__valid_until__isnull=True) | Q(role_grants__valid_until__gt=timezone.now()))
                .distinct()
            )
        if value == RoleGrant.REACH_MANAGED:
            return (
                queryset.filter(
                    role_grants__scopes__scope_type__in=(
                        RoleGrantScope.SCOPE_TENANT,
                        RoleGrantScope.SCOPE_TENANT_GROUP,
                        RoleGrantScope.SCOPE_ALL_MANAGED,
                    ),
                    role_grants__role__deleted_at__isnull=True,
                )
                .filter(Q(role_grants__valid_until__isnull=True) | Q(role_grants__valid_until__gt=timezone.now()))
                .distinct()
            )
        return queryset


class CostCenterFilterSet(BaseOrgFilterSet):
    tag = None  # CostCenter has no tags field

    tenant = django_filters.ModelChoiceFilter(
        queryset=Tenant.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    parent = django_filters.ModelChoiceFilter(
        queryset=CostCenter.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    is_active = django_filters.BooleanFilter(
        widget=forms.Select(
            choices=[("", _("Any")), ("true", _("Yes")), ("false", _("No"))],
            attrs={"class": "form-select"},
        ),
    )

    class Meta:
        model = CostCenter
        fields = ["name", "code", "tenant", "parent", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filters["parent"].field.label_from_instance = lambda cost_center: (
            f"{cost_center.code}: {cost_center.name}" if cost_center.code else cost_center.name
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(code__icontains=value) | Q(description__icontains=value)
        ).distinct()


class ContactAssignmentFilterSet(BaseOrgFilterSet):
    tag = None  # ContactAssignment has no tags field
    contact = django_filters.ModelChoiceFilter(
        queryset=Contact.objects.all(), label=_("Contact"), widget=forms.Select(attrs={"class": "form-select"})
    )
    role = django_filters.ModelChoiceFilter(
        queryset=ContactRole.objects.all(), label=_("Role"), widget=forms.Select(attrs={"class": "form-select"})
    )
    content_type = django_filters.ModelChoiceFilter(
        queryset=ContentType.objects.all(), label=_("Object Type"), widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = ContactAssignment
        fields = ["contact", "role", "content_type", "object_id"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(contact__name__icontains=value) | Q(contact__email__icontains=value) | Q(role__name__icontains=value)
        ).distinct()
