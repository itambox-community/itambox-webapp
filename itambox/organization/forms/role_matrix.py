"""Translated presentation metadata for the semantic role matrix."""

from __future__ import annotations

from django.apps import apps
from django.utils.translation import gettext_lazy as _

from organization.services.role_permission_policy import ROLE_PERMISSION_TARGETS, active_role_permission_targets

ROLE_PERMISSION_PRESENTATION = {
    # Inventory & Hardware
    "asset": {"label": _("Assets"), "app": "assets", "model_name": "asset", "group": _("Inventory & Hardware")},
    "assetrequest": {
        "label": _("Asset Requests"),
        "app": "assets",
        "model_name": "assetrequest",
        "group": _("Inventory & Hardware"),
    },
    "purchaseorder": {
        "label": _("Purchase Orders"),
        "app": "procurement",
        "model_name": "purchaseorder",
        "group": _("Inventory & Hardware"),
    },
    "auditsession": {
        "label": _("Audit Sessions"),
        "app": "compliance",
        "model_name": "auditsession",
        "group": _("Compliance & Custody"),
    },
    "assetaudit": {
        "label": _("Asset Audits"),
        "app": "compliance",
        "model_name": "assetaudit",
        "group": _("Compliance & Custody"),
    },
    "accessory": {
        "label": _("Accessories"),
        "app": "inventory",
        "model_name": "accessory",
        "group": _("Inventory & Hardware"),
    },
    "consumable": {
        "label": _("Consumables"),
        "app": "inventory",
        "model_name": "consumable",
        "group": _("Inventory & Hardware"),
    },
    "kit": {"label": _("Kits"), "app": "inventory", "model_name": "kit", "group": _("Inventory & Hardware")},
    "component": {
        "label": _("Components"),
        "app": "inventory",
        "model_name": "component",
        "group": _("Inventory & Hardware"),
    },
    # Software & Subscriptions
    "license": {
        "label": _("Licenses"),
        "app": "licenses",
        "model_name": "license",
        "group": _("Software & Subscriptions"),
    },
    "software": {
        "label": _("Software"),
        "app": "software",
        "model_name": "software",
        "group": _("Software & Subscriptions"),
    },
    "subscription": {
        "label": _("Subscriptions"),
        "app": "subscriptions",
        "model_name": "subscription",
        "group": _("Software & Subscriptions"),
    },
    "subscriptionassignment": {
        "label": _("Subscription Assignments"),
        "app": "subscriptions",
        "model_name": "subscriptionassignment",
        "group": _("Software & Subscriptions"),
    },
    # Organization & Structure
    "location": {
        "label": _("Locations"),
        "app": "organization",
        "model_name": "location",
        "group": _("Organization & Structure"),
    },
    "site": {"label": _("Sites"), "app": "organization", "model_name": "site", "group": _("Organization & Structure")},
    "assetholder": {
        "label": _("Asset Holders"),
        "app": "organization",
        "model_name": "assetholder",
        "group": _("Organization & Structure"),
    },
    "role": {
        "label": _("Roles & Permissions"),
        "app": "organization",
        "model_name": "role",
        "group": _("Organization & Structure"),
    },
    "membership": {
        "label": _("Memberships"),
        "app": "organization",
        "model_name": "membership",
        "group": _("Organization & Structure"),
    },
    "rolegrant": {
        "label": _("Role Grants"),
        "app": "organization",
        "model_name": "rolegrant",
        "group": _("Organization & Structure"),
    },
    "tenantresourcegrant": {
        "label": _("Resource Grants"),
        "app": "organization",
        "model_name": "tenantresourcegrant",
        "group": _("Organization & Structure"),
    },
    "region": {
        "label": _("Regions"),
        "app": "organization",
        "model_name": "region",
        "group": _("Organization & Structure"),
    },
    "sitegroup": {
        "label": _("Site Groups"),
        "app": "organization",
        "model_name": "sitegroup",
        "group": _("Organization & Structure"),
    },
    "tenantgroup": {
        "label": _("Tenant Groups"),
        "app": "organization",
        "model_name": "tenantgroup",
        "group": _("Organization & Structure"),
    },
    "contact": {
        "label": _("Contacts"),
        "app": "organization",
        "model_name": "contact",
        "group": _("Organization & Structure"),
    },
    "contactrole": {
        "label": _("Contact Roles"),
        "app": "organization",
        "model_name": "contactrole",
        "group": _("Organization & Structure"),
    },
    # Metadata & Settings
    "manufacturer": {
        "label": _("Manufacturers"),
        "app": "assets",
        "model_name": "manufacturer",
        "group": _("Metadata & Settings"),
    },
    "supplier": {
        "label": _("Suppliers"),
        "app": "assets",
        "model_name": "supplier",
        "group": _("Metadata & Settings"),
    },
    "statuslabel": {
        "label": _("Status Labels"),
        "app": "assets",
        "model_name": "statuslabel",
        "group": _("Metadata & Settings"),
    },
    "category": {
        "label": _("Categories"),
        "app": "assets",
        "model_name": "category",
        "group": _("Metadata & Settings"),
    },
    "depreciation": {
        "label": _("Depreciation Schedules"),
        "app": "assets",
        "model_name": "depreciation",
        "group": _("Metadata & Settings"),
    },
    "assettype": {
        "label": _("Asset Types"),
        "app": "assets",
        "model_name": "assettype",
        "group": _("Metadata & Settings"),
    },
    "customfield": {
        "label": _("Custom Fields"),
        "app": "extras",
        "model_name": "customfield",
        "group": _("Metadata & Settings"),
    },
    "tag": {"label": _("Tags"), "app": "extras", "model_name": "tag", "group": _("Metadata & Settings")},
    # System & Reporting
    "reporttemplate": {
        "label": _("Report Templates"),
        "app": "extras",
        "model_name": "reporttemplate",
        "group": _("System & Reporting"),
    },
    "scheduledreport": {
        "label": _("Scheduled Reports"),
        "app": "extras",
        "model_name": "scheduledreport",
        "group": _("System & Reporting"),
    },
    "alertrule": {
        "label": _("Alert Rules"),
        "app": "extras",
        "model_name": "alertrule",
        "group": _("System & Reporting"),
    },
    "alertlog": {"label": _("Alert Logs"), "app": "extras", "model_name": "alertlog", "group": _("System & Reporting")},
    "notificationchannel": {
        "label": _("Notification Channels"),
        "app": "extras",
        "model_name": "notificationchannel",
        "group": _("System & Reporting"),
    },
    "exporttemplate": {
        "label": _("Export Templates"),
        "app": "extras",
        "model_name": "exporttemplate",
        "group": _("System & Reporting"),
    },
    "webhookendpoint": {
        "label": _("Webhook Endpoints"),
        "app": "extras",
        "model_name": "webhookendpoint",
        "group": _("System & Reporting"),
    },
    "eventrule": {
        "label": _("Event Rules"),
        "app": "extras",
        "model_name": "eventrule",
        "group": _("System & Reporting"),
    },
    "labeltemplate": {
        "label": _("Label Templates"),
        "app": "extras",
        "model_name": "labeltemplate",
        "group": _("System & Reporting"),
    },
    "recyclebin": {
        "label": _("Recycle Bin"),
        "app": "core",
        "model_name": "recyclebin",
        "group": _("System & Reporting"),
    },
    # Compliance & Custody
    "custodytemplate": {
        "label": _("Custody Templates"),
        "app": "compliance",
        "model_name": "custodytemplate",
        "group": _("Compliance & Custody"),
    },
    "custodyreceipt": {
        "label": _("Custody Receipts"),
        "app": "compliance",
        "model_name": "custodyreceipt",
        "group": _("Compliance & Custody"),
    },
    "assetmaintenance": {
        "label": _("Asset Maintenances"),
        "app": "assets",
        "model_name": "assetmaintenance",
        "group": _("Inventory & Hardware"),
    },
    "user": {"label": _("Users"), "app": "users", "model_name": "user", "group": _("User Management")},
    "token": {"label": _("API Tokens"), "app": "users", "model_name": "token", "group": _("User Management")},
    "usergroup": {"label": _("User Groups"), "app": "users", "model_name": "usergroup", "group": _("User Management")},
    "warranty": {
        "label": _("Warranties"),
        "app": "assets",
        "model_name": "warranty",
        "group": _("Inventory & Hardware"),
    },
    "assetdisposal": {
        "label": _("Asset Disposals"),
        "app": "assets",
        "model_name": "assetdisposal",
        "group": _("Inventory & Hardware"),
    },
    "assetreservation": {
        "label": _("Asset Reservations"),
        "app": "assets",
        "model_name": "assetreservation",
        "group": _("Inventory & Hardware"),
    },
    "contract": {
        "label": _("Contracts"),
        "app": "procurement",
        "model_name": "contract",
        "group": _("Inventory & Hardware"),
    },
    "installedsoftware": {
        "label": _("Installed Software"),
        "app": "software",
        "model_name": "installedsoftware",
        "group": _("Software & Subscriptions"),
    },
    "assetrole": {
        "label": _("Asset Roles"),
        "app": "assets",
        "model_name": "assetrole",
        "group": _("Metadata & Settings"),
    },
    "costcenter": {
        "label": _("Cost Centers"),
        "app": "organization",
        "model_name": "costcenter",
        "group": _("Organization & Structure"),
    },
    "journalentry": {
        "label": _("Journal Entries"),
        "app": "extras",
        "model_name": "journalentry",
        "group": _("System & Reporting"),
    },
    "configcontext": {
        "label": _("Config Contexts"),
        "app": "extras",
        "model_name": "configcontext",
        "group": _("System & Reporting"),
    },
}


