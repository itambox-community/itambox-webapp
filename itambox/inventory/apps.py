from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"

    def ready(self):
        # inline import: app-registry: curated import forms load only after the app registry is ready.
        import inventory.forms.import_forms  # noqa: F401  # registers the kit importer
        import inventory.search  # noqa: F401  # registers search providers

        # inline import: app-registry: register handlers only after inventory models are loaded.
        from core.purge_handlers import register_purge_handler
        from inventory.services import ASSIGNMENT_MODELS, purge_inventory_assignment

        for model in ASSIGNMENT_MODELS:
            register_purge_handler(model._meta.label_lower, purge_inventory_assignment)

        # inline imports: app-registry: the archive registry is populated once models are loaded.
        from core.archive_handlers import ArchiveBehaviour, ArchiveRelation, register_archive_handler
        from inventory.archive_services import archive_kit, restore_kit
        from inventory.models import Kit

        register_archive_handler(
            Kit._meta.label_lower,
            archive=archive_kit,
            restore=restore_kit,
            relations=(
                ArchiveRelation(
                    "inventory.kititem.kit",
                    ArchiveBehaviour.ARCHIVE,
                    "archived with the kit through their own save(); restore brings them back",
                ),
                ArchiveRelation("journal_entries", ArchiveBehaviour.KEEP, "journal entries stay as evidence"),
            ),
        )

        # inline imports: app-registry: the inventory item aggregates (step 4 of #619).
        from inventory.item_archive_services import AGGREGATES, HANDLERS, archive_relations

        for aggregate in AGGREGATES:
            archive_handler, restore_handler = HANDLERS[aggregate.model._meta.label_lower]
            register_archive_handler(
                aggregate.model._meta.label_lower,
                archive=archive_handler,
                restore=restore_handler,
                relations=archive_relations(aggregate),
            )
