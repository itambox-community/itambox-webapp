"""The declarative capability registry.

A capability is one shippable contract: a named slice of the product with a
declared maturity, a declared way of being switched on, and a declared owner.
Domain applications register their own entries from ``AppConfig.ready``; this
module never learns what any of them are.

That blindness is the whole design. The registry stores dotted strings and
opaque callables the domain hands it, and resolves neither at registration
time, so ``itambox.capabilities`` imports no application, no model, and no
presentation code. The architecture gate classifies it as ``framework``, where
naming a domain application is a hard error -- see the private
``itambox-community/design-docs`` repository for the architecture policy.

Three rules make the declarations trustworthy rather than decorative:

* **Stable means always on.** A Stable capability carries no probe and cannot
  report inactive. If a slice can be switched off, it is not Stable. "Always
  on" describes *availability*, not self-activation: the slice's surfaces are
  always present, and any effect it gates on deliberate operator configuration
  (a rule, an endpoint, a target) still requires that configuration first. A
  provider of an external effect is therefore Stable and still dormant until
  somebody creates and enables the object that causes the effect.
* **Non-Stable means probed.** Beta and Experimental entries must say how a
  deployment turns them on, and must declare at least one limitation.
* **Security-critical means undeactivatable.** An entry that guards a boundary
  may not have an off switch, so it may not carry a probe at all.

Activation is evaluated live and never cached: a probe reports what a
deployment looks like *now*. Probes are expected to observe -- read a setting,
count rows -- never to mutate. A probe that raises fails closed, and only its
exception *type* is ever published, so a probe that touches a credential cannot
leak one into a diagnostics row.
"""

import dataclasses
import importlib.util
import re

from django.apps import apps

# Public operator-facing documentation for the capability contract. These are
# relative MkDocs paths beneath the compiled docs site; the banner template
# renders them against {% static 'docs/' %}. They must remain end-user,
# operator-, integration-, or security-facing pages -- never contributor or
# maintainer documentation.
CAPABILITY_REGISTRY_DOC_URL = "operations/capability-maturity.md"
RESOURCE_GRANT_SECURITY_DOC_URL = "operations/resource-grant-expiry.md"

STABLE = "stable"
BETA = "beta"
EXPERIMENTAL = "experimental"
MATURITIES = (STABLE, BETA, EXPERIMENTAL)

ALWAYS_ON = "always-on"
ENABLED = "enabled"
OPT_IN = "opt-in"
ACTIVATION_MODES = (ALWAYS_ON, ENABLED, OPT_IN)

SOURCE_ALWAYS = "always"
SOURCE_CONFIGURED = "configured"
SOURCE_OBJECT_ENABLED = "object-enabled"
SOURCE_OPERATOR_FLAG = "operator-flag"
ACTIVATION_SOURCES = (SOURCE_ALWAYS, SOURCE_CONFIGURED, SOURCE_OBJECT_ENABLED, SOURCE_OPERATOR_FLAG)

CONTRACT_VERSION = 1

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
AREA_RE = re.compile(r"^area:[a-z][a-z0-9-]*$")
REFERENCE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)+$")

#: The fields a diagnostics row publishes. Deliberately closed: the class, the
#: mode, the current state, the source, and whether a value is present -- never
#: a value, a probe, or a probe's message.
DIAGNOSTIC_FIELDS = (
    "key",
    "title",
    "owning_area",
    "maturity",
    "security_critical",
    "activation",
    "activation_source",
    "active",
    "value_present",
    "probe_error",
    "docs_url",
    "contract_version",
)


class CapabilityError(Exception):
    """Base class for registry faults."""


class DuplicateCapability(CapabilityError):
    """A key or an owned reference was claimed twice."""


class UnknownCapability(CapabilityError):
    """A key that was never registered was asked about."""


class ProbeError(CapabilityError):
    """A probe returned something other than an :class:`ActivationState`.

    This is a programming error in the declaring application, not a deployment
    condition, so it is raised rather than absorbed into a fail-closed state.
    """


