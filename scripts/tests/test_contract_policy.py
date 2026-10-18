"""Contract-policy gate: derived source facts, the machine-readable manifest, and drift.

The machine-readable 1.0 contract is only worth what it can be checked against. Three
failure modes make a prose compatibility promise worthless, and none of them is
visible in a review of the document alone:

* **The document drifts from source.** A ninth ``ScheduledReport`` frequency
  becomes a tenth, an ``ITAMBOX_*`` setting is renamed, a SCIM route changes
  shape -- and the published promise silently describes a product that no longer
  exists.
* **A new surface arrives unpublished.** A custom permission codename or a
  capability registered from an ``AppConfig`` that nobody added to the inventory
  is an external contract with no declared class.
* **The wording quietly weakens a safety floor.** A sentence that reads as
  though Beta, a superuser, or a hidden view may cross the tenant boundary is a
  security regression published as documentation.

Everything here is stdlib-only and reads source with :mod:`ast` rather than
importing Django, so CI runs it on the bare interpreter through the repository
gate-suite discovery, before any dependency is installed.

Test classes are labelled by intent. ``Derivation*`` classes are
**characterization** tests: they pin what the current source says today so a
later change to a contract surface has to be a deliberate edit here. The
``Drift*`` and ``Wording*`` classes are behavioural -- they prove the gate
actually fails when it should, which no amount of green on real inputs shows.
"""

import ast
import unittest
from pathlib import Path

from scripts import contract_policy as policy
from scripts.check_contract_policy import main

REPO_ROOT = Path(__file__).resolve().parents[2]

# The nine frequencies the issue freezes as a Stable-graded sub-surface of a
# Beta capability. Written out rather than derived: a test that recomputes the
# value it asserts proves nothing about what shipped.
NINE_FREQUENCIES = (
    "once",
    "hourly",
    "daily",
    "weekly",
    "biweekly",
    "monthly",
    "quarterly",
    "yearly",
    "cron",
)

FOUR_SUBSCRIPTION_STATUSES = (
    "active",
    "suspended",
    "cancelled",
    "expired",
)


def declared(key, maturity, activation, security_critical=False, limitations=("a declared limitation",)):
    """A registry declaration, built in one place so its shape can evolve."""
    return policy.DeclaredCapability(
        key=key,
        maturity=maturity,
        activation=activation,
        security_critical=security_critical,
        limitations=tuple(limitations),
    )


def compare_capabilities(derived, documented, reviewed=None):
    """``compare_capabilities`` against the reviewed limitation text a test declares.

    Passing the reviewed text explicitly keeps a fixture from failing ``C-CAP5``
    for the unrelated reason that a made-up capability key is not in the real
    module constant.
    """
    if reviewed is None:
        reviewed = {capability.key: capability.limitations for capability in derived}
    return policy.compare_capabilities(derived, documented, reviewed_limitations=reviewed)


class DerivationEnumTests(unittest.TestCase):
    """Characterization: the persisted choice sets this release publishes."""

    @classmethod
    def setUpClass(cls):
        cls.enums = policy.derived_enums(REPO_ROOT)

    def test_scheduled_report_declares_exactly_nine_frequencies(self):
        values = self.enums["extras.ScheduledReport.FREQUENCY_CHOICES"]
        self.assertEqual(values, NINE_FREQUENCIES)
        self.assertEqual(len(values), 9)

    def test_purchase_order_statuses_are_the_six_state_machine_values(self):
        self.assertEqual(
            self.enums["procurement.PurchaseOrder.STATUS_CHOICES"],
            ("draft", "approved", "ordered", "partial", "received", "cancelled"),
        )

    def test_subscription_statuses_are_the_four_persisted_values(self):
        self.assertEqual(
            self.enums["subscriptions.SubscriptionStatusChoices"],
            FOUR_SUBSCRIPTION_STATUSES,
        )

    def test_alert_types_and_severities_are_derived_from_the_model(self):
        self.assertEqual(
            self.enums["extras.AlertRule.ALERT_TYPE_CHOICES"],
            ("low_stock", "upcoming_eol", "license_expiry", "renewal_due", "warranty_expiry", "audit_overdue"),
        )
        self.assertEqual(self.enums["extras.AlertRule.SEVERITY_CHOICES"], ("info", "warning", "critical"))

    def test_report_types_are_the_eleven_persisted_identifiers(self):
        self.assertEqual(len(self.enums["extras.ReportTemplate.REPORT_TYPE_CHOICES"]), 11)
        self.assertIn("asset_summary", self.enums["extras.ReportTemplate.REPORT_TYPE_CHOICES"])
        self.assertIn("custody_compliance", self.enums["extras.ReportTemplate.REPORT_TYPE_CHOICES"])

    def test_report_column_keys_are_derived_from_the_designer_form(self):
        columns = self.enums["extras.ReportTemplateForm.COLUMN_CHOICES"]
        self.assertIn("asset_tag", columns)
        self.assertIn("custody_qms_reference", columns)
        self.assertEqual(
            len(columns), len(set(columns)), "a duplicated column key would silently shadow a report field"
        )

    def test_every_declared_enum_source_resolves(self):
        self.assertEqual(set(self.enums), {source.name for source in policy.ENUM_SOURCES})
        for name, values in self.enums.items():
            with self.subTest(enum=name):
                self.assertTrue(values, f"{name} derived no values")

    def test_a_closed_enum_matches_its_reviewed_frozen_values(self):
        for source in policy.ENUM_SOURCES:
            if source.openness != policy.CLOSED:
                continue
            with self.subTest(enum=source.name):
                self.assertEqual(self.enums[source.name], source.frozen_values)

    def test_an_open_enum_declares_no_frozen_values(self):
        for source in policy.ENUM_SOURCES:
            if source.openness == policy.OPEN:
                with self.subTest(enum=source.name):
                    self.assertEqual(source.frozen_values, ())


