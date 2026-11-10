"""Query budgets for the asset list and the HTML asset detail page (#882).

The fixture links every asset the way production data does (assignment, tags,
custom field, contract, license seat, installed software, maintenance,
warranty, journal entry), so a per-row relation that is not loaded in bulk
shows up as a growing query count. Each budget is asserted with 3 and with 30
assets: the counts must not depend on the number of assets.

The global search renders the first ten hits of every matching model through
the model's list table; its budget is asserted with 1 and with 10 matching
assets.
"""

import datetime
import re
from collections import Counter

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from assets.choices import StatusTypeChoices
from assets.models import Asset, AssetMaintenance, AssetRole, AssetType, Manufacturer, StatusLabel, Warranty
from assets.services import checkout_asset
from core.tests.mixins import TenantTestMixin, grant
from extras.models import CustomField, JournalEntry, Tag
from licenses.models import License, LicenseSeatAssignment
from organization.models import AssetHolder, Contact, ContactAssignment, ContactRole, Location, Site, Tenant
from procurement.models import Contract
from software.models import InstalledSoftware, Software

PERMISSIONS = [
    "assets.view_asset",
    "assets.change_asset",
    "assets.add_asset",
    "assets.delete_asset",
    "compliance.view_custodyreceipt",
    "licenses.view_licenseseatassignment",
    "software.view_installedsoftware",
]

# Measured on the realistic fixture below for a role-granted (non-superuser)
# member. Before #882 the same pages cost 33 (list) and 93 (detail) queries.
LIST_QUERY_BUDGET = 32
DETAIL_QUERY_BUDGET = 73
# Global search for one asset term, rendered by the same role-granted member.
# Before #882 it cost 56 queries with one matching asset and 156 with ten.
SEARCH_QUERY_BUDGET = 48


class _LinkedAssetFixture(TenantTestMixin, TestCase):
    """Assets linked the way production data is, plus a request-level counter."""

    def setUp(self):
        self.setup_tenant_context(name="Query Budget", slug="query-budget", permissions=PERMISSIONS)
        self.set_active_tenant(self.tenant, self.tenant_membership)
        manufacturer = Manufacturer.objects.create(name="QB Mfr", slug="qb-mfr")
        self.asset_type = AssetType.objects.create(manufacturer=manufacturer, model="QB Model", slug="qb-model")
        self.role = AssetRole.objects.create(name="QB Role", slug="qb-role")
        self.status = StatusLabel.objects.create(name="QB Deployable", slug="qb-deployable", type="deployable")
        self.deployed_status = StatusLabel.objects.create(
            name="Deployed", slug="deployed", type=StatusTypeChoices.DEPLOYED
        )
        site = Site.objects.create(name="QB Site", slug="qb-site")
        self.location = Location.objects.create(name="QB Loc", slug="qb-loc", tenant=self.tenant, site=site)
        self.tags = [Tag.objects.create(name=f"QB Tag {i}", slug=f"qb-tag-{i}") for i in range(3)]
        self.custom_field = CustomField.objects.create(
            name="qb_hostname", label="Hostname", field_type="text", activation=CustomField.ACTIVATION_GLOBAL
        )
        self.custom_field.object_types.add(ContentType.objects.get_for_model(Asset))
        software = Software.objects.create(name="QB Software", manufacturer=manufacturer, tenant=self.tenant)
        self.software = software
        self.license = License.objects.create(name="QB License", software=software, tenant=self.tenant, seats=100)
        self.contract = Contract.objects.create(
            tenant=self.tenant,
            name="QB Contract",
            contract_number="QB-C-1",
            contract_type="support",
            status="active",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )
        support_role = ContactRole.objects.create(name="Support", slug="support")
        other_role = ContactRole.objects.create(name="Sales", slug="sales")
        primary = Contact.objects.create(name="QB Primary Contact")
        self.support_contact = Contact.objects.create(name="QB Support Contact")
        manufacturer_ct = ContentType.objects.get_for_model(manufacturer)
        ContactAssignment.objects.create(
            contact=primary,
            role=other_role,
            content_type=manufacturer_ct,
            object_id=manufacturer.pk,
            priority="primary",
        )
        ContactAssignment.objects.create(
            contact=self.support_contact, role=support_role, content_type=manufacturer_ct, object_id=manufacturer.pk
        )
        self.assets = []
        self.client_login_to_tenant(self.tenant_user, self.tenant)

    def _add_assets(self, total):
        asset_ct = ContentType.objects.get_for_model(Asset)
        for index in range(len(self.assets), total):
            asset = Asset.objects.create(
                name=f"QB Asset {index:02d}",
                asset_tag=f"QB-{index:02d}",
                asset_type=self.asset_type,
                asset_role=self.role,
                status=self.status,
                tenant=self.tenant,
                location=self.location,
                custom_field_data={self.custom_field.name: f"host-{index}"},
            )
            asset.tags.set(self.tags)
            self.contract.assets.add(asset)
            holder = AssetHolder.objects.create(
                first_name="QB", last_name=f"Holder {index}", upn=f"qb-{index}@example.test", tenant=self.tenant
            )
            if index % 2 == 0:
                checkout_asset(asset, holder=holder, user=self.tenant_admin)
            LicenseSeatAssignment.objects.create(license=self.license, asset=asset)
            InstalledSoftware.objects.create(asset=asset, software=self.software)
            AssetMaintenance.objects.create(asset=asset, start_date=datetime.date.today())
            Warranty.objects.create(
                asset=asset,
                start_date=datetime.date.today(),
                end_date=datetime.date.today() + datetime.timedelta(days=30),
            )
            JournalEntry.objects.create(comment="note", user=self.tenant_admin, model=asset_ct, object_id=asset.pk)
            self.assets.append(asset)

    def _count(self, url):
        # Warm per-process caches (content types, translations) so the budget
        # measures the page, not which test ran first.
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)