@dataclasses.dataclass(frozen=True)
class ActivationState:
    """What a probe is allowed to say.

    Three flags and no free-text channel. ``value_present`` reports that an
    operator supplied *something* -- never what. ``probe_error`` holds an
    exception class name and is set by the registry, never by a probe.
    """

    active: bool
    value_present: bool
    probe_error: str = ""

    def __post_init__(self):
        for name in ("active", "value_present"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ActivationState.{name} must be a bool")
        if not isinstance(self.probe_error, str):
            raise TypeError("ActivationState.probe_error must be a str")


@dataclasses.dataclass(frozen=True)
class UnresolvedReference:
    """A dotted reference that did not resolve when someone finally looked."""

    key: str
    reference: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Capability:
    """One declared contract. Validated on construction, immutable after."""

    key: str
    title: str
    owning_area: str
    maturity: str
    security_critical: bool
    activation: str
    activation_probe: object
    activation_source: str
    owns: tuple
    docs_url: str
    limitations: tuple
    contract_version: int

    def __post_init__(self):
        self._validate_identity()
        self._validate_vocabularies()
        self._validate_activation()
        self._validate_references()

    def declaration(self):
        """This entry's comparable shape, with the probe reduced to its presence.

        Every ``AppConfig.ready`` builds fresh probe closures, so two runs of the
        same declaration are equal in every way that was *declared* and unequal
        in object identity. Comparing shapes lets :meth:`CapabilityRegistry.
        register_all` tell a harmless re-run from a genuine key collision.
        """
        return (
            self.key,
            self.title,
            self.owning_area,
            self.maturity,
            self.security_critical,
            self.activation,
            self.activation_source,
            self.owns,
            self.docs_url,
            self.limitations,
            self.contract_version,
            self.activation_probe is not None,
        )

    def _validate_identity(self):
        if not isinstance(self.key, str) or not KEY_RE.match(self.key):
            raise ValueError(f"capability key must be a lowercase dotted identifier, got {self.key!r}")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError(f"{self.key}: title is required")
        if not isinstance(self.owning_area, str) or not AREA_RE.match(self.owning_area):
            raise ValueError(f"{self.key}: owning_area must be an 'area:*' label, got {self.owning_area!r}")
        if not isinstance(self.docs_url, str) or not self.docs_url.strip():
            raise ValueError(f"{self.key}: docs_url is required")
        if not isinstance(self.contract_version, int) or isinstance(self.contract_version, bool):
            raise ValueError(f"{self.key}: contract_version must be an integer")
        if self.contract_version < 1:
            raise ValueError(f"{self.key}: contract_version must be a positive integer")

    def _validate_vocabularies(self):
        if self.maturity not in MATURITIES:
            raise ValueError(f"{self.key}: unknown maturity {self.maturity!r}; expected one of {MATURITIES}")
        if self.activation not in ACTIVATION_MODES:
            raise ValueError(f"{self.key}: unknown activation mode {self.activation!r}")
        if self.activation_source not in ACTIVATION_SOURCES:
            raise ValueError(f"{self.key}: unknown activation_source {self.activation_source!r}")
        if not isinstance(self.security_critical, bool):
            raise TypeError(f"{self.key}: security_critical must be a bool")

    def _validate_activation(self):
        if self.security_critical and self.activation != ALWAYS_ON:
            raise ValueError(f"{self.key}: a security_critical capability may not be deactivatable")
        if self.maturity == STABLE:
            if self.activation != ALWAYS_ON or self.activation_source != SOURCE_ALWAYS:
                raise ValueError(f"{self.key}: a stable capability is always-on from the 'always' source")
            if self.activation_probe is not None:
                raise ValueError(f"{self.key}: a stable capability carries no activation probe")
            return
        if self.activation == ALWAYS_ON or self.activation_source == SOURCE_ALWAYS:
            raise ValueError(f"{self.key}: a non-stable capability may not be always-on")
        if self.activation_probe is None:
            raise ValueError(f"{self.key}: a non-stable capability requires an activation probe")
        if not callable(self.activation_probe):
            raise TypeError(f"{self.key}: activation probe must be callable")
        if not self.limitations:
            raise ValueError(f"{self.key}: a non-stable capability declares its limitations")

    def _validate_references(self):
        if not isinstance(self.owns, tuple) or not self.owns:
            raise ValueError(f"{self.key}: owns must be a non-empty tuple of dotted references")
        for reference in self.owns:
            if not isinstance(reference, str):
                raise TypeError(f"{self.key}: owns entries must be dotted strings, got {reference!r}")
            if not REFERENCE_RE.match(reference):
                raise ValueError(f"{self.key}: owns entry {reference!r} is not a dotted reference")
        if not isinstance(self.limitations, tuple):
            raise TypeError(f"{self.key}: limitations must be a tuple of strings")
        for limitation in self.limitations:
            if not isinstance(limitation, str) or not limitation.strip():
                raise TypeError(f"{self.key}: limitations must be non-empty strings")


def resolve_reference(reference):
    """Resolve a dotted reference late, without importing an application.

    ``app_label.ModelName`` goes through Django's app registry; anything else is
    checked with :func:`importlib.util.find_spec`, which locates a module
    without executing it. Nothing here runs domain code, so a mistyped
    reference is a report, not an import-time crash.
    """
    head, _, tail = reference.rpartition(".")
    if head and "." not in head and tail[:1].isupper():
        return apps.get_model(head, tail)
    if importlib.util.find_spec(reference) is None:
        raise LookupError(f"no module named {reference!r}")
    return reference


class CapabilityRegistry:
    """An ordered, duplicate-free index of declared capabilities."""

    def __init__(self):
        self._by_key = {}
        self._owners = {}

    def register(self, capability):
        """Add one entry. Refuses a repeated key or a twice-claimed reference.

        Refusal is atomic: a rejected registration leaves the registry exactly
        as it was, so a half-registered capability can never be observed.
        """
        if not isinstance(capability, Capability):
            raise TypeError(f"only Capability entries can be registered, got {type(capability).__name__}")
        if capability.key in self._by_key:
            raise DuplicateCapability(f"capability {capability.key!r} is already registered")
        for reference in capability.owns:
            owner = self._owners.get(reference)
            if owner is not None:
                raise DuplicateCapability(f"{reference!r} is already owned by {owner!r}")
        self._by_key[capability.key] = capability
        for reference in capability.owns:
            self._owners[reference] = capability.key
        return capability

    def register_all(self, capabilities):
        """Declare several entries at once, resumably.

        ``AppConfig.ready`` runs again whenever a test swaps ``INSTALLED_APPS``,
        and a multipart declaration that failed partway through has to be
        finishable on the next run. So each entry is considered on its own: one
        already present under the same :meth:`Capability.declaration` is left
        alone, and one present under a *different* declaration is a real
        collision and still raises. A guard on the declaration's first key would
        instead freeze the registry in whatever half-registered state the
        failure produced.
        """
        for capability in capabilities:
            existing = self._by_key.get(getattr(capability, "key", None))
            if existing is None:
                self.register(capability)
            elif existing.declaration() != capability.declaration():
                raise DuplicateCapability(
                    f"capability {capability.key!r} is already registered under a different declaration"
                )
        return self

    def get(self, key):
        try:
            return self._by_key[key]
        except KeyError:
            raise UnknownCapability(f"no capability registered under {key!r}") from None

    def all(self):
        return tuple(self._by_key[key] for key in sorted(self._by_key))

    def keys(self):
        return tuple(sorted(self._by_key))

    def __len__(self):
        return len(self._by_key)

    def __contains__(self, key):
        return key in self._by_key

    def __iter__(self):
        return iter(self.all())

    def owner_of(self, reference):
        """The capability owning ``reference``, or ``None`` when unowned."""
        key = self._owners.get(reference)
        return None if key is None else self._by_key[key]

    def owner_of_module(self, module_name):
        """The most-specific capability owning a Python module subtree.

        Module ownership is declared as a dotted module reference such as
        ``users.api.scim``.  A view implemented in
        ``users.api.scim.provider_views`` belongs to that declaration too. Model
        references end in a class-style (uppercase) component and are excluded,
        so this fallback cannot accidentally turn ``users.Token`` into a module
        prefix claim.
        """
        if not isinstance(module_name, str) or not module_name:
            return None
        matches = [
            reference
            for reference in self._owners
            if reference.rsplit(".", 1)[-1][:1].islower()
            and (module_name == reference or module_name.startswith(f"{reference}."))
        ]
        if not matches:
            return None
        return self.owner_of(max(matches, key=len))

    def unresolved_references(self):
        """Every owned reference that does not resolve, with a short reason.

        Reported rather than raised: a stale reference is a documentation
        defect to fix in review, not a reason to refuse to boot.
        """
        unresolved = []
        for capability in self.all():
            for reference in capability.owns:
                try:
                    resolve_reference(reference)
                # broad except: boundary-isolation: late resolvers can raise app-specific errors
                except Exception as exc:
                    unresolved.append(
                        UnresolvedReference(
                            key=capability.key,
                            reference=reference,
                            reason=type(exc).__name__,
                        )
                    )
        return tuple(unresolved)

    def state(self, key):
        """Evaluate ``key`` now. Never cached, never mutating, fails closed."""
        capability = self.get(key)
        if capability.activation_probe is None:
            return ActivationState(active=True, value_present=True)
        try:
            observed = capability.activation_probe()
        # broad except: boundary-isolation: app-supplied probes have open exception sets
        except Exception as exc:
            return ActivationState(active=False, value_present=False, probe_error=type(exc).__name__)
        if not isinstance(observed, ActivationState):
            raise ProbeError(f"{key}: activation probe returned {type(observed).__name__}, expected ActivationState")
        return observed

    def is_active(self, key):
        return self.state(key).active

    def diagnostics(self):
        """One closed-shape row per capability, sorted by key.

        Every value is a string, a bool, or an int, and no value originates
        from a probe beyond the two flags it is allowed to set.
        """
        rows = []
        for capability in self.all():
            state = self.state(capability.key)
            rows.append(
                {
                    "key": capability.key,
                    "title": capability.title,
                    "owning_area": capability.owning_area,
                    "maturity": capability.maturity,
                    "security_critical": capability.security_critical,
                    "activation": capability.activation,
                    "activation_source": capability.activation_source,
                    "active": state.active,
                    "value_present": state.value_present,
                    "probe_error": state.probe_error,
                    "docs_url": capability.docs_url,
                    "contract_version": capability.contract_version,
                }
            )
        return tuple(rows)


#: The process-wide registry. Domain ``AppConfig.ready`` hooks register into it.
registry = CapabilityRegistry()