class DerivationSurfaceTests(unittest.TestCase):
    """Characterization: the non-enum surfaces the inventory has to cover."""

    def test_the_webhook_envelope_fields_are_read_from_the_task(self):
        envelope = policy.derived_webhook_envelope(REPO_ROOT)
        self.assertEqual(
            envelope.fields,
            (
                "schema_version",
                "event_id",
                "delivery_id",
                "attempt",
                "tenant",
                "event",
                "model",
                "object_id",
                "timestamp",
                "data",
            ),
        )
        self.assertEqual(envelope.signature_header, "X-Hub-Signature-256")

    def test_both_scim_mounts_route_on_string_compatible_dual_read_identifiers(self):
        routes = policy.derived_scim_routes(REPO_ROOT)
        mounts = {route.mount for route in routes}
        self.assertEqual(mounts, {"scim", "provider_scim"})
        string_routes = [route for route in routes if "<str:pk>" in route.path]
        self.assertEqual(
            sorted(route.name for route in string_routes),
            ["group-detail", "group-detail", "user-detail", "user-detail"],
            "1.x accepts both decimal legacy IDs and opaque IDs through string-compatible routes",
        )
        self.assertFalse(any("<int:pk>" in route.path for route in routes))

    def test_the_custom_permission_codenames_are_derived_from_model_meta(self):
        codenames = {permission.codename for permission in policy.derived_custom_permissions(REPO_ROOT)}
        self.assertEqual(
            codenames,
            {
                "dispose_asset",
                "add_delegated_assetrequest",
                "approve_assetrequest",
                "fulfill_assetrequest",
                "view_recyclebin",
                "view_plugin_diagnostics",
                "change_recyclebin",
                "delete_recyclebin",
                "receive_purchaseorder",
                "approve_purchaseorder",
                "prepare_custodyreceipt",
                "export_custodyreceipt",
            },
        )

    def test_every_registered_capability_is_derived_from_its_app_config(self):
        capabilities = policy.derived_capabilities(REPO_ROOT)
        self.assertEqual(len(capabilities), 13)
        by_key = {capability.key: capability for capability in capabilities}
        self.assertEqual(by_key["organization.role_grants"].security_critical, True)
        self.assertEqual(by_key["organization.resource_grants"].security_critical, True)
        self.assertEqual(by_key["platform.plugins"].maturity, "experimental")
        self.assertEqual(by_key["users.scim_provisioning"].activation, "always-on")

    def test_the_capability_vocabulary_is_read_from_the_registry_module(self):
        """The constant names in an ``apps.py`` mean whatever the registry says."""
        vocabulary = policy.registry_vocabulary(REPO_ROOT)
        self.assertEqual(vocabulary["STABLE"], "stable")
        self.assertEqual(vocabulary["ALWAYS_ON"], "always-on")
        self.assertEqual(vocabulary["SOURCE_OPERATOR_FLAG"], "operator-flag")

    def test_the_ui_url_namespaces_are_derived_from_the_urlconfs(self):
        namespaces = policy.derived_ui_namespaces(REPO_ROOT)
        self.assertIn("assets", namespaces)
        self.assertIn("organization", namespaces)
        self.assertNotIn(
            "plugins",
            namespaces,
            "the plugin URL namespace belongs to the Experimental plugin system, "
            "not to the promised first-party namespace set",
        )

    def test_the_root_entry_route_names_are_derived(self):
        names = policy.derived_root_route_names(REPO_ROOT)
        for expected in ("dashboard", "login", "search", "scan_resolve"):
            with self.subTest(route=expected):
                self.assertIn(expected, names)

    def test_the_uninventoried_internal_enums_are_named_rather_than_forgotten(self):
        """A bounded inventory has to say what it left out and why."""
        inventoried = {source.name for source in policy.ENUM_SOURCES}
        for name in policy.UNINVENTORIED_INTERNAL_ENUMS:
            with self.subTest(enum=name):
                self.assertNotIn(name, inventoried)

    def test_every_itambox_setting_the_application_reads_is_derived(self):
        names = policy.derived_settings(REPO_ROOT)
        self.assertNotIn("ITAMBOX_REPORT_DESIGNER_ENABLED", names)
        self.assertIn("ITAMBOX_FIELD_ENCRYPTION_KEYS", names)
        self.assertEqual(sorted(names), list(names), "derived setting names are sorted for a stable diff")


class PolicyShapeTests(unittest.TestCase):
    """The declared policy is internally consistent before it meets source."""

    def test_the_contract_class_vocabulary_is_closed(self):
        self.assertEqual(
            policy.CONTRACT_CLASSES,
            ("stable", "beta-enabled", "beta-opt-in", "experimental"),
        )

    def test_every_registry_contract_maps_to_exactly_one_contract_class(self):
        for contract, klass in policy.CLASS_BY_REGISTRY_CONTRACT.items():
            with self.subTest(contract=contract):
                self.assertIn(klass, policy.CONTRACT_CLASSES)

    def test_the_settings_partition_is_disjoint(self):
        overlap = set(policy.EXCLUDED_SETTINGS) & set(policy.documented_settings(REPO_ROOT))
        self.assertEqual(overlap, set())

    def test_every_excluded_setting_carries_a_reason(self):
        for name, reason in policy.EXCLUDED_SETTINGS.items():
            with self.subTest(setting=name):
                self.assertTrue(name.startswith("ITAMBOX_"))
                self.assertGreaterEqual(len(reason), 20, "an exclusion without a reason is an omission")

    def test_every_required_statement_has_an_identifier_and_a_source_document(self):
        for statement in policy.REQUIRED_STATEMENTS:
            with self.subTest(statement=statement.identifier):
                self.assertTrue(statement.identifier.startswith("P-"))
                self.assertIn(
                    statement.document,
                    (
                        policy.POLICY_DOC,
                        policy.INVENTORY_DOC,
                        policy.RESOURCE_GRANT_THREAT_DOC,
                    ),
                )
                self.assertTrue(statement.text.strip())

    def test_resource_grant_freeze_pins_named_suite_and_threat_invariants(self):
        identifiers = {statement.identifier for statement in policy.REQUIRED_STATEMENTS}
        self.assertTrue(
            {
                "P-RESOURCE-GRANT-DIRECT-WRITES",
                "P-RESOURCE-GRANT-CANONICAL",
                "P-RESOURCE-GRANT-CANONICAL-REEXPORT",
                "P-RESOURCE-GRANT-SYSTEM-PROVENANCE-SUITE",
                "P-RESOURCE-GRANT-ALLOWLIST-CLOSED",
                "P-RESOURCE-GRANT-PERSISTED-DERIVATION",
                "P-RESOURCE-GRANT-INDEPENDENT-RBAC",
                "P-RESOURCE-GRANT-TOPOLOGY-FAIL-CLOSED",
                "P-RESOURCE-GRANT-UNSCOPED-MANAGER",
                "P-RESOURCE-GRANT-CONTAINER-VISIBILITY",
                "P-RESOURCE-GRANT-SYSTEM-PROVENANCE",
                "P-RESOURCE-GRANT-IMMUTABLE-PROVENANCE",
                "P-RESOURCE-GRANT-LIFECYCLE-ATTRIBUTION",
            }.issubset(identifiers)
        )


