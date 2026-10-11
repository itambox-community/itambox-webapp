import hashlib
import json
from contextlib import contextmanager

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import RegexValidator
from django.db import DatabaseError, connections, models, router, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.csv_utils import csv_safe, safe_csv_filename
from core.managers import (
    AllObjectsManager,
    ExplicitScopeAllObjectsManager,
    ExplicitScopeManager,
    ExplicitScopeSoftDeleteManager,
    Scope,
    SoftDeleteManager,
    TenantScopingQuerySet,
)
from core.mixins import BookmarkableMixin, SoftDeleteMixin
from core.models import BaseModel, ChangeLoggingMixin
from core.report_keys import unknown_column_keys
from core.validators import validate_external_url, validate_file_attachment, validate_image_attachment

from .canonicalization import canonicalize_release_document
from .definition_contract import validate_custom_field_definition_contract


def has_authored_conditions(conditions):
    """Return whether conditions contain authored expressions or an unexpected form."""
    if conditions is None:
        return False
    if not isinstance(conditions, dict):
        return True
    return bool(conditions.get("rules")) or "field" in conditions or "op" in conditions


class Tag(ChangeLoggingMixin, BaseModel, SoftDeleteMixin, BookmarkableMixin):
    changelog_global = True  # global reference data → changelog attributed to tenant=None
    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    color = models.CharField(max_length=6, blank=True, verbose_name=_("Color"))  # Store hex color without #
    description = models.CharField(max_length=200, blank=True, verbose_name=_("Description"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Tag")
        verbose_name_plural = _("Tags")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_tag_name_active"
            ),
            models.UniqueConstraint(
                fields=["slug"], condition=models.Q(deleted_at__isnull=True), name="unique_tag_slug_active"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("extras:tag_detail", kwargs={"pk": self.pk})


class Dashboard(models.Model):
    # Dashboards are personal user objects — they are NOT tenant-scoped rows.
    # The `tenant` field only narrows widget data; it does not gate access to
    # the dashboard itself.  Using the plain manager ensures that
    # filter_by_tenant()'s fail-close (→ .none() when no tenant context) does
    # not prevent users from seeing their own dashboards.
    objects = models.Manager()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dashboards", verbose_name=_("User")
    )
    name = models.CharField(max_length=100, default="Main Dashboard", verbose_name=_("Name"))
    is_default = models.BooleanField(default=False, verbose_name=_("Is Default"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="dashboards",
        verbose_name=_("Tenant"),
        help_text=_("Scope all widgets on this dashboard to this specific tenant context."),
    )
    layout = models.JSONField(
        default=list, blank=True, verbose_name=_("Layout"), help_text=_("Ordered list of widget config dicts")
    )
    created = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "name"]
        verbose_name = _("Dashboard")
        verbose_name_plural = _("Dashboards")

    def __str__(self):
        if self.tenant:
            return f"{self.name} ({self.tenant.name}) for {self.user.username}"
        return f"{self.name} for {self.user.username}"

    def add_widget(self, widget_class, title=None, **config):
        """Add a new widget to the end of the layout."""
        entry = {
            "widget": widget_class,
            "title": title,
            "visible": True,
            "w": 4,
            "h": 2,
            "config": {},
            **config,
        }
        self.layout.append(entry)
        self.save(update_fields=["layout"])

    def remove_widget(self, index):
        """Remove a widget by its index in the layout list."""
        if 0 <= index < len(self.layout):
            self.layout.pop(index)
            self.save(update_fields=["layout"])

    def update_widget(self, index, **kwargs):
        """Update widget config at the given index."""
        if 0 <= index < len(self.layout):
            self.layout[index].update(kwargs)
            self.layout = list(self.layout)
            self.save(update_fields=["layout"])

    def move_widget(self, from_index, to_index):
        """Reorder a widget within the layout."""
        layout = self.layout
        if 0 <= from_index < len(layout) and 0 <= to_index < len(layout):
            widget = layout.pop(from_index)
            layout.insert(to_index, widget)
            self.save(update_fields=["layout"])


SPECIFICATION_LIBRARY_NAMESPACE_RE = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
_SPECIFICATION_LIBRARY_DIGEST_RE = r"^sha256:[0-9a-f]{64}$"
_SPECIFICATION_LIBRARY_RECONCILE_SETTING = "itambox.specification_library_reconcile"

_SPECIFICATION_LIBRARY_NAMESPACE_VALIDATOR = RegexValidator(SPECIFICATION_LIBRARY_NAMESPACE_RE)
_SPECIFICATION_LIBRARY_DIGEST_VALIDATOR = RegexValidator(_SPECIFICATION_LIBRARY_DIGEST_RE)


def normalize_release_document(source_document):
    """Return RFC 8785 JCS bytes for an already domain-normalized document.

    This function performs byte canonicalization only: JCS preserves array
    order and does not sort or otherwise normalize source-domain collections.
    """
    if not isinstance(source_document, dict):
        raise ValidationError({"source_document": _("A release document must be a JSON object.")})
    try:
        return canonicalize_release_document(source_document)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"source_document": _("The release document is not valid normalized JSON.")}) from exc


def release_document_digest(source_document):
    """Return the semantic SHA-256 digest for a normalized release document."""
    return "sha256:" + hashlib.sha256(normalize_release_document(source_document)).hexdigest()


@contextmanager
def _specification_library_reconcile_opt_in(using):
    """Permit one accepted-release pointer update in the current transaction."""
    database = connections[using]
    with database.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [_SPECIFICATION_LIBRARY_RECONCILE_SETTING])
        previous = cursor.fetchone()[0] or None
        cursor.execute("SELECT set_config(%s, 'on', true)", [_SPECIFICATION_LIBRARY_RECONCILE_SETTING])
    try:
        yield
    finally:
        if not database.needs_rollback:
            try:
                with database.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config(%s, %s, true)",
                        [_SPECIFICATION_LIBRARY_RECONCILE_SETTING, previous if previous is not None else "off"],
                    )
            except DatabaseError:
                # broad except: cleanup-reraise: the enclosing transaction already failed
                pass


