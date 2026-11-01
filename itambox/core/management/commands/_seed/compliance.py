"""Compliance seed mixin — audit sessions + custody receipts.

Produces ~2 years of realistic compliance history across all managed tenants:
  - 2–4 AuditSession records per tenant (quarterly / annual cadence) with
    AssetAudit rows for a sample of that tenant's assets.
  - CustodyReceipt records for a sample of assigned assets, using the custody
    templates that _seed_assets already built and stored on self._custody_templates /
    self._gxp_custody_template.

Registered on ``self`` when done:
  self._audit_sessions   list[AuditSession]
  self._custody_receipts list[CustodyReceipt]  (adds to any already created in
                                                 _seed_assets; does not replace them)
"""

import datetime
import hashlib
import random

from django.utils import timezone

from compliance.choices import AuditSessionStatusChoices, AuditVerificationMethodChoices
from compliance.models import AssetAudit, AuditSession, CustodyReceipt

# ── helpers ───────────────────────────────────────────────────────────────────

TODAY = datetime.date.today()


def _days_ago(n):
    return TODAY - datetime.timedelta(days=n)


def _as_dt(d):
    """Coerce a date to a noon-UTC-ish aware datetime."""
    if isinstance(d, datetime.datetime):
        return d
    return timezone.make_aware(
        datetime.datetime(d.year, d.month, d.day, 10, 0),
        timezone.get_current_timezone(),
    )


