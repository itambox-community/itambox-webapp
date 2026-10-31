"""Read-only tenant-integrity checks (remediation plan, phase 1).

Every function here inspects data and returns findings — nothing writes.
The checks deliberately query through ``_base_manager`` / ``all_objects``:
an integrity report must see every tenant's rows regardless of the caller's
context. (Outside a request with no bound user the scoped default managers
happen to return the unscoped queryset, but with a bound user and/or active
tenant — e.g. under tests — they filter or fail closed; ``_base_manager``
keeps the checks context-independent either way.)

Cross-tenant rows are classified, never legitimized: for sharing-eligible
relationships the report emits *proposed* ``TenantResourceGrant`` payloads
for operator review; it never creates them.

Consumed by ``manage.py integrity_report`` and the phase-1 regression tests.
"""

from dataclasses import dataclass, field

from django.apps import apps

from core.mfa import role_is_privileged

# Classification of a (resource-owner tenant, other-party tenant) pair.
CLASS_SAME_TENANT = "same-tenant"
CLASS_PROVIDER_MANAGED = "provider-to-managed"
CLASS_TENANT_GROUP = "within-tenant-group"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_INVALID = "unrelated-invalid"

# Classes for which a cross-tenant row is sharing-eligible under the target
# design (ADR-0001) and therefore yields a proposed TenantResourceGrant.
PROPOSAL_ELIGIBLE_CLASSES = frozenset({CLASS_PROVIDER_MANAGED, CLASS_TENANT_GROUP})

MAX_SAMPLE_PKS = 10


@dataclass(frozen=True)
class Finding:
    """One integrity violation (or one aggregated per-model violation set)."""

    check: str  # stable check identifier, e.g. 'stock_tenant_conflict'
    model: str  # dotted label, e.g. 'inventory.ComponentStock'
    pk: object  # offending row pk; None for aggregated findings
    summary: str  # human-readable one-liner
    classification: str = ""  # CLASS_* for cross-tenant findings
    details: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "check": self.check,
            "model": self.model,
            "pk": self.pk,
            "summary": self.summary,
            "classification": self.classification,
            "details": self.details,
        }


@dataclass(frozen=True)
class GrantProposal:
    """A proposed TenantResourceGrant for operator review (phase 2 shape)."""

    owner_tenant_id: int
    grantee_tenant_id: int
    resource_model: str  # dotted label of the stock model
    item_id: int  # catalogue item pk
    location_id: int  # stock location pk
    stock_id: object  # concrete stock row pk when one exists, else None
    access_level: str  # always 'use' — derived from observed consumption
    classification: str  # why the pair is sharing-eligible
    evidence: str  # which row surfaced the need

    def as_dict(self):
        return {
            "owner_tenant_id": self.owner_tenant_id,
            "grantee_tenant_id": self.grantee_tenant_id,
            "resource_model": self.resource_model,
            "item_id": self.item_id,
            "location_id": self.location_id,
            "stock_id": self.stock_id,
            "access_level": self.access_level,
            "classification": self.classification,
            "evidence": self.evidence,
        }


class TenantTopology:
    """One-shot snapshot of tenant management edges and the TenantGroup tree.

    Loads every tenant and group once (via ``_base_manager`` — no scoping) so
    per-row classification is pure dict work. The group walk is cycle-safe:
    a parent loop terminates at the first revisited node.
    """

    def __init__(self):
        Tenant = apps.get_model("organization", "Tenant")
        TenantGroup = apps.get_model("organization", "TenantGroup")
        self.tenants = {
            row["pk"]: row
            for row in Tenant._base_manager.values(
                "pk",
                "name",
                "managed_by_id",
                "group_id",
                "is_provider",
            )
        }
        self.group_parents = dict(TenantGroup._base_manager.values_list("pk", "parent_id"))
        self._group_root_cache = {}

    def name(self, tenant_id):
        row = self.tenants.get(tenant_id)
        return row["name"] if row else f"<missing tenant {tenant_id}>"

    def group_root(self, group_id):
        """Root ancestor of ``group_id`` (cycle-safe; a cycle member returns the
        smallest pk in the cycle so both sides of a comparison agree)."""
        if group_id is None:
            return None
        if group_id in self._group_root_cache:
            return self._group_root_cache[group_id]
        path = []
        node = group_id
        seen = set()
        while node is not None and node not in seen:
            seen.add(node)
            path.append(node)
            node = self.group_parents.get(node)
        if node is None:
            root = path[-1]
        else:
            # Cycle: normalize to a deterministic representative.
            cycle_start = path.index(node)
            root = min(path[cycle_start:])
        for visited in path:
            self._group_root_cache[visited] = root
        return root

    def classify(self, owner_tenant_id, other_tenant_id):
        """Classify the relationship between a resource owner and another party."""
        if owner_tenant_id is None or other_tenant_id is None:
            return CLASS_AMBIGUOUS
        if owner_tenant_id == other_tenant_id:
            return CLASS_SAME_TENANT
        owner = self.tenants.get(owner_tenant_id)
        other = self.tenants.get(other_tenant_id)
        if owner is None or other is None:
            return CLASS_AMBIGUOUS
        if other["managed_by_id"] == owner_tenant_id or owner["managed_by_id"] == other_tenant_id:
            return CLASS_PROVIDER_MANAGED
        owner_root = self.group_root(owner["group_id"])
        other_root = self.group_root(other["group_id"])
        if owner_root is not None and owner_root == other_root:
            return CLASS_TENANT_GROUP
        return CLASS_INVALID


