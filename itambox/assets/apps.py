from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

from itambox.labels import LabelField, LabelSpec, label_registry
from itambox.registry import registry


class AssetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "assets"

    def ready(self):
        # Import signals
        # Import search indexes to register them
        # inline import: app-registry: curated import forms load only after the app registry is ready.
        import assets.forms.import_forms
        import assets.search
        import assets.signals  # noqa: F401  # registers signal receivers

        # inline import: app-registry: attach dynamic Asset/AssetType definitions to the shared value validator.
        from assets.customfields import asset_custom_field_definitions, asset_type_custom_field_definitions

        registry.register_custom_field_data_definition_provider(
            self.get_model("AssetType"), asset_type_custom_field_definitions
        )
        registry.register_custom_field_data_definition_provider(self.get_model("Asset"), asset_custom_field_definitions)

        # inline imports: app-registry: the archive registry is populated once models are loaded.
        from assets.archive_services import archive_asset, restore_asset
        from core.archive_handlers import ArchiveBehaviour, ArchiveRelation, register_archive_handler

        register_archive_handler(
            self.get_model("Asset")._meta.label_lower,
            archive=archive_asset,
            restore=restore_asset,
            relations=(
                ArchiveRelation(
                    "assets.assetassignment.asset",
                    ArchiveBehaviour.REFUSE,
                    "while active; closed assignments are archived with the asset",
                ),
                ArchiveRelation(
                    "assets.assetassignment.assigned_asset",
                    ArchiveBehaviour.REFUSE,
                    "while active; closed rows stay as evidence",
                ),
                ArchiveRelation("assets.assetdisposal.asset", ArchiveBehaviour.KEEP, "disposals stay as evidence"),
                ArchiveRelation(
                    "assets.assetmaintenance.asset",
                    ArchiveBehaviour.REFUSE,
                    "while scheduled or in progress; finished records are archived with the asset",
                ),
                ArchiveRelation(
                    "assets.assetrequest.asset",
                    ArchiveBehaviour.DETACH,
                    "open requests are cancelled; closed requests stay as evidence",
                ),
                ArchiveRelation(
                    "assets.assetrequest.assigned_asset",
                    ArchiveBehaviour.DETACH,
                    "open requests are cancelled; closed requests stay as evidence",
                ),
                ArchiveRelation(
                    "assets.assetreservation.asset",
                    ArchiveBehaviour.REFUSE,
                    "while pending or active; other reservations are archived with the asset",
                ),
                ArchiveRelation(
                    "assets.warranty.asset", ArchiveBehaviour.ARCHIVE, "warranties are archived with the asset"
                ),
                ArchiveRelation("compliance.assetaudit.asset", ArchiveBehaviour.KEEP, "audit rows stay as evidence"),
                ArchiveRelation(
                    "compliance.custodyreceipt.asset", ArchiveBehaviour.KEEP, "custody receipts stay as evidence"
                ),
                ArchiveRelation(
                    "inventory.accessoryassignment.assigned_asset",
                    ArchiveBehaviour.REFUSE,
                    "while the accessory is checked out to the asset",
                ),
                ArchiveRelation(
                    "inventory.componentallocation.assigned_asset",
                    ArchiveBehaviour.REFUSE,
                    "while the component is allocated to the asset",
                ),
                ArchiveRelation(
                    "inventory.consumableassignment.assigned_asset",
                    ArchiveBehaviour.REFUSE,
                    "while the consumable is issued to the asset",
                ),
                ArchiveRelation(
                    "licenses.licenseseatassignment.asset",
                    ArchiveBehaviour.REFUSE,
                    "while a seat is held; released seats stay as evidence",
                ),
                ArchiveRelation(
                    "procurement.contract.assets",
                    ArchiveBehaviour.DETACH,
                    "the asset is removed from covering contracts",
                ),
                ArchiveRelation(
                    "software.installedsoftware.asset",
                    ArchiveBehaviour.ARCHIVE,
                    "installed software is archived with the asset",
                ),
                ArchiveRelation("subscriptions", ArchiveBehaviour.DETACH, "subscriptions covering the asset are ended"),
                ArchiveRelation("journal_entries", ArchiveBehaviour.KEEP, "journal entries stay as evidence"),
                ArchiveRelation("file_attachments", ArchiveBehaviour.KEEP, "attachments stay as evidence"),
                ArchiveRelation("image_attachments", ArchiveBehaviour.KEEP, "attachments stay as evidence"),
            ),
        )

        # Asset labels print the tag as the short identifier (Code 128 / Code 39)
        # and keep the ``asset`` template alias and the pre-1.0 itambox://asset/<pk>
        # scheme of the former asset-only engine.
        label_registry.register(
            self.get_model("Asset"),
            LabelSpec(
                fields=(
                    LabelField("asset_tag", _("Asset Tag"), lambda asset: asset.asset_tag),
                    LabelField("name", _("Name"), lambda asset: asset.name),
                    LabelField("serial_number", _("Serial Number"), lambda asset: asset.serial_number),
                    LabelField("location", _("Location"), lambda asset: asset.location),
                    LabelField("status", _("Status"), lambda asset: asset.status),
                ),
                title=lambda asset: asset.name,
                barcode_value=lambda asset: asset.asset_tag,
                template_alias="asset",
                legacy_scheme="asset",
            ),
        )
