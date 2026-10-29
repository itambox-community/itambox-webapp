from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.http import require_POST

from assets.tables import AccessoryTable, AssetTable, ConsumableTable, KitTable
from compliance.models import CustodyTemplate
from compliance.tables import CustodyTemplateTable
from core.managers import Scope
from core.models import Job
from inventory.tables import ComponentTable
from itambox.panels import Panel
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectBulkEditView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from licenses.tables import LicenseTable
from subscriptions.tables import SubscriptionTable

from ..access import tenant_access_report
from ..filters import TenantFilterSet
from ..forms import TenantFilterForm, TenantForm
from ..models import Tenant
from ..services.tenant_onboarding import onboard_managed_tenant
from ..tables import AssetHolderTable, LocationTable, SiteTable, TenantTable


class TenantListView(ObjectListView):
    """Tenant-scoped by default, always — an MSP admin no longer widens this list
    with ``?all_providers=true`` (retired: RBAC stage-3 §3). Superusers still see
    every tenant via the normal unscoped path (no active tenant context ⇒
    ``filter_by_tenant()`` is a no-op). MSP staff reach their customer tenants
    through the switcher (``organization.access.accessible_tenant_ids``) or the
    "Managed Tenants" tab on their own provider tenant's detail page.
    """

    queryset = (
        Tenant.objects.select_related("group", "managed_by")
        .prefetch_related("tags")
        .annotate(
            site_count=Count("sites", distinct=True),
            location_count=Count("locations", distinct=True),
        )
    )
    filterset = TenantFilterSet
    filterset_form = TenantFilterForm
    table = TenantTable
    action_buttons = ("add",)


class TenantDetailView(ObjectDetailView):
    queryset = Tenant.objects.select_related("group").prefetch_related("tags")

    layout = (((Panel("info", _("Tenant Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.get_object()

        context["tenant_site_count"] = tenant.sites.count()
        context["tenant_location_count"] = tenant.locations.count()
        context["tenant_assetholder_count"] = tenant.asset_holders.count()
        context["tenant_asset_count"] = tenant.assets.count()
        context["tenant_accessory_count"] = tenant.accessories.count()
        context["tenant_consumable_count"] = tenant.consumables.count()
        context["tenant_component_count"] = tenant.components.count()

        context["tenant_custody_count"] = (
            CustodyTemplate.objects.for_scope(Scope.current()).filter(tenant=tenant).count()
        )
        context["tenant_license_count"] = tenant.licenses.count()
        context["tenant_kit_count"] = tenant.kits.count()
        context["tenant_subscription_count"] = tenant.subscriptions_org.count()

        # Managed tenants are a DIFFERENT tenant than whichever one is active for
        # this request — the scoped default manager (``tenant.managed_tenants``)
        # would silently return none of them, so count via the explicit-scope default manager.
        context["tenant_managed_count"] = (
            Tenant.objects.filter(managed_by_id=tenant.pk, deleted_at__isnull=True).count() if tenant.is_provider else 0
        )

        tenant_configs = getattr(settings, "ITAMBOX_TENANT_LDAP_CONFIGS", {})
        context["has_ldap"] = tenant.slug in tenant_configs
        return context

    def get_tab_sites(self, request):
        tenant = self.get_object()
        table = SiteTable(tenant.sites.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Sites"),
                "empty_icon": "mdi-office-building",
                "empty_text": _("No sites found for this tenant."),
            },
        )

    def get_tab_locations(self, request):
        tenant = self.get_object()
        table = LocationTable(tenant.locations.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Locations"),
                "empty_icon": "mdi-map-marker-outline",
                "empty_text": _("No locations found for this tenant."),
            },
        )

    def get_tab_assetholders(self, request):
        tenant = self.get_object()
        table = AssetHolderTable(tenant.asset_holders.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Asset Holders"),
                "empty_icon": "mdi-account-group-outline",
                "empty_text": _("No asset holders found for this tenant."),
            },
        )

    def get_tab_assets(self, request):
        tenant = self.get_object()
        table = AssetTable(tenant.assets.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Assets"),
                "empty_icon": "mdi-laptop",
                "empty_text": _("No assets found for this tenant."),
            },
        )

    def get_tab_accessories(self, request):
        tenant = self.get_object()
        table = AccessoryTable(tenant.accessories.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Accessories"),
                "empty_icon": "mdi-keyboard-outline",
                "empty_text": _("No accessories found for this tenant."),
            },
        )

    def get_tab_consumables(self, request):
        tenant = self.get_object()
        table = ConsumableTable(tenant.consumables.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Consumables"),
                "empty_icon": "mdi-water-outline",
                "empty_text": _("No consumables found for this tenant."),
            },
        )

    def get_tab_components(self, request):
        tenant = self.get_object()

        table = ComponentTable(tenant.components.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Component Catalog"),
                "empty_text": _("No components found for this tenant."),
            },
        )

    def get_tab_custody_policies(self, request):
        tenant = self.get_object()

        custody_templates_qs = CustodyTemplate.objects.for_scope(Scope.current()).filter(tenant=tenant)
        table = CustodyTemplateTable(custody_templates_qs, request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Custody Policies & EULAs"),
                "empty_text": _("No custody policies found for this tenant."),
            },
        )

    def get_tab_licenses(self, request):
        tenant = self.get_object()
        table = LicenseTable(tenant.licenses.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Licenses"),
                "empty_icon": "mdi-key-outline",
                "empty_text": _("No licenses found for this tenant."),
            },
        )

    def get_tab_kits(self, request):
        tenant = self.get_object()
        table = KitTable(tenant.kits.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Kits"),
                "empty_icon": "mdi-package-variant",
                "empty_text": _("No kits found for this tenant."),
            },
        )

    def get_tab_subscriptions(self, request):
        tenant = self.get_object()
        table = SubscriptionTable(tenant.subscriptions_org.all(), request=request)
        table.configure(request)
        return render(
            request,
            "generic/tab_table.html",
            {
                "table": table,
                "title": _("Associated Subscriptions"),
                "empty_icon": "mdi-file-document-outline",
                "empty_text": _("No subscriptions found for this tenant."),
            },
        )


