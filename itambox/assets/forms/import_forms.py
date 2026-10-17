import ast
import json
import re
from functools import lru_cache
from typing import NamedTuple

import jsonschema
from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.db import models
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from assets.models import (
    Asset,
    AssetMaintenance,
    AssetRole,
    AssetType,
    Category,
    Manufacturer,
    Supplier,
    Warranty,
    WarrantyTypeChoices,
)
from assets.models.choices import MaintenanceStatusChoices
from assets.services.specifications.catalogue_schema import build_catalogue_schema
from assets.services.specifications.commands import create_asset_type, preview_asset_type_create
from assets.services.specifications.contracts import FieldsetSelectionDTO, SpecificationPatchDTO
from assets.services.specifications.core_vocabulary import get_core_vocabulary
from assets.services.specifications.messages import specification_message
from assets.specification_adapters import (
    actor_context_for_user,
    create_fieldset_selection,
    native_asset_type_create_input,
    require_command_success,
)
from core.context import get_current_tenant
from core.importers.bulk_forms import (
    ROW_CREATED,
    BulkImportForm,
    HybridCatalogueImportForm,
    RowSkipped,
    _row_error_messages,
    register_import_form,
)
from core.managers import Scope
from core.slugs import generate_unique_slug
from inventory.models import Accessory, Component, Consumable
from licenses.models import License
from organization.models import AssetHolder, Contact, ContactRole, Location, Region, Site, SiteGroup
from organization.services.catalogue_authorization import has_provider_catalogue_permission

# These forms are declarative: the base BulkImportForm.map_row maps scalar
# columns and resolves ForeignKey columns by id / slug / name unless a curated
# form explicitly narrows a reference contract to natural keys.
# required_fields/optional_fields drive validation and the Field Options help
# table. Do not list non-model columns here — they are silently skipped.
# @register_import_form wires each form to the centralized GenericObjectImportView
# at /import/<app>/<model>/ (no per-app view needed).


@register_import_form
class AssetBulkImportForm(BulkImportForm):
    model = Asset
    # Updates match an existing asset by its tag (unique per tenant).
    update_key = ("asset_tag",)
    # asset_tag is auto-generated when blank (AssetTagSequence), so it is optional.
    required_fields = ["name"]
    optional_fields = [
        "asset_tag",
        "serial_number",
        "asset_type",
        "asset_role",
        "status",
        "location",
        "supplier",
        "purchase_date",
        "purchase_cost",
        "order_number",
        "notes",
    ]


@lru_cache(maxsize=1)
def _specs_validator():
    """Validator over the same generated schema the community catalogue uses."""
    return jsonschema.Draft202012Validator(build_catalogue_schema()["properties"]["specs"])


@lru_cache(maxsize=1)
def _spec_field_meta():
    fields = get_core_vocabulary()["active_fields"]
    return {f["key"]: (f["field_type"], f["canonical_unit"]) for f in fields}


def _parse_specs(raw):
    """Return the specs mapping of a row (YAML mapping or JSON text), or raise."""
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValidationError(_("specs: must be a mapping of specification keys to values.")) from None
    if not isinstance(raw, dict):
        raise ValidationError(_("specs: must be a mapping of specification keys to values."))
    return raw


