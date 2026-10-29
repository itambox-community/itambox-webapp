import json
import logging
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseServerError, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import View
from django_tables2 import RequestConfig

from core.context import get_current_request_id
from core.managers import Scope
from core.reports.exporters import PDF_MIME, report_pdf_bytes
from itambox.ratelimit import get_client_ip
from itambox.views.generic import ObjectCloneView, ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView
from itambox.views.generic.htmx_responses import error_response, is_htmx_request, success_response
from itambox.views.generic.service_views import SimplePostView
from organization.services.identity_provisioning import login_may_act_as_holder

from .filters import CustodyReceiptFilterSet, SignatureProviderConfigFilterSet
from .forms import CustodyTemplateForm, SignatureProviderConfigForm
from .forms_filter import CustodyReceiptFilterForm, SignatureProviderConfigFilterForm
from .models import CustodyReceipt, CustodySigningSession, CustodyTemplate, SignatureProviderConfig
from .providers import (
    OUTCOME_ACCEPTED,
    OUTCOME_DECLINED,
    CallbackRejected,
    SignatureOutcome,
    SignatureProviderError,
)
from .registry import signature_providers
from .services import (
    CustodyDecisionRefused,
    CustodyHandoffGone,
    CustodyHandoffNotFound,
    CustodyHandoffPermissionDenied,
    ProviderUnavailable,
    build_custody_handoff_url,
    custody_handoff_email_is_configured,
    custody_handoff_qr_module_count,
    custody_handoff_qr_rendered_size,
    handle_provider_callback,
    provider_settings_for,
    receipt_period_is_active,
    record_custody_decision,
    record_provider_outcome,
    render_custody_handoff_qr_svg,
    resolve_custody_handoff,
    resolve_provider_config,
    scope_custody_receipts,
    send_custody_handoff_email,
    start_provider_signature,
    validated_signature_image,
    verify_custody_evidence,
)
from .tables import CustodyReceiptTable, CustodyTemplateTable, SignatureProviderConfigTable

logger = logging.getLogger(__name__)

VIEW_CUSTODY_RECEIPT_PERMISSION = "compliance.view_custodyreceipt"
PREPARE_CUSTODY_RECEIPT_PERMISSION = "compliance.prepare_custodyreceipt"
EXPORT_CUSTODY_RECEIPT_PERMISSION = "compliance.export_custodyreceipt"
CUSTODY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}")
CUSTODY_SIGNING_SESSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}")
CUSTODY_LINK_TTL = timedelta(days=7)


def _signature_provider_unavailable_response(request):
    return _custody_error_response(
        request,
        error_code="custody_provider_unavailable",
        title="Signature Provider Unavailable",
        error="The signature provider for this custody receipt is not available. Contact your IT administrator.",
        status=503,
    )


def _external_signature_redirect(receipt, request):
    if receipt.signature_provider == "local":
        return None
    provider = signature_providers.get(receipt.signature_provider)
    if provider is None:
        # Fail closed: never fall back to the local pad for a receipt whose
        # template chose a different provider.
        logger.error(
            "Custody receipt_id=%s names unregistered signature provider %r",
            receipt.pk,
            receipt.signature_provider,
        )
        return _signature_provider_unavailable_response(request)
    return_url = reverse("compliance:custody_provider_return", kwargs={"token": receipt.token})
    try:
        result = start_provider_signature(receipt, provider, request=request, return_url=_absolute(request, return_url))
    except ProviderUnavailable:
        logger.error(
            "Custody receipt_id=%s has no active configuration for signature provider %r",
            receipt.pk,
            receipt.signature_provider,
        )
        return _signature_provider_unavailable_response(request)
    except SignatureProviderError as exc:
        logger.error(
            "Custody receipt_id=%s signature provider %r failed to start: %s",
            receipt.pk,
            receipt.signature_provider,
            type(exc).__name__,
        )
        return _signature_provider_failed_response(request)
    if result is None:
        return None
    url = result.redirect_url
    if request.GET.get("onsite") == "true":
        parsed_url = urlparse(url)
        query = dict(parse_qsl(parsed_url.query))
        query["onsite"] = "true"
        url = urlunparse(parsed_url._replace(query=urlencode(query)))
    return redirect(url)


def _absolute(request, path):
    base_url = getattr(settings, "ITAMBOX_BASE_URL", "")
    return f"{base_url}{path}" if base_url else request.build_absolute_uri(path)


def _signature_provider_failed_response(request):
    return _custody_error_response(
        request,
        error_code="custody_provider_failed",
        title="Signature Provider Error",
        error="The signature provider could not start signing. Try again later or contact your IT administrator.",
        status=502,
    )


