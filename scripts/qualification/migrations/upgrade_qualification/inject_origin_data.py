"""Inject representative data into the supported-predecessor database.

Runs inside the predecessor checkout, so every row is created through the
model state that a supported upgrade starts from.
"""

from assets.models import Asset, AssetType, Manufacturer
from organization.models import Tenant

tenant = Tenant.objects.create(name="Upgrade Rehearsal Tenant", slug="upgrade-rehearsal-tenant")

manufacturer = Manufacturer.objects.create(name="Upgrade Rehearsal Manufacturer", slug="upgrade-rehearsal-manufacturer")
asset_type = AssetType.objects.create(
    manufacturer=manufacturer,
    model="Upgrade Switch",
    slug="upgrade-switch",
    custom_field_data={"memory_capacity": "16.000", "storage_capacity": "512.000"},
)
Asset.objects.create(
    name="Upgrade Switch 01",
    asset_type=asset_type,
    tenant=tenant,
    custom_field_data={"hostname": "up-01", "firmware_version": "1.2.3"},
)

print(f"INJECTED asset_type={asset_type.pk} tenant={tenant.pk}")