def _validate_specs(specs):
    errors = sorted(_specs_validator().iter_errors(specs), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        messages = []
        for error in errors:
            path = ".".join(["specs", *(str(p) for p in error.absolute_path)])
            messages.append(f"{path}: {error.message}")
        raise ValidationError(messages)


def _strip_unit(key, value):
    """Turn a schema-valid quantity string ("50.5 Wh") into the codec's bare value."""
    field_type, unit = _spec_field_meta().get(key, (None, None))
    if unit and isinstance(value, str) and value.endswith(f" {unit}"):
        bare = value[: -len(unit) - 1]
        return int(bare) if field_type == "integer" else bare
    return value


def _spec_patch_values(raw):
    specs = _parse_specs(raw)
    _validate_specs(specs)
    return {key: _strip_unit(key, value) for key, value in specs.items()}


class _TypeRowPlan(NamedTuple):
    manufacturer: Manufacturer | None
    manufacturer_name: str
    model: str
    category: Category | None
    asset_role: AssetRole | None
    values: dict
    patch_values: dict


@register_import_form
class AssetTypeBulkImportForm(BulkImportForm):
    """Catalogue-style asset type import.

    Rows are created through the specification commands (never ``model(**row)``).
    An existing type (same manufacturer and model) is skipped and reported, a
    missing manufacturer is created for actors holding ``add_manufacturer``, and
    an unknown category is an error. ``specs`` is validated against the same
    generated schema as the community catalogue.
    """

    model = AssetType
    structured_fields = ("specs",)
    required_fields = ["manufacturer", "model"]
    optional_fields = [
        "slug",
        "part_number",
        "ean",
        "category",
        "asset_role",
        "description",
        "comments",
        "specs",
    ]

    def clean(self):
        cleaned_data = super().clean()
        if not self._rows_data:
            return cleaned_data
        errors, notes = [], []
        for number, row in enumerate(self._rows_data, start=2):
            try:
                plan = self._plan_row(row)
                self._dry_run(plan)
                if plan.manufacturer is None:
                    notes.append(
                        _("Row %(row)s: manufacturer '%(name)s' will be created.")
                        % {"row": number, "name": plan.manufacturer_name}
                    )
            except RowSkipped as skip:
                notes.append(_("Row %(row)s: %(reason)s") % {"row": number, "reason": skip})
            except ValidationError as exc:
                errors.extend(f"Row {number}: {message}" for message in exc.messages)
        self.row_notes = notes
        if errors:
            raise ValidationError(errors)
        return cleaned_data

    # -- planning ---------------------------------------------------------

    def _plan_row(self, row):
        name = str(row.get("manufacturer") or "").strip()
        model_name = str(row.get("model") or "").strip()
        if not name or not model_name:
            raise ValidationError(_('"manufacturer" and "model" are required.'))
        manufacturer = self._find_manufacturer(name)
        if manufacturer is None:
            self._require_manufacturer_permission()
        elif AssetType.objects.filter(manufacturer=manufacturer, model__iexact=model_name).exists():
            raise RowSkipped(
                _("asset type '%(manufacturer)s %(model)s' already exists; skipped.")
                % {"manufacturer": manufacturer.name, "model": model_name}
            )
        category = self._find_category(row.get("category"))
        asset_role = self._find_asset_role(row.get("asset_role"))
        patch_values = _spec_patch_values(row.get("specs"))
        values = {
            "model": model_name,
            "slug": str(row.get("slug") or "").strip() or None,
            "part_number": str(row.get("part_number") or "").strip(),
            "ean": str(row.get("ean") or "").strip(),
            "description": str(row.get("description") or "").strip(),
            "comments": str(row.get("comments") or "").strip(),
        }
        return _TypeRowPlan(manufacturer, name, model_name, category, asset_role, values, patch_values)

    @staticmethod
    def _find_manufacturer(name):
        queryset = Manufacturer.objects
        return (
            queryset.filter(name=name).first()
            or queryset.filter(name__iexact=name).first()
            or queryset.filter(slug=name).first()
        )

    @staticmethod
    def _find_category(raw):
        slug = str(raw or "").strip()
        if not slug:
            return None
        category = Category.objects.filter(slug=slug).first()
        if category is None:
            raise ValidationError(
                _("category: unknown category '%(slug)s'. Categories are never created by an import.") % {"slug": slug}
            )
        return category

    @staticmethod
    def _find_asset_role(raw):
        value = str(raw or "").strip()
        if not value:
            return None
        role = AssetRole.objects.filter(slug=value).first() or AssetRole.objects.filter(name__iexact=value).first()
        if role is None:
            raise ValidationError(_("asset_role: unknown asset role '%(value)s'.") % {"value": value})
        return role

    def _require_manufacturer_permission(self):
        actor = self.actor
        if actor is None or not actor.has_perm("assets.add_manufacturer"):
            raise ValidationError(
                _("The manufacturer does not exist and you do not have permission to create manufacturers.")
            )

    # -- command construction --------------------------------------------

    def _actor_context(self):
        try:
            return actor_context_for_user(self.actor)
        except PermissionDenied:
            raise ValidationError(_("An authenticated actor is required for this import.")) from None

    @staticmethod
    def _fieldset_selection(plan):
        """Category defaults compose the Fieldsets; without a category, use the sections that own the keys."""
        if plan.category is not None or not plan.patch_values:
            return create_fieldset_selection((), omitted=True)
        identities = []
        for field in get_core_vocabulary()["active_fields"]:
            if field["key"] in plan.patch_values:
                for membership in field["memberships"]:
                    if membership["section"] not in identities:
                        identities.append(membership["section"])
        return FieldsetSelectionDTO(presence="explicit", identities=tuple(identities))

    def _preview_command(self, plan, manufacturer):
        actor = self._actor_context()
        native = native_asset_type_create_input(
            {
                **plan.values,
                "manufacturer": manufacturer,
                "category": plan.category,
                "asset_role": plan.asset_role,
            }
        )
        fieldsets = self._fieldset_selection(plan)
        patch = SpecificationPatchDTO(set_values=plan.patch_values, clear_keys=())
        preview = require_command_success(
            preview_asset_type_create(actor=actor, native=native, fieldsets=fieldsets, patch=patch)
        )
        issues = getattr(preview, "issues", ())
        if issues:
            raise ValidationError("; ".join(specification_message(issue.message_key) for issue in issues))
        return actor, native, fieldsets, patch, preview

    def _dry_run(self, plan):
        """Run the command's read-only preview during the form preview."""
        if self.actor is not None and plan.manufacturer is not None:
            self._preview_command(plan, plan.manufacturer)

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _create_manufacturer(name):
        manufacturer = Manufacturer(name=name)
        generate_unique_slug(manufacturer, "name")
        manufacturer.full_clean()
        manufacturer.save()
        return manufacturer

    def _import_row(self, row, row_number):
        plan = self._plan_row(row)
        manufacturer = plan.manufacturer or self._create_manufacturer(plan.manufacturer_name)
        actor, native, fieldsets, patch, preview = self._preview_command(plan, manufacturer)
        result = create_asset_type(
            actor=actor,
            native=native,
            fieldsets=fieldsets,
            patch=patch,
            preview_token=preview.preview_token,
            expected_definition_revision=preview.expected_definition_revision,
            expected_category_default_snapshot_revision=preview.expected_category_default_snapshot_revision,
        )
        require_command_success(result)

    @classmethod
    def can_import(cls, actor: object) -> bool:
        return has_provider_catalogue_permission(actor, "assets.add_assettype")


@register_import_form
class ManufacturerBulkImportForm(BulkImportForm):
    model = Manufacturer
    update_key = ("name",)
    generated_slug_source = "name"
    required_fields = ["name"]
    optional_fields = ["slug", "description"]


def _resolve_natural_key_reference(model, value, *, field, unknown_message=None):
    """Resolve a curated reference by slug/name without accepting numeric IDs."""
    for lookup in ("slug", "name", "slug__iexact", "name__iexact"):
        matches = list(model.objects.filter(**{lookup: value})[:2])
        if len(matches) > 1:
            raise ValidationError(
                _('%(field)s: "%(value)s" matches more than one %(model)s. Enter a unique Slug or Name.')
                % {"field": field, "value": value, "model": model._meta.verbose_name}
            )
        if matches:
            return matches[0].pk
    if unknown_message is not None:
        raise ValidationError({field: unknown_message % {"value": value}})
    raise ValidationError(
        _(
            '%(field)s: no matching %(model)s was found for "%(value)s". '
            "Check the Slug or Name, or create the missing parent before importing."
        )
        % {"field": field, "value": value, "model": model._meta.verbose_name}
    )


class _ReferenceDataBulkImportForm(BulkImportForm):
    """Small curated reference-data forms with row-level preview and safe skips."""

    model: type[models.Model]

    def clean(self):
        cleaned_data = super().clean()
        if any(column.casefold() in {"tenant", "tenant_id"} for row in self._rows_data for column in row):
            raise ValidationError(_("Tenant and tenant_id columns are not supported. Remove them before importing."))
        errors, notes = [], []
        for number, row in enumerate(self._rows_data, start=2):
            try:
                mapped = self.map_row(row)
                self._validate_row(mapped, number)
                instance = self._find_existing(mapped)
                if instance is not None:
                    self._authorize_update(instance)
                    if not any(self._differs(instance, key, value) for key, value in mapped.items()):
                        notes.append(
                            _("Row %(row)s: %(reason)s")
                            % {"row": number, "reason": _("record already exists and is unchanged; skipped.")}
                        )
                        continue
                    for key, value in mapped.items():
                        setattr(instance, key, value)
                else:
                    instance = self._create_instance(mapped)
                self._populate_generated_slug(instance)
                instance.full_clean()
            except RowSkipped as skip:
                notes.append(_("Row %(row)s: %(reason)s") % {"row": number, "reason": skip})
            except ValidationError as exc:
                errors.extend(
                    _("Row %(row)s: %(reason)s") % {"row": number, "reason": message}
                    for message in _row_error_messages(exc)
                )
        self.row_notes = notes
        if errors:
            raise ValidationError(errors)
        return cleaned_data

    def _authorize_update(self, instance):
        meta = self.model._meta
        permission = f"{meta.app_label}.change_{meta.model_name}"
        if self.actor is None or not self.actor.has_perm(permission, instance):
            raise RowSkipped(
                _(
                    "This row will be skipped because you do not have permission to update the "
                    "existing record. Ask an administrator for update permission."
                )
            )


class _ParentedReferenceDataBulkImportForm(_ReferenceDataBulkImportForm):
    parent_model = None
    natural_key_only_fields = ("parent",)

    def map_row(self, row):
        parent = str(row.get("parent") or "").strip()
        mapped = super().map_row({key: value for key, value in row.items() if key != "parent"})
        if parent:
            field_label = str(self.model._meta.get_field("parent").verbose_name)
            mapped["parent_id"] = _resolve_natural_key_reference(self.parent_model, parent, field=field_label)
        return mapped


@register_import_form
class CategoryBulkImportForm(_ReferenceDataBulkImportForm):
    model = Category
    update_key = ("name",)
    required_fields = ["name"]
    optional_fields = ["slug", "description", "color", "applies_to", "audit_interval_months"]
    # The target list reuses the category form's own choices (applies_to_flags).
    field_descriptions = {
        "color": _lazy("6-digit hex colour, for example 00ff00. A leading # is accepted."),
        "applies_to": _lazy(
            "Comma-separated targets: asset, accessory, consumable, component. "
            "Targets that are not listed do not apply."
        ),
        "audit_interval_months": _lazy("Positive whole number of months. Leave empty for no required cadence."),
    }

    def map_row(self, row):
        handled = {"color", "applies_to", "audit_interval_months"}
        mapped = super().map_row({key: value for key, value in row.items() if key not in handled})
        color = str(row.get("color") or "").strip()
        if color:
            mapped["color"] = self._clean_color(color)
        applies_to = str(row.get("applies_to") or "").strip()
        if applies_to:
            mapped["applies_to"] = self._parse_applies_to(applies_to)
        if "audit_interval_months" in row:
            mapped["audit_interval_months"] = self._parse_audit_interval(row["audit_interval_months"])
        return mapped

    @staticmethod
    def _clean_color(value):
        color = value[1:] if value.startswith("#") else value
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
            raise ValidationError({"color": _("Enter a valid 6-digit hex color (0-9, A-F).")})
        return color

    @staticmethod
    def _parse_applies_to(value):
        """Targets as a comma list; an export's mapping notation reads back unchanged."""
        from assets.forms.category_form import CategoryForm

        known = [choice for choice, _label in CategoryForm.APPLIES_TO_CHOICES]
        if value.startswith("{"):
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                parsed = None
            if not isinstance(parsed, dict):
                raise ValidationError({"applies_to": _("Enter the targets as a comma-separated list.")})
            requested = [str(key).strip().lower() for key, flag in parsed.items() if flag]
            names = [str(key).strip().lower() for key in parsed]
        else:
            requested = names = [part.strip().lower() for part in value.split(",") if part.strip()]
        unknown = [name for name in names if name not in known]
        if unknown:
            raise ValidationError(
                {
                    "applies_to": _("unknown target '%(value)s'. Use: %(choices)s.")
                    % {"value": ", ".join(unknown), "choices": ", ".join(known)}
                }
            )
        return {choice: choice in requested for choice in known}

    @staticmethod
    def _parse_audit_interval(value):
        text = str(value if value is not None else "").strip()
        if not text:
            return None
        try:
            months = int(text)
        except ValueError:
            months = 0
        if months < 1:
            raise ValidationError({"audit_interval_months": _("Enter a whole number of months greater than zero.")})
        return months

    def _differs(self, instance, key, value):
        if key == "applies_to":
            current = {name for name, flag in (instance.applies_to or {}).items() if flag}
            return current != {name for name, flag in value.items() if flag}
        return super()._differs(instance, key, value)


@register_import_form
class AssetRoleBulkImportForm(_ReferenceDataBulkImportForm):
    model = AssetRole
    update_key = ("name",)
    generated_slug_source = "name"
    required_fields = ["name"]
    optional_fields = ["slug", "description", "color", "allows_components"]


@register_import_form
class RegionBulkImportForm(_ParentedReferenceDataBulkImportForm):
    model = Region
    parent_model = Region
    update_key = ("name",)
    generated_slug_source = "name"
    required_fields = ["name"]
    optional_fields = ["slug", "parent", "description"]


@register_import_form
class SiteGroupBulkImportForm(_ParentedReferenceDataBulkImportForm):
    model = SiteGroup
    parent_model = SiteGroup
    update_key = ("name",)
    generated_slug_source = "name"
    required_fields = ["name"]
    optional_fields = ["slug", "parent", "description"]


@register_import_form
class SiteBulkImportForm(_ReferenceDataBulkImportForm):
    model = Site
    update_key = ("name",)
    generated_slug_source = "name"
    required_fields = ["name"]
    optional_fields = [
        "slug",
        "status",
        "region",
        "group",
        "facility",
        "time_zone",
        "description",
        "physical_address",
        "shipping_address",
        "latitude",
        "longitude",
        "comments",
    ]

    def map_row(self, row):
        if any(str(column).strip().casefold() == "parent" for column in row):
            raise ValidationError(
                {"parent": _("Sites have no parent site; use region or group, or model the hierarchy as locations.")}
            )

        mapped = super().map_row({key: value for key, value in row.items() if key not in {"region", "group"}})
        for field, related_model in (("region", Region), ("group", SiteGroup)):
            value = str(row.get(field) or "").strip()
            if not value:
                continue
            if field == "region":
                unknown_message = _("unknown region '%(value)s'. Regions are never created by a site import.")
            else:
                unknown_message = _("unknown site group '%(value)s'. Site groups are never created by a site import.")
            mapped[field + "_id"] = _resolve_natural_key_reference(
                related_model,
                value,
                field=field,
                unknown_message=unknown_message,
            )
        return mapped

    def _validate_row(self, mapped_data, row_number):
        tenant = get_current_tenant()
        if tenant is None:
            raise ValidationError(_("Select an active tenant before importing sites."))
        super()._validate_row(mapped_data, row_number)
        name = mapped_data.get("name")
        # unscoped: detect a global name collision without exposing the owning tenant.
        if name and Site._base_manager.filter(name=name, deleted_at__isnull=True).exclude(tenant_id=tenant.pk).exists():
            raise ValidationError({"name": _("a site with this name already exists.")})

    def _find_existing(self, mapped):
        tenant = get_current_tenant()
        if tenant is None:
            raise ValidationError(_("Select an active tenant before importing sites."))
        lookup = {}
        for attname in self._key_attnames():
            if attname not in mapped:
                return None
            lookup[attname] = mapped[attname]
        matches = list(Site.objects.filter(tenant_id=tenant.pk, **lookup)[:2])
        if len(matches) > 1:
            raise ValidationError(_("More than one existing object matches the update key."))
        return matches[0] if matches else None


@register_import_form
class ContactRoleBulkImportForm(_ReferenceDataBulkImportForm):
    model = ContactRole
    update_key = ("name",)
    required_fields = ["name"]
    optional_fields = ["slug", "description"]


@register_import_form
class ContactBulkImportForm(_ReferenceDataBulkImportForm):
    model = Contact
    update_key = ("name",)
    required_fields = ["name"]
    optional_fields = ["title", "phone", "email", "web_url", "description", "comments"]

    def map_row(self, row):
        if any(str(column).casefold() == "role" for column in row):
            raise ValidationError(
                _(
                    'Contact roles cannot be imported in this file. Remove the "role" column '
                    "and assign roles after importing contacts."
                )
            )
        return super().map_row(row)

    def _find_existing(self, mapped):
        tenant = get_current_tenant()
        if tenant is None:
            raise ValidationError(_("Select a tenant before importing contacts."))
        lookup = {}
        for attname in self._key_attnames():
            if attname not in mapped:
                return None
            lookup[attname] = mapped[attname]
        matches = list(Contact.objects.filter(tenant_id=tenant.pk, **lookup)[:2])
        if len(matches) > 1:
            raise ValidationError(
                _(
                    "More than one contact with this name exists in the active tenant. "
                    "Rename the duplicates before importing."
                )
            )
        return matches[0] if matches else None

    def _create_instance(self, mapped_data):
        if get_current_tenant() is None:
            raise ValidationError(_("Select a tenant before importing contacts."))
        return super()._create_instance(mapped_data)


@register_import_form
class AccessoryBulkImportForm(BulkImportForm):
    model = Accessory
    required_fields = ["name", "manufacturer"]
    optional_fields = ["category", "supplier", "slug", "part_number", "min_qty", "notes"]


@register_import_form
class ConsumableBulkImportForm(BulkImportForm):
    model = Consumable
    required_fields = ["name", "manufacturer"]
    optional_fields = ["category", "supplier", "slug", "part_number", "min_qty", "notes"]


@register_import_form
class ComponentBulkImportForm(HybridCatalogueImportForm):
    """Component catalogue only: stock and allocations are never written by an import."""

    model = Component
    update_key = ("manufacturer", "name")
    required_fields = ["name", "manufacturer"]
    optional_fields = ["category", "supplier", "part_number", "ean", "min_qty", "allow_overallocate", "notes"]
    rejected_columns = {
        **HybridCatalogueImportForm.rejected_columns,
        **{
            column: "stock is not imported; record stock movements on the component"
            for column in ("qty", "quantity", "location", "stock")
        },
    }
    reference_filters = {"category": lambda queryset: queryset.filter(applies_to__component=True)}


@register_import_form
class LicenseBulkImportForm(BulkImportForm):
    model = License
    required_fields = ["name", "software"]
    optional_fields = [
        "license_type",
        "supplier",
        "product_key",
        "seats",
        "purchase_date",
        "purchase_cost",
        "order_number",
        "expiration_date",
        "notes",
    ]


@register_import_form
class LocationBulkImportForm(BulkImportForm):
    model = Location
    generated_slug_source = "name"
    required_fields = ["name", "site"]
    optional_fields = ["slug", "status", "parent", "facility", "description"]


@register_import_form
class AssetHolderBulkImportForm(BulkImportForm):
    model = AssetHolder
    update_key = ("upn",)
    required_fields = ["first_name", "last_name", "upn"]
    optional_fields = ["email", "description", "comments"]


@register_import_form
class WarrantyBulkImportForm(BulkImportForm):
    """Create-only backfill of warranty records (#829, slice 4).

    The asset is referenced by its tag and resolved inside the active tenant
    only. The supplier is referenced by slug or name and never by numeric id.
    A row repeating a live warranty (same asset, type and dates) is skipped;
    there is no update path, so an import never modifies an existing row.
    A ``tenant`` column is rejected for the whole file.
    """

    model = Warranty
    required_fields = ["asset_tag", "start_date", "end_date"]
    optional_fields = ["warranty_type", "supplier", "cost", "currency", "reference", "terms", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._batch_keys = set()

    def clean(self):
        cleaned_data = super().clean()
        if not self._rows_data:
            return cleaned_data
        if any(str(key).strip().lower() == "tenant" for row in self._rows_data for key in row):
            raise ValidationError(_("A 'tenant' column is not allowed: rows always land in the active tenant."))
        self._batch_keys = set()
        errors, notes = [], []
        for number, row in enumerate(self._rows_data, start=2):
            try:
                mapped, key = self._prepare(row)
                if self._is_duplicate(key):
                    notes.append(_("Row %(row)s: record already exists; skipped.") % {"row": number})
                else:
                    self._batch_keys.add(key)
            except ValidationError as exc:
                errors.extend(f"Row {number}: {message}" for message in exc.messages)
        self.row_notes = notes
        if errors:
            raise ValidationError(errors)
        return cleaned_data

    def import_data(self, request=None):
        self._batch_keys = set()
        return super().import_data(request)

    def _import_row(self, row, row_number):
        mapped, key = self._prepare(row)
        if self._is_duplicate(key):
            raise RowSkipped(_("record already exists; skipped."))
        instance = self.model(**mapped)
        instance.full_clean()
        instance.save()
        self._batch_keys.add(key)
        return ROW_CREATED

    def _is_duplicate(self, key):
        if key in self._batch_keys:
            return True
        asset_id, warranty_type, start_date, end_date = key
        return Warranty.objects.filter(
            asset_id=asset_id, warranty_type=warranty_type, start_date=start_date, end_date=end_date
        ).exists()

    def _prepare(self, row):
        """Validate and resolve one row; return (mapped values, duplicate key)."""
        mapped = self.map_row(row)
        if mapped["end_date"] < mapped["start_date"]:
            raise ValidationError(_("end_date must be on or after start_date."))
        key = (mapped["asset_id"], mapped["warranty_type"], mapped["start_date"], mapped["end_date"])
        return mapped, key

    def map_row(self, row):
        mapped = {}
        tag = str(row.get("asset_tag") or "").strip()
        if not tag:
            raise ValidationError(_("asset_tag is required."))
        mapped["asset_id"] = _resolve_warranty_asset(tag).pk

        for column in ("start_date", "end_date"):
            raw = str(row.get(column) or "").strip()
            if not raw:
                raise ValidationError(_("%(column)s is required.") % {"column": column})
            mapped[column] = _parse_warranty_date(column, raw)

        warranty_type = str(row.get("warranty_type") or "").strip() or WarrantyTypeChoices.HARDWARE
        if warranty_type not in WarrantyTypeChoices.values:
            raise ValidationError(_("warranty_type: select a valid choice."))
        mapped["warranty_type"] = warranty_type

        supplier = str(row.get("supplier") or "").strip()
        if supplier:
            mapped["supplier_id"] = _resolve_warranty_supplier(supplier).pk

        for column in ("cost", "currency", "reference", "terms", "notes"):
            value = str(row.get(column) or "").strip()
            if value:
                mapped[column] = value
        return mapped


@register_import_form
class AssetMaintenanceBulkImportForm(BulkImportForm):
    """Create-only import of completed and cancelled maintenance history (#829, slice 5).

    Asset references use an exact asset tag in the active tenant. Supplier
    references use a scoped slug or name; neither may be resolved by numeric ID.
    Open maintenance and update paths stay in the maintenance workflow.
    """

    model = AssetMaintenance
    update_key = ()
    custom_reference_resolution = True
    required_fields = ["asset_tag", "maintenance_type", "status", "start_date"]
    optional_fields = ["completion_date", "supplier", "performed_by", "cost", "currency", "description", "notes"]
    field_descriptions = {
        "asset_tag": _lazy("Exact asset tag of a non-archived asset in the current tenant."),
        "start_date": _lazy("Date format: YYYY-MM-DD."),
        "completion_date": _lazy(
            "Optional for cancelled maintenance; required when status is completed. Format: YYYY-MM-DD."
        ),
        "status": _lazy("Only completed or cancelled maintenance records can be imported."),
        "supplier": _lazy(
            "Enter a supplier's slug or name from those available for this import; numeric IDs are not accepted."
        ),
    }
    field_accessors = {"supplier": "slug / name"}
    field_choices = {
        "status": [
            (MaintenanceStatusChoices.COMPLETED.value, MaintenanceStatusChoices.COMPLETED.label),
            (MaintenanceStatusChoices.CANCELLED.value, MaintenanceStatusChoices.CANCELLED.label),
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._batch_keys = set()

    def clean(self):
        cleaned_data = super().clean()
        if not self._rows_data:
            return cleaned_data
        if any(str(key).strip().lower() == "tenant" for row in self._rows_data for key in row):
            raise ValidationError(_("A 'tenant' column is not allowed: rows always land in the active tenant."))
        self._batch_keys = set()
        errors, notes = [], []
        for number, row in enumerate(self._rows_data, start=2):
            try:
                _mapped, key = self._prepare(row)
                if self._is_duplicate(key):
                    notes.append(_("Row %(row)s: record already exists; skipped.") % {"row": number})
                else:
                    self._batch_keys.add(key)
            except ValidationError as exc:
                errors.extend(
                    _("Row %(row)s: %(message)s") % {"row": number, "message": message} for message in exc.messages
                )
        self.row_notes = notes
        if errors:
            raise ValidationError(errors)
        return cleaned_data

    def import_data(self, request=None):
        self._batch_keys = set()
        return super().import_data(request)

    def _import_row(self, row, row_number):
        mapped, key = self._prepare(row)
        if self._is_duplicate(key):
            raise RowSkipped(_("record already exists; skipped."))
        self.model(**mapped).save()
        self._batch_keys.add(key)
        return ROW_CREATED

    def _prepare(self, row):
        mapped = self.map_row(row)
        self._validate_history(mapped)
        self._validate_model_values(mapped)
        key = (mapped["asset_id"], mapped["maintenance_type"], mapped["start_date"])
        return mapped, key

    def _is_duplicate(self, key):
        if key in self._batch_keys:
            return True
        tenant = get_current_tenant()
        if tenant is None:
            return False
        asset_id, maintenance_type, start_date = key
        return (
            AssetMaintenance.objects.for_scope(Scope.current())
            .filter(
                asset__tenant_id=tenant.pk,
                asset_id=asset_id,
                maintenance_type=maintenance_type,
                start_date=start_date,
            )
            .exists()
        )

    def _validate_history(self, mapped):
        maintenance_types = {value for value, _label in AssetMaintenance.MAINTENANCE_TYPE_CHOICES}
        if mapped["maintenance_type"] not in maintenance_types:
            raise ValidationError(_("maintenance_type: select a valid choice."))
        if mapped["status"] not in (MaintenanceStatusChoices.COMPLETED, MaintenanceStatusChoices.CANCELLED):
            raise ValidationError(
                _(
                    "status: only completed or cancelled maintenance can be imported; "
                    "manage open maintenance in the application."
                )
            )
        if mapped["status"] == MaintenanceStatusChoices.COMPLETED and mapped.get("completion_date") is None:
            raise ValidationError(_("completion_date: completed maintenance requires a completion date."))
        completion_date = mapped.get("completion_date")
        if completion_date is not None and completion_date < mapped["start_date"]:
            raise ValidationError(_("completion_date must be on or after start_date."))

    def _validate_model_values(self, mapped):
        try:
            self.model(**mapped).full_clean()
        except ValidationError as exc:
            if hasattr(exc, "message_dict"):
                messages = [
                    f"{field}: {message}" if field != NON_FIELD_ERRORS else str(message)
                    for field, field_messages in exc.message_dict.items()
                    for message in field_messages
                ]
                raise ValidationError(messages) from exc
            raise

    def map_row(self, row):
        mapped = {}
        tag = str(row.get("asset_tag") or "").strip()
        if not tag:
            raise ValidationError(_("asset_tag is required."))
        mapped["asset_id"] = _resolve_warranty_asset(tag).pk

        for column in ("maintenance_type", "status", "start_date"):
            raw = str(row.get(column) or "").strip()
            if not raw:
                raise ValidationError(_("%(column)s is required.") % {"column": column})
            mapped[column] = _parse_maintenance_date(column, raw) if column == "start_date" else raw

        completion_date = str(row.get("completion_date") or "").strip()
        if completion_date:
            mapped["completion_date"] = _parse_maintenance_date("completion_date", completion_date)

        supplier = str(row.get("supplier") or "").strip()
        if supplier:
            mapped["supplier_id"] = _resolve_maintenance_supplier(supplier).pk

        for column in ("performed_by", "cost", "currency", "description", "notes"):
            value = str(row.get(column) or "").strip()
            if value:
                mapped[column] = value
        return mapped


def _resolve_warranty_asset(tag):
    """Return the live asset with this tag in the active tenant, or fail the row."""
    tenant = get_current_tenant()
    queryset = Asset.objects.for_scope(Scope.current())
    asset = queryset.filter(asset_tag=tag, tenant=tenant).first() if tenant is not None else None
    if asset is None:
        raise ValidationError(
            _("asset_tag: no non-archived asset with tag '%(tag)s' exists in this tenant.") % {"tag": tag}
        )
    return asset


def _resolve_warranty_supplier(value):
    """Resolve a supplier by slug or name in scope; numeric ids are not accepted."""
    queryset = Supplier.objects.for_scope(Scope.current())
    supplier = (
        queryset.filter(slug=value).first()
        or queryset.filter(name=value).first()
        or queryset.filter(name__iexact=value).first()
    )
    if supplier is None:
        raise ValidationError(_("supplier: no matching supplier in the active tenant."))
    return supplier


def _parse_warranty_date(column, raw):
    try:
        return Warranty._meta.get_field(column).to_python(raw)
    except ValidationError:
        raise ValidationError(_("%(column)s: enter a valid date (YYYY-MM-DD).") % {"column": column}) from None


def _resolve_maintenance_supplier(value):
    """Resolve a supplier by the approved natural-key order inside the active scope."""
    queryset = Supplier.objects.for_scope(Scope.current())
    for lookup in ("slug", "name", "slug__iexact", "name__iexact"):
        matches = list(queryset.filter(**{lookup: value})[:2])
        if len(matches) > 1:
            raise ValidationError(_("supplier: '%(value)s' matches more than one supplier.") % {"value": value})
        if matches:
            return matches[0]
    raise ValidationError(
        _("supplier: no supplier matching '%(value)s' by slug or name is available for this import.") % {"value": value}
    )


def _parse_maintenance_date(column, raw):
    try:
        return AssetMaintenance._meta.get_field(column).to_python(raw)
    except ValidationError:
        raise ValidationError(_("%(column)s: enter a valid date (YYYY-MM-DD).") % {"column": column}) from None
