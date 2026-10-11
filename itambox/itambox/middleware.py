import base64
import os
import re
import uuid
from typing import Any

from django.apps import apps
from django.contrib import messages
from django.http import HttpRequest
from django.utils.translation import gettext as _

from core.authorization_cache import begin_authorization_request, end_authorization_request

# The user/request-id contextvars and their accessors live in the leaf module
# ``core.context``. This middleware is what *populates*
# them, while the tenant-scoping managers and the auth backends *read* them —
# owning them here forced those readers into a circular import back onto this
# module. They are re-exported unchanged so the established
# ``from itambox.middleware import get_current_user`` import sites keep working;
# ``core.context`` is the canonical home for new code.
from core.context import (  # noqa: F401 -- re-exported for existing importers
    _current_user,
    _request_id,
    get_current_all_accessible,
    get_current_csp_nonce,
    get_current_membership,
    get_current_request_id,
    get_current_tenant,
    get_current_tenant_group,
    get_current_user,
    reset_current_csp_nonce,
    set_current_all_accessible,
    set_current_csp_nonce,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
    set_current_user,
)
from core.tenant_access import (
    accessible_tenant_ids,
    active_membership,
    first_active_membership_in,
    get_tenant_access_policy,
)
from core.tenant_scope import (
    _descendant_group_ids_cache,
    get_descendant_tenant_group_ids,
    resolve_default_workspace,
)

from .ratelimit import RateLimitMiddleware  # noqa: F401 -- Django resolves this dotted path at runtime


class CurrentUserMiddleware:
    """
    Middleware to store the current user and a unique request ID in context variables.
    This makes them easily accessible throughout the request lifecycle, especially
    for logging changes, and is fully thread-safe and async-safe.
    """

    def __init__(self, get_response=None):
        self.get_response = get_response

    def __call__(self, request):
        tokens = self.process_request(request)
        try:
            response = self.get_response(request)
        except Exception as e:
            self.process_response(request, None, tokens)
            raise e
        return self.process_response(request, response, tokens)

    def process_request(self, request):
        user = request.user if hasattr(request, "user") and request.user.is_authenticated else None
        # Keep the reset tokens so the prior context is restored (not clobbered to
        # None) — correct for nested/async-shared contexts under ASGI.
        request_id = uuid.uuid4()
        user_token = _current_user.set(user)
        request_id_token = _request_id.set(request_id)
        authorization_token = begin_authorization_request(request_id)
        return (
            user_token,
            request_id_token,
            authorization_token,
        )

    def process_response(self, request, response, tokens=None):
        if tokens is not None:
            user_token, request_id_token, authorization_token = tokens
            end_authorization_request(authorization_token)
            _current_user.reset(user_token)
            _request_id.reset(request_id_token)
        else:
            # Called directly without the entry tokens (e.g. tests, or middleware
            # invoked outside __call__): clear to None as before.
            _current_user.set(None)
            _request_id.set(None)
            end_authorization_request()
        return response


_SAML_HTTPS_FORM_ACTION = "_itambox_saml_https_form_action"
_CSP_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9+/_=-]{1,128}$")


def allow_saml_https_form_action(response):
    """Mark a djangosaml2 response as allowed to POST its form to an HTTPS IdP."""
    setattr(response, _SAML_HTTPS_FORM_ACTION, True)
    return response


class CSPMiddleware:
    """
    Adds Content-Security-Policy headers to all responses.

    Uses a cryptographically secure random base64 nonce for inline scripts and
    nonce-authorized style elements. Runtime style attributes are allowed for
    bundled UI libraries; authored inline styles remain blocked by the source gate.
    """

    def __init__(self, get_response=None):
        self.get_response = get_response

    def __call__(self, request):
        headers = getattr(request, "headers", {})
        parent_nonce = headers.get("X-CSP-Nonce") if headers.get("HX-Request") else ""
        if not parent_nonce or not _CSP_NONCE_PATTERN.fullmatch(parent_nonce):
            parent_nonce = base64.b64encode(os.urandom(16)).decode("utf-8")
        request.csp_nonce = parent_nonce
        nonce_token = set_current_csp_nonce(request.csp_nonce)
        try:
            response = self.get_response(request)
            return self.process_response(request, response)
        finally:
            reset_current_csp_nonce(nonce_token)

    def process_response(self, request, response):
        if getattr(response, "_csp_default_none", False):
            response["Content-Security-Policy"] = "default-src 'none'"
            return response
        nonce = getattr(request, "csp_nonce", "")
        form_action = "'self' https:" if getattr(response, _SAML_HTTPS_FORM_ACTION, False) else "'self'"
        style_nonce = f" 'nonce-{nonce}'" if nonce else ""
        if nonce:
            response["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                f"style-src 'self'{style_nonce} https://rsms.me; "
                f"style-src-elem 'self'{style_nonce} https://rsms.me; "
                "style-src-attr 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "media-src 'self' data:; "
                "connect-src 'self'; "
                "object-src 'none'; "
                "base-uri 'self'; "
                f"form-action {form_action}; "
                "frame-ancestors 'self'"
            )
        else:
            response["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' https://rsms.me; "
                "style-src-elem 'self' https://rsms.me; "
                "style-src-attr 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "media-src 'self' data:; "
                "connect-src 'self'; "
                "object-src 'none'; "
                "base-uri 'self'; "
                f"form-action {form_action}; "
                "frame-ancestors 'self'"
            )
        return response