class SpecificationLibrary(ChangeLoggingMixin, BaseModel):
    """Immutable publisher identity and pointer to its accepted release."""

    changelog_global = True
    objects = models.Manager()

    namespace = models.CharField(
        max_length=62,
        unique=True,
        validators=[_SPECIFICATION_LIBRARY_NAMESPACE_VALIDATOR],
    )
    label = models.CharField(max_length=200, blank=True, default="")
    installed_at = models.DateTimeField(default=timezone.now)
    accepted_release = models.ForeignKey(
        "extras.SpecificationLibraryRelease",
        on_delete=models.PROTECT,
        related_name="accepted_by_libraries",
        null=True,
        blank=True,
    )

    immutable_fields = ("namespace",)

    class Meta:
        ordering = ["namespace"]
        permissions = [("manage_specification_library", "Can manage specification libraries")]
        constraints = [
            models.UniqueConstraint(fields=["namespace"], name="unique_specificationlibrary_namespace"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        try:
            _SPECIFICATION_LIBRARY_NAMESPACE_VALIDATOR(self.namespace)
        except ValidationError as exc:
            errors["namespace"] = exc.messages
        if self.accepted_release_id and self.accepted_release is not None:
            if self.accepted_release.library_id != self.pk:
                errors["accepted_release"] = _("The accepted Release must belong to this Library.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                .objects.using(self._state.db)
                .filter(pk=self.pk)
                .values("namespace", "accepted_release_id")
                .first()
            )
            if previous:
                if previous["namespace"] != self.namespace:
                    raise ValidationError({"namespace": _("A Library namespace is immutable after creation.")})
                if previous["accepted_release_id"] != self.accepted_release_id and not getattr(
                    self, "_accepted_release_write", False
                ):
                    raise ValidationError(
                        {"accepted_release": _("The accepted Release may only change through the library command.")}
                    )
        elif self.accepted_release_id and not getattr(self, "_accepted_release_write", False):
            raise ValidationError({"accepted_release": _("Create a Library before accepting a Release.")})
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(_("Specification Library identities are permanent."), {self})

    def accept_release(self, release, *, using=None):
        """Advance the accepted pointer to a same-library, non-older Release."""
        if self.pk is None:
            raise ValidationError({"pk": _("The Library must be saved before accepting a Release.")})
        using = using or self._state.db or router.db_for_write(type(self), instance=self)
        release_id = getattr(release, "pk", release)
        with transaction.atomic(using=using):
            locked = type(self).objects.using(using).select_for_update().get(pk=self.pk)
            release_model = locked._meta.get_field("accepted_release").remote_field.model
            candidate = release_model._base_manager.using(using).get(pk=release_id)
            if candidate.library_id != locked.pk:
                raise ValidationError({"accepted_release": _("The Release belongs to another Library.")})
            current = locked.accepted_release_id
            if current:
                current_release = release_model._base_manager.using(using).get(pk=current)
                if candidate.sequence < current_release.sequence:
                    raise ValidationError({"accepted_release": _("An older Release cannot become accepted.")})
            locked.accepted_release_id = candidate.pk
            locked._accepted_release_write = True
            try:
                with _specification_library_reconcile_opt_in(using):
                    locked.save(using=using, update_fields=["accepted_release", "updated_at"])
            finally:
                locked._accepted_release_write = False
            self.accepted_release_id = candidate.pk
            self.updated_at = locked.updated_at
        return self

    def __str__(self):
        return self.namespace


class SpecificationLibraryRelease(ChangeLoggingMixin, BaseModel):
    """Immutable normalized source snapshot retained for one library sequence."""

    changelog_global = True
    objects = models.Manager()

    library = models.ForeignKey(
        SpecificationLibrary,
        on_delete=models.PROTECT,
        related_name="releases",
    )
    sequence = models.PositiveBigIntegerField()
    semantic_digest = models.CharField(
        max_length=71,
        validators=[_SPECIFICATION_LIBRARY_DIGEST_VALIDATOR],
    )
    source_document = models.JSONField()
    accepted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["library", "-sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["library", "sequence"],
                name="unique_specificationlibraryrelease_library_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1),
                name="specificationlibraryrelease_sequence_positive",
            ),
        ]
        indexes = [models.Index(fields=["library", "-sequence"], name="spec_library_release_lookup")]

    def _validate_release_values(self):
        errors = {}
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            errors["sequence"] = _("Release sequence must be a positive integer.")
        try:
            _SPECIFICATION_LIBRARY_DIGEST_VALIDATOR(self.semantic_digest)
        except ValidationError as exc:
            errors["semantic_digest"] = exc.messages
        try:
            expected_digest = release_document_digest(self.source_document)
        except ValidationError as exc:
            errors.update(exc.message_dict)
        else:
            if self.semantic_digest != expected_digest:
                errors["semantic_digest"] = _("The digest does not match the normalized release document.")
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self._validate_release_values()
        if self.library_id and self.library is not None and self.library_id != self.library.pk:
            raise ValidationError({"library": _("The Release library identity is inconsistent.")})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError({"__all__": _("Release rows are immutable after insertion.")})
        self._validate_release_values()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(_("Release rows are immutable and retained."), {self})


class SpecificationLibraryLegacyProvenance(BaseModel):
    """Immutable transition evidence; never an active reconciliation source."""

    OWNER_LIBRARY = "library"
    OWNER_ASSET_TYPE = "asset_type"
    OWNER_CUSTOM_FIELD = "custom_field"
    OWNER_CUSTOM_FIELDSET = "custom_fieldset"
    OWNER_CHOICE_SET = "choice_set"
    OWNER_CHOICE = "choice"
    OWNER_KIND_CHOICES = [
        (OWNER_LIBRARY, _("Library")),
        (OWNER_ASSET_TYPE, _("Asset Type")),
        (OWNER_CUSTOM_FIELD, _("Custom Field")),
        (OWNER_CUSTOM_FIELDSET, _("Custom Fieldset")),
        (OWNER_CHOICE_SET, _("Choice Set")),
        (OWNER_CHOICE, _("Choice")),
    ]
    DISPOSITION_UNINITIALIZED = "uninitialized"
    DISPOSITION_UNRECONCILED = "unreconciled"
    DISPOSITION_CHOICES = [
        (DISPOSITION_UNINITIALIZED, _("Uninitialized")),
        (DISPOSITION_UNRECONCILED, _("Unreconciled")),
    ]

    objects = models.Manager()
    library = models.ForeignKey(
        SpecificationLibrary,
        on_delete=models.PROTECT,
        related_name="legacy_provenance",
        null=True,
        blank=True,
    )
    owner_kind = models.CharField(max_length=16, choices=OWNER_KIND_CHOICES)
    owner_id = models.PositiveBigIntegerField()
    owner_namespace = models.CharField(max_length=62)
    legacy_release = models.CharField(max_length=64, null=True, blank=True)
    legacy_source_checksum = models.CharField(max_length=71, null=True, blank=True)
    legacy_managed_paths = models.JSONField(default=dict)
    legacy_last_reconciled_at = models.DateTimeField(null=True, blank=True)
    disposition = models.CharField(max_length=16, choices=DISPOSITION_CHOICES)
    captured_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_kind", "owner_id"],
                name="unique_specificationlibrarylegacyprovenance_owner",
            ),
        ]
        indexes = [
            models.Index(fields=["library", "owner_kind"], name="spec_legacy_library_kind"),
            models.Index(fields=["owner_kind", "owner_id"], name="spec_legacy_owner_lookup"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError({"__all__": _("Legacy provenance rows are immutable after insertion.")})
        if not isinstance(self.legacy_managed_paths, dict):
            raise ValidationError({"legacy_managed_paths": _("Legacy managed paths must be a JSON object.")})
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(_("Legacy provenance is immutable transition evidence."), {self})


class _DefinitionIdentityMixin(models.Model):
    """Lifecycle, immutable identity and permanent-row behavior for definitions."""

    LIFECYCLE_ACTIVE = "active"
    LIFECYCLE_DEPRECATED = "deprecated"
    LIFECYCLE_CHOICES = [
        (LIFECYCLE_ACTIVE, _("Active")),
        (LIFECYCLE_DEPRECATED, _("Deprecated")),
    ]

    version = models.PositiveIntegerField(default=1)
    lifecycle = models.CharField(max_length=16, choices=LIFECYCLE_CHOICES, default=LIFECYCLE_ACTIVE)
    deprecated_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = ()

    class Meta:
        abstract = True

    def _validate_immutable_fields(self):
        if not self.pk:
            return
        previous = type(self).objects.filter(pk=self.pk).values(*self.immutable_fields).first()
        if previous is None:
            return
        changed = {
            field: _("This value is immutable after creation.")
            for field in self.immutable_fields
            if previous[field] != getattr(self, field)
        }
        if changed:
            raise ValidationError(changed)

    def delete(self, *args, **kwargs):
        """Reusable definition identities are retired, never deleted."""
        raise ProtectedError(
            f"{self._meta.verbose_name} identities are permanent; deprecate the row instead.",
            {self},
        )

    def save(self, *args, **kwargs):
        self._validate_immutable_fields()
        return super().save(*args, **kwargs)


class _ManagedDefinitionMixin(_DefinitionIdentityMixin):
    MANAGEMENT_CORE = "core"
    MANAGEMENT_LIBRARY = "library"
    MANAGEMENT_LOCAL = "local"
    MANAGEMENT_KIND_CHOICES = [
        (MANAGEMENT_CORE, _("Core")),
        (MANAGEMENT_LIBRARY, _("Library")),
        (MANAGEMENT_LOCAL, _("Local")),
    ]

    management_kind = models.CharField(max_length=16, choices=MANAGEMENT_KIND_CHOICES, default=MANAGEMENT_LOCAL)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.management_kind == self.MANAGEMENT_LIBRARY and self.library_id is None:
            raise ValidationError({"library": _("Library-managed definitions require a Library.")})
        if self.management_kind in {self.MANAGEMENT_CORE, self.MANAGEMENT_LOCAL} and self.library_id is not None:
            raise ValidationError({"library": _("Core and local definitions cannot carry a Library link.")})


class CustomFieldChoiceSet(_ManagedDefinitionMixin, ChangeLoggingMixin, BaseModel):
    changelog_global = True
    objects = models.Manager()

    namespace = models.CharField(
        max_length=62,
        validators=[_SPECIFICATION_LIBRARY_NAMESPACE_VALIDATOR],
    )
    slug = models.CharField(
        max_length=127,
        validators=[RegexValidator(r"^[a-z0-9][a-z0-9._-]{0,126}$")],
    )
    label = models.CharField(max_length=200)
    library = models.ForeignKey(
        SpecificationLibrary,
        on_delete=models.PROTECT,
        related_name="choice_sets",
        null=True,
        blank=True,
    )
    connector_identity = models.CharField(max_length=71, null=True, blank=True, db_index=True)
    replaced_by = models.CharField(max_length=190, null=True, blank=True)

    immutable_fields = ("namespace", "slug", "library_id", "connector_identity")

    class Meta:
        ordering = ["namespace", "slug"]
        constraints = [
            models.UniqueConstraint(fields=["namespace", "slug"], name="unique_customfieldchoiceset_identity"),
            models.CheckConstraint(
                condition=(
                    (models.Q(management_kind="library") & models.Q(library__isnull=False))
                    | (models.Q(management_kind__in=["core", "local"]) & models.Q(library__isnull=True))
                ),
                name="customfieldchoiceset_library_management_coherence",
            ),
        ]

    def __str__(self):
        return f"{self.namespace}/{self.slug}"


class CustomFieldChoice(_DefinitionIdentityMixin, ChangeLoggingMixin, BaseModel):
    changelog_global = True
    objects = models.Manager()

    choice_set = models.ForeignKey(CustomFieldChoiceSet, on_delete=models.CASCADE, related_name="choices")
    key = models.CharField(
        max_length=63,
        validators=[RegexValidator(r"^[a-z0-9][a-z0-9_]{0,62}$")],
    )
    label = models.CharField(max_length=200)
    position = models.PositiveIntegerField()
    replaced_by = models.CharField(max_length=254, null=True, blank=True)

    immutable_fields = ("choice_set_id", "key")

    class Meta:
        ordering = ["position", "key"]
        constraints = [
            models.UniqueConstraint(fields=["choice_set", "key"], name="unique_customfieldchoice_key"),
            models.UniqueConstraint(
                fields=["choice_set", "position"],
                name="unique_customfieldchoice_position",
                deferrable=models.Deferrable.DEFERRED,
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=1000000),
                name="customfieldchoice_position_range",
            ),
        ]

    def __str__(self):
        return self.label


class CustomField(_ManagedDefinitionMixin, ChangeLoggingMixin, BaseModel):
    changelog_global = True  # global config → changelog attributed to tenant=None
    objects = models.Manager()
    FIELD_TYPE_TEXT = "text"
    FIELD_TYPE_INTEGER = "integer"
    FIELD_TYPE_DECIMAL = "decimal"
    FIELD_TYPE_DATE = "date"
    FIELD_TYPE_BOOLEAN = "boolean"
    FIELD_TYPE_SINGLE_SELECT = "single-select"
    FIELD_TYPE_MULTI_SELECT = "multi-select"
    FIELD_TYPE_CHOICES = [
        (FIELD_TYPE_TEXT, _("Text")),
        (FIELD_TYPE_INTEGER, _("Integer")),
        (FIELD_TYPE_DECIMAL, _("Decimal")),
        (FIELD_TYPE_DATE, _("Date")),
        (FIELD_TYPE_BOOLEAN, _("Boolean")),
        (FIELD_TYPE_SINGLE_SELECT, _("Single select")),
        (FIELD_TYPE_MULTI_SELECT, _("Multi select")),
    ]

    ACTIVATION_COMPOSED = "composed"
    ACTIVATION_GLOBAL = "global"
    ACTIVATION_CHOICES = [
        (ACTIVATION_COMPOSED, _("Composed")),
        (ACTIVATION_GLOBAL, _("Global")),
    ]

    name = models.CharField(
        max_length=64,
        validators=[RegexValidator(r"^[a-z][a-z0-9_]{0,63}$")],
        verbose_name=_("Field Name"),
        help_text=_("Stable JSON key (e.g. sim_card_number)"),
    )
    namespace = models.CharField(
        max_length=62,
        default="local",
        validators=[_SPECIFICATION_LIBRARY_NAMESPACE_VALIDATOR],
    )
    library = models.ForeignKey(
        SpecificationLibrary,
        on_delete=models.PROTECT,
        related_name="fields",
        null=True,
        blank=True,
    )
    connector_identity = models.CharField(max_length=71, null=True, blank=True, db_index=True)
    label = models.CharField(max_length=200, db_index=True, verbose_name=_("Display Label"))
    help_text = models.TextField(max_length=4096, blank=True, default="")
    field_type = models.CharField(
        max_length=16, choices=FIELD_TYPE_CHOICES, default=FIELD_TYPE_TEXT, db_index=True, verbose_name=_("Field Type")
    )
    activation = models.CharField(
        max_length=16,
        choices=ACTIVATION_CHOICES,
        db_index=True,
        verbose_name=_("Activation"),
    )
    quantity_kind = models.CharField(max_length=32, null=True, blank=True)
    canonical_unit = models.CharField(max_length=16, null=True, blank=True)
    minimum_value = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    maximum_value = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    regex = models.CharField(max_length=256, null=True, blank=True)
    decimal_scale = models.PositiveSmallIntegerField(null=True, blank=True)
    max_values = models.PositiveSmallIntegerField(null=True, blank=True)
    text_max_length = models.PositiveSmallIntegerField(null=True, blank=True)
    validation_rule = models.CharField(max_length=32, null=True, blank=True)
    required = models.BooleanField(default=False, db_index=True, verbose_name=_("Required"))
    nullable = models.BooleanField(default=False)
    mappings = models.JSONField(default=list, blank=True)
    choice_set = models.ForeignKey(
        CustomFieldChoiceSet,
        on_delete=models.PROTECT,
        related_name="fields",
        null=True,
        blank=True,
    )
    replaced_by = models.CharField(max_length=190, null=True, blank=True)
    object_types = models.ManyToManyField(
        "contenttypes.ContentType",
        related_name="custom_fields",
        blank=True,
        verbose_name=_("Object Types"),
        help_text=_(
            "The model(s) this field applies to. A field applying to Asset Type "
            "describes a hardware specification; one applying to Asset describes "
            "a per-device detail."
        ),
    )

    class Meta:
        ordering = ["label"]
        verbose_name = _("Custom Field")
        verbose_name_plural = _("Custom Fields")
        constraints = [
            models.UniqueConstraint(fields=["name"], name="unique_customfield_name"),
            models.CheckConstraint(
                condition=models.Q(activation__in=["composed", "global"]),
                name="customfield_activation_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(management_kind="library") & models.Q(library__isnull=False))
                    | (models.Q(management_kind__in=["core", "local"]) & models.Q(library__isnull=True))
                ),
                name="customfield_library_management_coherence",
            ),
            models.CheckConstraint(
                condition=models.Q(minimum_value__isnull=True)
                | models.Q(maximum_value__isnull=True)
                | models.Q(minimum_value__lte=models.F("maximum_value")),
                name="customfield_minimum_lte_maximum",
            ),
            models.CheckConstraint(
                condition=models.Q(decimal_scale__isnull=True) | models.Q(decimal_scale__lte=6),
                name="customfield_decimal_scale_range",
            ),
            models.CheckConstraint(
                condition=models.Q(max_values__isnull=True) | models.Q(max_values__gte=1, max_values__lte=64),
                name="customfield_max_values_range",
            ),
            models.CheckConstraint(
                condition=models.Q(text_max_length__isnull=True)
                | models.Q(text_max_length__gte=1, text_max_length__lte=4096),
                name="customfield_text_length_range",
            ),
        ]

    immutable_fields = (
        "name",
        "namespace",
        "field_type",
        "quantity_kind",
        "canonical_unit",
        "minimum_value",
        "maximum_value",
        "regex",
        "decimal_scale",
        "max_values",
        "text_max_length",
        "validation_rule",
        "nullable",
        "choice_set_id",
        "library_id",
        "connector_identity",
    )

    def __str__(self):
        return f"{self.label} ({self.get_field_type_display()})"

    def clean(self):
        super().clean()
        object_types = self.object_types.all() if self.pk else None
        self.validate_definition_contract(object_types=object_types)
        if self.activation == self.ACTIVATION_GLOBAL and self.pk and self.fieldset_memberships.exists():
            raise ValidationError({"activation": _("A field with memberships cannot be global.")})

    def validate_definition_contract(self, *, object_types=None):
        validate_custom_field_definition_contract(
            field_type=self.field_type,
            activation=self.activation,
            quantity_kind=self.quantity_kind,
            canonical_unit=self.canonical_unit,
            minimum_value=self.minimum_value,
            maximum_value=self.maximum_value,
            regex=self.regex,
            decimal_scale=self.decimal_scale,
            max_values=self.max_values,
            text_max_length=self.text_max_length,
            validation_rule=self.validation_rule,
            mappings=self.mappings,
            choice_set=self.choice_set,
            object_types=object_types,
            management_kind=self.management_kind,
            lifecycle=self.lifecycle,
            required=self.required,
            nullable=self.nullable,
            name=self.name,
            namespace=self.namespace,
        )

    def get_absolute_url(self):
        return reverse("extras:customfield_detail", kwargs={"pk": self.pk})

    @property
    def is_asset_type_spec(self):
        """True when this field applies to AssetType (a hardware specification).
        Template-friendly replacement for the retired model_level flag."""
        return self.object_types.filter(app_label="assets", model="assettype").exists()