def _live(qs):
    """Restrict to non-soft-deleted rows when the model soft-deletes."""
    model = qs.model
    if any(f.name == "deleted_at" for f in model._meta.local_fields):
        return qs.filter(deleted_at__isnull=True)
    return qs


# --------------------------------------------------------------------------- 1
# Models whose nullable tenant is a designed, legitimate state even though
# they carry neither allow_global_tenant nor changelog_global:
#   * core.Job — tenant=NULL is a system-level job (management commands,
#     superuser-triggered maintenance); see the field's own docstring.
#   * extras.Dashboard — personal user object, deliberately unscoped; the
#     tenant field only narrows widget data.
NULL_TENANT_BY_DESIGN = frozenset({"core.Job", "extras.Dashboard"})


def check_null_tenants():
    """Operational rows with ``tenant=NULL``.

    Scans every concrete model carrying a nullable local FK named ``tenant``
    to organization.Tenant. Models that are *deliberately* hybrid or global
    (``allow_global_tenant`` — shared catalogue/contacts — or
    ``changelog_global`` — global reference data) are skipped, as are the
    documented by-design cases in :data:`NULL_TENANT_BY_DESIGN`: NULL is a
    supported state there, not an integrity violation.
    """
    Tenant = apps.get_model("organization", "Tenant")
    findings = []
    for model in apps.get_models():
        if model._meta.proxy:
            continue
        tenant_field = None
        for f in model._meta.local_fields:
            if f.name == "tenant" and f.is_relation and f.related_model is Tenant:
                tenant_field = f
                break
        if tenant_field is None or not tenant_field.null:
            continue
        if getattr(model, "allow_global_tenant", False):
            continue
        if getattr(model, "changelog_global", False):
            continue
        if model._meta.label in NULL_TENANT_BY_DESIGN:
            continue
        qs = _live(model._base_manager.filter(tenant__isnull=True))
        count = qs.count()
        if not count:
            continue
        label = model._meta.label
        findings.append(
            Finding(
                check="null_tenant",
                model=label,
                pk=None,
                summary=f"{label}: {count} live row(s) with tenant=NULL",
                details={
                    "count": count,
                    "sample_pks": list(qs.values_list("pk", flat=True)[:MAX_SAMPLE_PKS]),
                },
            )
        )
    return findings


# --------------------------------------------------------------------------- 2
STOCK_SPECS = (
    # (stock model label, catalogue-item FK name)
    ("inventory.ComponentStock", "component"),
    ("inventory.AccessoryStock", "accessory"),
    ("inventory.ConsumableStock", "consumable"),
)


