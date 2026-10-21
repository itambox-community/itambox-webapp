"""The declared 1.0 external-contract policy and the source facts it binds to.

The 1.0 compatibility contract has human-readable policy and inventory
documents in the private ``itambox-community/design-docs`` repository. The public
repository keeps the reviewed machine-readable projection in
``scripts/contract_policy_manifest.json``. A promise about a surface nobody
enumerated is unfalsifiable, and an enumeration that drifts from source is worse
than none -- so this module derives the surfaces from source and the gate in
``check_contract_policy.py`` compares them against that manifest.

Three design choices make the result trustworthy rather than decorative:

* **Derivation, not transcription.** Enum values, ``ITAMBOX_*`` names, SCIM
  routes, custom permission codenames, and capability declarations are read out
  of the source tree with :mod:`ast` and a small number of anchored regular
  expressions. Nothing here imports Django, so the gate runs on a bare
  interpreter with no database and no settings module.
* **Closed enums carry their frozen values here.** A Stable enum the policy
  declares ``CLOSED`` records its values in this reviewed module *as well as* in
  the published document. Changing such an enum then takes three coordinated
  edits -- source, policy, document -- which is exactly the review a frozen
  1.x value set is supposed to attract. An ``OPEN`` enum records none: values
  may be added in a minor release, but the addition must still be published.
* **The exclusions are explicit.** Every ``ITAMBOX_*`` name the application
  reads -- from the environment or from a Django settings attribute, anywhere in
  the first-party tree -- is either published as contract-bearing or listed in
  :data:`EXCLUDED_SETTINGS` with a reason. A name that is in neither is a
  finding, so a new setting cannot arrive unclassified.

This module deliberately does **not** re-check what the capability registry
already proves. ``itambox/tests/test_capability_slices.py`` compares the live
registry against the capability rows in the machine-readable manifest; that
runs under Django and owns grade/activation/limitation drift. What is added
here is the one thing the registry does not publish: the *contract class* each
capability is sold under, and the exclusions attached to it.

The gate never writes the manifest. Updating it is a reviewed edit alongside the human-readable private policy documents.
"""

import argparse
import ast
import collections
import json
import re
from pathlib import Path

CONTRACT_POLICY_VERSION = 1

# These are logical sections in the code-owned manifest, not paths to prose
# documents. The reviewed human-readable versions live in the private
# itambox-community/design-docs repository.
MANIFEST_DOC = "scripts/contract_policy_manifest.json"
POLICY_DOC = "policy"
INVENTORY_DOC = "inventory"
RESOURCE_GRANT_THREAT_DOC = "resource-grant"
CHANGELOG_DOC = "CHANGELOG.md"

#: The four classes every external surface is sold under. Closed vocabulary.
CLASS_STABLE = "stable"
CLASS_BETA_ENABLED = "beta-enabled"
CLASS_BETA_OPT_IN = "beta-opt-in"
CLASS_EXPERIMENTAL = "experimental"
CONTRACT_CLASSES = (CLASS_STABLE, CLASS_BETA_ENABLED, CLASS_BETA_OPT_IN, CLASS_EXPERIMENTAL)

#: How a registry declaration (maturity, activation mode) becomes a class.
#: Exactly the four shapes the published class table sells, and no others:
#: ``(stable, always-on)`` is the only Stable shape the registry can construct,
#: and the policy promises there is no default-on Experimental surface. The
#: registry *can* construct ``(experimental, enabled)`` -- it bars only always-on
#: and probe-less non-Stable slices -- so leaving that shape unmapped is what
#: makes a default-on Experimental capability fail closed as ``C-CAP2`` instead
#: of publishing under a class whose promise it contradicts.
CLASS_BY_REGISTRY_CONTRACT = {
    ("stable", "always-on"): CLASS_STABLE,
    ("beta", "enabled"): CLASS_BETA_ENABLED,
    ("beta", "opt-in"): CLASS_BETA_OPT_IN,
    ("experimental", "opt-in"): CLASS_EXPERIMENTAL,
}

OPEN = "open"
CLOSED = "closed"

#: Choice-set shapes the extractor understands.
KIND_PAIRS = "pairs"  # ``X = [(VALUE, label), ...]`` or ``((VALUE, label, colour), ...)``
KIND_TEXT_CHOICES = "text-choices"  # ``MEMBER = "value", _("Label")`` in a TextChoices body

Finding = collections.namedtuple("Finding", "rule detail")
EnumSource = collections.namedtuple(
    "EnumSource", "name path class_name attribute kind openness contract_class frozen_values"
)
Statement = collections.namedtuple("Statement", "identifier document text")
DeclaredCapability = collections.namedtuple(
    "DeclaredCapability", "key maturity activation security_critical limitations"
)
CapabilityRow = collections.namedtuple("CapabilityRow", "contract_class activation scope exclusions")
CustomPermission = collections.namedtuple("CustomPermission", "app_label model codename")
WebhookEnvelope = collections.namedtuple("WebhookEnvelope", "fields signature_header")
ScimRoute = collections.namedtuple("ScimRoute", "mount path name")


def _enum(name, path, class_name, attribute, kind, openness, contract_class, frozen_values=()):
    return EnumSource(name, path, class_name, attribute, kind, openness, contract_class, tuple(frozen_values))


#: The persisted choice sets this release publishes as an external contract.
#: Bounded on purpose: this is not an inventory of every ``choices=`` in the
#: repository, it is the set the 1.0 contract makes a promise about.
ENUM_SOURCES = (
    _enum(
        "extras.ScheduledReport.FREQUENCY_CHOICES",
        "itambox/extras/models.py",
        "ScheduledReport",
        "FREQUENCY_CHOICES",
        KIND_PAIRS,
        CLOSED,
        CLASS_STABLE,
        ("once", "hourly", "daily", "weekly", "biweekly", "monthly", "quarterly", "yearly", "cron"),
    ),
    _enum(
        "extras.ScheduledReport.FORMAT_CHOICES",
        "itambox/extras/models.py",
        "ScheduledReport",
        "FORMAT_CHOICES",
        KIND_PAIRS,
        OPEN,
        CLASS_STABLE,
    ),
    _enum(
        "extras.ReportTemplate.REPORT_TYPE_CHOICES",
        "itambox/extras/models.py",
        "ReportTemplate",
        "REPORT_TYPE_CHOICES",
        KIND_PAIRS,
        OPEN,
        CLASS_BETA_OPT_IN,
    ),
    _enum(
        "extras.ReportTemplateForm.COLUMN_CHOICES",
        "itambox/extras/forms.py",
        "ReportTemplateForm",
        "COLUMN_CHOICES",
        KIND_PAIRS,
        OPEN,
        CLASS_BETA_OPT_IN,
    ),
    _enum(
        "extras.AlertRule.ALERT_TYPE_CHOICES",
        "itambox/extras/models.py",
        "AlertRule",
        "ALERT_TYPE_CHOICES",
        KIND_PAIRS,
        OPEN,
        CLASS_STABLE,
    ),
    _enum(
        "extras.AlertRule.SEVERITY_CHOICES",
        "itambox/extras/models.py",
        "AlertRule",
        "SEVERITY_CHOICES",
        KIND_PAIRS,
        CLOSED,
        CLASS_STABLE,
        ("info", "warning", "critical"),
    ),
    _enum(
        "subscriptions.SubscriptionStatusChoices",
        "itambox/subscriptions/models.py",
        "SubscriptionStatusChoices",
        "",
        KIND_TEXT_CHOICES,
        CLOSED,
        CLASS_STABLE,
        ("active", "suspended", "cancelled", "expired"),
    ),
    _enum(
        "subscriptions.BillingCycleChoices",
        "itambox/subscriptions/models.py",
        "BillingCycleChoices",
        "",
        KIND_TEXT_CHOICES,
        OPEN,
        CLASS_STABLE,
    ),
    _enum(
        "procurement.PurchaseOrder.STATUS_CHOICES",
        "itambox/procurement/models.py",
        "PurchaseOrder",
        "STATUS_CHOICES",
        KIND_PAIRS,
        CLOSED,
        CLASS_STABLE,
        ("draft", "approved", "ordered", "partial", "received", "cancelled"),
    ),
    _enum(
        "core.ObjectChangeActionChoices",
        "itambox/core/choices.py",
        "ObjectChangeActionChoices",
        "CHOICES",
        KIND_PAIRS,
        OPEN,
        CLASS_STABLE,
    ),
    _enum(
        "core.EventActionChoices",
        "itambox/core/choices.py",
        "EventActionChoices",
        "CHOICES",
        KIND_PAIRS,
        OPEN,
        CLASS_STABLE,
    ),
)

