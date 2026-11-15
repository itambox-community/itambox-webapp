"""Repair the tenant of legacy outbox events backfilled by 0006 (issue #1102).

0006 reclassified unprocessed events younger than 24 h as ``pending`` but left
``tenant_id`` NULL. The processing path selects rules by exactly that field
(``Q(tenant_id=...) | Q(tenant__isnull=True)``), so a NULL leaves a tenant
event matching only *global* rules -- tenant rules never fire, a global rule can
run without the originating tenant, and the event is still marked ``processed``.

This migration reconstructs the owning tenant for every ``pending`` event with
``tenant_id IS NULL`` using the SAME resolution as
``extras.services.events._resolve_instance_tenant_id`` (direct ``tenant_id``,
then the model's ``tenant_lookup`` / ``changelog_tenant_lookup`` ORM path).
Events whose object resolves to exactly one tenant get that ``tenant_id``.
Events that cannot be assigned unambiguously -- a NULL ``object_id``, an unknown
model, a missing/deleted row, or a model with no tenant anchor -- fail closed:
they become ``dead`` with ``last_error_class="outbox.backfill_unassigned"`` and
so trigger no rules at all rather than running global rules under the wrong (or
no) tenant.

It serves both upgrade paths. A fresh 1.0.0 -> 1.0.2 install runs 0006 (crude
status classification) then this; a 1.0.1 install that already ran 0006 runs
only this. In both cases no ``pending`` event with a NULL ``tenant_id`` remains.

The work is data reconstruction, not schema, and it is idempotent: it only
touches ``pending`` rows whose ``tenant_id`` is still NULL, and every row it
touches either gains a ``tenant_id`` or becomes ``dead``, so it leaves the set
on each pass. The reverse is therefore a documented no-op -- there is no prior
state to restore (the tenant a row originally lacked), and 0006's own reverse
already refuses while pending rows exist.
"""

from django.db import migrations

BACKFILL_UNASSIGNED = "outbox.backfill_unassigned"
# Bounded batch so the repair never loads the whole outbox into memory at once.
REPAIR_BATCH = 500

# The ORM path to the owning tenant for models that derive it through a relation.
# Frozen here on purpose: a migration must not read the live model metadata,
# because a later release may rename or change a lookup path and would then
# misclassify events while upgrading from an older release.
TENANT_LOOKUPS = {
    "assets.assetassignment": "asset__tenant",
    "assets.assetdisposal": "asset__tenant",
    "assets.assetmaintenance": "asset__tenant",
    "assets.assetreservation": "asset__tenant",
    "assets.warranty": "asset__tenant",
    "compliance.assetaudit": "asset__tenant",
    "compliance.custodyhandoffdelivery": "signing_session__receipt__asset__tenant",
    "compliance.custodyreceipt": "asset__tenant",
    "compliance.custodysigningsession": "receipt__asset__tenant",
    "extras.fileattachment": "content_object__tenant",
    "extras.imageattachment": "content_object__tenant",
    "inventory.accessoryassignment": "source_tenant",
    "inventory.componentallocation": "source_tenant",
    "inventory.consumableassignment": "source_tenant",
    "inventory.kititem": "kit__tenant",
    "licenses.licenseseatassignment": "license__tenant",
    "organization.tenantresourcegrantexpiryrevocation": "run__tenant",
    "software.installedsoftware": "asset__tenant",
    "subscriptions.subscriptionassignment": "subscription__tenant",
}

# Models that may legitimately hold tenant-less (global) rows. Their events stay
# global; every other model with a tenant anchor and no resolved tenant is dead.

# Leave the event untouched: its model has no tenant anchor at all.
_KEEP = object()


