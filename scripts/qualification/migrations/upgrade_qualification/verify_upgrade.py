"""Verify data integrity, constraints, guards and tenant isolation after the
supported upgrade.

Runs inside the candidate checkout against the upgraded database.
"""

import json

from django.db import DatabaseError, connection, transaction

from assets.models import Asset, AssetType
from extras.models import CustomField, CustomFieldChoiceSet

result = {}

asset_type = AssetType.objects.get(slug="upgrade-switch")
expected_type_data = {"memory_capacity": "16.000", "storage_capacity": "512.000"}
assert asset_type.custom_field_data == expected_type_data, asset_type.custom_field_data
result["asset_type_custom_field_data"] = asset_type.custom_field_data

asset = Asset.objects.get(name="Upgrade Switch 01")
expected_asset_data = {"hostname": "up-01", "firmware_version": "1.2.3"}
assert asset.custom_field_data == expected_asset_data, asset.custom_field_data
result["asset_custom_field_data"] = asset.custom_field_data

# Tenant ownership survives and stays isolated.
assert asset.tenant is not None and asset.tenant.slug == "upgrade-rehearsal-tenant"
tenant_assets = Asset.objects.filter(tenant__slug="upgrade-rehearsal-tenant")
assert list(tenant_assets.values_list("name", flat=True)) == ["Upgrade Switch 01"]
assert not Asset.objects.filter(tenant__isnull=True, name="Upgrade Switch 01").exists()
result["tenant_isolation"] = "ok"

# Application-level read/write after the upgrade.
with transaction.atomic():
    asset_type.custom_field_data = {**asset_type.custom_field_data, "memory_capacity": "32.000"}
    asset_type.save(update_fields=["custom_field_data"])
asset_type.refresh_from_db()
assert asset_type.custom_field_data["memory_capacity"] == "32.000"
result["post_upgrade_write"] = asset_type.custom_field_data["memory_capacity"]

# The shipped core vocabulary is untouched by the upgrade.
result["core_fields"] = CustomField.objects.filter(namespace="itambox", management_kind="core").count()
result["core_choice_sets"] = CustomFieldChoiceSet.objects.filter(namespace="itambox", management_kind="core").count()
assert result["core_fields"] and result["core_choice_sets"]

# The identity guards hold on the upgraded database.
with connection.cursor() as cursor:
    try:
        cursor.execute("UPDATE extras_customfield SET name = 'renamed' WHERE namespace = 'itambox'")
        raise AssertionError("definition identity update unexpectedly succeeded")
    except DatabaseError as exc:
        assert "immutable" in str(exc), exc
result["identity_guard"] = "ok"

print("VERIFY " + json.dumps(result, sort_keys=True))