# Deliberately absent from ENUM_SOURCES, so the boundary is a decision rather
# than an oversight: ``core.JobStatusChoices`` and
# ``organization.TenantResourceGrant.ACCESS_CHOICES`` are persisted but reach no
# REST, GraphQL, SCIM, or webhook surface, so freezing them would be freezing an
# internal. Cross-tenant resource grants are still governed by this policy --
# through the review duties in the compatibility document, not through an enum.
UNINVENTORIED_INTERNAL_ENUMS = (
    "core.JobStatusChoices",
    "organization.TenantResourceGrant.ACCESS_CHOICES",
)

#: ``ITAMBOX_*`` reads are derived from the whole first-party application tree,
#: not from the settings package alone: the highest-value knobs are read where
#: they are used (``core/crypto.py``, ``organization/services/identity_provisioning.py``), and a
#: promise that a new name cannot arrive unclassified has to cover those.
SETTINGS_SCAN_ROOT = "itambox"
SETTINGS_PACKAGE_PREFIX = "itambox/core/settings/"
#: Directories that are not first-party runtime source: generated or vendored
#: trees, historical migrations, and test modules, whose reads are fixtures
#: rather than product configuration.
SETTINGS_SCAN_EXCLUDED_DIRS = frozenset(
    {"migrations", "tests", "docs", "static", "staticfiles", "node_modules", "__pycache__"}
)
SETTING_NAME_RE = re.compile(r"^ITAMBOX_[A-Z0-9_]+$")

#: ``ITAMBOX_*`` names the application reads that are deliberately NOT part of
#: the external contract, each with the reason it is out of scope. These are
#: deployment-local knobs: renaming one breaks an operator's environment file,
#: which the changelog covers, but it changes no API, no persisted value, and no
#: integration wire.
EXCLUDED_SETTINGS = {
    "ITAMBOX_ALLOWED_HOSTS": "deployment-local host allowlist; no product surface depends on it",
    "ITAMBOX_CACHE_TIMEOUT": "deployment-local cache tuning; changes no observable contract",
    "ITAMBOX_CORS_ALLOWED_ORIGINS": "deployment-local browser origin policy",
    "ITAMBOX_CORS_ALLOW_ALL_ORIGINS": "deployment-local browser origin policy",
    "ITAMBOX_CSRF_TRUSTED_ORIGINS": "deployment-local browser origin policy",
    "ITAMBOX_DB_CONN_MAX_AGE": "deployment-local database connection tuning",
    "ITAMBOX_DB_ENGINE": "deployment-local database connection parameter",
    "ITAMBOX_DB_HOST": "deployment-local database connection parameter",
    "ITAMBOX_DB_NAME": "deployment-local database connection parameter",
    "ITAMBOX_DB_PASSWORD": "deployment-local database credential; never published",
    "ITAMBOX_DB_PORT": "deployment-local database connection parameter",
    "ITAMBOX_DB_SSLMODE": "deployment-local database transport parameter",
    "ITAMBOX_DB_USER": "deployment-local database connection parameter",
    "ITAMBOX_DEBUG": "deployment-local development switch; production reads ITAMBOX_ENV",
    "ITAMBOX_DEFAULT_FROM_EMAIL": "deployment-local mail identity",
    "ITAMBOX_EMAIL_BACKEND": "deployment-local mail transport selection",
    "ITAMBOX_EMAIL_HOST": "deployment-local mail transport parameter",
    "ITAMBOX_EMAIL_HOST_PASSWORD": "deployment-local mail credential; never published",
    "ITAMBOX_EMAIL_HOST_USER": "deployment-local mail transport parameter",
    "ITAMBOX_EMAIL_PORT": "deployment-local mail transport parameter",
    "ITAMBOX_EMAIL_TIMEOUT": "deployment-local mail transport tuning",
    "ITAMBOX_EMAIL_USE_SSL": "deployment-local mail transport parameter",
    "ITAMBOX_EMAIL_USE_TLS": "deployment-local mail transport parameter",
    "ITAMBOX_HSTS_INCLUDE_SUBDOMAINS": "deployment-local transport-security header tuning",
    "ITAMBOX_HSTS_PRELOAD": "deployment-local transport-security header tuning",
    "ITAMBOX_HSTS_SECONDS": "deployment-local transport-security header tuning",
    "ITAMBOX_LOG_LEVEL": "deployment-local logging verbosity",
    "ITAMBOX_MEDIA_ROOT": "deployment-local filesystem path",
    "ITAMBOX_RECOVERY_API_TOKEN": (
        "operator-supplied credential for the recovery-drill evidence command; never published"
    ),
    "ITAMBOX_RECOVERY_PROBE_KEY": (
        "operator-supplied probe secret for the recovery-drill evidence command; never published"
    ),
    "ITAMBOX_REDIS_URL": "deployment-local broker connection string; never published",
    "ITAMBOX_SECRET_KEY": "deployment-local signing credential; never published",
    "ITAMBOX_SECURE_SSL_REDIRECT": "deployment-local transport policy",
    "ITAMBOX_SERVER_EMAIL": "deployment-local mail identity for error reports",
    "ITAMBOX_STATIC_ROOT": "deployment-local filesystem path",
}

#: The exact limitation text each published ``Exclusions`` summary was written
#: against. The inventory summarises rather than transcribes -- a verbatim table
#: of these sentences would not get read -- and a summary that nothing binds is
#: a claim that quietly stops being true. Recording the source text here in the
#: reviewed module is the same device the closed enums use: rewording, adding,
#: or removing a limitation fails ``C-CAP5`` until somebody re-reads the summary
#: against it. A capability with no declared limitation records ``()``, so a
#: Stable slice that gains one cannot keep publishing "none".
CAPABILITY_LIMITATIONS = {
    "alerting.inbox": (),
    "alerting.rules": (),
    "automation.webhooks": (),
    "organization.role_grants": (),
    "organization.resource_grants": (),
    "platform.plugins": (
        "Only the bounded extension points documented for plugin API 1.0 are supported; "
        "Experimental interfaces may change in any release.",
        "Plugin code runs in-process with full database access and is not sandboxed.",
    ),
    "procurement.core": (),
    "procurement.requisition_seam": (),
    "reporting.curated": (),
    "reporting.designer": (),
    "reporting.scheduled": (),
    "subscriptions.tracking": (),
    "users.scim_provisioning": (),
}