def _walk_lookup(apps, instance, lookup):
    """Walk ``lookup`` from ``instance``; resolve ``content_object`` historically."""
    obj = instance
    parts = lookup.split("__")
    index = 0
    ContentType = None
    while index < len(parts):
        part = parts[index]
        if part == "content_object":
            content_type_id = getattr(obj, "content_type_id", None)
            object_id = getattr(obj, "object_id", None)
            if not content_type_id or not object_id:
                return None
            if ContentType is None:
                ContentType = apps.get_model("contenttypes", "ContentType")
            content_type = ContentType.objects.filter(pk=content_type_id).first()
            if content_type is None:
                return None
            try:
                related_model = apps.get_model(content_type.app_label, content_type.model)
            except LookupError:
                return None
            obj = related_model._base_manager.filter(pk=object_id).first()
        else:
            obj = getattr(obj, part, None)
        if obj is None:
            return None
        index += 1
    return getattr(obj, "pk", None)


def _declares_global(event):
    """Whether the emitter recorded global scope on the event itself."""
    data = event.data if isinstance(event.data, dict) else {}
    return data.get("tenant_scope") == "global"


def _resolve_event_tenant_id(apps, event):
    """Resolve the single owning tenant for ``event``'s referenced object.

    Returns a tenant pk, ``_KEEP`` when the model has no tenant anchor at all
    (a legitimate global event), or ``None`` when the object is missing/unknown
    or carries an anchor without a resolvable tenant -- the caller fails closed.
    """
    content_type = event.model
    app_label, model_name = content_type.app_label, content_type.model
    label = f"{app_label}.{model_name}"
    try:
        historical_model = apps.get_model(app_label, model_name)
    except LookupError:
        return None

    fields = {field.name for field in historical_model._meta.fields}
    lookup = TENANT_LOOKUPS.get(label)
    if "tenant" not in fields and not lookup:
        # No tenant anchor at all: a legitimate global event, no row needed.
        return _KEEP

    instance = historical_model._base_manager.filter(pk=event.object_id).first()
    if instance is None:
        # A deleted row carries no proof that it was global; only an emitter
        # that recorded global provenance may run global rules.
        return _KEEP if _declares_global(event) else None

    tenant_id = getattr(instance, "tenant_id", None)
    if tenant_id is not None:
        return tenant_id

    if lookup:
        resolved = _walk_lookup(apps, instance, lookup)
        if resolved is not None:
            return resolved
        return _KEEP if _declares_global(event) else None
    # A tenant-capable row that is tenantless now may have been tenant-owned when
    # the event fired; without recorded provenance it fails closed.
    return _KEEP if _declares_global(event) else None


def backfill_event_tenant(apps, schema_editor):
    Event = apps.get_model("extras", "Event")
    manager = Event._base_manager
    last_pk = 0
    while True:
        # Keyset pagination: global (kept) rows stay eligible for the filter, so
        # an offset-free PK cursor guarantees later anchored rows are reached.
        batch = list(
            manager.filter(status="pending", tenant_id__isnull=True, pk__gt=last_pk)
            .order_by("pk")
            .select_for_update(of=("self",))
            .select_related("model")[:REPAIR_BATCH]
        )
        if not batch:
            return
        last_pk = batch[-1].pk
        assigned = []
        dead = []
        for event in batch:
            tenant_id = _resolve_event_tenant_id(apps, event)
            if tenant_id is _KEEP:
                continue
            if tenant_id is None:
                event.status = "dead"
                event.last_error_class = BACKFILL_UNASSIGNED
                dead.append(event)
            else:
                event.tenant_id = tenant_id
                assigned.append(event)
        if assigned:
            manager.bulk_update(assigned, ["tenant_id"])
        if dead:
            manager.bulk_update(dead, ["status", "last_error_class"])


def reverse_noop(apps, schema_editor):
    """No-op reverse: the repair reconstructs a tenant the row never carried, so
    there is no prior state to restore. 0006's reverse already refuses while any
    ``pending`` row exists, which is the drain that a downgrade needs."""


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0006_event_outbox"),
    ]

    operations = [
        migrations.RunPython(backfill_event_tenant, reverse_noop),
    ]
