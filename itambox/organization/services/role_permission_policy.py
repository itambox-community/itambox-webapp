"""Presentation-free semantic permission policy for SSO-created roles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from django.apps import apps
from django.contrib.auth.models import Permission

from itambox.registry import registry

CustomerRoleName: TypeAlias = Literal["Admin", "Manager", "Member"]


@dataclass(frozen=True)
class RolePermissionTarget:
    """One declared CRUD target used by the SSO role permission policy."""

    key: str
    app: str
    model: str
    required_app: str | None = None
    plugin_name: str | None = None


ROLE_PERMISSION_TARGETS: tuple[RolePermissionTarget, ...] = (
    RolePermissionTarget("asset", "assets", "asset"),
    RolePermissionTarget("assetrequest", "assets", "assetrequest"),
    RolePermissionTarget("purchaseorder", "procurement", "purchaseorder"),
    RolePermissionTarget("auditsession", "compliance", "auditsession"),
    RolePermissionTarget("assetaudit", "compliance", "assetaudit"),
    RolePermissionTarget("accessory", "inventory", "accessory"),
    RolePermissionTarget("consumable", "inventory", "consumable"),
    RolePermissionTarget("kit", "inventory", "kit"),
    RolePermissionTarget("component", "inventory", "component"),
    RolePermissionTarget("license", "licenses", "license"),
    RolePermissionTarget("software", "software", "software"),
    RolePermissionTarget("subscription", "subscriptions", "subscription"),
    RolePermissionTarget("subscriptionassignment", "subscriptions", "subscriptionassignment"),
    RolePermissionTarget("location", "organization", "location"),
    RolePermissionTarget("site", "organization", "site"),
    RolePermissionTarget("assetholder", "organization", "assetholder"),
    RolePermissionTarget("role", "organization", "role"),
    RolePermissionTarget("membership", "organization", "membership"),
    RolePermissionTarget("rolegrant", "organization", "rolegrant"),
    RolePermissionTarget("tenantresourcegrant", "organization", "tenantresourcegrant"),
    RolePermissionTarget("region", "organization", "region"),
    RolePermissionTarget("sitegroup", "organization", "sitegroup"),
    RolePermissionTarget("tenantgroup", "organization", "tenantgroup"),
    RolePermissionTarget("contact", "organization", "contact"),
    RolePermissionTarget("contactrole", "organization", "contactrole"),
    RolePermissionTarget("manufacturer", "assets", "manufacturer"),
    RolePermissionTarget("supplier", "assets", "supplier"),
    RolePermissionTarget("statuslabel", "assets", "statuslabel"),
    RolePermissionTarget("category", "assets", "category"),
    RolePermissionTarget("depreciation", "assets", "depreciation"),
    RolePermissionTarget("assettype", "assets", "assettype"),
    RolePermissionTarget("customfield", "extras", "customfield"),
    RolePermissionTarget("tag", "extras", "tag"),
    RolePermissionTarget("reporttemplate", "extras", "reporttemplate"),
    RolePermissionTarget("scheduledreport", "extras", "scheduledreport"),
    RolePermissionTarget("alertrule", "extras", "alertrule"),
    RolePermissionTarget("alertlog", "extras", "alertlog"),
    RolePermissionTarget("notificationchannel", "extras", "notificationchannel"),
    RolePermissionTarget("exporttemplate", "extras", "exporttemplate"),
    RolePermissionTarget("webhookendpoint", "extras", "webhookendpoint"),
    RolePermissionTarget("eventrule", "extras", "eventrule"),
    RolePermissionTarget("labeltemplate", "extras", "labeltemplate"),
    RolePermissionTarget("recyclebin", "core", "recyclebin"),
    RolePermissionTarget("custodytemplate", "compliance", "custodytemplate"),
    RolePermissionTarget("custodyreceipt", "compliance", "custodyreceipt"),
    RolePermissionTarget("assetmaintenance", "assets", "assetmaintenance"),
    RolePermissionTarget("user", "users", "user"),
    RolePermissionTarget("token", "users", "token"),
    RolePermissionTarget("usergroup", "users", "usergroup"),
    RolePermissionTarget("warranty", "assets", "warranty"),
    RolePermissionTarget("assetdisposal", "assets", "assetdisposal"),
    RolePermissionTarget("assetreservation", "assets", "assetreservation"),
    RolePermissionTarget("contract", "procurement", "contract"),
    RolePermissionTarget("installedsoftware", "software", "installedsoftware"),
    RolePermissionTarget("assetrole", "assets", "assetrole"),
    RolePermissionTarget("costcenter", "organization", "costcenter"),
    RolePermissionTarget("journalentry", "extras", "journalentry"),
    RolePermissionTarget("configcontext", "extras", "configcontext"),
)

DASHBOARD_SSO_PERMISSIONS = frozenset(
    {
        "extras.view_dashboard",
        "extras.add_dashboard",
        "extras.change_dashboard",
        "extras.delete_dashboard",
    }
)
DASHBOARD_CODENAMES = DASHBOARD_SSO_PERMISSIONS


def active_role_permission_targets() -> tuple[RolePermissionTarget, ...]:
    """Return core targets and the targets declared by active plugins."""

    core_targets = tuple(
        target
        for target in ROLE_PERMISSION_TARGETS
        if target.required_app is None or apps.is_installed(target.required_app)
    )
    plugin_targets = tuple(
        RolePermissionTarget(
            key=f"plugin_{len(app)}_{app}_{model}",
            app=app,
            model=model,
            plugin_name=plugin_name,
        )
        for plugin_name, labels in registry.get_plugin_permission_targets().items()
        for label in labels
        for app, model in (label.split(".", 1),)
    )
    return core_targets + plugin_targets


def permissions_for_sso_role(role_name: CustomerRoleName) -> list[str]:
    """Return live CRUD and Dashboard permissions for one SSO role."""

    actions = {
        "Admin": ("view", "add", "change", "delete"),
        "Manager": ("view", "add", "change"),
        "Member": ("view", "add", "change"),
    }.get(role_name, ())
    candidates = {
        f"{target.app}.{action}_{target.model}" for target in active_role_permission_targets() for action in actions
    }
    candidates.update(DASHBOARD_SSO_PERMISSIONS)
    live = {
        f"{app_label}.{codename}"
        for app_label, codename in Permission.objects.values_list("content_type__app_label", "codename")
    }
    return sorted(candidates.intersection(live))
