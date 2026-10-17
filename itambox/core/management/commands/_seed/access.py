"""Access seed mixin: users, per-tenant roles, memberships, and grants.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.access import SeedAccessMixin

    class Command(SeedAccessMixin, BaseCommand):
        ...

``_seed_access`` must run after ``_seed_organizations`` (it reads
``self._tenants`` / ``self._tenant_meta`` / ``self._tenant_holders`` /
``self._provider_tenant`` / ``self._orgs``). It populates ``self._roles``,
``self._users``, ``self._engineer_users`` and ``self._provisioner`` (consumed
by every later phase that needs an acting user).

Grant shape (per-grant RBAC): a person is anchored to a tenant by ONE
``Membership``; everything they may do is carried by ``RoleGrant`` rows and
their additive ``RoleGrantScope`` children. MSP staff hold a single membership
at the managing (``is_provider``) tenant; scopes project their grants into
managed tenants, never a second membership.
"""

import random
from datetime import timedelta
from functools import partial

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import CommandError
from django.utils import timezone

from core.mfa import role_is_privileged
from organization.services.role_permission_policy import active_role_permission_targets

User = get_user_model()

TECHNICIAN_CUSTODY_DENIED_PERMISSIONS = frozenset(
    {
        "compliance.add_custodyreceipt",
        "compliance.change_custodyreceipt",
        "compliance.delete_custodyreceipt",
        "compliance.add_custodytemplate",
        "compliance.change_custodytemplate",
        "compliance.delete_custodytemplate",
        "compliance.export_custodyreceipt",
    }
)
TECHNICIAN_CUSTODY_REQUIRED_PERMISSIONS = (
    "compliance.view_custodytemplate",
    "compliance.view_custodyreceipt",
    "compliance.prepare_custodyreceipt",
)

CORE_OPERATIONAL_APPS = frozenset(
    {
        "assets",
        "inventory",
        "organization",
        "compliance",
        "licenses",
        "subscriptions",
        "software",
        "procurement",
        "extras",
        "core",
    }
)


def _permission_name(permission):
    return f"{permission.content_type.app_label}.{permission.codename}"


def _technician_permissions(all_permissions, operational_apps, plugin_models=()):
    available = {_permission_name(permission) for permission in all_permissions}
    permissions = [
        _permission_name(permission)
        for permission in all_permissions
        if (
            permission.content_type.app_label in operational_apps
            or (permission.content_type.app_label, permission.content_type.model) in plugin_models
        )
        and not permission.codename.startswith("delete_")
        and _permission_name(permission) not in TECHNICIAN_CUSTODY_DENIED_PERMISSIONS
    ]
    permissions.extend(
        permission
        for permission in TECHNICIAN_CUSTODY_REQUIRED_PERMISSIONS
        if permission in available and permission not in permissions
    )
    return permissions


def _technician_plugin_models():
    """Return model labels declared by active plugins for Technician permissions."""
    return {(target.app, target.model) for target in active_role_permission_targets() if target.plugin_name is not None}


def _is_admin_seed_account(user):
    username = (user.username or "").lower()
    return username == "admin" or username.startswith("admin@")


def _holder_name(user):
    name_parts = (user.email or user.username).split("@", 1)[0].replace("_", ".").split(".")
    first_name = (user.first_name or name_parts[0] or "Demo").strip()
    last_name = (user.last_name or " ".join(name_parts[1:]) or "User").strip()
    return first_name, last_name


def _ensure_holder(user, tenant):
    """Return the user's active holder profile in ``tenant``, creating or linking one."""
    AssetHolder = apps.get_model("organization", "AssetHolder")
    fallback_upn = user.email or user.username
    candidate_upns = [user.username]
    if fallback_upn not in candidate_upns:
        candidate_upns.append(fallback_upn)

    for upn in candidate_upns:
        holder = AssetHolder._base_manager.filter(
            tenant=tenant,
            upn=upn,
            deleted_at__isnull=True,
        ).first()
        if holder is None:
            continue
        if holder.user_id not in (None, user.pk):
            raise CommandError(
                f"Seed holder invariant failed: active UPN {upn!r} in tenant {tenant.slug!r} "
                f"already belongs to another user."
            )
        if holder.user_id is None:
            holder.user = user
            holder.save(update_fields=["user"])
        return holder

    first_name, last_name = _holder_name(user)
    return AssetHolder._base_manager.create(
        first_name=first_name,
        last_name=last_name,
        upn=fallback_upn,
        email=fallback_upn,
        user=user,
        tenant=tenant,
    )


