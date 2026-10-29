from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.choices import RequestStatusChoices
from assets.models import (
    Asset,
    AssetRequest,
    AssetRole,
    AssetTagSequence,
    AssetType,
    Category,
    Manufacturer,
    StatusLabel,
    Supplier,
)
from assets.services import checkout_asset
from assets.views.request_views import approve_asset_request, deny_asset_request
from core.managers import set_current_tenant
from core.models import ObjectChange
from core.tasks.context import TaskContext
from core.tests.mixins import grant
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
)
from organization.models import AssetHolder, Location, Role, Site, Tenant

User = get_user_model()


class RequisitionSystemTestCase(TestCase):
    def setUp(self):
        # Create Tenant
        self.tenant = Tenant.objects.create(name="Acme Corp", slug="acme")

        # Create users
        self.admin = User.objects.create_user(
            username="adminuser", password="password123", is_staff=True, is_superuser=True
        )
        self.staff = User.objects.create_user(
            username="staffuser", password="password123", is_staff=True, is_superuser=False
        )
        self.requester_user = User.objects.create_user(
            username="requesteruser", password="password123", is_staff=False, is_superuser=False
        )
        self.other_user = User.objects.create_user(
            username="otheruser", password="password123", is_staff=False, is_superuser=False
        )

        # Create Tenant Memberships
        self.role_standard = Role.objects.create(
            tenant=self.tenant,
            name="Standard Employee",
            permissions=[
                "assets.add_assetrequest",
                "assets.view_assetrequest",
                "inventory.add_accessoryassignment",
                "inventory.add_componentallocation",
                "inventory.add_consumableassignment",
            ],
        )
        self.role_delegated = Role.objects.create(
            tenant=self.tenant,
            name="Helpdesk Technician",
            permissions=["assets.add_assetrequest", "assets.view_assetrequest", "assets.add_delegated_assetrequest"],
        )

        grant(self.requester_user, self.tenant, self.role_standard)
        grant(self.other_user, self.tenant, self.role_delegated)

        # Set active tenant thread-local context
        set_current_tenant(self.tenant)

        # Create Manufacturer
        self.manufacturer = Manufacturer.objects.create(name="Lenovo", slug="lenovo")

        # Create Asset Role
        self.role = AssetRole.objects.create(name="Laptop", slug="laptop")

        # Create Status Labels
        self.status_deployable = StatusLabel.objects.create(
            name="Deployable", slug="deployable", type=StatusLabel.TYPE_DEPLOYABLE
        )
        self.status_deployed = StatusLabel.objects.create(
            name="Deployed", slug="deployed", type=StatusLabel.TYPE_DEPLOYED
        )

        # Create Asset Types
        self.type_requestable = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="ThinkPad T14",
            slug="thinkpad-t14",
            requestable=True,
            asset_role=self.role,
        )
        self.type_not_requestable = AssetType.objects.create(
            manufacturer=self.manufacturer,
            model="ThinkPad T16",
            slug="thinkpad-t16",
            requestable=False,
            asset_role=self.role,
        )

        # Create Assets
        self.asset_requestable = Asset.objects.create(
            name="ThinkPad T14-001",
            asset_tag="TAG-001",
            asset_type=self.type_requestable,
            asset_role=self.role,
            status=self.status_deployable,
            requestable=True,
            tenant=self.tenant,
        )
        self.asset_not_requestable = Asset.objects.create(
            name="ThinkPad T14-002",
            asset_tag="TAG-002",
            asset_type=self.type_requestable,
            asset_role=self.role,
            status=self.status_deployable,
            requestable=False,
            tenant=self.tenant,
        )
        # Asset that inherits True from type_requestable
        self.asset_inherited_requestable = Asset.objects.create(
            name="ThinkPad T14-003",
            asset_tag="TAG-003",
            asset_type=self.type_requestable,
            asset_role=self.role,
            status=self.status_deployable,
            requestable=None,
            tenant=self.tenant,
        )
        # Asset that inherits False from type_not_requestable
        self.asset_inherited_not_requestable = Asset.objects.create(
            name="ThinkPad T16-003",
            asset_tag="TAG-004",
            asset_type=self.type_not_requestable,
            asset_role=self.role,
            status=self.status_deployable,
            requestable=None,
            tenant=self.tenant,
        )
        # Asset that overrides type_not_requestable to True
        self.asset_override_requestable = Asset.objects.create(
            name="ThinkPad T16-004",
            asset_tag="TAG-005",
            asset_type=self.type_not_requestable,
            asset_role=self.role,
            status=self.status_deployable,
            requestable=True,
            tenant=self.tenant,
        )

        # Create Asset Holder Profile for the requester
        self.holder = AssetHolder.objects.create(
            user=self.requester_user,
            first_name="Jane",
            last_name="Requester",
            upn="jane@example.com",
            tenant=self.tenant,
        )

        # Create Site and Location for checkout targets
        self.site = Site.objects.create(name="Main HQ", slug="main-hq", tenant=self.tenant)
        self.location = Location.objects.create(name="Staging Room", slug="staging", site=self.site, tenant=self.tenant)

    def test_request_creation_and_constraints(self):
        # 1. Valid request for requestable type only
        req1 = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, notes="Need a development laptop"
        )
        self.assertEqual(req1.status, RequestStatusChoices.PENDING)

        # 2. Valid request for requestable asset only
        req2 = AssetRequest.objects.create(
            requester=self.requester_user, asset=self.asset_requestable, notes="Need a specific laptop"
        )
        self.assertEqual(req2.status, RequestStatusChoices.PENDING)

        # 3. Requesting an unrequestable asset type should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.requester_user, asset_type=self.type_not_requestable)

        # 4. Requesting an unrequestable asset should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.requester_user, asset=self.asset_not_requestable)

        # 5. Requesting an asset that doesn't match the asset type should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(
                requester=self.requester_user, asset_type=self.type_not_requestable, asset=self.asset_requestable
            )

        # 6. Requesting without both asset and asset type should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.requester_user)

    def test_auto_fulfillment_on_checkout(self):
        # Create a pending request for a generic type
        req = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, notes="Need a laptop"
        )

        self.assertEqual(req.status, RequestStatusChoices.PENDING)

        # Check out the asset to the requester (holder)
        # Using checkout_asset service which triggers signals
        checkout_asset(
            asset=self.asset_requestable, holder=self.holder, user=self.admin, notes="Checking out standard laptop"
        )

        # Refresh request from DB
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.FULFILLED)
        self.assertEqual(req.asset, self.asset_requestable)
        self.assertEqual(req.responded_by, self.admin)
        self.assertIsNotNone(req.response_date)
        self.assertIn("Automatically fulfilled", req.response_notes)

    def test_auto_fulfillment_on_specific_checkout(self):
        # Create a pending request for a specific asset
        req = AssetRequest.objects.create(
            requester=self.requester_user, asset=self.asset_requestable, notes="Need this exact laptop"
        )

        # Check out to holder
        checkout_asset(asset=self.asset_requestable, holder=self.holder, user=self.admin, notes="Here you go")

        # Refresh request from DB
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.FULFILLED)
        self.assertEqual(req.asset, self.asset_requestable)

    def test_approve_asset_request_workflow(self):
        req = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, notes="Laptop request"
        )

        # Staff user approves the request
        approve_asset_request(
            request_instance=req,
            user=self.staff,
            allocated_asset=self.asset_requestable,
            response_notes="Approved for collection",
        )

        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.APPROVED)
        self.assertEqual(req.responded_by, self.staff)
        self.assertEqual(req.asset, self.asset_requestable)
        self.assertEqual(req.response_notes, "Approved for collection")

    def test_deny_asset_request_workflow(self):
        req = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, notes="Laptop request"
        )

        # Staff user denies the request
        deny_asset_request(request_instance=req, user=self.staff, response_notes="Out of budget")

        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.DENIED)
        self.assertEqual(req.responded_by, self.staff)
        self.assertEqual(req.response_notes, "Out of budget")

    def test_views_endpoints(self):
        # Log in as admin
        self.client.login(username="adminuser", password="password123")

        # Superusers select a tenant via the session. Without active_tenant_id the
        # TenantMiddleware leaves active_tenant=None, so any AssetRequest created
        # during the view gets tenant=None. The subsequent
        # AssetRequest.objects.get(...) filters by the current tenant context
        # (self.tenant, set in setUp via set_current_tenant), which would exclude
        # null-tenant rows. Binding the session to self.tenant ensures the view
        # creates the request with the correct tenant.
        session = self.client.session
        session["active_tenant_id"] = str(self.tenant.id)
        session.save()

        # 1. Create a request via view POST
        post_data = {"asset_type": self.type_requestable.pk, "asset": "", "notes": "Requested via UI"}
        response = self.client.post(reverse("assets:request_create"), data=post_data)
        self.assertEqual(response.status_code, 302)  # Success redirect

        # Verify created
        req = AssetRequest.objects.get(notes="Requested via UI")
        self.assertEqual(req.requester, self.admin)  # Auto-set to current user

        # 2. Detail View
        response = self.client.get(reverse("assets:request_detail", kwargs={"pk": req.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Requested via UI")

        # 3. List View
        response = self.client.get(reverse("assets:request_list"))
        self.assertEqual(response.status_code, 200)

        # Log in as regular user
        self.client.login(username="requesteruser", password="password123")
        # 4. Regular user can cancel their request
        user_req = AssetRequest.objects.create(
            requester=self.requester_user,
            asset_type=self.type_requestable,
            notes="Self-cancel test",
            tenant=self.tenant,
        )
        response = self.client.post(reverse("assets:request_cancel", kwargs={"pk": user_req.pk}))
        self.assertEqual(response.status_code, 302)  # Redirects

        user_req.refresh_from_db()
        self.assertEqual(user_req.status, RequestStatusChoices.CANCELLED)

        # 5. Regular user cannot cancel other user's request
        other_type = AssetType.objects.create(
            manufacturer=self.manufacturer, model="ThinkPad X1", slug="thinkpad-x1", requestable=True
        )
        admin_req = AssetRequest.objects.create(
            requester=self.admin, asset_type=other_type, notes="Admin request", tenant=self.tenant
        )
        response = self.client.post(reverse("assets:request_cancel", kwargs={"pk": admin_req.pk}))
        self.assertEqual(response.status_code, 403)
        admin_req.refresh_from_db()
        self.assertNotEqual(admin_req.status, RequestStatusChoices.CANCELLED)

    def test_request_inheritance_and_overrides(self):
        # 1. Asset inheriting True should succeed
        req_inherited_ok = AssetRequest.objects.create(
            requester=self.requester_user,
            asset=self.asset_inherited_requestable,
            notes="Inherited requestable",
            tenant=self.tenant,
        )
        self.assertEqual(req_inherited_ok.status, RequestStatusChoices.PENDING)

        # 2. Asset inheriting False should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(
                requester=self.other_user, asset=self.asset_inherited_not_requestable, tenant=self.tenant
            )

        # 3. Asset overriding False to True should succeed
        req_override_ok = AssetRequest.objects.create(
            requester=self.other_user, asset=self.asset_override_requestable, tenant=self.tenant
        )
        self.assertEqual(req_override_ok.status, RequestStatusChoices.PENDING)

    def test_double_request_and_status_constraints(self):
        # 1. Create initial request
        AssetRequest.objects.create(requester=self.requester_user, asset=self.asset_requestable, tenant=self.tenant)

        # Double requesting same specific asset should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.requester_user, asset=self.asset_requestable, tenant=self.tenant)

        # Double requesting same type should fail
        AssetRequest.objects.create(requester=self.other_user, asset_type=self.type_requestable, tenant=self.tenant)
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.other_user, asset_type=self.type_requestable, tenant=self.tenant)

        # Requesting non-deployable asset should fail
        in_repair = StatusLabel.objects.create(name="In Repair 009", slug="in-repair-009", type="in_repair")
        non_deployable_asset = Asset.objects.create(
            name="Broken ThinkPad",
            asset_tag="TAG-009",
            asset_type=self.type_requestable,
            status=in_repair,
            requestable=True,
            tenant=self.tenant,
        )
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.other_user, asset=non_deployable_asset, tenant=self.tenant)

    def test_delegated_request_targets_and_constraints(self):
        # 1. Valid request delegated to user
        req_user = AssetRequest.objects.create(
            requester=self.other_user, asset_type=self.type_requestable, assigned_user=self.holder, tenant=self.tenant
        )
        self.assertEqual(req_user.assigned_target, self.holder)
        self.assertEqual(req_user.assigned_to_type, "assetholder")
        req_user.delete()

        # 2. Valid request delegated to location
        req_loc = AssetRequest.objects.create(
            requester=self.other_user,
            asset_type=self.type_requestable,
            assigned_location=self.location,
            tenant=self.tenant,
        )
        self.assertEqual(req_loc.assigned_target, self.location)
        self.assertEqual(req_loc.assigned_to_type, "location")
        req_loc.delete()

        # 3. Valid request delegated to parent asset
        req_asset = AssetRequest.objects.create(
            requester=self.other_user,
            asset_type=self.type_requestable,
            assigned_asset=self.asset_requestable,
            tenant=self.tenant,
        )
        self.assertEqual(req_asset.assigned_target, self.asset_requestable)
        self.assertEqual(req_asset.assigned_to_type, "asset")
        req_asset.delete()

        # 4. Request with multiple targets is refused by validation and by the CheckConstraint
        req_invalid = AssetRequest(
            requester=self.other_user,
            asset_type=self.type_requestable,
            assigned_user=self.holder,
            assigned_location=self.location,
            tenant=self.tenant,
        )
        with self.assertRaises(ValidationError):
            req_invalid.full_clean()
        # save() runs full_clean(); bulk_create() skips it, so this reaches the database constraint.
        with transaction.atomic(), self.assertRaisesRegex(IntegrityError, "at_most_one_request_target"):
            AssetRequest.objects.bulk_create([req_invalid])

    def test_delegation_permissions(self):
        from django.test import RequestFactory

        from assets.forms.request_forms import AssetRequestForm

        factory = RequestFactory()

        # Test form validation for standard user (cannot delegate)
        request = factory.post("/")
        request.user = self.requester_user

        form_data = {
            "asset_type": self.type_requestable.pk,
            "target_type": "location",
            "assigned_location": self.location.pk,
        }
        form = AssetRequestForm(data=form_data, request=request)
        self.assertFalse(form.is_valid())
        self.assertIn("target_type", form.errors)

        # Test form validation for delegated user (can delegate)
        request_delegated = factory.post("/")
        request_delegated.user = self.other_user
        form_delegated = AssetRequestForm(data=form_data, request=request_delegated)
        self.assertTrue(form_delegated.is_valid())

        # Test form validation for staff user (can delegate)
        request_staff = factory.post("/")
        request_staff.user = self.staff
        form_staff = AssetRequestForm(data=form_data, request=request_staff)
        self.assertTrue(form_staff.is_valid())

    def test_form_validation_reports_each_missing_delegated_target(self):
        from django.test import RequestFactory
        from model_bakery import baker

        from assets.forms.request_forms import AssetRequestForm

        component = baker.make("inventory.Component", tenant=self.tenant)
        factory = RequestFactory()
        request = factory.post("/")
        request.user = self.other_user

        cases = (
            (
                "multiple item types",
                {
                    "asset_type": self.type_requestable.pk,
                    "component": component.pk,
                },
                "A request can contain only one item type.",
                "__all__",
            ),
            (
                "asset holder",
                {"asset_type": self.type_requestable.pk, "target_type": "assetholder"},
                "Select an Asset Holder.",
                "assigned_user",
            ),
            (
                "location",
                {"asset_type": self.type_requestable.pk, "target_type": "location"},
                "Select a Location.",
                "assigned_location",
            ),
            (
                "parent asset",
                {"asset_type": self.type_requestable.pk, "target_type": "asset"},
                "Select a Parent Asset.",
                "assigned_asset",
            ),
        )

        for name, form_data, message, field in cases:
            with self.subTest(name=name):
                form = AssetRequestForm(data=form_data, request=request)
                self.assertFalse(form.is_valid())
                self.assertIn(message, form.errors[field])

    def test_prefilled_checkout_and_fulfillment(self):
        # Create an approved request delegated to a location
        req = AssetRequest.objects.create(
            requester=self.other_user,
            asset_type=self.type_requestable,
            assigned_location=self.location,
            status=RequestStatusChoices.APPROVED,
            asset=self.asset_requestable,
            tenant=self.tenant,
        )

        from django.test import RequestFactory

        from assets.views.asset_views import AssetCheckoutView

        factory = RequestFactory()

        request = factory.get(f"/?request_id={req.pk}")
        request.user = self.admin

        view = AssetCheckoutView()
        view.request = request
        view.kwargs = {"pk": self.asset_requestable.pk}

        kwargs = view.get_form_kwargs()
        self.assertIn("initial", kwargs)
        self.assertEqual(kwargs["initial"]["target_type"], "location")
        self.assertEqual(kwargs["initial"]["location"], self.location)

        # Test form submission auto-fulfills request
        post_data = {"target_type": "location", "location": self.location.pk, "notes": "Checkout notes"}
        post_request = factory.post(f"/?request_id={req.pk}", data=post_data, HTTP_HX_REQUEST="true")
        post_request.user = self.admin
        SessionMiddleware(lambda request: None).process_request(post_request)
        MessageMiddleware(lambda request: None).process_request(post_request)

        from assets.forms.checkout_forms import AssetCheckOutForm

        form = AssetCheckOutForm(data=post_data, asset=self.asset_requestable)
        self.assertTrue(form.is_valid())

        view_post = AssetCheckoutView()
        view_post.request = post_request
        view_post.kwargs = {"pk": self.asset_requestable.pk}

        response = view_post.form_valid(form)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], self.asset_requestable.get_absolute_url())

        # Check request status
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.FULFILLED)
        self.assertEqual(req.asset, self.asset_requestable)

    def test_self_service_claim(self):
        # 1. Successful claim by requester
        req = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, tenant=self.tenant
        )
        # Approve and allocate asset
        approve_asset_request(
            request_instance=req,
            user=self.staff,
            allocated_asset=self.asset_inherited_requestable,
            response_notes="Ready for pickup",
        )

        self.client.login(username="requesteruser", password="password123")
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req.pk}))
        self.assertEqual(response.status_code, 302)  # Redirects on success

        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.FULFILLED)
        self.assertEqual(req.responded_by, self.requester_user)
        self.assertIsNotNone(req.response_date)

        # Verify asset was checked out to the requester's profile
        self.asset_inherited_requestable.refresh_from_db()
        self.assertEqual(self.asset_inherited_requestable.status.type, "deployed")
        self.assertTrue(
            self.asset_inherited_requestable.assignments.filter(assigned_user=self.holder, is_active=True).exists()
        )

        # 2. Permission Denied if unauthorized user attempts to claim
        req_other = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, tenant=self.tenant
        )
        approve_asset_request(
            request_instance=req_other,
            user=self.staff,
            allocated_asset=self.asset_requestable,
            response_notes="Ready for pickup",
        )

        self.client.login(username="otheruser", password="password123")
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_other.pk}))
        self.assertEqual(response.status_code, 403)  # PermissionDenied -> 403 response

        req_other.refresh_from_db()
        self.assertEqual(req_other.status, RequestStatusChoices.APPROVED)

        # 3. Validation block on non-approved requests
        req_pending = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, tenant=self.tenant
        )
        # Temporarily assign an asset without changing status to approved
        req_pending.asset = self.asset_requestable
        req_pending.save()

        self.client.login(username="requesteruser", password="password123")
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_pending.pk}))
        self.assertEqual(response.status_code, 302)
        from django.contrib import messages

        messages_list = list(messages.get_messages(response.wsgi_request))
        self.assertTrue(any("Only approved requests can be claimed." in m.message for m in messages_list))

        # 4. Validation block if request has no asset allocated
        req_no_asset = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, tenant=self.tenant
        )
        req_no_asset.status = RequestStatusChoices.APPROVED
        req_no_asset.save()

        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_no_asset.pk}))
        self.assertEqual(response.status_code, 302)
        messages_list = list(messages.get_messages(response.wsgi_request))
        self.assertTrue(any("No asset has been allocated to this request." in m.message for m in messages_list))

        # 5. Validation block if requester has no asset holder profile
        new_user = User.objects.create_user(
            username="noprofileuser", password="password123", is_staff=False, is_superuser=False
        )
        grant(new_user, self.tenant, self.role_standard)
        req_no_profile = AssetRequest.objects.create(
            requester=new_user, asset_type=self.type_requestable, tenant=self.tenant
        )
        approve_asset_request(
            request_instance=req_no_profile,
            user=self.staff,
            allocated_asset=self.asset_requestable,
            response_notes="Ready for pickup",
        )

        self.client.login(username="noprofileuser", password="password123")
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_no_profile.pk}))
        self.assertEqual(response.status_code, 302)
        messages_list = list(messages.get_messages(response.wsgi_request))
        self.assertTrue(
            any(
                "Requester does not have an active Asset Holder profile to assign the asset to." in m.message
                for m in messages_list
            )
        )

        # 6. Successful claim by delegated assigned user
        req_delegated = AssetRequest.objects.create(
            requester=self.other_user, asset_type=self.type_requestable, assigned_user=self.holder, tenant=self.tenant
        )
        approve_asset_request(
            request_instance=req_delegated,
            user=self.staff,
            allocated_asset=self.asset_requestable,
            response_notes="Delegated approved",
        )

        # Log in as the delegated user (requester_user associated with self.holder)
        self.client.login(username="requesteruser", password="password123")
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_delegated.pk}))
        self.assertEqual(response.status_code, 302)

        req_delegated.refresh_from_db()
        self.assertEqual(req_delegated.status, RequestStatusChoices.FULFILLED)

        self.asset_requestable.refresh_from_db()
        self.assertTrue(self.asset_requestable.assignments.filter(assigned_user=self.holder, is_active=True).exists())

    def test_asset_detail_claim_and_checkout_buttons(self):
        # Grant view_asset permission to standard role for this test
        self.role_standard.permissions.append("assets.view_asset")
        self.role_standard.save()

        # 1. Requester without approved request visits asset page:
        # Should NOT see Check-out button (lacks assets.change_asset) and NOT see Claim button.
        self.client.login(username="requesteruser", password="password123")
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset_requestable.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Check Out...")
        self.assertNotContains(response, "Claim &amp; Confirm Pickup")

        # 2. Admin visits asset page:
        # Should see Check-out button (has permission) but NOT see Claim button.
        self.client.login(username="adminuser", password="password123")
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset_requestable.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Check Out...")
        self.assertNotContains(response, "Claim &amp; Confirm Pickup")

        # 3. Create approved request for requester allocating this asset
        req = AssetRequest.objects.create(
            requester=self.requester_user, asset_type=self.type_requestable, tenant=self.tenant
        )
        approve_asset_request(
            request_instance=req,
            user=self.admin,
            allocated_asset=self.asset_requestable,
            response_notes="Ready for pickup",
        )

        # 4. Requester visits asset page now:
        # Should NOT see Check-out button (still lacks permission) but MUST see Claim & Confirm Pickup button!
        self.client.login(username="requesteruser", password="password123")
        response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset_requestable.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Check Out...")
        self.assertContains(response, "Claim &amp; Confirm Pickup")

    def test_inventory_request_validation(self):
        # Setup inventory items
        comp_cat = Category.objects.create(name="Components", slug="components", applies_to={"component": True})
        acc_cat = Category.objects.create(name="Accessories", slug="accessories", applies_to={"accessory": True})

        comp = Component.objects.create(name="16GB DDR5 RAM", manufacturer=self.manufacturer, category=comp_cat)
        acc = Accessory.objects.create(name="USB-C Dock", manufacturer=self.manufacturer, category=acc_cat)

        # 1. Valid request for component
        req_comp = AssetRequest.objects.create(requester=self.requester_user, component=comp, qty=2, tenant=self.tenant)
        self.assertEqual(req_comp.status, RequestStatusChoices.PENDING)
        self.assertEqual(req_comp.qty, 2)

        # 2. Requesting multiple categories (e.g. component and accessory) should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(
                requester=self.requester_user, component=comp, accessory=acc, qty=1, tenant=self.tenant
            )

        # 3. Invalid quantity (<= 0) should fail
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.requester_user, component=comp, qty=0, tenant=self.tenant)

        # 4. Duplicate request check: duplicate pending should raise ValidationError
        with self.assertRaises(ValidationError):
            AssetRequest.objects.create(requester=self.requester_user, component=comp, qty=1, tenant=self.tenant)

    def test_unconfigured_auto_approval_leaves_request_pending(self):
        acc_cat = Category.objects.create(name="Accessories", slug="accessories", applies_to={"accessory": True})
        acc = Accessory.objects.create(name="Wireless Mouse", manufacturer=self.manufacturer, category=acc_cat)
        AccessoryStock.objects.create(accessory=acc, location=self.location, qty=10)

        request = AssetRequest.objects.create(requester=self.requester_user, accessory=acc, qty=2, tenant=self.tenant)

        self.assertEqual(request.status, RequestStatusChoices.PENDING)
        self.assertIsNone(request.response_date)
        self.assertEqual(request.response_notes, "")

    @override_settings(ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS={"accessory": 3, "consumable": 5})
    def test_auto_approval_thresholds(self):
        acc_cat = Category.objects.create(name="Accessories", slug="accessories", applies_to={"accessory": True})
        acc = Accessory.objects.create(name="Wireless Mouse", manufacturer=self.manufacturer, category=acc_cat)

        # Add stock (required for auto-approval check to succeed)
        AccessoryStock.objects.create(accessory=acc, location=self.location, qty=10)

        # Default limit for accessory is 3 (defined in save())
        # Qty = 2 (<= 3) -> should auto-approve
        req_approved = AssetRequest.objects.create(
            requester=self.requester_user, accessory=acc, qty=2, tenant=self.tenant
        )
        self.assertEqual(req_approved.status, RequestStatusChoices.APPROVED)
        self.assertIn("configured threshold", req_approved.response_notes)
        self.assertIn("sufficient stock", req_approved.response_notes)

        # Qty = 4 (> 3) -> should remain pending
        req_pending = AssetRequest.objects.create(requester=self.other_user, accessory=acc, qty=4, tenant=self.tenant)
        self.assertEqual(req_pending.status, RequestStatusChoices.PENDING)

    @override_settings(ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS={"accessory": 3})
    def test_automatic_approval_records_reason_and_attributed_object_change(self):
        category = Category.objects.create(
            name="Audited Accessories",
            slug="audited-accessories",
            applies_to={"accessory": True},
        )
        accessory = Accessory.objects.create(
            name="Audited Mouse",
            manufacturer=self.manufacturer,
            category=category,
        )
        AccessoryStock.objects.create(accessory=accessory, location=self.location, qty=10)

        with TaskContext(tenant_id=self.tenant.pk, user_id=self.admin.pk):
            request = AssetRequest.objects.create(
                requester=self.requester_user,
                accessory=accessory,
                qty=2,
                tenant=self.tenant,
            )

        change = ObjectChange._base_manager.get(
            changed_object_type__app_label="assets",
            changed_object_type__model="assetrequest",
            changed_object_id=request.pk,
        )
        self.assertEqual(request.status, RequestStatusChoices.APPROVED)
        self.assertIn("Automatically approved", request.response_notes)
        self.assertEqual(change.user, self.admin)
        self.assertEqual(change.tenant, self.tenant)
        self.assertEqual(change.postchange_data["status"], RequestStatusChoices.APPROVED)
        self.assertIn("Automatically approved", change.postchange_data["response_notes"])

    @override_settings(
        ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS=None,
        REQUISITION_AUTO_APPROVAL_THRESHOLDS={"accessory": 3, "consumable": 5},
    )
    def test_legacy_auto_approval_threshold_setting_remains_a_runtime_fallback(self):
        acc_cat = Category.objects.create(name="Accessories", slug="accessories", applies_to={"accessory": True})
        acc = Accessory.objects.create(name="Wireless Mouse", manufacturer=self.manufacturer, category=acc_cat)
        AccessoryStock.objects.create(accessory=acc, location=self.location, qty=10)

        request = AssetRequest.objects.create(requester=self.requester_user, accessory=acc, qty=2, tenant=self.tenant)

        self.assertEqual(request.status, RequestStatusChoices.APPROVED)

    def test_partial_approval_and_location(self):
        from assets.forms.request_forms import AssetRequestActionForm

        comp_cat = Category.objects.create(name="Components", slug="components", applies_to={"component": True})
        comp = Component.objects.create(name="512GB SSD", manufacturer=self.manufacturer, category=comp_cat)

        req = AssetRequest.objects.create(requester=self.requester_user, component=comp, qty=5, tenant=self.tenant)

        # 1. Action form validation: requires stock location for inventory items
        form = AssetRequestActionForm(data={"qty": 3, "response_notes": "Partial approval"}, request_instance=req)
        self.assertFalse(form.is_valid())
        self.assertIn("allocated_location", form.errors)

        # 2. Action form validation: approved qty cannot exceed requested qty
        form = AssetRequestActionForm(
            data={"allocated_location": self.location.pk, "qty": 6, "response_notes": "Invalid quantity"},
            request_instance=req,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("qty", form.errors)

        # 3. Valid partial approval
        form = AssetRequestActionForm(
            data={"allocated_location": self.location.pk, "qty": 3, "response_notes": "Reduced to 3 units"},
            request_instance=req,
        )
        self.assertTrue(form.is_valid())

        approve_asset_request(
            request_instance=req,
            user=self.staff,
            allocated_location=self.location,
            qty=3,
            response_notes="Reduced to 3 units",
        )
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatusChoices.APPROVED)
        self.assertEqual(req.qty, 3)
        self.assertEqual(req.source_location, self.location)

    def test_claiming_inventory_items(self):
        comp_cat = Category.objects.create(name="Components", slug="components", applies_to={"component": True})
        acc_cat = Category.objects.create(name="Accessories", slug="accessories", applies_to={"accessory": True})
        cons_cat = Category.objects.create(name="Consumables", slug="consumables", applies_to={"consumable": True})

        comp = Component.objects.create(name="GPU RTX 4070", manufacturer=self.manufacturer, category=comp_cat)
        acc = Accessory.objects.create(name="Keyboard", manufacturer=self.manufacturer, category=acc_cat)
        cons = Consumable.objects.create(name="AAA Battery", manufacturer=self.manufacturer, category=cons_cat)

        # Add stock
        comp_stock = ComponentStock.objects.create(component=comp, location=self.location, qty=5)
        acc_stock = AccessoryStock.objects.create(accessory=acc, location=self.location, qty=10)
        cons_stock = ConsumableStock.objects.create(consumable=cons, location=self.location, qty=20)

        # 1. Component Claim
        req_comp = AssetRequest.objects.create(
            requester=self.requester_user,
            component=comp,
            qty=2,
            status=RequestStatusChoices.APPROVED,
            source_location=self.location,
            tenant=self.tenant,
        )
        self.client.login(username="requesteruser", password="password123")
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_comp.pk}))
        self.assertEqual(response.status_code, 302)

        req_comp.refresh_from_db()
        self.assertEqual(req_comp.status, RequestStatusChoices.FULFILLED)
        self.assertTrue(ComponentAllocation.objects.filter(component=comp, assigned_holder=self.holder, qty=2).exists())
        comp_stock.refresh_from_db()
        self.assertEqual(comp_stock.qty, 3)  # 5 - 2 = 3

        # 2. Accessory Claim
        req_acc = AssetRequest.objects.create(
            requester=self.requester_user,
            accessory=acc,
            qty=3,
            status=RequestStatusChoices.APPROVED,
            source_location=self.location,
            tenant=self.tenant,
        )
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_acc.pk}))
        self.assertEqual(response.status_code, 302)

        req_acc.refresh_from_db()
        self.assertEqual(req_acc.status, RequestStatusChoices.FULFILLED)
        self.assertTrue(AccessoryAssignment.objects.filter(accessory=acc, assigned_holder=self.holder, qty=3).exists())
        acc_stock.refresh_from_db()
        self.assertEqual(acc_stock.qty, 7)  # 10 - 3 = 7

        # 3. Consumable Claim
        req_cons = AssetRequest.objects.create(
            requester=self.requester_user,
            consumable=cons,
            qty=5,
            status=RequestStatusChoices.APPROVED,
            source_location=self.location,
            tenant=self.tenant,
        )
        response = self.client.post(reverse("assets:request_claim", kwargs={"pk": req_cons.pk}))
        self.assertEqual(response.status_code, 302)

        req_cons.refresh_from_db()
        self.assertEqual(req_cons.status, RequestStatusChoices.FULFILLED)
        self.assertTrue(
            ConsumableAssignment.objects.filter(consumable=cons, assigned_holder=self.holder, qty=5).exists()
        )
        cons_stock.refresh_from_db()
        self.assertEqual(cons_stock.qty, 15)  # 20 - 5 = 15

    def test_asset_type_bulk_request_splitting(self):
        self.client.login(username="requesteruser", password="password123")

        post_data = {
            "request_category": "asset_type",
            "asset_type": self.type_requestable.pk,
            "qty": 3,
            "notes": "Need 3 laptops for team",
        }

        response = self.client.post(reverse("assets:assetrequest_create"), data=post_data)
        self.assertEqual(response.status_code, 302)  # Redirects on success

        # Verify 3 separate request rows were created, each with qty=1
        new_requests = AssetRequest.objects.filter(
            asset_type=self.type_requestable, notes="Need 3 laptops for team", parent__isnull=False
        )
        self.assertEqual(new_requests.count(), 3)
        for req in new_requests:
            self.assertEqual(req.qty, 1)
            self.assertEqual(req.requester, self.requester_user)
            self.assertEqual(req.status, RequestStatusChoices.PENDING)

    def test_request_bulk_receive_workflow(self):
        # Create approved requests for AssetType
        self.client.login(username="adminuser", password="password123")

        # Create 2 approved requests for laptops (AssetType)
        req1 = AssetRequest.objects.create(
            requester=self.requester_user,
            asset_type=self.type_requestable,
            status=RequestStatusChoices.APPROVED,
            tenant=self.tenant,
        )
        req2 = AssetRequest.objects.create(
            requester=self.other_user,
            asset_type=self.type_requestable,
            status=RequestStatusChoices.APPROVED,
            tenant=self.tenant,
        )

        # Get initial next expected tag sequence value
        dummy = Asset(tenant=self.tenant, asset_type=self.type_requestable)
        seq = AssetTagSequence.resolve_sequence_for_asset(dummy)
        next_tag_val = seq.next_value

        # Create a test supplier
        supplier = Supplier.objects.create(name="Bechtle GmbH", slug="bechtle")

        # Test initial load / bulk-receive endpoint with POST of PKs
        post_pks = {"pk": [req1.pk, req2.pk]}
        url = reverse("assets:request_bulk_receive")
        response = self.client.post(url, data=post_pks)
        self.assertEqual(response.status_code, 200)  # Formset rendering
        self.assertContains(response, f"{seq.prefix}{next_tag_val:0{seq.zero_padding}d}")
        self.assertContains(response, f"{seq.prefix}{next_tag_val + 1:0{seq.zero_padding}d}")

        # Test valid submission of the formset
        status = StatusLabel.objects.filter(type="deployable").first()

        formset_data = {
            "form-TOTAL_FORMS": 2,
            "form-INITIAL_FORMS": 0,
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            # Form 0
            "form-0-request_id": req1.pk,
            "form-0-asset_tag": f"{seq.prefix}{next_tag_val:0{seq.zero_padding}d}",
            "form-0-serial_number": "SERIAL-REC-1111",
            "form-0-name": "HP EliteBook 860 G11",
            "form-0-status": status.pk,
            "form-0-location": self.location.pk,
            "form-0-supplier": supplier.pk,
            "form-0-order_number": "ORD-999-AA",
            "form-0-purchase_cost": "1250.00",
            "form-0-purchase_date": "2026-06-01",
            # Form 1
            "form-1-request_id": req2.pk,
            "form-1-asset_tag": f"{seq.prefix}{next_tag_val + 1:0{seq.zero_padding}d}",
            "form-1-serial_number": "SERIAL-REC-2222",
            "form-1-name": "HP EliteBook 860 G11",
            "form-1-status": status.pk,
            "form-1-location": self.location.pk,
            "form-1-supplier": supplier.pk,
            "form-1-order_number": "ORD-999-AA",
            "form-1-purchase_cost": "1250.00",
            "form-1-purchase_date": "2026-06-01",
            "submit": "Receive & Allocate Stock",
        }

        response = self.client.post(url, data=formset_data)
        self.assertEqual(response.status_code, 302)  # Success redirect

        # Verify assets are created in the database and linked to the requests
        req1.refresh_from_db()
        req2.refresh_from_db()

        self.assertIsNotNone(req1.asset)
        self.assertEqual(req1.asset.serial_number, "SERIAL-REC-1111")
        self.assertEqual(req1.asset.asset_tag, f"{seq.prefix}{next_tag_val:0{seq.zero_padding}d}")
        self.assertEqual(req1.asset.status, status)
        self.assertEqual(req1.asset.asset_role, self.role)
        self.assertEqual(req1.asset.supplier, supplier)
        self.assertEqual(req1.asset.order_number, "ORD-999-AA")
        self.assertEqual(req1.asset.purchase_cost, Decimal("1250.00"))
        self.assertEqual(req1.asset.purchase_date.strftime("%Y-%m-%d"), "2026-06-01")

        self.assertIsNotNone(req2.asset)
        self.assertEqual(req2.asset.serial_number, "SERIAL-REC-2222")
        self.assertEqual(req2.asset.asset_tag, f"{seq.prefix}{next_tag_val + 1:0{seq.zero_padding}d}")
        self.assertEqual(req2.asset.status, status)
        self.assertEqual(req2.asset.asset_role, self.role)
        self.assertEqual(req2.asset.supplier, supplier)
        self.assertEqual(req2.asset.order_number, "ORD-999-AA")
        self.assertEqual(req2.asset.purchase_cost, Decimal("1250.00"))
        self.assertEqual(req2.asset.purchase_date.strftime("%Y-%m-%d"), "2026-06-01")

        # Verify the sequence was incremented
        seq.refresh_from_db()
        self.assertEqual(seq.next_value, next_tag_val + 2)

        # Test validation failures - duplicate serial number
        req3 = AssetRequest.objects.create(
            requester=self.requester_user,
            asset_type=self.type_requestable,
            status=RequestStatusChoices.APPROVED,
            tenant=self.tenant,
        )

        formset_data_duplicate = {
            "form-TOTAL_FORMS": 1,
            "form-INITIAL_FORMS": 0,
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "form-0-request_id": req3.pk,
            "form-0-asset_tag": f"{seq.prefix}{next_tag_val + 2:0{seq.zero_padding}d}",
            "form-0-serial_number": "SERIAL-REC-1111",  # Existing serial!
            "form-0-name": "HP EliteBook 860 G11",
            "form-0-status": status.pk,
            "form-0-location": self.location.pk,
            "submit": "Receive & Allocate Stock",
        }

        response = self.client.post(url, data=formset_data_duplicate)
        self.assertEqual(response.status_code, 200)  # Returns form with validation errors
        self.assertContains(response, "Asset with this serial number already exists.")

    def test_bulk_receive_blank_order_number_succeeds(self):
        """Regression: blank order_number (required=False in form) must store '' not NULL."""
        self.client.login(username="adminuser", password="password123")

        req = AssetRequest.objects.create(
            requester=self.requester_user,
            asset_type=self.type_requestable,
            status=RequestStatusChoices.APPROVED,
            tenant=self.tenant,
        )
        status = StatusLabel.objects.filter(type="deployable").first()
        supplier = Supplier.objects.create(name="Blank Order Supplier", slug="blank-order-supplier")

        dummy = Asset(tenant=self.tenant, asset_type=self.type_requestable)
        seq = AssetTagSequence.resolve_sequence_for_asset(dummy)
        next_tag = f"{seq.prefix}{seq.next_value:0{seq.zero_padding}d}"

        formset_data = {
            "form-TOTAL_FORMS": 1,
            "form-INITIAL_FORMS": 0,
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "form-0-request_id": req.pk,
            "form-0-asset_tag": next_tag,
            "form-0-serial_number": "BLANK-ORDER-SN-001",
            "form-0-name": "Blank Order Test",
            "form-0-status": status.pk,
            "form-0-location": self.location.pk,
            "form-0-supplier": supplier.pk,
            "form-0-order_number": "",  # optional field — must store '' not raise IntegrityError
            "form-0-purchase_cost": "999.00",
            "form-0-purchase_date": "2026-06-11",
            "submit": "Receive & Allocate Stock",
        }
        bulk_url = reverse("assets:request_bulk_receive")
        response = self.client.post(bulk_url, data=formset_data)
        self.assertEqual(response.status_code, 302, f"Expected redirect on success; got {response.status_code}")

        req.refresh_from_db()
        self.assertIsNotNone(req.asset)
        self.assertEqual(req.asset.order_number, "")
