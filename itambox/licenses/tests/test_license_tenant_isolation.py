from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assets.models import Manufacturer
from core.tests.mixins import grant
from licenses.models import License, LicenseSeatAssignment, LicenseTypeChoices
from organization.models import AssetHolder, Role, Tenant
from software.models import Software

User = get_user_model()


class LicenseSeatAssignmentCrossTenantTests(TestCase):
    """Phase 1 tenant-boundary coverage for LicenseSeatAssignment.

    LicenseSeatAssignment has no direct tenant FK; it is scoped through its
    parent License (tenant_lookup = 'license__tenant') by
    TenantScopingSoftDeleteManager, and exposes a `tenant` property so
    StrictTenantPermission can enforce the object-level boundary. These tests
    target the REST API, where both layers apply: a Tenant A seat must be
    invisible and immutable to a Tenant B member.
    """

    def setUp(self):
        # Two isolated tenants
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")

        # Users
        self.user_a = User.objects.create_user(username="user_a", password="password123")
        self.user_b = User.objects.create_user(username="user_b", password="password123")

        # Roles grant full CRUD on the seat assignment so the request reaches the
        # tenant-scoped queryset/object lookup (a 404 there, not a 403 from a
        # missing model permission, is what proves the boundary).
        seat_perms = [
            "licenses.view_licenseseatassignment",
            "licenses.change_licenseseatassignment",
            "licenses.delete_licenseseatassignment",
        ]
        self.role_a = Role.objects.create(tenant=self.tenant_a, name="Admin", permissions=seat_perms)
        self.membership_a = grant(self.user_a, self.tenant_a, self.role_a).membership
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="Admin", permissions=seat_perms)
        self.membership_b = grant(self.user_b, self.tenant_b, self.role_b).membership

        # Shared manufacturer for the software catalogue entries
        self.mfr = Manufacturer.objects.create(name="Microsoft", slug="microsoft")

        # Per-tenant software -> license -> seat assignment (to a holder)
        self.software_a = Software.objects.create(name="Office A", manufacturer=self.mfr, tenant=self.tenant_a)
        self.software_b = Software.objects.create(name="Office B", manufacturer=self.mfr, tenant=self.tenant_b)

        self.license_a = License.objects.create(
            name="License A",
            software=self.software_a,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant_a,
        )
        self.license_b = License.objects.create(
            name="License B",
            software=self.software_b,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant_b,
        )

        self.holder_a = AssetHolder.objects.create(
            first_name="Alice", last_name="A", upn="alice@a.example.com", tenant=self.tenant_a
        )
        self.holder_b = AssetHolder.objects.create(
            first_name="Bob", last_name="B", upn="bob@b.example.com", tenant=self.tenant_b
        )

        self.seat_a = LicenseSeatAssignment.objects.create(license=self.license_a, assigned_holder=self.holder_a)
        self.seat_b = LicenseSeatAssignment.objects.create(license=self.license_b, assigned_holder=self.holder_b)

    def _activate(self, user, tenant):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

    def test_list_excludes_other_tenant_seat(self):
        # Tenant B member lists seat assignments: only their own seat appears.
        self._activate(self.user_b, self.tenant_b)

        list_url = reverse("api:licenses_api:licenseseatassignment-list")
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, 200)

        data = response.json()
        results = data["results"] if isinstance(data, dict) and "results" in data else data
        returned_ids = {row["id"] for row in results}

        self.assertIn(self.seat_b.pk, returned_ids)
        self.assertNotIn(self.seat_a.pk, returned_ids)

    def test_cross_tenant_detail_is_404(self):
        # Tenant B member tries to read Tenant A's seat directly.
        self._activate(self.user_b, self.tenant_b)

        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat_a.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_patch_is_404_and_row_unchanged(self):
        self._activate(self.user_b, self.tenant_b)

        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat_a.pk})
        response = self.client.patch(detail_url, data={"notes": "hacked"}, content_type="application/json")
        self.assertEqual(response.status_code, 404)

        self.seat_a.refresh_from_db()
        self.assertNotEqual(self.seat_a.notes, "hacked")

    def test_cross_tenant_delete_is_404_and_row_persists(self):
        self._activate(self.user_b, self.tenant_b)

        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat_a.pk})
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, 404)

        # Row still exists (and is not soft-deleted).
        self.assertTrue(LicenseSeatAssignment.all_objects.filter(pk=self.seat_a.pk, deleted_at__isnull=True).exists())

    def test_own_tenant_detail_is_visible(self):
        # Sanity: the boundary does not over-block; own-tenant detail is 200.
        self._activate(self.user_b, self.tenant_b)

        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat_b.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.seat_b.pk)


