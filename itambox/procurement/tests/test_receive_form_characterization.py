"""Characterization of the two-step purchase-order receive form view.

These tests pin every branch of ``PurchaseOrderReceiveFormView.post`` (step 1 quantities,
step 2 asset provisioning, error re-renders, service refusals) so the method can be split
into named helpers without changing behaviour, messages, or the order of side effects.
"""

import json
from unittest import mock

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse

from assets.models import Asset
from procurement.models import PurchaseOrderLine
from procurement.tests.test_partial_receipts import PartialReceiptFixture

FORMSET_MANAGEMENT = {
    "form-INITIAL_FORMS": "1",
    "form-MIN_NUM_FORMS": "0",
    "form-MAX_NUM_FORMS": "1000",
}


class ReceiveFormCharacterizationTests(PartialReceiptFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    def _url(self, purchase_order):
        return reverse("procurement:purchaseorder_receive_form", kwargs={"pk": purchase_order.pk})

    def _asset_order(self, number, qty=2):
        purchase_order = self._draft_purchase_order(number)
        line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            asset_type=self.asset_type,
            qty_ordered=qty,
            unit_price="5.00",
        )
        self._open(purchase_order)
        return purchase_order, line

    def _component_order(self, number, qty=10):
        component = self._component(f"Char {number}", f"char-{number}")
        purchase_order = self._draft_purchase_order(number)
        line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            component=component,
            qty_ordered=qty,
            unit_price="5.00",
        )
        self._open(purchase_order)
        return purchase_order, line

    def _step1(self, purchase_order, quantities, **extra):
        """Step-1 payload for ``quantities`` = [(line_id_value, qty_value), ...]."""
        payload = {
            "step": "1",
            "expected_received": self._form_receipt_state(self.client.get(self._url(purchase_order))),
            "form-TOTAL_FORMS": str(len(quantities)),
            **FORMSET_MANAGEMENT,
            "form-INITIAL_FORMS": str(len(quantities)),
        }
        for index, (line_id, qty) in enumerate(quantities):
            payload[f"form-{index}-line_id"] = str(line_id)
            payload[f"form-{index}-qty_to_receive"] = str(qty)
        payload.update(extra)
        return payload

    @staticmethod
    def _step2(expected_received, rows):
        """Step-2 payload for ``rows`` = [(line_id_value, asset_tag, name), ...]."""
        payload = {
            "step": "2",
            "expected_received": expected_received,
            "form-TOTAL_FORMS": str(len(rows)),
            **FORMSET_MANAGEMENT,
            "form-INITIAL_FORMS": str(len(rows)),
        }
        for index, (line_id, tag, name) in enumerate(rows):
            payload[f"form-{index}-line_id"] = str(line_id)
            payload[f"form-{index}-asset_tag"] = tag
            payload[f"form-{index}-name"] = name
        return payload

    @staticmethod
    def _message_texts(response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    # -- step 1 ---------------------------------------------------------------------------------

    def test_step1_inventory_only_books_directly_with_success_message(self):
        purchase_order, line = self._component_order("PO-CHAR-S1-OK")
        payload = self._step1(purchase_order, [(line.pk, 4)])

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            response = self.client.post(self._url(purchase_order), payload)

        self.assertRedirects(response, purchase_order.get_absolute_url(), fetch_redirect_response=False)
        receive.assert_called_once_with(
            purchase_order, {line.pk: 4}, None, expected_received={line.pk: line.qty_received}
        )
        self.assertEqual(self._message_texts(response), ["Stock received successfully."])
        self.assertNotIn("receive_po_quantities", self.client.session)

    def test_step1_zero_asset_quantity_skips_step_two(self):
        """An asset line left at 0 does not open provisioning when only stock is received."""
        purchase_order, asset_line = self._asset_order("PO-CHAR-S1-ZERO")
        component_line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            component=self._component("Char Zero", "char-zero"),
            qty_ordered=5,
            unit_price="1.00",
        )
        # The lines are added to an already open order; the payload is built from the form.
        payload = self._step1(purchase_order, [(asset_line.pk, 0), (component_line.pk, 3)])

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(receive.call_args.args[1], {asset_line.pk: 0, component_line.pk: 3})
        self.assertIsNone(receive.call_args.args[2])
        self.assertEqual(self._message_texts(response), ["Stock received successfully."])

    def test_step1_with_assets_renders_step_two_with_tag_previews_and_stores_quantities(self):
        purchase_order, line = self._asset_order("PO-CHAR-S1-ASSETS", qty=3)
        payload = self._step1(purchase_order, [(line.pk, 2)])

        response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "procurement/purchaseorder_receive_step2.html")
        self.assertEqual(response.context["step"], "2")
        self.assertEqual(self.client.session["receive_po_quantities"], {str(line.pk): 2})
        initial = response.context["formset"].initial
        self.assertEqual(len(initial), 2)
        self.assertEqual([row["line_id"] for row in initial], [line.pk, line.pk])
        self.assertEqual([row["name"] for row in initial], [str(self.asset_type)] * 2)
        self.assertTrue(all(row["asset_tag"] for row in initial))
        self.assertNotEqual(initial[0]["asset_tag"], initial[1]["asset_tag"])
        self.assertEqual([info.pk for info, _form in response.context["lines_info"]], [line.pk, line.pk])
        self.assertEqual(
            json.loads(response.context["expected_received_json"]),
            json.loads(payload["expected_received"]),
        )

    def test_step1_service_refusal_reports_error_and_redirects(self):
        purchase_order, line = self._component_order("PO-CHAR-S1-REFUSED", qty=2)
        payload = self._step1(purchase_order, [(line.pk, 5)])

        response = self.client.post(self._url(purchase_order), payload)

        self.assertRedirects(response, purchase_order.get_absolute_url(), fetch_redirect_response=False)
        (message,) = self._message_texts(response)
        self.assertTrue(message.startswith("Error receiving purchase order: "), message)
        line.refresh_from_db()
        self.assertEqual(line.qty_received, 0)

    def test_step1_invalid_quantity_rerenders_step_one_with_none_for_bad_line_id(self):
        purchase_order, line = self._component_order("PO-CHAR-S1-BADID")
        payload = self._step1(purchase_order, [("not-a-number", 1)])

        response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "procurement/purchaseorder_receive.html")
        self.assertEqual(response.context["step"], "1")
        self.assertEqual([info for info, _form in response.context["lines_and_forms"]], [None])
        self.assertFalse(response.context["formset"].is_valid())

    def test_step1_missing_and_unknown_line_ids_pair_with_none(self):
        purchase_order, line = self._component_order("PO-CHAR-S1-UNKNOWN")
        payload = self._step1(purchase_order, [("", 1), (999999999, "x"), (line.pk, "x")])

        response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 200)
        infos = [info for info, _form in response.context["lines_and_forms"]]
        self.assertEqual(infos[0], None)
        self.assertEqual(infos[1], None)
        self.assertEqual(infos[2].pk, line.pk)

    def test_step1_all_zero_quantities_show_the_formset_error(self):
        purchase_order, line = self._component_order("PO-CHAR-S1-ZEROALL")
        payload = self._step1(purchase_order, [(line.pk, 0)])

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            response = self.client.post(self._url(purchase_order), payload)

        receive.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["formset"].non_form_errors()), ["You must specify at least one item to receive."]
        )
        self.assertEqual([info.pk for info, _form in response.context["lines_and_forms"]], [line.pk])

    def test_step_defaults_to_one_when_absent(self):
        purchase_order, line = self._component_order("PO-CHAR-NOSTEP")
        payload = self._step1(purchase_order, [(line.pk, 1)])
        del payload["step"]

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 302)
        receive.assert_called_once()

    def test_unparseable_snapshot_is_treated_as_empty(self):
        purchase_order, line = self._component_order("PO-CHAR-BADSNAP")
        payload = self._step1(purchase_order, [(line.pk, 1)], expected_received="{not json")

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            self.client.post(self._url(purchase_order), payload)

        self.assertEqual(receive.call_args.kwargs["expected_received"], {})

    # -- step 2 ---------------------------------------------------------------------------------

    def _step_two_state(self, purchase_order, line, qty):
        """Run step 1 for real so the session holds the quantities; return the step-2 snapshot."""
        page = self.client.post(self._url(purchase_order), self._step1(purchase_order, [(line.pk, qty)]))
        self.assertEqual(page.status_code, 200)
        return self._form_receipt_state(page)

    def test_step2_success_provisions_assets_clears_session_and_reports(self):
        purchase_order, line = self._asset_order("PO-CHAR-S2-OK")
        state = self._step_two_state(purchase_order, line, 2)
        payload = self._step2(state, [(line.pk, "CHAR-TAG-1", "One"), (line.pk, "CHAR-TAG-2", "Two")])

        response = self.client.post(self._url(purchase_order), payload)

        self.assertRedirects(response, purchase_order.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(self._message_texts(response), ["Stock received and assets provisioned successfully."])
        self.assertNotIn("receive_po_quantities", self.client.session)
        line.refresh_from_db()
        self.assertEqual(line.qty_received, 2)
        self.assertEqual(
            sorted(Asset.objects.filter(purchase_order_line=line).values_list("asset_tag", flat=True)),
            ["CHAR-TAG-1", "CHAR-TAG-2"],
        )

    def test_step2_passes_session_quantities_details_and_snapshot_to_the_service(self):
        purchase_order, line = self._asset_order("PO-CHAR-S2-CALL")
        state = self._step_two_state(purchase_order, line, 1)
        payload = self._step2(state, [(line.pk, "CHAR-CALL-1", "One")])

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            self.client.post(self._url(purchase_order), payload)

        quantities, details = receive.call_args.args[1:3]
        self.assertEqual(quantities, {line.pk: 1})
        self.assertEqual(details[0]["asset_tag"], "CHAR-CALL-1")
        self.assertEqual(details[0]["line_id"], line.pk)
        self.assertEqual(receive.call_args.kwargs["expected_received"], {line.pk: 0})

    def test_step2_service_refusal_keeps_session_and_reports_error(self):
        purchase_order, line = self._asset_order("PO-CHAR-S2-REFUSED")
        state = self._step_two_state(purchase_order, line, 1)
        payload = self._step2(state, [(line.pk, "CHAR-REF-1", "One")])

        with mock.patch("procurement.views.receive_purchase_order", side_effect=ValidationError(["a", "b"])):
            response = self.client.post(self._url(purchase_order), payload)

        self.assertRedirects(response, purchase_order.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(self._message_texts(response), ["Error receiving purchase order: a b"])
        self.assertIn("receive_po_quantities", self.client.session)

    def test_step2_integrity_error_reports_tag_conflict_and_keeps_session(self):
        purchase_order, line = self._asset_order("PO-CHAR-S2-INTEGRITY")
        state = self._step_two_state(purchase_order, line, 1)
        payload = self._step2(state, [(line.pk, "CHAR-INT-1", "One")])

        with mock.patch("procurement.views.receive_purchase_order", side_effect=IntegrityError("dup")):
            with self.assertLogs("procurement.views", level="WARNING") as logs:
                response = self.client.post(self._url(purchase_order), payload)

        self.assertRedirects(response, purchase_order.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(
            self._message_texts(response),
            [
                "Nothing was received: an asset tag or serial number is already in use. Asset tags stay "
                "reserved by deleted assets. Change the values and submit again."
            ],
        )
        self.assertIn("refused by a database constraint", logs.output[0])
        self.assertIn(str(purchase_order.pk), logs.output[0])
        self.assertIn("receive_po_quantities", self.client.session)

    def test_step2_invalid_formset_pairs_unknown_lines_with_none_and_keeps_step_two(self):
        purchase_order, line = self._asset_order("PO-CHAR-S2-INVALID")
        state = self._step_two_state(purchase_order, line, 2)
        payload = self._step2(
            state, [(999999999, "DUP", "A"), (line.pk, "DUP", "B"), ("bad", "X", "C"), ("", "Y", "D")]
        )

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            response = self.client.post(self._url(purchase_order), payload)

        receive.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "procurement/purchaseorder_receive_step2.html")
        self.assertEqual(response.context["step"], "2")
        infos = [info for info, _form in response.context["lines_info"]]
        self.assertEqual([info.pk if info else None for info in infos], [None, line.pk, None, None])
        self.assertEqual(
            json.loads(response.context["expected_received_json"]),
            {str(pk): qty for pk, qty in json.loads(state).items()},
        )
        self.assertIn("receive_po_quantities", self.client.session)

    def test_step2_without_session_quantities_receives_nothing_asset_related(self):
        """Without stored quantities the formset starts empty; the service gets an empty map."""
        purchase_order, line = self._asset_order("PO-CHAR-S2-NOSESSION")
        state = self._form_receipt_state(self.client.get(self._url(purchase_order)))
        payload = self._step2(state, [(line.pk, "CHAR-NS-1", "One")])

        with mock.patch("procurement.views.receive_purchase_order") as receive:
            response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(receive.call_args.args[1], {})

    def test_step2_tolerates_component_lines_in_the_session_quantities(self):
        purchase_order, asset_line = self._asset_order("PO-CHAR-S2-MIXED", qty=2)
        component_line = PurchaseOrderLine.objects.create(
            tenant=self.tenant,
            purchase_order=purchase_order,
            component=self._component("Char Mixed", "char-mixed"),
            qty_ordered=5,
            unit_price="1.00",
        )
        session = self.client.session
        session["receive_po_quantities"] = {str(asset_line.pk): 2, str(component_line.pk): 3}
        session.save()
        state = self._form_receipt_state(self.client.get(self._url(purchase_order)))
        payload = self._step2(state, [(asset_line.pk, "", ""), (asset_line.pk, "", "")])
        payload["form-1-asset_tag"] = payload["form-0-asset_tag"] = "SAME"

        response = self.client.post(self._url(purchase_order), payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["formset"].forms), 2)

    # -- access ---------------------------------------------------------------------------------

    def test_unknown_order_returns_404(self):
        response = self.client.post(reverse("procurement:purchaseorder_receive_form", kwargs={"pk": 999999999}))
        self.assertEqual(response.status_code, 404)
