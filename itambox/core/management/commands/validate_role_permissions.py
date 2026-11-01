"""Report-only audit of Role.permissions JSON against real auth.Permission codenames.

Permissions on a ``Role`` are stored as raw ``app_label.codename`` strings in a
JSONField, so there is no FK integrity: a typo, a removed permission, or a renamed model
silently persists as a dead entry. This command scans every role (across all tenants,
unscoped) and flags any codename that does not match an existing ``auth.Permission``.

It never modifies data — it only reports, exiting non-zero when stale codenames are found
so it can gate CI / be wired into a periodic check.
"""

from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Report Role.permissions codenames that do not match any auth.Permission (read-only)."

    def handle(self, *args, **options):
        # inline import: app-registry: avoids AppRegistryNotReady when the command module is imported early
        from organization.models import Role

        valid = {
            f"{app_label}.{codename}"
            for app_label, codename in Permission.objects.values_list("content_type__app_label", "codename")
        }

        # _base_manager: scan every role regardless of tenant scoping / soft-delete state.
        roles = Role._base_manager.all().select_related("tenant")
        total_roles = 0
        roles_with_stale = 0
        stale_total = 0

        for role in roles:
            total_roles += 1
            stale = sorted(p for p in (role.permissions or []) if p not in valid)
            if stale:
                roles_with_stale += 1
                stale_total += len(stale)
                tenant_name = role.tenant.name if role.tenant_id else "(no tenant)"
                self.stdout.write(
                    self.style.WARNING(
                        "Role #%s '%s' [%s] has %s stale codename(s): %s"
                        % (role.pk, role.name, tenant_name, len(stale), ", ".join(stale))
                    )
                )

        if stale_total:
            self.stdout.write(
                self.style.ERROR(
                    f"Found {stale_total} stale codename(s) across {roles_with_stale} of {total_roles} role(s)."
                )
            )
            # Signal failure for CI / scripting without raising a traceback.
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(f"All permission codenames valid across {total_roles} role(s)."))
