"""Operations seed mixin: alerts, reports, event rules, config, dashboards, audit.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.operations import SeedOperationsMixin

    class Command(SeedOperationsMixin, BaseCommand):
        ...

``_seed_operations`` runs last of the original phases (it reads
``self._licenses`` / ``self._assets`` / ``self._tenants`` / ``self._tenant_meta``
/ ``self._tenant_locations`` / ``self._asset_types`` / ``self._users`` /
``self._provisioner``).
"""

import datetime
import random

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from assets.choices import RequestStatusChoices
from core.tasks.context import TaskContext

User = get_user_model()

TODAY = datetime.date.today()


def days_ahead(n):
    return TODAY + datetime.timedelta(days=n)


class SeedOperationsMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    def _tenant_of_user(self, user):
        """Return the tenant a seeded user belongs to, or ``None`` when it has none.

        ``AssetRequest.save`` falls back to the ambient tenant context, which the
        seed does not set, so the request's tenant has to be resolved from the
        user's active membership — the same single-membership invariant the access
        self-check enforces for every seeded person.
        """
        # inline import: app-registry: organization.Membership is read from the DB in a
        # management command; loading it at module top would import the app's models
        # before the app registry is populated.
        from organization.models import Membership

        membership = (
            Membership._base_manager.filter(user_id=user.pk, is_active=True)
            .select_related("tenant")
            .order_by("pk")
            .first()
        )
        return membership.tenant if membership else None

    def _claimable_asset_for(self, request_instance):
        """Return a free, claimable asset of the requested type in the request's tenant.

        Applies the shared issuance rule and the request-specific constraints:
        tenant, asset type, and requestability. ``is_requestable`` is a property
        (per-asset override, else the asset type's flag), so the query filters the
        two underlying columns. Returns ``None`` when the tenant has no spare
        unit, which the caller treats as "leave the request pending".
        """
        # inline import: app-registry: assets.Asset is queried inside the seed command,
        # where the model import must not happen at module load.
        from assets.models import Asset

        if not request_instance.asset_type_id or not request_instance.tenant_id:
            return None
        with TaskContext(tenant_id=request_instance.tenant_id, user_id=self._provisioner.pk):
            candidates = (
                Asset.issuable()
                .filter(
                    tenant_id=request_instance.tenant_id,
                    asset_type=request_instance.asset_type,
                )
                .filter(Q(requestable=True) | Q(requestable__isnull=True, asset_type__requestable=True))
            )
            return candidates.order_by("-in_service_date", "pk").first()

    def _request_target_holder(self, tenant):
        """The person a seeded request is for: a tenant holder without a device yet.

        The request must name a delegated target. ``RequestClaimView._checkout_unit``
        checks the unit out to ``assigned_user``, and the customer-admin logins this
        seed creates are technical accounts that deliberately carry no holder profile,
        so a targetless request raised by one of them can never be checked out to
        anybody (#506).
        """
        holders = (getattr(self, "_tenant_holders", None) or {}).get(tenant.slug, []) if tenant else []
        primary = getattr(self, "_primary_laptop_by_holder", None) or {}
        without_device = [holder for holder in holders if holder.pk not in primary]
        return next(iter(without_device or holders), None)

    def _seed_operations(self):
        # inline imports: app-registry: Resolve seed models only after Django has populated the app registry.
        from assets.models import Asset, AssetRequest
        from compliance.models import AssetAudit, AuditSession
        from extras.models import (
            AlertLog,
            AlertRule,
            Dashboard,
            EventRule,
            JournalEntry,
            LabelTemplate,
            NotificationChannel,
            ReportTemplate,
            ScheduledReport,
            WebhookEndpoint,
        )
        from inventory.models import Accessory, Component, Consumable
        from licenses.models import License
        from organization.models import Location

        self.stdout.write("--- Operations: alerts, reports, automation ---")

        # Assets handed to an open request. Published so the later reservation phase
        # leaves them alone: a reservation for a *different* holder covering today
        # blocks the requester's checkout in ``assets.services`` and would dead-end
        # the approved request.
        self._request_allocated_asset_ids = set()

        # Notification channels
        email_ch = NotificationChannel.objects.create(
            name="Northwind Service Desk",
            channel_type="email",
            enabled=True,
            config={"recipients": "servicedesk@northwind-it.com"},
        )
        slack_ch = NotificationChannel.objects.create(
            name="Northwind Slack #alerts",
            channel_type="slack",
            enabled=True,
            config={"webhook_url": "https://hooks.slack.com/services/T000/B000/XXXX"},
        )

        # Alert rules (system-wide)
        rules = {}
        for name, atype, thr, sev in [
            ("Low Inventory Stock", "low_stock", 5, "warning"),
            ("License Expiring Soon", "license_expiry", 30, "warning"),
            ("Subscription Renewal Due", "renewal_due", 45, "info"),
            ("Hardware Warranty Expiring", "warranty_expiry", 60, "warning"),
            ("Asset End-of-Life Planning", "upcoming_eol", 90, "info"),
            ("Audit Overdue", "audit_overdue", 365, "critical"),
        ]:
            r = AlertRule.objects.create(
                name=name,
                alert_type=atype,
                threshold_value=thr,
                severity=sev,
                is_active=True,
                description=f"{name} monitoring across managed tenants.",
            )
            r.channels.add(email_ch, slack_ch)
            rules[atype] = r

        # Alert logs referencing real objects (active / acknowledged)
        ct_asset = ContentType.objects.get_for_model(Asset)
        ct_lic = ContentType.objects.get_for_model(License)
        log_count = 0
        expiring_licenses = [
            lic for lic in self._licenses if lic.expiration_date and lic.expiration_date <= days_ahead(30)
        ]
        for lic in expiring_licenses[:12]:
            AlertLog.objects.create(
                rule=rules["license_expiry"],
                content_type=ct_lic,
                object_id=lic.pk,
                subject=f"License '{lic.name}' expires {lic.expiration_date:%Y-%m-%d}",
                message=f"{lic.name} ({lic.seats} seats) is due to expire on {lic.expiration_date:%Y-%m-%d}.",
                severity="warning",
                status=random.choice(["active", "active", "acknowledged"]),
            )
            log_count += 1
        warranty_assets = [
            a for a in self._assets if a.current_warranty_end and a.current_warranty_end <= days_ahead(60)
        ]
        for a in random.sample(warranty_assets, k=min(15, len(warranty_assets))):
            AlertLog.objects.create(
                rule=rules["warranty_expiry"],
                content_type=ct_asset,
                object_id=a.pk,
                subject=f"Warranty for {a.asset_tag} expires {a.current_warranty_end:%Y-%m-%d}",
                message=f"{a.name} ({a.asset_tag}) warranty ends {a.current_warranty_end:%Y-%m-%d}.",
                severity="warning",
                status=random.choice(["active", "acknowledged", "resolved"]),
            )
            log_count += 1

        # Report templates + schedules
        rt_summary = ReportTemplate.objects.create(
            name="Fleet Inventory Summary",
            report_type="asset_summary",
            description="All managed assets by status, role and tenant.",
            include_summary_cards=True,
            include_distribution_chart=True,
            group_by_field="status",
        )
        rt_lic = ReportTemplate.objects.create(
            name="License Utilization",
            report_type="license_utilization",
            description="Seat utilization and renewal exposure across customers.",
            include_summary_cards=True,
        )
        ReportTemplate.objects.create(
            name="Upcoming Subscription Renewals",
            report_type="subscription_renewals",
            description="Cloud and SaaS renewals due in the next quarter.",
            include_summary_cards=True,
        )
        ReportTemplate.objects.create(
            name="Asset Depreciation Summary",
            report_type="asset_depreciation",
            description="Written-down value of the managed fleet.",
            include_summary_cards=True,
            style_preset="financial",
        )

        sr1 = ScheduledReport.objects.create(
            name="Weekly Fleet Summary",
            report=rt_summary,
            frequency="weekly",
            format="html",
            recipients="ops@northwind-it.com",
            is_active=True,
            start_time=datetime.time(7, 0),
        )
        sr1.channels.add(email_ch)
        sr2 = ScheduledReport.objects.create(
            name="Monthly License Review",
            report=rt_lic,
            frequency="monthly",
            format="csv",
            recipients="licensing@northwind-it.com",
            is_active=True,
        )
        sr2.channels.add(email_ch)

        # Event rules + webhook
        webhook = WebhookEndpoint.objects.get_or_create(
            name="Northwind Slack Hardware Events",
            defaults={
                "url": "https://hooks.slack.com/services/T000/B001/HARDWARE",
                "secret": "demo_shared_secret",
                "enabled": True,
            },
        )[0]
        EventRule.objects.create(
            name="Notify on new asset",
            model=ct_asset,
            events=["create"],
            action_type="notification",
            action_config={"message": "A new asset was added to the managed fleet."},
            enabled=True,
        )
        EventRule.objects.create(
            name="Push asset status changes to Slack",
            model=ct_asset,
            events=["update"],
            action_type="webhook",
            action_config={"endpoint": webhook.name},
            enabled=True,
        )

        # Dashboards for the MSP operators
        for user in [self._provisioner] + list(User.objects.filter(is_superuser=True)[:1]):
            Dashboard.objects.get_or_create(
                user=user, name="Operations Overview", defaults={"is_default": True, "layout": []}
            )

        # Quarterly audit
        audit = AuditSession.objects.create(
            name="Q2 Managed Fleet Audit", status="in_progress", created_by=self._provisioner
        )
        audit_assets = random.sample(self._assets, k=min(25, len(self._assets)))
        audited = 0
        for a in audit_assets:
            loc = a.location or (
                self._tenant_locations.get(a.tenant.slug)[0]
                if a.tenant and self._tenant_locations.get(a.tenant.slug)
                else None
            )
            AssetAudit.objects.get_or_create(
                session=audit, asset=a, defaults={"status": a.status, "auditor": self._provisioner, "location": loc}
            )
            audited += 1

        # Asset requests from customer admins
        req_type = self._asset_types["dell-latitude-5550"]
        req_type.requestable = True
        req_type.save(update_fields=["requestable"])
        self._asset_types["iphone-15-pro"].requestable = True
        self._asset_types["iphone-15-pro"].save(update_fields=["requestable"])
        customer_admin_users = [u for name, u in self._users.items() if name.startswith("admin@")]
        req_count = 0
        for user in customer_admin_users[:5]:
            # An approved request is only claimable once a unit is allocated to it
            # (RequestClaimView refuses a claim without ``request.asset``) *and* once
            # the claim view can resolve an assignee for it — the customer-admin logins
            # this seed creates are technical accounts that deliberately carry no
            # holder profile. Seeding "approved" without either produced demo stories
            # that dead-end on a validation error the prospect cannot clear. So
            # both the allocation and the delegated target are decided *before* the row
            # exists: an approved request always carries them, and a tenant without a
            # spare unit is seeded as pending — which is the state that still has a
            # working approve path in the UI. Demoting an existing approved row to
            # pending is not an option: the product's state machine refuses
            # approved -> pending.
            #
            # The tenant is resolved from the user's membership and written
            # explicitly: AssetRequest.save() only falls back to the ambient tenant
            # context, which the seed does not set, so without this the request would
            # persist with tenant=NULL while its allocation came from one tenant.
            tenant = self._tenant_of_user(user)
            probe = AssetRequest(requester=user, tenant=tenant, asset_type=req_type)
            allocated = self._claimable_asset_for(probe)
            AssetRequest.objects.create(
                tenant=tenant,
                requester=user,
                asset_type=req_type,
                notes="A new employee joins next month and needs a standard laptop.",
                status=RequestStatusChoices.APPROVED if allocated else RequestStatusChoices.PENDING,
                asset=allocated,
                assigned_user=self._request_target_holder(tenant),
            )
            req_count += 1

        # Assets already handed to an open request must stay unreserved in the later
        # reservation phase: a reservation for a *different* holder covering
        # today blocks the claim's checkout and dead-ends the approved request. Read
        # back from the persisted rows so the set matches what the invariant later
        # verifies.
        self._request_allocated_asset_ids = set(
            AssetRequest._base_manager.filter(asset__isnull=False, deleted_at__isnull=True).values_list(
                "asset_id", flat=True
            )
        )

        # Journal entries on a few assets
        if self._assets:
            for a in random.sample(self._assets, k=min(6, len(self._assets))):
                JournalEntry.objects.create(
                    content_object=a,
                    user=self._provisioner,
                    comment=random.choice(
                        [
                            "Device inspected during a site visit. Minor cosmetic wear was found.",
                            "User reported fan noise under load; monitoring.",
                            "Re-imaged and re-enrolled in MDM after role change.",
                            "Confirmed asset present and tagged during audit.",
                        ]
                    ),
                )

        # Label template
        qr_cell = (
            '<table class="label-card-table"><tr>'
            '<td class="label-card-metadata"><div class="label-card-title">{{ asset.name }}</div>'
            '<div class="label-card-tag">{{ asset.asset_tag }}</div></td>'
            '<td class="label-card-barcode-cell">{{ barcode_img }}</td></tr></table>'
        )
        label_templates = [
            ("Standard QR Asset Label", "2.0 x 1.0 inch QR label for laptops & desktops", "qr", 2.0, 1.0, qr_cell),
            (
                "Compact QR Asset Tag",
                "1.5 x 0.5 inch QR tag for accessories & small items",
                "qr",
                1.5,
                0.5,
                '<div class="label-seed-centered">{{ barcode_img }}'
                '<div class="label-card-tag label-seed-small">{{ asset.asset_tag }}</div></div>',
            ),
            (
                "Datacenter Rack Label (Code 128)",
                "4.0 x 1.0 inch barcode label for rack/server gear",
                "code128",
                4.0,
                1.0,
                '<table class="label-card-table"><tr><td class="label-card-metadata">'
                '<div class="label-seed-rack-title">{{ asset.name }}</div>'
                "<div>{{ asset.asset_tag }} · {{ asset.serial_number }}</div></td>"
                '<td class="label-card-barcode-cell">{{ barcode_img }}</td></tr></table>',
            ),
            (
                "Shipping / Transfer Label (Code 39)",
                "4.0 x 2.0 inch label for in-transit assets",
                "code39",
                4.0,
                2.0,
                '<div><div class="label-card-title">{{ asset.name }}</div>'
                "<div>From: {{ asset.location }}</div><div>{{ asset.asset_tag }}</div>{{ barcode_img }}</div>",
            ),
        ]
        asset_type = ContentType.objects.get_for_model(Asset)
        for name, desc, fmt, w, h, code in label_templates:
            template, _created = LabelTemplate.objects.get_or_create(
                name=name,
                defaults={
                    "description": desc,
                    "barcode_format": fmt,
                    "page_width": w,
                    "page_height": h,
                    "template_code": code,
                },
            )
            template.content_types.add(asset_type)
        # QR-only layouts for stock bins and storage locations: the code carries
        # the object's own reference, so a scan opens exactly that bin or room.
        bin_template, _created = LabelTemplate.objects.get_or_create(
            name="Stock Bin Label",
            defaults={
                "description": "2.0 x 1.0 inch QR label for accessory, component and consumable bins",
                "barcode_format": "qr",
                "page_width": 2.0,
                "page_height": 1.0,
                "template_code": "",
            },
        )
        bin_template.content_types.add(
            *(ContentType.objects.get_for_model(model) for model in (Accessory, Component, Consumable))
        )
        location_template, _created = LabelTemplate.objects.get_or_create(
            name="Location Label",
            defaults={
                "description": "2.25 x 1.25 inch QR label for rooms, racks and shelves",
                "barcode_format": "qr",
                "page_width": 2.25,
                "page_height": 1.25,
                "template_code": "",
            },
        )
        location_template.content_types.add(ContentType.objects.get_for_model(Location))

        self.stdout.write(
            f"  {len(rules)} alert rules, {log_count} alert logs, 4 report templates, "
            f"2 schedules, 2 event rules, config contexts, {audited} audited assets, "
            f"{req_count} asset requests."
        )
