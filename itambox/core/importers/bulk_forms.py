# This file is adapted from NetBox (https://github.com/netbox-community/netbox).
# Copyright (c) DigitalOcean, LLC.
# Licensed under the Apache License, Version 2.0.

import csv
import io
import logging
from typing import NamedTuple

import yaml
from django import forms
from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, FieldDoesNotExist, ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from core.context import get_current_request_id, get_current_tenant, get_current_user
from core.data_transfer import policy_for
from core.managers import Scope
from core.mixins import AutoSlugMixin
from core.slugs import generate_unique_slug

logger = logging.getLogger(__name__)


class ImportResult(NamedTuple):
    """Tuple-compatible result that distinguishes row failures from file/task failures."""

    imported_count: int
    errors: list[str]


def _import_log_extra(*, operation, row_number=None, exception_type=None):
    tenant = get_current_tenant()
    user = get_current_user()
    request_id = get_current_request_id()
    context = {
        "operation": operation,
        "tenant_id": getattr(tenant, "pk", None),
        "actor_id": getattr(user, "pk", None),
        "request_id": str(request_id) if request_id else None,
    }
    if row_number is not None:
        context["row_number"] = row_number
    if exception_type is not None:
        context["exception_type"] = exception_type
    return {"import_context": context}


def _row_error_messages(exc):
    """Flatten a ``ValidationError`` into operator-facing row messages.

    A field-keyed error (``full_clean()`` reports them as a dict) names the
    failing field, so an operator can tell which column to fix; the flattened
    ``messages`` property drops the field name and leaves only the text.
    """
    if hasattr(exc, "message_dict"):
        messages = []
        for field, field_messages in exc.message_dict.items():
            prefix = "" if field == NON_FIELD_ERRORS else f"{field}: "
            messages.extend(f"{prefix}{message}" for message in field_messages)
        return messages
    return [str(message) for message in exc.messages]


def _normalize_csv_row(row, row_number):
    if None in row:
        raise ValidationError(_("Row %(row)s: CSV row has more cells than headers.") % {"row": row_number})
    return {key.strip() if key else "": (value.strip() if value else "") for key, value in row.items()}


def _validate_yaml_rows(rows, start_row=1):
    for row_number, row in enumerate(rows, start=start_row):
        if not isinstance(row, dict):
            raise ValidationError(
                _("Row %(row)s: YAML data elements must be mappings (key-value pairs).") % {"row": row_number}
            )


# Upper bound on rows accepted in a single bulk import. Beyond this, the request
# is rejected so a single submission can't exhaust memory / hold a transaction
# open indefinitely; large datasets should be split into batches.
MAX_IMPORT_ROWS = getattr(settings, "MAX_IMPORT_ROWS", 10000)

# Fields that are never user-importable: set from request context, computed by
# background tasks, or framework-managed. Excluded from the dynamic field
# enumeration, row mapper, and the "Field Options" help table.
IMPORT_EXCLUDED_FIELDS = frozenset(
    {
        "tenant",
        "deleted_at",
        "created_at",
        "updated_at",
        "custom_field_data",
        "current_book_value",
        "depreciation_updated_at",
        "disposed_at",
        "disposal_value",
        "last_audited",
        "last_audited_by",
    }
)


class ImportNotAllowed(Exception):
    """The model has no curated, declared import form (undeclared means denied)."""


# Registry of curated BulkImportForms keyed by model. A model is importable only
# when it is registered here AND declared ``import_=True`` in
# ``core.data_transfer.DECLARATIONS``; there is no reflection-built fallback.
_IMPORT_FORM_REGISTRY = {}


def register_import_form(form_cls):
    """Decorator: register a curated BulkImportForm for its model."""
    if getattr(form_cls, "model", None) is not None:
        _IMPORT_FORM_REGISTRY[form_cls.model] = form_cls
    return form_cls


def get_registered_import_form(model):
    """Return the curated BulkImportForm for *model*, or None."""
    return _IMPORT_FORM_REGISTRY.get(model)


def is_model_importable(model):
    """True only for a model declared importable that also has a curated form.

    Single source for the import view, the background task, and the import
    button in the nav and list header. Export visibility is a separate
    declaration (``core.data_transfer``).
    """
    if model is None or not policy_for(model).import_:
        return False
    return get_registered_import_form(model) is not None


