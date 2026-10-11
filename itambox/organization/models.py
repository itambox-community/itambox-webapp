from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.managers import (
    AllObjectsManager,
    ExplicitScopeAllObjectsManager,
    ExplicitScopeManager,
    ExplicitScopeSoftDeleteManager,
    SoftDeleteManager,
    TenantScopingQuerySet,
)
from core.mfa import role_is_privileged
from core.mixins import (
    AutoSlugMixin,
    BookmarkableMixin,
    CustomFieldDataMixin,
    SoftDeleteMixin,
    SubscribableMixin,
)
from core.models import BaseModel, ChangeLoggingMixin, DeletableVaultModel, StandardModel
from core.tenant_scope import get_descendant_tenant_group_ids


def _default_currency():
    return getattr(settings, "ITAMBOX_DEFAULT_CURRENCY", "EUR")


# Create your models here.


class Location(CustomFieldDataMixin, SubscribableMixin, StandardModel, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    STATUS_PLANNED = "planned"
    STATUS_STAGING = "staging"
    STATUS_ACTIVE = "active"
    STATUS_DECOMMISSIONING = "decommissioning"
    STATUS_RETIRED = "retired"
    STATUS_CHOICES = [
        (STATUS_PLANNED, _("Planned")),
        (STATUS_STAGING, _("Staging")),
        (STATUS_ACTIVE, _("Active")),
        (STATUS_DECOMMISSIONING, _("Decommissioning")),
        (STATUS_RETIRED, _("Retired")),
    ]

    site = models.ForeignKey(
        "Site",  # Use string reference
        on_delete=models.PROTECT,  # Or PROTECT if locations shouldn't be deleted when site is
        related_name="locations",
        db_index=True,
        verbose_name=_("Site"),
        # null=True # REMOVED temporary null
        # No blank=True as per requirements
    )
    name = models.CharField(
        max_length=100, db_index=True, verbose_name=_("Name")
    )  # Changed max_length based on Site/Region etc.
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    status = models.CharField(
        max_length=50, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True, verbose_name=_("Status")
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, related_name="children", blank=True, null=True, verbose_name=_("Parent")
    )
    tenant = models.ForeignKey(
        "Tenant",
        on_delete=models.PROTECT,
        related_name="locations",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Tenant"),
    )
    facility = models.CharField(max_length=100, blank=True, verbose_name=_("Facility"))
    description = models.TextField(
        blank=True, verbose_name=_("Description")
    )  # Using TextField for potentially longer descriptions
    tags = models.ManyToManyField("extras.Tag", related_name="locations", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["site", "name"]
        verbose_name = _("Location")
        verbose_name_plural = _("Locations")
        constraints = [
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_location_slug_active"
            ),
        ]

    def __str__(self):
        # Consider showing parent hierarchy later if needed
        return self.name

    def get_absolute_url(self):
        return reverse("organization:location_detail", kwargs={"pk": self.pk})


class Region(StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="children",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Parent"),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    tags = models.ManyToManyField("extras.Tag", related_name="regions", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Region")
        verbose_name_plural = _("Regions")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_region_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_region_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization:region_detail", kwargs={"pk": self.pk})


class SiteGroup(StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, related_name="children", blank=True, null=True, verbose_name=_("Parent")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    tags = models.ManyToManyField("extras.Tag", related_name="sitegroups", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Site Group")
        verbose_name_plural = _("Site Groups")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_sitegroup_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_sitegroup_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization:sitegroup_detail", kwargs={"pk": self.pk})


class TenantGroup(StandardModel, SoftDeleteMixin):
    tenant_scope_self = "group"
    # Tenant-scoped via a dedicated branch in filter_by_tenant: a user sees the
    # groups containing a tenant they're a member of, plus those groups'
    # ancestors. Internal tenancy machinery (the descendant walk, middleware
    # group resolution) uses TenantGroup._base_manager to stay unscoped.
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = AllObjectsManager()
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, related_name="children", blank=True, null=True, verbose_name=_("Parent")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    tags = models.ManyToManyField("extras.Tag", related_name="tenantgroups", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Tenant Group")
        verbose_name_plural = _("Tenant Groups")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_tenantgroup_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_tenantgroup_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization:tenantgroup_detail", kwargs={"pk": self.pk})

    def clean(self):
        # Runs on every save via the global pre_save validator — keep it cheap.
        # A parent cycle would send every group-scoped descendant walk in
        # circles (the walks are cycle-tolerant, but the tree would be wrong),
        # so reject it at write time like CostCenter does.
        from django.core.exceptions import ValidationError

        super().clean()
        if self.parent_id is None:
            return
        if self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": _("A tenant group cannot be its own parent.")})
        if self.pk:
            visited = set()
            node_id = self.parent_id
            while node_id is not None:
                if node_id == self.pk:
                    raise ValidationError(
                        {"parent": _("Setting this parent would create a cycle in the group hierarchy.")}
                    )
                if node_id in visited:
                    break
                visited.add(node_id)
                node_id = TenantGroup._base_manager.filter(pk=node_id).values_list("parent_id", flat=True).first()


class Tenant(DeletableVaultModel, BookmarkableMixin):
    tenant_scope_self = "tenant"
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    group = models.ForeignKey(
        TenantGroup, on_delete=models.SET_NULL, related_name="tenants", blank=True, null=True, verbose_name=_("Group")
    )
    is_provider = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Manages other tenants (MSP mode)"),
        help_text=_(
            "Superuser-controlled. When set, this tenant can manage other tenants "
            "and its members can be granted reach into them."
        ),
    )
    managed_by = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="managed_tenants",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Managed by"),
        help_text=_("The managing (provider) tenant. Empty for standalone/root tenants."),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    comments = models.TextField(blank=True, verbose_name=_("Comments"))
    tags = models.ManyToManyField("extras.Tag", related_name="tenants", blank=True, verbose_name=_("Tags"))
    currency = models.CharField(
        max_length=3,
        default=_default_currency,
        verbose_name=_("Display currency"),
        help_text=_("ISO 4217 currency code used for value display (display only, no conversion)."),
    )
    default_depreciation = models.ForeignKey(
        "assets.Depreciation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenants_defaulting",
        verbose_name=_("Default depreciation policy"),
        help_text=_(
            "Fallback policy applied to all assets that have no type-level schedule and no per-asset override."
        ),
    )
    changelog_retention_days = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        verbose_name=_("Changelog retention override (days)"),
        help_text=_(
            "Overrides ITAMBOX_CHANGELOG_RETENTION_DAYS for this tenant's ObjectChange "
            "rows only. Blank uses the global setting. 0 = unlimited (legal hold -- this "
            "tenant's changelog is never pruned by prune_changelog)."
        ),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("Tenant")
        verbose_name_plural = _("Tenants")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_tenant_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_tenant_slug_active"
            ),
        ]

    def clean(self):
        # Runs on every save via the global pre_save validator — keep it cheap.
        # Depth-1 management tree (decided 2026-07-10): a manager cannot itself be
        # managed, and a managed tenant cannot manage others.
        from django.core.exceptions import ValidationError

        super().clean()
        if self.managed_by_id:
            if self.managed_by_id == self.pk:
                raise ValidationError({"managed_by": _("A tenant cannot manage itself.")})
            if self.is_provider:
                raise ValidationError(
                    {"is_provider": _("A tenant managed by another tenant cannot manage tenants itself.")}
                )
            manager = self.managed_by
            if not manager.is_provider:
                raise ValidationError(
                    {"managed_by": _("The selected managing tenant must be set to manage other tenants.")}
                )
            if manager.managed_by_id:
                raise ValidationError(
                    {
                        "managed_by": _(
                            "The selected managing tenant is already managed by another tenant. "
                            "Nested management is not supported."
                        )
                    }
                )

        if (
            self.pk
            and (not self.is_provider or self.deleted_at is not None)
            and Tenant._base_manager.filter(
                managed_by_id=self.pk,
                deleted_at__isnull=True,
            ).exists()
        ):
            raise ValidationError(
                {"is_provider": _("Before disabling or deleting this managing tenant, move all tenants it manages.")}
            )

    def get_absolute_url(self):
        return reverse("organization:tenant_detail", kwargs={"pk": self.pk})

    def __str__(self):
        return self.name


class Site(DeletableVaultModel, BookmarkableMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    STATUS_PLANNED = "planned"
    STATUS_STAGING = "staging"
    STATUS_ACTIVE = "active"
    STATUS_DECOMMISSIONING = "decommissioning"
    STATUS_RETIRED = "retired"
    STATUS_CHOICES = [
        (STATUS_PLANNED, _("Planned")),
        (STATUS_STAGING, _("Staging")),
        (STATUS_ACTIVE, _("Active")),
        (STATUS_DECOMMISSIONING, _("Decommissioning")),
        (STATUS_RETIRED, _("Retired")),
    ]

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    status = models.CharField(
        max_length=50, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True, verbose_name=_("Status")
    )
    # Use local models for FKs within the app
    region = models.ForeignKey(
        Region,
        on_delete=models.SET_NULL,
        related_name="sites",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Region"),
    )
    group = models.ForeignKey(
        SiteGroup,
        on_delete=models.SET_NULL,
        related_name="sites",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Group"),
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        related_name="sites",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Tenant"),
    )
    facility = models.CharField(max_length=100, blank=True, verbose_name=_("Facility"))
    time_zone = models.CharField(
        max_length=63, blank=True, verbose_name=_("Time Zone")
    )  # Consider using timezone_field package later
    description = models.CharField(max_length=200, blank=True, verbose_name=_("Description"))
    physical_address = models.CharField(max_length=200, blank=True, verbose_name=_("Physical Address"))
    shipping_address = models.CharField(max_length=200, blank=True, verbose_name=_("Shipping Address"))
    latitude = models.DecimalField(max_digits=8, decimal_places=6, blank=True, null=True, verbose_name=_("Latitude"))
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True, verbose_name=_("Longitude"))
    comments = models.TextField(blank=True, verbose_name=_("Comments"))
    tags = models.ManyToManyField("extras.Tag", related_name="sites", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Site")
        verbose_name_plural = _("Sites")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_site_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_site_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization:site_detail", kwargs={"pk": self.pk})


# +++ AssetHolder Model +++
class AssetHolder(CustomFieldDataMixin, SubscribableMixin, StandardModel, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,  # Keep holder if user is deleted, set user link to null
        related_name="asset_holder_profiles",  # Custom related_name
        blank=True,
        null=True,
        verbose_name=_("User"),
    )
    first_name = models.CharField(max_length=100, verbose_name=_("First Name"))
    last_name = models.CharField(max_length=100, verbose_name=_("Last Name"))
    upn = models.CharField(max_length=255, verbose_name=_("User Principal Name"))
    email = models.EmailField(blank=True, verbose_name=_("Email"))
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="asset_holders",
        db_index=True,
        verbose_name=_("Tenant"),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    comments = models.TextField(blank=True, verbose_name=_("Comments"))
    tags = models.ManyToManyField(
        "extras.Tag", blank=True, related_name="organization_assetholders", verbose_name=_("Tags")
    )  # M2M to extras.Tag

    class Meta:
        ordering = ["last_name", "first_name", "upn"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_tenant_user_profile",
            ),
            models.UniqueConstraint(
                fields=["tenant", "upn"], condition=models.Q(deleted_at__isnull=True), name="unique_tenant_upn"
            ),
        ]
        verbose_name = _("Asset Holder")
        verbose_name_plural = _("Asset Holders")

    @property
    def checked_out_assets(self):
        AssetAssignment = apps.get_model("assets", "AssetAssignment")

        return AssetAssignment.objects.filter(assigned_user=self, is_active=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.upn})"

    def get_absolute_url(self):
        # Assuming you'll have a detail view named 'assetholder_detail'
        return reverse("organization:assetholder_detail", kwargs={"pk": self.pk})


