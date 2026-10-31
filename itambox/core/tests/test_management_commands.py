import datetime
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command, get_commands, load_command_class
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from assets.customfields import resolve_asset_type_custom_fields
from assets.models import (
    Asset,
    AssetAssignment,
    AssetDisposal,
    AssetMaintenance,
    AssetRequest,
    AssetReservation,
    AssetRole,
    AssetType,
    Category,
    Manufacturer,
    ReservationStatusChoices,
    StatusLabel,
    Supplier,
)
from assets.services import checkin_asset, checkout_asset
from core.management.commands._seed.access import check_seed_access_invariants
from core.management.commands._seed.consistency import check_seed_operational_invariants
from core.management.commands._seed.maintenance import link_repair_records
from core.management.commands.seed_data import Command as SeedDataCommand
from core.management.commands.sync_tenant_ldap import Command as SyncTenantLDAPCommand
from core.models import ChangeLoggingMixin, Job, ObjectChange
from core.tasks.management import SystemTaskCommand
from extras.models import CustomField, CustomFieldChoice, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField
from extras.signals import _safe_dispatch
from inventory.models import Accessory
from licenses.models import License
from organization.models import AssetHolder, Location, Membership, Site, Tenant
from procurement.models import PurchaseOrder, PurchaseOrderLine

User = get_user_model()


