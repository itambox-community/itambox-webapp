"""Views for the unified ``Role`` model (tenant-owned, optionally shared down)."""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.generic import View

from core.mfa import role_is_privileged
from itambox.views.generic import (
    ObjectBulkDeleteView,
    ObjectCloneView,
    ObjectDeleteView,
    ObjectDetailView,
    ObjectEditView,
    ObjectListView,
)
from itambox.views.generic.mixins import filter_permitted_rows
from itambox.views.generic.utils import safe_return_url
from organization.services.role_grant_validation import validate_role_grant

from ..filters import RoleFilterSet
from ..forms import RoleAssignUsersForm, RoleFilterForm, RoleForm
from ..forms.role_form import MATRIX_MODELS, get_custom_permissions
from ..models import Membership, Role, RoleGrant, RoleGrantScope, Tenant
from ..tables import RoleTable


def _roles_visible_in(tenant):
    """Roles assignable/visible inside ``tenant``: its own plus the ones its
    managing tenant shares down. ``_base_manager`` because the shared half lives
    outside the active tenant's scope."""
    q = Q(tenant=tenant)
    if tenant.managed_by_id:
        q |= Q(
            tenant_id=tenant.managed_by_id,
            tenant__is_provider=True,
            shared_with_managed=True,
        )
    # unscoped: shared roles live outside the active tenant scope
    return Role._base_manager.filter(deleted_at__isnull=True).filter(q)


def _annotate_member_count(qs):
    live_grants = Q(role_grants__valid_until__isnull=True) | Q(role_grants__valid_until__gt=timezone.now())
    return qs.annotate(
        member_count=Count(
            "role_grants__membership",
            filter=(live_grants & Q(role_grants__membership__is_active=True) & Q(role_grants__scopes__isnull=False)),
            distinct=True,
        )
    )


class RoleListView(ObjectListView):
    queryset = _annotate_member_count(Role.objects.all()).select_related("tenant")
    filterset = RoleFilterSet
    filterset_form = RoleFilterForm
    table = RoleTable
    action_buttons = ("add",)


class RoleDetailView(ObjectDetailView):
    queryset = _annotate_member_count(Role.objects.all())
    template_name = "organization/roles/role_detail.html"

    def get_queryset(self):
        # A managed tenant may view (read-only) the roles shared down to it.
        # Build the base fresh per request rather than reuse the import-baked class
        # attribute (whose tenant scope is frozen to whatever context was active at
        # module import — an order-dependent 404 hazard in tests / on first reverse).
        tenant = getattr(self.request, "active_tenant", None)
        if tenant is not None:
            self.queryset = _annotate_member_count(_roles_visible_in(tenant))
        else:
            self.queryset = _annotate_member_count(Role.objects.all())
        return super().get_queryset()

    def has_permission(self):
        # The generic check evaluates `view_role` against the role's OWNING tenant,
        # which denies a managed-tenant admin a role their managing organization
        # shares down (they hold no membership in the provider). Resolve the
        # boundary explicitly instead: owner context checks against the role, a
        # shared-in context checks `view_role` against the ACTIVE managed tenant.
        try:
            role = self.get_object()  # get_queryset already 404s a role not visible here
        except Http404:
            if self.request.user.is_authenticated:
                raise
            return False
        user = self.request.user
        if user.is_superuser:
            return True
        active = getattr(self.request, "active_tenant", None)
        if active is None:
            return False  # non-superuser global context: fail closed
        if role.tenant_id == active.pk:  # owner context
            return user.has_perm("organization.view_role", obj=role)
        if role.shared_with_managed and role.tenant.is_provider and active.managed_by_id == role.tenant_id:
            # Shared-in: authorize against the managed tenant, not the provider role.
            return user.has_perm("organization.view_role", obj=active)
        return False  # fail closed

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        role = self.object
        groups = {}
        for _key, info in MATRIX_MODELS.items():
            app, model = info["app"], info["model_name"]
            groups.setdefault(info.get("group", "Other"), []).append(
                {
                    "label": info["label"],
                    "read_codename": f"{app}.view_{model}",
                    "create_codename": f"{app}.add_{model}",
                    "edit_codename": f"{app}.change_{model}",
                    "delete_codename": f"{app}.delete_{model}",
                }
            )
        context["matrix_grouped_items"] = groups
        context["custom_permissions"] = get_custom_permissions()
        active = getattr(self.request, "active_tenant", None)
        # Scope the member count to the tenant being viewed so it agrees with the
        # (active-tenant-scoped) members list the link resolves to — a shared-in
        # role must never surface sibling customers' or provider-internal counts.
        members_url = f"{reverse('organization:membership_list')}?role={role.pk}"
        if active is not None:
            member_count = (
                RoleGrant.objects.filter(
                    role=role,
                    membership__tenant=active,
                    membership__is_active=True,
                    scopes__isnull=False,
                )
                .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=timezone.now()))
                .values("membership_id")
                .distinct()
                .count()
            )
        else:
            member_count = getattr(role, "member_count", 0) or 0  # superuser global total
        context["member_count"] = member_count
        context["members_url"] = members_url
        # A role shared down by a managing tenant is read-only here.
        shared_in_role = bool(
            active is not None
            and active.managed_by_id == role.tenant_id
            and role.shared_with_managed
            and role.tenant.is_provider
        )
        # Never link the owning-tenant name to a page the viewer cannot open: link
        # only when they may view that tenant, otherwise the template renders plain
        # text (a managed-tenant admin can read a shared role but not its provider).
        provider_url = None
        if role.tenant_id and (
            self.request.user.is_superuser or self.request.user.has_perm("organization.view_tenant", obj=role.tenant)
        ):
            provider_url = role.tenant.get_absolute_url()
        context["provider_tenant_url"] = provider_url
        context["provider_tenant_name"] = role.tenant.name if role.tenant_id else ""
        role_editable = bool(
            (active is not None and role.tenant_id == active.pk) or (active is None and self.request.user.is_superuser)
        )
        context["role_editable"] = role_editable
        context["shared_in_role"] = shared_in_role
        if not role_editable:
            # Edit/Delete must not surface on a shared-in role viewed from a managed
            # tenant, even for a superuser: ``has_perm(obj=role)`` resolves against
            # the role's OWNING tenant (RoleGrant-agnostic superuser bypass in
            # MembershipBackend.has_perm), so ``can_change``/``can_delete`` can be
            # True here while RoleEditView's tenant-scoped queryset still 404s on
            # this pk. role_editable is the authoritative gate for this page.
            context["can_change"] = False
            context["can_delete"] = False
            context["edit_url"] = None
            context["delete_url"] = None
            context["action_urls"]["edit"] = None
            context["action_urls"]["delete"] = None
        return context


