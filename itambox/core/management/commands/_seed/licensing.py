"""Licensing seed mixin: per-tenant software licenses + seat assignments.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.licensing import SeedLicensingMixin

    class Command(SeedLicensingMixin, BaseCommand):
        ...

``_seed_licensing`` must run after ``_seed_assets`` (it reads
``self._tenants`` / ``self._tenant_meta`` / ``self._tenant_holders`` /
``self._software`` / ``self._primary_laptop_by_holder``). It populates
``self._licenses`` (consumed by the operations + contracts/costing phases).
"""

import datetime
import random

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from licenses.models import License, LicenseSeatAssignment

TODAY = datetime.date.today()


def days_ago(n):
    return TODAY - datetime.timedelta(days=n)


def days_ahead(n):
    return TODAY + datetime.timedelta(days=n)


class SeedLicensingMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    def _seed_licensing(self):

        self.stdout.write("--- Licensing ---")
        self._licenses = []
        seat_assigns = 0
        for slug, tenant in self._tenants.items():
            meta = self._tenant_meta[slug]
            holders = self._tenant_holders[slug]
            hc = max(len(holders), 5)
            code = meta["code"]
            plan = [
                ("Microsoft 365 E5", "subscription_seat", round(hc * 1.2) + 5, 57 * (round(hc * 1.2) + 5), True),
                ("CrowdStrike Falcon", "subscription_seat", round(hc * 1.2) + 5, 60 * (round(hc * 1.2) + 5), True),
                ("1Password Business", "subscription_seat", round(hc * 1.1) + 5, 8 * (round(hc * 1.1) + 5), True),
                ("Windows 11 Enterprise", "perpetual_seat", hc + 10, None, False),
            ]
            if meta["industry"] == "Pharmaceuticals":
                plan.append(("SAS Analytics Pro", "subscription_seat", 15, 45000, True))
            if meta["industry"] == "Architecture & Design":
                plan.append(("Autodesk AutoCAD", "subscription_seat", 12, 24000, True))
            if meta["industry"] in ("Banking", "Asset Management"):
                plan.append(("Bloomberg Terminal", "subscription_seat", 8, 192000, True))
            for sw_name, ltype, seats, cost, has_expiry in plan:
                expiry = days_ahead(random.choice([18, 25, 40, 90, 180, 365])) if has_expiry else None
                lic = License.objects.create(
                    name=sw_name,
                    software=self._software[sw_name],
                    license_type=ltype,
                    product_key=("" if ltype != "perpetual_seat" else f"{code}-XXXXX-YYYYY-ZZZZZ"),
                    seats=seats,
                    purchase_cost=cost,
                    purchase_date=days_ago(random.randint(60, 600)),
                    order_number=f"PO-SW-{random.randint(1000, 9999)}",
                    tenant=tenant,
                    expiration_date=expiry,
                )
                self._licenses.append(lic)
                # Assign seats to a sample of holders for the seat-based subscriptions.
                # Per-user products (e.g. Microsoft 365 E5) are user-bound — the seat
                # targets the holder. Per-device products (e.g. CrowdStrike Falcon, an
                # endpoint agent) are device-bound — the seat targets the holder's
                # primary laptop when known. The model enforces asset XOR holder.
                DEVICE_BOUND_SOFTWARE = {"CrowdStrike Falcon"}
                if ltype == "subscription_seat" and holders and sw_name in ("Microsoft 365 E5", "CrowdStrike Falcon"):
                    device_bound = sw_name in DEVICE_BOUND_SOFTWARE
                    for h in random.sample(holders, k=min(len(holders), max(3, len(holders) // 2))):
                        laptop = self._primary_laptop_by_holder.get(h.pk)
                        try:
                            # Savepoint: the whole seed runs in one transaction, which a
                            # swallowed IntegrityError would otherwise leave unusable.
                            with transaction.atomic():
                                if device_bound and laptop is not None:
                                    LicenseSeatAssignment.objects.create(license=lic, asset=laptop)
                                else:
                                    LicenseSeatAssignment.objects.create(license=lic, assigned_holder=h)
                        except (IntegrityError, ValidationError):
                            # The sample draw can repeat a target or exceed the seat count;
                            # the demo data just skips that seat.
                            continue
                        seat_assigns += 1
        self.stdout.write(f"  {len(self._licenses)} licenses, {seat_assigns} seat assignments.")