def check_seed_access_invariants(users=None):
    """Fail closed unless every seeded named person has one holder and membership."""
    Membership = apps.get_model("organization", "Membership")
    AssetHolder = apps.get_model("organization", "AssetHolder")
    users = User._base_manager.all() if users is None else users
    violations = []

    for user in users:
        if not user.has_usable_password():
            continue
        active_membership_tenant_ids = set(
            Membership._base_manager.filter(user_id=user.pk, is_active=True).values_list("tenant_id", flat=True)
        )
        reasons = []
        if not active_membership_tenant_ids:
            reasons.append("no active membership")
        if not _is_admin_seed_account(user):
            holders = AssetHolder._base_manager.filter(
                user_id=user.pk,
                deleted_at__isnull=True,
            )
            holder_count = holders.count()
            if holder_count != 1:
                reasons.append(f"{holder_count} active AssetHolder profiles")
            elif holders.values_list("tenant_id", flat=True).first() not in active_membership_tenant_ids:
                reasons.append("holder tenant is not an active membership tenant")
        if reasons:
            violations.append(f"{user.username}: {', '.join(reasons)}")

    if violations:
        raise CommandError("Seed access invariant failed: " + "; ".join(violations))


def _grant_access(
    user,
    tenant,
    role,
    *,
    seed_grant_expiry,
    granted_by=None,
    scope_type=None,
    scoped_tenants=None,
    reason="",
):
    """Create a membership-backed grant and its requested additive scopes."""
    Membership = apps.get_model("organization", "Membership")
    RoleGrant = apps.get_model("organization", "RoleGrant")
    RoleGrantScope = apps.get_model("organization", "RoleGrantScope")
    scope_type = scope_type or RoleGrantScope.SCOPE_OWN
    membership, _ = Membership.objects.get_or_create(user=user, tenant=tenant)
    privileged = role_is_privileged(role)
    if privileged and not reason.strip():
        raise ValueError(f"Privileged seed grant {role} requires a reason.")

    role_grant = RoleGrant.objects.filter(membership=membership, role=role).order_by("pk").first()
    if role_grant is None:
        role_grant = RoleGrant(
            membership=membership,
            role=role,
            granted_by=granted_by,
            reason=reason,
            valid_until=seed_grant_expiry if privileged else None,
        )
        role_grant.full_clean()
        role_grant.save()
    elif privileged:
        role_grant.granted_by = granted_by
        role_grant.reason = reason
        role_grant.valid_until = seed_grant_expiry
        role_grant.full_clean()
        role_grant.save(update_fields=["granted_by", "reason", "valid_until"])

    if scope_type == RoleGrantScope.SCOPE_TENANT:
        if not scoped_tenants:
            raise ValueError("A tenant-scoped seed grant requires at least one tenant.")
        scope_specs = [{"scope_type": scope_type, "tenant": scoped_tenant} for scoped_tenant in scoped_tenants]
    else:
        scope_specs = [{"scope_type": scope_type}]

    for scope_spec in scope_specs:
        if RoleGrantScope.objects.filter(role_grant=role_grant, **scope_spec).exists():
            continue
        scope = RoleGrantScope(role_grant=role_grant, **scope_spec)
        scope.full_clean()
        scope.save()
    return role_grant


class SeedAccessMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    def _seed_customer_admins(self, grant):
        """Seed technical customer-admin accounts without holder profiles."""
        customer_admins = 0
        for org in self._orgs:
            if org["kind"] != "customer":
                continue
            domain = org["domain"]
            label = org["group"][0] if org["group"] else org["tenants"][0]["name"]
            username = f"admin@{domain}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": username,
                    "first_name": "IT",
                    "last_name": f"Admin ({label})",
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            if created:
                user.set_password(settings.SEED_PASSWORD)
                user.save()
            self._users[username] = user
            customer_admins += 1
            for tenant_spec in org["tenants"]:
                slug = tenant_spec["slug"]
                grant(
                    user,
                    self._tenants[slug],
                    self._roles[(slug, "Administrator")],
                    granted_by=self._provisioner,
                    reason="Demo seed: customer administrator access.",
                )

        return customer_admins

    def _seed_named_customer_users(self, grant):
        """Seed customer logins backed by existing holder profiles."""
        # Per tenant, promote one existing holder to Asset Manager and a few
        # more to Read-Only. They log in with their own holder identity.
        team_leads = 0
        end_users = 0
        for slug, tenant in self._tenants.items():
            if self._tenant_meta[slug]["kind"] == "msp":
                continue
            holders = [holder for holder in self._tenant_holders.get(slug, []) if holder.user_id is None]
            if not holders:
                continue
            scoped_logins = [(holders[0], "Asset Manager")]
            scoped_logins.extend((holder, "Read-Only") for holder in holders[1 : 1 + random.randint(2, 3)])
            for holder, role_name in scoped_logins:
                username = holder.upn
                user, created = User.objects.get_or_create(
                    username=username,
                    defaults={
                        "email": holder.email or username,
                        "first_name": holder.first_name,
                        "last_name": holder.last_name,
                        "is_staff": False,
                        "is_superuser": False,
                    },
                )
                if created:
                    user.set_password(settings.SEED_PASSWORD)
                    user.save()
                self._users[username] = user
                _ensure_holder(user, tenant)
                grant(
                    user,
                    tenant,
                    self._roles[(slug, role_name)],
                    granted_by=self._provisioner,
                    reason=f"Demo seed: {role_name} access for a customer user.",
                )
                if role_name == "Asset Manager":
                    team_leads += 1
                else:
                    end_users += 1

        return team_leads, end_users

    def _seed_access(self):
        from organization.models import Membership, Role, RoleGrant, RoleGrantScope

        self.stdout.write("--- Access: users, roles, role grants ---")
        seed_grant_expiry = timezone.now() + timedelta(days=3650)
        grant = partial(_grant_access, seed_grant_expiry=seed_grant_expiry)

        # Build permission catalogs from Django's permission table.
        all_perms = [
            permission
            for permission in Permission.objects.select_related("content_type")
            if permission.content_type.model_class() is not None
        ]

        ADMIN = [_permission_name(p) for p in all_perms]
        TECH = _technician_permissions(all_perms, CORE_OPERATIONAL_APPS, _technician_plugin_models())
        ASSETMGR = [
            _permission_name(p)
            for p in all_perms
            if p.content_type.app_label in {"assets", "inventory", "compliance", "organization", "procurement"}
            and p.codename.split("_")[0] in {"view", "add", "change"}
        ]
        READONLY = [_permission_name(p) for p in all_perms if p.codename.startswith("view_")]
        ROLE_PERMS = {"Administrator": ADMIN, "Technician": TECH, "Asset Manager": ASSETMGR, "Read-Only": READONLY}

        # Per-tenant local roles: every tenant owns its own four role definitions.
        self._roles = {}  # (tenant_slug, role_name) -> Role
        for slug, tenant in self._tenants.items():
            for role_name, perms in ROLE_PERMS.items():
                role, _created = Role.objects.get_or_create(
                    tenant=tenant,
                    name=role_name,
                    defaults={"permissions": perms, "description": f"{role_name} role for {tenant.name}"},
                )
                role.permissions = perms
                role.save(update_fields=["permissions"])
                self._roles[(slug, role_name)] = role

        # MSP-owned shared roles. The managing tenant additionally owns "MSP Technician"
        # and shares it (plus its local "Read-Only") with the managed tenants: managed
        # tenants may assign these to their own members, only the MSP can edit them.
        # There is no separate provider-admin role any more — the engineers' MSP-wide
        # power (tenant/membership/group CRUD) is the MSP tenant's local Administrator.
        msp_tenant = self._provider_tenant
        msp_slug = msp_tenant.slug
        msp_tech_role, _ = Role.objects.get_or_create(
            tenant=msp_tenant,
            name="MSP Technician",
            defaults={
                "permissions": TECH,
                "shared_with_managed": True,
                "description": "Operate managed customer tenants. Shared definition: "
                "managed tenants may assign it, only the MSP edits it.",
            },
        )
        msp_tech_role.permissions = TECH
        msp_tech_role.save(update_fields=["permissions"])
        self._roles[(msp_slug, "MSP Technician")] = msp_tech_role
        shared_readonly_role = self._roles[(msp_slug, "Read-Only")]
        for shared_role in (msp_tech_role, shared_readonly_role):
            if not shared_role.shared_with_managed:
                shared_role.shared_with_managed = True
                shared_role.save(update_fields=["shared_with_managed"])

        # MSP staff (login users). (username, full_name, kind, assigned_group_slugs or None=all)
        # ONE membership at the MSP tenant per person; reach into managed tenants is a
        # scoped RoleGrant (no per-customer-tenant staff memberships).
        self._users = {}
        self._engineer_users = []
        msp_domain = "northwind-it.com"
        staff = [
            ("lars.eklund", "Lars Eklund", "engineer", None),  # Lead infra engineer
            ("deepa.rao", "Deepa Rao", "engineer", None),  # Senior engineer
            ("tom.berger", "Tom Berger", "engineer", None),  # Field engineer
            ("sara.lind", "Sara Lind", "engineer", None),  # Field engineer
            ("ravi.anand", "Ravi Anand", "helpdesk", None),  # Service desk L1
            ("mia.koch", "Mia Koch", "helpdesk", None),  # Service desk L1
            ("nadia.haas", "Nadia Haas", "account", ["helix-biopharma", "sterling-am"]),
            ("peter.voss", "Peter Voss", "account", ["meridian-bank"]),
        ]
        own_role_for_kind = {"engineer": "Administrator", "helpdesk": "Technician", "account": "Read-Only"}
        for username, full_name, kind, group_scope in staff:
            first, last = full_name.split(" ", 1)
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@{msp_domain}",
                    "first_name": first,
                    "last_name": last,
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            if created:
                user.set_password(settings.SEED_PASSWORD)
                user.save()
            self._users[username] = user
            _ensure_holder(user, msp_tenant)
            if kind == "engineer":
                self._engineer_users.append(user)

            # Local power inside the MSP tenant itself (engineers = full Administrator).
            grant(
                user,
                msp_tenant,
                self._roles[(msp_slug, own_role_for_kind[kind])],
                reason=f"Demo seed: {kind} access in the provider tenant.",
            )

            # Reach into the managed tenants.
            if kind == "account":
                # Account managers: read-only, limited to their assigned customer groups.
                assigned = [
                    t for slug, t in self._tenants.items() if self._tenant_meta[slug]["group_slug"] in group_scope
                ]
                grant(
                    user,
                    msp_tenant,
                    shared_readonly_role,
                    scope_type=RoleGrantScope.SCOPE_TENANT,
                    scoped_tenants=assigned,
                    reason="Demo seed: account access to assigned managed tenants.",
                )
            else:
                # Engineers and helpdesk operate every managed tenant as technicians.
                grant(
                    user,
                    msp_tenant,
                    msp_tech_role,
                    scope_type=RoleGrantScope.SCOPE_ALL_MANAGED,
                    reason=f"Demo seed: {kind} access to all managed tenants.",
                )

        if not self._engineer_users:
            self._engineer_users = list(self._users.values())
        self._provisioner = self._engineer_users[0]

        customer_admins = self._seed_customer_admins(grant)
        team_leads, end_users = self._seed_named_customer_users(grant)

        managed_count = sum(1 for t in self._tenants.values() if t.managed_by_id == msp_tenant.pk)
        staff_reach = (
            RoleGrant.objects.filter(
                membership__tenant=msp_tenant,
                scopes__scope_type__in=(
                    RoleGrantScope.SCOPE_TENANT,
                    RoleGrantScope.SCOPE_TENANT_GROUP,
                    RoleGrantScope.SCOPE_ALL_MANAGED,
                ),
            )
            .distinct()
            .count()
        )
        self.stdout.write(
            f"  MSP layer: 1 managing tenant, {managed_count} managed tenants, "
            f"{staff_reach} managed-scope staff grants."
        )

        total_memberships = Membership.objects.count()
        total_grants = RoleGrant.objects.count()
        self.stdout.write(
            f"  {team_leads} single-tenant team leads (Asset Manager), {end_users} single-tenant read-only end users."
        )
        self.stdout.write(
            f"  {len(self._roles)} roles, {len(self._users)} login users "
            f"({customer_admins} customer admins), {total_memberships} memberships, "
            f"{total_grants} role grants."
        )
