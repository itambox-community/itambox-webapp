from django.apps import AppConfig


class SoftwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "software"

    def ready(self):
        import software.import_forms  # noqa: F401  # registers the curated import form
        import software.search  # noqa