class AssetPageQueryCountTests(_LinkedAssetFixture):
    def _measure(self):
        return {
            "list": self._count(reverse("assets:asset_list")),
            "filtered": self._count(reverse("assets:asset_list") + "?q=QB"),
            "detail": self._count(reverse("assets:asset_detail", kwargs={"pk": self.assets[0].pk})),
        }

    def test_page_query_counts_stay_within_budget_and_do_not_grow_with_assets(self):
        self._add_assets(3)
        small = self._measure()
        self._add_assets(30)
        large = self._measure()

        for size, counts in (("3 assets", small), ("30 assets", large)):
            with self.subTest(size=size):
                self.assertLessEqual(counts["list"], LIST_QUERY_BUDGET)
                self.assertLessEqual(counts["filtered"], LIST_QUERY_BUDGET)
                self.assertLessEqual(counts["detail"], DETAIL_QUERY_BUDGET)
        self.assertEqual(small, large)


class AssetDetailPreloadedContextTests(TenantTestMixin, TestCase):
    """The values the detail view now computes once keep the page's meaning."""

    def setUp(self):
        self.setup_tenant_context(name="Detail Ctx", slug="detail-ctx", permissions=PERMISSIONS)
        self.set_active_tenant(self.tenant, self.tenant_membership)
        self.manufacturer = Manufacturer.objects.create(name="Ctx Mfr", slug="ctx-mfr")
        asset_type = AssetType.objects.create(manufacturer=self.manufacturer, model="Ctx Model", slug="ctx-model")
        self.role = AssetRole.objects.create(name="Ctx Role", slug="ctx-role", allows_components=True)
        status = StatusLabel.objects.create(name="Ctx Deployable", slug="ctx-deployable", type="deployable")
        self.asset = Asset.objects.create(
            name="Ctx Asset",
            asset_tag="CTX-1",
            asset_type=asset_type,
            asset_role=self.role,
            status=status,
            tenant=self.tenant,
        )
        self.client_login_to_tenant(self.tenant_user, self.tenant)
        self.url = reverse("assets:asset_detail", kwargs={"pk": self.asset.pk})

    def _assign_contact(self, name, role_slug, priority=""):
        role, _created = ContactRole.objects.get_or_create(slug=role_slug, defaults={"name": role_slug.title()})
        contact = Contact.objects.create(name=name)
        ContactAssignment.objects.create(
            contact=contact,
            role=role,
            content_type=ContentType.objects.get_for_model(self.manufacturer),
            object_id=self.manufacturer.pk,
            priority=priority,
        )
        return contact

    def test_support_contact_follows_the_model_precedence(self):
        self._assign_contact("Ctx Primary", "sales", priority="primary")
        support = self._assign_contact("Ctx Tech Support", "technical-support")

        response = self.client.get(self.url)

        self.assertEqual(response.context["support_contact"], support)
        self.assertEqual(response.context["support_contact"], self.manufacturer.get_support_contact)
        self.assertContains(response, "Ctx Tech Support")

    def test_support_contact_falls_back_to_primary_then_any_contact(self):
        other = self._assign_contact("Ctx Other", "sales")
        self.assertEqual(self.client.get(self.url).context["support_contact"], other)

        primary = self._assign_contact("Ctx Primary", "billing", priority="primary")
        self.assertEqual(self.client.get(self.url).context["support_contact"], primary)

    def test_support_contact_is_empty_without_assignments(self):
        response = self.client.get(self.url)
        self.assertIsNone(response.context["support_contact"])

    def test_tab_counts_match_the_tab_tables(self):
        software = Software.objects.create(name="Ctx SW", manufacturer=self.manufacturer, tenant=self.tenant)
        InstalledSoftware.objects.create(asset=self.asset, software=software)
        InstalledSoftware.objects.create(
            asset=self.asset,
            software=Software.objects.create(name="Ctx SW 2", manufacturer=self.manufacturer, tenant=self.tenant),
        )
        license_ = License.objects.create(name="Ctx Lic", software=software, tenant=self.tenant, seats=5)
        LicenseSeatAssignment.objects.create(license=license_, asset=self.asset)
        Warranty.objects.create(
            asset=self.asset, start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(5)
        )

        context = self.client.get(self.url).context

        self.assertEqual(
            context["tab_counts"],
            {"software": 2, "requests": 0, "audits": 0, "licenses": 1, "warranties": 1, "reservations": 0},
        )
        self.assertEqual(context["current_warranty_end"], self.asset.current_warranty_end)

    def test_modular_flag_follows_role_and_allocations(self):
        self.assertTrue(self.client.get(self.url).context["is_modular"])

        self.role.allows_components = False
        self.role.save()
        self.assertFalse(self.client.get(self.url).context["is_modular"])
        self.assertFalse(Asset.objects.get(pk=self.asset.pk).is_modular)


