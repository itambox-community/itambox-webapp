"""Shared barcode/QR code resolver for asset scanning.

Handles the multiple input shapes that a scan can produce:
- Bare asset tag:      ITM-00042
- Bare serial number:  SN-ABC123
- EAN / GTIN:          4012345678901  (resolved through the AssetType catalogue)
- Label reference:     itambox://assets.asset/7, the absolute detail URL a label
                       printed with ITAMBOX_BASE_URL encodes, or the pre-1.0
                       itambox://asset/7 -- identifies exactly one row
- itambox scheme:      itambox:ITM-00042  (a tag; tags are unique per tenant only)
- Other URL:           https://host/.../7/  (last path segment tried as pk, then tag)

Tenant behavior: a scan without an explicit ``user`` resolves within the
current tenant scope — audit sessions and bulk baskets stay bound to their
session tenant. The global scan-to-find flow passes ``user``, which resolves
across every tenant the user can access regardless of the tenant currently
selected (superusers resolve globally). Lookups never return rows from
tenants the user cannot access.
"""

from urllib.parse import urlencode

from django.core.exceptions import FieldError
from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from assets.models import Asset, AssetType
from core.managers import Scope
from core.tenant_scope import accessible_tenant_ids
from inventory.models import Accessory, Component, Consumable
from itambox.labels import parse_label_reference
from itambox.scanning import strip_itambox_prefix


def _accessible_model_queryset(model, user, tenant=None):
    """Return the queryset a scan should search for ``model``.

    - ``user is None`` (audit / bulk flows): the tenant-scoped manager, so the
      resolution stays within the current tenant scope.
    - Superuser: every row (soft-deleted excluded), i.e. the global view the
      superuser already has everywhere else.
    - Any other user: exactly the tenants that user can access
      (``accessible_tenant_ids``), independent of the active tenant selection.

    Returns ``None`` when the user has no accessible tenants (fail closed).
    """
    if tenant is not None:
        qs = model._base_manager.filter(tenant_id=tenant.pk)
    elif user is None:
        return model.objects.for_scope(Scope.current())
    elif user.is_superuser:
        qs = model._base_manager.all()
    else:
        tenant_ids = accessible_tenant_ids(user)
        if not tenant_ids:
            return None
        qs = model._base_manager.filter(tenant_id__in=tenant_ids)

    try:
        return qs.filter(deleted_at__isnull=True)
    except FieldError:
        return qs


def _resolve_pk(code_fragment, qs):
    """Resolve a numeric code fragment as a primary key within ``qs``, or ``None``."""
    try:
        return qs.get(pk=int(code_fragment))
    except (Asset.DoesNotExist, ValueError):
        return None


def _candidate_from_url(raw, qs):
    """Extract the last path segment of a URL, trying numeric segments as pk.

    Returns ``(candidate, asset)`` — ``asset`` is set when the trailing
    segment resolved as a primary key.
    """
    path_part = raw.split("?")[0].split("#")[0]
    segments = [s for s in path_part.split("/") if s]
    if not segments:
        return raw, None
    candidate = segments[-1]
    if candidate.isdigit():
        asset = _resolve_pk(candidate, qs)
        if asset is not None:
            return candidate, asset
    return candidate, None


def _resolve_ean(raw, qs, match_ean):
    """Resolve an EAN/GTIN through the AssetType catalogue.

    Returns ``(asset, ambiguous)``. ``ambiguous`` is True when the AssetType
    maps to several assets in scope — callers must surface that instead of a
    silent miss. Returns ``(None, False)`` when EAN matching is disabled or
    the code belongs to no AssetType.
    """
    if not match_ean:
        return None, False
    atype = AssetType.objects.filter(ean__iexact=raw).first()
    if atype is None:
        return None, False
    type_assets = qs.filter(asset_type=atype)
    count = type_assets.count()
    if count == 1:
        return type_assets.first(), False
    if count > 1:
        return None, True
    return None, False


def resolve_scanned_asset_in_queryset(code, queryset, match_ean=True):
    """Resolve a code using a queryset whose tenant scope was already authorized.

    This keeps the parser and EAN behavior in one place while allowing audit
    operations to bind resolution to their exact operation permission instead
    of the broader global scanner scope.
    """
    # A label reference names one row: never fall back to a tag lookup for it.
    reference = parse_label_reference(code)
    if reference is not None:
        model, pk = reference
        if model is not queryset.model:
            return None, False
        return _resolve_pk(str(pk), queryset), False

    raw = strip_itambox_prefix(code)
    if not raw:
        return None, False

    qs = queryset

    # Full URL — extract last non-empty path segment as pk or tag/serial
    if raw.lower().startswith(("http://", "https://")):
        raw, asset = _candidate_from_url(raw, qs)
        if asset is not None:
            return asset, False

    # Tags are unique per tenant only and serials not at all: a scope spanning
    # several tenants can hold more than one match, which must surface as
    # ambiguous instead of silently opening one of them.
    matches = list(qs.filter(Q(asset_tag__iexact=raw) | Q(serial_number__iexact=raw)).order_by("pk")[:2])
    if len(matches) > 1:
        return None, True
    if matches:
        return matches[0], False

    return _resolve_ean(raw, qs, match_ean)


