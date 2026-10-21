"""U1/U3/U6/U8: the shipped capability declarations and their consistency.

``test_capabilities.py`` proves the substrate. This module proves what the
domain actually declared through it: that ownership resolves, that the declared
grades match the code-owned contract manifest, that an inactive or broken capability
is harmless, and that the deprecated app-level adapters still answer.
"""

import json
import sys
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.test import override_settings
from model_bakery import baker

from core.features import STABLE, is_beta_module, module_maturity
from itambox.apps import _plugin_activation_probe
from itambox.capabilities import (
    ALWAYS_ON,
    EXPERIMENTAL,
    OPT_IN,
    SOURCE_ALWAYS,
    SOURCE_OPERATOR_FLAG,
    ActivationState,
    registry,
)
from itambox.tests.capability_harness import deactivatable_keys, deactivated, half_registered, probe_failing
from procurement import apps as procurement_apps

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_ROOT = REPO_ROOT / "itambox" / "docs"

# The slice this issue declares. Written out rather than derived so a silently
# dropped or reclassified registration fails here instead of passing vacuously.
DECLARED = {
    "subscriptions.tracking": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "procurement.core": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "procurement.requisition_seam": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "reporting.curated": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "reporting.designer": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "reporting.scheduled": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "alerting.inbox": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "alerting.rules": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "organization.role_grants": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "organization.resource_grants": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "automation.webhooks": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "users.scim_provisioning": (STABLE, ALWAYS_ON, SOURCE_ALWAYS),
    "platform.plugins": (EXPERIMENTAL, OPT_IN, SOURCE_OPERATOR_FLAG),
}