class GlobalLicenseSeatResidualTests(TestCase):
    """H1: a seat assignment hanging off a GLOBAL (tenant=None) License must not
    leak across tenants.

    LicenseSeatAssignment scopes through ``tenant_lookup = 'license__tenant'``
    and does NOT opt into ``allow_global_tenant``, so children of a tenant=None
    License are default-DENY: invisible to any tenant member. The tenant-B role
    is granted the full seat CRUD permission set, so a 404 (not a 403) proves the
    boundary is SCOPING, not a missing model permission.
    """

    def setUp(self):
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")

        self.user_b = User.objects.create_user(username="user_b", password="password123")
        seat_perms = [
            "licenses.view_licenseseatassignment",
            "licenses.change_licenseseatassignment",
            "licenses.delete_licenseseatassignment",
        ]
        self.role_b = Role.objects.create(tenant=self.tenant_b, name="Admin", permissions=seat_perms)
        self.membership_b = grant(self.user_b, self.tenant_b, self.role_b).membership

        self.mfr = Manufacturer.objects.create(name="Microsoft", slug="microsoft")

        # GLOBAL software + license (tenant=None) and a seat on it.
        self.software_global = Software.objects.create(name="Office (global)", manufacturer=self.mfr, tenant=None)
        self.license_global = License.objects.create(
            name="Global License",
            software=self.software_global,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=None,
        )
        # Holder is global too so the seat row itself has no direct tenant FK.
        self.holder_global = AssetHolder.objects.create(
            first_name="Gina", last_name="Global", upn="gina@global.example.com", tenant=None
        )
        self.seat_global = LicenseSeatAssignment.objects.create(
            license=self.license_global, assigned_holder=self.holder_global
        )

    def _activate(self, user, tenant):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

    def test_global_seat_excluded_from_list(self):
        self._activate(self.user_b, self.tenant_b)
        list_url = reverse("api:licenses_api:licenseseatassignment-list")
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        results = data["results"] if isinstance(data, dict) and "results" in data else data
        returned_ids = {row["id"] for row in results}
        self.assertNotIn(self.seat_global.pk, returned_ids)

    def test_global_seat_detail_is_404(self):
        self._activate(self.user_b, self.tenant_b)
        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat_global.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 404)

    def test_global_seat_delete_is_404_and_row_persists(self):
        self._activate(self.user_b, self.tenant_b)
        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat_global.pk})
        response = self.client.delete(detail_url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            LicenseSeatAssignment.all_objects.filter(pk=self.seat_global.pk, deleted_at__isnull=True).exists()
        )


class NestedLicensePermissionTests(TestCase):
    """A seat assignment must not disclose license fields its reader cannot view.

    ``LicenseSeatAssignmentSerializer`` nests the full ``LicenseSerializer``.
    The assignment endpoint is gated by ``licenses.view_licenseseatassignment``,
    so without a nested-object check a seat-only role reads the licence's
    purchase cost, order number and notes even though the licence list, the
    licence detail page and the GraphQL licence roots all require
    ``licenses.view_license``.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Nested Tenant", slug="nested-tenant")
        self.seat_user = User.objects.create_user(username="seat-only", password="password123")
        self.license_user = User.objects.create_user(username="seat-and-license", password="password123")

        seat_perms = [
            "licenses.view_licenseseatassignment",
            "licenses.change_licenseseatassignment",
            "licenses.delete_licenseseatassignment",
        ]
        grant(
            self.seat_user,
            self.tenant,
            Role.objects.create(tenant=self.tenant, name="Seat Only", permissions=seat_perms),
        )
        grant(
            self.license_user,
            self.tenant,
            Role.objects.create(
                tenant=self.tenant,
                name="Seat And License",
                permissions=[*seat_perms, "licenses.view_license"],
            ),
        )

        manufacturer = Manufacturer.objects.create(name="Nested Maker", slug="nested-maker")
        software = Software.objects.create(name="Nested App", manufacturer=manufacturer, tenant=self.tenant)
        self.license = License.objects.create(
            name="Nested Confidential License",
            software=software,
            license_type=LicenseTypeChoices.PERPETUAL_SEAT,
            seats=10,
            tenant=self.tenant,
            purchase_cost=Decimal("1234.56"),
            order_number="PO-SECRET-42",
            notes="confidential renewal note",
        )
        holder = AssetHolder.objects.create(
            first_name="Nia", last_name="Nested", upn="nia@nested.example.com", tenant=self.tenant
        )
        self.seat = LicenseSeatAssignment.objects.create(license=self.license, assigned_holder=holder)

    def _activate(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    def test_assignment_detail_hides_license_fields_without_view_license(self):
        self._activate(self.seat_user)

        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)

        nested = response.json()["license"]
        self.assertIsNone(nested, "seat-only reader received the nested license object")

    def test_assignment_detail_keeps_license_fields_with_view_license(self):
        self._activate(self.license_user)

        detail_url = reverse("api:licenses_api:licenseseatassignment-detail", kwargs={"pk": self.seat.pk})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)

        nested = response.json()["license"]
        self.assertIsNotNone(nested)
        self.assertEqual(nested["purchase_cost"], "1234.56")
        self.assertEqual(nested["order_number"], "PO-SECRET-42")
        self.assertEqual(nested["notes"], "confidential renewal note")