def _external_provider_response(request, receipt, holder):
    if receipt.signature_provider == "local":
        return None
    if not request.user.is_authenticated:
        return _signer_error_response(request, holder, require_authenticated=True)
    return _external_signature_redirect(receipt, request)


def _custody_error_response(request, *, error_code, title, error, status):
    return render(
        request,
        "compliance/custody/sign_error.html",
        {
            "error_code": error_code,
            "error_title": title,
            "error": error,
        },
        status=status,
    )


def _completed_receipt_response(request, receipt):
    if receipt.acceptance_status == CustodyReceipt.STATUS_ACCEPTED:
        context = {"receipt": receipt, "asset": receipt.asset, "holder": receipt.holder}
        return render(request, "compliance/custody/receipt_success.html", context)
    if receipt.acceptance_status == CustodyReceipt.STATUS_DECLINED:
        return _custody_error_response(
            request,
            error_code="custody_declined",
            title="Custody Transfer Declined",
            error="This custody transfer has been declined.",
            status=200,
        )
    if receipt.acceptance_status == CustodyReceipt.STATUS_SUPERSEDED:
        return _custody_superseded_response(request)
    return None


def _custody_superseded_response(request):
    return _custody_error_response(
        request,
        error_code="custody_superseded",
        title="Custody Receipt Superseded",
        error="This custody receipt is no longer valid because the asset is no longer assigned to you.",
        status=410,
    )


def _receipt_period_is_active(receipt):
    """True while the custody period the receipt evidences is the asset's active one."""
    return receipt_period_is_active(receipt)


def _superseded_pending_response(request, receipt):
    if receipt.acceptance_status == CustodyReceipt.STATUS_PENDING and not _receipt_period_is_active(receipt):
        return _custody_superseded_response(request)
    return None


def _expired_receipt_response(request, receipt, *, signing_session=None):
    if signing_session is not None:
        # A valid signing session is the operator-authorized, fresh handoff
        # channel (30-minute TTL, one-time consumption) and overrides the
        # bearer-link TTL — the link-TTL check applies only to the unassisted
        # path without a session.
        return None
    if receipt.created_date is None or receipt.created_date + CUSTODY_LINK_TTL <= timezone.now():
        return _custody_error_response(
            request,
            error_code="custody_link_expired",
            title="Custody Link Expired",
            error="This custody acceptance link has expired. Request a new link from your IT administrator.",
            status=410,
        )
    return None


def _signer_error_response(request, holder, *, require_authenticated=False):
    if holder is None:
        return _custody_error_response(
            request,
            error_code="wrong_recipient",
            title="Recipient Verification Failed",
            error="You are not the intended recipient of this custody receipt.",
            status=403,
        )
    if not request.user.is_authenticated:
        if require_authenticated:
            return _custody_error_response(
                request,
                error_code="recipient_authentication_required",
                title="Recipient Authentication Required",
                error="Sign in as the intended recipient before completing this custody action.",
                status=403,
            )
        return None
    if not login_may_act_as_holder(request.user, holder):
        return _custody_error_response(
            request,
            error_code="wrong_recipient",
            title="Recipient Verification Failed",
            error="You are not the intended recipient of this custody receipt.",
            status=403,
        )
    return None


def _resolve_custody_receipt(token):
    if CUSTODY_TOKEN_PATTERN.fullmatch(token) is None:
        return None
    return (
        CustodyReceipt.objects.select_related("asset", "asset__tenant", "holder", "holder__user")
        .filter(token=token)
        .first()
    )


def _custody_signing_session_error_response(request, *, gone):
    if gone:
        return _custody_error_response(
            request,
            error_code="custody_session_expired_or_used",
            title="Custody Signing Session Unavailable",
            error="This custody signing session has expired or is no longer available.",
            status=410,
        )
    return _custody_error_response(
        request,
        error_code="custody_session_unavailable",
        title="Custody Signing Session Unavailable",
        error="The requested custody signing session is unavailable.",
        status=404,
    )


def _custody_signing_session_state_error(request, signing_session, receipt):
    if (
        signing_session.receipt_id != receipt.pk
        or signing_session.intended_holder_id is None
        or signing_session.intended_holder_id != receipt.holder_id
    ):
        return _custody_signing_session_error_response(request, gone=False)
    if (
        signing_session.consumed_at is not None
        or signing_session.canceled_at is not None
        or signing_session.expires_at <= timezone.now()
    ):
        return _custody_signing_session_error_response(request, gone=True)
    return None