class TenantAccessView(LoginRequiredMixin, View):
    """Per-tenant "Who Has Access" audit: lists every user who can reach the tenant,
    the source(s) of their access, and their effective permission count."""

    def get(self, request, pk):
        tenant = get_object_or_404(Tenant, pk=pk)
        has_access = request.user.is_superuser or request.user.has_perm("organization.view_membership", obj=tenant)
        if not has_access:
            raise PermissionDenied(_("You do not have permission to view the access report for this tenant."))
        report = tenant_access_report(tenant)
        return render(
            request,
            "organization/tenants/who_has_access.html",
            {
                "tenant": tenant,
                "access_report": report,
            },
        )


class TenantManagedTenantsTabView(LoginRequiredMixin, View):
    """Lazy ``hx-get`` partial for the "Managed Tenants" tab on an ``is_provider``
    tenant's detail page (RBAC stage-3 §3).

    Deliberately its own URL (``tenants/<pk>/managed/``) rather than the ``?tab=``
    dispatch ``TenantDetailView``'s sibling tabs use — keeps this tab
    coordinate-free from that view's ``get_tab_*`` method surface.
    """

    def get(self, request, pk):
        # get_object_or_404 goes through Tenant.objects (tenant-scoped): this is
        # correct here, since the tab only ever renders on the PROVIDER tenant's
        # own detail page, which is by definition reachable in the requester's
        # current tenant context.
        tenant = get_object_or_404(Tenant, pk=pk)
        has_access = request.user.is_superuser or request.user.has_perm("organization.view_tenant", obj=tenant)
        if not has_access:
            raise PermissionDenied(_("You do not have permission to view this tenant."))
        if not tenant.is_provider:
            raise Http404()

        # _base_manager: managed (customer) tenants are a DIFFERENT tenant than
        # the one active for this request — the scoped default manager would
        # silently return none of them (see "Architecture: tenant scoping").
        managed_tenants = (
            Tenant.objects.filter(managed_by_id=tenant.pk, deleted_at__isnull=True)
            .select_related("group")
            .annotate(
                member_count=Count(
                    "memberships",
                    filter=Q(memberships__is_active=True),
                    distinct=True,
                ),
            )
            .order_by("name")
        )
        can_add_managed_tenant = request.user.is_superuser or request.user.has_perm(
            "organization.add_tenant", obj=tenant
        )
        return render(
            request,
            "organization/tenants/_tab_managed_tenants.html",
            {
                "tenant": tenant,
                "managed_tenants": managed_tenants,
                "can_add_managed_tenant": can_add_managed_tenant,
            },
        )


