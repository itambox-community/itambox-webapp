from core.importers.bulk_forms import HybridCatalogueImportForm, register_import_form
from software.models import Software


@register_import_form
class SoftwareBulkImportForm(HybridCatalogueImportForm):
    """Software catalogue rows; the key is the name within the active tenant."""

    model = Software
    update_key = ("name",)
    required_fields = ["name", "manufacturer"]
    optional_fields = ["version", "category", "license_type", "website", "description"]