class GlobalSearchQueryCountTests(_LinkedAssetFixture):
    """The search result table loads its rows' relations in bulk (#882)."""

    def _search_queries(self):
        url = reverse("search") + "?q=QB+Asset"
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return [query["sql"] for query in ctx.captured_queries]

    @staticmethod
    def _shape(sql):
        # Collapse literal values so per-row repeats of one statement count together.
        return re.sub(r"IN \([^)]*\)|\b\d+\b|'[^']*'", "?", sql)

    def test_search_query_count_does_not_grow_with_matching_assets(self):
        self._add_assets(1)
        one = self._search_queries()
        self._add_assets(10)
        ten = self._search_queries()

        repeated = Counter(map(self._shape, ten)) - Counter(map(self._shape, one))
        self.assertEqual(
            len(one),
            len(ten),
            f"search queries grew with the hits ({len(one)} -> {len(ten)}): {repeated.most_common(3)}",
        )
        self.assertLessEqual(len(ten), SEARCH_QUERY_BUDGET)

    def test_search_table_keeps_the_rendered_row_values(self):
        self._add_assets(2)
        response = self.client.get(reverse("search") + "?q=QB+Asset")

        self.assertEqual(response.status_code, 200)
        table = response.context["results"][Asset]["table"]
        rows = {row.record.asset_tag: row for row in table.rows}
        self.assertEqual(set(rows), {"QB-00", "QB-01"})
        # QB-00 is checked out (even index), QB-01 is not: the prefetched active
        # assignment must keep that difference in the actions cell.
        self.assertIn("Check in", rows["QB-00"].get_cell("actions"))
        self.assertIn("Check out", rows["QB-01"].get_cell("actions"))
        self.assertIn("QB Tag 0", rows["QB-00"].get_cell("tags"))
        self.assertIn("QB Holder 0", rows["QB-00"].get_cell("assignee"))
        self.assertIn("QB Mfr", str(rows["QB-01"].get_cell("manufacturer")))


class AllAccessibleListQueryCountTests(_LinkedAssetFixture):
    """The all-accessible scope must not repeat its resolution per rendered row.

    The tenant and tenant-group paths are pinned above. The all-accessible
    scope resolves every accessible tenant, so any per-row resolution turns a
    large page into a multi-second one: the demo installation measured ~6 ms
    per row at 1000 rows across tenants while the same 835 rows cost a flat
    second inside a single tenant.
    """

    def _add_assets_for_tenant(self, tenant, start, total):
        for index in range(start, start + total):
            Asset.objects.create(
                name=f"QB Alt {index:02d}",
                asset_tag=f"QB-ALT-{index:02d}",
                asset_type=self.asset_type,
                asset_role=self.role,
                status=self.status,
                tenant=tenant,
            )

    def _measure_all_accessible(self):
        url = reverse("assets:asset_list") + "?switch_all_accessible=1"
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_all_accessible_list_query_count_does_not_grow_with_tenant_count(self):
        self._add_assets(6)
        one_tenant = self._measure_all_accessible()

        for index in range(3):
            extra = Tenant.objects.create(name=f"QB Extra {index}", slug=f"qb-extra-{index}")
            grant(self.tenant_user, extra, self.tenant_role)
            self._add_assets_for_tenant(extra, index * 2, 2)
        self.tenant_user.refresh_from_db()

        four_tenants = self._measure_all_accessible()
        self.assertEqual(
            one_tenant,
            four_tenants,
            f"all-accessible list used {four_tenants} queries across 4 tenants against "
            f"{one_tenant} for 1: it must not scale with the accessible tenant count",
        )
