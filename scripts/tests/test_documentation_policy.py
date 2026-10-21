"""Deterministic documentation-policy gate.

Pins the public documentation surface to operator-facing content after the
intentional removal of the internal ``docs/development/`` tree:

* public surfaces (README, ``itambox/docs/``, ``mkdocs.yml``, Django
  templates, ``.env.example``) must not reference the removed internal tree or
  the private ``itambox-community/design-docs`` repository;
* the MkDocs navigation must cover every public page and must not point at
  internal-only documents;
* every local link in the public documentation must resolve to an existing
  repository file;
* contract-bearing operator settings implemented in ``core/settings`` must be
  present in the canonical operator configuration reference (``.env.example``)
  and in the installation guide;
* the retention documentation and the ``prune_changelog`` command vocabulary
  must not diverge (class names, ``--event-days``, ``ITAMBOX_EVENT_RETENTION_DAYS``).

Normal source-code references to internal implementation concepts are out of
scope: the gate targets public documentation and UI-facing surfaces only.
"""

import re
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPOSITORY_ROOT / "itambox" / "docs"
REPO_README = REPOSITORY_ROOT / "README.md"
MKDOCS_YML = REPOSITORY_ROOT / "itambox" / "mkdocs.yml"
ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"
SETTINGS_BASE = REPOSITORY_ROOT / "itambox" / "core" / "settings" / "base.py"
SETTINGS_PROD = REPOSITORY_ROOT / "itambox" / "core" / "settings" / "prod.py"
PRUNE_CHANGELOG = REPOSITORY_ROOT / "itambox" / "core" / "management" / "commands" / "prune_changelog.py"
DATA_RETENTION = DOCS_ROOT / "operations" / "data-retention.md"
INSTALLATION = DOCS_ROOT / "operations" / "installation.md"
COMMERCIAL_VOCABULARY = DOCS_ROOT / "usage" / "commercial-vocabulary.md"

#: Private/internal reference patterns. These never belong in a public surface.
INTERNAL_REFERENCE_PATTERNS = (
    re.compile(r"github\.com/itambox-community/design-docs"),
    re.compile(r"docs/development/"),
    re.compile(
        r"development/(?:capability-registry|module-maturity|compatibility-policy|"
        r"external-contract-inventory|architecture-policy|capability-fallbacks|"
        r"tenant-resource-grant-security)\.md"
    ),
)

#: Public surfaces scanned for internal references.
PUBLIC_SURFACES = [
    REPO_README,
    ENV_EXAMPLE,
    MKDOCS_YML,
]

#: Contract-bearing operator settings. Implemented in core/settings, they must
#: be discoverable in .env.example and the installation env reference.
CONTRACT_SETTINGS = (
    "ITAMBOX_SECRET_KEY",
    "ITAMBOX_FIELD_ENCRYPTION_KEYS",
    "ITAMBOX_API_TOKEN_PEPPERS",
    "ITAMBOX_DB_PASSWORD",
    "ITAMBOX_ALLOWED_HOSTS",
    "ITAMBOX_CSRF_TRUSTED_ORIGINS",
    "ITAMBOX_CACHE_BACKEND",
    "ITAMBOX_REDIS_URL",
    "ITAMBOX_CHANGELOG_RETENTION_DAYS",
    "ITAMBOX_ALERTLOG_RETENTION_DAYS",
    "ITAMBOX_NOTIFICATION_RETENTION_DAYS",
    "ITAMBOX_EVENT_RETENTION_DAYS",
    "ITAMBOX_QTASK_FAILED_RETENTION_DAYS",
    "ITAMBOX_HEALTH_SCHEDULE_GRACE_SECONDS",
    "ITAMBOX_HEALTH_QUEUE_BACKLOG_MAX",
    "ITAMBOX_HEALTH_QUEUE_AGE_SECONDS",
    "ITAMBOX_HEALTH_JOB_STUCK_SECONDS",
    "ITAMBOX_HEALTH_FAILURES_24H_MAX",
    "ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS",
)

MARKDOWN_LINK_RE = re.compile(r"\]\(([^)]+)\)")
TARGET_RE = re.compile(r"^\s*-\s+[^:]+:\s*'([^']+)'|^\s*-\s+'([^']+)'")
NAV_TARGET_RE = re.compile(r"-\s+(?:[^:'\"]+:\s+)?'([^']+)'")

INTERNAL_DOC_NAMES = (
    "DEVELOPMENT.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
)


def _iter_public_markdown():
    yield REPO_README, "README.md"
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        yield path, path.relative_to(REPOSITORY_ROOT).as_posix()