def check_stock_tenant_conflicts(topology=None):
    """Stock pools whose catalogue item and location imply conflicting tenants.

    Under the target design (ADR-0001) a pool is owned by ``location.tenant``.
    A global catalogue item (tenant=NULL) is fine; a location without a tenant
    leaves the pool owner-less (ambiguous); an item privately owned by a
    different tenant than the location is a conflict to resolve.
    """
    topo = topology or TenantTopology()
    findings = []
    for label, item_attr in STOCK_SPECS:
        model = apps.get_model(label)
        rows = model._base_manager.values(
            "pk",
            "tenant_id",
            "location_id",
            "location__tenant_id",
            f"{item_attr}_id",
            f"{item_attr}__tenant_id",
        )
        for row in rows:
            loc_tenant = row["location__tenant_id"]
            item_tenant = row[f"{item_attr}__tenant_id"]
            if loc_tenant is not None and row["tenant_id"] != loc_tenant:
                # Stock ownership invariant: stock.tenant is derived from and must
                # match location.tenant — drift means someone bypassed save().
                # (A tenant-less location is the AMBIGUOUS case below, not
                # drift — one finding per row, not two.)
                findings.append(
                    Finding(
                        check="stock_tenant_conflict",
                        model=label,
                        pk=row["pk"],
                        summary=(
                            f"{label} #{row['pk']}: stored tenant "
                            f"{topo.name(row['tenant_id']) if row['tenant_id'] else 'NULL'} "
                            f"!= location tenant "
                            f"{topo.name(loc_tenant)} (drift)"
                        ),
                        classification=topo.classify(loc_tenant, row["tenant_id"]),
                        details={
                            "stored_tenant_id": row["tenant_id"],
                            "location_id": row["location_id"],
                            "location_tenant_id": loc_tenant,
                        },
                    )
                )
            if loc_tenant is None:
                findings.append(
                    Finding(
                        check="stock_tenant_conflict",
                        model=label,
                        pk=row["pk"],
                        summary=(
                            f"{label} #{row['pk']}: location #{row['location_id']} has no "
                            f"tenant, so the pool owner cannot be determined"
                        ),
                        classification=CLASS_AMBIGUOUS,
                        details={
                            "location_id": row["location_id"],
                            "item_id": row[f"{item_attr}_id"],
                            "item_tenant_id": item_tenant,
                        },
                    )
                )
                continue
            if item_tenant is None:
                continue  # global catalogue item + owned location: supported
            cls = topo.classify(loc_tenant, item_tenant)
            if cls == CLASS_SAME_TENANT:
                continue
            findings.append(
                Finding(
                    check="stock_tenant_conflict",
                    model=label,
                    pk=row["pk"],
                    summary=(
                        '%s #%s: item tenant "%s" != location tenant "%s"'
                        % (label, row["pk"], topo.name(item_tenant), topo.name(loc_tenant))
                    ),
                    classification=cls,
                    details={
                        "location_id": row["location_id"],
                        "location_tenant_id": loc_tenant,
                        "item_id": row[f"{item_attr}_id"],
                        "item_tenant_id": item_tenant,
                    },
                )
            )
    return findings


# --------------------------------------------------------------------------- 3
ASSIGNMENT_SPECS = (
    # (assignment model label, item FK name, matching stock model label)
    ("inventory.ComponentAllocation", "component", "inventory.ComponentStock"),
    ("inventory.AccessoryAssignment", "accessory", "inventory.AccessoryStock"),
    ("inventory.ConsumableAssignment", "consumable", "inventory.ConsumableStock"),
)


def _target_tenant(row, prefix=""):
    """Tenant id of the single assignment target (holder / location / asset)."""
    for key in (
        f"{prefix}assigned_holder__tenant_id",
        f"{prefix}assigned_location__tenant_id",
        f"{prefix}assigned_asset__tenant_id",
    ):
        if row.get(key) is not None:
            return row[key]
    return None


def _has_target(row, prefix=""):
    return any(
        row.get(k) is not None
        for k in (f"{prefix}assigned_holder_id", f"{prefix}assigned_location_id", f"{prefix}assigned_asset_id")
    )


def _inventory_assignment_finding(topo, label, item_attr, row):
    """Finding for one cross-tenant inventory assignment row, or None."""
    if row["from_location_id"] is not None:
        source_tenant = row["from_location__tenant_id"]
        source_kind = "from_location"
    else:
        source_tenant = row[f"{item_attr}__tenant_id"]
        source_kind = "item"
    target_tenant = _target_tenant(row)
    cls = topo.classify(source_tenant, target_tenant)
    if cls == CLASS_SAME_TENANT:
        return None
    if source_tenant is None and source_kind == "item":
        # No from-location recorded and the catalogue item is global: the
        # source pool (and its owner) cannot be derived at all — these rows
        # block the stock-ownership backfill.
        summary = (
            f"{label} #{row['pk']}: no source pool derivable "
            f"(from_location empty, item is global); target tenant "
            f"{topo.name(target_tenant) if target_tenant else 'NULL'}"
        )
    else:
        summary = (
            f"{label} #{row['pk']}: source tenant "
            f"{topo.name(source_tenant) if source_tenant else 'NULL'} "
            f"({source_kind}) != target tenant "
            f"{topo.name(target_tenant) if target_tenant else 'NULL'}"
        )
    return Finding(
        check="cross_tenant_assignment",
        model=label,
        pk=row["pk"],
        summary=summary,
        classification=cls,
        details={
            "source_tenant_id": source_tenant,
            "source_kind": source_kind,
            "target_tenant_id": target_tenant,
            "item_id": row[f"{item_attr}_id"],
            "from_location_id": row["from_location_id"],
        },
    )


