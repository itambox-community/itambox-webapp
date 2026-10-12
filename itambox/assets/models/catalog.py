"""Catalog models: StatusLabel, AssetRole, Manufacturer, Depreciation, AssetType,
Supplier, Category — shared reference data that assets point into.
"""

from django.contrib.contenttypes.fields import GenericRelation
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Count, IntegerField, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.choices import StatusTypeChoices
from core.managers import (
    AllObjectsManager,
    ExplicitScopeAllObjectsManager,
    ExplicitScopeSoftDeleteManager,
    SoftDeleteManager,
)
from core.mixins import AutoSlugMixin, CustomFieldDataMixin, SoftDeleteMixin
from core.models import BaseModel, StandardModel
from extras.models import CustomFieldset, SpecificationLibrary

_LIBRARY_DEFINITION_KEY_VALIDATOR = RegexValidator(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")


def scoped_related_count(child_queryset: QuerySet, relation_lookup: str) -> Coalesce:
    """Return a correlated count over an already-authorized child queryset."""
    related_rows = child_queryset.filter(**{relation_lookup: OuterRef("pk")}).order_by()
    count_query = (
        related_rows.values(relation_lookup)
        .annotate(scoped_count=Count("pk", distinct=True))
        .values("scoped_count")[:1]
    )
    return Coalesce(
        Subquery(count_query, output_field=IntegerField()),
        Value(0),
        output_field=IntegerField(),
    )


class StatusLabel(AutoSlugMixin, StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    # Back-compat aliases — canonical definitions live in assets.choices.
    TYPE_DEPLOYABLE = StatusTypeChoices.DEPLOYABLE
    TYPE_DEPLOYED = StatusTypeChoices.DEPLOYED
    TYPE_PENDING = StatusTypeChoices.PENDING
    TYPE_UNDEPLOYABLE = StatusTypeChoices.UNDEPLOYABLE
    TYPE_ARCHIVED = StatusTypeChoices.ARCHIVED
    TYPE_IN_REPAIR = StatusTypeChoices.IN_REPAIR
    TYPE_ON_ORDER = StatusTypeChoices.ON_ORDER
    TYPE_CHOICES = StatusTypeChoices.choices

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    type = models.CharField(
        max_length=50,
        choices=StatusTypeChoices.choices,
        default=StatusTypeChoices.DEPLOYABLE,
        db_index=True,
        verbose_name=_("Type"),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    color = models.CharField(
        max_length=6, blank=True, verbose_name=_("Color"), help_text=_("RGB color in hexadecimal (e.g. 00ff00)")
    )
    tags = models.ManyToManyField("extras.Tag", related_name="status_labels_tagged", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Status Label")
        verbose_name_plural = _("Status Labels")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_statuslabel_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_statuslabel_slug_active"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"

    def get_absolute_url(self):
        return reverse("assets:statuslabel_detail", kwargs={"pk": self.pk})


class AssetRole(StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    """Categorizes assets based on their functional role (e.g., Laptop, Monitor, Server)."""
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    color = models.CharField(
        max_length=6, blank=True, verbose_name=_("Color"), help_text=_("RGB color in hexadecimal (e.g. 00ff00)")
    )
    allows_components = models.BooleanField(
        default=False,
        verbose_name=_("Allows Components"),
        help_text=_("Assets with this role can have components allocated (servers, workstations, …)"),
    )
    tags = models.ManyToManyField(to="extras.Tag", related_name="asset_roles", blank=True, verbose_name=_("Tags"))

    class Meta:
        verbose_name = _("Asset Role")
        verbose_name_plural = _("Asset Roles")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_assetrole_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_assetrole_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        # Use standardized URL name
        return reverse("assets:assetrole_detail", args=[self.pk])


class Manufacturer(StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    slug = models.SlugField(max_length=255, verbose_name=_("Slug"))
    description = models.TextField(blank=True, verbose_name=_("Description"))

    contacts = GenericRelation("organization.ContactAssignment")
    tags = models.ManyToManyField("extras.Tag", related_name="manufacturers", blank=True, verbose_name=_("Tags"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Manufacturer")
        verbose_name_plural = _("Manufacturers")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_manufacturer_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_manufacturer_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("assets:manufacturer_detail", kwargs={"pk": self.pk})

    @property
    def get_support_contact(self):
        """Resolves the active support contact assignment dynamically."""
        # 1. Search for a Contact assignment with role slug 'support' or 'technical-support'
        assignment = self.contacts.filter(role__slug__in=["support", "technical-support"]).first()
        if not assignment:
            # 2. Fallback to any assignment with 'primary' priority
            assignment = self.contacts.filter(priority="primary").first()
        if not assignment:
            # 3. Fallback to any contact assignment
            assignment = self.contacts.first()

        return assignment.contact if assignment else None


class Depreciation(StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Method(models.TextChoices):
        STRAIGHT_LINE = "straight_line", _("Straight-line")
        NONE = "none", _("None (no depreciation)")

    class Convention(models.TextChoices):
        EXCLUDE_PURCHASE_MONTH = "exclude_purchase_month", _("Exclude purchase month (month diff)")
        INCLUDE_PURCHASE_MONTH = "include_purchase_month", _("Include purchase month (pro rata temporis)")

    name = models.CharField(max_length=100, verbose_name=_("Depreciation Name"))
    months = models.PositiveIntegerField(
        verbose_name=_("Lifespan (Months)"),
        help_text=_("Useful lifespan in months for straight-line calculations"),
    )
    method = models.CharField(
        max_length=20,
        choices=Method.choices,
        default=Method.STRAIGHT_LINE,
        verbose_name=_("Method"),
    )
    convention = models.CharField(
        max_length=30,
        choices=Convention.choices,
        default=Convention.INCLUDE_PURCHASE_MONTH,
        verbose_name=_("Convention"),
        help_text=_("Determines whether the acquisition month counts as a full depreciation month."),
    )
    immediate_expense_threshold = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Immediate expense threshold (GWG)"),
        help_text=_(
            "Assets with purchase cost at or below this amount are fully expensed in the month "
            "of acquisition (e.g. 800 for German GWG)."
        ),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Depreciation")
        verbose_name_plural = _("Depreciations")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_depreciation_name_active"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.months} months)"

    def get_absolute_url(self):
        return reverse("assets:depreciation_detail", kwargs={"pk": self.pk})


class AssetType(CustomFieldDataMixin, AutoSlugMixin, StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    soft_delete_preserve_references = True
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    """Defines a specific type of asset (e.g., a specific laptop model)."""
    slug_source = ("manufacturer__name", "model")

    MANAGEMENT_CORE = "core"
    MANAGEMENT_LIBRARY = "library"
    MANAGEMENT_LOCAL = "local"
    MANAGEMENT_KIND_CHOICES = [
        (MANAGEMENT_CORE, _("Core")),
        (MANAGEMENT_LIBRARY, _("Library")),
        (MANAGEMENT_LOCAL, _("Local")),
    ]
    LIFECYCLE_ACTIVE = "active"
    LIFECYCLE_DEPRECATED = "deprecated"
    LIFECYCLE_CHOICES = [
        (LIFECYCLE_ACTIVE, _("Active")),
        (LIFECYCLE_DEPRECATED, _("Deprecated")),
    ]

    manufacturer = models.ForeignKey(
        "assets.Manufacturer", on_delete=models.PROTECT, related_name="asset_types", verbose_name=_("Manufacturer")
    )
    model = models.CharField(max_length=255, db_index=True, verbose_name=_("Model"))
    slug = models.SlugField(max_length=255, verbose_name=_("Slug"))
    part_number = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name=_("Part Number"),
        help_text=_("Manufacturer part number or SKU"),
    )
    ean = models.CharField(
        max_length=14,
        blank=True,
        db_index=True,
        verbose_name=_("EAN"),
        help_text=_("Barcode (EAN / UPC / GTIN) — scanning shows assets of this type."),
    )
    region = models.CharField(max_length=64, blank=True, default="")
    configuration = models.CharField(max_length=255, blank=True, default="")
    management_kind = models.CharField(max_length=16, choices=MANAGEMENT_KIND_CHOICES, default=MANAGEMENT_LOCAL)
    library = models.ForeignKey(
        SpecificationLibrary,
        on_delete=models.PROTECT,
        related_name="asset_types",
        null=True,
        blank=True,
    )
    connector_identity = models.CharField(max_length=71, null=True, blank=True, db_index=True)
    library_definition_key = models.CharField(
        max_length=127,
        null=True,
        blank=True,
        validators=[_LIBRARY_DEFINITION_KEY_VALIDATOR],
    )
    lifecycle = models.CharField(max_length=16, choices=LIFECYCLE_CHOICES, default=LIFECYCLE_ACTIVE)
    deprecated_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.CharField(max_length=190, null=True, blank=True)

    eol_months = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("EOL (Months)"), help_text=_("Lifespan in months before EOL replacement")
    )
    custom_fieldsets = models.ManyToManyField(
        CustomFieldset,
        related_name="composed_asset_types",
        through="AssetTypeFieldset",
        blank=True,
    )
    # custom_field_data JSONField comes from CustomFieldDataMixin
    depreciation = models.ForeignKey(
        "assets.Depreciation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_types",
        verbose_name=_("Depreciation"),
    )

    category = models.ForeignKey(
        "assets.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_types",
        verbose_name=_("Category"),
        db_index=True,
    )
    asset_role = models.ForeignKey(
        "assets.AssetRole",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_types",
        verbose_name=_("Asset Role"),
        db_index=True,
    )
    image = models.ImageField(upload_to="asset_types/", blank=True, null=True, verbose_name=_("Model Image"))

    # Other
    description = models.TextField(blank=True, verbose_name=_("Description"))
    comments = models.TextField(blank=True, verbose_name=_("Comments"))
    tags = models.ManyToManyField("extras.Tag", related_name="asset_types", blank=True, verbose_name=_("Tags"))
    requestable = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Requestable"),
        help_text=_("Allow users to request assets of this type"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_assettype_slug_active"
            ),
            models.UniqueConstraint(
                fields=["library", "library_definition_key"],
                name="assettype_library_identity_uq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(library__isnull=True, library_definition_key__isnull=True)
                    | models.Q(library__isnull=False, library_definition_key__isnull=False)
                ),
                name="assettype_library_identity_ck",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(management_kind="library")
                        & models.Q(library__isnull=False)
                        & models.Q(library_definition_key__isnull=False)
                        & ~models.Q(library_definition_key="")
                    )
                    | (
                        models.Q(management_kind__in=["core", "local"])
                        & models.Q(library__isnull=True)
                        & models.Q(library_definition_key__isnull=True)
                    )
                ),
                name="assettype_library_mgmt_ck",
            ),
        ]
        verbose_name = _("Asset Type")
        verbose_name_plural = _("Asset Types")

    def __str__(self):
        return f"{self.manufacturer.name} {self.model}"

    def get_absolute_url(self):
        return reverse("assets:assettype_detail", kwargs={"pk": self.pk})

    def restore(self):
        """Restore an Asset Type and normalize the pre-cutover deleted state."""
        update_fields = ["deleted_at"]
        self.deleted_at = None
        if self.lifecycle == "deleted":
            self.lifecycle = self.LIFECYCLE_DEPRECATED
            update_fields.append("lifecycle")
        self.save(update_fields=update_fields)

    def clean(self):
        super().clean()
        errors = {}
        if self.library_definition_key:
            try:
                _LIBRARY_DEFINITION_KEY_VALIDATOR(self.library_definition_key)
            except ValidationError as exc:
                errors["library_definition_key"] = exc.messages
        if self.management_kind == self.MANAGEMENT_LIBRARY:
            if self.library_id is None:
                errors["library"] = _("Library-managed Asset Types require a Library.")
            if not self.library_definition_key:
                errors["library_definition_key"] = _("Library-managed Asset Types require a non-empty definition key.")
        elif self.library_id is not None or self.library_definition_key not in (None, ""):
            errors["management_kind"] = _("Only library-managed Asset Types may carry Library identity.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                .all_objects.filter(pk=self.pk)
                .values("library_id", "library_definition_key", "connector_identity")
                .first()
            )
            if previous and any(
                previous[field_name] != getattr(self, field_name)
                for field_name in ("library_id", "library_definition_key", "connector_identity")
            ):
                raise ValidationError(
                    {"library_definition_key": _("The library Asset Type identity is immutable after creation.")}
                )
        return super().save(*args, **kwargs)


class AssetTypeFieldset(BaseModel):
    asset_type = models.ForeignKey(AssetType, on_delete=models.CASCADE, related_name="fieldset_memberships")
    fieldset = models.ForeignKey(CustomFieldset, on_delete=models.PROTECT, related_name="asset_type_memberships")
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "fieldset__namespace", "fieldset__slug"]
        constraints = [
            models.UniqueConstraint(fields=["asset_type", "fieldset"], name="unique_assettype_fieldset"),
            models.UniqueConstraint(
                fields=["asset_type", "position"],
                name="unique_assettype_fieldset_position",
                deferrable=models.Deferrable.DEFERRED,
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=1000000),
                name="assettype_fieldset_position_range",
            ),
        ]

    def __str__(self):
        return f"{self.asset_type}: {self.fieldset}"


class Supplier(CustomFieldDataMixin, AutoSlugMixin, StandardModel, SoftDeleteMixin):
    # Tenant rows log to their tenant; group-scoped rows fan out to the group's
    # tenants; only global suppliers log system-wide changes.
    changelog_global = True
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    slug = models.SlugField(max_length=255, verbose_name=_("Slug"))
    website = models.URLField(max_length=500, blank=True, verbose_name=_("Website"))
    portal_url = models.URLField(
        blank=True,
        verbose_name=_("Admin Portal URL"),
        help_text=_("URL for the supplier's management/administration portal"),
    )
    account_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Account ID"),
        help_text=_("Optional customer account number with the supplier"),
    )
    address = models.TextField(blank=True, verbose_name=_("Address"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        db_index=True,
        help_text=_("Deactivate to hide from selection lists without deleting"),
    )
    tags = models.ManyToManyField("extras.Tag", related_name="suppliers", blank=True, verbose_name=_("Tags"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="suppliers",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this supplier. Null represents system-wide/global suppliers."),
    )
    tenant_group = models.ForeignKey(
        "organization.TenantGroup",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="suppliers",
        db_index=True,
        verbose_name=_("Tenant Group"),
        help_text=_("The tenant group owning this supplier."),
    )
    contacts = GenericRelation("organization.ContactAssignment")

    @property
    def primary_contact(self):
        assignment = self.contacts.filter(priority="primary").first() or self.contacts.first()
        return assignment.contact if assignment else None

    class Meta:
        ordering = ["name"]
        verbose_name = _("Supplier")
        verbose_name_plural = _("Suppliers")
        constraints = [
            models.CheckConstraint(
                check=models.Q(tenant__isnull=True) | models.Q(tenant_group__isnull=True),
                name="supplier_tenant_or_group",
            ),
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(tenant__isnull=False) & models.Q(deleted_at__isnull=True),
                name="unique_tenant_supplier_name",
            ),
            models.UniqueConstraint(
                fields=["tenant", "slug"],
                condition=models.Q(tenant__isnull=False) & models.Q(deleted_at__isnull=True),
                name="unique_tenant_supplier_slug",
            ),
            models.UniqueConstraint(
                fields=["tenant_group", "name"],
                condition=models.Q(tenant_group__isnull=False) & models.Q(deleted_at__isnull=True),
                name="unique_tenant_group_supplier_name",
            ),
            models.UniqueConstraint(
                fields=["tenant_group", "slug"],
                condition=models.Q(tenant_group__isnull=False) & models.Q(deleted_at__isnull=True),
                name="unique_tenant_group_supplier_slug",
            ),
            models.UniqueConstraint(
                fields=["name"],
                condition=(
                    models.Q(tenant__isnull=True)
                    & models.Q(tenant_group__isnull=True)
                    & models.Q(deleted_at__isnull=True)
                ),
                name="unique_global_supplier_name",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=(
                    models.Q(tenant__isnull=True)
                    & models.Q(tenant_group__isnull=True)
                    & models.Q(deleted_at__isnull=True)
                ),
                name="unique_global_supplier_slug",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("assets:supplier_detail", kwargs={"pk": self.pk})


class Category(AutoSlugMixin, StandardModel, SoftDeleteMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    soft_delete_preserve_references = True
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    slug = models.SlugField(max_length=255, verbose_name=_("Slug"))
    color = models.CharField(
        max_length=6, blank=True, verbose_name=_("Color"), help_text=_("RGB color in hexadecimal (e.g. 00ff00)")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    applies_to = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Applies To"),
        help_text=_("Applies to: {'asset': True, 'accessory': True, 'component': True}"),
    )
    audit_interval_months = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Audit Interval (Months)"),
        help_text=_(
            "How often assets in this category must be physically audited, in months. Leave blank for no required cadence."
        ),
    )
    tags = models.ManyToManyField("extras.Tag", related_name="categories", blank=True, verbose_name=_("Tags"))
    default_custom_fieldsets = models.ManyToManyField(
        CustomFieldset,
        related_name="default_for_categories",
        through="CategoryDefaultFieldset",
        blank=True,
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_category_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_category_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("assets:category_detail", kwargs={"pk": self.pk})


class CategoryDefaultFieldset(BaseModel):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="default_fieldset_memberships")
    fieldset = models.ForeignKey(CustomFieldset, on_delete=models.PROTECT, related_name="category_default_memberships")
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "fieldset__namespace", "fieldset__slug"]
        constraints = [
            models.UniqueConstraint(fields=["category", "fieldset"], name="unique_category_default_fieldset"),
            models.UniqueConstraint(
                fields=["category", "position"],
                name="unique_category_default_position",
                deferrable=models.Deferrable.DEFERRED,
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=1000000),
                name="category_default_position_range",
            ),
        ]

    def __str__(self):
        return f"{self.category}: {self.fieldset}"


class AssetTypeImageStage(BaseModel):
    """Internal durable staging authority for a pending Asset Type model image.

    An adapter ingests validated immutable bytes through the staging service
    before command entry and receives a bounded opaque ``stage_id``. The public
    create command consumes or discards that reference atomically inside the
    owner transaction; the stage row and its server-owned storage key are the
    durable authority (a signed path or cache key cannot record one-time
    consumption with the owner transaction). Stage metadata is immutable after
    ingestion so a previewed ID can never later refer to different bytes.
    """

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        CONSUMED = "consumed", _("Consumed")
        DISCARDED = "discarded", _("Discarded")

    stage_id = models.CharField(max_length=64, unique=True, db_index=True, verbose_name=_("Stage ID"))
    actor_id = models.PositiveBigIntegerField(db_index=True, verbose_name=_("Actor ID"))
    authentication_revision = models.CharField(max_length=64, verbose_name=_("Authentication Revision"))
    command_kind = models.CharField(max_length=64, verbose_name=_("Command Kind"))
    storage_key = models.CharField(max_length=255, unique=True, verbose_name=_("Storage Key"))
    byte_size = models.PositiveBigIntegerField(verbose_name=_("Byte Size"))
    content_digest = models.CharField(max_length=64, verbose_name=_("Content Digest"))
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.PENDING,
        db_index=True,
        verbose_name=_("State"),
    )
    expires_at = models.DateTimeField(db_index=True, verbose_name=_("Expires At"))
    consumed_asset_type = models.ForeignKey(
        "assets.AssetType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consumed_image_stages",
        verbose_name=_("Consumed Asset Type"),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Asset Type Image Stage")
        verbose_name_plural = _("Asset Type Image Stages")
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(state="consumed") & models.Q(consumed_asset_type__isnull=False))
                    | (~models.Q(state="consumed") & models.Q(consumed_asset_type__isnull=True))
                ),
                name="assettypeimagestage_consumed_link_ck",
            ),
        ]

    def __str__(self):
        return f"{self.stage_id} ({self.state})"
