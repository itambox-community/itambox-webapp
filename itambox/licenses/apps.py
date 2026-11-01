from django.apps import AppConfig


class LicensesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "licenses"

    def ready(self):
        # inline import: app-registry: licenses.search imports license models to register its search providers
        import licenses.search  # noqa

        # inline imports: app-registry: the archive registry is populated once models are loaded.
        from core.archive_handlers import ArchiveBehaviour, ArchiveRelation, register_archive_handler
        from licenses.archive_services import archive_license, restore_license
        from licenses.models import License

        register_archive_handler(
            License._meta.label_lower,
            archive=archive_license,
            restore=restore_license,
            relations=(
                ArchiveRelation(
                    "licenses.licenseseatassignment.license",
                    ArchiveBehaviour.ARCHIVE,
                    "refused while a seat is held; a released seat is already archived and stays so",
                ),
                ArchiveRelation("inventory.kititem.license", ArchiveBehaviour.REFUSE, "while a live kit lists it"),
                ArchiveRelation(
                    "procurement.purchaseorderline.license",
                    ArchiveBehaviour.KEEP,
                    "purchase order lines stay as evidence",
                ),
                ArchiveRelation("journal_entries", ArchiveBehaviour.KEEP, "journal entries stay as evidence"),
                ArchiveRelation("file_attachments", ArchiveBehaviour.KEEP, "attachments stay as evidence"),
                ArchiveRelation("image_attachments", ArchiveBehaviour.KEEP, "attachments stay as evidence"),
            ),
        )
