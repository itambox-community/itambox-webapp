"""Assets seed mixin: per-tenant devices, assignments, custody, installs.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.assets import SeedAssetsMixin

    class Command(SeedAssetsMixin, BaseCommand):
        ...

``_seed_assets`` must run after ``_seed_organizations`` / ``_seed_access`` (it
reads ``self._tenants`` / ``self._tenant_meta`` / ``self._tenant_holders`` /
``self._tenant_locations`` / ``self._asset_types`` / ``self._status_labels`` /
``self._tags`` / ``self._categories`` / ``self._suppliers`` / ``self._software``
/ ``self._components`` / ``self._provisioner`` / ``self._tgroups``). It populates
``self._custody_templates``, ``self._gxp_custody_template``, ``self._assets``,
``self._assets_by_tenant``, ``self._laptops_by_tenant``,
``self._primary_laptop_by_holder``, ``self._retired_assets`` and ``self._servers``.

The ``_os_family_for`` helper
live here and are also read by later phases (maintenance, procurement) via the
shared Command instance.
"""

import datetime
import hashlib
import random

from django.utils import timezone

from assets.models import Asset, AssetAssignment, AssetTagSequence, Warranty, WarrantyTypeChoices
from assets.services import checkout_asset
from assets.services.specification_writers import apply_asset_specification_patch
from compliance.models import CustodyReceipt, CustodyTemplate
from core.management.commands._seed.engine import as_aware_datetime
from core.tasks.context import TaskContext
from inventory.services import COMPONENT_ALLOCATION_OPERATION, create_component_allocation
from software.models import InstalledSoftware

TODAY = datetime.date.today()


def days_ago(n):
    return TODAY - datetime.timedelta(days=n)


class SeedAssetsMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    PRICES = {
        "dell-latitude-5550": 1899,
        "hp-elitebook-860-g11": 2099,
        "thinkpad-x1-carbon-g12": 2199,
        "macbook-pro-16-2024": 3599,
        "macbook-air-15-2024": 1499,
        "dell-precision-5680": 4299,
        "dell-optiplex-7010": 1299,
        "mac-studio-2024": 6999,
        "dell-precision-7960-tower": 7499,
        "dell-poweredge-r760": 18500,
        "hpe-proliant-dl380-g11": 12500,
        "synology-ds1823xs": 4200,
        "iphone-15-pro": 1299,
        "galaxy-s24-ultra": 1249,
        "ipad-pro-129-2024": 1099,
        "surface-pro-10": 1799,
        "cisco-catalyst-9300": 8500,
        "unifi-switch-pro-48": 1099,
        "meraki-mr46": 1200,
        "unifi-dream-machine-pro": 379,
        "dell-p2723de-monitor": 499,
        "dell-p2422he-monitor": 379,
        "logitech-rally-bar": 2799,
    }
    HW_SUPPLIERS = ["dell-direct", "apple-business", "cdw-deutschland", "bechtle-ag", "insight-enterprises"]

    def _os_family_for(self, atype_slug):
        if "macbook" in atype_slug or "mac-studio" in atype_slug:
            return "macos"
        if "iphone" in atype_slug:
            return "ios"
        if "ipad" in atype_slug:
            return "ipados"
        if "galaxy" in atype_slug:
            return "android"
        if atype_slug == "synology-ds1823xs":
            return "embedded"
        if atype_slug in ("dell-poweredge-r760", "hpe-proliant-dl380-g11"):
            return "linux"
        if atype_slug in ("cisco-catalyst-9300", "unifi-switch-pro-48", "meraki-mr46", "unifi-dream-machine-pro"):
            return "network_os"
        if "thinkpad" in atype_slug or "precision" in atype_slug:
            return random.choice(["windows", "linux"])
        return "windows"

    def _os_version_for(self, atype_slug, os_family=None):
        os_family = os_family or self._os_family_for(atype_slug)
        versions = {
            "macos": "macOS Sequoia 15.1",
            "ios": "iOS 17.6",
            "ipados": "iPadOS 17.6",
            "android": "Android 14",
            "embedded": "DSM 7.2",
            "network_os": "Network OS 17.12",
            "linux": "Ubuntu 24.04 LTS",
            "windows": "Windows 11 23H2",
        }
        return versions.get(os_family, os_family)

    def _seed_component_allocation(self, allocation_model, component, qty, asset):
        """Create a planned component allocation through the authorized seam.

        Assignment writes are protected before stock bookkeeping, so the seed
        obtains an audited actorless authorization in the asset's tenant and
        uses the service rather than writing the allocation row directly.
        Idempotency is an explicit live-row check because the service has no
        ``get_or_create`` equivalent.
        """
        if allocation_model._base_manager.filter(
            component=component,
            assigned_asset=asset,
            deleted_at__isnull=True,
        ).exists():
            return False
        with TaskContext(tenant_id=asset.tenant_id) as task_context:
            create_component_allocation(
                component,
                qty,
                asset=asset,
                system_authorization=task_context.authorize_system(
                    permission="inventory.add_componentallocation",
                    operation=COMPONENT_ALLOCATION_OPERATION,
                    reason="Seed approved same-tenant component allocations",
                ),
            )
        return True

    def _apply_seed_specifications(self, asset, values):
        if not values:
            return
        apply_asset_specification_patch(asset_id=asset.pk, user=self._provisioner, set_values=values)
        asset.refresh_from_db(fields=["custom_field_data"])

    def _seed_reissue_history(self, asset, holders, current_holder):
        """Give ~a quarter of the laptops a prior, closed loan to someone else.

        The prior loan is dated relative to the asset's own purchase date, never
        relative to "now": a laptop bought six months ago cannot have been on loan 700
        days ago, and an assignment predating the asset would read as "still held"
        during any repair window drawn after the purchase date — a lifecycle the
        product could never have produced.
        """
        prev = random.choice([h for h in holders if h.pk != current_holder.pk])
        p_date = asset.purchase_date or days_ago(400)
        # The prior loan must fit strictly between the purchase and the end of the
        # history the simulation can still reach. A return date in the future is not
        # a closed loan: the row would be marked inactive while its interval still
        # covers months of history, so it reads as "this person held the unit" across
        # any repair drawn in that stretch. Closes well before today instead.
        latest = days_ago(90)
        if p_date >= latest:
            return
        span = (latest - p_date).days
        co = p_date + datetime.timedelta(days=random.randint(0, span))
        room = (latest - co).days
        if room < 1:
            return
        ci = co + datetime.timedelta(days=random.randint(1, room))
        AssetAssignment.objects.create(
            asset=asset,
            assigned_user=prev,
            checked_out_by=self._provisioner,
            checked_out_at=as_aware_datetime(co),
            is_active=False,
            checked_in_at=as_aware_datetime(ci),
            checked_in_by=self._provisioner,
            notes="Previously issued; returned to the service desk on a role change.",
        )

    def _seed_assets(self):

        self.stdout.write("--- Assets ---")

        # Global custody templates (category-matched), fully configured with QMS
        # references, disclaimers and tags.
        self._custody_templates = {}
        for name, slug, cat_slug, eula, tag_slugs in [
            (
                "Standard Workstation & Laptop Agreement",
                "laptop-agreement",
                "laptops",
                "I acknowledge receipt of the issued laptop/workstation and agree to the acceptable-use and "
                "disk-encryption policy. The equipment remains company property and must be returned on demand.",
                ["production", "encrypted"],
            ),
            (
                "Mobile Device Agreement",
                "mobile-agreement",
                "mobile-phones",
                "I acknowledge receipt of the mobile device and SIM. I will keep it secured with a passcode/biometrics "
                "and will not remove the mobile device management (MDM) profile.",
                ["mdm-enrolled"],
            ),
            (
                "Desktop Workstation Agreement",
                "desktop-agreement",
                "desktops",
                "I acknowledge custody of the desktop workstation and agree not to modify its hardware or connect it to "
                "unauthorized networks without IT approval.",
                ["production"],
            ),
            (
                "Field Tablet Agreement",
                "tablet-agreement",
                "tablets",
                "I acknowledge receipt of the ruggedized field tablet and agree to return it undamaged at the end of "
                "my assignment. Lost or damaged devices must be reported to the service desk within 24 hours.",
                ["field", "mdm-enrolled"],
            ),
        ]:
            tmpl = CustodyTemplate.objects.get_or_create(
                name=name,
                defaults={
                    "category": self._categories[cat_slug],
                    "eula_text": eula,
                    "disclaimer": "This equipment remains the property of the organization.",
                    "qms_reference": f"NMS-IT-{slug.upper()}",
                    "is_active": True,
                    "require_acceptance": True,
                    "email_signature_request": True,
                    "signature_provider": "local",
                },
            )[0]
            tmpl.tags.set([self._tags[t] for t in tag_slugs if t in self._tags])
            self._custody_templates[cat_slug] = tmpl

        # Tenant-scoped, regulator-specific agreements (PCI / GxP) — show per-tenant
        # custody configuration layered on top of the global defaults.
        for tname, tslug, cat_slug, qms, eula, tags in [
            (
                "Meridian: PCI-DSS Laptop Custody Agreement",
                "meridian-retail",
                "laptops",
                "MER-PCI-IT-007",
                "I acknowledge that this device operates within PCI-DSS scope. I will not store cardholder data locally, "
                "will keep the endpoint agent active and will comply with quarterly access reviews.",
                ["pci-scope", "critical"],
            ),
            (
                "Brightwell: Confidential Records Custody Agreement",
                "brightwell-legal",
                "laptops",
                "BWL-LEG-IT-003",
                "I acknowledge custody of a device with access to privileged client matter files and agree to full-disk "
                "encryption and the firm’s information-barrier policy.",
                ["encrypted", "vip"],
            ),
        ]:
            tenant = self._tenants.get(tslug)
            if not tenant:
                continue
            t = CustodyTemplate.objects.get_or_create(
                name=tname,
                defaults={
                    "tenant": tenant,
                    "category": self._categories[cat_slug],
                    "eula_text": eula,
                    "disclaimer": "Regulated equipment. Handle it according to the referenced policy.",
                    "qms_reference": qms,
                    "is_active": True,
                    "require_acceptance": True,
                    "email_signature_request": True,
                    "signature_provider": "local",
                },
            )[0]
            t.tags.set([self._tags[x] for x in tags if x in self._tags])
            self._custody_templates.setdefault(f"{tslug}:{cat_slug}", t)

        # A group-scoped (tenant_group) GxP laptop agreement shared across all Helix
        # Biopharma entities — exercises CustodyTemplate.tenant_group / group-wide sharing.
        helix_group = self._tgroups.get("helix-biopharma")
        self._gxp_custody_template = None
        if helix_group:
            self._gxp_custody_template = CustodyTemplate.objects.get_or_create(
                name="Helix Biopharma: GxP Laptop & Workstation Agreement",
                defaults={
                    "tenant_group": helix_group,
                    "category": self._categories["laptops"],
                    "eula_text": (
                        "I acknowledge receipt of a GxP-validated workstation. I will not install "
                        "unvalidated software, will keep audit logging enabled and will follow the "
                        "Helix Biopharma QMS change-control procedure for any modification."
                    ),
                    "disclaimer": "GxP-validated equipment is subject to QMS change control.",
                    "qms_reference": "HLX-QMS-IT-014",
                    "is_active": True,
                    "require_acceptance": True,
                    "email_signature_request": True,
                    "signature_provider": "local",
                },
            )[0]

        self._assets = []
        self._assets_by_tenant = {}
        self._laptops_by_tenant = {}
        self._primary_laptop_by_holder = {}  # holder.pk -> current laptop (for license seats)
        self._retired_assets = []
        self._servers = []

        # Per-tenant asset-tag sequence (prefix = tenant code). Assets are created with a
        # blank asset_tag so Asset.save() draws the next value from AssetTagSequence — the
        # product's real numbering mechanism — instead of hand-formatted tags.
        for slug, tenant in self._tenants.items():
            AssetTagSequence.objects.get_or_create(
                tenant=tenant,
                category=None,
                defaults={
                    "prefix": f"{self._tenant_meta[slug]['code']}-",
                    "zero_padding": 4,
                    "next_value": 1,
                    "is_active": True,
                },
            )

        def shared_location(tenant_slug):
            locs = self._tenant_locations[tenant_slug]
            for kw in ("srv", "rack", "dc", "server", "network", "closet", "cabinet", "farm"):
                for loc in locs:
                    if kw in loc.slug:
                        return loc
            return locs[0] if locs else None

        def make_asset(tenant, code, atype_slug, status_slug, holder, location, dept, tags=None):
            atype = self._asset_types[atype_slug]
            role = atype.asset_role
            base_cost = self.PRICES.get(atype_slug, 1000)
            cost = round(base_cost * random.uniform(0.95, 1.05), 2)
            years_old = random.choice([0, 1, 1, 2, 2, 3])
            p_date = days_ago(years_old * 365 + random.randint(0, 300))
            warranty = p_date + datetime.timedelta(days=(atype.eol_months or 36) * 30)
            category_slug = atype.category.slug if atype.category else ""

            base_location = None if (holder and status_slug == "in-use") else location
            salvage = round(cost * 0.1, 2)
            # In service a few days after purchase; book value straight-lined to salvage over EoL.
            in_service = p_date + datetime.timedelta(days=random.randint(2, 14))
            eol_months = atype.eol_months or 36
            age_months = max(0, (TODAY - p_date).days / 30.0)
            fraction = min(age_months / eol_months, 1.0)
            book_value = round(cost - (cost - salvage) * fraction, 2)
            # Blank asset_tag → Asset.save() draws the next value from the tenant's
            # AssetTagSequence. Tag-derived fields (hostname, display name) are filled
            # in the second save, once the generated tag is known.
            asset = Asset(
                name=atype.model,
                asset_tag="",
                asset_type=atype,
                asset_role=role,
                status=self._status_labels["available"]
                if status_slug == "in-use"
                else self._status_labels[status_slug],
                location=base_location,
                tenant=tenant,
                serial_number=f"{code}{random.randint(100000, 999999)}",
                purchase_cost=cost,
                salvage_value=salvage,
                purchase_date=p_date,
                in_service_date=in_service,
                current_book_value=book_value,
                depreciation_updated_at=timezone.now(),
                supplier=self._suppliers[random.choice(self.HW_SUPPLIERS)],
                order_number=f"PO-{p_date.year}-{random.randint(1000, 9999)}",
                custom_field_data={},
            )
            asset.save()

            Warranty.objects.create(
                asset=asset,
                warranty_type=WarrantyTypeChoices.HARDWARE,
                supplier=asset.supplier,
                start_date=p_date,
                end_date=warranty,
            )
            tag = asset.asset_tag
            host = f"{code.lower()}-{tag.split('-')[-1]}"
            cv = {}
            if category_slug in {"laptops", "desktops"}:
                cv = {"hostname": host, "operating_system_family": self._os_family_for(atype_slug)}
            elif category_slug in {"mobile-phones", "tablets"}:
                cv = {"operating_system_family": self._os_family_for(atype_slug)}
            elif category_slug in {"servers", "storage-devices"}:
                cv = {
                    "hostname": f"{host}.svc.local",
                    "operating_system_family": self._os_family_for(atype_slug),
                    "firmware_version": f"v{random.randint(7, 17)}.{random.randint(0, 12)}.{random.randint(0, 9)}",
                }
            elif category_slug in {"network-devices", "switches", "access-points", "routers"}:
                # Network gear by canonical starter category; the local demo category
                # stays supported for users who assign it.
                cv = {
                    "hostname": f"{host}.net.local",
                    "firmware_version": f"v{random.randint(7, 17)}.{random.randint(0, 12)}.{random.randint(0, 9)}",
                }
            elif category_slug == "conference-systems":
                cv = {"operating_system_family": "embedded"}
            # Name is the model only (e.g. "Catalyst 9300"); the unique asset_tag is its
            # own field and column, not baked into the display name.
            self._apply_seed_specifications(asset, cv)
            if tags:
                asset.tags.add(*[self._tags[t] for t in tags if t in self._tags])
            if status_slug == "in-use" and (holder or location):
                if holder:
                    checkout_notes = "Provisioned by Northwind service desk."
                else:
                    checkout_notes = "Deployed to site infrastructure."
                with TaskContext(tenant_id=tenant.pk, user_id=self._provisioner.pk):
                    checkout_asset(
                        asset,
                        holder=holder,
                        location=location,
                        user=self._provisioner,
                        notes=checkout_notes,
                        _suppress_custody_receipt=True,
                    )
                asset.refresh_from_db()
            self._assets.append(asset)
            return asset

        for slug, tenant in self._tenants.items():
            meta = self._tenant_meta[slug]
            profile = self.PROFILES[meta["profile"]]
            code = meta["code"]
            holders = self._tenant_holders[slug]
            locs = self._tenant_locations[slug]
            self._assets_by_tenant[slug] = []
            self._laptops_by_tenant[slug] = []
            regulated = meta["industry"] in ("Pharmaceuticals", "Banking", "Asset Management")

            # Per-employee primary device + optional mobile + monitors
            for holder in holders:
                dept = random.choice(profile["depts"])
                laptop_slug = random.choice(profile["laptops"])
                lt = make_asset(
                    tenant,
                    code,
                    laptop_slug,
                    "in-use",
                    holder,
                    None,
                    dept,
                    tags=["encrypted"] + (["gxp-validated"] if regulated and "pharma" in meta["profile"] else []),
                )
                # ~25% of laptops carry re-issue history: a prior, closed assignment to a
                # different employee who returned the device (lifecycle realism).
                if len(holders) > 3 and random.random() < 0.25:
                    self._seed_reissue_history(lt, holders, holder)
                self._laptops_by_tenant[slug].append(lt)
                self._assets_by_tenant[slug].append(lt)
                self._primary_laptop_by_holder[holder.pk] = lt
                if random.random() < profile["mobile"]:
                    self._assets_by_tenant[slug].append(
                        make_asset(
                            tenant,
                            code,
                            random.choice(["iphone-15-pro", "galaxy-s24-ultra"]),
                            "in-use",
                            holder,
                            None,
                            dept,
                            tags=["mdm-enrolled"],
                        )
                    )
                n_mon = int(profile["monitors"]) + (1 if random.random() < (profile["monitors"] % 1) else 0)
                for _ in range(n_mon):
                    self._assets_by_tenant[slug].append(
                        make_asset(
                            tenant,
                            code,
                            random.choice(["dell-p2723de-monitor", "dell-p2422he-monitor"]),
                            "in-use",
                            holder,
                            None,
                            dept,
                        )
                    )

            # A few loaner/spare and repair units
            for _ in range(max(1, len(holders) // 12)):
                self._assets_by_tenant[slug].append(
                    make_asset(
                        tenant,
                        code,
                        random.choice(profile["laptops"]),
                        "available",
                        None,
                        locs[0] if locs else None,
                        random.choice(profile["depts"]),
                        tags=["loaner"],
                    )
                )
            if len(holders) > 8:
                self._assets_by_tenant[slug].append(
                    make_asset(
                        tenant,
                        code,
                        random.choice(profile["laptops"]),
                        "pending-repair",
                        None,
                        locs[0] if locs else None,
                        random.choice(profile["depts"]),
                    )
                )

            # Shared / infrastructure assets
            infra_loc = shared_location(slug)
            for atype_slug, count in profile["shared"]:
                for _ in range(count):
                    a = make_asset(
                        tenant,
                        code,
                        atype_slug,
                        "in-use",
                        None,
                        infra_loc,
                        "Operations",
                        tags=["production"] if "poweredge" in atype_slug else None,
                    )
                    self._assets_by_tenant[slug].append(a)
                    if a.asset_role and a.asset_role.slug in (
                        "virtualization-host-server",
                        "database-server",
                        "application-server",
                        "backup-server",
                    ):
                        self._servers.append(a)

            # A couple of retired/replaced devices per larger tenant: the old unit was
            # superseded by a current in-use laptop (lifecycle + disposal realism).
            if len(holders) > 6:
                for _ in range(random.randint(1, 2)):
                    replacement = (
                        random.choice(self._laptops_by_tenant[slug]) if self._laptops_by_tenant[slug] else None
                    )
                    old = make_asset(
                        tenant,
                        code,
                        random.choice(profile["laptops"]),
                        "retired",
                        None,
                        locs[0] if locs else None,
                        random.choice(profile["depts"]),
                        tags=["legacy"],
                    )
                    disposed = timezone.now() - datetime.timedelta(days=random.randint(20, 180))
                    old.disposed_at = disposed
                    old.disposal_value = round(float(old.purchase_cost) * random.uniform(0.03, 0.12), 2)
                    old.current_book_value = 0
                    if replacement:
                        old.notes = f"Decommissioned and superseded by {replacement.asset_tag} ({replacement.name})."
                    old.save(update_fields=["disposed_at", "disposal_value", "current_book_value", "notes"])
                    self._assets_by_tenant[slug].append(old)
                    self._retired_assets.append(old)

        # Installed software — every managed endpoint carries a security/productivity
        # baseline (discovered by the MDM/inventory agent), plus role- and industry-
        # specific titles. Versions are populated so the software inventory looks real.
        sw_versions = {
            "Microsoft 365 E5": ["16.83.2", "16.84.1", "16.85.0"],
            "CrowdStrike Falcon": ["7.16.18019", "7.17.18101", "7.18.18206"],
            "1Password Business": ["8.10.40", "8.10.42", "8.10.44"],
            "Zoom Workplace Enterprise": ["6.0.10", "6.1.5", "6.2.0"],
            "Microsoft Office LTSC 2024": ["16.0.17328", "16.0.17425"],
            "Adobe Creative Cloud": ["6.4.0.345", "6.5.0.348"],
            "JetBrains All Products Pack": ["2024.1.4", "2024.2.1"],
            "Autodesk AutoCAD": ["2024.1.3", "2025.0.1"],
            "Bloomberg Terminal": ["DAPI-4.18", "DAPI-4.19"],
            "SAS Analytics Pro": ["9.4M8", "9.4M9"],
        }

        def install(asset, sw_name, agent, install_date):
            sw = self._software.get(sw_name)
            if not sw:
                return 0
            ver = random.choice(sw_versions.get(sw_name, [""]))
            _, created = InstalledSoftware.objects.get_or_create(
                asset=asset,
                software=sw,
                version_detected=ver,
                defaults={
                    "discovered_by_agent": agent,
                    "install_date": install_date,
                    "last_seen_date": timezone.now() - datetime.timedelta(days=random.randint(0, 14)),
                },
            )
            return int(created)

        sw_count = 0
        for slug in self._tenants:
            meta = self._tenant_meta[slug]
            for lt in self._laptops_by_tenant[slug]:
                agent = "Lansweeper" if meta["industry"] == "Asset Management" else "Intune"
                # OS family is a stable choice key in the final core vocabulary.
                os_family = lt.custom_field_data.get("operating_system_family", "windows")
                os_version = self._os_version_for(lt.asset_type.slug, os_family)
                os_sw = {
                    "macos": "macOS Sequoia",
                    "linux": "Ubuntu Pro 24.04",
                    "windows": "Windows 11 Enterprise",
                }.get(os_family, "Windows 11 Enterprise")
                os_obj = self._software.get(os_sw)
                if os_obj:
                    _, c = InstalledSoftware.objects.get_or_create(
                        asset=lt,
                        software=os_obj,
                        version_detected=os_version,
                        defaults={
                            "discovered_by_agent": agent,
                            "install_date": lt.purchase_date,
                            "last_seen_date": timezone.now() - datetime.timedelta(days=random.randint(0, 7)),
                        },
                    )
                    sw_count += int(c)
                # Security + productivity baseline on every laptop.
                for sw_name in (
                    "Microsoft 365 E5",
                    "CrowdStrike Falcon",
                    "1Password Business",
                    "Zoom Workplace Enterprise",
                ):
                    sw_count += install(lt, sw_name, agent, lt.purchase_date)
                # Role / industry specific titles.
                role_slug = lt.asset_role.slug if lt.asset_role else ""
                if role_slug == "developer-workstation":
                    sw_count += install(lt, "JetBrains All Products Pack", agent, lt.purchase_date)
                if meta["industry"] == "Architecture & Design":
                    sw_count += install(lt, "Autodesk AutoCAD", agent, lt.purchase_date)
                    sw_count += install(lt, "Adobe Creative Cloud", agent, lt.purchase_date)
                if meta["industry"] in ("Banking", "Asset Management") and random.random() < 0.4:
                    sw_count += install(lt, "Bloomberg Terminal", agent, lt.purchase_date)
                if meta["industry"] == "Pharmaceuticals" and random.random() < 0.3:
                    sw_count += install(lt, "SAS Analytics Pro", agent, lt.purchase_date)
                if meta["kind"] == "msp" and random.random() < 0.5:
                    sw_count += install(lt, "Microsoft Office LTSC 2024", agent, lt.purchase_date)
            # Mobile devices report MDM-managed apps too.
            for a in self._assets_by_tenant[slug]:
                if a.asset_type and a.asset_type.category and a.asset_type.category.slug == "mobile-phones":
                    for sw_name in ("Microsoft 365 E5", "1Password Business"):
                        sw_count += install(a, sw_name, "Intune", a.purchase_date)
        for srv in self._servers:
            for sw_name in random.sample(
                ["VMware vSphere 8 Enterprise Plus", "Ubuntu Pro 24.04", "Veeam Backup & Replication"], 2
            ):
                _, created = InstalledSoftware.objects.get_or_create(
                    asset=srv,
                    software=self._software[sw_name],
                    version_detected="",
                    defaults={
                        "discovered_by_agent": "Lansweeper",
                        "install_date": srv.purchase_date,
                        "last_seen_date": timezone.now() - datetime.timedelta(days=random.randint(0, 7)),
                    },
                )
                sw_count += created

        # Plan component allocations here without adding random draws to the
        # asset phase. The inventory phase creates them after component stock
        # exists, so the authorized service can enforce availability normally.
        self._component_allocation_plan = []
        cad = [
            a
            for a in self._assets
            if a.asset_type
            and a.asset_type.slug in ("dell-precision-7960-tower", "mac-studio-2024", "dell-precision-5680")
        ]
        for srv in self._servers:
            for comp_slug, qty in [
                ("samsung-32gb-ddr5", 4),
                ("samsung-2tb-nvme", 2),
                ("wd-red-8tb", 4),
                ("intel-x710-nic", 1),
                ("dell-perc-h755", 1),
            ]:
                if random.random() < 0.7:
                    self._component_allocation_plan.append((comp_slug, qty, srv))
        for ws in cad[:30]:
            for comp_slug, qty in [("crucial-16gb-ddr5", 2), ("nvidia-rtx-6000", 1), ("samsung-2tb-nvme", 1)]:
                if random.random() < 0.6:
                    self._component_allocation_plan.append((comp_slug, qty, ws))

        # Custody receipts for regulated-industry laptops/mobiles (accepted, no
        # signature image).
        receipts = 0
        for slug, _tenant in self._tenants.items():
            # Sign receipts for regulated industries plus any tenant that has its own
            # tenant-scoped custody template (e.g. Brightwell Legal).
            has_scoped = any(k.startswith(f"{slug}:") for k in self._custody_templates)
            if self._tenant_meta[slug]["industry"] not in ("Pharmaceuticals", "Banking") and not has_scoped:
                continue
            is_helix = self._tenant_meta[slug]["group_slug"] == "helix-biopharma"
            for asset in self._assets_by_tenant[slug]:
                cat = asset.asset_type.category.slug if asset.asset_type and asset.asset_type.category else None
                # Preference: tenant-scoped template > group GxP (Helix laptops) > global default.
                if f"{slug}:{cat}" in self._custody_templates:
                    tmpl = self._custody_templates[f"{slug}:{cat}"]
                elif is_helix and cat == "laptops" and self._gxp_custody_template:
                    tmpl = self._gxp_custody_template
                else:
                    tmpl = self._custody_templates.get(cat)
                active = asset.assignments.filter(is_active=True, assigned_user__isnull=False).first()
                if not (tmpl and active and random.random() < 0.5):
                    continue
                holder = active.assigned_user
                h = hashlib.sha256(f"{asset.asset_tag}-{holder.pk}-{timezone.now()}".encode()).hexdigest()[:64]
                signed_at = timezone.now() - datetime.timedelta(days=random.randint(5, 200))
                # Accepted demo receipts are modeled without a signature image:
                # no placeholder PNG payload is stored, and the internal detail
                # surface renders its explicit no-image state.
                CustodyReceipt.objects.create(
                    asset=asset,
                    holder=holder,
                    custody_template=tmpl,
                    verification_hash=h,
                    eula_version="1.0",
                    acceptance_status=CustodyReceipt.STATUS_ACCEPTED,
                    accepted_date=signed_at,
                    signed_at=signed_at,
                )
                receipts += 1

        self.stdout.write(
            f"  {len(self._assets)} assets, {sw_count} software installs, "
            f"{len(self._component_allocation_plan)} component allocations planned, {receipts} custody receipts."
        )