def get_import_form_class(model):
    """Return the curated import form for ``model`` or raise ``ImportNotAllowed``."""
    if not is_model_importable(model):
        raise ImportNotAllowed(getattr(getattr(model, "_meta", None), "label_lower", repr(model)))
    return get_registered_import_form(model)


def _model_has_concrete_field(model, name):
    """True only if *name* is a real (concrete) model field — guards FK lookups
    so we never build a queryset filter on a Python property/attribute."""
    try:
        model._meta.get_field(name)
        return True
    except FieldDoesNotExist:
        return False


def resolve_related(related_model, value):
    """Resolve a raw import cell to a related-object PK by id, then slug/name/
    model/username/upn (case-sensitive then case-insensitive). Returns the PK, or
    the original value (so model validation surfaces a clear per-row error)."""
    obj = None
    if value.isdigit():
        obj = related_model.objects.filter(pk=int(value)).first()
    lookup_fields = ["slug", "name", "model", "username", "upn"]
    if not obj:
        for lookup in lookup_fields:
            if _model_has_concrete_field(related_model, lookup):
                obj = related_model.objects.filter(**{lookup: value}).first()
                if obj:
                    break
    if not obj:
        for lookup in lookup_fields:
            if _model_has_concrete_field(related_model, lookup):
                obj = related_model.objects.filter(**{f"{lookup}__iexact": value}).first()
                if obj:
                    break
    return obj.pk if obj else value