def _proposal_for_finding(finding, stock_model, stock_label, item_attr):
    """GrantProposal for a sharing-eligible finding with a concrete pool, or None."""
    d = finding.details
    if (
        finding.classification not in PROPOSAL_ELIGIBLE_CLASSES
        or d["from_location_id"] is None
        or d["source_tenant_id"] is None
        or d["target_tenant_id"] is None
    ):
        return None
    stock_pk = (
        stock_model._base_manager.filter(
            **{
                f"{item_attr}_id": d["item_id"],
                "location_id": d["from_location_id"],
            }
        )
        .values_list("pk", flat=True)
        .first()
    )
    return GrantProposal(
        owner_tenant_id=d["source_tenant_id"],
        grantee_tenant_id=d["target_tenant_id"],
        resource_model=stock_label,
        item_id=d["item_id"],
        location_id=d["from_location_id"],
        stock_id=stock_pk,
        access_level="use",
        classification=finding.classification,
        evidence=f"{finding.model} #{finding.pk}",
    )


def _check_asset_assignments(topo):
    """Cross-tenant findings for assets.AssetAssignment (current custody only)."""
    AssetAssignment = apps.get_model("assets", "AssetAssignment")
    findings = []
    # Only CURRENT custody (is_active=True): a checked-in asset assignment
    # stays as an is_active=False history row, mirroring the inventory family
    # where check-in soft-deletes the row (excluded by _live()).
    rows = _live(AssetAssignment._base_manager.filter(is_active=True)).values(
        "pk",
        "asset_id",
        "asset__tenant_id",
        "assigned_user_id",
        "assigned_user__tenant_id",
        "assigned_location_id",
        "assigned_location__tenant_id",
        "assigned_asset_id",
        "assigned_asset__tenant_id",
    )
    for row in rows:
        if row["assigned_user_id"] is not None:
            target_tenant = row["assigned_user__tenant_id"]
        elif row["assigned_location_id"] is not None:
            target_tenant = row["assigned_location__tenant_id"]
        elif row["assigned_asset_id"] is not None:
            target_tenant = row["assigned_asset__tenant_id"]
        else:
            continue
        source_tenant = row["asset__tenant_id"]
        cls = topo.classify(source_tenant, target_tenant)
        if cls == CLASS_SAME_TENANT:
            continue
        findings.append(
            Finding(
                check="cross_tenant_assignment",
                model="assets.AssetAssignment",
                pk=row["pk"],
                summary=(
                    f"assets.AssetAssignment #{row['pk']}: asset tenant "
                    f"{topo.name(source_tenant) if source_tenant else 'NULL'} != "
                    f"target tenant "
                    f"{topo.name(target_tenant) if target_tenant else 'NULL'}"
                ),
                classification=cls,
                details={
                    "source_tenant_id": source_tenant,
                    "target_tenant_id": target_tenant,
                    "asset_id": row["asset_id"],
                },
            )
        )
    return findings


def check_cross_tenant_assignments(topology=None):
    """Assignments whose source and target imply different tenants.

    Covers the three inventory assignment models (source = the from-location's
    tenant when a from-location exists, else the catalogue item's tenant) and
    assets.AssetAssignment (source = the asset's tenant). Sharing-eligible
    inventory rows with a concrete source pool also yield grant proposals.
    """
    topo = topology or TenantTopology()
    findings = []
    proposals = []

    for label, item_attr, stock_label in ASSIGNMENT_SPECS:
        model = apps.get_model(label)
        stock_model = apps.get_model(stock_label)
        rows = _live(model._base_manager).values(
            "pk",
            f"{item_attr}_id",
            f"{item_attr}__tenant_id",
            "from_location_id",
            "from_location__tenant_id",
            "assigned_holder_id",
            "assigned_holder__tenant_id",
            "assigned_location_id",
            "assigned_location__tenant_id",
            "assigned_asset_id",
            "assigned_asset__tenant_id",
        )
        for row in rows:
            if not _has_target(row):
                continue
            finding = _inventory_assignment_finding(topo, label, item_attr, row)
            if finding is None:
                continue
            findings.append(finding)
            proposal = _proposal_for_finding(finding, stock_model, stock_label, item_attr)
            if proposal is not None:
                proposals.append(proposal)

    findings += _check_asset_assignments(topo)
    return findings, proposals