class ManagementCommandsTestCase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def test_purge_deleted_command(self):
        call_command("purge_deleted", days=30, dry_run=True, stdout=self.stdout, stderr=self.stderr)
        self.assertIn("Total objects that would be purged", self.stdout.getvalue())

    def test_rotate_encryption_keys_command(self):
        from assets.models import Manufacturer
        from software.models import Software

        mfr = Manufacturer.objects.create(name="Microsoft", slug="microsoft")
        software = Software.objects.create(name="Office 365", manufacturer=mfr)
        License.objects.create(name="Office 365", software=software, product_key="abc")
        call_command("rotate_encryption_keys", dry_run=True, stdout=self.stdout, stderr=self.stderr)
        self.assertIn("Scanning for encrypted fields", self.stdout.getvalue())

    def test_run_jobs_command(self):
        Job.objects.create(name="Script: my_script.py", status=Job.STATUS_PENDING)
        call_command("run_jobs", stdout=self.stdout, stderr=self.stderr)
        self.assertIn("Job processing complete", self.stdout.getvalue())

    def test_run_jobs_reports_job_failure(self):
        job = Job.objects.create(name="Failing script", status=Job.STATUS_PENDING)
        with patch(
            "core.management.commands.run_jobs.Command._execute_job",
            side_effect=RuntimeError("backend unavailable"),
        ):
            call_command("run_jobs", stdout=self.stdout, stderr=self.stderr)

        job.refresh_from_db()
        self.assertEqual(job.status, Job.STATUS_FAILED)
        self.assertEqual(self.stderr.getvalue(), 'Job "Failing script" failed: backend unavailable\n')

    def test_seed_data_reports_explicit_superuser_password_source(self):
        env = {
            "DJANGO_SUPERUSER_USERNAME": "seed-output-admin",
            "DJANGO_SUPERUSER_EMAIL": "seed-output-admin@example.com",
            "DJANGO_SUPERUSER_PASSWORD": "not-a-real-password",
        }
        with patch.dict("os.environ", env):
            call_command("seed_data", production=True, skip_drop=True, stdout=self.stdout, stderr=self.stderr)

        admin = User.objects.get(username=env["DJANGO_SUPERUSER_USERNAME"])
        self.assertTrue(admin.is_superuser)
        self.assertIn(
            '  Created superuser "seed-output-admin" (password from DJANGO_SUPERUSER_PASSWORD).',
            self.stdout.getvalue(),
        )

    def test_sync_intune_reports_enqueued_job(self):
        tenant = Tenant.objects.create(name="Intune Enqueue Tenant", slug="intune-enqueue-tenant")
        admin = User.objects.create_superuser(
            username="intune-enqueue-admin",
            email="intune-enqueue-admin@example.com",
            password="not-a-real-password",
        )
        with (
            override_settings(ITAMBOX_TENANT_INTUNE_CONFIGS={tenant.slug: {}}),
            patch("core.management.commands.sync_intune.async_task") as enqueue,
        ):
            call_command("sync_intune", tenant=tenant.slug, stdout=self.stdout, stderr=self.stderr)

        job = Job.objects.get(name=f"intune-sync:{tenant.slug}")
        self.assertEqual(
            self.stdout.getvalue(),
            f"Created Job #{job.pk}: {job.name}\n"
            "Intune sync for tenant '%s' enqueued (Job #%s).\n" % (tenant.slug, job.pk),
        )
        enqueue.assert_called_once_with(
            "assets.tasks.intune_sync.sync_tenant_intune",
            tenant_id=tenant.pk,
            user_id=admin.pk,
            job_id=job.pk,
            dry_run=False,
        )

    def test_every_mutating_management_command_uses_system_task_context(self):
        """Every project command is classified: mutating commands run on the
        SystemTaskCommand base, read-only commands stay plain."""
        mutating_commands = {
            "bind_oidc_identity",
            "import_snipeit",
            "prune_changelog",
            "purge_deleted",
            "restore_resource_grant",
            "rotate_encryption_keys",
            "run_jobs",
            "seed_data",
            "sync_intune",
            "sync_tenant_ldap",
            "reconcile_procurement_legacy",
        }
        read_only_commands = {
            "capabilities",
            "capture_recovery_evidence",
            "capture_schema_evidence",
            "compile_locales",
            "eventrule_withdrawn_report",
            "export_datamodel",
            "integrity_report",
            "list_failed_tasks",
            "migration_baseline_preflight",
            "plugins",
            "validate_role_permissions",
        }
        available_commands = get_commands()
        self.assertTrue((mutating_commands | read_only_commands).issubset(available_commands))

        for name in sorted(mutating_commands):
            with self.subTest(command=name):
                command = load_command_class(available_commands[name], name)
                self.assertIsInstance(command, SystemTaskCommand)
        for name in sorted(read_only_commands):
            with self.subTest(command=name):
                command = load_command_class(available_commands[name], name)
                self.assertNotIsInstance(command, SystemTaskCommand)

    def test_seed_data_command(self):
        call_command("seed_data", production=True, force=True, stdout=self.stdout, stderr=self.stderr)
        self.assertIn("Database seeding complete", self.stdout.getvalue())

    def test_seed_catalog_writes_normative_compute_fieldset_memberships(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        fieldset = CustomFieldset.objects.get(namespace="itambox", slug="compute-memory")
        self.assertEqual(fieldset.label, "Compute and Memory")
        self.assertEqual(fieldset.management_kind, CustomFieldset.MANAGEMENT_CORE)
        self.assertEqual(
            list(fieldset.field_memberships.values_list("custom_field__name", "position")),
            [("processor_model", 10), ("core_count", 20), ("memory_capacity", 30), ("memory_type", 40)],
        )

    def test_seed_catalog_writes_complete_normative_composition(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        canonical = json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "scripts"
                / "tests"
                / "fixtures"
                / "specification_vocabulary"
                / "canonical-target.json"
            ).read_text(encoding="utf-8")
        )
        expected_fields = {row["key"]: row for row in canonical["active_fields"] + canonical["reserved_retired_fields"]}
        expected_choice_sets = {row["slug"]: row for row in canonical["choice_sets"]}
        expected_fieldsets = {row["slug"]: row for row in canonical["sections"]}
        choice_ids = dict(CustomFieldChoiceSet.objects.filter(namespace="itambox").values_list("slug", "pk"))
        command._seed_catalog()
        self.assertEqual(
            dict(CustomFieldChoiceSet.objects.filter(namespace="itambox").values_list("slug", "pk")), choice_ids
        )
        self.assertEqual(
            CustomField.objects.filter(namespace="itambox", management_kind=CustomField.MANAGEMENT_CORE).count(),
            len(expected_fields),
        )
        self.assertEqual(CustomFieldChoiceSet.objects.filter(namespace="itambox").count(), len(expected_choice_sets))
        self.assertEqual(CustomFieldset.objects.filter(namespace="itambox").count(), len(expected_fieldsets))
        for field in CustomField.objects.filter(namespace="itambox", management_kind=CustomField.MANAGEMENT_CORE):
            expected = expected_fields[field.name]
            self.assertFalse(field.required)
            self.assertEqual(field.activation, expected["activation"])
            self.assertEqual(field.lifecycle, expected["lifecycle"])
            expected_models = {"assettype" if target == "asset_type" else target for target in expected["targets"]}
            self.assertEqual(set(field.object_types.values_list("model", flat=True)), expected_models)
            expected_choice_slug = (
                expected["choice_set"].rsplit("/", 1)[1] if expected["choice_set"] is not None else None
            )
            expected_choice_id = (
                CustomFieldChoiceSet.objects.get(namespace="itambox", slug=expected_choice_slug).pk
                if expected_choice_slug is not None
                else None
            )
            self.assertEqual(field.choice_set_id, expected_choice_id)
            if field.field_type == CustomField.FIELD_TYPE_SINGLE_SELECT:
                self.assertEqual(field.max_values, 1)
        asset_type = AssetType.objects.get(slug="dell-latitude-5550")
        self.assertFalse(hasattr(asset_type, "custom_fieldset"))
        self.assertNotIn("custom_fieldset", {field.name for field in AssetType._meta.get_fields()})
        self.assertGreater(asset_type.fieldset_memberships.count(), 1)
        expected_asset_type_fieldsets = list(
            asset_type.category.default_fieldset_memberships.values_list("fieldset__slug", "position")
        )
        self.assertEqual(
            list(asset_type.fieldset_memberships.values_list("fieldset__slug", "position")),
            expected_asset_type_fieldsets,
        )
        expected_category_fieldsets = [
            (item["fieldset"].rsplit("/", 1)[1], item["position"])
            for item in next((row for row in canonical["categories"] if row["slug"] == "laptops"))["default_fieldsets"]
        ]
        self.assertEqual(
            list(
                Category.objects.get(slug="laptops").default_fieldset_memberships.values_list(
                    "fieldset__slug", "position"
                )
            ),
            expected_category_fieldsets,
        )
        stored_keys = set(asset_type.custom_field_data)
        resolved_keys = {item.definition.name for item in resolve_asset_type_custom_fields(asset_type)}
        self.assertTrue(stored_keys)
        self.assertTrue(stored_keys.issubset(resolved_keys))
        self.assertFalse(
            stored_keys
            & {
                "cpu",
                "ram_gb",
                "storage_gb",
                "storage_type",
                "screen_size",
                "port_count",
                "poe_budget_w",
                "input_voltage",
                "os_version",
                "gpu",
                "cpu_architecture",
            }
        )

    def test_seed_catalog_validates_field_before_reconciling_object_types(self):

        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        field = CustomField.objects.get(name="form_factor")
        asset_ct = ContentType.objects.get(app_label="assets", model="asset")
        field.object_types.set([asset_ct])
        CustomField.objects.filter(pk=field.pk).update(field_type=CustomField.FIELD_TYPE_TEXT)
        with self.assertRaisesRegex(ValueError, "Core field semantics differ for identity: form_factor"):
            command._seed_catalog()
        field.refresh_from_db()
        self.assertEqual(set(field.object_types.values_list("model", flat=True)), {"asset"})

    def test_seed_catalog_refuses_local_choice_set_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        choice_set = CustomFieldChoiceSet.objects.get(namespace="itambox", slug="form-factor")
        choice_set.management_kind = CustomFieldChoiceSet.MANAGEMENT_LOCAL
        choice_set.save(update_fields=["management_kind"])
        with self.assertRaisesRegex(ValueError, "management"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_allows_local_choice_set_same_slug(self):
        CustomFieldChoiceSet.objects.create(
            namespace="local",
            slug="form-factor",
            label="Local form factor",
            management_kind=CustomFieldChoiceSet.MANAGEMENT_LOCAL,
            lifecycle=CustomFieldChoiceSet.LIFECYCLE_ACTIVE,
        )
        SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()
        self.assertTrue(CustomFieldChoiceSet.objects.filter(namespace="local", slug="form-factor").exists())
        self.assertTrue(CustomFieldChoiceSet.objects.filter(namespace="itambox", slug="form-factor").exists())

    def test_seed_catalog_refuses_local_field_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        field = CustomField.objects.get(name="processor_model")
        field.management_kind = CustomField.MANAGEMENT_LOCAL
        field.save(update_fields=["namespace", "management_kind"])
        with self.assertRaisesRegex(ValueError, "management"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_refuses_local_fieldset_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        fieldset = CustomFieldset.objects.get(namespace="itambox", slug="compute-memory")
        fieldset.management_kind = CustomFieldset.MANAGEMENT_LOCAL
        fieldset.save(update_fields=["management_kind"])
        with self.assertRaisesRegex(ValueError, "management"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_allows_local_fieldset_same_slug(self):
        CustomFieldset.objects.create(
            namespace="local",
            slug="compute-memory",
            label="Local compute memory",
            management_kind=CustomFieldset.MANAGEMENT_LOCAL,
            lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
        )
        SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()
        self.assertTrue(CustomFieldset.objects.filter(namespace="local", slug="compute-memory").exists())
        self.assertTrue(CustomFieldset.objects.filter(namespace="itambox", slug="compute-memory").exists())

    def test_seed_catalog_refuses_inactive_core_field_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        field = CustomField.objects.get(name="processor_model")
        field.lifecycle = CustomField.LIFECYCLE_DEPRECATED
        field.save(update_fields=["lifecycle"])
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_refuses_inactive_core_choice_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        choice_set = CustomFieldChoiceSet.objects.get(namespace="itambox", slug="form-factor")
        choice = choice_set.choices.get(key="notebook")
        choice.lifecycle = CustomFieldChoice.LIFECYCLE_DEPRECATED
        choice.save(update_fields=["lifecycle"])
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_refuses_unexpected_choice_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        choice_set = CustomFieldChoiceSet.objects.get(namespace="itambox", slug="form-factor")
        CustomFieldChoice.objects.create(
            choice_set=choice_set,
            key="unexpected-core",
            label="Unexpected local",
            position=999,
            version=1,
            lifecycle=CustomFieldChoice.LIFECYCLE_ACTIVE,
        )
        with self.assertRaisesRegex(ValueError, "Choice identity"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_refuses_unexpected_fieldset_membership(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        fieldset = CustomFieldset.objects.get(namespace="itambox", slug="compute-memory")
        unexpected = CustomField.objects.create(
            name="unexpected_fieldset_child",
            label="Unexpected fieldset child",
            field_type="text",
            activation=CustomField.ACTIVATION_COMPOSED,
            namespace="itambox",
            management_kind=CustomField.MANAGEMENT_CORE,
            version=1,
            lifecycle=CustomField.LIFECYCLE_ACTIVE,
        )
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=unexpected, position=999)
        with self.assertRaisesRegex(ValueError, "unexpected membership"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_catalog_refuses_inactive_core_fieldset_identity(self):
        command = SeedDataCommand(stdout=self.stdout, stderr=self.stderr)
        command._seed_catalog()
        fieldset = CustomFieldset.objects.get(namespace="itambox", slug="compute-memory")
        fieldset.lifecycle = CustomFieldset.LIFECYCLE_DEPRECATED
        fieldset.save(update_fields=["lifecycle"])
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            SeedDataCommand(stdout=self.stdout, stderr=self.stderr)._seed_catalog()

    def test_seed_access_invariant_requires_admin_memberships_but_exempts_admin_holders(self):
        tenant = Tenant.objects.create(name="Seed Invariant Tenant", slug="seed-invariant-tenant")
        admin = User.objects.create_user(username="admin", password="password")
        org_admin = User.objects.create_user(username="admin@example.com", password="password")
        for user in (admin, org_admin):
            with self.subTest(user=user.username), pytest.raises(CommandError, match="no active membership"):
                check_seed_access_invariants([user])
            Membership._base_manager.create(user=user, tenant=tenant, is_active=True)
        check_seed_access_invariants([admin, org_admin])
        passwordless = User.objects.create(username="passwordless@example.com")
        passwordless.set_unusable_password()
        passwordless.save(update_fields=["password"])
        check_seed_access_invariants([passwordless])
        named_person = User.objects.create_user(
            username="named.person@example.com",
            email="named.person@example.com",
            first_name="Named",
            last_name="Person",
            password="password",
        )
        membership = Membership._base_manager.create(user=named_person, tenant=tenant, is_active=True)
        with pytest.raises(CommandError, match="named.person@example.com: 0 active AssetHolder profiles"):
            check_seed_access_invariants([named_person])
        membership.delete()
        AssetHolder._base_manager.create(
            tenant=tenant,
            user=named_person,
            first_name="Named",
            last_name="Person",
            upn=named_person.username,
            email=named_person.email,
        )
        with pytest.raises(CommandError, match="named.person@example.com: no active membership"):
            check_seed_access_invariants([named_person])
        Membership._base_manager.create(user=named_person, tenant=tenant, is_active=True)
        check_seed_access_invariants([named_person])

    def test_seed_data_refuses_to_wipe_without_force_when_not_debug(self):
        from django.test import override_settings

        with override_settings(DEBUG=False):
            with self.assertRaises(CommandError):
                call_command("seed_data", production=True, stdout=self.stdout, stderr=self.stderr)

    def test_sync_tenant_ldap_command_invalid(self):
        with self.assertRaises(CommandError):
            call_command("sync_tenant_ldap", tenant="non-existent-tenant")


class SeedOperationalInvariantTestCase(TransactionTestCase):
    """The #506 self-check must fail closed on each operational-story contradiction.

    Each test builds the minimal row that violates one acceptance criterion, then
    asserts the check rejects it. They double as the sabotage runs: with the matching
    seed fix reverted, the seeded row is exactly what the check must refuse.
    """

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.create(name="Invariant Tenant", slug="invariant-tenant")
        self.manufacturer = Manufacturer.objects.create(name="Invariant Vendor", slug="invariant-vendor")
        self.category = Category.objects.create(name="Invariant Category", slug="invariant-category")
        self.asset_role = AssetRole.objects.create(name="Invariant Endpoint", slug="invariant-endpoint")
        self.asset_type = AssetType.objects.create(
            manufacturer=self.manufacturer,
            category=self.category,
            model="Invariant Laptop",
            slug="invariant-laptop",
            asset_role=self.asset_role,
            requestable=True,
        )
        self.site = Site.objects.create(name="Invariant Site", slug="invariant-site")
        self.location = Location.objects.create(
            name="Invariant HQ", slug="invariant-hq", tenant=self.tenant, site=self.site
        )
        self.holder = AssetHolder._base_manager.create(
            tenant=self.tenant,
            first_name="Invariant",
            last_name="Holder",
            email="invariant.holder@example.com",
            upn="invariant.holder@example.com",
        )
        self.second_holder = AssetHolder._base_manager.create(
            tenant=self.tenant,
            first_name="Second",
            last_name="Holder",
            email="second.holder@example.com",
            upn="second.holder@example.com",
        )
        # The status labels are migration-seeded reference data (assets.0003), but a
        # test database may legitimately not carry them: TransactionTestCase flushes
        # leave only rows a fixture re-created, and a reused database from an older
        # checkout can lack them entirely. get_or_create makes the fixture work in
        # both worlds without tripping unique_statuslabel_name_active.
        self.available = self._status_label("Available", "available", "deployable")
        self.in_use = self._status_label("In Use", "in-use", "deployed")
        self.pending_repair = self._status_label("Pending Repair", "pending-repair", "pending")
        self.requester = User.objects.create_user(username="requester@example.com", password="password")
        Membership._base_manager.create(user=self.requester, tenant=self.tenant, is_active=True)

    @staticmethod
    def _status_label(name, slug, status_type):
        """Return the status label, creating it only when it is genuinely absent."""
        existing = StatusLabel._base_manager.filter(slug=slug).first()
        if existing is not None:
            return existing
        return StatusLabel._base_manager.create(name=name, slug=slug, type=status_type)

    def _asset(self, **kwargs):
        defaults = dict(
            name="Invariant Laptop",
            asset_type=self.asset_type,
            tenant=self.tenant,
            location=self.location,
            status=self.available,
            purchase_date=datetime.date.today() - datetime.timedelta(days=400),
        )
        defaults.update(kwargs)
        return Asset._base_manager.create(**defaults)

    def test_coherent_rows_pass(self):
        check_seed_operational_invariants()

    def _received_line(self, order_number, qty):
        supplier = Supplier.objects.create(name=f"Supplier {order_number}", slug=f"supplier-{order_number.lower()}")
        po = PurchaseOrder.objects.create(
            tenant=self.tenant,
            order_number=order_number,
            supplier=supplier,
            status="received",
            order_date=datetime.date.today() - datetime.timedelta(days=30),
            destination_location=self.location,
        )
        return PurchaseOrderLine.objects.create(
            purchase_order=po,
            tenant=self.tenant,
            asset_type=self.asset_type,
            qty_ordered=qty,
            qty_received=qty,
            unit_price=1000,
        )

    def test_po_line_under_materialised_assets_fails(self):
        # The #506 bug: a line received with 10 units but only 3 assets in the ledger.
        line = self._received_line("INV-PO-1", 10)
        for _ in range(3):
            self._asset(purchase_order_line=line)
        with self.assertRaisesRegex(CommandError, "reports 10 received but only 3 asset"):
            check_seed_operational_invariants()

    def test_inverted_assignment_interval_fails(self):
        """An assignment checked in before it was checked out is refused.

        Seeded assignments carry a ``checked_out_at`` of "now"; back-dating only the
        check-in would invert the interval and make the assignment look open across
        windows it never spanned.
        """
        asset = self._asset()
        assignment = AssetAssignment.objects.create(
            asset=asset,
            assigned_user=self.holder,
            is_active=False,
            checked_out_at=timezone.now(),
            notes="Seeded in its final state.",
        )
        AssetAssignment._base_manager.filter(pk=assignment.pk).update(
            checked_in_at=timezone.now() - datetime.timedelta(days=30)
        )
        with self.assertRaisesRegex(CommandError, "but checked in earlier"):
            check_seed_operational_invariants()

    def test_closed_assignment_with_future_checkin_fails(self):
        """A closed loan that returns in the future reads as an open one.

        The row is inactive but its interval still covers months of history, so any
        repair drawn in that stretch looks like it happened while somebody held it.
        """
        asset = self._asset()
        AssetAssignment.objects.create(
            asset=asset,
            assigned_user=self.holder,
            is_active=False,
            checked_out_at=timezone.now() - datetime.timedelta(days=200),
            checked_in_at=timezone.now() + datetime.timedelta(days=60),
            notes="Closed, but not really yet.",
        )
        with self.assertRaisesRegex(CommandError, "which is in the future"):
            check_seed_operational_invariants()

    def test_assignment_before_purchase_fails(self):
        """An assignment that starts before the asset existed is refused.

        A prior-loan row dated relative to "now" rather than to the purchase date
        makes a freshly bought laptop look like it was on loan long before it was
        bought, which then reads as "still held" during any later repair window.
        """
        asset = self._asset(purchase_date=datetime.date.today() - datetime.timedelta(days=30))
        AssetAssignment.objects.create(
            asset=asset,
            assigned_user=self.holder,
            is_active=False,
            checked_out_at=timezone.now() - datetime.timedelta(days=300),
            checked_in_at=timezone.now() - datetime.timedelta(days=200),
            notes="Impossible prior loan.",
        )
        with self.assertRaisesRegex(CommandError, "before the asset was bought"):
            check_seed_operational_invariants()

    def test_po_line_fully_materialised_passes(self):
        line = self._received_line("INV-PO-2", 4)
        for _ in range(4):
            self._asset(purchase_order_line=line)
        check_seed_operational_invariants()

    def test_po_line_over_materialised_fails(self):
        """The criterion is equality, so too many assets is a mismatch as well."""
        line = self._received_line("INV-PO-3", 2)
        for _ in range(3):
            self._asset(purchase_order_line=line)
        with self.assertRaisesRegex(CommandError, "reports 2 received but only 3 asset"):
            check_seed_operational_invariants()

    def test_repair_history_on_held_asset_fails(self):
        """A repair recorded while a person still holds the unit is refused."""
        asset = self._asset()
        checkout_asset(asset, holder=self.holder, user=self.requester, status=self.in_use)
        assignment = AssetAssignment.all_objects.get(asset=asset, is_active=True)
        AssetAssignment._base_manager.filter(pk=assignment.pk).update(
            checked_out_at=timezone.now() - datetime.timedelta(days=200)
        )
        # The #506 bug: history walks the unit into repair and back while the same
        # person holds it throughout.
        _log_status_change(asset, self.pending_repair, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=30)
        with self.assertRaisesRegex(CommandError, "while a person still held it"):
            check_seed_operational_invariants()

    def test_earlier_checkin_does_not_excuse_a_later_repair(self):
        """A check-in from months earlier must not satisfy the temporal question.

        The old check asked whether *any* assignment had been checked in before the
        repair, so a closed assignment from January satisfied a June repair even
        though a different holder (assignment B) kept the unit through it.
        """
        asset = self._asset()
        checkout_asset(asset, holder=self.holder, user=self.requester, status=self.in_use)
        early = AssetAssignment.all_objects.get(asset=asset, is_active=True)
        checkin_asset(asset, user=self.requester)
        AssetAssignment._base_manager.filter(pk=early.pk).update(
            is_active=False,
            checked_out_at=timezone.now() - datetime.timedelta(days=200),
            checked_in_at=timezone.now() - datetime.timedelta(days=180),
        )
        checkout_asset(asset, holder=self.second_holder, user=self.requester, status=self.in_use)
        second = AssetAssignment.all_objects.get(asset=asset, is_active=True)
        # Open well before the June repair and never checked back in, so this
        # assignment was open across the whole repair window.
        AssetAssignment._base_manager.filter(pk=second.pk).update(
            checked_out_at=timezone.now() - datetime.timedelta(days=100)
        )
        _log_status_change(asset, self.pending_repair, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=30)
        with self.assertRaisesRegex(CommandError, "while a person still held it"):
            check_seed_operational_invariants()

    def test_repair_history_with_checkin_passes(self):
        """The #506 fix: the unit is checked in before repair and handed back after."""
        asset = self._asset()
        checkout_asset(asset, holder=self.holder, user=self.requester, status=self.in_use)
        assignment = AssetAssignment.all_objects.get(asset=asset, is_active=True)
        checkin_asset(asset, user=self.requester)
        AssetAssignment._base_manager.filter(pk=assignment.pk).update(
            is_active=False,
            checked_out_at=timezone.now() - datetime.timedelta(days=200),
            checked_in_at=timezone.now() - datetime.timedelta(days=70),
        )
        checkout_asset(asset, holder=self.holder, user=self.requester, status=self.in_use)
        active = AssetAssignment.all_objects.get(asset=asset, is_active=True)
        AssetAssignment._base_manager.filter(pk=active.pk).update(
            checked_out_at=timezone.now() - datetime.timedelta(days=29)
        )
        _log_status_change(asset, self.pending_repair, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=30)
        _log_status_change(asset, self.in_use, "update", days_ago=29)
        check_seed_operational_invariants()

    def test_incomplete_maintenance_fails(self):
        """A completed record with no completion date is the #506 maintenance bug."""
        asset = self._asset()
        AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="upgrade",
            status="completed",
            start_date=datetime.date.today() - datetime.timedelta(days=10),
            completion_date=None,
        )
        with self.assertRaisesRegex(CommandError, "has no completion date"):
            check_seed_operational_invariants()

    def test_repair_maintenance_outside_any_repair_window_fails(self):
        """The #506 defect proper: a "repair" on an asset that never went out of service.

        The record's service interval must overlap a repair window the change log
        actually recorded for that asset; paperwork alone is not evidence.
        """
        asset = self._asset()
        AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="repair",
            status="completed",
            start_date=datetime.date.today() - datetime.timedelta(days=10),
            completion_date=datetime.date.today() - datetime.timedelta(days=8),
        )
        with self.assertRaisesRegex(CommandError, "records no repair in that period"):
            check_seed_operational_invariants()

    def test_repair_maintenance_inside_a_repair_window_passes(self):
        """The #506 fix: the record documents a repair the history really performed."""
        asset = self._asset()
        _log_status_change(asset, self.pending_repair, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=40)
        AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="repair",
            status="completed",
            start_date=datetime.date.today() - datetime.timedelta(days=55),
            completion_date=datetime.date.today() - datetime.timedelta(days=50),
        )
        check_seed_operational_invariants()

    def test_seeded_repair_window_links_its_loan_and_disposal(self):
        """The repair maintenance anchors the story; its loan and disposal link to it (#644)."""
        asset = self._asset()
        loaner = self._asset(name="Invariant Loaner")
        start = datetime.date.today() - datetime.timedelta(days=30)
        end = datetime.date.today() - datetime.timedelta(days=20)
        maintenance = AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="repair",
            status="completed",
            start_date=start,
            completion_date=end,
        )
        # A seeded stand-in loan is a real checkout, so the unit really wears the
        # deployed status; its interval is back-dated into the repair window.
        checkout_asset(loaner, holder=self.holder, user=self.requester, is_loan=True, due_date=end)
        loan = AssetAssignment.objects.get(asset=loaner, is_active=True)
        AssetAssignment._base_manager.filter(pk=loan.pk).update(
            checked_out_at=timezone.now() - datetime.timedelta(days=28)
        )
        disposal = AssetDisposal._base_manager.create(
            asset=asset,
            disposal_method="recycle",
            disposal_date=start + datetime.timedelta(days=9),
        )
        window = {"asset": asset, "start": start, "end": end, "holder_id": self.holder.pk}

        linked = link_repair_records(maintenance, window)

        self.assertEqual(linked, 2)
        loan.refresh_from_db()
        disposal.refresh_from_db()
        self.assertEqual(loan.maintenance_id, maintenance.pk)
        self.assertEqual(disposal.maintenance_id, maintenance.pk)

    def test_seeded_links_outside_the_window_stay_unlinked(self):
        """Somebody else's loan and a disposal outside the window are not this repair's."""
        asset = self._asset()
        loaner = self._asset(name="Invariant Spare")
        start = datetime.date.today() - datetime.timedelta(days=30)
        end = datetime.date.today() - datetime.timedelta(days=20)
        maintenance = AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="repair",
            status="completed",
            start_date=start,
            completion_date=end,
        )
        checkout_asset(loaner, holder=self.second_holder, user=self.requester, is_loan=True)
        foreign_loan = AssetAssignment.objects.get(asset=loaner, is_active=True)
        AssetAssignment._base_manager.filter(pk=foreign_loan.pk).update(
            checked_out_at=timezone.now() - datetime.timedelta(days=28)
        )
        late_disposal = AssetDisposal._base_manager.create(
            asset=asset,
            disposal_method="recycle",
            disposal_date=end + datetime.timedelta(days=3),
        )
        window = {"asset": asset, "start": start, "end": end, "holder_id": self.holder.pk}

        linked = link_repair_records(maintenance, window)

        self.assertEqual(linked, 0)
        foreign_loan.refresh_from_db()
        late_disposal.refresh_from_db()
        self.assertIsNone(foreign_loan.maintenance_id)
        self.assertIsNone(late_disposal.maintenance_id)

    def test_in_service_maintenance_needs_no_repair_window(self):
        """An upgrade never takes the unit out of service, so it is exempt."""
        asset = self._asset()
        AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="upgrade",
            status="completed",
            start_date=datetime.date.today() - datetime.timedelta(days=10),
            completion_date=datetime.date.today() - datetime.timedelta(days=8),
        )
        check_seed_operational_invariants()

    def test_approved_request_without_asset_fails(self):
        """An approved request with no allocation can never be claimed (#506)."""
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            status="approved",
        )
        with self.assertRaisesRegex(CommandError, "has no allocated asset"):
            check_seed_operational_invariants()

    def test_approved_request_with_deployable_asset_passes(self):
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            assigned_user=self.holder,
            status="approved",
        )
        check_seed_operational_invariants()

    def test_request_without_tenant_fails(self):
        """A tenantless request is invisible in every tenant scope."""
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=None,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            assigned_user=self.holder,
            status="approved",
        )
        with self.assertRaisesRegex(CommandError, "has no tenant"):
            check_seed_operational_invariants()

    def test_pending_request_may_name_a_concrete_asset(self):
        """A pending request for a specific asset is a legitimate product state.

        ``AssetRequestForm`` offers "Specific Asset (by Tag)", so the check must not
        reject this shape just because the request is not yet approved.
        """
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            assigned_user=self.holder,
            status="pending",
        )
        check_seed_operational_invariants()

    def test_approved_inventory_request_may_have_no_asset(self):
        """An approved accessory request legitimately carries asset=NULL."""
        accessory = Accessory.objects.create(
            tenant=self.tenant,
            manufacturer=self.manufacturer,
            name="Invariant Accessory",
            slug="invariant-accessory",
        )
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            accessory=accessory,
            status="approved",
        )
        check_seed_operational_invariants()

    def test_transit_window_is_not_a_repair(self):
        """A transport window must not be read as a repair.

        ``pending`` is a META-type: the seed types Pending Repair, In Transit and
        Quarantined all as ``pending``, so a reader keyed on the meta-type accepts
        out-of-service paperwork against a window in which the unit was only being
        shipped.
        """
        transit = self._status_label("In Transit", "in-transit", "pending")
        asset = self._asset()
        _log_status_change(asset, transit, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=40)
        AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="repair",
            status="completed",
            start_date=datetime.date.today() - datetime.timedelta(days=55),
            completion_date=datetime.date.today() - datetime.timedelta(days=50),
        )
        with self.assertRaisesRegex(CommandError, "records no repair in that period"):
            check_seed_operational_invariants()

    def test_repair_window_closes_at_the_exit_not_the_last_change(self):
        """The window ends when the unit leaves repair, not at its last change.

        Repair from -60 to -40, an unrelated status change at -10, and paperwork
        dated -20..-18: re-deriving the end from the asset's last change of any kind
        would stretch the window to -10 and accept evidence that is plainly outside
        the repair.
        """
        asset = self._asset()
        _log_status_change(asset, self.pending_repair, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=40)
        _log_status_change(asset, self.in_use, "update", days_ago=10)
        AssetMaintenance._base_manager.create(
            asset=asset,
            maintenance_type="repair",
            status="completed",
            start_date=datetime.date.today() - datetime.timedelta(days=20),
            completion_date=datetime.date.today() - datetime.timedelta(days=18),
        )
        with self.assertRaisesRegex(CommandError, "records no repair in that period"):
            check_seed_operational_invariants()

    def test_checkout_during_a_repair_window_fails(self):
        """A unit handed out *during* its repair is a state the product refuses.

        Asking only whether somebody held the unit when the repair began misses this
        shape: the assignment starts inside the window and never ends.
        """
        asset = self._asset()
        checkout_asset(asset, holder=self.holder, user=self.requester, status=self.in_use)
        assignment = AssetAssignment.all_objects.get(asset=asset, is_active=True)
        AssetAssignment._base_manager.filter(pk=assignment.pk).update(
            checked_out_at=timezone.now() - datetime.timedelta(days=50)
        )
        _log_status_change(asset, self.pending_repair, "update", days_ago=60)
        _log_status_change(asset, self.available, "update", days_ago=40)
        with self.assertRaisesRegex(CommandError, "while a person still held it"):
            check_seed_operational_invariants()

    def _requester_profile(self):
        """Give the requester the holder profile the claim path falls back to.

        ``RequestClaimView._checkout_unit`` uses the request's delegated assignee and
        falls back to the requester's ``AssetHolder`` profile in the request's tenant,
        so both routes have to be exercised.
        """
        return AssetHolder._base_manager.create(
            tenant=self.tenant,
            user=self.requester,
            first_name="Request",
            last_name="Owner",
            upn="requester@example.com",
        )

    def test_approved_request_blocked_by_a_foreign_reservation_fails(self):
        """Allocated is not the same as claimable (#506).

        The checkout service refuses a unit reserved for a *different* holder over
        today, so an approved request pointing at one is still a dead end even though
        the allocation itself looks complete.
        """
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            assigned_user=self.holder,
            status="approved",
        )
        AssetReservation._base_manager.create(
            asset=asset,
            reserved_for=self.second_holder,
            start_date=datetime.date.today() - datetime.timedelta(days=5),
            end_date=datetime.date.today() + datetime.timedelta(days=10),
            status=ReservationStatusChoices.ACTIVE,
        )
        with self.assertRaisesRegex(CommandError, "cannot check it out"):
            check_seed_operational_invariants()

    def test_request_reserved_for_its_own_claim_holder_passes(self):
        """A reservation held by the claim's own holder does not block the claim.

        The blocker is only ever a *different* holder, so this legitimate shape must
        stay allowed rather than being swept up by a wider check.
        """
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            assigned_user=self.holder,
            status="approved",
        )
        AssetReservation._base_manager.create(
            asset=asset,
            reserved_for=self.holder,
            start_date=datetime.date.today() - datetime.timedelta(days=5),
            end_date=datetime.date.today() + datetime.timedelta(days=10),
            status=ReservationStatusChoices.ACTIVE,
        )
        check_seed_operational_invariants()

    def test_request_without_a_resolvable_assignee_fails(self):
        """A request the claim view cannot resolve an assignee for is a dead end.

        The customer-admin logins the seed creates deliberately carry no holder
        profile, so a request raised by one of them without a delegated target can
        never be checked out to anybody.
        """
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            status="approved",
        )
        with self.assertRaisesRegex(CommandError, "resolves no assignee"):
            check_seed_operational_invariants()

    def test_claim_holder_falls_back_to_the_requester_profile(self):
        """With no delegated target, the requester's own profile carries the claim.

        A reservation for somebody else still blocks that fallback route, so the
        blocker check must resolve the same holder the claim path would.
        """
        self._requester_profile()
        asset = self._asset()
        AssetRequest._base_manager.create(
            tenant=self.tenant,
            requester=self.requester,
            asset_type=self.asset_type,
            asset=asset,
            status="approved",
        )
        AssetReservation._base_manager.create(
            asset=asset,
            reserved_for=self.second_holder,
            start_date=datetime.date.today() - datetime.timedelta(days=5),
            end_date=datetime.date.today() + datetime.timedelta(days=10),
            status=ReservationStatusChoices.ACTIVE,
        )
        with self.assertRaisesRegex(CommandError, "cannot check it out"):
            check_seed_operational_invariants()