class PublishedContractTests(unittest.TestCase):
    """The repository as it stands satisfies every rule the gate enforces."""

    def test_the_code_owned_contract_manifest_is_tracked(self):
        self.assertTrue((REPO_ROOT / policy.MANIFEST_DOC).is_file())

    def test_the_gate_reports_no_finding_for_this_repository(self):
        findings = policy.check_all(REPO_ROOT)
        self.assertEqual(
            [f"{finding.rule} {finding.detail}" for finding in findings],
            [],
        )

    def test_the_command_line_gate_exits_zero(self):
        self.assertEqual(main(["--root", str(REPO_ROOT)]), 0)

    def test_every_capability_is_published_with_a_class_and_an_exclusion_note(self):
        rows = policy.documented_capabilities(REPO_ROOT)
        derived = {capability.key for capability in policy.derived_capabilities(REPO_ROOT)}
        self.assertEqual(set(rows), derived)
        for key, row in rows.items():
            with self.subTest(capability=key):
                self.assertIn(row.contract_class, policy.CONTRACT_CLASSES)
                self.assertTrue(row.activation)
                self.assertTrue(row.scope)
                self.assertTrue(row.exclusions)


class DriftDetectionTests(unittest.TestCase):
    """Behavioural: the gate has to fail, or the green above means nothing."""

    def test_a_dropped_enum_value_in_the_document_is_a_finding(self):
        documented = {"extras.ScheduledReport.FREQUENCY_CHOICES": NINE_FREQUENCIES[:-1]}
        derived = {"extras.ScheduledReport.FREQUENCY_CHOICES": NINE_FREQUENCIES}
        findings = policy.compare_enums(derived, documented)
        self.assertEqual([finding.rule for finding in findings], ["C-ENUM1"])
        self.assertIn("cron", findings[0].detail)

    def test_a_value_the_document_invents_is_a_finding(self):
        documented = {"extras.ScheduledReport.FREQUENCY_CHOICES": NINE_FREQUENCIES + ("fortnightly",)}
        derived = {"extras.ScheduledReport.FREQUENCY_CHOICES": NINE_FREQUENCIES}
        findings = policy.compare_enums(derived, documented)
        self.assertEqual([finding.rule for finding in findings], ["C-ENUM1"])
        self.assertIn("fortnightly", findings[0].detail)

    def test_a_tenth_frequency_in_source_breaks_the_closed_enum_rule(self):
        documented = {"extras.ScheduledReport.FREQUENCY_CHOICES": NINE_FREQUENCIES + ("fortnightly",)}
        derived = dict(documented)
        rules = [finding.rule for finding in policy.compare_enums(derived, documented)]
        self.assertIn("C-ENUM2", rules)

    def test_an_unpublished_setting_is_a_finding(self):
        findings = policy.compare_settings(("ITAMBOX_ENV", "ITAMBOX_BRAND_NEW"), ("ITAMBOX_ENV",))
        self.assertEqual([finding.rule for finding in findings], ["C-SET1"])
        self.assertIn("ITAMBOX_BRAND_NEW", findings[0].detail)

    def test_a_setting_the_document_lists_but_source_never_reads_is_a_finding(self):
        findings = policy.compare_settings(("ITAMBOX_ENV",), ("ITAMBOX_ENV", "ITAMBOX_GONE"))
        self.assertEqual([finding.rule for finding in findings], ["C-SET2"])
        self.assertIn("ITAMBOX_GONE", findings[0].detail)

    def test_an_excluded_setting_may_not_also_be_published(self):
        name = next(iter(policy.EXCLUDED_SETTINGS))
        findings = policy.compare_settings((name,), (name,))
        self.assertEqual([finding.rule for finding in findings], ["C-SET3"])

    def test_an_unpublished_capability_is_a_finding(self):
        derived = (declared("demo.thing", "beta", "opt-in"),)
        findings = compare_capabilities(derived, {})
        self.assertEqual([finding.rule for finding in findings], ["C-CAP1"])

    def test_a_capability_published_under_the_wrong_class_is_a_finding(self):
        derived = (declared("demo.thing", "beta", "opt-in"),)
        documented = {"demo.thing": policy.CapabilityRow("stable", "opt-in", "scope", "none")}
        findings = compare_capabilities(derived, documented)
        self.assertEqual([finding.rule for finding in findings], ["C-CAP2"])

    def test_a_non_stable_capability_published_without_exclusions_is_a_finding(self):
        derived = (declared("demo.thing", "beta", "opt-in"),)
        documented = {"demo.thing": policy.CapabilityRow("beta-opt-in", "opt-in", "scope", "")}
        findings = compare_capabilities(derived, documented)
        self.assertEqual([finding.rule for finding in findings], ["C-CAP3"])

    def test_an_unpublished_permission_codename_is_a_finding(self):
        derived = (policy.CustomPermission("assets", "Asset", "teleport_asset"),)
        findings = policy.compare_permissions(derived, ())
        self.assertEqual([finding.rule for finding in findings], ["C-PERM1"])
        self.assertIn("assets.teleport_asset", findings[0].detail)

    def test_a_changed_webhook_envelope_field_is_a_finding(self):
        envelope = policy.WebhookEnvelope(
            ("event", "model", "object_id", "timestamp", "payload"), "X-Hub-Signature-256"
        )
        findings = policy.compare_webhook_envelope(envelope, ("event", "model", "object_id", "timestamp", "data"))
        self.assertEqual([finding.rule for finding in findings], ["C-HOOK1"])

    def test_a_scim_route_that_stops_being_published_is_a_finding(self):
        routes = (policy.ScimRoute("scim", "Users/<int:pk>", "user-detail"),)
        findings = policy.compare_scim_routes(routes, ())
        self.assertEqual([finding.rule for finding in findings], ["C-SCIM1"])

    def test_an_unpublished_url_namespace_is_a_finding(self):
        findings = policy.compare_ui_namespaces(("assets", "brandnew"), ("assets",))
        self.assertEqual([finding.rule for finding in findings], ["C-URL1"])
        self.assertIn("brandnew", findings[0].detail)

    def test_a_published_namespace_no_urlconf_declares_is_a_finding(self):
        findings = policy.compare_ui_namespaces(("assets",), ("assets", "ghosts"))
        self.assertEqual([finding.rule for finding in findings], ["C-URL2"])

    def test_a_published_entry_route_that_disappeared_is_a_finding(self):
        findings = policy.compare_entry_routes(("dashboard",), ("dashboard", "gone"))
        self.assertEqual([finding.rule for finding in findings], ["C-URL3"])

    def test_an_unpublished_entry_route_is_not_a_finding(self):
        """The root URLconf may grow routes; the promise covers only the named ones."""
        self.assertEqual(policy.compare_entry_routes(("dashboard", "brand_new"), ("dashboard",)), ())

    def test_a_missing_required_statement_is_a_finding(self):
        statement = policy.REQUIRED_STATEMENTS[0]
        findings = policy.compare_statements({statement.document: "nothing relevant here"})
        self.assertIn(statement.identifier, " ".join(finding.detail for finding in findings))
        self.assertEqual({finding.rule for finding in findings}, {"C-DOC1"})

    def test_a_re_wrapped_or_recapitalised_promise_is_still_found(self):
        """Presentation may change; the commitment may not."""
        statement = policy.REQUIRED_STATEMENTS[0]
        rendered = f"Intro.\n**{statement.text.upper()}**\nmore text."
        reported = " ".join(finding.detail for finding in policy.compare_statements({statement.document: rendered}))
        self.assertNotIn(statement.identifier, reported)

    def test_a_missing_document_is_a_finding_rather_than_a_crash(self):
        findings = policy.check_all(REPO_ROOT / "does-not-exist")
        self.assertIn("C-DOC3", {finding.rule for finding in findings})

    def test_the_command_line_gate_exits_non_zero_when_a_document_is_missing(self):
        self.assertEqual(main(["--root", str(REPO_ROOT / "does-not-exist")]), 1)


