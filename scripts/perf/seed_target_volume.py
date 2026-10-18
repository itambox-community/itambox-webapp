"""Create the deterministic ITAMbox beta.4 target-volume benchmark dataset.

Run only against an explicitly named disposable PostgreSQL database. The script
uses bounded ORM bulk inserts and refuses to run against a non-empty core data
set; a failed run should be discarded by dropping its database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ITAMBOX_ROOT = REPO_ROOT / "itambox"
sys.path.insert(0, str(ITAMBOX_ROOT))
os.environ.setdefault("ITAMBOX_ENV", "dev")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings.dev")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402

from assets.choices import StatusTypeChoices  # noqa: E402
from assets.models import Asset, AssetType, Manufacturer, StatusLabel  # noqa: E402
from extras.models import CustomField, JournalEntry  # noqa: E402
from organization.models import AssetHolder, Location, Site, Tenant, TenantGroup  # noqa: E402

BATCH_SIZE = 2_000


class ReferenceData(TypedDict):
    tenant_group_id: int
    tenant_ids: list[int]
    location_ids: list[int]
    asset_type_ids: list[int]
    status_id: int
    manufacturer_count: int
    asset_type_count: int
    location_count: int
    tenant_count: int
    holder_count: int
    custom_field_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable", action="store_true", help="confirm the DB is disposable")
    parser.add_argument("--seed", type=int, default=882, help="stable synthetic data seed (default: 882)")
    parser.add_argument("--scale", type=float, default=1.0, help="scale target counts for a smoke run")
    parser.add_argument("--manifest-out", type=Path, help="write the generated dataset manifest to this path")
    parser.add_argument("--username", default="perf-882-admin", help="benchmark superuser name")
    parser.add_argument("--assets", type=int, default=100_000)
    parser.add_argument("--holders", type=int, default=2_000)
    parser.add_argument("--tenants", type=int, default=500)
    parser.add_argument("--journals", type=int, default=250_000)
    args = parser.parse_args()
    if args.scale <= 0:
        parser.error("--scale must be positive")
    return args


def require_disposable_database(confirmed: bool) -> str:
    database_name = str(connection.settings_dict.get("NAME", ""))
    if connection.vendor != "postgresql":
        raise RuntimeError("target-volume generation requires PostgreSQL")
    if not confirmed:
        raise RuntimeError("refusing to write without --confirm-disposable")
    if not database_name.startswith("perf_qual_"):
        raise RuntimeError("refusing to write: database name must start with 'perf_qual_'")
    return database_name


def require_empty_core_dataset() -> None:
    populated = {model._meta.label: model._base_manager.count() for model in (Asset, AssetHolder, JournalEntry, Tenant)}
    nonempty = {label: count for label, count in populated.items() if count}
    if nonempty:
        raise RuntimeError(f"refusing to seed a non-empty benchmark database: {nonempty}")


def scaled_count(value: int, scale: float) -> int:
    return max(1, round(value * scale))


def create_admin(username: str) -> Any:
    password = os.environ.get("PERF_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("PERF_ADMIN_PASSWORD must be set; its value is never printed")
    user_model = get_user_model()
    if user_model._base_manager.filter(username=username).exists():
        raise RuntimeError(f"benchmark user {username!r} already exists")
    return user_model.objects.create_superuser(
        username=username,
        email="perf-882@example.invalid",
        password=password,
    )


def create_reference_data(tenant_count: int, holder_count: int, asset_count: int) -> ReferenceData:
    group = TenantGroup.objects.create(name="Perf 882 Target", slug="perf-882-target")
    tenants = Tenant._base_manager.bulk_create(
        [
            Tenant(name=f"Perf 882 Tenant {index:04d}", slug=f"perf-882-tenant-{index:04d}", group=group)
            for index in range(tenant_count)
        ],
        batch_size=BATCH_SIZE,
    )
    tenant_ids = [tenant.pk for tenant in tenants]

    sites = Site._base_manager.bulk_create(
        [
            Site(
                name=f"Perf 882 Site {index:04d}",
                slug=f"perf-882-site-{index:04d}",
                tenant_id=tenant_ids[index],
            )
            for index in range(tenant_count)
        ],
        batch_size=BATCH_SIZE,
    )
    locations = Location._base_manager.bulk_create(
        [
            Location(
                name=f"Perf 882 Location {index:04d}-{slot}",
                slug=f"perf-882-location-{index:04d}-{slot}",
                tenant_id=tenant_ids[index],
                site_id=sites[index].pk,
            )
            for index in range(tenant_count)
            for slot in range(2)
        ],
        batch_size=BATCH_SIZE,
    )

    AssetHolder._base_manager.bulk_create(
        [
            AssetHolder(
                first_name=f"Perf {index:05d}",
                last_name=f"Holder {index % 251:03d}",
                upn=f"perf-882-{index:05d}@example.invalid",
                email=f"perf-882-{index:05d}@example.invalid",
                tenant_id=tenant_ids[index % tenant_count],
            )
            for index in range(holder_count)
        ],
        batch_size=BATCH_SIZE,
    )

    manufacturer_count = max(1, min(25, tenant_count))
    manufacturers = Manufacturer._base_manager.bulk_create(
        [
            Manufacturer(name=f"Perf 882 Manufacturer {index:03d}", slug=f"perf-882-mfr-{index:03d}")
            for index in range(manufacturer_count)
        ],
        batch_size=BATCH_SIZE,
    )
    type_count = max(1, min(100, asset_count))
    asset_types = AssetType._base_manager.bulk_create(
        [
            AssetType(
                manufacturer_id=manufacturers[index % manufacturer_count].pk,
                model=f"Perf 882 Model {index:03d}",
                slug=f"perf-882-type-{index:03d}",
                description=f"Synthetic performance reference type {index:03d}",
            )
            for index in range(type_count)
        ],
        batch_size=BATCH_SIZE,
    )
    asset_content_type = ContentType.objects.get_for_model(Asset)
    custom_fields = CustomField._base_manager.bulk_create(
        [
            CustomField(
                name=name,
                namespace="local",
                label=label,
                field_type=CustomField.FIELD_TYPE_TEXT,
                activation=CustomField.ACTIVATION_GLOBAL,
                nullable=True,
            )
            for name, label in (
                ("perf882_department", "Performance department"),
                ("perf882_service_band", "Performance service band"),
                ("perf882_ticket_group", "Performance ticket group"),
            )
        ],
        batch_size=BATCH_SIZE,
    )
    for custom_field in custom_fields:
        custom_field.object_types.add(asset_content_type)

    status = StatusLabel._base_manager.filter(type=StatusTypeChoices.DEPLOYABLE).order_by("pk").first()
    if status is None:
        status = StatusLabel.objects.create(
            name="Perf 882 Deployable",
            slug="perf-882-deployable",
            type=StatusTypeChoices.DEPLOYABLE,
        )
    location_ids = [location.pk for location in locations]
    return {
        "tenant_group_id": group.pk,
        "tenant_ids": tenant_ids,
        "location_ids": location_ids,
        "asset_type_ids": [asset_type.pk for asset_type in asset_types],
        "status_id": status.pk,
        "manufacturer_count": manufacturer_count,
        "asset_type_count": type_count,
        "location_count": len(location_ids),
        "tenant_count": tenant_count,
        "holder_count": holder_count,
        "custom_field_count": len(custom_fields),
    }


def create_assets(seed: int, count: int, references: ReferenceData) -> tuple[list[int], list[int]]:
    tenant_ids = references["tenant_ids"]
    location_ids = references["location_ids"]
    asset_type_ids = references["asset_type_ids"]
    status_id = references["status_id"]
    asset_ids: list[int] = []
    asset_tenant_ids: list[int] = []
    for start in range(0, count, BATCH_SIZE):
        batch = []
        end = min(start + BATCH_SIZE, count)
        for index in range(start, end):
            tenant_index = (index * 37 + seed) % len(tenant_ids)
            type_index = (index * 13 + seed) % len(asset_type_ids)
            location_index = tenant_index * 2 + ((index // len(tenant_ids)) % 2)
            batch.append(
                Asset(
                    name=f"Perf 882 Asset {index:06d}",
                    asset_tag=f"PF882-{index:06d}",
                    serial_number=f"PF882-SN-{index:06d}",
                    notes=f"PERF-SEARCH-{index % 1000:04d}",
                    tenant_id=tenant_ids[tenant_index],
                    location_id=location_ids[location_index],
                    asset_type_id=asset_type_ids[type_index],
                    status_id=status_id,
                    custom_field_data={
                        "perf882_department": f"D{tenant_index % 50:02d}",
                        "perf882_service_band": f"B{index % 10}",
                        "perf882_ticket_group": f"TG-{index % 250:03d}",
                    },
                )
            )
        created = Asset._base_manager.bulk_create(batch, batch_size=BATCH_SIZE)
        asset_ids.extend([asset.pk for asset in created])
        asset_tenant_ids.extend([asset.tenant_id for asset in created])
    if len(asset_ids) != count or any(asset_id is None for asset_id in asset_ids):
        raise RuntimeError("PostgreSQL did not return every generated asset primary key")
    return asset_ids, asset_tenant_ids


def create_journal_entries(seed: int, count: int, asset_ids: list[int], asset_tenants: list[int], user_id: int) -> None:
    content_type = ContentType.objects.get_for_model(Asset)
    for start in range(0, count, BATCH_SIZE):
        end = min(start + BATCH_SIZE, count)
        batch = []
        for index in range(start, end):
            asset_index = (index * 17 + seed) % len(asset_ids)
            batch.append(
                JournalEntry(
                    model_id=content_type.pk,
                    object_id=asset_ids[asset_index],
                    user_id=user_id,
                    tenant_id=asset_tenants[asset_index],
                    comment=f"Perf 882 journal {index:06d} bucket {index % 1000:04d}",
                )
            )
        JournalEntry._base_manager.bulk_create(batch, batch_size=BATCH_SIZE)


def build_manifest(
    database_name: str,
    seed: int,
    references: ReferenceData,
    asset_ids: list[int],
    asset_count: int,
    holder_count: int,
    journal_count: int,
) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        database_size = cursor.fetchone()[0]
    return {
        "dataset": "itambox-beta4-target-volume",
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "seed": seed,
        "database": database_name,
        "counts": {
            "assets": asset_count,
            "asset_holders": holder_count,
            "tenants": references["tenant_count"],
            "tenant_groups": 1,
            "journal_entries": journal_count,
            "sites": references["tenant_count"],
            "locations": references["location_count"],
            "manufacturers": references["manufacturer_count"],
            "asset_types": references["asset_type_count"],
            "custom_field_values": asset_count * 3,
            "custom_field_value_keys_per_asset": 3,
            "custom_field_definitions": references["custom_field_count"],
        },
        "database_size_bytes": database_size,
        "benchmark": {
            "asset_id": asset_ids[0],
            "filtered_asset_id": asset_ids[123 % len(asset_ids)],
            "search_term": "PERF-SEARCH-0042",
            "filtered_q": f"PF882-SN-{123:06d}",
            "tenant_id": references["tenant_ids"][0],
            "tenant_group_id": references["tenant_group_id"],
            "import_tenant_id": references["tenant_ids"][0],
            "asset_type_id": references["asset_type_ids"][0],
            "status_id": references["status_id"],
        },
        "created_at": timezone.now().isoformat(),
    }


def main() -> int:
    args = parse_args()
    database_name = require_disposable_database(args.confirm_disposable)
    require_empty_core_dataset()
    tenant_count = scaled_count(args.tenants, args.scale)
    asset_count = scaled_count(args.assets, args.scale)
    holder_count = scaled_count(args.holders, args.scale)
    journal_count = scaled_count(args.journals, args.scale)
    started = time.perf_counter()
    user = create_admin(args.username)
    references = create_reference_data(tenant_count, holder_count, asset_count)
    # The relationship map and generated identities are deterministic for the seed.
    asset_ids, asset_tenants = create_assets(args.seed, asset_count, references)
    create_journal_entries(args.seed, journal_count, asset_ids, asset_tenants, user.pk)
    manifest = build_manifest(
        database_name,
        args.seed,
        references,
        asset_ids,
        asset_count,
        holder_count,
        journal_count,
    )
    manifest["generation_seconds"] = round(time.perf_counter() - started, 3)
    manifest["target_deviation"] = (
        None
        if (
            args.scale == 1.0
            and (asset_count, holder_count, tenant_count, journal_count) == (100_000, 2_000, 500, 250_000)
        )
        else "non-default counts/scale were requested"
    )
    output = json.dumps(manifest, sort_keys=True, indent=2)
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