def _log_status_change(asset, status_label, action, days_ago=0):
    """Write one change-log entry recording ``asset`` as being in ``status_label``."""
    content_type = ContentType.objects.get_for_model(Asset)
    ObjectChange._base_manager.create(
        tenant=asset.tenant,
        time=timezone.now() - datetime.timedelta(days=days_ago),
        action=action,
        changed_object_type=content_type,
        changed_object_id=asset.pk,
        object_repr=str(asset)[:200],
        object_type_repr=f"{content_type.app_label} | {content_type.model}",
        prechange_data={},
        # serialize_object stores relations as bare pks, so the recorded status is
        # the StatusLabel primary key.
        postchange_data={"status": status_label.pk},
        request_id=uuid.uuid4(),
    )


class SyncTenantLDAPDependencyTest(SimpleTestCase):
    @override_settings(
        ITAMBOX_TENANT_LDAP_CONFIGS={
            "test": {
                "SERVER_URI": "ldap://127.0.0.1",
                "BIND_DN": "cn=bind,dc=example,dc=test",
                "BIND_PASSWORD": "test",
                "USER_SEARCH_BASE": "ou=users,dc=example,dc=test",
                "USER_SEARCH_FILTER": "(uid=%(user)s)",
            }
        }
    )
    @patch("core.management.commands.sync_tenant_ldap.django_auth_ldap_installed", False)
    @patch("core.management.commands.sync_tenant_ldap.ldap.initialize")
    def test_sync_tenant_ldap_requires_locked_native_dependencies(self, mock_ldap_init):
        stdout = io.StringIO()
        command = SyncTenantLDAPCommand(stdout=stdout)
        with self.assertRaisesRegex(CommandError, "locked Linux/WSL or Docker environment"):
            command._run_sync(SimpleNamespace(pk=1, slug="test", name="Test"))
        self.assertNotIn("Connecting to LDAP server", stdout.getvalue())
        mock_ldap_init.assert_not_called()


class EventDispatchFailureTests(SimpleTestCase):
    @patch("extras.signals._table_exists", return_value=True)
    @patch("extras.signals.dispatch_event", side_effect=RuntimeError("sensitive backend detail"))
    def test_dispatch_failure_logs_error_with_identifiers_only(self, dispatch, _table_exists):
        with self.assertLogs("extras.signals", level="ERROR") as captured:
            _safe_dispatch(ChangeLoggingMixin, SimpleNamespace(pk=123), "delete")

        self.assertEqual(dispatch.call_count, 1)
        log = captured.output[0]
        self.assertIn("model=ChangeLoggingMixin", log)
        self.assertIn("object_id=123", log)
        self.assertIn("action=delete", log)
        self.assertIn("error_class=RuntimeError", log)
        self.assertNotIn("sensitive backend detail", log)
