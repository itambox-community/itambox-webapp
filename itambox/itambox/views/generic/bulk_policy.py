"""One answer to "may this list offer bulk edit/delete, and where does it post?".

The list toolbar (``ObjectListView`` and the ``bulk_action_context`` tag for
embedded tables) and the request-selected ``/bulk-edit/`` and ``/bulk-delete/``
endpoints used to decide this independently. They drifted: lists rendered a
button whose global endpoint then answered 404 (shared catalogue models with no
``filter_by_tenant``) or 403 (``organization.role``). Both sides now ask
:func:`request_selected_bulk_allowed`, so a rendered button always reaches a
handler that accepts its model.

Resolution order for a model's bulk URL:

1. a model-specific route (``<app>:<model>_bulk_edit``) wins, unchanged;
2. otherwise the global endpoint, only when the policy admits the model;
3. otherwise ``None`` -- no button.
"""

from django.core.exceptions import FieldDoesNotExist
from django.urls import NoReverseMatch, reverse

from itambox.utils import get_model_viewname

BULK_EDIT = "bulk_edit"
BULK_DELETE = "bulk_delete"
BULK_ACTIONS = (BULK_EDIT, BULK_DELETE)

# Never served by the request-selected endpoints, for either action. These
# models carry invariants the generic field form and the generic archive path
# cannot reproduce (tenant topology, role ownership and escalation checks,
# delegated cross-tenant grants, service-owned lifecycle rows, group
# membership escalation). Their dedicated views stay available where they exist.
REQUEST_SELECTED_BULK_DENYLIST = frozenset(
    {
        "organization.role",
        "organization.tenant",
        "organization.tenantresourcegrant",
        "compliance.custodyreceipt",
        "users.usergroup",
    }
)

# Tenantless shared catalogue models. Their rows belong to no tenant, so there
# is no tenant scope to apply: the generic endpoints serve them gated by the
# model permission (``change_``/``delete_``), the per-row check, and the
# model-wide mutation policy (``SUPERUSER_ONLY_MUTATION_MODELS``).
#
# Explicit on purpose: a new model without ``filter_by_tenant`` is refused by
# default rather than silently treated as global. Classify it here (or give it
# a dedicated route) -- ``test_bulk_policy`` fails until every list model is
# classified.
SHARED_CATALOGUE_BULK_MODELS = frozenset(
    {
        "assets.assetrole",
        "assets.assettype",
        "assets.category",
        "assets.depreciation",
        "assets.manufacturer",
        "assets.statuslabel",
        "extras.exporttemplate",
        "extras.labeltemplate",
        "organization.sitegroup",
    }
)


def is_managed_definition_model(model) -> bool:
    """Whether ``model`` stores core/library-managed definition rows."""
    if model is None:
        return False
    try:
        model._meta.get_field("management_kind")
    except FieldDoesNotExist:
        return False
    return True


def is_request_selectable(model) -> bool:
    """Whether the global endpoints may resolve ``model`` from ``model_name`` at all.

    Unselectable models are answered with 404 (the endpoint does not know
    them); selectable but refused ones (denylist, managed edit) with 403 by
    the views' ``has_permission``.
    """
    if model is None or model._meta.swapped:
        return False
    # ``_meta.swapped`` is checked first so the manager of a swapped-out model
    # is never touched.
    return hasattr(model.objects, "filter_by_tenant") or model._meta.label_lower in SHARED_CATALOGUE_BULK_MODELS


def request_selected_bulk_refused(model, action: str) -> bool:
    """Whether a selectable model is refused for ``action`` (answered 403).

    Managed definition models never take the generic bulk *edit* form (their
    rows are protected field by field); bulk *delete* stays available and the
    endpoint refuses any managed row inside the batch.
    """
    if action not in BULK_ACTIONS:
        raise ValueError(f"unknown bulk action {action!r}")
    if model._meta.label_lower in REQUEST_SELECTED_BULK_DENYLIST:
        return True
    return action == BULK_EDIT and is_managed_definition_model(model)


def request_selected_bulk_allowed(model, action: str) -> bool:
    """Whether the global endpoint for ``action`` serves ``model``."""
    if action not in BULK_ACTIONS:
        raise ValueError(f"unknown bulk action {action!r}")
    return is_request_selectable(model) and not request_selected_bulk_refused(model, action)


def _reverse_or_none(viewname: str) -> str | None:
    try:
        return reverse(viewname)
    except NoReverseMatch:
        return None


def bulk_action_url(model, action: str) -> str | None:
    """The URL a list should post ``action`` to, or ``None`` for no button."""
    if model is None:
        return None
    dedicated = _reverse_or_none(get_model_viewname(model, action))
    if dedicated is not None:
        return dedicated
    if not request_selected_bulk_allowed(model, action):
        return None
    return _reverse_or_none(action)