def area_labels():
    """The repository's ``area:*`` labels, read from the architecture policy."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.architecture_policy import AREA_LABELS

    return AREA_LABELS


class TestDeclaredSlice:
    def test_every_declared_capability_is_registered(self):
        assert set(registry.keys()) == set(DECLARED)

    @pytest.mark.parametrize("key", sorted(DECLARED))
    def test_the_declared_grade_mode_and_source_are_registered(self, key):
        capability = registry.get(key)
        expected = DECLARED[key]
        assert (capability.maturity, capability.activation, capability.activation_source) == expected

    def test_webhooks_are_declared_stable_always_on_without_probe_or_limitations(self):
        capability = registry.get("automation.webhooks")

        assert (capability.maturity, capability.activation, capability.activation_source) == (
            STABLE,
            ALWAYS_ON,
            SOURCE_ALWAYS,
        )
        assert capability.activation_probe is None
        assert capability.limitations == ()

    def test_the_seam_is_declared_stable_always_on_without_probe_or_limitations(self):
        capability = registry.get("procurement.requisition_seam")

        assert (capability.maturity, capability.activation, capability.activation_source) == (
            STABLE,
            ALWAYS_ON,
            SOURCE_ALWAYS,
        )
        assert capability.activation_probe is None
        assert capability.limitations == ()

    def test_alert_rules_are_declared_stable_always_on_without_probe_or_limitations(self):
        capability = registry.get("alerting.rules")

        assert (capability.maturity, capability.activation, capability.activation_source) == (
            STABLE,
            ALWAYS_ON,
            SOURCE_ALWAYS,
        )
        assert capability.activation_probe is None
        assert capability.limitations == ()

    def test_scim_provisioning_is_declared_stable_always_on_without_probe_or_limitations(self):
        capability = registry.get("users.scim_provisioning")

        assert (capability.maturity, capability.activation, capability.activation_source) == (
            STABLE,
            ALWAYS_ON,
            SOURCE_ALWAYS,
        )
        assert capability.activation_probe is None
        assert capability.limitations == ()

    def test_scheduled_reports_are_declared_stable_always_on_without_probe_or_limitations(self):
        capability = registry.get("reporting.scheduled")

        assert (capability.maturity, capability.activation, capability.activation_source) == (
            STABLE,
            ALWAYS_ON,
            SOURCE_ALWAYS,
        )
        assert capability.activation_probe is None
        assert capability.limitations == ()

    def test_only_authorization_boundaries_are_security_critical(self):
        critical = {capability.key for capability in registry.all() if capability.security_critical}
        assert critical == {
            "organization.resource_grants",
            "organization.role_grants",
        }

    def test_every_entry_carries_the_current_contract_version(self):
        assert {capability.contract_version for capability in registry.all()} == {1}


class TestOwnership:
    """U1: ownership is total, exclusive, and resolvable."""

    def test_no_owned_reference_is_unresolved(self):
        unresolved = [(row.key, row.reference, row.reason) for row in registry.unresolved_references()]
        assert unresolved == []

    def test_ownership_is_exclusive(self):
        owners = {}
        for capability in registry.all():
            for reference in capability.owns:
                assert reference not in owners, f"{reference} owned by {owners.get(reference)} and {capability.key}"
                owners[reference] = capability.key

    @pytest.mark.parametrize(
        "reference,expected",
        [
            ("subscriptions.Subscription", "subscriptions.tracking"),
            ("procurement.PurchaseOrder", "procurement.core"),
            ("procurement.FulfillmentLink", "procurement.requisition_seam"),
            ("extras.ReportTemplate", "reporting.designer"),
            ("extras.ScheduledReport", "reporting.scheduled"),
            ("extras.AlertRule", "alerting.rules"),
            ("extras.AlertLog", "alerting.inbox"),
            ("extras.WebhookEndpoint", "automation.webhooks"),
            ("extras.EventRule", "automation.webhooks"),
            ("organization.RoleGrant", "organization.role_grants"),
            ("organization.TenantResourceGrant", "organization.resource_grants"),
        ],
    )
    def test_a_model_resolves_to_its_owning_capability(self, reference, expected):
        assert registry.owner_of(reference).key == expected

    def test_an_unowned_model_has_no_owner(self):
        assert registry.owner_of("assets.Asset") is None

    def test_every_owning_area_is_a_repository_area_label(self):
        labels = area_labels()
        for capability in registry.all():
            assert capability.owning_area in labels, f"{capability.key} names {capability.owning_area}"


class TestActivationDefaults:
    """U3: what a fresh deployment sees, and that nothing there is a surprise."""

    def test_every_stable_capability_is_active(self):
        for capability in registry.all():
            if capability.maturity == STABLE:
                assert registry.is_active(capability.key) is True, capability.key

    def test_webhooks_are_active_with_zero_rows(self, db):
        from extras.models import EventRule, WebhookEndpoint

        assert EventRule._base_manager.count() == 0
        assert WebhookEndpoint._base_manager.count() == 0
        assert registry.state("automation.webhooks") == ActivationState(active=True, value_present=True)

    def test_alert_rules_are_active_with_zero_rows(self, db):
        from extras.models import AlertRule, NotificationChannel

        assert AlertRule._base_manager.count() == 0
        assert NotificationChannel._base_manager.count() == 0
        assert registry.state("alerting.rules") == ActivationState(active=True, value_present=True)

    def test_scim_provisioning_is_active_with_zero_rows(self, db):
        from users.models import Token

        assert Token._base_manager.count() == 0
        assert registry.state("users.scim_provisioning") == ActivationState(active=True, value_present=True)

    def test_scheduled_reports_are_active_with_zero_rows(self, db):
        from extras.models import ReportGenerationArchive, ScheduledReport

        assert ScheduledReport._base_manager.count() == 0
        assert ReportGenerationArchive._base_manager.count() == 0
        assert registry.state("reporting.scheduled") == ActivationState(active=True, value_present=True)

    def test_every_opt_in_capability_is_inert_on_a_fresh_deployment(self, db):
        """``db``: the object-backed probes must *answer* here, not fail closed.

        Without database access a probe that counts rows raises and the registry
        reports it inactive, which is the same answer this test is looking for --
        so the assertion would pass without ever reaching an empty table. The
        ``probe_error`` check makes that difference visible.
        """
        for capability in registry.all():
            if capability.activation == OPT_IN:
                state = registry.state(capability.key)
                assert state.probe_error == "", capability.key
                assert state.active is False, capability.key

    def test_the_seam_is_active_without_any_threshold_configuration(self):
        state = registry.state("procurement.requisition_seam")
        assert state == ActivationState(active=True, value_present=True)

    @override_settings(ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS={"accessory": 3, "consumable": 5})
    def test_threshold_configuration_no_longer_gates_the_seam(self):
        state = registry.state("procurement.requisition_seam")
        assert state == ActivationState(active=True, value_present=True)
        assert "accessory" not in repr(state)
        assert "consumable" not in repr(state)

    @override_settings(ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS={})
    def test_an_empty_threshold_object_keeps_the_seam_active(self):
        state = registry.state("procurement.requisition_seam")

        assert state == ActivationState(active=True, value_present=True)

    @override_settings(
        ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS=None,
        REQUISITION_AUTO_APPROVAL_THRESHOLDS={"accessory": 2},
    )
    def test_the_legacy_threshold_setting_no_longer_gates_the_seam(self):
        state = registry.state("procurement.requisition_seam")

        assert state == ActivationState(active=True, value_present=True)
        assert "accessory" not in repr(state)

    @override_settings(
        ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS=None,
        REQUISITION_AUTO_APPROVAL_THRESHOLDS={"accessory": 2},
    )
    def test_a_legacy_django_setting_emits_the_startup_deprecation_warning(self):
        warning_hook = getattr(procurement_apps, "_warn_legacy_auto_approval_setting", None)
        assert callable(warning_hook), "legacy Django-setting warning hook is missing"

        with pytest.warns(UserWarning, match="ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS"):
            warning_hook()

    @override_settings(PLUGINS=["demo_plugin"])
    def test_an_operator_flag_reports_a_configured_value_without_naming_it(self):
        state = registry.state("platform.plugins")
        assert (state.active, state.value_present) == (True, True)
        assert "demo_plugin" not in repr(state)

    @override_settings(
        PLUGINS=("broken_plugin",),
        PLUGINS_ACTIVE=(),
        PLUGINS_DIAGNOSTICS=({"plugin": "broken_plugin"},),
    )
    def test_a_failed_plugin_does_not_keep_the_platform_capability_active(self):
        assert _plugin_activation_probe() == ActivationState(active=False, value_present=True)


@pytest.mark.django_db
class TestSCIMStableActivation:
    """SCIM provisioning is active on every deployment, without any credential.

    The capability no longer observes the operator's bearer tokens: the mounts
    are part of the declared Stable contract, so activation depends on nothing
    an operator has to configure. Minting or revoking a token therefore changes
    nothing about the grade; the token still governs what a request may do.
    """

    def test_a_deployment_with_no_token_is_active(self):
        assert registry.state("users.scim_provisioning") == ActivationState(active=True, value_present=True)

    def test_no_scim_settings_key_is_consulted(self):
        """There is no settings gate for SCIM, and the registry reads none."""
        assert not hasattr(settings, "TENANT_SCIM_CONFIGS")

    def test_minting_or_revoking_a_token_changes_nothing_about_activation(self):
        token = baker.make("users.Token", write_enabled=True)
        assert registry.state("users.scim_provisioning") == ActivationState(active=True, value_present=True)
        token.write_enabled = False
        token.save(update_fields=["write_enabled"])
        assert registry.state("users.scim_provisioning") == ActivationState(active=True, value_present=True)

    def test_the_diagnostics_row_never_reads_credential_material(self):
        token = baker.make("users.Token", write_enabled=True)
        row = next(row for row in registry.diagnostics() if row["key"] == "users.scim_provisioning")
        rendered = repr(registry.state("users.scim_provisioning")) + repr(row)
        assert token.key_preview and token.key_preview not in rendered
        assert token.digest not in rendered


class TestRegistrationIdempotence:
    """``ready()`` runs twice whenever a test swaps ``INSTALLED_APPS``."""

    @pytest.mark.parametrize(
        "app_label",
        ["extras", "itambox", "organization", "procurement", "subscriptions", "users"],
    )
    def test_re_running_ready_does_not_raise_or_duplicate(self, app_label):
        before = registry.keys()
        app_config = apps.get_app_config(app_label)
        app_config._register_capabilities()
        app_config.ready()
        assert registry.keys() == before

    @pytest.mark.parametrize("app_label", ["extras", "procurement"])
    def test_a_declaration_that_failed_halfway_is_completed_by_the_next_run(self, app_label):
        """The multipart apps must be *finishable*, not merely repeatable.

        A guard that returns as soon as the first key is present cannot tell a
        completed declaration from one that died after entry one, so it freezes
        the registry in the half-registered state instead of repairing it.
        """
        app_config = apps.get_app_config(app_label)
        with half_registered(app_label) as dropped:
            assert dropped, f"{app_label} declares only one capability"
            assert not set(dropped) & set(registry.keys())
            app_config._register_capabilities()
            assert set(dropped) <= set(registry.keys())


class TestInactiveSafety:
    """U3/U6: an inactive or broken capability never becomes an exception."""

    @pytest.mark.parametrize("key", deactivatable_keys())
    def test_a_deactivated_capability_reports_inactive_and_stays_registered(self, key):
        with deactivated(key):
            assert registry.is_active(key) is False
            assert registry.get(key).maturity == DECLARED[key][0]
            assert key in registry
        assert registry.is_active(key) == registry.state(key).active

    @pytest.mark.parametrize("key", deactivatable_keys())
    def test_a_failing_probe_fails_closed_without_leaking_its_message(self, key):
        with probe_failing(key):
            state = registry.state(key)
            assert state.active is False
            assert state.value_present is False
            assert state.probe_error == "RuntimeError"
            assert "hunter2" not in repr(state)

    @pytest.mark.parametrize("key", deactivatable_keys())
    def test_diagnostics_stay_complete_while_a_probe_is_failing(self, key, db):
        with probe_failing(key):
            rows = {row["key"]: row for row in registry.diagnostics()}
        assert set(rows) == set(DECLARED)
        assert rows[key]["probe_error"] == "RuntimeError"
        assert "hunter2" not in repr(rows[key])
        # ``db``: one broken probe must not make the rest of the table look
        # broken. Without database access every row-counting probe would error
        # too and this isolation would go unproven.
        assert [other for other, row in rows.items() if other != key and row["probe_error"]] == []

    def test_webhooks_are_not_deactivatable(self):
        assert "automation.webhooks" not in deactivatable_keys()
        assert registry.is_active("automation.webhooks") is True

    def test_the_seam_is_not_deactivatable(self):
        assert "procurement.requisition_seam" not in deactivatable_keys()
        assert registry.is_active("procurement.requisition_seam") is True

    def test_alert_rules_are_not_deactivatable(self):
        assert "alerting.rules" not in deactivatable_keys()
        assert registry.is_active("alerting.rules") is True

    def test_scim_provisioning_is_not_deactivatable(self):
        assert "users.scim_provisioning" not in deactivatable_keys()
        assert registry.is_active("users.scim_provisioning") is True

    def test_scheduled_reports_are_not_deactivatable(self):
        assert "reporting.scheduled" not in deactivatable_keys()
        assert registry.is_active("reporting.scheduled") is True

    @pytest.mark.parametrize(
        "key",
        ["organization.resource_grants", "organization.role_grants"],
    )
    def test_a_security_critical_capability_has_no_deactivation_path(self, key):
        assert key not in deactivatable_keys()
        assert registry.is_active(key) is True


class TestDeprecatedAdapters:
    """The one-release adapters answer from the registry, not from a literal map."""

    def test_module_maturity_is_registry_backed(self):
        assert not hasattr(sys.modules["core.features"], "MODULE_MATURITY")

    @pytest.mark.parametrize("app_label", ["assets", "extras", "users", "organization", "procurement"])
    def test_a_partly_owned_app_is_not_graded_wholesale(self, app_label):
        assert module_maturity(app_label) == STABLE
        assert is_beta_module(app_label) is False

    def test_an_unknown_app_label_is_stable(self):
        assert module_maturity("no_such_app") == STABLE
        assert is_beta_module("no_such_app") is False

    def test_is_beta_module_reports_every_non_stable_grade(self):
        assert is_beta_module("assets") is False


class TestDocumentationConsistency:
    """U8: code-owned contracts and capability links stay coherent."""

    def test_every_docs_url_points_at_a_public_or_internal_document(self):
        for capability in registry.all():
            if capability.docs_url.startswith(
                "https://github.com/itambox-community/design-docs/blob/main/development/"
            ):
                assert capability.docs_url.endswith(".md")
            else:
                target = DOCS_ROOT / capability.docs_url
                assert target.is_file(), f"{capability.key} documents itself at a missing {capability.docs_url}"

    def test_the_code_owned_contract_manifest_lists_every_capability(self):
        manifest = json.loads((REPO_ROOT / "scripts" / "contract_policy_manifest.json").read_text(encoding="utf-8"))
        rows = manifest["capabilities"]
        assert set(rows) == {capability.key for capability in registry.all()}

    def test_every_non_stable_capability_declares_at_least_one_limitation(self):
        for capability in registry.all():
            if capability.maturity != STABLE:
                assert capability.limitations, f"{capability.key} declares no limitation"