def _resolve_custody_signing_session(request, receipt):
    session_token = request.GET.get("session")
    if session_token is None:
        return None, None
    if CUSTODY_SIGNING_SESSION_TOKEN_PATTERN.fullmatch(session_token) is None:
        return None, _custody_signing_session_error_response(request, gone=False)
    # unscoped: public signing token resolves the session without a logged-in tenant scope
    signing_session = (
        CustodySigningSession._base_manager.select_related("intended_holder", "intended_holder__user")
        .filter(token=session_token, receipt_id=receipt.pk)
        .first()
    )
    if signing_session is None:
        return None, _custody_signing_session_error_response(request, gone=False)
    state_error = _custody_signing_session_state_error(request, signing_session, receipt)
    return signing_session, state_error


def _resolve_custody_signing_context(request, receipt):
    signing_session, context_error = _resolve_custody_signing_session(request, receipt)
    if context_error is not None:
        return None, None, context_error
    holder = signing_session.intended_holder if signing_session is not None else receipt.holder
    return signing_session, holder, _signer_error_response(request, holder)


def _lock_custody_signing_session(request, receipt, signing_session):
    if signing_session is None:
        return None, None
    # unscoped: public signing session lock by pk; token possession is the authorization
    signing_session = (
        CustodySigningSession._base_manager.select_for_update()
        .filter(pk=signing_session.pk, receipt_id=receipt.pk)
        .first()
    )
    if signing_session is None:
        return None, _custody_signing_session_error_response(request, gone=False)
    return signing_session, _custody_signing_session_state_error(request, signing_session, receipt)


def _isoformat_or_none(value):
    return value.isoformat() if value is not None else None


def _evidence_export(receipt):
    check = verify_custody_evidence(receipt)
    return {
        "version": receipt.evidence_version,
        "key_id": receipt.evidence_key_id,
        "status": check.status,
        "differing_fields": list(check.differing_fields),
        "snapshot": receipt.evidence_snapshot,
    }


def _custody_receipt_export_payload(receipt):
    signing_sessions = (
        CustodySigningSession.objects.select_related("operator", "intended_holder")
        .filter(receipt=receipt, receipt__asset__tenant_id=receipt.asset.tenant_id)
        .order_by("created_at", "pk")
    )
    return {
        "format": "itambox.custody-receipt",
        "version": 1,
        "omitted_sensitive_fields": ["signature_canvas", "signature_data", "token"],
        "receipt": {
            "id": receipt.pk,
            "asset_id": receipt.asset_id,
            "holder_id": receipt.holder_id,
            "custody_template_id": receipt.custody_template_id,
            "signature_provider": receipt.signature_provider,
            "eula_text": receipt.eula_text,
            "disclaimer": receipt.disclaimer,
            "qms_reference": receipt.qms_reference,
            "accepted": receipt.accepted,
            "accepted_date": _isoformat_or_none(receipt.accepted_date),
            "acceptance_method": receipt.acceptance_method,
            "acceptance_status": receipt.acceptance_status,
            "signature_hash": receipt.signature_hash,
            "verification_hash": receipt.verification_hash,
            "signed_at": _isoformat_or_none(receipt.signed_at),
            "eula_version": receipt.eula_version,
            "created_date": _isoformat_or_none(receipt.created_date),
            "ip_address": receipt.ip_address,
            "user_agent": receipt.user_agent,
            "created_at": _isoformat_or_none(receipt.created_at),
            "updated_at": _isoformat_or_none(receipt.updated_at),
        },
        "asset": {
            "id": receipt.asset_id,
            "tenant_id": receipt.asset.tenant_id,
            "name": receipt.asset.name,
            "asset_tag": receipt.asset.asset_tag,
            "serial_number": receipt.asset.serial_number,
        },
        "holder": {
            "id": receipt.holder_id,
            "user_id": receipt.holder.user_id,
            "first_name": receipt.holder.first_name,
            "last_name": receipt.holder.last_name,
            "upn": receipt.holder.upn,
            "email": receipt.holder.email,
        },
        "evidence": _evidence_export(receipt),
        "signing_sessions": [
            {
                "id": signing_session.pk,
                "operator_id": signing_session.operator_id,
                "operator_username": signing_session.operator.get_username(),
                "intended_holder_id": signing_session.intended_holder_id,
                "created_at": _isoformat_or_none(signing_session.created_at),
                "expires_at": _isoformat_or_none(signing_session.expires_at),
                "consumed_at": _isoformat_or_none(signing_session.consumed_at),
                "canceled_at": _isoformat_or_none(signing_session.canceled_at),
                "outcome": signing_session.outcome,
            }
            for signing_session in signing_sessions
        ],
    }