# Approximate quarterly anchors over the last two years (oldest first).
def _audit_schedule(n_sessions):
    """Return n_sessions dates spread across the last ~2 years."""
    total_days = 730
    if n_sessions <= 1:
        return [_days_ago(total_days // 2)]
    step = total_days // (n_sessions - 1)
    return [_days_ago(total_days - i * step) for i in range(n_sessions)]


class SeedComplianceMixin:
    """Mixin for Command — provides _seed_compliance()."""

    def _seed_compliance(self):

        self.stdout.write("--- Compliance: audit sessions & custody receipts ---")

        actors = self._engineer_users or [self._provisioner]

        # ── 1. Audit sessions ────────────────────────────────────────────────
        self._audit_sessions = []
        total_asset_audits = 0

        for slug, tenant in self._tenants.items():
            tenant_assets = self._assets_by_tenant.get(slug, [])
            if not tenant_assets:
                continue

            locs = self._tenant_locations.get(slug, [])
            fallback_loc = locs[0] if locs else None

            n_sessions = random.randint(2, 4)
            session_dates = _audit_schedule(n_sessions)

            for idx, audit_date in enumerate(session_dates):
                is_last = idx == len(session_dates) - 1
                # The most-recent session may still be active; older ones are completed.
                if is_last and random.random() < 0.35:
                    status = AuditSessionStatusChoices.ACTIVE
                    completed_at = None
                else:
                    status = AuditSessionStatusChoices.COMPLETED
                    close_date = audit_date + datetime.timedelta(days=random.randint(1, 5))
                    completed_at = _as_dt(close_date)

                quarter_label = f"Q{((audit_date.month - 1) // 3) + 1} {audit_date.year}"
                engineer = random.choice(actors)

                # ~40% of sessions are scoped to a single location (a site / stockroom
                # walk-through); the rest are tenant-wide. Prefer a location that has
                # assets physically sitting there so the session isn't empty.
                session_location = None
                if locs and random.random() < 0.4:
                    loc_candidates = [
                        location for location in locs if any(a.location_id == location.pk for a in tenant_assets)
                    ]
                    session_location = random.choice(loc_candidates or locs)

                if session_location:
                    session_name = f"{quarter_label}: {session_location.name} Audit"
                else:
                    session_name = f"{quarter_label} Asset Audit"

                session = AuditSession.objects.create(
                    name=session_name,
                    tenant=tenant,
                    location=session_location,
                    status=AuditSessionStatusChoices.PLANNED,
                    created_by=engineer,
                    completed_at=completed_at if status == AuditSessionStatusChoices.COMPLETED else None,
                )
                self._engine.log_create(session, when=audit_date, user=engineer)

                # Narrate planned → active → completed lifecycle via change log.
                activate_date = audit_date + datetime.timedelta(days=1)
                self._engine.change(
                    session,
                    when=activate_date,
                    user=engineer,
                    status=AuditSessionStatusChoices.ACTIVE,
                )
                if status == AuditSessionStatusChoices.COMPLETED:
                    self._engine.change(
                        session,
                        when=completed_at,
                        user=engineer,
                        status=AuditSessionStatusChoices.COMPLETED,
                    )

                # A location-scoped session audits the assets physically at that location;
                # a tenant-wide session samples ~60 % of the tenant's assets (min 3, max 40).
                if session_location:
                    pool = [a for a in tenant_assets if a.location_id == session_location.pk]
                    audited_assets = pool or random.sample(tenant_assets, k=min(3, len(tenant_assets)))
                else:
                    sample_size = max(3, min(40, int(len(tenant_assets) * 0.6)))
                    audited_assets = random.sample(tenant_assets, k=min(sample_size, len(tenant_assets)))

                for asset in audited_assets:
                    # Determine observed location — asset's own location or fallback.
                    obs_loc = session_location or asset.location or fallback_loc
                    if obs_loc is None:
                        continue  # AssetAudit.location is required — skip if we have none

                    # 85 % verified in expected status; 15 % observed as 'in-use' (mismatch proxy)
                    obs_status = asset.status
                    if random.random() < 0.15:
                        # Pick a different status from the registry if available
                        other_statuses = [s for k, s in self._status_labels.items() if s != asset.status]
                        if other_statuses:
                            obs_status = random.choice(other_statuses)

                    v_method = random.choice(
                        [
                            AuditVerificationMethodChoices.MANUAL,
                            AuditVerificationMethodChoices.MANUAL,
                            AuditVerificationMethodChoices.BARCODE,
                            AuditVerificationMethodChoices.AUTO,
                        ]
                    )
                    notes = ""
                    if obs_status != asset.status:
                        notes = "Status mismatch observed during physical audit."

                    AssetAudit.objects.get_or_create(
                        session=session,
                        asset=asset,
                        defaults={
                            "auditor": engineer,
                            "location": obs_loc,
                            "status": obs_status,
                            "verification_method": v_method,
                            "notes": notes,
                        },
                    )
                    total_asset_audits += 1

                self._audit_sessions.append(session)

        # ── 2. Custody receipts (supplement _seed_assets receipts) ──────────
        # _seed_assets already created receipts for regulated industries. Here we
        # add receipts for tenants that were skipped there — and backfill a small
        # number of *pending* (unsigned) receipts to show an incomplete workflow.

        self._custody_receipts = []
        new_receipts = 0

        for slug, _tenant in self._tenants.items():
            tenant_assets = self._assets_by_tenant.get(slug, [])
            if not tenant_assets:
                continue

            is_helix = self._tenant_meta[slug].get("group_slug") == "helix-biopharma"

            for asset in tenant_assets:
                # Only assets with an active user assignment carry a custody receipt.
                active_assign = asset.assignments.filter(is_active=True, assigned_user__isnull=False).first()
                if not active_assign:
                    continue

                holder = active_assign.assigned_user

                # Skip if a receipt already exists for this asset + holder.
                if CustodyReceipt.objects.filter(asset=asset, holder=holder).exists():
                    continue

                # Determine the best matching custody template.
                cat = asset.asset_type.category.slug if asset.asset_type and asset.asset_type.category else None
                if f"{slug}:{cat}" in self._custody_templates:
                    tmpl = self._custody_templates[f"{slug}:{cat}"]
                elif is_helix and cat == "laptops" and self._gxp_custody_template:
                    tmpl = self._gxp_custody_template
                else:
                    tmpl = self._custody_templates.get(cat)

                if tmpl is None:
                    continue  # no template for this category — skip

                # ~55 % chance of generating a receipt to keep volumes sane.
                if random.random() > 0.55:
                    continue

                # Date the receipt near the assignment (within 1–7 days after).
                assign_date = (
                    active_assign.created_at.date()
                    if hasattr(active_assign, "created_at") and active_assign.created_at
                    else _days_ago(random.randint(30, 600))
                )
                receipt_date = assign_date + datetime.timedelta(days=random.randint(0, 7))
                receipt_dt = _as_dt(receipt_date)

                # 75 % signed/accepted; 25 % still pending. Accepted demo
                # receipts carry no signature image (no placeholder payload).
                accepted = random.random() < 0.75
                if accepted:
                    h = hashlib.sha256(f"{asset.asset_tag}-{holder.pk}-{receipt_date}".encode()).hexdigest()[:64]
                    receipt = CustodyReceipt.objects.create(
                        asset=asset,
                        holder=holder,
                        custody_template=tmpl,
                        eula_text=tmpl.eula_text,
                        disclaimer=tmpl.disclaimer,
                        qms_reference=tmpl.qms_reference,
                        eula_version="1.0",
                        acceptance_status=CustodyReceipt.STATUS_ACCEPTED,
                        accepted_date=receipt_dt,
                        signed_at=receipt_dt,
                        verification_hash=h,
                        acceptance_method="link",
                    )
                else:
                    receipt = CustodyReceipt.objects.create(
                        asset=asset,
                        holder=holder,
                        custody_template=tmpl,
                        eula_text=tmpl.eula_text,
                        disclaimer=tmpl.disclaimer,
                        qms_reference=tmpl.qms_reference,
                        eula_version="1.0",
                        acceptance_status=CustodyReceipt.STATUS_PENDING,
                        acceptance_method="link",
                    )

                actor = random.choice(actors)
                self._engine.log_create(receipt, when=receipt_date, user=actor)
                self._custody_receipts.append(receipt)
                new_receipts += 1

        self.stdout.write(
            f"  {len(self._audit_sessions)} audit sessions, "
            f"{total_asset_audits} asset audit rows, "
            f"{new_receipts} new custody receipts."
        )
