from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError

from core.auth.ldap import (
    LDAPConfigurationError,
    LDAPDependencyUnavailableError,
    classify_ldap_error,
    django_auth_ldap_installed,
    ldap,
)
from core.context import get_current_request_id
from core.errors import IntegrationContext
from core.tasks.context import TaskContext
from core.tasks.management import SystemTaskCommand
from itambox.middleware import get_current_user
from organization.models import Tenant
from organization.services import identity_provisioning

User = get_user_model()
_LDAP_PROVIDER_ERROR = getattr(ldap, "LDAPError", Exception)


def _ldap_context(tenant, operation):
    actor = get_current_user()
    request_id = get_current_request_id()
    return IntegrationContext(
        provider="ldap",
        operation=operation,
        tenant_id=getattr(tenant, "pk", None),
        actor_id=getattr(actor, "pk", None),
        request_id=str(request_id) if request_id else None,
    )


def _initialize_connection(server_uri, tenant):
    try:
        connection = ldap.initialize(server_uri)
        connection.set_option(ldap.OPT_REFERRALS, 0)
        connection.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
        return connection
    except _LDAP_PROVIDER_ERROR as exc:
        raise classify_ldap_error(exc, context=_ldap_context(tenant, "sync.connect")) from exc


def _require_real_ldap_backend():
    if not django_auth_ldap_installed:
        raise ImportError


def _directory_text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


class Command(SystemTaskCommand):
    help = "Sync users from LDAP directory into local Django users for a specific tenant scope"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Slug of the tenant to sync users for")

    def handle(self, *args, **options):
        tenant_slug = options["tenant"]
        try:
            tenant = Tenant._base_manager.get(slug=tenant_slug, deleted_at__isnull=True)
        except Tenant.DoesNotExist:
            raise CommandError("The requested LDAP tenant does not exist.") from None

        # TaskContext sets the tenant scope AND wires _request_id + _current_user
        # so that ChangeLoggingMixin records User saves during the sync.
        # Direct CLI execution has no actor and remains system-attributed. When
        # called from sync_tenant_ldap_task, carry its actor into this nested
        # context instead of replacing it with user=None.
        outer_actor = get_current_user()
        with TaskContext(
            tenant_id=tenant.pk,
            user_id=getattr(outer_actor, "pk", None),
        ):
            self._run_sync(tenant)

    def _run_sync(self, tenant):
        self.stdout.write(self.style.NOTICE(f"Scoping LDAP synchronization (tenant_id={tenant.pk})"))

        tenant_configs = getattr(settings, "ITAMBOX_TENANT_LDAP_CONFIGS", {})
        config = tenant_configs.get(tenant.slug)

        if not config:
            raise LDAPConfigurationError(context=_ldap_context(tenant, "sync.configure"))
        if not isinstance(config, dict):
            raise LDAPConfigurationError(context=_ldap_context(tenant, "sync.configure"))

        server_uri = config.get("SERVER_URI") or config.get("server_uri")
        bind_dn = config.get("BIND_DN") or config.get("bind_dn")
        bind_password = config.get("BIND_PASSWORD") or config.get("bind_password")
        user_search_base = config.get("USER_SEARCH_BASE") or config.get("user_search_base")
        user_search_filter = config.get("USER_SEARCH_FILTER") or config.get("user_search_filter")

        if not user_search_base or not user_search_filter:
            user_search = config.get("USER_SEARCH") or config.get("user_search")
            if user_search and isinstance(user_search, dict):
                if not user_search_base:
                    user_search_base = user_search.get("base_dn") or user_search.get("base")
                if not user_search_filter:
                    user_search_filter = user_search.get("filter")

        if not user_search_filter:
            user_search_filter = "(uid=%(user)s)"
        if not isinstance(user_search_filter, str):
            raise LDAPConfigurationError(context=_ldap_context(tenant, "sync.configure"))
        require_group = config.get("REQUIRE_GROUP") or config.get("require_group")

        if not server_uri or not bind_dn or not user_search_base:
            raise LDAPConfigurationError(context=_ldap_context(tenant, "sync.configure"))

        try:
            _require_real_ldap_backend()
        except ImportError:
            raise LDAPDependencyUnavailableError(context=_ldap_context(tenant, "sync.dependency")) from None

        self.stdout.write("Connecting to configured LDAP server...")
        connection = _initialize_connection(server_uri, tenant)
        operation = "sync.bind"

        try:
            connection.simple_bind_s(bind_dn, bind_password)
            self.stdout.write(self.style.SUCCESS("LDAP bind successful."))
        except _LDAP_PROVIDER_ERROR as exc:
            connection.unbind_s()
            raise classify_ldap_error(exc, context=_ldap_context(tenant, operation)) from exc

        search_filter = user_search_filter
        if "%(user)s" in search_filter:
            search_filter = search_filter.replace("%(user)s", "*")

        scope = ldap.SCOPE_SUBTREE
        retrieve_attrs = ["uid", "cn", "sn", "givenName", "mail", "memberOf"]
        operation = "sync.search"

        try:
            result_id = connection.search(user_search_base, scope, search_filter, retrieve_attrs)
            created_count = 0
            updated_count = 0

            while True:
                result_type, result_data = connection.result(result_id, 0)
                if not result_data:
                    break
                if result_type == ldap.RES_SEARCH_ENTRY:
                    for _dn, entry in result_data:
                        uid_vals = entry.get("uid", [])
                        mail_vals = entry.get("mail", [])
                        cn_vals = entry.get("cn", [])
                        sn_vals = entry.get("sn", [])
                        gn_vals = entry.get("givenName", [])

                        if not uid_vals:
                            continue

                        username = _directory_text(uid_vals[0])
                        email = _directory_text(mail_vals[0]) if mail_vals else ""
                        first_name = _directory_text(gn_vals[0]) if gn_vals else ""
                        last_name = _directory_text(sn_vals[0]) if sn_vals else ""
                        if not last_name and cn_vals:
                            last_name = _directory_text(cn_vals[0])

                        if require_group:
                            member_of = [_directory_text(value) for value in entry.get("memberOf", [])]
                            if require_group not in member_of:
                                continue

                        user, created = User.objects.update_or_create(
                            username=username,
                            defaults={
                                "email": email,
                                "first_name": first_name,
                                "last_name": last_name,
                                "is_active": True,
                            },
                        )
                        identity_provisioning.provision_ldap_directory_identity(
                            identity_provisioning.LDAPDirectoryIdentityCommand(
                                user=user,
                                tenant=tenant,
                            )
                        )

                        if created:
                            created_count += 1
                            self.stdout.write(self.style.SUCCESS(f"Created directory user_id={user.pk}"))
                        else:
                            updated_count += 1
                            self.stdout.write(f"Updated directory user_id={user.pk}")

            self.stdout.write(
                self.style.SUCCESS(
                    "LDAP sync complete for tenant '%s'. Created: %s, Updated: %s"
                    % (tenant.slug, created_count, updated_count)
                )
            )

        except _LDAP_PROVIDER_ERROR as exc:
            raise classify_ldap_error(exc, context=_ldap_context(tenant, operation)) from exc
        finally:
            connection.unbind_s()