def _process_custody_post(request, token, receipt, signing_session=None):
    with transaction.atomic():
        # Re-fetch under a row lock and re-check status so concurrent or
        # double-submitted POSTs cannot compute non-deterministic evidence.
        receipt = CustodyReceipt.objects.select_for_update().get(pk=receipt.pk)

        signing_session, session_error = _lock_custody_signing_session(request, receipt, signing_session)
        if session_error is not None:
            return session_error

        asset = receipt.asset
        holder = signing_session.intended_holder if signing_session is not None else receipt.holder
        signer_error = _signer_error_response(request, holder, require_authenticated=True)
        if signer_error is not None:
            return signer_error

        completed_response = _completed_receipt_response(request, receipt)
        if completed_response is not None:
            return completed_response

        action = request.POST.get("action", "accept")
        if action == "decline":
            outcome = SignatureOutcome(status=OUTCOME_DECLINED)
        else:
            signature = validated_signature_image(request.POST.get("signature_canvas"))
            if signature is None:
                return render(
                    request,
                    "compliance/custody/sign_portal.html",
                    {
                        "asset": asset,
                        "holder": holder,
                        "token": token,
                        "receipt": receipt,
                        "error": "Please provide a valid signature.",
                    },
                )
            outcome = SignatureOutcome(status=OUTCOME_ACCEPTED, signature_image=signature)

        try:
            record_custody_decision(
                receipt,
                outcome,
                method="digital",
                client_ip=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                signing_session=signing_session,
                actor_id=request.user.pk,
            )
        except CustodyDecisionRefused as refused:
            if refused.reason == "superseded":
                return _custody_superseded_response(request)
            raise
        return _decision_response(request, receipt, holder)


def _decision_response(request, receipt, holder):
    if receipt.acceptance_status == CustodyReceipt.STATUS_DECLINED:
        return _custody_error_response(
            request,
            error_code="custody_declined",
            title="Custody Transfer Declined",
            error="You have declined the custody transfer.",
            status=200,
        )
    return render(
        request,
        "compliance/custody/receipt_success.html",
        {"receipt": receipt, "asset": receipt.asset, "holder": holder},
    )


def _pre_sign_response(request, receipt, holder):
    if receipt.acceptance_status != CustodyReceipt.STATUS_PENDING:
        if not request.user.is_authenticated:
            return _signer_error_response(request, holder, require_authenticated=True)
        return _completed_receipt_response(request, receipt)
    superseded_response = _superseded_pending_response(request, receipt)
    if superseded_response is not None:
        return superseded_response
    return _external_provider_response(request, receipt, holder)


def custody_eula_sign(request, token):
    receipt = _resolve_custody_receipt(token)
    if receipt is None:
        return _custody_error_response(
            request,
            error_code="custody_link_unavailable",
            title="Custody Link Unavailable",
            error="The requested custody link is unavailable.",
            status=404,
        )

    signing_session, holder, signing_context_error = _resolve_custody_signing_context(request, receipt)
    if signing_context_error is not None:
        return signing_context_error

    expired_response = _expired_receipt_response(request, receipt, signing_session=signing_session)
    if expired_response is not None:
        return expired_response
    asset = receipt.asset

    require_signin = settings.REQUIRE_CUSTODY_SIGNIN
    if require_signin and not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())

    pre_sign_response = _pre_sign_response(request, receipt, holder)
    if pre_sign_response is not None:
        return pre_sign_response

    if request.method == "POST":
        signer_error = _signer_error_response(request, holder, require_authenticated=True)
        if signer_error is not None:
            return signer_error
        return _process_custody_post(request, token, receipt, signing_session=signing_session)

    return render(
        request,
        "compliance/custody/sign_portal.html",
        {"asset": asset, "holder": holder, "token": token, "receipt": receipt},
    )


def _provider_return_outcome(request, receipt):
    """Ask the receipt's provider for the outcome on the browser return: (outcome, error response)."""
    provider = signature_providers.get(receipt.signature_provider)
    if provider is None or receipt.signature_provider == "local":
        return None, _signature_provider_unavailable_response(request)
    try:
        settings_for_receipt = provider_settings_for(resolve_provider_config(receipt, provider), request=request)
        outcome = provider.complete_return(receipt, request=request, settings=settings_for_receipt)
    except ProviderUnavailable:
        return None, _signature_provider_unavailable_response(request)
    except SignatureProviderError as exc:
        logger.error(
            "Custody receipt_id=%s signature provider %r failed on return: %s",
            receipt.pk,
            receipt.signature_provider,
            type(exc).__name__,
        )
        return None, _signature_provider_failed_response(request)
    if outcome is None:
        return None, _custody_error_response(
            request,
            error_code="custody_provider_pending",
            title="Waiting for Confirmation",
            error="Your decision was handed to the signature provider. This page will show the result once the "
            "provider confirms it; you can close it now.",
            status=202,
        )
    return outcome, None


