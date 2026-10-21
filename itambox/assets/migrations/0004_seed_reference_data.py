"""Reference data every installation needs: asset status labels and depreciation policies."""

from django.db import migrations

STATUS_LABELS = (
    ("Available", "available", "deployable", "28a745"),
    ("In Use", "in-use", "deployed", "007bff"),
    ("Pending Repair", "pending-repair", "pending", "ffc107"),
    ("Retired", "retired", "archived", "dc3545"),
    ("In Transit", "in-transit", "pending", "6f42c1"),
    ("Decommissioned", "decommissioned", "undeployable", "6c757d"),
    ("Quarantined", "quarantined", "pending", "fd7e14"),
    ("Missing", "missing", "undeployable", "dc3545"),
)

DEPRECIATION_POLICIES = (
    {
        "name": "IT-Hardware 36 Monate (AfA)",
        "months": 36,
        "method": "straight_line",
        "convention": "include_purchase_month",
        "description": "AfA-Tabelle 2021 — Computer, Notebooks, Tablets: 3 Jahre",
    },
    {
        "name": "Server 60 Monate (AfA)",
        "months": 60,
        "method": "straight_line",
        "convention": "include_purchase_month",
        "description": "AfA-Tabelle 2021 — Server / Workstations: 5 Jahre",
    },
    {
        "name": "Sofortabschreibung GWG (≤ 800 €)",
        "months": 1,
        "method": "straight_line",
        "convention": "include_purchase_month",
        "immediate_expense_threshold": "800.00",
        "description": "Geringwertige Wirtschaftsgüter §6 Abs. 2 EStG — Sofortabschreibung bis 800 €",
    },
)


def seed_reference_data(apps, schema_editor):
    status_label = apps.get_model("assets", "StatusLabel")
    for name, slug, status_type, color in STATUS_LABELS:
        status_label.objects.update_or_create(
            slug=slug,
            defaults={"name": name, "type": status_type, "color": color},
        )

    depreciation = apps.get_model("assets", "Depreciation")
    for policy in DEPRECIATION_POLICIES:
        depreciation.objects.update_or_create(
            name=policy["name"],
            defaults={key: value for key, value in policy.items() if key != "name"},
        )


def unseed_reference_data(apps, schema_editor):
    apps.get_model("assets", "StatusLabel").objects.filter(
        slug__in=[item[1] for item in STATUS_LABELS]
    ).delete()
    apps.get_model("assets", "Depreciation").objects.filter(
        name__in=[item["name"] for item in DEPRECIATION_POLICIES]
    ).delete()


class Migration(migrations.Migration):
    replaces = [
        ("assets", "0037_remove_auditsession_created_by_and_more"),
        ("assets", "0038_assetmaintenance"),
        ("assets", "0039_repoint_assetmaintenance_contenttype"),
        ("assets", "0040_null_to_empty_strings"),
        ("assets", "0041_category_audit_interval_months"),
        ("assets", "0042_depreciation_v2"),
        ("assets", "0043_seed_depreciation_policies"),
        ("assets", "0044_assetrole_allows_components"),
        ("assets", "0045_asset_uniq_active_tenant_serial"),
        ("assets", "0046_asset_currency_assetmaintenance_currency_and_more"),
        ("assets", "0047_assetdisposal"),
        ("assets", "0048_remove_asset_warranty_expiration_asset_cost_center_and_more"),
    ]

    dependencies = [
        ("assets", "0003_cross_app_relations_late"),
    ]

    operations = [
        migrations.RunPython(seed_reference_data, unseed_reference_data),
    ]