def resolve_scanned_asset(code, user=None, match_ean=True, tenant=None):
    """Resolve a scanned code to an Asset, with ambiguity information.

    Returns ``(asset, ambiguous)``:

    - ``(asset, False)`` — exactly one match: asset tag, serial number, a
      PK-based deep link, or an AssetType EAN that maps to exactly one asset
      in scope.
    - ``(None, True)`` — the code is an AssetType EAN that maps to several
      assets in scope; callers must surface this instead of a silent miss.
    - ``(None, False)`` — nothing matched, or the user has no accessible
      tenants (fail closed).

    ``match_ean=False`` skips the AssetType-EAN step (used by the global
    scan-to-find flow, which redirects EANs to the filtered asset list).
    """
    qs = _accessible_model_queryset(Asset, user, tenant=tenant)
    if qs is None:
        return None, False
    return resolve_scanned_asset_in_queryset(code, qs, match_ean=match_ean)


def resolve_scanned_code(code, user=None, match_ean=True, tenant=None):
    """Resolve a scanned code to an Asset, or ``None``.

    An AssetType EAN that matches several assets in scope returns ``None`` so
    no wrong asset is ever picked; use :func:`resolve_scanned_asset` to tell
    an ambiguous EAN apart from a plain miss.

    ``match_ean=False`` skips the AssetType-EAN step: the global scan-to-find
    flow uses this so an EAN redirects to the filtered asset list (its
    existing behavior) instead of jumping to a single asset detail page.
    """
    asset, _ambiguous = resolve_scanned_asset(code, user=user, match_ean=match_ean, tenant=tenant)
    return asset


def _reference_target(reference, user):
    """The detail target for a label reference the user may view, else ``None``."""
    model, pk = reference
    meta = model._meta
    if not user.has_perm(f"{meta.app_label}.view_{meta.model_name}"):
        return None
    qs = _accessible_model_queryset(model, user)
    obj = qs.filter(pk=pk).first() if qs is not None else None
    if obj is None or not user.has_perm(f"{meta.app_label}.view_{meta.model_name}", obj=obj):
        return None
    return {"url": obj.get_absolute_url(), "label": str(obj)}


def _asset_target(code, raw, user):
    """Steps 1 and 2 of :func:`resolve_scanned_target` for a user who may view assets."""
    asset, ambiguous = resolve_scanned_asset(code, user=user, match_ean=False)
    if asset is not None:
        return {"url": asset.get_absolute_url(), "label": str(asset)}
    if ambiguous:
        url = "%s?%s" % (reverse("assets:asset_list"), urlencode({"q": raw}))
        return {"url": url, "label": _("Several assets match %(code)s") % {"code": raw}}
    # AssetType EAN -> the asset list filtered to assets of that type.
    atype = AssetType.objects.filter(ean__iexact=raw).first()
    if atype is not None:
        url = "%s?%s" % (reverse("assets:asset_list"), urlencode({"ean": raw}))
        return {"url": url, "label": str(atype)}
    return None


def resolve_scanned_target(code, user):
    """Resolve a scanned code to a navigation target for the global scanner.

    Resolution order (permission-gated so names never leak across object
    types; cross-tenant across every tenant the user can access):
      0. Label reference (any labelled model)    -> that object's detail.
      1. Asset by tag / serial                   -> asset detail; a tag that
         several accessible assets share         -> asset list searched for it.
      2. AssetType by EAN                        -> asset list filtered to that EAN.
      3. Component / Accessory / Consumable EAN  -> that item's detail.

    Returns ``{'url': ..., 'label': ...}`` or ``None``.
    """
    reference = parse_label_reference(code)
    if reference is not None:
        return _reference_target(reference, user)

    raw = strip_itambox_prefix(code)
    if not raw:
        return None

    if user.has_perm("assets.view_asset"):
        target = _asset_target(code, raw, user)
        if target is not None:
            return target

    # Inventory item EAN -> item detail.
    # Deferred at call time: inventory reaches back into assets, so a module-top
    # import here would couple the two apps' load order. Not a module-level cycle
    # today -- tracked as local-import debt rather than a policy justification.

    for model, perm in (
        (Component, "inventory.view_component"),
        (Accessory, "inventory.view_accessory"),
        (Consumable, "inventory.view_consumable"),
    ):
        if not user.has_perm(perm):
            continue
        qs = _accessible_model_queryset(model, user)
        if qs is None:
            continue
        obj = qs.filter(ean__iexact=raw).first()
        if obj is not None:
            return {"url": obj.get_absolute_url(), "label": str(obj)}

    return None