# --------------------------------------------------------------------------- 4
def check_location_site_tenants(topology=None):
    """Locations whose tenant disagrees with their site's tenant."""
    topo = topology or TenantTopology()
    Location = apps.get_model("organization", "Location")
    findings = []
    rows = _live(Location._base_manager).values(
        "pk",
        "tenant_id",
        "site_id",
        "site__tenant_id",
    )
    for row in rows:
        loc_tenant, site_tenant = row["tenant_id"], row["site__tenant_id"]
        if loc_tenant == site_tenant:
            continue
        cls = topo.classify(site_tenant, loc_tenant)
        if cls == CLASS_SAME_TENANT:
            continue
        findings.append(
            Finding(
                check="location_site_tenant_mismatch",
                model="organization.Location",
                pk=row["pk"],
                summary=(
                    f"organization.Location #{row['pk']}: location tenant "
                    f"{topo.name(loc_tenant) if loc_tenant else 'NULL'} != site tenant "
                    f"{topo.name(site_tenant) if site_tenant else 'NULL'}"
                ),
                classification=cls,
                details={"tenant_id": loc_tenant, "site_id": row["site_id"], "site_tenant_id": site_tenant},
            )
        )
    return findings


# --------------------------------------------------------------------------- 5
PO_LINE_ITEM_FKS = ("asset_type", "component", "accessory", "consumable", "license")


def _check_po_headers(topo):
    """PO tenant vs destination-location tenant."""
    PurchaseOrder = apps.get_model("procurement", "PurchaseOrder")
    findings = []
    for row in _live(PurchaseOrder._base_manager).values(
        "pk", "tenant_id", "destination_location_id", "destination_location__tenant_id"
    ):
        po_tenant = row["tenant_id"]
        dest_tenant = row["destination_location__tenant_id"]
        cls = topo.classify(po_tenant, dest_tenant)
        if cls == CLASS_SAME_TENANT:
            continue
        findings.append(
            Finding(
                check="po_tenant_mismatch",
                model="procurement.PurchaseOrder",
                pk=row["pk"],
                summary=(
                    f"procurement.PurchaseOrder #{row['pk']}: PO tenant "
                    f"{topo.name(po_tenant) if po_tenant else 'NULL'} != destination-"
                    f"location tenant {topo.name(dest_tenant) if dest_tenant else 'NULL'}"
                ),
                classification=cls,
                details={
                    "tenant_id": po_tenant,
                    "destination_location_id": row["destination_location_id"],
                    "destination_tenant_id": dest_tenant,
                },
            )
        )
    return findings


def _po_line_item_findings(topo, row, line_tenant, tenant_bearing_fks):
    """Per-line findings for item FKs owned by a different tenant."""
    findings = []
    for fk in tenant_bearing_fks:
        item_id, item_tenant = row[f"{fk}_id"], row[f"{fk}__tenant_id"]
        if item_id is None or item_tenant is None:
            continue  # no item on this fan arm, or global catalogue item
        if line_tenant is not None and item_tenant != line_tenant:
            findings.append(
                Finding(
                    check="po_line_item_tenant_mismatch",
                    model="procurement.PurchaseOrderLine",
                    pk=row["pk"],
                    summary=(
                        'procurement.PurchaseOrderLine #%s: %s #%s belongs to "%s", line belongs to "%s"'
                        % (row["pk"], fk, item_id, topo.name(item_tenant), topo.name(line_tenant))
                    ),
                    classification=topo.classify(line_tenant, item_tenant),
                    details={
                        "item_fk": fk,
                        "item_id": item_id,
                        "item_tenant_id": item_tenant,
                        "tenant_id": line_tenant,
                    },
                )
            )
    return findings


