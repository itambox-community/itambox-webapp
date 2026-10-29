"""
Tests for the Snipe-IT importer (core/importers/snipeit/).

No network access: all HTTP calls are intercepted via unittest.mock.patch on
SnipeITClient._get, which backs every paginated get_all() call.
"""

from __future__ import annotations

import logging
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.management.base import OutputWrapper

from core.errors import IntegrationAuthenticationError, IntegrationContext, IntegrationUnavailableError, RetryBudget
from core.importers.snipeit import IMPORT_NOTE, SnipeITClient, SnipeITImporter, _clean_field_name
from core.tests.mixins import TenantTestMixin
from inventory.services import checkout_inventory_item, create_component_allocation

User = get_user_model()

# ---------------------------------------------------------------------------
# Fixtures shared by multiple tests
# ---------------------------------------------------------------------------

# Minimal Snipe-IT API payloads. Pagination is simulated by returning 'total'
# equal to the number of rows so a single page is sufficient.

SNIPE_STATUS_LABELS = {
    "total": 2,
    "rows": [
        {"id": 1, "name": "Ready to Deploy", "type": "deployable"},
        {"id": 2, "name": "In Use", "type": "deployed"},
    ],
}
SNIPE_MANUFACTURERS = {
    "total": 1,
    "rows": [{"id": 1, "name": "Acme Corp"}],
}
SNIPE_CATEGORIES = {
    "total": 1,
    "rows": [{"id": 1, "name": "Laptops", "category_type": "asset"}],
}
SNIPE_SUPPLIERS = {
    "total": 1,
    "rows": [
        {
            "id": 1,
            "name": "TechSupply",
            "email": "sales@techsupply.com",
            "phone": "",
            "url": "",
            "contact": "",
            "notes": "",
        }
    ],
}
SNIPE_LOCATIONS = {
    "total": 2,
    "rows": [
        {"id": 10, "name": "HQ", "parent": None},
        {"id": 11, "name": "Floor 1", "parent": {"id": 10}},
    ],
}
SNIPE_USERS = {
    "total": 1,
    "rows": [
        {
            "id": 5,
            "username": "jdoe",
            "first_name": "John",
            "last_name": "Doe",
            "email": "jdoe@example.com",
            "company": None,
        },
    ],
}
SNIPE_FIELDS = {
    "total": 1,
    "rows": [
        {
            "id": 3,
            "name": "CPU Model",
            "db_column_name": "_snipeit_cpu_model_3",
            "format": "TEXT",
            "field_values": None,
            "type": "text",
        },
    ],
}
SNIPE_FIELDSETS = {
    "total": 1,
    "rows": [
        {"id": 2, "name": "Laptop Specs", "fields": {"rows": [{"db_column_name": "_snipeit_cpu_model_3", "id": 3}]}},
    ],
}
SNIPE_MODELS = {
    "total": 1,
    "rows": [
        {
            "id": 7,
            "name": "ThinkPad X1",
            "manufacturer": {"id": 1},
            "category": {"id": 1},
            "fieldset": {"id": 2},
            "eol": 36,
            "model_number": "TP-X1",
        },
    ],
}
SNIPE_HARDWARE = {
    "total": 1,
    "rows": [
        {
            "id": 42,
            "asset_tag": "NW-0001",
            "serial": "SN-ABC123",
            "name": "Alice Laptop",
            "model": {"id": 7},
            "status_label": {"id": 1},
            "supplier": {"id": 1},
            "location": {"id": 10},
            "rtd_location": None,
            "purchase_date": {"date": "2023-01-15"},
            "purchase_cost": "1299.00",
            "order_number": "PO-2023-0001",
            "notes": "Primary device",
            "warranty_months": 36,
            "company": None,
            "assigned_to": None,
            "custom_fields": {
                "CPU Model": {
                    "field": "_snipeit_cpu_model_3",
                    "value": "Intel i7-1270P",
                    "field_format": "TEXT",
                },
            },
        },
    ],
}
SNIPE_HARDWARE_CHECKED_OUT = {
    "total": 1,
    "rows": [
        {
            "id": 43,
            "asset_tag": "NW-0002",
            "serial": "SN-DEF456",
            "name": "Bob Laptop",
            "model": {"id": 7},
            "status_label": {"id": 1},
            "supplier": None,
            "location": None,
            "rtd_location": None,
            "purchase_date": None,
            "purchase_cost": None,
            "order_number": "",
            "notes": "",
            "warranty_months": None,
            "company": None,
            "assigned_to": {"id": 5, "type": "user"},
            "custom_fields": {},
        },
    ],
}
SNIPE_COMPANIES = {
    "total": 2,
    "rows": [
        {"id": 100, "name": "Acme Corp"},
        {"id": 101, "name": "Globex"},
    ],
}
SNIPE_ACCESSORIES = {"total": 0, "rows": []}
SNIPE_CONSUMABLES = {"total": 0, "rows": []}
SNIPE_COMPONENTS = {"total": 0, "rows": []}
SNIPE_LICENSES = {"total": 0, "rows": []}
SNIPE_MAINTENANCES = {"total": 0, "rows": []}