class ContactRole(AutoSlugMixin, StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    description = models.TextField(blank=True, verbose_name=_("Description"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Contact Role")
        verbose_name_plural = _("Contact Roles")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_contactrole_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_contactrole_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization:contactrole_detail", kwargs={"pk": self.pk})


class Contact(CustomFieldDataMixin, StandardModel, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    # Hybrid tenancy: tenant=None is a global/shared contact (manufacturers,
    # service desks) visible to every tenant; a set tenant makes the contact
    # private to that tenant. allow_global_tenant surfaces the tenant=None rows
    # under tenant scoping. Changelog attribution follows the `tenant` field
    # automatically (None → global, surfaced via ObjectChange.allow_global_tenant).
    allow_global_tenant = True

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="contacts",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_(
            "Owning tenant. Leave blank for a global/shared contact "
            "(e.g. a manufacturer or service desk) visible to all tenants."
        ),
    )
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    title = models.CharField(max_length=100, blank=True, verbose_name=_("Title"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Phone"))
    email = models.EmailField(blank=True, verbose_name=_("Email"))
    web_url = models.URLField(blank=True, verbose_name=_("Web URL"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    comments = models.TextField(blank=True, verbose_name=_("Comments"))
    tags = models.ManyToManyField(
        "extras.Tag", blank=True, related_name="organization_contacts", verbose_name=_("Tags")
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("Contact")
        verbose_name_plural = _("Contacts")

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization:contact_detail", kwargs={"pk": self.pk})


class ContactAssignment(ChangeLoggingMixin, BaseModel):
    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="assignments", verbose_name=_("Contact")
    )
    role = models.ForeignKey(ContactRole, on_delete=models.PROTECT, related_name="assignments", verbose_name=_("Role"))
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    assigned_object = GenericForeignKey("content_type", "object_id")
    priority = models.CharField(
        max_length=50,
        choices=[
            ("primary", _("Primary")),
            ("secondary", _("Secondary")),
            ("tertiary", _("Tertiary")),
            ("inactive", _("Inactive")),
        ],
        blank=True,
        verbose_name=_("Priority"),
    )

    class Meta:
        ordering = ["contact", "role", "content_type", "object_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "role", "content_type", "object_id"], name="organization_contactassignment_unique"
            )
        ]
        verbose_name = _("Contact Assignment")
        verbose_name_plural = _("Contact Assignments")

    @property
    def tenant(self):
        # Contact is hybrid-tenant-scoped (a `tenant` field, tenant=None for
        # global/shared rows), but an assignment's own tenant is still derived
        # from the generic-FK target, not from `self.contact.tenant` — the
        # target is what StrictTenantPermission must bound the assignment to.
        # Exposing `tenant` lets StrictTenantPermission enforce the object-level
        # boundary on detail/mutation: it compares obj.tenant to the request's
        # active tenant. Targets with no tenant (global/shared catalogue rows)
        # return None and remain accessible, matching validate_gfk_target_tenant.
        obj = self.assigned_object
        return getattr(obj, "tenant", None) if obj is not None else None

    def __str__(self):
        return f"{self.contact} ({self.role}) assigned to {self.assigned_object}"


# ---------------------------------------------------------------------------
# Unified RBAC: Role + Membership + RoleGrant + RoleGrantScope
#
# One container type (Tenant), one permission vocabulary ('app.codename'), and
# scoped RoleGrant rows. A provider (MSP) is just a tenant with
# ``is_provider=True`` that other tenants point at via ``managed_by``; reach
# into those managed tenants is a property of the individual grant.
# ---------------------------------------------------------------------------


class Role(AutoSlugMixin, StandardModel, SoftDeleteMixin):
    """A named permission set owned by (and applying in) one tenant.

    A role owned by a managing (``is_provider``) tenant may additionally be
    *shared* with its managed tenants (``shared_with_managed``): managed-tenant
    admins can assign it to their own members but never edit it — a live shared
    definition, not a clone, so an edit at the owner propagates everywhere.
    """

    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="roles",
        db_index=True,
        verbose_name=_("Tenant"),
    )
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    permissions = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Permissions"),
        help_text=_("List of permission codenames ('app_label.codename')."),
    )
    shared_with_managed = models.BooleanField(
        default=False,
        verbose_name=_("Share with managed tenants"),
        help_text=_(
            "Only meaningful when the owning tenant manages others: managed tenants "
            "may assign this role to their own members; only the owning tenant can "
            "edit it."
        ),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Role")
        verbose_name_plural = _("Roles")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="organization_role_unique_tenant_name",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.tenant.name})" if self.tenant_id else self.name

    def get_absolute_url(self):
        return reverse("organization:role_detail", kwargs={"pk": self.pk})

    # NOTE: Role intentionally has NO model-layer ``clean()`` validation of ``permissions``.
    # A global ``pre_save`` receiver (core/signals.py::validate_custom_validators_on_save) runs
    # ``clean()`` on every ChangeLoggingMixin save, so a hard check here would fire on ALL
    # writes — incompatible with this codebase's deliberate design: RoleForm drops unknown
    # codenames, and ``validate_role_permissions`` audits persisted stale codenames post-hoc.
    # Real write-time integrity belongs in a future ManyToManyField(auth.Permission) migration.


class Membership(ChangeLoggingMixin, models.Model):
    """A user's binding to one tenant — the thin "person belongs here" anchor.

    What the person may do is carried by direct and group ``RoleGrant`` rows.
    ``is_active=False`` suspends every grant reached through this membership while
    retaining its audit history and any AssetHolder linkage for re-activation.

    NOTE: the global ``User.is_active`` / ``User.can_login`` is synced from
    memberships ONLY by the SCIM provisioning paths, which clear it when a
    provisioned user has no active membership left. The interactive UI does NOT
    auto-clear it: a user whose last membership is deactivated stays authenticated
    but lands on the "no accessible workspace" page.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("User"),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="memberships",
        db_index=True,
        verbose_name=_("Tenant"),
    )
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Active"))
    external_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("External ID"),
        help_text=_("SCIM identifier scoped to this provisioning domain."),
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user"]
        verbose_name = _("Membership")
        verbose_name_plural = _("Memberships")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "tenant"],
                name="organization_membership_unique_user_tenant",
            ),
            models.UniqueConstraint(
                fields=["tenant", "external_id"],
                condition=~models.Q(external_id=""),
                name="organization_membership_unique_tenant_external_id",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "external_id"], name="org_membership_external_idx"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.tenant}"

    def get_absolute_url(self):
        return reverse("organization:membership_detail", kwargs={"pk": self.pk})

    @property
    def is_staff_membership(self):
        """True when any grant on this membership reaches managed tenants."""
        return (
            self.role_grants.filter(
                role__deleted_at__isnull=True,
                scopes__scope_type__in=(
                    RoleGrantScope.SCOPE_TENANT,
                    RoleGrantScope.SCOPE_TENANT_GROUP,
                    RoleGrantScope.SCOPE_ALL_MANAGED,
                ),
            )
            .filter(models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=timezone.now()))
            .exists()
        )


class RoleGrant(ChangeLoggingMixin, models.Model):
    """A role granted to exactly one Membership or tenant-owned UserGroup.

    Scope is represented exclusively by child :class:`RoleGrantScope` rows.
    """

    REACH_OWN = "own"
    REACH_MANAGED = "managed"
    REACH_CHOICES = [
        (REACH_OWN, _("This tenant")),
        (REACH_MANAGED, _("Managed tenants")),
    ]

    membership = models.ForeignKey(
        "organization.Membership",
        on_delete=models.CASCADE,
        related_name="role_grants",
        blank=True,
        null=True,
        verbose_name=_("Membership"),
    )
    user_group = models.ForeignKey(
        "users.UserGroup",
        on_delete=models.CASCADE,
        related_name="role_grants",
        blank=True,
        null=True,
        verbose_name=_("User group"),
    )
    role = models.ForeignKey(
        "organization.Role",
        on_delete=models.CASCADE,
        related_name="role_grants",
        verbose_name=_("Role"),
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="granted_role_grants",
        blank=True,
        null=True,
        verbose_name=_("Granted by"),
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True, verbose_name=_("Reason"))
    valid_until = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Valid until"),
    )

    class Meta:
        ordering = ["role", "membership", "user_group"]
        verbose_name = _("Role grant")
        verbose_name_plural = _("Role grants")
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(membership__isnull=False, user_group__isnull=True)
                    | models.Q(membership__isnull=True, user_group__isnull=False)
                ),
                name="organization_rolegrant_exactly_one_principal",
            ),
            models.UniqueConstraint(
                fields=["user_group", "role"],
                condition=models.Q(user_group__isnull=False),
                name="organization_rolegrant_unique_group_role",
            ),
        ]
        indexes = [
            models.Index(fields=["membership", "role"], name="org_rolegrant_member_role_idx"),
            models.Index(fields=["user_group", "role"], name="org_rolegrant_group_role_idx"),
        ]

    @property
    def tenant(self):
        if self.membership_id:
            return self.membership.tenant
        if self.user_group_id:
            return self.user_group.tenant
        return None

    @property
    def principal_tenant_id(self):
        if self.membership_id:
            return self.membership.tenant_id
        if self.user_group_id:
            return self.user_group.tenant_id
        return None

    @property
    def is_active(self):
        return self.valid_until is None or self.valid_until > timezone.now()

    @property
    def reach(self):
        """Presentation-level reach derived from the grant's additive scopes."""
        if any(scope.scope_type != RoleGrantScope.SCOPE_OWN for scope in self.scopes.all()):
            return self.REACH_MANAGED
        return self.REACH_OWN

    def get_reach_display(self):
        return dict(self.REACH_CHOICES)[self.reach]

    def __str__(self):
        principal = self.membership or self.user_group
        return f"{principal}: {self.role}"

    def clean(self):
        super().clean()
        if bool(self.membership_id) == bool(self.user_group_id):
            raise ValidationError(_("Choose either a member or a user group for this role, not both."))

        owner_id = self.principal_tenant_id
        if owner_id is None:
            raise ValidationError(_("The selected member or user group must belong to a tenant."))

        if self.user_group_id and self.role_id and self.role.tenant_id != owner_id:
            raise ValidationError({"role": _("Choose a role owned by the group's tenant.")})

        if self.membership_id and self.role_id and self.role.tenant_id != owner_id:
            shared_own_role = (
                self.role.shared_with_managed
                and self.role.tenant.is_provider
                and self.membership.tenant.managed_by_id == self.role.tenant_id
            )
            if not shared_own_role:
                raise ValidationError({"role": _("Choose a role owned by this tenant or its managing tenant.")})

        if self.membership_id and self.role_id and role_is_privileged(self.role):
            errors = {}
            if not self.reason.strip():
                errors["reason"] = _("Directly assigned elevated roles require a reason.")
            if self.valid_until is None:
                errors["valid_until"] = _("Directly assigned elevated roles require an expiration.")
            elif self.valid_until <= timezone.now():
                errors["valid_until"] = _("The expiration must be in the future.")
            if errors:
                raise ValidationError(errors)

    def scoped_tenant_ids(self):
        """Concrete managed-tenant coverage of this grant."""
        if self.principal_tenant_id != self.role.tenant_id or not self.role.tenant.is_provider:
            return set()
        tenant_ids = set()
        for scope in self.scopes.all():
            if scope.scope_type == RoleGrantScope.SCOPE_ALL_MANAGED:
                tenant_ids.update(
                    Tenant._base_manager.filter(
                        managed_by_id=self.role.tenant_id,
                        deleted_at__isnull=True,
                    ).values_list("pk", flat=True)
                )
            elif scope.scope_type == RoleGrantScope.SCOPE_TENANT:
                if (
                    scope.tenant_id
                    and Tenant._base_manager.filter(
                        pk=scope.tenant_id,
                        managed_by_id=self.role.tenant_id,
                        deleted_at__isnull=True,
                    ).exists()
                ):
                    tenant_ids.add(scope.tenant_id)
            elif scope.scope_type == RoleGrantScope.SCOPE_TENANT_GROUP:
                tenant_ids.update(
                    Tenant._base_manager.filter(
                        managed_by_id=self.role.tenant_id,
                        group_id__in=get_descendant_tenant_group_ids(
                            scope.tenant_group_id,
                            live_only=True,
                        ),
                        deleted_at__isnull=True,
                    ).values_list("pk", flat=True)
                )
        return tenant_ids

    def covers_tenant(self, tenant):
        """Return whether any live additive scope on this grant covers ``tenant``."""
        if not self.is_active or self.role.deleted_at is not None:
            return False
        owner_id = self.principal_tenant_id
        if owner_id is None:
            return False

        for scope in self.scopes.all():
            if scope.scope_type == RoleGrantScope.SCOPE_OWN:
                if tenant.pk != owner_id:
                    continue
                if self.role.tenant_id == owner_id:
                    return True
                if (
                    self.membership_id
                    and self.role.shared_with_managed
                    and self.role.tenant.is_provider
                    and tenant.managed_by_id == self.role.tenant_id
                ):
                    return True
                continue

            # Every managed projection is gated by the live management edge and
            # by provider ownership of both principal and role.
            if (
                tenant.managed_by_id != self.role.tenant_id
                or owner_id != self.role.tenant_id
                or not self.role.tenant.is_provider
            ):
                continue
            if scope.scope_type == RoleGrantScope.SCOPE_ALL_MANAGED:
                return True
            if scope.scope_type == RoleGrantScope.SCOPE_TENANT:
                if scope.tenant_id == tenant.pk:
                    return True
                continue
            if scope.scope_type == RoleGrantScope.SCOPE_TENANT_GROUP:
                if not scope.tenant_group_id or not tenant.group_id:
                    continue
                if tenant.group_id in get_descendant_tenant_group_ids(
                    scope.tenant_group_id,
                    live_only=True,
                ):
                    return True
        return False


class RoleGrantScope(ChangeLoggingMixin, models.Model):
    """One additive reach boundary for a RoleGrant; explicit deny is unsupported."""

    # RoleGrant survives a parent Role soft-delete as inert audit history. The
    # soft-delete collector also sees its non-soft-deletable children directly,
    # so scopes must opt out independently or the aggregate is only half kept.

    SCOPE_OWN = "own"
    SCOPE_TENANT = "tenant"
    SCOPE_TENANT_GROUP = "tenant_group"
    SCOPE_ALL_MANAGED = "all_managed"
    SCOPE_CHOICES = [
        (SCOPE_OWN, _("Principal tenant")),
        (SCOPE_TENANT, _("Specific managed tenant")),
        (SCOPE_TENANT_GROUP, _("Managed tenant group + descendants")),
        (SCOPE_ALL_MANAGED, _("All managed tenants")),
    ]

    role_grant = models.ForeignKey(
        "organization.RoleGrant",
        on_delete=models.CASCADE,
        related_name="scopes",
        verbose_name=_("Role grant"),
    )
    scope_type = models.CharField(
        max_length=20,
        choices=SCOPE_CHOICES,
        db_index=True,
        verbose_name=_("Scope type"),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="role_grant_scopes",
        blank=True,
        null=True,
        verbose_name=_("Tenant"),
    )
    tenant_group = models.ForeignKey(
        "organization.TenantGroup",
        on_delete=models.CASCADE,
        related_name="role_grant_scopes",
        blank=True,
        null=True,
        verbose_name=_("Tenant group"),
    )

    class Meta:
        ordering = ["role_grant", "scope_type", "tenant", "tenant_group"]
        verbose_name = _("Role grant scope")
        verbose_name_plural = _("Role grant scopes")
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(
                        scope_type__in=["own", "all_managed"],
                        tenant__isnull=True,
                        tenant_group__isnull=True,
                    )
                    | models.Q(
                        scope_type="tenant",
                        tenant__isnull=False,
                        tenant_group__isnull=True,
                    )
                    | models.Q(
                        scope_type="tenant_group",
                        tenant__isnull=True,
                        tenant_group__isnull=False,
                    )
                ),
                name="organization_rolegrantscope_shape",
            ),
            models.UniqueConstraint(
                fields=["role_grant", "scope_type"],
                condition=models.Q(scope_type__in=["own", "all_managed"]),
                name="organization_rolegrantscope_unique_singleton",
            ),
            models.UniqueConstraint(
                fields=["role_grant", "tenant"],
                condition=models.Q(scope_type="tenant"),
                name="organization_rolegrantscope_unique_tenant",
            ),
            models.UniqueConstraint(
                fields=["role_grant", "tenant_group"],
                condition=models.Q(scope_type="tenant_group"),
                name="organization_rolegrantscope_unique_group",
            ),
        ]

    def __str__(self):
        target = self.tenant or self.tenant_group or self.get_scope_type_display()
        return f"{self.role_grant} -> {target}"

    def clean(self):
        super().clean()
        has_tenant = self.tenant_id is not None
        has_group = self.tenant_group_id is not None
        if self.scope_type in (self.SCOPE_OWN, self.SCOPE_ALL_MANAGED):
            if has_tenant or has_group:
                raise ValidationError(_("This coverage option cannot include a specific tenant or tenant group."))
        elif self.scope_type == self.SCOPE_TENANT:
            if not has_tenant or has_group:
                raise ValidationError(_("Choose exactly one tenant for this coverage."))
        elif self.scope_type == self.SCOPE_TENANT_GROUP:
            if has_tenant or not has_group:
                raise ValidationError(_("Choose exactly one tenant group for this coverage."))
        else:
            raise ValidationError({"scope_type": _("Choose a valid role coverage option.")})

        # The identity service sets this operation-local marker only while moving
        # a locked historical scope between grants with the same role and principal.
        if not self.role_grant_id or getattr(self, "_identity_scope_merge", False):
            return
        grant = self.role_grant
        owner_id = grant.principal_tenant_id
        if self.scope_type == self.SCOPE_OWN:
            valid_shared_role = (
                grant.membership_id
                and grant.role.shared_with_managed
                and grant.role.tenant.is_provider
                and grant.membership.tenant.managed_by_id == grant.role.tenant_id
            )
            if grant.role.tenant_id != owner_id and not valid_shared_role:
                raise ValidationError(_("Choose a role available in the member or group's tenant."))
            return
        if owner_id != grant.role.tenant_id or not grant.role.tenant.is_provider:
            raise ValidationError(_("Managed-tenant access requires a role owned by the managing tenant."))
        if self.scope_type == self.SCOPE_TENANT and self.tenant.managed_by_id != owner_id:
            raise ValidationError(
                {"tenant": _("The selected tenant is not managed by the tenant that owns this role.")}
            )