class TextChoicesDerivationTests(unittest.TestCase):
    """Adversarial: Django's bare ``MEMBER = "value"`` is a real persisted value.

    ``TextChoices`` derives the label when a member is written without one, so
    the two spellings are interchangeable in source and must be interchangeable
    here. Reading only the labelled spelling lets a value be added to an enum
    this policy declares frozen while every rule stays silent.
    """

    def _members(self, body):
        return policy._text_choices_values(ast.parse(body).body[0])

    def test_a_bare_string_member_is_derived_like_a_labelled_one(self):
        self.assertEqual(
            self._members(
                'class Status(models.TextChoices):\n    ACTIVE = "active", _("Active")\n    ARCHIVED = "archived"\n'
            ),
            ("active", "archived"),
        )

    def test_a_private_class_attribute_is_not_a_member(self):
        """``TextChoices`` skips underscore-prefixed names; so does the reader."""
        self.assertEqual(
            self._members(
                "class Status(models.TextChoices):\n"
                '    _internal = "not-a-member"\n'
                '    ACTIVE = "active", _("Active")\n'
            ),
            ("active",),
        )

    def test_a_bare_member_appended_to_a_closed_enum_trips_both_enum_rules(self):
        """Mutation probe: the exact edit that used to leave the gate green."""
        source = (REPO_ROOT / "itambox" / "subscriptions" / "models.py").read_text(encoding="utf-8")
        mutated = source.replace(
            '    EXPIRED = "expired", _("Expired")',
            '    EXPIRED = "expired", _("Expired")\n    ARCHIVED = "archived"',
            1,
        )
        self.assertNotEqual(mutated, source, "the probe no longer matches the model source it mutates")
        node = policy._class_node(ast.parse(mutated), "SubscriptionStatusChoices")
        values = policy._text_choices_values(node)
        self.assertEqual(values, FOUR_SUBSCRIPTION_STATUSES + ("archived",))
        name = "subscriptions.SubscriptionStatusChoices"
        findings = policy.compare_enums({name: values}, {name: FOUR_SUBSCRIPTION_STATUSES})
        self.assertEqual(sorted({finding.rule for finding in findings}), ["C-ENUM1", "C-ENUM2"])


