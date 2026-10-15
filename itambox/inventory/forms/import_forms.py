"""Bulk import of kits with an explicit, atomic ``items`` contract."""

from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from core.context import get_current_tenant
from core.importers.bulk_forms import ROW_CREATED, ROW_UPDATED, BulkImportForm, RowSkipped, register_import_form
from core.managers import Scope
from inventory.models import Kit, KitItem

# kind -> (model label, KitItem foreign key, lookup fields tried in order)
_ITEM_KINDS = {
    "asset_type": ("assets.AssetType", "asset_type", ("slug",)),
    "accessory": ("inventory.Accessory", "accessory", ("slug", "name")),
    "consumable": ("inventory.Consumable", "consumable", ("slug", "name")),
    "component": ("inventory.Component", "component", ("slug", "name")),
    "license": ("licenses.License", "license", ("name",)),
}
_REPLACE_PERMS = ("change_kit", "add_kititem", "delete_kititem")


def _parse_entry(entry):
    """Normalise one declared item (``kind:reference*qty`` string or mapping) to ``(kind, ref, qty)``."""
    if isinstance(entry, dict):
        kind = str(entry.get("type") or entry.get("kind") or "").strip().lower()
        reference = str(entry.get("ref") or entry.get("reference") or entry.get("name") or "").strip()
        qty_raw = str(entry.get("qty", 1)).strip()
    else:
        kind, _sep, rest = str(entry).strip().partition(":")
        kind = kind.strip().lower()
        reference, qty_raw = rest.strip(), "1"
        head, star, tail = rest.rpartition("*")
        if star and tail.strip().isdigit():
            reference, qty_raw = head.strip(), tail.strip()
    if kind not in _ITEM_KINDS:
        raise ValidationError(_("Unknown item type '%(kind)s'.") % {"kind": kind})
    if not reference:
        raise ValidationError(_("Item of type '%(kind)s' has no reference.") % {"kind": kind})
    if not qty_raw.isdigit() or int(qty_raw) < 1:
        raise ValidationError(_("Item '%(ref)s' has an invalid quantity.") % {"ref": reference})
    return kind, reference, int(qty_raw)


def _declared_entries(cell):
    """Split an ``items`` cell (CSV text or YAML list) into raw entries; None means untouched."""
    if isinstance(cell, list):
        return cell or None
    text = str(cell or "").strip()
    if not text:
        return None
    return [part for part in text.replace("\n", ";").split(";") if part.strip()]


def _owned_or_shared(obj, kind, tenant):
    if not hasattr(obj, "tenant_id"):
        return True
    if kind == "license":
        return obj.tenant_id == tenant.pk
    return obj.tenant_id in (None, tenant.pk)


def _resolve_item(kind, reference, tenant):
    """Resolve one reference inside the importing tenant's own or shared catalogue; never creates."""
    label, _fk, lookups = _ITEM_KINDS[kind]
    queryset = apps.get_model(label).objects
    if hasattr(queryset, "for_scope"):
        queryset = queryset.for_scope(Scope.current())
    for lookup in lookups:
        matches = [obj for obj in queryset.filter(**{lookup: reference})[:5] if _owned_or_shared(obj, kind, tenant)]
        if len(matches) > 1:
            raise ValidationError(_("Item '%(ref)s' is ambiguous.") % {"ref": reference})
        if matches:
            return matches[0]
    raise ValidationError(_("Item '%(ref)s' was not found in this tenant's catalogue.") % {"ref": reference})


def _current_items(kit):
    current = set()
    for item in kit.items.all():
        for kind, (_label, fk, _lookups) in _ITEM_KINDS.items():
            if getattr(item, f"{fk}_id"):
                current.add((kind, getattr(item, f"{fk}_id"), item.qty))
    return current


