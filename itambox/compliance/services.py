import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from datetime import timezone as dt_timezone
from urllib.parse import urlencode

import segno
from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

from assets.models import AssetAssignment
from core import events as core_events
from core.crypto import derive_purpose_keys
from core.events import DeliveryDisposition, DeliveryResult
from core.managers import Scope
from core.models import EmailSettings
from extras.models import JournalEntry
from extras.services.events import dispatch_event
from organization.models import Tenant

from .models import CustodyHandoffDelivery, CustodyReceipt, CustodySigningSession, SignatureProviderConfig
from .providers import (
    OUTCOME_ACCEPTED,
    CallbackEvent,
    ProviderSettings,
    SignatureOutcome,
    SignatureProviderError,
    StartResult,
)

SIGNATURE_PNG_PREFIX = "data:image/png;base64,"
MAX_SIGNATURE_IMAGE_BYTES = 2 * 1024 * 1024
CUSTODY_HANDOFF_QR_BORDER = 4
CUSTODY_HANDOFF_QR_SCALE = 3
# The symbol ships its own opaque quiet zone: a transparent background renders
# as near-invisible black-on-black under [data-bs-theme="dark"] and under
# WebView algorithmic darkening, and leaves the 4-module quiet zone at the
# mercy of whatever paints behind it. Decoders assume dark-on-light.
CUSTODY_HANDOFF_QR_DARK = "#000000"
CUSTODY_HANDOFF_QR_LIGHT = "#ffffff"
CUSTODY_HANDOFF_COOLDOWN_SECONDS = 10
CUSTODY_HANDOFF_SESSION_ATTEMPT_LIMIT = 3
CUSTODY_HANDOFF_RECEIPT_ATTEMPT_LIMIT = 6


# ── Keyed evidence seal ────────────────────────────────────────────────────
# A decided receipt carries a frozen snapshot of what the holder attested to and
# an HMAC over it, keyed by a purpose key derived from the field-encryption
# keyring. Without the key, nobody with database access alone can rewrite a
# receipt and recompute a matching seal.
EVIDENCE_VERSION = 1
EVIDENCE_KEY_PURPOSE = "custody-receipt-evidence"

EVIDENCE_UNSEALED = "unsealed"
EVIDENCE_LEGACY = "legacy"
EVIDENCE_VERIFIED = "verified"
EVIDENCE_MISMATCH = "mismatch"
EVIDENCE_UNKNOWN_KEY = "unknown_key"
EVIDENCE_STATUS_LABELS = {
    EVIDENCE_UNSEALED: _("Not sealed"),
    EVIDENCE_LEGACY: _("Legacy (unkeyed digest, cannot be verified)"),
    EVIDENCE_VERIFIED: _("Verified"),
    EVIDENCE_MISMATCH: _("Mismatch: the receipt no longer matches its sealed evidence"),
    EVIDENCE_UNKNOWN_KEY: _("Unknown key: sealed with a key that is no longer configured"),
}


@dataclass(frozen=True)
class EvidenceCheck:
    status: str
    differing_fields: tuple[str, ...] = ()

    @property
    def label(self):
        return EVIDENCE_STATUS_LABELS[self.status]

    @property
    def verified(self):
        return self.status == EVIDENCE_VERIFIED