class AnchorParsingTests(unittest.TestCase):
    """Adversarial: publication and disclosure may not be separated.

    Every rule that compares source against the inventory reads its published
    set through one anchored-region parser. A row a reader never sees because
    it sits inside an ordinary HTML comment is not published, and must not
    silence the rule that would otherwise fire.
    """

    def test_a_commented_out_table_row_does_not_count_as_published(self):
        text = (
            "<!-- contract-inventory: settings -->\n\n"
            "| Setting | What |\n|---|---|\n"
            "| `ITAMBOX_VISIBLE` | shown |\n"
            "<!--\n| `ITAMBOX_HIDDEN` | invisible in the rendered page |\n-->\n"
        )
        self.assertEqual(policy.anchored_tokens(text, "settings"), ("ITAMBOX_VISIBLE",))

    def test_an_inline_html_comment_does_not_publish_a_row(self):
        text = (
            "<!-- contract-inventory: permissions -->\n\n"
            "| Codename | Grants |\n|---|---|\n"
            "<!-- | `assets.ghost_asset` | never rendered | -->\n"
            "| `assets.dispose_asset` | Record asset disposal |\n"
        )
        self.assertEqual(policy.anchored_tokens(text, "permissions"), ("assets.dispose_asset",))

    def test_a_commented_out_line_in_a_fenced_block_is_not_published(self):
        anchor = "enum extras.ReportTemplateForm.COLUMN_CHOICES"
        text = f"<!-- contract-inventory: {anchor} -->\n\n```text\nasset_tag\n<!-- ghost_column -->\nname\n```\n"
        self.assertEqual(policy.anchored_tokens(text, anchor), ("asset_tag", "name"))

    def test_a_hash_inside_a_fenced_block_does_not_truncate_the_published_set(self):
        """A fence holds content, not headings; truncating there fails for the wrong reason."""
        anchor = "enum extras.ReportTemplateForm.COLUMN_CHOICES"
        text = f"<!-- contract-inventory: {anchor} -->\n\n```text\nasset_tag\n#not_a_heading\nname\n```\n"
        self.assertEqual(policy.anchored_tokens(text, anchor), ("asset_tag", "#not_a_heading", "name"))

    def test_the_anchor_comment_itself_still_delimits_the_region(self):
        text = (
            "<!-- contract-inventory: ui-namespaces -->\n\n"
            "| `assets` | /assets/ |\n"
            "<!-- contract-inventory: entry-routes -->\n\n"
            "| `dashboard` | Application root |\n"
        )
        self.assertEqual(policy.anchored_tokens(text, "ui-namespaces"), ("assets",))
        self.assertEqual(policy.anchored_tokens(text, "entry-routes"), ("dashboard",))


class SettingsDerivationTests(unittest.TestCase):
    """Adversarial: a real read counts, and prose never does.

    ``C-SET1``'s promise is that a new ``ITAMBOX_*`` name cannot arrive
    unclassified. A text scan of one directory cannot keep it: it misses every
    read made outside that directory and counts a comment, a docstring, or a
    warning message as though it were a read.
    """

    @classmethod
    def setUpClass(cls):
        cls.names = policy.derived_settings(REPO_ROOT)
        cls.scanned = {path.as_posix() for path in policy.scanned_python_files(REPO_ROOT)}

    def _names(self, body, in_settings_package=False):
        return policy.setting_names_in(ast.parse(body), in_settings_package)

    def test_a_comment_is_not_a_read(self):
        self.assertEqual(self._names("# TODO: drop ITAMBOX_PHANTOM_KNOB one day\nx = 1\n"), ())

    def test_a_docstring_is_not_a_read(self):
        self.assertEqual(self._names('"""Set ITAMBOX_PHANTOM_KNOB to enable this."""\n'), ())

    def test_a_warning_message_is_not_a_read(self):
        body = 'warnings.warn("ITAMBOX_PHANTOM_KNOB is not set; using the insecure default.")\n'
        self.assertEqual(self._names(body, in_settings_package=True), ())

    def test_the_production_warning_prose_no_longer_publishes_a_setting(self):
        """The live phantom: this name is read in ``core/crypto.py``, not here."""
        prod = ast.parse((REPO_ROOT / "itambox" / "core" / "settings" / "prod.py").read_text(encoding="utf-8"))
        self.assertNotIn("ITAMBOX_FIELD_ENCRYPTION_KEYS", policy.setting_names_in(prod, in_settings_package=True))

    def test_an_environment_read_is_a_read(self):
        body = (
            'import os\na = os.environ.get("ITAMBOX_A", "")\nb = os.getenv("ITAMBOX_B")\nc = os.environ["ITAMBOX_C"]\n'
        )
        self.assertEqual(self._names(body), ("ITAMBOX_A", "ITAMBOX_B", "ITAMBOX_C"))

    def test_a_django_settings_read_is_a_read(self):
        body = 'from django.conf import settings\nd = getattr(settings, "ITAMBOX_D", True)\ne = settings.ITAMBOX_E\n'
        self.assertEqual(self._names(body), ("ITAMBOX_D", "ITAMBOX_E"))

    def test_a_settings_package_assignment_publishes_the_name_it_defines(self):
        body = 'ITAMBOX_TENANT_X_CONFIGS = _load_tenant_json_config("ITAMBOX_TENANT_X_CONFIGS")\n'
        self.assertEqual(self._names(body, in_settings_package=True), ("ITAMBOX_TENANT_X_CONFIGS",))
        self.assertEqual(self._names(body), (), "only the settings package defines a settings attribute")

    def test_the_field_encryption_key_read_outside_the_settings_package_is_derived(self):
        self.assertIn("ITAMBOX_FIELD_ENCRYPTION_KEYS", self.names)
        self.assertIn("itambox/core/crypto.py", self.scanned)

    def test_the_sso_privileged_role_switch_is_derived_and_classified(self):
        """The authorization-relevant knob the old scan could not see."""
        self.assertIn("ITAMBOX_SSO_AUTOCREATE_PRIVILEGED_ROLES", self.names)
        self.assertIn("itambox/organization/services/identity_provisioning.py", self.scanned)
        self.assertNotIn("itambox/core/auth/provisioning.py", self.scanned)
        self.assertEqual(policy.compare_settings(self.names, policy.documented_settings(REPO_ROOT)), ())

    def test_every_derived_name_is_published_or_excluded_with_a_reason(self):
        unclassified = [
            name
            for name in self.names
            if name not in policy.documented_settings(REPO_ROOT) and name not in policy.EXCLUDED_SETTINGS
        ]
        self.assertEqual(unclassified, [])

    def test_tests_migrations_and_generated_trees_are_out_of_scope(self):
        self.assertNotIn("itambox/docs/integration/offboard_user.py", self.scanned)
        self.assertNotIn("itambox/static/docs/integration/offboard_user.py", self.scanned)
        self.assertEqual([path for path in self.scanned if "/migrations/" in path], [])
        self.assertEqual([path for path in self.scanned if "/tests/" in path], [])
        self.assertEqual([path for path in self.scanned if path.rsplit("/", 1)[-1].startswith("test_")], [])

    def test_the_derived_names_are_sorted_for_a_stable_diff(self):
        self.assertEqual(sorted(self.names), list(self.names))