def check_purchase_orders(topology=None):
    """PO ↔ destination-location and PO ↔ line ↔ item tenant mismatches."""
    topo = topology or TenantTopology()
    PurchaseOrderLine = apps.get_model("procurement", "PurchaseOrderLine")
    findings = _check_po_headers(topo)

    # Only follow <fk>__tenant for targets that actually carry a tenant field
    # (asset_type is global catalogue and has none).
    tenant_bearing_fks = []
    for fk in PO_LINE_ITEM_FKS:
        related = PurchaseOrderLine._meta.get_field(fk).related_model
        if any(f.name == "tenant" for f in related._meta.local_fields):
            tenant_bearing_fks.append(fk)
    value_fields = ["pk", "tenant_id", "purchase_order_id", "purchase_order__tenant_id"]
    for fk in tenant_bearing_fks:
        value_fields += [f"{fk}_id", f"{fk}__tenant_id"]

    for row in _live(PurchaseOrderLine._base_manager).values(*value_fields):
        line_tenant = row["tenant_id"]
        po_tenant = row["purchase_order__tenant_id"]
        cls = topo.classify(po_tenant, line_tenant)
        if cls != CLASS_SAME_TENANT:
            findings.append(
                Finding(
                    check="po_line_tenant_mismatch",
                    model="procurement.PurchaseOrderLine",
                    pk=row["pk"],
                    summary=(
                        f"procurement.PurchaseOrderLine #{row['pk']}: line tenant "
                        f"{topo.name(line_tenant) if line_tenant else 'NULL'} != PO tenant "
                        f"{topo.name(po_tenant) if po_tenant else 'NULL'}"
                    ),
                    classification=cls,
                    details={
                        "tenant_id": line_tenant,
                        "purchase_order_id": row["purchase_order_id"],
                        "po_tenant_id": po_tenant,
                    },
                )
            )
        findings += _po_line_item_findings(topo, row, line_tenant, tenant_bearing_fks)
    return findings


# --------------------------------------------------------------------------- 6
def check_license_seats(topology=None):
    """License-seat assignments whose target belongs to another tenant."""
    topo = topology or TenantTopology()
    LicenseSeatAssignment = apps.get_model("licenses", "LicenseSeatAssignment")
    findings = []
    for row in _live(LicenseSeatAssignment._base_manager).values(
        "pk",
        "license_id",
        "license__tenant_id",
        "asset_id",
        "asset__tenant_id",
        "assigned_holder_id",
        "assigned_holder__tenant_id",
    ):
        license_tenant = row["license__tenant_id"]
        if row["asset_id"] is not None:
            target_tenant, target = row["asset__tenant_id"], f"asset #{row['asset_id']}"
        elif row["assigned_holder_id"] is not None:
            target_tenant = row["assigned_holder__tenant_id"]
            target = f"holder #{row['assigned_holder_id']}"
        else:
            continue
        cls = topo.classify(license_tenant, target_tenant)
        if cls == CLASS_SAME_TENANT:
            continue
        findings.append(
            Finding(
                check="license_seat_tenant_mismatch",
                model="licenses.LicenseSeatAssignment",
                pk=row["pk"],
                summary=(
                    f"licenses.LicenseSeatAssignment #{row['pk']}: license tenant "
                    f"{topo.name(license_tenant) if license_tenant else 'NULL'} != "
                    f"{target} tenant "
                    f"{topo.name(target_tenant) if target_tenant else 'NULL'}"
                ),
                classification=cls,
                details={
                    "license_id": row["license_id"],
                    "license_tenant_id": license_tenant,
                    "target": target,
                    "target_tenant_id": target_tenant,
                },
            )
        )
    return findings


# --------------------------------------------------------------------------- 7
def check_custody_receipts(topology=None):
    """Custody receipts whose asset and holder belong to different tenants."""
    topo = topology or TenantTopology()
    CustodyReceipt = apps.get_model("compliance", "CustodyReceipt")
    findings = []
    for row in CustodyReceipt._base_manager.values(
        "pk", "asset_id", "asset__tenant_id", "holder_id", "holder__tenant_id"
    ):
        cls = topo.classify(row["asset__tenant_id"], row["holder__tenant_id"])
        if cls == CLASS_SAME_TENANT:
            continue
        findings.append(
            Finding(
                check="custody_tenant_mismatch",
                model="compliance.CustodyReceipt",
                pk=row["pk"],
                summary=(
                    f"compliance.CustodyReceipt #{row['pk']}: asset tenant "
                    f"{topo.name(row['asset__tenant_id']) if row['asset__tenant_id'] else 'NULL'}"
                    f" != holder tenant "
                    f"{topo.name(row['holder__tenant_id']) if row['holder__tenant_id'] else 'NULL'}"
                ),
                classification=cls,
                details={
                    "asset_id": row["asset_id"],
                    "asset_tenant_id": row["asset__tenant_id"],
                    "holder_id": row["holder_id"],
                    "holder_tenant_id": row["holder__tenant_id"],
                },
            )
        )
    return findings