class CustomFieldset(_ManagedDefinitionMixin, ChangeLoggingMixin, BaseModel):
    changelog_global = True  # global config → changelog attributed to tenant=None
    objects = models.Manager()
    fields = models.ManyToManyField(
        CustomField,
        related_name="fieldsets",
        through="CustomFieldsetField",
        blank=True,
        verbose_name=_("Custom Fields"),
    )
    namespace = models.CharField(
        max_length=62,
        default="local",
        validators=[_SPECIFICATION_LIBRARY_NAMESPACE_VALIDATOR],
    )
    library = models.ForeignKey(
        SpecificationLibrary,
        on_delete=models.PROTECT,
        related_name="fieldsets",
        null=True,
        blank=True,
    )
    connector_identity = models.CharField(max_length=71, null=True, blank=True, db_index=True)
    slug = models.CharField(
        max_length=127,
        validators=[RegexValidator(r"^[a-z0-9][a-z0-9._-]{0,126}$")],
    )
    label = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(max_length=4096, blank=True, default="")
    replaced_by = models.CharField(max_length=190, null=True, blank=True)

    immutable_fields = ("namespace", "slug", "library_id", "connector_identity")

    class Meta:
        ordering = ["namespace", "slug"]
        verbose_name = _("Custom Fieldset")
        verbose_name_plural = _("Custom Fieldsets")
        constraints = [
            models.UniqueConstraint(fields=["namespace", "slug"], name="unique_customfieldset_identity"),
            models.CheckConstraint(
                condition=(
                    (models.Q(management_kind="library") & models.Q(library__isnull=False))
                    | (models.Q(management_kind__in=["core", "local"]) & models.Q(library__isnull=True))
                ),
                name="customfieldset_library_management_coherence",
            ),
        ]

    def __str__(self):
        return self.label

    def get_absolute_url(self):
        return reverse("extras:customfieldset_detail", kwargs={"pk": self.pk})


class CustomFieldsetField(BaseModel):
    fieldset = models.ForeignKey(CustomFieldset, on_delete=models.CASCADE, related_name="field_memberships")
    custom_field = models.ForeignKey(CustomField, on_delete=models.PROTECT, related_name="fieldset_memberships")
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position", "custom_field__name"]
        constraints = [
            models.UniqueConstraint(fields=["fieldset", "custom_field"], name="unique_customfieldset_field"),
            models.UniqueConstraint(
                fields=["fieldset", "position"],
                name="unique_customfieldset_position",
                deferrable=models.Deferrable.DEFERRED,
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=1000000),
                name="customfieldset_position_range",
            ),
        ]

    def clean(self):
        super().clean()
        if self.custom_field_id and self.custom_field.activation == CustomField.ACTIVATION_GLOBAL:
            raise ValidationError({"custom_field": _("Global fields cannot join a fieldset.")})

    def __str__(self):
        return f"{self.fieldset}: {self.custom_field}"


# Machine-generated event-bus row. Intentionally NOT change-logged: it is
# append-only, and logging it would write a second ObjectChange for every
# tracked change (plus one more for the processed-flag flip), doubling volume.
class Event(BaseModel):
    ACTION_CREATE = "create"
    ACTION_UPDATE = "update"
    ACTION_DELETE = "delete"
    ACTION_RESTORE = "restore"
    ACTION_CHECKOUT = "checkout"
    ACTION_CHECKIN = "checkin"

    ACTION_CHOICES = [
        (ACTION_CREATE, _("Create")),
        (ACTION_UPDATE, _("Update")),
        (ACTION_DELETE, _("Delete")),
        # Emitted on a soft-delete restore (set -> None). A distinct, subscribable
        # action so EventRules can target restores and the value is a declared choice.
        (ACTION_RESTORE, _("Restore")),
        (ACTION_CHECKOUT, _("Checkout")),
        (ACTION_CHECKIN, _("Checkin")),
    ]

    model = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="events")
    object_id = models.PositiveBigIntegerField(db_index=True)
    content_object = GenericForeignKey("model", "object_id")
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True, verbose_name=_("Action"))
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Data"))
    processed = models.BooleanField(default=False, db_index=True, verbose_name=_("Processed"))

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = _("Event")
        verbose_name_plural = _("Events")
        indexes = [
            models.Index(fields=["model", "object_id"], name="core_event_model_i_6d722d_idx"),
            models.Index(fields=["processed", "timestamp"], name="core_event_process_17ef77_idx"),
        ]

    def __str__(self):
        return f"Event {self.get_action_display()} on {self.content_object}"