class RegistryContractMappingTests(unittest.TestCase):
    """The derivation map may admit exactly the shapes the class table sells."""

    def test_exactly_the_four_published_registry_contracts_map_to_a_class(self):
        self.assertEqual(
            policy.CLASS_BY_REGISTRY_CONTRACT,
            {
                ("stable", "always-on"): "stable",
                ("beta", "enabled"): "beta-enabled",
                ("beta", "opt-in"): "beta-opt-in",
                ("experimental", "opt-in"): "experimental",
            },
        )

    def test_a_default_on_experimental_capability_fails_closed(self):
        """The policy promises there is no default-on Experimental surface."""
        derived = (declared("demo.thing", "experimental", "enabled"),)
        documented = {"demo.thing": policy.CapabilityRow("experimental", "enabled", "scope", "pin the revision")}
        findings = compare_capabilities(derived, documented)
        self.assertEqual([finding.rule for finding in findings], ["C-CAP2"])
        self.assertIn("maps to no contract class", findings[0].detail)


class CapabilityExclusionTests(unittest.TestCase):
    """The ``Exclusions`` column is a summary, pinned to the text it summarises.

    The inventory cannot restate a registry limitation verbatim and stay
    readable, and a summary nothing binds is a claim that quietly stops being
    true. So the exact declared text each summary was written against is
    recorded in the reviewed module, and a change to that text fails the gate
    until the summary is re-read against it.
    """

    def test_every_registered_capability_has_reviewed_limitation_text(self):
        derived = policy.derived_capabilities(REPO_ROOT)
        self.assertEqual(set(policy.CAPABILITY_LIMITATIONS), {capability.key for capability in derived})
        for capability in derived:
            with self.subTest(capability=capability.key):
                self.assertEqual(capability.limitations, policy.CAPABILITY_LIMITATIONS[capability.key])

    def test_the_repository_publishes_a_summary_for_every_declared_limitation(self):
        """Every non-Stable capability's summary is non-empty and its text is pinned."""
        rows = policy.documented_capabilities(REPO_ROOT)
        for capability in policy.derived_capabilities(REPO_ROOT):
            with self.subTest(capability=capability.key):
                row = rows[capability.key]
                if capability.limitations:
                    self.assertTrue(row.exclusions.strip())
                    self.assertNotEqual(row.exclusions.strip().lower(), "none")
                else:
                    self.assertEqual(row.exclusions.strip().lower(), "none")

    def test_a_reworded_limitation_leaves_the_summary_stale_and_is_a_finding(self):
        """The exact failure this rule exists to make impossible."""
        derived = (
            declared(
                "automation.webhooks",
                "beta",
                "opt-in",
                limitations=("Deliveries are now recorded in a durable delivery log and replayable.",),
            ),
        )
        documented = {
            "automation.webhooks": policy.CapabilityRow(
                "beta-opt-in",
                "opt-in",
                "Event rules and webhook endpoints",
                "Payload schema is not frozen; deliveries are fire-and-forget with no delivery log or replay",
            )
        }
        findings = compare_capabilities(derived, documented, reviewed=policy.CAPABILITY_LIMITATIONS)
        self.assertEqual([finding.rule for finding in findings], ["C-CAP5"])
        self.assertIn("automation.webhooks", findings[0].detail)

    def test_a_limitation_added_to_a_stable_capability_is_a_finding(self):
        derived = (
            declared(
                "procurement.core",
                "stable",
                "always-on",
                limitations=("Partial receipts are not reconciled against the order line.",),
            ),
        )
        documented = {"procurement.core": policy.CapabilityRow("stable", "always-on", "Purchase orders", "none")}
        findings = compare_capabilities(derived, documented, reviewed=policy.CAPABILITY_LIMITATIONS)
        self.assertEqual([finding.rule for finding in findings], ["C-CAP5"])

    def test_a_capability_with_no_reviewed_limitation_text_is_a_finding(self):
        derived = (declared("demo.thing", "beta", "opt-in"),)
        documented = {"demo.thing": policy.CapabilityRow("beta-opt-in", "opt-in", "scope", "an exclusion")}
        findings = compare_capabilities(derived, documented, reviewed={})
        self.assertEqual([finding.rule for finding in findings], ["C-CAP5"])
        self.assertIn("no reviewed limitation text", findings[0].detail)


