"""Aggregate-scope regression: a global Manufacturer must not re-expose software
from a tenant where the caller holds no ``software.view_software`` (#1088).

The asset read scope is resolved with ``assets.view_asset``; a caller may hold
that in tenants A and B while holding the software permission only in A. Both
software prefetch paths (asset root and asset-type root) must filter the
prefetched products by the software permission itself.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetType, Manufacturer, StatusLabel
from core.tests.mixins import grant
from organization.models import Role, Tenant
from software.models import Software

User = get_user_model()


class AggregateScopeSoftwareProductsTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="SwAgg A", slug="swagg-a")
        self.tenant_b = Tenant.objects.create(name="SwAgg B", slug="swagg-b")
        self.user = User.objects.create_user("swagg_user", "swagg@example.com", "pw")

        role_a = Role.objects.create(
            tenant=self.tenant_a,
            name="SwAgg Role A",
            permissions=["assets.view_asset", "assets.view_assettype", "software.view_software"],
        )
        grant(self.user, self.tenant_a, role_a)
        role_b = Role.objects.create(
            tenant=self.tenant_b,
            name="SwAgg Role B",
            permissions=["assets.view_asset", "assets.view_assettype"],
        )
        grant(self.user, self.tenant_b, role_b)

        manufacturer = Manufacturer.objects.create(name="SwAgg Mfr", slug="swagg-mfr")
        Software.objects.create(name="SwAgg A Tool", manufacturer=manufacturer, tenant=self.tenant_a)
        Software.objects.create(name="SwAgg B Tool", manufacturer=manufacturer, tenant=self.tenant_b)

        status = StatusLabel.objects.create(name="SwAgg Ready", slug="swagg-ready", type=StatusLabel.TYPE_DEPLOYABLE)
        asset_type = AssetType.objects.create(manufacturer=manufacturer, model="SwAgg Model", slug="swagg-m")
        for tenant, tag in ((self.tenant_a, "A"), (self.tenant_b, "B")):
            Asset.objects.create(
                name=f"SwAgg Asset {tag}",
                asset_tag=f"SWAGG-{tag}",
                asset_type=asset_type,
                status=status,
                tenant=tenant,
            )

        self.client.force_login(self.user)
        session = self.client.session
        session.pop("active_tenant_id", None)
        session.pop("active_tenant_group_id", None)
        session["active_all_accessible"] = True
        session.save()

    def _names(self, query):
        response = self.client.post(
            reverse("graphql"),
            data=json.dumps({"query": query}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        body = response.json()
        self.assertNotIn("errors", body)
        data = body["data"]
        payload = data["assets"] if "assets" in data else data["assetTypes"]
        rows = payload["edges"] if isinstance(payload, dict) and "edges" in payload else payload
        names = set()
        for row in rows:
            node = row["node"] if isinstance(row, dict) and "node" in row else row
            manufacturer = node["assetType"]["manufacturer"] if "assetType" in node else node["manufacturer"]
            names.update(item["name"] for item in manufacturer["softwareProducts"])
        return names

    def test_asset_root_prefetch_filters_by_software_permission(self):
        names = self._names("{ assets { assetType { manufacturer { softwareProducts { name } } } } }")
        self.assertEqual(names, {"SwAgg A Tool"})

    def test_asset_type_root_prefetch_filters_by_software_permission(self):
        names = self._names("{ assetTypes { edges { node { manufacturer { softwareProducts { name } } } } } }")
        self.assertEqual(names, {"SwAgg A Tool"})