# --------------------------------------------------------------------------- 8
def check_rbac_grants(topology=None):
    """Find malformed canonical RoleGrant aggregates and group principals."""
    topo = topology or TenantTopology()
    return _check_role_grants(topo) + _check_group_memberships(topo)


def _own_scope_finding(topo, grant):
    """Return a finding for an invalid own scope, or None."""
    owner_id = grant.principal_tenant_id
    role_tenant_id = grant.role.tenant_id
    if role_tenant_id == owner_id:
        return None
    shared_ok = (
        grant.membership_id
        and grant.role.shared_with_managed
        and topo.tenants.get(role_tenant_id, {}).get("is_provider")
        and topo.tenants.get(owner_id, {}).get("managed_by_id") == role_tenant_id
    )
    if shared_ok:
        return None
    return Finding(
        check="rbac_grant_inconsistent",
        model="organization.RoleGrant",
        pk=grant.pk,
        summary=(
            'RoleGrant #%s (own scope): role "%s" owned by "%s" granted '
            'to a principal in "%s" without a valid shared-role relationship'
            % (grant.pk, grant.role.name, topo.name(role_tenant_id), topo.name(owner_id))
        ),
        classification=topo.classify(role_tenant_id, owner_id),
        details={
            "role_id": grant.role_id,
            "role_tenant_id": role_tenant_id,
            "membership_id": grant.membership_id,
            "user_group_id": grant.user_group_id,
            "principal_tenant_id": owner_id,
            "shared_with_managed": grant.role.shared_with_managed,
        },
    )


def _managed_scope_finding(topo, grant, scope):
    """Return a finding for an invalid managed scope, or None."""
    owner_id = grant.principal_tenant_id
    role_tenant_id = grant.role.tenant_id
    problems = []
    if not topo.tenants.get(owner_id, {}).get("is_provider"):
        problems.append("principal tenant is not a provider")
    if role_tenant_id != owner_id:
        problems.append(
            'managed-scope role must be owned by the granting provider, but is owned by "%s"'
            % topo.name(role_tenant_id)
        )
    if scope.scope_type == "tenant_group" and not scope.tenant_group_id:
        problems.append("tenant_group scope without a scope group")
    stale = []
    if scope.scope_type == "tenant" and scope.tenant_id:
        managed_by_id = topo.tenants.get(scope.tenant_id, {}).get("managed_by_id")
        if managed_by_id != owner_id:
            stale.append(scope.tenant_id)
            problems.append("target tenant is no longer managed by the provider")
    if not problems:
        return None
    return Finding(
        check="rbac_grant_inconsistent",
        model="organization.RoleGrant",
        pk=grant.pk,
        summary=f"RoleGrant #{grant.pk} (managed scope): " + "; ".join(problems),
        classification=CLASS_INVALID,
        details={
            "role_id": grant.role_id,
            "role_tenant_id": role_tenant_id,
            "membership_id": grant.membership_id,
            "user_group_id": grant.user_group_id,
            "principal_tenant_id": owner_id,
            "scope_type": scope.scope_type,
            "stale_tenant_ids": sorted(stale),
        },
    )