class UnpublishedSurfaceDriftTests(unittest.TestCase):
    """Behavioural: the three rules that fire on a published surface source lost."""

    def test_a_published_capability_no_app_config_registers_is_a_finding(self):
        documented = {"ghost.capability": policy.CapabilityRow("stable", "always-on", "scope", "none")}
        findings = compare_capabilities((), documented)
        self.assertEqual([finding.rule for finding in findings], ["C-CAP4"])
        self.assertIn("ghost.capability", findings[0].detail)

    def test_a_published_permission_codename_no_model_declares_is_a_finding(self):
        findings = policy.compare_permissions((), ("assets.ghost_permission",))
        self.assertEqual([finding.rule for finding in findings], ["C-PERM2"])
        self.assertIn("assets.ghost_permission", findings[0].detail)

    def test_a_published_scim_route_that_is_not_routed_is_a_finding(self):
        findings = policy.compare_scim_routes((), ("scim:Users/<uuid:pk>",))
        self.assertEqual([finding.rule for finding in findings], ["C-SCIM2"])
        self.assertIn("scim:Users/<uuid:pk>", findings[0].detail)


class WebhookSignatureDriftTests(unittest.TestCase):
    """The signed header is a contract, so it is compared rather than characterized."""

    def test_a_renamed_signature_header_is_a_finding(self):
        envelope = policy.WebhookEnvelope(
            ("event", "model", "object_id", "timestamp", "data"), "X-ITAMbox-Signature-256"
        )
        findings = policy.compare_webhook_envelope(envelope, ("event", "model", "object_id", "timestamp", "data"))
        self.assertEqual([finding.rule for finding in findings], ["C-HOOK2"])
        self.assertIn("X-Hub-Signature-256", findings[0].detail)

    def test_a_header_that_disappeared_entirely_is_a_finding(self):
        envelope = policy.WebhookEnvelope(("event", "model", "object_id", "timestamp", "data"), "")
        findings = policy.compare_webhook_envelope(envelope, ("event", "model", "object_id", "timestamp", "data"))
        self.assertEqual([finding.rule for finding in findings], ["C-HOOK2"])

    def test_the_signature_header_is_read_from_the_header_assignment_not_the_file_text(self):
        """Mutation probe: rename it in code, leave the old name in prose."""
        source = (REPO_ROOT / policy.WEBHOOK_TASK_MODULE).read_text(encoding="utf-8")
        mutated = source.replace(
            'req_headers["X-Hub-Signature-256"]',
            'req_headers["X-ITAMbox-Signature-256"]',
            1,
        )
        self.assertNotEqual(mutated, source, "the probe no longer matches the delivery task it mutates")
        mutated += '\n\n_HISTORY = """Deliveries were once signed with X-Hub-Signature-256."""\n'
        self.assertEqual(policy._signature_header(ast.parse(mutated)), "X-ITAMbox-Signature-256")
        self.assertEqual(policy._signature_header(ast.parse(source)), policy.WEBHOOK_SIGNATURE_HEADER)


class EnumRowFidelityTests(unittest.TestCase):
    """A published row set is a sequence, not a membership test."""

    def test_a_duplicated_published_row_is_a_finding(self):
        name = "extras.ScheduledReport.FREQUENCY_CHOICES"
        findings = policy.compare_enums({name: NINE_FREQUENCIES}, {name: NINE_FREQUENCIES + ("cron",)})
        self.assertEqual([finding.rule for finding in findings], ["C-ENUM1"])
        self.assertIn("duplicate", findings[0].detail.lower())

    def test_a_duplicated_row_in_an_open_enum_is_still_a_finding(self):
        name = "extras.ScheduledReport.FORMAT_CHOICES"
        findings = policy.compare_enums({name: ("html", "csv")}, {name: ("csv", "html", "html")})
        self.assertEqual([finding.rule for finding in findings], ["C-ENUM1"])

    def test_a_reordered_closed_enum_is_a_finding(self):
        name = "subscriptions.SubscriptionStatusChoices"
        findings = policy.compare_enums(
            {name: FOUR_SUBSCRIPTION_STATUSES}, {name: tuple(reversed(FOUR_SUBSCRIPTION_STATUSES))}
        )
        self.assertEqual([finding.rule for finding in findings], ["C-ENUM1"])
        self.assertIn("order", findings[0].detail.lower())

    def test_a_reordered_open_enum_is_not_a_finding(self):
        """Order is part of the promise only where the value set is frozen."""
        name = "extras.ScheduledReport.FORMAT_CHOICES"
        self.assertEqual(policy.compare_enums({name: ("html", "csv")}, {name: ("csv", "html")}), ())

    def test_a_reordered_duplicated_publication_cannot_read_as_the_source_set(self):
        """The checker's probe: ``(b, a, a, a)`` may not pass for ``(a, b)``."""
        name = "subscriptions.SubscriptionStatusChoices"
        findings = policy.compare_enums({name: ("a", "b")}, {name: ("b", "a", "a", "a")})
        self.assertIn("C-ENUM1", {finding.rule for finding in findings})


class WordingTests(unittest.TestCase):
    """No published sentence may read as an isolation or authorization escape."""

    def test_the_published_documents_contain_no_bypass_claim(self):
        self.assertEqual(policy.check_forbidden_wording(REPO_ROOT), ())

    def test_a_sentence_claiming_beta_skips_tenant_isolation_is_rejected(self):
        text = "A Beta capability bypasses tenant isolation while it is switched on."
        self.assertEqual([finding.rule for finding in policy.forbidden_wording_in(text, "example.md")], ["C-DOC2"])

    def test_a_sentence_claiming_a_superuser_ignores_the_tenant_boundary_is_rejected(self):
        text = "A superuser overrides the tenant boundary for support purposes."
        self.assertEqual([finding.rule for finding in policy.forbidden_wording_in(text, "example.md")], ["C-DOC2"])

    def test_a_sentence_claiming_a_hidden_view_skips_authorization_is_rejected(self):
        text = "The hidden UI skips authorization for internal support pages."
        self.assertEqual([finding.rule for finding in policy.forbidden_wording_in(text, "example.md")], ["C-DOC2"])

    def test_a_negated_sentence_is_not_a_finding(self):
        """The policy has to be able to state the rule it enforces."""
        for text in (
            "No Beta grade waives tenant isolation.",
            "A superuser never bypasses the tenant boundary.",
            "A hidden view does not skip authorization.",
            "Beta capabilities cannot bypass tenant scoping.",
        ):
            with self.subTest(sentence=text):
                self.assertEqual(policy.forbidden_wording_in(text, "example.md"), ())

    def test_an_ordinary_sentence_is_not_a_finding(self):
        text = "Stable surfaces receive a two-minor removal notice before any removal."
        self.assertEqual(policy.forbidden_wording_in(text, "example.md"), ())


