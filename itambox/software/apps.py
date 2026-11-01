from django.apps import AppConfig


class SoftwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "software"

    def ready(self):
        # inline import: app-registry: software.search imports software models to register its search providers
        import software.search  # noqa

        # inline import: app-registry: curated import forms load only after the app registry is ready.
        import software.import_forms  # noqa: F401