class EventRule(ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    ACTION_WEBHOOK = "webhook"
    ACTION_NOTIFICATION = "notification"

    ACTION_TYPE_CHOICES = [
        (ACTION_WEBHOOK, _("Webhook")),
        (ACTION_NOTIFICATION, _("Notification")),
    ]

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    model = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="event_rules", verbose_name=_("Model")
    )
    events = models.JSONField(
        default=list, verbose_name=_("Events"), help_text=_("List of event action types, e.g. ['create', 'update']")
    )
    conditions = models.JSONField(
        default=dict, blank=True, verbose_name=_("Conditions"), help_text=_("Optional conditions for rule matching")
    )
    action_type = models.CharField(max_length=20, choices=ACTION_TYPE_CHOICES, verbose_name=_("Action Type"))
    webhook = models.ForeignKey(
        "WebhookEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="event_rules",
        verbose_name=_("Webhook"),
        help_text=_(
            "Endpoint to call when the action type is Webhook. Takes precedence over any 'url' in action_config."
        ),
    )
    action_config = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Action Config"),
        help_text=_("Advanced/optional JSON config (notification body, header overrides, etc.)"),
    )
    enabled = models.BooleanField(default=True, verbose_name=_("Enabled"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="event_rules",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this rule. Null represents system-wide rules."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Event Rule")
        verbose_name_plural = _("Event Rules")

    def __str__(self):
        return self.name

    @property
    def conditions_withdrawn(self):
        return has_authored_conditions(self.conditions)

    @property
    def conditions_json(self):
        return json.dumps(self.conditions, indent=2)

    def get_absolute_url(self):
        return reverse("extras:eventrule_detail", kwargs={"pk": self.pk})

    def clean(self):
        super().clean()
        if self.action_type == self.ACTION_WEBHOOK and self.webhook_id and self.tenant_id is not None:
            endpoint_tenant_id = self.webhook.tenant_id
            if endpoint_tenant_id is not None and endpoint_tenant_id != self.tenant_id:
                raise ValidationError(
                    {"webhook": _("Webhook endpoint must belong to the same tenant as the rule, or be system-wide.")}
                )


class WebhookEndpoint(ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True
    # Keep the (encrypted) HMAC secret and the headers — which may carry
    # Authorization tokens — out of the changelog JSON.
    _change_logging_excluded_fields = ["updated_at", "secret", "headers"]

    HTTP_GET = "GET"
    HTTP_POST = "POST"
    HTTP_PUT = "PUT"
    HTTP_PATCH = "PATCH"
    METHOD_CHOICES = [
        (HTTP_GET, "GET"),
        (HTTP_POST, "POST"),
        (HTTP_PUT, "PUT"),
        (HTTP_PATCH, "PATCH"),
    ]

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    url = models.URLField(max_length=2000, verbose_name=_("URL"))
    http_method = models.CharField(
        max_length=10, choices=METHOD_CHOICES, default=HTTP_POST, verbose_name=_("HTTP Method")
    )
    headers = models.JSONField(default=dict, blank=True, verbose_name=_("Headers"))
    secret = models.CharField(
        max_length=255, blank=True, verbose_name=_("Secret"), help_text=_("Shared secret for HMAC payload signing")
    )
    enabled = models.BooleanField(default=True, verbose_name=_("Enabled"))
    retry_count = models.PositiveSmallIntegerField(
        default=3, verbose_name=_("Retry Count"), help_text=_("Max retry attempts on failure")
    )
    retry_backoff = models.PositiveSmallIntegerField(
        default=60, verbose_name=_("Retry Backoff"), help_text=_("Backoff in seconds between retries")
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="webhook_endpoints",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this endpoint. Null represents system-wide endpoints."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Webhook Endpoint")
        verbose_name_plural = _("Webhook Endpoints")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_webhookendpoint_tenant_name_active",
            )
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("extras:webhookendpoint_detail", kwargs={"pk": self.pk})

    def clean(self):
        super().clean()
        if self.url:
            # SSRF guard at the WRITE boundary (forms/admin/full_clean), not only at dispatch:
            # reject loopback/link-local/private/metadata URLs before they are persisted.
            validate_external_url(self.url)

    def save(self, *args, **kwargs):
        if self.secret and not self.secret.startswith("enc$"):
            from core.crypto import encrypt_string

            self.secret = encrypt_string(self.secret)
        super().save(*args, **kwargs)

    @property
    def secret_decrypted(self) -> str:
        if not self.secret:
            return ""
        if self.secret.startswith("enc$"):
            from core.crypto import decrypt_string

            return decrypt_string(self.secret)
        return self.secret


class WebhookDeliveryQuerySet(TenantScopingQuerySet):
    """Tenant-scoped delivery history with explicit platform visibility."""

    def visible_to(self, user):
        """Return deliveries visible to ``user``, including global rows only for platform users."""

        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        if getattr(user, "is_superuser", False):
            # Platform-wide read: tenant=None operational history must stay visible
            # whatever tenant the session selected, so only the explicit platform
            # principal bypasses the scope. A tenant role grant never does.
            return self.model._base_manager.all()
        # Tenant role grants stay bounded to the authorized scope; global rows
        # (tenant=None) are platform history and never reach them.
        return self.for_scope(Scope.current()).filter(tenant__isnull=False)


class WebhookDeliveryManager(models.Manager.from_queryset(WebhookDeliveryQuerySet)):
    """Plain manager exposing the delivery visibility helper; readers scope explicitly."""

    def visible_to(self, user):
        return self.get_queryset().visible_to(user)


class WebhookDelivery(BaseModel):
    objects = WebhookDeliveryManager()
    all_objects = ExplicitScopeAllObjectsManager()

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_DEAD = "dead"
    STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_SUCCESS, _("Success")),
        (STATUS_FAILED, _("Failed")),
        (STATUS_DEAD, _("Dead")),
    ]

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_deliveries",
        db_index=True,
        verbose_name=_("Tenant"),
    )
    endpoint = models.ForeignKey(
        "WebhookEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
        verbose_name=_("Endpoint"),
    )
    event = models.ForeignKey(
        "Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
        verbose_name=_("Event"),
    )
    event_rule_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Event Rule ID"),
        help_text=_("Immutable identifier of the event rule execution that created this delivery."),
    )
    payload_timestamp = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Payload Timestamp"),
        help_text=_("Immutable timestamp used in every attempt of this delivery."),
    )
    target_url = models.URLField(max_length=2000, blank=True, verbose_name=_("Target URL"))
    target_http_method = models.CharField(
        max_length=10,
        choices=WebhookEndpoint.METHOD_CHOICES,
        default=WebhookEndpoint.HTTP_POST,
        verbose_name=_("Target HTTP Method"),
    )
    target_headers = models.JSONField(default=dict, blank=True, verbose_name=_("Target Headers"))
    target_secret = models.CharField(max_length=1024, blank=True, verbose_name=_("Target Secret"))
    target_enabled = models.BooleanField(default=True, verbose_name=_("Target Enabled"))
    target_tenant_id = models.PositiveBigIntegerField(null=True, blank=True, verbose_name=_("Target Tenant ID"))
    target_retry_count = models.PositiveSmallIntegerField(default=3, verbose_name=_("Target Retry Count"))
    target_retry_backoff = models.PositiveSmallIntegerField(default=60, verbose_name=_("Target Retry Backoff"))
    delivery_id = models.CharField(max_length=36, unique=True, db_index=True, verbose_name=_("Delivery ID"))
    attempt = models.PositiveIntegerField(default=1, verbose_name=_("Attempt"))
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        verbose_name=_("Status"),
    )
    response_code = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_("Response Code"))
    error_class = models.CharField(max_length=64, blank=True, verbose_name=_("Error Class"))
    error_message = models.TextField(blank=True, verbose_name=_("Error Message"))
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("Next Retry At"))
    test_send = models.BooleanField(default=False, db_index=True, verbose_name=_("Test Send"))
    redelivered_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redeliveries",
        verbose_name=_("Redelivered From"),
    )
    redelivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Redelivered By"),
    )
    redelivered_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Redelivered At"))
    attempted_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Attempted At"))
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Completed At"))
    claim_token = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Claim Token"))
    claim_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        editable=False,
        verbose_name=_("Claim Expires At"),
    )
    dispatch_stale_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        editable=False,
        verbose_name=_("Dispatch Stale At"),
        help_text=_("Recovery coordinator lease for a queued durable delivery."),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Webhook Delivery")
        verbose_name_plural = _("Webhook Deliveries")
        indexes = [
            models.Index(fields=["endpoint", "status"]),
            models.Index(fields=["tenant", "status", "created_at"]),
        ]

    def __str__(self):
        return f"Delivery {self.delivery_id} ({self.status})"


class JournalEntry(ChangeLoggingMixin, BaseModel):
    objects = ExplicitScopeManager()
    # Journal entries are scoped to the tenant (or tenant group) that owns the
    # journaled object (denormalised in the `tenant` / `tenant_group` fields
    # below). allow_global_tenant keeps entries on truly global/shared objects
    # (tenant=None, tenant_group=None) visible to any tenant that can see the
    # object — mirrors the shared-catalogue pattern — while group-scoped entries
    # stay bounded to their group.
    allow_global_tenant = True

    model = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="journal_entries")
    object_id = models.PositiveBigIntegerField(db_index=True)
    content_object = GenericForeignKey("model", "object_id")
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="journal_entries",
        verbose_name=_("User"),
    )
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    comment = models.TextField(verbose_name=_("Comment"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="journal_entries",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_(
            "Denormalised owning tenant, derived from the journaled object on save. Null = system/global object."
        ),
    )
    tenant_group = models.ForeignKey(
        "organization.TenantGroup",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="journal_entries",
        db_index=True,
        verbose_name=_("Tenant group"),
        help_text=_(
            "Denormalised owning tenant group, derived from the journaled object on save. "
            "Null = the object is tenant-scoped or system/global."
        ),
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Journal Entry")
        verbose_name_plural = _("Journal Entries")
        indexes = [
            models.Index(fields=["model", "object_id"], name="core_journa_model_i_3f2f97_idx"),
        ]

    def __str__(self):
        return f"Journal entry on {self.content_object} by {self.user}"

    def save(self, *args, **kwargs):
        # Denormalise the owning tenant/tenant group from the journaled object
        # so entries can be scoped (a GFK alone can't be filtered in the ORM).
        # The object's scope is authoritative — derive it on every save so the
        # UI, REST and seed create paths all agree regardless of ambient context.
        parent = self.content_object
        if parent is not None:
            self.tenant = getattr(parent, "tenant", None)
            self.tenant_group = getattr(parent, "tenant_group", None)
        super().save(*args, **kwargs)


class Bookmark(BaseModel):
    # Personal pin — intentionally NOT change-logged: no audit value, and with no
    # tenant of its own the changelog would mis-attribute to the ambient tenant.
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookmarks", verbose_name=_("User")
    )
    model = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("model", "object_id")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Bookmark")
        verbose_name_plural = _("Bookmarks")
        indexes = [
            models.Index(fields=["user", "model", "object_id"], name="core_bookma_user_id_69a2d6_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "model", "object_id"], name="core_bookmark_unique_user_model_object"
            )
        ]

    def __str__(self):
        return f"Bookmark by {self.user} on {self.content_object}"


class ObjectWatch(BaseModel):
    """Notify the user on every change to the watched object (bell / Watch feature)."""

    # Personal subscription — intentionally NOT change-logged: no audit value, and
    # no tenant of its own to attribute the change to.
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="watches", verbose_name=_("User")
    )
    model = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("model", "object_id")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Object Watch")
        verbose_name_plural = _("Object Watches")
        indexes = [
            models.Index(fields=["user", "model", "object_id"], name="extras_watch_user_id_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "model", "object_id"], name="extras_objectwatch_unique_user_model_object"
            )
        ]

    def __str__(self):
        return f"Watch by {self.user} on {self.content_object}"


