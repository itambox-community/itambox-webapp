"""
Seed the database with a coherent, presentation-ready demo dataset.

The narrative is a **Managed Service Provider (MSP)** — "Northwind Managed
Services" — that runs its own internal IT and manages IT for a handful of
customers across regulated industries (pharma, banking, asset management, plus
a few smaller single-tenant clients). MSP staff hold one membership at the
managing (``is_provider``) tenant with scoped role grants into the customer
tenants, which is what makes the multi-tenant story tangible.

Every app is touched with realistic, headcount-driven daily data: organization
hierarchy, access (users/roles/memberships), assets + assignments + custody,
inventory stock, licensing, subscriptions, maintenance, procurement, and the
operational layer (alerts, reports, event rules, config contexts, dashboards).

Usage:
    python manage.py seed_data                  # full demo dataset (clears first)
    python manage.py seed_data --skip-drop      # add without clearing
    python manage.py seed_data --production      # minimal essential data only
"""

import datetime
import os
import random
import secrets as _secrets

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import CommandError
from django.db import transaction

from core.management.commands._seed.access import SeedAccessMixin, check_seed_access_invariants
from core.management.commands._seed.assets import SeedAssetsMixin
from core.management.commands._seed.catalog import SeedCatalogMixin
from core.management.commands._seed.compliance import SeedComplianceMixin
from core.management.commands._seed.consistency import check_seed_operational_invariants
from core.management.commands._seed.engine import ChangeLogEngine
from core.management.commands._seed.finance import SeedFinanceMixin
from core.management.commands._seed.history import SeedHistoryMixin
from core.management.commands._seed.inventory import SeedInventoryStockMixin, check_seed_inventory_invariants
from core.management.commands._seed.licensing import SeedLicensingMixin
from core.management.commands._seed.lifecycle import SeedLifecycleMixin
from core.management.commands._seed.maintenance import SeedMaintenanceMixin
from core.management.commands._seed.operations import SeedOperationsMixin
from core.management.commands._seed.organizations import SeedOrganizationsMixin
from core.management.commands._seed.procurement import SeedProcurementMixin
from core.management.commands._seed.subscriptions import SeedSubscriptionsMixin
from core.tasks.management import SystemTaskCommand

User = get_user_model()

TODAY = datetime.date.today()


def days_ago(n):
    return TODAY - datetime.timedelta(days=n)


def days_ahead(n):
    return TODAY + datetime.timedelta(days=n)