def _make_client_mock(pages: dict | None = None) -> SnipeITClient:
    """
    Return a SnipeITClient whose _get is patched to return fixture pages.

    `pages` maps endpoint prefixes to response dicts.  Defaults to the
    standard single-asset scenario.
    """
    defaults = {
        "/api/v1/statuslabels": SNIPE_STATUS_LABELS,
        "/api/v1/manufacturers": SNIPE_MANUFACTURERS,
        "/api/v1/categories": SNIPE_CATEGORIES,
        "/api/v1/suppliers": SNIPE_SUPPLIERS,
        "/api/v1/locations": SNIPE_LOCATIONS,
        "/api/v1/users": SNIPE_USERS,
        "/api/v1/fields": SNIPE_FIELDS,
        "/api/v1/fieldsets": SNIPE_FIELDSETS,
        "/api/v1/models": SNIPE_MODELS,
        "/api/v1/hardware": SNIPE_HARDWARE,
        "/api/v1/accessories": SNIPE_ACCESSORIES,
        "/api/v1/consumables": SNIPE_CONSUMABLES,
        "/api/v1/components": SNIPE_COMPONENTS,
        "/api/v1/licenses": SNIPE_LICENSES,
        "/api/v1/maintenances": SNIPE_MAINTENANCES,
    }
    if pages:
        defaults.update(pages)

    client = SnipeITClient.__new__(SnipeITClient)
    client.base_url = "https://snipe.example"
    client.PAGE_SIZE = 500
    client.context = IntegrationContext(provider="snipe-it", operation="test")
    client.retry_budget_factory = RetryBudget

    def fake_get(endpoint, params=None, **kwargs):
        path = endpoint.split("?")[0]
        for prefix, data in defaults.items():
            if path == prefix or path.startswith(prefix + "/"):
                return data
        return {"total": 0, "rows": []}

    client._get = fake_get
    return client


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_clean_field_name_strips_prefix_and_id(self):
        assert _clean_field_name("_snipeit_cpu_model_3") == "cpu_model"

    def test_clean_field_name_no_prefix(self):
        assert _clean_field_name("hostname") == "hostname"

    def test_clean_field_name_trailing_large_id(self):
        assert _clean_field_name("_snipeit_department_123") == "department"


