from typing import cast

from django.core.exceptions import PermissionDenied, ValidationError

from assets.models import Asset, AssetType, Manufacturer
from assets.services.specifications.commands import create_asset_type, preview_asset_type_create
from assets.services.specifications.contracts import (
    AssetTypePreviewDTO,
    FieldsetSelectionDTO,
    SpecificationPatchDTO,
)
from assets.services.specifications.messages import specification_message
from assets.specification_adapters import (
    actor_context_for_user,
    native_asset_type_create_input,
    owner_id_from_result,
    require_command_success,
)
from core.importers.bulk_forms import BulkImportForm, register_import_form
from inventory.models import Accessory, Consumable
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


@register_import_form
class AssetTypeBulkImportForm(BulkImportForm):
    model = AssetType
    required_fields = ["manufacturer", "model"]
    optional_fields = ["slug", "part_number", "category", "asset_role", "description", "comments"]

    @classmethod
    def can_import(cls, actor: object) -> bool:
        return has_provider_catalogue_permission(actor, "assets.add_assettype")

    def _import_row(self, row, row_number):
        mapped = self.map_row(row)
        self._validate_row(mapped, row_number)
        instance = self._create_instance(mapped)
        self._populate_generated_slug(instance)
        instance.full_clean()

        if self.actor is None:
            raise PermissionDenied("An authenticated actor is required to import Asset Types.")
        actor = actor_context_for_user(self.actor)
        native = native_asset_type_create_input(
            {
                "manufacturer": instance.manufacturer,
                "model": instance.model,
                "slug": instance.slug,
                "part_number": instance.part_number,
                "ean": instance.ean,
                "region": instance.region,
                "configuration": instance.configuration,
                "eol_months": instance.eol_months,
                "category": instance.category,
                "asset_role": instance.asset_role,
                "depreciation": instance.depreciation,
                "description": instance.description,
                "comments": instance.comments,
                "requestable": instance.requestable,
                "tags": (),
            }
        )
        fieldsets = FieldsetSelectionDTO(presence="omitted", identities=())
        patch = SpecificationPatchDTO(set_values={}, clear_keys=())
        preview = cast(
            AssetTypePreviewDTO,
            require_command_success(
                preview_asset_type_create(
                    actor=actor,
                    native=native,
                    fieldsets=fieldsets,
                    patch=patch,
                )
            ),
        )
        if preview.issues:
            raise ValidationError("; ".join(specification_message(issue.message_key) for issue in preview.issues))
        result = create_asset_type(
            actor=actor,
            native=native,
            fieldsets=fieldsets,
            patch=patch,
            preview_token=preview.preview_token,
            expected_definition_revision=preview.expected_definition_revision,
            expected_category_default_snapshot_revision=preview.expected_category_default_snapshot_revision,
        )
        owner_id_from_result(result)


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