class RoleEditView(ObjectEditView):
    queryset = Role.objects.all()
    model = Role
    model_form = RoleForm
    template_name = "organization/role_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        # A ?tenant=<pk> deep-link pre-binds the new role's owner (e.g. role-less
        # technician onboarding). On edit the form locks to the instance's own
        # tenant, so this is create-only. The owner tenant is AUTHORIZED before the
        # form is built: an explicit deep link to a tenant the requester may not add
        # roles to 404s (same non-confirming pattern as the membership-create view)
        # rather than pre-binding a foreign owner and rendering its form.
        if self.kwargs.get("pk") is None:
            tenant_id = self.request.GET.get("tenant")
            if tenant_id:
                try:
                    tenant = Tenant.objects.filter(
                        pk=tenant_id,
                        deleted_at__isnull=True,
                    ).first()
                except (TypeError, ValueError):  # non-numeric ?tenant= must not 500
                    tenant = None
                if tenant is None or not (
                    self.request.user.is_superuser or self.request.user.has_perm("organization.add_role", obj=tenant)
                ):
                    raise Http404("No role can be added to the requested tenant.")
                kwargs["tenant"] = tenant
        return kwargs


class RoleCloneView(ObjectCloneView):
    """Clone a role's permission set; the form re-binds the owner tenant."""

    model = Role
    model_form = RoleForm
    template_name = "organization/role_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def pre_save_clone(self, original, cloned):
        # Clear the owner so the clone binds to the admin's context tenant on save
        # (a shared role cloned from a managed tenant must not stay provider-owned).
        cloned.tenant = None


class RoleDeleteView(ObjectDeleteView):
    queryset = Role.objects.all()
    model = Role
    template_name = "generic/object_confirm_delete.html"
    success_url = reverse_lazy("organization:role_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        role = self.object
        # Role.delete() is a soft delete (SoftDeleteMixin): RoleGrant rows
        # survive it (RoleGrant is not soft-deletable) as the audit
        # trail, but a deleted role's permissions stop projecting everywhere
        # immediately (MembershipBackend checks role.deleted_at). Surface that
        # explicitly when other tenants actually hold live grants against this
        # shared definition — deleting it is not undone by "it's just soft".
        if (
            role.shared_with_managed
            and RoleGrant.objects.filter(
                role=role,
                scopes__scope_type__in=(
                    RoleGrantScope.SCOPE_TENANT,
                    RoleGrantScope.SCOPE_TENANT_GROUP,
                    RoleGrantScope.SCOPE_ALL_MANAGED,
                ),
            )
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=timezone.now()))
            .exists()
        ):
            context["extra_warning"] = _(
                "This role is shared with managed tenants and still grants access there. "
                "Deleting it preserves the grant records for auditing, but they stop granting access immediately."
            )
        return context


