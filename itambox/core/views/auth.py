"""Interactive login entry points.

:class:`ITAMboxLoginView` renders the local username/password form together with
one action per genuinely configured external identity provider (see
``core.auth.providers``). Local password login always stays available: no
deployment setting exists that turns it off, so the page never hides it.
"""

from django.contrib.auth import views as auth_views
from django.contrib.auth.views import RedirectURLMixin
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from djangosaml2 import views as djangosaml2_views

from core.auth.discovery import LoginRoute, login_domains_configured, resolve_login_route
from core.auth.providers import get_login_providers
from core.auth.saml import SAML_TENANT_SESSION_KEY, bind_saml_tenant, restore_saml_tenant
from core.forms.auth import LoginForm, LoginIdentifierForm
from core.managers import get_current_tenant


class ITAMboxLoginView(auth_views.LoginView):
    """Django's ``LoginView`` plus the configured SSO entry points."""

    template_name = "registration/login.html"
    authentication_form = LoginForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # ``get_redirect_url()`` returns '' for a missing or unsafe ``next``, so
        # only a validated same-origin destination is ever handed to a provider.
        discovery_enabled = login_domains_configured()
        context.update(
            _login_page_context(
                context["form"],
                LoginIdentifierForm(),
                discovery_enabled=discovery_enabled,
                credentials_focus=context["form"].is_bound,
                sso_providers=[] if discovery_enabled else get_login_providers(self.get_redirect_url()),
            )
        )
        return context


def _login_page_context(
    login_form, identifier_form, *, discovery_enabled, credentials_focus, sso_providers, route=None, next_url=None
):
    """Build the login page context shared by the login and discovery views."""
    context = {
        "form": login_form,
        "identifier_form": identifier_form,
        "login_discovery_enabled": discovery_enabled,
        "sso_providers": sso_providers,
    }
    provider_focus = route is not None and route.mode == "choose" and bool(route.actions)
    if discovery_enabled:
        _move_focus(login_form, identifier_form, credentials=credentials_focus, provider=provider_focus)
    if route is not None:
        context["login_discovery_mode"] = route.mode
        context["login_discovery_actions"] = route.actions
    if next_url is not None:
        context["next"] = next_url
    return context


def _move_focus(login_form, identifier_form, *, credentials, provider=False):
    """Keep exactly one autofocus target on the discovery login page.

    With ``provider`` the target is the first provider action, which the
    template marks itself; both inputs then carry no autofocus.
    """
    username = login_form.fields["username"].widget.attrs
    identifier = identifier_form.fields["identifier"].widget.attrs
    if provider:
        username.pop("autofocus", None)
        identifier.pop("autofocus", None)
    elif credentials:
        identifier.pop("autofocus", None)
        username["autofocus"] = True
    else:
        username.pop("autofocus", None)
        identifier["autofocus"] = True


@method_decorator(csrf_protect, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class LoginDiscoveryView(RedirectURLMixin, View):
    """Resolve an identifier without persisting it or revealing account state."""

    http_method_names = ["get", "post"]
    template_name = "registration/login.html"

    def get(self, request, *args, **kwargs):
        return HttpResponseRedirect(reverse("login"))

    def post(self, request, *args, **kwargs):
        if not login_domains_configured():
            return HttpResponseRedirect(reverse("login"))
        identifier_form = LoginIdentifierForm(request.POST)
        if not identifier_form.is_valid():
            return self._render_login(request, identifier_form, LoginRoute("password", reverse("login"), ()))

        route = resolve_login_route(identifier_form.cleaned_data["identifier"], self.get_redirect_url())
        if route.mode == "redirect" and route.url:
            return HttpResponseRedirect(route.url)
        return self._render_login(request, LoginIdentifierForm(), route, credentials_focus=route.mode == "password")

    def _render_login(self, request, identifier_form, route, credentials_focus=False):
        context = _login_page_context(
            LoginForm(request),
            identifier_form,
            discovery_enabled=True,
            credentials_focus=credentials_focus,
            sso_providers=[],
            route=route,
            next_url=self.get_redirect_url(),
        )
        return render(request, self.template_name, context)


class TenantSamlLoginView(djangosaml2_views.LoginView):
    """SAML AuthnRequest initiator bound to one configured tenant.

    djangosaml2's own entry point has nowhere to learn which tenant a login is
    for, so ``load_saml_config`` would fall back to the ``default`` (or first)
    configuration. Binding happens in ``get`` rather than ``dispatch`` so the
    parent's ``dispatch`` decorators stay in place.
    """

    def get(self, request, *args, **kwargs):
        tenant_slug = kwargs.pop("tenant_slug", None)
        if not self.should_prevent_auth(request):
            bind_saml_tenant(request, tenant_slug)
        return super().get(request, *args, **kwargs)


class TenantSamlAcsView(djangosaml2_views.AssertionConsumerServiceView):
    """Assertion consumer that re-activates the tenant the flow started for.

    The IdP posts back to an anonymous endpoint, so without restoring the pin
    the SP configuration, the SAML backend's JIT provisioning and the session's
    active tenant would all resolve to the wrong tenant. ``post`` is overridden
    instead of ``dispatch`` to preserve the parent's ``csrf_exempt``.
    """

    def post(self, request, *args, **kwargs):
        restore_saml_tenant(request)
        return super().post(request, *args, **kwargs)

    def customize_session(self, user, session_info):
        super().customize_session(user, session_info)
        tenant = get_current_tenant()
        if tenant is not None:
            # Land the user in the tenant they authenticated against rather
            # than TenantMiddleware's first-accessible-tenant default.
            self.request.session["active_tenant_id"] = tenant.pk
            self.request.saml_session.pop(SAML_TENANT_SESSION_KEY, None)
