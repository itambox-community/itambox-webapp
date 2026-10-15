import json
from functools import lru_cache
from typing import NamedTuple

import jsonschema
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _

from assets.models import Asset, AssetRole, AssetType, Category, Manufacturer, Supplier, Warranty, WarrantyTypeChoices
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
    register_import_form,
)
from core.managers import Scope
from core.slugs import generate_unique_slug
from inventory.models import Accessory, Component, Consumable
from licenses.models import License
from organization.models import AssetHolder, Location
from organization.services.catalogue_authorization import has_provider_catalogue_permission

# These forms are declarative: the base BulkImportForm.map_row maps scalar
# columns and resolves ForeignKey columns (by id / slug / name) automatically.
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


def _resolve_warranty_asset(tag):
    """Return the live asset with this tag in the active tenant, or fail the row."""
    tenant = get_current_tenant()
    queryset = Asset.objects.for_scope(Scope.current())
    asset = queryset.filter(asset_tag=tag, tenant=tenant).first() if tenant is not None else None
    if asset is None:
        raise ValidationError(_("asset_tag: no asset with this tag exists in the active tenant."))
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
