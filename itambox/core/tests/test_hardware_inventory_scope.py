"""The hardware inventory report counts only the report's tenant set."""

from django.test import TestCase
from django.utils.translation import gettext as _

from assets.models import Manufacturer
from core.tests.mixins import TenantTestMixin, compile_report_with_system_authorization
from extras.models import ReportTemplate
from inventory.models import Accessory, AccessoryStock
from organization.models import Location, Site, Tenant


class HardwareInventoryReportScopeTests(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="HW Scope A", slug="hw-scope-a")
        self.tenant_b = Tenant.objects.create(name="HW Scope B", slug="hw-scope-b")
        site_a = Site.objects.create(name="HW A Site", slug="hw-a-site", tenant=self.tenant)
        site_b = Site.objects.create(name="HW B Site", slug="hw-b-site", tenant=self.tenant_b)
        location_a = Location.objects.create(name="HW A Depot", slug="hw-a-depot", site=site_a, tenant=self.tenant)
        location_b = Location.objects.create(name="HW B Depot", slug="hw-b-depot", site=site_b, tenant=self.tenant_b)

        manufacturer = Manufacturer.objects.create(name="HW Scope Mfr", slug="hw-scope-mfr")
        item = Accessory.objects.create(
            name="HW Scope Global Item", slug="hw-scope-global-item", manufacturer=manufacturer
        )
        AccessoryStock.objects.create(accessory=item, location=location_a, qty=10)
        AccessoryStock.objects.create(accessory=item, location=location_b, qty=20)

        self.template = ReportTemplate.objects.create(
            name="HW Scope Report",
            report_type=ReportTemplate.REPORT_TYPE_HARDWARE_INVENTORY,
            tenant=self.tenant,
        )

    def test_report_uses_only_the_active_tenant_stock(self):
        _headers, rows, _cards, _grouped, _chart, context = compile_report_with_system_authorization(
            self.template, active_tenant=self.tenant
        )

        row = next(row for row in rows if row.get(_("Name")) == "HW Scope Global Item")
        self.assertEqual(row[_("Total Stock")], "10")
        self.assertEqual(row[_("Available")], "10")
        self.assertEqual(context["report_name"], self.template.name)