CAPABILITY_APP_CONFIG_GLOB = "itambox/*/apps.py"
CAPABILITY_REGISTRY_MODULE = "itambox/itambox/capabilities.py"
MODEL_GLOBS = ("itambox/*/models.py", "itambox/*/models/*.py")
WEBHOOK_TASK_MODULE = "itambox/extras/tasks/webhooks.py"
WEBHOOK_SIGNATURE_HEADER = "X-Hub-Signature-256"
SCIM_URL_MODULES = ("itambox/users/api/scim/urls.py", "itambox/users/api/scim/provider_urls.py")

#: UI URL coverage is deliberately bounded to two facts: the set of application
#: namespaces, and a named handful of root entry routes. The repository declares
#: several hundred individual UI route names; freezing all of them would turn
#: every ordinary view rename into a breaking change, which is not the promise
#: this policy makes. The route-naming convention is published as guidance and
#: is not gate-enforced.
UI_URLCONF_GLOB = "itambox/*/urls.py"
ROOT_URLCONF = "itambox/core/urls.py"

ANCHOR_PREFIX = "<!-- contract-inventory:"
#: Any HTML comment that is not a contract anchor, including a multi-line one.
HTML_COMMENT_RE = re.compile(r"<!--(?!\s*contract-inventory:).*?-->", re.DOTALL)
ANCHOR_CAPABILITIES = "capabilities"
ANCHOR_PERMISSIONS = "permissions"
ANCHOR_SETTINGS = "settings"
ANCHOR_WEBHOOK = "webhook-envelope"
ANCHOR_SCIM = "scim-routes"
ANCHOR_UI_NAMESPACES = "ui-namespaces"
ANCHOR_ENTRY_ROUTES = "entry-routes"


def _statement(identifier, document, text):
    return Statement(identifier, document, text)