class CostCenter(AutoSlugMixin, CustomFieldDataMixin, StandardModel, SoftDeleteMixin):
    """
    Represents a cost center or department.  A top-level instance (parent=None)
    is a cost center; a child instance is a department within that cost center.
    The same model handles both via the optional self-referential parent.
    """

    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    tenant = models.ForeignKey(
        "Tenant",
        on_delete=models.PROTECT,
        related_name="cost_centers",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Tenant"),
    )
    name = models.CharField(max_length=100, db_index=True, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    code = models.CharField(
        max_length=50,
        db_index=True,
        help_text=_('Short identifier for this cost center (e.g. "CC-100").'),
        verbose_name=_("Code"),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Parent"),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Is Active"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Cost Center")
        verbose_name_plural = _("Cost Centers")
        constraints = [
            # code unique per active tenant (soft-delete-aware)
            models.UniqueConstraint(
                fields=["tenant", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="organization_costcenter_unique_tenant_code_active",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(deleted_at__isnull=True),
                name="organization_costcenter_unique_slug_active",
            ),
        ]

    def __str__(self):
        return f"{self.code} – {self.name}" if self.code else self.name

    def get_absolute_url(self):
        return reverse("organization:costcenter_detail", kwargs={"pk": self.pk})

    @property
    def depth(self):
        """Zero-based depth in the hierarchy (0 = top-level cost center)."""
        level = 0
        node = self
        while node.parent_id is not None:
            node = node.parent
            level += 1
            if level > 50:  # guard against accidental cycles in DB
                break
        return level

    @property
    def full_path(self):
        """Slash-joined name path from root to this node."""
        parts = [self.name]
        node = self
        visited = {self.pk}
        while node.parent_id is not None:
            node = node.parent
            if node.pk in visited:
                break
            visited.add(node.pk)
            parts.insert(0, node.name)
        return " / ".join(parts)

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.parent_id is None:
            return
        # Self-parent guard
        if self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": _("A cost center cannot be its own parent.")})
        # Ancestor-cycle guard
        if self.pk:
            visited = set()
            node = self.parent
            while node is not None:
                if node.pk == self.pk:
                    raise ValidationError({"parent": _("Setting this parent would create a cycle in the hierarchy.")})
                if node.pk in visited:
                    break
                visited.add(node.pk)
                node = node.parent


# ---------------------------------------------------------------------------
# Cross-tenant resource sharing
# ---------------------------------------------------------------------------


class TenantResourceGrant(SoftDeleteMixin, ChangeLoggingMixin, BaseModel):
    """One tenant's explicit permission for another tenant (or a TenantGroup
    subtree) to see/use ONE of its stock pools.

    ADR-0001: TenantGroup membership makes sharing *eligible*, never
    automatic — every cross-tenant resource use requires one of these rows.
    Grants are non-transitive, convey no user permissions (RBAC is checked
    independently by the resolver), and are revoked by soft-delete so that
    historical assignments keep their provenance pointer.

    Like Membership/RoleGrant, the default manager is deliberately
    UNSCOPED: this is authorization infrastructure that the resolver must
    read from both the owner's and the grantee's tenant context. UI/API
    surfaces scope their querysets explicitly.
    """

    # Soft-deleting the owner/grantee tenant must not destroy the audit
    # trail; grants become inert (the resolver re-checks liveness) and
    # restore re-arms them. Hard deletes still cascade.

    # NO all_objects manager, on purpose (mirrors Membership/RoleGrant):
    # revocation is a lifecycle state, not "trash" — grants must not surface
    # in the generic recycle bin for restore. Audit surfaces (admin) read
    # revoked rows through _base_manager.
    objects = SoftDeleteManager()

    ACCESS_VIEW = "view"
    ACCESS_USE = "use"
    ACCESS_CHOICES = [
        (ACCESS_VIEW, _("View")),
        (ACCESS_USE, _("View + allocate/consume")),
    ]

    #: The only models a grant may reference (tight allowlist by design —
    #: extend deliberately, never generically).
    APPROVED_RESOURCE_MODELS = (
        "inventory.componentstock",
        "inventory.accessorystock",
        "inventory.consumablestock",
    )

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="resource_grants_given",
        db_index=True,
        verbose_name=_("Owning tenant"),
        help_text=_("The tenant whose resource is being shared."),
    )
    grantee_tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="resource_grants_received",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Grantee tenant"),
    )
    grantee_tenant_group = models.ForeignKey(
        "organization.TenantGroup",
        on_delete=models.CASCADE,
        related_name="resource_grants_received",
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Grantee tenant group"),
        help_text=_("Grants to a group include its descendant groups."),
    )
    resource_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="+",
        limit_choices_to=models.Q(
            app_label="inventory",
            model__in=("componentstock", "accessorystock", "consumablestock"),
        ),
        verbose_name=_("Resource type"),
    )
    resource_id = models.PositiveBigIntegerField(verbose_name=_("Resource ID"))
    resource = GenericForeignKey("resource_type", "resource_id")
    access_level = models.CharField(
        max_length=10,
        choices=ACCESS_CHOICES,
        default=ACCESS_VIEW,
        db_index=True,
        verbose_name=_("Access level"),
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_resource_grants",
        verbose_name=_("Granted by"),
    )
    reason = models.TextField(blank=True, verbose_name=_("Reason"))
    valid_until = models.DateTimeField(blank=True, null=True, verbose_name=_("Valid until"))

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Resource Grant")
        verbose_name_plural = _("Resource Grants")
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(grantee_tenant__isnull=False, grantee_tenant_group__isnull=True)
                    | models.Q(grantee_tenant__isnull=True, grantee_tenant_group__isnull=False)
                ),
                name="organization_trg_exactly_one_grantee",
            ),
            models.CheckConstraint(
                check=(models.Q(grantee_tenant__isnull=True) | ~models.Q(grantee_tenant=models.F("tenant"))),
                name="organization_trg_owner_not_grantee",
            ),
            models.UniqueConstraint(
                fields=["resource_type", "resource_id", "grantee_tenant"],
                condition=models.Q(deleted_at__isnull=True, grantee_tenant__isnull=False),
                name="organization_trg_unique_active_tenant_grant",
            ),
            models.UniqueConstraint(
                fields=["resource_type", "resource_id", "grantee_tenant_group"],
                condition=models.Q(deleted_at__isnull=True, grantee_tenant_group__isnull=False),
                name="organization_trg_unique_active_group_grant",
            ),
        ]
        indexes = [
            models.Index(
                fields=["resource_type", "resource_id"],
                name="org_trg_resource_idx",
            ),
            models.Index(
                fields=["resource_type", "resource_id"],
                condition=models.Q(deleted_at__isnull=True),
                name="org_trg_active_resource_idx",
            ),
            models.Index(
                fields=["tenant", "valid_until"],
                condition=models.Q(deleted_at__isnull=True, valid_until__isnull=False),
                name="org_trg_active_expiry_idx",
            ),
        ]

    def __str__(self):
        grantee = self.grantee_tenant or self.grantee_tenant_group
        return f"{self.tenant} -> {grantee}: {self.resource_type.model} #{self.resource_id} ({self.access_level})"

    @property
    def is_active(self):
        return self.deleted_at is None

    def delete(self, *args, force_hard_delete=False, **kwargs):
        """Revocation (soft delete) must ALWAYS be possible.

        Historical assignments reference the grant with on_delete=PROTECT so
        a HARD delete cannot destroy provenance — but the SoftDeleteMixin
        soft path runs the deletion collector, which would trip over that
        very PROTECT. A revocation only stamps ``deleted_at`` and cascades
        to nothing, so skip the collector entirely on the soft path.
        """
        force_hard = force_hard_delete or getattr(self, "_force_hard_delete", False)
        if force_hard:
            return super().delete(*args, force_hard_delete=True, **kwargs)
        from django.db import transaction

        with transaction.atomic():
            if hasattr(self, "_changelog_action"):
                self._changelog_action = ObjectChangeActionChoices.ACTION_DELETE
            if hasattr(self, "snapshot") and callable(self.snapshot):
                self.snapshot()
            self.soft_delete()

    def clean(self):
        # Runs on every save via the global pre_save validator — keep it cheap.
        from django.core.exceptions import ValidationError

        super().clean()

        has_tenant = self.grantee_tenant_id is not None
        has_group = self.grantee_tenant_group_id is not None
        if has_tenant == has_group:
            raise ValidationError(_("Exactly one of grantee tenant or grantee tenant group must be set."))
        if has_tenant and self.grantee_tenant_id == self.tenant_id:
            raise ValidationError({"grantee_tenant": _("A tenant cannot be granted its own resource.")})

        if self.resource_type_id:
            label = f"{self.resource_type.app_label}.{self.resource_type.model}"
            if label not in self.APPROVED_RESOURCE_MODELS:
                raise ValidationError(
                    {
                        "resource_type": _("This model cannot be shared. Approved resources: %(models)s.")
                        % {"models": ", ".join(self.APPROVED_RESOURCE_MODELS)}
                    }
                )

        # Ownership-through-location proof — only while the grant is active:
        # a revoked grant must stay saveable (and restorable-to-inspect) even
        # after the pool has been deleted or moved.
        if self.deleted_at is None and self.resource_type_id and self.resource_id:
            model = self.resource_type.model_class()
            stock = (
                model._base_manager.filter(
                    pk=self.resource_id,
                )
                .select_related("location")
                .first()
            )
            if stock is None:
                raise ValidationError({"resource_id": _("The referenced stock pool does not exist.")})
            if stock.location.tenant_id is None or stock.location.tenant_id != self.tenant_id:
                raise ValidationError(
                    {
                        "tenant": _(
                            "The resource must belong to the owning tenant through its "
                            "location (the pool's location tenant does not match)."
                        )
                    }
                )