def _utc_timestamp(value):
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text_sha256(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _normalized_ip(value):
    if not value:
        return ""
    try:
        return ipaddress.ip_address(str(value)).compressed
    except ValueError:
        return str(value)


def _canonical_bytes(snapshot):
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _evidence_mac(key, snapshot):
    return hmac.new(key, _canonical_bytes(snapshot), hashlib.sha256).hexdigest()


def _bound_evidence(receipt):
    """The evidence fields that the receipt row itself must keep agreeing with."""
    return {
        "receipt_id": receipt.pk,
        "asset_id": receipt.asset_id,
        "holder_id": receipt.holder_id,
        "assignment_id": receipt.assignment_id,
        "custody_template_id": receipt.custody_template_id,
        "signature_provider": receipt.signature_provider,
        "provider_config_id": receipt.provider_config_id,
        "provider_reference": receipt.provider_reference or "",
        "provider_evidence_sha256": (
            hashlib.sha256(_canonical_bytes(receipt.provider_evidence)).hexdigest() if receipt.provider_evidence else ""
        ),
        "decision": {
            "status": receipt.acceptance_status,
            "method": receipt.acceptance_method,
            "signed_at": _utc_timestamp(receipt.signed_at),
        },
        "terms": {
            "eula_sha256": _text_sha256(receipt.eula_text),
            "disclaimer_sha256": _text_sha256(receipt.disclaimer),
            "qms_reference": receipt.qms_reference,
            "eula_version": receipt.eula_version,
        },
        # Both copies of the signature are covered: signature_canvas is what
        # the detail page and the PDF export render.
        "signature_sha256": _text_sha256(receipt.signature_data) if receipt.signature_data else "",
        "signature_canvas_sha256": _text_sha256(receipt.signature_canvas) if receipt.signature_canvas else "",
        "client": {
            "ip_address": _normalized_ip(receipt.ip_address),
            "user_agent": receipt.user_agent or "",
        },
    }


def seal_custody_evidence(receipt, *, decided_at):
    """Freeze the decision's evidence onto ``receipt`` and seal it; the caller saves.

    Returns the field names written so callers using ``update_fields`` can
    include them.
    """
    receipt.signature_hash = _text_sha256(receipt.signature_data) if receipt.signature_data else ""
    asset = receipt.asset
    holder = receipt.holder
    snapshot = {
        "version": EVIDENCE_VERSION,
        **_bound_evidence(receipt),
        "decided_at": _utc_timestamp(decided_at),
        # Descriptive identity as it stood at the decision; later edits to the
        # asset or holder do not change what was attested.
        "asset": {"asset_tag": asset.asset_tag, "serial_number": asset.serial_number or "", "name": asset.name},
        "holder": {
            "upn": holder.upn or "",
            "email": holder.email or "",
            "name": " ".join(part for part in (holder.first_name, holder.last_name) if part),
        },
    }
    key_id, key = derive_purpose_keys(EVIDENCE_KEY_PURPOSE)[0]
    receipt.evidence_snapshot = snapshot
    receipt.evidence_version = EVIDENCE_VERSION
    receipt.evidence_key_id = key_id
    receipt.verification_hash = _evidence_mac(key, snapshot)
    return ["signature_hash", "evidence_snapshot", "evidence_version", "evidence_key_id", "verification_hash"]


def verify_custody_evidence(receipt):
    """Check a receipt's evidence seal against the configured keyring."""
    if receipt.evidence_version == 0:
        return EvidenceCheck(EVIDENCE_LEGACY if receipt.verification_hash else EVIDENCE_UNSEALED)
    snapshot = receipt.evidence_snapshot
    keys = dict(derive_purpose_keys(EVIDENCE_KEY_PURPOSE))
    key = keys.get(receipt.evidence_key_id)
    if key is None:
        return EvidenceCheck(EVIDENCE_UNKNOWN_KEY)
    if not isinstance(snapshot, dict) or not hmac.compare_digest(
        _evidence_mac(key, snapshot), receipt.verification_hash or ""
    ):
        return EvidenceCheck(EVIDENCE_MISMATCH, ("evidence_snapshot",))
    bound = _bound_evidence(receipt)
    differing = tuple(sorted(name for name, value in bound.items() if snapshot.get(name) != value))
    if differing:
        return EvidenceCheck(EVIDENCE_MISMATCH, differing)
    return EvidenceCheck(EVIDENCE_VERIFIED)


class CustodyHandoffNotFound(Exception):
    """The requested receipt/session pair is not visible or not bound."""


class CustodyHandoffPermissionDenied(Exception):
    """The receipt is visible but the actor lacks the prepare permission."""


class CustodyHandoffGone(Exception):
    """A known operator session can no longer mint a handoff credential."""


class CustodyHandoffBoundExceeded(Exception):
    """A durable e-mail-attempt bound refused a new send."""


def build_custody_handoff_url(request, receipt, signing_session):
    """Build the one canonical absolute URL for copy, QR, and e-mail delivery."""
    handoff_path = reverse("compliance:custody_eula_sign", kwargs={"token": receipt.token})
    handoff_path = f"{handoff_path}?{urlencode({'session': signing_session.token})}"
    base_url = getattr(settings, "ITAMBOX_BASE_URL", "")
    if base_url:
        return f"{base_url}{handoff_path}"
    return request.build_absolute_uri(handoff_path)


def resolve_custody_handoff(request, *, receipt_id, session_id):
    """Resolve and re-authorize one live operator handoff in design order."""
    receipt = (
        scope_custody_receipts(
            CustodyReceipt.objects.select_related("asset", "asset__tenant", "holder", "holder__user"),
            user=request.user,
        )
        .filter(pk=receipt_id)
        .first()
    )
    if receipt is None:
        raise CustodyHandoffNotFound

    if not request.user.has_perm("compliance.prepare_custodyreceipt", obj=receipt.asset):
        raise CustodyHandoffPermissionDenied

    signing_session = (
        CustodySigningSession.objects.select_related("operator", "intended_holder", "intended_holder__user")
        .filter(pk=session_id, receipt_id=receipt.pk)
        .first()
    )
    if signing_session is None:
        raise CustodyHandoffNotFound
    if signing_session.operator_id != request.user.pk or signing_session.intended_holder_id != receipt.holder_id:
        raise CustodyHandoffNotFound

    if (
        signing_session.consumed_at is not None
        or signing_session.canceled_at is not None
        or signing_session.expires_at <= timezone.now()
    ):
        raise CustodyHandoffGone
    # This is intentionally last: a non-owner probing a terminal receipt must
    # receive the neutral 404 above rather than learn its terminal state.
    if receipt.acceptance_status != CustodyReceipt.STATUS_PENDING or receipt.holder_id is None:
        raise CustodyHandoffGone
    return receipt, signing_session


def validated_signature_image(signature_canvas):
    """Return a canonical PNG data URI, or ``None`` for unsafe legacy data."""
    if not isinstance(signature_canvas, str) or not signature_canvas.startswith(SIGNATURE_PNG_PREFIX):
        return None
    encoded = signature_canvas[len(SIGNATURE_PNG_PREFIX) :]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if len(decoded) > MAX_SIGNATURE_IMAGE_BYTES or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return SIGNATURE_PNG_PREFIX + base64.b64encode(decoded).decode("ascii")


def render_custody_handoff_qr_svg(handoff_url):
    """Render a token-free SVG artifact from the server-controlled QR serializer."""
    qr = segno.make_qr(handoff_url)
    output = io.BytesIO()
    qr.save(
        output,
        kind="svg",
        scale=CUSTODY_HANDOFF_QR_SCALE,
        border=CUSTODY_HANDOFF_QR_BORDER,
        dark=CUSTODY_HANDOFF_QR_DARK,
        light=CUSTODY_HANDOFF_QR_LIGHT,
        xmldecl=False,
    )
    return output.getvalue()


def custody_handoff_qr_module_count(handoff_url):
    """Return the symbol module count used to choose its natural rendered size."""
    return segno.make_qr(handoff_url).symbol_size(scale=1, border=0)[0]


def custody_handoff_qr_rendered_size(handoff_url):
    """Return the natural SVG pixel size from the symbol's module count."""
    modules = custody_handoff_qr_module_count(handoff_url)
    return (modules + (CUSTODY_HANDOFF_QR_BORDER * 2)) * CUSTODY_HANDOFF_QR_SCALE


def custody_handoff_email_is_configured():
    email_config = EmailSettings.load()
    return bool(email_config and email_config.enabled and email_config.from_address)


def _holder_language(holder):
    language = settings.LANGUAGE_CODE
    if holder.user_id:
        try:
            stored_language = (holder.user.preferences.data or {}).get("language")
        # broad except: availability-tradeoff: a preferences lookup failure falls back to the default language
        except Exception:
            stored_language = None
        if stored_language in dict(settings.LANGUAGES):
            language = stored_language
    return language


def _normalized_subject(value, *, limit=160):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _custody_handoff_email_content(receipt, signing_session, handoff_url):
    holder = receipt.holder
    asset = receipt.asset
    holder_name = " ".join(part for part in (holder.first_name, holder.last_name) if part).strip() or str(holder)
    expiry = timezone.localtime(signing_session.expires_at).strftime("%Y-%m-%d %H:%M %Z")
    language = _holder_language(holder)
    with translation.override(language):
        subject = _("Custody handoff action: %(name)s (%(tag)s)") % {
            "name": _normalized_subject(asset.name),
            "tag": _normalized_subject(asset.asset_tag),
        }
        body = _(
            "Hello %(holder)s,\n\n"
            "You have a custody handoff waiting for you.\n\n"
            "Asset: %(name)s\n"
            "Asset tag: %(tag)s\n"
            "Serial number: %(serial)s\n\n"
            "Open this one-time handoff link:\n%(url)s\n\n"
            "The session expires at %(expires)s. Sign in as the intended holder before opening the link.\n\n"
            "If you were not expecting this message, ignore it and report it to your IT administrator."
        ) % {
            "holder": holder_name,
            "name": asset.name,
            "tag": asset.asset_tag,
            "serial": asset.serial_number or "N/A",
            "url": handoff_url,
            "expires": expiry,
        }
    return str(subject), str(body), expiry


def send_email_notification(recipients, subject, body, *, tenant_id=None):
    """Proxy the shared transport so tests can patch either service boundary."""
    return core_events.send_email_notification(recipients, subject, body, tenant_id=tenant_id)


def _journal_handoff_event(asset, user, *, state, expires_at):
    expiry = timezone.localtime(expires_at).strftime("%Y-%m-%d %H:%M %Z")
    with transaction.atomic():
        return JournalEntry.objects.create(
            content_object=asset,
            user=user,
            comment=f"Custody handoff e-mail delivery: {state}; expires at {expiry}.",
        )


def _update_handoff_journal(entry, *, state, expires_at):
    expiry = timezone.localtime(expires_at).strftime("%Y-%m-%d %H:%M %Z")
    with transaction.atomic():
        entry.comment = f"Custody handoff e-mail delivery: {state}; expires at {expiry}."
        entry.save(update_fields=["comment", "updated_at"])


def _cooldown_allows_handoff(request, receipt):
    alias = getattr(settings, "RATELIMIT_CACHE", "default")
    key = f"custody-handoff-email:{receipt.asset.tenant_id}:{request.user.pk}:{receipt.pk}"
    try:
        return caches[alias].add(key, True, CUSTODY_HANDOFF_COOLDOWN_SECONDS)
    # broad except: availability-tradeoff: a cache failure fails open so handoff e-mail is not blocked
    except Exception:
        return True


def _book_handoff_delivery(request, receipt, signing_session):
    with transaction.atomic():
        locked_session = (
            CustodySigningSession.objects.select_for_update(of=("self",))
            .select_related("intended_holder", "intended_holder__user", "operator")
            .get(pk=signing_session.pk)
        )
        locked_receipt = (
            CustodyReceipt.objects.select_for_update(of=("self",)).select_related("asset", "holder").get(pk=receipt.pk)
        )
        if (
            locked_session.operator_id != request.user.pk
            or locked_session.intended_holder_id != locked_receipt.holder_id
        ):
            raise CustodyHandoffNotFound
        if (
            locked_session.consumed_at is not None
            or locked_session.canceled_at is not None
            or locked_session.expires_at <= timezone.now()
            or locked_receipt.acceptance_status != CustodyReceipt.STATUS_PENDING
            or locked_receipt.holder_id is None
        ):
            raise CustodyHandoffGone

        session_attempts = CustodyHandoffDelivery.objects.filter(
            signing_session_id=locked_session.pk,
            status__in=(
                CustodyHandoffDelivery.STATUS_REQUESTED,
                CustodyHandoffDelivery.STATUS_SUCCEEDED,
                CustodyHandoffDelivery.STATUS_TERMINAL_FAILED,
            ),
        ).count()
        receipt_attempts = CustodyHandoffDelivery.objects.filter(
            signing_session__receipt_id=locked_receipt.pk,
            status__in=(
                CustodyHandoffDelivery.STATUS_REQUESTED,
                CustodyHandoffDelivery.STATUS_SUCCEEDED,
                CustodyHandoffDelivery.STATUS_TERMINAL_FAILED,
            ),
        ).count()
        if (
            session_attempts >= CUSTODY_HANDOFF_SESSION_ATTEMPT_LIMIT
            or receipt_attempts >= CUSTODY_HANDOFF_RECEIPT_ATTEMPT_LIMIT
        ):
            raise CustodyHandoffBoundExceeded

        delivery = CustodyHandoffDelivery.objects.create(
            receipt=locked_receipt,
            signing_session=locked_session,
            operator=request.user,
            attempt=session_attempts + 1,
            status=CustodyHandoffDelivery.STATUS_REQUESTED,
        )
    return delivery, locked_receipt, locked_session


def _update_handoff_delivery(delivery, result):
    if result.disposition == DeliveryDisposition.SUCCESS:
        delivery.status = CustodyHandoffDelivery.STATUS_SUCCEEDED
        delivery.delivered_at = timezone.now()
        delivery.error_class = None
    elif result.disposition == DeliveryDisposition.TERMINAL:
        delivery.status = CustodyHandoffDelivery.STATUS_TERMINAL_FAILED
        delivery.error_class = result.error_class or "terminal"
    else:
        delivery.error_class = result.error_class
    with transaction.atomic():
        delivery.save(update_fields=["status", "error_class", "delivered_at", "updated_at"])


def _record_refused_handoff(request, receipt, signing_session):
    _journal_handoff_event(
        receipt.asset,
        request.user,
        state="refused",
        expires_at=signing_session.expires_at,
    )


def send_custody_handoff_email(request, receipt, signing_session):
    """Book, send, classify, and journal one holder-bound handoff e-mail."""
    if not receipt.holder or not receipt.holder.email:
        raise ValidationError(_("The holder has no e-mail address."))
    if not custody_handoff_email_is_configured():
        raise ValidationError(_("Email is not configured."))
    if not _cooldown_allows_handoff(request, receipt):
        _record_refused_handoff(request, receipt, signing_session)
        raise ValidationError(_("Please wait before sending another handoff e-mail."))

    try:
        delivery, receipt, signing_session = _book_handoff_delivery(request, receipt, signing_session)
    except CustodyHandoffBoundExceeded:
        _record_refused_handoff(request, receipt, signing_session)
        raise ValidationError(_("The handoff e-mail attempt limit has been reached.")) from None

    handoff_url = build_custody_handoff_url(request, receipt, signing_session)
    subject, body, expiry = _custody_handoff_email_content(receipt, signing_session, handoff_url)
    journal_entry = _journal_handoff_event(
        receipt.asset,
        request.user,
        state="requested",
        expires_at=signing_session.expires_at,
    )
    try:
        result = send_email_notification([receipt.holder.email], subject, body, tenant_id=receipt.asset.tenant_id)
    # broad except: boundary-isolation: an e-mail integration failure is classified as a retryable delivery outcome
    except Exception as exc:
        result = DeliveryResult(
            "email.deliver",
            DeliveryDisposition.RETRYABLE,
            error_class=type(exc).__name__,
        )
    _update_handoff_delivery(delivery, result)

    if result.disposition == DeliveryDisposition.SUCCESS:
        state = "succeeded"
    elif result.disposition == DeliveryDisposition.TERMINAL:
        state = "terminal_failed"
    else:
        state = "retryable_unconfirmed"
    _update_handoff_journal(journal_entry, state=state, expires_at=signing_session.expires_at)
    return {
        "delivery": delivery,
        "result": result,
        "expires_at": expiry,
        "remaining_minutes": max(0, int((signing_session.expires_at - timezone.now()).total_seconds() // 60)),
    }


def scope_custody_receipts(queryset, *, user, permission=None):
    """Limit receipts to asset tenants in the request's authorized tenant scope.

    CustodyReceipt deliberately has no tenant field and keeps an unscoped default
    manager for its public bearer-token route. Every internal caller must therefore
    pass through this helper, which scopes on ``asset__tenant`` and can additionally
    require a permission in each candidate tenant.
    """
    if user is None or not user.is_authenticated:
        return queryset.none()

    candidate_tenants = Tenant.objects.for_scope(Scope.current())
    if permission and not user.is_superuser:
        tenant_ids = [tenant.pk for tenant in candidate_tenants if user.has_perm(permission, obj=tenant)]
    else:
        tenant_ids = candidate_tenants.values_list("pk", flat=True)

    return queryset.filter(asset__tenant_id__in=tenant_ids)


def supersede_pending_receipts(assignment):
    """Close the pending receipts of a custody period that is ending.

    Called from the check-in path (reassignment auto-checks-in first), inside its
    transaction. Accepted and declined receipts are signer-authored evidence and
    are never rewritten; open signing sessions of the closed receipts are canceled
    so a prepared handoff cannot be consumed afterwards.
    """
    period = Q(assignment=assignment)
    if assignment.assigned_user_id is not None:
        period |= Q(assignment__isnull=True, asset_id=assignment.asset_id, holder_id=assignment.assigned_user_id)
    pending = list(
        CustodyReceipt.objects.select_for_update()
        .filter(period, acceptance_status=CustodyReceipt.STATUS_PENDING)
        .only("pk")
    )
    if not pending:
        return 0
    ids = [receipt.pk for receipt in pending]
    now = timezone.now()
    # unscoped: sessions of receipts already locked above; check-in may run without a tenant scope
    CustodySigningSession._base_manager.filter(
        receipt_id__in=ids, consumed_at__isnull=True, canceled_at__isnull=True
    ).update(canceled_at=now, updated_at=now)
    CustodyReceipt.objects.filter(pk__in=ids).update(acceptance_status=CustodyReceipt.STATUS_SUPERSEDED, updated_at=now)
    return len(ids)


# ── Signature providers and the one decision path ──────────────────────────

logger = logging.getLogger(__name__)

# Bound the stored evidence string; browsers send well under this.
MAX_EVIDENCE_USER_AGENT_LENGTH = 512


class ProviderUnavailable(Exception):
    """The receipt's provider is unregistered or has no active configuration."""


class CustodyDecisionRefused(Exception):
    """The receipt can no longer take a decision."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def build_custody_sign_url(request, receipt):
    """The holder's signing link, on ITAMBOX_BASE_URL when configured."""
    path = reverse("compliance:custody_eula_sign", kwargs={"token": receipt.token})
    base_url = getattr(settings, "ITAMBOX_BASE_URL", "")
    if base_url:
        return f"{base_url}{path}"
    if request is not None and hasattr(request, "build_absolute_uri"):
        return request.build_absolute_uri(path)
    return path


def _absolute_url(request, path):
    base_url = getattr(settings, "ITAMBOX_BASE_URL", "")
    if base_url:
        return f"{base_url}{path}"
    return request.build_absolute_uri(path) if request is not None else path


def provider_settings_for(config, *, request=None):
    """The ProviderSettings view of a stored config: options, decrypted secrets, callback URL."""
    if config is None:
        return ProviderSettings(config_id=None)
    callback = reverse("compliance:signature_provider_callback", kwargs={"config_pk": config.pk})
    return ProviderSettings(
        config_id=config.pk,
        options=dict(config.options or {}),
        secrets=config.get_secrets(),
        callback_url=_absolute_url(request, callback),
    )


def resolve_provider_config(receipt, provider):
    """The active config for the receipt's provider: the asset tenant's, else the install-wide one."""
    if not provider.requires_config:
        return None
    # unscoped: the public signing flow runs without a tenant scope; the receipt token is the authority
    candidates = SignatureProviderConfig._base_manager.filter(
        provider=provider.name, is_active=True, deleted_at__isnull=True
    )
    tenant_id = receipt.asset.tenant_id
    config = candidates.filter(tenant_id=tenant_id).first() if tenant_id is not None else None
    if config is None:
        config = candidates.filter(tenant__isnull=True).first()
    if config is None:
        raise ProviderUnavailable(f"no active {provider.name!r} configuration for tenant {tenant_id}")
    return config


def start_provider_signature(receipt, provider, *, request, return_url):
    """Let the provider start (or resume) signing; remember which request it opened."""
    config = resolve_provider_config(receipt, provider)
    # Start under the receipt's row lock so two quick visits resume one request
    # at the provider instead of opening two.
    with transaction.atomic():
        locked = CustodyReceipt.objects.select_for_update().select_related("asset", "holder").get(pk=receipt.pk)
        result = provider.start(
            locked, request=request, settings=provider_settings_for(config, request=request), return_url=return_url
        )
        if result is None:
            return None
        if not isinstance(result, StartResult) or not result.redirect_url:
            raise SignatureProviderError(f"provider {provider.name!r} returned no redirect URL")
        reference = (result.provider_reference or "")[:255]
        changed = []
        if config is not None and locked.provider_config_id != config.pk:
            locked.provider_config = config
            changed.append("provider_config")
        if reference and reference != locked.provider_reference:
            locked.provider_reference = reference
            changed.append("provider_reference")
        if changed:
            locked.save(update_fields=[*changed, "updated_at"])
    return result


def receipt_period_is_active(receipt):
    """True while the custody period the receipt evidences is the asset's active one."""
    # Read through the base manager: the signing holder need not be a member of
    # the asset's tenant, and the tenant-scoped manager would hide the assignment.
    if receipt.assignment_id is not None:
        # unscoped: the signing holder may not be a tenant member; the receipt token is the authority
        return AssetAssignment._base_manager.filter(
            pk=receipt.assignment_id, asset_id=receipt.asset_id, is_active=True, deleted_at__isnull=True
        ).exists()
    # unscoped: legacy receipt without a bound period; same token-authority reasoning as above
    return AssetAssignment._base_manager.filter(
        asset_id=receipt.asset_id, assigned_user_id=receipt.holder_id, is_active=True, deleted_at__isnull=True
    ).exists()


def _dispatch_custody_event(receipt, *, actor_id=None, tenant_id=None):
    try:
        dispatch_event(CustodyReceipt, receipt, action="update")
    # broad except: boundary-isolation: event delivery failure must not invalidate a recorded custody decision
    except Exception as exc:
        logger.error(
            "Custody event dispatch failed for receipt_id=%s tenant_id=%s actor_id=%s exception_type=%s",
            receipt.pk,
            tenant_id,
            actor_id,
            type(exc).__name__,
        )


def record_custody_decision(
    receipt,
    outcome: SignatureOutcome,
    *,
    method,
    client_ip="",
    user_agent="",
    signing_session=None,
    actor_id=None,
):
    """Write one holder decision onto a receipt the caller has locked.

    Every provider ends here. The receipt must still be pending and its custody
    period active; an acceptance must carry a well-formed PNG signature when the
    provider supplies one. The decision is sealed, the signing session consumed,
    the asset journal written and the receipt event scheduled after commit.
    """
    if receipt.acceptance_status != CustodyReceipt.STATUS_PENDING:
        raise CustodyDecisionRefused("not_pending")
    if not receipt_period_is_active(receipt):
        raise CustodyDecisionRefused("superseded")
    signature = None
    if outcome.signature_image is not None:
        signature = validated_signature_image(outcome.signature_image)
        if signature is None:
            raise CustodyDecisionRefused("invalid_signature")

    decided_at = timezone.now()
    receipt.ip_address = client_ip or None
    receipt.user_agent = (user_agent or "")[:MAX_EVIDENCE_USER_AGENT_LENGTH]
    if outcome.provider_reference:
        receipt.provider_reference = outcome.provider_reference[:255]
    if outcome.provider_evidence:
        receipt.provider_evidence = outcome.provider_evidence
    if outcome.status == OUTCOME_ACCEPTED:
        receipt.accepted_date = decided_at
        receipt.signed_at = decided_at
        receipt.acceptance_method = method
        receipt.acceptance_status = CustodyReceipt.STATUS_ACCEPTED
        receipt.signature_canvas = signature or ""
        receipt.signature_data = signature or ""
        receipt.eula_version = CustodyReceipt.EULA_VERSION
    else:
        receipt.accepted_date = None
        receipt.signed_at = None
        receipt.acceptance_status = CustodyReceipt.STATUS_DECLINED
    seal_custody_evidence(receipt, decided_at=decided_at)
    receipt.save()

    if signing_session is not None:
        signing_session.consumed_at = decided_at
        signing_session.outcome = receipt.acceptance_status
        signing_session.save(update_fields=["consumed_at", "outcome", "updated_at"])

    asset = receipt.asset
    tenant_id = asset.tenant_id
    transaction.on_commit(lambda: _dispatch_custody_event(receipt, actor_id=actor_id, tenant_id=tenant_id))
    if receipt.acceptance_status == CustodyReceipt.STATUS_ACCEPTED:
        # Journal-style audit write: no asset save, so tag allocation and model
        # validation never re-run and a stale in-request copy of the asset row
        # can never overwrite concurrent edits.
        asset_state = asset._serialize_for_change(asset)
        asset._log_change(
            action="audit",
            prechange_data=asset_state,
            postchange_data=asset_state,
            message=f"EULA digital custody receipt accepted. Evidence seal: {receipt.verification_hash[:16]}...",
        )
    return receipt


def record_provider_outcome(receipt_pk, outcome: SignatureOutcome, *, provider_name, client_ip="", user_agent=""):
    """Lock a receipt and record a provider-reported decision on it."""
    with transaction.atomic():
        receipt = CustodyReceipt.objects.select_for_update().select_related("asset", "holder").get(pk=receipt_pk)
        if receipt.signature_provider != provider_name:
            raise CustodyDecisionRefused("provider_mismatch")
        return record_custody_decision(
            receipt,
            outcome,
            method=f"provider:{provider_name}"[:50],
            client_ip=client_ip,
            user_agent=user_agent,
        )


def handle_provider_callback(config, event: CallbackEvent):
    """Apply an authenticated provider callback; returns a short status word.

    The callback is matched to a receipt through the config that started the
    signing and the provider's own reference, so a config can never reach
    another config's receipts.
    """
    if not event.provider_reference:
        return "unknown_reference"
    receipt_pk = (
        CustodyReceipt.objects.filter(provider_config=config, provider_reference=event.provider_reference)
        .values_list("pk", flat=True)
        .first()
    )
    if receipt_pk is None:
        return "unknown_reference"
    if event.outcome is None:
        return "acknowledged"
    try:
        record_provider_outcome(receipt_pk, event.outcome, provider_name=config.provider)
    except CustodyDecisionRefused as refused:
        return refused.reason
    return event.outcome.status