class ImageAttachment(ChangeLoggingMixin, BaseModel):
    # Attribute the changelog to the parent object's tenant (None for global
    # parents) via the generic FK, instead of the ambient request tenant.
    changelog_tenant_lookup = "content_object__tenant"

    model = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="image_attachments")
    object_id = models.PositiveBigIntegerField(db_index=True)
    content_object = GenericForeignKey("model", "object_id")
    image = models.ImageField(
        upload_to="attachments/images/", validators=[validate_image_attachment], verbose_name=_("Image")
    )
    name = models.CharField(max_length=255, blank=True, verbose_name=_("Name"))
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Image Attachment")
        verbose_name_plural = _("Image Attachments")
        indexes = [
            models.Index(fields=["model", "object_id"], name="core_imagea_model_i_684849_idx"),
        ]

    def __str__(self):
        return self.name or f"Image {self.pk}"

    def get_serve_url(self):
        # Serve through the authenticated, tenant-scoped proxy rather than the
        # raw MEDIA_URL (which the web server exposes with no access control).
        return reverse("image_attachment_serve", kwargs={"pk": self.pk})


class FileAttachment(ChangeLoggingMixin, BaseModel):
    # Attribute the changelog to the parent object's tenant (None for global
    # parents) via the generic FK, instead of the ambient request tenant.
    changelog_tenant_lookup = "content_object__tenant"

    model = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="file_attachments")
    object_id = models.PositiveBigIntegerField(db_index=True)
    content_object = GenericForeignKey("model", "object_id")
    file = models.FileField(
        upload_to="attachments/files/", validators=[validate_file_attachment], verbose_name=_("File")
    )
    name = models.CharField(max_length=255, blank=True, verbose_name=_("Name"))
    mime_type = models.CharField(max_length=100, blank=True, verbose_name=_("MIME Type"))
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("File Attachment")
        verbose_name_plural = _("File Attachments")
        indexes = [
            models.Index(fields=["model", "object_id"], name="core_fileat_model_i_c8edb4_idx"),
        ]

    def __str__(self):
        return self.name or f"File {self.pk}"

    def get_download_url(self):
        # Download through the authenticated, tenant-scoped proxy (forces
        # attachment + nosniff) instead of the raw MEDIA_URL.
        return reverse("file_attachment_download", kwargs={"pk": self.pk})


class ExportTemplate(ChangeLoggingMixin, BaseModel):
    changelog_global = True  # global template → changelog attributed to tenant=None
    # Server-side Jinja2 template (an SSTI surface) — audit changes to it, to
    # match its siblings LabelTemplate / ReportTemplate which already log.
    # Fallback Content-Type when ``mime_type`` is left blank (NetBox parity).
    DEFAULT_MIME_TYPE = "text/plain; charset=utf-8"

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="export_templates", verbose_name=_("Content Type")
    )
    template_code = models.TextField(
        verbose_name=_("Template Code"),
        help_text=_("Jinja2 template rendered once over the whole result set, which is available as `queryset`."),
    )
    mime_type = models.CharField(
        max_length=50,
        default="text/csv",
        blank=True,
        verbose_name=_("MIME Type"),
        help_text=_("MIME type for the exported file"),
    )
    file_extension = models.CharField(max_length=10, default="csv", blank=True, verbose_name=_("File Extension"))
    as_attachment = models.BooleanField(
        default=True,
        verbose_name=_("Download as attachment"),
        help_text=_("Serve the rendered output as a file download. Disable to display it inline in the browser."),
    )

    class Meta:
        ordering = ["content_type", "name"]
        verbose_name = _("Export Template")
        verbose_name_plural = _("Export Templates")
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "name"], name="core_exporttemplate_unique_content_type_name"
            )
        ]

    def __str__(self):
        return f"{self.content_type.model} - {self.name}"

    def get_absolute_url(self):
        return reverse("extras:exporttemplate_detail", kwargs={"pk": self.pk})

    @staticmethod
    def get_jinja_environment(autoescape=False):
        """A hardened sandboxed Jinja2 environment for rendering export templates.

        Built on ``ImmutableSandboxedEnvironment`` (blocks dunder access + builtin
        mutation gadgets), with the documented SSTI sandbox-escape primitives
        (``|attr``, ``|format``, ``|format_map``, ``|map``, ``|pprint``, ``|xmlattr``)
        and gadget globals (``cycler``/``joiner``/``namespace``/``lipsum``) removed.
        Authoring is already restricted to superusers, so this is defence-in-depth.

        We deliberately never expose Jinja ``Environment`` parameters (``finalize``,
        ``undefined``, …) to stored template data — that is the CVE-2026-29514 RCE
        vector in NetBox's analogous ExportTemplate.
        """
        # inline import: heavy-import: jinja2 is a render-only dependency; keep it off the
        # import-time path of extras.models.
        from jinja2.sandbox import ImmutableSandboxedEnvironment

        env = ImmutableSandboxedEnvironment(autoescape=autoescape)
        for unsafe_filter in ("attr", "format", "format_map", "map", "pprint", "xmlattr"):
            env.filters.pop(unsafe_filter, None)
        for unsafe_global in ("cycler", "joiner", "namespace", "lipsum"):
            env.globals.pop(unsafe_global, None)
        # Useful, safe export helper: neutralise spreadsheet formula injection.
        env.filters["csv_safe"] = csv_safe
        return env

    def _autoescape_for_output(self):
        # Escape interpolated {{ ... }} only for markup output (HTML/XHTML/SVG/XML),
        # where unescaped tenant data would be stored XSS or break the document. CSV/
        # JSON/plain text MUST stay un-escaped, or the rendered document is corrupted.
        mime = (self.mime_type or "").lower()
        return "html" in mime or "svg" in mime or "xml" in mime

    def render(self, queryset):
        """Render the entire queryset in a single pass (NetBox-style).

        The template author iterates the rows themselves via ``queryset`` and emits
        any header. Returns the rendered string with CRLF normalised to LF.
        """
        env = self.get_jinja_environment(autoescape=self._autoescape_for_output())
        template = env.from_string(self.template_code)
        output = template.render(queryset=queryset)
        return output.replace("\r\n", "\n")

    def get_export_filename(self, model):
        """ASCII-safe download filename for this template applied to ``model``."""
        ext = f".{self.file_extension}" if self.file_extension else ""
        return safe_csv_filename(f"{model._meta.model_name}_export{ext}", default=f"export{ext}")