#: Promises the published documents must state in so many words. Each entry is
#: matched against the document with whitespace normalised, so re-wrapping a
#: paragraph is free but deleting a promise is not.
REQUIRED_STATEMENTS = (
    _statement("P-API-V1", POLICY_DOC, "`/api/` is the version 1 compatibility convention for the whole of 1.x"),
    _statement("P-API-NO-SEAM", POLICY_DOC, "no version segment, no negotiation header, and no runtime version seam"),
    _statement("P-API-V2", POLICY_DOC, "would have to be mounted separately as `/api/v2/`"),
    _statement("P-STABLE-ADDITIVE", POLICY_DOC, "additive-only for the whole of 1.x"),
    _statement("P-STABLE-FROZEN", POLICY_DOC, "names, types, closed enum values, primary-key URLs"),
    _statement("P-STABLE-NOTICE", POLICY_DOC, "two minor releases of removal notice"),
    _statement("P-STABLE-PATCH", POLICY_DOC, "never removed in a patch release"),
    _statement("P-STABLE-NO-WEAKENING", POLICY_DOC, "cannot be weakened within 1.x"),
    _statement("P-STABLE-ROLLBACK", POLICY_DOC, "S5 realistic rollback"),
    _statement(
        "P-BETA-ENABLED-LABELLING",
        POLICY_DOC,
        "a release obligation on Beta enabled work, not a claim that every surface already carries it",
    ),
    _statement("P-BETA-ENABLED-DATA", POLICY_DOC, "preserves the data an operator already recorded"),
    _statement("P-BETA-ENABLED-MINOR", POLICY_DOC, "only in a minor release, never in a patch"),
    _statement("P-BETA-ENABLED-NOTICE", POLICY_DOC, "one minor release of notice and an export"),
    _statement("P-BETA-ENABLED-INACTIVE", POLICY_DOC, "a real inactive state"),
    _statement("P-BETA-OPT-IN-INERT", POLICY_DOC, "inert on a fresh deployment"),
    _statement("P-BETA-OPT-IN-ACTIVATION", POLICY_DOC, "activation surface itself is held to the Stable standard"),
    _statement("P-BETA-OPT-IN-WIRE", POLICY_DOC, "must carry its own independent wire version"),
    _statement(
        "P-WIRE-V1",
        POLICY_DOC,
        "carries an explicit `schema_version`, `event_id`, `delivery_id`, `attempt`, `tenant`, "
        "`event`, `model`, `object_id`, `timestamp`, and `data` field",
    ),
    _statement("P-WIRE-NOT-CONTRACT-VERSION", POLICY_DOC, "`contract_version` versions the registry declaration"),
    _statement("P-EXPERIMENTAL-OPT-IN", POLICY_DOC, "reached only through an explicit configuration opt-in"),
    _statement("P-EXPERIMENTAL-CHANGE", POLICY_DOC, "may change in any release, including a patch"),
    _statement("P-EXPERIMENTAL-SUPPORT", POLICY_DOC, "security fixes only"),
    _statement("P-EXPERIMENTAL-PINNING", POLICY_DOC, "pin the exact revision"),
    _statement("P-SAFETY-FLOOR", POLICY_DOC, "N1-N11 and X1-X6 safety floors"),
    _statement("P-SECURITY-CRITICAL", POLICY_DOC, "security-critical marker"),
    _statement("P-TENANT-ISOLATION", POLICY_DOC, "tenant B cannot read, write, list, filter, export, or reference"),
    _statement("P-SERVICE-LAYER-PARITY", POLICY_DOC, "service and data layers enforce the same authorization"),
    _statement("P-SCHEDULED-NINE", POLICY_DOC, "nine `ScheduledReport` frequency values"),
    _statement("P-CONFIG-RENAME", POLICY_DOC, "compatibility read and a startup warning"),
    _statement("P-N9-OBSERVABILITY", POLICY_DOC, "N9 and S6"),
    _statement("P-N9-DEPLOYMENT", POLICY_DOC, "no in-product actor"),
    _statement("P-N9-SYNTHETIC", POLICY_DOC, "synthetic request identifier and a `None` user"),
    _statement("P-N9-NEVER-HUMAN", POLICY_DOC, "never presented as a human action"),
    _statement("P-IDEMPOTENCY", POLICY_DOC, "idempotency, retry, and durable-outcome"),
    _statement("P-X2-X4-OBLIGATION", POLICY_DOC, "X2-X4 are obligations on capability work, not claims about today"),
    _statement("P-N11-REHEARSAL", POLICY_DOC, "N11 reverse-or-export rehearsal"),
    _statement("P-DEFERRED-SUBSCRIPTION", POLICY_DOC, "`vendor_contract_auto_renews`"),
    _statement("P-DEFERRED-SUBSCRIPTION-2", POLICY_DOC, "removed no earlier than 2.0"),
    _statement("P-DEFERRED-REQUISITION", POLICY_DOC, "`REQUISITION_AUTO_APPROVAL_THRESHOLDS`"),
    _statement("P-DEFERRED-SCIM", POLICY_DOC, "integer-keyed SCIM detail routes remain supported for the whole of 1.x"),
    _statement("P-RESOURCE-GRANT", POLICY_DOC, "no baseline escape"),
    _statement("P-RESOURCE-GRANT-REVIEW", POLICY_DOC, "adversarial review"),
    _statement("P-RESOURCE-GRANT-THREAT", POLICY_DOC, "threat-model duty"),
    _statement(
        "P-RESOURCE-GRANT-DENY",
        RESOURCE_GRANT_THREAT_DOC,
        "Failure of any condition denies access. A grant authorizes a tenant, never a user.",
    ),
    _statement(
        "P-RESOURCE-GRANT-ACTORLESS",
        RESOURCE_GRANT_THREAT_DOC,
        "a bare `user=None` is denied",
    ),
    _statement(
        "P-RESOURCE-GRANT-CANONICAL",
        RESOURCE_GRANT_THREAT_DOC,
        "`organization.access.resolve_stock_access()` is the decision primitive",
    ),
    _statement(
        "P-RESOURCE-GRANT-CANONICAL-REEXPORT",
        RESOURCE_GRANT_THREAT_DOC,
        "`organization.services.resolve_stock_access` is its compatibility re-export",
    ),
    _statement(
        "P-RESOURCE-GRANT-PRECEDENCE",
        RESOURCE_GRANT_THREAT_DOC,
        "Direct tenant grants take deterministic precedence over ancestor-group grants.",
    ),
    _statement(
        "P-RESOURCE-GRANT-MATRIX",
        RESOURCE_GRANT_THREAT_DOC,
        "inventory/tests/test_tenant_resource_grant_security.py",
    ),
    _statement(
        "P-RESOURCE-GRANT-DIRECT-WRITES",
        RESOURCE_GRANT_THREAT_DOC,
        "inventory/tests/test_direct_assignment_writes.py",
    ),
    _statement(
        "P-RESOURCE-GRANT-SYSTEM-PROVENANCE-SUITE",
        RESOURCE_GRANT_THREAT_DOC,
        "inventory/tests/test_assignment_system_authorization_provenance.py",
    ),
    _statement(
        "P-RESOURCE-GRANT-ALLOWLIST-CLOSED",
        RESOURCE_GRANT_THREAT_DOC,
        "The approved resource allowlist is closed:",
    ),
    _statement("P-RESOURCE-GRANT-ACCESSORY-STOCK", RESOURCE_GRANT_THREAT_DOC, "`inventory.AccessoryStock`"),
    _statement("P-RESOURCE-GRANT-COMPONENT-STOCK", RESOURCE_GRANT_THREAT_DOC, "`inventory.ComponentStock`"),
    _statement("P-RESOURCE-GRANT-CONSUMABLE-STOCK", RESOURCE_GRANT_THREAT_DOC, "`inventory.ConsumableStock`"),
    _statement(
        "P-RESOURCE-GRANT-PERSISTED-DERIVATION",
        RESOURCE_GRANT_THREAT_DOC,
        "caller-held relation caches never define `source_tenant`, `target_tenant`, or the covering grant.",
    ),
    _statement(
        "P-RESOURCE-GRANT-INDEPENDENT-RBAC",
        RESOURCE_GRANT_THREAT_DOC,
        "must independently pass RBAC in the active tenant.",
    ),
    _statement(
        "P-RESOURCE-GRANT-TOPOLOGY-FAIL-CLOSED",
        RESOURCE_GRANT_THREAT_DOC,
        "live-only ancestry walk; no coverage through the broken chain",
    ),
    _statement(
        "P-RESOURCE-GRANT-UNSCOPED-MANAGER",
        RESOURCE_GRANT_THREAT_DOC,
        "`TenantResourceGrant.objects` is deliberately unscoped",
    ),
    _statement(
        "P-RESOURCE-GRANT-SYSTEM-PROVENANCE",
        RESOURCE_GRANT_THREAT_DOC,
        "Actorless assignments persist the exact authorized system operation and reason.",
    ),
    _statement(
        "P-RESOURCE-GRANT-IMMUTABLE-PROVENANCE",
        RESOURCE_GRANT_THREAT_DOC,
        "Those values are immutable historical evidence",
    ),
    _statement(
        "P-RESOURCE-GRANT-CONTAINER-VISIBILITY",
        RESOURCE_GRANT_THREAT_DOC,
        "`visible_to_containers()` or fail closed",
    ),
    _statement(
        "P-RESOURCE-GRANT-LIFECYCLE-ATTRIBUTION",
        RESOURCE_GRANT_THREAT_DOC,
        "Future grant expiry or revocation workflows must preserve durable attribution",
    ),
    _statement("P-SCOPE-EXCLUSIONS", POLICY_DOC, "What this policy deliberately does not cover"),
    _statement("P-URL-BOUNDARY", POLICY_DOC, "individual UI route names are not frozen"),
    _statement("P-INVENTORY-BOUNDED", INVENTORY_DOC, "bounded inventory"),
    _statement(
        "P-INVENTORY-ENUM-OPENNESS", INVENTORY_DOC, "every inventoried enum is marked explicitly open or closed"
    ),
    _statement("P-INVENTORY-SIGNATURE", INVENTORY_DOC, WEBHOOK_SIGNATURE_HEADER),
    _statement("P-INVENTORY-ROLE-FIELD", INVENTORY_DOC, "`organization.Role.permissions`"),
    _statement("P-INVENTORY-ROLE-CONCEPT", INVENTORY_DOC, "Tenant Role is the user-facing name"),
    _statement("P-INVENTORY-API-VERSION", INVENTORY_DOC, "unversioned `/api/` prefix is the version 1 convention"),
    _statement("P-INVENTORY-URL-BOUNDARY", INVENTORY_DOC, "namespaces are inventoried; individual route names are not"),
    _statement(
        "P-INVENTORY-EXCLUSIONS-PINNED",
        INVENTORY_DOC,
        "records the exact declared limitation text each cell was written against",
    ),
)

#: Wording that would publish an isolation or authorization escape. A sentence
#: is flagged only when it names one of these actors, one of these verbs, and
#: one of these boundaries, and carries no negation -- so the policy can state
#: the rule ("no Beta grade waives tenant isolation") without tripping it.
BYPASS_ACTORS = (
    "beta",
    "experimental",
    "superuser",
    "super user",
    "staff user",
    "hidden ui",
    "hidden view",
    "hidden page",
    "hidden interface",
)
BYPASS_VERBS = (
    "bypass",
    "bypasses",
    "skip",
    "skips",
    "waive",
    "waives",
    "ignore",
    "ignores",
    "exempt",
    "exempts",
    "override",
    "overrides",
    "circumvent",
    "circumvents",
    "relax",
    "relaxes",
)
BYPASS_BOUNDARIES = (
    "tenant isolation",
    "tenant boundary",
    "tenant scoping",
    "tenant scope",
    "authorization",
    "authorisation",
    "permission check",
)
#: The negative formulations the published documents are allowed to use when
#: they state the rule itself. An allowlist rather than "a negation occurs
#: somewhere in the sentence": a negation that lands *after* the verb negates
#: something else -- "... for none of the read paths", "... without exception",
#: "; not a bug, by design" -- and exempting the claim in front of it exempts
#: nearly every escape claim anyone would actually write. Each entry is a
#: template; ``{verb}`` is filled with the matched bypass verb, so the negation
#: has to govern *that* verb rather than merely share a sentence with it.
SANCTIONED_NEGATIONS = (
    r"\bno\s+(?:\w+[,;]?\s+){0,4}?{verb}\b",
    r"\bno\s+(?:\w+[,;]?\s+){0,4}?(?:is|are|was|were)\s+{verb}\b",
    r"\bneither\s+(?:\w+[,;]?\s+){0,4}?{verb}\b",
    r"\bnothing\s+(?:\w+\s+){0,3}?{verb}\b",
    r"\bnever\s+{verb}\b",
    r"\b(?:do|does|did)\s+not\s+{verb}\b",
    r"\b(?:can|could|may|might|must|shall|will|would)\s*not\s+{verb}\b",
    r"\bcan't\s+{verb}\b",
    r"\b(?:is|are|was|were)\s+(?:not|never)\s+{verb}\b",
)