class WordingNegationTests(unittest.TestCase):
    """Adversarial: a negation after the verb negates something else.

    Exempting a whole sentence because ``no``/``not``/``without`` occurs
    anywhere in it exempts nearly every real escape claim, because a real
    escape claim is usually qualified.
    """

    #: Verbatim from the checker review. Each contains a negation token and each
    #: is a published isolation or authorization escape.
    NEGATION_BYPASSES = (
        "A Beta capability bypasses tenant isolation when no explicit tenant is set.",
        "Superuser support tooling overrides authorization; not a bug, by design.",
        "Beta capabilities bypass tenant scoping for none of the read paths, but do for writes.",
        "Experimental plugins skip permission checks without exception.",
    )

    def test_a_negation_that_lands_after_the_verb_does_not_exempt_the_claim(self):
        for text in self.NEGATION_BYPASSES:
            with self.subTest(sentence=text):
                self.assertEqual([finding.rule for finding in policy.forbidden_wording_in(text, "x.md")], ["C-DOC2"])

    def test_the_sanctioned_negative_formulations_are_still_allowed(self):
        """The policy has to be able to state the rule it enforces."""
        for text in (
            "No Beta grade waives tenant isolation.",
            "A superuser never bypasses the tenant boundary.",
            "A hidden view does not skip authorization.",
            "Beta capabilities cannot bypass tenant scoping.",
            "No Beta capability is exempt from the tenant boundary.",
            "A superuser account is not exempt from tenant isolation.",
        ):
            with self.subTest(sentence=text):
                self.assertEqual(policy.forbidden_wording_in(text, "x.md"), ())

    def test_a_claim_wrapped_across_two_lines_is_still_read_as_one_sentence(self):
        """Published prose wraps at eighty columns; the claim does not."""
        text = "Support tooling is documented here. A Beta capability bypasses tenant\nisolation while it is on.\n"
        self.assertEqual([finding.rule for finding in policy.forbidden_wording_in(text, "x.md")], ["C-DOC2"])

    def test_a_claim_wrapped_inside_a_list_item_is_still_read_as_one_sentence(self):
        text = "- A superuser account overrides the tenant\n  boundary for support work.\n"
        self.assertEqual([finding.rule for finding in policy.forbidden_wording_in(text, "x.md")], ["C-DOC2"])

    def test_a_bolded_sentence_still_ends_where_its_full_stop_is(self):
        """Emphasis is presentation; it may not merge a rule into a claim."""
        text = "**No Beta grade waives tenant isolation.** Experimental plugins skip authorization.\n"
        findings = policy.forbidden_wording_in(text, "x.md")
        self.assertEqual([finding.rule for finding in findings], ["C-DOC2"])
        self.assertIn("Experimental plugins skip authorization.", findings[0].detail)


class WordingScopeTests(unittest.TestCase):
    """The rule covers exactly what the policy says it covers."""

    @classmethod
    def setUpClass(cls):
        cls.sources = policy.forbidden_wording_sources(REPO_ROOT)

    def test_the_policy_the_inventory_and_the_release_notes_are_all_checked(self):
        for relative in (policy.POLICY_DOC, policy.INVENTORY_DOC, policy.CHANGELOG_DOC):
            with self.subTest(document=relative):
                self.assertIn(relative, self.sources)

    def test_every_declared_capability_limitation_is_checked(self):
        limitation_sources = {label: text for label, text in self.sources.items() if label.endswith("limitations")}
        declared_with_limitations = [
            capability for capability in policy.derived_capabilities(REPO_ROOT) if capability.limitations
        ]
        self.assertEqual(len(limitation_sources), len(declared_with_limitations))
        # Webhooks graduated to Stable in the #566 promotion: a Stable capability
        # declares no limitations, so its former exclusions text left the checked
        # wording scope entirely.
        self.assertNotIn("itambox/extras/apps.py: automation.webhooks limitations", limitation_sources)
        # The Asset Request Procurement Seam graduated to Stable in the #569
        # promotion: a Stable capability declares no limitations, so its former
        # exclusions text left the checked wording scope entirely.
        self.assertNotIn("itambox/procurement/apps.py: procurement.requisition_seam limitations", limitation_sources)

        # Alert Rules and Channels graduated to Stable in the #567 promotion:
        # a Stable capability declares no limitations, so its former exclusions
        # text left the checked wording scope entirely.
        self.assertNotIn("itambox/extras/apps.py: alerting.rules limitations", limitation_sources)
        for capability in declared_with_limitations:
            with self.subTest(capability=capability.key):
                joined = " ".join(
                    text
                    for label, text in limitation_sources.items()
                    if label.endswith(f"{capability.key} limitations")
                )
                for limitation in capability.limitations:
                    self.assertIn(limitation, joined)

    def test_a_bypass_claim_written_into_a_capability_limitation_is_a_finding(self):
        """The drift hole C-DOC2 exists to close, in the one place it was open."""
        text = "Experimental plugins skip the tenant scope check while loading."
        self.assertEqual(
            [finding.rule for finding in policy.forbidden_wording_in(text, "itambox/x/apps.py")], ["C-DOC2"]
        )

    def test_the_whole_checked_corpus_is_clean_today(self):
        self.assertEqual(policy.check_forbidden_wording(REPO_ROOT), ())


if __name__ == "__main__":
    unittest.main()