class BulkImportForm(forms.Form):
    """
    Base form for CSV/TSV or YAML bulk import of objects.
    Subclasses define their model, required fields, optional fields,
    and field mapping logic.

    Usage:
        class AssetBulkImportForm(BulkImportForm):
            model = Asset
            required_fields = ['name', 'asset_tag']
            optional_fields = ['serial_number', 'purchase_date']

            def map_row(self, row):
                return {k: row.get(k, '') for k in self.field_names}
    """

    model = None
    required_fields = []
    optional_fields = []
    # Updates are opt-in. ``update_key`` names the model fields that identify an
    # existing row (a natural key, never the raw pk). A row whose key matches an
    # in-scope object updates it, and only for an actor holding ``change_<model>``
    # on that object. Without a key every row creates a new object.
    update_key = ()
    # The authenticated actor of the import. Set by the task; an unset actor can
    # never update.
    actor = None

    active_tab = forms.CharField(widget=forms.HiddenInput(), initial="upload", required=False)
    import_format = forms.ChoiceField(
        choices=[("csv", "CSV"), ("yaml", "YAML")],
        initial="csv",
        widget=forms.RadioSelect(attrs={"class": "form-selectgroup-input"}),
        required=False,
    )
    csv_file = forms.FileField(
        label=_("File"),
        help_text=_("Upload a CSV or YAML file with headers matching the field names."),
        widget=forms.FileInput(attrs={"class": "form-control"}),
        required=False,
    )
    import_text = forms.CharField(
        label=_("Direct Data Input"),
        help_text=_("Paste CSV or YAML data matching the field names."),
        widget=forms.Textarea(attrs={"class": "form-control font-monospace", "rows": 8}),
        required=False,
    )
    delimiter = forms.ChoiceField(
        label=_("CSV Delimiter"),
        choices=[(",", _("Comma (,)")), ("\t", _("Tab")), (";", _("Semicolon (;)"))],
        initial=",",
        widget=forms.Select(attrs={"class": "form-select", "data-tom-select": ""}),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.imported_count = 0
        self.errors_list = []
        self._rows_data = []

    @property
    def field_names(self):
        return [name for name in [*self.required_fields, *self.optional_fields] if name not in IMPORT_EXCLUDED_FIELDS]

    def clean_csv_file(self):
        return self.cleaned_data.get("csv_file")

    def clean(self):
        cleaned_data = super().clean()
        import_format = cleaned_data.get("import_format") or self.data.get("import_format", "csv")

        csv_file = cleaned_data.get("csv_file")
        import_text = cleaned_data.get("import_text") or ""
        active_tab = cleaned_data.get("active_tab") or self.data.get("active_tab", "upload")

        # Auto-detect active tab based on which data is provided
        if csv_file:
            active_tab = "upload"
        elif import_text.strip():
            active_tab = "editor"

        self._rows_data = []
        raw_data = ""

        if active_tab == "upload":
            if not csv_file:
                raise ValidationError(_("Please select a file to upload."))
            try:
                raw_data = csv_file.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    csv_file.seek(0)
                    raw_data = csv_file.read().decode("latin-1")
                except (AttributeError, UnicodeDecodeError) as exc:
                    raise ValidationError(
                        _("Unable to decode file. Please upload a valid text-based CSV or YAML file.")
                    ) from exc
        else:
            if not import_text.strip():
                raise ValidationError(_("Please paste data in the editor tab."))
            raw_data = import_text

        if import_format == "csv":
            delimiter = cleaned_data.get("delimiter") or ","
            try:
                reader = csv.DictReader(io.StringIO(raw_data), delimiter=delimiter)
                rows = list(reader)
            except csv.Error:
                raise ValidationError(_("Failed to parse CSV data.")) from None

            if not rows:
                raise ValidationError(_("CSV data is empty."))

            if len(rows) > MAX_IMPORT_ROWS:
                raise ValidationError(
                    _("Import exceeds the maximum of {max} rows; split into smaller batches.").format(
                        max=MAX_IMPORT_ROWS
                    )
                )

            headers = set(rows[0].keys())
            headers = {h.strip() if h else "" for h in headers}
            missing_required = [f for f in self.required_fields if f not in headers]
            if missing_required:
                raise ValidationError(
                    _("Missing required columns: {columns}").format(columns=", ".join(missing_required))
                )

            self._rows_data = []
            for row_number, row in enumerate(rows, start=2):
                self._rows_data.append(_normalize_csv_row(row, row_number))

        elif import_format == "yaml":
            try:
                parsed_yaml = yaml.safe_load(raw_data)
            except yaml.YAMLError:
                raise ValidationError(_("Failed to parse YAML data.")) from None

            if not parsed_yaml:
                raise ValidationError(_("YAML document is empty."))

            if not isinstance(parsed_yaml, list):
                if isinstance(parsed_yaml, dict):
                    parsed_yaml = [parsed_yaml]
                else:
                    raise ValidationError(_("YAML input must be a list of objects or a single object mapping."))

            if not parsed_yaml or not isinstance(parsed_yaml[0], dict):
                _validate_yaml_rows(parsed_yaml[:1])

            if len(parsed_yaml) > MAX_IMPORT_ROWS:
                raise ValidationError(
                    _("Import exceeds the maximum of {max} rows; split into smaller batches.").format(
                        max=MAX_IMPORT_ROWS
                    )
                )

            _validate_yaml_rows(parsed_yaml[1:], start_row=2)

            headers = set(parsed_yaml[0].keys())
            missing_required = [f for f in self.required_fields if f not in headers]
            if missing_required:
                raise ValidationError(_("Missing required fields: {fields}").format(fields=", ".join(missing_required)))

            self._rows_data = []
            for row in parsed_yaml:
                cleaned_row = {}
                for k, v in row.items():
                    if k is None:
                        continue
                    key_str = str(k).strip()
                    if v is None:
                        cleaned_row[key_str] = ""
                    elif isinstance(v, bool):
                        cleaned_row[key_str] = str(v)
                    elif isinstance(v, (int, float)):
                        cleaned_row[key_str] = str(v)
                    else:
                        cleaned_row[key_str] = str(v).strip()
                self._rows_data.append(cleaned_row)

        return cleaned_data

    def import_data(self, request=None):
        if not self.model:
            raise NotImplementedError("BulkImportForm subclass must define a `model` attribute.")

        if not self._rows_data:
            return ImportResult(0, [_("No data to import.")])

        imported = 0
        errors = []

        for i, row in enumerate(self._rows_data, start=2):
            try:
                with transaction.atomic():
                    self._import_row(row, i)
                imported += 1
            except ValidationError as e:
                errors.append(f"Row {i}: {'; '.join(_row_error_messages(e))}")
            except Exception as exc:
                # broad except: task-isolation: one malformed import row must not abort the reviewed batch
                extra = _import_log_extra(
                    operation="row.persist",
                    row_number=i,
                    exception_type=type(exc).__name__,
                )
                logger.error("Import row failed import_context=%s", extra["import_context"], extra=extra)
                errors.append(str(_("Row %(row)s: could not be imported due to an unexpected error.") % {"row": i}))

        self.imported_count = imported
        self.errors_list = errors
        return ImportResult(imported, errors)

    def _import_row(self, row, row_number):
        mapped = self.map_row(row)
        self._validate_row(mapped, row_number)

        instance = self._find_existing(mapped)
        if instance is not None:
            self._authorize_update(instance)
            if hasattr(instance, "snapshot"):
                instance.snapshot()
            for key, val in mapped.items():
                setattr(instance, key, val)
        else:
            instance = self._create_instance(mapped)

        self._populate_generated_slug(instance)
        if hasattr(instance, "full_clean"):
            instance.full_clean()
        instance.save()

    def _populate_generated_slug(self, instance):
        """Fill in a configured model slug before row validation.

        ``full_clean()`` runs before ``save()``, so validation sees a blank slug
        for a row that legitimately omits the column and rejects it. Models with
        ``AutoSlugMixin`` use their declared source; curated forms may declare
        ``generated_slug_source`` for supported models without that mixin. An
        explicit slug is left untouched by the shared generator.
        """
        if isinstance(instance, AutoSlugMixin):
            slug_source = instance.slug_source
        else:
            slug_source = getattr(self, "generated_slug_source", None)
        if slug_source:
            generate_unique_slug(instance, slug_source)

    def _key_attnames(self):
        return [self.model._meta.get_field(name).attname for name in self.update_key]

    def _find_existing(self, mapped):
        """Return the in-scope object matching the declared natural key, or None."""
        if not self.update_key:
            return None
        lookup = {}
        for attname in self._key_attnames():
            if attname not in mapped:
                return None
            lookup[attname] = mapped[attname]
        queryset = self.model.objects
        if hasattr(queryset, "for_scope"):
            queryset = queryset.for_scope(Scope.current())
        matches = list(queryset.filter(**lookup)[:2])
        if len(matches) > 1:
            raise ValidationError(_("More than one existing object matches the update key."))
        return matches[0] if matches else None

    def _authorize_update(self, instance):
        meta = self.model._meta
        perm = f"{meta.app_label}.change_{meta.model_name}"
        actor = self.actor
        if actor is None or not actor.has_perm(perm, instance):
            raise ValidationError(_("You do not have permission to update existing objects."))

    def map_row(self, row):
        """Map an import row dict to model field values.

        Scalar columns are passed through (stripped); ForeignKey columns are
        resolved to a PK by id / slug / name (see ``resolve_related``). The pk
        column is never mapped: an ``id`` column (as written by an export) is
        ignored, and updates match on ``update_key`` only. Subclasses rarely
        need to override this: declare ``required_fields`` / ``optional_fields``.
        """
        mapped = {}
        if not self.model:
            return mapped

        for k in self.field_names:
            if k in IMPORT_EXCLUDED_FIELDS or k not in row or row[k] is None:
                continue
            val = str(row[k]).strip()
            if not val:
                continue
            try:
                field = self.model._meta.get_field(k)
            except FieldDoesNotExist:
                # Not a real field on this model: skip rather than crash on save.
                continue
            if field.is_relation and field.many_to_one:
                mapped[field.attname] = resolve_related(field.related_model, val)
            else:
                mapped[k] = val
        return mapped

    def _validate_row(self, mapped_data, row_number):
        """Validate a mapped row. Override for custom validation."""
        for name in self.required_fields:
            try:
                key = self.model._meta.get_field(name).attname if self.model else name
            except FieldDoesNotExist:
                key = name
            if not mapped_data.get(key):
                raise ValidationError(_('Row %(row)s: "%(field)s" is required.') % {"row": row_number, "field": name})

    def _create_instance(self, mapped_data):
        """Bind a new tenant-owned row to the active import tenant before validation."""
        tenant = get_current_tenant()
        tenant_field = next((field for field in self.model._meta.fields if field.name == "tenant"), None)
        if tenant is not None and tenant_field is not None:
            mapped_data = {**mapped_data, tenant_field.attname: tenant.pk}
        return self.model(**mapped_data)
