from itambox.registry import registry as platform_registry

from .providers import BaseSignatureProvider, LocalSignatureProvider


class SignatureProviderRegistry:
    """Built-in providers plus the ones plugins declare via ``PluginConfig.signature_providers``."""

    def __init__(self):
        self._registry = {}
        self._plugin_entries_loaded = 0

    @staticmethod
    def _validate(provider_class):
        if not (isinstance(provider_class, type) and issubclass(provider_class, BaseSignatureProvider)):
            raise TypeError(f"signature provider {provider_class!r} must subclass BaseSignatureProvider")
        if not isinstance(provider_class.name, str) or not provider_class.name:
            raise ValueError(f"signature provider {provider_class.__name__} must declare a non-empty name")

    def register(self, provider_class):
        self._validate(provider_class)
        existing = self._registry.get(provider_class.name)
        if existing is not None and type(existing) is not provider_class:
            raise ValueError(f"signature provider name {provider_class.name!r} is already registered")
        self._registry[provider_class.name] = provider_class()

    def unregister(self, name):
        """Remove a provider. Use only in tests."""
        self._registry.pop(name, None)

    def _load_plugin_providers(self):
        declared = platform_registry.get_plugin_signature_providers()
        for _plugin_name, provider_class in declared[self._plugin_entries_loaded :]:
            self.register(provider_class)
        self._plugin_entries_loaded = len(declared)

    def get(self, name):
        self._load_plugin_providers()
        return self._registry.get(name)

    def choices(self):
        self._load_plugin_providers()
        return [(k, v.verbose_name) for k, v in self._registry.items()]

    def configurable_choices(self):
        """Providers that need a SignatureProviderConfig."""
        return [(k, v) for k, v in self.choices() if self._registry[k].requires_config]


signature_providers = SignatureProviderRegistry()
signature_providers.register(LocalSignatureProvider)