#: Emphasis and code markers, removed before sentences are split: a bolded
#: sentence still ends where its full stop is, not where its ``**`` is.
MARKDOWN_MARKS_RE = re.compile(r"[*_`]+")
FENCE_RE = re.compile(r"^\s*```")
#: A line that starts its own unit rather than continuing the previous one.
BLOCK_START_RE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|\||#|>|!!!)")


# --------------------------------------------------------------------------
# Source reading
# --------------------------------------------------------------------------


def _parse(root, relative):
    """Parse one tracked module, or raise a readable error naming it."""
    path = Path(root) / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class_node(module, name):
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _string_constants(class_node):
    """Class-body ``NAME = "value"`` assignments, for resolving choice tuples."""
    constants = {}
    for node in class_node.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                constants[target.id] = node.value.value
    return constants


def _attribute_value(class_node, attribute):
    for node in class_node.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == attribute:
                return node.value
    return None


def _first_element(element, constants):
    """The stored value of one ``(value, label[, colour])`` choice entry."""
    if not isinstance(element, (ast.Tuple, ast.List)) or not element.elts:
        return None
    head = element.elts[0]
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        return head.value
    if isinstance(head, ast.Name):
        return constants.get(head.id)
    return None


def _pair_values(class_node, attribute):
    container = _attribute_value(class_node, attribute)
    if not isinstance(container, (ast.List, ast.Tuple)):
        return ()
    constants = _string_constants(class_node)
    values = [_first_element(element, constants) for element in container.elts]
    return tuple(value for value in values if value is not None)


