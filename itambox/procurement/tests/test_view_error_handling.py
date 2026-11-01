"""Error handling of the purchase-order line edit and receive views.

Expected failures (invalid input, the receipt service's own refusals, a database
uniqueness conflict) reach the user as a readable message and change nothing.
Raw exception text never reaches the user, and unexpected errors are not
swallowed: they propagate so Django logs them.
"""

from unittest import mock

from django.contrib.messages import get_messages
from django.urls import reverse

from assets.models import Asset, StatusLabel
from procurement.models import PurchaseOrderLine

from .test_partial_receipts import PartialReceiptFixture

FORMSET_MANAGEMENT = {
    "form-INITIAL_FORMS": "1",
    "form-MIN_NUM_FORMS": "0",
    "form-MAX_NUM_FORMS": "1000",
}


class _LoggedInFixture(PartialReceiptFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    @staticmethod
    def _messages(response):
        return [str(message) for message in get_messages(response.wsgi_request)]


class PurchaseOrderLineEditErrorTests(_LoggedInFixture):
    def setUp(self):
        super().setUp()
        self.purchase_order = self._draft_purchase_order("PO-LINE-EDIT-001")
        self.line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=self.purchase_order,
            asset_type=self.asset_type,
            qty_ordered=5,
            unit_price="10.00",
        )
        self.url = reverse("procurement:purchaseorderline_edit", kwargs={"pk": self.line.pk})

    def test_blank_price_clears_the_unit_price(self):
        response = self.client.post(self.url, {"qty_ordered": "7", "unit_price": "  "})

        self.assertEqual(response.status_code, 200)
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_ordered, 7)
        self.assertIsNone(self.line.unit_price)
        self.assertEqual(self._messages(response), ["Line item updated successfully."])

    def test_invalid_values_are_refused_with_a_readable_message(self):
        invalid = [
            {"qty_ordered": "abc", "unit_price": "1.00"},
            {"qty_ordered": "0", "unit_price": "1.00"},
            {"qty_ordered": "3", "unit_price": "-1"},
            {"qty_ordered": "3", "unit_price": "not-a-price"},
            {"qty_ordered": "3", "unit_price": "NaN"},
            {"qty_ordered": "3", "unit_price": "Infinity"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.post(self.url, payload)

                self.assertEqual(response.status_code, 200)
                self.line.refresh_from_db()
                self.assertEqual(self.line.qty_ordered, 5)
                self.assertEqual(str(self.line.unit_price), "10.00")
                # The partial does not render messages, so they accumulate across subtests.
                self.assertEqual(
                    set(self._messages(response)),
                    {"Enter a whole quantity of at least 1 and a unit price that is not negative."},
                )


class PurchaseOrderReceiveErrorTests(_LoggedInFixture):
    def _open_asset_order(self, order_number, qty=1):
        purchase_order = self._draft_purchase_order(order_number)
        line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            asset_type=self.asset_type,
            qty_ordered=qty,
            unit_price="5.00",
        )
        self._open(purchase_order)
        return purchase_order, line

    def _expected_received(self, url):
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        return self._form_receipt_state(page)

    def _step_one(self, url, line, qty):
        """Post step 1 for an asset line; returns the step-2 page and its receipt snapshot."""
        page = self.client.post(
            url,
            {
                "step": "1",
                "expected_received": self._expected_received(url),
                "form-TOTAL_FORMS": "1",
                **FORMSET_MANAGEMENT,
                "form-0-line_id": str(line.pk),
                "form-0-qty_to_receive": str(qty),
            },
        )
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "procurement/purchaseorder_receive_step2.html")
        return page, self._form_receipt_state(page)

    def _existing_asset(self, **kwargs):
        return Asset.objects.create(
            name="Existing asset",
            asset_type=self.asset_type,
            status=StatusLabel.objects.get(slug="deployable"),
            tenant=self.tenant,
            **kwargs,
        )

    def _assert_nothing_received(self, response, line):
        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertEqual(line.qty_received, 0)
        self.assertFalse(Asset.objects.filter(purchase_order_line=line).exists())
        messages = self._messages(response)
        self.assertEqual(len(messages), 1)
        self.assertIn("Nothing was received", messages[0])
        # The database's own wording never reaches the user.
        self.assertNotIn("duplicate key", messages[0])
        self.assertNotIn("constraint", messages[0])

    def test_tag_held_by_a_deleted_asset_is_refused_without_a_server_error(self):
        held = self._existing_asset(asset_tag="HELD-TAG-1")
        held.delete()
        self.assertTrue(Asset.all_objects.filter(pk=held.pk, deleted_at__isnull=False).exists())

        purchase_order, line = self._open_asset_order("PO-RECEIVE-HELD-TAG")
        url = reverse("procurement:purchaseorder_receive_form", kwargs={"pk": purchase_order.pk})
        _page, snapshot = self._step_one(url, line, 1)

        response = self.client.post(
            url,
            {
                "step": "2",
                "expected_received": snapshot,
                "form-TOTAL_FORMS": "1",
                **FORMSET_MANAGEMENT,
                "form-0-line_id": str(line.pk),
                "form-0-asset_tag": "HELD-TAG-1",
                "form-0-name": "Received asset",
            },
            follow=True,
        )

        self._assert_nothing_received(response, line)
        self.assertRedirects(response, purchase_order.get_absolute_url())

    def test_unexpected_receipt_errors_propagate(self):
        component = self._component("Unexpected Error RAM", "unexpected-error-ram")
        purchase_order = self._draft_purchase_order("PO-RECEIVE-UNEXPECTED")
        line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            component=component,
            qty_ordered=4,
            unit_price="5.00",
        )
        self._open(purchase_order)
        url = reverse("procurement:purchaseorder_receive_form", kwargs={"pk": purchase_order.pk})
        payload = {
            "step": "1",
            "expected_received": self._expected_received(url),
            "form-TOTAL_FORMS": "1",
            **FORMSET_MANAGEMENT,
            "form-0-line_id": str(line.pk),
            "form-0-qty_to_receive": "2",
        }

        with mock.patch("procurement.views.receive_purchase_order", side_effect=RuntimeError("receipt bug")):
            with self.assertRaisesRegex(RuntimeError, "receipt bug"):
                self.client.post(url, payload)

    def test_invalid_step_one_with_a_tampered_line_id_still_renders(self):
        component = self._component("Tampered Step One RAM", "tampered-step-one-ram")
        purchase_order = self._draft_purchase_order("PO-RECEIVE-TAMPERED-1")
        PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            component=component,
            qty_ordered=4,
            unit_price="5.00",
        )
        self._open(purchase_order)
        url = reverse("procurement:purchaseorder_receive_form", kwargs={"pk": purchase_order.pk})

        response = self.client.post(
            url,
            {
                "step": "1",
                "expected_received": self._expected_received(url),
                "form-TOTAL_FORMS": "2",
                **FORMSET_MANAGEMENT,
                "form-0-line_id": "not-a-number",
                "form-0-qty_to_receive": "1",
                "form-1-line_id": "999999999",
                "form-1-qty_to_receive": "not-a-number",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "procurement/purchaseorder_receive.html")
        self.assertEqual([pair[0] for pair in response.context["lines_and_forms"]], [None, None])

    def test_invalid_step_two_with_a_tampered_line_id_still_renders(self):
        purchase_order, line = self._open_asset_order("PO-RECEIVE-TAMPERED-2", qty=2)
        url = reverse("procurement:purchaseorder_receive_form", kwargs={"pk": purchase_order.pk})
        _page, snapshot = self._step_one(url, line, 2)

        response = self.client.post(
            url,
            {
                "step": "2",
                "expected_received": snapshot,
                "form-TOTAL_FORMS": "2",
                **FORMSET_MANAGEMENT,
                "form-0-line_id": "not-a-number",
                "form-0-asset_tag": "TAMPER-A",
                "form-0-name": "First",
                "form-1-line_id": "999999999",
                "form-1-asset_tag": "TAMPER-B",
                "form-1-name": "Second",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "procurement/purchaseorder_receive_step2.html")
        self.assertEqual([pair[0] for pair in response.context["lines_info"]], [None, None])
        line.refresh_from_db()
        self.assertEqual(line.qty_received, 0)