class LabelTemplate(ChangeLoggingMixin, BaseModel):
    changelog_global = True  # global template → changelog attributed to tenant=None
    name = models.CharField(max_length=255, unique=True, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    page_width = models.FloatField(default=2.25, verbose_name=_("Page Width"), help_text=_("Label width in inches"))
    page_height = models.FloatField(default=1.25, verbose_name=_("Page Height"), help_text=_("Label height in inches"))
    barcode_format = models.CharField(
        max_length=20,
        default="code128",
        verbose_name=_("Barcode Format"),
        choices=[
            ("code128", _("Code 128")),
            ("code39", _("Code 39")),
            ("qr", _("QR Code")),
            ("datamatrix", _("Data Matrix")),
        ],
    )
    template_code = models.TextField(
        blank=True, verbose_name=_("Template Code"), help_text=_("Jinja2/HTML template for label layout")
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("Label Template")
        verbose_name_plural = _("Label Templates")

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("extras:labeltemplate_detail", kwargs={"pk": self.pk})


class ReportTemplate(ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    REPORT_TYPE_ASSET_SUMMARY = "asset_summary"
    REPORT_TYPE_LICENSE_UTILIZATION = "license_utilization"
    REPORT_TYPE_SUBSCRIPTION_RENEWALS = "subscription_renewals"
    REPORT_TYPE_ASSET_MAINTENANCE = "asset_maintenance"
    REPORT_TYPE_ASSET_DEPRECIATION = "asset_depreciation"
    REPORT_TYPE_SOFTWARE_INVENTORY = "software_inventory"

    REPORT_TYPE_CONTRACT_RENEWALS = "contract_renewals"
    REPORT_TYPE_WARRANTY_EXPIRATION = "warranty_expiration"
    REPORT_TYPE_ASSET_DISPOSAL_EOL = "asset_disposal_eol"
    REPORT_TYPE_HARDWARE_INVENTORY = "hardware_inventory"
    REPORT_TYPE_CUSTODY_COMPLIANCE = "custody_compliance"

    REPORT_TYPE_CHOICES = [
        (REPORT_TYPE_ASSET_SUMMARY, _("Asset Inventory Summary")),
        (REPORT_TYPE_LICENSE_UTILIZATION, _("License Utilization")),
        (REPORT_TYPE_SUBSCRIPTION_RENEWALS, _("Subscription Renewals")),
        (REPORT_TYPE_ASSET_MAINTENANCE, _("Asset Maintenance & Repairs")),
        (REPORT_TYPE_ASSET_DEPRECIATION, _("Asset Depreciation Summary")),
        (REPORT_TYPE_SOFTWARE_INVENTORY, _("Software Catalog & Installations")),
        (REPORT_TYPE_CONTRACT_RENEWALS, _("Contract Renewals & Expirations")),
        (REPORT_TYPE_WARRANTY_EXPIRATION, _("Warranty Expiration")),
        (REPORT_TYPE_ASSET_DISPOSAL_EOL, _("Asset Disposal & End-of-Life")),
        (REPORT_TYPE_HARDWARE_INVENTORY, _("Hardware Inventory (Accessories, Consumables, Components)")),
        (REPORT_TYPE_CUSTODY_COMPLIANCE, _("Custody & EULA Sign-off Compliance")),
    ]

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="report_templates",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this report template. Null represents system-wide templates."),
    )
    filter_tenants = models.ManyToManyField(
        "organization.Tenant",
        blank=True,
        related_name="filtered_templates",
        verbose_name=_("Filter Tenants"),
        help_text=_(
            "Filter compiled data to only include these selected tenants. If none are selected, aggregates data globally."
        ),
    )
    report_type = models.CharField(max_length=50, choices=REPORT_TYPE_CHOICES, verbose_name=_("Report Type"))
    included_columns = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Included Columns"),
        help_text=_("Checked columns to render in the report data grid."),
    )
    include_summary_cards = models.BooleanField(
        default=True,
        verbose_name=_("Include Summary Cards"),
        help_text=_("Toggle displaying top card widgets (totals, counts, financial sums)."),
    )
    include_distribution_chart = models.BooleanField(
        default=False,
        verbose_name=_("Include Distribution Chart"),
        help_text=_("Toggle embedding spend or status distribution charts in the HTML report."),
    )
    group_by_field = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Group By Field"),
        help_text=_("Optional column key to group grid records under (e.g. location, status)."),
    )
    style_preset = models.CharField(
        max_length=50,
        default="default",
        verbose_name=_("Style Preset"),
        choices=[
            ("default", _("Executive (Branded)")),
            ("compact", _("Compact (Dense)")),
            ("financial", _("Financial (Ledger)")),
            ("minimal", _("Minimal (Clean)")),
        ],
    )
    template_content = models.TextField(
        blank=True,
        verbose_name=_("Custom HTML Template"),
        help_text=_("Optional sandboxed Jinja2 custom HTML template."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Report Template")
        verbose_name_plural = _("Report Templates")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_reporttemplate_name_active"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_report_type_display()})"

    def get_absolute_url(self):
        return reverse("extras:reporttemplate_detail", kwargs={"pk": self.pk})

    def persisted_filter_tenant_ids(self):
        """Return the explicit persisted filter-scope ids without tenant scoping.

        Real model relations are read through their unscoped through-table
        manager so an ambient tenant cannot silently truncate the persisted
        constellation; lightweight test doubles use the relation's ``all()``
        fallback. Mirrors ``ScheduledReport.persisted_scope_tenant_ids``.
        """
        through = getattr(self.filter_tenants, "through", None)
        if through is not None:
            return sorted(
                set(
                    through._base_manager.filter(reporttemplate_id=getattr(self, "pk", None)).values_list(
                        "tenant_id", flat=True
                    )
                )
            )
        return sorted({tenant.pk for tenant in self.filter_tenants.all()})

    def clean(self):
        super().clean()
        unknown = unknown_column_keys(self.included_columns)
        if unknown:
            raise ValidationError(
                {"included_columns": _("Unknown report columns: %(keys)s") % {"keys": ", ".join(unknown)}}
            )


class ScheduledReport(ChangeLoggingMixin, BaseModel):
    objects = ExplicitScopeManager()
    allow_global_tenant = True

    FORMAT_HTML = "html"
    FORMAT_CSV = "csv"
    FORMAT_PDF = "pdf"
    FORMAT_XLSX = "xlsx"
    FORMAT_CHOICES = [
        (FORMAT_HTML, _("HTML Email")),
        (FORMAT_CSV, _("CSV Attachment")),
        (FORMAT_PDF, _("PDF Attachment")),
        (FORMAT_XLSX, _("Excel (XLSX) Attachment")),
    ]

    FREQUENCY_ONCE = "once"
    FREQUENCY_HOURLY = "hourly"
    FREQUENCY_DAILY = "daily"
    FREQUENCY_WEEKLY = "weekly"
    FREQUENCY_BIWEEKLY = "biweekly"
    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_QUARTERLY = "quarterly"
    FREQUENCY_YEARLY = "yearly"
    FREQUENCY_CRON = "cron"

    FREQUENCY_CHOICES = [
        (FREQUENCY_ONCE, _("Once")),
        (FREQUENCY_HOURLY, _("Hourly")),
        (FREQUENCY_DAILY, _("Daily")),
        (FREQUENCY_WEEKLY, _("Weekly")),
        (FREQUENCY_BIWEEKLY, _("Biweekly")),
        (FREQUENCY_MONTHLY, _("Monthly")),
        (FREQUENCY_QUARTERLY, _("Quarterly")),
        (FREQUENCY_YEARLY, _("Yearly")),
        (FREQUENCY_CRON, _("Custom Cron Expression")),
    ]

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="scheduled_reports",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this scheduled report. Null represents system-wide schedules."),
    )
    filter_tenants = models.ManyToManyField(
        "organization.Tenant",
        blank=True,
        related_name="filtered_schedules",
        verbose_name=_("Filter Tenants"),
        help_text=_(
            "Filter compiled data to only include these selected tenants. If none are selected, aggregates data globally."
        ),
    )
    report = models.ForeignKey(
        ReportTemplate, on_delete=models.CASCADE, related_name="schedules", verbose_name=_("Report")
    )
    schedule = models.ForeignKey(
        "django_q.Schedule",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_reports",
        verbose_name=_("Schedule"),
        help_text=_("Linked Django-Q Schedule"),
    )
    recipients = models.TextField(
        blank=True, default="", verbose_name=_("Recipients"), help_text=_("Comma-separated email addresses")
    )
    frequency = models.CharField(
        max_length=50, default="weekly", choices=FREQUENCY_CHOICES, verbose_name=_("Frequency")
    )
    format = models.CharField(max_length=20, choices=FORMAT_CHOICES, default=FORMAT_HTML, verbose_name=_("Format"))
    cron_expression = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Cron Expression"),
        help_text=_("Custom Cron Expression (e.g. '0 8 * * 1-5')"),
    )
    start_time = models.TimeField(
        null=True,
        blank=True,
        verbose_name=_("Start Time"),
        help_text=_("Time of day to run the schedule (e.g. 08:00:00)"),
    )
    channels = models.ManyToManyField(
        "extras.NotificationChannel", blank=True, related_name="scheduled_reports", verbose_name=_("Channels")
    )
    save_to_archive = models.BooleanField(
        default=True,
        verbose_name=_("Save To Archive"),
        help_text=_("Store a copy of generated reports in the local file archive"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))
    last_run = models.DateTimeField(null=True, blank=True, verbose_name=_("Last Run"))
    last_status = models.CharField(max_length=50, blank=True, verbose_name=_("Last Status"))
    last_accepted_fire_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Last Accepted Fire"),
        help_text=_(
            "Newest intended run time accepted for execution. Informational marker only; per-occurrence "
            "idempotency is enforced by the fire records, where a redelivered occurrence is an exact-match no-op."
        ),
    )
    last_run_archive = models.ForeignKey(
        "ReportGenerationArchive",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
        verbose_name=_("Last Run Archive"),
        help_text=_(
            "Output archive retained by the newest completed run. Retry delivery re-sends exactly this "
            "archive; a run that retained none clears the reference, so an older archived report is never "
            "redelivered in its place."
        ),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("Scheduled Report")
        verbose_name_plural = _("Scheduled Reports")

    def __str__(self):
        return f"{self.name} -> {self.report.name}"

    @property
    def delivery_retryable(self):
        """Whether the last run left a retryable delivery failure to recover.

        Requires an active schedule: pausing a schedule stops its delivery, so
        the recovery action only surfaces while the schedule is enabled; a
        paused schedule must never dispatch externally, mirroring the run path
        that skips inactive schedules. Generation failures are terminal for
        the run and are recovered with ``Run now``; this only marks runs whose
        generation succeeded but whose delivery fan-out did not fully succeed,
        including legacy rows that still carry the pre-promotion
        ``delivery_`` status text. The action binds to the archive of the
        last run (``last_run_archive``): when that run retained no archive,
        retrying is refused so an older archived report can never be
        re-sent in its place.
        """
        if not self.is_active:
            return False
        if self.last_run_archive_id is None:
            return False
        return self.last_status in ("partial", "failed") or self.last_status.startswith("delivery_")

    def persisted_scope_tenant_ids(self):
        """Return explicit persisted filter-scope ids without tenant scoping.

        A schedule's own filter tenants take precedence over the report
        template's inherited filter tenants. Real model relations are read
        through their unscoped through-table manager so an ambient tenant
        cannot truncate the persisted scope; lightweight test doubles use
        their relation's ``all()`` fallback.
        """

        def relation_tenant_ids(relation, owner_field, owner_id):
            through = getattr(relation, "through", None)
            if through is not None:
                return set(through._base_manager.filter(**{owner_field: owner_id}).values_list("tenant_id", flat=True))
            return {tenant.pk for tenant in relation.all()}

        filter_ids = relation_tenant_ids(self.filter_tenants, "scheduledreport_id", getattr(self, "pk", None))
        report_id = getattr(self, "report_id", None)
        report = getattr(self, "report", None)
        if not filter_ids and report_id and report is not None:
            filter_ids = relation_tenant_ids(report.filter_tenants, "reporttemplate_id", report_id)
        return sorted(filter_ids)

    def explicit_scope_tenant_ids(self):
        """Backward-compatible alias for the persisted explicit scope helper."""
        return self.persisted_scope_tenant_ids()

    def effective_scope_tenant_ids(self):
        """Return the persisted tenant ids this schedule will compile.

        The owner tenant is only a fallback when neither the schedule nor its
        report template has an explicit persisted filter scope.
        """
        scope_ids = (
            self.persisted_scope_tenant_ids()
            if hasattr(self, "persisted_scope_tenant_ids")
            else ScheduledReport.persisted_scope_tenant_ids(self)
        )
        if scope_ids:
            return scope_ids
        report = getattr(self, "report", None)
        active_tenant = getattr(self, "tenant", None) or (getattr(report, "tenant", None) if report else None)
        return [active_tenant.pk] if active_tenant else []

    def scope_requires_authorization(self):
        """Whether this schedule cannot be represented by one owner tenant."""
        active_tenant = self.tenant or (self.report.tenant if self.report_id else None)
        scope_ids = self.effective_scope_tenant_ids()
        return active_tenant is None or scope_ids != [active_tenant.pk]

    def clean(self):
        super().clean()
        if self.frequency == "cron":
            if not self.cron_expression:
                raise ValidationError(
                    {"cron_expression": _("Cron expression is required when frequency is set to Custom Cron.")}
                )
            try:
                from croniter import croniter
                from django.utils import timezone

                croniter(self.cron_expression, timezone.now())
            except Exception as e:
                raise ValidationError(
                    {"cron_expression": _("Invalid Cron expression: %(error)s") % {"error": str(e)}}
                ) from None
        if self.recipients:
            from django.core.validators import validate_email

            emails = [e.strip() for e in self.recipients.split(",") if e.strip()]
            if not emails:
                raise ValidationError({"recipients": _("No recipient email addresses entered.")})
            for email in emails:
                try:
                    validate_email(email)
                except ValidationError:
                    raise ValidationError(
                        {"recipients": _("'%(email)s' is not a valid email address.") % {"email": email}}
                    ) from None


class ScheduledReportFire(models.Model):
    """One accepted occurrence of a scheduled report.

    Idempotency is per ``(schedule, intended fire time)``: the unique record
    makes the insert the fence, so a broker redelivery of the same occurrence
    is an exact-match no-op, and out-of-order execution (parallel workers, a
    catch-up backlog replayed after downtime) can never let a newer occurrence
    discard an older, not-yet-accepted one.
    """

    schedule = models.ForeignKey(
        ScheduledReport,
        on_delete=models.CASCADE,
        related_name="fires",
        verbose_name=_("Scheduled Report"),
    )
    intended_fire_at = models.DateTimeField(verbose_name=_("Intended Fire"))
    accepted_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Accepted At"))

    class Meta:
        ordering = ["-intended_fire_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "intended_fire_at"],
                name="extras_scheduledreportfire_unique_occurrence",
            )
        ]
        verbose_name = _("Scheduled Report Fire")
        verbose_name_plural = _("Scheduled Report Fires")

    def __str__(self):
        return f"{self.schedule_id}@{self.intended_fire_at:%Y-%m-%d %H:%M:%S}"