def _refused_decision_response(request, receipt, refused):
    if refused.reason == "superseded":
        return _custody_superseded_response(request)
    receipt.refresh_from_db()
    return _completed_receipt_response(request, receipt) or _signature_provider_failed_response(request)


def custody_provider_return(request, token):
    """The provider sends the holder's browser back here after signing."""
    receipt = _resolve_custody_receipt(token)
    if receipt is None:
        return _custody_error_response(
            request,
            error_code="custody_link_unavailable",
            title="Custody Link Unavailable",
            error="The requested custody link is unavailable.",
            status=404,
        )
    holder = receipt.holder
    signer_error = _signer_error_response(request, holder, require_authenticated=True)
    if signer_error is not None:
        return signer_error
    if receipt.acceptance_status != CustodyReceipt.STATUS_PENDING:
        return _completed_receipt_response(request, receipt)
    outcome, error = _provider_return_outcome(request, receipt)
    if error is not None:
        return error
    try:
        receipt = record_provider_outcome(
            receipt.pk,
            outcome,
            provider_name=receipt.signature_provider,
            client_ip=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except CustodyDecisionRefused as refused:
        return _refused_decision_response(request, receipt, refused)
    return _decision_response(request, receipt, holder)


@csrf_exempt
@require_POST
def signature_provider_callback(request, config_pk):
    """Server-to-server callback; the provider authenticates the request itself."""
    # unscoped: provider callbacks carry no session or tenant; the provider's own request authentication is the authority
    config = SignatureProviderConfig._base_manager.filter(pk=config_pk, is_active=True, deleted_at__isnull=True).first()
    provider = signature_providers.get(config.provider) if config is not None else None
    if provider is None:
        return JsonResponse({"status": "unknown_config"}, status=404)
    try:
        event = provider.parse_callback(request, settings=provider_settings_for(config, request=request))
    except CallbackRejected:
        logger.warning("Rejected signature provider callback for config_id=%s", config.pk)
        return JsonResponse({"status": "rejected"}, status=403)
    except SignatureProviderError as exc:
        logger.error("Signature provider callback failed for config_id=%s: %s", config.pk, type(exc).__name__)
        return JsonResponse({"status": "error"}, status=400)
    status = handle_provider_callback(config, event)
    http_status = 404 if status == "unknown_reference" else 200
    return JsonResponse({"status": status}, status=http_status)


class CustodyTemplateListView(ObjectListView):
    queryset = CustodyTemplate.objects.select_related("tenant", "tenant_group").prefetch_related("tags")
    table = CustodyTemplateTable
    action_buttons = ("add",)


class CustodyTemplateDetailView(ObjectDetailView):
    queryset = CustodyTemplate.objects.select_related("tenant", "tenant_group").prefetch_related("tags")
    related_object_exclusions = ("compliance.custodyreceipt",)
    template_name = "compliance/custodytemplates/custodytemplate_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        template = self.get_object()

        from itambox.utils import get_paginate_count

        from .tables import CustodyReceiptTable

        can_view_receipts = self.request.user.has_perm(VIEW_CUSTODY_RECEIPT_PERMISSION)
        context["can_view_custody_receipts"] = can_view_receipts
        if can_view_receipts:
            receipts_qs = scope_custody_receipts(
                template.receipts.all().select_related("asset", "holder", "custody_template"),
                user=self.request.user,
                permission=VIEW_CUSTODY_RECEIPT_PERMISSION,
            )
            receipts_table = CustodyReceiptTable(receipts_qs, request=self.request)
            RequestConfig(self.request, paginate={"per_page": get_paginate_count(self.request)}).configure(
                receipts_table
            )
            context["receipts_table"] = receipts_table

        receipt_list_url = reverse("compliance:custodyreceipt_list")
        context["related_objects_list"] = [
            item for item in context.get("related_objects_list", []) if not item["url"].startswith(receipt_list_url)
        ]

        return context


class InternalCustodyPermissionMixin:
    """Render a custody-specific 403 for authenticated internal users."""

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        return render(
            self.request,
            "compliance/custody/internal_permission_error.html",
            {"error_code": "internal_custody_permission_required"},
            status=403,
        )


class CustodyReceiptListView(InternalCustodyPermissionMixin, ObjectListView):
    queryset = CustodyReceipt.objects.select_related("asset", "holder", "custody_template")
    filterset = CustodyReceiptFilterSet
    filterset_form = CustodyReceiptFilterForm
    table = CustodyReceiptTable
    action_buttons = ()

    def get_queryset(self):
        return scope_custody_receipts(
            super().get_queryset(),
            user=self.request.user,
            permission=VIEW_CUSTODY_RECEIPT_PERMISSION,
        )


class CustodyReceiptDetailView(InternalCustodyPermissionMixin, ObjectDetailView):
    queryset = CustodyReceipt.objects.select_related("asset", "asset__tenant", "holder", "custody_template")
    template_name = "compliance/custodyreceipts/custodyreceipt_detail.html"

    def get_queryset(self):
        return scope_custody_receipts(super().get_queryset(), user=self.request.user)

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False
        receipt = self.get_object()
        return self.request.user.has_perm(VIEW_CUSTODY_RECEIPT_PERMISSION, obj=receipt.asset)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        receipt = self.object
        signature_image = validated_signature_image(receipt.signature_canvas)
        if signature_image is not None:
            context["signature_image"] = signature_image
        context["evidence_check"] = verify_custody_evidence(receipt)

        now = timezone.now()
        signing_sessions = CustodySigningSession.objects.select_related("operator", "intended_holder").filter(
            receipt=receipt,
            receipt__asset__tenant_id=receipt.asset.tenant_id,
        )
        context["signing_sessions"] = signing_sessions.defer("token")
        context["can_prepare_signing_session"] = (
            receipt.acceptance_status == CustodyReceipt.STATUS_PENDING
            and receipt.holder_id is not None
            and self.request.user.has_perm(PREPARE_CUSTODY_RECEIPT_PERMISSION, obj=receipt.asset)
        )
        context["can_export_custody_receipt"] = (
            receipt.acceptance_status == CustodyReceipt.STATUS_ACCEPTED
            and receipt.accepted
            and self.request.user.has_perm(EXPORT_CUSTODY_RECEIPT_PERMISSION, obj=receipt.asset)
        )
        if context["can_prepare_signing_session"]:
            handoff_session = signing_sessions.filter(
                operator=self.request.user,
                intended_holder_id=receipt.holder_id,
                consumed_at__isnull=True,
                canceled_at__isnull=True,
                expires_at__gt=now,
            ).first()
            if handoff_session is not None:
                context["custody_handoff_url"] = build_custody_handoff_url(self.request, receipt, handoff_session)
                context["custody_handoff_expires_at"] = handoff_session.expires_at
                context["custody_handoff_session"] = handoff_session
                context["custody_handoff_qr_url"] = reverse(
                    "compliance:custodyreceipt_handoff_qr",
                    kwargs={"pk": receipt.pk, "session_pk": handoff_session.pk},
                )
                context["custody_handoff_email_url"] = reverse(
                    "compliance:custodyreceipt_handoff_email",
                    kwargs={"pk": receipt.pk, "session_pk": handoff_session.pk},
                )
                context["custody_handoff_qr_module_count"] = custody_handoff_qr_module_count(
                    context["custody_handoff_url"]
                )
                context["custody_handoff_qr_rendered_size"] = custody_handoff_qr_rendered_size(
                    context["custody_handoff_url"]
                )
                context["custody_handoff_email_enabled"] = bool(
                    receipt.holder.email and custody_handoff_email_is_configured()
                )
                context["custody_handoff_email_disabled_reason"] = (
                    _("The holder has no e-mail address.")
                    if not receipt.holder.email
                    else _("E-mail is not configured.")
                )
        return context


class CustodyReceiptPrepareView(InternalCustodyPermissionMixin, SimplePostView):
    queryset = CustodyReceipt.objects.select_related("asset", "asset__tenant", "holder")
    permission_required = PREPARE_CUSTODY_RECEIPT_PERMISSION

    def get_queryset(self):
        return scope_custody_receipts(super().get_queryset(), user=self.request.user)

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False
        receipt = self.get_object()
        return self.request.user.has_perm(PREPARE_CUSTODY_RECEIPT_PERMISSION, obj=receipt.asset)

    def perform_action(self, receipt, request):
        if receipt.acceptance_status != CustodyReceipt.STATUS_PENDING:
            raise ValidationError("Only pending custody receipts can have a signing session prepared.")
        if receipt.holder_id is None:
            raise ValidationError("A custody signing session requires an intended holder.")
        CustodySigningSession.objects.create(
            receipt=receipt,
            operator=request.user,
            intended_holder=receipt.holder,
        )
        return {"message": "Custody signing session prepared for recipient handoff."}


class CustodyReceiptExportView(InternalCustodyPermissionMixin, ObjectDetailView):
    queryset = CustodyReceipt.objects.select_related("asset", "asset__tenant", "holder")
    permission_required = EXPORT_CUSTODY_RECEIPT_PERMISSION

    def get_queryset(self):
        return scope_custody_receipts(super().get_queryset(), user=self.request.user)

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False
        receipt = self.get_object()
        return self.request.user.has_perm(EXPORT_CUSTODY_RECEIPT_PERMISSION, obj=receipt.asset)

    def get(self, request, *args, **kwargs):
        receipt = self.get_object()
        if receipt.acceptance_status != CustodyReceipt.STATUS_ACCEPTED or not receipt.accepted:
            raise Http404
        payload = _custody_receipt_export_payload(receipt)
        content = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        response = HttpResponse(content, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="custody-receipt-{receipt.pk}.json"'
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


def _custody_receipt_pdf_context(receipt):
    payload = _custody_receipt_export_payload(receipt)
    tenant = receipt.asset.tenant
    return {
        "receipt_data": payload["receipt"],
        "asset_data": payload["asset"],
        "holder_data": payload["holder"],
        "signing_sessions": payload["signing_sessions"],
        "tenant_name": tenant.name if tenant is not None else _("Global"),
        "signature_image": validated_signature_image(receipt.signature_canvas),
        "evidence_label": verify_custody_evidence(receipt).label,
    }


class CustodyReceiptPdfExportView(InternalCustodyPermissionMixin, ObjectDetailView):
    queryset = CustodyReceipt.objects.select_related("asset", "asset__tenant", "holder")
    permission_required = EXPORT_CUSTODY_RECEIPT_PERMISSION

    def get_queryset(self):
        return scope_custody_receipts(super().get_queryset(), user=self.request.user)

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False
        receipt = self.get_object()
        return self.request.user.has_perm(EXPORT_CUSTODY_RECEIPT_PERMISSION, obj=receipt.asset)

    def get(self, request, *args, **kwargs):
        receipt = self.get_object()
        if receipt.acceptance_status != CustodyReceipt.STATUS_ACCEPTED or not receipt.accepted:
            raise Http404
        rendered_html = render_to_string(
            "compliance/custodyreceipts/custodyreceipt_export_pdf.html",
            {"request": request, **_custody_receipt_pdf_context(receipt)},
            request=request,
        )
        try:
            pdf_bytes = report_pdf_bytes(rendered_html)
        # broad except: render-degrade: a renderer failure degrades the export to an error page instead of a crash
        except Exception as exc:
            logger.error(
                "custody_receipt_pdf_render_failed receipt_id=%s tenant_id=%s actor_id=%s request_id=%s "
                "exception_type=%s",
                receipt.pk,
                receipt.asset.tenant_id,
                request.user.pk,
                get_current_request_id(),
                type(exc).__name__,
            )
            return HttpResponseServerError(_("Unable to render the custody receipt PDF."))
        response = HttpResponse(pdf_bytes, content_type=PDF_MIME)
        response["Content-Disposition"] = f'attachment; filename="custody-receipt-{receipt.pk}.pdf"'
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


def _custody_internal_handoff_gone_response(request):
    return render(
        request,
        "compliance/custody/internal_handoff_error.html",
        {"error_code": "internal_custody_handoff_unavailable"},
        status=410,
    )


def _custody_handoff_outcome_message(outcome):
    result = outcome["result"]
    if result.disposition == "success":
        return _(
            "The handoff e-mail was accepted for delivery. The signing session expires in %(minutes)s minutes."
        ) % {"minutes": outcome["remaining_minutes"]}
    if result.disposition == "retryable":
        return _("Delivery could not be confirmed; the session remains active.")
    raise ValidationError(result.user_message or _("Email delivery was rejected."))


class CustodyHandoffViewBase(InternalCustodyPermissionMixin, LoginRequiredMixin, View):
    def resolve_handoff(self, request, kwargs):
        try:
            return resolve_custody_handoff(
                request,
                receipt_id=kwargs["pk"],
                session_id=kwargs["session_pk"],
            )
        except CustodyHandoffPermissionDenied:
            return self.handle_no_permission()
        except CustodyHandoffNotFound:
            raise Http404 from None
        except CustodyHandoffGone:
            return _custody_internal_handoff_gone_response(request)


class CustodyReceiptHandoffQrView(CustodyHandoffViewBase):
    def get(self, request, *args, **kwargs):
        handoff = self.resolve_handoff(request, kwargs)
        if isinstance(handoff, HttpResponse):
            return handoff
        receipt, signing_session = handoff
        handoff_url = build_custody_handoff_url(request, receipt, signing_session)
        response = HttpResponse(render_custody_handoff_qr_svg(handoff_url), content_type="image/svg+xml")
        response["Content-Disposition"] = f'inline; filename="custody-handoff-{signing_session.pk}.svg"'
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'"
        response._csp_default_none = True
        return response


class CustodyReceiptHandoffEmailView(CustodyHandoffViewBase):
    def post(self, request, *args, **kwargs):
        handoff = self.resolve_handoff(request, kwargs)
        if isinstance(handoff, HttpResponse):
            return handoff
        receipt, signing_session = handoff
        try:
            outcome = send_custody_handoff_email(request, receipt, signing_session)
            result = outcome["result"]
            message = _custody_handoff_outcome_message(outcome)
        except CustodyHandoffNotFound:
            raise Http404 from None
        except CustodyHandoffGone:
            return _custody_internal_handoff_gone_response(request)
        except ValidationError as exc:
            message = "; ".join(exc.messages)
            if is_htmx_request(request):
                return error_response(message)
            messages.error(request, message)
            return redirect(receipt.get_absolute_url())

        if is_htmx_request(request):
            return success_response(message)
        if result.disposition == "retryable":
            messages.info(request, message)
        else:
            messages.success(request, message)
        return redirect(receipt.get_absolute_url())


class CustodyTemplateEditView(ObjectEditView):
    queryset = CustodyTemplate.objects.all()
    model = CustodyTemplate
    model_form = CustodyTemplateForm
    template_name = "generic/object_edit.html"
    default_return_url = "compliance:custodytemplate_list"

    def get_initial(self):
        initial = super().get_initial()
        if "category" in self.request.GET:
            initial["category"] = self.request.GET["category"]
        return initial


class CustodyTemplateCloneView(CustodyTemplateEditView, ObjectCloneView):
    model = CustodyTemplate


class CustodyTemplateDeleteView(ObjectDeleteView):
    queryset = CustodyTemplate.objects.all()
    model = CustodyTemplate
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("compliance:custodytemplate_list")


@login_required
@permission_required("compliance.view_custodytemplate", raise_exception=True)
def custody_template_preview(request, pk):
    template = get_object_or_404(CustodyTemplate.objects.for_scope(Scope.current()), pk=pk)

    from assets.models import Asset
    from organization.models import AssetHolder

    asset = Asset.objects.for_scope(Scope.current()).first()
    if not asset:
        asset = Asset(
            name="[Preview] Professional Corporate Laptop (M3 Max)",
            asset_tag="PREVIEW-LT-099",
            serial_number="PREVIEW-SN-88291-XYZ",
        )

    holder = AssetHolder.objects.first()
    if not holder:
        holder = AssetHolder(first_name="Jane", last_name="Doe", email="jane.doe@organization.com", upn="jane.doe")

    receipt = CustodyReceipt(
        custody_template=template,
        signature_provider=template.signature_provider,
        eula_text=template.eula_text,
        disclaimer=template.disclaimer,
        qms_reference=template.qms_reference,
        acceptance_status=CustodyReceipt.STATUS_PENDING,
    )

    return render(
        request,
        "compliance/custody/sign_portal.html",
        {"asset": asset, "holder": holder, "receipt": receipt, "token": "preview", "is_preview": True},
    )


class _ProviderConfigQuerysetMixin:
    """Install-wide configs are visible to everyone in scope but writable only by platform admins."""

    queryset = SignatureProviderConfig.objects.select_related("tenant")

    def get_queryset(self):
        queryset = super().get_queryset()
        if getattr(self, "writes", False) and not self.request.user.is_superuser:
            queryset = queryset.filter(tenant__isnull=False)
        return queryset


class SignatureProviderConfigListView(ObjectListView):
    queryset = SignatureProviderConfig.objects.select_related("tenant")
    table = SignatureProviderConfigTable
    filterset = SignatureProviderConfigFilterSet
    filterset_form = SignatureProviderConfigFilterForm
    action_buttons = ("add",)


class SignatureProviderConfigDetailView(_ProviderConfigQuerysetMixin, ObjectDetailView):
    template_name = "compliance/signatureproviderconfigs/signatureproviderconfig_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = self.object
        provider = signature_providers.get(config.provider)
        context["provider_label"] = provider.verbose_name if provider is not None else config.provider
        context["provider_registered"] = provider is not None
        context["secret_keys"] = config.secret_keys
        context["callback_url"] = provider_settings_for(config, request=self.request).callback_url
        return context


class SignatureProviderConfigEditView(_ProviderConfigQuerysetMixin, ObjectEditView):
    writes = True
    model = SignatureProviderConfig
    model_form = SignatureProviderConfigForm
    template_name = "generic/object_edit.html"
    default_return_url = "compliance:signatureproviderconfig_list"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs


class SignatureProviderConfigDeleteView(_ProviderConfigQuerysetMixin, ObjectDeleteView):
    writes = True
    model = SignatureProviderConfig
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("compliance:signatureproviderconfig_list")