class InternalReferencePolicyTest(unittest.TestCase):
    """Public surfaces never point at the removed internal docs or the private repo."""

    def test_public_surfaces_have_no_internal_references(self):
        surfaces = [REPO_README, ENV_EXAMPLE, MKDOCS_YML]
        surfaces += sorted(DOCS_ROOT.rglob("*.md"))
        templates_root = REPOSITORY_ROOT / "itambox"
        surfaces += sorted(path for path in templates_root.rglob("*.html") if "templates" in path.parts)
        for surface in surfaces:
            if not surface.is_file():
                continue
            relative = surface.relative_to(REPOSITORY_ROOT).as_posix()
            text = surface.read_text(encoding="utf-8")
            for pattern in INTERNAL_REFERENCE_PATTERNS:
                match = pattern.search(text)
                if match:
                    self.fail(
                        f"{relative} references removed/internal documentation "
                        f"({pattern.pattern!r}: {match.group(0)!r})"
                    )

    def test_the_readme_links_public_maturity_documentation(self):
        readme = REPO_README.read_text(encoding="utf-8")
        self.assertIn("capability-maturity.md", readme)
        self.assertNotIn("capability-registry.md", readme)


class NavigationPolicyTest(unittest.TestCase):
    """The MkDocs navigation covers every public page and nothing internal."""

    def test_every_public_page_is_in_the_navigation(self):
        nav_text = MKDOCS_YML.read_text(encoding="utf-8")
        nav_targets = set(NAV_TARGET_RE.findall(nav_text))
        self.assertTrue(nav_targets, "mkdocs.yml contains no nav targets")
        tracked = {path.relative_to(DOCS_ROOT).as_posix() for path in DOCS_ROOT.rglob("*.md")}
        not_in_nav = sorted(set(tracked) - set(nav_targets))
        self.assertEqual(not_in_nav, [], "public pages missing from the MkDocs navigation")

    def test_navigation_targets_exist_and_stay_inside_public_docs(self):
        nav_text = MKDOCS_YML.read_text(encoding="utf-8")
        nav_targets = set(NAV_TARGET_RE.findall(nav_text))
        for target in sorted(nav_targets):
            self.assertFalse(
                target.endswith(INTERNAL_DOC_NAMES),
                f"navigation points at internal-only document {target!r}",
            )
            self.assertTrue(
                target.startswith(
                    (
                        "index.md",
                        "dashboard.md",
                        "operations/",
                        "usage/",
                        "models/",
                        "integration/",
                        "plugins/",
                        "security/",
                    )
                ),
                f"navigation target {target!r} is outside the public documentation tree",
            )
            self.assertTrue(
                (REPOSITORY_ROOT / "itambox" / "docs" / target).is_file(),
                f"navigation target {target!r} does not exist",
            )


class PublicLinkPolicyTest(unittest.TestCase):
    """Local documentation links resolve to files that exist."""

    def test_local_markdown_links_resolve(self):
        broken = []
        for path, relative in _iter_public_markdown():
            text = path.read_text(encoding="utf-8")
            for match in MARKDOWN_LINK_RE.finditer(text):
                target = match.group(1).split("#", 1)[0].strip()
                if not target or target.startswith(("http://", "https://", "mailto:", "tel:")):
                    continue
                resolved = (path.parent / target).resolve()
                # Links may reach out of docs/ (e.g. root README assets, CHANGELOG).
                try:
                    resolved.relative_to(REPOSITORY_ROOT)
                except ValueError:
                    broken.append(f"{relative}: {target} escapes the repository")
                    continue
                if not resolved.exists():
                    broken.append(f"{relative}: {target} -> {resolved.relative_to(REPOSITORY_ROOT)} missing")
        self.assertEqual(broken, [], "broken local documentation links:\n" + "\n".join(broken))


class OperatorConfigurationPolicyTest(unittest.TestCase):
    """Contract-bearing settings are implemented and documented consistently."""

    def test_settings_are_implemented(self):
        sources = []
        for path in (REPOSITORY_ROOT / "itambox").rglob("*.py"):
            if "tests" in path.parts:
                continue
            sources.append(path.read_text(encoding="utf-8"))
        corpus = "\n".join(sources)
        # Plain concatenation (not an f-string): the double quotes are the
        # literal search needle — settings appear as "NAME" in source.
        missing = [setting for setting in CONTRACT_SETTINGS if '"' + setting + '"' not in corpus]
        self.assertEqual(missing, [], "contract settings not found in core sources")

    def test_settings_are_in_the_canonical_configuration_reference(self):
        env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
        installation = INSTALLATION.read_text(encoding="utf-8")
        missing = [
            setting for setting in CONTRACT_SETTINGS if setting not in env_example or setting not in installation
        ]
        self.assertEqual(
            missing,
            [],
            "contract settings absent from .env.example and/or the installation env reference",
        )


