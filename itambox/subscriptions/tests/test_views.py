import re
from datetime import date
from decimal import Decimal

import html5lib
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset, StatusLabel, Supplier
from extras.models import JournalEntry
from licenses.models import License
from organization.models import AssetHolder, Location, Site, Tenant, TenantGroup
from software.models import Software
from subscriptions.models import (
    BillingCycleChoices,
    Subscription,
    SubscriptionAssignment,
    SubscriptionStatusChoices,
    SubscriptionTypeChoices,
)

User = get_user_model()


class SubscriptionViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="testuser", password="testpass", is_staff=True, is_superuser=True)
        self.client.login(username="testuser", password="testpass")
        self.supplier = Supplier.objects.create(name="Test Supplier")
        self.sub = Subscription.objects.create(
            name="Test Subscription",
            supplier=self.supplier,
            type=SubscriptionTypeChoices.SAAS,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_cost=999.99,
            currency="EUR",
            billing_cycle=BillingCycleChoices.ANNUAL,
            licensed_quantity=50,
        )

    def test_list_view_status_200(self):
        url = reverse("subscriptions:subscription_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Subscription")

    def test_detail_view_status_200(self):
        url = reverse("subscriptions:subscription_detail", kwargs={"pk": self.sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Subscription")
        self.assertContains(resp, "999.99")
        self.assertContains(resp, "EUR")

    def test_detail_links_to_the_supplier(self):
        url = reverse("subscriptions:subscription_detail", kwargs={"pk": self.sub.pk})
        response = self.client.get(url)
        supplier_url = reverse("assets:supplier_detail", kwargs={"pk": self.supplier.pk})

        self.assertEqual(response.status_code, 200)
        self.assertIn('<a href="' + supplier_url + '">Test Supplier</a>', response.content.decode())

    def test_detail_renders_seat_usage_with_one_assignment_count_query(self):
        url = reverse("subscriptions:subscription_detail", kwargs={"pk": self.sub.pk})
        with CaptureQueriesContext(connection) as queries:
            resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        seat_queries = [query for query in queries.captured_queries if "licenseseatassignment" in query["sql"].lower()]
        self.assertEqual(len(seat_queries), 1, queries.captured_queries)

    def _detail_content(self, sub):
        url = reverse("subscriptions:subscription_detail", kwargs={"pk": sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def _row_value(self, content, label):
        """Return the <dd> body rendered after the given labeled <dt>."""
        match = re.search(re.escape(label) + r"\s*</dt>\s*<dd[^>]*>(.*?)</dd>", content, re.S)
        self.assertIsNotNone(match, f"no rendered row for label {label!r}")
        return match.group(1)

    def _row_text(self, content, label):
        """Tag-stripped text of the <dd> rendered after the given labeled <dt>."""
        return re.sub(r"<[^>]+>", "", self._row_value(content, label)).strip()

    def _seat_matrix_subscription(self, licensed_quantity):
        return Subscription.objects.create(
            name=f"Seat Matrix {licensed_quantity}",
            supplier=self.supplier,
            type=SubscriptionTypeChoices.SAAS,
            status=SubscriptionStatusChoices.ACTIVE,
            licensed_quantity=licensed_quantity,
        )

    def test_detail_labels_one_time_cost_and_omits_annual_row(self):
        """Issue #501: a one-time payment must not read as a yearly cost."""
        sub = Subscription.objects.create(
            name="Perpetual Software",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_cost=Decimal("500.00"),
            currency="EUR",
            billing_cycle=BillingCycleChoices.ONETIME,
        )
        content = self._detail_content(sub)

        self.assertIn("One-Time Cost:", content)
        self.assertIn("500.00 EUR", self._row_value(content, "One-Time Cost:"))
        self.assertNotIn("Renewal Cost:", content)
        self.assertNotIn("Est. Annual Cost:", content)
        self.assertNotIn("Annualized Cost:", content)

    def test_detail_labels_multi_year_annual_cost_as_annualized(self):
        """Issue #501: the multi-year figure in the annual slot is the annualized value."""
        sub = Subscription.objects.create(
            name="Three-Year Contract",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_cost=Decimal("3600.00"),
            currency="EUR",
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
            term_months=36,
        )
        content = self._detail_content(sub)

        self.assertIn("3600.00 EUR", self._row_value(content, "Renewal Cost:"))
        self.assertIn("1200.00 EUR", self._row_value(content, "Annualized Cost:"))
        self.assertNotIn("Est. Annual Cost:", content)

    def test_detail_omits_annual_row_for_multi_year_without_term(self):
        """Issue #501 fallback: no yearly figure when the term is unknown."""
        sub = Subscription.objects.create(
            name="Termless Multi-Year",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_cost=Decimal("3600.00"),
            currency="EUR",
            billing_cycle=BillingCycleChoices.MULTI_YEAR,
        )
        content = self._detail_content(sub)

        self.assertIn("3600.00 EUR", self._row_value(content, "Renewal Cost:"))
        self.assertNotIn("Annualized Cost:", content)
        self.assertNotIn("Est. Annual Cost:", content)

    def test_detail_renders_zero_cost_as_zero(self):
        """Issue #501: a zero-cost row shows 0.00 instead of being dropped or showing Not set."""
        sub = Subscription.objects.create(
            name="Free Annual Plan",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_cost=Decimal("0.00"),
            currency="EUR",
            billing_cycle=BillingCycleChoices.ANNUAL,
        )
        content = self._detail_content(sub)

        self.assertIn("0.00 EUR", self._row_value(content, "Renewal Cost:"))
        self.assertIn("0.00 EUR", self._row_value(content, "Est. Annual Cost:"))

    def test_detail_hides_unset_agreement_entitlement_and_shows_zero_linked_seats(self):
        """Issue #500: an unset entitlement hides only the agreement row."""
        sub = self._seat_matrix_subscription(None)
        content = self._detail_content(sub)

        self.assertNotIn("Agreement Entitled Quantity:", content)
        self.assertIn("Linked License Seats:", content)
        self.assertTrue(self._row_text(content, "Linked License Seats:").startswith("0 / 0"))

    def test_detail_renders_zero_agreement_entitlement(self):
        """Issue #500: zero is a real entitlement value and stays visible."""
        sub = self._seat_matrix_subscription(0)
        content = self._detail_content(sub)

        self.assertEqual(self._row_text(content, "Agreement Entitled Quantity:"), "0")
        self.assertTrue(self._row_text(content, "Linked License Seats:").startswith("0 / 0"))

    def test_detail_renders_set_agreement_entitlement(self):
        """Issue #500: a set entitlement renders on its own agreement row."""
        sub = self._seat_matrix_subscription(120)
        content = self._detail_content(sub)

        self.assertEqual(self._row_text(content, "Agreement Entitled Quantity:"), "120")
        self.assertTrue(self._row_text(content, "Linked License Seats:").startswith("0 / 0"))

    def test_detail_keeps_agreement_entitlement_and_linked_license_seats_distinct(self):
        """Issue #500: entitlement 120 next to 115 linked license seats never swaps."""
        sub = self._seat_matrix_subscription(120)
        software = baker.make(Software, manufacturer__name="Seat Co", manufacturer__slug="seat-co", tenant=None)
        baker.make(License, software=software, subscription=sub, seats=100, tenant=None)
        baker.make(License, software=software, subscription=sub, seats=15, tenant=None)

        content = self._detail_content(sub)

        entitlement = self._row_text(content, "Agreement Entitled Quantity:")
        linked = self._row_text(content, "Linked License Seats:")
        self.assertEqual(entitlement, "120")
        self.assertNotIn("115", entitlement)
        self.assertTrue(linked.startswith("0 / 115"), linked)
        self.assertNotIn("120", linked)

    def test_detail_keeps_linked_license_totals_with_unset_or_zero_entitlement(self):
        """Issue #500: linked license totals render when the entitlement is unset or zero."""
        software = baker.make(Software, manufacturer__name="Seat Co", manufacturer__slug="seat-co", tenant=None)
        unset = self._seat_matrix_subscription(None)
        zero = self._seat_matrix_subscription(0)
        for sub in (unset, zero):
            baker.make(License, software=software, subscription=sub, seats=100, tenant=None)
            baker.make(License, software=software, subscription=sub, seats=15, tenant=None)

        unset_content = self._detail_content(unset)
        self.assertNotIn("Agreement Entitled Quantity:", unset_content)
        unset_linked = self._row_text(unset_content, "Linked License Seats:")
        self.assertTrue(unset_linked.startswith("0 / 115"), unset_linked)
        self.assertIn("(115", unset_linked)

        zero_content = self._detail_content(zero)
        self.assertEqual(self._row_text(zero_content, "Agreement Entitled Quantity:"), "0")
        zero_linked = self._row_text(zero_content, "Linked License Seats:")
        self.assertTrue(zero_linked.startswith("0 / 115"), zero_linked)
        self.assertIn("(115", zero_linked)

    def test_create_view_get(self):
        url = reverse("subscriptions:subscription_create")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_create_view_post(self):
        url = reverse("subscriptions:subscription_create")
        resp = self.client.post(
            url,
            {
                "name": "New Subscription",
                "supplier": self.supplier.pk,
                "type": SubscriptionTypeChoices.SAAS,
                "status": SubscriptionStatusChoices.ACTIVE,
                "renewal_cost": "499.00",
                "currency": "USD",
                "billing_cycle": BillingCycleChoices.ANNUAL,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Subscription.objects.filter(name="New Subscription").exists())

    def test_edit_view_get(self):
        url = reverse("subscriptions:subscription_update", kwargs={"pk": self.sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_edit_view_post(self):
        url = reverse("subscriptions:subscription_update", kwargs={"pk": self.sub.pk})
        resp = self.client.post(
            url,
            {
                "name": "Renamed Subscription",
                "supplier": self.supplier.pk,
                "type": SubscriptionTypeChoices.SAAS,
                "status": SubscriptionStatusChoices.ACTIVE,
                "renewal_cost": "999.99",
                "currency": "EUR",
                "billing_cycle": BillingCycleChoices.ANNUAL,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.name, "Renamed Subscription")

    def test_delete_view_get(self):
        url = reverse("subscriptions:subscription_delete", kwargs={"pk": self.sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_delete_view_post(self):
        url = reverse("subscriptions:subscription_delete", kwargs={"pk": self.sub.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Subscription.objects.filter(pk=self.sub.pk).exists())


class SubscriptionAssignmentViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="testuser", password="testpass", is_staff=True, is_superuser=True)
        self.client.login(username="testuser", password="testpass")
        self.supplier = Supplier.objects.create(name="AWS", is_active=True)
        self.subscription = Subscription.objects.create(
            name="AWS Business Support",
            supplier=self.supplier,
            status=SubscriptionStatusChoices.ACTIVE,
        )
        self.tg = TenantGroup.objects.create(name="TG1", slug="tg1")
        self.tenant = Tenant.objects.create(name="Tenant1", slug="tenant1", group=self.tg)
        self.subscription.tenant = self.tenant
        self.subscription.save(update_fields=["tenant"])
        self.site = Site.objects.create(name="Dublin", slug="dublin", tenant=self.tenant)
        self.location = Location.objects.create(name="Rack A", slug="rack-a", site=self.site, tenant=self.tenant)
        self.asset = Asset.objects.create(
            name="Server Ireland",
            asset_tag="SRV-IRE-01",
            location=self.location,
            tenant=self.tenant,
        )
        self.asset_ct = ContentType.objects.get_for_model(Asset)

    def test_assignment_create_view_get(self):
        url = reverse("subscriptions:subscriptionassignment_create")
        resp = self.client.get(f"{url}?content_type={self.asset_ct.pk}&object_id={self.asset.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assign Subscription")

    def test_assignment_create_view_cancel_remains_a_navigation_link(self):
        url = reverse("subscriptions:subscriptionassignment_create")
        resp = self.client.get(f"{url}?content_type={self.asset_ct.pk}&object_id={self.asset.pk}")
        document = html5lib.parse(resp.content, namespaceHTMLElements=False)
        cancel_links = [
            link for link in document.iter("a") if "Cancel" in "".join(link.itertext()) and link.attrib.get("href")
        ]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(cancel_links), 1)
        self.assertEqual(cancel_links[0].attrib["href"], self.asset.get_absolute_url())
        self.assertNotIn("data-bs-dismiss", cancel_links[0].attrib)

    def test_assignment_quick_add_cancel_dismisses_and_fragments_have_no_scripts(self):
        url = (
            f"{reverse('subscriptions:subscriptionassignment_create')}?"
            f"content_type={self.asset_ct.pk}&object_id={self.asset.pk}&_quickadd=1"
        )
        responses = (
            self.client.get(url),
            self.client.post(url, data={}, HTTP_HX_REQUEST="true"),
        )

        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, "generic/includes/quick_add_modal.html")
            document = html5lib.parse(response.content, namespaceHTMLElements=False)
            self.assertFalse(list(document.iter("script")), "quick-add fragments must not include script elements")
            self.assertFalse(
                [link for link in document.iter("a") if "Cancel" in "".join(link.itertext())],
                "quick-add Cancel must not be a navigable anchor",
            )
            cancel_buttons = [button for button in document.iter("button") if "Cancel" in "".join(button.itertext())]
            self.assertEqual(len(cancel_buttons), 1)
            self.assertEqual(cancel_buttons[0].attrib.get("type"), "button")
            self.assertEqual(cancel_buttons[0].attrib.get("data-bs-dismiss"), "modal")

    def test_assignment_create_view_post(self):
        url = reverse("subscriptions:subscriptionassignment_create")
        post_url = f"{url}?content_type={self.asset_ct.pk}&object_id={self.asset.pk}"
        resp = self.client.post(
            post_url,
            {
                "subscription": self.subscription.pk,
                "notes": "Test support assignment",
            },
        )
        self.assertEqual(resp.status_code, 302)

        assignment = SubscriptionAssignment.objects.filter(
            subscription=self.subscription, content_type=self.asset_ct, object_id=self.asset.pk
        ).first()
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.notes, "Test support assignment")
        self.assertEqual(assignment.assigned_by, self.user)

    def test_assignment_delete_view_post(self):
        assignment = SubscriptionAssignment.objects.create(
            subscription=self.subscription, content_type=self.asset_ct, object_id=self.asset.pk, assigned_by=self.user
        )
        url = reverse("subscriptions:subscriptionassignment_delete", kwargs={"pk": assignment.pk})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SubscriptionAssignment.objects.filter(pk=assignment.pk).exists())


class SubscriptionLifecycleViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="testuser", password="testpass", is_staff=True, is_superuser=True)
        self.client.login(username="testuser", password="testpass")
        self.supplier = Supplier.objects.create(name="Test Supplier")
        self.sub = Subscription.objects.create(
            name="Test Subscription",
            supplier=self.supplier,
            type=SubscriptionTypeChoices.SAAS,
            status=SubscriptionStatusChoices.ACTIVE,
            renewal_date=date(2026, 6, 1),
            renewal_cost=999.99,
            currency="EUR",
            billing_cycle=BillingCycleChoices.ANNUAL,
            licensed_quantity=50,
        )
        self.tg = TenantGroup.objects.create(name="TG1", slug="tg1")
        self.tenant = Tenant.objects.create(name="Tenant1", slug="tenant1", group=self.tg)
        self.sub.tenant = self.tenant
        self.sub.save(update_fields=["tenant"])
        self.site = Site.objects.create(name="Dublin", slug="dublin", tenant=self.tenant)
        self.location = Location.objects.create(name="Rack A", slug="rack-a", site=self.site, tenant=self.tenant)
        self.deployable_status = StatusLabel.objects.create(
            name="Subscription Deployable", slug="subscription-deployable", type="deployable"
        )
        self.asset = Asset.objects.create(
            name="Server Ireland",
            asset_tag="SRV-IRE-01",
            location=self.location,
            tenant=self.tenant,
            status=self.deployable_status,
        )
        self.holder = AssetHolder.objects.create(
            first_name="John", last_name="Doe", email="john@example.com", tenant=self.tenant
        )

    def test_subscription_renew_workflow(self):
        # 1. Test GET request renders the form
        url = reverse("subscriptions:subscription_renew", kwargs={"pk": self.sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Renew Subscription:")

        # 2. Test POST request updates the subscription and returns HTMX headers
        new_renewal_date = date(2027, 6, 1)
        resp = self.client.post(url, {"renewal_date": "2027-06-01", "renewal_cost": "1099.99"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.renewal_date, new_renewal_date)
        self.assertEqual(float(self.sub.renewal_cost), 1099.99)
        self.assertEqual(self.sub.status, SubscriptionStatusChoices.ACTIVE)
        self.assertIn("tableRefreshRequired", resp["HX-Trigger"])

    def test_subscription_cancel_workflow(self):
        # 1. Test GET request renders the form
        url = reverse("subscriptions:subscription_cancel", kwargs={"pk": self.sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cancel Subscription:")

        # 2. Test POST request updates status and logs cancellation notes
        cancel_date = date(2026, 6, 2)
        resp = self.client.post(
            url, {"cancellation_date": "2026-06-02", "reason": "Moving to AWS"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(resp.status_code, 204)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.cancellation_date, cancel_date)
        self.assertEqual(self.sub.status, SubscriptionStatusChoices.CANCELLED)
        self.assertIn("Moving to AWS", self.sub.notes)
        self.assertIn("tableRefreshRequired", resp["HX-Trigger"])

    def test_subscription_suspend_workflow(self):
        url = reverse("subscriptions:subscription_suspend", kwargs={"pk": self.sub.pk})
        resp = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, SubscriptionStatusChoices.SUSPENDED)
        self.assertIn("tableRefreshRequired", resp["HX-Trigger"])

        resume_url = reverse("subscriptions:subscription_resume", kwargs={"pk": self.sub.pk})
        resp = self.client.post(resume_url, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 204)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, SubscriptionStatusChoices.ACTIVE)
        self.assertIn("tableRefreshRequired", resp["HX-Trigger"])

    def test_illegal_lifecycle_transitions_return_controlled_responses(self):
        self.sub.cancel(cancellation_date=date(2026, 6, 2), reason="terminal")

        renew_url = reverse("subscriptions:subscription_renew", kwargs={"pk": self.sub.pk})
        response = self.client.post(renew_url, {"renewal_date": "2027-06-01", "renewal_cost": "1099.99"})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            None,
            "Invalid subscription status transition from cancelled to active.",
        )

        suspend_url = reverse("subscriptions:subscription_suspend", kwargs={"pk": self.sub.pk})
        response = self.client.post(suspend_url)
        self.assertEqual(response.status_code, 400)

        resume_url = reverse("subscriptions:subscription_resume", kwargs={"pk": self.sub.pk})
        response = self.client.post(resume_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.assertIn('"level": "danger"', response["HX-Trigger"])

    def test_lifecycle_view_retries_do_not_duplicate_journal_entries(self):
        object_type = ContentType.objects.get_for_model(Subscription)
        entries = JournalEntry.objects.filter(model=object_type, object_id=self.sub.pk)

        renew_url = reverse("subscriptions:subscription_renew", kwargs={"pk": self.sub.pk})
        renewal = {"renewal_date": "2027-06-01", "renewal_cost": "1099.99"}
        self.assertEqual(self.client.post(renew_url, renewal).status_code, 302)
        count_after_renewal = entries.count()
        self.assertEqual(self.client.post(renew_url, renewal).status_code, 302)
        self.assertEqual(entries.count(), count_after_renewal)

        suspend_url = reverse("subscriptions:subscription_suspend", kwargs={"pk": self.sub.pk})
        self.assertEqual(self.client.post(suspend_url).status_code, 302)
        count_after_suspend = entries.count()
        self.assertEqual(self.client.post(suspend_url).status_code, 302)
        self.assertEqual(entries.count(), count_after_suspend)

        resume_url = reverse("subscriptions:subscription_resume", kwargs={"pk": self.sub.pk})
        self.assertEqual(self.client.post(resume_url).status_code, 302)
        count_after_resume = entries.count()
        self.assertEqual(self.client.post(resume_url).status_code, 302)
        self.assertEqual(entries.count(), count_after_resume)

        cancel_url = reverse("subscriptions:subscription_cancel", kwargs={"pk": self.sub.pk})
        cancellation = {"cancellation_date": "2026-06-02", "reason": "retired"}
        self.assertEqual(self.client.post(cancel_url, cancellation).status_code, 302)
        count_after_cancel = entries.count()
        self.assertEqual(self.client.post(cancel_url, cancellation).status_code, 302)
        self.assertEqual(entries.count(), count_after_cancel)

    def test_subscription_assignment_lifecycle(self):
        # 1. Test GET request to checkout/assign renders form
        url = reverse("subscriptions:subscription_checkout", kwargs={"pk": self.sub.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assign Subscription:")

        # 2. Test POST checkout to Employee (AssetHolder)
        resp = self.client.post(
            url,
            {"target_type": "holder", "assigned_holder": self.holder.pk, "notes": "Assigning to John"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.sub.assignments.count(), 1)
        assignment = self.sub.assignments.first()
        self.assertEqual(assignment.assigned_object, self.holder)
        self.assertEqual(assignment.notes, "Assigning to John")

        # 3. Test POST checkout to Hardware Asset
        resp = self.client.post(
            url,
            {"target_type": "asset", "asset": self.asset.pk, "notes": "Assigning to server"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.sub.assignments.count(), 2)

        # 4. Checkin / Delete assignment restores capacity
        delete_url = reverse("subscriptions:subscriptionassignment_delete", kwargs={"pk": assignment.pk})
        resp = self.client.post(delete_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.sub.assignments.count(), 1)