class TenantEditView(ObjectEditView):
    queryset = Tenant.objects.all()
    model = Tenant
    model_form = TenantForm
    template_name = "generic/object_edit.html"

    def get_form_kwargs(self):
        # ``managed_by_param`` is only a convenience initial value for the
        # authorized selector. TenantForm validates it against the same queryset
        # used for POST data; it is not a separate authority path.
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["managed_by_param"] = self.request.GET.get("managed_by")
        return kwargs

    def form_invalid(self, form):
        if getattr(form, "invalid_managed_by_param", False):
            raise PermissionDenied(_("You do not have permission to onboard a tenant for this provider."))
        return super().form_invalid(form)

    def form_valid(self, form):
        is_creating = self.object is None or self.object.pk is None
        provider_id = None
        if is_creating and not self.request.user.is_superuser:
            provider_id = form.instance.managed_by_id
        if provider_id is None:
            return super().form_valid(form)

        with transaction.atomic():
            self.object = form.save()
            onboard_managed_tenant(
                actor=self.request.user,
                provider_id=provider_id,
                tenant=self.object,
            )

        self.add_success_message(
            _("Created %(model)s <a href='%(url)s'>%(object)s</a>")
            % {
                "model": self.model._meta.verbose_name,
                "url": self.object.get_absolute_url(),
                "object": self.object,
            }
        )
        self.request.session["active_tenant_id"] = self.object.pk
        self.request.session.pop("active_tenant_group_id", None)
        self.request.session.pop("active_all_accessible", None)
        return redirect(self.object.get_absolute_url())


class TenantDeleteView(ObjectDeleteView):
    queryset = Tenant.objects.all()
    model = Tenant
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:tenant_list")


class TenantBulkEditView(ObjectBulkEditView):
    queryset = Tenant.objects.all()

    def _get_bulk_edit_form(self, data=None, model=None):
        form = super()._get_bulk_edit_form(data=data, model=model)
        if not self.request.user.is_superuser:
            for field_name in ("group", "is_provider", "managed_by"):
                form.fields.pop(field_name, None)
        return form


class TenantBulkDeleteView(ObjectBulkDeleteView):
    queryset = Tenant.objects.all()


@login_required
@require_POST
def tenant_ldap_sync(request, pk):
    from django_q.tasks import async_task

    tenant = get_object_or_404(Tenant, pk=pk)

    # LDAP synchronization mutates the tenant named by the URL. Under a
    # tenant-group context an ambient permission can come from a sibling, so
    # anchor authorization at this exact target tenant.
    if not request.user.has_perm("organization.change_tenant", obj=tenant):
        messages.error(request, _("You do not have permission to sync directory settings for this tenant."))
        return redirect(tenant.get_absolute_url())

    tenant_configs = getattr(settings, "ITAMBOX_TENANT_LDAP_CONFIGS", {})
    if tenant.slug not in tenant_configs:
        messages.error(request, _("No LDAP configuration found for tenant '%(name)s'.") % {"name": tenant.name})
        return redirect(tenant.get_absolute_url())

    ct = ContentType.objects.get_for_model(Tenant)
    tenant_id = tenant.pk

    job = Job.objects.create(name=f"LDAP Sync: {tenant.name}", tenant=tenant, model=ct, status=Job.STATUS_PENDING)

    if getattr(settings, "Q_CLUSTER", {}).get("sync", False):
        async_task("core.tasks.sync_tenant_ldap_task", job.pk, tenant.slug, request.user.pk, tenant_id)
    else:
        transaction.on_commit(
            lambda: async_task("core.tasks.sync_tenant_ldap_task", job.pk, tenant.slug, request.user.pk, tenant_id)
        )

    messages.success(
        request,
        _("LDAP synchronization job '%(name)s' enqueued successfully! Tracking progress in real-time.")
        % {"name": job.name},
    )

    try:
        redirect_url = reverse("job_detail", kwargs={"pk": job.pk})
    except NoReverseMatch:
        redirect_url = f"/jobs/{job.pk}/"

    if request.headers.get("HX-Request") or getattr(request, "htmx", False):
        response = HttpResponse()
        response["HX-Redirect"] = redirect_url
        return response

    return redirect(redirect_url)