class Command(
    SeedCatalogMixin,
    SeedOrganizationsMixin,
    SeedAccessMixin,
    SeedAssetsMixin,
    SeedInventoryStockMixin,
    SeedLicensingMixin,
    SeedSubscriptionsMixin,
    SeedMaintenanceMixin,
    SeedProcurementMixin,
    SeedOperationsMixin,
    SeedFinanceMixin,
    SeedLifecycleMixin,
    SeedComplianceMixin,
    SeedHistoryMixin,
    SystemTaskCommand,
):
    help = "Seed the database with a presentation-ready MSP demo dataset."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-drop", action="store_true", default=False, help="Add data without clearing existing records."
        )
        parser.add_argument(
            "--production",
            action="store_true",
            default=False,
            help="Only create minimal essential data (admin user, status labels).",
        )
        parser.add_argument(
            "--force", action="store_true", default=False, help="Required to clear data or run with DEBUG disabled."
        )

    def handle(self, *args, **options):

        random.seed(42)  # reproducible dataset

        will_clear = not options["skip_drop"]

        # This command is destructive: unless --skip-drop is given it TRUNCATEs every
        # domain table via the unfiltered manager. Refuse to do that against a non-DEBUG
        # (production) database unless the operator explicitly passes --force.
        if will_clear and not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to clear data with DEBUG disabled. This deletes ALL domain "
                "records. Re-run with --skip-drop to only add data, or --force if you "
                "really intend to wipe a production database."
            )

        if will_clear:
            self._clear_all_data()

        if options["production"]:
            self._seed_minimal()
            check_seed_access_invariants(())
        else:
            self._seed_all()

        self.stdout.write(self.style.SUCCESS("\nDatabase seeding complete."))

    # ─────────────────────────────────────────────────────────────────
    # Clear
    # ─────────────────────────────────────────────────────────────────

    def _clear_all_data(self):
        self.stdout.write("Clearing all existing data...")

        models_to_clear = [
            ("extras", "AlertLog"),
            ("extras", "AlertRule"),
            ("extras", "ScheduledReport"),
            ("extras", "ReportTemplate"),
            ("extras", "NotificationChannel"),
            ("extras", "EventRule"),
            ("extras", "WebhookEndpoint"),
            ("extras", "LabelTemplate"),
            ("extras", "ExportTemplate"),
            ("extras", "JournalEntry"),
            ("core", "Notification"),
            ("core", "ObjectChange"),
            ("extras", "Dashboard"),
            ("procurement", "FulfillmentLink"),
            ("procurement", "PurchaseOrderLine"),
            ("procurement", "PurchaseOrder"),
            ("procurement", "Contract"),
            ("assets", "AssetRequest"),
            ("compliance", "AssetAudit"),
            ("compliance", "AuditSession"),
            ("compliance", "CustodyReceipt"),
            ("compliance", "CustodyTemplate"),
            ("assets", "AssetMaintenance"),
            ("licenses", "LicenseSeatAssignment"),
            ("subscriptions", "SubscriptionAssignment"),
            ("inventory", "AccessoryAssignment"),
            ("inventory", "ConsumableAssignment"),
            ("inventory", "ComponentAllocation"),
            ("inventory", "ComponentStock"),
            ("inventory", "AccessoryStock"),
            ("inventory", "ConsumableStock"),
            ("inventory", "KitItem"),
            ("inventory", "Kit"),
            ("software", "InstalledSoftware"),
            ("assets", "AssetAssignment"),
            ("assets", "Warranty"),
            ("assets", "AssetReservation"),
            ("assets", "AssetDisposal"),
            ("assets", "Asset"),
            ("assets", "AssetType"),
            ("inventory", "Component"),
            ("inventory", "Accessory"),
            ("inventory", "Consumable"),
            ("licenses", "License"),
            ("software", "Software"),
            ("subscriptions", "Subscription"),
            ("organization", "RoleGrantScope"),
            ("organization", "RoleGrant"),
            ("organization", "Membership"),
            ("organization", "Role"),
            ("organization", "ContactAssignment"),
            ("organization", "Contact"),
            ("organization", "ContactRole"),
            ("organization", "Location"),
            ("organization", "Site"),
            ("organization", "AssetHolder"),
            ("organization", "CostCenter"),
            ("organization", "Tenant"),
            ("organization", "TenantGroup"),
            ("organization", "Region"),
            ("organization", "SiteGroup"),
            ("assets", "AssetRole"),
            ("assets", "StatusLabel"),
            ("assets", "Manufacturer"),
            ("assets", "Supplier"),
            ("assets", "Category"),
            ("extras", "CustomFieldset"),
            ("extras", "CustomField"),
            ("assets", "Depreciation"),
            ("extras", "Tag"),
        ]
        # Clear every domain table in a single TRUNCATE ... CASCADE. This is
        # reliable and quiet regardless of FK ordering: CASCADE also truncates any
        # table that references the listed ones — dependent rows (assignments,
        # change log, m2m through tables, API tokens) and tables left behind by
        # any table that references them.
        # The auth user table is NOT in this set and has no FK into it (default
        # auth.User), so superuser accounts survive; regular users are removed
        # afterwards. A previous best-effort multi-pass delete printed dozens of
        # spurious "could not fully clear" warnings even when cascade had already
        # emptied the rows.
        from django.db import connection

        existing = set(connection.introspection.table_names())
        tables = []
        for app_label, model_name in models_to_clear:
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                continue
            db_table = model._meta.db_table
            if db_table in existing and db_table not in tables:
                tables.append(db_table)

        if tables:
            with connection.cursor() as cur:
                cur.execute("TRUNCATE TABLE %s CASCADE" % ", ".join('"%s"' % t for t in tables))
            self.stdout.write(f"  Cleared {len(tables)} domain tables.")

        User.objects.filter(is_superuser=False).delete()
        ContentType.objects.clear_cache()
        self.stdout.write("  Kept superuser accounts, deleted regular users.")

    # ─────────────────────────────────────────────────────────────────
    # Minimal Seed (--production)
    # ─────────────────────────────────────────────────────────────────

    def _seed_minimal(self):
        from assets.models import StatusLabel

        if not User.objects.filter(is_superuser=True).exists():
            # Never ship a hardcoded credential. Use DJANGO_SUPERUSER_PASSWORD when
            # provided (CI/automation), otherwise generate a strong random password
            # and print it once so the operator can capture it.
            password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
            generated = password is None
            if generated:
                password = _secrets.token_urlsafe(18)
            username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
            email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@itambox.local")
            User.objects.create_superuser(username=username, email=email, password=password)
            if generated:
                self.stdout.write(
                    self.style.WARNING(
                        f'  Created superuser "{username}" with a generated password: {password}\n'
                        f"  Store it now and change it after first login."
                    )
                )
            else:
                self.stdout.write(f'  Created superuser "{username}" (password from DJANGO_SUPERUSER_PASSWORD).')
        for name, slug, stype, color in self._status_label_defs():
            StatusLabel.objects.get_or_create(slug=slug, defaults={"name": name, "type": stype, "color": color})
        self.stdout.write("  Seeded default StatusLabels.")

    # ─────────────────────────────────────────────────────────────────
    # Full Seed
    # ─────────────────────────────────────────────────────────────────

    def _seed_all(self):
        self.stdout.write("\nSeeding MSP demo dataset...\n")
        self._engine = ChangeLogEngine(stdout=self.stdout, style=self.style)
        with transaction.atomic():
            # Existing phases run first, in their ORIGINAL order. They rely on the
            # deterministic random stream seeded by random.seed(42); inserting a
            # random-consuming phase earlier shifts the generated serials/keys and
            # triggers unique-constraint collisions. New phases are appended below.
            self._seed_catalog()
            self._seed_organizations()
            self._seed_access()
            self._seed_assets()
            self._seed_inventory_stock()
            self._seed_licensing()
            self._seed_subscriptions()
            self._seed_procurement()
            self._seed_operations()
            # New realistic-dataset phases (appended; order respects data deps).
            self._seed_cost_centers()  # tenants + engineers
            self._seed_lifecycle()  # warranties, reservations, disposals (assets)
            self._seed_contracts_and_costing()  # contracts + cost-centre backfill (licenses/subs)
            self._seed_compliance()  # audit sessions + custody receipts
            self._seed_export_templates()  # global Jinja export templates (no tenant/random)
            self._simulate_history()  # real 2-year change history (last)
            # Maintenance runs after the history simulation because out-of-service
            # records must document the repair windows that simulation produced
            # A repair on an asset that never left service is the exact
            # contradiction the seed must avoid. It consumes no random draws of its
            # own for those records, so the stream order of earlier phases is
            # unaffected.
            self._seed_maintenance()
            check_seed_access_invariants(self._users.values())
            check_seed_inventory_invariants()
            # Operational-story coherence: a received PO line has its assets, a
            # repair agrees with the assignment, maintenance belongs to the
            # asset's timeline, and an approved request can actually be claimed.
            check_seed_operational_invariants()

    # ─────────────────────────────────────────────────────────────────
    # Catalog (shared status-label defs used by both _seed_minimal and _seed_catalog)
    # ─────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────
    # Export templates (global, Jinja-rendered; no tenant, no random stream)
    # ─────────────────────────────────────────────────────────────────

    def _seed_export_templates(self):
        from django.contrib.contenttypes.models import ContentType

        from assets.models import Asset
        from extras.models import ExportTemplate
        from licenses.models import License

        asset_csv = (
            "Asset Tag,Name,Serial Number,Status\n"
            "{% for obj in queryset %}"
            "{{ obj.asset_tag|csv_safe }},{{ obj.name|csv_safe }},"
            "{{ obj.serial_number|csv_safe }},{{ obj.status|csv_safe }}\n"
            "{% endfor %}"
        )
        asset_json = (
            "[\n"
            "{% for obj in queryset %}"
            '  {"asset_tag": {{ obj.asset_tag|tojson }}, "name": {{ obj.name|tojson }}, '
            '"serial_number": {{ obj.serial_number|tojson }}, '
            '"status": {{ obj.status|string|tojson }}}{% if not loop.last %},{% endif %}\n'
            "{% endfor %}"
            "]\n"
        )
        license_csv = (
            "License,Software,Seats,Expiration\n"
            "{% for obj in queryset %}"
            "{{ obj.name|csv_safe }},{{ obj.software|csv_safe }},"
            "{{ obj.seats }},{{ obj.expiration_date|csv_safe }}\n"
            "{% endfor %}"
        )

        defs = [
            (
                Asset,
                "Assets: CSV",
                "Flat CSV of every asset (tag, name, serial, status).",
                asset_csv,
                "text/csv",
                "csv",
            ),
            (
                Asset,
                "Assets: JSON",
                "JSON array of assets for downstream tooling.",
                asset_json,
                "application/json",
                "json",
            ),
            (
                License,
                "Licenses: CSV",
                "Flat CSV of licenses with seat counts and expiry.",
                license_csv,
                "text/csv",
                "csv",
            ),
        ]
        created = 0
        for model, name, description, code, mime, ext in defs:
            ct = ContentType.objects.get_for_model(model)
            _obj, was_created = ExportTemplate.objects.get_or_create(
                content_type=ct,
                name=name,
                defaults={
                    "description": description,
                    "template_code": code,
                    "mime_type": mime,
                    "file_extension": ext,
                    "as_attachment": True,
                },
            )
            created += int(was_created)
        self.stdout.write(f"  Seeded export templates ({created} new).")

    @staticmethod
    def _status_label_defs():
        return [
            ("Available", "available", "deployable", "28a745"),
            ("In Use", "in-use", "deployed", "007bff"),
            ("Pending Repair", "pending-repair", "pending", "ffc107"),
            ("Retired", "retired", "archived", "dc3545"),
            ("In Transit", "in-transit", "pending", "6f42c1"),
            ("Decommissioned", "decommissioned", "undeployable", "6c757d"),
            ("Quarantined", "quarantined", "pending", "fd7e14"),
        ]
