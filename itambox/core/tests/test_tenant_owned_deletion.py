import io
import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset, AssetAssignment, AssetMaintenance, AssetRequest, AssetType, StatusLabel
from assets.services import checkin_asset, checkout_asset
from compliance.models import CustodyReceipt
from core import identity_provisioning
from core.auth import ldap as ldap_module
from core.auth.ldap import MultiTenantLDAPBackend
from core.auth.saml import TenantSaml2Backend
from core.managers import set_current_tenant
from core.tests.mixins import grant
from core.validators import validate_file_attachment, validate_image_attachment
from licenses.models import License, LicenseSeatAssignment
from organization.models import AssetHolder, Membership, Role, Tenant
from organization.services.identity_provisioning import organization_identity_provisioner

User = get_user_model()


class MitigationsPhase4Tests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", email="test@example.com", password="password123")
        self.tenant = Tenant.objects.create(name="Primary Tenant", slug="primary-tenant")
        self.other_tenant = Tenant.objects.create(name="Other Tenant", slug="other-tenant")

        self.asset_holder = AssetHolder.objects.create(
            user=self.user,
            first_name="Test",
            last_name="User",
            upn="test@example.com",
            email="test@example.com",
            tenant=self.tenant,
        )

        self.status = StatusLabel.objects.create(name="Ready", slug="ready", type=StatusLabel.TYPE_DEPLOYABLE)
        self.deployed_status = StatusLabel.objects.create(
            name="Phase4 In Use", slug="phase4-in-use", type=StatusLabel.TYPE_DEPLOYED
        )

        self.asset_type = baker.make(
            AssetType, manufacturer__name="Mfg", model="Model", slug="mfg-model", requestable=True
        )

        self.asset = Asset.objects.create(
            name="Test Asset",
            asset_tag="ASSET-1001",
            asset_type=self.asset_type,
            status=self.status,
            tenant=self.tenant,
        )

    def tearDown(self):
        set_current_tenant(None)

    def test_deletion_cascades_assets_and_requests(self):
        # 1. AssetAssignment SET_NULL check
        checkout_asset(self.asset, holder=self.asset_holder, user=self.user, status=self.deployed_status)
        assignment = AssetAssignment.objects.get(asset=self.asset, is_active=True)
        # Delete the asset holder using force_hard_delete to trigger DB cascade/SET_NULL
        self.asset_holder.delete(force_hard_delete=True)
        # Verify assignment is not deleted, but assigned_user is SET_NULL
        assignment.refresh_from_db()
        self.assertIsNone(assignment.assigned_user_id)
        self.assertEqual(assignment.asset, self.asset)
        checkin_asset(self.asset, user=self.user)

        # Re-create asset holder for request test
        self.asset_holder = AssetHolder.objects.create(
            user=self.user,
            first_name="Test",
            last_name="User",
            upn="test@example.com",
            email="test@example.com",
            tenant=self.tenant,
        )

        # 2. AssetRequest SET_NULL check
        req = AssetRequest.objects.create(requester=self.user, asset=self.asset, assigned_user=self.asset_holder, qty=1)
        # Delete the asset holder
        self.asset_holder.delete(force_hard_delete=True)
        req.refresh_from_db()
        self.assertIsNone(req.assigned_user_id)
        self.assertEqual(req.asset, self.asset)

    def test_deletion_cascades_license_seat_assignments(self):
        software = baker.make("software.Software")
        lic = License.objects.create(name="Test License", software=software, seats=10, tenant=self.tenant)
        seat = LicenseSeatAssignment.objects.create(license=lic, asset=self.asset)

        # Delete asset using force_hard_delete
        self.asset.delete(force_hard_delete=True)

        # The seat was assigned to that asset; hard-deleting the asset releases
        # the seat (CASCADE) so it returns to the available pool. Leaving an
        # asset+holder-null seat would violate chk_assignment_to_one_target.
        self.assertFalse(LicenseSeatAssignment.objects.filter(pk=seat.pk).exists())
        # The license itself is untouched (PROTECT guards it separately).
        self.assertTrue(License.objects.filter(pk=lic.pk).exists())

    def test_deletion_cascades_protected_compliance_data(self):
        # CustodyReceipt protects asset and holder
        receipt = CustodyReceipt.objects.create(
            asset=self.asset, holder=self.asset_holder, acceptance_status=CustodyReceipt.STATUS_ACCEPTED
        )

        with self.assertRaises(ProtectedError):
            self.asset.delete(force_hard_delete=True)

        with self.assertRaises(ProtectedError):
            self.asset_holder.delete(force_hard_delete=True)

        receipt.delete(force_hard_delete=True)

        # AssetMaintenance protects asset
        AssetMaintenance.objects.create(asset=self.asset, start_date="2026-01-01")

        with self.assertRaises(ProtectedError):
            self.asset.delete(force_hard_delete=True)

    def test_magic_byte_validation_dangerous_files(self):
        # 1. Dangerous extension
        dangerous_file = SimpleUploadedFile("danger.exe", b"MZ" + b"\x00" * 120)
        with self.assertRaises(ValidationError):
            validate_file_attachment(dangerous_file)

        # Exercise MIME classification deterministically on every supported
        # development platform. Native Windows intentionally omits libmagic.
        magic = SimpleNamespace(
            from_buffer=lambda chunk, mime: "application/x-dosexec" if chunk.startswith(b"MZ") else "application/pdf"
        )
        with patch.dict(sys.modules, {"magic": magic}):
            # 2. Spoofed extension with dangerous magic bytes (e.g. exe bytes inside .txt)
            spoofed_file = SimpleUploadedFile("safe.txt", b"MZ" + b"\x00" * 120)
            with self.assertRaises(ValidationError):
                validate_file_attachment(spoofed_file)

            # 3. Clean file attachment
            clean_file = SimpleUploadedFile("clean.pdf", b"%PDF-1.4\n...")
            # Should not raise ValidationError
            validate_file_attachment(clean_file)

    def test_magic_byte_validation_images(self):
        # 1. Safe PNG image
        from PIL import Image

        png_buffer = io.BytesIO()
        Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(png_buffer, format="PNG")
        png_data = png_buffer.getvalue()
        safe_image = SimpleUploadedFile("image.png", png_data)
        # Should not raise
        validate_image_attachment(safe_image)

        # 2. Fake image extension but actually dangerous file
        fake_image = SimpleUploadedFile("image.png", b"MZ\x90\x00\x03\x00\x00\x00")
        with self.assertRaises(ValidationError):
            validate_image_attachment(fake_image)

    def test_bulk_edit_tenant_validation(self):
        from itambox.views.generic import ObjectBulkEditView

        # Create a staff user who has permission to change assets on self.tenant, but NOT on self.other_tenant
        staff_user = User.objects.create_user(username="staff", email="staff@example.com", password="password123")
        role = Role.objects.create(tenant=self.tenant, name="Tenant Staff", permissions=["assets.change_asset"])
        grant(staff_user, self.tenant, role)

        factory = RequestFactory()

        # Test post bulk edit moving asset to self.other_tenant
        # We need to simulate the view call
        set_current_tenant(self.tenant)

        view = ObjectBulkEditView()
        view.queryset = Asset.objects.all()
        view.request = factory.post(
            reverse("assets:asset_bulk_edit"),
            {"pk": [self.asset.pk], "_selected_fields": ["tenant"], "tenant": self.other_tenant.pk, "_apply": "Apply"},
        )
        view.request.user = staff_user
        view.request.session = {}
        # Enable messages framework for the test request
        from django.contrib.messages.storage.fallback import FallbackStorage

        view.request._messages = FallbackStorage(view.request)

        # Perform request dispatch / post call
        response = view.post(view.request)

        # Verify validation error is added to form and edit is blocked
        # Form should be re-rendered with errors, not a redirect to return_url
        self.assertEqual(response.status_code, 200)
        self.assertIn("tenant", response.context_data["form"].errors)

    def test_ldap_user_profile_and_membership_syncing(self):
        backend = MultiTenantLDAPBackend()

        # Set active tenant context
        set_current_tenant(self.tenant)

        # Create a new user who doesn't have an asset holder profile yet
        ldap_user = User.objects.create_user(username="ldapuser", email="ldap@example.com", password="password123")

        # Mock ldap_user object with attributes
        class MockLDAPUser:
            def __init__(self):
                self.attrs = {
                    "mail": [b"ldap@example.com"],
                    "givenName": [b"Ldap"],
                    "sn": [b"User"],
                    "userPrincipalName": [b"ldap@example.com"],
                }
                self.group_names = ["AdminLDAPGroup"]
                self.group_dns = []

        ldap_user.ldap_user = MockLDAPUser()

        # Exercise the real public backend path with a deterministic parent
        # result; no external LDAP connection is made by this test.
        ldap_configs = {
            self.tenant.slug: {
                "USER_SEARCH": {
                    "base_dn": "ou=users,dc=example,dc=test",
                    "filter": "(uid=%(user)s)",
                },
                "LDAP_GROUP_ROLE_MAPPING": {"AdminLDAPGroup": "admin"},
            }
        }

        with (
            identity_provisioning.override_identity_provisioner(organization_identity_provisioner),
            patch.object(ldap_module, "django_auth_ldap_installed", True),
            patch.object(ldap_module.LDAPBackend, "authenticate", return_value=ldap_user) as parent_authenticate,
            override_settings(
                ITAMBOX_TENANT_LDAP_CONFIGS=ldap_configs,
                AUTH_LDAP_USER_SEARCH=None,
                AUTH_LDAP_USER_DN_TEMPLATE=None,
            ),
        ):
            result = backend.authenticate(
                request=None,
                username=ldap_user.username,
                password="directory-password",
            )

        self.assertIs(result, ldap_user)
        parent_authenticate.assert_called_once()

        # Verify AssetHolder profile is created
        holder = AssetHolder.objects.get(user=ldap_user)
        self.assertEqual(holder.email, "ldap@example.com")
        self.assertEqual(holder.first_name, "Ldap")
        self.assertEqual(holder.last_name, "User")
        self.assertEqual(holder.tenant, self.tenant)

        # Verify Membership is provisioned with proper Admin role
        membership = Membership.objects.get(user=ldap_user, tenant=self.tenant)
        self.assertEqual(membership.role_grants.first().role.name, "Admin")

    def test_saml_user_profile_and_membership_syncing(self):
        backend = TenantSaml2Backend()
        set_current_tenant(self.tenant)

        saml_user = User.objects.create_user(username="samluser", email="saml@example.com", password="password123")

        session_info = {
            "ava": {"mail": ["saml@example.com"], "givenName": ["Saml"], "sn": ["User"], "groups": ["ManagerSAMLGroup"]}
        }

        from django.test import override_settings

        saml_configs = {self.tenant.slug: {"SAML_GROUP_ROLE_MAPPING": {"ManagerSAMLGroup": "manager"}}}

        with override_settings(ITAMBOX_TENANT_SAML_CONFIGS=saml_configs):
            with identity_provisioning.override_identity_provisioner(organization_identity_provisioner):
                backend.sync_saml_user_profile_and_memberships(saml_user, session_info)

        # Verify AssetHolder profile is created
        holder = AssetHolder.objects.get(user=saml_user)
        self.assertEqual(holder.email, "saml@example.com")
        self.assertEqual(holder.first_name, "Saml")

        # Verify Membership is provisioned with Manager role
        membership = Membership.objects.get(user=saml_user, tenant=self.tenant)
        self.assertEqual(membership.role_grants.first().role.name, "Manager")
