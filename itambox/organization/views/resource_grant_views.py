"""Operator UI for cross-tenant resource grants (ADR-0001 phase 4b).

Grants are created FROM a concrete stock pool (the share action on a stock
row binds resource type + id in the URL) — the owner tenant is always
derived from the pool's location, never client-supplied. Revocation is the
generic delete flow (TenantResourceGrant.delete soft-revokes).
"""

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _

from core.managers import get_current_all_accessible, get_current_tenant, get_current_tenant_group
from itambox.views.generic import ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView

from .. import tables
from ..access import accessible_tenant_ids, get_ancestor_tenant_group_ids, get_descendant_tenant_group_ids
from ..forms import TenantResourceGrantForm
from ..models import (
    Tenant,
    TenantResourceGrant,
    TenantResourceGrantExpiryRevocation,
    TenantResourceGrantExpiryRun,
)
from ..services.resource_grants import revoke_resource_grant


def _grants_involving(tenant):
    """Live grants given BY or received BY ``tenant`` (direct or via group)."""
    q = Q(tenant=tenant) | Q(grantee_tenant=tenant)
    ancestor_ids = get_ancestor_tenant_group_ids(tenant.group_id, live_only=True)
    if ancestor_ids:
        q |= Q(grantee_tenant_group_id__in=ancestor_ids)
    return TenantResourceGrant.objects.filter(q)


class TenantResourceGrantListView(ObjectListView):
    queryset = TenantResourceGrant.objects.none()
    table = tables.TenantResourceGrantTable
    document_path = "usage/resource-grants"
    action_buttons = ()

    def get_queryset(self):
        # The grant manager is deliberately unscoped (authorization infra);
        # this view scopes explicitly: everything involving the active tenant.
        tenant = get_current_tenant()
        if tenant is None:
            if self.request.user.is_superuser:
                return TenantResourceGrant.objects.select_related(
                    "tenant",
                    "grantee_tenant",
                    "grantee_tenant_group",
                    "resource_type",
                    "granted_by",
                )
            return TenantResourceGrant.objects.none()
        return _grants_involving(tenant).select_related(
            "tenant",
            "grantee_tenant",
            "grantee_tenant_group",
            "resource_type",
            "granted_by",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Resource Grants")
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (None, _("Tenancy")),
            (None, _("Resource Grants")),
        ]
        return context


class TenantResourceGrantCreateView(ObjectEditView):
    """Share ONE stock pool: /resource-grants/add/<content_type_id>/<resource_id>/."""

    model = TenantResourceGrant
    model_form = TenantResourceGrantForm
    template_name = "generic/object_edit.html"
    default_return_url = "organization:tenantresourcegrant_list"

    def _resolve_pool(self):
        if getattr(self, "_pool", None) is None:
            ct = get_object_or_404(ContentType, pk=self.kwargs["content_type_id"])
            if f"{ct.app_label}.{ct.model}" not in TenantResourceGrant.APPROVED_RESOURCE_MODELS:
                raise Http404()
            model = ct.model_class()
            # unscoped: grant flows read pools and run owners across tenants; authorization is checked explicitly
            stock = (
                model._base_manager.filter(
                    pk=self.kwargs["resource_id"],
                )
                .select_related("location__tenant")
                .first()
            )
            if stock is None or stock.location.tenant_id is None:
                raise Http404()
            self._pool = (ct, stock, stock.location.tenant)
        return self._pool

    def has_permission(self):
        # Anchor at the pool's OWNER tenant: only someone holding the add
        # permission there may share the owner's stock.
        _ct, _stock, owner = self._resolve_pool()
        return self.request.user.has_perms(
            ("organization.add_tenantresourcegrant",),
            obj=owner,
        )

    def get_form(self, form_class=None):
        ct, stock, owner = self._resolve_pool()
        # Bind the non-form fields BEFORE validation (ObjectEditView.get_form
        # passes self.object as the form instance) so the model's full clean
        # (ownership-through-location, allowlist) produces form errors instead
        # of save-time surprises.
        self.object = TenantResourceGrant(
            tenant=owner,
            resource_type=ct,
            resource_id=stock.pk,
            granted_by=self.request.user,
        )
        return super().get_form(form_class)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        _ct, _stock, owner = self._resolve_pool()
        kwargs["owner_tenant"] = owner
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        _ct, stock, owner = self._resolve_pool()
        context["title"] = _("Share stock pool: %(stock)s") % {"stock": stock}
        context["breadcrumbs"] = [
            (reverse("dashboard"), _("Dashboard")),
            (reverse("organization:tenantresourcegrant_list"), _("Resource Grants")),
            (None, _("Share")),
        ]
        return context