class ScheduledReportScopeAuthorization(models.Model):
    """Durable approval for a scheduled report's cross-tenant scope."""

    scheduled_report = models.OneToOneField(
        ScheduledReport,
        on_delete=models.CASCADE,
        related_name="scope_authorization",
        verbose_name=_("Scheduled Report"),
    )
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="scheduled_report_scope_authorizations",
        editable=False,
        verbose_name=_("Authorized By"),
    )
    scope_tenant_ids = models.JSONField(default=list, editable=False, verbose_name=_("Authorized Tenant Scope"))
    approved_at = models.DateTimeField(default=timezone.now, editable=False, verbose_name=_("Approved At"))
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="revoked_scheduled_report_scope_authorizations",
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Revoked By"),
    )
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("Revoked At"))

    @classmethod
    def approve(cls, scheduled_report, actor):
        """Persist an exact scope approval for a principal with cross-tenant permission."""
        if not getattr(actor, "is_active", False) or not actor.has_perm("reports.view_cross_tenant_reports"):
            raise PermissionDenied(_("Cross-tenant report scope approval requires the cross-tenant report permission."))
        # The scope reads use the unscoped through table, so the permission
        # check above binding the ambient tenant cannot truncate the scope.
        if not scheduled_report.scope_requires_authorization():
            raise ValidationError(_("A single-tenant schedule does not need cross-tenant scope approval."))
        scope_tenant_ids = scheduled_report.effective_scope_tenant_ids()
        authorization, _created = cls.objects.update_or_create(
            scheduled_report=scheduled_report,
            defaults={
                "authorized_by": actor,
                "scope_tenant_ids": scope_tenant_ids,
                "approved_at": timezone.now(),
                "revoked_by": None,
                "revoked_at": None,
            },
        )
        return authorization

    @classmethod
    def revoke(cls, scheduled_report, actor):
        """Persist the revocation of an existing scope approval.

        Revocation keeps the row so the approve/revoke history stays visible,
        but marks it void: a revoked approval authorizes nothing. Generation
        fails closed with ``report.scope_unauthorized`` while the schedule
        remains cross-tenant; a schedule wound back to single-tenant runs as
        an ordinary single-tenant schedule.

        Revocation is immediately and permanently effective on write, so the
        actor must hold the cross-tenant permission on every tenant of the
        stored approval. Approve has a generation-time reach backstop;
        revoke does not.
        """
        if not getattr(actor, "is_active", False) or not actor.has_perm("reports.view_cross_tenant_reports"):
            raise PermissionDenied(
                _("Cross-tenant report scope revocation requires the cross-tenant report permission.")
            )
        authorization = cls.objects.filter(scheduled_report=scheduled_report).first()
        if authorization is None:
            raise ValidationError(_("This schedule has no cross-tenant scope approval to revoke."))
        if authorization.revoked_at is not None:
            raise ValidationError(_("This schedule's cross-tenant scope approval is already revoked."))
        Tenant = apps.get_model("organization", "Tenant")
        missing_tenants = [
            tenant
            for tenant in Tenant._base_manager.filter(pk__in=authorization.scope_tenant_ids)
            if not actor.has_perm("reports.view_cross_tenant_reports", obj=tenant)
        ]
        if missing_tenants:
            raise PermissionDenied(
                _("Your permission does not cover these tenants: %(tenants)s. The revocation would not take effect.")
                % {"tenants": ", ".join(tenant.name for tenant in missing_tenants)}
            )
        authorization.revoked_by = actor
        authorization.revoked_at = timezone.now()
        authorization.save(update_fields=["revoked_by", "revoked_at"])
        return authorization

    def is_revoked(self):
        return self.revoked_at is not None

    def __str__(self):
        return f"Scope approval for {self.scheduled_report}"


class ReportGenerationArchive(ChangeLoggingMixin, BaseModel):
    objects = ExplicitScopeManager()

    scheduled_report = models.ForeignKey(
        ScheduledReport, on_delete=models.CASCADE, related_name="archives", verbose_name=_("Scheduled Report")
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    format = models.CharField(max_length=20, verbose_name=_("Format"))
    status = models.CharField(max_length=50, verbose_name=_("Status"))
    error_message = models.TextField(blank=True, verbose_name=_("Error Message"))
    delivery_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name=_("Delivery Status"),
        help_text=_(
            "Outcome of the delivery fan-out for this run: blank (not dispatched or pre-upgrade row), "
            "'none' (no targets configured), 'success', 'partial', or 'failed'."
        ),
    )
    delivery_targets = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Delivery Targets"),
        help_text=_(
            "Per-target delivery ledger of this run (email aggregate and one entry per notification "
            "channel). Retry delivery re-attempts only targets recorded as failed, using the recorded "
            "original targets and payloads."
        ),
    )
    generation_scope = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        verbose_name=_("Generation Scope"),
        help_text=_(
            "Tenant scope snapshot of the archived generation (active tenant, data tenant ids, cross-tenant "
            "flag, approval reference). Retry delivery re-validates this archived scope, so a later scope "
            "change cannot legitimize redelivering an older export."
        ),
    )
    retry_claim_token = models.CharField(
        max_length=64,
        blank=True,
        default="",
        editable=False,
        verbose_name=_("Retry Claim Token"),
    )
    retry_claim_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Retry Claim Expires At"),
    )
    disclosure_text = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Disclosure Text"),
        help_text=_("Scope/truncation disclosure carried by the delivered output, kept for faithful redelivery."),
    )
    file = models.ForeignKey(
        "extras.FileAttachment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="report_archives",
        verbose_name=_("File"),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="report_archives",
        db_index=True,
        verbose_name=_("Tenant"),
    )

    class Meta:
        ordering = ["-generated_at"]
        verbose_name = _("Report Generation Archive")
        verbose_name_plural = _("Report Generation Archives")

    def __str__(self):
        return f"{self.scheduled_report.name} - {self.generated_at:%Y-%m-%d %H:%M:%S}"


class NotificationChannel(ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    # config holds channel secrets (SMTP password, Slack/Teams webhook URLs with
    # embedded tokens); keep it out of the changelog JSON.
    _change_logging_excluded_fields = ["updated_at", "config"]

    TYPE_EMAIL = "email"
    TYPE_IN_APP = "in_app"
    TYPE_SLACK = "slack"
    TYPE_TEAMS = "teams"

    CHANNEL_TYPE_CHOICES = [
        (TYPE_EMAIL, _("Email")),
        (TYPE_IN_APP, _("In-App")),
        (TYPE_SLACK, _("Slack")),
        (TYPE_TEAMS, _("Microsoft Teams")),
    ]

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    channel_type = models.CharField(max_length=20, choices=CHANNEL_TYPE_CHOICES, verbose_name=_("Channel Type"))
    enabled = models.BooleanField(default=True, verbose_name=_("Enabled"))
    config = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Config"),
        help_text=_("Channel-specific config (SMTP settings, webhook URL, etc.)"),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="notification_channels",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this channel. Null represents system-wide channels."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Notification Channel")
        verbose_name_plural = _("Notification Channels")
        constraints = [
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_notificationchannel_name_active",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_channel_type_display()})"


def alert_rule_channel_scope_errors(channels, tenant_id):
    """Explain every channel attachment outside the rule's delivery scope.

    A rule notifies only through channels of its own scope: a tenant rule
    through its own tenant's channels, a platform-wide rule through
    platform-wide channels. The dispatch guard treats out-of-scope rows as
    absent (fail closed); the form and API boundaries reject them up front
    with these messages so a configuration that can never deliver fails
    predictably instead of silently (issue #567).
    """
    errors = []
    for channel in channels:
        if channel.tenant_id == tenant_id:
            continue
        if tenant_id is None:
            reason = _("it belongs to a tenant; a platform-wide rule notifies only through platform-wide channels.")
        elif channel.tenant_id is None:
            reason = _("it is platform-wide; a tenant rule notifies only through its own tenant's channels.")
        else:
            reason = _("it belongs to another tenant; a tenant rule notifies only through its own tenant's channels.")
        errors.append(
            _('Channel "%(name)s" cannot deliver for this rule: %(reason)s') % {"name": channel.name, "reason": reason}
        )
    return errors


class AlertRule(ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    ALERT_TYPE_LOW_STOCK = "low_stock"
    ALERT_TYPE_UPCOMING_EOL = "upcoming_eol"
    ALERT_TYPE_LICENSE_EXPIRY = "license_expiry"
    ALERT_TYPE_RENEWAL_DUE = "renewal_due"
    ALERT_TYPE_WARRANTY_EXPIRY = "warranty_expiry"
    ALERT_TYPE_AUDIT_OVERDUE = "audit_overdue"

    ALERT_TYPE_CHOICES = [
        (ALERT_TYPE_LOW_STOCK, _("Low Stock Alert")),
        (ALERT_TYPE_UPCOMING_EOL, _("Upcoming EOL Planning")),
        (ALERT_TYPE_LICENSE_EXPIRY, _("License Expiry Alert")),
        (ALERT_TYPE_RENEWAL_DUE, _("Renewal Due Alert")),
        (ALERT_TYPE_WARRANTY_EXPIRY, _("Warranty Expiry Alert")),
        (ALERT_TYPE_AUDIT_OVERDUE, _("Audit Overdue")),
    ]

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_CRITICAL = "critical"

    SEVERITY_CHOICES = [
        (SEVERITY_INFO, _("Info")),
        (SEVERITY_WARNING, _("Warning")),
        (SEVERITY_CRITICAL, _("Critical")),
    ]

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    alert_type = models.CharField(max_length=50, choices=ALERT_TYPE_CHOICES, verbose_name=_("Alert Type"))
    threshold_value = models.PositiveIntegerField(
        verbose_name=_("Threshold Value"), help_text=_("Limit count or days horizon")
    )
    severity = models.CharField(
        max_length=20, choices=SEVERITY_CHOICES, default=SEVERITY_WARNING, verbose_name=_("Severity")
    )
    is_active = models.BooleanField(
        default=True, verbose_name=_("Is Active"), help_text=_("Inactive rules are not evaluated at all.")
    )
    is_muted = models.BooleanField(
        default=False,
        verbose_name=_("Is Muted"),
        help_text=_("Muted rules still track alerts in the Alert Center but send no channel notifications."),
    )
    renotify_interval_days = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Renotify Interval Days"),
        help_text=_("0 = notify once. N = re-send channel notifications every N days while an alert stays unresolved."),
    )
    last_fired_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text=_("When this rule was last evaluated by the engine."),
    )
    channels = models.ManyToManyField(
        NotificationChannel, blank=True, related_name="alert_rules", verbose_name=_("Channels")
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="alert_rules",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this rule. Null represents system-wide rules."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Alert Rule")
        verbose_name_plural = _("Alert Rules")
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=models.Q(deleted_at__isnull=True), name="unique_alertrule_name_active"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_alert_type_display()})"

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("extras:alertrule_detail", kwargs={"pk": self.pk})