def _check_role_grants(topo):
    RoleGrant = apps.get_model("organization", "RoleGrant")
    findings = []
    grants = (
        RoleGrant._base_manager.select_related(
            "membership",
            "membership__tenant",
            "user_group",
            "user_group__tenant",
            "role",
            "role__tenant",
        )
        .prefetch_related("scopes")
        .filter(role__deleted_at__isnull=True)
    )
    for grant in grants:
        has_membership = grant.membership_id is not None
        has_group = grant.user_group_id is not None
        if has_membership == has_group:
            findings.append(
                Finding(
                    check="rbac_grant_inconsistent",
                    model="organization.RoleGrant",
                    pk=grant.pk,
                    summary=f"RoleGrant #{grant.pk} does not have exactly one principal",
                    classification=CLASS_INVALID,
                    details={},
                )
            )
            continue
        if has_group and grant.role.tenant_id != grant.user_group.tenant_id:
            findings.append(
                Finding(
                    check="rbac_grant_inconsistent",
                    model="organization.RoleGrant",
                    pk=grant.pk,
                    summary=(f"RoleGrant #{grant.pk}: group and role have different owners"),
                    classification=topo.classify(
                        grant.user_group.tenant_id,
                        grant.role.tenant_id,
                    ),
                    details={"user_group_id": grant.user_group_id, "role_id": grant.role_id},
                )
            )
        scopes = list(grant.scopes.all())
        if not scopes:
            findings.append(
                Finding(
                    check="rbac_grant_inconsistent",
                    model="organization.RoleGrant",
                    pk=grant.pk,
                    summary=f"RoleGrant #{grant.pk} has no scope",
                    classification=CLASS_INVALID,
                    details={},
                )
            )
        if has_membership:
            if role_is_privileged(grant.role) and (not (grant.reason or "").strip() or grant.valid_until is None):
                findings.append(
                    Finding(
                        check="rbac_grant_inconsistent",
                        model="organization.RoleGrant",
                        pk=grant.pk,
                        summary=(f"RoleGrant #{grant.pk}: elevated direct grant lacks a reason or expiration"),
                        classification=CLASS_INVALID,
                        details={"membership_id": grant.membership_id, "role_id": grant.role_id},
                    )
                )
        for scope in scopes:
            finding = (
                _own_scope_finding(topo, grant)
                if scope.scope_type == "own"
                else _managed_scope_finding(topo, grant, scope)
            )
            if finding is not None:
                findings.append(finding)
    return findings


def _group_membership_findings(topo, group):
    """Find GroupMembership rows that do not belong to the group owner."""
    findings = []
    for link in group.group_memberships.all():
        if link.membership.tenant_id != group.tenant_id:
            findings.append(
                Finding(
                    check="rbac_group_inconsistent",
                    model="users.GroupMembership",
                    pk=link.pk,
                    summary=(
                        'GroupMembership #%s: membership tenant "%s" differs from group owner "%s"'
                        % (link.pk, topo.name(link.membership.tenant_id), topo.name(group.tenant_id))
                    ),
                    classification=topo.classify(
                        group.tenant_id,
                        link.membership.tenant_id,
                    ),
                    details={"user_group_id": group.pk, "membership_id": link.membership_id},
                )
            )
    return findings


def _check_group_memberships(topo):
    UserGroup = apps.get_model("users", "UserGroup")
    findings = []
    groups = _live(UserGroup._base_manager.filter(is_active=True)).prefetch_related(
        "group_memberships__membership",
    )
    for group in groups:
        if group.tenant_id is None:
            findings.append(
                Finding(
                    check="rbac_group_inconsistent",
                    model="users.UserGroup",
                    pk=group.pk,
                    summary=(
                        'UserGroup #%s "%s" has no owning tenant '
                        "(global groups are disallowed by the target design)" % (group.pk, group.name)
                    ),
                    classification=CLASS_AMBIGUOUS,
                    details={},
                )
            )
        else:
            findings += _group_membership_findings(topo, group)
    return findings


# ---------------------------------------------------------------------------
def run_all_checks():
    """Run every check once and return ``(findings, proposals, stats)``."""
    topo = TenantTopology()
    findings = []
    findings += check_null_tenants()
    findings += check_stock_tenant_conflicts(topo)
    assignment_findings, proposals = check_cross_tenant_assignments(topo)
    findings += assignment_findings
    findings += check_location_site_tenants(topo)
    findings += check_purchase_orders(topo)
    findings += check_license_seats(topo)
    findings += check_custody_receipts(topo)
    findings += check_rbac_grants(topo)

    # De-duplicate proposals: many assignments can point at one pool+grantee.
    unique = {}
    for p in proposals:
        key = (p.resource_model, p.item_id, p.location_id, p.grantee_tenant_id)
        unique.setdefault(key, p)
    proposals = list(unique.values())

    stats = {"total_findings": len(findings), "proposals": len(proposals), "by_check": {}, "by_classification": {}}
    for f in findings:
        stats["by_check"][f.check] = stats["by_check"].get(f.check, 0) + 1
        if f.classification:
            stats["by_classification"][f.classification] = stats["by_classification"].get(f.classification, 0) + 1
    return findings, proposals, stats
