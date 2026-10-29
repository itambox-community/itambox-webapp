# itambox/organization/tables.py
from types import SimpleNamespace

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django_tables2.utils import A

from assets.models import AssetAssignment
from core.html_styles import status_color_class
from core.tables import ActionsColumn, BaseTable, CountLinkColumn, ToggleColumn
from core.tables.constants import TABLE_EMPTY_VALUE
from extras.tables import TagColumn
from itambox.utils import get_status_color

from .models import (
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
    Site,
    SiteGroup,
    Tenant,
    TenantGroup,
    TenantResourceGrant,
    TenantResourceGrantExpiryRevocation,
    TenantResourceGrantExpiryRun,
)
from .templatetags.rbac_badges import membership_kind_badge, reach_badge, shared_role_badge


class RegionTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:region_detail", args=[A("pk")], verbose_name=_("Name"))
    site_count = CountLinkColumn("organization:site_list", "region", verbose_name=_("Sites"), orderable=False)
    tags = TagColumn(url_name="organization:region_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Region
        fields = ("pk", "name", "slug", "description", "site_count", "tags", "actions")
        default_columns = ("pk", "name", "site_count", "description", "tags", "actions")


class SiteGroupTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:sitegroup_detail", args=[A("pk")], verbose_name=_("Name"))
    site_count = CountLinkColumn("organization:site_list", "group", verbose_name=_("Sites"), orderable=False)
    tags = TagColumn(url_name="organization:sitegroup_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = SiteGroup
        fields = ("pk", "name", "site_count", "description", "tags", "actions")
        default_columns = ("pk", "name", "site_count", "description", "tags", "actions")


class SiteTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:site_detail", args=[A("pk")], verbose_name=_("Name"))
    region = tables.LinkColumn("organization:region_detail", args=[A("region_id")], accessor="region")
    group = tables.LinkColumn("organization:sitegroup_detail", args=[A("group_id")], accessor="group")
    tenant = tables.LinkColumn("organization:tenant_detail", args=[A("tenant_id")], accessor="tenant")
    location_count = CountLinkColumn("organization:location_list", "site", verbose_name=_("Locations"), orderable=False)
    asset_count = CountLinkColumn("assets:asset_list", "site", verbose_name=_("Assets"), orderable=False)
    tags = TagColumn(url_name="organization:site_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Site
        fields = (
            "pk",
            "name",
            "slug",
            "status",
            "region",
            "group",
            "tenant",
            "description",
            "location_count",
            "asset_count",
            "tags",
            "actions",
        )
        default_columns = (
            "pk",
            "name",
            "status",
            "region",
            "group",
            "tenant",
            "location_count",
            "asset_count",
            "tags",
            "actions",
        )

    def render_status(self, value, record):
        if record and record.status:
            color = get_status_color(record.status)
            display = record.get_status_display()
            color_class, style_block = status_color_class(color)
            return format_html(
                '{}<span class="badge badge-status {}"><span class="badge-status-dot"></span>{}</span>',
                style_block,
                color_class,
                display,
            )
        return TABLE_EMPTY_VALUE


class LocationTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:location_detail", args=[A("pk")], verbose_name=_("Name"))
    site = tables.LinkColumn("organization:site_detail", args=[A("site_id")], accessor="site")
    tenant = tables.LinkColumn("organization:tenant_detail", args=[A("tenant_id")], accessor="tenant")
    asset_count = CountLinkColumn("assets:asset_list", "location", verbose_name=_("Assets"), orderable=False)
    tags = TagColumn(url_name="organization:location_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Location
        fields = ("pk", "name", "slug", "status", "site", "tenant", "description", "asset_count", "tags", "actions")
        default_columns = ("pk", "name", "status", "site", "tenant", "asset_count", "tags", "actions")

    def render_status(self, value, record):
        if record and record.status:
            color = get_status_color(record.status)
            display = record.get_status_display()
            color_class, style_block = status_color_class(color)
            return format_html(
                '{}<span class="badge badge-status {}"><span class="badge-status-dot"></span>{}</span>',
                style_block,
                color_class,
                display,
            )
        return TABLE_EMPTY_VALUE


class TenantGroupTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:tenantgroup_detail", args=[A("pk")], verbose_name=_("Name"))
    tenant_count = CountLinkColumn("organization:tenant_list", "group", verbose_name=_("Tenants"), orderable=False)
    tags = TagColumn(url_name="organization:tenantgroup_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = TenantGroup
        fields = ("pk", "name", "slug", "description", "tenant_count", "tags", "actions")
        default_columns = ("pk", "name", "tenant_count", "description", "tags", "actions")


class TenantTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:tenant_detail", args=[A("pk")], verbose_name=_("Name"))
    group = tables.LinkColumn("organization:tenantgroup_detail", args=[A("group_id")], accessor="group")
    managed_by = tables.LinkColumn(
        "organization:tenant_detail", args=[A("managed_by_id")], accessor="managed_by", verbose_name=_("Managed by")
    )
    site_count = CountLinkColumn("organization:site_list", "tenant", verbose_name=_("Sites"), orderable=False)
    location_count = CountLinkColumn(
        "organization:location_list", "tenant", verbose_name=_("Locations"), orderable=False
    )
    tags = TagColumn(url_name="organization:tenant_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Tenant
        fields = (
            "pk",
            "name",
            "slug",
            "group",
            "managed_by",
            "description",
            "site_count",
            "location_count",
            "tags",
            "actions",
        )
        default_columns = ("pk", "name", "group", "managed_by", "site_count", "location_count", "tags", "actions")


# --- AssetHolder Table ---
class AssetHolderTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    upn = tables.LinkColumn("organization:assetholder_detail", args=[A("pk")], verbose_name=_("UPN"))
    first_name = tables.Column()
    last_name = tables.Column()
    tenant = tables.LinkColumn(
        "organization:tenant_detail", args=[A("tenant_id")], accessor="tenant", verbose_name=_("Tenant")
    )
    assignment_count = CountLinkColumn(
        "assets:asset_list", "assigned_to", verbose_name=_("Assignments"), orderable=False, accessor="assignment_count"
    )
    tags = TagColumn(url_name="organization:assetholder_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = AssetHolder
        fields = (
            "pk",
            "upn",
            "first_name",
            "last_name",
            "email",
            "tenant",
            "assignment_count",
            "description",
            "tags",
            "actions",
        )
        default_columns = ("pk", "upn", "first_name", "last_name", "tenant", "assignment_count", "tags", "actions")


# --- AssetAssignment Table ---
class AssetAssignmentTable(BaseTable):
    asset = tables.LinkColumn("assets:asset_detail", args=[A("asset_id")], verbose_name=_("Asset"))

    asset_tag = tables.Column(accessor="asset.asset_tag", verbose_name=_("Asset Tag"))
    asset_role = tables.Column(accessor="asset.asset_role", verbose_name=_("Role"))
    checked_out_at = tables.DateTimeColumn(format="Y-m-d H:i", verbose_name=_("Checked Out"))
    expected_checkin_date = tables.DateColumn(format="Y-m-d", verbose_name=_("Expected Check-in"))
    checkin_btn = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-checkout text-nowrap"},
            "td": {"class": "text-center text-nowrap noprint p-1 col-checkout"},
        },
    )

    class Meta(BaseTable.Meta):
        model = AssetAssignment
        fields = ("asset", "asset_tag", "asset_role", "checked_out_at", "expected_checkin_date", "checkin_btn")
        default_columns = ("asset", "asset_tag", "asset_role", "checked_out_at", "expected_checkin_date", "checkin_btn")

    def render_asset_role(self, value):
        return value.name if value else _("No role")

    def render_checkin_btn(self, record):
        request = getattr(self, "request", None)
        if not request or not self.has_perm(request.user, "assets.change_asset", record.asset):
            return format_html('<span class="text-muted small">{}</span>', _("Not available"))

        url = reverse("assets:asset_checkin", kwargs={"pk": record.asset.pk})
        checkin_label = _("Check in")
        return format_html(
            '<div class="d-inline-block"><button type="button" class="btn btn-sm btn-outline-success text-success" '
            'hx-post="{}" hx-swap="none" title="{}" aria-label="{}">'
            '<i class="mdi mdi-keyboard-return"></i> {}</button></div>',
            url,
            checkin_label,
            checkin_label,
            checkin_label,
        )


# --- Contact Table ---
class ContactTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:contact_detail", args=[A("pk")], verbose_name=_("Name"))
    title = tables.Column()
    phone = tables.Column()
    email = tables.EmailColumn()
    tenant = tables.Column(verbose_name=_("Tenant"), default=_("Global"))
    tags = TagColumn(url_name="organization:contact_list")
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Contact
        fields = ("pk", "name", "title", "phone", "email", "web_url", "tenant", "description", "tags", "actions")
        default_columns = ("pk", "name", "title", "phone", "email", "tenant", "tags", "actions")


# --- ContactRole Table ---
class ContactRoleTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:contactrole_detail", args=[A("pk")], verbose_name=_("Name"))
    slug = tables.Column()
    description = tables.Column()
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = ContactRole
        fields = ("pk", "name", "slug", "description", "actions")
        default_columns = ("pk", "name", "slug", "description", "actions")


# --- ContactAssignment Table ---
class ContactAssignmentTable(BaseTable):
    contact = tables.LinkColumn("organization:contact_detail", args=[A("contact.pk")], accessor="contact")
    role = tables.Column()
    assigned_object_type = tables.Column(accessor="content_type", verbose_name=_("Object Type"))
    assigned_object = tables.Column(linkify=True, verbose_name=_("Assigned Object"))
    priority = tables.Column()
    actions = tables.TemplateColumn(
        template_code="""
        <a href="{% url 'organization:contactassignment_delete' record.pk %}?return_url={{ request.path }}"
           class="btn btn-sm btn-action btn-action-danger px-2"
           title="{{ delete }}"
           aria-label="{{ delete }}">
            <i class="mdi mdi-trash-can-outline m-0"></i>
        </a>
        """,
        extra_context={"delete": _("Delete")},
        verbose_name=_("Actions"),
        orderable=False,
    )

    class Meta(BaseTable.Meta):
        model = ContactAssignment
        fields = ("contact", "role", "assigned_object_type", "assigned_object", "priority", "actions")
        default_columns = ("contact", "role", "assigned_object_type", "assigned_object", "priority", "actions")

    def render_assigned_object_type(self, record):
        return record.content_type.model_class()._meta.verbose_name.title()


class RoleTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:role_detail", args=[A("pk")], verbose_name=_("Name"))
    tenant = tables.Column(verbose_name=_("Owner"), accessor="tenant", orderable=True)
    shared = tables.Column(verbose_name=_("Shared"), accessor="shared_with_managed", orderable=True, empty_values=())
    description = tables.Column()
    member_count = tables.Column(verbose_name=_("Direct members"), orderable=True, empty_values=[])
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = Role
        fields = ("pk", "name", "tenant", "shared", "description", "member_count", "actions")
        default_columns = ("pk", "name", "tenant", "shared", "description", "member_count", "actions")

    def render_tenant(self, value, record):
        tenant = record.tenant
        if tenant is None:
            return TABLE_EMPTY_VALUE
        return format_html('<a href="{}">{}</a>', tenant.get_absolute_url(), tenant)

    def render_shared(self, value, record):
        return shared_role_badge(record) or _("No")

    def render_member_count(self, value, record):
        count = getattr(record, "member_count", 0) or 0
        url = f"{reverse('organization:membership_list')}?role={record.pk}"
        return format_html('<a href="{}">{}</a>', url, count)


class MembershipTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    user = tables.LinkColumn("users:user_detail", args=[A("user.pk")], verbose_name=_("User"))
    # Shown only in a multi-tenant context (group scope / superuser global) — the
    # MembershipListView excludes it under a single active tenant, where every row
    # shares that tenant. Never render a mixed table without this identifier.
    tenant = tables.LinkColumn(
        "organization:tenant_detail",
        args=[A("tenant_id")],
        accessor="tenant",
        verbose_name=_("Tenant"),
    )
    roles = tables.Column(verbose_name=_("Roles"), accessor="role_grants", orderable=False, empty_values=())
    kind = tables.Column(verbose_name=_("Reach"), accessor="pk", orderable=False, empty_values=())
    is_active = tables.BooleanColumn(verbose_name=_("Active"))
    joined_at = tables.DateTimeColumn(format="Y-m-d H:i", verbose_name=_("Joined"))
    actions = ActionsColumn(actions=("edit", "delete"))

    class Meta(BaseTable.Meta):
        model = Membership
        fields = ("pk", "user", "tenant", "roles", "kind", "is_active", "joined_at", "actions")
        default_columns = ("pk", "user", "tenant", "roles", "kind", "is_active", "joined_at", "actions")

    def render_kind(self, record):
        # Purple "Staff" when any grant carries managed reach, blue "Member" otherwise.
        # Prefer the list view's Exists() annotation (``has_managed_reach``) so the
        # badge costs zero queries per row; fall back to the model property (one
        # exists() query) for callers that pass an unannotated queryset. The badge
        # helper only reads ``.is_staff_membership``, which is a read-only property
        # on Membership — an annotation cannot reuse that name, so hand the helper
        # the precomputed answer instead.
        staff = getattr(record, "has_managed_reach", None)
        if staff is None:
            staff = record.is_staff_membership
        return membership_kind_badge(SimpleNamespace(is_staff_membership=bool(staff)))

    def render_roles(self, value, record):
        # Aggregated from the prefetched grants: link per role, "Shared" badge
        # on shared-in definitions, reach badge on managed-reach grants.
        parts = []
        for grant in record.role_grants.all():
            if not grant.is_active or grant.role.deleted_at is not None or not grant.scopes.all():
                continue
            url = reverse("organization:role_detail", kwargs={"pk": grant.role_id})
            link = format_html('<a href="{}">{}</a>', url, grant.role.name)
            shared = shared_role_badge(grant.role)
            if shared:
                link = format_html("{} {}", link, shared)
            if grant.reach == RoleGrant.REACH_MANAGED:
                link = format_html("{} {}", link, reach_badge(grant))
            parts.append(link)
        return mark_safe(", ".join(parts)) if parts else _("(none)")


class CostCenterTable(BaseTable):
    pk = ToggleColumn(accessor="pk")
    name = tables.LinkColumn("organization:costcenter_detail", args=[A("pk")], verbose_name=_("Name"))
    code = tables.Column(verbose_name=_("Code"))
    tenant = tables.LinkColumn(
        "organization:tenant_detail", args=[A("tenant_id")], accessor="tenant", verbose_name=_("Tenant")
    )
    parent = tables.LinkColumn(
        "organization:costcenter_detail", args=[A("parent_id")], accessor="parent", verbose_name=_("Parent")
    )
    child_count = CountLinkColumn(
        "organization:costcenter_list", "parent", verbose_name=_("Sub-units"), orderable=False
    )
    is_active = tables.BooleanColumn(verbose_name=_("Active"))
    actions = ActionsColumn()

    class Meta(BaseTable.Meta):
        model = CostCenter
        fields = ("pk", "code", "name", "tenant", "parent", "description", "child_count", "is_active", "actions")
        default_columns = ("pk", "code", "name", "tenant", "parent", "child_count", "is_active", "actions")

    def render_parent(self, value):
        if not value:
            return TABLE_EMPTY_VALUE
        label = f"{value.code}: {value.name}" if value.code else value.name
        return format_html('<a href="{}">{}</a>', value.get_absolute_url(), label)


class TenantResourceGrantTable(BaseTable):
    """Grants involving the active tenant (given and received)."""

    pk = ToggleColumn(accessor="pk")
    tenant = tables.Column(verbose_name=_("Owner"))
    grantee = tables.Column(verbose_name=_("Grantee"), empty_values=(), orderable=False)
    resource = tables.Column(verbose_name=_("Shared pool"), empty_values=(), orderable=False)
    access_level = tables.Column(verbose_name=_("Access"))
    granted_by = tables.Column(verbose_name=_("Granted by"))
    created_at = tables.DateTimeColumn(verbose_name=_("Granted at"), format="Y-m-d H:i")
    actions = tables.Column(
        verbose_name="",
        orderable=False,
        empty_values=(),
        attrs={
            "th": {"class": "col-actions-wide text-nowrap"},
            "td": {"class": "text-end text-nowrap noprint p-1 col-actions-wide"},
        },
    )

    class Meta(BaseTable.Meta):
        model = TenantResourceGrant
        fields = ("pk", "tenant", "grantee", "resource", "access_level", "granted_by", "created_at", "actions")
        default_columns = ("pk", "tenant", "grantee", "resource", "access_level", "created_at", "actions")

    def render_grantee(self, record):
        if record.grantee_tenant_id:
            return str(record.grantee_tenant)
        return format_html(
            '{} <span class="badge bg-azure-lt">{}</span>',
            record.grantee_tenant_group,
            _("Group (incl. descendants)"),
        )

    def render_resource(self, record):
        resource = record.resource  # GenericFK; may be gone (revoked orphans)
        if resource is None:
            return format_html(
                '<span class="text-muted">{} #{}</span>',
                record.resource_type.model,
                record.resource_id,
            )
        return str(resource)

    def render_actions(self, record):
        request = getattr(self, "request", None)
        if not request or not self.has_perm(
            request.user,
            "organization.delete_tenantresourcegrant",
            record,
        ):
            return format_html('<span class="text-muted small">{}</span>', _("Not available"))
        revoke_url = reverse("organization:tenantresourcegrant_delete", kwargs={"pk": record.pk})
        return format_html(
            '<a href="{}" class="btn btn-sm btn-outline-danger" title="{}">'
            '<i class="mdi mdi-link-variant-off"></i> {}</a>',
            revoke_url,
            _("Revoke this grant"),
            _("Revoke"),
        )


class TenantResourceGrantExpiryRunTable(BaseTable):
    schedule_slot = tables.LinkColumn(
        "organization:tenantresourcegrantexpiryrun_detail",
        args=[A("pk")],
        verbose_name=_("Schedule slot"),
    )
    tenant = tables.Column(verbose_name=_("Tenant"))
    state = tables.Column(verbose_name=_("State"))
    outcome = tables.Column(verbose_name=_("Outcome"))
    attempt_count = tables.Column(verbose_name=_("Attempts"))
    revoked_count = tables.Column(verbose_name=_("Revoked"))
    remaining_due_count = tables.Column(verbose_name=_("Remaining due"))
    invalid_count = tables.Column(verbose_name=_("Invalid"))
    started_at = tables.DateTimeColumn(verbose_name=_("Started"), format="Y-m-d H:i")
    finished_at = tables.DateTimeColumn(verbose_name=_("Finished"), format="Y-m-d H:i")
    next_retry_at = tables.DateTimeColumn(verbose_name=_("Next retry"), format="Y-m-d H:i")
    error_code = tables.Column(verbose_name=_("Error code"))

    class Meta(BaseTable.Meta):
        model = TenantResourceGrantExpiryRun
        fields = (
            "pk",
            "tenant",
            "schedule_slot",
            "cutoff",
            "state",
            "outcome",
            "attempt_count",
            "revoked_count",
            "remaining_due_count",
            "invalid_count",
            "started_at",
            "finished_at",
            "next_retry_at",
            "error_code",
        )
        default_columns = (
            "pk",
            "tenant",
            "schedule_slot",
            "state",
            "outcome",
            "revoked_count",
            "remaining_due_count",
            "invalid_count",
            "finished_at",
        )


class TenantResourceGrantExpiryRevocationTable(BaseTable):
    grant_id = tables.Column(verbose_name=_("Grant"), accessor="grant_id")
    triggering_valid_until = tables.DateTimeColumn(verbose_name=_("Deadline"), format="Y-m-d H:i")
    revoked_at = tables.DateTimeColumn(verbose_name=_("Revoked"), format="Y-m-d H:i")
    request_id = tables.Column(verbose_name=_("Request ID"))
    object_change = tables.Column(verbose_name=_("Audit change"), empty_values=())

    class Meta(BaseTable.Meta):
        model = TenantResourceGrantExpiryRevocation
        fields = ("grant_id", "triggering_valid_until", "revoked_at", "request_id", "object_change")
        default_columns = fields

    def render_grant_id(self, value, record):
        return format_html('<a href="{}">{}</a>', f"/api/organization/resource-grant-audit/{value}/", value)

    def render_object_change(self, value, record):
        if value is None:
            return _("Retained evidence; audit change pruned")
        return format_html('<a href="{}">{}</a>', value.get_absolute_url(), value.pk)