# ---------------------------------------------------------------------------
# Integration tests (hit a real test DB, no network)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSnipeITImporter(TenantTestMixin):
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.setup_tenant_context(
            name="Acme",
            slug="acme",
            permissions=["assets.change_asset"],
        )
        self.admin = User.objects.create_superuser(username="impadmin", email="impadmin@example.com", password="pw")
        self.grant(self.admin, self.tenant, self.tenant_role)

    def _run(self, pages=None, dry_run=False, update=False, map_companies=False, skip=None):
        from core.tasks.context import TaskContext

        client = _make_client_mock(pages)
        with TaskContext(tenant_id=self.tenant.pk, user_id=self.admin.pk):
            importer = SnipeITImporter(
                client=client,
                tenant=self.tenant,
                user=self.admin,
                dry_run=dry_run,
                update=update,
                map_companies=map_companies,
                skip=skip or set(),
                checkout_inventory_item=checkout_inventory_item,
                create_component_allocation=create_component_allocation,
            )
            return importer.run()

    # ------------------------------------------------------------------
    # Basic import
    # ------------------------------------------------------------------

    def test_basic_import_creates_records(self):
        from assets.models import Asset, AssetType, Category, Manufacturer, StatusLabel
        from extras.models import CustomField, CustomFieldset
        from organization.models import AssetHolder, Location

        counts = self._run()

        assert StatusLabel._base_manager.filter(name="Ready to Deploy").exists()
        assert StatusLabel._base_manager.filter(name="In Use").exists()
        assert Manufacturer._base_manager.filter(name="Acme Corp").exists()
        assert Category._base_manager.filter(name="Laptops").exists()
        assert Location._base_manager.filter(name="HQ").exists()
        assert Location._base_manager.filter(name="Floor 1").exists()
        assert AssetHolder._base_manager.filter(upn="jdoe").exists()
        field = CustomField._base_manager.get(name="cpu_model")
        fieldset = CustomFieldset._base_manager.get(namespace="local", slug="snipeit-2", label="Laptop Specs")
        asset_type = AssetType._base_manager.get(model="ThinkPad X1")
        assert field.activation == CustomField.ACTIVATION_COMPOSED
        assert field.lifecycle == CustomField.LIFECYCLE_ACTIVE
        assert set(field.object_types.values_list("app_label", "model")) == {
            ("assets", "asset"),
            ("assets", "assettype"),
        }
        assert fieldset.lifecycle == CustomFieldset.LIFECYCLE_ACTIVE
        assert list(fieldset.field_memberships.values_list("custom_field_id", "position")) == [(field.pk, 1)]
        assert list(asset_type.fieldset_memberships.values_list("fieldset_id", "position")) == [(fieldset.pk, 1)]
        assert Asset._base_manager.filter(asset_tag="NW-0001").exists()

        assert counts["assets"]["created"] == 1
        assert counts["users"]["created"] == 1

    def test_custom_field_value_stored_in_custom_field_data(self):
        from assets.models import Asset

        self._run()
        asset = Asset._base_manager.get(asset_tag="NW-0001")
        assert asset.custom_field_data.get("cpu_model") == "Intel i7-1270P"
        assert asset.custom_field_data.get("snipeit_id") == "42"

    # ------------------------------------------------------------------
    # Parent-child location hierarchy
    # ------------------------------------------------------------------

    def test_custom_field_values_are_canonicalized_before_storage(self):
        from assets.models import Asset

        fields = {
            "total": 3,
            "rows": [
                *SNIPE_FIELDS["rows"],
                {
                    "id": 4,
                    "name": "Stage Core Count",
                    "db_column_name": "_snipeit_stage_core_count_4",
                    "format": "NUMERIC",
                    "field_values": None,
                    "type": "numeric",
                },
                {
                    "id": 5,
                    "name": "Stage Environment",
                    "db_column_name": "_snipeit_stage_environment_5",
                    "format": "LIST",
                    "field_values": "Production\nStaging",
                    "type": "list",
                },
            ],
        }
        hardware = {
            "total": 1,
            "rows": [
                {
                    **SNIPE_HARDWARE["rows"][0],
                    "custom_fields": {
                        "Core Count": {
                            "field": "_snipeit_stage_core_count_4",
                            "value": "16",
                            "field_format": "NUMERIC",
                        },
                        "Environment": {
                            "field": "_snipeit_stage_environment_5",
                            "value": "Production",
                            "field_format": "LIST",
                        },
                    },
                }
            ],
        }

        fieldsets = {
            "total": 1,
            "rows": [{**SNIPE_FIELDSETS["rows"][0], "fields": {"rows": fields["rows"]}}],
        }
        self._run(pages={"/api/v1/fields": fields, "/api/v1/fieldsets": fieldsets, "/api/v1/hardware": hardware})

        asset = Asset._base_manager.get(asset_tag="NW-0001")
        assert asset.custom_field_data["stage_core_count"] == "16.00"
        assert asset.custom_field_data["stage_environment"] == "production"

    def test_location_parent_wired_up(self):
        from organization.models import Location

        self._run()
        parent = Location._base_manager.get(name="HQ")
        child = Location._base_manager.get(name="Floor 1")
        assert child.parent == parent

    # ------------------------------------------------------------------
    # Idempotency: run twice → same counts, no duplicates
    # ------------------------------------------------------------------

    def test_idempotent_rerun_no_duplicates(self):
        from assets.models import Asset, Manufacturer, StatusLabel

        self._run()
        counts2 = self._run()

        assert Asset._base_manager.filter(asset_tag="NW-0001").count() == 1
        assert StatusLabel._base_manager.filter(name="Ready to Deploy").count() == 1
        assert Manufacturer._base_manager.filter(name="Acme Corp").count() == 1
        # Second run should have zero created (everything already exists)
        assert counts2["assets"]["created"] == 0
        assert counts2["assets"]["skipped"] == 1

    # ------------------------------------------------------------------
    # --update syncs fields
    # ------------------------------------------------------------------

    def test_update_flag_refreshes_existing_records(self):
        from assets.models import Asset

        self._run()
        # Modify locally
        asset = Asset._base_manager.get(asset_tag="NW-0001")
        asset.notes = "Overwritten locally"
        asset.save(update_fields=["notes"])

        # Re-run with --update
        self._run(update=True)
        asset.refresh_from_db()
        assert asset.notes == "Primary device"

    # ------------------------------------------------------------------
    # Checkout flips status to deployed type
    # ------------------------------------------------------------------

    def test_checkout_creates_assignment(self):
        from assets.models import Asset

        self._run(pages={"/api/v1/hardware": SNIPE_HARDWARE_CHECKED_OUT})

        asset = Asset._base_manager.get(asset_tag="NW-0002")
        assert asset.active_assignment is not None
        assert asset.active_assignment.assigned_user is not None

    def test_checked_out_asset_gets_deployed_status(self):
        from assets.choices import StatusTypeChoices
        from assets.models import Asset

        self._run(pages={"/api/v1/hardware": SNIPE_HARDWARE_CHECKED_OUT})

        asset = Asset._base_manager.get(asset_tag="NW-0002")
        assert asset.status.type == StatusTypeChoices.DEPLOYED

    # ------------------------------------------------------------------
    # Dry-run writes nothing
    # ------------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        from assets.models import Asset, Manufacturer, StatusLabel
        from organization.models import AssetHolder, Location

        # Capture baseline counts (migration seeds pre-existing status labels)
        sl_before = StatusLabel._base_manager.count()

        self._run(dry_run=True)

        assert Asset._base_manager.count() == 0
        assert Manufacturer._base_manager.count() == 0
        assert StatusLabel._base_manager.count() == sl_before  # no new ones created
        assert AssetHolder._base_manager.count() == 0
        assert Location._base_manager.count() == 0

    def test_dry_run_returns_nonzero_created_counts(self):
        from assets.models import StatusLabel

        # "In Use" normally exists via the seed migration (assets 0003), but a
        # TransactionTestCase flush earlier in the run wipes seeded rows from a
        # reused test DB — recreate it so the skip assertion holds regardless.
        StatusLabel.all_objects.get_or_create(
            name="In Use",
            defaults={"slug": "in-use", "type": "deployed", "color": "007bff"},
        )
        counts = self._run(dry_run=True)
        assert counts["assets"]["created"] == 1
        # "Ready to Deploy" is new; "In Use" already exists → skipped
        assert counts["statuslabels"]["created"] == 1
        assert counts["statuslabels"]["skipped"] == 1

    # ------------------------------------------------------------------
    # --map-companies-to-tenants
    # ------------------------------------------------------------------

    def test_map_companies_creates_tenants(self):
        from organization.models import Tenant

        before = Tenant._base_manager.count()
        self._run(
            map_companies=True,
            pages={
                "/api/v1/companies": SNIPE_COMPANIES,
                "/api/v1/hardware": SNIPE_HARDWARE,
            },
        )
        after = Tenant._base_manager.count()
        assert after >= before + 2

    def test_no_map_companies_does_not_create_extra_tenants(self):
        from organization.models import Tenant

        before = Tenant._base_manager.count()
        self._run(map_companies=False)
        after = Tenant._base_manager.count()
        assert after == before  # default tenant already exists; none created

    # ------------------------------------------------------------------
    # --skip
    # ------------------------------------------------------------------

    def test_skip_assets_does_not_create_assets(self):
        from assets.models import Asset

        self._run(skip={"assets"})
        assert Asset._base_manager.count() == 0

    # ------------------------------------------------------------------
    # Pagination: two pages → all rows imported
    # ------------------------------------------------------------------

    def test_pagination_imports_all_rows(self):
        from assets.models import StatusLabel

        two_page_labels = {
            "total": 4,
            "rows": [
                {"id": 10, "name": "Status A", "type": "deployable"},
                {"id": 11, "name": "Status B", "type": "pending"},
            ],
        }
        page2 = {
            "total": 4,
            "rows": [
                {"id": 12, "name": "Status C", "type": "undeployable"},
                {"id": 13, "name": "Status D", "type": "archived"},
            ],
        }

        def fake_get(endpoint, params=None, **kwargs):
            if endpoint == "/api/v1/statuslabels":
                if (params or {}).get("offset", 0) == 0:
                    return two_page_labels
                return {**page2, "rows": page2["rows"]}
            return {"total": 0, "rows": []}

        from core.tasks.context import TaskContext

        client = SnipeITClient.__new__(SnipeITClient)
        client.base_url = "https://snipe.example"
        client.PAGE_SIZE = 2
        client.context = IntegrationContext(provider="snipe-it", operation="test")
        client.retry_budget_factory = RetryBudget
        client._get = fake_get

        with TaskContext(tenant_id=self.tenant.pk, user_id=self.admin.pk):
            importer = SnipeITImporter(
                client=client,
                tenant=self.tenant,
                user=self.admin,
                dry_run=False,
                skip={"assets", "accessories", "consumables", "components", "licenses", "maintenances"},
                checkout_inventory_item=checkout_inventory_item,
                create_component_allocation=create_component_allocation,
            )
            importer.run()

        assert StatusLabel._base_manager.filter(name__in=["Status A", "Status B", "Status C", "Status D"]).count() == 4

    # ------------------------------------------------------------------
    # Injected inventory services
    # ------------------------------------------------------------------

    def _injected_importer(self, checkout, allocate):
        return SnipeITImporter(
            client=_make_client_mock(),
            tenant=self.tenant,
            user=self.admin,
            checkout_inventory_item=checkout,
            create_component_allocation=allocate,
        )

    def test_assignment_goes_through_the_injected_services(self):
        checkout, allocate = MagicMock(), MagicMock()
        importer = self._injected_importer(checkout, allocate)
        item = SimpleNamespace(tenant_id=self.tenant.pk)
        holder = SimpleNamespace(tenant_id=self.tenant.pk)
        asset = SimpleNamespace(tenant_id=self.tenant.pk)

        importer._inventory_assignments.assign(item, 2, holder=holder)
        importer._inventory_assignments.assign(item, 3, asset=asset)

        checkout.assert_called_once_with(item, 2, holder=holder, user=self.admin, notes=IMPORT_NOTE)
        allocate.assert_called_once_with(item, 3, asset=asset, user=self.admin, notes=IMPORT_NOTE)

    def test_services_must_be_injected_explicitly(self):
        # No default: the importer has no way to reach inventory.services on its
        # own, so omitting either callable fails at construction, not mid-import.
        with pytest.raises(TypeError):
            SnipeITImporter(client=_make_client_mock(), tenant=self.tenant, user=self.admin)

    def test_checkout_asset_falls_back_to_canonical_service(self):
        from django.utils.module_loading import import_string

        importer = SnipeITImporter(
            client=_make_client_mock(),
            tenant=self.tenant,
            user=self.admin,
            checkout_inventory_item=MagicMock(),
            create_component_allocation=MagicMock(),
        )

        assert importer.checkout_asset is import_string("assets.services.checkout_asset")

    @pytest.mark.parametrize("abort_during_import", [False, True])
    def test_management_command_reports_safe_integration_failures(self, abort_during_import, monkeypatch, caplog):
        from django.core.management.base import CommandError

        from core.management.commands import import_snipeit

        secret = "customer@example.test bearer-secret"
        client = MagicMock()
        client.base_url = "https://snipe.example"
        context = IntegrationContext(provider="snipe-it", operation="import", tenant_id=self.tenant.pk)
        if abort_during_import:
            client.get_detail.return_value = {"id": 1}
            importer = MagicMock()
            importer.run.side_effect = IntegrationUnavailableError(context=context, cause_type="Timeout")
        else:
            client.get_detail.side_effect = IntegrationAuthenticationError(context=context, cause_type="HTTPError")
            importer = MagicMock()

        monkeypatch.setenv("SNIPEIT_TOKEN", secret)
        monkeypatch.setattr("core.management.commands.import_snipeit.SnipeITClient", MagicMock(return_value=client))
        monkeypatch.setattr("core.management.commands.import_snipeit.SnipeITImporter", MagicMock(return_value=importer))
        command = import_snipeit.Command()
        output = StringIO()
        command.stdout = OutputWrapper(output)
        options = {
            "token_env": "SNIPEIT_TOKEN",
            "url": "https://user:password@snipe.example/?token=query-secret",
            "tenant": self.tenant.slug,
            "map_companies_to_tenants": False,
            "dry_run": True,
            "update": False,
            "skip": "",
            "admin_user": self.admin.username,
        }

        expected = pytest.raises(SystemExit) if abort_during_import else pytest.raises(CommandError)
        with caplog.at_level(logging.ERROR), expected as raised:
            command.handle(**options)

        if abort_during_import:
            assert raised.value.code == 1
        combined = caplog.text + output.getvalue()
        assert "integration" in combined.lower()
        assert secret not in combined
        assert "query-secret" not in combined