class TenantResourceGrantExpiryRun(BaseModel):
    """One generation-bound expiry sweep for one owner tenant and hour."""

    objects = ExplicitScopeManager()
    deny_global_tenant = True

    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_ENQUEUE_FAILED = "enqueue_failed"
    STATE_COMPLETE = "complete"
    STATE_CHOICES = (
        (STATE_QUEUED, _("Queued")),
        (STATE_RUNNING, _("Running")),
        (STATE_ENQUEUE_FAILED, _("Enqueue failed")),
        (STATE_COMPLETE, _("Complete")),
    )

    OUTCOME_CHOICES = (
        ("success", _("Success")),
        ("partial", _("Partial")),
        ("skipped", _("Skipped")),
        ("retryable", _("Retryable")),
        ("terminal", _("Terminal")),
    )

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        related_name="resource_grant_expiry_runs",
        verbose_name=_("Tenant"),
    )
    schedule_slot = models.DateTimeField(verbose_name=_("Schedule slot"))
    cutoff = models.DateTimeField(verbose_name=_("Cutoff"))
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_QUEUED, verbose_name=_("State"))
    outcome = models.CharField(
        max_length=20,
        choices=OUTCOME_CHOICES,
        blank=True,
        null=True,
        verbose_name=_("Outcome"),
    )
    generation = models.PositiveIntegerField(default=1, verbose_name=_("Generation"))
    attempt_count = models.PositiveIntegerField(default=0, verbose_name=_("Attempts"))
    started_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Started at"))
    last_attempt_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Last attempt at"))
    finished_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Finished at"))
    next_retry_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Next retry at"))
    dispatch_stale_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Dispatch stale at"))
    lease_expires_at = models.DateTimeField(blank=True, null=True, verbose_name=_("Lease expires at"))
    revoked_count = models.PositiveIntegerField(default=0, verbose_name=_("Revoked count"))
    remaining_due_count = models.PositiveIntegerField(default=0, verbose_name=_("Remaining due count"))
    invalid_count = models.PositiveIntegerField(default=0, verbose_name=_("Invalid count"))
    error_code = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Error code"))
    error_message = models.TextField(blank=True, null=True, verbose_name=_("Error message"))

    class Meta:
        default_permissions = ()
        ordering = ("-schedule_slot", "-pk")
        verbose_name = _("Resource Grant Expiry Run")
        verbose_name_plural = _("Resource Grant Expiry Runs")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "schedule_slot"],
                name="organization_trg_expiry_run_tenant_slot_uniq",
            ),
            models.CheckConstraint(
                check=models.Q(generation__gte=1),
                name="organization_trg_expiry_run_generation_ck",
            ),
            models.CheckConstraint(
                check=models.Q(attempt_count__gte=0),
                name="organization_trg_expiry_run_attempts_ck",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(
                        state="queued",
                        outcome__isnull=True,
                        finished_at__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                    | models.Q(
                        state="queued",
                        outcome="retryable",
                        finished_at__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                    | models.Q(
                        state="running",
                        outcome__isnull=True,
                        finished_at__isnull=True,
                        lease_expires_at__isnull=False,
                    )
                    | models.Q(
                        state="enqueue_failed",
                        outcome__isnull=False,
                        outcome="retryable",
                        finished_at__isnull=True,
                        lease_expires_at__isnull=True,
                    )
                    | models.Q(
                        state="complete",
                        outcome__isnull=False,
                        outcome__in=("success", "partial", "skipped", "terminal"),
                        finished_at__isnull=False,
                        lease_expires_at__isnull=True,
                        next_retry_at__isnull=True,
                    )
                ),
                name="organization_trg_expiry_run_state_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "-schedule_slot"],
                name="org_trg_run_tenant_slot_idx",
            ),
            models.Index(
                fields=["tenant", "outcome", "-schedule_slot"],
                name="org_trg_expiry_run_outcome_idx",
            ),
        ]

    def __str__(self):
        return f"{self.tenant} resource grant expiry {self.schedule_slot:%Y-%m-%d %H:%M UTC}"

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                ._base_manager.filter(pk=self.pk)
                .values(
                    "tenant_id",
                    "schedule_slot",
                    "cutoff",
                )
                .first()
            )
            if previous is not None and any(
                previous[field] != getattr(self, field) for field in ("tenant_id", "schedule_slot", "cutoff")
            ):
                raise ValidationError("Expiry run identity fields are immutable.")
        return super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("organization:tenantresourcegrantexpiryrun_detail", kwargs={"pk": self.pk})