class RoleBulkDeleteView(ObjectBulkDeleteView):
    queryset = Role.objects.all()

    def post(self, request, *args, **kwargs):
        pks = request.POST.getlist("pk")
        model = self._get_model()
        return_url = safe_return_url(
            request,
            request.POST.get("return_url") or request.META.get("HTTP_REFERER"),
            reverse("organization:role_list"),
        )

        if not pks:
            messages.warning(request, _("No roles were selected."))
            return HttpResponseRedirect(return_url)

        # The dispatch gate is ambient: in a tenant-group scope it passes when
        # ``delete_role`` is held in any accessible tenant. Re-anchor the check
        # at each role's owning tenant before deleting a mixed-tenant selection.
        objects_to_delete, skipped = filter_permitted_rows(
            request.user,
            self._get_queryset(pks),
            model,
            "delete",
        )
        if skipped:
            messages.warning(
                request,
                _("Skipped %(count)s %(objects)s you do not have permission to delete.")
                % {
                    "count": skipped,
                    "objects": model._meta.verbose_name_plural,
                },
            )
        if not objects_to_delete:
            messages.warning(request, _("No valid roles selected for deletion."))
            return HttpResponseRedirect(return_url)

        if "_confirm" in request.POST:
            try:
                count = 0
                with transaction.atomic():
                    for obj in objects_to_delete:
                        obj.delete()
                        count += 1
                messages.success(
                    request,
                    ngettext("Deleted %(count)d role.", "Deleted %(count)d roles.", count) % {"count": count},
                )
                return HttpResponseRedirect(return_url)
            except ProtectedError:
                blocked = ", ".join(str(o) for o in objects_to_delete if o.role_grants.exists())
                messages.error(
                    request,
                    _("Cannot delete: the following roles are still referenced and are protected: %(names)s.")
                    % {"names": blocked},
                )
                return HttpResponseRedirect(return_url)

        context = {
            "model": model,
            "model_name": f"{model._meta.app_label}.{model._meta.model_name}",
            "model_verbose_name": model._meta.verbose_name,
            "model_verbose_name_plural": model._meta.verbose_name_plural,
            "objects": objects_to_delete,
            "object_pks": pks,
            "return_url": return_url,
            "title": _("Confirm Bulk Deletion"),
            "breadcrumbs": [
                (reverse("dashboard"), _("Dashboard")),
                (return_url, _("Roles")),
                (None, _("Delete (%(count)d)") % {"count": len(objects_to_delete)}),
            ],
        }
        return self.render_to_response(context)


class RoleAssignUsersView(LoginRequiredMixin, View):
    """Bulk-add users to a Role: get_or_create a Membership at the role's tenant
    plus a direct RoleGrant with an own-tenant scope per user."""

    template_name = "organization/roles/role_assign_users.html"

    def _get_role(self, pk):
        return get_object_or_404(Role, pk=pk)

    def _check_perms(self, request, role):
        # Granting happens at the role's owning tenant.
        tenant = role.tenant
        return request.user.has_perm("organization.add_membership", obj=tenant) and request.user.has_perm(
            "organization.change_membership", obj=tenant
        )

    def _guard_grant(self, request, role):
        """Privilege-escalation guard for granting this role at its own tenant."""
        try:
            validate_role_grant(
                request.user,
                role,
                role.tenant,
                scope_type=RoleGrantScope.SCOPE_OWN,
            )
        except ValidationError as e:
            raise PermissionDenied(", ".join(e.messages)) from None

    def get(self, request, pk, *args, **kwargs):
        role = self._get_role(pk)
        if not self._check_perms(request, role):
            raise PermissionDenied
        self._guard_grant(request, role)
        return render(
            request,
            self.template_name,
            {"role": role, "form": RoleAssignUsersForm(role=role)},
        )

    def post(self, request, pk, *args, **kwargs):
        role = self._get_role(pk)
        if not self._check_perms(request, role):
            raise PermissionDenied
        self._guard_grant(request, role)

        form = RoleAssignUsersForm(request.POST, role=role)
        if form.is_valid():
            users = form.cleaned_data["users"]
            reason = form.cleaned_data.get("reason") or ""
            valid_until = form.cleaned_data.get("valid_until")
            added = updated = unchanged = 0
            with transaction.atomic():
                for user in users:
                    membership, membership_created = Membership.objects.get_or_create(
                        user=user,
                        tenant=role.tenant,
                        defaults={"is_active": True},
                    )
                    existing_own = (
                        RoleGrant.objects.filter(
                            membership=membership,
                            role=role,
                            scopes__scope_type=RoleGrantScope.SCOPE_OWN,
                        )
                        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=timezone.now()))
                        .first()
                    )
                    grant_created = False
                    scope_created = False
                    if existing_own is None:
                        elevated = role_is_privileged(role)
                        grant = RoleGrant(
                            membership=membership,
                            role=role,
                            granted_by=request.user,
                            reason=reason if elevated else "",
                            valid_until=valid_until if elevated else None,
                        )
                        grant.full_clean()
                        grant.save()
                        grant_created = True
                        scope = RoleGrantScope(
                            role_grant=grant,
                            scope_type=RoleGrantScope.SCOPE_OWN,
                        )
                        scope.full_clean()
                        scope.save()
                        scope_created = True
                    if membership_created:
                        added += 1
                    elif grant_created or scope_created:
                        updated += 1
                    else:
                        unchanged += 1
            messages.success(
                request,
                _("Assigned '%(role)s': %(added)d added, %(updated)d updated, %(unchanged)d unchanged.")
                % {
                    "role": role.name,
                    "added": added,
                    "updated": updated,
                    "unchanged": unchanged,
                },
            )
            return redirect(reverse("organization:role_detail", kwargs={"pk": role.pk}))
        return render(request, self.template_name, {"role": role, "form": form})
