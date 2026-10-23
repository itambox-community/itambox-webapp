"""Signature provider interface for custody receipts.

A provider decides *how* a holder signs; ITAMbox decides *whether* the decision
counts. Every provider hands its result back as a :class:`SignatureOutcome`, and
one service (``compliance.services.record_custody_decision``) checks the receipt
is still pending and its custody period still active, writes the decision,
seals the evidence and emits the change log and event, whichever provider
produced it.

A provider takes part at three points:

``start``
    The holder opened their signing link and ITAMbox has verified who they
    are. Return ``None`` to render ITAMbox's own signature pad, or a
    :class:`StartResult` whose ``redirect_url`` sends the holder to the
    provider. ``StartResult.provider_reference`` (an envelope or request id) is
    stored on the receipt; on a repeat visit ``receipt.provider_reference`` is
    already set so the provider can resume instead of opening a second request.

``complete_return``
    The provider sent the holder's browser back to ``return_url``. Return the
    outcome if the provider can already report it, else ``None`` and wait for
    the callback.

``parse_callback``
    The provider calls ``/compliance/custody/providers/<config id>/callback/``.
    The provider MUST authenticate the request itself (for example an HMAC
    header checked against a secret from ``settings.secrets``) and raise
    :class:`CallbackRejected` otherwise; the route carries no other
    authentication.

Providers that talk to an external service read their account details from a
per-tenant :class:`~compliance.models.SignatureProviderConfig`, resolved for the
receipt's asset tenant with an install-wide (tenant-less) config as fallback,
and handed to every hook as :class:`ProviderSettings`.

Plugins contribute providers with ``PluginConfig.signature_providers``
(Experimental), a list of :class:`BaseSignatureProvider` subclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext_lazy as _

OUTCOME_ACCEPTED = "accepted"
OUTCOME_DECLINED = "declined"


class SignatureProviderError(Exception):
    """A provider could not complete a step."""


class CallbackRejected(SignatureProviderError):
    """A callback failed the provider's own authentication or could not be parsed."""


@dataclass(frozen=True)
class ProviderSettings:
    """The resolved configuration a provider works with for one receipt."""

    config_id: int | None
    options: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)
    callback_url: str = ""


@dataclass(frozen=True)
class StartResult:
    redirect_url: str
    provider_reference: str = ""


@dataclass(frozen=True)
class SignatureOutcome:
    """A holder's decision as a provider reports it."""

    status: str
    signature_image: str | None = None
    provider_reference: str = ""
    provider_evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in (OUTCOME_ACCEPTED, OUTCOME_DECLINED):
            raise ValueError(f"unknown signature outcome {self.status!r}")


@dataclass(frozen=True)
class CallbackEvent:
    """What an authenticated callback says: which request, and its outcome if final."""

    provider_reference: str
    outcome: SignatureOutcome | None = None


class BaseSignatureProvider:
    #: Stable identifier stored on templates and receipts.
    name: str = ""
    verbose_name: Any = ""
    #: Whether the provider needs a SignatureProviderConfig to run.
    requires_config: bool = True
    #: Option and secret keys a config must carry (checked when it is saved).
    required_options: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()

    def validate_config(self, options: dict[str, Any], secrets: dict[str, Any]) -> list[str]:
        """Return human-readable problems with a config; empty when it is usable."""
        problems = [
            str(_("Missing option: %(key)s")) % {"key": key} for key in self.required_options if not options.get(key)
        ]
        problems += [
            str(_("Missing secret: %(key)s")) % {"key": key} for key in self.required_secrets if not secrets.get(key)
        ]
        return problems

    def start(self, receipt, *, request, settings: ProviderSettings, return_url: str) -> StartResult | None:
        raise NotImplementedError

    def complete_return(self, receipt, *, request, settings: ProviderSettings) -> SignatureOutcome | None:
        return None

    def parse_callback(self, request, *, settings: ProviderSettings) -> CallbackEvent:
        raise CallbackRejected("this provider does not accept callbacks")


class LocalSignatureProvider(BaseSignatureProvider):
    """ITAMbox's own signature pad; the decision is posted to the signing page."""

    name = "local"
    verbose_name = _("Local Canvas Signature Pad")
    requires_config = False

    def start(self, receipt, *, request, settings, return_url):
        return None