def _validate_declarations() -> None:
    semantic_keys = tuple(target.key for target in ROLE_PERMISSION_TARGETS)
    presentation_keys = tuple(ROLE_PERMISSION_PRESENTATION)
    semantic_key_set = set(semantic_keys)
    presentation_key_set = set(presentation_keys)
    missing = tuple(key for key in semantic_keys if key not in presentation_key_set)
    extra = tuple(key for key in presentation_keys if key not in semantic_key_set)
    if missing or extra:
        raise RuntimeError(f"Role permission matrix declarations differ: missing={missing!r} extra={extra!r}")


def _plugin_permission_presentations():
    """Derive presentation rows from the active plugins' declared model targets."""
    result = {}
    for target in active_role_permission_targets():
        if target.plugin_name is None:
            continue
        model = apps.get_model(target.app, target.model)
        app_config = apps.get_app_config(target.app)
        result[target.key] = {
            "label": model._meta.verbose_name_plural,
            "app": target.app,
            "model_name": target.model,
            "group": app_config.verbose_name,
        }
    return result


def build_matrix_models():
    """Build the core matrix and append rows declared by active plugins."""

    _validate_declarations()
    result = dict(ROLE_PERMISSION_PRESENTATION)
    result.update(_plugin_permission_presentations())
    return result


MATRIX_MODELS = build_matrix_models()


def refresh_matrix_models() -> None:
    """Refresh exported rows in place after the final plugin registers its targets."""
    refreshed = build_matrix_models()
    MATRIX_MODELS.clear()
    MATRIX_MODELS.update(refreshed)
