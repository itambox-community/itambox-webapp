"""Lifecycle mixin for the seed command.

Adds three lifecycle phases to the MSP demo dataset:
  1. **Warranties** — supplemental coverage (accidental, extended, on-site, parts-labor)
     layered on top of the basic hardware warranties already created by _seed_assets.
  2. **Reservations** — PAST (fulfilled), ACTIVE (now), FUTURE (pending) per tenant,
     with a realistic loaner subset.
  3. **Disposals** — end-of-life records for self._retired_assets + a 5 % random sample.

Wire-up (in seed_data.py):
    from core.management.commands._seed.lifecycle import SeedLifecycleMixin
    class Command(SeedLifecycleMixin, BaseCommand): ...
    # then add  self._seed_lifecycle()  inside _seed_all()
"""

import datetime
import random

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from assets.models import (
    AssetDisposal,
    AssetReservation,
    DataSanitizationMethodChoices,
    DisposalMethodChoices,
    ReservationStatusChoices,
    Warranty,
    WarrantyTypeChoices,
)
from assets.models import AssetDisposal as _AD
from core.management.commands._seed.engine import ChangeLogEngine


class SeedLifecycleMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _as_date(d):
        """Coerce date/datetime to date."""
        if isinstance(d, datetime.datetime):
            return d.date()
        return d

    def _tenant_currency(self, asset):
        """The asset tenant's configured currency, EUR when unknown."""
        tenant_meta = getattr(self, "_tenant_meta", None)
        if not tenant_meta or not asset.tenant_id:
            return "EUR"
        return tenant_meta.get(asset.tenant.slug, {}).get("currency", "EUR")

    def _ensure_engine(self):
        """Lazily instantiate self._engine if the wiring step hasn't done it yet."""
        if not hasattr(self, "_engine") or self._engine is None:
            self._engine = ChangeLogEngine(stdout=self.stdout, style=self.style)

    # ------------------------------------------------------------------ main entry
    def _seed_lifecycle(self):  # noqa: C901  (complex-but-linear, keep together)
        """Create Warranty, AssetReservation, and AssetDisposal records with
        back-dated changelog entries so history looks naturally grown over ~2 years.
        """

        self._ensure_engine()
        eng = self._engine
        TODAY = datetime.date.today()

        actors = getattr(self, "_engineer_users", None) or []
        if not actors:
            # Fallback: any user in self._users
            actors = list((getattr(self, "_users", None) or {}).values()) or [None]

        def _pick_actor():
            return random.choice(actors)

        # ================================================================
        # 1. WARRANTIES
        # ================================================================
        self.stdout.write("--- Lifecycle: warranties ---")
        self._warranties = []

        # Supplemental coverage types we add on top of the basic HARDWARE warranty
        # that _seed_assets already creates on every asset. The warranty vendor is
        # the Supplier link, so each record keeps the asset's
        # procurement supplier instead of a free-text vendor name.
        SUPPLEMENTAL_TYPES = [
            (WarrantyTypeChoices.ACCIDENTAL, 0.35),
            (WarrantyTypeChoices.EXTENDED, 0.25),
            (WarrantyTypeChoices.ONSITE, 0.20),
            (WarrantyTypeChoices.PARTS_LABOR, 0.15),
        ]

        # Candidate assets: laptops + servers first, then the rest.
        candidate_assets = list(getattr(self, "_servers", [])) + [
            a
            for slug in (getattr(self, "_laptops_by_tenant", None) or {})
            for a in (self._laptops_by_tenant[slug] or [])
        ]
        # De-duplicate while preserving order.
        seen_pks = {a.pk for a in candidate_assets}
        for a in getattr(self, "_assets", None) or []:
            if a.pk not in seen_pks:
                candidate_assets.append(a)
                seen_pks.add(a.pk)

        # Shuffle so the coverage distribution isn't perfectly ordered.
        shuffled = list(candidate_assets)
        random.shuffle(shuffled)

        for asset in shuffled:
            p_date = (
                self._as_date(asset.purchase_date)
                if asset.purchase_date
                else (TODAY - datetime.timedelta(days=random.randint(180, 700)))
            )
            supplier = asset.supplier if asset.supplier_id and asset.supplier else None

            for wtype, prob in SUPPLEMENTAL_TYPES:
                if random.random() > prob:
                    continue

                # Duration: 1–3 years; add a small random offset from purchase_date.
                offset_days = random.randint(0, 90)  # bought shortly after asset
                start = p_date + datetime.timedelta(days=offset_days)
                years = random.choice([1, 1, 2, 2, 3])
                end = start + datetime.timedelta(days=365 * years + random.randint(-30, 30))
                if end <= start:
                    end = start + datetime.timedelta(days=365)

                currency = self._tenant_currency(asset)

                # Rough cost range by type
                cost_map = {
                    WarrantyTypeChoices.ACCIDENTAL: (59, 249),
                    WarrantyTypeChoices.EXTENDED: (149, 599),
                    WarrantyTypeChoices.ONSITE: (199, 799),
                    WarrantyTypeChoices.PARTS_LABOR: (99, 399),
                }
                lo, hi = cost_map.get(wtype, (50, 300))
                cost = round(random.uniform(lo, hi), 2)

                ref_suffix = random.randint(100000, 999999)
                reference = f"WR-{p_date.year}-{ref_suffix}"

                warranty = Warranty.objects.create(
                    asset=asset,
                    warranty_type=wtype,
                    supplier=supplier,
                    start_date=start,
                    end_date=end,
                    cost=cost,
                    currency=currency,
                    reference=reference,
                )
                eng.log_create(warranty, when=start, user=_pick_actor())
                self._warranties.append(warranty)

        self.stdout.write(f"  {len(self._warranties)} supplemental warranties created.")

        # ================================================================
        # 2. RESERVATIONS
        # ================================================================
        self.stdout.write("--- Lifecycle: reservations ---")
        self._reservations = []

        tenant_holders = getattr(self, "_tenant_holders", {})
        assets_by_tenant = getattr(self, "_assets_by_tenant", {})

        LOANER_PURPOSES = [
            "Loaner for a hardware refresh in progress",
            "Temporary loaner while the user's laptop is being repaired",
            "Loaner device for new employee onboarding",
            "Short-term loaner for a conference or event",
        ]
        REGULAR_PURPOSES = [
            "Equipment reserved for new hire",
            "Planned role transition for a seat transfer",
            "Project: infrastructure migration",
            "Temporary allocation during a department reorganization",
            "Reserved for contractor onboarding",
            "Pre-deployment staging hold",
        ]

        # A unit already handed to an open request must stay unreserved: a reservation
        # for a *different* holder covering today blocks the requester's checkout in
        # ``assets.services`` and turns the approved request into a dead end.
        # ``_seed_operations`` runs first and publishes those assets; the
        # claim-blocker invariant is the fail-closed net behind this exclusion.
        request_allocated = getattr(self, "_request_allocated_asset_ids", set())

        for slug, _tenant in (getattr(self, "_tenants", None) or {}).items():
            holders = tenant_holders.get(slug, [])
            tenant_assets = assets_by_tenant.get(slug, [])

            # Only reserve laptops/desktops/workstations; avoid servers.
            reservable = [
                a
                for a in tenant_assets
                if a.pk not in request_allocated
                and (
                    a.asset_type is None
                    or (a.asset_type.category is None)
                    or (a.asset_type.category.slug not in ("servers", "network-equipment", "display-monitors"))
                )
            ]
            if not reservable or not holders:
                continue

            # Scale reservation count with headcount; min 3, max ~10.
            n_res = min(max(3, len(holders) // 3), 10)
            # Ensure we don't exhaust reservable assets.
            n_res = min(n_res, len(reservable))

            used_assets = set()

            for _i in range(n_res):
                # Pick an asset not yet reserved in this batch.
                avail = [a for a in reservable if a.pk not in used_assets]
                if not avail:
                    break
                asset = random.choice(avail)
                used_assets.add(asset.pk)

                holder = random.choice(holders)
                eng_user = _pick_actor()
                is_loaner = random.random() < 0.25  # ~25 % are loaners
                purpose = random.choice(LOANER_PURPOSES if is_loaner else REGULAR_PURPOSES)

                # Distribute across three buckets:
                #   PAST (fulfilled) — ~50 %
                #   ACTIVE (now)     — ~30 %
                #   FUTURE (pending) — ~20 %
                bucket_roll = random.random()
                if bucket_roll < 0.50:
                    # PAST: both start and end are in the past
                    end_offset = random.randint(30, 700)
                    dur = random.randint(3, 30)
                    end_date = TODAY - datetime.timedelta(days=end_offset)
                    start_date = end_date - datetime.timedelta(days=dur)
                    created_when = start_date - datetime.timedelta(days=random.randint(3, 21))

                    res = AssetReservation.objects.create(
                        asset=asset,
                        reserved_for=holder,
                        start_date=start_date,
                        end_date=end_date,
                        status=ReservationStatusChoices.PENDING,  # start pending
                        created_by=eng_user,
                        purpose=purpose,
                        notes=f"Closed out: {'returned loaner' if is_loaner else 'reservation fulfilled'}.",
                    )
                    eng.log_create(res, when=created_when, user=eng_user)
                    # Transition: pending → active at start, then fulfilled at end
                    eng.change(res, when=start_date, user=eng_user, status=ReservationStatusChoices.ACTIVE)
                    eng.change(res, when=end_date, user=eng_user, status=ReservationStatusChoices.FULFILLED)

                elif bucket_roll < 0.80:
                    # ACTIVE: started in the past, ends in the future
                    start_offset = random.randint(1, 90)
                    dur = random.randint(7, 60)
                    start_date = TODAY - datetime.timedelta(days=start_offset)
                    end_date = TODAY + datetime.timedelta(days=dur)
                    created_when = start_date - datetime.timedelta(days=random.randint(1, 14))

                    res = AssetReservation.objects.create(
                        asset=asset,
                        reserved_for=holder,
                        start_date=start_date,
                        end_date=end_date,
                        status=ReservationStatusChoices.PENDING,
                        created_by=eng_user,
                        purpose=purpose,
                        notes=f"{'Loaner issued. Monitor it closely.' if is_loaner else 'In progress.'}",
                    )
                    eng.log_create(res, when=created_when, user=eng_user)
                    eng.change(res, when=start_date, user=eng_user, status=ReservationStatusChoices.ACTIVE)

                else:
                    # FUTURE: pending, both dates ahead
                    start_offset = random.randint(5, 90)
                    dur = random.randint(3, 30)
                    start_date = TODAY + datetime.timedelta(days=start_offset)
                    end_date = start_date + datetime.timedelta(days=dur)
                    created_when = TODAY - datetime.timedelta(days=random.randint(1, 10))

                    res = AssetReservation.objects.create(
                        asset=asset,
                        reserved_for=holder,
                        start_date=start_date,
                        end_date=end_date,
                        status=ReservationStatusChoices.PENDING,
                        created_by=eng_user,
                        purpose=purpose,
                        notes=f"Upcoming {'loaner' if is_loaner else 'reservation'}; it has not started yet.",
                    )
                    eng.log_create(res, when=created_when, user=eng_user)

                self._reservations.append(res)

        self.stdout.write(f"  {len(self._reservations)} asset reservations created.")

        # ================================================================
        # 3. DISPOSALS
        # ================================================================
        self.stdout.write("--- Lifecycle: disposals ---")
        self._disposals = []

        # Primary targets: already-retired assets.
        retired = list(getattr(self, "_retired_assets", []))

        # Add ~5 % of non-retired assets to show opportunistic disposal.
        non_retired = [a for a in (getattr(self, "_assets", None) or []) if a not in retired]
        extra_count = max(1, len(non_retired) // 20)
        extra_targets = random.sample(non_retired, k=min(extra_count, len(non_retired)))
        disposal_targets = retired + extra_targets

        DISPOSAL_MIX = [
            (DisposalMethodChoices.RECYCLE, 0.35),
            (DisposalMethodChoices.RESALE, 0.25),
            (DisposalMethodChoices.DESTRUCTION, 0.20),
            (DisposalMethodChoices.DONATION, 0.12),
            (DisposalMethodChoices.OTHER, 0.08),
        ]

        SANIT_MIX = [
            (DataSanitizationMethodChoices.NIST_PURGE, 0.30),
            (DataSanitizationMethodChoices.CRYPTO_ERASE, 0.25),
            (DataSanitizationMethodChoices.NIST_CLEAR, 0.20),
            (DataSanitizationMethodChoices.NIST_DESTROY, 0.10),
            (DataSanitizationMethodChoices.DOD_3PASS, 0.08),
            (DataSanitizationMethodChoices.PHYSICAL_DESTRUCTION, 0.04),
            (DataSanitizationMethodChoices.NONE, 0.03),
        ]

        SANITIZERS = [
            "Blancco GmbH",
            "Iron Mountain DE",
            "Sims Lifecycle Services",
            "IT-Entsorgung München",
            "RecycIT GmbH",
            "Internal IT",
        ]
        RECIPIENTS = [
            "WeRecycle GmbH",
            "Ebay-Auktion",
            "VEBEG GmbH",
            "Caritas IT-Spende",
            "Internal Asset Pool",
            "Iron Mountain",
            "Techsoup DE",
            "",
            "",
            "",  # blanks = sold/destroyed with no named recipient
        ]

        def _weighted_choice(pairs):
            roll = random.random()
            cumul = 0.0
            for val, weight in pairs:
                cumul += weight
                if roll < cumul:
                    return val
            return pairs[-1][0]

        # Guard against duplicate disposals (OneToOne on asset).
        disposed_pks = set()

        for asset in disposal_targets:
            if asset.pk in disposed_pks:
                continue
            # Skip assets that already have a disposal record.
            if (
                hasattr(asset, "disposal")
                and asset.__class__.all_objects.filter(pk=asset.pk).values_list("disposal__pk", flat=True).first()
            ):
                continue
            if _AD.all_objects.filter(asset=asset).exists():
                continue

            disposed_pks.add(asset.pk)

            p_date = (
                self._as_date(asset.purchase_date)
                if asset.purchase_date
                else (TODAY - datetime.timedelta(days=random.randint(400, 730)))
            )
            # Disposal 6 months–2 years after purchase (or within recent 2 yrs).
            min_offset = max(180, (TODAY - p_date).days - 365)
            max_offset = max(min_offset + 30, (TODAY - p_date).days)
            disposal_date = p_date + datetime.timedelta(days=random.randint(min(min_offset, 350), min(max_offset, 730)))
            if disposal_date > TODAY:
                disposal_date = TODAY - datetime.timedelta(days=random.randint(1, 30))

            method = _weighted_choice(DISPOSAL_MIX)
            san_method = _weighted_choice(SANIT_MIX)

            # No sanitization for non-data-bearing types (monitors, etc.)
            cat_slug = asset.asset_type.category.slug if asset.asset_type and asset.asset_type.category else ""
            if cat_slug in ("display-monitors", "network-equipment", "printers-mfps"):
                san_method = DataSanitizationMethodChoices.NONE

            san_cert = ""
            san_by = ""
            if san_method != DataSanitizationMethodChoices.NONE:
                sanitizer = random.choice(SANITIZERS)
                san_by = sanitizer
                san_cert = f"CERT-{disposal_date.year}-{random.randint(10000, 99999)}"

            recipient = random.choice(RECIPIENTS)
            proceeds = None
            if method == DisposalMethodChoices.RESALE:
                # ~10-30 % of purchase cost
                try:
                    cost = float(asset.purchase_cost or 0)
                    proceeds = round(cost * random.uniform(0.05, 0.30), 2)
                except (TypeError, ValueError):
                    proceeds = round(random.uniform(50, 500), 2)

            currency = self._tenant_currency(asset)

            weee = method in (DisposalMethodChoices.RECYCLE,) and random.random() < 0.75

            try:
                # Savepoint: the whole seed runs in one transaction, which a swallowed
                # IntegrityError would otherwise leave unusable.
                with transaction.atomic():
                    disposal = AssetDisposal.objects.create(
                        asset=asset,
                        disposal_method=method,
                        disposal_date=disposal_date,
                        data_sanitization_method=san_method,
                        sanitization_certificate=san_cert,
                        sanitized_by=san_by,
                        recipient=recipient,
                        proceeds=proceeds,
                        currency=currency,
                        weee_compliant=weee,
                        notes=f"Disposal recorded {disposal_date}. Method: {method}.",
                    )
                    eng.log_create(disposal, when=disposal_date, user=_pick_actor())
                self._disposals.append(disposal)
            except (IntegrityError, ValidationError):
                # Gracefully skip if the asset already has a disposal or violates a constraint.
                disposed_pks.discard(asset.pk)
                continue

        self.stdout.write(f"  {len(self._disposals)} asset disposal records created.")
        self.stdout.write(
            self.style.SUCCESS(
                f"  Lifecycle totals: "
                f"{len(self._warranties)} warranties, "
                f"{len(self._reservations)} reservations, "
                f"{len(self._disposals)} disposals."
            )
        )
