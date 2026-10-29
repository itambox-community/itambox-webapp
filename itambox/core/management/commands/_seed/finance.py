"""Finance seed mixin: cost centres, service contracts, cost-centre backfill.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.finance import SeedFinanceMixin

    class Command(SeedFinanceMixin, BaseCommand):
        ...

``_seed_cost_centers`` must run right after ``_seed_organizations``.
``_seed_contracts_and_costing`` must run after ``_seed_licensing`` and
``_seed_subscriptions`` have populated ``self._licenses`` / ``self._subscriptions``.
"""

import datetime
import random

from core.management.commands._seed.engine import ChangeLogEngine
from organization.models import CostCenter

TODAY = datetime.date.today()


def _days_ago(n):
    return TODAY - datetime.timedelta(days=n)


# ─────────────────────────────────────────────────────────────────────────────
# Industry-driven cost-centre templates
# Each entry: (name, code_suffix, description)
# ─────────────────────────────────────────────────────────────────────────────

_CC_TEMPLATES = {
    # MSP own tenants
    "msp": [
        ("IT / Infrastructure", "CC-110", "Core infrastructure, DC, network, security tooling"),
        ("End-User Computing", "CC-120", "Laptops, mobile devices, peripherals, helpdesk"),
        ("Operations", "CC-130", "NOC, service-desk tools, monitoring"),
        ("Sales & Marketing", "CC-140", "CRM, demo gear, travel tech"),
    ],
    # Pharma
    "pharma_rnd": [
        ("IT / Infrastructure", "CC-110", "Core network, server, storage"),
        ("End-User Computing", "CC-120", "Workstations and laptops for research staff"),
        ("R&D Systems", "CC-200", "Lab informatics, LIMS, scientific software"),
        ("GxP Compliance", "CC-210", "Validated systems, audit trail tooling"),
    ],
    "pharma_mfg": [
        ("IT / Infrastructure", "CC-110", "Plant network and server infrastructure"),
        ("Manufacturing Systems", "CC-300", "MES, SCADA, line terminals"),
        ("QA / Validation", "CC-310", "QA workstations, calibration systems"),
    ],
    "pharma_commercial": [
        ("IT / Infrastructure", "CC-110", "Office network and servers"),
        ("End-User Computing", "CC-120", "Sales laptops and phones"),
        ("CRM & Digital Marketing", "CC-140", "Salesforce, marketing automation"),
    ],
    # Banking
    "bank_retail": [
        ("IT / Infrastructure", "CC-110", "Data centre, network, storage"),
        ("End-User Computing", "CC-120", "Branch and back-office desktops"),
        ("Core Banking", "CC-400", "Core banking platform and middleware"),
        ("Compliance & RegTech", "CC-410", "AML, KYC, audit tooling"),
    ],
    "bank_invest": [
        ("IT / Infrastructure", "CC-110", "Trading infrastructure"),
        ("Trading Technology", "CC-420", "Bloomberg terminals, OMS, market-data feeds"),
        ("Risk Systems", "CC-430", "Pre-trade risk, VaR tooling"),
    ],
    "bank_risk": [
        ("IT / Infrastructure", "CC-110", "Office and server infrastructure"),
        ("Risk Analytics", "CC-430", "Risk-modelling workstations and software"),
    ],
    # Asset management
    "fund_portfolio": [
        ("IT / Infrastructure", "CC-110", "Office network and servers"),
        ("Portfolio Management", "CC-500", "Portfolio and order-management systems"),
        ("Compliance", "CC-510", "Regulatory reporting and record-keeping"),
    ],
    "fund_ops": [
        ("IT / Infrastructure", "CC-110", "Office infrastructure"),
        ("Fund Administration", "CC-520", "NAV calculation, transfer-agent systems"),
    ],
    # Legal
    "legal": [
        ("IT / Infrastructure", "CC-110", "Network, servers, backup"),
        ("End-User Computing", "CC-120", "Attorney workstations and laptops"),
        ("Document Management", "CC-600", "DMS, e-discovery, contract tooling"),
    ],
    # Architecture
    "architecture": [
        ("IT / Infrastructure", "CC-110", "Network, servers, render farm"),
        ("Design & Visualisation", "CC-700", "CAD workstations, render nodes, GPU cluster"),
        ("End-User Computing", "CC-120", "General office laptops and peripherals"),
    ],
    # Logistics
    "logistics": [
        ("IT / Infrastructure", "CC-110", "Depot network and servers"),
        ("Warehouse Technology", "CC-800", "Scanners, terminals, WMS"),
        ("Fleet & Tracking", "CC-810", "GPS/IoT devices, telemetry"),
    ],
    # Default fallback for any profile not listed above
    "_default": [
        ("IT / Infrastructure", "CC-110", "Core infrastructure"),
        ("End-User Computing", "CC-120", "End-user devices and helpdesk"),
        ("Operations", "CC-130", "Business operations"),
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# Contract templates: (name_tpl, contract_type, billing_cycle, cost_range,
#                      sla_response, sla_resolution, coverage, notes,
#                      supplier_slug_hint, duration_months)
# ─────────────────────────────────────────────────────────────────────────────

_CONTRACT_TEMPLATES = [
    (
        "{supplier} Hardware Support: {tenant_code}",
        "maintenance",
        "annual",
        (2_400, 9_600),
        "4 business hours",
        "1 business day",
        "9-5 Mon-Fri",
        "Standard next-business-day on-site hardware maintenance.",
        ["dell-direct", "bechtle-ag", "cdw-deutschland"],
        12,
    ),
    (
        "{supplier} Premium Care: {tenant_code}",
        "maintenance",
        "annual",
        (4_800, 18_000),
        "2 business hours",
        "4 business hours",
        "24x7",
        "ProSupport Plus: 24×7 on-site with accidental-damage coverage.",
        ["dell-direct", "apple-business"],
        24,
    ),
    (
        "{supplier} Network SLA: {tenant_code}",
        "support",
        "annual",
        (3_600, 12_000),
        "4 business hours",
        "8 business hours",
        "24x7",
        "TAC support + SmartNet for all network infrastructure devices.",
        ["bechtle-ag", "insight-enterprises"],
        12,
    ),
    (
        "{supplier} Software Assurance: {tenant_code}",
        "support",
        "annual",
        (1_200, 6_000),
        "Next business day",
        "3 business days",
        "9-5 Mon-Fri",
        "Software assurance, version entitlement and L2/L3 helpdesk.",
        ["cdw-deutschland", "insight-enterprises", "northwind-procurement"],
        12,
    ),
    (
        "{supplier} Server Infrastructure SLA: {tenant_code}",
        "support",
        "annual",
        (5_000, 22_000),
        "2 business hours",
        "6 business hours",
        "24x7",
        "Mission-critical server support with 4-hour hardware replacement.",
        ["dell-direct", "bechtle-ag"],
        24,
    ),
    (
        "{supplier} Managed Endpoint Lease: {tenant_code}",
        "lease",
        "monthly",
        (800, 3_200),
        "Next business day",
        "3 business days",
        "9-5 Mon-Fri",
        "Device-as-a-service: refresh every 36 months, loaner pool included.",
        ["dell-direct", "cdw-deutschland"],
        36,
    ),
]


class SeedFinanceMixin:
    """Adds cost-centre creation and contract / costing backfill to the seed command."""

    # ──────────────────────────────────────────────────────────────────────────
    # Helper: lazy engine initialisation
    # ──────────────────────────────────────────────────────────────────────────

    def _get_engine(self):
        """Return the shared ChangeLogEngine, creating it once if needed."""
        if not hasattr(self, "_engine") or self._engine is None:
            self._engine = ChangeLogEngine(stdout=self.stdout, style=self.style)
        return self._engine

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Cost centres
    # ──────────────────────────────────────────────────────────────────────────

    def _seed_cost_centers(self):

        engine = self._get_engine()
        self.stdout.write("--- Finance: cost centres ---")

        self._cost_centers = {}  # tenant_slug -> [CostCenter]
        total = 0

        for slug, tenant in self._tenants.items():
            meta = self._tenant_meta.get(slug, {})
            profile = meta.get("profile", "_default")

            templates = _CC_TEMPLATES.get(profile, _CC_TEMPLATES["_default"])

            # Onboarding was ~2 years ago for all tenants; scatter slightly.
            onboard_days = random.randint(700, 760)

            ccs = []
            seen_codes = set()
            for name, base_code, desc in templates:
                # Keep codes unique per tenant (tenant+code is the unique constraint)
                code = base_code
                suffix = 1
                while code in seen_codes:
                    code = f"{base_code}-{suffix}"
                    suffix += 1
                seen_codes.add(code)

                cc = CostCenter.objects.create(
                    tenant=tenant,
                    name=name,
                    code=code,
                    description=desc,
                    is_active=True,
                )
                eng_user = random.choice(self._engineer_users)
                engine.log_create(
                    cc,
                    when=_days_ago(onboard_days - random.randint(0, 30)),
                    user=eng_user,
                )
                ccs.append(cc)
                total += 1

            self._cost_centers[slug] = ccs

        self.stdout.write(f"  {total} cost centres across {len(self._cost_centers)} tenants.")

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Contracts + cost-centre backfill
    # ──────────────────────────────────────────────────────────────────────────

    def _seed_contracts_and_costing(self):
        # inline import: app-registry: Resolve procurement models only after Django has populated the app registry.
        from procurement.models import (
            Contract,
            ContractStatusChoices,
        )

        engine = self._get_engine()
        self.stdout.write("--- Finance: contracts & costing ---")

        self._contracts = []
        contract_count = 0
        renewal_count = 0

        for slug, tenant in self._tenants.items():
            meta = self._tenant_meta.get(slug, {})
            currency = meta.get("currency", "EUR")
            ccs = self._cost_centers.get(slug, [])

            # Pick 2–4 contract templates per tenant
            n_contracts = random.randint(2, 4)
            chosen_templates = random.sample(_CONTRACT_TEMPLATES, k=min(n_contracts, len(_CONTRACT_TEMPLATES)))

            for (
                name_tpl,
                ctype,
                billing,
                (cost_lo, cost_hi),
                sla_resp,
                sla_res,
                coverage,
                notes,
                preferred_suppliers,
                duration_months,
            ) in chosen_templates:
                # Pick a supplier — prefer the hint list, fall back to any
                supplier = None
                for sup_slug in preferred_suppliers:
                    if sup_slug in self._suppliers:
                        supplier = self._suppliers[sup_slug]
                        break
                if supplier is None:
                    supplier = random.choice(list(self._suppliers.values()))

                # Contract name
                name = name_tpl.format(
                    supplier=supplier.name,
                    tenant_code=meta.get("code", slug.upper()[:6]),
                )

                # Dates: started somewhere in the past 2 years
                start_days_ago = random.randint(60, 700)
                start_date = _days_ago(start_days_ago)
                end_date = start_date + datetime.timedelta(days=30 * duration_months)
                renewal_date = end_date - datetime.timedelta(days=random.randint(30, 60))

                # Status: expired if end_date is past, else active
                if end_date < TODAY:
                    status = ContractStatusChoices.EXPIRED
                else:
                    status = ContractStatusChoices.ACTIVE

                cost = round(random.uniform(cost_lo, cost_hi), 2)

                # Optional cost-centre link
                cc = random.choice(ccs) if ccs else None

                contract_number = (
                    f"CTR-{meta.get('code', slug[:6].upper())}-{start_date.year}-{random.randint(100, 999)}"
                )

                contract = Contract.objects.create(
                    tenant=tenant,
                    name=name,
                    contract_number=contract_number,
                    contract_type=ctype,
                    status=status,
                    supplier=supplier,
                    cost=cost,
                    currency=currency,
                    billing_cycle=billing,
                    start_date=start_date,
                    end_date=end_date,
                    renewal_date=renewal_date,
                    auto_renew=random.random() < 0.4,
                    sla_response_time=sla_resp,
                    sla_resolution_time=sla_res,
                    coverage_hours=coverage,
                    sla_terms=notes,
                    cost_center=cc,
                    notes="",
                )

                eng_user = random.choice(self._engineer_users)
                engine.log_create(contract, when=start_date, user=eng_user)

                # ~30 % of active contracts: log a renewal / cost-update edit
                if status == ContractStatusChoices.ACTIVE and random.random() < 0.30:
                    new_cost = round(cost * random.uniform(1.03, 1.12), 2)
                    update_when = start_date + datetime.timedelta(
                        days=random.randint(30, max(31, (TODAY - start_date).days - 10))
                    )
                    engine.change(
                        contract,
                        when=update_when,
                        user=random.choice(self._engineer_users),
                        cost=new_cost,
                    )
                    renewal_count += 1

                self._contracts.append(contract)
                contract_count += 1

        # ── Backfill cost_center on assets ──────────────────────────────
        asset_cc_count = 0
        for slug, assets in self._assets_by_tenant.items():
            ccs = self._cost_centers.get(slug, [])
            if not ccs or not assets:
                continue

            # ~60 % of assets get a cost centre
            sample_size = max(1, int(len(assets) * 0.60))
            sample = random.sample(assets, k=min(sample_size, len(assets)))

            # First third: logged edits (visible in history)
            logged_cutoff = len(sample) // 3
            for i, asset in enumerate(sample):
                cc = random.choice(ccs)
                when = _days_ago(random.randint(30, 500))
                if i < logged_cutoff:
                    engine.change(
                        asset,
                        when=when,
                        user=random.choice(self._engineer_users),
                        cost_center=cc,
                    )
                else:
                    asset.cost_center = cc
                    asset.save(update_fields=["cost_center"])
                    engine.touch_created(asset, when)
                asset_cc_count += 1

        # ── Backfill cost_center on licenses ────────────────────────────
        lic_cc_count = 0
        licenses = getattr(self, "_licenses", [])
        for lic in licenses:
            slug = getattr(getattr(lic, "tenant", None), "slug", None)
            if not slug:
                continue
            ccs = self._cost_centers.get(slug, [])
            if not ccs:
                continue
            if random.random() < 0.65:
                cc = random.choice(ccs)
                when = _days_ago(random.randint(20, 400))
                if random.random() < 0.40:
                    engine.change(
                        lic,
                        when=when,
                        user=random.choice(self._engineer_users),
                        cost_center=cc,
                    )
                else:
                    lic.cost_center = cc
                    lic.save(update_fields=["cost_center"])
                lic_cc_count += 1

        # ── Backfill cost_center on subscriptions ───────────────────────
        sub_cc_count = 0
        subscriptions = getattr(self, "_subscriptions", [])
        for sub in subscriptions:
            slug = getattr(getattr(sub, "tenant", None), "slug", None)
            if not slug:
                continue
            ccs = self._cost_centers.get(slug, [])
            if not ccs:
                continue
            if random.random() < 0.55:
                cc = random.choice(ccs)
                when = _days_ago(random.randint(20, 400))
                if random.random() < 0.35:
                    engine.change(
                        sub,
                        when=when,
                        user=random.choice(self._engineer_users),
                        cost_center=cc,
                    )
                else:
                    sub.cost_center = cc
                    sub.save(update_fields=["cost_center"])
                sub_cc_count += 1

        self.stdout.write(
            f"  {contract_count} contracts ({renewal_count} with logged renewal edits); "
            f"cost-centre backfill: {asset_cc_count} assets, "
            f"{lic_cc_count} licenses, {sub_cc_count} subscriptions."
        )
