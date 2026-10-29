"""Procurement seed mixin: purchase orders, order lines, PO-fulfilled assets.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.procurement import SeedProcurementMixin

    class Command(SeedProcurementMixin, BaseCommand):
        ...

``_seed_procurement`` must run after ``_seed_assets`` (it reads
``self._tenants`` / ``self._tenant_locations`` / ``self._tenant_meta`` /
``self._suppliers`` / ``self._provisioner`` / ``self._asset_types`` /
``self._accessories`` / ``self._status_labels`` and the ``PROFILES`` /
``PRICES`` / ``HW_SUPPLIERS`` class attributes from the org / assets mixins).
"""

import datetime
import random

from django.core.management import CommandError

from assets.models import Asset
from procurement.models import PurchaseOrder, PurchaseOrderLine

TODAY = datetime.date.today()


def days_ago(n):
    return TODAY - datetime.timedelta(days=n)


class SeedProcurementMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    def _unique_serial(self, code: str) -> str:
        """Return a serial number that is unused for this tenant's active assets.

        ``(tenant, serial_number)`` is unique among non-deleted assets, and the PO
        phase now materialises one asset per received unit, so a plain random draw
        can collide and abort the seed. Probing the live table keeps the draw
        collision-free without changing the deterministic random stream that later
        phases depend on.
        """
        # inline import: app-registry: assets.Asset is queried inside the seed command,
        # where the model import must not happen at module load.
        from assets.models import Asset

        for _ in range(1000):
            candidate = f"{code}{random.randint(100000, 999999)}"
            if not Asset._base_manager.filter(serial_number=candidate).exists():
                return candidate
        raise CommandError(
            "Seed procurement invariant failed: could not draw a free serial number for tenant code "
            f"{code} after 1000 attempts."
        )

    def _seed_procurement(self):

        self.stdout.write("--- Procurement ---")
        po_count = 0
        line_count = 0
        fulfilled = 0
        statuses = ["ordered", "partial", "received", "draft", "approved"]
        target_slugs = [
            "northwind-internal-it",
            "helix-rnd",
            "meridian-retail",
            "meridian-investment",
            "sterling-portfolio",
            "brightwell-legal",
            "aurora-architects",
            "vantage-logistics",
        ]
        for i, slug in enumerate(target_slugs):
            tenant = self._tenants.get(slug)
            locs = self._tenant_locations.get(slug)
            if not (tenant and locs):
                continue
            meta = self._tenant_meta[slug]
            status = statuses[i % len(statuses)]
            order_date = days_ago(random.randint(5, 90))
            po = PurchaseOrder.objects.create(
                tenant=tenant,
                order_number=f"{meta['code']}-PO-{order_date.year}-{1000 + i}",
                supplier=self._suppliers[random.choice(self.HW_SUPPLIERS)],
                status=status,
                order_date=order_date,
                expected_delivery_date=order_date + datetime.timedelta(days=21),
                destination_location=locs[0],
                created_by=self._provisioner,
                notes="Quarterly hardware refresh order.",
            )
            po_count += 1
            laptop = random.choice(self.PROFILES[meta["profile"]]["laptops"])
            lines = [
                ("asset_type", laptop, random.randint(3, 10)),
                ("accessory", "tb4-dock", random.randint(3, 10)),
                ("asset_type", random.choice(["dell-p2723de-monitor", "dell-p2422he-monitor"]), random.randint(4, 12)),
            ]
            for kind, key, qty in lines:
                received = qty if status == "received" else (qty // 2 if status == "partial" else 0)
                kwargs = dict(
                    purchase_order=po,
                    tenant=tenant,
                    qty_ordered=qty,
                    qty_received=received,
                    unit_price=round(self.PRICES.get(key, 100) if kind == "asset_type" else 250, 2),
                )
                if kind == "asset_type":
                    kwargs["asset_type"] = self._asset_types[key]
                else:
                    kwargs["accessory"] = self._accessories[key]
                line = PurchaseOrderLine.objects.create(**kwargs)
                line_count += 1
                # Received asset-type lines materialise into real Assets that point back
                # to the originating PO line (Asset.purchase_order_line) — closes the
                # order → inventory loop instead of leaving received qty abstract.
                #
                # Every received unit becomes an asset: a line received with qty 10 must
                # show 10 assets, or the PO's receipt claim and the asset ledger
                # contradict each other and total-cost reports sum units that do not
                # exist. The serial number is drawn collision-free because
                # (tenant, serial_number) is unique among active rows and the received
                # volume per tenant is far larger than before.
                if kind == "asset_type" and received:
                    atype = self._asset_types[key]
                    for _n in range(received):
                        cost = round(float(line.unit_price or 0) * random.uniform(0.97, 1.03), 2)
                        a = Asset(
                            name=atype.model,
                            asset_tag="",
                            asset_type=atype,
                            asset_role=atype.asset_role,
                            status=self._status_labels["available"],
                            location=locs[0],
                            tenant=tenant,
                            purchase_order_line=line,
                            serial_number=self._unique_serial(meta["code"]),
                            purchase_cost=cost,
                            salvage_value=round(cost * 0.1, 2),
                            purchase_date=order_date,
                            in_service_date=order_date,
                            order_number=po.order_number,
                            supplier=po.supplier,
                            notes="Received against purchase order; awaiting deployment.",
                        )
                        a.save()  # asset_tag drawn from the tenant's AssetTagSequence
                        fulfilled += 1
        self.stdout.write(
            f"  {po_count} purchase orders, {line_count} order lines, {fulfilled} assets received against PO lines."
        )
