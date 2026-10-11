import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assets.models import AssetType, Manufacturer, StatusLabel, Supplier
from organization.models import Location, Site, Tenant
from procurement.forms import ContractForm
from procurement.models import Contract, PurchaseOrder

User = get_user_model()


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


class ContractModelTests(TestCase):
    """Unit tests for the Contract model."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Contract Tenant", slug="contract-tenant")
        self.supplier = Supplier.objects.create(name="Contract Supplier", slug="contract-supplier")

    def _make_contract(self, **kwargs):
        defaults = dict(
            name="Hardware Support Agreement",
            contract_number="CTR-001",
            contract_type="support",
            status="active",
            supplier=self.supplier,
            tenant=self.tenant,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )
        defaults.update(kwargs)
        return Contract.objects.create(**defaults)

    # --- Basic creation ---

    def test_contract_creation_defaults(self):
        contract = self._make_contract()
        self.assertEqual(contract.status, "active")
        self.assertEqual(contract.contract_type, "support")
        self.assertFalse(contract.auto_renew)
        self.assertEqual(contract.currency, "")  # CurrencyField blanks = tenant default
        self.assertIsNone(contract.cost)

    def test_contract_str(self):
        contract = self._make_contract()
        self.assertEqual(str(contract), "CTR-001 – Hardware Support Agreement")

    def test_get_absolute_url(self):
        contract = self._make_contract()
        self.assertIn(str(contract.pk), contract.get_absolute_url())

    # --- Uniqueness: contract_number per-active row ---

    def test_unique_contract_number_active_enforced(self):
        """Two active contracts with the same number must raise IntegrityError."""
        self._make_contract(contract_number="CTR-DUP")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            # Bypass full_clean — hit the DB constraint directly
            Contract.objects.create(
                name="Duplicate",
                contract_number="CTR-DUP",
                contract_type="maintenance",
                status="draft",
                start_date=datetime.date(2026, 1, 1),
                end_date=datetime.date(2027, 1, 1),
            )

    def test_unique_contract_number_allows_soft_deleted_resurrection(self):
        """After soft-deleting a contract, a new one with the same number is allowed."""
        contract = self._make_contract(contract_number="CTR-REBORN")
        contract.delete()  # soft-delete sets deleted_at
        # This must not raise — the old row is soft-deleted so the constraint lifts
        new_contract = self._make_contract(contract_number="CTR-REBORN")
        self.assertIsNotNone(new_contract.pk)

    # --- Assets M2M ---

    def test_assets_m2m(self):
        from assets.models import Asset

        manufacturer = Manufacturer.objects.create(name="M2M Mfr", slug="m2m-mfr")
        asset_type = AssetType.objects.create(manufacturer=manufacturer, model="M2M Model", slug="m2m-model")
        status_label, _ = StatusLabel.objects.get_or_create(
            name="Deployable", defaults={"type": "deployable", "slug": "deployable"}
        )
        asset = Asset.objects.create(
            asset_type=asset_type,
            name="Test Asset",
            asset_tag="TAG-M2M",
            status=status_label,
        )
        contract = self._make_contract()
        contract.assets.add(asset)
        self.assertIn(asset, contract.assets.all())
        self.assertIn(contract, asset.contracts.all())

    # --- Currency field ---

    def test_currency_blank_default(self):
        contract = self._make_contract()
        self.assertEqual(contract.currency, "")

    def test_currency_explicit(self):
        contract = self._make_contract(currency="USD")
        contract.refresh_from_db()
        self.assertEqual(contract.currency, "USD")

    # --- Date properties ---

    def test_days_until_expiry_future(self):
        future_end = timezone.now().date() + datetime.timedelta(days=15)
        contract = self._make_contract(end_date=future_end)
        self.assertEqual(contract.days_until_expiry, 15)

    def test_is_expiring_soon_true(self):
        future_end = timezone.now().date() + datetime.timedelta(days=10)
        contract = self._make_contract(end_date=future_end)
        self.assertTrue(contract.is_expiring_soon)

    def test_is_expiring_soon_false_when_far(self):
        future_end = timezone.now().date() + datetime.timedelta(days=90)
        contract = self._make_contract(end_date=future_end)
        self.assertFalse(contract.is_expiring_soon)

    def test_days_until_expiry_negative_when_past(self):
        past_end = timezone.now().date() - datetime.timedelta(days=5)
        contract = self._make_contract(
            start_date=datetime.date(2020, 1, 1),
            end_date=past_end,
        )
        self.assertLess(contract.days_until_expiry, 0)

    # --- Validation ---

    def test_clean_raises_when_end_before_start(self):

        contract = Contract(
            name="Bad Dates",
            contract_number="CTR-BAD",
            contract_type="lease",
            status="draft",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2026, 1, 1),  # before start
        )
        with self.assertRaises(ValidationError):
            contract.clean()

    # --- PO link ---

    def test_purchase_order_link(self):
        self.site = Site.objects.create(name="Contract Site", slug="contract-site")
        self.location = Location.objects.create(name="Contract Loc", slug="contract-loc", site=self.site)
        user = User.objects.create_superuser(username="ctruser", email="ctr@example.com", password="pw")
        po = PurchaseOrder.objects.create(
            order_number="PO-CTR-001",
            tenant=self.tenant,
            supplier=self.supplier,
            destination_location=self.location,
            created_by=user,
        )
        contract = self._make_contract(purchase_order=po)
        self.assertEqual(contract.purchase_order, po)
        self.assertIn(contract, po.contracts.all())


class ContractFormTests(TestCase):
    """Smoke tests for ContractForm."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Form Tenant", slug="form-tenant")
        self.supplier = Supplier.objects.create(name="Form Supplier", slug="form-supplier")

    def test_contract_form_valid(self):
        form_data = {
            "name": "Annual Support",
            "contract_number": "CTR-FORM-001",
            "contract_type": "support",
            "status": "draft",
            "supplier": self.supplier.pk,
            "cost": "12000.00",
            "currency": "EUR",
            "billing_cycle": "annual",
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",
            "renewal_date": "",
            "auto_renew": False,
            "sla_response_time": "4 business hours",
            "sla_resolution_time": "8 business hours",
            "coverage_hours": "24x7",
            "sla_terms": "",
            "assets": [],
            "purchase_order": "",
            "tenant": self.tenant.pk,
            "notes": "",
        }
        form = ContractForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        contract = form.save()
        self.assertEqual(contract.contract_number, "CTR-FORM-001")
        self.assertEqual(contract.currency, "EUR")

    def test_contract_form_invalid_missing_dates(self):
        form_data = {
            "name": "No Dates",
            "contract_number": "CTR-NODATE",
            "contract_type": "maintenance",
            "status": "draft",
            # start_date and end_date deliberately omitted
        }
        form = ContractForm(data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn("start_date", form.errors)
        self.assertIn("end_date", form.errors)


class ContractViewSmokeTests(TestCase):
    """List and detail view smoke tests."""

    def setUp(self):
        self.user = User.objects.create_superuser(username="contractview", email="cv@example.com", password="password")
        self.supplier = Supplier.objects.create(name="View Supplier", slug="view-supplier")
        self.contract = Contract.objects.create(
            name="View Test Contract",
            contract_number="CTR-VIEW-001",
            contract_type="support",
            status="active",
            supplier=self.supplier,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )

    def test_contract_list_view(self):
        self.client.force_login(self.user)
        response = self.client.get("/procurement/contracts/")
        self.assertEqual(response.status_code, 200)

    def test_contract_detail_view(self):
        self.client.force_login(self.user)
        response = self.client.get(f"/procurement/contracts/{self.contract.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.contract.contract_number)


# ---------------------------------------------------------------------------
# Contract.cost_center FK tests
# ---------------------------------------------------------------------------


class ContractCostCenterTests(TestCase):
    """Tests for the cost_center FK on Contract."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="CC Tenant", slug="cc-tenant")
        self.supplier = Supplier.objects.create(name="CC Supplier", slug="cc-supplier")

    def _make_contract(self, **kwargs):
        defaults = dict(
            name="CC Contract",
            contract_number="CTR-CC-001",
            contract_type="support",
            status="draft",
            tenant=self.tenant,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )
        defaults.update(kwargs)
        return Contract.objects.create(**defaults)

    def test_cost_center_defaults_to_null(self):
        """A new Contract must have cost_center=None when not specified."""
        contract = self._make_contract()
        self.assertIsNone(contract.cost_center)

    def test_cost_center_blank_is_valid_in_form(self):
        """ContractForm must be valid when cost_center is omitted."""
        form_data = {
            "name": "No CC Contract",
            "contract_number": "CTR-NOCC-001",
            "contract_type": "support",
            "status": "draft",
            "supplier": self.supplier.pk,
            "cost": "",
            "currency": "",
            "billing_cycle": "annual",
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",
            "renewal_date": "",
            "auto_renew": False,
            "sla_response_time": "",
            "sla_resolution_time": "",
            "coverage_hours": "",
            "sla_terms": "",
            "assets": [],
            "purchase_order": "",
            "cost_center": "",
            "tenant": self.tenant.pk,
            "notes": "",
        }
        from procurement.forms import ContractForm

        form = ContractForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        contract = form.save()
        self.assertIsNone(contract.cost_center)

    def test_cost_center_field_present_in_form(self):
        """ContractForm must expose the cost_center field."""
        from procurement.forms import ContractForm

        form = ContractForm()
        self.assertIn("cost_center", form.fields)


# ---------------------------------------------------------------------------
# ContractForm asset queryset tenant-scoping test
# ---------------------------------------------------------------------------


class ContractFormAssetScopingTests(TestCase):
    """Verify that ContractForm scopes the assets queryset to the active tenant."""

    def setUp(self):

        self.tenant_a = Tenant.objects.create(name="Scope Tenant A", slug="scope-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Scope Tenant B", slug="scope-tenant-b")

        manufacturer = Manufacturer.objects.create(name="Scope Mfr", slug="scope-mfr")
        asset_type = AssetType.objects.create(manufacturer=manufacturer, model="Scope Model", slug="scope-model")
        status_label, _ = StatusLabel.objects.get_or_create(
            name="Deployable", defaults={"type": "deployable", "slug": "deployable"}
        )
        from assets.models import Asset

        self.asset_a = Asset.objects.create(
            asset_type=asset_type,
            name="Asset A",
            asset_tag="TAG-SCOPE-A",
            status=status_label,
            tenant=self.tenant_a,
        )
        self.asset_b = Asset.objects.create(
            asset_type=asset_type,
            name="Asset B",
            asset_tag="TAG-SCOPE-B",
            status=status_label,
            tenant=self.tenant_b,
        )

    def test_assets_queryset_scoped_to_active_tenant(self):
        """When Tenant A is active, ContractForm.assets must only list Tenant A's assets."""
        from core.managers import set_current_tenant
        from procurement.forms import ContractForm

        set_current_tenant(self.tenant_a)
        try:
            form = ContractForm()
            qs = form.fields["assets"].queryset
            self.assertIn(self.asset_a, qs)
            self.assertNotIn(self.asset_b, qs)
        finally:
            set_current_tenant(None)

    def test_assets_queryset_unfiltered_when_no_tenant(self):
        """When no tenant is active, ContractForm.assets falls back to the default manager queryset."""
        from core.managers import set_current_tenant
        from procurement.forms import ContractForm

        set_current_tenant(None)
        form = ContractForm()
        qs = form.fields["assets"].queryset
        # Both assets should be visible (no tenant restriction)
        self.assertIn(self.asset_a, qs)
        self.assertIn(self.asset_b, qs)


# ---------------------------------------------------------------------------
# Contract REST API smoke tests
# ---------------------------------------------------------------------------


class ContractAPITests(TestCase):
    """Smoke tests for the Contract REST API (list + create)."""

    def setUp(self):
        self.user = User.objects.create_superuser(username="apiuser", email="api@example.com", password="password")
        self.tenant = Tenant.objects.create(name="API Tenant", slug="api-tenant")
        self.supplier = Supplier.objects.create(name="API Supplier", slug="api-supplier")
        self.contract = Contract.objects.create(
            name="API Contract",
            contract_number="CTR-API-001",
            contract_type="support",
            status="active",
            supplier=self.supplier,
            tenant=self.tenant,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )

    def test_contract_api_list(self):
        """GET /api/procurement/contracts/ returns 200 for a superuser."""
        self.client.force_login(self.user)
        response = self.client.get("/api/procurement/contracts/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("results", data)

    def test_contract_api_detail(self):
        """GET /api/procurement/contracts/<pk>/ returns the contract."""
        self.client.force_login(self.user)
        response = self.client.get(f"/api/procurement/contracts/{self.contract.pk}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["contract_number"], "CTR-API-001")
        self.assertEqual(data["display"], "CTR-API-001 – API Contract")

    def test_contract_api_create(self):
        """POST /api/procurement/contracts/ creates a new contract."""
        self.client.force_login(self.user)
        payload = {
            "name": "Created via API",
            "contract_number": "CTR-API-NEW",
            "contract_type": "maintenance",
            "status": "draft",
            "start_date": "2026-06-01",
            "end_date": "2027-06-01",
            "billing_cycle": "annual",
            "tenant_id": self.tenant.pk,
        }
        response = self.client.post(
            "/api/procurement/contracts/",
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["contract_number"], "CTR-API-NEW")