class TenantResourceGrantRevokeView(ObjectDeleteView):
    """Revocation = the generic delete flow; the model soft-revokes."""

    model = TenantResourceGrant
    queryset = TenantResourceGrant.objects.all()
    document_path = "usage/resource-grants"
    success_url = reverse_lazy("organization:tenantresourcegrant_list")

    def get_queryset(self):
        # Owner-side only: the grantee cannot revoke (nor 403-probe) a grant.
        tenant = get_current_tenant()
        if tenant is None:
            if self.request.user.is_superuser:
                return TenantResourceGrant.objects.all()
            return TenantResourceGrant.objects.none()
        return TenantResourceGrant.objects.filter(tenant=tenant)

    def form_valid(self, form):
        result = revoke_resource_grant(
            self.object.pk,
            user=self.request.user,
            active_tenant=get_current_tenant(),
        )
        if result is None:
            raise Http404
        messages.success(self.request, _("Resource grant revoked."))
        return HttpResponseRedirect(self.get_success_url())


def _run_owner_ids(request):
    """Owner tenants visible to the run UI in the active request scope."""

    tenant = get_current_tenant()
    group = get_current_tenant_group()
    all_accessible = get_current_all_accessible()
    if request.user.is_superuser and tenant is None and group is None and not all_accessible:
        return None
    if tenant is not None:
        candidate_ids = {tenant.pk}
    elif group is not None:
        candidate_ids = set(
            Tenant.objects.filter(
                group_id__in=get_descendant_tenant_group_ids(group.pk, live_only=True),
                deleted_at__isnull=True,
            ).values_list("pk", flat=True)
        )
        if not request.user.is_superuser:
            candidate_ids &= set(accessible_tenant_ids(request.user))
    elif all_accessible:
        candidate_ids = set(accessible_tenant_ids(request.user))
    else:
        return set()
    live = Tenant.objects.filter(pk__in=candidate_ids, deleted_at__isnull=True)
    if request.user.is_superuser:
        return set(live.values_list("pk", flat=True))
    return {item.pk for item in live if request.user.has_perm("organization.view_tenantresourcegrant", obj=item)}


class TenantResourceGrantExpiryRunListView(ObjectListView):
    queryset = TenantResourceGrantExpiryRun.objects.none()
    table = tables.TenantResourceGrantExpiryRunTable
    action_buttons = ()
    document_path = "usage/resource-grants"
    template_name = "organization/resource_grant_expiry_run_list.html"

    def get_permission_required(self):
        return ("organization.view_tenantresourcegrant",)

    def has_permission(self):
        owner_ids = _run_owner_ids(self.request)
        return owner_ids is None or bool(owner_ids)

    def get_queryset(self):
        owner_ids = _run_owner_ids(self.request)
        if owner_ids is None:
            return TenantResourceGrantExpiryRun.objects.select_related("tenant")
        if not owner_ids:
            return TenantResourceGrantExpiryRun.objects.none()
        return TenantResourceGrantExpiryRun.objects.filter(tenant_id__in=owner_ids).select_related("tenant")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Resource grant expiry runs")
        return context


class TenantResourceGrantExpiryRunDetailView(ObjectDetailView):
    queryset = TenantResourceGrantExpiryRun.objects.select_related("tenant")
    document_path = "usage/resource-grants"
    template_name = "organization/resource_grant_expiry_run_detail.html"
    related_object_exclusions = ("organization.tenantresourcegrantexpiryrevocation",)

    def get_permission_required(self):
        return ("organization.view_tenantresourcegrant",)

    def get_queryset(self):
        owner_ids = _run_owner_ids(self.request)
        if owner_ids is None:
            return TenantResourceGrantExpiryRun.objects.select_related("tenant")
        if not owner_ids:
            return TenantResourceGrantExpiryRun.objects.none()
        return TenantResourceGrantExpiryRun.objects.filter(tenant_id__in=owner_ids).select_related("tenant")

    def has_permission(self):
        owner_ids = _run_owner_ids(self.request)
        if owner_ids is None:
            return True
        obj = self.get_object()
        return obj.tenant_id in owner_ids and self.request.user.has_perm(
            "organization.view_tenantresourcegrant", obj=obj.tenant
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # unscoped: grant flows read pools and run owners across tenants; authorization is checked explicitly
        context["expiry_evidence"] = (
            TenantResourceGrantExpiryRevocation._base_manager.integrity_valid()
            .filter(run=self.object)
            .select_related("grant", "object_change")
        )
        context["title"] = _("Resource grant expiry run")
        return context
