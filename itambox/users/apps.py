from django.apps import AppConfig

from itambox.capabilities import (
    ALWAYS_ON,
    CAPABILITY_REGISTRY_DOC_URL,
    CONTRACT_VERSION,
    SOURCE_ALWAYS,
    STABLE,
    Capability,
    registry,
)


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "users"

    def ready(self):
        # inline imports: app-registry: users.search and users.signals import user models and register receivers
        import users.search  # noqa
        import users.signals  # noqa

        # inline imports: app-registry: register the user-owned workspace resolver after models load.
        from core.tenant_scope import register_tenant_scope_provider
        from users.services import resolve_default_workspace

        register_tenant_scope_provider(resolve_default_workspace=resolve_default_workspace)
        self._register_capabilities()

    def _register_capabilities(self):
        registry.register_all(self._capabilities())

    def _capabilities(self):
        # Stable and always-on: the declared contract covers both SCIM mounts.
        # Nothing provisions automatically — provisioning still requires an
        # operator-minted, tenant-scoped token with writes enabled and an identity
        # provider that deliberately drives the mounts.
        return (
            Capability(
                key="users.scim_provisioning",
                title="SCIM Provisioning",
                owning_area="area:auth-rbac",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("users.api.scim",),
                docs_url=CAPABILITY_REGISTRY_DOC_URL,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
        )