class TenantResourceGrantExpiryRevocationQuerySet(TenantScopingQuerySet):
    """Evidence queries that reject cross-tenant or mismatched audit rows."""

    def integrity_valid(self):
        change_type = ContentType.objects.get_for_model(TenantResourceGrant)
        return self.filter(run__tenant_id=F("grant__tenant_id")).filter(
            models.Q(object_change__isnull=True)
            | models.Q(
                object_change__tenant_id=F("run__tenant_id"),
                object_change__changed_object_type_id=change_type.pk,
                object_change__changed_object_id=F("grant_id"),
                object_change__action=ObjectChangeActionChoices.ACTION_DELETE,
                object_change__request_id=F("request_id"),
            )
        )


class TenantResourceGrantExpiryRevocation(BaseModel):
    """Immutable evidence for one expiry-driven grant soft deletion."""

    objects = ExplicitScopeManager.from_queryset(TenantResourceGrantExpiryRevocationQuerySet)()
    all_objects = TenantResourceGrantExpiryRevocationQuerySet.as_manager()
    tenant_lookup = "run__tenant"
    deny_global_tenant = True

    run = models.ForeignKey(
        TenantResourceGrantExpiryRun,
        on_delete=models.CASCADE,
        related_name="revocations",
        verbose_name=_("Expiry run"),
    )
    grant = models.ForeignKey(
        TenantResourceGrant,
        on_delete=models.PROTECT,
        related_name="expiry_revocations",
        verbose_name=_("Resource grant"),
    )
    object_change = models.OneToOneField(
        "core.ObjectChange",
        on_delete=models.SET_NULL,
        related_name="resource_grant_expiry_revocation",
        blank=True,
        null=True,
        verbose_name=_("Delete change"),
    )
    triggering_valid_until = models.DateTimeField(verbose_name=_("Triggering deadline"))
    revoked_at = models.DateTimeField(verbose_name=_("Revoked at"))
    request_id = models.UUIDField(verbose_name=_("Request ID"))

    class Meta:
        default_permissions = ()
        base_manager_name = "all_objects"
        ordering = ("-revoked_at", "-pk")
        verbose_name = _("Resource Grant Expiry Revocation")
        verbose_name_plural = _("Resource Grant Expiry Revocations")
        constraints = [
            models.UniqueConstraint(
                fields=["grant", "revoked_at"],
                name="organization_trg_expiry_revoke_grant_time_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["run", "grant"],
                name="org_trg_revoke_run_grant_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                ._base_manager.filter(pk=self.pk)
                .values(
                    "triggering_valid_until",
                    "revoked_at",
                    "request_id",
                )
                .first()
            )
            if previous is not None and any(
                previous[field] != getattr(self, field)
                for field in ("triggering_valid_until", "revoked_at", "request_id")
            ):
                raise ValidationError("Expiry revocation identity fields are immutable.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Expiry revocation {self.grant_id} in run {self.run_id}"