def _text_choices_values(class_node):
    """Members of a ``TextChoices`` body, in declaration order.

    Both spellings Django accepts are members and both are read here:
    ``MEMBER = "value", _("Label")`` and the equally idiomatic bare
    ``MEMBER = "value"``, whose label Django derives from the member name.
    Reading only the labelled form would let a value be added to an enum this
    policy declares frozen without any rule noticing. Underscore-prefixed names
    are not members -- ``Choices`` skips them -- so they are skipped here too.
    """
    values = []
    for node in class_node.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id.startswith("_"):
            continue
        value = node.value
        if isinstance(value, ast.Tuple) and value.elts:
            value = value.elts[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.append(value.value)
    return tuple(values)


def derived_enums(root):
    """Every declared choice set, read from source. Keyed by declared name."""
    modules = {}
    derived = {}
    for source in ENUM_SOURCES:
        if source.path not in modules:
            modules[source.path] = _parse(root, source.path)
        class_node = _class_node(modules[source.path], source.class_name)
        if class_node is None:
            derived[source.name] = ()
            continue
        if source.kind == KIND_TEXT_CHOICES:
            derived[source.name] = _text_choices_values(class_node)
        else:
            derived[source.name] = _pair_values(class_node, source.attribute)
    return derived


def _dotted_name(node):
    """``os.environ`` / ``settings`` / ``django.conf.settings`` as written."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _tail(node):
    return _dotted_name(node).rsplit(".", 1)[-1]


def _constant_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _setting_name_from_call(node):
    if not isinstance(node, ast.Call):
        return ""
    function = node.func
    if isinstance(function, ast.Attribute) and function.attr == "get" and node.args:
        if _tail(function.value) == "environ":
            return _constant_string(node.args[0])
    if _tail(function) == "getenv" and node.args:
        return _constant_string(node.args[0])
    if _tail(function) == "getattr" and len(node.args) >= 2 and _tail(node.args[0]) == "settings":
        return _constant_string(node.args[1])
    return ""


def _setting_name_from_subscript(node):
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _tail(node.value) == "environ":
        return _constant_string(node.slice)
    return ""


def _setting_name_from_attribute(node):
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and _tail(node.value) == "settings":
        return node.attr
    return ""


def _setting_assignment_names(node, in_settings_package):
    if not in_settings_package or not isinstance(node, ast.Assign):
        return ()
    return tuple(target.id for target in node.targets if isinstance(target, ast.Name))


def setting_names_in(module, in_settings_package=False):
    """Every ``ITAMBOX_*`` name one parsed module reads, sorted.

    Four shapes count, and nothing else does:

    * ``os.environ.get("NAME", ...)``, ``os.getenv("NAME")``, ``os.environ["NAME"]``
      -- a read of the process environment;
    * ``getattr(settings, "NAME", ...)`` and ``settings.NAME`` -- a read of a
      Django settings attribute, which is how application code reaches a knob
      the settings package never has to mention;
    * a module-level ``NAME = ...`` assignment *inside the settings package* --
      how the package publishes an attribute for the reads above to find.

    A comment, a docstring, a warning message, and a string in an f-string are
    prose. Counting prose as a read is how a setting the code no longer has
    stays published, and how rewording a warning makes ``C-SET2`` fire against a
    real one.
    """
    found = set()
    for node in ast.walk(module):
        found.add(_setting_name_from_call(node))
        found.add(_setting_name_from_subscript(node))
        found.add(_setting_name_from_attribute(node))
        found.update(_setting_assignment_names(node, in_settings_package))
    return tuple(sorted(name for name in found if SETTING_NAME_RE.match(name)))


def scanned_python_files(root):
    """First-party runtime modules the settings derivation reads, root-relative."""
    root = Path(root)
    paths = []
    for path in sorted((root / SETTINGS_SCAN_ROOT).rglob("*.py")):
        relative = path.relative_to(root)
        if set(relative.parts) & SETTINGS_SCAN_EXCLUDED_DIRS:
            continue
        if relative.name.startswith("test_") or relative.name == "conftest.py":
            continue
        paths.append(relative)
    return tuple(paths)


def derived_settings(root):
    """Every ``ITAMBOX_*`` name the application reads, sorted."""
    root = Path(root)
    names = set()
    for relative in scanned_python_files(root):
        module = ast.parse((root / relative).read_text(encoding="utf-8"), filename=str(relative))
        in_package = relative.as_posix().startswith(SETTINGS_PACKAGE_PREFIX)
        names.update(setting_names_in(module, in_settings_package=in_package))
    return tuple(sorted(names))


def registry_vocabulary(root):
    """``STABLE`` -> ``"stable"`` and friends, read from the registry module.

    The constant names in an ``apps.py`` mean whatever ``itambox.capabilities``
    says they mean; hard-coding the mapping here would let the two drift apart
    silently.
    """
    module = _parse(root, CAPABILITY_REGISTRY_MODULE)
    vocabulary = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            vocabulary[target.id] = node.value.value
    return vocabulary


def _keyword_map(call):
    return {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}


def _resolved_word(node, vocabulary):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return vocabulary.get(node.id, node.id)
    return ""


def _joined_strings(container):
    """Each element of a ``limitations=(...)`` tuple, implicit concatenation folded."""
    if not isinstance(container, (ast.Tuple, ast.List)):
        return ()
    strings = []
    for element in container.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            strings.append(element.value)
    return tuple(strings)


def _capability_from_call(call, vocabulary):
    keywords = _keyword_map(call)
    key = keywords.get("key")
    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
        return None
    critical = keywords.get("security_critical")
    return DeclaredCapability(
        key=key.value,
        maturity=_resolved_word(keywords.get("maturity"), vocabulary),
        activation=_resolved_word(keywords.get("activation"), vocabulary),
        security_critical=bool(isinstance(critical, ast.Constant) and critical.value is True),
        limitations=_joined_strings(keywords.get("limitations")),
    )


def _capability_declarations(root):
    """Every ``Capability(...)`` an ``AppConfig`` declares, with its module."""
    root = Path(root)
    vocabulary = registry_vocabulary(root)
    declared = []
    for path in sorted(root.glob(CAPABILITY_APP_CONFIG_GLOB)):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Capability":
                capability = _capability_from_call(node, vocabulary)
                if capability is not None:
                    declared.append((relative, capability))
    return tuple(sorted(declared, key=lambda entry: entry[1].key))


def derived_capabilities(root):
    """Every registered capability, sorted by key."""
    return tuple(capability for _, capability in _capability_declarations(root))


def _meta_permissions(model_node):
    for node in model_node.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Meta":
            continue
        container = _attribute_value(node, "permissions")
        if isinstance(container, (ast.List, ast.Tuple)):
            return tuple(value for value in (_first_element(item, {}) for item in container.elts) if value)
    return ()


def derived_custom_permissions(root):
    """Every ``Meta.permissions`` codename a first-party model declares."""
    found = []
    seen = set()
    for pattern in MODEL_GLOBS:
        for path in sorted(Path(root).glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            app_label = _app_label_for(root, path)
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in module.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for codename in _meta_permissions(node):
                    found.append(CustomPermission(app_label, node.name, codename))
    return tuple(sorted(found))


def _app_label_for(root, path):
    return path.relative_to(Path(root) / "itambox").parts[0]


def _signature_header(module):
    """The header name the delivery task writes its HMAC signature into.

    Read from the header assignment rather than from the file text: a rename
    that leaves the old name behind in a docstring or a comment must not read
    as the header still being sent. Ambiguity fails closed to ``""``, which is
    a ``C-HOOK2`` finding rather than a silent pass.
    """
    assigned = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key = target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                assigned.add(key.value)
    candidates = sorted(name for name in assigned if "signature" in name.lower())
    return candidates[0] if len(candidates) == 1 else ""


def derived_webhook_envelope(root):
    """The outbound event envelope the webhook task actually sends."""
    module = _parse(root, WEBHOOK_TASK_MODULE)
    fields = ()
    for node in ast.walk(module):
        if not isinstance(node, ast.Dict):
            continue
        keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
        if "object_id" in keys and "event" in keys:
            fields = tuple(keys)
            break
    return WebhookEnvelope(fields, _signature_header(module))


def _module_app_name(module):
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "app_name":
                if isinstance(node.value, ast.Constant):
                    return node.value.value
    return ""


def derived_ui_namespaces(root):
    """Every application URL namespace a first-party UI URLconf declares.

    The namespace is what an external link, a bookmark, and a template
    ``{% url %}`` all resolve through, so it is the part of the UI URL surface
    worth a promise. The individual route names underneath it are not.
    """
    namespaces = set()
    for path in sorted(Path(root).glob(UI_URLCONF_GLOB)):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        name = _module_app_name(module)
        if name:
            namespaces.add(name)
    return tuple(sorted(namespaces))


def derived_root_route_names(root):
    """Every ``name=`` a route in the root URLconf declares."""
    module = _parse(root, ROOT_URLCONF)
    names = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("path", "re_path"):
            continue
        name = _keyword_map(node).get("name")
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            names.add(name.value)
    return tuple(sorted(names))


def derived_scim_routes(root):
    """Every SCIM route both mounts publish, with its URL name."""
    routes = []
    for relative in SCIM_URL_MODULES:
        module = _parse(root, relative)
        mount = _module_app_name(module)
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "path" or not node.args:
                continue
            route = node.args[0]
            keywords = _keyword_map(node)
            name = keywords.get("name")
            if isinstance(route, ast.Constant) and isinstance(name, ast.Constant):
                routes.append(ScimRoute(mount, route.value, name.value))
    return tuple(sorted(routes))


# --------------------------------------------------------------------------
# Document reading
# --------------------------------------------------------------------------


def _without_html_comments(text):
    """The document with ordinary HTML comments removed, anchors preserved.

    A row inside ``<!-- ... -->`` is invisible in the rendered page, so it is
    not published -- and an unpublished row must not silence the rule that
    would otherwise fire on the surface it describes. Publication and
    disclosure are the same act here, which is the whole premise of a reviewed
    inventory. The contract anchors are themselves HTML comments and are the
    one exception: they delimit regions and are matched again below.
    """
    return HTML_COMMENT_RE.sub("", text)


def _anchored_lines(text, anchor):
    """The lines between one inventory anchor and the next anchor or heading.

    A fenced block is content, not structure: a ``#`` inside one is a published
    value, not the heading that ends the region.
    """
    marker = f"{ANCHOR_PREFIX} {anchor} -->"
    text = _without_html_comments(text)
    if marker not in text:
        return None
    collected = []
    fenced = False
    for line in text.split(marker, 1)[1].splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        elif not fenced and (line.startswith(ANCHOR_PREFIX) or line.startswith("#")):
            break
        collected.append(line)
    return collected


def _row_cells(line):
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _code_span(cell):
    return cell.strip("`").strip() if cell.startswith("`") and cell.endswith("`") else ""


def anchored_tokens(text, anchor):
    """Backticked first cells and fenced-block lines under one anchor.

    Two shapes, one reader: a short surface is published as a table and a long
    one (ninety report column keys) as a fenced list, because a ninety-row table
    is unreadable and an unreadable inventory does not get reviewed.
    """
    lines = _anchored_lines(text, anchor)
    if lines is None:
        return None
    tokens = []
    fenced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            if stripped:
                tokens.append(stripped)
            continue
        cells = _row_cells(line)
        if cells:
            token = _code_span(cells[0])
            if token:
                tokens.append(token)
    return tuple(tokens)


def _manifest(root):
    """Load the reviewed machine-readable contract manifest."""
    return json.loads((Path(root) / MANIFEST_DOC).read_text(encoding="utf-8"))


def documented_enum(root, name):
    values = _manifest(root).get("enums", {})
    return tuple(values[name]) if name in values else None


def documented_settings(root):
    return tuple(_manifest(root).get("settings", ()))


def documented_permissions(root):
    return tuple(_manifest(root).get("permissions", ()))


def documented_webhook_fields(root):
    return tuple(_manifest(root).get("webhook_fields", ()))


def documented_scim_routes(root):
    return tuple(_manifest(root).get("scim_routes", ()))


def documented_ui_namespaces(root):
    return tuple(_manifest(root).get("ui_namespaces", ()))


def documented_entry_routes(root):
    return tuple(_manifest(root).get("entry_routes", ()))


def documented_capabilities(root):
    """Capability rows: key -> (class, activation, scope, exclusions)."""
    return {
        key: CapabilityRow(
            row["contract_class"],
            row["activation"],
            row["scope"],
            row["exclusions"],
        )
        for key, row in _manifest(root).get("capabilities", {}).items()
    }


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _repeated(values):
    """Values a published set lists more than once, in first-repeat order."""
    seen = set()
    repeats = []
    for value in values:
        if value in seen and value not in repeats:
            repeats.append(value)
        seen.add(value)
    return repeats


def _difference_detail(name, derived, documented):
    """Set-level drift between a derived surface and its published rows.

    Membership is not enough on its own: a published set is a list of rows a
    reader counts, so a row published twice is drift even though every value is
    present.
    """
    missing = [value for value in derived if value not in documented]
    extra = [value for value in documented if value not in derived]
    repeats = _repeated(documented)
    parts = []
    if missing:
        parts.append(f"source has {missing} which the inventory omits")
    if extra:
        parts.append(f"the inventory lists {extra} which source does not declare")
    if repeats:
        parts.append(f"the inventory publishes duplicate rows for {repeats}")
    return f"{name}: " + "; ".join(parts) if parts else ""


def compare_enums(derived, documented):
    """Published values match source, and a closed enum matches its frozen set."""
    findings = []
    for source in ENUM_SOURCES:
        if source.name not in derived:
            continue
        values = derived[source.name]
        published = documented.get(source.name)
        if published is None:
            findings.append(Finding("C-ENUM1", f"{source.name}: no inventory anchor publishes this choice set"))
        else:
            detail = _difference_detail(source.name, values, published)
            if not detail and source.openness == CLOSED and tuple(published) != tuple(values):
                # A consumer may switch exhaustively on a closed set, so the
                # published rows are the value list in source order -- not a
                # bag of the same members shuffled.
                detail = (
                    f"{source.name} is declared closed for 1.x: the inventory publishes {list(published)} "
                    f"but source declares {list(values)} in that order"
                )
            if detail:
                findings.append(Finding("C-ENUM1", detail))
        if source.openness == CLOSED and values != source.frozen_values:
            findings.append(
                Finding(
                    "C-ENUM2",
                    f"{source.name} is declared closed for 1.x: source now reads {list(values)}, "
                    f"the reviewed frozen set is {list(source.frozen_values)}",
                )
            )
    return tuple(findings)


def compare_settings(derived, documented):
    """Every read name is published or explicitly excluded, and never both."""
    findings = []
    documented = tuple(documented)
    for name in derived:
        if name in documented or name in EXCLUDED_SETTINGS:
            continue
        findings.append(
            Finding("C-SET1", f"{name} is read by the settings package but is neither published nor excluded")
        )
    for name in documented:
        if name not in derived:
            findings.append(Finding("C-SET2", f"{name} is published but no settings module reads it"))
    for name in documented:
        if name in EXCLUDED_SETTINGS:
            findings.append(Finding("C-SET3", f"{name} is both published and listed as out of scope"))
    return tuple(findings)


def _capability_problems(capability, row):
    problems = []
    expected = CLASS_BY_REGISTRY_CONTRACT.get((capability.maturity, capability.activation))
    if expected is None:
        problems.append(f"({capability.maturity}, {capability.activation}) maps to no contract class")
    elif row.contract_class != expected:
        problems.append(f"published as {row.contract_class!r}, the registry declaration means {expected!r}")
    if row.activation.strip().strip("`") != capability.activation:
        problems.append(f"published activation {row.activation.strip()!r} is not {capability.activation!r}")
    return problems


def compare_capabilities(derived, documented, reviewed_limitations=None):
    """Every registered capability is published with a class and exclusions.

    ``reviewed_limitations`` is the declared limitation text each published
    ``Exclusions`` summary was written against; it defaults to the reviewed
    :data:`CAPABILITY_LIMITATIONS` and is a parameter so a test can state its
    own rather than depend on the repository's.
    """
    reviewed = CAPABILITY_LIMITATIONS if reviewed_limitations is None else reviewed_limitations
    findings = []
    for capability in derived:
        row = documented.get(capability.key)
        if row is None:
            findings.append(Finding("C-CAP1", f"{capability.key} is registered but absent from the inventory"))
            continue
        problems = _capability_problems(capability, row)
        if problems:
            findings.append(Finding("C-CAP2", f"{capability.key}: " + "; ".join(problems)))
        if row.contract_class != CLASS_STABLE and not row.exclusions.strip():
            findings.append(Finding("C-CAP3", f"{capability.key} is non-Stable and publishes no exclusions"))
        expected = reviewed.get(capability.key)
        if expected is None:
            findings.append(
                Finding(
                    "C-CAP5",
                    f"{capability.key} is published but scripts/contract_policy.py records no reviewed "
                    "limitation text its Exclusions summary was written against",
                )
            )
        elif tuple(expected) != tuple(capability.limitations):
            findings.append(
                Finding(
                    "C-CAP5",
                    f"{capability.key}: the declaration now carries {list(capability.limitations)}, "
                    f"the reviewed text behind the published Exclusions summary is {list(expected)} -- "
                    "re-read the summary against the change",
                )
            )
    keys = {capability.key for capability in derived}
    for key in documented:
        if key not in keys:
            findings.append(Finding("C-CAP4", f"{key} is published but no AppConfig registers it"))
    return tuple(findings)


def compare_permissions(derived, documented):
    """Custom permission codenames are published as ``app_label.codename``."""
    findings = []
    published = set(documented)
    identities = {f"{permission.app_label}.{permission.codename}" for permission in derived}
    for identity in sorted(identities - published):
        findings.append(Finding("C-PERM1", f"{identity} is declared in Meta.permissions but is not published"))
    for identity in sorted(published - identities):
        findings.append(Finding("C-PERM2", f"{identity} is published but no model declares it"))
    return tuple(findings)


def compare_webhook_envelope(envelope, documented):
    """The envelope's fields *and* the header its signature is sent in."""
    findings = []
    detail = _difference_detail("webhook envelope", envelope.fields, tuple(documented))
    if detail:
        findings.append(Finding("C-HOOK1", detail))
    if envelope.signature_header != WEBHOOK_SIGNATURE_HEADER:
        findings.append(
            Finding(
                "C-HOOK2",
                f"the delivery task signs with {envelope.signature_header or 'no recognisable header'!r}; "
                f"the published signature header is {WEBHOOK_SIGNATURE_HEADER!r}",
            )
        )
    return tuple(findings)


def compare_scim_routes(routes, documented):
    findings = []
    published = set(documented)
    identities = {f"{route.mount}:{route.path}" for route in routes}
    for identity in sorted(identities - published):
        findings.append(Finding("C-SCIM1", f"{identity} is routed but not published"))
    for identity in sorted(published - identities):
        findings.append(Finding("C-SCIM2", f"{identity} is published but not routed"))
    return tuple(findings)


def compare_ui_namespaces(derived, documented):
    """The namespace set is published exactly; route names are not frozen."""
    findings = []
    published = set(documented)
    for name in sorted(set(derived) - published):
        findings.append(Finding("C-URL1", f"URL namespace {name!r} is declared but not published"))
    for name in sorted(published - set(derived)):
        findings.append(Finding("C-URL2", f"URL namespace {name!r} is published but no URLconf declares it"))
    return tuple(findings)


def compare_entry_routes(derived, documented):
    """One-directional: every published entry route still exists in source.

    Deliberately not the reverse. The root URLconf declares far more names than
    the handful this policy promises, and asserting equality here would freeze
    every one of them the first time somebody added a route.
    """
    findings = []
    available = set(derived)
    for name in documented:
        if name not in available:
            findings.append(
                Finding("C-URL3", f"entry route {name!r} is published but the root URLconf has no such name")
            )
    return tuple(findings)


def _normalised(text):
    """Collapse whitespace and case.

    Both are presentation, not promise: a paragraph may be re-wrapped and a
    sentence may start with a capital without the commitment changing. Word
    order and vocabulary are not collapsed, so deleting or weakening a promise
    still fails.
    """
    return " ".join(text.split()).lower()


def compare_statements(texts):
    """Every required promise is still stated, whitespace- and case-insensitively."""
    normalised = {document: _normalised(text) for document, text in texts.items()}
    findings = []
    for statement in REQUIRED_STATEMENTS:
        haystack = normalised.get(statement.document, "")
        if _normalised(statement.text) not in haystack:
            findings.append(
                Finding("C-DOC1", f"{statement.identifier}: {statement.document} no longer states {statement.text!r}")
            )
    return tuple(findings)


def _is_sanctioned_negation(lowered, verb):
    """Whether a sanctioned negative formulation governs *this* verb."""
    return any(re.search(template.replace("{verb}", verb), lowered) for template in SANCTIONED_NEGATIONS)


def _is_bypass_claim(sentence):
    lowered = sentence.lower()
    if not any(actor in lowered for actor in BYPASS_ACTORS):
        return False
    if not any(boundary in lowered for boundary in BYPASS_BOUNDARIES):
        return False
    matched = [verb for verb in BYPASS_VERBS if re.search(rf"\b{verb}\b", lowered)]
    if not matched:
        return False
    return not all(_is_sanctioned_negation(lowered, verb) for verb in matched)


def _prose_units(text):
    """Markdown prose blocks with wrapped lines rejoined."""
    units = []
    buffer = []
    fenced = False
    for raw in text.splitlines():
        if FENCE_RE.match(raw):
            fenced = not fenced
            if buffer:
                units.append(" ".join(buffer))
                buffer = []
            continue
        if fenced:
            continue
        line = raw.strip()
        if (not line or BLOCK_START_RE.match(line)) and buffer:
            units.append(" ".join(buffer))
            buffer = []
        if line:
            buffer.append(line)
    if buffer:
        units.append(" ".join(buffer))
    return tuple(units)


def _sentences(text):
    """Sentences, with wrapped prose rejoined first.

    Published prose wraps at eighty columns, so a claim reads across two lines.
    Splitting on newlines before splitting on full stops would mean
    ``... bypasses tenant`` / ``isolation ...`` never forms a sentence anything
    can match -- the same hole as not checking at all. Fenced blocks are code
    and are skipped; a list item, a table row, a heading, and an admonition
    marker each start their own unit; everything else in a block is one flow.
    """
    for unit in _prose_units(text):
        for sentence in re.split(r"(?<=[.!?])\s+", MARKDOWN_MARKS_RE.sub("", unit)):
            stripped = sentence.strip()
            if stripped:
                yield stripped


def forbidden_wording_in(text, relative):
    """Sentences that would publish an isolation or authorization escape."""
    findings = []
    for sentence in _sentences(text):
        if _is_bypass_claim(sentence):
            findings.append(
                Finding("C-DOC2", f"{relative}: {sentence!r} reads as an isolation or authorization escape")
            )
    return tuple(findings)


def forbidden_wording_sources(root):
    """Everything the wording rule reads, keyed by manifest section.

    The prose documents moved to the private repository, so the public gate
    scans the same reviewed assertions from the code-owned manifest instead.
    Release notes and capability limitations remain local source-controlled
    surfaces and continue to be checked independently.
    """
    root = Path(root)
    manifest = _manifest(root)
    grouped = {POLICY_DOC: [], INVENTORY_DOC: [], RESOURCE_GRANT_THREAT_DOC: []}
    for statement in manifest.get("required_statements", ()):
        grouped.setdefault(statement["document"], []).append(statement["text"])
    grouped[INVENTORY_DOC].append(
        json.dumps(
            {
                key: manifest.get(key)
                for key in (
                    "enums",
                    "settings",
                    "capabilities",
                    "permissions",
                    "webhook_fields",
                    "scim_routes",
                    "ui_namespaces",
                    "entry_routes",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    sources = {}
    for section, texts in grouped.items():
        sources[section] = "\n".join(texts)
    changelog = root / CHANGELOG_DOC
    if changelog.is_file():
        sources[CHANGELOG_DOC] = changelog.read_text(encoding="utf-8")
    for relative, capability in _capability_declarations(root):
        if capability.limitations:
            sources[f"{relative}: {capability.key} limitations"] = " ".join(capability.limitations)
    return sources


def check_forbidden_wording(root):
    findings = []
    for label, text in forbidden_wording_sources(root).items():
        findings.extend(forbidden_wording_in(text, label))
    return tuple(findings)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def _documents(root):
    path = Path(root) / MANIFEST_DOC
    if not path.is_file():
        return {}, (Finding("C-DOC3", f"{MANIFEST_DOC} is missing; the 1.0 contract has no reviewed record"),)
    try:
        manifest = _manifest(root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, (Finding("C-DOC3", f"{MANIFEST_DOC} is invalid: {exc}"),)
    if manifest.get("schema_version") != 1 or manifest.get("contract_policy_version") != CONTRACT_POLICY_VERSION:
        return {}, (Finding("C-DOC3", f"{MANIFEST_DOC} has an unsupported schema or contract-policy version"),)
    grouped = {POLICY_DOC: [], INVENTORY_DOC: [], RESOURCE_GRANT_THREAT_DOC: []}
    for statement in manifest.get("required_statements", ()):
        if not isinstance(statement, dict) or not isinstance(statement.get("text"), str):
            return {}, (Finding("C-DOC3", f"{MANIFEST_DOC} contains an invalid required statement"),)
        grouped.setdefault(statement.get("document"), []).append(statement["text"])
    return {section: "\n".join(texts) for section, texts in grouped.items()}, ()


def check_all(root):
    """Every contract-policy finding for ``root``, in rule order."""
    root = Path(root)
    texts, missing = _documents(root)
    if missing:
        return missing
    documented_enums = {source.name: documented_enum(root, source.name) for source in ENUM_SOURCES}
    findings = []
    findings.extend(compare_enums(derived_enums(root), {k: v for k, v in documented_enums.items() if v is not None}))
    findings.extend(compare_settings(derived_settings(root), documented_settings(root)))
    findings.extend(compare_capabilities(derived_capabilities(root), documented_capabilities(root)))
    findings.extend(compare_permissions(derived_custom_permissions(root), documented_permissions(root)))
    findings.extend(compare_webhook_envelope(derived_webhook_envelope(root), documented_webhook_fields(root)))
    findings.extend(compare_scim_routes(derived_scim_routes(root), documented_scim_routes(root)))
    findings.extend(compare_ui_namespaces(derived_ui_namespaces(root), documented_ui_namespaces(root)))
    findings.extend(compare_entry_routes(derived_root_route_names(root), documented_entry_routes(root)))
    findings.extend(compare_statements(texts))
    findings.extend(check_forbidden_wording(root))
    return tuple(findings)


def build_parser():
    parser = argparse.ArgumentParser(description="Check the published 1.0 external-contract policy against source.")
    parser.add_argument("--root", default=None, help="repository root (defaults to this file's repository)")
    parser.add_argument("--list", action="store_true", help="print the derived surfaces instead of checking them")
    return parser