@register_import_form
class KitBulkImportForm(BulkImportForm):
    """Kit import. Items are one column, validated as a set and applied atomically per row."""

    model = Kit
    structured_fields = ("items",)
    required_fields = ["name"]
    optional_fields = ["description", "items"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._withheld = []

    def clean(self):
        cleaned_data = super().clean()
        if not self._rows_data:
            return cleaned_data
        if any("tenant" in row for row in self._rows_data):
            raise ValidationError(_("A tenant column is not allowed; kits are imported into the active tenant."))
        errors = []
        for number, row in enumerate(self._rows_data, start=2):
            try:
                self._plan_row(row)
            except ValidationError as exc:
                errors.extend(f"Row {number}: {message}" for message in exc.messages)
        if errors:
            raise ValidationError(errors)
        return cleaned_data

    # -- planning (read only; preview and import share it) -----------------

    def _plan_row(self, row):
        tenant = get_current_tenant()
        if tenant is None:
            raise ValidationError(_("Kits can only be imported into an active tenant."))
        name = str(row.get("name") or "").strip()
        if not name:
            raise ValidationError(_('"name" is required.'))
        existing = Kit.objects.for_scope(Scope.current()).filter(name=name, tenant=tenant).first()
        if existing is None and Kit.objects.filter(name=name).exists():
            raise ValidationError(
                _("A kit named '%(name)s' is shared or not available and is never modified.") % {"name": name}
            )
        entries = _declared_entries(row.get("items"))
        items = self._plan_items(entries, tenant) if entries is not None else None
        return tenant, name, str(row.get("description") or "").strip(), existing, items

    @staticmethod
    def _plan_items(entries, tenant):
        planned, seen, errors = [], set(), []
        for position, entry in enumerate(entries, start=1):
            try:
                kind, reference, qty = _parse_entry(entry)
                obj = _resolve_item(kind, reference, tenant)
            except ValidationError as exc:
                errors.extend(f"item {position}: {message}" for message in exc.messages)
                continue
            if (kind, obj.pk) in seen:
                duplicate = _("Item '%(ref)s' is declared more than once.") % {"ref": reference}
                errors.append(f"item {position}: {duplicate}")
                continue
            seen.add((kind, obj.pk))
            planned.append((kind, obj, qty))
        if errors:
            raise ValidationError(errors)
        return planned

    # -- persistence ---------------------------------------------------------

    def _import_row(self, row, row_number):
        tenant, name, description, kit, items = self._plan_row(row)
        if kit is None:
            return self._create(tenant, name, description, items)
        self._authorize_update(kit)
        return self._update(kit, description, items)

    def _can_replace_items(self, kit):
        actor = self.actor
        return actor is not None and all(actor.has_perm(f"inventory.{perm}", kit) for perm in _REPLACE_PERMS)

    def _create(self, tenant, name, description, items):
        actor = self.actor
        if items and (actor is None or not actor.has_perm("inventory.add_kititem")):
            raise ValidationError(_("You do not have permission to add kit items."))
        kit = Kit(name=name, description=description, tenant=tenant)
        kit.full_clean()
        kit.save()
        self._write_items(kit, items or [])
        return ROW_CREATED

    def _update(self, kit, description, items):
        changed = bool(description) and description != kit.description
        replace = items is not None and _current_items(kit) != {(k, o.pk, q) for k, o, q in items}
        if replace and not self._can_replace_items(kit):
            replace = False
            self._withheld.append(
                _("Kit '%(name)s': items not replaced; this needs change, add and delete permissions for kit items.")
                % {"name": kit.name}
            )
        if not changed and not replace:
            raise RowSkipped(_("record already exists and is unchanged; skipped."))
        if hasattr(kit, "snapshot"):
            kit.snapshot()
        if changed:
            kit.description = description
            kit.full_clean()
            kit.save()
        if replace:
            for item in list(kit.items.all()):
                item.delete()
            self._write_items(kit, items)
        return ROW_UPDATED

    @staticmethod
    def _write_items(kit, items):
        for kind, obj, qty in items:
            item = KitItem(kit=kit, qty=qty, **{_ITEM_KINDS[kind][1]: obj})
            item.full_clean()
            item.save()

    def import_data(self, request=None):
        result = super().import_data(request)
        self.skipped_list = [*self.skipped_list, *self._withheld]
        return result
