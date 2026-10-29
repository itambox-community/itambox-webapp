"""Organizations seed mixin: groups, tenants, sites, locations, holders, contacts.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.organizations import SeedOrganizationsMixin

    class Command(SeedOrganizationsMixin, BaseCommand):
        ...

``_seed_organizations`` must run after ``_seed_catalog`` (it reads
``self._demo_depreciation_afa`` / ``self._depreciations`` / ``self._manufacturers``).
It populates ``self._regions``, ``self._sitegroups``, ``self._tgroups``,
``self._tenants``, ``self._tenant_meta``, ``self._provider_tenant`` (the
``is_provider`` MSP tenant every other tenant is ``managed_by``), ``self._sites``,
``self._locations``, ``self._tenant_locations``, ``self._tenant_holders``,
``self._orgs``, ``self._contact_roles`` and ``self._contacts``.

The ``PROFILES`` / ``FIRST_NAMES`` / ``LAST_NAMES`` class attributes and the
``_org_spec`` / ``_make_holders`` helpers live here and are read by later phases
(assets, licensing, subscriptions) via the shared Command instance.
"""

import random

from django.contrib.contenttypes.models import ContentType

from organization.models import AssetHolder


def _seed_saas_suppliers(supplier_model, supplier_data, tenant, tenant_group):
    suppliers = {}
    for name, account_id, portal_url in supplier_data:
        scope = {"tenant": None, "tenant_group": None}
        if name == "Google Cloud Platform":
            scope["tenant"] = tenant
        elif name == "Cloudflare":
            scope["tenant_group"] = tenant_group
        supplier, _ = supplier_model.all_objects.get_or_create(
            name=name,
            tenant=scope["tenant"],
            tenant_group=scope["tenant_group"],
            defaults={"account_id": account_id, "portal_url": portal_url, "website": ""},
        )
        suppliers[name] = supplier
    return suppliers


class SeedOrganizationsMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    # Industry profile -> how assets are distributed for a tenant.
    #   laptops: candidate workstation asset-type slugs (one picked per employee)
    #   mobile:  fraction of employees issued a corporate phone
    #   monitors: avg external monitors per deskbound employee
    #   depts:   department choices for custom fields
    #   shared:  list of (asset_type_slug, count) for shared / infrastructure assets
    PROFILES = {
        "msp_internal": dict(
            laptops=["macbook-pro-16-2024", "thinkpad-x1-carbon-g12", "dell-precision-5680"],
            mobile=0.8,
            monitors=1.5,
            depts=["Engineering", "Operations"],
            shared=[
                ("dell-poweredge-r760", 4),
                ("hpe-proliant-dl380-g11", 2),
                ("synology-ds1823xs", 2),
                ("cisco-catalyst-9300", 2),
                ("unifi-switch-pro-48", 3),
                ("meraki-mr46", 6),
                ("unifi-dream-machine-pro", 2),
                ("mac-studio-2024", 2),
                ("logitech-rally-bar", 2),
            ],
        ),
        "msp_corp": dict(
            laptops=["dell-latitude-5550", "macbook-air-15-2024"],
            mobile=0.5,
            monitors=1.0,
            depts=["Finance", "HR", "Sales", "Marketing"],
            shared=[("dell-optiplex-7010", 4), ("logitech-rally-bar", 2), ("meraki-mr46", 2)],
        ),
        "pharma_rnd": dict(
            laptops=["thinkpad-x1-carbon-g12", "dell-precision-5680", "macbook-pro-16-2024"],
            mobile=0.6,
            monitors=1.3,
            depts=["Research", "Engineering", "Operations"],
            shared=[
                ("dell-poweredge-r760", 2),
                ("hpe-proliant-dl380-g11", 1),
                ("synology-ds1823xs", 1),
                ("cisco-catalyst-9300", 2),
                ("meraki-mr46", 4),
                ("unifi-dream-machine-pro", 1),
                ("ipad-pro-129-2024", 4),
                ("dell-optiplex-7010", 4),
                ("logitech-rally-bar", 2),
            ],
        ),
        "pharma_mfg": dict(
            laptops=["dell-latitude-5550", "hp-elitebook-860-g11"],
            mobile=0.4,
            monitors=0.5,
            depts=["Operations", "Engineering"],
            shared=[
                ("surface-pro-10", 6),
                ("dell-optiplex-7010", 6),
                ("cisco-catalyst-9300", 2),
                ("meraki-mr46", 6),
                ("unifi-dream-machine-pro", 1),
            ],
        ),
        "pharma_commercial": dict(
            laptops=["dell-latitude-5550", "macbook-air-15-2024"],
            mobile=0.7,
            monitors=1.0,
            depts=["Sales", "Marketing", "Finance"],
            shared=[("dell-optiplex-7010", 3), ("meraki-mr46", 3), ("logitech-rally-bar", 2)],
        ),
        "bank_retail": dict(
            laptops=["dell-latitude-5550", "hp-elitebook-860-g11"],
            mobile=0.5,
            monitors=1.5,
            depts=["Sales", "Operations", "Finance"],
            shared=[
                ("dell-poweredge-r760", 2),
                ("cisco-catalyst-9300", 2),
                ("meraki-mr46", 4),
                ("unifi-dream-machine-pro", 1),
                ("dell-optiplex-7010", 6),
                ("logitech-rally-bar", 2),
            ],
        ),
        "bank_invest": dict(
            laptops=["macbook-pro-16-2024", "dell-precision-5680"],
            mobile=0.9,
            monitors=2.0,
            depts=["Sales", "Finance", "Operations"],
            shared=[
                ("dell-poweredge-r760", 2),
                ("cisco-catalyst-9300", 2),
                ("unifi-dream-machine-pro", 1),
                ("logitech-rally-bar", 2),
            ],
        ),
        "bank_risk": dict(
            laptops=["dell-latitude-5550"],
            mobile=0.3,
            monitors=1.5,
            depts=["Finance", "Operations"],
            shared=[("dell-optiplex-7010", 4), ("meraki-mr46", 2)],
        ),
        "fund_portfolio": dict(
            laptops=["macbook-pro-16-2024", "dell-latitude-5550"],
            mobile=0.8,
            monitors=2.0,
            depts=["Finance", "Operations"],
            shared=[("dell-poweredge-r760", 1), ("unifi-dream-machine-pro", 1), ("logitech-rally-bar", 1)],
        ),
        "fund_ops": dict(
            laptops=["dell-latitude-5550"],
            mobile=0.4,
            monitors=1.5,
            depts=["Operations", "Finance"],
            shared=[("dell-optiplex-7010", 4), ("meraki-mr46", 2)],
        ),
        "legal": dict(
            laptops=["hp-elitebook-860-g11", "dell-latitude-5550"],
            mobile=0.6,
            monitors=1.0,
            depts=["Legal", "Operations"],
            shared=[
                ("dell-optiplex-7010", 4),
                ("unifi-dream-machine-pro", 1),
                ("meraki-mr46", 2),
                ("logitech-rally-bar", 1),
            ],
        ),
        "architecture": dict(
            laptops=["macbook-pro-16-2024", "dell-precision-5680"],
            mobile=0.4,
            monitors=2.0,
            depts=["Engineering", "Operations"],
            shared=[
                ("mac-studio-2024", 2),
                ("dell-precision-7960-tower", 2),
                ("dell-poweredge-r760", 1),
                ("unifi-dream-machine-pro", 1),
                ("logitech-rally-bar", 1),
            ],
        ),
        "logistics": dict(
            laptops=["dell-latitude-5550"],
            mobile=0.6,
            monitors=0.4,
            depts=["Operations"],
            shared=[
                ("surface-pro-10", 8),
                ("ipad-pro-129-2024", 4),
                ("dell-optiplex-7010", 4),
                ("cisco-catalyst-9300", 2),
                ("meraki-mr46", 6),
                ("unifi-dream-machine-pro", 1),
            ],
        ),
    }

    FIRST_NAMES = [
        "Anna",
        "Lukas",
        "Sophie",
        "Felix",
        "Marie",
        "Jonas",
        "Lena",
        "Paul",
        "Emma",
        "Leon",
        "Hannah",
        "Noah",
        "Mia",
        "Elias",
        "Clara",
        "Finn",
        "Laura",
        "Ben",
        "Julia",
        "Tim",
        "Sara",
        "David",
        "Nina",
        "Jan",
        "Lea",
        "Tom",
        "Eva",
        "Max",
        "Pia",
        "Niklas",
        "Yusuf",
        "Aisha",
        "Marco",
        "Chloe",
        "Omar",
        "Elena",
        "Raj",
        "Wei",
        "Ingrid",
        "Pierre",
        "Sofia",
        "Karl",
        "Maya",
        "Henrik",
        "Lucia",
        "Andre",
        "Petra",
        "Samir",
        "Greta",
        "Viktor",
    ]
    LAST_NAMES = [
        "Muller",
        "Schmidt",
        "Schneider",
        "Fischer",
        "Weber",
        "Meyer",
        "Wagner",
        "Becker",
        "Hoffmann",
        "Schafer",
        "Koch",
        "Bauer",
        "Richter",
        "Klein",
        "Wolf",
        "Neumann",
        "Schwarz",
        "Zimmermann",
        "Braun",
        "Krueger",
        "Hartmann",
        "Lange",
        "Werner",
        "Krause",
        "Lehmann",
        "Koehler",
        "Maier",
        "Walter",
        "Huber",
        "Kaiser",
        "Fuchs",
        "Peters",
        "Lang",
        "Scholz",
        "Jung",
        "Hahn",
        "Vogel",
        "Friedrich",
        "Keller",
        "Gunther",
        "Frank",
        "Berger",
        "Winkler",
        "Roth",
        "Beck",
        "Lorenz",
        "Baumann",
        "Franke",
        "Albrecht",
        "Ludwig",
    ]

    def _org_spec(self):
        """Compact specification of the MSP and its customers.

        Each multi-tenant 'group' is a corporate family of distinct **legal
        entities** (different legal forms, registered seats and — where the
        country differs — currencies), not a single company split by function.
        Tenant slugs/codes/profiles/sites stay stable; only the legal identity
        (name suffix, currency, registered seat + commercial-register number)
        is added so downstream slug-based wiring keeps working.
        """
        return [
            # kind, group(name,slug,description), domain, [tenants...]
            dict(
                kind="msp",
                group=(
                    "Northwind Managed Services Group",
                    "northwind-msp",
                    "Berlin-based managed-service provider: the operating company plus its corporate holding.",
                ),
                domain="northwind-it.com",
                tenants=[
                    dict(
                        name="Northwind Managed Services GmbH",
                        slug="northwind-internal-it",
                        code="NW-IT",
                        profile="msp_internal",
                        headcount=12,
                        currency="EUR",
                        legal_seat="Berlin, Germany",
                        reg_no="HRB 121544 B (Amtsgericht Berlin-Charlottenburg)",
                        site=(
                            "Northwind Berlin HQ",
                            "nw-berlin-hq",
                            "Berlin",
                            "Friedrichstrasse 88\n10117 Berlin\nGermany",
                            "dach",
                            "corporate-offices",
                            "52.5200",
                            "13.4050",
                        ),
                        extra_sites=[
                            (
                                "Northwind Frankfurt DC",
                                "nw-frankfurt-dc",
                                "Frankfurt",
                                "Hanauer Landstrasse 200\n60314 Frankfurt\nGermany",
                                "dach",
                                "datacenters",
                                "50.1109",
                                "8.6821",
                            )
                        ],
                        locations=[
                            ("Engineering Floor", "nw-eng-floor"),
                            ("Service Desk", "nw-service-desk"),
                            ("DC Rack Row 1", "nw-dc-rack-1"),
                            ("DC Rack Row 2", "nw-dc-rack-2"),
                        ],
                    ),
                    dict(
                        name="Northwind Services Holding GmbH",
                        slug="northwind-corporate",
                        code="NW-CORP",
                        profile="msp_corp",
                        headcount=16,
                        currency="EUR",
                        legal_seat="Berlin, Germany",
                        reg_no="HRB 121310 B (Amtsgericht Berlin-Charlottenburg)",
                        site=(
                            "Northwind Berlin HQ",
                            "nw-berlin-hq",
                            "Berlin",
                            "",
                            "dach",
                            "corporate-offices",
                            None,
                            None,
                        ),
                        locations=[("Finance & HR", "nw-finance-hr"), ("Sales Floor", "nw-sales-floor")],
                    ),
                ],
            ),
            dict(
                kind="customer",
                industry="Pharmaceuticals",
                group=(
                    "Helix Biopharma Group",
                    "helix-biopharma",
                    "Swiss life-sciences group: research (Basel), production (Visp) and commercial (Zürich) entities.",
                ),
                domain="helixbio.com",
                tenants=[
                    dict(
                        name="Helix Biopharma AG",
                        slug="helix-rnd",
                        code="HLX-RD",
                        profile="pharma_rnd",
                        headcount=22,
                        currency="CHF",
                        legal_seat="Basel, Switzerland",
                        reg_no="CHE-114.227.911 (Handelsregister Basel-Stadt)",
                        site=(
                            "Helix Basel Research Campus",
                            "helix-basel",
                            "Basel",
                            "Hochbergerstrasse 60\n4057 Basel\nSwitzerland",
                            "western-europe",
                            "labs-plants",
                            "47.5596",
                            "7.5886",
                        ),
                        locations=[
                            ("Lab Block A", "helix-lab-a"),
                            ("Lab Block B", "helix-lab-b"),
                            ("R&D Offices", "helix-rnd-offices"),
                            ("Server Room", "helix-basel-srv"),
                        ],
                    ),
                    dict(
                        name="Helix Biopharma Production GmbH",
                        slug="helix-mfg",
                        code="HLX-MF",
                        profile="pharma_mfg",
                        headcount=16,
                        currency="CHF",
                        legal_seat="Visp, Switzerland",
                        reg_no="CHE-217.034.882 (Handelsregister Wallis)",
                        site=(
                            "Helix Visp Plant",
                            "helix-visp",
                            "Visp",
                            "Schachenstrasse 12\n3930 Visp\nSwitzerland",
                            "western-europe",
                            "labs-plants",
                            "46.2940",
                            "7.8810",
                        ),
                        locations=[
                            ("Production Line 1", "helix-line-1"),
                            ("Production Line 2", "helix-line-2"),
                            ("QA Lab", "helix-qa-lab"),
                            ("Plant IT Room", "helix-visp-srv"),
                        ],
                    ),
                    dict(
                        name="Helix Biopharma Commercial AG",
                        slug="helix-commercial",
                        code="HLX-CO",
                        profile="pharma_commercial",
                        headcount=14,
                        currency="CHF",
                        legal_seat="Zürich, Switzerland",
                        reg_no="CHE-330.519.704 (Handelsregister Zürich)",
                        site=(
                            "Helix Zurich Office",
                            "helix-zurich",
                            "Zurich",
                            "Bahnhofstrasse 45\n8001 Zurich\nSwitzerland",
                            "western-europe",
                            "corporate-offices",
                            "47.3769",
                            "8.5417",
                        ),
                        locations=[
                            ("Commercial Floor", "helix-commercial-floor"),
                            ("Meeting Suites", "helix-meeting-suites"),
                        ],
                    ),
                ],
            ),
            dict(
                kind="customer",
                industry="Banking",
                group=(
                    "Meridian Capital Group",
                    "meridian-bank",
                    "Cross-border banking group: German retail bank (AG), UK capital-markets arm "
                    "(plc) and a shared risk-services entity.",
                ),
                domain="meridianbank.com",
                tenants=[
                    dict(
                        name="Meridian Bank AG",
                        slug="meridian-retail",
                        code="MER-RT",
                        profile="bank_retail",
                        headcount=26,
                        currency="EUR",
                        legal_seat="Frankfurt am Main, Germany",
                        reg_no="HRB 88245 (Amtsgericht Frankfurt am Main)",
                        site=(
                            "Meridian Frankfurt Tower",
                            "meridian-frankfurt",
                            "Frankfurt",
                            "Taunusanlage 12\n60325 Frankfurt\nGermany",
                            "dach",
                            "corporate-offices",
                            "50.1109",
                            "8.6700",
                        ),
                        locations=[
                            ("Retail Floor 2", "mer-retail-f2"),
                            ("Retail Floor 3", "mer-retail-f3"),
                            ("Branch Ops", "mer-branch-ops"),
                            ("Data Center", "mer-frankfurt-dc"),
                        ],
                    ),
                    dict(
                        name="Meridian Capital Markets plc",
                        slug="meridian-investment",
                        code="MER-IB",
                        profile="bank_invest",
                        headcount=16,
                        currency="GBP",
                        legal_seat="London, United Kingdom",
                        reg_no="Companies House 09183477",
                        site=(
                            "Meridian London Office",
                            "meridian-london",
                            "London",
                            "30 St Mary Axe\nLondon EC3A 8BF\nUnited Kingdom",
                            "western-europe",
                            "corporate-offices",
                            "51.5145",
                            "-0.0803",
                        ),
                        locations=[("Trading Floor", "mer-trading-floor"), ("Deal Rooms", "mer-deal-rooms")],
                    ),
                    dict(
                        name="Meridian Risk Services GmbH",
                        slug="meridian-risk",
                        code="MER-RC",
                        profile="bank_risk",
                        headcount=10,
                        currency="EUR",
                        legal_seat="Frankfurt am Main, Germany",
                        reg_no="HRB 90112 (Amtsgericht Frankfurt am Main)",
                        site=(
                            "Meridian Frankfurt Tower",
                            "meridian-frankfurt",
                            "Frankfurt",
                            "",
                            "dach",
                            "corporate-offices",
                            None,
                            None,
                        ),
                        locations=[("Risk Analytics", "mer-risk-analytics")],
                    ),
                ],
            ),
            dict(
                kind="customer",
                industry="Asset Management",
                group=(
                    "Sterling Asset Management Group",
                    "sterling-am",
                    "Munich fund manager (KVG) with a dedicated fund-services entity.",
                ),
                domain="sterling-am.com",
                tenants=[
                    dict(
                        name="Sterling Asset Management GmbH",
                        slug="sterling-portfolio",
                        code="STG-PM",
                        profile="fund_portfolio",
                        headcount=14,
                        currency="EUR",
                        legal_seat="Munich, Germany",
                        reg_no="HRB 204417 (Amtsgericht München)",
                        site=(
                            "Sterling Munich Office",
                            "sterling-munich",
                            "Munich",
                            "Maximilianstrasse 35\n80539 Munich\nGermany",
                            "dach",
                            "corporate-offices",
                            "48.1391",
                            "11.5802",
                        ),
                        locations=[
                            ("Portfolio Desk", "stg-portfolio-desk"),
                            ("Partner Suites", "stg-partner-suites"),
                            ("Server Closet", "stg-server-closet"),
                        ],
                    ),
                    dict(
                        name="Sterling Fund Services GmbH",
                        slug="sterling-ops",
                        code="STG-OP",
                        profile="fund_ops",
                        headcount=8,
                        currency="EUR",
                        legal_seat="Munich, Germany",
                        reg_no="HRB 204902 (Amtsgericht München)",
                        site=(
                            "Sterling Munich Office",
                            "sterling-munich",
                            "Munich",
                            "",
                            "dach",
                            "corporate-offices",
                            None,
                            None,
                        ),
                        locations=[("Fund Operations", "stg-fund-ops")],
                    ),
                ],
            ),
            dict(
                kind="customer",
                industry="Legal Services",
                group=None,
                domain="brightwell-legal.com",
                tenants=[
                    dict(
                        name="Brightwell Legal PartG mbB",
                        slug="brightwell-legal",
                        code="BWL",
                        profile="legal",
                        headcount=16,
                        currency="EUR",
                        legal_seat="Berlin, Germany",
                        reg_no="PR 1284 B (Partnerschaftsregister Berlin)",
                        site=(
                            "Brightwell Berlin Chambers",
                            "brightwell-berlin",
                            "Berlin",
                            "Kurfurstendamm 21\n10719 Berlin\nGermany",
                            "dach",
                            "corporate-offices",
                            "52.5030",
                            "13.3270",
                        ),
                        locations=[
                            ("Partner Offices", "bwl-partner-offices"),
                            ("Associate Bullpen", "bwl-associates"),
                            ("Records Room", "bwl-records"),
                        ],
                    ),
                ],
            ),
            dict(
                kind="customer",
                industry="Architecture & Design",
                group=None,
                domain="aurora-arch.com",
                tenants=[
                    dict(
                        name="Aurora Architekten GmbH",
                        slug="aurora-architects",
                        code="AUR",
                        profile="architecture",
                        headcount=11,
                        currency="EUR",
                        legal_seat="Hamburg, Germany",
                        reg_no="HRB 167733 (Amtsgericht Hamburg)",
                        site=(
                            "Aurora Hamburg Studio",
                            "aurora-hamburg",
                            "Hamburg",
                            "Am Kaiserkai 10\n20457 Hamburg\nGermany",
                            "dach",
                            "corporate-offices",
                            "53.5413",
                            "9.9920",
                        ),
                        locations=[("Design Studio", "aur-design-studio"), ("Render Farm", "aur-render-farm")],
                    ),
                ],
            ),
            dict(
                kind="customer",
                industry="Logistics",
                group=None,
                domain="vantage-logistics.com",
                tenants=[
                    dict(
                        name="Vantage Logistics GmbH & Co. KG",
                        slug="vantage-logistics",
                        code="VAN",
                        profile="logistics",
                        headcount=18,
                        currency="EUR",
                        legal_seat="Frankfurt am Main, Germany",
                        reg_no="HRA 49882 (Amtsgericht Frankfurt am Main)",
                        site=(
                            "Vantage Frankfurt Airport Depot",
                            "vantage-fra",
                            "Frankfurt",
                            "CargoCity Sued, Gebaude 535\n60549 Frankfurt\nGermany",
                            "dach",
                            "field-depots",
                            "50.0490",
                            "8.5870",
                        ),
                        locations=[
                            ("Warehouse Floor", "van-warehouse"),
                            ("Dispatch Office", "van-dispatch"),
                            ("Network Cabinet", "van-network"),
                        ],
                    ),
                ],
            ),
        ]

    def _seed_organizations(self):
        # inline imports: app-registry: assets.models and organization.models load after Django setup.
        from assets.models import Supplier
        from organization.models import (
            Contact,
            ContactAssignment,
            ContactRole,
            Location,
            Region,
            Site,
            SiteGroup,
            Tenant,
            TenantGroup,
        )

        self.stdout.write("--- Organizations ---")

        # Regions
        self._regions = {}
        eu, _ = Region.objects.get_or_create(slug="europe", defaults={"name": "Europe"})
        self._regions["europe"] = eu
        for name, slug in [("DACH", "dach"), ("Western Europe", "western-europe"), ("Nordics", "nordics")]:
            obj, _ = Region.objects.get_or_create(slug=slug, defaults={"name": name, "parent": eu})
            self._regions[slug] = obj

        # Site groups
        self._sitegroups = {}
        for name, slug in [
            ("Corporate Offices", "corporate-offices"),
            ("Datacenters", "datacenters"),
            ("Labs & Plants", "labs-plants"),
            ("Field & Depots", "field-depots"),
        ]:
            obj, _ = SiteGroup.objects.get_or_create(slug=slug, defaults={"name": name})
            self._sitegroups[slug] = obj

        self._tgroups = {}
        self._tenants = {}
        self._tenant_meta = {}  # slug -> dict(profile, domain, code, group_slug, industry, kind)
        self._sites = {}
        self._locations = {}  # slug -> Location
        self._tenant_locations = {}  # tenant slug -> [Location]
        self._tenant_holders = {}  # tenant slug -> [AssetHolder]
        self._orgs = self._org_spec()

        for org in self._orgs:
            group_obj = None
            if org["group"]:
                gname, gslug, gdesc = org["group"]
                group_obj, _ = TenantGroup.objects.get_or_create(
                    slug=gslug, defaults={"name": gname, "description": gdesc}
                )
                self._tgroups[gslug] = group_obj

            for t in org["tenants"]:
                # Per-entity depreciation default: German (EUR) entities default to the German
                # AfA 36-month policy so the "tenant-level default" resolution rung is visible;
                # foreign-currency entities fall back to a generic straight-line schedule.
                currency = t.get("currency", "EUR")
                default_dep = (
                    self._demo_depreciation_afa
                    if currency == "EUR"
                    else self._depreciations.get("3-Year Straight-Line")
                )
                industry = org.get("industry", "Managed Service Provider")
                description = (
                    f"{t['name']}: {industry} entity of "
                    f"{(org['group'][0] if org['group'] else t['name'])}.\n"
                    f"Registered seat: {t['legal_seat']} · {t['reg_no']} · functional currency {currency}.\n"
                    f"IT managed by Northwind Managed Services GmbH."
                )
                tenant, _ = Tenant.objects.get_or_create(
                    slug=t["slug"],
                    defaults={
                        "name": t["name"],
                        "group": group_obj,
                        "currency": currency,
                        "default_depreciation": default_dep,
                        "description": description,
                    },
                )
                self._tenants[t["slug"]] = tenant
                self._tenant_meta[t["slug"]] = dict(
                    profile=t["profile"],
                    domain=org["domain"],
                    code=t["code"],
                    group_slug=org["group"][1] if org["group"] else None,
                    industry=org.get("industry"),
                    kind=org["kind"],
                    currency=currency,
                    legal_seat=t["legal_seat"],
                )
                self._tenant_locations[t["slug"]] = []

                # Primary site (may be shared across tenants of the same org)
                for site_spec in [t["site"]] + t.get("extra_sites", []):
                    sname, sslug, city, addr, region_slug, sg_slug, lat, lon = site_spec
                    if sslug not in self._sites:
                        self._sites[sslug] = Site.objects.get_or_create(
                            slug=sslug,
                            defaults={
                                "name": sname,
                                "tenant": tenant,
                                "group": self._sitegroups[sg_slug],
                                "region": self._regions[region_slug],
                                "physical_address": addr,
                                "latitude": lat,
                                "longitude": lon,
                                "time_zone": "Europe/Berlin",
                            },
                        )[0]
                primary_site = self._sites[t["site"][1]]

                # Locations
                for lname, lslug in t["locations"]:
                    loc, _ = Location.objects.get_or_create(
                        slug=lslug, defaults={"name": lname, "site": primary_site, "tenant": tenant}
                    )
                    self._locations[lslug] = loc
                    self._tenant_locations[t["slug"]].append(loc)

                # Holders (employees)
                holders = self._make_holders(tenant, org["domain"], t["headcount"])
                self._tenant_holders[t["slug"]] = holders

        # MSP layer: the Northwind operating company is the managing (provider) tenant;
        # every other seeded tenant — the customers AND the MSP's own corporate holding —
        # points at it via ``managed_by``, so the MSP admin surfaces (customer-tenants
        # list, technician onboarding, managed-reach role assignments) are populated.
        # Single-company installs simply have no ``is_provider`` tenant and the whole
        # layer stays hidden. ``is_provider`` is set (and saved) BEFORE any ``managed_by``
        # points at the tenant — Tenant.clean() enforces that ordering.
        msp_tenant = self._tenants["northwind-internal-it"]
        if not msp_tenant.is_provider:
            msp_tenant.is_provider = True
            msp_tenant.save(update_fields=["is_provider"])
        self._provider_tenant = msp_tenant
        for tenant in self._tenants.values():
            if tenant.pk == msp_tenant.pk:
                continue
            if tenant.managed_by_id != msp_tenant.pk:
                tenant.managed_by = msp_tenant
                tenant.save(update_fields=["managed_by"])

        # SaaS catalogue rows are created after the intended scopes exist.
        scoped_tenant = self._tenants["northwind-internal-it"]
        scoped_group = next(iter(self._tgroups.values()))
        self._saas_suppliers = _seed_saas_suppliers(Supplier, self._saas_supplier_data, scoped_tenant, scoped_group)

        # Contacts: a primary customer contact per customer group/tenant + vendor reps
        self._contact_roles = {}
        for name, slug in [
            ("Account Manager", "account-manager"),
            ("Customer Primary Contact", "customer-primary"),
            ("Vendor Support", "vendor-support"),
        ]:
            self._contact_roles[slug] = ContactRole.objects.get_or_create(slug=slug, defaults={"name": name})[0]

        self._contacts = []
        for org in self._orgs:
            if org["kind"] != "customer":
                continue
            label = org["group"][0] if org["group"] else org["tenants"][0]["name"]
            c = Contact.objects.create(
                name=f"{random.choice(self.FIRST_NAMES)} {random.choice(self.LAST_NAMES)}",
                title=f"IT Manager, {label}",
                phone="+49-69-555-0%03d" % random.randint(100, 999),
                email=f"it.manager@{org['domain']}",
                web_url=f"https://{org['domain']}",
            )
            self._contacts.append(c)
            # Assign as primary contact on each tenant of the customer
            for t in org["tenants"]:
                tenant = self._tenants[t["slug"]]
                ContactAssignment.objects.get_or_create(
                    contact=c,
                    role=self._contact_roles["customer-primary"],
                    content_type=ContentType.objects.get_for_model(tenant),
                    object_id=tenant.pk,
                    defaults={"priority": "primary"},
                )

        # Vendor-support reps attached to the hardware manufacturers (exercises
        # Manufacturer.contacts + the vendor-support role).
        vendor_contacts = {
            "dell-technologies": ("Dell ProSupport Plus Desk", "support.de@dell.com", "+49-69-9792-0000"),
            "apple-inc": ("Apple Business Care", "business.eu@apple.com", "+49-800-2000-136"),
            "hp-inc": ("HP Care Pack Support", "enterprise.support@hp.com", "+49-7031-986-0"),
            "lenovo-group": ("Lenovo Premier Support", "premier.eu@lenovo.com", "+49-711-6517-0"),
            "cisco-systems": ("Cisco TAC EMEA", "tac@cisco.com", "+31-20-357-1000"),
            "samsung-electronics": ("Samsung B2B Support", "b2b.support@samsung.com", "+49-6196-77-55555"),
            "microsoft-corporation": ("Microsoft Premier Support", "premier@microsoft.com", "+49-89-31760-0"),
            "logitech-international": ("Logitech B2B Care", "b2bsupport@logitech.com", "+41-21-863-5111"),
            "brother-industries": ("Brother Business Support", "support@brother.de", "+49-6172-863-0"),
            "synology-inc": ("Synology Technical Support", "support@synology.com", "+49-89-3838-2700"),
            "ubiquiti-inc": ("Ubiquiti Enterprise Support", "support@ui.com", "+1-844-674-4357"),
        }
        mfr_ct = ContentType.objects.get_for_model(next(iter(self._manufacturers.values())))
        for mfr_slug, (cname, cemail, cphone) in vendor_contacts.items():
            mfr = self._manufacturers.get(mfr_slug)
            if not mfr:
                continue
            vc = Contact.objects.create(name=cname, title="Vendor Support", email=cemail, phone=cphone)
            self._contacts.append(vc)
            ContactAssignment.objects.get_or_create(
                contact=vc,
                role=self._contact_roles["vendor-support"],
                content_type=mfr_ct,
                object_id=mfr.pk,
                defaults={"priority": "primary"},
            )

        # MSP account managers as contacts on the MSP operating entity.
        msp_tenant = self._tenants["northwind-internal-it"]
        msp_ct = ContentType.objects.get_for_model(msp_tenant)
        for am_name, am_email in [
            ("Nadia Haas", "nadia.haas@northwind-it.com"),
            ("Peter Voss", "peter.voss@northwind-it.com"),
        ]:
            ac = Contact.objects.create(
                name=am_name,
                title="Account Manager, Northwind Managed Services",
                email=am_email,
                phone="+49-30-555-0%03d" % random.randint(100, 999),
            )
            self._contacts.append(ac)
            ContactAssignment.objects.get_or_create(
                contact=ac,
                role=self._contact_roles["account-manager"],
                content_type=msp_ct,
                object_id=msp_tenant.pk,
                defaults={"priority": "secondary"},
            )

        total_holders = sum(len(v) for v in self._tenant_holders.values())
        self.stdout.write(
            f"  {len(self._tgroups)} tenant groups, {len(self._tenants)} tenants, "
            f"{len(self._sites)} sites, {len(self._locations)} locations, {total_holders} asset holders."
        )

    def _make_holders(self, tenant, domain, n):

        holders = []
        used = set()
        for _ in range(n):
            for _attempt in range(20):
                first = random.choice(self.FIRST_NAMES)
                last = random.choice(self.LAST_NAMES)
                upn = f"{first}.{last}@{domain}".lower()
                if upn not in used:
                    break
            if upn in used:
                upn = f"{first}.{last}{random.randint(1, 99)}@{domain}".lower()
            used.add(upn)
            holder = AssetHolder.objects.create(first_name=first, last_name=last, upn=upn, email=upn, tenant=tenant)
            holders.append(holder)
        return holders
