"""Subscriptions seed mixin: per-organization cloud/SaaS subscriptions.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.subscriptions import SeedSubscriptionsMixin

    class Command(SeedSubscriptionsMixin, BaseCommand):
        ...

``_seed_subscriptions`` must run after ``_seed_assets`` (it reads
``self._orgs`` / ``self._tenants`` / ``self._tenant_meta`` / ``self._saas_suppliers``
/ ``self._provisioner`` / ``self._assets_by_tenant``). It populates
``self._subscriptions`` (consumed by the contracts/costing phase).
"""

import datetime
import random

from django.contrib.contenttypes.models import ContentType

from assets.models import Asset
from subscriptions.models import Subscription, SubscriptionAssignment

TODAY = datetime.date.today()


def days_ago(n):
    return TODAY - datetime.timedelta(days=n)


def days_ahead(n):
    return TODAY + datetime.timedelta(days=n)


class SeedSubscriptionsMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    def _seed_subscriptions(self):

        self.stdout.write("--- Subscriptions ---")
        self._subscriptions = []
        ct_asset = ContentType.objects.get_for_model(Asset)
        # One cloud footprint per organization, contracted centrally by the parent
        # entity (the group's primary tenant). SubscriptionAssignment is a strict
        # tenant boundary, so the demo data may only link targets owned by that tenant.
        for org in self._orgs:
            primary_slug = org["tenants"][0]["slug"]
            tenant = self._tenants[primary_slug]
            currency = self._tenant_meta[primary_slug]["currency"]
            plan = [
                ("Amazon Web Services", random.randint(30000, 150000)),
                ("Microsoft Azure", random.randint(40000, 200000)),
            ]
            if org["kind"] == "msp" or random.random() < 0.5:
                plan.append(("GitHub Enterprise", random.randint(4000, 40000)))
            if random.random() < 0.5:
                plan.append(("Datadog", random.randint(8000, 36000)))
            aws_sub = None
            for supplier_name, cost in plan:
                start = days_ago(random.randint(60, 700))
                renewal = days_ahead(random.choice([20, 35, 60, 120, 300]))
                sub = Subscription.objects.create(
                    name=supplier_name,
                    supplier=self._saas_suppliers[supplier_name],
                    type="saas",
                    start_date=start,
                    renewal_date=renewal,
                    renewal_cost=cost,
                    currency=currency,
                    billing_cycle="annual",
                    term_months=12,
                    vendor_contract_auto_renews=True,
                    contract_reference=f"MSA-{supplier_name.split()[0].upper()}-{start.year}",
                    owner=self._provisioner,
                    description=f"{supplier_name} cloud subscription. The group contract is held by {tenant.name}.",
                    tenant=tenant,
                )
                self._subscriptions.append(sub)
                if supplier_name == "Amazon Web Services":
                    aws_sub = sub
            # The contract belongs to the primary tenant, so only its servers are
            # valid assignment targets. Cross-entity cost allocation needs a separate,
            # explicitly group-scoped model rather than weakening this boundary.
            if aws_sub:
                servers = [
                    asset
                    for asset in self._assets_by_tenant.get(primary_slug, [])
                    if asset.asset_role and "server" in asset.asset_role.slug
                ]
                for server in servers[:3]:
                    SubscriptionAssignment.objects.get_or_create(
                        subscription=aws_sub,
                        content_type=ct_asset,
                        object_id=server.pk,
                        defaults={
                            "assigned_by": self._provisioner,
                            "notes": "Hybrid workload node",
                        },
                    )
        self.stdout.write(f"  {len(self._subscriptions)} subscriptions across {len(self._orgs)} organizations.")
