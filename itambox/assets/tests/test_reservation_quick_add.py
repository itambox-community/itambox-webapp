import html5lib
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset, AssetReservation, AssetType, ReservationStatusChoices, StatusLabel
from core.tests.mixins import TenantTestMixin
from organization.models import AssetHolder


class AssetReservationQuickAddRegressionTest(TenantTestMixin, TestCase):
    def setUp(self):
        self.setup_tenant_context(name="Issue 390 Tenant", slug="issue-390")
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        asset_type = baker.make(AssetType)
        status = baker.make(StatusLabel, type=StatusLabel.TYPE_DEPLOYABLE)
        self.asset = Asset.objects.create(
            name="Issue 390 Laptop",
            asset_tag="ISSUE-390",
            asset_type=asset_type,
            status=status,
            tenant=self.tenant,
        )
        self.holder = AssetHolder.objects.create(
            first_name="Ada",
            last_name="Lovelace",
            upn="ada.lovelace@example.com",
            tenant=self.tenant,
        )

    def test_asset_detail_reservation_quick_add_native_post_creates_one_and_is_readable(self):
        detail_response = self.client.get(reverse("assets:asset_detail", kwargs={"pk": self.asset.pk}))
        self.assertEqual(detail_response.status_code, 200)
        detail_document = html5lib.parse(detail_response.content, namespaceHTMLElements=False)
        add_button = next(
            element for element in detail_document.iter("button") if "Add Reservation" in "".join(element.itertext())
        )
        quick_add_url = add_button.attrib["hx-get"]

        modal_response = self.client.get(quick_add_url)
        self.assertEqual(modal_response.status_code, 200)
        self.assertTemplateUsed(modal_response, "generic/includes/quick_add_modal.html")
        modal_document = html5lib.parse(modal_response.content, namespaceHTMLElements=False)
        modal_forms = list(modal_document.iter("form"))
        self.assertEqual(len(modal_forms), 1)
        modal_form = modal_forms[0]
        self.assertEqual(modal_form.attrib.get("method"), "post")
        self.assertEqual(modal_form.attrib.get("action"), quick_add_url)
        self.assertEqual(modal_form.attrib.get("hx-post"), quick_add_url)
        self.assertEqual(modal_form.attrib.get("hx-target"), "#quick-add-modal-content")
        self.assertEqual(modal_form.attrib.get("hx-swap"), "innerHTML")

        invalid_response = self.client.post(modal_form.attrib["action"], data={"asset": self.asset.pk})
        self.assertEqual(invalid_response.status_code, 200)
        self.assertTemplateUsed(invalid_response, "generic/includes/quick_add_modal.html")
        self.assertContains(invalid_response, "This field is required")
        self.assertEqual(AssetReservation.objects.count(), 0)

        purpose = "Issue 390 native fallback reservation"
        create_response = self.client.post(
            modal_form.attrib["action"],
            data={
                "asset": self.asset.pk,
                "reserved_for": self.holder.pk,
                "start_date": "2030-01-10",
                "end_date": "2030-01-12",
                "status": ReservationStatusChoices.PENDING,
                "purpose": purpose,
                "notes": "Created through the asset detail quick-add modal.",
            },
        )
        self.assertEqual(create_response.status_code, 204)
        self.assertEqual(create_response.headers["HX-Redirect"], self.asset.get_absolute_url())
        self.assertEqual(AssetReservation.objects.filter(asset=self.asset, purpose=purpose).count(), 1)
        reservation = AssetReservation.objects.get(asset=self.asset, purpose=purpose)

        list_response = self.client.get(reverse("assets:assetreservation_list"), {"q": purpose})
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, purpose)
        self.assertEqual([row.record.pk for row in list_response.context["table"].rows], [reservation.pk])

        reservation_detail_response = self.client.get(
            reverse("assets:assetreservation_detail", kwargs={"pk": reservation.pk})
        )
        self.assertEqual(reservation_detail_response.status_code, 200)
        self.assertContains(reservation_detail_response, purpose)
        self.assertContains(reservation_detail_response, "Ada Lovelace")
        self.assertContains(reservation_detail_response, "2030-01-10")
        self.assertContains(reservation_detail_response, "2030-01-12")

    def test_quick_add_cancel_is_a_dismiss_control_and_fragments_have_no_scripts(self):
        quick_add_url = reverse("assets:assetreservation_create") + f"?asset={self.asset.pk}&_quickadd=1"
        responses = (
            (self.client.get(quick_add_url), 200),
            (self.client.post(quick_add_url, data={"asset": self.asset.pk}, HTTP_HX_REQUEST="true"), 422),
        )

        for response, expected_status in responses:
            self.assertEqual(response.status_code, expected_status)
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

        modal_form = next(html5lib.parse(responses[0][0].content, namespaceHTMLElements=False).iter("form"))
        self.assertEqual(modal_form.attrib.get("hx-boost"), "false")

    def test_full_page_create_cancel_remains_a_navigation_link(self):
        response = self.client.get(reverse("assets:assetreservation_create"))
        self.assertEqual(response.status_code, 200)
        document = html5lib.parse(response.content, namespaceHTMLElements=False)
        cancel_links = [
            link for link in document.iter("a") if "Cancel" in "".join(link.itertext()) and link.attrib.get("href")
        ]
        self.assertEqual(len(cancel_links), 1)
        self.assertNotIn("data-bs-dismiss", cancel_links[0].attrib)
