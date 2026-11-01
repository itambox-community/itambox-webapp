"""SeedHistoryMixin — drives realistic, back-dated MSP change history.

Replaces the old hand-written ``_seed_changelog`` with engine-powered saves so
every ObjectChange carries a genuine diff produced by the same serializer the
live application uses.

Wire-up (in seed_data.py):
1. Import and add to Command's base classes:
       from core.management.commands._seed.history import SeedHistoryMixin
       class Command(SeedHistoryMixin, BaseCommand): ...
2. In ``_seed_all``, replace ``self._seed_changelog()`` with
       self._engine = ChangeLogEngine(stdout=self.stdout, style=self.style)
       self._simulate_history()
3. Delete (or leave inert) the old ``_seed_changelog`` method.
"""

import datetime
import random

from django.utils import timezone as _tz

from core.management.commands._seed.engine import as_aware_datetime


class SeedHistoryMixin:
    """Mixin for the seed ``Command``.  Requires ``self._engine`` (a
    ``ChangeLogEngine`` instance) to be set before ``_simulate_history`` is
    called.  All other dependencies are the standard seed ``self.*`` registries.
    """

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _rand_date(self, start: datetime.date, end: datetime.date) -> datetime.date:
        """Return a random date in [start, end], clamped so start <= end."""
        if start >= end:
            return start
        delta = (end - start).days
        return start + datetime.timedelta(days=random.randint(0, delta))

    def _sl(self, slug: str):
        """Fetch a StatusLabel by slug from self._status_labels (never raises)."""
        return self._status_labels.get(slug)

    def _pick_actor(self, actors, helpdesk=None):
        pool = helpdesk if (helpdesk and random.random() < 0.35) else actors
        return random.choice(pool)

    def _checkin_for_repair(self, asset, assignment, *, when, user, status):
        """End ``assignment`` and drop the asset into the deployable pool.

        Mirrors ``assets.services.checkin_asset`` (deactivate the assignment, stamp
        the check-in, revert the status) and records both facts through the change
        log, so the seeded history shows the holder really lost the device before
        the repair rather than silently keeping it across a repair episode.

        ``checked_out_at`` is back-dated to the assignment's own purchase-era start
        if it is still stamped "now". Seeded assignments are created in their final
        state, so their default checkout time is the seed run's timestamp; back-dating
        only the check-in would leave checked_in_at *before* checked_out_at, and an
        inverted interval is not a state the product can produce — worse, it makes
        the assignment look like it was open across any window it does not actually
        span. The back-date never predates the purchase, either: a unit received on a
        purchase order carries a future purchase date, and a loan cannot predate it.
        """
        engine = self._engine
        checked_in_at = as_aware_datetime(when)
        updates = {
            "is_active": False,
            "checked_in_at": checked_in_at,
            "checked_in_by_id": getattr(user, "pk", None),
        }
        if assignment.checked_out_at is None or assignment.checked_out_at > checked_in_at:
            # Never later than the check-in, and never before the asset was bought.
            floor = checked_in_at - datetime.timedelta(days=1)
            purchased = asset.purchase_date
            if purchased is not None and purchased <= when:
                floor = max(floor, as_aware_datetime(purchased))
            else:
                # The unit's purchase date is in the future, so it was not on loan
                # before then; fall back to the same default the repair window uses.
                floor = max(floor, as_aware_datetime(min(when, datetime.date.today()) - datetime.timedelta(days=120)))
            updates["checked_out_at"] = floor
            assignment.checked_out_at = floor
        type(assignment)._base_manager.filter(pk=assignment.pk).update(**updates)
        assignment.is_active = False
        assignment.checked_in_at = checked_in_at
        engine.change(
            asset,
            when=when,
            user=user,
            action="checkin",
            status=status,
        )

    def _run_repair_episode(
        self,
        asset,
        *,
        repair_start,
        repair_end,
        actors,
        helpdesk,
        sl_available,
        sl_in_use,
        sl_pending_repair,
        engine,
    ):
        """Walk one asset through a complete, closed repair episode.

        Only repair assets that are actually deployable right now: an asset that is
        still 'in use' must first be checked in, and one that is already in repair has
        nothing to enter. Assigned assets are rolled back to the deployable pool for
        the duration of the episode so the status and the assignment cannot disagree.

        *Every* open person assignment is checked in, not just one of them. A seeded
        asset can carry more than one assignment row — the checkout step appends a new
        active one rather than re-dating the existing row — so closing only the first
        would leave a second holder still holding the unit straight through the repair.
        """
        repairable = asset.status in (sl_available, sl_in_use) and asset.status is not None
        open_assignments = list(asset.assignments.filter(is_active=True, assigned_user__isnull=False))
        # A site or asset-targeted deployment is not closed by this episode, and an
        # active assignment pins the asset to the deployed status, so such a unit
        # never enters the workshop.
        if asset.assignments.filter(is_active=True, assigned_user__isnull=True).exists():
            repairable = False
        # A unit cannot enter the workshop while somebody still holds it, whatever
        # status it currently wears. Gating the check-in on the status being exactly
        # "in use" left a person assignment open straight through the repair whenever
        # the status had already been rolled back to the deployable pool.
        if repairable and open_assignments:
            # Check the device in before the repair starts, exactly as
            # the product's checkin does: the assignment becomes
            # inactive, and the asset reverts to a deployable status.
            for assignment in open_assignments:
                self._checkin_for_repair(
                    asset,
                    assignment,
                    when=repair_start,
                    user=self._pick_actor(actors, helpdesk),
                    status=sl_available,
                )
        if repairable and asset.status != sl_pending_repair:
            engine.change(
                asset,
                when=repair_start,
                user=self._pick_actor(actors, helpdesk),
                action="update",
                status=sl_pending_repair,
            )
        # Returned to the deployable pool after repair. The
        # 'pending-repair' label's type is 'pending', and
        # pending -> deployed is an illegal transition, so the legal
        # path back into service is via 'available' (deployable).
        repaired = False
        if repairable and sl_available and asset.status != sl_available:
            engine.change(
                asset,
                when=repair_end,
                user=self._pick_actor(actors),
                action="update",
                status=sl_available,
            )
            repaired = True
        # The holder gets the device back, so the assignment history
        # and the status history tell the same story again. The unit sits
        # in the deployable pool at this point, so that is the status the
        # assignment records as the one to revert to on a later check-in.
        # With several holders checked in above, the most recent one gets it back.
        if repaired and open_assignments:
            self._recheckout_after_repair(
                asset,
                open_assignments[-1],
                when=repair_end,
                user=self._pick_actor(actors),
                status=sl_in_use,
                pre_checkout_status=sl_available,
            )
        # Publish the window so the maintenance phase can create the
        # paperwork of *this* repair instead of inventing unrelated
        # "repair" records on assets that never left service.
        if repaired:
            self._repair_windows.append(
                {
                    "asset": asset,
                    "start": repair_start,
                    "end": repair_end,
                    "checked_in": bool(open_assignments),
                    "holder_id": open_assignments[-1].assigned_user_id if open_assignments else None,
                }
            )

    def _recheckout_after_repair(self, asset, assignment, *, when, user, status, pre_checkout_status):
        """Issue a fresh active assignment to the same holder after a repair.

        A repair episode ends with the device going back to the person who had it,
        which in the product means a new checkout. Reusing the closed assignment
        row is not an option: ``unique_active_assignment_per_asset`` allows only
        one active row per asset, and the closed one must keep its history.

        ``pre_checkout_status`` is the status the asset carried *before* this
        checkout, exactly as ``checkout_asset`` records it: a later check-in reverts
        the unit to that label. Recording the post-checkout status instead would make
        a normal check-in hand the unit back as "in use" while nobody holds it.
        """
        # inline import: app-registry: assets.AssetAssignment is created inside the seed
        # command, where the model import must not happen at module load.
        from assets.models import AssetAssignment

        engine = self._engine
        engine.change(
            asset,
            when=when,
            user=user,
            action="checkout",
            status=status,
        )
        AssetAssignment.objects.create(
            asset=asset,
            assigned_user_id=assignment.assigned_user_id,
            assigned_location_id=assignment.assigned_location_id,
            pre_checkout_status=pre_checkout_status,
            checked_out_by=user,
            checked_out_at=as_aware_datetime(when),
            notes="Returned to the holder after the repair completed.",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────────────

    def _simulate_history(self):  # noqa: C901  (complex-but-linear, keep together)
        """Generate a ~2-year MSP change history through the real change-log engine.

        The engine drives actual ``snapshot() → mutate → save()`` calls wrapped in
        ``TaskContext``, so every ObjectChange has a genuine prechange/postchange
        diff — no hand-written partial dicts.

        Steps:
        1. Aging pass — back-date *all* assets, licenses and subscriptions to their
           purchase/start dates so the whole dataset looks organically grown.
        2. Per-tenant sample — provisioning, checkout, mid-life edits, repair
           cycles, and audits for up to 20 assets per tenant.
        3. Retired assets — decommission entry for each.
        4. Licenses — create + optional seat-count bump.
        5. Subscriptions — renewal (cost + date update).
        """
        engine = self._engine

        today = datetime.date.today()

        # Repair windows published for the maintenance phase, which must create the
        # paperwork of these very episodes rather than unrelated records.
        self._repair_windows = []

        actors = self._engineer_users or [getattr(self, "_provisioner", None)]
        actors = [a for a in actors if a is not None]
        if not actors:
            actors = list(self._users.values())[:3]

        helpdesk = [u for name, u in self._users.items() if name in ("ravi.anand", "mia.koch")]
        if not helpdesk:
            helpdesk = actors[:2]

        provisioner = getattr(self, "_provisioner", None) or actors[0]

        # ── Status label shortcuts ────────────────────────────────────────────
        sl_available = self._sl("available")
        sl_in_use = self._sl("in-use")
        sl_pending_repair = self._sl("pending-repair")
        sl_retired = self._sl("retired")

        # ── 1. Aging pass: touch created_at/updated_at for all entities ───────
        self.stdout.write("--- Change history (engine-driven) ---")

        for asset in self._assets:
            when = asset.purchase_date or today - datetime.timedelta(days=365)
            engine.touch_created(asset, when)

        for lic in self._licenses:
            when = lic.purchase_date or today - datetime.timedelta(days=365)
            engine.touch_created(lic, when)

        for sub in self._subscriptions:
            when = sub.start_date or today - datetime.timedelta(days=365)
            engine.touch_created(sub, when)

        # ── 2. Per-tenant asset lifecycle ─────────────────────────────────────
        mid_life_notes = [
            "Re-imaged and re-enrolled in MDM.",
            "BIOS/firmware updated to latest vendor release.",
            "RAM upgraded to 32 GB for performance.",
            "Relocated during office move to new floor.",
            "Warranty extended by 12 months via support contract.",
            "Enrolled in new endpoint protection policy.",
            "SSD replaced under warranty; data migration complete.",
            "Asset audited and label reprinted (old label damaged).",
            "Intune profile reapplied after OS reinstall.",
            "Network adapter replaced following intermittent faults.",
            "Bitlocker key rotated per security policy.",
            "Assigned to replacement pool after user departure.",
            "Bluetooth/NFC disabled per hardening policy.",
        ]

        for slug in self._tenants:
            assets = self._assets_by_tenant.get(slug, [])
            if not assets:
                continue

            sample = random.sample(assets, k=min(20, len(assets)))

            for asset in sample:
                p_date = asset.purchase_date or today - datetime.timedelta(days=400)

                # ── a) provisioning create entry ──────────────────────────────
                # Assets are seeded in their final 'in-use' state, so a naive
                # available->in-use check would never fire. To produce genuine
                # 'checkout' history, roll a subset of actively-assigned assets
                # back to the deployable pool *before* logging the create (no log —
                # so the create entry records them as 'available'); they are then
                # checked out below via a real available(deployable)->in-use(deployed)
                # transition. The rest keep their seeded state and just get a create.
                active = asset.assignments.filter(is_active=True, assigned_user__isnull=False).first()
                checks_out = (
                    active is not None
                    and sl_available is not None
                    and sl_in_use is not None
                    and asset.status == sl_in_use
                    and random.random() < 0.6
                )
                if checks_out:
                    type(asset)._base_manager.filter(pk=asset.pk).update(status=sl_available)
                    asset.status = sl_available

                actor = self._pick_actor(actors)
                engine.log_create(asset, when=p_date, user=actor)

                # ── b) checkout (available -> in-use) ~3-14 days after purchase ─
                if checks_out:
                    checkout_date = p_date + datetime.timedelta(days=random.randint(3, 14))
                    if checkout_date > today:
                        checkout_date = today - datetime.timedelta(days=1)
                    engine.change(
                        asset,
                        when=checkout_date,
                        user=self._pick_actor(actors),
                        action="checkout",
                        status=sl_in_use,
                    )

                # ── c) ~30 % mid-life edit ────────────────────────────────────
                if random.random() < 0.30:
                    window_start = p_date + datetime.timedelta(days=30)
                    window_end = today - datetime.timedelta(days=15)
                    edit_date = self._rand_date(window_start, window_end)
                    new_note = random.choice(mid_life_notes)
                    # Only update if the note differs (engine skips no-ops)
                    if asset.notes != new_note:
                        engine.change(
                            asset,
                            when=edit_date,
                            user=self._pick_actor(actors, helpdesk),
                            action="update",
                            notes=new_note,
                        )

                # ── d) ~20 % repair cycle ─────────────────────────────────────
                # A repair episode is only coherent if the asset's assignment
                # story agrees with its status story. The product cannot move an
                # asset that is actively assigned to a person into repair without
                # checking it in first (checkin_asset reverts status to the
                # deployable pool), so a seeded repair on an assigned asset must
                # log that checkin and, once the repair completes, re-check the
                # asset back out to the same holder. Leaving the assignment
                # untouched produced a contradiction: history said
                # "in repair" / "available" while the same person held the device
                # the whole time.
                if random.random() < 0.20 and sl_pending_repair:
                    # Anchor the window on when the unit actually entered service, not
                    # on its purchase date: a unit received on a purchase order carries
                    # a *future* purchase date, and one bought last month has no room
                    # to host a repair. _rand_date clamps an inverted range, which
                    # would otherwise hand us repair_start *after* repair_end: the unit
                    # walks into repair and the episode never closes, leaving it stuck
                    # in the pending-repair label.
                    in_service = min(p_date, today - datetime.timedelta(days=120))
                    window_start = in_service + datetime.timedelta(days=60)
                    window_end = today - datetime.timedelta(days=60)
                    repair_start = None
                    repair_end = None
                    if window_start < window_end:
                        repair_start = self._rand_date(window_start, window_end)
                        repair_end = repair_start + datetime.timedelta(days=random.randint(7, 30))
                        if repair_end > today:
                            repair_end = today - datetime.timedelta(days=1)
                    if repair_start is not None and repair_start < repair_end:
                        self._run_repair_episode(
                            asset,
                            repair_start=repair_start,
                            repair_end=repair_end,
                            actors=actors,
                            helpdesk=helpdesk,
                            sl_available=sl_available,
                            sl_in_use=sl_in_use,
                            sl_pending_repair=sl_pending_repair,
                            engine=engine,
                        )

                # ── e) ~25 % physical audit ───────────────────────────────────
                has_last_audited = any(f.name == "last_audited" for f in asset._meta.fields)
                if has_last_audited and random.random() < 0.25:
                    audit_date = self._rand_date(
                        today - datetime.timedelta(days=90),
                        today - datetime.timedelta(days=1),
                    )

                    audit_dt = _tz.make_aware(
                        datetime.datetime(
                            audit_date.year,
                            audit_date.month,
                            audit_date.day,
                            random.randint(8, 17),
                            random.randint(0, 59),
                        ),
                        _tz.get_current_timezone(),
                    )
                    if asset.last_audited != audit_dt:
                        engine.change(
                            asset,
                            when=audit_date,
                            user=self._pick_actor(actors, helpdesk),
                            action="audit",
                            last_audited=audit_dt,
                        )

        # ── 3. Retired assets: decommission entry ─────────────────────────────
        for asset in self._retired_assets:
            if sl_retired and asset.status != sl_retired:
                window_start = (asset.purchase_date or today - datetime.timedelta(days=500)) + datetime.timedelta(
                    days=180
                )
                window_end = today - datetime.timedelta(days=10)
                decom_date = self._rand_date(window_start, window_end)
                engine.change(
                    asset,
                    when=decom_date,
                    user=self._pick_actor(actors),
                    action="update",
                    status=sl_retired,
                    notes="Decommissioned at the end of its useful life. Disposed of by a certified e-waste vendor.",
                )

        # ── 4. Licenses: create log + optional seat bump ──────────────────────
        for lic in random.sample(self._licenses, k=min(30, len(self._licenses))):
            p_date = lic.purchase_date or today - datetime.timedelta(days=365)
            engine.log_create(lic, when=p_date, user=provisioner)

            if random.random() < 0.40:
                bump_date = self._rand_date(
                    p_date + datetime.timedelta(days=60),
                    today - datetime.timedelta(days=14),
                )
                bump = random.randint(5, 25)
                new_seats = lic.seats + bump
                engine.change(
                    lic,
                    when=bump_date,
                    user=self._pick_actor(actors),
                    action="update",
                    seats=new_seats,
                )

        # ── 5. Subscriptions: renewal (cost + date bump) ──────────────────────
        for sub in random.sample(self._subscriptions, k=min(15, len(self._subscriptions))):
            if sub.renewal_cost is None or sub.renewal_date is None:
                continue

            renewal_log_date = self._rand_date(
                today - datetime.timedelta(days=60),
                today - datetime.timedelta(days=3),
            )
            # Bump cost by a realistic 3–12 % price increase
            new_cost = round(float(sub.renewal_cost) * random.uniform(1.03, 1.12), 2)
            # Advance the renewal date by one year
            try:
                new_renewal_date = sub.renewal_date.replace(year=sub.renewal_date.year + 1)
            except ValueError:
                # Feb 29 edge case
                new_renewal_date = sub.renewal_date + datetime.timedelta(days=365)

            # Only apply if both values actually change (engine skips no-ops)
            if new_cost != float(sub.renewal_cost):
                engine.change(
                    sub,
                    when=renewal_log_date,
                    user=self._pick_actor(actors, helpdesk),
                    action="update",
                    renewal_cost=new_cost,
                    renewal_date=new_renewal_date,
                )

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write(
            f"  {engine.count} authentic change-history entries across assets, licenses and subscriptions."
        )