class TenantMiddleware:
    """
    Middleware to resolve the active tenant from the session or switch_tenant query parameters,
    validate user's membership for that tenant, and bind the active tenant and membership context.
    """

    def __init__(self, get_response=None):
        # Fail loudly during handler construction when the composition root did
        # not register the organization policy. The policy is deliberately not
        # cached: request/test ContextVar overrides must remain visible per call.
        get_tenant_access_policy()
        self.get_response = get_response

    def __call__(self, request):
        prev = self.process_request(request)
        try:
            response = self.get_response(request)
        except Exception as e:
            self.process_response(request, None, prev)
            raise e
        return self.process_response(request, response, prev)

    @staticmethod
    def _apply_default_workspace(request):
        selection = resolve_default_workspace(request.user)
        if selection is None:
            return False

        request.session.pop("active_tenant_id", None)
        request.session.pop("active_tenant_group_id", None)
        request.session.pop("active_all_accessible", None)
        if selection.tenant is not None:
            request.session["active_tenant_id"] = selection.tenant.pk
        elif selection.group is not None:
            request.session["active_tenant_group_id"] = selection.group.pk
        elif selection.all_accessible:
            request.session["active_all_accessible"] = True
        return True

    @staticmethod
    def _resolve_switch_params(request, session_tenant_id, session_group_id, session_all_accessible):
        """Apply a switch_tenant / switch_tenant_group / switch_all_accessible
        query param to the session-derived scope. Selecting a single tenant or
        a group always leaves the all-accessible scope; entering the
        all-accessible scope always drops any single tenant/group pin.
        """
        query_tenant_id = request.GET.get("switch_tenant")
        query_group_id = request.GET.get("switch_tenant_group")
        query_all_accessible = request.GET.get("switch_all_accessible")

        if query_tenant_id is not None:
            if query_tenant_id == "":
                session_tenant_id = None
                session_group_id = None
            else:
                session_tenant_id = query_tenant_id
                session_group_id = None
            request.session["active_tenant_id"] = session_tenant_id
            if "active_tenant_group_id" in request.session:
                del request.session["active_tenant_group_id"]
            session_all_accessible = False
            request.session.pop("active_all_accessible", None)

        elif query_group_id is not None:
            if query_group_id == "":
                session_tenant_id = None
                session_group_id = None
            else:
                session_tenant_id = None
                session_group_id = query_group_id
            request.session["active_tenant_group_id"] = session_group_id
            if "active_tenant_id" in request.session:
                del request.session["active_tenant_id"]
            session_all_accessible = False
            request.session.pop("active_all_accessible", None)

        elif query_all_accessible is not None:
            # Enter the all-accessible scope; drop any single tenant/group pin.
            session_tenant_id = None
            session_group_id = None
            session_all_accessible = True
            request.session["active_all_accessible"] = True
            request.session.pop("active_tenant_id", None)
            request.session.pop("active_tenant_group_id", None)

        if (
            query_tenant_id is None
            and query_group_id is None
            and query_all_accessible is None
            and not request.META.get("HTTP_AUTHORIZATION")
            and not any(
                key in request.session
                for key in ("active_tenant_id", "active_tenant_group_id", "active_all_accessible")
            )
        ):
            # A saved default is only a bootstrap preference. An explicit query or
            # existing session selection always wins.
            TenantMiddleware._apply_default_workspace(request)
            session_tenant_id = request.session.get("active_tenant_id")
            session_group_id = request.session.get("active_tenant_group_id")
            session_all_accessible = bool(request.session.get("active_all_accessible"))

        return session_tenant_id, session_group_id, session_all_accessible

    @staticmethod
    def _scope_signature(tenant_id, group_id, all_accessible):
        """Return a comparable signature of a resolved scope.

        Session ids are integers while switch parameters arrive as strings, so
        the raw values cannot be compared directly.
        """
        return (
            None if tenant_id in (None, "") else str(tenant_id),
            None if group_id in (None, "") else str(group_id),
            bool(all_accessible),
        )

    @staticmethod
    def _apply_scope_notice(request: HttpRequest) -> None:
        """Surface a scope-switch filter reset as a visible message.

        The switcher drops a tenant filter that the new tenant/group scope
        already defines; the notice marker keeps that reset from being silent.
        Callers only reach this after a switch that changed the resolved scope,
        so reloading a switch URL does not replay the message.
        """
        if request.GET.get("scope_notice") != "filters":
            return
        if "tenant" in request.GET:
            # The target scope keeps the tenant filter: nothing was reset.
            return
        if not any(key in request.GET for key in ("switch_tenant", "switch_tenant_group", "switch_all_accessible")):
            return
        messages.info(
            request, _("The tenant filter was reset because the selected scope defines which tenants are shown.")
        )

    @staticmethod
    def _resolve_all_accessible(request, accessible, session_all_accessible):
        """Fail closed: the all-accessible scope is only honoured for a member
        who actually reaches at least one tenant. With none, the scope is
        refused (never widened to global) and resolution falls through to the
        first-accessible-tenant default.
        """
        if accessible:
            return True, session_all_accessible
        request.session.pop("active_all_accessible", None)
        return False, False

    @staticmethod
    def _clear_request_scope(request: HttpRequest) -> None:
        request.active_tenant = None
        request.active_tenant_group = None
        request.active_membership = None
        request.active_all_accessible = False
        set_current_tenant(None)
        set_current_tenant_group(None)
        set_current_membership(None)
        set_current_all_accessible(False)

    @staticmethod
    def _parse_id(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clear_tenant_session_selection(request: HttpRequest) -> None:
        request.session.pop("active_tenant_id", None)
        request.session.pop("active_tenant_group_id", None)

    @staticmethod
    def _resolve_superuser_scope(
        request: HttpRequest,
        tenant_model: Any,
        tenant_group_model: Any,
        session_tenant_id: object,
        session_group_id: object,
    ) -> tuple[Any | None, Any | None]:
        request.session.pop("active_all_accessible", None)
        if session_tenant_id:
            try:
                return tenant_model._base_manager.get(pk=session_tenant_id, deleted_at__isnull=True), None
            except tenant_model.DoesNotExist:
                request.session.pop("active_tenant_id", None)
                return None, None
        if session_group_id:
            try:
                return None, tenant_group_model._base_manager.get(pk=session_group_id, deleted_at__isnull=True)
            except tenant_group_model.DoesNotExist:
                request.session.pop("active_tenant_group_id", None)
        return None, None

    @staticmethod
    def _resolve_selected_tenant(
        request: HttpRequest,
        tenant_model: Any,
        selected_tenant_id: object,
        accessible: set[int],
    ) -> tuple[Any | None, Any | None]:
        tenant_id = TenantMiddleware._parse_id(selected_tenant_id)
        if tenant_id is None or tenant_id not in accessible:
            return None, None
        membership = active_membership(request.user, tenant_id)
        if membership is not None:
            return membership.tenant, membership
        tenant = tenant_model._base_manager.filter(pk=tenant_id, deleted_at__isnull=True).first()
        return tenant, None

    @staticmethod
    def _resolve_selected_group(
        request: HttpRequest,
        tenant_model: Any,
        tenant_group_model: Any,
        selected_group_id: object,
        accessible: set[int],
    ) -> tuple[Any | None, Any | None, bool]:
        group_id = TenantMiddleware._parse_id(selected_group_id)
        group_tenant_ids = set(
            tenant_model._base_manager.filter(
                group_id__in=get_descendant_tenant_group_ids(group_id, live_only=True),
                deleted_at__isnull=True,
            ).values_list("pk", flat=True)
        )
        authorized_group_tenant_ids = accessible & group_tenant_ids
        if not authorized_group_tenant_ids or group_id is None:
            return None, None, False
        group = tenant_group_model._base_manager.get(pk=group_id)
        membership = first_active_membership_in(request.user, authorized_group_tenant_ids)
        return group, membership, True

    @staticmethod
    def _resolve_default_scope(
        request: HttpRequest,
        tenant_model: Any,
        accessible: set[int],
    ) -> tuple[Any | None, Any | None]:
        membership = first_active_membership_in(request.user, accessible)
        if membership is not None:
            return membership.tenant, membership
        if not accessible:
            return None, None
        return (
            tenant_model._base_manager.filter(
                pk__in=accessible,
                deleted_at__isnull=True,
            )
            .order_by("name")
            .first(),
            None,
        )

    @staticmethod
    def _bind_request_scope(
        request: HttpRequest,
        active_tenant: Any | None,
        active_tenant_group: Any | None,
        active_membership: Any | None,
        active_all_accessible: bool,
    ) -> None:
        request.active_tenant = active_tenant
        request.active_tenant_group = active_tenant_group
        request.active_membership = active_membership
        request.active_all_accessible = active_all_accessible
        set_current_tenant(active_tenant)
        set_current_tenant_group(active_tenant_group)
        set_current_membership(active_membership)
        set_current_all_accessible(active_all_accessible)

    def _resolve_standard_scope(
        self,
        request: HttpRequest,
        tenant_model: Any,
        tenant_group_model: Any,
        session_tenant_id: object,
        session_group_id: object,
        session_all_accessible: bool,
        accessible: set[int],
    ) -> tuple[Any | None, Any | None, Any | None, bool]:
        active_tenant = None
        active_tenant_group = None
        active_membership = None
        active_all_accessible = False

        if session_all_accessible:
            active_all_accessible, _ = self._resolve_all_accessible(request, accessible, session_all_accessible)
        elif session_tenant_id:
            active_tenant, active_membership = self._resolve_selected_tenant(
                request,
                tenant_model,
                session_tenant_id,
                accessible,
            )
            if active_tenant is None:
                request.session.pop("active_tenant_id", None)
        elif session_group_id:
            active_tenant_group, active_membership, resolved = self._resolve_selected_group(
                request,
                tenant_model,
                tenant_group_model,
                session_group_id,
                accessible,
            )
            if not resolved:
                request.session.pop("active_tenant_group_id", None)

        if not active_tenant and not active_tenant_group and not active_all_accessible:
            active_tenant, active_membership = self._resolve_default_scope(request, tenant_model, accessible)
            if active_tenant is not None:
                request.session["active_tenant_id"] = active_tenant.pk
                request.session.pop("active_tenant_group_id", None)
            else:
                self._clear_tenant_session_selection(request)

        return active_tenant, active_tenant_group, active_membership, active_all_accessible

    def process_request(self, request: HttpRequest):
        _descendant_group_ids_cache.set(None)
        prev = (
            get_current_tenant(),
            get_current_tenant_group(),
            get_current_membership(),
            get_current_all_accessible(),
        )
        if not hasattr(request, "user") or not request.user.is_authenticated:
            self._clear_request_scope(request)
            return prev

        session_tenant_id = request.session.get("active_tenant_id")
        session_group_id = request.session.get("active_tenant_group_id")
        session_all_accessible = bool(request.session.get("active_all_accessible"))
        previous_scope = self._scope_signature(session_tenant_id, session_group_id, session_all_accessible)
        session_tenant_id, session_group_id, session_all_accessible = self._resolve_switch_params(
            request,
            session_tenant_id,
            session_group_id,
            session_all_accessible,
        )
        if self._scope_signature(session_tenant_id, session_group_id, session_all_accessible) != previous_scope:
            self._apply_scope_notice(request)
        tenant_model = apps.get_model("organization", "Tenant")
        tenant_group_model = apps.get_model("organization", "TenantGroup")

        if request.user.is_superuser:
            active_tenant, active_tenant_group = self._resolve_superuser_scope(
                request,
                tenant_model,
                tenant_group_model,
                session_tenant_id,
                session_group_id,
            )
            active_membership = None
            active_all_accessible = False
        else:
            accessible = accessible_tenant_ids(request.user)
            active_tenant, active_tenant_group, active_membership, active_all_accessible = self._resolve_standard_scope(
                request,
                tenant_model,
                tenant_group_model,
                session_tenant_id,
                session_group_id,
                session_all_accessible,
                accessible,
            )

        self._bind_request_scope(
            request,
            active_tenant,
            active_tenant_group,
            active_membership,
            active_all_accessible,
        )
        return prev

    def process_response(self, request, response, prev=None):
        if prev is not None:
            prev_tenant, prev_group, prev_membership, prev_all_accessible = prev
            set_current_tenant(prev_tenant)
            set_current_tenant_group(prev_group)
            set_current_membership(prev_membership)
            set_current_all_accessible(prev_all_accessible)
        else:
            set_current_tenant(None)
            set_current_tenant_group(None)
            set_current_membership(None)
            set_current_all_accessible(False)
        return response