class AlertLog(ChangeLoggingMixin, BaseModel):
    objects = ExplicitScopeManager()
    # Deliberately cross-tenant / unscoped manager for context-independent
    # dedup/auto-resolve in the alert engine ONLY: it spans all tenants, so the
    # engine does not depend on an active scope. NOT named ``all_objects`` on
    # purpose (that name carries a tenant-scoped contract; the Recycle Bin relies
    # on it); this one must never back a tenant-facing view/API.
    unscoped = AllObjectsManager()

    STATUS_ACTIVE = "active"
    STATUS_ACKNOWLEDGED = "acknowledged"
    STATUS_RESOLVED = "resolved"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, _("Active")),
        (STATUS_ACKNOWLEDGED, _("Acknowledged")),
        (STATUS_RESOLVED, _("Resolved")),
    ]

    # Denormalized single-attempt delivery outcome. ``delivery_status``
    # keeps the full per-channel typed payload; this field makes delivery queryable/
    # filterable without JSON lookups and stays truthful under the no-retry policy.
    DELIVERY_OUTCOME_NONE = "none"
    DELIVERY_OUTCOME_PENDING = "pending"
    DELIVERY_OUTCOME_DELIVERED = "delivered"
    DELIVERY_OUTCOME_FAILED = "failed"

    DELIVERY_OUTCOME_CHOICES = [
        (DELIVERY_OUTCOME_NONE, _("No delivery planned")),
        (DELIVERY_OUTCOME_PENDING, _("Dispatch pending")),
        (DELIVERY_OUTCOME_DELIVERED, _("Delivered")),
        (DELIVERY_OUTCOME_FAILED, _("Failed")),
    ]

    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="logs", verbose_name=_("Rule"))
    subject = models.CharField(max_length=255, verbose_name=_("Subject"))
    message = models.TextField(verbose_name=_("Message"))
    severity = models.CharField(
        max_length=20,
        choices=AlertRule.SEVERITY_CHOICES,
        default=AlertRule.SEVERITY_WARNING,
        db_index=True,
        verbose_name=_("Severity"),
    )
    content_type = models.ForeignKey("contenttypes.ContentType", on_delete=models.CASCADE, related_name="alert_logs")
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True, verbose_name=_("Status")
    )
    delivery_status = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Delivery Status"),
        help_text=_(
            "Per-channel typed delivery outcome: {channel_pk: {disposition, operation, "
            "delivery_id, attempted_at, error_class?, message?}} plus dispatch bookkeeping "
            "keys (__dispatch__, __delivery_id__, __no_channels__). Legacy string values "
            "('ok'|'failed'|'error: ...') remain readable."
        ),
    )
    delivery_outcome = models.CharField(
        max_length=20,
        choices=DELIVERY_OUTCOME_CHOICES,
        default=DELIVERY_OUTCOME_NONE,
        db_index=True,
        verbose_name=_("Delivery Outcome"),
        help_text=_("Denormalized single-attempt delivery outcome (none|pending|delivered|failed)."),
    )
    delivery_attempts = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Delivery Attempts"),
        help_text=_(
            "Number of dispatch runs attempted for this alert; each planned dispatch (including "
            "renotification) counts as one fresh attempt."
        ),
    )
    last_delivery_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name=_("Last Delivery ID"),
        help_text=_("Stable unique identifier of the most recent dispatch run; unchanged for that run."),
    )
    last_delivery_error = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name=_("Last Delivery Error"),
        help_text=_("Typed error class (or disposition) of the most recent failed channel delivery, if any."),
    )
    last_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last Notified At"),
        help_text=_("When channel notifications were last dispatched for this alert (drives re-notify)."),
    )
    acknowledged_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_alerts",
        verbose_name=_("Acknowledged By"),
    )
    resolved_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_alerts",
        verbose_name=_("Resolved By"),
    )
    resolution_notes = models.TextField(blank=True, verbose_name=_("Resolution Notes"))
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Resolved At"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="alert_logs",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this log. Null represents system-wide logs."),
    )
    tenant_resolution_status = models.CharField(
        max_length=20,
        choices=[
            ("not_required", _("Not required")),
            ("resolved", _("Resolved from target")),
            ("global", _("Global target")),
            ("unresolved", _("Unresolved — operator review required")),
        ],
        default="not_required",
        db_index=True,
        verbose_name=_("Tenant Resolution"),
        help_text=_("Reconciliation state for legacy tenant-less alerts."),
    )

    @property
    def content_object_safe(self):
        # Resolve through an unscoped fallback, then enforce the alert tenant.
        try:
            obj = self._resolve_content_object()
            if obj is None or not self._content_object_matches_tenant(obj):
                return None
            return obj
        # broad except: render-degrade: an unresolved alert target must not fail serialization
        except Exception:
            return None

    def _resolve_content_object(self):
        obj = self.content_object
        if obj is not None or not self.content_type or not self.object_id:
            return obj
        model_class = self.content_type.model_class()
        manager = getattr(model_class, "all_objects", None) or model_class._base_manager
        return manager.filter(pk=self.object_id).first()

    def _content_object_matches_tenant(self, obj):
        model_class = self.content_type.model_class() if self.content_type else None
        allows_global = bool(
            getattr(model_class, "allow_global_tenant", False) or getattr(model_class, "changelog_global", False)
        )
        has_tenant, object_tenant_id = self._content_object_tenant_id(obj)
        if not has_tenant:
            # Tenant ownership that cannot be derived from the target itself
            # must never be guessed from the active request context.
            return allows_global
        if object_tenant_id is None and not allows_global:
            return False
        if self.tenant_id is None:
            return object_tenant_id is None
        return object_tenant_id is None or object_tenant_id == self.tenant_id

    @staticmethod
    def _content_object_tenant_id(obj):
        if hasattr(obj, "tenant_id"):
            return True, obj.tenant_id
        if hasattr(obj, "tenant"):
            object_tenant = getattr(obj, "tenant", None)
            return True, getattr(object_tenant, "pk", None)
        return False, None

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Alert Log")
        verbose_name_plural = _("Alert Logs")
        indexes = [
            models.Index(fields=["content_type", "object_id"], name="core_alertl_content_706751_idx"),
            models.Index(fields=["severity"], name="core_alertl_severit_f0ec11_idx"),
            models.Index(fields=["status"], name="core_alertl_status_b2f47a_idx"),
        ]
        constraints = [
            # At most one OPEN (active/acknowledged) alert per rule+object.
            # Resolved rows are exempt so a cleared condition can legitimately
            # re-fire later. Literal status strings: the class constants are not
            # in scope inside Meta.
            models.UniqueConstraint(
                fields=["rule", "content_type", "object_id"],
                condition=models.Q(status__in=["active", "acknowledged"]),
                name="uniq_open_alert_per_object",
            ),
        ]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.subject}"


class SavedFilter(ChangeLoggingMixin, SoftDeleteMixin, BaseModel):
    """A named, reusable set of list-view filter parameters.

    Scoped to one ``content_type`` (the model whose list it filters). Owned by a
    tenant (visible only to that tenant) or system-wide when ``tenant`` is null
    (``allow_global_tenant`` — only superusers create global filters). Within a
    tenant a filter is visible to every member when ``shared`` (the default), or
    only to its ``created_by`` owner otherwise.
    """

    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    name = models.CharField(max_length=255, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="saved_filters",
        verbose_name=_("Content Type"),
        help_text=_("The model whose list view this filter applies to."),
    )
    parameters = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Parameters"),
        help_text=_("Stored list-view filter querystring parameters."),
    )
    shared = models.BooleanField(
        default=True,
        verbose_name=_("Shared"),
        help_text=_("Visible to all members of the owning tenant. If unset, only the creator can use it."),
    )
    enabled = models.BooleanField(default=True, verbose_name=_("Enabled"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saved_filters",
        verbose_name=_("Created By"),
    )
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="saved_filters",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this filter. Null represents system-wide filters."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["content_type", "name"]
        verbose_name = _("Saved Filter")
        verbose_name_plural = _("Saved Filters")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "content_type", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_savedfilter_name_active",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("extras:savedfilter_detail", kwargs={"pk": self.pk})