class RetentionVocabularyPolicyTest(unittest.TestCase):
    """prune_changelog and the operator documentation speak the same vocabulary."""

    def test_documented_classes_match_the_command(self):
        source = PRUNE_CHANGELOG.read_text(encoding="utf-8")
        documented = DATA_RETENTION.read_text(encoding="utf-8")
        match = re.search(r"CLASS_CHOICES = \((.*?)\)", source, re.S)
        self.assertIsNotNone(match, "CLASS_CHOICES not found in prune_changelog")
        classes = re.findall(r'"(\w+)"', match.group(1))
        self.assertTrue(classes)
        for class_name in classes:
            self.assertIn(class_name, documented, f"data-retention.md omits class {class_name!r}")

    def test_event_retention_surfaces_agree(self):
        source = PRUNE_CHANGELOG.read_text(encoding="utf-8")
        documented = DATA_RETENTION.read_text(encoding="utf-8")
        env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("--event-days", source)
        self.assertIn("--event-days", documented)
        self.assertIn("Event, and failed django-q2 tasks", source, "command help omits Event")
        self.assertIn("ITAMBOX_EVENT_RETENTION_DAYS", documented)
        self.assertIn("ITAMBOX_EVENT_RETENTION_DAYS", env_example)
        # The default must be documented truthfully as retain-indefinitely.
        self.assertIn("retain indefinitely", documented)

    def test_documentation_does_not_claim_everything_is_pruned_by_default(self):
        documented = DATA_RETENTION.read_text(encoding="utf-8")
        self.assertIn("event stream is retained indefinitely by default", documented)


class OIDCBindingRolloutDocumentationPolicyTest(unittest.TestCase):
    """The irreversible OIDC binding rollout contract remains operator-visible."""

    def test_oidc_binding_rollout_discloses_backfill_fail_closed_and_rollback_loss(self):
        upgrades = (DOCS_ROOT / "operations" / "upgrades.md").read_text(encoding="utf-8")
        required = (
            "no automatic backfill",
            "legacy logins fail closed",
            "users.0103_oidcidentity",
            "permanently drops every binding",
            "email/username claim resolution behavior is reopened",
        )
        for phrase in required:
            self.assertIn(phrase, upgrades)

    def test_sso_document_links_the_binding_rollout_runbook(self):
        sso = (DOCS_ROOT / "usage" / "sso-and-mfa.md").read_text(encoding="utf-8")
        self.assertIn("../operations/upgrades.md", sso)


class CommercialVocabularyDocumentationPolicyTest(unittest.TestCase):
    """The commercial vocabulary contract stays consistent between docs and source."""

    def test_page_publishes_the_exclusive_agreement_mapping(self):
        page = COMMERCIAL_VOCABULARY.read_text(encoding="utf-8")
        required = (
            "SaaS and cloud entitlement",
            "record as Subscription",
            "Support, maintenance, lease, warranty, SLA, or asset-covered service",
            "record as Contract",
            "Other recurring entitlement without asset/SLA coverage",
            "Other legal or commercial agreement",
            "Record one agreement in one module only",
        )
        for phrase in required:
            self.assertIn(phrase, page)

    def test_page_publishes_the_seat_metric_labels(self):
        page = COMMERCIAL_VOCABULARY.read_text(encoding="utf-8")
        self.assertIn("Agreement Entitled Quantity", page)
        self.assertIn("Linked License Seats", page)
        self.assertIn("This value is independent of linked License seats", page)

    def test_subscription_source_matches_the_documented_labels(self):
        models_source = (REPOSITORY_ROOT / "itambox" / "subscriptions" / "models.py").read_text(encoding="utf-8")
        template_source = (
            REPOSITORY_ROOT
            / "itambox"
            / "templates"
            / "subscriptions"
            / "includes"
            / "detail"
            / "subscription_info.html"
        ).read_text(encoding="utf-8")
        forms_source = (REPOSITORY_ROOT / "itambox" / "subscriptions" / "forms.py").read_text(encoding="utf-8")
        self.assertIn("Agreement Entitled Quantity", models_source)
        self.assertIn("Linked License Seats", template_source)
        self.assertIn("Record one agreement in one module only", forms_source)


if __name__ == "__main__":
    unittest.main()
